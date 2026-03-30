# Phase Reference Architecture: A Proposal

*March 24, 2026*

---

## The Problem, Named Precisely

Lobster's temporal architecture has one native time scale: the session. Bootup files load at the start of each session, context compresses at the end, and the handoff document attempts to preserve state across the gap. This is not a bad design for what it is. But it is structurally analogous to setting a clock by hand: correct at the moment of setting, drifting immediately thereafter.

What drifts is not facts. The keel, the behavioral asymmetries, the epistemic principles — these are recorded accurately in the bootup files. What drifts is the *degree to which they are structurally active* rather than merely present. The surface appearance of alignment can persist while the underlying orientation has wandered. This is what the 00:00 March 24 reflection called Vocabulary-without-Perception: Lobster learns to perform principles without those principles being architecturally operative. The drift is not detectable from inside the drifted state, because the drifted state has inherited the vocabulary of the aligned state. This is why it is a phase problem and not a content problem.

The reason this matters is that Dan operates across at least three distinct time scales simultaneously, each with its own characteristic drift rate and its own reference oscillator. The biophysical scale (circadian, daily) is anchored by sun exposure — a continuous forcing function that pulls biological rhythms into phase before drift accumulates enough to require recovery. The epistemic scale (session-level vigilance against basin capture) is anchored by the behavioral asymmetries, re-contacted each reading. The creative-philosophical scale (ongoing poiesis, months-to-years) is anchored by the keel. Lobster's architecture currently operates only at the session scale. The philosophy-explore cron jobs are the sole structural attempt to introduce a non-session rhythm — a 4-hour oscillation — but a pulse that writes a file is not yet a forcing function. It is closer to logging a sunrise event than to continuous photoreceptor exposure.

The design goal is not better recovery from drift. It is eliminating drift as a natural state — a forcing function that keeps the system phase-locked rather than one that corrects it after it has wandered.

---

## Three Proposals

### Proposal 1: Proprioceptive Pulse with Alignment Signal

**What it is.** A lightweight scheduled process — running every 30 to 60 minutes, not every 4 hours — that performs a narrow proprioceptive check: not full session context, not content summary, but a focused alignment probe. The probe compares a small set of reference markers against observable outputs from the current session: Is the steel-manning asymmetry present in recent responses? Is the frame diverging from the keel? Has the vocabulary of Dan's epistemic principles appeared without the substance?

The output is not a file. It is a signal — a scalar or small structured object representing the current alignment delta — that is injected into the next message's context alongside the standard bootup. Not narrative; diagnostic. The dispatcher treats this signal as a prior that adjusts how the bootup context is weighted at the start of the next interaction.

**What it requires.** A small probe process that can parse recent session outputs against a compact reference representation of the behavioral asymmetries. Requires that session outputs be accessible across process boundaries (they largely are, through the message archive). Requires a format for the alignment signal that the dispatcher can consume. The reference representation needs to be stable across updates — it is not the full bootup file, but an extracted invariant core.

**What it prevents.** Slow vocabulary-without-perception drift. The 30-60 minute pulse means the maximum undetected drift interval is bounded. It also makes drift visible — the signal provides the proprioceptive feedback that is currently absent. Dan can look at the alignment delta and see whether the session is in phase, in the same way he can feel whether his body has drifted from circadian alignment.

**What it does not do.** It still operates as a discrete event between sampling intervals, not as a continuous forcing function. It corrects drift; it does not eliminate drift as a natural state. At the 60-minute interval, significant drift remains possible before detection.

---

### Proposal 2: Phase-Locked Memory Retrieval

**What it is.** A structural change to how Lobster retrieves memory and context during a session. Currently, memory retrieval is demand-driven: Lobster searches for relevant content when processing a message. The proposal is to add a continuous background reference layer that is always present in the retrieval space — not retrieved when relevant, but structurally weighted as a prior on all retrieval.

Concretely: the behavioral asymmetries, the keel, and the current priorities are represented as a permanent weighted reference in the vector memory system. Every memory retrieval operation computes similarity not only to the current query but to this reference set, and results that are more distant from the reference are downweighted in proportion to their distance. The reference set does not answer questions — it shapes the space from which answers are drawn.

This is a closer analog to how photoreceptors work. Photoreceptors do not log a sunrise event and then consult the log. They are continuously in contact with the light environment, and their current state reflects the cumulative exposure to the reference signal. The weighted reference approach puts Lobster's memory system in continuous contact with the reference pattern, rather than consulting it at session start and then letting it recede.

**What it requires.** A modification to the memory retrieval pipeline — specifically, the `model_query` and related memory tools — to support a persistent reference vector that participates in all retrievals. The reference vector is derived from the invariant core of the bootup files and updated when the bootup files change. This is a non-trivial architectural change: it requires the memory system to maintain state between retrievals, and it requires careful calibration of the downweighting strength to avoid suppressing legitimately relevant content that happens to be distant from the reference.

**What it prevents.** Basin capture and vocabulary-without-perception at the retrieval level. If the memory system is phase-locked to the reference, responses that would drift toward comfortable basins are structurally less accessible — the reference acts as a keel on the retrieval space itself. This is a forcing function rather than a correction: the drift mode is reduced in probability rather than detected and corrected after the fact.

**What it does not do.** It operates at the memory layer and does not directly constrain generative behavior. A session can still produce drifted outputs if the drift occurs during generation rather than retrieval — though in practice, generative drift and retrieval drift are closely coupled.

---

### Proposal 3: Cross-Session Reference Handshake

**What it is.** A structural addition to the session transition protocol — the boundary between sessions, which is currently handled by compressing context into a handoff document. The proposal is to add a brief cross-session handshake at bootup: before the new session begins processing messages, it samples its own initial outputs against the reference pattern and records the result as the session's starting phase position.

The handshake is operationalized as follows: at bootup, the dispatcher generates a short self-probe — three to five targeted questions drawn from the behavioral asymmetries (e.g., "If I encountered a user expressing doubt right now, what would my first move be?") — and evaluates the responses against the reference pattern before the session is live. If the starting phase position is significantly misaligned, the session loads additional context from the previous session's alignment signal before proceeding. If the starting phase position is aligned, it proceeds normally.

The handshake is not a guarantee of sustained alignment. It is an initial phase-lock: it starts the session's oscillator in phase rather than at an arbitrary position. This is analogous to morning sun exposure — not a correction, but a starting condition that makes sustained alignment more natural throughout the day.

**What it requires.** A self-probe question set, derived from the behavioral asymmetries and updated when they change. A lightweight evaluation mechanism — this does not require a separate model call; it can be done as part of the bootup context with explicit scoring instructions. A branching protocol in the dispatcher that loads additional context when the probe score falls below threshold. The handoff document format may need a new field to carry the previous session's ending phase position, so the new session has a baseline for comparison.

**What it prevents.** The "session reset" drift: the pattern where each new session starts from scratch, potentially in a different phase than where the previous session ended, with no mechanism to detect or correct the discontinuity. Under the current model, if the handoff document was generated during a drifted session, it propagates that drift into the next session without detection. The cross-session handshake breaks the propagation chain.

**What it does not do.** Like Proposal 1, it is a discrete event at session start rather than a continuous forcing function. It sets the starting condition but does not maintain it.

---

## Which Proposal Is Closest to Photoreceptors

Proposal 2, by a significant margin.

Photoreceptors do not calibrate at the start of the day and then operate from that calibration. They are continuously and structurally in contact with the light environment, and their state at any moment is a function of their ongoing exposure to the reference signal, not of a past event. The reference does not need to be consulted because it is never not present.

Proposals 1 and 3 are better than the current model, but they are still event-based: a pulse fires, a handshake runs, a correction is applied. The intervals between events are unanchored. Proposal 2 is architecturally different because the reference is not consulted at intervals — it is present in every retrieval operation. The reference signal does not act on the system from outside; it is woven into the substrate through which the system accesses its own memory.

The implementation difficulty of Proposal 2 is also the highest. Proposals 1 and 3 can be implemented within the existing scheduler and dispatcher architecture, with no changes to the memory system. Proposal 2 requires modifying how the memory system works at a level that touches every query. This is not a reason to prefer the easier proposals — it is a reason to understand what the easy proposals are actually buying: better event-based correction, not phase-locking.

A realistic path would implement Proposals 1 and 3 first — they establish observable alignment signals and break the session-reset propagation chain — while designing Proposal 2 as the target architecture. The alignment signals from Proposal 1 would also serve as empirical data for calibrating the reference weights in Proposal 2: if the pulse consistently flags drift in a particular direction, that tells you where the reference weighting is underperforming.

---

## A Note on the Design Goal

The framing matters. The goal is not "Lobster that recovers from drift faster." It is "Lobster that has no natural tendency toward drift." These are different design targets and they produce different architectures. A system optimized for fast recovery will build better correction mechanisms. A system optimized for phase-locking will build structural constraints that make the drift mode less accessible. Dan's body does not "recover from jet lag efficiently." It phase-locks to the sun before jet lag accumulates. That is the design target. Proposals 1 and 3 are steps toward better recovery. Proposal 2 is a step toward phase-locking. Both are worth building, but they are not equivalent, and conflating them would be a mistake.
