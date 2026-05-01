# WOS V3 — Architecture and Implementation Spec

> **Status: AUTHORITATIVE**
> This is the single source of truth for WOS v3 architecture and implementation.
> wos-v3-proposal.md and wos-v3-steward-executor-spec.md are archived at docs/wos/archived/.

---

## 1. Vision and Design Premises

### Vision

In full flourishing, WOS V3 is not a task runner. It is a living cybernetic substrate — an extension of Dan's attentional field that knows what register a piece of work lives in, routes it accordingly, evaluates completion against criteria that match the work's ontological category, and collapses the loop between observation and closed action at minimum thermodynamic cost. Seeds arrive from any source (philosophy sessions, nightly sweeps, voice notes, Telegram). The Steward classifies each into its register — operational, philosophical, iterative-convergent, or human-judgment — and dispatches it into an execution context that matches that register. Pearls circulate through gardens. Executors close loops and write corrective traces. The Observation Loop detects drift before Dan notices it. The system's own structure is a living argument for what Dan is building toward.

### Why WOS Exists

WOS exists because non-convergent conversations accumulate as issues that never close. A conversation that surfaces a problem, files a GitHub issue, and then stalls has discharged its surface energy without converting it to kinetic change. WOS is the mechanism that converts observations into closed loops.

### What the 0.8% Success Rate Revealed

The overnight 50-run (252 UoWs injected, 250 failed) was not a bug — it was a signal. The V2 executor (`claude -p` subprocess, synchronous) was running tasks that were category-wrong for the execution model. Operational tasks dispatched into philosophical register contexts, philosophical seeds dispatched as code execution tasks, register-mismatch throughout. The pipeline mechanics were sound (3-minute small-batch tests proved this). The routing intelligence was absent.

### Three Structural Misalignments in V2

1. **Register blindness** — The Steward prescribed workflow primitives without regard to what register the UoW lived in. A design document prescription and a bug-fix prescription use the same machinery. They require fundamentally different execution contexts.

2. **Completion criterion mismatch** — `success_criteria` was required at germination but was evaluated against outputs without checking whether the criteria and the work were in the same ontological category. "Implementation-ready spec" is a human-judgment criterion, not a machine-observable one.

3. **The mode field was aspirational** — The `ralph-wos-seed.md` design exploration correctly identified that some UoWs are machine-observable-done (tests pass) and others require human judgment. V2 never operationalized this distinction.

### What V3 Resolves

V3 operationalizes register classification, matches completion evaluation to work ontology, and installs a gate before execution that prevents category-wrong dispatch. It is not a rewrite — it is a precision refinement of the V2 mechanics with register-awareness as the new structural primitive.

---

## 2. Architecture Overview

```
SEEDS (any source)
  Telegram / voice note / philosophy session / nightly sweep / direct request
      │
      ▼
CULTIVATOR (classification gate) — two actors:
  Tending Cultivator (orientation-register, human-in-loop):
    Reads session outputs phenomenologically
    Holds candidate pearls for re-encounter before routing
    May be Dan's act of re-reading — not automatable
    If pearl → write-path (frontier docs, bootup candidates)
    If seed confirmed → passes to Filing Cultivator
  Filing Cultivator (execution-register, automatable):
    Takes confirmed seeds, writes success_criteria at germination
    Files → GitHub issue  [= github-issue-cultivator job]
      │
      ▼
UoW REGISTRAR (germination gate)
  Reads GitHub issues meeting gate criteria
  Classifies register at germination time
  Creates UoWRegistry entry with:
    - success_criteria (required, ontology-matched)
    - uow_mode: operational | philosophical | iterative | human-judgment
    - register: the attentional configuration required for evaluation
  Proposed → (Dan gate or auto-gate by trust level) → pending
      │
      ▼
STEWARD (diagnosis + prescription engine)
  Reads: UoW record, steward_agenda, audit_log, Dan's current register (from context)
  Diagnoses: what does this UoW need? In what register?
  Prescribes: workflow primitive + executor context + skills + register hint
  Evaluates (on re-entry): did the executor's output close the loop?
    - For operational/iterative UoWs: evaluate against machine-observable gate
    - For philosophical/human-judgment UoWs: surface to Dan with evidence
  Surfaces to Dan: if stuck_cycles ≥ 5, if register mismatch detected, if gate fails repeatedly
      │              │
      ▼              ▼
EXECUTOR        DAN INTERRUPT
  Claims UoW      Receives surfaced UoWs
  Dispatches      Provides orientation or decision
  via register-   Returns UoW to Steward with Dan's
  appropriate     classification added
  context
  Writes result.json + corrective trace
  Returns to ready-for-steward
      │
      ▼
OBSERVATION LOOP
  Detects: stalled active UoWs, dark pipeline, ready-queue growth, register-drift
  Reports: structured signals, not guesswork
  Never acts unilaterally
      │
      ▼
GARDEN (living knowledge layer)
  Pearls, corrective traces, attunement records
  Circulate via re-encounter, not re-execution
  Steward reads garden context at diagnosis time
```

### The Dispatch Loop (OODA at Every Scale)

```
# Per-UoW: OODA instantiated

# OBSERVE
uow = registry.next_ready_for_steward()
prior_outputs = read(uow.output_ref) if uow.output_ref else None
audit_history = read(uow.audit_log)
garden_context = garden.relevant_to(uow.title, uow.register)
dan_register = context.current_register()  # from recent activity signals

# ORIENT
diagnosis = steward.diagnose(
    uow=uow,
    prior_outputs=prior_outputs,
    audit_history=audit_history,
    garden_context=garden_context,
    dan_register=dan_register,
)
# Orient is the schwerpunkt — all subsequent decisions depend on quality here

# DECIDE
if diagnosis.completion_met:
    transition(uow, "done")
    write_corrective_trace(uow, diagnosis)
elif diagnosis.surface_to_dan:
    transition(uow, "blocked")
    send_dan_surface(uow, diagnosis)
else:
    prescription = steward.prescribe(
        uow=uow,
        diagnosis=diagnosis,
        workflow_library=PRIMITIVES,
    )
    write_workflow_artifact(prescription)
    transition(uow, "ready-for-executor")

# ACT (Executor)
claim(uow)  # atomic 6-step sequence
result = execute(prescription, register=uow.register)
write_result_json(result)           # required at ALL exit paths
write_corrective_trace(uow, result) # new in V3 — learning artifact
transition(uow, "ready-for-steward")

# Loop continues until Steward declares done
```

**Corrective traces** are V3's core new primitive: every executor return — complete, partial, blocked, or failed — writes a structured trace capturing what happened, what was surprising, and what would change the prescription. These traces accumulate in the garden. The Steward reads them at diagnosis time. This is how the system learns without a training loop.

---

## 3. Register Taxonomy

Registers are not tone, format, or complexity levels. A register is the attentional configuration a UoW requires for correct completion evaluation. Register-mismatch produces coupling failure even when execution mechanics succeed.

### Operational Register

**Presupposes:** an executor that is attending to concrete, externally-verifiable outcomes.
**Success evaluation:** machine-observable (tests pass, CI green, file written, PR merged).
**Dispatch:** standard executor subagent with functional-engineer skills.
**Gate command:** explicit command or check that verifies completion without human reading.
**Examples:** fix failing test, apply code patch, write timestamped log entry, sync database records.

### Iterative-Convergent Register

**Presupposes:** an executor that can run a work loop, evaluate progress against a gate score, and continue until the gate condition is met or a no-improvement threshold triggers escalation.
**Success evaluation:** machine-observable gate score improving across cycles.
**Dispatch:** ralph-loop-style executor — each cycle reads prior output, checks gate, continues or escalates.
**Gate command:** required. Escalation trigger: `no_gate_improvement_for_3_cycles`.
**Examples:** fix all auth test failures, make mypy clean, bring CI from 40% to 100% passing.

### Philosophical Register

**Presupposes:** an executor attending to phenomenological content — what shows up in first-person encounter, not what can be verified from outside.
**Success evaluation:** human judgment only. Steward cannot declare done; surfaces to Dan with evidence.
**Dispatch:** frontier-writer subagent or human-facing surface. Never functional-engineer.
**Examples:** philosophy session synthesis, frontier document update, register exploration, poiesis-driven writing.

### Human-Judgment Register

**Presupposes:** a human reader who evaluates the output against criteria that cannot be formalized.
**Success evaluation:** Dan's explicit confirmation. No other path to done.
**Dispatch:** design-review or synthesis primitive. Surfaces to Dan with maximum context density.
**Examples:** design document review, architecture decision, success criteria ambiguity, anything where "done" requires Dan to say so.

### Register Classification Algorithm

```
At germination, classify register using this ordered gate:

1. Does the UoW body contain a machine-executable gate command?
   YES → operational or iterative-convergent (see #2)
   NO  → continue

2. (If #1 yes) Does the work require multiple iterations against the gate?
   YES → iterative-convergent
   NO  → operational

3. Does the UoW originate from a philosophy session, frontier doc, or
   contains vocabulary from Dan's phenomenological register?
   YES → philosophical
   NO  → continue

4. Is the success_criteria evaluable without reading the output?
   NO  → human-judgment
   YES → operational (default)
```

Register is written to the UoW at germination and is **immutable**. If the Steward determines on diagnosis that the register is wrong, it surfaces to Dan — it does not reclassify autonomously.

---

## 4. Implementation Spec

This section provides the detailed implementation changes required across `steward.py` and `executor.py`.

### Current State of Relevant Files

**steward.py** (27,573 tokens — the largest file in the orchestration module): Implements `run_steward_cycle()` and `_process_uow()`. The core prescription path is LLM-only (`_llm_prescribe` via `claude -p`; deterministic fallback retained only when `llm_prescriber=None` is explicitly injected). PR #607 is already merged: the corrective trace one-cycle gate (`trace_gate_waited` / `trace_gate_contract_violation`) is live in the prescribe branch. The `_select_executor_type()` function routes by keyword matching on `uow.summary` and `uow.source`, with no awareness of `uow.register`. The `_assess_completion()` function reads `result.json` outcome but does not branch on register. `_detect_stuck_condition()` only fires on `hard_cap` (cycles >= 5) and `crash_repeated`.

**executor.py**: Implements the 6-step atomic claim sequence. The `_dispatch_via_claude_p` dispatcher spawns a functional-engineer subagent unconditionally for all UoWs. No register awareness: `executor_uow_view` now exposes `register` and `uow_mode` (added by migration 0007), but the Executor reads neither. `_write_result_json()` is called at all intentional exit paths; there is no `_write_trace_json()` counterpart. The `corrective_traces` table exists in the schema (migration 0007) but no code writes to it.

**germinator.py** (PR #602, merged): Implements `classify_register()` with the 4-gate ordered algorithm. Returns a `RegisterClassification` dataclass with `register`, `gate_matched`, `confidence`, `rationale`. Register is immutable after germination. The classification function is pure and well-tested by design.

**registry.py**: `UoW` dataclass includes `register: str = "operational"` and `uow_mode: str | None = None` (from migration 0007). `closed_at` and `close_reason` fields are also present. The `_write_steward_fields()` helper in steward.py does not yet write `closed_at` or `close_reason` on the done transition path.

**schema.sql + migration 0007**: `corrective_traces` table is in place with columns: `id`, `uow_id`, `register`, `execution_summary`, `surprises` (JSON), `prescription_delta`, `gate_score` (JSON), `created_at`. Index on `uow_id`. The `executor_uow_view` now includes `register` and `uow_mode`.

---

### Change 1 — Register-Aware Diagnosis (Steward)

**Location**: `steward.py` — `_diagnose_uow()` and `_assess_completion()`

**Current behavior**: `_assess_completion()` branches on `outcome` from `result.json` and on `reentry_posture`, but makes no distinction based on `uow.register`. Philosophical and human-judgment UoWs go through the same `outcome == "complete"` -> close pathway as operational ones.

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

**Iterative-convergent gate score logic**: For `register == "iterative-convergent"`, read the `gate_score.score` field (float 0.0-1.0) from the trace. Track consecutive gate scores by reading the last N `trace_injection` entries from `steward_log` (a pure function `_count_non_improving_gate_cycles(steward_log, n=3) -> int`). If the score has not improved over the last 3 cycles, set `stuck_condition = "no_gate_improvement"` — this triggers the Dan surface path. See Change 4 for the interrupt condition.

**Write-back**: When a trace is read, write a `trace_injection` entry to `steward_log`:
```json
{
  "event": "trace_injection",
  "uow_id": "<id>",
  "steward_cycles": "<n>",
  "register": "<register from trace>",
  "gate_score": "<value or null>",
  "surprises_count": "<N>",
  "prescription_delta_present": "<bool>",
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

**Valid executor_type -> register mappings**:

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
  "direction": "<which register->executor_type pairing fired the gate>",
  "steward_cycles": "<n>",
  "timestamp": "<ISO>"
}
```

The `direction` field encodes the pairing explicitly (e.g., `"philosophical->functional-engineer"`) so downstream analysis can detect systematic routing failures — not just individual mismatch events.

**Why this is required:** The register table (above) was reasoned from first principles, not validated through operational data. Systematic mismatch patterns (the same register->executor_type combination firing repeatedly) are the signal that the classification or routing logic needs refinement. Without structured observability on every gate fire, classification quality is invisible.

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

**Register -> execution context mapping**:

| register | dispatcher | subagent context / preamble |
|---|---|---|
| `operational` | `_dispatch_via_claude_p` | `_FUNCTIONAL_ENGINEER_PREAMBLE` (unchanged) |
| `iterative-convergent` | `_dispatch_via_claude_p` | `_ITERATIVE_ENGINEER_PREAMBLE` (new) — same as functional-engineer but instructs the subagent to run the gate command, check the score, and continue iterations within the session until convergence or cycle limit |
| `philosophical` | `_dispatch_via_frontier_writer` (new) | `_FRONTIER_WRITER_PREAMBLE` — instructs subagent to write phenomenological synthesis output, write to output_ref, and write trace.json; does NOT open a PR |
| `human-judgment` | `_dispatch_via_design_review` (new) | `_DESIGN_REVIEW_PREAMBLE` — instructs subagent to write structured analysis output for Dan's review, write trace.json; surfaces to Dan for confirmation |

The `_dispatch_via_frontier_writer` and `_dispatch_via_design_review` dispatchers can delegate to `_dispatch_via_claude_p` with a different preamble — no new subprocess mechanism is needed. The distinction is purely in the subagent instructions.

**How the Executor reads register**: The `executor_uow_view` already includes `register` and `uow_mode` (migration 0007). After the claim commits, the Executor needs to read these fields from the claimed UoW. The recommended approach is to read `register` from the workflow artifact. The Steward (Change 3) already embeds `executor_type` in the artifact, and the register-mismatch gate guarantees `executor_type` is compatible with `register`. The Executor can dispatch based on `executor_type` alone, without re-reading `register` from the DB. This preserves the Steward/Executor isolation contract (Executor reads workflow_artifact; does not query registry fields beyond what the claim provides).

Add `executor_type` -> dispatcher function mapping in `_run_execution()`:

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

Add `_build_trace(uow_id: str, register: str, outcome: ExecutorOutcome, ...) -> dict` — constructs the trace dict. The `register` value comes from the `WorkflowArtifact.executor_type` field.

**Full trace.json schema** (canonical):

```json
{
  "uow_id": "<id>",
  "register": "<operational|iterative-convergent|philosophical|human-judgment>",
  "execution_summary": "<1-3 sentence prose: what happened in this execution cycle>",
  "surprises": ["<string: unexpected finding, constraint, or blocking condition>"],
  "prescription_delta": "<what would change the next prescription>",
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

`score` is a float 0.0-1.0 where 1.0 = gate fully passed (e.g., all tests passing), 0.0 = gate completely failing. For non-iterative-convergent registers, `gate_score` is `null`.

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

## 5. Hook Enforcement

### SubagentStop Contract (unchanged from V2, enforced more strictly)

Every executor **must** write `{output_ref}.result.json` before transitioning to `ready-for-steward`. This applies at all exit paths.

**V3 addition — corrective trace contract:**

Every executor **must also** write `{output_ref}.trace.json` alongside the result. Minimum schema: see Change 6 above.

Absence of `trace.json` is logged as a contract violation but does not block Steward re-entry (unlike `result.json` absence, which blocks completion declaration).

### Register-Mismatch Gate (V3 new)

Before the Steward writes a prescription, it runs a single gate check:

```
if prescription.executor_type incompatible_with uow.register:
    log warning
    transition(uow, "blocked")
    surface to Dan: "Register mismatch: UoW is {register}, prescription targets {executor_type}"
```

This gate fires before the workflow artifact is written — no category-wrong dispatch reaches the Executor.

---

## 6. PR Sequencing Table

| PR | Title | Contents | Depends on |
|---|---|---|---|
| PR A | `executor: write trace.json at all exit paths (Issue #608)` | `_write_trace_json()`, `_build_trace()`, `_insert_corrective_trace()`, trace write in `_run_execution()` / `report_partial()` / `report_blocked()` / exception handler | PRs #601, #607 (already merged) |
| PR B | `executor: register-appropriate routing via executor_type dispatch table` | `_ITERATIVE_ENGINEER_PREAMBLE`, `_FRONTIER_WRITER_PREAMBLE`, `_DESIGN_REVIEW_PREAMBLE`, `_EXECUTOR_TYPE_TO_DISPATCHER` mapping in `_run_execution()` | PR A (trace.json must ship before or with this — the new dispatchers must write trace.json) |
| PR C | `steward: register-aware diagnosis + corrective trace injection` | `_register_completion_policy()`, `_assess_completion()` policy branch, `_read_trace_json()`, `_count_non_improving_gate_cycles()`, trace injection in prescribe branch | PR A (steward reads trace.json — must exist before this is useful) |
| PR D | `steward: register-mismatch gate + expanded Dan interrupt conditions` | `_check_register_executor_compatibility()`, mismatch gate in prescribe branch, `philosophical_register` / `no_gate_improvement` stuck conditions, `_detect_stuck_condition()` additions, surface message formatting | PR C (register-aware diagnosis must be in place so mismatch detection has correct context) |

**Recommended sequence**: A -> B (can be one PR if small) -> C -> D.

---

## 7. What's Different from V2

| Dimension | V2 | V3 |
|-----------|----|----|
| Register awareness | Absent — all UoWs dispatched via same executor path | Structural — register classified at germination, immutable, gates prescription |
| Completion evaluation | success_criteria prose evaluated by Steward | Register-matched: machine-observable gates for operational/iterative, human surface for philosophical/human-judgment |
| Corrective traces | Absent | Required on every executor return — accumulate in garden |
| Iterative mode | Designed in ralph-wos-seed.md, never implemented | First-class register with gate command, cycle scoring, and no-improvement escalation |
| 0.8% failure root cause | Unaddressed | Addressed by register-mismatch gate — category-wrong dispatch is blocked before execution |
| Garden coupling | Steward reads no prior learning artifacts | Steward reads garden_context (corrective traces) at diagnosis time |
| Dan interrupt trigger | 3 conditions: severe/Dan-useful/steward_cycles>=5 | Same 3 plus: register mismatch, gate failure for 3+ cycles, philosophical UoWs always surface |
| DB path | Two conflicting DBs (data/wos.db, orchestration/registry.db) | Single canonical path: orchestration/registry.db. Documented. Migration required. |

**What stays the same:**
- State machine (proposed -> pending -> ready-for-steward -> diagnosing -> ready-for-executor -> active -> ready-for-steward -> done)
- 6-step atomic claim sequence
- Optimistic locking
- Crash recovery and startup sweep
- Steward/Executor isolation via executor_uow_view
- result.json contract
- Steward private fields (steward_agenda, steward_log)
- Vision Object anchoring (vision_ref)

---

## 8. What Remains Unsolved

**1. The Cultivator gap — two actors, not one.** The Cultivator remains aspirational, and it splits into two distinct actors: a Tending Cultivator (orientation-register, human-in-loop, possibly Dan's own act of re-reading) and a Filing Cultivator (execution-register, automatable as the `github-issue-cultivator` job). The seam between the two is germination. Until this split is built, register classification is applied retroactively to existing issues.

**2. Register inference at scale.** The classification algorithm is a rule-based heuristic. At 252 UoWs, it will misclassify a meaningful fraction. V3 needs an observability instrument: a log of all register classifications with confidence signals.

**3. Garden write discipline.** Corrective traces are only valuable if they are retrieved and read at diagnosis time. The garden retrieval mechanism must be tested with real traces before the system relies on it.

**4. Dan's register signal.** The mechanism for detecting Dan's current register is unspecified (recent Telegram message tone? vision.yaml current_focus? explicit declaration?). This is a design gap with real behavioral consequences.

**5. Feedback arm for Dan-surfaced UoWs.** When the Steward surfaces a UoW to Dan, delivery is not closure. Dan's reply must be detected, transition the UoW, and write a closure record. The mechanism for all three steps is unspecified.

**6. Trust-level autonomy gates.** V3 should distinguish: operational UoWs with machine-observable gates could auto-advance beyond `proposed`. The autonomy gate logic needs design.

**7. The scaling governor.** V3 addresses register mismatch but not the structural pattern: the system immediately overextended to maximum load when it became operational. A scaling governor that modulates batch size or execution rate based on recent success signal is the missing gate.

---

## 9. Testability Notes

### Change 1 — Register-Aware Diagnosis

**Unit tests**: `_register_completion_policy()` is a pure function — test all 4 registers directly. `_assess_completion()` with a `philosophical` UoW and a valid `result.json` must return `is_complete=False`. `_assess_completion()` with `human-judgment` and no `close_reason` must return `is_complete=False`.

### Change 2 — Corrective Trace Injection

**Unit tests**: `_read_trace_json()` — test valid file, missing file, mismatched uow_id, malformed JSON. `_count_non_improving_gate_cycles()` — test with 0, 1, 2, 3, 4 gate score entries at various improvement levels.

### Change 3 — Register-Mismatch Gate

**Unit tests**: `_check_register_executor_compatibility()` — 20 test cases covering all (register, executor_type) pairs in the compatibility table.

### Change 4 — Expanded Dan Interrupt Conditions

**Unit tests**: Each new stuck condition has a dedicated test: `philosophical_register` fires on cycle >= 1 for philosophical UoW; `no_gate_improvement` fires when 3 consecutive gate scores are equal; `register_mismatch` fires before artifact write.

### Change 5 — Register-Appropriate Executor Routing

**Unit tests**: `_run_execution()` with a workflow artifact carrying `executor_type="frontier-writer"` — confirm `_dispatch_via_frontier_writer` is called.

### Change 6 — trace.json Write Requirement

**Unit tests**: After `_run_execution()` returns (mocked dispatcher), check that `{output_ref}.trace.json` exists and is valid JSON. Validate `gate_score` is null for operational; validate it has `command`/`result`/`score` for iterative-convergent.

---

## 10. V4 Design Directions

*Captured from philosophical review session, 2026-04-04. These are not current PRs — they are design directions to hold as V3 lands and V4 scope opens.*

### Direction 1 — Philosopher Routing as Composable Cartridge

The Dan-interrupt path should become a composable cartridge system: OODA-coupled (fires on lack-of-clarity and suspect-of-certainty), cartridge-swappable by UoW register/content, with slight randomness to prevent calcification.

### Direction 2 — Dan Attentional State + Reversible Forward Commitment

When Dan is unavailable, the system makes a reversible forward commitment: proceed with the best available decision, document the decision point explicitly, and tag it as "future refactor potential."

### Direction 3 — Garden Retrieval as Structural Prerequisite for Orient Phase Quality

The Orient phase needs explicit retrieval receipts, sparsity signals, and cross-cycle synthesis (S3) to be real rather than nominal.

### Direction 4 — Scaling Governor (S4)

A governor that reads recent `gate_score` history and UoW completion rate to modulate batch size and execution rate. The deepest open structural gap from Coherence's collapse.

---

## 11. Relationship to Larger Philosophy

V3 embodies three heuristics from Dan's vocabulary:

**Phase alignment:** A UoW dispatched into the wrong register is misaligned regardless of how well the mechanics execute. Register classification is the phase-alignment mechanism.

**Thermodynamic efficiency:** Category-wrong dispatch wastes full execution cycles producing outputs that cannot close the loop. The register-mismatch gate blocks the expensive operation before it consumes resources.

**Cybernetic extension:** The corrective trace mechanism is not logging — it is learning. The system accumulates experience in a form that shapes future decisions. This is the distinction between a task runner (executes, forgets) and a cybernetic extension (executes, learns, orients next pass more precisely).

---

## 12. Registry Operations

See: [docs/wos/current/wos-registry-reference.md](wos-registry-reference.md)

---

## 13. Critical Files for Implementation

- `/home/lobster/lobster/src/orchestration/steward.py`
- `/home/lobster/lobster/src/orchestration/executor.py`
- `/home/lobster/lobster/src/orchestration/workflow_artifact.py`
- `/home/lobster/lobster/src/orchestration/registry.py`
- `/home/lobster/lobster/src/orchestration/migrations/0007_wos_v3_register_and_corrective_traces.sql`

---

## Related Documents

- **[wos-registry-reference.md](wos-registry-reference.md)** — UoW status state machine, registry path, and injection pattern.
- **[wos-constitution.md](wos-constitution.md)** — WOS constitutional principles.
- **[wos-golden-pattern.md](wos-golden-pattern.md)** — Golden patterns for WOS operations.
- **[wos-orchestration-landscape.md](wos-orchestration-landscape.md)** — WOS orchestration landscape overview.

---

*Unified from: wos-v3-proposal.md and wos-v3-steward-executor-spec.md. Source documents archived at docs/wos/archived/.*
