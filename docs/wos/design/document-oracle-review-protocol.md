---
title: Document Oracle Review Protocol
status: draft
created: 2026-05-25
uow_id: uow_20260525_fb5bda
---

# Document Oracle Review Protocol

**WOS-UoW: uow_20260525_fb5bda**

## 1. Problem Statement

Code PRs pass through a Tier-1 structural gate that enforces oracle review before merge, leaving a machine-readable verdict trail. Substantial document artifacts (retros, design docs, audits) are covered only by an advisory gate in the dispatcher bootup file — a gate that can be forgotten after context compaction and leaves no traceability marker confirming review occurred. The fix: YAML frontmatter in substantial docs pointing to an oracle record file that contains adversarially-generated content a doc author cannot fabricate without running a real oracle pass.

---

## 2. Protocol Overview

Every substantial doc subject to this protocol carries a frontmatter block:

```yaml
---
oracle_reviewed: true
oracle_record: oracle/doc-verdicts/<doc-slug>.md
oracle_verdict: APPROVED        # or NEEDS_CHANGES (interim, not final)
oracle_round: 2                 # total rounds completed
oracle_date: 2026-05-25
---
```

**Field semantics:**

| Field | Required | Semantics |
|-------|----------|-----------|
| `oracle_reviewed` | Yes | `true` only after a VERDICT: APPROVED from the oracle record file. Never `false` (omit field entirely for exempt docs). |
| `oracle_record` | Yes | Path to the oracle record file relative to repo root. File must exist or the gate errors. |
| `oracle_verdict` | Yes | Reflects only the final verdict. Always `APPROVED` on delivery. |
| `oracle_round` | Yes | Total number of rounds run. 1 = approved on first pass. |
| `oracle_date` | Yes | ISO date of the final approval. |

The frontmatter is a **pointer**, not the proof. The oracle record file at `oracle_record` is the proof.

---

## 3. Oracle Record File Schema

**Location:** `oracle/doc-verdicts/<doc-slug>.md`

**Naming convention:** `<doc-slug>` is the doc filename without extension, prefixed by doc type. Examples:
- `retro-sprint-42.md`
- `design-wos-router.md`
- `synthesis-metabolic-cycle-q2.md`

For multi-round cases, the same file is appended in reverse chronological order (latest round at top). See Section 5 for the append format.

### Required fields (all rounds)

```markdown
VERDICT: APPROVED
Doc: <human-readable title>
Type: <retro | design-spec | synthesis | vision-strategy>
Round: <N>
Date: <YYYY-MM-DD>

---

## Adversarial Challenge List

_What the reviewer attempted to break. Specific to this document._

1. <challenge: the specific claim, assumption, or mechanism the reviewer attacked>
   - Method: <how the challenge was mounted — e.g., "searched for counterexamples", "traced causal chain", "checked against constraint-X in vision.yaml">
   - Outcome: <held | failed | modified>

2. <challenge>
   ...

_Minimum 3 challenges required. A challenge that does not name a specific mechanism or claim is invalid._

---

## Verdict Block

### What Held

- <specific claim or structure that survived adversarial challenge, with one sentence on why>

### What Changed

- <specific change made as a result of review, or "None" if no changes required>

### Gaps Deferred

- <anything left open with explicit rationale for deferral, or "None">

---

## Stage 1: Vision Alignment

**Prior entering review:** <the adversarial prior — why this doc might be wrong or premature>

<alignment verdict and reasoning>

---

## Stage 2: Quality Review

<concrete findings — gaps, inconsistencies, missing evidence>

---

## Patterns Applied

<learnings.md or golden-patterns.md entries that shaped the review>
```

### Filled-in example

```markdown
VERDICT: APPROVED
Doc: WOS Throttle Design
Type: design-spec
Round: 1
Date: 2026-05-20

---

## Adversarial Challenge List

1. The throttle gate assumes executor-heartbeat is the only dispatch path.
   - Method: Searched codebase for all callers of `_dispatch_via_claude_p` and checked whether any bypass executor-heartbeat.
   - Outcome: held — only executor-heartbeat dispatches UoWs in production; the direct path is marked legacy.

2. The TTL constant (90s) is asserted without measurement.
   - Method: Compared the TTL to the median UoW execution time from the last 7 days of registry logs.
   - Outcome: modified — TTL raised to 120s after finding p95 execution time is 105s; original 90s would have caused false orphan reclassifications.

3. The concurrency cap of 3 is presented as safe without analyzing queue depth behavior under burst.
   - Method: Traced what happens when 10 UoWs arrive simultaneously: first 3 dispatch, remaining 7 stay queued; checked whether the queue has a max-depth guard.
   - Outcome: held — the registry status field prevents double-dispatch; queue is unbounded but that is acceptable at current throughput.

---

## Verdict Block

### What Held

- The executor-heartbeat as sole dispatch path is structurally sound — no bypass path found.
- The concurrency cap logic is correct for current throughput; no queue guard needed yet.

### What Changed

- TTL constant raised from 90s to 120s based on p95 measurement.

### Gaps Deferred

- Queue depth monitoring at scale: deferred; throughput does not warrant it yet.

---

## Stage 1: Vision Alignment

**Prior entering review:** This design might be prematurely optimizing for throughput we do not have, adding complexity the current phase does not justify.

The throttle gate is additive and reversible. It does not foreclose higher-throughput architectures. Consistent with principle-4 (wire what exists before building more). Alignment: ALIGNED.

---

## Stage 2: Quality Review

One gap found (TTL constant ungrounded) and resolved by measurement. No structural gaps remain.

---

## Patterns Applied

**learnings.md PR #717** ("Constants asserted without measurement are latent configuration bugs"): Applied to the TTL and concurrency cap. Measurement resolved TTL; cap was justified by queue analysis.
```

---

## 4. Scope Boundary

### Taxonomy

The dispatcher classifies docs by their file path pattern or a `doc_type` frontmatter field — not by reading content.

| Doc Type | Path Pattern | Oracle Required? |
|----------|-------------|-----------------|
| **retro** | `docs/retros/`, filename contains `retro` or `retrospective` | **REQUIRES** |
| **design-spec** | `docs/design/`, `docs/wos/design/` | **REQUIRES** |
| **synthesis doc** | filename contains `synthesis`, `analysis`, `audit` | **REQUIRES** |
| **vision/strategy doc** | `docs/vision/`, `vision.yaml`, filename contains `vision` or `strategy` | **REQUIRES** |
| **session note** | `memory/canonical/`, filename contains `session-note`, `handoff` | **EXEMPT** |
| **oracle decision entry** | `oracle/verdicts/`, `oracle/doc-verdicts/` | **EXEMPT** (oracle produces these; they are not reviewed by oracle) |
| **working/scratch doc** | `docs/scratch/`, filename contains `scratch`, `draft`, `wip` | **EXEMPT** |
| **sprint plan** | filename contains `sprint` and extension `.md` | **EXEMPT** (see edge cases) |
| **wos-completion-report** | filename matches `wos-completion-report-*` | **EXEMPT** |
| **philosophy-explore** | `philosophy/` directory | **EXEMPT** |

**Classification rule:** If a doc matches a REQUIRES pattern, it requires review regardless of length. The `doc_type` frontmatter field overrides path-based classification when present; this allows a short doc to explicitly self-declare its type.

### Edge Cases

**One-page sprint plan:** EXEMPT. Sprint plans are operational scheduling artifacts — their audience is the executor (short-term), not decision-makers or future reviewers. They do not assert architectural claims or synthesize evidence. A sprint plan that contains a significant design rationale section should be split: the plan portion stays exempt, the rationale becomes a design-spec and requires review.

**WOS completion report:** EXEMPT. Completion reports are factual records of what ran — they do not assert design claims or synthesize across sources. Their durability is archival (what happened) rather than prescriptive (what to do). Oracle review of factual records would produce false adversarial challenges with no traction.

**Philosophy-explore session note:** EXEMPT. These are exploratory thought records, not prescriptive documents intended to drive decisions. Their audience is internal (the session itself, possibly future philosophical context), not downstream decision-makers. If a philosophy-explore doc graduates into a design proposal, it re-enters scope as a design-spec.

**Synthesis doc vs. design-spec:** When a doc synthesizes evidence without making implementation claims (e.g., "here is what the data shows"), it is a synthesis doc and REQUIRES review. When it proposes a concrete implementation (e.g., "here is what we should build and how"), it is a design-spec and also REQUIRES review. Both are in scope; the distinction matters only for the oracle reviewer's framing.

**Scope anchor:** The REQUIRES boundary is anchored to intended audience and durability:
- REQUIRES: doc is intended for Dan's review as a decision input OR is expected to persist as a reference for future decisions
- EXEMPT: doc is ephemeral, operational, factual-record-only, or exclusively self-referential (oracle verdicts reviewing themselves)

---

## 5. Chain of Validation

### Q2 Decision: Single File with Rounds Appended

**Decision:** Single oracle record file per doc, with rounds appended in reverse chronological order (latest round at top, earliest at bottom).

**Justification:**

| Criterion | Single file | One file per round |
|-----------|------------|-------------------|
| Auditability | Full history in one place; no directory traversal needed | History requires reading N files in sequence |
| Fakeability resistance | Appended content includes cross-references to prior gaps; harder to fabricate a coherent gap-resolution history | Per-round files are easier to fake in isolation; no cross-referencing pressure |
| Filesystem cleanliness | One file per doc; `oracle/doc-verdicts/` stays flat | N×docs files; directory becomes noisy |
| Dispatcher simplicity | `oracle_record` field points to one path; existence check is O(1) | Dispatcher would need to enumerate files or track N paths |

**Append format for multi-round cases:**

```markdown
VERDICT: APPROVED          ← Always the final verdict at top of file
Doc: <title>
Type: <type>
Round: 2                   ← Total rounds
Date: <date of final approval>

---

## Round 2 — <date>

<full Round 2 content: challenges, verdict block, alignment, quality review>

---

## Round 1 — <date>

VERDICT: NEEDS_CHANGES     ← Round 1 verdict preserved inline
<full Round 1 content>
```

The first line of the file (`VERDICT: APPROVED` or `VERDICT: NEEDS_CHANGES`) always reflects the current/final verdict. The dispatcher reads only this line to check approval status — identical to the PR verdict file convention. Round 1 content survives in full at the bottom of the file.

### Q4: Iterative Reviews — Final State After Round 2 Approval

**Frontmatter after Round 2 APPROVED:**

```yaml
oracle_reviewed: true
oracle_record: oracle/doc-verdicts/design-my-doc.md
oracle_verdict: APPROVED
oracle_round: 2
oracle_date: 2026-05-25
```

`oracle_verdict` reflects only the final verdict. The round number records how many passes were required. The frontmatter does not carry Round 1's `NEEDS_CHANGES` — the oracle record file does.

**Oracle record file after Round 2 APPROVED:** Round 2 is at the top; Round 1 content (including its `VERDICT: NEEDS_CHANGES` line) is preserved as a dated section below the horizontal rule. The full challenge list and gap resolution from Round 1 is present and readable. Nothing from Round 1 is deleted.

---

## 6. Enforcement Mechanism

### Q5: File-Not-Found as Hard Error

**Where the check lives:** The dispatcher performs the existence check when a subagent delivers a substantial doc artifact — specifically, when the subagent calls `write_result` with `artifacts` containing a path to a `.md` file that falls within the REQUIRES taxonomy. The check fires before the dispatcher marks the message processed or relays the artifact to Dan.

**Check sequence:**

1. Subagent delivers artifact (via `write_result` or `send_reply` with file ref)
2. Dispatcher reads the file's frontmatter
3. If `oracle_reviewed: true` → read `oracle_record` path → check file exists
4. If file does not exist → **hard error** (not a warning)
5. If `oracle_reviewed` is absent and doc falls within REQUIRES taxonomy → same hard error path

**Error surfacing:** The dispatcher does NOT silently log the error. It:
1. Sends a Telegram message to Dan: `"Oracle record missing for [doc path]. Artifact not delivered. oracle_record field points to [path] which does not exist."`
2. Opens a GitHub issue on `SiderealPress/lobster` with label `oracle-gate-miss` and body containing the artifact path, the missing oracle record path, and the task_id that produced it.
3. Does NOT deliver the artifact to Dan until the oracle record file exists and the frontmatter check passes.

**Agent performing the check:** The dispatcher itself (not a subagent). The check is synchronous and inline with artifact delivery — it cannot be deferred or delegated, because the artifact delivery is the moment of enforcement.

---

## 7. Anti-Gaming Properties

### Q1: Why the Oracle Record is Structurally Unfakeable

The frontmatter (`oracle_reviewed: true`, `oracle_record: <path>`) is trivially stampable by a doc author. The oracle record file at that path is not.

Three structural properties make the oracle record unfakeable by the doc author:

**Property 1 — Adversarial Challenge List requires mechanism-specific attacks.** The challenge list requires naming specific claims or mechanisms in the doc and describing the attack method (what was checked, against what). A doc author who writes their own oracle record must either: (a) accurately describe real weaknesses they found in their own doc (which is useful oracle work, not gaming), or (b) fabricate plausible-sounding attacks that will be visibly generic upon inspection. The minimum-3-challenge requirement and the "mechanism named" validity rule create a floor that generic self-assessment cannot clear.

**Property 2 — Gap resolution history creates cross-referencing pressure.** In multi-round cases, the oracle record's Round 2 section references Round 1 gaps by name and states their disposition. A fabricated oracle record that invents Round 1 gaps must invent plausible gap names, plausible resolution content, and invent a coherent reason Round 1 did not approve — all while the doc author's own document is visible to falsify the gaps against. This is possible but requires genuinely more work than running the oracle agent.

**Property 3 — The oracle agent writes the file; the doc author does not.** The protocol assigns oracle record creation to the oracle agent, not the implementation subagent or the doc author. The dispatcher dispatches the oracle agent with the doc path and the agent writes to `oracle/doc-verdicts/`. A doc author has no mechanism in the normal workflow to write to this path themselves — doing so requires either running the oracle agent (correct) or directly editing the file outside the workflow (visible in git blame). Git blame is the final enforcement layer: if an oracle record file is authored by the same commit that authored the doc, the oracle agent did not write it.

**Summary:** The frontmatter is a pointer that proves nothing. The oracle record is the proof, and its content (challenge list + gap resolution + vision alignment reasoning) is only producible by an agent that has genuinely attempted adversarial review.

---

## 8. Proposed Tier-1 Gate Row for CLAUDE.md

The following row is a draft for the Tier-1 gate table in CLAUDE.md. Use the PR Merge Gate row as the formatting model. **This row should not be added to CLAUDE.md directly — it requires an oracle review and a separate implementation PR.**

| Gate | Trigger (one sentence) | Enforcement |
|------|----------------------|-------------|
| **Doc Oracle Gate** | A subagent delivers a substantial document artifact (retro, design-spec, synthesis doc, or vision/strategy doc — classified by path pattern or `doc_type` frontmatter) intended for Dan's review or as a durable reference. | Advisory — before relaying any artifact matching the REQUIRES taxonomy: (1) check frontmatter for `oracle_reviewed: true` and a valid `oracle_record` path; (2) verify the oracle record file exists at that path; (3) verify the first line of the oracle record is `VERDICT: APPROVED`. If any check fails: do not deliver the artifact, send Dan a Telegram alert naming the missing or failing element, and open a GitHub issue with label `oracle-gate-miss`. Flow: subagent writes doc → dispatcher dispatches oracle agent → oracle writes `oracle/doc-verdicts/<doc-slug>.md` → if `VERDICT: APPROVED`, subagent adds frontmatter and delivers artifact; if `VERDICT: NEEDS_CHANGES`, dispatcher dispatches fix subagent → re-oracle → repeat. Round cap same as PR Merge Gate: Rounds 1–2 auto-fix; Round 3 notify Dan; Round 4+ escalate. |

---

## 9. Deferred Decisions

**Archive convention for approved doc oracle records.** The PR Merge Gate moves approved verdict files to `oracle/verdicts/archive/`. Whether approved doc oracle records should similarly be archived (moved to `oracle/doc-verdicts/archive/`) or kept in-place is left open. Tradeoff: in-place keeps the `oracle_record` pointer valid without updating frontmatter; archive provides filesystem cleanliness at the cost of requiring frontmatter pointer updates.

**Retroactive review of existing docs.** Whether existing substantial docs in the repo should be backfilled with oracle review is intentionally deferred. Backfilling is labor-intensive and of uncertain value for docs whose decisions are already implemented. The protocol applies to new artifacts from the date of Tier-1 gate adoption.

**Oracle agent seeding for doc reviews vs. PR reviews.** The oracle agent's adversarial prior for code PRs is "this PR is solving the wrong problem." Whether doc reviews should use a different prior (e.g., "this doc is asserting claims without evidence" or "this design is premature given the current phase") is left to the oracle agent's judgment. A doc-type-specific prior table could be added if the oracle agent's default prior proves poorly calibrated for doc reviews.

**`doc_type` frontmatter field as an override mechanism.** The protocol allows a `doc_type` frontmatter field to override path-based classification. The exact allowed values and their mapping to REQUIRES/EXEMPT is specified in Section 4 but not yet enforced by any tool. A validation script or hook that checks `doc_type` values against the taxonomy is deferred.
