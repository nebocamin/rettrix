# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for rettrix without network: message conversion and the fwdsvc protocol."""

import pytest

import rettrix
from rettrix import Config, GroupSession, RelayBot, classify_group_message, matrix_to_group, split_text

CFG = Config(homeserver="https://example.org", user="@rettrix:example.org", password="x",
             room="!room:example.org", group=bytes(16),
             relay_bots={"@mcbridge:example.org": RelayBot(prefix="[McBridge] ", label="MeshCore")})


# --- Messages from the group service (texts as in fwdsvc's source) --------------------------

@pytest.mark.parametrize("text, kind", [
    ("[alice] hallo", "chat"),
    ("[a_b-9] mehr\nZeilen", "chat"),
    ("[replay 5m] [alice] hallo", "replay"),
    ("[replay 3d] [bob] alt", "replay"),
    ("Welcome. To join this chat send /join. Send /? for help. Until you join, your messages aren't forwarded.",
     "invite"),
    ("Joined. You'll receive forwarded messages from now on. /pause to mute, /leave to exit, /? for help.",
     "joined"),
    ("You're already in the chat. Send /pause to mute, /leave to exit.", "joined"),
    ("Nickname set to matrix.", "service"),
    ("Message rejected: limit is 500 characters per message, yours was 612. Please shorten and resend.",
     "service"),
    ("[not a nick!] text", "service"),
])
def test_classify(text, kind):
    assert classify_group_message(text) == kind


# --- Matrix -> group ---------------------------------------------------------------------

def test_plain_message():
    assert matrix_to_group(CFG, "@bob:example.org", "bob", "hallo") == ["bob: hallo"]


def test_meshcore_messages_via_relay_bot():
    assert matrix_to_group(CFG, "@mcbridge:example.org", "mcbridge", "[McBridge] alice: moin") == \
        ["MeshCore: alice: moin"]


def test_reply_fallback_is_dropped():
    body = "> <@alice:example.org> Wie gehts?\n> zweite Zeile\n\nGut, danke"
    assert matrix_to_group(CFG, "@bob:example.org", "bob", body, reply=True) == ["bob: Gut, danke"]


def test_emote_and_edit():
    assert matrix_to_group(CFG, "@bob:example.org", "bob", "winkt", emote=True) == ["* bob winkt"]
    assert matrix_to_group(CFG, "@bob:example.org", "bob", "correct", edit=True) == ["bob (edited): correct"]


def test_empty_is_not_sent():
    assert matrix_to_group(CFG, "@bob:example.org", "bob", "   ") == []
    assert matrix_to_group(CFG, "@mcbridge:example.org", "mcbridge", "[McBridge] ") == []


def test_long_messages_are_split_within_the_limit():
    words = " ".join(f"wort{i}" for i in range(300))
    parts = split_text("bob: ", words)
    assert len(parts) > 1
    assert all(len(p) <= rettrix.MAX_GROUP_CHARS and p.startswith("bob: ") for p in parts)
    assert " ".join(p[len("bob: "):] for p in parts) == words
    # No spaces: cut hard
    assert all(len(p) <= rettrix.MAX_GROUP_CHARS for p in split_text("x: ", "a" * 1200))


# --- Protocol with fwdsvc ------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.now = 1000.0
        self.sent, self.posted = [], []

    def send(self, text, urgent=False):
        self.sent.append(("!" if urgent else "") + text)


@pytest.fixture
def session():
    r = Recorder()
    s = GroupSession("matrix", r.send, r.posted.append, clock=lambda: r.now)
    return s, r


def test_start_joins_and_sets_nick(session):
    s, r = session
    s.start()
    assert r.sent == ["!/join", "!/nick matrix"]


def test_chat_is_posted_replay_and_service_replies_are_not(session):
    s, r = session
    s.received("[alice] hallo")
    s.received("[replay 5m] [alice] alt")
    s.received("Nickname set to matrix.")
    s.received("You're already in the chat. Send /pause to mute, /leave to exit.")
    assert r.posted == ["[alice] hallo"]


def test_rejoins_and_resends_after_being_pruned(session):
    s, r = session
    s.chat("bob: vor langer Zeit")
    r.now += 1000
    s.chat("bob: eins")
    r.now += 5
    s.chat("bob: zwei")
    r.sent.clear()
    # fwdsvc answers each dropped message with the invite
    s.received("Welcome. To join this chat send /join. Send /? for help.")
    s.received("Welcome. To join this chat send /join. Send /? for help.")
    assert r.sent == ["!/join"]  # once
    s.received("Joined. You'll receive forwarded messages from now on.")
    assert r.sent == ["!/join", "!/nick matrix", "bob: eins", "bob: zwei"]
    # A later "already in the chat" doesn't resend anything
    s.received("You're already in the chat. Send /pause to mute, /leave to exit.")
    assert len(r.sent) == 4


def test_config_example_loads(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("RETTRIX_MATRIX_PASSWORD", "secret")
    cfg = rettrix.load_config(os.path.join(os.path.dirname(__file__), "rettrix.example.toml"))
    assert cfg.group.hex() == "00112233445566778899aabbccddeeff"
    assert cfg.room == "!AbCdEfGhIjKlMnOpQr:example.org"
    assert cfg.relay_bots["@mcbridge:example.org"] == RelayBot("[McBridge] ", "MeshCore")
    assert cfg.password == "secret"


def test_only_dropped_messages_are_resent(session):
    """Messages delivered before the removal must not be sent twice."""
    s, r = session
    s.chat("bob: zugestellt 1")
    s.chat("bob: zugestellt 2")
    s.chat("bob: verworfen")
    r.sent.clear()
    s.received("Welcome. To join this chat send /join. Send /? for help.")  # one dropped message
    s.received("Joined. You'll receive forwarded messages from now on.")
    assert r.sent == ["!/join", "!/nick matrix", "bob: verworfen"]


def test_urgent_commands_keep_their_order():
    link = rettrix.GroupLink(CFG, lambda text: None)
    link.send("bob: chat")
    link.send("/join", urgent=True)
    link.send("/nick matrix", urgent=True)
    order = list(link._urgent) + list(link._queue)
    assert order == ["/join", "/nick matrix", "bob: chat"]


# --- Matrix events (real matrix-nio classes, no server) --------------------------------------

try:
    import nio
except ImportError:  # the tests above need no Matrix library
    nio = None

needs_nio = pytest.mark.skipif(nio is None, reason="matrix-nio not installed")


def _event(body, sender="@bob:example.org", ts=2_000_000_000_000, msgtype="m.text", **content):
    source = {"type": "m.room.message", "event_id": "$e", "sender": sender, "origin_server_ts": ts,
              "content": {"msgtype": msgtype, "body": body, **content}}
    return nio.Event.parse_event(source)


@pytest.fixture
def matrix():
    import asyncio
    got = []
    link = rettrix.MatrixLink(CFG, lambda *a, **k: got.append((a, k)))
    link.client = type("C", (), {"user_id": "@rettrix:example.org"})()
    link._since_ms = 1_000_000_000_000
    room = nio.MatrixRoom(CFG.room, "@rettrix:example.org")
    room.add_member("@bob:example.org", "Bob B", None)

    def deliver(event, in_room=room):
        asyncio.run(link._on_event(in_room, event))
        return got[-1] if got else None
    return deliver, got


@needs_nio
def test_matrix_text_uses_display_name(matrix):
    deliver, _ = matrix
    args, kind = deliver(_event("hallo"))
    assert args == ("@bob:example.org", "Bob B", "hallo")
    assert kind == {"emote": False, "edit": False, "reply": False}


@needs_nio
def test_matrix_emote_edit_reply(matrix):
    deliver, _ = matrix
    assert deliver(_event("winkt", msgtype="m.emote"))[1]["emote"] is True
    args, kind = deliver(_event("* neu", **{"m.new_content": {"msgtype": "m.text", "body": "neu"},
                                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$x"}}))
    assert args[2] == "neu" and kind["edit"] is True
    args, kind = deliver(_event("> <@a:x> frage\n\nantwort",
                                **{"m.relates_to": {"m.in_reply_to": {"event_id": "$x"}}}))
    assert kind["reply"] is True
    assert matrix_to_group(CFG, *args, **kind) == ["Bob B: antwort"]


@needs_nio
def test_matrix_ignores_own_old_and_other_rooms(matrix):
    deliver, got = matrix
    deliver(_event("eigene", sender="@rettrix:example.org"))
    deliver(_event("alt", ts=999))
    deliver(_event("anderer Raum"), in_room=nio.MatrixRoom("!other:example.org", "@rettrix:example.org"))
    assert got == []


@needs_nio
def test_matrix_post_sends_plain_text():
    import asyncio
    calls = []

    class Client:
        async def room_send(self, room_id, message_type, content):
            calls.append((room_id, message_type, content))

    link = rettrix.MatrixLink(CFG, lambda *a, **k: None)
    link.client = Client()
    asyncio.run(link.post("[alice] hallo"))
    # m.text, not m.notice: some bridges forward only m.text (not m.notice)
    assert calls == [(CFG.room, "m.room.message", {"msgtype": "m.text", "body": "[alice] hallo"})]
