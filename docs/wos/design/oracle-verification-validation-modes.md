# Oracle Modes: Verification vs. Validation

**UoW:** uow_20260525_46bc95  
**Date:** 2026-05-25

## The Structural Distinction

Verification applies to artifacts that have an external adjudicator: a test suite, a type checker, a linter, a schema. When the oracle reviews a code PR, there is ground truth outside the oracle's judgment. A failing test is not an oracle opinion — it is a fact the oracle can observe and report. The oracle's role in verification mode is to locate the external standard violation and name it precisely. This means NEEDS_CHANGES verdicts in verification mode are not evaluative claims; they are pointers. "Test `test_quarantine_does_not_affect_recognized_sources` fails because the assertion limit is hardcoded to 10" is a complete verdict. "The code could be improved" is not.

Validation applies to artifacts that have no external adjudicator: design documents, specifications, bootup files, prescriptions, UoW documents. When the oracle reviews a design doc, there is no test suite that can fail. There is no linter that can catch a missing assumption. The oracle's attending — its capacity to notice what the document does not say — is the primary mechanism for surfacing what the document occludes. In validation mode, the oracle cannot appeal to an external standard; it is the standard. This means a vague NEEDS_CHANGES verdict in validation mode is not a document failure — it is an oracle failure. If the oracle cannot name specifically what the document presupposes without stating, it has not done the work.

These are structurally different protocols, not just different subject areas. Verification approaches automation: as the test suite grows more complete, the oracle's judgment increasingly converges on what a deterministic process could produce. Validation has an automation ceiling: genuine attunement to what a document is not saying cannot be encoded into a checklist. A checklist-based validation verdict is indistinguishable from no verdict at all — it imposes form without substance.

## Design Implications

**Verification mode:**
- NEEDS_CHANGES verdict points to a failing external check (test, lint rule, type error)
- Oracle judgment is separable from ground truth — the test either passes or it doesn't, independent of what the oracle says about it
- Automation trajectory: the test suite increasingly embodies the standard; the oracle's role approaches "run the suite and report what failed"
- A verification NEEDS_CHANGES that cites no specific external check is malformed — it substitutes oracle opinion for the external adjudicator that exists

**Validation mode:**
- NEEDS_CHANGES verdict articulates a specific attending observation about what the document occludes
- Oracle judgment is the mechanism — not a proxy for an external standard, because no such standard exists for this artifact type
- Automation ceiling: genuine attunement cannot be encoded into a checklist; a vague verdict is an oracle failure, not a document failure
- A validation NEEDS_CHANGES that says "this section needs more detail" without naming the specific presupposition, deferral, or premature resolution is malformed

## Verdict Format Differences

### Verification mode — NEEDS_CHANGES

```
VERDICT: NEEDS_CHANGES
MODE: VERIFICATION
PR: 1234
Round: 1

Failing checks:
- test_source_registration_completeness: assertion on len(INBOX_MESSAGE_SOURCES) fails after adding pr_review_request
- mypy: src/message_types.py:47 — Argument 1 to "frozenset" has incompatible type "str"; expected "MessageType"
```

Each item is a specific, nameable external check. A subsequent reviewer can confirm "addressed" by re-running the check and observing it pass. No narrative required.

### Validation mode — NEEDS_CHANGES

```
VERDICT: NEEDS_CHANGES
MODE: VALIDATION
Round: 1

Attending observations:
- This document proposes a two-phase dispatch model but does not name what happens to UoWs that enter Phase 1 and never receive a Phase 2 signal — it presupposes a reliable delivery guarantee it has not stated.
- Section 3 defers the question of retry semantics to "a future design doc" without naming what system behavior governs in the interim — this creates an operational gap that is active now, not in the future.
```

Each observation is a complete sentence naming the specific gap. "Addressed" is decidable by a subsequent reviewer without re-reading the full original document — they can check whether the revision shows the thing the observation named.

### Approved in either mode

```
VERDICT: APPROVED
MODE: VERIFICATION

Checked: full test suite passes, type checker clean, no lint violations. Core fix correctly breaks the quarantine loop by registering missing sources. The one unhandled type (pr_review_request) produces safe dispatcher fallthrough, not a crash.
```

```
VERDICT: APPROVED
MODE: VALIDATION

Checked: document names all four phases of dispatch with explicit entry/exit conditions. Retry semantics are stated for both TTL recovery and observation-loop recovery. No presuppositions surfaced that are not either stated or scoped as explicit deferrals.
```

## What This Changes in Practice

For code PRs, the oracle now makes its mode explicit in every verdict. A verdict reviewer confirming oracle operation in verification mode should check: does the NEEDS_CHANGES verdict (if any) cite a specific failing check? If it does not — if it reads like a code review opinion without an external check citation — the oracle has drifted into editorial mode and the verdict should be treated as incomplete.

For design doc reviews, the distinction matters more. The existing oracle definition already had a "Document review" invocation mode with a "Named gaps" verdict structure. Mode Detection formalizes what was implicit: the document review invocation mode is validation mode, and the named gaps structure is not a style preference — it is a requirement following from the absence of an external adjudicator. A verdict reviewer confirming oracle operation in validation mode should check: does each named gap name a specific presupposition, deferral, or premature resolution? If a gap reads as a generic quality complaint without naming what the document does not say, it is malformed regardless of whether it is technically present in the verdict.
