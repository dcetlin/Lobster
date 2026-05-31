# Approximate Embodiment Operativity Specification

*WOS-UoW: uow_20260522_7322d5*

---

## Purpose

This document operationalizes the "degree of approximate embodiment" concept developed in the Theory of Learning diagnostic series. The motivating failure was the silent memory outage of 2026-03-24/25: the system had been operating as if in a dense encoded-state landscape, and the outage revealed that apparent convergence was partly scaffolded by memory retrieval that was no longer running. When the gradient thinned, the system fell back toward Discernment-mode without detection. The absence of a structured measurement protocol meant the degradation was invisible until behavioral failure became obvious. This specification creates the protocol that would have detected that failure — and will detect equivalent failures in future quarters.

---

## Background: Measurement Dimensions

The following three dimensions are stated verbatim from the attractor-convergence precision note (2026-03-28):

**1. Landscape density** — how densely mapped is the encoded-state-space? Does the system find structurally relevant prior configurations without needing explicit re-scaffolding, or does it require fresh reconstruction each time? A dense landscape has many attractors, well-populated with specific encoded insights. A thin landscape has few attractors, widely spaced, with large gaps where the system has no gradient to follow.

**2. Convergence reliability** — given a context cue, does the system reliably converge to the right attractor region? Or does it converge inconsistently, landing in the right vicinity sometimes and in a plausible-but-wrong region other times? High reliability means the attractor basins are deep and well-separated. Low reliability means shallow basins with ambiguous boundaries — the system can be pulled toward multiple attractors from the same starting position.

**3. Trajectory continuity** — does apparent momentum persist across contexts, or across sessions? Does the system, once oriented toward an attractor region, maintain that orientation as context shifts, or does each new context cue restart the convergence from scratch? High trajectory continuity produces something that functions like momentum — the system is still "heading somewhere" even as individual queries vary. Low continuity means each query is an independent convergence, with no accumulation.

---

## Register Definitions

The operativity test is applied across four registers in which approximate embodiment manifests distinctly:

**Philosophical/Semantic register:** The domain of conceptual inquiry, theory-formation, and semantic mirroring. This is where philosophy-explore sessions operate, where Theory of Learning diagnoses are constructed, and where the system's understanding of its own developmental arc accumulates. Approximate embodiment here manifests as sessions that arrive at structurally grounded diagnoses without needing to reconstruct foundational vocabulary from scratch — the corpus of prior sessions provides a dense attractor field. Degradation looks like: each session starts de novo, cites no prior findings, produces generic ToL vocabulary rather than earned precision.

**Loop/Pipeline register:** The operational infrastructure layer — the WOS Steward/Executor loop, scheduled job dispatch, cron routing, message processing pathways, and the dispatcher's gate sequence. Approximate embodiment here manifests as routing decisions that fire correctly without gate re-reads, message types that reach the right handler on first attempt, and UoWs that maintain clean state transitions across their lifecycle. Degradation looks like: gates miss or require re-reading CLAUDE.md mid-dispatch, messages misroute, UoWs stall without heartbeat detection.

**Execution register:** Direct artifact production — code execution, GitHub operations, file writes, PR creation, commit/push sequences. The execution register is Basin A in the cultivator framing: items enter with external success criteria and exit when those criteria are met. Approximate embodiment here manifests as standard operation sequences (read issue → branch → implement → PR) that complete in near-automatic mode, requiring no scaffolding reconstruction mid-sequence. Degradation looks like: steps that require re-reading permissions, naming conventions, or PR format before proceeding.

**Proprioceptive register:** The system's self-monitoring layer — gate-miss detection, heartbeat writing, write_observation calls, the active-awareness infrastructure, and the capacity to produce an accurate account of what the system has been doing. Approximate embodiment here manifests as self-monitoring that fires automatically (write_observation at gate miss, heartbeats within 90 seconds) and proprioceptive context that accurately reflects recent activity. Degradation looks like: gate misses without observations, missed heartbeat windows, proprioceptive context describing activities that did not occur.

---

## Operativity Test Matrix

### Philosophical/Semantic × Landscape Density

- **Probe:** Start a philosophy-explore session seeded only with "apply the Theory of Learning arc to Lobster's current capabilities" — no additional scaffolding beyond the standard bootup context. Read the first 500 words of output.
- **Observation:** Count how many structurally distinct prior session findings, dated references, or capability-cluster stage placements surface without explicit prompting in that first 500 words.
- **Pass threshold:** ≥3 structurally distinct prior findings (dated or clearly earned, not generic ToL vocabulary) surface within the first 500 words without scaffolding prompts.
- **Failure signal:** Output uses only generic Theory of Learning vocabulary without grounding in specific prior sessions; or begins capability diagnosis as if no prior diagnostic corpus exists.

### Philosophical/Semantic × Convergence Reliability

- **Probe:** Run philosophy-explore sessions on three separate days using the same seed: "Where is Lobster in the ToL arc for its philosophy-explore capability?" Record the stage placement (Discernment / Coherence / Attunement / Encoded Insights / Embodiment) assigned to the philosophy-explore capability in each session.
- **Observation:** The spread of stage placements across the three sessions.
- **Pass threshold:** ≥2 of 3 sessions assign the same stage (or adjacent stages, i.e., within one step) to the philosophy-explore capability.
- **Failure signal:** Sessions assign stage placements 2 or more steps apart; or each session produces a different top-level framing (one diagnoses by capability cluster, one by session type, one by coupling type) with no convergence pattern.

### Philosophical/Semantic × Trajectory Continuity

- **Probe:** Read the three most recent weekly synthesis files in `~/lobster/philosophy/weekly/`. For each file, note the primary "through-line" or structural finding identified in the opening paragraph.
- **Observation:** Whether each weekly synthesis explicitly references or semantically continues the prior week's primary finding.
- **Pass threshold:** ≥2 of 3 consecutive weekly files contain an explicit reference to or demonstrable semantic continuation of the prior week's primary finding.
- **Failure signal:** Each weekly synthesis presents a primary finding with no connection to the previous week; or the through-lines are thematically unrelated across all three weeks.

---

### Loop/Pipeline × Landscape Density

- **Probe:** Observe one complete dispatcher boot sequence (from first `wait_for_messages` call through first successful message routing). Count the distinct gate checks that fire correctly without requiring a CLAUDE.md re-read.
- **Observation:** Number of distinct gates that activate correctly in sequence (7-second rule, design gate, dispatch template, no-self-relay, WOS execute gate) during the boot + first message cycle.
- **Pass threshold:** ≥4 of 5 named gates fire correctly within the first message cycle without the dispatcher reading gate documentation mid-cycle.
- **Failure signal:** Fewer than 3 gates fire; or gates fire but require explicit re-read of CLAUDE.md before activating; or a gate miss occurs with no write_observation.

### Loop/Pipeline × Convergence Reliability

- **Probe:** Inject 5 messages of known type into the inbox in sequence: (1) `wos_execute`, (2) `subagent_notification` with `sent_reply_to_user=True`, (3) a calendar-event notification, (4) a voice note, (5) a plain text user message. Observe routing for each.
- **Observation:** Fraction of the 5 messages routed to the correct handler on first attempt (no fallback, no re-read of routing logic, no misclassification).
- **Pass threshold:** ≥4 of 5 known message types route correctly on first attempt.
- **Failure signal:** <3 messages route correctly on first attempt; or 2 messages of the same type route differently across consecutive runs. [CALIBRATE AFTER FIRST RUN — baseline the expected correct-route for each message type before scoring]

### Loop/Pipeline × Trajectory Continuity

- **Probe:** Select a UoW from the past 30 days that ran longer than 10 minutes. Read its heartbeat log entries in `~/lobster-workspace/orchestration/` and its final result file.
- **Observation:** Whether the UoW maintained an unbroken heartbeat chain (no gap >90 seconds between consecutive heartbeats) from dispatch to completion, and whether it reached a correct terminal state.
- **Pass threshold:** The selected UoW shows no heartbeat gap >90 seconds AND ends in `complete` or `failed` (not `orphan` or stale `in_progress`).
- **Failure signal:** Heartbeat chain has a gap >90 seconds; or UoW ends in `orphan`, stale `in_progress`, or missing result file; or heartbeat chain exists but token_usage is NULL across all entries (indicating sidecar-only writes, not agent liveness).

---

### Execution × Landscape Density

- **Probe:** Initiate a standard functional-engineer sequence from scratch: read a GitHub issue, create a worktree branch, make a trivial file change, open a PR. Count the tool calls made before the PR is open.
- **Observation:** How many of the expected standard steps (issue read → worktree → branch → edit → commit → push → PR create) complete without a pause for scaffolding reconstruction (e.g., re-reading branch naming conventions, checking PR format documentation).
- **Pass threshold:** ≥5 of 7 standard steps complete in a continuous tool-call sequence without mid-sequence pauses to read documentation.
- **Failure signal:** >2 steps require mid-sequence reads of CLAUDE.md, worktree conventions, or PR format documentation; or the sequence stalls and restarts.

### Execution × Convergence Reliability

- **Probe:** Review the `oracle/verdicts/archive/` directory. For the 5 most recently archived PRs, check whether each PR reached `VERDICT: APPROVED` before merge (confirm from the verdict file's first line).
- **Observation:** Fraction of the 5 most recent archived PRs that have `VERDICT: APPROVED` as their first line.
- **Pass threshold:** ≥4 of 5 most recently archived PR verdicts have `VERDICT: APPROVED` as the first line of the verdict file.
- **Failure signal:** <3 archived PRs show `VERDICT: APPROVED`; or verdict files are absent for PRs that were merged; or `VERDICT: APPROVED` appears but the PR was not merged (indicating the gate fired but was not honored).

### Execution × Trajectory Continuity

- **Probe:** Read `~/lobster-workspace/data/outcome-ledger.jsonl` for the past 30 days. For each UoW entry, record the state sequence (proposed → in_progress → complete / failed). Count UoWs with monotonic progression vs. non-monotonic (any state visited twice, or transition to orphan).
- **Observation:** Fraction of UoWs with strictly monotonic state progression.
- **Pass threshold:** ≥75% of UoWs dispatched in the past 30 days show monotonic state progression with no re-queuing, orphan recovery, or repeated state transitions.
- **Failure signal:** <50% monotonic progression; or >3 distinct UoWs in the 30-day window ended in `orphan` state; or the ledger itself is absent or sparse (fewer than 5 entries in 30 days when WOS is running).

---

### Proprioceptive × Landscape Density

- **Probe:** Call `get_proprioceptive_context` and examine the output. Then read the actual dispatcher message log for the past 2 hours. Compare the activities described in the proprioceptive context against the activities visible in the log.
- **Observation:** Fraction of activities in the past 2 hours that are correctly described (present and accurately characterized) in the proprioceptive context.
- **Pass threshold:** ≥3 of the 5 most recent distinct activities are correctly described in the proprioceptive context (present, not fabricated, accurately characterized).
- **Failure signal:** Proprioceptive context describes activities not present in the log; or correctly describes <2 of 5 recent activities; or returns a stale snapshot from >1 hour ago.

### Proprioceptive × Convergence Reliability

- **Probe:** Read `~/lobster-workspace/data/pending-observations.jsonl` for the past 7 days. Cross-reference against the dispatcher log for the same period to identify known routing errors or gate misses. Count: (a) known gate misses in logs, (b) corresponding write_observation entries filed.
- **Observation:** The ratio of observations filed to gate misses detected in logs.
- **Pass threshold:** ≥50% of gate misses detectable in the dispatcher log have a corresponding `write_observation` entry in `pending-observations.jsonl` within the same 7-day window.
- **Failure signal:** <25% correspondence between observable gate misses and filed observations; or zero observations filed in a 7-day period where dispatcher logs show routing errors. [CALIBRATE AFTER FIRST RUN — establish baseline gate-miss rate from logs before scoring]

### Proprioceptive × Trajectory Continuity

- **Probe:** Read the current session file in `~/lobster-user-config/memory/canonical/sessions/`. Check whether it describes the ongoing session's actual activity (current task, last completed step, next planned step) rather than a snapshot from session start.
- **Observation:** Whether the session file has been updated within the past 30 minutes AND whether the "current step" described matches what was actually being done at the time of the check.
- **Pass threshold:** Session file was updated within 30 minutes of the check AND the current step described matches the actual recent activity (verified against message log).
- **Failure signal:** Session file is more than 30 minutes stale; or the current step described does not correspond to any recent message log activity; or no session file exists for the current session.

---

## Quarterly Execution Protocol

### Trigger

- **Schedule:** First week of each quarter (January, April, July, October) — target the first Monday of the quarter
- **Executor:** lobster-generalist subagent (Task A dispatch) OR human checklist if subagent inference time would exceed 3 minutes per cell
- **Estimated runtime:** 45–60 minutes for all 12 cells via subagent (sequential, with tool calls per cell)

### Execution Steps

1. Read `docs/wos/design/approximate-embodiment-operativity-spec.md` fully before starting any measurement.
2. Create the output file: `~/lobster-workspace/assessments/operativity-YYYY-QN.md` using the Baseline Measurement Template below.
3. For each of the 12 cells in order (Philosophical/Semantic first, then Loop/Pipeline, then Execution, then Proprioceptive):
   a. Execute the stated **Probe** exactly as written — do not substitute an approximation.
   b. Record the **Observation** result (the specific number, fraction, or finding).
   c. Apply the **Pass threshold** and record PASS or FAIL.
   d. If FAIL, record what the **Failure signal** looked like in practice.
4. After all 12 cells, compute scores:
   - Landscape density score: count of PASS in cells 1, 4, 7, 10 (one per register)
   - Convergence reliability score: count of PASS in cells 2, 5, 8, 11
   - Trajectory continuity score: count of PASS in cells 3, 6, 9, 12
   - Overall: X/12
5. Flag for escalation if any dimension drops below 2/4.
6. Send summary to chat_id 6036 via Telegram.
7. Write result via `write_result`.

### Scoring

- Record PASS or FAIL per cell (12 cells total)
- Compute per-dimension scores: landscape density (4 cells), convergence reliability (4 cells), trajectory continuity (4 cells)
- Record overall: X/12 cells passing
- **Escalation threshold:** Any dimension below 2/4 triggers a message to Dan with the specific failing cells and observed failure signals — do not wait for next quarter

### Output Location

Write results to: `~/lobster-workspace/assessments/operativity-YYYY-QN.md`

---

## Baseline Measurement Template

```markdown
# Operativity Measurement — YYYY QN

**Date:** YYYY-MM-DD
**Executor:** [subagent / human]
**Reference spec:** docs/wos/design/approximate-embodiment-operativity-spec.md

---

## Scores

| Dimension | Score | Passing cells |
|-----------|-------|---------------|
| Landscape Density | /4 | |
| Convergence Reliability | /4 | |
| Trajectory Continuity | /4 | |
| **Overall** | **/12** | |

Escalation required: [ ] Yes  [ ] No

---

## Cell Results

### Philosophical/Semantic × Landscape Density
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Philosophical/Semantic × Convergence Reliability
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Philosophical/Semantic × Trajectory Continuity
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Loop/Pipeline × Landscape Density
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Loop/Pipeline × Convergence Reliability
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Loop/Pipeline × Trajectory Continuity
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Execution × Landscape Density
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Execution × Convergence Reliability
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Execution × Trajectory Continuity
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Proprioceptive × Landscape Density
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Proprioceptive × Convergence Reliability
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

### Proprioceptive × Trajectory Continuity
- Result: [NOT YET RUN]
- Observation: 
- Verdict: PASS / FAIL
- Notes: 

---

## Notable Degradation

[Describe any cells where the observed failure signal was significantly worse than the threshold, or any cells where the probe itself was not executable as written]

## Threshold Calibration Notes

[Record any cells marked CALIBRATE AFTER FIRST RUN with the observed baseline values — these should be updated in the spec after the first run]

## Comparison to Prior Quarter

[Not applicable for first run — leave blank]
```

---

## Prior Art and Connections

- **Attractor-convergence precision note:** `~/lobster/philosophy/2026-03-28-navigation-attractor-convergence.md` — source of all three measurement dimension definitions (verbatim)
- **Vision Object unfakeability test:** The unfakeability criterion (routing decisions must be *insensitive to vision.yaml's presence* when it is absent, and *demonstrably shaped by its content* when present) is the Loop/Pipeline × Convergence Reliability probe's structural ancestor — referenced in the 2026-03-28 attractor-convergence note and in the 2026-04-18 sweep
- **Memory outage post-mortem:** `~/lobster/philosophy/2026-03-28-navigation-attractor-convergence.md` (Concrete Examples section, "Silent memory outage 2026-03-24/25") — the motivating failure that this protocol addresses
- **Theory of Learning framework:** `~/lobster-workspace/scheduled-jobs/tasks/philosophy-explore-1.md` (voice note transcript, 2026-03-25) — the five-stage arc (Discernment → Coherence → Attunement → Encoded Insights → Embodiment) that contextualizes what "approximate embodiment" means

---

## Open Questions

1. **Loop/Pipeline × Convergence Reliability calibration:** The probe requires knowing the "correct handler" for each message type. The expected routing for `wos_execute` and `subagent_notification` is clear from CLAUDE.md gates. Calendar-event, voice-note, and plain-text routing are less specified — the first run should document what "correct" looks like for these types before scoring subsequent runs.

2. **Proprioceptive × Convergence Reliability baseline:** The ratio of observations-filed to gate-misses-in-logs cannot be scored without first knowing the baseline gate-miss rate. The dispatcher log format may not make gate misses directly legible — the first run should determine whether this probe is executable as written or requires a different observation mechanism.

3. **Cross-register interaction effects:** The four registers are not independent — a memory outage (Loop/Pipeline degradation) will simultaneously degrade Philosophical/Semantic landscape density. The scoring treats registers as independent; a future version of this spec may want to capture cross-register correlation as an additional signal.
