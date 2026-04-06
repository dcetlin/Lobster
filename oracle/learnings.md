# Oracle Learnings

Patterns and antipatterns surfaced through oracle review. These inform future design decisions.

**Structure:** Layer 1 (index) is a compact, categorized reference — one line per entry. Layer 2 (archive) contains full entries with detail. Add new entries to both layers.

---

## Layer 1: Index

### State Machines

| Date | PR | Learning |
|------|----|----------|
| 2026-04-04 | #607 | Comment/code mismatch at state-machine transition causes silent state divergence — state and comment must be synchronized |

### Contract & Interface Design

| Date | PR | Learning |
|------|----|----------|
| 2026-04-04 | #607 | Recovery gate tolerating an open executor contract gap inverts principle-1 — fix the contract at the producer, not the consumer |

### Classification & Detection

| Date | PR | Learning |
|------|----|----------|
| 2026-04-04 | #602 | Boolean frozenset intersection over-eager for shared vocabulary — use weighted scoring or frequency thresholds instead of presence-only detection |
| 2026-04-04 | #602 | Classification results should be typed frozen dataclasses with observability fields — not bare return values |

---

## Layer 2: Archive

---

### [2026-04-04] PR #607 — Corrective trace temporal gate

**Learning 1: Recovery gate tolerating an open executor contract gap — principle-1 inversion**
When a steward-side temporal gate is added to tolerate a missing executor output artifact (trace.json absent → wait one cycle, proceed on second entry), the gate makes the steward the adaptation layer for an incomplete executor contract. Vision.yaml principle-1 ("Proactive resilience over reactive recovery") identifies the correct fix layer: close the contract at the executor exit side (require trace.json before result.json is written), not at the steward re-entry side. Once the steward gracefully tolerates the gap, pressure to close the upstream contract decreases. Detection: when a steward-side gate's purpose is "wait for an artifact the executor should have written," check whether the fix should be in the executor's exit protocol instead.

**Learning 2: Comment/code mismatch at state-machine transition — silent state divergence**
When a code comment says "Transition back to X so next heartbeat picks it up" but the `registry.transition()` call transitions to Y (not X), the UoW ends up in Y — which may or may not be a pickup state for the next heartbeat. In the corrective trace gate skip-path: comment says "ready-for-steward" but the call transitions to `DIAGNOSING`. If the heartbeat only picks up `ready-for-steward`, the one-cycle wait becomes indefinite. State-machine code must keep comments and transition arguments synchronized — any mismatch is a reliability liability, not just a documentation issue.

---

### [2026-04-04] PR #602 — Germinator register classification

**Learning 1: Boolean frozenset intersection is an over-eager gate for shared vocabulary**
`_PHILOSOPHICAL_TERMS` using a frozenset intersection means a single ambiguous term (like "register") fires Gate 3 at full confidence. When technical vocabulary overlaps with phenomenological vocabulary, weighted scoring or term-frequency thresholds are more appropriate than presence-only detection. Monitor for false positives as the term list grows.

**Learning 2: Classification results should be typed frozen dataclasses with observability fields**
`RegisterClassification(register, gate_fired, evidence, confidence)` is better than returning just the register string. The observability fields (gate_fired, evidence) make the classification auditable from the result alone without log access. Apply this pattern to any future classifier returning a consequential decision.

---

*To add a new entry: (1) add a one-line row to the appropriate Layer 1 category (or create a new category); (2) append the full entry to Layer 2 with date, PR, and bold learning titles.*
