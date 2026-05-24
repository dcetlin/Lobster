# Engineering Principles

*Last updated: 2026-03-30*

This document is a PR review checklist, not a philosophy statement. Use it to evaluate whether a change is consistent with how Lobster is built and why.

When a PR fails a check, the question is not "is this forgivable" but "does the architecture hold."

---

## PR Checklist

### 1. Each module has one contract

**One sentence:** A module's public interface should be stateable in a single sentence.

**Passing looks like:** The reviewer can describe what the module does — its inputs, outputs, and guarantees — without reading its implementation. If the description requires "and also," the module has two contracts and needs to be split.

---

### 2. Invariants are enforced structurally, not by convention

**One sentence:** System boundaries and access constraints belong in the architecture itself, not in conditional logic that depends on every future developer knowing the convention exists.

**Passing looks like:** Removing a single application-layer guard does not expose data or violate an invariant. The architecture itself — schema design, type constraints, message boundaries, query construction — enforces the boundary. If you must trust the programmer to call the right check, the boundary is wrong.

> **Example (WOS pipeline):** `executor_uow_view` enforces job-scoped isolation at the DB layer. A future developer can't break the cross-tenant boundary even without knowing it exists, because the view never exposes rows they shouldn't see.

---

### 3. State transitions are auditable before they complete

**One sentence:** Every meaningful state transition produces an audit record in the same atomic operation as the transition itself.

**Passing looks like:** A reader can reconstruct what happened, by whom, and when, from the audit trail alone — including transitions that were attempted and rolled back. There is no valid exception to this rule: if a transition is too cheap to audit, it is probably not a transition.

---

### 4. Cognitive clarity: intent survives without the author

**One sentence:** A developer unfamiliar with a change should be able to reconstruct what the system did and why, without reading the source code.

**Passing looks like:** Given only the observable outputs of a workflow — audit records, log entries, variable names, commit messages — a developer can correctly answer: what was the user trying to do, what did the system decide, and what changed? If reconstructing intent requires reading the implementation, the signal is missing from where it needs to be.

> **Example (WOS pipeline):** `audit_log` entries include intent fields alongside state fields. A reader opening `audit_log` after a workflow runs can answer all three questions without opening source.

---

## Heuristics

These are not rules but tests. Apply them when something feels off and you cannot name why.

**Fractal structure test:** Does the same organizational logic that applies to the repo apply to this module, this function, this variable? If the answer is no, ask why the exception exists.

**Fractal coherence test:** If you extracted this module and handed it to someone with no other context, would it behave consistently? A module that is coherent in isolation is coherent in the system.

**Golden pattern test:** Is there an existing pattern in the codebase that solves this problem? If yes, use it. If the new approach is better, migrate the old usage — do not leave two patterns coexisting without a migration path.

**Maximal elegance test:** Is there a simpler version of this that is equally correct? If yes, that version is the right answer. Complexity is not a sign of thoroughness; it is a sign that the design has not been finished.

**Cognitive clarity test:** Three months from now, will a developer reading this change — including its audit entries, its variable names, and its commit message — understand not just what it does but why it exists? If not, clarify before merging.
