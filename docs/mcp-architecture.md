# MCP Architecture

How the Lobster dispatcher's tools (`lobster-inbox`) are served, why the
transport moved from stdio to HTTP, the service topology, the reconnect
story after a restart, and the runbook for reloading the MCP server safely.

Written for a Lobster maintainer diagnosing MCP connectivity, planning a
code change to `src/mcp/inbox_server.py`, or reloading the server.

## TL;DR

`src/mcp/inbox_server.py` is the single source of truth for every
`lobster-inbox` tool (`wait_for_messages`, `send_reply`, `check_inbox`, …).
It can run two ways:

- **stdio** — spawned directly as a child process of the `claude` CLI,
  registered in `~/.claude.json`. Dies whenever `claude` dies (tightly
  coupled); no separate service, no separate restart.
- **HTTP** (current default) — runs as its own systemd service,
  `lobster-mcp-local.service`, listening on `127.0.0.1:8766`. The
  dispatcher connects to it as an HTTP client. This lets the MCP server be
  reloaded (new code, `Restart=always` recovery from a crash) **without**
  bouncing the dispatcher's own `claude` process — the dispatcher's
  conversation state and context survive an MCP server restart.

This host runs the HTTP transport. The switch happened 2026-08-05, see
[Migration history](#migration-history) below.

## Why two transports exist

Prior to the HTTP transport, every MCP code change (or any accidental
server crash) required restarting the whole `lobster-claude.service` —
losing the dispatcher's live conversation context and situational
awareness built up over the session, not just its tool connection. The
HTTP transport decouples "the process that holds conversation state" from
"the process that serves tools," so the latter can be reloaded on its own.

**Do not confuse this with `inbox_server_http.py` / `lobster-mcp.service`**
(port 8741) — that is a *different*, unrelated HTTP service: a read-only
remote-access bridge for an external Claude Code instance to read Lobster
context over the network (bearer-token auth, ~30-tool allowlist, blocks
`send_reply`/`mark_processed`/write tools by design). It has also
accumulated unrelated product webhooks (OAuth token push, contact
enrichment). It is not a candidate for dispatcher transport and is not
covered further here — see `services/lobster-mcp.service` and
`src/mcp/inbox_server_http.py` if you need to touch it.

## Service topology

| Unit | Purpose | Port | Bind | Auth |
|---|---|---|---|---|
| `lobster-claude.service` | The dispatcher: an always-on `claude` CLI session running inside a `tmux` session (`ExecStart` wraps `tmux new-session ... start-claude.sh`). Holds all conversation state and context. | — | — | — |
| `lobster-mcp-local.service` | Serves every `lobster-inbox` MCP tool over HTTP. Same code as the stdio server (`src/mcp/inbox_server.py --http --port 8766`), full tool parity, nothing stripped. | 8766 | `127.0.0.1` only | none — trusts localhost (equivalent to stdio's implicit trust: only local processes can reach it) |
| `lobster-mcp.service` | Unrelated read-only remote-access bridge (`inbox_server_http.py`). Not part of this architecture. | 8741 | `0.0.0.0` | bearer token (`MCP_HTTP_TOKEN`) |
| `lobster-router.service` | Telegram → Claude Code bridge (writes to the shared inbox that both transports read). | — | — | — |
| `lobster-bisque.service` | WebSocket relay/file server for the Bisque channel. | — | — | — |

`lobster-mcp-local.service` unit file: `services/lobster-mcp-local.service`
(rendered from `services/lobster-mcp-local.service.template` via
`scripts/lib/template.sh` during install/upgrade). Key properties:

```ini
Environment=MCP_TRANSPORT=http
Environment=MCP_HTTP_PORT=8766
ExecStart=<venv>/bin/python <install_dir>/src/mcp/inbox_server.py --http --port 8766
Restart=always
RestartSec=5
```

`Restart=always` means a crashed MCP server comes back on its own within a
few seconds — the dispatcher just needs to reconnect (see
[Reconnect story](#reconnect-story-what-survives-a-restart) below).

## How the dispatcher connects

`~/.claude.json` registers the transport:

```json
"lobster-inbox": {
  "type": "http",
  "url": "http://localhost:8766/mcp"
}
```

(Previously, under stdio, this was a `command`/`args` entry that spawned
`inbox_server.py` directly — no URL, no separate process.)

Internally, `inbox_server.py --http` uses the `mcp` SDK's
`StreamableHTTPSessionManager(app=server, stateless=False)` — it keeps
server-side session state keyed by an `mcp-session-id` header. Because the
HTTP server can be talking to several Claude Code sessions at once
(the dispatcher itself, plus any concurrently running background
subagents that also hold MCP connections), it needs to know **which**
connected session is *the* dispatcher, so it can gate dispatcher-only
tools (`wait_for_messages`, etc.) to that one session. This replaces the
tmux-ancestry check stdio mode used (where "is this the dispatcher" could
be inferred from the process tree, since stdio meant one server process
per client).

**Dispatcher session tagging:** the first call to `session_start` with
`agent_type="dispatcher"` (or the first `wait_for_messages` call) tags the
current HTTP session id as "the dispatcher" server-side. That tag — not
process ancestry — is what subsequent calls check to authorize
dispatcher-only tools. This is genuinely fiddly (the original
implementation needed several follow-up fixes to get the session-context
plumbing right); if a dispatcher-only tool call is unexpectedly denied,
suspect a stale or mistagged session before suspecting the caller.

## Reconnect story: what survives a restart

This is the property the whole migration exists to provide, so it's
documented precisely.

**When `lobster-mcp-local.service` restarts** (planned reload, or
`Restart=always` recovering from a crash), any dispatcher call blocked in
`wait_for_messages` (which can block for up to 20 hours) receives a
protocol-level `-32600 Session not found` error. The dispatcher's `claude`
process itself is untouched — its context and conversation history are
intact. It just needs to reconnect and resume.

Two mechanisms cooperate to make that reconnect self-healing:

1. **PID-based liveness guard** (`_is_dispatcher_alive()` in
   `inbox_server.py`). On its own startup, the MCP server checks whether
   the PID recorded in `dispatcher.pid` (written by `claude-persistent.sh`)
   is still alive via `kill -0`. A live PID means the dispatcher process
   survived — this was a transport-only hiccup, not real session loss.

2. **`session_reconnect` reminder** (`_write_session_lost_reminder()`).
   If the dispatcher PID is *not* alive (or absent), the MCP server writes
   a synthetic P0-priority inbox message with `type: "session_reconnect"`
   so that whenever a dispatcher does reconnect and call
   `wait_for_messages()`, the very first thing it sees is a prompt to
   re-orient. If the PID *is* alive, no reminder is written — reconnecting
   silently is enough.

**Historical note (postbounce #5, fixed 2026-08-05):** this reminder used
to reuse the same message type (`compact-reminder`) that a real context
compaction uses. The dispatcher's compact-reminder handler unconditionally
spawns the `compact-catchup` subagent — a 10-15 minute recovery workflow —
so every MCP restart, even a zero-actual-loss code reload, paid the full
compaction-recovery cost. The fix gave the reconnect case its own message
type, `session_reconnect` (registered in `src/mcp/message_types.py`,
P0-priority via `_INBOX_P0_TYPES` in `inbox_server.py`), with its own
lightweight dispatcher handler (`sys.dispatcher.bootup.md` → "session-reconnect")
that just re-orients — no subagent spawn. `subtype: "compact-reminder"`
now unambiguously means "real compaction, spawn catchup"; `type:
"session_reconnect"` means "transport-only hiccup, just re-orient."

**What does NOT self-heal:** any in-flight *non*-`wait_for_messages` tool
call (e.g. a `send_reply` mid-execution) fails immediately when the server
restarts — it must be retried by the caller. `wait_for_messages` is the
one call designed to be interrupted and resumed via the reconnect
mechanism above, because it's the one call expected to be blocked for
long stretches.

## Restart runbook

**Always use `scripts/restart-mcp.sh`, never a bare `systemctl restart`.**
A direct `sudo systemctl restart lobster-mcp-local` invalidates the active
MCP session immediately with no advance warning — the dispatcher sees the
`-32600` error with nothing in its inbox yet explaining why. The wrapper:

```
~/lobster/scripts/restart-mcp.sh          # writes a warning, waits 2s, restarts
~/lobster/scripts/restart-mcp.sh --no-wait  # skip the 2s wait (scripted use)
```

It auto-detects the installed unit name (`lobster-mcp-local` if present,
else falls back to `lobster-mcp`) and writes an inbox warning message
before restarting, giving the dispatcher two chances to see recovery
guidance: the pre-restart warning, and the post-restart
`session_reconnect` reminder from §[Reconnect story](#reconnect-story-what-survives-a-restart)
if the dispatcher process itself didn't survive.

**Never run `sudo systemctl restart lobster-mcp-local` (or `lobster-claude`)
directly** for the same reason — see the "MCP Service Restart" section in
the top-level `CLAUDE.md`.

**To verify a restart landed cleanly:**

```
systemctl status lobster-mcp-local.service   # active (running), recent start time
claude mcp list                              # lobster-inbox reports Connected
```

## Migration history

The HTTP transport (`inbox_server.py --http`, PR #961, `cad5104d`,
2026-03-27) shipped with full tool parity and has been `install.sh`'s
default for new installs since then. Existing installs were meant to be
retrofitted by `upgrade.sh` Migration 43, but a logic bug in its
idempotency guard meant the migration silently no-op'd on every run, on
every host — not specific to this one:

```bash
# scripts/upgrade.sh, line ~1870
mcp_http_already_registered=$(claude mcp list 2>/dev/null | grep -c "localhost:8766" || echo "0")
```

`grep -c` with zero matches prints `0` to stdout but exits with status 1,
so the `|| echo "0"` fallback *also* fired, appending a second `"0"` line.
The guard then compared the two-line string `"0\n0"` against the literal
`"0"` — always false — so the migration body (install the service, switch
the `~/.claude.json` registration) was skipped even when it should have
run. This is tracked separately (postbounce #4) — check `scripts/upgrade.sh`
line ~1870 for current status before relying on Migration 43 to retrofit a
new host unattended; this doc describes the mechanism, not a guarantee the
guard has been fixed yet.

This host's cutover (2026-08-05) was staged: `lobster-mcp-local.service`
was stood up and health/tool-parity-verified *alongside* the still-active
stdio connection first (zero risk to the live session), then the
`~/.claude.json` registration was switched and `lobster-claude.service`
was bounced once (the one unavoidable bounce — `claude mcp add` only
affects future process starts, it can't hot-swap an already-running
session's transport). A detached watchdog (see next section) was armed
before that bounce as an auto-revert safety net.

## Detached watchdogs (postbounce #10)

A migration like the stdio→HTTP cutover — or any future change that
requires bouncing `lobster-claude.service` and might leave the dispatcher
with no working MCP tools if something goes wrong — should be armed with
an auto-revert watchdog *before* the bounce: sleep through a generous boot
window, then verify the new dispatcher actually has working tools, and
revert automatically if not.

**The watchdog itself must survive the restart it's observing.** A
watchdog armed with bare `setsid`/`nohup`/`disown` escapes the POSIX
session and process group, but **not** the systemd cgroup —
`systemctl restart <unit>` sweeps the unit's whole control group by
default (`KillMode=control-group`), killing a detached-but-still-in-cgroup
child regardless of `setsid`. This is exactly what happened during the
2026-08-05 cutover: `mcp-cutover-watchdog.sh`, armed via bare `setsid`,
died ~98s into its 210s observation window when `lobster-claude.service`
restarted. The cutover happened to be healthy, so the missing watchdog
didn't matter that time — but a failed cutover with a dead watchdog would
have had no auto-revert.

**Fix: launch the watchdog as its own transient systemd unit**, not as a
detached child of the calling shell. `scripts/lib/arm-detached-watchdog.sh`
provides a reusable helper:

```bash
source scripts/lib/arm-detached-watchdog.sh
arm_detached_watchdog "mcp-cutover-watchdog-$(date -u +%s)" \
    /home/lobster/lobster-workspace/scripts/mcp-cutover-watchdog.sh
```

This runs `sudo systemd-run --unit=<name> --collect -- <command>` under
the hood. The launched command runs under `/system.slice/<name>.service`
— a cgroup that is a **sibling** of the service being restarted, not a
descendant of it, so a `KillMode=control-group` sweep of the restarted
service never touches it. `systemd-run` also detaches inherently: it
returns as soon as the transient unit is accepted, output goes to the
journal (`journalctl -u <name>.service`), and the command has no
controlling terminal to lose — no `setsid`/`nohup`/`disown` needed.
`--collect` removes the transient unit definition once the command exits,
so repeated arms don't accumulate dead unit files.

Any future one-shot watchdog that must survive a `systemctl restart` of
the service that armed it should use this helper instead of bare
`setsid`/`nohup`.

## Related source material

- `reports/design-mcp-decoupling.md` and `reports/mcp-decouple-steps-1-2.md`
  (workspace-local investigation/execution notes, not checked into this
  repo) — the original design proposal and staged execution report this
  doc synthesizes.
- `src/mcp/inbox_server.py` — canonical implementation, both transports.
- `src/mcp/message_types.py` — message type taxonomy referenced above.
- `.claude/sys.dispatcher.bootup.md` → "Message Handlers" → `compact-reminder`
  and `session-reconnect` — the two handlers this doc's reconnect story
  depends on.
- `scripts/restart-mcp.sh`, `scripts/lib/arm-detached-watchdog.sh`.
