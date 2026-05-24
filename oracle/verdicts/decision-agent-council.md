# Design Decision: Agent Council — Ergonomics Synthesis and Dispatcher Routing

**Date:** 2026-05-23
**Status:** ACCEPTED
**Vision anchor:** constraint-1 ("extends my cognitive reach without requiring me to be in front of a screen"), principle-4 ("Wire what exists before building more") — explicitly overridden by owner authorization below
**Authorization:** Dan Cetlin, explicit — "Build it" (Telegram, 4:13 PM EDT 2026-05-23) in response to the agent council design proposal at `workstreams/agent-council/design-v1.md`
**Linked PR:** dcetlin/Lobster#1276

## Decision

Authorize the agent council as a dispatcher-level behavioral route and autonomous synthesis system:

- **Dispatcher routing:** Messages matching `^council:\s+(.+)` (case-insensitive) are routed by `parse_council_command()` in `dispatcher_handlers.py`. The dispatcher acks and spawns a `lobster-generalist` subagent with the `council-deliberation.md` task definition. This is an Encoded Orientation change: it durably alters the dispatcher's message-routing behavior.
- **Autonomous trigger:** `council-note-accumulation-check.py` (Type B cron script, every 30 minutes) counts new notes in `ergonomics-orient/notes/` since the last deliberation. When the threshold (5 notes) is reached, it writes an inbox trigger that causes the dispatcher to spawn a council-deliberation subagent. This fires without Dan's explicit per-invocation input.
- **Canon structure:** Six adjacency-zone directories under `workstreams/agent-council/canon/` (material-science, biomechanics, phenomenology, systems-ecology, tool-design, cognitive-ergonomics) store committed synthesis entries. Canon entries are immutable once committed — only superseded, not edited.
- **Sunday sweep:** `council-sunday-sweep.md` task definition handles weekly scheduled synthesis and inbox-triggered sweeps. Registered post-deploy via `create_scheduled_job`.

## Rationale

The ergonomics frontier has a structural gap: notes accumulate across sessions but nothing converts that accumulation into durable, committed synthesis without Dan initiating it. The council closes this gap under constraint-1: cognitive extension without requiring Dan to be at a screen.

**Principle-4 acknowledgment:** vision.yaml principle-4 ("Wire what exists before building more") applies here — the WOS bidirectional interface (#1118) is the named current constraint. The agent council is a parallel track that does not depend on or block WOS loop closure. It is low-metabolic-cost: no new infrastructure services, no new LLM invocations except on explicit trigger or note-threshold. Dan explicitly reviewed this tradeoff in the design proposal and authorized proceeding: "Build it" (Telegram, 4:13 PM EDT 2026-05-23). Principle-4 is acknowledged, not ignored — owner authorization overrides the default prioritization.

**Ergonomics frontier is not in vision.yaml** as a named field. The nearest anchor is constraint-1 (cognitive extension). Dan's authorization makes the ergonomics synthesis use case an instance of constraint-1 in the ergonomics domain — the council extends his cognitive reach into accumulated research material without requiring a screen-present synthesis session.

This is an Encoded Orientation decision: the dispatcher gains a permanent behavioral route (`council:`) backed by this logged decision and the constraint-1 vision anchor. Satisfies constraint-3 via this document.

## Structural class

Same class as `decision-cc-quota-gate.md` and `decision-github-rate-limit-gate.md`: an autonomous behavioral gate (dispatcher routing + autonomous trigger) authorized by a logged prior decision with a traceable vision.yaml anchor. The autonomous trigger pattern (note-accumulation → inbox message → dispatcher spawn) is structurally equivalent to the rate-limit gate pattern (metric read → suppress dispatch cycle).

## Constraints

- Council fires only on explicit `council: [topic]` trigger OR when note-accumulation threshold (5 notes) is reached — not continuously
- The `council-note-check` job is gated by `is_job_enabled("council-note-check")` in jobs.json — runtime disable is available via `wos stop council-note-check`
- Canon entries are immutable once committed; the Canon-Keeper role may decline to commit if quality threshold is not met
- Devil's Advocate role excluded from MVP — per design doc, the first 3–5 deliberations establish baseline quality before adding a stress-testing pass
- Sunday sweep requires a post-deploy manual `create_scheduled_job` call — not automated in upgrade.sh; failure to run it means the weekly sweep never fires (only the note-accumulation trigger is active)
