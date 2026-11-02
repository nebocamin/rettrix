# rettrix

A bridge between a [Matrix](https://matrix.org) room and a
[Reticulum](https://reticulum.network) group chat.

It targets the group chat service
[reticulum-group-chat](https://github.com/thatSFguy/reticulum-group-chat)
("fwdsvc"), which runs a group as an LXMF endpoint. rettrix joins the group
like any other member, with its own LXMF identity and nickname:

```
group  -> Matrix   "[alice] hi"            is posted to the room as is
Matrix -> group    "bob: hi"               is sent to the group,
                                           members see "[matrix] bob: hi"
```

Matrix replies, edits and emotes are handled, and long messages are split to
fit fwdsvc's 500 character limit. Messages are sent one at a time, at most
every `min_interval` seconds, so slow LoRa links are not flooded.

## Chaining other bridges

Other bridges can post to the same Matrix room, for example one relaying a
MeshCore channel. Give rettrix its own Matrix account (a bridge ignores the
messages of its own account) and list the other bots under
`[matrix.relay_bots."@bot:server"]`. Their prefix is dropped and a label is
shown instead of the bot's name: `[matrix] MeshCore: alice: hello`.

## Install

Requires Python 3.11+ and a running Reticulum instance (`rnsd`) of the same
user, or `reticulum.configdir` pointing to its config directory.

```
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp rettrix.example.toml rettrix.toml     # then edit it
```

The Matrix password is read from `RETTRIX_MATRIX_PASSWORD` or from the file
named in `matrix.password_file` (use `chmod 600`). `rettrix.toml` is in
`.gitignore`; don't commit it.

```
venv/bin/python rettrix.py -c rettrix.toml      # -v for debug output
```

`rettrix.service` is a systemd unit template; adjust `User` and the paths.

## How it copes with fwdsvc

- `/join` replays recent history as `[replay 5m] [nick] text`: not posted.
- Members silent for weeks are pruned and every message from a non-member
  is answered with an invitation and dropped. rettrix then joins again and
  resends the messages that were dropped.
- The nickname is set right after joining (fwdsvc takes it from the
  announced display name otherwise).
- Command replies ("Joined. ...") are only logged.
- Messages from anyone but the group, or with an unverified signature,
  are ignored.

Matrix end-to-end encryption is not supported: use an unencrypted room.

## Tests

```
venv/bin/pip install pytest
venv/bin/pytest
```

The tests need no network. Tests of the Matrix event handling are skipped
if `matrix-nio` is not installed.

## License

[GNU AGPL-3.0-or-later](LICENSE). If you run a modified version as a
network service, you must offer its users the modified source.
