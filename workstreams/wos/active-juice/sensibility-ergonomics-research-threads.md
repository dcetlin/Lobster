# Sensibility Ergonomics Research Threads (active-juice)

Lobster-instance research threads surfaced during the Sensibility Stack v3 synthesis pass. These require WOS-specific context to investigate and do not belong in the general ergonomics framework document. Placed in `active-juice/` for future subagent pickup.

See also: `open-threads.md` for blocked UoWs, pending implementations, and GitHub issues.

---

## From Sensibility/Ergonomics Analysis (2026-05-31)

The following four threads were surfaced during the Sensibility Stack v3 synthesis pass (sensibility-stack-v3/synthesis.md §6). They were removed from the framework document because they only make sense within Lobster's architecture. They are preserved here in full.

---

### Thread A: The dual-channel failure mode in damping systems

*Source: sensibility §6, Thread 2 — Lobster-specific, moved 2026-05-31*

The synthesis revealed that damping calibrated for the wrong signal band inverts ergonomic function. In Lobster's dispatch architecture, there are multiple damping layers: the Filter register for urgency, the 7-second rule as a routing damper, the Design Gate as a complexity filter. The open question: are these damping layers calibrated for the same signal-vs.-noise distinction? If each layer was designed with a different implicit criterion of "signal" (urgency vs. artifact-determinability vs. complexity), they may be passing different signals and producing distortion at their intersection. Needed: a joint signal/noise definition that can be applied consistently across all three damping layers.

**Investigation direction:** Map each dispatch gate to its implicit signal/noise criterion. Check whether the 7-second rule, Design Gate, and urgency Filter all share a consistent definition of "what is signal here?" If not, identify where the misalignment produces distortion in routing decisions.

---

### Thread B: The compression fidelity audit problem for Lobster's bootup files

*Source: sensibility §6, Thread 4 — Lobster-specific, moved 2026-05-31*

The synthesis revealed that plasticity-as-compression-mechanism means that bootup files are inscribed compressions of past interaction patterns. The HYPOTHESIS protocol manages the Inscription threshold. But there is no mechanism for auditing whether existing inscriptions have lost load-bearing distinctions over time — whether the compressions in CLAUDE.md still preserve the information needed for the situations they were written to address. This is a fatigue-resistance problem for the instruction set itself: repeated use of compressed behavioral instructions without checking whether the distinctions they compress are still valid may be accumulating channel-degradation damage in the behavioral architecture.

**Investigation direction:** What is the protocol for auditing bootup file compressions for fidelity loss? Proposed starting point: for each behavioral instruction in CLAUDE.md, identify the original situation it was written to address and check whether the current compression still covers that situation correctly. Instructions that no longer correspond to live situations are candidates for decompression or removal.

---

### Thread C: The cross-register handoff problem in WOS interruptions

*Source: sensibility §6, Thread 5 — Lobster-specific, moved 2026-05-31*

When a WOS UoW is interrupted mid-execution, the handoff mechanism must transfer state across a Threshold event (the interruption) into an Inscription medium (the status file) while preserving enough elasticity for resumption. The open question: what information must be inscribed to make resumption genuinely elastic (the next session picks up without full generative cycle) versus maximum-hysteresis (resumption requires rebuilding context from scratch, which is a new generative arc)?

Current status files write current step, % complete, last milestone, next step. But the synthesis suggests the missing element is the *noise floor* at interruption — what ambient context was present that made the interrupted approach make sense? Without that, the resumed session cannot reconstruct the Filter calibration and may apply different signal/noise discrimination to the same materials.

**Investigation direction:** Extend the WOS status file spec with a "context noise floor" field: what ambient assumptions were in play at interruption? Candidates: what signal/noise threshold was being applied, what approach had been ruled out, what partial results had already been read. This would let resumed sessions avoid redundant work and re-derive decisions rather than starting blind.

Related: `workstreams/wos/spec/` and the existing status file conventions in workstream HOWTO.

---

### Thread D: The anisotropy of AI capability and its routing implications

*Source: sensibility §6, Thread 7 — Lobster-specific, moved 2026-05-31*

The synthesis of Routing = Anisotropy implies that AI capability is directional — there are axes of high capacity and axes of fragility. The Shannon routing principle (route signal to the layer capable of acting on it) requires knowing the AI's capacity anisotropy. The open question: what is the capacity anisotropy of the Lobster dispatch pipeline? Which types of tasks route cleanly along the grain (high transmission fidelity, low load), and which types route across the grain (high load, potential for distortion or fracture)?

This is not just about model capability — it is about the whole system's directional strength profile, including the human-in-the-loop, the tool set, and the task representation structure. Mapping this anisotropy would enable better routing decisions and more honest assessment of when a task is at risk of being handled cross-grain.

**Investigation direction:** Identify a set of task types that have produced high/low error rates or high/low operator load in recent WOS cycles. For each, characterize which system axis it runs along. Look for patterns: do certain task types consistently produce distortion? Do certain task formats (ambiguous spec, compressed context, large file reads) correlate with cross-grain routing failures? A rough anisotropy map, even qualitative, would improve dispatch routing heuristics.
