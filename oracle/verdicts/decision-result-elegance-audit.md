# Design Decision: Result Elegance Audit as Dispatcher Behavioral Default

**Date:** 2026-05-01
**Status:** ACCEPTED
**Vision anchor:** `core.inviolable_constraints.constraint-4` — minimize metabolic cost of cybernetic engagement; the system should reduce friction and screen time, not maximize output volume or feature density
**Secondary anchor:** `core.operating_principles.principle-1` — structural prevention is preferred over reactive recovery
**Linked commit:** ef131141 (feat(gates): integrate elegant economy as structural dispatch gate)
**WOS-UoW:** uow_20260427_c174a3

## Decision

Authorize the Result Elegance Audit block inserted into the dispatcher main loop before the `# RELAY` step in `.claude/sys.dispatcher.bootup.md`:

- **What it does:** Before relaying a subagent result to the user, the dispatcher evaluates whether each section of the response is load-bearing (would the response fail without it?), whether there is noise trimmable without losing meaning, and whether output volume matches task complexity.
- **Trim gate:** If output-to-task ratio is high, trim non-load-bearing sections before sending.
- **Calibration signal (not a hard gate):** If trimming would lose meaning, relay as-is but log: `write_observation(category="elegance_signal", text="task=<slug> ratio=high sections=<count> trimmed=<count>")`.
- **Not blocking:** The audit never suppresses delivery — it either reduces volume before relay or logs a calibration signal without changing the output.

## Rationale

This is an Encoded Orientation decision: the dispatcher applies a behavioral heuristic (output volume calibration) without Dan's explicit per-message input. Per `core.inviolable_constraints.constraint-3`, Encoded Orientation decisions require a prior logged decision of the same class and a traceable vision.yaml anchor.

**Primary vision anchor:** `core.inviolable_constraints.constraint-4` states: "Minimize metabolic cost of cybernetic engagement. The system should reduce friction and screen time, not maximize output volume or feature density." Output volume calibration before relay is a direct structural implementation of constraint-4: it reduces screen time by trimming non-load-bearing content before delivery rather than requiring Dan to manually filter signal from noise.

**Secondary anchor:** `core.operating_principles.principle-1` (proactive resilience over reactive recovery). A relay filter that fires before delivery is structural prevention — it prevents volume accumulation rather than correcting for it after the user receives it.

**Structural class:** Same as `decision-github-rate-limit-gate.md` (autonomous behavioral gate backed by a logged decision and a vision.yaml anchor) and `decision-system-retrospective-automation.md` (autonomous action backed by constraint-3 and principle-1). The elegance audit is calibration (soft signal + optional trim), not suppression (hard gate). This makes it lower-stakes than either prior decision — it never prevents delivery.

## Constraints

- The audit is not a hard gate — it never blocks or delays delivery
- Trim decisions apply to non-load-bearing sections: repeated context, scaffolding prose, header boilerplate
- The `elegance_signal` observation is informational only — it does not trigger automated remediation
- The block applies only to subagent results before relay, not to direct dispatcher responses
- Tasks explicitly requesting full output (audit reports, transcripts, complete diffs) are not subject to trim
