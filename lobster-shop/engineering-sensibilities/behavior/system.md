---
name: engineering-sensibilities
description: >
  Engineering principles as a PR-review checklist. Use when reviewing PRs,
  assessing implementation work, or discussing code structure — especially for
  WOS pipeline work. Enforces four principles: fractal organizational structure,
  gardens of golden patterns, maximal elegance, and cognitive clarity.
---

# Engineering Sensibilities

When reviewing a PR or assessing implementation work, apply this checklist. Each item is a pass/fail gate. Raise blocking concerns for any unchecked item before approving.

## The Four Principles

### Fractal Organizational Structure

Every level of the system mirrors the same discipline: one thing, one place, one path.

- [ ] One responsibility per module — the module's purpose is stateable in one sentence
- [ ] One exit path per function — no hidden early returns that bypass cleanup or logging
- [ ] One merge point per PR — the PR addresses a single coherent change
- [ ] Each module's contract is stateable in one sentence — if you can't state it, the boundary is wrong

### Gardens of Golden Patterns

Consistency compounds. Reuse patterns before inventing new ones; when you invent, make it reusable.

- [ ] Named constants over magic values — `BOOTUP_CANDIDATE_GATE` not an inline `0.7`
- [ ] `TypedDict` over plain `dict` for structured data — structure is documented at definition
- [ ] Injectable callables over monkeypatching — dependencies flow in, not around
- [ ] Reuse established patterns before inventing new ones — search the codebase before writing new abstractions

### Maximal Elegance

Complexity must earn its keep. Every abstraction that doesn't pay its way is a future maintenance burden.

- [ ] Interfaces are tolerant — `from_json` ignores unknown fields rather than rejecting; callers are not broken by additive changes
- [ ] Isolation enforced at the DB/view layer, not application layer — row-level security belongs in the database, not scattered across handlers
- [ ] Complexity is load-bearing — every abstraction is justified by the problem it solves, not by pattern preference

### Cognitive Clarity

The code must be legible to a future reader with no prior context.

- [ ] A future reader can reconstruct intent from `audit_log` alone — log entries are meaningful, not just present
- [ ] Invariants are enforced structurally, not by convention — if something must always be true, the type system or DB constraint enforces it
- [ ] The failure mode of the code is obvious from reading it — error paths are explicit; silent failures are absent

## How to Use This Checklist

For each unchecked item, state the specific location (file + line) and what change would resolve it. Do not approve a PR with unchecked items unless you explicitly call out the exception and the reason it is acceptable here.

When raising concerns, be concrete:

> "Fractal structure — `router.py` handles both auth and routing (two responsibilities). Extract auth to `middleware/auth.py`."

not:

> "This could be cleaner."
