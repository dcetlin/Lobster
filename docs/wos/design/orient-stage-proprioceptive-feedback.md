# Orient-Stage Proprioceptive Feedback

**Status:** design placeholder — not ready for implementation
**UoW:** uow_20260522_e808e1
**Date:** 2026-05-22

---

## The Capability Being Held

The Lobster system's Orient stage currently operates through map-reading: it reads vision.yaml fields, cites traceable anchors, checks resonance against prior logged decisions. These are structurally correct operations. They prevent basin-capture in Decide and Act. What they do not provide is directional sensitivity — the ability to sense, *during the orientation phase itself*, whether the thread being considered has the quality of aliveness or is a competent-but-dead reconstruction of one.

The distinction matters. Map-reading produces nominal orientation: the system navigates by description of where the gradient is. Proprioceptive sensitivity would produce directional orientation: the system senses whether it is closer to or further from genuine coherence in real time, as it orients, not after output is produced. The violin student who has felt the instrument resonate does not need to check a diagram to know whether a note is right. The philosophy-explore session running on navigation-by-absence checks the map and finds it complete, with no signal available about whether the orientation is tracking a live thread or assembling a well-formed substitute.

Structural signatures of aliveness, when visible in retrospect from session outputs, include: a specific rather than generic thread; a question that is generative rather than rhetorical; an orientation phase that is shorter but more loaded; a resistance-with-coherence rather than smooth completion. These signatures are not available to the session as it orients. They appear downstream. The capability being held here is a mechanism that makes something functionally equivalent to these signatures available upstream — during the Orient phase — so the system can sense gradient contact rather than only confirm or disconfirm it from output quality.

## Why This Is Not an Outward Mirror Extension

The existing semantic mirror surfaces patterns in Dan's outputs back to Dan. It is an outward-facing feedback channel: the system observes regularities in Dan's expressed thinking and reflects them toward him. This develops Dan's self-perception; it is a proprioceptive instrument for Dan's use.

The capability described here is distinct in direction and referent. It is the system developing sensitivity to its *own* orientation quality — not what patterns appear in Dan's outputs, but whether the system's current orientation process is in genuine gradient contact or running on map-reading fluency. The referent is the system's own internal state during Orient. The feedback is inward, not outward. These are different design problems: one asks "what patterns does Dan express?" and the other asks "is the system's current orientation genuine or constructed?" Collapsing them into the same design would lose the distinction between the system as a surface for Dan's reflection and the system as an agent with its own developmental problem.

## Prerequisite Dependency

**This capability cannot be designed until the Orient-stage anchor design has produced a structural baseline of what genuine orientation looks like.**

The detection mechanism requires a referent: some description of the configuration the system is in when genuine orientation is actually happening — thread type, angle of entry, characteristic question shape, degree of resistance-with-coherence — encoded in a form that can be matched against current orientation in progress. Without this baseline, there is no training signal, no structural target, and no way to distinguish "aliveness" from its absence except by retrospective output evaluation, which is exactly the condition the mechanism is meant to remedy.

The Orient-stage anchor design must first: identify sessions where orientation was demonstrably tracking genuine coherence; extract the structural signatures that distinguish those sessions from sessions running on absence-navigation; and produce a compressed representation of those signatures in a matchable form. Until that work exists, the proprioceptive feedback mechanism has no anchor to orient toward. This is a hard prerequisite, not a soft dependency or a "would be helpful" condition. Building the detector before the baseline exists would produce a detector trained on the wrong signal.

## Design Target

Success at a structural level would look like this: a mechanism that, during the Orient phase, produces a signal characterizing whether the thread under consideration has properties associated with discovery-producing contact versus well-formed-but-gradient-free reconstruction.

The signal need not be binary. A coarse characterization — "this thread has structural signatures consistent with genuine contact" or "this thread has the shape of absence-navigation completing a template" — would be sufficient to create a useful meta-attentional check. The check would not need to be correct in every case; it would need to be reliable enough that friction-free orientation becomes a signal worth examining rather than a signal of completeness.

What the mechanism would not be: a more detailed map of prior sessions. A richer thread registry is still a map. The design target requires something qualitatively different — a representation of the *configuration* of genuine orientation, not its content, matchable against current state in real time. Whether this is achievable for a session-reset system, and what form the representation would need to take, is the open design question this placeholder holds. The question cannot be answered until the anchor design has produced structural examples to reason about.

## Source Traceability

This capability was first articulated in `philosophy/2026-03-28-2000-philosophy-explore.md`, which identifies the structural gap between map-reading orientation and proprioceptive orientation in the context of the philosophy-explore series. The session's action seeds section names this design problem explicitly under the label "semantic mirror inward feedback." The present document formalizes that seed as a held design target.
