# Engineering Principles

This document is a PR review checklist, not a philosophy statement. Use it to evaluate whether a change is consistent with how Lobster is built and why.

When a PR fails a check, the question is not "is this forgivable" but "does the architecture hold."

---

## PR Checklist

### 1. Each module has one contract

**One sentence:** A module's public interface should be stateable in a single sentence.

**Passing looks like:** The reviewer can describe what the module does — its inputs, outputs, and guarantees — without reading its implementation. If the description requires "and also," the module has two contracts and needs to be split.

---

### 2. Isolation boundaries are enforced at the DB/view layer, not the application layer

**One sentence:** Access control and data scoping belong in schema design and query construction, not in conditional logic scattered through application code.

**Passing looks like:** Removing a single application-layer guard does not expose data it should not. The architecture itself — foreign keys, row ownership, view filters — enforces the boundary. If you must trust the programmer to call the right check, the boundary is wrong.

---

### 3. Audit-before-transition is a non-negotiable invariant

**One sentence:** Every state transition writes an audit record before the transition completes, in the same transaction.

**Passing looks like:** A reader can reconstruct exactly what happened, by whom, and when, solely from the `audit_log` — including transitions that were attempted and rolled back. There is no valid exception to this rule: if a transition is too cheap to audit, it is probably not a transition.

---

### 4. Cognitive clarity: can a future reader reconstruct intent from the audit log alone?

**One sentence:** The audit trail is the canonical record of what the system did and why.

**Passing looks like:** A developer unfamiliar with the change can open `audit_log`, read the entries produced by a workflow, and correctly answer: what was the user trying to do, what did the system decide, and what changed? If the answer requires reading source code to interpret the log, the log entries lack sufficient context.

---

## Heuristics

These are not rules but tests. Apply them when something feels off and you cannot name why.

**Fractal structure test:** Does the same organizational logic that applies to the repo apply to this module, this function, this variable? If the answer is no, ask why the exception exists.

**Fractal coherence test:** If you extracted this module and handed it to someone with no other context, would it behave consistently? A module that is coherent in isolation is coherent in the system.

**Golden pattern test:** Is there an existing pattern in the codebase that solves this problem? If yes, use it. If the new approach is better, migrate the old usage — do not leave two patterns coexisting without a migration path.

**Maximal elegance test:** Is there a simpler version of this that is equally correct? If yes, that version is the right answer. Complexity is not a sign of thoroughness; it is a sign that the design has not been finished.

**Cognitive clarity test:** Three months from now, will a developer reading this change — including its audit entries, its variable names, and its commit message — understand not just what it does but why it exists? If not, clarify before merging.
