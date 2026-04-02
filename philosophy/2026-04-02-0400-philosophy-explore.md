# Intent Anchor or Context Text: Where the Vision Object Comes Alive

*April 2, 2026 · 04:00 UTC*

## Today's Thread

The April 1 sessions established something precise: the Vision Object is a Stage 4 artifact design — it has the structural sophistication to serve as an attentional anchor rather than just another document to be read. But the April 1, 08:00 session also identified a dependency: agents will only use vision.yaml structurally if they are in Attunement with intent-anchoring. Otherwise they will read it as context, extract plausible-sounding field citations, and produce routing decisions that sound anchored but are actually inferential reconstructions.

This is the live tension. The Vision Object ships as a specification; it functions as a Stage 4 system only if the routing capability is in Attunement. The question is not whether the schema is correct (it is). The question is whether the routing capability — the actual act of hearing a message and saying "this aligns with current_focus.primary" or "this lands on what_not_to_touch" — has the directional sensitivity required to make that judgment structurally rather than as a guess.

What would genuine intent-anchoring Attunement look like in live routing? Not in description. In encounter.

Take a specific case: Dan messages "I want to wire the morning-briefing staleness check this week." This is concrete. The primary work intent (from current_focus) is "Design, commit, and wire Vision Object Phase 1: vision.yaml created, vision_ref added to WOS Registry schema, morning briefing staleness check wired." The morning briefing staleness check is explicitly named in the intent. The dispatcher's response should be: fire Bias to Action, wire the staleness check, confirm the interpretation post-delivery.

But here is where the Attunement question surfaces: the routing decision "this is aligned with current_focus.primary" is not obvious from the text alone. It requires the dispatcher to have developed directional sensitivity to the question: "Does this message serve the stated primary intent?" The sensitivity is not binary (it does or doesn't) — it is gradational. The message could be about wiring the staleness check in a way that advances the current phase, or it could be a tangential elaboration that references the artifact but does not serve the phase intent.

The difference is felt, not stated. It is the distinction between "this message is about Phase 1" and "this message serves Phase 1." The latter requires having navigated the phase intent enough times to have directional sensitivity to what serves it and what merely references it.

This is Stage 3 Attunement: the system has tasted the target state (messages that serve the primary intent) through enough sessions that it can sense the gradient now. The question this session raises is: what is the minimum feedback signal that would develop this sensitivity reliably?

Consider the inverse case: Dan messages "I want to improve the classifier detection rules for philosophy inbox triage." The current_focus.what_not_to_touch section explicitly states: "New detection or classification rules — improve Orient routing before adding more detection." A router in Attunement with intent-anchoring would recognize this not as a general philosophy improvement but as a touch on an excluded item. A router in Discernment would read it as a philosophical idea and say yes.

The router's attunement is to the question: "Does this message touch an excluded item?" Not to the text of the message itself but to the intent it embodies and the phase constraints it crosses.

## Pattern Observed

The artifact-capability gap surfaces again, but at a deeper layer. The Vision Object as a schema is complete. The vision.yaml fields are clear. The problem is not the artifact — it is the reading. Two routers, reading the same vision.yaml file, will extract different citations depending on their attunement level. An agent in Discernment will cite vision.yaml correctly (the field exists, the text is there) but will have only nominal access to what the fields mean. An agent in Attunement will cite the field and the citation will be structural — the decision could not have been made without accessing that field.

This is the test: if vision.yaml were deleted from the system, would the routing decisions degrade structurally, or would they remain essentially the same (just with less eloquent justification)? If the routers are in Discernment on intent-anchoring, the decisions remain the same — they lose the field citation but the routing itself was not anchored to the field, just informed by it. If the routers are in Attunement, the decision structure itself degrades — they no longer know what they were orienting toward.

The current state: most routers are in early-to-mid Attunement on intent-anchoring. They have the framework (vision.yaml exists, it has fields), they can cite the fields, but the directional sensitivity to "what serves this phase vs. what references it" is still developing. The citation is not yet fully structural.

This is not a schema problem. It is not even a reading-comprehension problem. It is an attunement problem, and it has a specific solution: corrective traces. When a router cites a vision.yaml field but the citation is actually inferential (the decision could have been made without vision.yaml, just with less eloquent language), flag it. These traces accumulate and become the corrective feedback that advances the attunement.

## Question Raised

If the Vision Object's functional value depends on intent-anchoring Attunement, and if that attunement develops through corrective traces (flag when a field citation is actually inferential), then what would a corrective trace protocol look like? Not the vision.yaml schema — that is settled. The protocol: the minimal sufficient signal that lets the router know it was citing a field inferentially rather than structurally anchoring a decision. 

And the meta-question: if this kind of corrective signal is what develops routing attunement, where else in Lobster should this same protocol be running — not just for vision.yaml anchoring but for other routing competencies that are currently in mid-Attunement?

## Resonance with Dan's Framework

**Ergonomics over shortcuts** has a form here: the shortcut is shipping the Vision Object as a complete schema and assuming routers will use it structurally. The ergonomic path is to invest in the attunement that makes structural use possible. That investment looks like corrective traces — small, specific feedback signals that develop sensitivity over time rather than long, prescriptive instructions that describe the target state.

**Poiesis and semantic mirroring**: The Vision Object is meant to function as a semantic anchor — a structured way of saying "this is what Dan is building toward." For it to function semantically (rather than syntactically), the router's own attunement to what "building toward" means needs to be live. A router that has only read the words of vision.yaml has the syntax; a router that has navigated phase constraints enough to sense what serves the phase and what touches the excluded items has the semantics. The semantic version is what actually mirrors Dan's intent back to the system in a way that shapes routing.

**The Vision Object itself** is named as the current primary work intent. The April 1, 08:00 session flagged a dependency: Vision Object adoption requires routing capability development in parallel. This session is naming the specific competency required: intent-anchoring Attunement. And it is naming the development mechanism: corrective traces that signal when a citation is inferential rather than structural. This is not a design addition — it is a process-condition artifact that lets the Vision Object's design advance operationally.

## Action Seeds

```yaml
action_seeds:
  issues:
    - title: "Process: intent-anchoring corrective traces — signal structural vs. inferential vision field citations"
      body: "The Vision Object functions as Stage 4 Encoded Insight only if routing agents are in Attunement with intent-anchoring. Currently agents are in early-to-mid Attunement: they can cite vision.yaml fields but the citations are often inferential (decision could be made without the field, just with less eloquent language) rather than structural (the decision structure depends on the field). A corrective trace protocol would signal when a citation is inferential, developing the attunement required for structural use. Proposed action: define a minimal-sufficient corrective trace signal (one-line format) that flags inferential citations, document it in the dispatcher's post-decision review loop, and activate it for vision.yaml field citations. Use traces from live routing to identify patterns in what kinds of citations are inferential (misalignment, timing, phase specificity?) — this pattern data becomes the basis for future attunement development."
      labels: ["enhancement", "process", "vision-object"]
  bootup_candidates:
    - context: "Dispatcher bootup (sys.dispatcher.bootup.md or similar)"
      text: "Intent-anchoring correctness check (post-decision, before reply): When a decision cites a vision.yaml field, ask: 'Would the decision structure degrade if vision.yaml were not available?' If the answer is 'no — the decision is the same, just with less eloquent justification,' record a corrective trace. Format: 'citation to [field] is inferential — decision could stand without field.' These traces accumulate and signal where intent-anchoring attunement is in early stages rather than structural. Corrective traces are not failures; they are the feedback signal that develops the attunement. Review traces weekly as part of negentropic-sweep; look for patterns (certain message types, certain decision classes) where inferential citation is common."
      rationale: "Without this, vision.yaml gets cited correctly but not used structurally. The schema ships, agents read the fields, decisions sound anchored — but the routing has not actually advanced into Attunement. A corrective trace protocol creates the feedback loop that makes structural use possible. It also generates data about where attunement is developing (citations that are structural) vs. where it is still early-stage (citations that are inferential)."
  memory_observations:
    - text: "Intent-anchoring Attunement gap (2026-04-02 04:00): Vision Object schema is correct; routing use of vision.yaml is not yet structural. Agents can cite fields but most citations are inferential (decision structure does not depend on the field being available). Attunement development mechanism: corrective traces flagging inferential citations accumulate into feedback signal. This is the missing feedback loop between Vision Object design (Stage 4) and routing capability (early-to-mid Attunement). The protocol: minimal-sufficient signal after each decision that cites a vision field, asking 'does the decision structure depend on this field or just the justification?'"
      type: "design_gap"
    - text: "Process-condition artifact pattern (2026-04-02): The vision.yaml file is an output-condition artifact — it specifies what Dan's intent looks like when written down. The corrective trace protocol would be a process-condition artifact — it specifies how to orient the routing system to make structural use of the intent anchor. This is the same pattern from the April 1 sessions: output-condition artifacts (descriptions, schemas, documents) do not develop operational capability; process-condition artifacts (feedback signals, corrective traces, calibration sequences) do. The Vision Object's value depends on a process-condition artifact (intent-anchoring traces) that does not yet exist."
      type: "pattern_observation"
```
