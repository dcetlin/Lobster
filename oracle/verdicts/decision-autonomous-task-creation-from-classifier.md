# Design Decision: Autonomous Task Creation from Slow-Reclassifier Pattern Output

**Date:** 2026-05-23
**Status:** ACCEPTED
**Vision anchor:** vision.yaml `core.operating_principles.principle-3` (Determinism over judgment for conditionals) and `core.inviolable_constraints.constraint-3` (Encoded Orientation requires prior logged decision of same class and a traceable vision.yaml anchor)
**Linked PR:** dcetlin/Lobster#1023

## Decision

Authorize the `route_pattern_to_action` gate in `src/classifiers/slow_reclassifier.py`:

- **Behavioral default being encoded:** When the slow-reclassifier detects a cross-event pattern and computes `confidence == "HIGH"` (event count >= pattern threshold * 2), the system autonomously creates a task in `~/messages/tasks.json` (for `_TASK_PATTERNS`: design_session, complex_request) or writes a `digest_flag` event (for `_DIGEST_PATTERNS`: brainstorm_mode, meta_thread, philosophy_thread) — without Dan reviewing the detected pattern before the action occurs.

- **Authorization gate:** HIGH confidence is the exclusive gate. HIGH requires `len(obs.event_ids) >= threshold * 2`. MEDIUM confidence (event count below the double-threshold) produces no autonomous action. This is an if-then conditional in code, not an LLM instruction — consistent with principle-3.

- **Idempotency:** The `pattern_actions` table uses `INSERT OR IGNORE` with `pattern_event_id` as primary key. A given pattern event triggers at most one action regardless of how many times the route function is called.

## Rationale

The routing gate (`confidence != ACTION_CONFIDENCE_MINIMUM`) is a deterministic conditional in code: given the same `obs.event_ids` and `pattern_type`, it always produces the same routing outcome. There is no LLM judgment in the routing path. This is the canonical application of principle-3: if-then logic is code, not instructions.

The HIGH confidence gate (events >= threshold * 2) doubles the detection threshold precisely to distinguish noise from a validated signal. A design_session pattern at threshold (3 events) fires on a minimal match; at double-threshold (6 events), it represents sustained activity that has a structural claim on a task entry.

This is an Encoded Orientation decision under constraint-3: the system acts without Dan's explicit per-pattern input. It is authorized here with:
- A traceable vision.yaml anchor: `core.operating_principles.principle-3` (the routing logic is if-then code, not judgment), `core.inviolable_constraints.constraint-3` (this document is the logged prior)
- A structural class precedent: `decision-system-retrospective-automation.md` (autonomous issue filing from pattern detection) and `decision-github-rate-limit-gate.md` (autonomous dispatch suppression) are the same Encoded Orientation class

The WOS UoW `uow_20260427_fd343f` is referenced as dispatch context. It is not the authorizing record; this document is.

## Constraints

- Autonomous action fires only at HIGH confidence (events >= threshold * 2 for the detected pattern type)
- Task writes go to `LOBSTER_MESSAGES/tasks.json` (env var `LOBSTER_MESSAGES`, defaulting to `~/messages`) using the canonical `{"tasks": [...], "next_id": N}` schema — the path and schema the MCP task management system and dashboard both read
- Digest flags are written as `digest_flag` events to the events table; a separate nightly-consolidation step is required to consume them (the connection is wired but not yet active — see PR #1023 description)
- The `pattern_actions` dedup table prevents duplicate actions per `pattern_event_id`; re-running the classifier on the same events is safe
- No autonomous close, PR merge, code change, or deletion occurs from this path — all actions are additive (task creation, event flag)
- The gate does not bypass: MEDIUM confidence patterns write no action and log at DEBUG level only
