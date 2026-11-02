#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""rettrix: bridge between a Matrix room and a Reticulum group chat (fwdsvc).

https://github.com/thatSFguy/reticulum-group-chat runs a group chat as an
LXMF endpoint: members send /join, and every message of a member is
forwarded to all others as "[nick] text". rettrix joins such a group like
any member, with its own LXMF identity and nick (e.g. "matrix"):

  group -> Matrix   "[alice] hi" from the group is posted to the room as is.
  Matrix -> group   "bob: hi" is sent to the group, members see
                    "[matrix] bob: hi".

No loops: fwdsvc never sends a member's own messages back to it, and
rettrix ignores the messages of its own Matrix account. Other bridges
posting to the same room (e.g. one for MeshCore) can be chained up: give
rettrix its own account, and list them as relay_bots so their messages are
shown with a label of their network instead of the bot's name.

fwdsvc specifics handled here (from its source, v1.17):
- /join replays recent history as "[replay 5m] [nick] text": not posted.
- Members silent for weeks are pruned; every message from a non-member is
  answered with "Welcome. To join this chat send /join ..." and dropped.
  rettrix then joins again and resends as many of its last messages as
  invites came back.
- On /join, fwdsvc takes the nick from the announced display name; the
  configured nick is set right after joining.
- Messages longer than 500 characters are rejected: split here.
- Command replies ("Joined. ...", "Nickname set to ...") are only logged.

Reticulum: rettrix uses the shared instance of the local rnsd (run it as
the same user, or set reticulum.configdir to rnsd's config directory).

Usage: rettrix.py -c rettrix.toml     (see rettrix.example.toml)
Requires Python 3.11+, rns, lxmf, matrix-nio.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import threading
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

log = logging.getLogger("rettrix")

# fwdsvc's default max_inbound_chars; longer messages are rejected
MAX_GROUP_CHARS = 500
# A forwarded group message: "[nick] text" (nick as fwdsvc allows it)
FORWARDED_RE = re.compile(r"\[([A-Za-z0-9_-]{1,24})\] (.*)", re.S)
REPLAY_RE = re.compile(r"\[replay \d+[smhd]\] ")
NICK_RE = re.compile(r"[A-Za-z0-9_-]{1,24}")
INVITE_PREFIX = "Welcome. To join this chat send /join"
JOINED_PREFIXES = ("Joined.", "You're already in the chat")
# Only messages sent this recently are resent after rejoining
RESEND_WINDOW = 300.0
# Seconds to wait for the delivery of a message before sending the next
DELIVERY_TIMEOUT = 60.0
# Seconds between announces (fwdsvc prunes members unheard of for weeks)
ANNOUNCE_INTERVAL = 6 * 3600


# --- Configuration ----------------------------------------------------------------

@dataclass
class RelayBot:
    """A Matrix bot posting for another network (e.g. a MeshCore bridge)."""
    prefix: str  # stripped from its messages, e.g. "[McBridge] "
    label: str   # shown instead of its name, e.g. "MeshCore"


@dataclass
class Config:
    homeserver: str
    user: str
    password: str
    room: str
    group: bytes
    nick: str = "matrix"
    display_name: str = "Matrix bridge"
    min_interval: float = 2.0
    storage: str = "~/.rettrix"
    reticulum_configdir: Optional[str] = None
    relay_bots: Dict[str, RelayBot] = field(default_factory=dict)


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    matrix, group, ret = raw.get("matrix", {}), raw.get("group", {}), raw.get("reticulum", {})

    password = os.environ.get("RETTRIX_MATRIX_PASSWORD")
    if not password and matrix.get("password_file"):
        with open(os.path.expanduser(matrix["password_file"])) as f:
            password = f.read().strip()
    if not password:
        raise SystemExit("Matrix password missing: set RETTRIX_MATRIX_PASSWORD or matrix.password_file")

    address = str(group.get("address", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", address):
        raise SystemExit("group.address must be the 32 hex character LXMF address of the group chat")
    nick = group.get("nick", "matrix")
    if not NICK_RE.fullmatch(nick):
        raise SystemExit("group.nick: 1-24 characters from A-Z a-z 0-9 _ - (fwdsvc rule)")

    bots = {user: RelayBot(prefix=b.get("prefix", ""), label=b["label"])
            for user, b in matrix.get("relay_bots", {}).items()}
    return Config(
        homeserver=matrix["homeserver"], user=matrix["user"], password=password, room=matrix["room"],
        group=bytes.fromhex(address), nick=nick,
        display_name=ret.get("display_name", "Matrix bridge"),
        min_interval=float(group.get("min_interval", 2.0)),
        storage=ret.get("storage", "~/.rettrix"),
        reticulum_configdir=ret.get("configdir"),
        relay_bots=bots,
    )


# --- Message conversion -------------------------------------------------------------

def classify_group_message(text: str) -> str:
    """Kind of a message from the group service: chat, replay, invite, joined or service."""
    if REPLAY_RE.match(text):
        return "replay"
    if FORWARDED_RE.fullmatch(text):
        return "chat"
    if text.startswith(INVITE_PREFIX):
        return "invite"
    if text.startswith(JOINED_PREFIXES):
        return "joined"
    return "service"


def strip_reply_fallback(body: str) -> str:
    """Drop the quoted "> <@user> ..." lines Matrix clients put above a reply."""
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    if i and i < len(lines) and lines[i] == "":
        return "\n".join(lines[i + 1:])
    return body


def split_text(prefix: str, text: str, limit: int = MAX_GROUP_CHARS) -> List[str]:
    """``prefix + text`` in messages of at most ``limit`` characters, split at spaces if possible."""
    room = max(20, limit - len(prefix))
    parts, rest = [], text.strip()
    while len(rest) > room:
        cut = rest.rfind(" ", room // 2, room)
        cut = cut if cut > 0 else room
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest or not parts:
        parts.append(rest)
    return [prefix + p for p in parts]


def matrix_to_group(cfg: Config, sender: str, name: str, body: str,
                    emote: bool = False, edit: bool = False, reply: bool = False) -> List[str]:
    """Texts sent to the group for a Matrix message."""
    if reply:
        body = strip_reply_fallback(body)
    bot = cfg.relay_bots.get(sender)
    if bot is not None:
        if bot.prefix and body.startswith(bot.prefix):
            body = body[len(bot.prefix):]
        name = bot.label
    body = body.strip()
    if not body:
        return []
    if emote:
        prefix = f"* {name} "
    else:
        prefix = f"{name}{' (edited)' if edit else ''}: "
    return split_text(prefix, body)


# --- Group protocol (no network) ---------------------------------------------------

class GroupSession:
    """What to send to the group service and what to post to Matrix.

    ``send(text)`` queues a message to the group, ``post(text)`` posts to
    Matrix. Commands go ahead of queued chat (``send(text, urgent=True)``),
    in the order given.
    """

    def __init__(self, nick: str, send: Callable[..., None], post: Callable[[str], None],
                 clock: Callable[[], float] = time.monotonic):
        self.nick = nick
        self._send = send
        self._post = post
        self._clock = clock
        self._recent: Deque[Tuple[float, str]] = deque(maxlen=20)
        self._dropped = 0  # invites received: each one is a dropped message
        self._rejoining = False

    def start(self):
        # Idempotent: an existing member gets "You're already in the chat"
        self._send("/join", urgent=True)
        self._send(f"/nick {self.nick}", urgent=True)

    def chat(self, text: str):
        self._recent.append((self._clock(), text))
        self._send(text)

    def received(self, text: str):
        kind = classify_group_message(text)
        if kind == "chat":
            self._post(text)
        elif kind == "invite":
            self._dropped += 1
            if not self._rejoining:
                self._rejoining = True
                log.warning("Not a member of the group (anymore), joining again")
                self._send("/join", urgent=True)
        elif kind == "joined" and self._rejoining:
            self._rejoining = False
            self._send(f"/nick {self.nick}", urgent=True)
            now = self._clock()
            recent = [t for at, t in self._recent if now - at <= RESEND_WINDOW]
            resend = recent[-self._dropped:] if self._dropped else []
            for t in resend:
                self._send(t)
            log.info("Rejoined the group, resending %d dropped message(s)", len(resend))
            self._dropped = 0
        else:
            log.info("Group service (%s): %s", kind, text[:200])


# --- Reticulum / LXMF ---------------------------------------------------------------

class GroupLink:
    """LXMF endpoint of the bridge; sends at most every ``min_interval`` seconds."""

    def __init__(self, cfg: Config, on_text: Callable[[str], None]):
        self.cfg = cfg
        self._on_text = on_text
        self._urgent: Deque[str] = deque()
        self._queue: Deque[str] = deque()
        self._cond = threading.Condition()
        self._stop = False

    def start(self):
        import LXMF
        import RNS
        self.RNS, self.LXMF = RNS, LXMF
        storage = os.path.expanduser(self.cfg.storage)
        os.makedirs(storage, mode=0o700, exist_ok=True)
        self.rns = RNS.Reticulum(configdir=self.cfg.reticulum_configdir, loglevel=RNS.LOG_WARNING)
        id_path = os.path.join(storage, "identity")
        if os.path.isfile(id_path):
            self.identity = RNS.Identity.from_file(id_path)
        else:
            self.identity = RNS.Identity()
            self.identity.to_file(id_path)
            os.chmod(id_path, 0o600)
        self.router = LXMF.LXMRouter(identity=self.identity, storagepath=os.path.join(storage, "lxmf"))
        self.dest = self.router.register_delivery_identity(self.identity, display_name=self.cfg.display_name)
        self.router.register_delivery_callback(self._on_lxmf)
        log.info("Bridge LXMF address: %s, group: %s", self.dest.hash.hex(), self.cfg.group.hex())
        if not RNS.Transport.has_path(self.cfg.group):
            RNS.Transport.request_path(self.cfg.group)
        threading.Thread(target=self._sender, daemon=True, name="rettrix-send").start()
        threading.Thread(target=self._announcer, daemon=True, name="rettrix-announce").start()

    def send(self, text: str, urgent: bool = False):
        with self._cond:
            (self._urgent if urgent else self._queue).append(text)
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify()

    def _announcer(self):
        while not self._stop:
            self.router.announce(self.dest.hash)
            time.sleep(ANNOUNCE_INTERVAL)

    def _group_destination(self):
        identity = self.RNS.Identity.recall(self.cfg.group)
        if identity is None:
            return None
        return self.RNS.Destination(identity, self.RNS.Destination.OUT, self.RNS.Destination.SINGLE,
                                    "lxmf", "delivery")

    def _sender(self):
        last_request = 0.0
        while True:
            with self._cond:
                while not (self._urgent or self._queue) and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
            dest = self._group_destination()
            if dest is None:
                # The group's identity is learned from its announce or a path response
                if time.monotonic() - last_request > 30:
                    log.info("Waiting for the group's announce (path requested)")
                    self.RNS.Transport.request_path(self.cfg.group)
                    last_request = time.monotonic()
                time.sleep(2)
                continue
            with self._cond:
                text = (self._urgent or self._queue).popleft()
            self._deliver(dest, text)
            time.sleep(self.cfg.min_interval)

    def _deliver(self, dest, text: str):
        """Send one message and wait for its delivery (or failure), so the
        group gets them in order; LXMF doesn't keep the order of messages
        in flight at the same time."""
        done = threading.Event()
        lxm = self.LXMF.LXMessage(dest, self.dest, text, desired_method=self.LXMF.LXMessage.DIRECT)
        lxm.register_delivery_callback(lambda m: done.set())

        def failed(m):
            log.warning("Delivery to the group failed: %.60s", m.content_as_string())
            done.set()

        lxm.register_failed_callback(failed)
        self.router.handle_outbound(lxm)
        if not done.wait(DELIVERY_TIMEOUT):
            log.warning("No delivery confirmation within %d s: %.60s", DELIVERY_TIMEOUT, text)

    def _on_lxmf(self, message):
        """Message received (Reticulum thread)."""
        if message.source_hash != self.cfg.group:
            log.info("Ignored message from %s (not the group)", message.source_hash.hex())
            return
        if not message.signature_validated:
            # Without the group's identity the sender can't be verified;
            # anyone could claim to be the group.
            log.warning("Ignored unverified message claiming to come from the group")
            self.RNS.Transport.request_path(self.cfg.group)
            return
        text = message.content_as_string() or ""
        if text:
            self._on_text(text)


# --- Matrix --------------------------------------------------------------------------

class MatrixLink:
    def __init__(self, cfg: Config, on_message: Callable[..., None]):
        self.cfg = cfg
        self._on_message = on_message
        self.client = None

    async def run(self):
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Matrix: %s, reconnecting in 30 s", e)
            if self.client is not None:
                await self.client.close()
            await asyncio.sleep(30)

    async def _run_once(self):
        from nio import AsyncClient, LoginResponse, RoomMessageEmote, RoomMessageText
        self.client = AsyncClient(self.cfg.homeserver, self.cfg.user)
        resp = await self.client.login(self.cfg.password, device_name="rettrix")
        if not isinstance(resp, LoginResponse):
            raise RuntimeError(f"login failed: {resp}")
        await self.client.join(self.cfg.room)
        # Skip the room history: only messages from now on are bridged
        await self.client.sync(timeout=0, full_state=True)
        self._since_ms = int(time.time() * 1000)
        self.client.add_event_callback(self._on_event, (RoomMessageText, RoomMessageEmote))
        log.info("Matrix: logged in as %s, bridging %s", self.cfg.user, self.cfg.room)
        await self.client.sync_forever(timeout=30000)

    async def _on_event(self, room, event):
        from nio import RoomMessageEmote
        if room.room_id != self.cfg.room or event.sender == self.client.user_id:
            return
        if event.server_timestamp < self._since_ms:
            return
        content = event.source.get("content", {})
        relation = content.get("m.relates_to") or {}
        body = event.body
        edit = relation.get("rel_type") == "m.replace"
        if edit:
            body = (content.get("m.new_content") or {}).get("body", body)
        name = room.user_name(event.sender) or event.sender.split(":")[0].lstrip("@")
        self._on_message(event.sender, name, body, emote=isinstance(event, RoomMessageEmote),
                         edit=edit, reply="m.in_reply_to" in relation)

    async def post(self, text: str):
        if self.client is None:
            return
        try:
            await self.client.room_send(self.cfg.room, "m.room.message", {"msgtype": "m.text", "body": text})
        except Exception as e:
            log.error("Matrix: posting failed: %s", e)


# --- Wiring ------------------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg: Config, matrix: Optional[MatrixLink] = None, group: Optional[GroupLink] = None):
        self.cfg = cfg
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.matrix = matrix or MatrixLink(cfg, self.from_matrix)
        self.group = group or GroupLink(cfg, self.from_group)
        self.session = GroupSession(cfg.nick, self.group.send, self._post_threadsafe)

    def from_group(self, text: str):
        """Reticulum thread."""
        self.session.received(text)

    def from_matrix(self, sender: str, name: str, body: str, **kind):
        """Matrix event loop."""
        for text in matrix_to_group(self.cfg, sender, name, body, **kind):
            self.session.chat(text)

    def _post_threadsafe(self, text: str):
        asyncio.run_coroutine_threadsafe(self.matrix.post(text), self.loop)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.group.start()
        self.session.start()
        await self.matrix.run()


def main():
    parser = argparse.ArgumentParser(description="Bridge a Matrix room and a Reticulum group chat (fwdsvc)")
    parser.add_argument("-c", "--config", default="rettrix.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # matrix-nio logs every room event at INFO
    logging.getLogger("nio").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    cfg = load_config(args.config)
    try:
        asyncio.run(Bridge(cfg).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
