> **Status:** Reference glossary. Read-only for agents. Not a decision substrate.
> This document resolves WOS component naming. For intent queries, use `vision.yaml`.

# WOS Component Glossary (V3)

This document is the authoritative component glossary for the Work Orchestration
System. It resolves naming ambiguities introduced across V1, V2, and V3 design
documents.

---

## Component Map

### Germinator (`src/orchestration/germinator.py`)

**Role:** Classifies the register of a UoW at germination time.

**When it runs:** Called by the GitHub Issue Cultivator (scheduled job) at the
moment a GitHub issue is promoted into the WOS registry.

**What it produces:** A `RegisterClassification` containing `register`,
`gate_matched`, `confidence`, and `rationale`. The `register` value is written
to the UoW at INSERT time and is **immutable thereafter**.

**Naming note:** The V3 proposal (`wos-v3-proposal.md`) uses the term "Cultivator"
in two different senses:
1. The pearl-vs-seed classifier (aspirational, not yet built)
2. The register classifier (now implemented as `germinator.py`)

To prevent propagating this ambiguity:
- **Germinator** = the register classifier in `germinator.py`
- **GitHub Issue Cultivator** = the scheduled job (`cultivator.py`, job name
  `github-issue-cultivator`) that promotes open GitHub issues into the registry
- The aspirational pearl-vs-seed classifier, when built, should be named
  **Seed Classifier** or **Pearl Classifier** — not "Cultivator"

---

### GitHub Issue Cultivator (`src/orchestration/cultivator.py`)

**Role:** Promotes open GitHub issues from `dcetlin/Lobster` into the WOS registry
as proposed UoWs.

**When it runs:** Scheduled job (`github-issue-cultivator` in jobs.json).

**What it does:**
1. Fetches open issues from GitHub via `gh issue list`
2. Skips meta-tracking issues (labels: `wos-phase-2`, `tracking`)
3. Extracts success_criteria from issue body
4. Calls the **Germinator** to classify register
5. Calls `Registry.upsert()` with title, success_criteria, and register

**Note:** This component retains its existing scheduled job name
`github-issue-cultivator`. The naming ambiguity exists only in design docs.

---

### Registry (`src/orchestration/registry.py`)

**Role:** SQLite-backed store for Units of Work. All writes use BEGIN IMMEDIATE
transactions. Audit log entry is written in the same transaction as each registry
change.

**V3 additions (migration 0007):**
- `register` field: attentional configuration, classified by Germinator, immutable
- `uow_mode` field: mirrors register; used by Executor for context selection
- `closed_at` / `close_reason` fields: delivery≠closure (see below)
- `corrective_traces` table: learning artifacts from executor returns

---

### Delivery vs. Closure (V3 Distinction)

V2 conflated "executor delivered result.json" with "loop is closed." V3 makes
this distinction explicit:

| Event | Field written | Who writes it |
|-------|--------------|---------------|
| Executor delivers output | `completed_at` | Executor (via `complete_uow`) |
| Steward declares loop done | `closed_at` + `close_reason` | Steward (explicit decision) |

The `done` transition requires `close_reason` prose. A UoW with `completed_at`
set but `closed_at` NULL has been delivered but not closed — the Steward must
still evaluate the output before declaring done.

---

### Register Taxonomy

| Register | Completion evaluation | Dispatch |
|----------|----------------------|---------|
| `operational` | Machine-observable gate | Standard executor |
| `iterative-convergent` | Gate score improving across cycles | Ralph-loop style executor |
| `philosophical` | Human judgment only | Frontier-writer or Dan surface |
| `human-judgment` | Dan's explicit confirmation | Design-review primitive |

Register is classified at germination by the **Germinator** using a 4-gate
ordered algorithm. See `germinator.py` docstring for the full algorithm.

---

### Corrective Traces (`corrective_traces` table)

**Role:** Learning artifacts written by every executor return (complete, partial,
blocked, or failed).

**Schema:** `uow_id`, `register`, `execution_summary`, `surprises` (JSON array),
`prescription_delta`, `gate_score` (JSON or null), `created_at`.

**Contract:** Absence of a trace is logged as a contract violation but does not
block Steward re-entry (unlike `result.json` absence, which blocks completion
declaration).

**Purpose:** Traces accumulate in the garden. The Steward reads them at diagnosis
time via the `idx_corrective_traces_uow_id` index. This is how the system learns
without a training loop.

---

### Steward (`src/orchestration/steward.py`)

Diagnoses UoWs and prescribes workflow primitives. In V3, the Steward:
- Reads `register` to select an appropriate execution context
- Reads corrective traces from prior executor returns
- Fires the Register-Mismatch Gate before writing a prescription
- Requires `close_reason` when transitioning a UoW to `done`
- Surfaces philosophical/human-judgment UoWs to Dan rather than auto-closing

---

*Last updated: 2026-04-04 (WOS V3 PR1 — Germinator)*
