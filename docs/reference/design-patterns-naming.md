# Design Patterns: Naming and Multi-Register Thinking

These patterns emerged from the WOS naming exploration. They address how names, metaphors, and vocabularies function as design tools rather than decoration. Each pattern is generalizable — none of them are specific to WOS, though WOS provides the illustrative examples throughout.

---

## Pattern 1: Multi-Register Design

Different vocabularies serve different audiences and contexts. In WOS: biological metaphor (seeds, garden, pearls, harvest) for vision and design alignment conversations; operational names (UoW, GardenCaretaker, Steward, Executor) for code and logs. Neither register colonizes the other. When a system has a strong founding metaphor, preserve it as a distinct register rather than collapsing it into the operational vocabulary. The coexistence is intentional — it means design conversations and implementation conversations can each use their native language without translation loss. Trying to unify the registers produces names that serve neither purpose well: too poetic for logs, too clinical for vision conversations. Let the registers diverge and manage the mapping explicitly.

---

## Pattern 2: Keeper of Names (Naming as Constraint)

Name selection is a design decision, not a labeling exercise. Names that encode character — "the steward is wise," "the caretaker is continuous," "the executor is obedient" — create decision rules. When a proposal makes the steward impatient (auto-prescribing without evaluation), the name rejects it. This is the keeper-of-names function: when a design decision is unclear, ask what the name demands. Choose names that will constrain future proposals, not names that merely describe current behavior. A name that only describes current behavior has no leverage on future decisions. A name that encodes character or disposition acts as a standing constraint — a way of asking "does this proposal fit the character of this component?" before asking "does this proposal work technically?"

---

## Pattern 3: Lifecycle-Stage Naming

The same entity can carry different names at different lifecycle stages, and the name transition is meaningful signal — not inconsistency. In WOS: "seed" in the pre-registry conceptual phase, "UoW" once it enters the execution substrate. The name change tracks a contract change. When designing systems with multi-stage lifecycles, consider whether a single name should carry through or whether the transition itself deserves a naming boundary. Forced name continuity can obscure meaningful stage transitions. If an entity's obligations, representations, and behaviors change substantially at a stage boundary, a name change makes that boundary visible and inspectable. The awkwardness of "what do we call it?" at a transition point is often signal that a real contract boundary exists and deserves acknowledgment.

---

## Pattern 4: Pipeline Bypass (The Pearl Principle)

Not everything entering a system needs to flow through the full pipeline. Some artifacts are already complete — recognition events, not execution events. In WOS: a philosophy session that produced a frontier document is already done. If it gets routed through the seed-to-harvest pipeline, the pipeline breaks because there's nothing to prescribe. Recognize this class of artifact explicitly (pearls), name it, and design bypass routes. Routing complete things through growth pipelines creates category errors that break both the pipeline and the artifact. The diagnostic question is: does this artifact need to grow, or does it need to be recognized? Growth pipelines are the wrong tool for recognition events. When a new artifact class keeps breaking the pipeline, the likely cause is that the pipeline assumes a single lifecycle shape, and the artifact doesn't fit that shape.

---

## Pattern 5: Pre-Metabolic Framing

Distinguish "theoretically complete but not breathing" from "broken." A system can be architecturally sound, fully designed, and correctly implemented but not yet alive — because the heartbeat (the thing that makes it rhythmic and self-sustaining) hasn't been built or enabled. The diagnostic question is: is it pre-metabolic or broken? The intervention for pre-metabolic is different from the intervention for broken. Don't treat pre-metabolic systems as failures; treat them as organisms waiting for their first breath. Misdiagnosing pre-metabolic as broken leads to unnecessary rework — pulling apart something that was correctly assembled and reassembling it in ways that may actually introduce defects. The correct intervention for a pre-metabolic system is to identify and enable the heartbeat, not to question the architecture.

---

These patterns were distilled from the Lobster WOS naming exploration (March 2026). See also: [wos-metaphors-and-naming.md](wos-metaphors-and-naming.md), [wos-constitution.md](wos-constitution.md).
