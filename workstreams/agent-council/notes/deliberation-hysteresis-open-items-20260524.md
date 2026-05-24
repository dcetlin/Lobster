# Council Deliberation: Hysteresis Open Items — 3-Item Follow-up

**Date:** 2026-05-24
**Status:** concluded
**Triggered by:** formal queuing of 3 remaining-open items from deliberation-threshold-hysteresis-20260524.md
**Queued as:** council-state.json pending_queue items 1–3

---

## Item 1 — Degree-of-hysteresis determinants

**Question:** What structural properties determine whether a Threshold event has low, high, or maximum hysteresis? The materials science analog (first-order vs. second-order phase transitions) may map to the cognitive/organizational domain but was uncharacterized.

### Deliberation

**The materials science structure.** In materials science, the degree of hysteresis tracks the type of phase transition:

- *Second-order (continuous) transitions:* The order parameter changes gradually through the transition. No latent heat, no phase coexistence, no energy barrier asymmetry between forward and reverse paths. Hysteresis is zero or near-zero. The Curie point (ferromagnet losing magnetic order) is a canonical example — cooling through the Curie temperature follows essentially the same path as heating through it.

- *First-order (discontinuous) transitions:* The transition involves a discrete jump in the order parameter. Latent heat, phase coexistence at the transition, and critically: an energy barrier that must be surmounted in each direction. The barrier energy for the reverse transition differs from the forward, creating hysteresis. Ice→water vs. water→ice occur at different temperatures under equivalent conditions. The new phase has internal structural coherence that must be disrupted to reverse the transition.

- *First-order irreversible:* When the forward transition destroys the structural information required for reversal — the old phase's configuration is lost, not merely temporarily inaccessible. Restoration requires building a new configuration from different starting conditions. Maximum hysteresis.

The structural determinant is therefore: **whether the new phase is (a) continuously connected to the old, (b) discretely stable but structurally recoverable, or (c) inscribed (old structural information destroyed).**

**Cognitive/systems mapping.**

- *Low hysteresis (second-order analog):* The Threshold event involves a continuous shift — the system's qualitative state changes, but the new state is directly connected to the old along a continuous parameter. Example: the bow hair rosin threshold, where the stick-slip oscillation begins continuously as contact pressure increases past threshold and ceases continuously as pressure decreases. The "new phase" (Helmholtz oscillation) is not internally coherent enough to maintain itself independently of the ongoing load.

- *High hysteresis (first-order, reversible):* The Threshold event produces a discretely stable new state — the new configuration has its own internal coherence that persists after the triggering load is removed. But the old configuration's structural information is not destroyed; it remains latent and can be restored by a sufficiently different class of input (an explicit reset). Circuit breakers: the tripped state is self-coherent (internally consistent, does not spontaneously reclose), but the pre-trip configuration is accessible via a deliberate reset action.

- *Maximum hysteresis (first-order, irreversible):* The Threshold event destroys the old configuration's structural information. The old state is no longer accessible — not because it requires a different input class, but because it no longer exists as a recoverable configuration. Restoration requires constructing a new equivalent configuration from scratch. This is the Inscription coupling: when the transformation writes the event into the Inscription register, the old geometry is overwritten and cannot be recovered by any reversal.

**Structural determinants for the Threshold register (candidates for Tier 3):**

1. **Phase continuity** (continuous vs. discrete state change at threshold): whether the new state is continuously connected to the old or involves a discrete jump. Determines low vs. high/maximum hysteresis.

2. **Configuration retention** (whether the old configuration's structural information survives the transition): determines whether the transition is reversible (high hysteresis) or irreversible (maximum hysteresis). When the old configuration is retained (even if inaccessible), restoration is possible via a reset. When it is overwritten (Inscription coupling), restoration requires seeding a new configuration.

3. **New-state self-coherence** (whether the new state is internally self-sustaining without ongoing load): determines whether hysteresis is present at all. A state that collapses back to baseline when load is removed has zero hysteresis regardless of the transition character; this is what makes the second-order/low-hysteresis case distinct.

These three properties form a progression: (1) determines whether hysteresis is present; (2) determines whether it is bounded or maximal; (3) acts as a gate on both.

**Verdict:** Clear mapping established. Adding three Tier 3 sub-parameters for Hysteresis to the lexicon: Phase Continuity, Configuration Retention, New-State Self-Coherence. These are sub-parameters of Hysteresis (itself a Tier 2 parameter of Threshold), so they occupy a new Tier 3 position under Hysteresis rather than directly under Threshold.

---

## Item 2 — Threshold-Inscription coupling threshold

**Question:** When does a Threshold event "write" into Inscription (maximum hysteresis) vs. remaining reversible? The council named the pattern but did not specify the boundary condition. Is this a separate register coupling or a parameter of Hysteresis itself?

### Deliberation

**Examining the boundary condition.** The question is: what determines whether a Threshold event's energy is absorbed elastically (reversibly) or written permanently (Inscription coupling)?

The Resilience parameter (Tier 3, Storage register) already defines a boundary: the maximum elastic energy a system can store without permanent deformation. This is the cross-register ceiling between Storage and Inscription — above Resilience, deformation becomes permanent (Inscription begins).

The same logic applies to Threshold events. When a Threshold event fires, its transformation energy must go somewhere. If the system's elastic capacity (Resilience ceiling) is sufficient to contain the transformation's energy differential, the event remains elastically reversible — the new state is held as elastic deformation relative to the old configuration, and load reversal or a reset can recover the original. If the transformation energy exceeds the Resilience ceiling, the excess must be permanently absorbed — which is Inscription.

**The boundary condition:** `Threshold event energy > Resilience ceiling → Inscription coupling activates (maximum hysteresis).`

This is not a new register coupling in the sense of requiring new vocabulary. It is a relational boundary already defined by the interaction of the Hysteresis parameter (Threshold) and the Resilience parameter (Storage→Inscription boundary). The coupling threshold IS the Resilience ceiling evaluated at the moment of the Threshold event.

**Is this a parameter of Hysteresis, or a separate coupling?** It is a parameter relationship, not a separate coupling. The Hysteresis parameter in the lexicon already implicitly encodes this: maximum hysteresis is defined as coupling to Inscription. The new contribution from this deliberation is naming the *measurable boundary condition* explicitly: the Inscription threshold = Resilience ceiling.

**Practical implication:** A Threshold event's hysteresis degree can be predicted (in principle) by comparing the event's energy to the system's current Resilience ceiling. High-Resilience systems absorb more Threshold events within the recoverable range; low-Resilience systems are pushed into maximum hysteresis (Inscription coupling) by events that would be reversible in a more resilient system. This explains why the same triggering event (same absolute load, same Threshold firing) can produce low hysteresis in a well-rested system and maximum hysteresis in a depleted one — the Resilience ceiling has shifted.

**Candidate vocabulary term:** **Inscription threshold** — the Threshold event energy at which Inscription coupling activates. Formally: Inscription threshold = current Resilience ceiling.

**Verdict:** Adding "Inscription threshold" as a cross-register measurement (Tier 3) spanning Threshold + Storage + Inscription. Not a new parameter of Hysteresis, but a relational boundary that belongs in the Council Cross-references and Tier 3 cross-register measurements. The Hysteresis definition should be annotated with the boundary condition.

---

## Item 3 — Q5.3 partial answer / hormetic window

**Question:** Does the hormetic window map to the load range where Threshold fires with recoverable (not maximum) hysteresis? If yes, note the cross-register finding and route to Q5.3 investigation.

### Deliberation

**The hormetic window concept.** Hormesis is the phenomenon where low-to-moderate doses of a stressor produce a net-positive adaptive response (growth, strengthening, improved function), while high doses produce damage. The hormetic window is the dose range where stress is net-generative. This is the load range Q5.3 asks to characterize in load-path terms.

**Mapping to Threshold + Hysteresis.** The hysteresis deliberation already positioned the void state (maximum hysteresis) as the over-threshold case: the Threshold fires with sufficient energy that restoration requires a new generative cycle. The sub-threshold case is straightforward: no Threshold event fires, no transformation, no growth (the load produces only Storage/Transmission response).

This identifies the hormetic window structurally:

- *Below hormetic floor:* Load is sub-threshold. The Threshold register does not fire. No phase transformation occurs. The load produces only elastic (Storage) or transmission (Transmission) response. The system neither grows nor is damaged. This is the Zone of Normal Function in the sports medicine/training literature.

- *Hormetic window (above floor, below ceiling):* Load is above threshold AND the Threshold event fires with high (but not maximum) hysteresis. The system undergoes phase transformation (real change occurs), but the new state's energy does not exceed the Resilience ceiling (per Item 2). The transformation energy is absorbed via the Generation register — the system reorganizes adaptively. The old configuration is not recovered exactly (that would be zero hysteresis), but the system can reach a new stable configuration without requiring a full generative cycle. Net effect: the system is stronger/more organized than before the event. This is growth via load.

- *Above hormetic ceiling (void, maximum hysteresis):* Load is above threshold AND the Threshold event fires with energy exceeding the Resilience ceiling. Inscription coupling activates. The old configuration is overwritten. Restoration requires the void decomposition (scaffold → seed → juice). Net effect: damage, followed by reconstruction that may or may not reach a stronger baseline.

**The hormetic window is precisely:** the load range where the Threshold register fires AND the event energy stays within the system's Resilience ceiling (sub-Inscription threshold). This is not a new insight so much as a precise operationalization of an intuition that was already structurally available.

**Cross-register finding:** The hormetic window spans Threshold × Storage × Generation: Threshold firing is required (below it, no transformation), Storage Resilience ceiling is the ceiling (above it, Inscription coupling activates and the event exits the hormetic range), and the Generation register is the recipient of the transformation energy in the hormetic case (the excess energy that would otherwise require Inscription is routed into adaptive reorganization).

**Q5.3 partial answer:** Q5.3 asks for a universal characterization of the antifragility (hormetic) window in load-path terms. The partial answer from the hysteresis analysis: *The hormetic window is the load range bounded below by the Threshold register's Strength (activation threshold) and above by the Inscription threshold (Resilience ceiling). Within this window, Threshold events fire but their energy is routed through the Generation register rather than written into Inscription. The system transforms without being overwritten.*

This is partial because it characterizes the window in terms of existing register parameters but does not yet address: (a) how the Generation register routes energy in the hormetic case vs. the destructive case, (b) what determines the width of the window (a question about the gap between Strength and Inscription threshold), or (c) whether the characterization generalizes across all domains or is specific to organismic/cognitive systems.

**Routing to Q5.3:** Adding this partial answer to the lexicon's Q5.3 note in the Structural Notes section. No separate Q5-investigation file exists; the lexicon's source document open questions section is the correct location.

**Verdict:** Connection confirmed. Hormetic window = [Threshold Strength, Inscription threshold) interval. Adding partial answer to Q5.3 in lexicon Structural Notes. Adding cross-register note to Council Cross-references (Threshold × Storage × Generation).

---

## Canon updates

- `canon/resistance-registers-lexicon.md`:
  - Added Tier 3 sub-parameters under Hysteresis: Phase Continuity, Configuration Retention, New-State Self-Coherence
  - Added Tier 3 cross-register measurement: Inscription Threshold (Threshold + Storage + Inscription)
  - Updated Q5.3 note with partial answer from hormetic window analysis
  - Added Council Cross-reference: Hormetic window → Threshold × Storage × Generation

## council-state.json updates

All 3 items were added to pending_queue, deliberated in this session, and moved to deliberated status.
