# Design Decision: CC Quota Dispatch Suppression Gate

**Date:** 2026-05-10
**Status:** ACCEPTED
**Vision anchor:** principle-1 (structural prevention is preferred over reactive recovery), vision.yaml core.inviolable_constraints.constraint-3 (Encoded Orientation requires prior logged decision of same class and a traceable vision.yaml anchor)
**Linked PR:** dcetlin/Lobster#1125

## Decision

Authorize the `CC_QUOTA_SKIP_THRESHOLD = 90.0` gate in `executor-heartbeat.py`:

- **Pre-dispatch quota check:** Once per dispatch cycle (after the GitHub rate limit check, before `run_executor_cycle`), the heartbeat calls `_read_cc_quota()`. If the returned value is >= 90.0%, the cycle is skipped and main() returns 0.
- **Threshold value:** `CC_QUOTA_SKIP_THRESHOLD = 90.0` (five-hour utilization percentage). Named constant, not a magic number.
- **Fail-open design:** If state.json is absent, stale (last_updated > 60 minutes ago), malformed, or the cc-usage-poller is not configured, `_read_cc_quota()` returns None and dispatch proceeds normally. Lack of quota data never suppresses work.
- **Staleness window:** 60 minutes (`_CC_QUOTA_FRESHNESS_SECONDS = 3600`). The cc-usage-poller runs every 30 minutes, so a 60-minute staleness window tolerates one missed poll cycle before failing open.
- **State file shape:** Reads `rate_limits.five_hour.pct` and `last_updated` from `~/.claude/cc-budget/state.json`, which is written by `scheduled-tasks/cc-usage-poller.py`.

## Rationale

At the 20X Claude Max plan, token economy is a first-class operational constraint. When the 5-hour quota is near exhaustion, dispatching new subagents is counterproductive: each subagent burns tokens before producing output, and if the quota is exhausted mid-task the work is lost anyway. Suppressing dispatch at 90% prevents this waste without blocking TTL recovery (orphan cleanup runs independently of the dispatch gate).

This is an Encoded Orientation decision: the system autonomously suppresses dispatch cycles without explicit per-invocation user input. It is authorized here with a traceable vision.yaml anchor (principle-1) and satisfies constraint-3 via this logged decision.

The structural class matches `decision-github-rate-limit-gate.md` (same executor-heartbeat.py gate pattern, same fail-open invariant, same pre-dispatch position in the cycle). The two gates compose cleanly: GitHub rate limit is checked first; CC quota is checked second; only if both pass does `run_executor_cycle()` execute.

## Constraints

- The check is per-cycle, not per-UoW — one file read per dispatch loop iteration
- The threshold (90.0) is a named constant; changing it requires a code change (intentionally not runtime-configurable, to keep behavioral complexity bounded)
- Fail-open on all error paths — absent, stale, or malformed state.json does not suppress dispatch
- The gate does not affect TTL recovery, which runs independently
- The cc-usage-poller must be running (cron every 30 min) for the gate to be active; without a fresh state file the gate is permanently open (correct default for new installs)
