# Oracle Learnings

Patterns and antipatterns surfaced through oracle review. These inform future design decisions.

---

## [2026-04-04] PR #602 — Germinator register classification

**Learning 1: Boolean frozenset intersection is an over-eager gate for shared vocabulary**
`_PHILOSOPHICAL_TERMS` using a frozenset intersection means a single ambiguous term (like "register") fires Gate 3 at full confidence. When technical vocabulary overlaps with phenomenological vocabulary, weighted scoring or term-frequency thresholds are more appropriate than presence-only detection. Monitor for false positives as the term list grows.

**Learning 2: Classification results should be typed frozen dataclasses with observability fields**
`RegisterClassification(register, gate_fired, evidence, confidence)` is better than returning just the register string. The observability fields (gate_fired, evidence) make the classification auditable from the result alone without log access. Apply this pattern to any future classifier returning a consequential decision.

---
