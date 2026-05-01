> **Status: SUPERSEDED**
> Superseded by: `docs/wos/current/wos-v3-spec.md`
> Do not treat as authoritative. Retained for historical reference only.

> **Addendum to [WOS V3 Proposal](./wos-v3-proposal.md).** This document provides the detailed steward/executor contract. The proposal is the authoritative unified reference; this file is retained as the technical addendum with full implementation detail.

# WOS V3 Steward/Executor Spec

## Current State of Relevant Files

**steward.py** (27,573 tokens — the largest file in the orchestration module): Implements `run_steward_cycle()` and `_process_uow()`. The core prescription path is LLM-only (`_llm_prescribe` via `claude -p`; deterministic fallback retained only when `llm_prescriber=None` is explicitly injected). PR #607 is already merged: the corrective trace one-cycle gate (`trace_gate_waited` / `trace_gate_contract_violation`) is live in the prescribe branch. The `_select_executor_type()` function routes by keyword matching on `uow.summary` and `uow.source`, with no awareness of `uow.register`. The `_assess_completion()` function reads `result.json` outcome but does not branch on register. `_detect_stuck_condition()` only fires on `hard_cap` (cycles ≥ 5) and `crash_repeated`.

**executor.py**: Implements the 6-step atomic claim sequence. The `_dispatch_via_claude_p` dispatcher spawns a functional-engineer subagent unconditionally for all UoWs. No register awareness: `executor_uow_view` now exposes `register` and `uow_mode` (added by migration 0007), but the Executor reads neither. `_write_result_json()` is called at all intentional exit paths; there is no `_write_trace_json()` counterpart. The `corrective_traces` table exists in the schema (migration 0007) but no code writes to it.

**germinator.py** (PR #602, merged): Implements `classify_register()` with the 4-gate ordered algorithm. Returns a `RegisterClassification` dataclass with `register`, `gate_matched`, `confidence`, `rationale`. Register is immutable after germination. The classification function is pure and well-tested by design.

**registry.py**: `UoW` dataclass includes `register: str = "operational"` and `uow_mode: str | None = None` (from migration 0007). `closed_at` and `close_reason` fields are also present. The `_write_steward_fields()` helper in steward.py does not yet write `closed_at` or `close_reason` on the done transition path.

**schema.sql + migration 0007**: `corrective_traces` table is in place with columns: `id`, `uow_id`, `register`, `execution_summary`, `surprises` (JSON), `prescription_delta`, `gate_score` (JSON), `created_at`. Index on `uow_id`. The `executor_uow_view` now includes `register` and `uow_mode`.

---

## Spec: 6 V3 Changes

---

### Change 1 — Register-Aware Diagnosis (Steward)

**Location**: `steward.py` — `_diagnose_uow()` and `_assess_completion()`

**Current behavior**: `_assess_completion()` branches on `outcome` from `result.json` and on `reentry_posture`, but makes no distinction based on `uow.register`. Philosophical and human-judgment UoWs go through the same `outcome == "complete"` → close pathway as operational ones.

**V3 behavior**:

Add a `_register_completion_policy(register: str) -> str` pure function that returns one of three policy identifiers:

- `"machine-gate"` for `operational` and `iterative-convergent`
- `"always-surface"` for `philosophical`
- `"require-confirmation"` for `human-judgment`

In `_assess_completion()`, after reading `outcome` from `result.json`, apply the policy:

1. **`machine-gate`** (operational / iterative-convergent): existing logic unchanged. `outcome == "complete"` can close the loop; evaluate against `success_criteria` as before.

2. **`always-surface`** (philosophical): `is_complete` is always `False` regardless of what `result.json` says. The function returns `(False, "register=philosophical: completion requires human judgment — surfacing to Dan", "philosophical_surface")`. This causes the stuck-condition path to fire (see Change 4: expanded Dan interrupt conditions). Note: do NOT set `stuck_condition = "hard_cap"` — instead, add a new stuck-condition type `"philosophical_register"` so the surface message carries the correct context.

3. **`require-confirmation`** (human-judgment): `is_complete` is `False` unless `uow.close_reason` is already populated (explicit Dan confirmation written by the feedback arm). Without `close_reason`, return `(False, "register=human-judgment: awaiting Dan's explicit confirmation", "human_judgment_pending")`. When `close_reason` is present (Dan confirmed via the future feedback arm), `is_complete = True`.

**Fields from trace.json available to diagnosis**: See Change 2. When trace.json exists, diagnosis receives: `gate_score` (for iterative-convergent: the numeric improvement signal), `surprises` (list: injected into `completion_gap_for_prescription` when re-prescribing), `prescription_delta` (injected as prior context in `_build_prescription_instructions`). The `execution_summary` is logged to steward_log but not used in routing decisions.

**How trace fields influence prescription**: In the prescribe branch (4c), after clearing the trace gate, read `{output_ref}.trace.json` and:
- For iterative-convergent: include `gate_score` in the `completion_gap_for_prescription` string so the LLM prescriber sees the numeric progress signal.
- For all registers: if `surprises` is non-empty, prepend them to the prescription context block as "Executor reported surprises: [...]".
- If `prescription_delta` is non-empty, append it as "Executor recommends prescription change: [...]".

These injections happen in `_process_uow()` before calling `_build_prescription_instructions()`, not inside the LLM prescriber itself, so they appear in the context string passed to the LLM.

---

### Change 2 — Corrective Trace Injection (Steward)

**Location**: `steward.py` — `_process_uow()`, prescribe branch (4c), after the trace gate check

**Current behavior**: The trace gate checks whether `{output_ref}.trace.json` exists but does not read its content. When the trace exists, it only clears the `trace_gate_waited` entry from `steward_log`.

**V3 behavior**:

Add `_read_trace_json(output_ref: str) -> dict | None` — a pure function that reads `{output_ref}.trace.json` (trying `.with_suffix(".trace.json")` and the `.trace.json` suffix append convention, mirroring the existing result.json dual-path logic). Returns the parsed dict or `None` on any error. Validates that `uow_id` in the trace matches the UoW being processed (same misrouted-file guard as in `_assess_completion`).

**Trace.json schema fields and their uses in diagnosis**:

| Field | Type | Used in diagnosis | Usage |
|---|---|---|---|
| `uow_id` | str | yes — validation | Must match UoW.id before reading other fields |
| `register` | str | yes — observability | Log if mismatches UoW.register (potential drift signal) |
| `execution_summary` | str | yes — logging | Append to `steward_log` as `trace_injection` event |
| `surprises` | list[str] | yes — prescription context | Prepend to completion_gap / prescription context if non-empty |
| `prescription_delta` | str | yes — prescription context | Append to prescription context block |
| `gate_score` | dict\|null | yes — iterative routing | For iterative-convergent: include score in completion_gap |
| `timestamp` | str | no | Not used in routing |

**Iterative-convergent gate score logic**: For `register == "iterative-convergent"`, read the `gate_score.score` field (float 0.0–1.0) from the trace. Track consecutive gate scores by reading the last N `trace_injection` entries from `steward_log` (a pure function `_count_non_improving_gate_cycles(steward_log, n=3) -> int`). If the score has not improved over the last 3 cycles, set `stuck_condition = "no_gate_improvement"` — this triggers the Dan surface path. See Change 4 for the interrupt condition.

**Write-back**: When a trace is read, write a `trace_injection` entry to `steward_log`:
```json
{
  "event": "trace_injection",
  "uow_id": "<id>",
  "steward_cycles": <n>,
  "register": "<register from trace>",
  "gate_score": <value or null>,
  "surprises_count": <N>,
  "prescription_delta_present": <bool>,
  "timestamp": "<ISO>"
}
```

#### Loop gain bounding (S1)

`prescription_delta` must be bounded before injection into the prescriber context. Raw `prescription_delta` values can accumulate aggressive corrections that cause the garden to oscillate rather than converge.

**Required mechanism (choose one at implementation time):**
- **Magnitude threshold**: discard `prescription_delta` strings exceeding a character-count threshold (candidate: 500 chars) — truncate with a trailing note that the delta was bounded
- **Cycle-averaged smoothing**: maintain a rolling buffer of the last N `prescription_delta` entries (candidate: N=3) from `corrective_traces`, average or de-duplicate before injection

The bounding step is a pure utility: `_bound_prescription_delta(delta: str, history: list[str]) -> str`. Apply in `_process_uow()` before `_build_prescription_instructions()`, after `_read_trace_json()`. Without this guard, a single aggressive corrective trace can destabilize subsequent prescription cycles.

---

### Change 3 — Register-Mismatch Gate (Steward)

**Location**: `steward.py` — `_process_uow()`, prescribe branch (4c), immediately before `_write_workflow_artifact()`

**Current behavior**: `_select_executor_type()` returns `functional-engineer`, `lobster-ops`, or `general` based on keyword matching on `uow.summary` and `uow.source`. No compatibility check is performed before writing the artifact.

**V3 behavior**:

Add `_check_register_executor_compatibility(register: str, executor_type: str) -> tuple[bool, str]` — a pure function returning `(is_compatible, reason)`.

**Valid executor_type → register mappings**:

| executor_type | Compatible registers | Incompatible registers |
|---|---|---|
| `functional-engineer` | `operational`, `iterative-convergent` | `philosophical`, `human-judgment` |
| `lobster-ops` | `operational`, `iterative-convergent` | `philosophical`, `human-judgment` |
| `general` | `operational` | `iterative-convergent`, `philosophical`, `human-judgment` |
| `frontier-writer` (new, V3) | `philosophical` | `operational`, `iterative-convergent`, `human-judgment` |
| `design-review` (new, V3) | `human-judgment` | `operational`, `iterative-convergent`, `philosophical` |

The `frontier-writer` and `design-review` executor types do not yet have corresponding dispatch implementations — they are gated register names that cause an intentional surface to Dan when the register-mismatch gate fires for `philosophical` or `human-judgment` UoWs that would otherwise receive a `functional-engineer` prescription.

**Gate logic** (inserted immediately before `_write_workflow_artifact()`):

```
selected_executor_type = _select_executor_type(uow)
is_compatible, mismatch_reason = _check_register_executor_compatibility(
    uow.register, selected_executor_type
)
if not is_compatible:
    # Do NOT write the workflow artifact.
    # Log mismatch to steward_log and audit_log.
    # Set stuck_condition = "register_mismatch" and route to surface path.
    # Transition to BLOCKED (not READY_FOR_EXECUTOR).
```

The mismatch should be treated as a stuck condition so that the existing surface-to-Dan machinery handles it. The `stuck_condition = "register_mismatch"` string is new — add it to `_detect_stuck_condition()`'s docstring and ensure `_default_notify_dan()` formats it with useful context (UoW register, prescription executor_type, and the reason).

**Implementation note**: The gate fires only in the prescribe branch, not in the done or surface branches. If `stuck_condition` was already set earlier (e.g., `hard_cap`), the earlier condition wins and the gate is never reached — consistent with current logic ordering.

#### Mismatch observability (S2)

Every register-mismatch gate fire must emit a structured observation to the mismatch log, not just a Dan-surface message. The observation captures:

```json
{
  "event": "register_mismatch_observation",
  "uow_id": "<id>",
  "register": "<register of the UoW>",
  "executor_type_attempted": "<what _select_executor_type returned>",
  "direction": "<which register→executor_type pairing fired the gate>",
  "steward_cycles": <n>,
  "timestamp": "<ISO>"
}
```

The `direction` field encodes the pairing explicitly (e.g., `"philosophical→functional-engineer"`) so downstream analysis can detect systematic routing failures — not just individual mismatch events.

**Why this is required:** The register table (above) was reasoned from first principles, not validated through operational data. Systematic mismatch patterns (the same register→executor_type combination firing repeatedly) are the signal that the classification or routing logic needs refinement. Without structured observability on every gate fire, classification quality is invisible.

**Implementation**: Log to `audit_log` alongside the existing `steward_log` write for the `register_mismatch` stuck condition. The `check_task_outputs` tool and the S3 Observation Loop (future PR) are the downstream consumers.

---

### Change 4 — Expanded Dan Interrupt Conditions (Steward)

**Location**: `steward.py` — `_detect_stuck_condition()` and `_process_uow()`

**Current V2 conditions**:
- `hard_cap`: `steward_cycles >= 5`
- `crash_repeated`: `return_reason == "crashed_no_output" and cycles >= 2`
- `executor_blocked`: `executor_outcome == "blocked"` (set in `_diagnose_uow`, not `_detect_stuck_condition`)

**V3 additions**:

**4a. `philosophical_register`**: Fires when `uow.register == "philosophical"` AND `reentry_posture != "first_execution"` (i.e., the executor has returned at least once with output). On first execution (`steward_cycles == 0`), do not surface yet — wait for the executor to produce output first, then surface with evidence. Set in `_detect_stuck_condition()` or in `_assess_completion()` returning the new policy code.

Rationale: philosophical UoWs must always surface to Dan with evidence. But surfacing before any execution produces no useful evidence. The one-cycle wait ensures Dan sees actual output.

**4b. `register_mismatch`**: Fires in the prescribe branch gate (Change 3). The Steward cannot dispatch an executor-type-incompatible prescription. Blocks before artifact write.

**4c. `no_gate_improvement`**: Fires for `iterative-convergent` register when `gate_score` has not improved over 3 consecutive cycles. Set by `_count_non_improving_gate_cycles()` (Change 2). The surface message to Dan should include the gate command, the score history, and the `prescription_delta` from the most recent trace.

**Surface message format for new conditions**:

For `philosophical_register`: `"WOS: UoW {id} is in philosophical register — executor returned output but completion requires human judgment. See output at {output_ref}. Summary: {summary[:200]}"`

For `register_mismatch`: `"WOS: UoW {id} — register mismatch. UoW register: {register}. Prescribed executor type: {executor_type}. A {register}-register UoW cannot be dispatched to {executor_type}. Manual routing required."`

For `no_gate_improvement`: `"WOS: UoW {id} — iterative-convergent gate not improving after 3 cycles. Gate scores: {history}. Last gate command: {cmd}. Prescription delta: {delta}"`

All new conditions use `notify_dan` (injectable), consistent with existing surface paths.

---

### Change 5 — Register-Appropriate Executor Routing (Executor)

**Location**: `executor.py` — `_run_execution()`, between step 1 (activate skills) and step 2 (dispatch)

**Current behavior**: `_dispatch_via_claude_p` is the single production dispatcher for all UoWs. The subagent prompt preamble is `_FUNCTIONAL_ENGINEER_PREAMBLE`, which instructs the subagent to read a GitHub issue, create a worktree, implement changes, and open a PR.

**V3 behavior**:

The Executor reads `uow.register` (now available in `executor_uow_view`) to select the dispatch strategy.

**Register → execution context mapping**:

| register | dispatcher | subagent context / preamble |
|---|---|---|
| `operational` | `_dispatch_via_claude_p` | `_FUNCTIONAL_ENGINEER_PREAMBLE` (unchanged) |
| `iterative-convergent` | `_dispatch_via_claude_p` | `_ITERATIVE_ENGINEER_PREAMBLE` (new) — same as functional-engineer but instructs the subagent to run the gate command, check the score, and continue iterations within the session until convergence or cycle limit |
| `philosophical` | `_dispatch_via_frontier_writer` (new) | `_FRONTIER_WRITER_PREAMBLE` — instructs subagent to write phenomenological synthesis output, write to output_ref, and write trace.json; does NOT open a PR |
| `human-judgment` | `_dispatch_via_design_review` (new) | `_DESIGN_REVIEW_PREAMBLE` — instructs subagent to write structured analysis output for Dan's review, write trace.json; surfaces to Dan for confirmation |

The `_dispatch_via_frontier_writer` and `_dispatch_via_design_review` dispatchers can delegate to `_dispatch_via_claude_p` with a different preamble — no new subprocess mechanism is needed. The distinction is purely in the subagent instructions.

**How the Executor reads register**: The `executor_uow_view` already includes `register` and `uow_mode` (migration 0007). After the claim commits, the Executor needs to read these fields from the claimed UoW. Currently the Executor's `_claim()` method reads from `executor_uow_view` at step 1 (pre-flight), but the `ClaimSucceeded` result carries only `uow_id`, `output_ref`, and `artifact`. To pass `register` to `_run_execution()`, either:
- Add `register: str` to `ClaimSucceeded` (cleanest — typed)
- Read it from the workflow artifact `executor_type` field (already set by Steward)

The recommended approach is to read `register` from the workflow artifact. The Steward (Change 3) already embeds `executor_type` in the artifact, and the register-mismatch gate guarantees `executor_type` is compatible with `register`. The Executor can dispatch based on `executor_type` alone, without re-reading `register` from the DB. This preserves the Steward/Executor isolation contract (Executor reads workflow_artifact; does not query registry fields beyond what the claim provides).

Add `executor_type` → dispatcher function mapping in `_run_execution()`:

```python
_EXECUTOR_TYPE_TO_DISPATCHER = {
    "functional-engineer": _dispatch_via_claude_p,
    "lobster-ops": _dispatch_via_claude_p,   # same mechanism, different preamble via instructions
    "general": _dispatch_via_claude_p,
    "frontier-writer": _dispatch_via_frontier_writer,
    "design-review": _dispatch_via_design_review,
}
```

The injected `dispatcher` parameter on `Executor.__init__` (used for tests and CI) takes precedence over this table — backward-compatible.

---

### Change 6 — trace.json Write Requirement (Executor, Issue #608)

**Location**: `executor.py` — `_run_execution()`, `report_partial()`, `report_blocked()`, and the exception handler in `_run_step_sequence()`

**Current behavior**: No trace.json is written anywhere in executor.py. The `corrective_traces` DB table exists (migration 0007) but is unpopulated. The Steward's trace gate (PR #607) waits one cycle for trace.json before re-prescribing, but the Executor never produces it.

**V3 behavior**:

Add `_write_trace_json(output_ref: str, trace: dict) -> None` — mirrors `_write_result_json()` exactly. Path derivation: `Path(output_ref).with_suffix(".trace.json")` primary, `Path(str(output_ref) + ".trace.json")` fallback. Creates parent dir. Writes atomically (write to `.tmp`, rename).

Add `_build_trace(uow_id: str, register: str, outcome: ExecutorOutcome, ...) -> dict` — constructs the trace dict. The `register` value comes from the `WorkflowArtifact.executor_type` field (the Executor infers register from executor_type, or it reads it from the view if `ClaimSucceeded` is extended to carry it).

**Full trace.json schema** (canonical, per V3 proposal section 6):

```json
{
  "uow_id": "<id>",
  "register": "<operational|iterative-convergent|philosophical|human-judgment>",
  "execution_summary": "<1-3 sentence prose: what happened in this execution cycle>",
  "surprises": ["<string: unexpected finding, constraint, or blocking condition>"],
  "prescription_delta": "<what would change the next prescription — e.g., 'gate command needs --no-header flag', 'success criterion is ambiguous'>",
  "gate_score": null,
  "timestamp": "<ISO-8601 UTC>"
}
```

For `iterative-convergent` register, `gate_score` must be populated:
```json
{
  "command": "<the gate command from the workflow artifact instructions>",
  "result": "<stdout/stderr excerpt — first 200 chars>",
  "score": 0.85
}
```

`score` is a float 0.0–1.0 where 1.0 = gate fully passed (e.g., all tests passing), 0.0 = gate completely failing. The score is computed by the executor subagent using the gate command result. For non-iterative-convergent registers, `gate_score` is `null`.

**Where trace.json is written** (all 4 exit paths):

| Exit path | Location in code | trace content |
|---|---|---|
| Normal complete | `_run_execution()`, step 5 (after `_write_result_json`) | `execution_summary` = "Executor dispatched subagent {executor_id}, subprocess exit 0"; `surprises` = []; `prescription_delta` = "" |
| Partial | `report_partial()`, after `_write_result_json` | `execution_summary` = reason; `surprises` = [reason]; `prescription_delta` = "partial completion — {steps_completed}/{steps_total} steps done" |
| Blocked | `report_blocked()`, after `_write_result_json` | `execution_summary` = reason; `surprises` = [reason]; `prescription_delta` = "blocked — external resolution required before re-prescription" |
| Exception (crash) | `_run_step_sequence()` exception handler, after `_write_subagent_result` | `execution_summary` = "Executor crashed: {type(exc).__name__}: {exc}"; `surprises` = [str(exc)]; `prescription_delta` = "exception before subagent dispatch — check executor logs" |

**Contract violation semantics** (unchanged from V3 proposal): Absence of trace.json does not block the Steward's re-entry (unlike result.json). The existing one-cycle gate in the Steward (PR #607) handles the absence case correctly. The V3 change makes trace.json present on all intentional exit paths, so the gate will pass on the next Steward cycle without triggering a contract violation log.

**DB write to `corrective_traces` table**: Write to the table at the same time as writing the file. Both writes happen in `_write_trace_json()` (or a sibling function). The DB write is a best-effort INSERT (log on failure, do not raise — consistent with the proposal's non-blocking contract). The table provides the Steward's garden retrieval path; the file provides the immediate one-cycle gate path.

```python
def _insert_corrective_trace(registry_db_path: Path, trace: dict) -> None:
    """Best-effort INSERT to corrective_traces. Logs on failure, does not raise."""
```

The Executor needs `registry.db_path` to write to the table. It already holds a `registry` reference — this is already available.

---

## PR Sequencing Table

| PR | Title | Contents | Depends on |
|---|---|---|---|
| PR A | `executor: write trace.json at all exit paths (Issue #608)` | `_write_trace_json()`, `_build_trace()`, `_insert_corrective_trace()`, trace write in `_run_execution()` / `report_partial()` / `report_blocked()` / exception handler | PRs #601, #607 (already merged) |
| PR B | `executor: register-appropriate routing via executor_type dispatch table` | `_ITERATIVE_ENGINEER_PREAMBLE`, `_FRONTIER_WRITER_PREAMBLE`, `_DESIGN_REVIEW_PREAMBLE`, `_EXECUTOR_TYPE_TO_DISPATCHER` mapping in `_run_execution()` | PR A (trace.json must ship before or with this — the new dispatchers must write trace.json) |
| PR C | `steward: register-aware diagnosis + corrective trace injection` | `_register_completion_policy()`, `_assess_completion()` policy branch, `_read_trace_json()`, `_count_non_improving_gate_cycles()`, trace injection in prescribe branch | PR A (steward reads trace.json — must exist before this is useful) |
| PR D | `steward: register-mismatch gate + expanded Dan interrupt conditions` | `_check_register_executor_compatibility()`, mismatch gate in prescribe branch, `philosophical_register` / `no_gate_improvement` stuck conditions, `_detect_stuck_condition()` additions, surface message formatting | PR C (register-aware diagnosis must be in place so mismatch detection has correct context) |

**Recommended sequence**: A → B (can be one PR if small) → C → D.

The critical dependency is PR A before PR C: the Steward's trace injection logic (Change 2) reads `{output_ref}.trace.json`. If that file is never written (current state), the trace gate fires its one-cycle wait on every cycle and never reads content. Shipping PR A first means the Steward's existing PR #607 gate starts passing immediately, and PR C's trace injection finds real content to read.

PR B and PR C can be developed in parallel but B should land first because the new executor types (`frontier-writer`, `design-review`) that PR B introduces are the executor_types that the register-mismatch gate in PR D allows through.

---

## Future PRs

*These are not scheduled for the current PR sequence. They depend on evidence from the V3 sprint.*

---

### Future PR: S3 — Observation Loop Pattern Synthesis

**Precondition:** `corrective_traces` table populated from V3 PRs A–D + 10-UoW sprint evidence.

**Scope:**

The V3 Observation Loop detects stalled UoWs within a single garden pass. S3 extends it to synthesize across accumulated traces — a scheduled pass that reads the full `corrective_traces` table and writes structured candidate amendments.

**What it does:**

1. Reads `corrective_traces` grouped by `register` and `execution_summary` patterns.
2. Detects: repeated surprises (same surprise text appearing in 3+ distinct UoWs), register-mismatch clustering (same `direction` appearing in the mismatch observability log 3+ times), cross-UoW prescription recycling (high token overlap in `prescription_delta` across same-register UoWs).
3. For each detected pattern: writes a candidate amendment to a `pattern_observations` file — not mutating classification logic, observations only.
4. Surfaces the structured digest to Dan during the next engagement window.

**Implementation class:** Type C cron-direct scheduled job (not a UoW — it reads the garden, it does not act on it). New `scheduled-tasks/` script.

**Dependency:** Requires meaningful corrective_traces volume. Design only after the first sprint.

---

### Future PR: S5 — Dan-Interrupt Cartridge Specification

**Precondition:** V3 PRs A–D shipped; philosopher postures catalog in Garden retrieval.

**Scope:**

The "surface to Dan" path in V3 is a terminal stuck condition — it delivers evidence without an orientation lens. S5 makes the interrupt path an encounter by coupling it to a composable cartridge system.

**Design requirements:**

- **OODA-coupled triggers:** The cartridge fires on `lack-of-clarity` (Observe) and `suspect-of-certainty` (Orient) in addition to explicit stuck conditions. These are new trigger classes that require design — they are not fired by any current V3 gate.
- **Lens-swappable:** The Garden's philosopher postures catalog becomes the cartridge library. Mito-governor for load/scaling UoWs; cybernetics for feedback loop UoWs; Theory of Learning for iterative-convergent UoWs approaching plateau. The cartridge selection uses UoW register + content summary + stuck condition as inputs.
- **Slight randomness (anti-calcification):** 10–15% probability of sampling a non-top-ranked cartridge to prevent the system from over-optimizing to a single philosopher posture.
- **Cartridge interface:** `CartridgeContext(uow_register, content_summary, stuck_condition) -> philosopher_lens_block: str`. Pure function; composable with existing `_build_prescription_instructions()`.

**What must exist before S5 design begins:** Philosopher postures catalog (Garden retrieval, at least 5 distinct postures), operational evidence of what the V3 `philosophical_register` and `register_mismatch` surfaces actually look like in practice, and a `cartridge_interface` spec (inputs/outputs) that can be reviewed with Dan.

---

## Testability Notes

### Change 1 — Register-Aware Diagnosis

**Unit tests**: `_register_completion_policy()` is a pure function — test all 4 registers directly. `_assess_completion()` with a `philosophical` UoW and a valid `result.json` must return `is_complete=False`. `_assess_completion()` with `human-judgment` and no `close_reason` must return `is_complete=False`.

**Observable without full WOS loop**: Write a fixture DB with a `philosophical` UoW at `ready-for-steward`, run `run_steward_cycle(dry_run=True)`, inspect the returned `StewardOutcome` — it should be `Surfaced` with `condition="philosophical_register"`.

### Change 2 — Corrective Trace Injection

**Unit tests**: `_read_trace_json()` — test valid file, missing file, mismatched uow_id, malformed JSON. `_count_non_improving_gate_cycles()` — test with 0, 1, 2, 3, 4 gate score entries at various improvement levels.

**Observable without full WOS loop**: Write a fixture trace.json to `{output_ref}.trace.json` with `surprises: ["unexpected constraint"]`, run `_process_uow()` with `dry_run=True`, check that the instructions returned by `_build_prescription_instructions()` include the surprise text. Check that `steward_log` contains a `trace_injection` event.

### Change 3 — Register-Mismatch Gate

**Unit tests**: `_check_register_executor_compatibility()` — 20 test cases covering all (register, executor_type) pairs in the compatibility table. `_process_uow()` with a `philosophical` UoW and `_select_executor_type` returning `functional-engineer` — must produce `Surfaced` with `condition="register_mismatch"` and no artifact written.

**Observable without full WOS loop**: Dry-run a `philosophical` UoW through `run_steward_cycle()`. Confirm no artifact file is written to `~/lobster-workspace/orchestration/artifacts/`. Confirm `notify_dan` is called with `condition="register_mismatch"`.

### Change 4 — Expanded Dan Interrupt Conditions

**Unit tests**: Each new stuck condition has a dedicated test: `philosophical_register` fires on cycle ≥ 1 for philosophical UoW; `no_gate_improvement` fires when 3 consecutive gate scores are equal; `register_mismatch` fires before artifact write.

**Observable without full WOS loop**: Check `steward_log` and `audit_log` entries — the condition name appears in the `surface_condition` field. The inbox message written by `_default_notify_dan()` contains the expected condition-specific text.

### Change 5 — Register-Appropriate Executor Routing

**Unit tests**: `_run_execution()` with a workflow artifact carrying `executor_type="frontier-writer"` — confirm `_dispatch_via_frontier_writer` is called, not `_dispatch_via_claude_p`. Use the injectable `dispatcher` parameter to capture the dispatch call.

**Observable without full WOS loop**: Inject a no-op dispatcher and call `execute_uow()` on a UoW whose workflow artifact has `executor_type="design-review"` — confirm the no-op is called with the `_DESIGN_REVIEW_PREAMBLE` prefix in the instructions string.

### Change 6 — trace.json Write Requirement

**Unit tests**: After `_run_execution()` returns (mocked dispatcher), check that `{output_ref}.trace.json` exists and is valid JSON. After `report_partial()`, same check. After `report_blocked()`, same check. After the exception handler in `_run_step_sequence()`, same check. Validate uow_id field in each. Validate `gate_score` is null for operational; validate it has `command`/`result`/`score` for iterative-convergent.

**Observable without full WOS loop**: Run `executor.execute_uow(uow_id)` against a test registry with a no-op dispatcher. List `~/lobster-workspace/orchestration/outputs/`. Confirm `{uow_id}.trace.json` exists alongside `{uow_id}.result.json`. Check DB: `SELECT * FROM corrective_traces WHERE uow_id = ?` returns one row.

---

### Critical Files for Implementation
- `/home/lobster/lobster/src/orchestration/steward.py`
- `/home/lobster/lobster/src/orchestration/executor.py`
- `/home/lobster/lobster/src/orchestration/workflow_artifact.py`
- `/home/lobster/lobster/src/orchestration/registry.py`
- `/home/lobster/lobster/src/orchestration/migrations/0007_wos_v3_register_and_corrective_traces.sql`

---

## V4 Design Directions

*Captured from philosophical review session, 2026-04-04. These are not current PRs — they are design directions to hold as V3 lands and V4 scope opens.*

---

### Direction 1 — Philosopher Routing as Composable Cartridge

**Current state (V3):** The "surface to Dan" path is a terminal stuck condition. When a UoW reaches `philosophical_register` or `register_mismatch`, the Steward fires `notify_dan` and stops. The interrupt is useful but flat — it delivers evidence without an orientation lens.

**V4 direction:** The Dan-interrupt path should become a composable cartridge system:

- **OODA-coupled:** The cartridge fires not just on explicit stuck conditions but on lack-of-clarity (Observe) and suspect-of-certainty (Orient) — the two moments where a philosopher lens adds the most value.
- **Cartridge-swappable by UoW register/content:** The Garden's philosophical postures catalog (mito-governor, cybernetics, Theory of Learning, etc.) becomes the cartridge library. Different UoW registers and content patterns select different philosopher lenses — mito-governor for load/scaling UoWs, cybernetics for feedback loop UoWs, etc.
- **Slight randomness:** Prevents calcification. A deterministic cartridge selector will over-optimize for the most recent successful lens. A small stochastic component (e.g., 10–15% probability of sampling a non-top-ranked cartridge) keeps the system from converging to a single philosopher posture.

**What needs to be in spec before this can be designed:** The philosophical postures catalog (Garden retrieval), a cartridge interface definition (inputs: UoW register + content summary + stuck condition; outputs: philosopher lens context block), and the OODA trigger conditions beyond the current `philosophical_register` stuck condition.

**Precondition:** V3 must land first and generate 10+ UoW sprint evidence. Cartridge design is premature without knowing what the Dan-interrupt path actually surfaces in practice.

---

### Direction 2 — Dan Attentional State + Reversible Forward Commitment

**Current state (V3):** When a UoW surfaces to Dan and Dan does not respond, the UoW sits in BLOCKED indefinitely. The system halts rather than proceeding or failing explicitly.

**V4 design principle:** When Dan is unavailable, the system makes a **reversible forward commitment**:

1. Proceed with the best available decision (do not halt).
2. Document the decision point explicitly as a structured artifact: what was decided, what alternatives were available, what assumptions were made, what would change the decision if wrong.
3. Tag the decision point as "future refactor potential" — a labeled decision that Dan can revisit during his next available engagement window without needing to reconstruct the context.

This is not "blindly proceed" (which ignores Dan's judgment) and not "halt" (which blocks the system on human availability). It is a bounded commitment that preserves reversibility.

**What this seeds:** A routing intelligence layer that models Dan's attentional state as a system variable — not just "available/unavailable" but a richer signal (recent engagement patterns, current cognitive load, time-since-last-interaction) that informs how much autonomy to assume.

**Implementation note:** The reversible commitment artifact schema is a natural extension of `trace.json` — add a `decision_points` array field that captures structured forward commitments made under Dan-unavailability conditions. The Steward's Orient phase reads these on re-entry so Dan's next engagement window has explicit, structured decision points to review rather than undifferentiated history.

---

### Direction 3 — Garden Retrieval as Structural Prerequisite for Orient Phase Quality

**Current state (V3):** Garden retrieval (reading accumulated traces, past UoW outcomes, classification history) is implied by the architecture but not structurally enforced. The Orient phase can proceed nominally — with LLM reasoning but without retrieving relevant garden state — and the Steward has no signal that the Orient was shallow.

**V4 design principle:** Structural hygiene is not optional overhead — it is what makes the Orient phase real rather than nominal.

Concretely:

- The Steward's `_build_prescription_instructions()` should have an explicit retrieval step that produces a structured retrieval receipt: what was searched, what was found, what relevance score was assigned. If retrieval returns nothing (empty garden for this UoW class), that is logged as a distinct state — not silently treated as equivalent to retrieval success.
- Garden quality metrics (corrective_traces row count, trace recency, register distribution) become first-class observability instruments. If the garden for a given register is sparse (fewer than N traces), that sparsity is injected as context for the prescriber: "Note: limited prior trace data for this register."
- The S3 Observation Loop (cross-cycle pattern synthesis) — already identified in the convergence doc as a future PR — is the mechanism that populates the garden. Until S3 ships, Orient phase quality is bounded by the sparsity of the initial garden.

**Why this is V4 and not V3:** V3 already specifies trace.json writes and corrective_traces DB inserts (Changes 2 and 6). The garden population mechanism is in the current spec. What is missing is the retrieval side: explicit retrieval receipts, sparsity signals, and the cross-cycle synthesis (S3). These require the garden to have accumulated data before they are useful — which means V3 must run for a meaningful sprint first.

---

### Direction 4 — Scaling Governor (S4)

**The deepest open problem:** V3 addresses register mismatch — the proximate failure. It does not address the structural pattern that produced Coherence's collapse: the system immediately overextended to maximum load when it became operational. V3 has no mechanism that modulates how aggressively work is dispatched based on recent performance signals.

**What the scaling governor does:**

A governor that reads recent `gate_score` history and UoW completion rate to modulate two parameters:
- **Batch size:** How many UoWs are dispatched in a single Steward pass.
- **Execution rate:** How frequently the Steward cycle runs (or equivalently, how long the dispatch window remains open before pausing).

The governor is a feedback control loop: success signal → increase throughput; failure signal → reduce throughput + increase diagnosis time before next dispatch.

**Signal source:** The `corrective_traces` table (gate_scores, execution_summary) and the registry (UoW completion rate over rolling window). Both are available after V3 PRs A–D land.

**Why deferred:** The governor requires the corrective trace data to be real before it can be calibrated. A governor designed on synthetic data will be tuned for the wrong operating point. Design only after the first 10-UoW sprint with real traces.

**Design horizon (not spec):**
- Governor input: rolling 7-day completion rate + average gate_score across last N completed UoWs.
- Governor output: `dispatch_batch_size` (int, floor 1, ceiling configurable) + `inter-cycle-pause-seconds` (float).
- Anti-windup: if batch_size has been at floor for > 3 cycles with no improvement, surface to Dan — the system is in a mode the governor cannot resolve autonomously.
- Configuration: `wos-config.json` extension with `governor_enabled: bool` + tuning parameters. Default: disabled until calibrated.

---

## Related Documents

- **[wos-v3-proposal.md](wos-v3-proposal.md)** — Foundational V3 design proposal that this spec implements. Covers vision, register taxonomy, architecture, dispatch loop pseudocode, and open design questions.
- **[wos-v3-convergence.md](../philosophy/frontier/wos-v3-convergence.md)** — Seeds, sprouts, and pearls synthesis from multi-thread philosophical review. Contains S1 (loop gain bounding for PR B) and S2 (mismatch observability for PR C) as explicit spec requirements, plus the ungoverned timescales section (register-portfolio diversity, cross-cycle pattern learning).
- **[corrective-trace-loop-gain-research.md](corrective-trace-loop-gain-research.md)** — Research note on bounded correction magnitude for Change 2 (Corrective Trace Injection, PR B). Provides engineering and biological grounding for the loop gain bounding requirement (S1). See the bounded-correction mechanisms (section 3) for implementation guidance.
- **[2026-04-04-philosopher-cybernetics.md](../philosophy/sessions/2026-04-04-philosopher-cybernetics.md)** — Cybernetics lens (Ashby's Law). Named the unbounded loop gain risk in the corrective trace mechanism and the orientation gaps in the OODA phase.
- **[2026-04-04-philosopher-theory-of-learning.md](../philosophy/sessions/2026-04-04-philosopher-theory-of-learning.md)** — Theory of Learning lens. Placed the spec at Discernment-Coherence designing for Attunement; identified the scaling governor gap (S4) and the trace mechanism as developmental scaffolding rather than a completed learning loop.
- **[2026-04-04-philosopher-mito-governor.md](../philosophy/sessions/2026-04-04-philosopher-mito-governor.md)** — Mito-governor lens. Named the timing-structure vs. content-processing distinction for the trace gate (PR #607), the register-portfolio diversity gap, and the multi-timescale governance gap.
