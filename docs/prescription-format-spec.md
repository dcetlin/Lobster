# Prescription Format Specification

*WOS-UoW: uow_20260502_08b895*
*Schema version: 1.0.0*
*Effective: 2026-05-03*

---

## Introduction

A **prescription** is the structured artifact the Steward produces at the end of each
diagnosis cycle and hands to the Executor as its complete work order. It sits at the
central handoff point of the WOS pipeline: the Steward writes one prescription per cycle
and the Executor consumes exactly that prescription before beginning execution. The format
exists to enforce determinism (the Executor has all information it needs without back-
channel queries), auditability (every field is traceable to a UoW event), and
composability (each section is independently readable and validatable, enabling fan-out
workflows and corrective-trace injection without reformatting).

---

## Seven-Section Reference

A prescription is a JSON object with exactly seven top-level keys. All seven are required.

---

### 1. `diagnosis`

**Purpose:** Captures what the Steward observed that prompted this prescription — the
upstream signal that moved the UoW from `ready-for-steward` into the prescribe branch.

**Required:** yes

| Field | Type | Required | Description | Constraints |
|-------|------|----------|-------------|-------------|
| `signal` | string | yes | One-sentence summary of the triggering observation. | Non-empty. |
| `reentry_posture` | string | yes | The executor reentry posture assigned by `_diagnose_uow()`. | Enum: `first_execution`, `executor_orphan`, `execution_complete`, `execution_failed`, `execution_partial`, `execution_blocked`, `startup_sweep_possibly_complete`, `diagnosing_orphan`, `executing_orphan`. |
| `completion_gap` | string | yes | Human-readable rationale for why the UoW is not yet `done`. On `first_execution` this is an empty string. | String; may be empty only when `reentry_posture == "first_execution"`. |
| `executor_outcome` | string or null | yes | The `outcome` field from the Executor's `result.json`, if present. Null on first execution or when no result file exists. | Enum: `complete`, `partial`, `failed`, `blocked`, or `null`. |
| `prior_cycle_count` | integer | yes | `steward_cycles` value at the time this prescription was produced. Zero on first execution. | Integer ≥ 0. |

**Invariants:**
- When `reentry_posture == "first_execution"`, `prior_cycle_count` must be `0` and `completion_gap` must be `""`.
- When `executor_outcome` is non-null, it must be one of the `ExecutorOutcome` values defined in `executor-contract.md`.

---

### 2. `prescription`

**Purpose:** The Steward's core recommendation — what the Executor should do and why, distilled from the diagnosis.

**Required:** yes

| Field | Type | Required | Description | Constraints |
|-------|------|----------|-------------|-------------|
| `summary` | string | yes | One-to-two sentence statement of the work to be done. Scoped to this cycle only. | Non-empty. Max 500 characters. |
| `instructions` | string | yes | Complete, actionable natural-language instructions for the Executor. Must be executable by an autonomous agent without clarification. | Non-empty. |
| `estimated_cycles` | integer | yes | Steward's estimate of how many Executor passes this prescription will require before `done`. | Integer in [1, 5]. |
| `minimum_viable_output` | string | yes | The single concrete deliverable that defines the floor for this cycle. Corresponds to the "Minimum viable output:" line in the Executor prompt. | Non-empty. |
| `boundary` | string | yes | The hard scope exclusion that the Executor must not cross. Corresponds to the "Boundary: do not …" line in the Executor prompt. | Non-empty. |

**Invariants:**
- `instructions` must embed `minimum_viable_output` and `boundary` as recognizable blocks (the Executor dispatch conventions require them; see `CLAUDE.md` dispatch template gate).
- `estimated_cycles` must not exceed `5`; if the Steward's LLM prescriber returns a higher value, it is clamped.

---

### 3. `workflow`

**Purpose:** The execution plan — agent type, ordered steps, and optional fan-out structure when multiple Executor perspectives are needed.

**Required:** yes

| Field | Type | Required | Description | Constraints |
|-------|------|----------|-------------|-------------|
| `agent_type` | string | yes | The Executor dispatch type that will process this prescription. | Enum: `functional-engineer`, `lobster-ops`, `lobster-generalist`, `lobster-meta`, `frontier-writer`, `design-review`. |
| `steps` | array of string | yes | Ordered list of execution steps. Each step is one imperative sentence. | Array length ≥ 1. Each element non-empty. |
| `fan_out` | array of object | no | Present only for multi-perspective workflows. Each entry describes one parallel Executor agent. When absent or null, the workflow is single-agent. | See fan-out object schema below. May be null or omitted. |

**Fan-out entry object** (each element of `fan_out`):

| Field | Type | Required | Description | Constraints |
|-------|------|----------|-------------|-------------|
| `agent_type` | string | yes | Executor type for this parallel branch. | Same enum as `workflow.agent_type`. |
| `focus` | string | yes | One sentence describing what aspect this branch investigates or produces. | Non-empty. |
| `task_id_suffix` | string | no | Optional suffix appended to the UoW task_id to form this branch's task_id (e.g. `"-reviewer-1"`). | String. |

**Invariants:**
- `workflow.agent_type` must be compatible with the UoW's register as defined in the register-executor compatibility table (see `wos-v3-steward-executor-spec.md`, Change 3). The Steward's register-mismatch gate enforces this before the prescription is written.
- When `fan_out` is present, `workflow.steps` describes the merge/synthesis step performed after all fan-out branches complete.
- `fan_out` entries must each have a distinct `focus`.

---

### 4. `constraints`

**Purpose:** Hard limits that the Executor must not violate. These are non-negotiable exclusions from scope, system modifications to avoid, and authority boundaries.

**Required:** yes

| Field | Type | Required | Description | Constraints |
|-------|------|----------|-------------|-------------|
| `boundary` | string | yes | The primary scope exclusion. Mirrors `prescription.boundary` but lives here for structural completeness — the constraints section is the authoritative location for all "do not" rules. | Non-empty. |
| `no_modify` | array of string | no | List of specific files, modules, tables, or systems the Executor must not touch. | Array of non-empty strings. May be empty array. |
| `no_deploy` | boolean | no | If true, the Executor must not push to remote, merge PRs, or modify production infrastructure. Defaults to false. | Boolean. |
| `max_cycles` | integer | no | Override for the maximum number of Executor passes before the Steward surfaces to Dan. Defaults to the system hard cap (5). | Integer in [1, 10]. |

**Invariants:**
- `constraints.boundary` must not contradict `prescription.minimum_viable_output`. If the boundary would prevent producing the minimum viable output, the prescription is ill-formed.
- When `no_deploy` is true, the Executor must not call `git push`, `gh pr merge`, or any equivalent deployment action.

---

### 5. `success_criteria`

**Purpose:** The verifiable conditions that the Steward will evaluate to determine whether the UoW is `done`. The Executor does not self-evaluate against these — they are the Steward's evaluation target.

**Required:** yes

| Field | Type | Required | Description | Constraints |
|-------|------|----------|-------------|-------------|
| `check` | string | yes | Human-readable description of the verification method: what the Steward will inspect, what file must exist, what content must be present. | Non-empty. |
| `artifacts` | array of string | no | Absolute paths or glob patterns of files or directories that must exist and be non-empty after execution. | Array of non-empty strings. May be empty array. |
| `commands` | array of string | no | Shell commands the Steward or reviewer may run to verify completion (e.g., `uv run -m pytest`, `gh pr view <number>`). | Array of non-empty strings. May be empty array. |
| `gate_command` | string or null | no | For `iterative-convergent` register UoWs: the canonical gate command whose score the Executor must report in `trace.json`. Null for all other registers. | String or null. |

**Invariants:**
- `success_criteria.check` must be specific enough that two independent readers produce the same pass/fail judgment.
- For `iterative-convergent` UoWs, `gate_command` must be present and non-null. The Executor writes its gate score to `trace.json`; the Steward reads it via the trace injection path (see `wos-v3-steward-executor-spec.md`, Change 2).
- The Steward evaluates `success_criteria` before reading the Executor's `result.json` outcome. This re-read-first protocol prevents anchoring to the Executor's self-reported status.

---

### 6. `dan_context`

**Purpose:** Human-facing context that orients the user (Dan) to this UoW's position within a larger initiative. All fields are freeform prose — this section is not machine-routed; it exists for human readability in surface-to-Dan notifications and morning briefings.

**Required:** yes

| Field | Type | Required | Description | Constraints |
|-------|------|----------|-------------|-------------|
| `orientation` | string | no | One or two sentences placing this UoW in its broader initiative context. | Freeform string. May be empty. |
| `priority_signal` | string | no | Why this UoW matters now — the urgency or dependency signal that moved it to the front of the queue. | Freeform string. May be empty. |
| `open_questions` | array of string | no | Questions the Executor cannot resolve and that may require Dan's input. The Executor surfaces these via `trace.json.surprises` when they are discovered during execution. | Array of strings. May be empty array. |
| `load_bearing_assumption` | string | no | The single most important assumption this prescription rests on. If this assumption is wrong, the prescription should be revised before the Executor proceeds. | Freeform string. May be empty. |

**Invariants:**
- All `dan_context` fields are optional and may be empty strings or empty arrays. The section itself is required (must be present as a JSON object), but may contain only empty values.
- `dan_context` content must not contradict the `prescription` or `constraints` sections. If there is a conflict, the `constraints` section is authoritative.

---

### 7. `audit_metadata`

**Purpose:** Machine-readable tracking fields used by the WOS registry, the corrective-trace system, and the morning briefing to correlate this prescription with its UoW, cycle history, and schema version.

**Required:** yes

| Field | Type | Required | Description | Constraints |
|-------|------|----------|-------------|-------------|
| `uow_id` | string | yes | The UoW ID this prescription belongs to. | Pattern: `^uow_\d{8}_[a-f0-9]{6}$`. Must match the UoW record's `id` field. |
| `cycle` | integer | yes | The Steward cycle number at the time this prescription was produced. Zero-indexed: `0` on first execution. | Integer ≥ 0. Must equal `diagnosis.prior_cycle_count`. |
| `executor_posture` | string | yes | The execution posture this prescription targets. Derived from the Steward's reentry classification. | Enum: `first_execution`, `continuation`, `remediation`. |
| `schema_version` | string | yes | Semver string identifying the prescription format version. | Pattern: `^\d+\.\d+\.\d+$`. Current version: `1.0.0`. |
| `prescribed_at` | string | yes | ISO-8601 UTC timestamp of when this prescription was produced. | Format: `date-time`. |
| `prescribed_skills` | array of string | no | Skill identifiers to activate before dispatching the Executor (e.g., `["claude-api", "functional-engineer"]`). | Array of non-empty strings. May be empty array. |

**Invariants:**
- `audit_metadata.uow_id` must match the UoW record's `id` field exactly. A prescription with a mismatched `uow_id` is rejected by the Executor before any execution begins.
- `audit_metadata.cycle` must equal `diagnosis.prior_cycle_count`. A prescription where these differ is internally inconsistent and must not be dispatched.
- `executor_posture` maps from `reentry_posture` as follows:
  - `first_execution` → `first_execution`
  - Any orphan posture (`executor_orphan`, `executing_orphan`, `diagnosing_orphan`) → `continuation`
  - `execution_complete` with `is_complete=False`, `execution_partial`, `execution_failed` → `continuation`
  - `execution_failed` with corrective intent → `remediation`
  - `execution_blocked` after unblock → `continuation`

---

## Workflow Types

Three worked examples follow showing a complete prescription JSON object for each
workflow type. All three are valid against `prescription-format.schema.json`.

---

### Example 1 — Investigation Pass

A single-agent diagnostic task. The Executor is asked to inspect current state and
report findings; no code changes are produced. Register: `operational`.

See: [`docs/examples/investigation-pass.json`](examples/investigation-pass.json)

```json
{
  "diagnosis": {
    "signal": "UoW uow_20260501_a1b2c3 entered ready-for-steward on first execution; source is a GitHub issue requesting an audit of the WOS throttle configuration.",
    "reentry_posture": "first_execution",
    "completion_gap": "",
    "executor_outcome": null,
    "prior_cycle_count": 0
  },
  "prescription": {
    "summary": "Audit the current WOS throttle configuration and report findings against the documented defaults in wos-throttle-design.md.",
    "instructions": "Read ~/lobster-workspace/data/wos-config.json and ~/lobster/src/orchestration/wos_throttle.py. Compare all throttle parameters against the defaults documented in docs/wos-throttle-design.md. Write findings to ~/lobster-workspace/workstreams/uow_20260501_a1b2c3/audit-report.md. Include: current values, documented defaults, any discrepancies, and a one-sentence recommendation for each discrepancy.\n\nMinimum viable output: ~/lobster-workspace/workstreams/uow_20260501_a1b2c3/audit-report.md with all findings documented.\nBoundary: do not modify wos-config.json, wos_throttle.py, or any production configuration.",
    "estimated_cycles": 1,
    "minimum_viable_output": "~/lobster-workspace/workstreams/uow_20260501_a1b2c3/audit-report.md with all findings documented",
    "boundary": "do not modify wos-config.json, wos_throttle.py, or any production configuration"
  },
  "workflow": {
    "agent_type": "lobster-generalist",
    "steps": [
      "Read wos-config.json and wos_throttle.py",
      "Read docs/wos-throttle-design.md for documented defaults",
      "Compare current values against defaults and note discrepancies",
      "Write audit-report.md with findings and recommendations"
    ],
    "fan_out": null
  },
  "constraints": {
    "boundary": "do not modify wos-config.json, wos_throttle.py, or any production configuration",
    "no_modify": [
      "~/lobster-workspace/data/wos-config.json",
      "~/lobster/src/orchestration/wos_throttle.py"
    ],
    "no_deploy": true,
    "max_cycles": 2
  },
  "success_criteria": {
    "check": "audit-report.md exists at the specified path, is non-empty, and contains sections for current values, documented defaults, and recommendations.",
    "artifacts": [
      "~/lobster-workspace/workstreams/uow_20260501_a1b2c3/audit-report.md"
    ],
    "commands": [],
    "gate_command": null
  },
  "dan_context": {
    "orientation": "Part of the WOS throttle calibration initiative; this audit provides the baseline before any parameter changes.",
    "priority_signal": "WOS was running at 100% dispatch rate during the April sprint and showed no backpressure — audit will confirm whether defaults need adjustment.",
    "open_questions": [
      "Is the current max_concurrent_uows setting intentional or a migration oversight?"
    ],
    "load_bearing_assumption": "wos-config.json reflects the currently active throttle state (not a cached copy)."
  },
  "audit_metadata": {
    "uow_id": "uow_20260501_a1b2c3",
    "cycle": 0,
    "executor_posture": "first_execution",
    "schema_version": "1.0.0",
    "prescribed_at": "2026-05-01T14:32:00Z",
    "prescribed_skills": []
  }
}
```

---

### Example 2 — Design Review (Multi-Perspective Fan-Out)

A two-agent fan-out where each Executor produces an independent analysis; the results are
merged by the Steward. Register: `human-judgment`.

See: [`docs/examples/design-review.json`](examples/design-review.json)

```json
{
  "diagnosis": {
    "signal": "UoW uow_20260428_d4e5f6 returned from first execution with outcome=partial; Executor determined that the proposed API schema change requires independent security and ergonomics review before a recommendation can be made.",
    "reentry_posture": "execution_partial",
    "completion_gap": "Executor completed investigation but flagged that the design decision requires two independent perspectives before a recommendation is safe to make.",
    "executor_outcome": "partial",
    "prior_cycle_count": 1
  },
  "prescription": {
    "summary": "Conduct parallel security and ergonomics review of the proposed v2 API schema; merge findings into a single recommendation document.",
    "instructions": "Two parallel Executor agents will review the proposed v2 API schema (see ~/lobster-workspace/workstreams/uow_20260428_d4e5f6/proposed-schema.json).\n\nAgent 1 (security focus): Evaluate the proposed schema for authentication gaps, data exposure risks, and injection surface area. Write findings to ~/lobster-workspace/workstreams/uow_20260428_d4e5f6/security-review.md.\n\nAgent 2 (ergonomics focus): Evaluate the proposed schema for API consistency, naming conventions, and consumer usability. Write findings to ~/lobster-workspace/workstreams/uow_20260428_d4e5f6/ergonomics-review.md.\n\nAfter both agents complete, the Steward will merge findings into a final recommendation.\n\nMinimum viable output: Both review documents present and non-empty.\nBoundary: do not modify the proposed schema or any production API files.",
    "estimated_cycles": 2,
    "minimum_viable_output": "Both security-review.md and ergonomics-review.md present and non-empty",
    "boundary": "do not modify the proposed schema or any production API files"
  },
  "workflow": {
    "agent_type": "lobster-generalist",
    "steps": [
      "Merge findings from security-review.md and ergonomics-review.md",
      "Produce a single recommendation document with a clear go/no-go signal"
    ],
    "fan_out": [
      {
        "agent_type": "lobster-generalist",
        "focus": "Security review: authentication gaps, data exposure, injection surface",
        "task_id_suffix": "-security"
      },
      {
        "agent_type": "lobster-generalist",
        "focus": "Ergonomics review: API consistency, naming conventions, consumer usability",
        "task_id_suffix": "-ergonomics"
      }
    ]
  },
  "constraints": {
    "boundary": "do not modify the proposed schema or any production API files",
    "no_modify": [
      "~/lobster-workspace/workstreams/uow_20260428_d4e5f6/proposed-schema.json"
    ],
    "no_deploy": true,
    "max_cycles": 3
  },
  "success_criteria": {
    "check": "Both security-review.md and ergonomics-review.md exist and are non-empty. Each contains a section titled 'Findings' and a section titled 'Recommendation'.",
    "artifacts": [
      "~/lobster-workspace/workstreams/uow_20260428_d4e5f6/security-review.md",
      "~/lobster-workspace/workstreams/uow_20260428_d4e5f6/ergonomics-review.md"
    ],
    "commands": [],
    "gate_command": null
  },
  "dan_context": {
    "orientation": "This is the second cycle of the v2 API schema review; the first cycle surfaced the need for independent perspectives.",
    "priority_signal": "The v2 API is blocking two downstream features; the review needs to resolve before those can proceed.",
    "open_questions": [
      "Should the security reviewer also evaluate the authentication token refresh flow, or is that out of scope for this UoW?"
    ],
    "load_bearing_assumption": "proposed-schema.json at the workstream path reflects the latest version discussed in the design session."
  },
  "audit_metadata": {
    "uow_id": "uow_20260428_d4e5f6",
    "cycle": 1,
    "executor_posture": "continuation",
    "schema_version": "1.0.0",
    "prescribed_at": "2026-04-28T19:15:00Z",
    "prescribed_skills": []
  }
}
```

---

### Example 3 — Execution Pass (First Execution with Concrete Deliverables)

A `first_execution` posture prescription for a concrete implementation task. Register:
`operational`. The Executor is a `functional-engineer` opening a GitHub PR.

See: [`docs/examples/execution-pass.json`](examples/execution-pass.json)

```json
{
  "diagnosis": {
    "signal": "UoW uow_20260430_7c8d9e entered ready-for-steward on first execution; source is GitHub issue #1042 requesting addition of the --dry-run flag to the registry CLI.",
    "reentry_posture": "first_execution",
    "completion_gap": "",
    "executor_outcome": null,
    "prior_cycle_count": 0
  },
  "prescription": {
    "summary": "Add a --dry-run flag to the registry CLI (registry_cli.py) that prints planned mutations without writing to the database.",
    "instructions": "Implement the --dry-run flag for the registry CLI as described in GitHub issue #1042.\n\n1. Read the issue: gh issue view 1042 --repo SiderealPress/lobster\n2. Create a worktree branch: git worktree add ~/lobster-workspace/projects/lobster-1042 feat/issue-1042-dry-run\n3. Add --dry-run to the CLI argument parser in src/orchestration/registry_cli.py\n4. When --dry-run is set, print each planned DB mutation as 'DRY-RUN: <mutation>' and return without executing\n5. Add a test in tests/unit/test_orchestration/test_registry_cli.py covering the --dry-run path\n6. Open a PR: gh pr create --repo SiderealPress/lobster --title 'feat: add --dry-run flag to registry CLI (#1042)'\n\nMinimum viable output: PR opened against main with the --dry-run implementation and at least one passing test.\nBoundary: do not modify registry.py, schema.sql, or any migration files.",
    "estimated_cycles": 1,
    "minimum_viable_output": "PR opened against main with the --dry-run implementation and at least one passing test",
    "boundary": "do not modify registry.py, schema.sql, or any migration files"
  },
  "workflow": {
    "agent_type": "functional-engineer",
    "steps": [
      "Read GitHub issue #1042 for full requirements",
      "Create feature branch via git worktree",
      "Implement --dry-run flag in registry_cli.py",
      "Write test covering --dry-run path",
      "Open PR against main"
    ],
    "fan_out": null
  },
  "constraints": {
    "boundary": "do not modify registry.py, schema.sql, or any migration files",
    "no_modify": [
      "src/orchestration/registry.py",
      "src/orchestration/schema.sql"
    ],
    "no_deploy": false,
    "max_cycles": 2
  },
  "success_criteria": {
    "check": "A PR is open against main in SiderealPress/lobster with title containing '#1042'. The PR diff includes changes to registry_cli.py and a new or modified test file. Tests pass in CI.",
    "artifacts": [],
    "commands": [
      "gh pr list --repo SiderealPress/lobster --state open --search '1042'"
    ],
    "gate_command": null
  },
  "dan_context": {
    "orientation": "This is a standalone CLI improvement; no dependencies on other active UoWs.",
    "priority_signal": "Requested by Dan for use in the WOS sprint runbook — needed before the next sprint planning session.",
    "open_questions": [],
    "load_bearing_assumption": "Issue #1042 is open and unassigned; no duplicate work is in progress."
  },
  "audit_metadata": {
    "uow_id": "uow_20260430_7c8d9e",
    "cycle": 0,
    "executor_posture": "first_execution",
    "schema_version": "1.0.0",
    "prescribed_at": "2026-04-30T09:00:00Z",
    "prescribed_skills": ["functional-engineer"]
  }
}
```

---

## Validation

To validate a prescription JSON file against the schema:

```bash
uv run -m jsonschema -i <prescription.json> docs/prescription-format.schema.json
```

To validate all example files at once:

```bash
for f in docs/examples/*.json; do uv run -m jsonschema -i "$f" docs/prescription-format.schema.json && echo "$f OK"; done
```

A valid prescription produces no output and exits 0. Any schema violation is reported
with the field path and the constraint that was violated. The `jsonschema` package is
available via `uv pip install jsonschema`.
