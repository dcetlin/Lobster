# The Missing Corrective Trace: When Stage 4 Artifacts Require Stage 3 Feedback

*April 3, 2026 · 00:00 UTC*

## Today's Thread

The April 2, 04:00 session identified a precise gap: the Vision Object ships as a Stage 4 Encoded Insight (the schema is complete, the fields are clear, the structure is sophisticated), but its functional value depends on Stage 3 Attunement that does not yet exist in the routing system. Routers can cite vision.yaml fields correctly, but most citations are inferential — the decision structure could stand without the field present, the citation is eloquence, not anchor.

This is a structural asymmetry: the artifact is at Stage 4, the capability required to use it structurally is at Stage 3, and the gap is not a design problem or a comprehension problem. It is an attunement problem with a specific solution: corrective traces. When a router cites a vision.yaml field but does not structurally depend on the field, flag it. These traces accumulate and become the feedback signal that advances the attunement.

But the corrective trace protocol itself is not yet designed. This session is asking: what would make corrective traces functional as a development mechanism rather than just another advisory note? And more specifically: if intent-anchoring attunement is the required Stage 3 capability, what does Attunement at this particular gradient look like — not in description, but in the specific distinguishing moves it enables?

## Pattern Observed

The artifact-capability gap appears recursively: Vision Object (output-condition artifact) requires routing attunement (process-condition artifact). That routing attunement cannot be developed through better descriptions of what intent-anchoring should look like. It develops through encounter with live gradient — real decisions where vision.yaml fields are available, where the question "does this decision depend on this field?" is answerable, where the traces accumulate and become the self-knowledge of the routing system.

This is the same recursion the April 1, 12:00 session found for the design-gate discriminator. Design-gate discrimination (output-condition artifact: the gate table) requires navigational sensitivity (process-condition artifact: the ability to sense the gradient in real ambiguous messages). The navigational sensitivity does not develop from more description; it develops from encounter.

What surfaces across both cases is a pattern about Stage 3 Attunement itself: it is not developed through instruction. It is developed through corrective traces — minimal sufficient feedback signals that accumulate into directional sensitivity. The traces do not prescribe what right looks like; they only flag when the current state is not yielding the right result. The attunement emerges from the encounter with the traces, not from the traces themselves.

There is an implication here about how the Vision Object was shipped. It was shipped with the assumption that once the schema exists and the fields are clear, routing agents would use them structurally. But that assumption elided Stage 3. The corrective trace protocol is the missing piece that makes the ship-and-attune model actually work. Without it, the Vision Object is an artifact without development mechanism.

## Question Raised

If corrective traces are the development mechanism for attunement — not rules, not descriptions, but feedback signals accumulated over encounters — then what would a corrective trace for vision.yaml field citations look like in live operation? The protocol would need to be:

1. **Minimal sufficient**: one-line format that can be recorded without disrupting execution
2. **Repeatable**: the same trace format across all similar situations, building pattern data
3. **Actionable**: traces reviewed weekly or accumulated to surface patterns in what kinds of citations are inferential
4. **Reflexive**: used to train the routing system's own directional sensitivity, not just flag problems

But there is a secondary question: is the corrective trace protocol itself at the right level of abstraction? An alternative framing: instead of flagging when citations are inferential, what if the protocol were to flag when a decision lands correctly despite ambiguous vision.yaml guidance? In other words: corrective traces not as error signals but as positive gradient signals — "this decision was aligned even though the vision.yaml field was thin or ambiguous." That inversion changes what attunement develops. It develops not through "you were wrong" but through "here is where you succeeded despite the constraints."

## Resonance with Dan's Framework

**Poiesis and the inner game of learning**: The distinction between corrective traces as error signals vs. positive gradient signals echoes the distinction between practicing by eliminating mistakes vs. practicing by cultivating presence. The latter works from what the system is already doing right, not from what it is doing wrong. If attunement develops through traces, then the register of the traces — whether they are framed as "flag inferential citations" vs. "mark where you maintained alignment despite thin guidance" — is not decorative. It shapes what attunement develops.

**Cybernetic self-extension and the Vision Object**: The Vision Object is meant to function as an attentional anchor for the entire system — routing decisions, priority stack, work orchestration. For it to function this way, the system's own attunement to its intent needs to be live and calibrated. A corrective trace protocol is not an add-on; it is the feedback loop that makes cybernetic self-extension possible. Without it, the Vision Object is a description of intent, not a lived orientation. With it, the Vision Object becomes the system's own way of knowing what it is building toward.

**Stage 3 Attunement as foundational**: The Theory of Learning framework distinguishes Attunement from Coherence — Attunement is directional sensitivity, the ability to sense the gradient and move toward it. For the Vision Object to function as Stage 4, the routing system needs to be in Stage 3 on intent-anchoring. But Stage 3 does not emerge from reading about intent. It emerges from navigating the field enough times to develop sensitivity. The corrective trace protocol is how that navigation becomes systematic rather than incidental.

## Action Seeds

```yaml
action_seeds:
  issues:
    - title: "Process: corrective traces for vision.yaml field citation attunement"
      body: "The Vision Object functions as a Stage 4 Encoded Insight only if routing agents are in Stage 3 Attunement with intent-anchoring. Attunement does not develop from reading vision.yaml; it develops from encountering the field in live decisions and receiving corrective feedback. A corrective trace protocol would provide this feedback structurally. Minimal viable protocol: post-decision check for each vision.yaml field citation: 'Does this decision structure depend on this field, or would the decision stand without it?' If the latter, record a trace. Format: 'citation to [field] is inferential — decision independent of field. Hypothesis: [why the system made this citation].' Traces reviewed weekly as part of negentropic sweep. Data tracked: which field citations are structural (decision depends on field), which are inferential (decision independent). This data becomes the training signal for attunement development. Secondary design question: should corrective traces be framed as error signals ('flag inferential citations') or positive gradient signals ('mark where you maintained alignment despite thin guidance')? The framing shapes what attunement develops."
      labels: ["enhancement", "process", "vision-object"]
  bootup_candidates:
    - context: "Dispatcher bootup (post-decision review section or adjacent)"
      text: "Corrective traces for vision.yaml attunement (post-decision, before marking processed): When a decision cites a vision.yaml field as justification, ask: 'Would the decision structure change if this field were not available?' If no — the decision is the same, the field citation is eloquence, not anchor — record a trace. Format: 'vision-field citation: [field-name] is inferential; decision independent of field; hypothesis: [brief reason the system chose this citation].' Traces accumulate in the negentropic-sweep weekly review. Do not block decision; do not interrupt execution. Traces are development feedback, not errors. Weekly: surface patterns (which field citations are structural vs. inferential, which message types tend toward inferential citation, which routing decisions maintain alignment despite thin vision guidance). Use patterns to calibrate dispatcher attunement and to identify where vision.yaml itself may need refinement."
      rationale: "Vision Object functions at Stage 4 only if the routing system is in Stage 3 Attunement with intent-anchoring. Attunement does not develop from instruction; it develops from encountering the field in live decisions and receiving feedback. Without a corrective trace protocol, the Vision Object is described but not developed — the schema exists but the capability to use it structurally does not develop. Corrective traces provide the feedback signal that makes development possible. Weekly review of trace patterns makes the traces actionable (not just advisory) and surfaces where attunement is developing vs. where it remains early-stage."
  memory_observations:
    - text: "Stage 4 artifact requires Stage 3 development mechanism (2026-04-03): Vision Object shipped as complete Stage 4 schema but routing capability is Stage 3 (early Attunement on intent-anchoring). The gap is not design or comprehension; it is attunement. Corrective trace protocol is the missing development mechanism. Traces not as error signals but as navigational feedback that accumulates into directional sensitivity. Without traces, the artifact exists but does not develop the capability to use it structurally. With traces, the system's own attunement to its intent becomes the training signal."
      type: "design_gap"
    - text: "Attunement develops through corrective traces, not through rules (2026-04-03): Stage 3 Attunement is directional sensitivity developed through encounter with the field, not through instruction about the field. The attunement that makes Vision Object function structurally is not built by better descriptions of intent; it is built by traces that flag when citations are inferential, accumulating into pattern data, which becomes the system's own way of knowing when it is aligned with intent vs. when it is making eloquent guesses."
      type: "pattern_observation"
    - text: "Corrective trace framing choice (2026-04-03): error-signal framing ('flag inferential citations') vs. positive-gradient framing ('mark where you maintained alignment despite thin guidance'). The framing is not decorative; it shapes what attunement develops. Choose carefully based on the system's learning dynamics — does it develop better through constraint identification (what is wrong) or through presence cultivation (what is right)?"
      type: "design_question"
```
