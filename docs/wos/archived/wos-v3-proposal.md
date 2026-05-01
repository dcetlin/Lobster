> **Status: SUPERSEDED**
> Superseded by: `docs/wos/current/wos-v3-spec.md`
> Do not treat as authoritative. Retained for historical reference only.

# Work Orchestration System v3 — Design Proposal

*Status: Proposal — 2026-04-04*
*Author: Lobster subagent synthesis from multi-thread research*

> **Registry reference**: For UoW status state machine, registry path, and injection pattern, see [docs/wos-registry-reference.md](wos-registry-reference.md).

---

## 1. Vision

In full flourishing, WOS V3 is not a task runner. It is a living cybernetic substrate — an extension of Dan's attentional field that knows what register a piece of work lives in, routes it accordingly, evaluates completion against criteria that match the work's ontological category, and collapses the loop between observation and closed action at minimum thermodynamic cost. Seeds arrive from any source (philosophy sessions, nightly sweeps, voice notes, Telegram). The Steward classifies each into its register — operational, philosophical, iterative-convergent, or human-judgment — and dispatches it into an execution context that matches that register. Pearls circulate through gardens. Executors close loops and write corrective traces. The Observation Loop detects drift before Dan notices it. The system's own structure is a living argument for what Dan is building toward.

---

## 2. Core Premises

**Why WOS exists:**
WOS exists because non-convergent conversations accumulate as issued that never close. A conversation that surfaces a problem, files a GitHub issue, and then stalls has discharged its surface energy without converting it to kinetic change. WOS is the mechanism that converts observations into closed loops.

**What the 0.8% success rate revealed:**
The overnight 50-run (252 UoWs injected, 250 failed) was not a bug — it was a signal. The V2 executor (`claude -p` subprocess, synchronous) was running tasks that were category-wrong for the execution model. Operational tasks dispatched into philosophical register contexts, philosophical seeds dispatched as code execution tasks, register-mismatch throughout. The pipeline mechanics were sound (3-minute small-batch tests proved this). The routing intelligence was absent.

**Three structural misalignments in V2:**

1. **Register blindness** — The Steward prescribed workflow primitives without regard to what register the UoW lived in. A design document prescription and a bug-fix prescription use the same machinery. They require fundamentally different execution contexts.

2. **Completion criterion mismatch** — `success_criteria` was required at germination but was evaluated against outputs without checking whether the criteria and the work were in the same ontological category. "Implementation-ready spec" is a human-judgment criterion, not a machine-observable one.

3. **The mode field was aspirational** — The `ralph-wos-seed.md` design exploration correctly identified that some UoWs are machine-observable-done (tests pass) and others require human judgment. V2 never operationalized this distinction.

**What V3 resolves:**
V3 operationalizes register classification, matches completion evaluation to work ontology, and installs a gate before execution that prevents category-wrong dispatch. It is not a rewrite — it is a precision refinement of the V2 mechanics with register-awareness as the new structural primitive.

---

## 3. Architecture

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

---

## 4. The Dispatch Loop

The V3 loop is OODA at every scale:

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

## 5. Register Taxonomy

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

## 6. Hook Enforcement

### SubagentStop Contract (unchanged from V2, enforced more strictly)

Every executor **must** write `{output_ref}.result.json` before transitioning to `ready-for-steward`. This applies at all exit paths.

**V3 addition — corrective trace contract:**

Every executor **must also** write `{output_ref}.trace.json` alongside the result. Minimum schema:

```json
{
  "uow_id": "<id>",
  "register": "<register used>",
  "execution_summary": "<1-3 sentences>",
  "surprises": ["<anything unexpected>"],
  "prescription_delta": "<what would change the prescription if ran again>",
  "gate_score": null,  // or {"command": "...", "result": "...", "score": 0.9}
  "timestamp": "<ISO-8601>"
}
```

Absence of `trace.json` is logged as a contract violation but does not block Steward re-entry (unlike `result.json` absence, which blocks completion declaration).

### BOOTUP_CANDIDATE_GATE

Unchanged. When True, UoWs with `bootup-candidate` label are skipped by the Steward.

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

## 7. What's Different from V2

| Dimension | V2 | V3 |
|-----------|----|----|
| Register awareness | Absent — all UoWs dispatched via same executor path | Structural — register classified at germination, immutable, gates prescription |
| Completion evaluation | success_criteria prose evaluated by Steward | Register-matched: machine-observable gates for operational/iterative, human surface for philosophical/human-judgment |
| Corrective traces | Absent | Required on every executor return — accumulate in garden |
| Iterative mode | Designed in ralph-wos-seed.md, never implemented | First-class register with gate command, cycle scoring, and no-improvement escalation |
| 0.8% failure root cause | Unaddressed | Addressed by register-mismatch gate — category-wrong dispatch is blocked before execution |
| Garden coupling | Steward reads no prior learning artifacts | Steward reads garden_context (corrective traces) at diagnosis time |
| Dan interrupt trigger | 3 conditions: severe/Dan-useful/steward_cycles≥5 | Same 3 plus: register mismatch, gate failure for 3+ cycles, philosophical UoWs always surface |
| DB path | Two conflicting DBs (data/wos.db, orchestration/registry.db) | Single canonical path: orchestration/registry.db. Documented. Migration required. |

**What stays the same:**
- State machine (proposed → pending → ready-for-steward → diagnosing → ready-for-executor → active → ready-for-steward → done)
- 6-step atomic claim sequence
- Optimistic locking
- Crash recovery and startup sweep
- Steward/Executor isolation via executor_uow_view
- result.json contract
- Steward private fields (steward_agenda, steward_log)
- Vision Object anchoring (vision_ref)

---

## 8. What Remains Unsolved

**1. The Cultivator gap — two actors, not one.** The Cultivator remains aspirational, and it splits into two distinct actors:

- **Tending Cultivator** (orientation-register): attends to philosophy session outputs phenomenologically; holds candidate pearls for re-encounter before routing; cannot be fully automated without collapsing orientation-register work into production tasks. This actor may be Dan's own act of re-reading session output in a subsequent session — not a Lobster subagent at all.
- **Filing Cultivator** (execution-register): takes confirmed seeds and files them as GitHub issues with `success_criteria` written at germination time; can and should be automated. The existing `github-issue-cultivator` scheduled job is this actor.

The seam between the two is **germination** — the event at which a candidate from the orientation basin is classified as ready for the execution basin. Until this split is built and wired, register classification at germination is applied retroactively to existing issues — the Registrar must infer register from issue content rather than receiving it from the classification step. Inference quality is the bottleneck. Premature germination (filing a GitHub issue before a seed's inquiry has stabilized) produces an issue with underspecified `success_criteria`.

**2. Register inference at scale.** The classification algorithm above is a rule-based heuristic. At 252 UoWs, it will misclassify a meaningful fraction. The misclassification rate is currently invisible. V3 needs an observability instrument: a log of all register classifications with confidence signals, so the Observation Loop can flag systematic misrouting.

**3. Garden write discipline.** Corrective traces are only valuable if they are retrieved and read at diagnosis time. The garden retrieval mechanism (vector search against memory.db) must be tested with real traces before the system relies on it. Retrieval quality is unknown.

**4. Dan's register signal.** The Steward is supposed to read "Dan's current register" from context — but the mechanism for detecting Dan's current register is unspecified. Is it from recent Telegram message tone? From vision.yaml current_focus? From explicit declaration? This is a design gap with real behavioral consequences: a Steward that misjudges Dan's register sends the right work at the wrong time.

**5. Feedback arm for Dan-surfaced UoWs.** When the Steward surfaces a UoW to Dan — in any register — the current architecture treats delivery as completion. The UoW is marked delivered when it reaches Dan's Telegram. But delivery is not closure: closure is when Dan's encounter with the item modifies something in the system's state.

This is not a philosophical UoW lifecycle problem specifically — it applies to every surfaced item across all registers. When Dan replies to a surfaced UoW (with "acknowledged," "reject," "good but keep going," or any response), that reply must:
1. Be detected by the Steward as a closure signal
2. Transition the UoW to the appropriate state
3. Write a closure record to the UoW's source or output path

The mechanism for all three steps is unspecified. Until this feedback arm exists, the Dan-interrupt path is a delivery system with no receiver — it sends and never hears back. The orientation basin's reflective surface queue has the same pathology: items are marked `delivered: true` but no write-back path closes the loop.

**6. Trust-level autonomy gates.** V2 required Dan's `/confirm` for all UoWs. V3 should distinguish: operational UoWs with machine-observable gates could auto-advance beyond `proposed`. The autonomy gate logic needs design before it can be implemented safely.

**7. The scaling governor.** V3 addresses register mismatch but not the structural pattern: Coherence immediately overextended to maximum load. The system became operational and saturated its dispatch capacity before any feedback signal could moderate it. A scaling governor that modulates batch size or execution rate based on recent success signal is the missing gate. Not in current PRs — but in the design horizon. Without it, V3 is vulnerable to the same failure mode as V2 at scale: the system works until it doesn't, with no mechanism to gracefully throttle before collapse.

---

## 9. Relationship to Larger Philosophy

V3 embodies three heuristics from Dan's vocabulary:

**Phase alignment:** A UoW dispatched into the wrong register is misaligned regardless of how well the mechanics execute. Register classification is the phase-alignment mechanism — it ensures work arrives in a context that can receive it.

**Thermodynamic efficiency:** Category-wrong dispatch wastes full execution cycles producing outputs that cannot close the loop. The register-mismatch gate is a thermodynamic gate: it blocks the expensive operation (executor dispatch) before it consumes resources on work that will fail to converge.

**Cybernetic extension:** The corrective trace mechanism is not logging — it is learning. When the Steward reads prior traces at diagnosis time, it is extending Dan's orientation across time. The system accumulates experience in a form that shapes future decisions. This is the distinction between a task runner (executes, forgets) and a cybernetic extension (executes, learns, orients next pass more precisely).

The dual register — operational and philosophical — is not a technical distinction. It mirrors the fundamental frequency distinction: operational register is the outer expression of building; philosophical register is the inner coherence that makes the building worth doing. A system that can only operate in one is not a full extension of what Dan is.

---

*Synthesized from: wos-v2-design.md, ralph-wos-seed.md, human-ai-ooda-protocol.md, vision.yaml, registers.md (frontier), hygiene sweeps 2026-04-03/04, overnight test observations, GitHub landscape research (RALPH loop, BabyAGI/AutoGPT architectures, LLM agent canonical patterns), and cybernetics theory (Ashby's requisite variety, VSM, Boyd's OODA).*

---

## Steward–Executor Contract

*Detailed steward/executor contract — integrated from wos-v3-steward-executor-spec.md*

Full spec: [wos-v3-steward-executor-spec.md](./wos-v3-steward-executor-spec.md)

The implementation spec translates this proposal's architecture into 6 concrete V3 changes across `steward.py` and `executor.py`. Summary:

**Change 1 — Register-Aware Diagnosis (Steward):** `_assess_completion()` gains a `_register_completion_policy()` branch. Operational/iterative-convergent UoWs use the existing machine-gate path. Philosophical UoWs are never auto-closed — `is_complete` is always `False`, routing to a new `philosophical_register` stuck condition. Human-judgment UoWs require an explicit `close_reason` (Dan confirmation) before closing.

**Change 2 — Corrective Trace Injection (Steward):** After the trace gate, the Steward reads `{output_ref}.trace.json` and injects `surprises` and `prescription_delta` into the prescription context. For iterative-convergent UoWs, `gate_score` is read and tracked; no improvement across 3 cycles triggers the `no_gate_improvement` Dan interrupt. A loop gain bounding step (`_bound_prescription_delta()`) guards against oscillation from aggressive corrective deltas.

**Change 3 — Register-Mismatch Gate (Steward):** Before writing a workflow artifact, `_check_register_executor_compatibility(register, executor_type)` runs. If a `philosophical` or `human-judgment` UoW would be dispatched to `functional-engineer`, the artifact is not written, the UoW transitions to BLOCKED, and Dan is surfaced with context. Every gate fire emits a structured `register_mismatch_observation` to the audit log for downstream pattern detection.

**Change 4 — Expanded Dan Interrupt Conditions (Steward):** Three new stuck conditions: `philosophical_register` (fires after first executor cycle on philosophical UoWs), `register_mismatch` (Change 3), and `no_gate_improvement` (iterative-convergent with stalled gate scores over 3 cycles). Each has a distinct surface message format.

**Change 5 — Register-Appropriate Executor Routing (Executor):** `_run_execution()` reads `executor_type` from the workflow artifact and dispatches to the appropriate function via `_EXECUTOR_TYPE_TO_DISPATCHER`. New preambles: `_ITERATIVE_ENGINEER_PREAMBLE` (run gate command, score, iterate), `_FRONTIER_WRITER_PREAMBLE` (phenomenological synthesis, no PR), `_DESIGN_REVIEW_PREAMBLE` (structured analysis for Dan's review). Existing `functional-engineer` and `lobster-ops` paths unchanged.

**Change 6 — trace.json Write Requirement (Executor):** `_write_trace_json()` and `_insert_corrective_trace()` added. Trace is written at all 4 exit paths (complete, partial, blocked, exception). The corrective_traces DB table (migration 0007) receives a best-effort INSERT alongside the file write. For iterative-convergent UoWs, `gate_score` must include `command`, `result`, and a float `score` (0.0–1.0).

**PR sequence:** A (trace.json write) → B (register routing) → C (register-aware diagnosis) → D (mismatch gate + expanded interrupts). A must precede C; B and C can be developed in parallel.

**V4 design directions** captured in the spec: philosopher routing as composable cartridge (S5), Dan attentional state + reversible forward commitment, garden retrieval as structural prerequisite for Orient phase quality, and the scaling governor (S4) — the deepest open structural gap from Coherence's collapse.

---

## Related Documents

- **[wos-v3-steward-executor-spec.md](wos-v3-steward-executor-spec.md)** — Detailed steward/executor implementation spec: 6 V3 changes with full code locations, testability notes, PR sequencing (A → B → C → D), and V4 design directions including the scaling governor (S4). This is the addendum to the unified reference above.
- **[wos-v3-convergence.md](../philosophy/frontier/wos-v3-convergence.md)** — Seeds, sprouts, and pearls synthesis from multi-thread philosophical review. Contains S1–S5 candidate spec additions, ungoverned timescales, and final bearings.
- **[corrective-trace-loop-gain-research.md](corrective-trace-loop-gain-research.md)** — Research note on bounded correction in the corrective trace feedback loop. Directly relevant to section 4 (the corrective trace contract) and section 8 item 3 (garden write discipline).
- **[2026-04-04-philosopher-cybernetics.md](../philosophy/sessions/2026-04-04-philosopher-cybernetics.md)** — Cybernetics philosopher session (Ashby's Law). Grounds section 9's cybernetic extension claim and names residual variety-matching gaps.
- **[2026-04-04-philosopher-theory-of-learning.md](../philosophy/sessions/2026-04-04-philosopher-theory-of-learning.md)** — Theory of Learning philosopher session. Grounds section 8 item 7 (scaling governor) and the Discernment-Coherence-Attunement arc framing.
- **[2026-04-04-philosopher-mito-governor.md](../philosophy/sessions/2026-04-04-philosopher-mito-governor.md)** — Mito-governor philosopher session. Grounds the timescale observations (register-portfolio diversity, cross-cycle learning) and the four-governor-as-one-structure observation.
