# Agent Channel

A bidirectional, Dan-facing-noise-free channel between a local Claude Code
session (running on your laptop, over SSH) and the always-on Lobster
dispatcher. Use it to ask the dispatcher questions or hand it small tasks
from your local machine without going through Telegram/Slack.

## Why this exists

Lobster already has one inbound channel per messaging platform (Telegram,
Slack, SMS, ...), each with its own bot process draining a per-source
outbox. The agent channel adds a machine-to-machine variant of the same
pattern — `source="local-claude"` — that:

- Requires no bot process, webhook, or third-party account (just SSH).
- Never appears in Telegram/Slack (no notification noise for Dan).
- Round-trips through the same inbox the dispatcher already polls, so it
  needed no change to the dispatcher's main loop.

## Message schema

### Inbound: `~/messages/inbox/<request_id>.json`

Written by the local `lobster-chat` CLI (or any other local-machine
process) over SSH. Matches the standard inbox message shape used by every
other source (see `src/bot/lobster_bot.py`), with one addition:

```json
{
  "id": "1732900000-a1b2c3d4",
  "source": "local-claude",
  "type": "text",
  "chat_id": "local-claude",
  "text": "what's the status of PR 1234?",
  "request_id": "1732900000-a1b2c3d4",
  "timestamp": "2026-08-04T04:40:00.000000+00:00"
}
```

- `source` must be `"local-claude"` — it's registered in
  `INBOX_MESSAGE_SOURCES` (`src/mcp/message_types.py`) so `check_inbox` /
  `wait_for_messages` recognize it instead of warning about an unknown
  source.
- `type` is `"text"`, same as a normal Telegram/Slack message — it's
  already in `INBOX_USER_TYPES` and `USER_FACING_TYPES`, so the existing
  mark-processed guard rails apply unchanged.
- `id` and `request_id` are conventionally the same value; `id` is what the
  dispatcher's `mark_processed` / `send_reply(message_id=...)` operate on,
  `request_id` is what correlates the reply file. Keeping them equal lets
  the dispatcher pass `message_id=request_id` to `send_reply` and get both
  effects from a single call.
- `chat_id` is required by the inbox schema but unused for routing on this
  channel (routing is by `request_id`, not `chat_id`) — any stable string
  is fine; `lobster-chat` sends `"local-claude"`.

### Outbound: `~/messages/agent-replies/<request_id>.json`

Written by the dispatcher via a single `send_reply` call:

```python
send_reply(
    source="local-claude",
    chat_id=<chat_id from the inbound message>,
    request_id=<request_id from the inbound message>,
    text="...",
    message_id=<the inbox message's id, to mark it processed>,
)
```

Produces:

```json
{
  "request_id": "1732900000-a1b2c3d4",
  "text": "...",
  "ts": "2026-08-04T04:40:03.000000+00:00",
  "in_reply_to": "1732900000-a1b2c3d4"
}
```

`lobster-chat` polls for this file and prints `text` once it appears.

## Dispatcher behavior

When the dispatcher's main loop sees an inbox message with
`source="local-claude"`, it should reply with:

```python
send_reply(
    source="local-claude",
    chat_id=<chat_id from the message>,
    request_id=<request_id from the message>,
    text=<the answer>,
    message_id=<the message's id>,
)
```

This is a single normal MCP tool call — no subagent dispatch needed for the
reply itself (though the dispatcher may still delegate the underlying work
to a subagent per the usual 7-second rule, same as any other message type).
The reply must **never** be sent via `source="telegram"` — that would leak
agent-channel traffic to Dan's phone.

## Deploy: does this need an MCP restart?

**Yes.** The routing lives in `handle_send_reply()` inside
`src/mcp/inbox_server.py`, which runs as a long-lived `stdio` subprocess
(`.venv/bin/python src/mcp/inbox_server.py`) started when the Claude Code
session connects to the `lobster-inbox` MCP server. Python code changes in
an already-running process don't take effect until that process restarts.

After merging, run `~/lobster/scripts/restart-mcp.sh` (never
`systemctl restart` directly — see the MCP Service Restart section of the
top-level `CLAUDE.md`) to pick up the new `source="local-claude"` branch
and the `request_id` parameter on `send_reply`.

The inbound side (writing to `~/messages/inbox/`) needs **no** restart —
the dispatcher's `wait_for_messages` loop reads inbox files generically and
`local-claude` just needed to be added to `INBOX_MESSAGE_SOURCES` so it's
not flagged as unrecognized. That part is a plain Python data change, but
it lives in the same MCP server process, so in practice both changes ship
together and need the one restart.

## Setup (local machine)

1. Ensure passwordless SSH key auth to the VPS works: `ssh lobster@<host>`
   should log in without a password prompt (`ssh-copy-id lobster@<host>` if
   not, or an entry in `~/.ssh/config`).
2. Get `scripts/lobster-chat.py` onto your machine — either clone this repo
   locally, or just copy the single file (it has no third-party
   dependencies, stdlib only).
3. Configure the target host, either via flags or env vars:

   ```bash
   export LOBSTER_CHAT_HOST=<your-vps-host>
   export LOBSTER_CHAT_USER=lobster   # default
   ```

## Usage

```bash
uv run scripts/lobster-chat.py "what's the status of PR 1234?"
# or, if not using uv:
python3 scripts/lobster-chat.py "what's the status of PR 1234?"
```

Options:

| Flag | Env var | Default | Meaning |
|------|---------|---------|---------|
| `--host` | `LOBSTER_CHAT_HOST` | *(required)* | VPS hostname |
| `--user` | `LOBSTER_CHAT_USER` | `lobster` | SSH user |
| `--timeout` | `LOBSTER_CHAT_TIMEOUT` | `90` | Seconds to wait for a reply before giving up |
| `--interval` | `LOBSTER_CHAT_INTERVAL` | `2` | Poll interval in seconds |

If no reply arrives within `--timeout`, the CLI exits 1 and prints the
`request_id` so you can check `~/messages/agent-replies/<request_id>.json`
manually later — the dispatcher may still be working on it.

## Files touched by this feature

- `src/mcp/message_types.py` — `"local-claude"` added to
  `INBOX_MESSAGE_SOURCES`.
- `src/mcp/reliability.py` — `"local-claude"` added to `send_reply`'s
  `valid_sources`; `request_id` required and sanitized for that source.
- `src/mcp/inbox_server.py` — `AGENT_REPLIES_DIR` constant + directory
  creation; `send_reply` tool schema gained `request_id`; `handle_send_reply`
  branches on `source == "local-claude"` to write
  `agent-replies/<request_id>.json` instead of the Telegram/Slack/bisque
  outbox.
- `scripts/lobster-chat.py` — the local-machine CLI.
- `scripts/upgrade.sh` — Migration 139 creates `~/messages/agent-replies/`
  on existing installs.
