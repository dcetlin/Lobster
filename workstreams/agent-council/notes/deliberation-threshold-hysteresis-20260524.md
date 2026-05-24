# Council Deliberation: Threshold Register Hysteresis

**Date:** 2026-05-24  
**Status:** concluded  
**Triggered by:** cross-link sweep (resistance registers ↔ metabolic taxonomy)

## Question

Does the Threshold register have hysteresis (directional asymmetry)? The metabolic taxonomy's void irreversibility implies it does; the lexicon treated it as symmetric (threshold value specified, no mention of return path).

## Deliberation

**What hysteresis means structurally.** Hysteresis is the property where a system's response to decreasing load follows a different path than its response to increasing load. The key feature: the system's state at a given load value depends on whether load is being applied or removed. In engineering, hysteresis is quantified as the area enclosed by the load-unload curve — zero for purely elastic (Storage register) elements, positive for any element with path-dependence.

**Testing the domain examples.** The Threshold register's four domain examples are not uniformly hysteretic, which is important:

- *Bow hair/rosin threshold:* Low hysteresis. Reducing contact pressure below threshold stops the Helmholtz oscillation. The system returns to sub-threshold behavior by reversing the original load. The onset and cessation thresholds are close.
- *Circuit breaker:* High hysteresis by design. The breaker trips at the failure threshold but does not re-close when load decreases. Restoration requires an explicit reset — a different class of input than load reduction.
- *Immune activation:* Intermediate. The adaptive response winds down as antigen load decreases, but memory cells persist — the return state is different from the initial state, even if the active response reverses.
- *Immune and circuit breaker examples suggest:* hysteresis is a property that varies across Threshold register instances, not a binary feature.

**The metabolic evidence.** The metabolic taxonomy's void cross-reference says void is "post-Threshold liminal space" and the void decomposition (void-event → scaffold → seed) describes what follows when the Threshold register fires. The deliberation question is whether the three-step decomposition implies that the Threshold register itself is irreversible, or merely that the post-threshold state requires a new generative cycle.

The council examined this carefully. The distinction matters: if the Threshold register is merely a trigger and the irreversibility belongs to the downstream state, hysteresis is not a Threshold property. But if we follow the engineering convention — hysteresis is measured across the full load cycle, including both the forward transition (loading past threshold) and the return path — then the question "what does it take to return from post-threshold state?" IS a question about the Threshold register's hysteresis.

Under this reading, the void decomposition describes the maximal-hysteresis case: restoration to a Threshold-eligible state requires completing scaffold → seed → juice rather than simply reducing the original load. This is analogous to a first-order phase transition where the melting and freezing temperatures differ — the system's return path doesn't retrace the forward path.

**Resolution.** Hysteresis is confirmed as a Tier 2 parameter of the Threshold register — not because all Threshold registers are irreversible (the rosin example is evidence against that), but because hysteresis is a *variable property* that Threshold registers exhibit to different degrees. Some Threshold events are low-hysteresis (easily reversible by load reduction); some are high-hysteresis (require explicit reset); some are maximally hysteretic (require a new generative cycle to restore the pre-threshold configuration). The current lexicon lacked vocabulary for this dimension.

The void decomposition specifically describes the maximally-hysteretic case: the pre-threshold configuration is not recoverable from within the same cycle. This is why void is irreversible — not because thresholds are categorically irreversible, but because the specific Threshold event that produces void has maximum hysteresis (the energy of transformation exceeded the system's elastic return capacity, writing the event permanently — an Inscription-register property coupled to the Threshold event).

## Conclusion

The Threshold register has hysteresis as a variable parameter. Hysteresis is added to Tier 2 as a measurable dimension of Threshold register behavior. The lexicon's Threshold definition was incomplete without it — it named the threshold value (Strength, in Tier 3) but not the asymmetry between loading and unloading paths.

The metabolic taxonomy's void irreversibility is correctly understood as the metabolic expression of maximum Threshold hysteresis: once the Threshold fires with sufficient energy, the return path requires a new generative cycle rather than load reversal. This aligns with the Inscription register's participation in the event — the phase transformation writes itself permanently, which is why restoration requires seeding (not just reversal).

A clarification on the relationship between Threshold and Inscription: a Threshold event that has maximum hysteresis has effectively coupled to the Inscription register — the transformation was written permanently. Low-hysteresis Threshold events (bow hair) remain in the Threshold register without writing into Inscription. This coupling degree is part of what hysteresis measures in the Threshold context.

## Canon updates

- `canon/resistance-registers-lexicon.md`: added Hysteresis as Tier 2 Threshold parameter; updated Void/Threshold cross-reference to name the asymmetry explicitly
- `notes/metabolic-taxonomy.md`: updated Void cross-reference to name the hysteresis arc explicitly

## Remaining open

1. **Degree-of-hysteresis determinants:** What structural properties determine whether a Threshold event has low, high, or maximum hysteresis? The materials analog (first-order vs. second-order phase transitions) may map to the cognitive/organizational domain, but this is not yet characterized in the lexicon.

2. **Threshold-Inscription coupling threshold:** When does a Threshold event "write" into Inscription (maximum hysteresis) vs. remaining reversible? The council named the pattern but did not specify the boundary condition.

3. **Open question Q5.3 (from lexicon source notes):** Whether the antifragility/hormetic window has a universal characterization in load-path terms. The hysteresis analysis suggests a partial answer: the hormetic window is the load range where Threshold fires with recoverable (not maximum) hysteresis — enough transformation to reorganize, not so much that restoration requires a full new cycle. This is worth carrying forward to the Q5.3 investigation.
