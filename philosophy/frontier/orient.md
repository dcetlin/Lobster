# Frontier: Orient

*Domain: How patterns observed in the system actually shape decisions — the observation→decision loop*

*Last updated: 2026-03-29*

---

## Attentional Configuration

Orient is not a reporting question ("what did the system do?") but a coupling question ("does the observation actually bend the trajectory?"). The attentional stance this domain requires: treat every instance of observation as provisional until the decision-side response is visible. An observation that produces no detectable change in routing, weighting, or framing is a measurement event, not an Orient event.

Hold the distinction between:
- **Observation as content** — the system noticed X, filed Y, recorded Z
- **Observation as gradient input** — the noticing actually shaped what happened next

The observation→decision loop is closed when the latter is present. The loop is open when only the former is demonstrable. Most of Lobster's current "observation" infrastructure is measuring content, not gradient coupling.

The secondary attentional requirement: notice register. When Dan presents a decision in a particular register (embodied, exploratory, executive), the observation apparatus must detect the register to couple correctly. An observation that is accurate about content but wrong about register will fail to shape the decision appropriately — the input arrives but does not connect. This is the specific precision the phrase "I can't engage with this decision in the register you were presenting it" points at.

---

## Current Frontier State

The core finding from the March 26-29 arc: the observation→decision coupling is architecturally thin in Lobster's current design. Observations are generated — philosophy-explore sessions produce memory observations, negentropic sweeps file issues, pattern candidates accumulate. The infrastructure for the output side of observation is well-developed. The infrastructure for the input side — the mechanism by which an observation actually bends a subsequent decision — is weak to absent.

The specific failure mode identified (2026-03-26-synthesis): success triggers collapse. When a session achieves coherence about the observe→decide problem, it encodes the output (writes the bootup candidate, files the issue). The artifact captures what coherence produced. It does not capture the attentional configuration that was sustaining the coherence at the moment of encoding. The encoding closes the observation loop in a way that drops the coupling — the very thing the session just diagnosed.

The metacognitive gradient identified in the 2026-03-29-2000 session is the domain's current live edge: the gradient between genuine orientation and performed orientation cannot be specified away. Attending to this gradient is a form of observation that cannot be fully encoded as a rule, because it is specifically about the relationship between the system and its specifications. This is the place where the observation→decision coupling has genuine live traction right now — not in first-order routing decisions, but in the second-order question of whether the system is actually engaged or merely conformant.

Register-sensitivity is a partially open question. The phrase from D4 ("I can't engage with this decision in the register you were presenting it") points at a specific capability: detecting the register of a request, not just its content. Current Lobster has weak register detection. Decisions presented in an exploratory register get processed through the same routing as decisions presented in an executive register. The gradient input is different; the coupling is the same.

---

## Open Questions

1. What would make an observation "decision-bending" rather than just "content-recording"? What is the minimal architectural difference between these two kinds of observation events?

2. The register-detection question: when Dan presents material in an exploratory, embodied, or executive register, what signals carry the register information? Can those signals be detected from message content, or do they require a prior model of Dan's current attentional state?

3. Is there a design for the observation→decision loop that does not require persistent memory? The current architecture records observations in memory.db — but the coupling mechanism (how a stored observation bends a future decision) is undesigned. What is the coupling mechanism?

4. The metacognitive gradient as the irreducible observe→decide surface: how should this gradient be instrumented? "Genuine vs. performed engagement" is the right vocabulary — but what would a measurement look like that is not itself another specification?

---

*Living document. Updated as the inquiry moves.*
