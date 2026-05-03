# WOS Dispatch Failure Modes

*April 2026*

---

This document describes the executor→dispatcher inbox dispatch boundary: how dispatch failures manifest, how they are detected, and how recovery operates.

---

## Background

PR #584 refactored executor dispatch from subprocess (`claude -p`) to the dispatcher inbox pattern. The executor now writes a `wos_execute` message to `~/messages/inbox/` and returns immediately. The Lobster dispatcher picks up the message on its next cycle (typically seconds) and spawns a background subagent via the Task tool.

This design eliminates:
- The 0–3 minute polling hop from heartbeat-based dispatch
- Direct subprocess management in the executor
- TTL concerns tied to subprocess lifecycle

But it introduces a new coupling: executor dispatch success now depends on inbox filesystem health and dispatcher responsiveness.

---

## Failure Modes

### 1. Inbox Write Failure

**Trigger:** The inbox directory (`~/messages/inbox/`) is unavailable — disk full, permissions changed, NFS mount lost, etc.

**Symptom:** `OSError` raised from `_dispatch_via_inbox`. The UoW claim succeeds (6-step atomic sequence completes), but dispatch fails. The UoW remains in `active` state with no corresponding inbox message.

**Detection:**
- Immediate: The failure is logged to `~/lobster-workspace/logs/dispatch-boundary.jsonl` with `outcome: failure` and the specific `failure_reason`.
- Downstream: The dispatcher sees no `wos_execute` message. The steward's heartbeat observation loop detects the stall within `heartbeat_ttl` + 3 min poll (default: ~8 minutes) and returns the UoW to `ready-for-steward`. The 24h orphan safety net in `recover_ttl_exceeded_uows` is the backstop if the observation loop also misses it.

**Recovery time bound:** ~8 minutes (heartbeat_ttl 300s + 3-minute poll), down from 4 hours.

**Observability record:**
```json
{
  "ts": "2026-04-08T12:34:56Z",
  "uow_id": "uow_abc123",
  "dispatch_attempt": 1,
  "outcome": "failure",
  "failure_reason": "inbox_write_failed: [Errno 28] No space left on device"
}
```

---

### 2. Inbox Message Not Picked Up

**Trigger:** The inbox message is written successfully, but the dispatcher does not read it — dispatcher crashed, dispatcher context compacted before reading, or MCP server disconnected.

**Symptom:** The UoW is in `active` state, the inbox message exists (or was processed by mark_processed without spawning a subagent), but no subagent is running.

**Detection:**
- The dispatch boundary log shows `outcome: success` with a valid `msg_id`.
- No corresponding Task call appears in dispatcher logs.
- Steward observation loop detects heartbeat silence and fires stall recovery within ~8 minutes (heartbeat_ttl 300s + 3-minute poll).

**Recovery time bound:** ~8 minutes (heartbeat stall detection), down from 4 hours.

---

### 3. Dispatcher Processes Message But Subagent Fails to Start

**Trigger:** The dispatcher reads the `wos_execute` message and attempts to spawn a subagent, but the Task tool call fails — rate limit, context overflow, or transient API error.

**Symptom:** The inbox message is marked processed. No subagent is running. The UoW is stuck in `active` state.

**Detection:**
- Dispatch boundary log shows `outcome: success` (the executor's write succeeded).
- Dispatcher logs show a Task tool failure or no Task call for that `uow_id`.
- TTL recovery fires after 4 hours.

**Recovery time bound:** 4 hours (TTL_EXCEEDED_HOURS).

---

## Observability

All dispatch attempts are logged to `~/lobster-workspace/logs/dispatch-boundary.jsonl` with structured records:

| Field | Type | Description |
|-------|------|-------------|
| `ts` | ISO 8601 | Timestamp of the dispatch attempt |
| `uow_id` | string | The UoW being dispatched |
| `dispatch_attempt` | int | Attempt number (always `1`; `2+` reserved for a future retry loop) |
| `outcome` | enum | `success` or `failure` (`retry` is a reserved value; no current code path emits it) |
| `msg_id` | string? | Inbox message ID (on success) |
| `failure_reason` | string? | Reason for failure (on non-success) |

### Query Examples

```bash
# Recent dispatch failures
jq 'select(.outcome == "failure")' ~/lobster-workspace/logs/dispatch-boundary.jsonl | tail -20

# Dispatch attempts for a specific UoW
jq 'select(.uow_id == "uow_abc123")' ~/lobster-workspace/logs/dispatch-boundary.jsonl

# Failure rate over last 24h
grep '"failure"' ~/lobster-workspace/logs/dispatch-boundary.jsonl | \
  jq 'select(.ts > "2026-04-07")' | wc -l

# All unique failure reasons
jq -r 'select(.outcome == "failure") | .failure_reason' ~/lobster-workspace/logs/dispatch-boundary.jsonl | sort | uniq -c
```

---

## Recovery Path

### Reactive: TTL Recovery (Primary)

The executor-heartbeat runs every 3 minutes and includes TTL recovery:

1. Scans for UoWs in `active` or `executing` state older than `TTL_EXCEEDED_HOURS` (4 hours).
2. Marks them `failed` with `return_reason='ttl_exceeded'`.
3. The Steward re-diagnoses on its next pass and may re-prescribe.

**Time bound:** 4 hours from dispatch to recovery detection.

### Proactive: Dispatch Boundary Monitoring (Recommended)

For faster detection, monitor `dispatch-boundary.jsonl`:

```bash
# Alert if >2 failures in last 10 minutes
failures=$(jq -r 'select(.outcome == "failure" and .ts > "'$(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%S)'")' \
  ~/lobster-workspace/logs/dispatch-boundary.jsonl 2>/dev/null | wc -l)
if [ "$failures" -gt 2 ]; then
  echo "ALERT: $failures dispatch failures in last 10 minutes"
fi
```

### Fallback Dispatch (Not Implemented)

A file-based queue or secondary notification path could provide fallback dispatch when the inbox is unavailable. This is not currently implemented. If implemented in the future, it should be gated behind a feature flag (`WOS_FALLBACK_DISPATCH_ENABLED`) with the primary path remaining inbox dispatch.

---

## Design Rationale

The inbox dispatch pattern was chosen over subprocess dispatch for several reasons:

1. **Event-driven latency:** Dispatch happens on the next dispatcher cycle (~seconds) rather than the next heartbeat tick (0–3 minutes).
2. **Architectural alignment:** All subagent dispatch routes through the same inbox→dispatcher→Task path.
3. **No subprocess management:** The executor does not need to spawn, monitor, or clean up `claude -p` processes.

The tradeoff is inbox coupling. The steward's heartbeat observation loop is the primary stall detection mechanism (detects silence within `heartbeat_ttl` + poll interval, default ~8 minutes). The 24h TTL orphan safety net is the last-resort backstop. The dispatch boundary log closes the observability gap: failures are visible immediately even if stall detection has a latency window.

---

## Related Documentation

- `docs/executor-contract.md` — Executor protocol and result.json contract
- `docs/wos-v2-design.md` — WOS architecture overview
- `docs/wos-sprint3-part2-design.md` — S3P2-E issue specification
- PR #584 — Original inbox dispatch refactor
