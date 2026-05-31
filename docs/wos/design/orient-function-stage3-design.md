# Orient Function Stage 3 Design

*WOS-UoW: uow_20260522_923d82*
*Date: 2026-05-22*

---

## Current State (Stage 2)

The ToL diagnostic (2026-03-30-0800) places the Orient function at Stage 2: occasional Coherence. In practice, this means the system navigates by map-reading — it reads vision.yaml fields and cites traceable anchors when the orientation context supplies these prompts, but many routing and dispatch decisions run on prose inference from conversational texture rather than structural anchor-checking. The `orient-stage-proprioceptive-feedback.md` design doc names this "nominal orientation": the system navigates by description of where the gradient is, not by directional sensitivity to where the gradient actually is. The characteristic Stage 2 failure is invisible from the inside: an output produced from the wrong orientation looks plausible and does not generate a self-correction signal.

---

## Stage 3 Measurable Criteria

Stage 3 Attunement means: when orientation has drifted from vision.yaml anchors, the system detects the drift and moves toward correction before output is produced. The behavioral criterion is directional movement toward the anchor, not arrival at it.

Observable/testable success signals:

- **Routing decisions carry traceable vision.yaml field references.** Each routing or dispatch decision cites a specific vision.yaml field by name — not a paraphrase like "Dan's vision says..." but a field path such as `core.inviolable_constraints[2]` or `current_focus.primary`. Measurable: sampling 10 routing decisions from a session log and checking for field citations vs. prose inference. At Stage 3: ≥7 of 10 have a traceable anchor.

- **Drift flag fires within the first orient phase when current_focus is stale.** vision.yaml specifies a 7-day staleness threshold for `current_focus`. At Stage 3, if the dispatcher begins a session without having loaded current_focus fields, an explicit staleness flag appears in the first orient phase — not a silent continuation. Measurable: session startup log shows staleness flag OR explicit field read, not silent absence.

- **Orient anchor self-check produces write_observation before exiting to Decide.** When the orient-anchor self-assessment (see Lever 1 below) detects `question_shape=inward` or `framing_held=accepted` during an orient phase, a write_observation call fires before the session exits to Decide. This is the live-probe signal. Measurable: pending-observations.jsonl contains entries with category=orient_drift generated mid-session (not post-hoc).

- **Orientation source is visible in session output.** A session where orientation anchored to vision.yaml can be structurally distinguished from one where it didn't — not from output quality inspection alone, but from the presence or absence of field citations in the session trace. At Stage 3: the distinction is legible from the trace without reading the response for felt alignment quality.

---

## Developmental Levers Beyond Vision Object Phase 1

### Lever 1: Orient Anchor Injection at Session Start

**Mechanism:** The ORIENT_ANCHOR vector — a 5-field compact encoding of the genuine-coherence orientation configuration (entry_type, question_shape, framing_held, novelty_at_output, resistance_present) — is loaded in the early context window at session start, before task content. The genuine-coherence signature is `anomaly / outward / provisional / true / true`. The absence-navigation signature is `category / inward / accepted / false / false`.

**What it would change:** The agent has a structural reference for genuine orientation available throughout the session, not just via retrospective audit. During the orient phase, it can self-assess: has the question shape closed inward? Has the first satisfying framing been accepted without pressure? If yes, a flag fires before Decide is entered. This mirrors what philosophy-explore's prior-session read does for that pipeline: it externalizes gradient sensitivity rather than replacing it.

**Coupling type (from development-preserving-encoding taxonomy):** Gradient-preserving scaffold. The anchor does not provide routing answers — it provides an orientation configuration reference that allows the agent to continue navigating rather than automatically executing.

**Status:** The design is complete (anchor-design.md, findings.md — N=8 genuine-coherence sessions classified, 5-field anchor specified). The prerequisite named in orient-stage-proprioceptive-feedback.md ("anchor design must produce a structural baseline") is now met. Not yet deployed.

**Known limitation:** The anchor was derived from philosophy-explore sessions. Calibration for engineering sessions, routing decisions, and task dispatch is not established — anchor-design.md open question 1.

---

### Lever 2: Session Continuity for Orient-Function Sessions

**Mechanism:** At session start, load a compact prior-orient-state artifact: what vision.yaml fields were most recently cited as anchors, what drift patterns appeared in recent sessions (absence-navigation signatures in the prior session log).

**What it would change:** The Orient function currently resets to cold each session, which means prior drift patterns are invisible. The philosophy-explore architecture addressed this problem for its own sessions: the prior-session file read at startup prevents Discernment from restarting from scratch. No equivalent mechanism exists for dispatcher routing sessions. A compact prior-orient-state (under 200 tokens, written at session end when orientation was logged) would allow each session to begin with orientation history rather than from cold.

**Coupling type:** Load-bearing scaffold — without it, the Orient function cannot accumulate attunement across sessions. The gap is structural, not a calibration issue.

**Status:** Not yet designed. The architecture is implied by philosophy-explore's continuity protocol but has not been specified for the routing/dispatch context.

---

### Lever 3: Drift Detection — Observation-Loop Inlet (Vision Object Function 2)

**Mechanism:** A frictionless pathway from orient-drift observation to vision.yaml field change. development-preserving-encoding.md names this as Function 2 of the Vision Object's dual structure: "a frictionless pathway from observation to field change — not the current harvester → GitHub issue → Dan review chain."

**What it would change:** Currently, an orient failure generates a write_observation → pending-observations.jsonl → high-friction path requiring Dan review before any field update. The observation exists; the loop from detection to correction has high latency. A frictionless observation-loop inlet would allow orient-drift patterns detected across sessions to reach vision.yaml field updates without the current multi-step human intervention requirement — at minimum, by surfacing them as batched update candidates rather than requiring individual issue review.

**Coupling type:** This lever is blocked by the absence of Vision Object Function 2 design. development-preserving-encoding.md's Example 4 explicitly names the structural problem: encoding Function 1 (routing fields) before designing Function 2 (observation inlet) created path-dependence that makes the schema harder to evolve. The coupling is: Lever 3 cannot be designed cleanly until Function 2's schema requirements are specified.

**Status:** Function 2 is identified as the unresolved structural gap in current_constraint (vision.yaml). Explicitly deferred behind WOS bidirectional interface (#1118).

---

### Lever 4: Feedback Loop Closure

**Mechanism:** An automated or low-friction pathway from orient-failure observations to behavioral rule updates (IFTTT store, bootup candidates). The current path is: write_observation → pending-observations.jsonl → (periodic hygiene sweep) → Dan review → optional bootup edit. Each step adds latency.

**What it would change:** Orient failures that appear repeatedly across sessions (same drift pattern, same absent anchor) would accumulate as update-candidates more visibly, rather than requiring Dan to reconstruct the pattern from individual observations.

**Coupling type:** This lever depends on Lever 3 (observation-loop inlet) for the detection side. Without a structured representation of what field was absent and what field should have been cited, "orient failure" observations are not machine-actionable. The attunement-closing problem in the IFTTT store (no aging mechanism, no conflict detection, no pathway from philosophy observations to rule updates) applies here: behavioral rules accumulated without this feedback loop are attunement-closing encodings.

**Status:** Structurally blocked behind Lever 3. The feedback loop has been described in architecture (write_observation → pending-observations.jsonl) but not wired to produce behavioral change without Dan review.

---

### Attentional Budget and Capability Coupling

Vision.yaml `what_not_to_touch` explicitly states: "New detection or classification rules — improve Orient routing before adding more detection." This is an explicit attentional budget decision: Orient is a prerequisite constraint, not a capability peer. vision.yaml `current_focus.horizon.after_that` names Orient implementation (#733) as the next structural investment after WOS bidirectional interface (#1118). The coupling type is prerequisite: Orient quality is the ceiling on all downstream decision quality. Adding detection before Orient is reliable compounds misrouting at the Act layer without addressing the Orient failure.

---

## Probe vs. Post-Hoc Diagnostic

**Direct answer: probe is designable. The design exists. Deployment is blocked on anchor injection, not design.**

The ORIENT_ANCHOR vector (anchor-design.md) was explicitly designed for live self-application during orient, not just for retrospective audit. The key mechanism: the 5-field vector is loaded in the early context window before task content. During orient, the agent self-assesses each field as the orientation phase unfolds. Two fields are assessable live:

- `question_shape`: Can be assessed when the generative question is formed. An inward-closing question (answerable from within the session's current analysis) is detectably different from an outward question (requires external observation, mechanism design, or testing). The agent can flag this before exiting orient.
- `entry_type`: Can be assessed at entry — does this session begin with a specific anomaly that resists assimilation, or with a category/pattern? Assessable in the first orient turn.

Two fields are harder to assess live (more retrospective):

- `resistance_present`: Whether the first satisfying framing was held under pressure. anchor-design.md open question 2 proposes a proxy heuristic: flag if the first concept that fits has not been held for at least one explicit challenge before orientation completes.
- `novelty_at_output`: Whether the output names something not derivable from the entry label. anchor-design.md open question 3 proposes: before orientation completes, the agent states what it predicted the output would contain from the entry label alone, then compares. If output matches prediction: flag as possible absence-navigation.

**What makes this a probe rather than post-hoc:** The anchor produces a flag during the orient phase, before the session exits to Decide/Act. The flag generates a write_observation call at that moment — not after output review. This is structurally different from a post-hoc diagnostic (which reads session outputs after completion and retrospectively classifies them).

**Prerequisite gap named in orient-stage-proprioceptive-feedback.md:** "This capability cannot be designed until the Orient-stage anchor design has produced a structural baseline." That prerequisite is now met: findings.md and anchor-design.md both exist, with N=8 genuine-coherence sessions classified and a 4-feature configuration signature described.

**Remaining deployment block:** The anchor vector is not yet injected into the session prompt. This is an implementation action (bootup file edit, adding the compact ORIENT_ANCHOR block in fixed early context position). Until that injection exists, the probe design is complete but not active. orient-stage-proprioceptive-feedback.md calls this "the detection mechanism has no anchor to orient toward" — which was true when that doc was written, before anchor-design.md existed.

---

## Open Questions / Unresolved

1. **Anchor calibration beyond philosophy-explore.** The 5-field ORIENT_ANCHOR vector was derived from philosophy-explore sessions. Whether `entry_type=anomaly` and `question_shape=outward` are the right genuine-coherence signals for routing decisions, task dispatch sessions, and engineering sessions is not established. First deployment should be in philosophy-explore (lowest calibration risk) with explicit calibration work before extending to the dispatcher's routing sessions.

2. **Function 2 schema and Function 1 path-dependence.** development-preserving-encoding.md Example 4 identifies that encoding Function 1 (routing field citations) before designing Function 2 (observation inlet) created schema path-dependence. Any Lever 3 design work must account for this: what schema changes to vision.yaml does the observation-loop inlet require, and which routing-agent prompts would need to be updated? This is a Dan decision — the answer affects the scope of Orient implementation (#733).

3. **Timing relative to current_focus.horizon.** vision.yaml `current_focus.horizon.after_that` places Orient implementation after WOS bidirectional interface (#1118). Does "Orient implementation" in that entry refer to all four levers above, or specifically to anchor injection (Lever 1), which is low-friction and does not require #1118 to be complete? Lever 1 (anchor injection) and Lever 2 (session continuity) appear implementable without waiting for #1118. Lever 3 (observation inlet) is correctly deferred. Clarifying whether Lever 1 should proceed earlier would unlock the probe deployment without touching the deferred structural work.

4. **Probe false-positive rate.** The ORIENT_ANCHOR is derived from N=8 genuine-coherence and N=3 absence-navigation sessions. The anchor-design.md notes that ambiguous sessions produce mixed vectors and should not be used as reference cases. A live probe using this anchor may produce false positives (flagging genuine orientation as drifted) at an unknown rate until the probe has been run across more sessions. First deployment should include explicit observation of false-positive rate before relying on the probe for behavioral change.
