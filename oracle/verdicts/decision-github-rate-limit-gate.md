# Design Decision: GitHub API Rate Limit Pre-Dispatch Gate

**Date:** 2026-04-27
**Status:** ACCEPTED
**Vision anchor:** principle-1 (structural prevention is preferred over reactive recovery), vision.yaml core.inviolable_constraints.constraint-3 (Encoded Orientation requires prior logged decision of same class and a traceable vision.yaml anchor)
**Linked issue:** dcetlin/Lobster#984

## Decision

Authorize the `GITHUB_RATE_LIMIT_DISPATCH_THRESHOLD = 100` gate in `executor-heartbeat.py`:

- **Pre-dispatch rate limit check:** Once per dispatch cycle (after the context pressure throttle, before `run_executor_cycle`), the heartbeat calls `check_github_rate_limit()` via `gh api rate_limit`. If `remaining < 100`, the cycle is skipped and main() returns 0.
- **Threshold value:** `GITHUB_RATE_LIMIT_DISPATCH_THRESHOLD = 100` is the authorized default. This is a named module constant, not a magic number.
- **Fail-open on tool failure:** If the gh CLI is unavailable, returns a non-zero exit code, or emits unparseable JSON, the gate is bypassed and dispatch proceeds normally. Tooling failure is not a reason to suppress user work.

## Rationale

The observed failure mode (issue #984): when the GitHub API rate limit is exhausted, dispatched subagents burn their full TTL waiting for gh CLI calls that never succeed. The user's work is consumed — quota spent, TTL burned — with no successful outcome. This is reactive degradation: the system dispatches work it cannot complete.

The rate limit gate converts this from reactive failure to structural prevention (principle-1). By checking remaining quota before dispatch, the system avoids spawning subagents that will fail immediately. One skipped cycle at low quota is less costly than one or more subagents burning full TTLs.

This is an Encoded Orientation decision: the system autonomously suppresses dispatch cycles without Dan's explicit input per invocation. It is authorized here with a traceable vision.yaml anchor (principle-1) and a logged prior decision (this document), satisfying constraint-3.

The structural class is the same as `decision-needs-human-review-escalation.md` (steward escalation after MAX_RETRIES) and `decision-system-retrospective-automation.md` (automated issue filing): an autonomous behavioral gate backed by a logged decision and a vision.yaml anchor.

## Constraints

- The check is per-cycle, not per-UoW — one gh CLI call per dispatch loop iteration
- The threshold (100) is a named constant; changing it requires a code change (intentionally not runtime-configurable, to keep behavioral complexity bounded)
- Fail-open on all error paths — a broken gh CLI does not suppress dispatch
- The gate does not affect TTL recovery, which runs independently of the rate limit check
- The 10-second subprocess timeout adds negligible latency at a 3-minute cycle cadence
