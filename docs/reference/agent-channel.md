# Agent Channel

A bidirectional, Dan-facing-noise-free channel between a local Claude Code
session (running on your laptop, over SSH) and the always-on Lobster
dispatcher. Use it to ask the dispatcher questions or hand it small tasks
from your local machine without going through Telegram/Slack.

**If you are an external agent with no other Lobster context**, you probably
want [`docs/reference/agent-channel-schema.md`](agent-channel-schema.md) instead of
this file — it's the generated wire-format reference (envelope, request_id
rules, addressing, error/ack semantics), or run
`uv run scripts/lobster-chat.py --schema` for the same thing as JSON with no
repo checkout needed. Both are generated from
`src/protocol/agent_channel_schema.py`, the single canonical schema module,
so they can't drift from each other or from what the dispatcher actually
enforces. This document (`agent-channel.md`) is the prose walkthrough —
why the channel exists, deploy/observability/retention — written for a
Lobster maintainer, not an external agent.

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
  "timestamp": "2026-08-04T04:40:00.000000+00:00",
  "agent": "glyph"
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
- `agent` (**optional**) is a cosmetic identity label — set via `lobster-chat
  --agent`/`LOBSTER_CHAT_AGENT` — for the calling agent/session (e.g.
  `"glyph"`). It plays no role in routing or correlation (`request_id` is
  still the sole correlation key); `check_inbox`/`wait_for_messages`
  renders it in place of a generic source label when present, and falls
  back to the previous behavior when it's omitted.

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

### Delegating to a subagent

`write_result`'s dispatcher-relay path does not carry `request_id` — only the
inbound message and whatever the dispatcher explicitly forwards do. So when
the dispatcher delegates the work behind a `local-claude` request to a
background subagent (7-second rule), `request_id` must ride along in that
subagent's prompt frontmatter:

```
---
task_id: <task_id>
chat_id: local-claude
source: local-claude
request_id: <request_id from the inbound message>
background: true
---
```

The subagent is then responsible for calling
`send_reply(source="local-claude", request_id=..., ...)` **itself** — never
the internal/`write_result`-only delivery pattern, since the dispatcher has no
route back to `request_id` once the subagent's result reaches it as a plain
`subagent_result`/`subagent_notification` message.

This is enforced structurally, not just by convention: `hooks/require-background-agent.py`
(a `PreToolUse` hook on `Agent`/`Task` calls) blocks any dispatch whose
frontmatter declares `source: local-claude` without a valid, filesystem-safe
`request_id` — regardless of whether the caller is the dispatcher or a
subagent spawning a nested agent. `hooks/auto-register-agent.py` additionally
records `request_id` into `agent_sessions.db` for audit/observability.

## Error handling

Two independent guarantees, both from the protocol spec, both enforced
below the bootup-doc layer:

- **Single-shot reply slot (principle 1).** `handle_send_reply()` writes the
  answer via `atomic_create_json()` (`src/utils/fs.py`), not the general
  `atomic_write_json()` every other source uses — the difference matters
  here: `atomic_create_json` fails closed if `agent-replies/<request_id>.json`
  already exists instead of silently overwriting it. This is what makes a
  crash-and-redispatch race (the original subagent finishing just as
  `_recover_stale_processing()` reclaims and re-runs the same request past
  `_LOCAL_CLAUDE_STALE_TIMEOUT_SECONDS`) safe: whichever writer gets there
  first wins, and the second is told so rather than clobbering the answer.
- **Silence is a sanctioned failure (principle 5), not something to route
  around.** `write_result` has no `request_id` field and never will — it
  cannot address a reply on this channel, by design. So there is no
  "fallback to write_result" for a failed or crashed delegated subagent.
  Instead:
  - If the subagent *called* `send_reply` and it errored, the subagent
    retries with its own (correct) `request_id` a couple of times, then
    gives up — the `lobster-chat` client times out and reports "no reply."
    See `.claude/sys.subagent.bootup.md` → "Agent Channel Tasks."
  - If the subagent *crashed before calling* `send_reply` at all, the
    reconciler's `agent_failed` notification carries `original_source` (see
    `_build_reconciler_message` in `src/mcp/inbox_server.py`), and the
    dispatcher can recover `request_id` from the saved in-flight prompt
    (`~/lobster-workspace/data/inflight-prompts/<task_id>.txt`) to close the
    loop with a known-true "internal error" reply — see
    `.claude/sys.dispatcher.bootup.md` → "agent_failed" → "Local-claude
    originated failures."
  - In neither case does a `source="telegram"`/`"slack"` fallback ever
    happen — the fail-closed source invariant holds through both failure
    paths, not just the happy path.

## Observability & retention

The channel's request/ack/reply round trip is audited on the EventBus
(`src/mcp/event_bus.py`), written to `~/lobster-workspace/logs/events.jsonl`
by the standard `JsonlFileListener`. This closes a gap: earlier versions of
this channel were entirely excluded from event emission (`source ==
"local-claude"` was skipped alongside `bot-talk`), so agent-channel traffic
was invisible in the audit trail. Distinct event types from the
Telegram/Slack stream, since this is a machine-to-machine round trip, not a
chat delivery:

| Event type              | Emitted by             | When                                                    |
|--------------------------|-------------------------|----------------------------------------------------------|
| `agent_channel.request`  | `handle_claim_and_ack`  | A `local-claude` message is claimed (delegated path only — the direct-reply fast path has no separate claim step). |
| `agent_channel.ack`      | `handle_claim_and_ack`  | The distinct ack file is written (or fails to write, at `warn` severity). |
| `agent_channel.reply`    | `handle_send_reply`     | Every call, whether it wins the single-shot reply slot (`info`) or loses the race (`warn`, `reply_slot_created: false`) — a lost race is worth seeing in the audit trail, not silently dropping. |

None of these events touch Telegram/Slack — `JsonlFileListener` accepts all
severities unconditionally, and `TelegramOutboxListener`/`CriticalAlertListener`
only forward `warn`/`error`/`critical` events when `LOBSTER_DEBUG=true` or
severity is `critical`, same as every other event type. This does not change
the fail-closed source invariant: it is an audit sink, not a reply path.

**Retention.** `~/messages/agent-replies/` accumulates one file per answered
or acked request and nothing previously removed them. `scheduled-tasks/agent-replies-sweep.py`
(a Type B cron-direct job, job name `agent-replies-sweep`) sweeps it daily:

- Only ever removes a file whose name matches `<request_id>.json` or
  `<request_id>.ack.json` with `request_id` passing the same charset
  allowlist `handle_send_reply` enforces (protocol spec principle 6).
  Anything else in the directory is left untouched and counted separately
  in the job's summary.
- Only removes a file once it is older than the retention window (default
  7 days, `LOBSTER_AGENT_REPLIES_RETENTION_HOURS` env var / `--retention-hours`
  flag) — age is read from the reply's own `ts` field when present, falling
  back to file mtime.
- Never touches `inbox/` or `processing/`: a request still being worked has
  no file in `agent-replies/` at all (that state lives in `inbox/`/`processing/`,
  which this job never scans), so there is nothing "in-flight" to
  accidentally delete from this directory — the retention window's only job
  is to give a slow/offline `lobster-chat` client time to poll before its
  answer is swept, which the default 7-day window does with wide margin
  over the client's 300s default poll timeout.

Standalone: `uv run scheduled-tasks/agent-replies-sweep.py [--dry-run] [--retention-hours N]`.

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
| `--timeout` | `LOBSTER_CHAT_TIMEOUT` | `300` | Seconds to wait for a reply before giving up |
| `--interval` | `LOBSTER_CHAT_INTERVAL` | `2` | Poll interval in seconds |
| `--agent` | `LOBSTER_CHAT_AGENT` | *(unset)* | Optional identity label (e.g. `"glyph"`) shown in the dispatcher's inbox instead of a generic source label |

`request_id` is printed to stderr immediately after the inbox message is
written, on every send — not only on timeout — so a dropped SSH connection
never loses the value needed to check
`~/messages/agent-replies/<request_id>.json` manually. If no reply arrives
within `--timeout`, the CLI additionally exits 1 and repeats `request_id` in
the timeout message — the dispatcher may still be working on it.

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
- `hooks/require-background-agent.py` — fail-closed `PreToolUse` check: blocks
  any `Agent`/`Task` dispatch whose frontmatter declares `source: local-claude`
  without a valid `request_id`. Applies unconditionally (dispatcher and
  subagent-initiated dispatch alike).
- `hooks/auto-register-agent.py` — `request_id` added as an optional
  frontmatter field, parsed and persisted to `agent_sessions.db` for
  audit/observability (additive `ALTER TABLE` migration for pre-existing DBs).
- `.claude/sys.dispatcher.bootup.md` — "Agent channel" subsection under
  Message Source Handling; `claim_and_ack`/`Task()` template notes on carrying
  `request_id` into a delegated subagent; the LOCAL-CLAUDE SOURCE GUARD in
  the `subagent_result`/`subagent_error` handler; "Local-claude originated
  failures" under the `agent_failed` handler.
- `.claude/sys.subagent.bootup.md` — "Agent Channel Tasks" section: detection,
  the mandatory direct-`send_reply` delivery pattern, ack≠answer, the
  fail-closed source invariant, and the retry-then-stop procedure for a
  failing `send_reply`.
- `src/utils/fs.py` — `atomic_create_json()`: exclusive-create write (temp
  file + `os.link`) used for the single-shot `agent-replies/<request_id>.json`
  slot so a second writer can never overwrite the first.
- `src/mcp/inbox_server.py` — `agent_channel.request`/`agent_channel.ack`
  events emitted from `handle_claim_and_ack`'s `local-claude` branch;
  `agent_channel.reply` emitted from `handle_send_reply` (replaces the prior
  unconditional skip of `source == "local-claude"` from event emission
  entirely). See "Observability & retention" above.
- `scheduled-tasks/agent-replies-sweep.py` — Type B cron-direct retention
  sweep for `~/messages/agent-replies/`. See "Observability & retention" above.
- `scripts/upgrade.sh` — Migration 139 creates `~/messages/agent-replies/` on
  existing installs; Migration 140 registers `agent-replies-sweep` in
  `jobs.json` and adds its daily cron entry.
- `src/protocol/agent_channel_schema.py` — the single canonical schema module
  for the protocol's envelopes, request_id rules, addressing model, and
  error/ack semantics. Stdlib-only, no other Lobster-internal imports.
  `src/mcp/reliability.py` imports its `REQUEST_ID_MAX_LEN`/`REQUEST_ID_PATTERN`/
  `SOURCE` rather than redefining them; `src/mcp/message_types.py` imports
  `SOURCE` for `INBOX_MESSAGE_SOURCES`.
- `scripts/generate_agent_channel_docs.py` — generates
  `docs/reference/agent-channel-schema.md` and the embedded schema block in
  `scripts/lobster-chat.py` (between its `BEGIN`/`END GENERATED SCHEMA`
  markers) from `src/protocol/agent_channel_schema.py`. Run with `--check` to
  verify the generated artifacts are still in sync (no write) — this is what
  `tests/unit/test_protocol/test_agent_channel_schema.py` calls to catch
  drift.
- `docs/reference/agent-channel-schema.md` — generated. The wire-format reference for
  an external agent with no other Lobster context: envelope field tables,
  request_id rules, addressing (Dan vs. the Agent), error/ack semantics, and
  worked JSON examples. Do not hand-edit — regenerate instead.
- `scripts/lobster-chat.py` — gained `--schema` (prints the schema as JSON,
  no SSH round trip) and a `--help` epilog summarizing the protocol; both
  sourced from the generated block described above.
