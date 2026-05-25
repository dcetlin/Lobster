# MYR System as Oracle Knowledge Sink — Option A Design

WOS-UoW: uow_20260525_458a53

## Current State

The oracle agent writes knowledge back to `oracle/learnings.md` in two layers: a compact index table (Layer 1) keyed by date, PR number, and one-line learning summary, organized under broad categories (Test Design, Classification & Detection, etc.), and a full-detail section below (Layer 2) containing the complete prose entry. Before reviewing any PR, the oracle agent reads `oracle/learnings.md` and `oracle/golden-patterns.md` for orientation — to prime detection of recurring patterns before seeing the diff.

Write-back happens at the end of each oracle review round: the oracle agent appends one entry to the Layer 1 index table and a corresponding full prose block to Layer 2, then writes (or prepends to) `oracle/verdicts/pr-{number}.md`. The learnings file has no expiry, no query interface beyond grep, and no type system — entries are undifferentiated prose in a flat-markdown structure that grows linearly.

As of 2026-05-25, `oracle/learnings.md` contains approximately 80 learning entries across six named categories. The file is approximately 700 lines long. There is no structured way to query "all falsifications from test-design learnings in the last 30 days" or "learnings from PRs that touched steward.py."

---

## MYR Record Schema (Relevant Fields)

Source: `schema/myr-report.json` and `db/schema.sql` from `JordanGreenhall/myr-system` (v1.3.1).

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Pattern: `{node_id}-{YYYYMMDD}-{seq}` |
| `timestamp` | ISO8601 | Record creation time |
| `agent_id` | string | Producing agent identifier |
| `node_id` | string | Node where the record was created |
| `cycle.intent` | string | What OODA cycle this yield came from |
| `yield.type` | enum | `technique`, `insight`, `falsification`, `pattern` |
| `yield.question_answered` | string | The specific question this cycle resolved |
| `yield.evidence` | string | Observable evidence supporting the answer |
| `yield.what_changes_next` | string | What will be different in the next cycle |

### Optional Fields (High Relevance for Oracle Use Case)

| Field | Type | Description |
|-------|------|-------------|
| `session_ref` | string | External reference — use for PR number |
| `cycle.domain_tags` | string[] | Domain/category tags (FTS-indexed) |
| `cycle.context` | string | 1-3 sentence situation description |
| `yield.what_was_falsified` | string | What was proven NOT to work |
| `yield.transferable_to` | string[] | Other domains this applies to |
| `yield.confidence` | float (0-1) | Signal strength |
| `verification.operator_rating` | int (1-5) | Operator review score |
| `lineage.derived_from` | string | Source MYR ID if derived |

### SQLite FTS5 Index

The schema includes a `myr_fts` virtual table indexing: `id`, `cycle_intent`, `cycle_context`, `question_answered`, `evidence`, `what_changes_next`, `what_was_falsified`, `domain_tags`. This enables full-text search across all substantive fields.

---

## Mapping: Oracle Artifacts → MYR Records

### Mapping Table

| Oracle Artifact | MYR yield.type | Field Mapping | Gaps |
|----------------|----------------|---------------|------|
| Learning: "don't do X" antipattern | `falsification` | Learning text → `what_was_falsified`; detection heuristic → `what_changes_next`; PR → `session_ref` | No direct `category` field — use `domain_tags` |
| Learning: "correct approach is Y" | `technique` | Method description → `evidence` + `what_changes_next`; PR → `session_ref` | None significant |
| Learning: conceptual orientation shift | `insight` | Full prose → `cycle.context` + `yield.evidence`; category → `domain_tags` | None significant |
| Learning: recurring structural pattern across PRs | `pattern` | Pattern description → `yield.evidence`; instances → `cycle.context`; first PR → `session_ref` | Multi-PR patterns lose individual PR references unless linked via `lineage` |
| Oracle decision / ADR | `insight` | Decision rationale → `yield.evidence`; architecture change → `what_changes_next`; vision anchor → `cycle.context` | ADR metadata (Status, WOS ref) has no MYR equivalent — put in `cycle.context` |
| Oracle verdict | Not a MYR | Process artifact, not yield | Verdict summary could become a `technique` MYR only if it contains a new detection heuristic |

### Field-by-Field Notes

- **PR number → `session_ref`**: The oracle's primary provenance signal maps cleanly. `session_ref` is freeform — use `"pr-602"` format.
- **learnings.md category → `domain_tags`**: The six current categories (State Machines, Contract & Interface Design, Classification & Detection, Test Design, Timezone Handling, CLI Query Semantics) map directly as tags. Tags are multi-valued, so a learning that spans categories can carry both.
- **Date → `timestamp`**: Maps directly to ISO8601. The existing entry dates (YYYY-MM-DD) map without loss.
- **Learning text (full prose) → distributed across fields**: The prose entry is typically structured as observation + detection heuristic + fix. Split: observation → `yield.evidence`; detection heuristic → `yield.question_answered`; fix → `yield.what_changes_next`. For falsifications: the antipattern → `yield.what_was_falsified`.
- **No MYR equivalent for**: learnings.md Layer 1 vs Layer 2 distinction (MYR is single-layer by design), and the PR URL context that oracle readers typically click through to verify. The `cycle.context` field can carry a one-line PR summary.

---

## Prototype: "Boolean frozenset intersection over-eager" (PR #602) as MYR Record

Source entry from `oracle/learnings.md` Layer 1:
> `2026-04-04 | #602 | Boolean frozenset intersection over-eager for shared vocabulary — use weighted scoring or frequency thresholds instead of presence-only detection`

Full Layer 2 prose (inferred from Layer 1 — no separate Layer 2 block was present in the file for this entry; the one-liner is the canonical record):

```json
{
  "id": "lobster-20260404-001",
  "timestamp": "2026-04-04T00:00:00Z",
  "agent_id": "lobster-oracle",
  "node_id": "lobster-dcetlin",
  "session_ref": "pr-602",

  "cycle": {
    "intent": "Oracle review of PR #602 — boolean frozenset intersection for shared vocabulary detection",
    "domain_tags": ["classification", "detection", "code-review"],
    "context": "PR #602 introduced a classifier using frozenset intersection to detect shared vocabulary. The oracle review found the approach over-eager: any shared term triggered the classification regardless of frequency or weight."
  },

  "yield": {
    "type": "falsification",
    "question_answered": "Is boolean frozenset intersection sufficient for shared-vocabulary detection in a classification context?",
    "evidence": "PR #602: frozenset intersection fires on any shared term, including high-frequency stop-word-adjacent terms that appear in both signal and non-signal documents. No frequency or weight threshold means every shared term has equal classification weight — producing false positives at scale.",
    "what_changes_next": "Use weighted scoring or frequency thresholds instead of presence-only detection. A term present in 80% of documents should not carry the same classification weight as a term present in 5%.",
    "what_was_falsified": "Boolean frozenset intersection is not sufficient for shared-vocabulary classification when the vocabulary includes terms of varying discriminative value.",
    "transferable_to": ["nlp", "classification", "feature-selection"],
    "confidence": 0.9
  },

  "verification": {
    "operator_rating": null,
    "operator_notes": null,
    "verified_at": null,
    "verified_by_me": null
  },

  "network": {
    "signed_by": null,
    "shared_with": [],
    "synthesis_id": null
  }
}
```

**Prototype: "Module-level constant computed from env var" (PR #839) as MYR Record — second example:**

```json
{
  "id": "lobster-20260422-002",
  "timestamp": "2026-04-22T00:00:00Z",
  "agent_id": "lobster-oracle",
  "node_id": "lobster-dcetlin",
  "session_ref": "pr-839",

  "cycle": {
    "intent": "Oracle review of PR #839 — env var overrideability in test setup",
    "domain_tags": ["test-design", "configuration", "code-review"],
    "context": "PR #839 used monkeypatch.setenv to override a path constant derived from an env var at module scope. The test silently validated against the pre-patching value."
  },

  "yield": {
    "type": "falsification",
    "question_answered": "Can monkeypatch.setenv override a module-level constant computed from os.environ at import time?",
    "evidence": "When `_DEFAULT_PATH = Path(os.environ.get('VAR', default))` is evaluated at module scope, the constant is frozen at import time. monkeypatching the env var after import has no effect on the already-computed value. Tests calling the function with path=None silently tested the original import-time value.",
    "what_changes_next": "Either pass the override explicitly as a parameter, or reload the module inside the test. Detection: when a module computes a config value from an env var at module scope, grep tests for monkeypatch.setenv on that var — if present without module reload, the patch is silent.",
    "what_was_falsified": "monkeypatch.setenv cannot override a module-scope constant computed from os.environ at import time.",
    "transferable_to": ["test-design", "configuration-management"],
    "confidence": 0.95
  },

  "verification": {
    "operator_rating": null,
    "operator_notes": null,
    "verified_at": null
  },

  "network": {
    "signed_by": null,
    "shared_with": [],
    "synthesis_id": null
  }
}
```

**Non-lossy mapping verification:** Both prototypes capture the full information content of the learnings.md entries. The oracle category (Test Design, Classification & Detection) is preserved as `domain_tags`. The PR provenance is preserved as `session_ref`. The detection heuristic (currently embedded in the prose) is separated into `what_changes_next`, making it directly addressable. No information from the source entries was discarded.

---

## Queryability Assessment

### Queries Unlocked by MYR Backing

These queries are difficult or impossible with flat-markdown learnings.md but straightforward with the MYR SQLite + FTS5 backend:

1. **Type-filtered orientation before review** — "Give me all falsifications from test-design learnings before I review this PR":
   ```bash
   myr recall --type falsification --tags "test-design"
   # SQL: SELECT * FROM myr_reports WHERE yield_type='falsification' AND domain_tags LIKE '%test-design%' ORDER BY timestamp DESC
   ```
   Current approach: grep learnings.md for "Test Design" section, read all entries, mentally filter to antipatterns. Returns ~20 entries with no type separation.

2. **Date-windowed orientation** — "What learnings came from the last 30 days?":
   ```bash
   myr recall --intent "recent" --after 2026-04-25
   # SQL: SELECT * FROM myr_reports WHERE timestamp > '2026-04-25' ORDER BY timestamp DESC
   ```
   Current approach: scan Layer 1 tables by date column — possible but requires reading the entire file.

3. **File-scope orientation** — "What have past oracle reviews found about steward.py?":
   ```bash
   myr recall --intent "steward" --tags "code-review"
   # FTS5: SELECT * FROM myr_fts WHERE myr_fts MATCH 'steward'
   ```
   Current approach: grep learnings.md for "steward" — returns raw text blocks, no relevance ranking.

4. **Pattern deduplication** — "Has the mirror-constant pattern been flagged before?":
   ```bash
   myr recall --intent "mirror constant"
   # FTS5: SELECT * FROM myr_fts WHERE myr_fts MATCH 'mirror-constant OR "mirror constant"'
   ```
   Current approach: grep — works, but returns no confidence or recurrence count.

5. **Confidence-weighted orientation** — "Show me only high-confidence learnings about test design":
   ```bash
   # SQL: SELECT * FROM myr_reports WHERE confidence >= 0.85 AND domain_tags LIKE '%test-design%'
   ```
   No current equivalent. learnings.md has no confidence field.

6. **Cross-domain transfer** — "What classification learnings are transferable to NLP work?":
   ```bash
   # SQL: SELECT * FROM myr_reports WHERE transferable_to LIKE '%nlp%'
   ```
   No current equivalent.

7. **Pattern synthesis** — "Show me all learnings that recur across ≥3 PRs":
   ```bash
   node scripts/myr-synthesize.js --min-nodes 1 --tags "code-review"
   ```
   Partial equivalent: learnings.md notes recurrence in prose ("fourth recurrence") but no structural count.

### Queries Unchanged

These queries work equally well in both systems:

1. **Keyword search** — grep learnings.md vs FTS5 on myr_fts. Both return relevant entries; FTS5 adds ranking.
2. **Category browsing** — learnings.md sections vs `--tags` filter. Both are one-step.
3. **PR-specific lookup** — grep for "#602" vs `--session_ref pr-602`. Roughly equivalent.

### Write Overhead

| Operation | Current (flat-markdown) | MYR-backed |
|-----------|------------------------|-----------|
| New learning entry | Append 2 text blocks to `oracle/learnings.md` (one Layer 1 table row, one Layer 2 prose block) | Call `myr-store.js` or Python subprocess with structured fields; SQLite write + FTS5 index update |
| Oracle agent tooling | Edit tool call (file append) | Bash tool call: `myr capture --session-intent "..." --type falsification ...` or subprocess call to `node scripts/myr-store.js` |
| Per-entry time (wall clock) | ~1 second (file write) | ~2-3 seconds (Node.js startup + SQLite write) |
| Schema enforcement | None — prose is freeform | Schema validated at write time; `yield.type` enum-constrained |
| Pre-review retrieval | Read `oracle/learnings.md` once (~700 lines) | `myr recall --tags "..." --type ...` — returns filtered subset only |

The write overhead is slightly higher (~2x) but the retrieval benefit compounds with file size. At 80 entries, the current file is still manageable to read whole. At 500 entries across two years, selective pre-review retrieval becomes operationally necessary.

### Query Interface Fit for Oracle Orientation Use Case

The myr-system query interface (`myr-search.js` with `--query`, `--tags`, `--type`, `--limit`) supports the oracle's pre-review orientation step well:

- The oracle currently reads `oracle/learnings.md` wholesale before each review. With MYR, the equivalent would be: `myr recall --type falsification --tags "$(extract_tags_from_pr_diff)"` — returning only the most relevant prior learnings for the specific PR under review.
- The `--limit N` flag prevents context flooding — the oracle gets the top-5 most relevant learnings rather than all 80.
- The `--unverified` flag from `myr-verify.js` could be used to surface learnings awaiting operator validation — a function learnings.md has no equivalent for.

**Gap requiring adapter work:** The oracle's orientation step would need to extract domain tags from the PR diff before querying. This is not supported natively by myr-system — an adapter that maps changed files to domain tags (e.g., `steward.py` → `["wos", "steward"]`) would be needed. This is not a blocker but is integration work beyond the myr-system CLI.

---

## Recommendation

**Proceed with modifications.** The MYR schema maps cleanly to oracle learning artifacts with no information loss. The FTS5 + type + tag query interface materially improves oracle pre-review orientation as the learnings corpus grows. The two concrete prototypes demonstrate the mapping is non-lossy.

The modification required before proceeding: the oracle agent currently writes to `oracle/learnings.md` via a text-append operation in a single tool call. Replacing this with a `myr-store.js` subprocess call requires either (a) that `myr-system` be installed in the lobster environment (`npm install` in `~/lobster-workspace/projects/myr-system/`), or (b) a Python adapter that writes directly to the MYR SQLite database using the same schema. Option (b) is lower-dependency and keeps the oracle agent's tooling within the existing Python/bash ecosystem.

A migration path that appends to both sinks in parallel for a transition period (≥30 days) would allow validation that no learning entries are lost before retiring the flat-markdown backend. learnings.md would become a derived export of the MYR database rather than the primary store.

Do not implement without also designing the domain-tag extraction adapter (file-path → tag mapping) and the operator verification workflow for oracle-generated MYRs. These are blockers for production use but not for the design phase.

---

## Option B Note

Option B would position the oracle's prescription-source angle differently: rather than writing MYRs at the end of each oracle review cycle, a negentropic sweep agent would read the MYR database before each review cycle and surface high-confidence falsifications as anti-patterns to probe. This is architecturally cleaner — the oracle remains a producer of verdicts and learnings.md entries, while a separate sweep agent translates the MYR corpus into orientation prompts. Option B is deferred to cycle 2, contingent on Option A write-path validation. The MYR query interface is the same in both options; the difference is producer vs. consumer positioning of the oracle.
