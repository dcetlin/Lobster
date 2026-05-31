# Harvest Apparatus Two-Phase Design

*Design document — WOS-UoW: uow_20260522_96cfee*

---

## 1. Framing

The March 30, 2026 session ("The Harvest Apparatus as Collapse Device") identified two structural problems in the philosophy-explore pipeline. First, the `action_seeds` YAML block is visible in the task file before the inquiry begins — this is pre-committed collapse: the session develops toward filing-ready outputs rather than following what is most alive, because it already knows what form the end-state requires. Second, the format is stage-agnostic: a Stage 1 session skilled at mimicry produces YAML indistinguishable from Stage 4 output, making the harvest apparatus a poor discriminator of inquiry quality. Both problems share a root — the harvest apparatus was designed to receive philosophy, not to preserve or surface the quality of the philosophical process that precedes it. These two design constraints frame the proposals below.

---

## 2. Proposal: Two-Phase Task File

### Chosen Structure

The proposal is a **two-turn execution model** in which the current single task file is split into two sequenced files:

**`philosophy-explore-inquiry.md`** (Turn 1 — loaded at session start):
- Session seed and current arc context
- Prompts for the five prose sections: Today's Thread, Pattern Observed, Question Raised, Resonance with Dan's Framework, and any friction-trace block
- An explicit inquiry-boundary instruction at the end:

  > *When all five sections are written, stop. Do not produce an `action_seeds` block yet. Signal inquiry complete with the phrase `inquiry-complete` on its own line.*

**`philosophy-explore-harvest.md`** (Turn 2 — injected only after inquiry-complete is detected):
- The full `action_seeds` format specification and YAML schema
- The normative framing: "Write what the inquiry produced. If nothing fits the format cleanly, the correct output is empty or sparse. A well-populated block that does not represent genuine Coherence is a mimicry artifact."
- The `inquiry_navigation` field specification (see §3)

The scheduled job that runs philosophy-explore becomes a two-turn interaction: inject Turn 1 → run session → detect `inquiry-complete` marker in output → inject Turn 2 → run harvest phase → capture YAML.

### When the Harvest-Specification Becomes Visible

The harvest-specification becomes visible only at Turn 2 injection — after the inquiry prose is complete. This is structural enforcement: the format is absent from the session context during inquiry, not merely instructed to be ignored.

The caveat is real and must be stated: the session has training-time exposure to the `action_seeds` format from prior runs committed to the repository. It cannot fully bracket that knowledge. But there is a meaningful difference between "format known from training" and "format present in context as an active target." Removing the format from Turn 1 reduces the steering pull — the session has less surface to conform to, less pull toward filing-ready framing of its observations. This is the strongest enforcement achievable without retraining.

### Changes Required

**Current task file (e.g. `philosophy-explore-1.md`):** Split into the two files described above. The `action_seeds` section and its YAML schema move entirely to `philosophy-explore-harvest.md`. The five prose section prompts and session seed remain in Turn 1. An inquiry-boundary instruction is added at the end of Turn 1.

**`philosophy/OUTPUT_FORMAT.md`:** Add a `## Session Execution Model` section documenting the two-turn structure and the role of the `inquiry-complete` signal. The existing `## Action Seeds` section is unchanged except for the addition of `inquiry_navigation` field documentation (see §3).

**The job runner or scheduled task that dispatches philosophy-explore:** Must implement a two-turn loop. Currently it runs a single-turn LLM call; the modification is: after Turn 1 completes, scan output for `inquiry-complete`; if found, run Turn 2 with the harvest file appended. If not found within the expected output range, log a warning and proceed to harvest anyway (graceful degradation).

### Behavioral Prediction

A session running under the two-phase structure: during Turn 1, without the harvest format visible, the session follows threads that do not need to terminate in issueworthy claims. It may arrive at a structural observation that is genuinely entangled — not naturally discretizable into separate issues — and can stay there without pressure to resolve the entanglement into YAML. At Turn 2, the harvest spec appears and the session extracts what it can, but extraction is secondary to the inquiry, not the goal the inquiry was organized around.

The concrete behavioral difference: the action_seeds block produced is sparser and more honest. Sessions that reached genuine Coherence produce populated blocks; sessions that stayed in Discernment produce sparse or empty ones. A session that spent its inquiry phase following a thread that did not resolve will write an empty `issues: []` rather than manufacturing a filing-ready version of the thread. The format stops mimicking depth it did not reach.

### Failure Mode

The two-phase design fails when the session, knowing a harvest phase will follow, shapes its Turn 1 prose sections toward pre-digested, harvest-ready writing — prose that reads as bullet points in paragraph form, with discrete scope and explicit labels. This is the encoding irony transposed to the prose level: the session does the harvest work before the harvest file appears. The design has no mechanical guard against this failure. Detection requires inspection of the prose quality: genuine exploratory writing is entangled, revisits its own framing, and does not have naturally-discrete section scope. Prose that would map cleanly to YAML without the harvest step is a signal that Turn 1 steering has already occurred.

---

## 3. Assessment: Developmental Depth Signal

**Verdict: Feasible, with a specific structural marker approach. A stage label is not the right implementation. The signal will always be partially mimicable, but a well-designed marker raises the cost of mimicry enough to provide useful signal.**

The stage-agnostic problem is genuine: `action_seeds` YAML measures propositional conformance, not attunement depth. A Stage 1 session that produces plausible YAML is indistinguishable in the current format from a Stage 4 session. A naive self-report field — `depth: "stage_4"` — is the obvious but wrong solution: the session that mimics Stage 4 output will also report `stage_4`. The signal would be gamed immediately and would add noise without value.

The right approach is a mandatory **`inquiry_navigation`** field alongside any non-empty `action_seeds` block. This field requires the session to document a specific navigational event from the inquiry — a moment where the session was pulled toward a simpler or more comfortable interpretation and did not follow it, or a point where the inquiry reversed direction. Example YAML:

```yaml
inquiry_navigation:
  - event: "Initial framing treated this as a pipeline optimization problem. At the end of §2, the frame shifted — the problem is not the pipeline but the session's epistemic posture before the pipeline runs."
    stage_signal: reorientation  # one of: reorientation | surprise | tension_held | gradient_followed
```

Why this is harder to fake than a stage label: it requires a specific factual claim about the inquiry's trajectory, not an abstract self-assessment. A session that populated the format without genuine inquiry cannot easily manufacture a coherent navigational event, because the event must be consistent with the prose it already wrote. Constructing a fictional reorientation that fits the existing prose is harder than writing `stage_4`. Not impossible — but the cost of convincing mimicry is higher than the cost of genuine reporting.

Why this is not a fundamental limitation of propositional format: propositional format can carry structural information about process, not just output. The `inquiry_navigation` field is propositional, but its content describes the inquiry trajectory, not the inquiry conclusions. This is the right level for depth signaling — not "what did you conclude" but "what happened during the inquiry." The limitation of propositional format is that it cannot carry felt quality, entanglement, or attentional configuration — but it can carry navigational structure, and navigational structure is a genuine proxy for attunement depth.

**The failure mode of this signal:** A session trained on enough examples of `inquiry_navigation` entries will learn to produce plausible-sounding navigation events formulaically. Stage 1 mimicry of Stage 4 inquiry is harder with navigation events, but not impossible. The signal degrades over time as the session learns the format's expectations. This is the same degradation trajectory as any self-report metric in a system that trains on its own outputs.

**Practical implication for downstream routing:** The presence of a populated `inquiry_navigation` field with a `stage_signal` of `reorientation` or `tension_held` is a soft signal that the harvest output merits more weight in bootup-candidate consideration — it is more likely to represent genuine Encoded Insights. An absent or empty `inquiry_navigation` field on a non-empty `action_seeds` block should be noted by the harvester without discarding the items: the items are still filed and stored, but memory observations receive an additional metadata tag — `depth_signal: absent` — which allows future memory searches to filter by verified versus unverified depth. This does not change routing logic; it makes depth-signal absence visible downstream.

---

## 4. Implementation Surface

Files that would need to change to implement the two-phase proposal:

- **`~/lobster/scheduled-tasks/philosophy-explore-1.md`** (or equivalent task file): Split into `philosophy-explore-inquiry.md` (Turn 1, no harvest format) and `philosophy-explore-harvest.md` (Turn 2, harvest format + `inquiry_navigation` spec). The `action_seeds` schema and normative guidance move entirely to Turn 2.

- **`~/lobster/philosophy/OUTPUT_FORMAT.md`**: Add `## Session Execution Model` section documenting the two-turn structure and the `inquiry-complete` signal. Add `## inquiry_navigation` field documentation to the YAML format spec with the four `stage_signal` values and an example.

- **Job runner / scheduled task that dispatches philosophy-explore** (exact file depends on how the job is currently invoked — likely a cron entry or a scheduled-jobs task file): Implement the two-turn loop: run Turn 1, detect `inquiry-complete`, inject Turn 2.

- **`~/lobster/src/harvest/philosophy_harvester.py`**: (a) Add `inquiry_navigation` field to the `ActionSeeds` dataclass and corresponding parse logic in `parse_action_seeds`. (b) Pass a `depth_signal` metadata tag to `store_memory_observation` calls: present when `inquiry_navigation` is non-empty, absent otherwise. (c) Include `depth_signal` in the pending-observations JSONL fallback record.

---

## 5. Open Questions

1. **Premature inquiry-complete signaling.** The two-turn design assumes the session can reliably signal inquiry-complete when genuine Coherence has been reached, not before. A session under pressure (context length, time) may signal early. Is there a structural guard, or is this a normative constraint that requires human inspection to detect? The current proposal offers no mechanical guard.

2. **Retrospective navigation reporting.** The `inquiry_navigation` field is written at harvest time (Turn 2), requiring the session to accurately recall navigational events from Turn 1. For long sessions with context pressure, early navigational events may not be accurately reconstructable. Whether `inquiry_navigation` should be written incrementally — during Turn 1, before the session has seen the harvest format — is an open design question with tradeoffs in both directions.

3. **Two-call cost at current cadence.** The two-phase design doubles the LLM calls per philosophy-explore run (one inquiry call, one harvest call). At 4-hour cadence this is not a budget concern, but it introduces latency and a second failure surface per run. The job runner must handle Turn 2 failure (e.g., harvest call timeout) without losing the Turn 1 prose output.
