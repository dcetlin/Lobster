# WOS V3 — Architecture and Implementation Spec

> **Status: AUTHORITATIVE**
> This is the single source of truth for WOS v3 architecture and implementation.
> wos-v3-proposal.md and wos-v3-steward-executor-spec.md are archived at docs/wos/archived/.

## 1. Vision and Design Premises

### Vision

In full flourishing, WOS V3 is not a task runner. It is a living cybernetic substrate — an extension of Dan's attentional field that knows what register a piece of work lives in, routes it accordingly, evaluates completion against criteria that match the work's ontological category, and collapses the loop between observation and closed action at minimum thermodynamic cost. Seeds arrive from any source (philosophy sessions, nightly sweeps, voice notes, Telegram). The Steward classifies each into its register — operational, philosophical, iterative-convergent, or human-judgment — and dispatches it into an execution context that matches that register. Pearls circulate through gardens. Executors close loops and write corrective traces. The Observation Loop detects drift before Dan notices it. The system's own structure is a living argument for what Dan is building toward.

### Core Premises

**Why WOS exists:**
WOS exists because non-convergent conversations accumulate as issues that never close. A conversation that surfaces a problem, files a GitHub issue, and then stalls has discharged its surface energy without converting it to kinetic change. WOS is the mechanism that converts observations into closed loops.

**What the 0.8% success rate revealed:**
The overnight 50-run (252 UoWs injected, 250 failed) was not a bug — it was a signal. The V2 executor was running tasks that were category-wrong for the execution model. Operational tasks dispatched into philosophical register contexts, philosophical seeds dispatched as code execution tasks, register-mismatch throughout. The pipeline mechanics were sound (3-minute small-batch tests proved this). The routing intelligence was absent.

**Three structural misalignments in V2:**

1. **Register blindness** — The Steward prescribed workflow primitives without regard to what register the UoW lived in.
2. **Completion criterion mismatch** — `success_criteria` was evaluated against outputs without checking whether the criteria and the work were in the same ontological category.
3. **The mode field was aspirational** — The `ralph-wos-seed.md` design exploration correctly identified that some UoWs are machine-observable-done and others require human judgment. V2 never operationalized this distinction.

**What V3 resolves:**
V3 operationalizes register classification, matches completion evaluation to work ontology, and installs a gate before execution that prevents category-wrong dispatch.

## 2. Architecture Overview

```
SEEDS (any source)
  Telegram / voice note / philosophy session / nightly sweep / direct request
      |
      v
CULTIVATOR (classification gate) — two actors:
  Tending Cultivator (orientation-register, human-in-loop)
  Filing Cultivator (execution-register, automatable)
      |
      v
UoW REGISTRAR (germination gate)
  Classifies register at germination time
  Creates UoWRegistry entry with:
    - success_criteria (required, ontology-matched)
    - uow_mode: operational | philosophical | iterative | human-judgment
    - register: the attentional configuration required for evaluation
      |
      v
STEWARD (diagnosis + prescription engine)
  Reads: UoW record, steward_agenda, audit_log, garden_context
  Diagnoses: what does this UoW need? In what register?
  Prescribes: workflow primitive + executor context + skills + register hint
  Evaluates (on re-entry): did the executor's output close the loop?
    - operational/iterative: evaluate against machine-observable gate
    - philosophical/human-judgment: surface to Dan with evidence
      |              |
      v              v
EXECUTOR        DAN INTERRUPT
  Claims UoW      Receives surfaced UoWs
  Dispatches      Provides orientation or decision
  via register-   Returns UoW to Steward with Dan's
  appropriate     classification added
  context
  Writes result.json + corrective trace
  Returns to ready-for-steward
      |
      v
OBSERVATION LOOP
  Detects: stalled active UoWs, dark pipeline, ready-queue growth, register-drift
```

### The Dispatch Loop (OODA)

```
# OBSERVE
uow = registry.next_ready_for_steward()
prior_outputs = read(uow.output_ref) if uow.output_ref else None
audit_history = read(uow.audit_log)
garden_context = garden.relevant_to(uow.title, uow.register)

# ORIENT
diagnosis = steward.diagnose(uow, prior_outputs, audit_history, garden_context)

# DECIDE
if diagnosis.completion_met:
    transition(uow, "done")
    write_corrective_trace(uow, diagnosis)
elif diagnosis.surface_to_dan:
    transition(uow, "blocked")
    send_dan_surface(uow, diagnosis)
else:
    prescription = steward.prescribe(uow, diagnosis, PRIMITIVES)
    write_workflow_artifact(prescription)
    transition(uow, "ready-for-executor")

# ACT (Executor)
claim(uow)  # atomic 6-step sequence
result = execute(prescription, register=uow.register)
write_result_json(result)
write_corrective_trace(uow, result)  # V3 — learning artifact
transition(uow, "ready-for-steward")
```

### Register Taxonomy

| Register | Presupposes | Success evaluation | Dispatch |
|---|---|---|---|
| **Operational** | Executor attending to concrete, externally-verifiable outcomes | Machine-observable (tests pass, CI green, file written, PR merged) | functional-engineer |
| **Iterative-Convergent** | Executor that can run a work loop, evaluate against a gate score | Machine-observable gate score improving across cycles | iterative-engineer |
| **Philosophical** | Executor attending to phenomenological content | Human judgment only — surfaces to Dan with evidence | frontier-writer |
| **Human-Judgment** | Human reader evaluating against non-formalizable criteria | Dan's explicit confirmation — no other path to done | design-review |

Register is classified at germination and is **immutable**. If the Steward determines the register is wrong, it surfaces to Dan — it does not reclassify autonomously.

## 3. Implementation Spec

### Change 1 — Register-Aware Diagnosis (Steward)

**Location**: `steward.py` — `_diagnose_uow()` and `_assess_completion()`

Add `_register_completion_policy(register: str) -> str`:
- `"machine-gate"` for `operational` and `iterative-convergent`
- `"always-surface"` for `philosophical`
- `"require-confirmation"` for `human-judgment`

Policy application in `_assess_completion()`:
1. **machine-gate**: existing logic unchanged
2. **always-surface**: `is_complete` always `False`; new stuck condition `"philosophical_register"`
3. **require-confirmation**: `is_complete` `False` unless `uow.close_reason` populated (Dan confirmation)

### Change 2 — Corrective Trace Injection (Steward)

**Location**: `steward.py` — `_process_uow()`, prescribe branch

Add `_read_trace_json(output_ref: str) -> dict | None` — reads and validates `{output_ref}.trace.json`.

Trace fields used in diagnosis:

| Field | Usage |
|---|---|
| `execution_summary` | Logged to steward_log |
| `surprises` | Prepended to prescription context |
| `prescription_delta` | Appended to prescription context (bounded by `_bound_prescription_delta()`) |
| `gate_score` | For iterative-convergent: tracked across cycles; 3 non-improving → `no_gate_improvement` |

Loop gain bounding (S1): `_bound_prescription_delta(delta, history) -> str` bounds raw deltas before injection to prevent oscillation.

### Change 3 — Register-Mismatch Gate (Steward)

**Location**: `steward.py` — prescribe branch, before `_write_workflow_artifact()`

`_check_register_executor_compatibility(register, executor_type) -> (bool, str)`:

| executor_type | Compatible registers |
|---|---|
| `functional-engineer` | operational, iterative-convergent |
| `lobster-ops` | operational, iterative-convergent |
| `general` | operational |
| `frontier-writer` | philosophical |
| `design-review` | human-judgment |

On mismatch: do not write artifact, set `stuck_condition = "register_mismatch"`, transition to BLOCKED, surface to Dan. Emit structured `register_mismatch_observation` to audit log.

### Change 4 — Expanded Dan Interrupt Conditions (Steward)

V3 additions to `_detect_stuck_condition()`:

| Condition | Trigger |
|---|---|
| `philosophical_register` | philosophical UoW after first executor cycle |
| `register_mismatch` | Change 3 gate fire |
| `no_gate_improvement` | iterative-convergent gate score not improving over 3 cycles |

### Change 5 — Register-Appropriate Executor Routing (Executor)

**Location**: `executor.py` — `_run_execution()`

```python
_EXECUTOR_TYPE_TO_DISPATCHER = {
    "functional-engineer": _dispatch_via_claude_p,
    "lobster-ops": _dispatch_via_claude_p,
    "general": _dispatch_via_claude_p,
    "frontier-writer": _dispatch_via_frontier_writer,
    "design-review": _dispatch_via_design_review,
}
```

New preambles: `_ITERATIVE_ENGINEER_PREAMBLE`, `_FRONTIER_WRITER_PREAMBLE`, `_DESIGN_REVIEW_PREAMBLE`.

### Change 6 — trace.json Write Requirement (Executor)

`_write_trace_json(output_ref, trace)` — mirrors `_write_result_json()`. Written at all 4 exit paths (complete, partial, blocked, exception).

trace.json schema:
```json
{
  "uow_id": "<id>",
  "register": "<operational|iterative-convergent|philosophical|human-judgment>",
  "execution_summary": "<1-3 sentences>",
  "surprises": ["<unexpected finding>"],
  "prescription_delta": "<what would change the next prescription>",
  "gate_score": null,
  "timestamp": "<ISO-8601 UTC>"
}
```

For iterative-convergent: `gate_score` = `{"command": "...", "result": "...", "score": 0.85}`.

Best-effort INSERT to `corrective_traces` DB table alongside file write.

### PR Sequencing

A (trace.json write) -> B (register routing) -> C (register-aware diagnosis) -> D (mismatch gate + expanded interrupts)

Critical dependency: A before C (Steward reads trace.json that Executor must produce).

## 4. Registry Operations

See: docs/wos/current/wos-registry-reference.md

## 5. What Remains Unsolved

1. **Cultivator gap** — two actors (tending/filing), not yet wired
2. **Register inference at scale** — rule-based heuristic misclassification rate is invisible
3. **Garden write discipline** — retrieval quality unknown until tested with real traces
4. **Dan's register signal** — mechanism for detecting Dan's current register is unspecified
5. **Feedback arm for Dan-surfaced UoWs** — delivery != closure
6. **Trust-level autonomy gates** — operational UoWs could auto-advance
7. **Scaling governor** — V3 addresses register mismatch but not overextension at scale

## 6. V4 Design Directions

- Philosopher routing as composable cartridge (S5)
- Dan attentional state + reversible forward commitment
- Garden retrieval as structural prerequisite for Orient phase quality
- Scaling governor (S4) — modulates batch size and execution rate based on success signals

---

*Unified from: docs/wos/archived/wos-v3-proposal.md and docs/wos/archived/wos-v3-steward-executor-spec.md*
