# WOS Design Audit — 2026-04-08

**Scope:** Design audit of the WOS Steward/Executor pipeline — Stage 1 (vision alignment) + Stage 2 (implementation quality)
**Context docs read:** wos-v2-design.md, wos-v3-proposal.md, sprint2-uow-pipeline-report.md, mito-modeling.md, governor-timing-structure.md, wos-v3-convergence.md, executor-heartbeat.py, steward.py (excerpted), learnings.md, golden-patterns.md, vision.yaml
**Auditor posture:** adversarial prior seeded before implementation review (golden-patterns.md 2026-03-27)

---

## Stage 1: Vision Alignment (written before re-reviewing implementation)

### Adversarial prior

The prior entering this review: WOS is being built as a pipeline-throughput optimization system wearing the costume of a mitochondrial model. The biological framing is rich and coherent, but the execution metrics being optimized — UoW dispatch rate, cycle count, false-complete recovery, staleness gates — are executor-throughput signals, not governor-health signals. The mitochondrial model says health is controlled oscillation at sustainable rate with mandatory temporal spacing between cause and next action. What the Sprint 2 work actually measures is: did we close the three UoWs? The prior is that these are not the same question.

### Stage 1 questions

**What is the implicit theory of change in vision.yaml?**

The vision's theory of change is: build a substrate that lets agents answer "is this aligned with Dan's intent?" by pointing to a structural field, not paraphrasing prose. The active_project.phase_intent names three completion criteria: Registry live and populated by sweeper, UoWs carry vision_ref pointing to the Vision Object layer, morning briefing surfaces staleness warnings. None of these are about executor throughput, cycle counts, or false-complete bugs.

**What would have to be true for WOS sprint work to be the right path?**

The sprint work (S2-A: cycle trace logging, S2-B: prescription format, S2-C: observability metrics) would be the right path if the pipeline's primary bottleneck is observability deficit — if the system fails because we cannot see what it is doing. The Sprint 2 pipeline report disconfirms this. The bottleneck was not observability: it was three structural dispatch bugs (staleness gate, age anchor, false-complete). All three were visible in logs without any of the S2 observability PRs. The S2 observability work adds instrumentation after the fact, over a pipeline that required 4-5 manual resets per UoW and three emergency mid-sprint bug fixes.

**Is there a cheaper test of the underlying assumption?**

The cheaper test: run the pipeline for 10 UoWs without any human resets and measure what fraction complete autonomously. Sprint 2 did not achieve this — S2-A and S2-B required extensive manual rescue. S2-C achieved it but benefited from fixes that S2-A/B's struggles exposed. The vision's principle-1 (structural prevention over reactive recovery) and principle-4 (wire what exists before building more) both suggest the test to run first is "can the pipeline complete one UoW cleanly without human intervention?" before building observability over a pipeline that cannot.

**What does this work foreclose?**

The sprint work forecloses relatively little structurally. The PRs are additive and read-only (analytics.py) or format-only (prescription front-matter) or additive instrumentation (cycle traces). The opportunity cost is real but not structural foreclosure.

**What is the opportunity cost?**

The opportunity cost is the vision.yaml success criteria that remain unbuilt: sc-1 (Registry query answering "what should I work on?" anchored to vision.current_focus), sc-2 (agents citing vision fields as routing basis), sc-3 (morning briefing staleness check). These are Phase 1 completion criteria per active_project.phase_intent. The sprint work serves the pipeline's internal health but does not advance these criteria.

**Does the design diverge from the mitochondrial model?**

This is the key adversarial question. The mitochondrial model, per the philosophy docs, says: health is not throughput maximization. Health is asymmetric governor behavior — both empty queue AND full queue are failure states. Health is oscillation, not maximization. Health is mandatory temporal spacing between cause and re-prescription. Health is commitment gate (threshold + amplification) not just threshold. 

The current implementation diverges on all three of these:
- **Throughput vs. oscillation:** The pipeline is designed for steady-state dispatch — prescribe as fast as the Steward cycle allows, claim as fast as the executor can, close as soon as result.json is present. The mitochondrial model says maximizing throughput is the pathological mode.
- **Commitment gate:** The hard cap (5 cycles) is the threshold. The amplification — cleanup arc, archive artifacts, close executor context, update garden with failure trace — does not exist. mito-modeling.md Section 5.1 names this as the highest-leverage gap. Sprint 2 did not address it.
- **Asymmetric governor:** The observation loop does not yet exist. The system has no backlog-starvation alert (cultivator not proposing) and no backlog-toxicity alert (executor under-capacity, forcing exceeds dissipation rate). mito-modeling.md Section 2, row 4, names this as "not yet implemented." Sprint 2 did not address it.

**Stage 1 finding:**

The Sprint 2 work closes three valid pipeline bugs and adds instrumentation. It is not the wrong work — but it is optimizing for executor throughput on a pipeline that still lacks the governing structures the mitochondrial model requires: commitment gate with cleanup arc, asymmetric governor (both failure directions), mandatory corrective trace as temporal gate (not just logging), and cross-UoW pattern learning. The vision.yaml success criteria (sc-1, sc-2, sc-3) remain unbuilt and are not advanced by Sprint 2. The adversarial prior partially survives: the system is adding execution capability before installing the governing structures that would make that execution self-regulating. The prior does not reach "this work is wrong" but it reaches "this work may be advancing the wrong dimension."

---

## Stage 2: Implementation Quality

### What the implementation does well

The Steward/Executor boundary is correctly drawn: the Steward is the sole writer of closure decisions; the Executor does the work but cannot declare done. governor-timing-structure.md's Isomorphism 3 ("delivery is not closure") is correctly instantiated. The result.json contract (uow_id validation, outcome field priority over backward-compat `success` field) is correctly enforced in `_assess_completion`. The optimistic lock pattern (`UPDATE ... WHERE status = 'ready-for-steward'`) correctly prevents concurrent Steward claims. The TTL recovery in executor-heartbeat is a correctly-designed safety net. The `_filter_stale_uows` fix (PR #667 — applying staleness gate only to previously-orphaned UoWs, not all fresh UoWs) is the right design.

### Implementation gaps

**1. Steward_cycles is reset on decide-retry — no lifetime tracking**

The registry field `steward_cycles` is reset to 0 on each decide-retry. S2-A's final registry shows `steward_cycles = 1` despite running approximately 20+ actual diagnosis cycles across all reset rounds. This means the hard cap is per-attempt, not per-UoW-lifetime. A UoW that consistently hits the hard cap and is manually reset repeatedly can run indefinitely — the cap provides no cumulative protection. The mitochondrial model requires the commitment gate to be irreversible once threshold is crossed. The current design makes the threshold resettable at will, which means it is not actually a commitment gate — it is a pause-and-reset mechanism.

**2. False-complete bug (#669) not yet fixed**

The executor writes `execution_complete` at dispatch time (when the `wos_execute` message is queued), not when the subagent returns work. This is the most structurally incorrect bug in the pipeline: the state machine claims execution is complete before any work has been done. The steward then evaluates completion against a null output_ref and creates a false closure. This bug was identified during Sprint 2 but not fixed during the sprint. It is in the post-sprint2 work list as item 1.

**3. Corrective trace is not a mandatory temporal gate**

Per mito-modeling.md Section 5.3 and governor-timing-structure.md Isomorphism 4, the corrective trace (trace.json) should be a mandatory blocking gate before re-prescription: the Steward cannot prescribe again until the prior executor's trace.json is written. Currently, trace.json absence is logged but not blocking. The cycle trace (`.cycles.jsonl`) added in S2-A records what happened but does not enforce the temporal spacing. The cristae-junction analog is present as observability but not as mandatory delay structure.

**4. Commitment gate has no cleanup arc**

When a UoW hits hard cap and is surfaced to Dan, nothing is cleaned up automatically. Artifacts remain, executor context is held, the garden is not updated with a failure trace. The UoW is flagged but not committed. mito-modeling.md Section 5.1 explicitly names this as the highest-leverage missing piece: "once threshold is crossed, the commitment is rapid, irreversible, and resource-recovering." Without the cleanup arc, hard cap is a pause, not a gate. The biological analog: flagging the damaged mitochondrion but leaving it in the network.

**5. Garden caretaker interaction with in-flight UoWs**

The sprint report documents that both S2-A and S2-B were expired by the garden caretaker because their source GitHub issues were closed during the sprint — before PRs were opened. The caretaker correctly identifies that the source issue is closed, but does not check whether the UoW is currently `active` or `ready-for-executor`. The fix (item 5 in post-sprint2 list) is in progress, but the design question is not just "add in-flight detection" — it is what the caretaker should do when the source issue is closed mid-execution. Options: (a) apply a grace period (30 min after active/ready-for-executor), (b) defer expiry until after executor returns, (c) notify Dan and require explicit decision. Option (c) is more consistent with the mitochondrial model's escalation-under-ambiguity principle.

**6. Registry path inconsistency**

The sprint report notes: "the `wos-registry.db` file at `~/lobster-workspace/data/wos-registry.db` is empty (0 rows). The active production registry is at `~/lobster-workspace/orchestration/registry.db`." This means the code has two plausible registry paths and uses the non-obvious one. Any code or agent that constructs the path naively (LOBSTER_WORKSPACE/data/wos-registry.db) will operate on an empty registry without error. This is a silent divergence bug.

**7. Observability metrics (PR #674) over an unstable pipeline**

The oracle's existing decisions.md entry for PR #674 captures this accurately: "the numbers are not yet trustworthy" because the audit events they aggregate include race conditions, orphan misclassifications, and false-complete records. `convergence_rate`, `diagnostic_accuracy`, and `execution_fidelity` computed over Sprint 2's audit log include events generated by three structural bugs. The metrics layer is correct but the denominator is not meaningful yet.

**8. Missing invariants the design assumes but code does not enforce**

- The design assumes vision_ref is populated at germination. The code does not enforce a non-null vision_ref before `proposed` → `pending` transition.
- The design assumes success_criteria is set at germination and never changes. The code does not enforce immutability of success_criteria after creation.
- The design assumes the philosophical register always surfaces to Dan (never machine-closes). The `_register_completion_policy` function implements this correctly for `philosophical` register, but there is no validation that a UoW cannot have register=philosophical set incorrectly at germination and then be machine-closed. The classification can be wrong; the policy applies to the (possibly wrong) classification.

---

## New Issues to File

### Issue A: Commitment gate missing cleanup arc — hard cap is a pause, not a gate

**Title:** `[wos] Hard cap surfaces UoW but does not trigger cleanup arc (commitment is not irreversible)`

**Problem statement:**
When a UoW hits `_HARD_CAP_CYCLES` (currently 5), the Steward surfaces it to Dan via `blocked` state. No cleanup occurs: artifacts remain, executor context is held, the garden is not updated with a failure trace. The UoW can be manually reset (decide-retry) and re-attempt immediately. This means hard cap is functionally a pause mechanism, not a commitment gate. Per `mito-modeling.md` Section 5.1, the biological analog (PINK1/Parkin amplification) makes commitment irreversible once threshold is crossed and includes resource recovery (cleanup arc). Without the cleanup arc, failed UoWs leave executor resource footprint and garden state corruption.

**Proposed fix:**
When a UoW transitions to `blocked` via hard cap, trigger a structured cleanup arc:
1. Archive the UoW's artifacts (move to `orchestration/artifacts/archived/<uow_id>/`)
2. Write a failure trace to the garden: `orchestration/failure-traces/<uow_id>.json` with `reason`, `final_return_reason`, `cycle_count_lifetime`, `timestamp`
3. Close executor context (currently done ad-hoc; make it explicit in the hard-cap path)
4. Set `close_reason = "hard_cap_cleanup"` and `closed_at` so the caretaker does not re-open
5. Optionally: open a new GitHub comment on the source issue noting the failure trace

This makes hard cap an actual commitment gate that recovers resources and documents failure for future germination decisions.

---

### Issue B: steward_cycles reset on decide-retry enables indefinite retry without lifetime tracking

**Title:** `[wos] steward_cycles resets to 0 on decide-retry — hard cap provides no cumulative protection`

**Problem statement:**
The `steward_cycles` field is reset to 0 on each `decide_retry` operation. S2-A ran approximately 20+ diagnosis cycles across 4 decide-retry rounds but shows `steward_cycles = 1` in the registry at closure. The hard cap (5 cycles) is therefore per-attempt, not per-UoW-lifetime. A UoW that consistently hits the cap can be manually reset indefinitely. This is the failure the post-sprint2 fix list item #4 is addressing (TBD issue number), but the fix should also add a lifetime cycle counter field to the registry schema.

**Proposed fix:**
1. Add `steward_cycles_lifetime` (INTEGER, default 0) to the UoW registry schema
2. `steward_cycles_lifetime` is never reset — it accumulates across all decide-retry rounds
3. Surface `steward_cycles_lifetime` in the steward log and audit entries
4. Consider adding a lifetime hard cap (e.g., 20 cycles across all rounds) that cannot be manually reset — only Dan's explicit override can clear it. This makes the commitment gate binding.
5. The sprint report's audit trail discrepancy ("20+ cycles for S2-A, 1 in registry") becomes visible if this field exists.

---

### Issue C: Corrective trace (trace.json) is not enforced as mandatory temporal gate before re-prescription

**Title:** `[wos] Corrective trace absence is logged but not blocking — removes mandatory temporal spacing`

**Problem statement:**
Per `mito-modeling.md` Section 5.3 and `governor-timing-structure.md` Isomorphism 4, the corrective trace (`trace.json`) is designed as a mandatory temporal gate: the Steward cannot prescribe again until the prior executor's trace.json is written. Currently, the cycle trace (`.cycles.jsonl` added in S2-A, PR #672) records what happened, but trace.json absence is not enforced as a blocking condition in `_process_uow` or `_assess_completion`. The Steward can re-prescribe immediately after an executor dispatch without waiting for trace feedback. Per the biological analog, removing the cristae junction removes mandatory delay between action and next prescription — the consequence is rapid re-prescription that does not incorporate what the last execution learned.

**Proposed fix:**
In `_process_uow`, after diagnosis determines that re-prescription is needed, check whether the prior executor's trace.json is present. If not, return a `WaitForTrace` outcome rather than prescribing immediately. On the next Steward heartbeat, diagnose again — if trace.json is now present, proceed with trace-informed prescription; if still absent after a configurable window (e.g., 1 heartbeat interval = 3 min), proceed with prescription but log the trace absence. This creates one mandatory cycle of dwell time between execution and re-dispatch.

---

### Issue D: Asymmetric governor — system has no backlog-starvation or backlog-toxicity alert

**Title:** `[wos] Observation loop missing: no alert on backlog starvation (zero queue) or toxicity (full queue)`

**Problem statement:**
The current WOS observation layer watches executor throughput but does not model the asymmetric governor described in `mito-modeling.md` Section 2, Row 4. There are two distinct failure modes:
- **Toxicity (full queue):** backlog growing beyond X UoWs for Y consecutive cycles — executor under-capacity, forcing exceeds dissipation rate. Currently, no automated signal.
- **Starvation (empty queue):** backlog empty for Z consecutive cycles — the cultivator/sweeper has stopped proposing work. Queue depth zero is not rest; it is death of throughput. Currently, no automated signal.

Both are failure modes. The current architecture treats zero queue as neutral (nothing to do). Per the mitochondrial model, extended zero queue is a starvation signal that should trigger an observation — "has the germinator stopped proposing? Has the cultivator stalled?" — just as a full queue signals executor under-capacity.

**Proposed fix:**
Add a scheduled observation pass (Type C cron-direct or Type A LLM job) that:
1. Queries `uow_registry` for count of `ready-for-steward` + `ready-for-executor` + `active` UoWs
2. Tracks this count over rolling N-cycle window (stored in `data/queue-depth-history.jsonl`)
3. Emits a `write_task_output` observation (not alert) when:
   - Queue depth has been 0 for > 6 consecutive hours
   - Queue depth has grown > 10 UoWs for > 3 consecutive cycles
4. Dan reviews during engagement windows; system does not act autonomously on the signal

---

### Issue E: Registry path inconsistency — two plausible paths, one empty

**Title:** `[wos] Two registry paths exist; active registry at orchestration/registry.db, default path at data/wos-registry.db — silent empty-registry risk`

**Problem statement:**
The sprint report documents: "`wos-registry.db` at `~/lobster-workspace/data/wos-registry.db` is empty (0 rows). The active production registry is at `~/lobster-workspace/orchestration/registry.db`." Any code or agent that constructs the DB path naively (`LOBSTER_WORKSPACE/data/wos-registry.db`) will operate on an empty registry without error — queries return zero rows, which is a valid result, not an exception. This is a silent divergence bug. `executor-heartbeat.py` uses `workspace / "orchestration" / "registry.db"` (correct). But the legacy path at `data/wos-registry.db` exists and is empty, not removed, creating a latent confusion risk.

**Proposed fix:**
1. Remove `~/lobster-workspace/data/wos-registry.db` (the empty legacy file) or write a marker JSON file explaining it is deprecated
2. Audit all code paths that construct the registry DB path and ensure they all use the canonical `workspace / "orchestration" / "registry.db"` path
3. Add a startup check in steward-heartbeat.py and executor-heartbeat.py that emits a warning if both paths exist and the legacy path is non-empty

---

## What the 5 In-Flight Fixes Do and Don't Address

The post-sprint2 fix list (#669, #670, #671, cycle counter, caretaker) addresses execution correctness bugs — things that cause the pipeline to fail. They do not address:

- Commitment gate cleanup arc (Issue A above)
- Lifetime cycle tracking (Issue B above — partially addressed by item 4, but without schema change)
- Corrective trace as mandatory temporal gate (Issue C above)
- Asymmetric governor observation loop (Issue D above)
- Registry path inconsistency (Issue E above)

The distinction is important: the in-flight fixes make the pipeline complete UoWs correctly. The new issues above make the pipeline govern itself in the mitochondrial model sense — restraint, temporal spacing, asymmetric alerting, irreversible commitment.

---

## Closed / Not Concerns

**The Steward/Executor boundary is correct.** The design correctly separates diagnosis+prescription (Steward) from execution (Executor). The Steward is the sole closure authority. The Executor cannot declare done. This matches governor-timing-structure.md Isomorphism 1 exactly.

**The hard cap threshold is correctly sized.** 5 cycles before surfacing to Dan is a reasonable threshold. The concern is not the number but the lack of cleanup arc and lifetime tracking (Issues A and B).

**The LLM prescription path is mandatory.** All three Sprint 2 UoWs used `prescription_path: "llm"` with no fallback. This is correct per the logged design decision (Dan's direct statement: "Prescription should never be deterministic ever"). Confirmed by learnings.md 2026-04-06 pattern on Encoded Orientation.

**The primary event-driven dispatch path is correctly prioritized.** The executor-heartbeat is a recovery net, not the primary path. The primary path (inbox dispatch on `ready-for-executor` transition) is the correct architecture. The staleness gate fix (PR #667) correctly distinguishes fresh UoWs (dispatch immediately) from previously-orphaned UoWs (apply staleness window). This is the right design.

**The register-completion policy (philosophical always surfaces, human-judgment requires Dan) is correctly implemented** in `_assess_completion`. The policy implementation matches the V3 design intent.

---

## Premise-Level Observations

**P1: The system is adding execution capability before installing governing structures.**
Sprint 2's work (cycle trace logging, prescription format, observability metrics, and the 5 in-flight fixes) all serve the execution layer. The governing structures named by the mitochondrial model — commitment gate with cleanup arc, asymmetric governor alerts, mandatory temporal spacing, lifetime commitment tracking — remain unbuilt. The mitochondrial model explicitly says: "production is downstream of the gate, not the reason for the gate" (governor-timing-structure.md). Building execution capability before the governing structures means the system will keep needing manual rescue operations at each sprint.

**P2: The corrective trace mechanism exists but does not yet close the feedback loop.**
The cycle trace (S2-A, PR #672) records what happened. The corrective trace mechanism in wos-v3-convergence.md is designed to inject trace content into the next prescription. Without the mandatory temporal gate (Issue C), the trace is recorded but not used as a structural input to the next prescription. The feedback loop is observed but not closed.

**P3: Vision Object integration remains the stated priority but is unaddressed by Sprint 2.**
vision.yaml active_project.phase_intent explicitly names three completion criteria: Registry live and populated by sweeper, UoWs carry vision_ref, morning briefing staleness warnings. Sprint 2 addressed none of these. This is not a criticism of Sprint 2 specifically — these are pre-existing gaps — but the audit notes that the pattern of work (execution throughput improvements) is not advancing the stated phase completion criteria.
