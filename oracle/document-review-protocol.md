# Oracle Document Review Protocol

Extends the oracle review system to support document artifacts — retros, design docs, synthesis work — that have no external test adjudicator. Produced by WOS UoW uow_20260427_a30505.

For code PR review format, see `oracle/verdicts/` and the PR Merge Gate in `CLAUDE.md`.

---

## 1. Verdict Format for Document Artifacts

Each document verdict must include the following fields, in this order:

```
VERDICT: APPROVED | NEEDS_CHANGES | DEFERRED
Document: <filename or short identifier>
Type: retro | design-doc | synthesis | protocol | other
Round: <integer>

---

Named gap: <one sentence, stated as a falsifiable claim>
  (What the document is making invisible. Not a vague direction — a specific,
  checkable claim about what is absent, misrepresented, or unresolved.)

Citable position: <one paragraph max>
  (The oracle's interpretation, stated so a reader can dispute it. Must be
  specific enough that a subsequent revision can address it by name.)

Answerable revision requirement: <one sentence>
  (What a round-2 review will check. A checkable condition, not a preference.
  Can be stated as: "The revised document must [X] in order to close this gap.")

Out of scope for this verdict: <one sentence>
  (Explicit boundary: what the author is NOT being asked to change.)
```

When VERDICT is APPROVED, the Named gap, Citable position, Answerable revision requirement, and Out of scope fields are replaced by a single **Basis for approval** field:

```
Basis for approval: <one paragraph>
  (What structural condition the document satisfies that warrants APPROVED.
  Not "it is good" — a named condition the document demonstrates.)
```

Approved document verdicts are stored in `oracle/verdicts/` using the naming convention `document-<slug>.md` (e.g., `document-wos-phase1-retro.md`). Archiving to `oracle/verdicts/archive/` is not required — document verdicts are not cycle-bound the way PR verdicts are.

---

## 2. Protocol Rules

**Minimum structural condition for APPROVED**

A document is APPROVED when it demonstrates that its central claim is grounded: the specific decision, tradeoff, or finding it names is stated in terms a reader can verify or dispute independently, without access to context that exists only in the author's memory. "We chose X" is not grounded. "We chose X because Y was ruled out due to Z constraint, and the consequence is W" is grounded. The oracle does not judge quality of writing or completeness of coverage — it judges whether the document's central claims can stand on their own.

**What makes NEEDS_CHANGES accountable rather than vague**

A NEEDS_CHANGES verdict is accountable when the author can write a one-sentence response that either accepts or disputes the named gap. "This retro names the symptom but not the decision that produced it" is accountable — the author can respond: "Agreed, the decision was X" or "The decision was documented in PR #N, here is the link." A vague verdict like "this could be more specific" is not accountable — the author has no anchor for a one-sentence response.

The test: if the oracle cannot state what a one-sentence reply from the author would look like, the named gap is too vague to be NEEDS_CHANGES.

**How a revision closes a NEEDS_CHANGES**

In round 2, the oracle checks the answerable revision requirement from round 1 — not whether the document "improved" in a general sense. The oracle reads the specific condition named in round 1 and confirms it is met or is not. If met: gap closed, proceed to overall verdict. If not met: re-assert the same gap (do not introduce new gaps unless independently warranted). The revision contract is one-to-one: one gap, one requirement, one check.

**When DEFERRED is appropriate**

DEFERRED applies when a document is inherently incomplete because the work it describes is not finished, and completion is a prerequisite to meaningful review. A design doc mid-implementation, a retro started before the incident is closed, a synthesis document missing a section still in progress. DEFERRED is not appropriate for documents that are complete but inadequately grounded — those are NEEDS_CHANGES. The discriminator: does the gap exist because the author has not written it yet (DEFERRED) or because the author wrote it without sufficient grounding (NEEDS_CHANGES)?

---

## 3. Worked Example

**Scenario:** An oracle agent is reviewing `oracle/document-review-protocol.md` itself — a protocol document proposing expansion of oracle review from code artifacts to document artifacts.

```
VERDICT: NEEDS_CHANGES
Document: oracle/document-review-protocol.md
Type: protocol
Round: 1

---

Named gap: The protocol defines accountable NEEDS_CHANGES verdicts and a
named-gap structure, but does not specify where approved document verdicts are
stored or how a subsequent reader confirms a document was oracle-reviewed.

Citable position: A review protocol that produces verdicts without specifying
where approved verdicts are stored creates a structural gap between the
accountability the protocol promises ("the oracle's interpretation is citable
and disputable") and the traceability a reader needs to honor that promise.
Code verdicts are stored in oracle/verdicts/pr-{number}.md and archived after
merge — the storage location is defined by convention and enforced by the PR
Merge Gate. Document verdicts have no equivalent convention defined in this
protocol. The APPROVED path produces a verdict but names no home for it. A
document reviewed and approved by the oracle is indistinguishable from a
document never reviewed, unless the verdict has a defined location a reader
can check.

Answerable revision requirement: The revised protocol must name a specific
storage location for approved document verdicts (a directory path and naming
convention) and must state whether archiving completed document verdicts is
required or optional.

Out of scope for this verdict: This verdict does not require rewriting the
NEEDS_CHANGES accountability structure, the DEFERRED criteria, or the
named-gap field definitions, which are correctly specified.
```

---

## 4. Integration Note

The PR Merge Gate in `CLAUDE.md` gates code merges on `VERDICT: APPROVED` in `oracle/verdicts/pr-{number}.md`. Document verdicts do not participate in this gate — a document verdict does not block or unblock a code PR merge.

What document verdicts do gate: the decision to act on a document's conclusions. A design doc with a NEEDS_CHANGES verdict should not be used as the authoritative basis for implementation decisions until the named gap is closed. A retro with a NEEDS_CHANGES verdict should not be archived to `memory/canonical/` as settled learning until the gap is addressed. The gate is semantic, not mechanical — no automated enforcement exists, and this protocol does not introduce one.

The accountable interpretation record for a document verdict lives in `oracle/verdicts/document-<slug>.md`. This is a parallel path to `oracle/verdicts/pr-{number}.md`, under the same directory, with a different naming convention that signals artifact type at a glance.
