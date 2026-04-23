# Design Decision: needs-human-review Escalation for Blocked UoWs

**Date:** 2026-04-23
**Status:** ACCEPTED
**Vision anchor:** principle-1 (proactive resilience), vision.yaml current_focus.blocked_uow_resolution
**Linked issue:** dcetlin/Lobster#887

## Decision

Authorize the `needs-human-review` WOS status and autonomous steward escalation behavior:
- When a UoW reaches MAX_RETRIES (3) failed re-dispatch attempts, the steward transitions it to `needs-human-review` status
- The steward sends a Telegram notification to the admin chat summarizing the stuck UoWs
- The UoW is no longer re-dispatched automatically; human decision (retry/close) is required

## Rationale

Three UoWs (3cc6ca, c0a82e, 654519) have been stuck in re-dispatch loops for 24+ hours with no resolution mechanism. Unlimited retry creates phantom work and blocks queue throughput. Proactive escalation (principle-1) favors surfacing the stuck state to Dan rather than silently re-queuing indefinitely.

This is an extension of the existing steward pattern-aware dispatch gates (dead-end, spiral, burst) to add an explicit human-review gate at N retries.

## Constraints

- Escalation notification is informational only; no autonomous close/retry occurs
- Retry/Close button handlers are a follow-on (see Gap 2 below)
- MAX_RETRIES is a module constant (not per-UoW configurable) to minimize behavioral complexity
