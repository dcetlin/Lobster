# Corrective Trace Loop Gain — Research Note

*Written: 2026-04-04*
*Context: WOS V3 spec review — cybernetics philosopher concern about unbounded loop gain in the corrective trace feedback mechanism*

---

## Problem Being Researched

The V3 corrective trace injects `prescription_delta` from `trace.json` as unstructured text into the LLM prescriber's context block. The philosopher's concern: this is a negative feedback loop with no bound on correction magnitude. Large rapid corrections produce oscillation. The `no_gate_improvement` stuck condition guards against runaway for iterative-convergent register (numeric gate score), but operational register has no equivalent guard.

---

## Q1. What do biological and engineering systems teach us about bounded correction magnitude?

### Engineering: PID integral windup

PID controllers exhibit a canonical pathology called integral windup: when a large setpoint change occurs, the integral term accumulates error faster than the actuator can respond. When the actuator catches up, the accumulated integral error overshoots the target. In severe cases this produces sustained oscillation. Standard solutions:

- **Clamping (conditional integration):** Turn off the integrator when the controller output saturates. This is the simplest magnitude bound — stop accumulating when you've already issued a maximum correction.
- **Back-calculation anti-windup:** Feed the difference between saturated and unsaturated output back to the integrator through a gain `Kb`. This unwinds the accumulator proportionally. The key property: the magnitude of the next correction is bounded by the size of the previous correction error, not by raw accumulated error.
- **Bounded integral limits:** Prevent the integral term from exceeding pre-determined bounds absolutely. Simpler but less elegant than back-calculation.

The key insight from PID: **the danger is accumulation without feedback on the accumulation itself.** If the system keeps accumulating corrections without a separate signal that tracks how much has already been corrected, it cannot know when to stop. Anti-windup works by introducing a second loop that monitors the correction channel itself.

### Biology: homeostatic overshoot and damping

Biological negative feedback systems share the same pathology. Even before phase shift reaches 180 degrees, stability becomes compromised — variables fluctuate around target rather than converging, because regulatory responses take time and can overshoot. The engineering concept of damping ratio applies directly: a critically damped system (ratio = 1.0) converges fastest without oscillating. Calcium homeostasis in mammals operates at a damping ratio slightly above 1.0 — slightly overdamped, prioritizing stability over speed.

**Biological mechanisms for bounded correction:**

1. **Proportional response:** Correction magnitude is proportional to deviation, not to duration. The hormone concentration response to blood glucose is proportional to departure from setpoint, not to time spent off-setpoint.
2. **Multiple parallel pathways with different time constants:** Fast and slow correction paths run in parallel. The fast path handles large acute deviations; the slow path handles persistent drift. This prevents a single fast-acting overcorrection from destabilizing the system.
3. **Feedforward (anticipatory) control:** Corrections are initiated before the deviation fully manifests, based on predicted trajectory. This reduces the magnitude of corrective response required at any single moment.
4. **Allostasis vs. homeostasis:** Not all biological regulation aims for a fixed setpoint. Allostasis (variation around a dynamically shifting target) is more stable than homeostasis (fixed setpoint) in variable environments — because it doesn't overreact to normal environmental variation.

**Key insight from biology:** Stable adaptive systems use multiple corrective pathways at different time scales, not a single unbounded correction channel. They also distinguish between routine variation (not requiring maximum correction) and genuine drift (requiring full response).

---

## Q2. What does ML/AI literature say about LLM prompt injection of prior outputs?

### Self-correction and amplification risk

The LLM self-correction literature (intrinsic self-correction — refining outputs via prompting without external feedback) shows that correction quality depends heavily on whether the feedback signal is grounded in an external verifiable state or is purely model-generated. When feedback is purely model-generated (the model critiquing its own output), evidence suggests corrections can amplify existing errors rather than reduce them — the model is over-confident about what it believes is wrong.

**The critical distinction:** External feedback (test suite results, gate scores, compilation output) grounds correction. Internal feedback (the model's own assessment of what was wrong, expressed as `prescription_delta`) is unverifiable. Unverifiable feedback injected into the next cycle can compound error if the initial assessment was wrong.

### Amplification in multi-agent systems

Research on multi-agent LLM architectures (Prompt Infection, agentic feedback loops) documents a related phenomenon: a single injected instruction can self-amplify across agent iterations if the system has no normalization mechanism. The infection grows because each cycle treats the injected content as authoritative context. In the WOS case, a `prescription_delta` that misdiagnoses the root cause of a failed execution could be injected into the next prescriber cycle as authoritative context, causing the prescriber to overfit to a wrong diagnosis.

### Output-refinement loop dynamics

Survey literature on LLM agent feedback mechanisms notes that output-refinement loops (prompting LLMs with their own previous outputs) lead to higher quality initially but can amplify undesired qualities after several cycles, particularly when the refinement signal is not normalized. The phrase "each cycle reflects increased optimization for measured objectives" describes the mechanism: the model increasingly optimizes for whatever the feedback signal emphasizes, even if the signal is partially wrong.

**Key insight from ML:** Ungrounded (model-generated) feedback injected without normalization can amplify errors. Grounded (external-state) feedback is safer. The V3 `prescription_delta` is ungrounded — it is the executor subagent's own assessment, not an external verification signal.

---

## Q3. What specific bounded-correction mechanisms should V3 consider?

The following mechanisms are ordered from simplest to implement to most structurally sound:

### 3a. Magnitude limit via truncation

Cap `prescription_delta` at a fixed character or token budget before injection (e.g., 300 tokens). This prevents a long, aggressive prescription delta from dominating the prescriber's context. Simple to implement; does not address content quality. Analogous to clamping in PID.

**Tradeoff:** Truncation is arbitrary and can cut a coherent correction mid-thought, producing a worse signal than no injection. Summarization is preferable if summarization capacity exists.

### 3b. Cycle-averaged injection (smoothing)

Instead of injecting the most recent `prescription_delta` only, inject a synthesis across the last N traces. This is the feedback-loop equivalent of a moving average — it smooths out single-cycle overcorrections. The Steward already reads N consecutive `trace_injection` entries from `steward_log` for the `no_gate_improvement` check; the same infrastructure supports reading the last 2-3 `prescription_delta` values and summarizing them.

**Tradeoff:** Averaging can dilute a genuine new signal. If the executor discovered something important on cycle 3 that wasn't present on cycles 1-2, the averaged injection will underweight it. The smoothing window needs to be short (2-3 cycles) to remain responsive.

### 3c. Confidence-weighted injection

Inject `prescription_delta` only if the execution outcome was in a regime that makes the delta credible. A crashed execution produces a `prescription_delta` of lower epistemic quality than a partial execution that reached step 4 of 6. The trace schema already has enough information to infer this: `execution_summary` and the exit path (complete, partial, blocked, exception) can weight injection confidence.

**Implementation sketch:** Define an injection confidence score based on exit path:
- `complete`: high confidence (full execution, good epistemic basis)
- `partial`: medium confidence
- `blocked`: medium confidence (executor knows what blocked it)
- `exception/crash`: low confidence (executor may have incomplete picture)

Suppress injection entirely when confidence is low, or apply greater truncation. This is analogous to the biological proportional response mechanism: correct proportionally to evidence quality, not uniformly.

### 3d. Explicit surprise threshold before injection

Only inject `prescription_delta` when `surprises` is non-empty. A `prescription_delta` that accompanies no surprises is likely incremental noise rather than a genuine correction signal. This gate is already partially implied by the spec (surprises trigger injection) but could be made explicit: no surprises, no delta injection.

**Tradeoff:** This may suppress useful optimization feedback from executions that went smoothly. The risk is accepting unnecessary drift in the prescription when no surprises are reported but incremental improvement is possible.

### 3e. Structural recommendation

The most robust bounded-correction approach combines 3b (short moving average) and 3c (confidence weighting). This mirrors the biological two-pathway model: fast correction on high-confidence signals, dampened correction on low-confidence signals, and averaging across recent cycles to prevent single-cycle overcorrection. This does not require new data — it uses the `steward_log` trace_injection history already specified.

---

## Q4. What guard should exist for operational register to detect runaway correction?

### Why the iterative-convergent guard doesn't transfer

The `no_gate_improvement` condition works because iterative-convergent has a numeric `gate_score` (0.0–1.0). Improvement is observable. For operational register, there is no equivalent signal — the executor reports complete or not, but the Steward has no numeric trajectory to track.

### Behavioral signals available without a numeric gate

The following signals are available from existing trace schema and steward_log for operational UoWs:

1. **Prescription delta entropy across cycles:** If `prescription_delta` across consecutive cycles is high-variance (each delta pointing in a different direction), the feedback loop is not converging — it is exploring without narrowing. This is detectable by semantic difference between consecutive deltas, though that requires an LLM comparison call which may be expensive.

2. **Prescription similarity (simpler):** If `prescription_delta` in cycle N is similar in content to cycle N-2, the system is oscillating — returning to the same correction it already tried. This is detectable with a simple token overlap check on the last 3 deltas stored in steward_log.

3. **Surprise recycling:** If the same surprise text (or semantically similar text) appears in traces across 3+ consecutive cycles, the executor is encountering the same unexpected condition repeatedly without resolution. This indicates the prescription delta is not addressing the actual blocker. Detectable via substring matching or simple hash of the surprise content.

4. **Steward cycle count without state change:** This is the existing `hard_cap` (steward_cycles >= 5). It is blunt but effective as a last-resort runaway guard.

### Recommended guard for operational register

**Prescription recycling detection:** Add a `_count_recycled_prescription_cycles(steward_log, n=3) -> int` function that checks whether `prescription_delta` content from the most recent N traces has a token overlap ratio above a threshold (e.g., 0.6) with any prior trace. If true for 3 consecutive cycles, set `stuck_condition = "prescription_recycling"` and surface to Dan.

This is structurally analogous to `no_gate_improvement` but without requiring a numeric gate: instead of tracking whether a score improves, it tracks whether the correction signal itself is converging or cycling. A feedback loop that keeps issuing the same prescription delta is not making progress, regardless of what the executor reports.

**Surface message for `prescription_recycling`:**
`"WOS: UoW {id} — prescription delta recycling detected after {N} cycles. The executor is recommending the same change repeatedly without resolution. Last delta: {delta[:200]}. Possible cause: blocker requires external intervention or success criteria are ambiguous."`

---

## Summary of Key Findings

1. **Engineering and biological literature agree:** Stable adaptive systems bound correction magnitude via a second-order monitor on the correction channel itself (anti-windup feedback, damping ratio, allostasis). A single unbounded correction channel with delay produces oscillation. The mechanism is not a harder limit on the primary signal — it is a feedback path that monitors accumulation.

2. **LLM-specific risk is amplification of ungrounded feedback.** `prescription_delta` is model-generated and unverified. If the executor misdiagnoses its own failure, that misdiagnosis is injected as authoritative context into the next prescriber cycle. Unlike a gate score (external, verifiable), the delta is not self-correcting. The risk grows with cycle count.

3. **Recommended bounded-correction mechanisms (in priority order):** (a) Confidence-weighted injection based on execution exit path — suppress or dampen low-confidence deltas; (b) Short moving average across last 2-3 traces rather than single-cycle injection; (c) Surprise threshold gate before injection.

4. **Recommended operational register guard:** `prescription_recycling` stuck condition — detect when consecutive `prescription_delta` values have high token overlap, indicating the feedback loop is cycling rather than converging. Structurally mirrors `no_gate_improvement` without requiring a numeric gate.

---

## References

- Integral windup / anti-windup: https://en.wikipedia.org/wiki/Integral_windup
- PID anti-windup schemes: https://www.scilab.org/pid-anti-windup-schemes
- Homeostasis and feedback loops: https://pmc.ncbi.nlm.nih.gov/articles/PMC4669363/
- Intrinsic self-correction in LLMs: https://arxiv.org/html/2505.11924
- LLM-to-LLM prompt injection in multi-agent systems: https://arxiv.org/html/2410.07283v1
- Survey on feedback mechanisms in LLM-based agents: https://www.ijcai.org/proceedings/2025/1175.pdf
- Runaway agent prevention: https://medium.com/@aiteacher/how-to-prevent-hallucinations-and-runaway-agents-in-agentic-ai-systems-bf2cfe281248
- Agentic AI architectures and feedback control: https://arxiv.org/html/2601.12560v1
