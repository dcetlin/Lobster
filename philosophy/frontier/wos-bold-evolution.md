---
status: design invitation response
context: Dan's invitation to gut core systems — "bold ideas, refinements, transitions, net new"
occasion: Reply to sia-hexo-synthesis.md, 2026-05-29
---

# WOS Bold Evolution: Structural Leaps

*Written in response to Dan's call to gut core systems if high-leverage evolutions are visible. His critique: WOS is "purely conceptual, not exceptional" — doesn't yet live up to the vision of an autopoietic task execution system that leverages shared resources, individual autonomy, and complex organizational constructions. This document holds nothing back.*

---

## The Real Problem

The current WOS loop has the right shape but the wrong metabolism. It is:

- **Prescription-driven, not learning-driven.** The steward diagnoses and prescribes. The executor executes. Failure → re-prescribe. But the prescriptions don't accumulate into structural improvement. Every cycle the steward starts fresh with the same LLM prior. There is no lever that makes the *system itself* better at prescribing over time — only Dan can do that, by editing prompts.

- **Polled, not event-native.** Every 3 minutes, heartbeats fire, the system checks what needs doing. The loop is a clock, not a nervous system. Real autopoiesis responds to signals, not to time intervals.

- **Single-body, not distributed.** WOS is a loop running on one machine, against one GitHub repo, with one Lobster instance as the executor population. "Leveraging shared resources and individual autonomy at the organizational level" — that's a network, not a loop.

- **Improvement is out-of-band.** When the system fails, the failure is surfaced to Dan. Dan adjusts bootup files. The system reruns. The learning happened in Dan, not in the system. This is human-mediated self-improvement at best — autopoiesis requires the system to close the loop on its own.

What follows are four structural leaps. Each one targets a different load-bearing failure.

---

## I. From Prescription Loop to Learning Loop: The Adaptive Steward

**The problem it solves:** The steward prescribes from a frozen LLM prior. No matter how many UoWs cycle through, the quality of prescriptions doesn't improve. The corrective trace mechanism (V3 Change 2) reads past traces but passes them as context to the same unimproved prompt. The system is learning-shaped but not learning.

**What we gut:** The current steward architecture — a single Python script that reads traces, generates prescriptions, and writes them to the registry. The diagnosis/prescribe/close loop as the terminal abstraction.

**What the new shape is:**

Three components replace one:

1. **Prescriber** — generates prescriptions as before, but outputs structured prescription objects (not prose strings). Each object tags: register, diagnosis hypothesis, proposed steps, confidence estimate, and a *counterfactual question* — "what would falsify this diagnosis?"

2. **Verdict Accumulator** — a persistent store that maps (register, diagnosis_hypothesis) → (n_successes, n_failures, n_partial). After each UoW closes, the prescriber's hypothesis is scored against the actual outcome. Over time, this is a structured track record of diagnostic accuracy by hypothesis type.

3. **Selector** — a meta-component that reads the verdict accumulator before each prescription and biases the prescriber toward hypothesis types that have higher success rates for this register + symptom pattern. Initially, the selector is just retrieval — "here are the five most similar prior diagnoses and their outcomes." Later, the selector becomes a lightweight learned policy.

The key shift: **diagnosis is no longer stateless**. The system accumulates evidence about which diagnostic moves work. Dan still controls the prescriber's structure, but the system tracks its own batting average.

SIA names this "meta-RL over the lever-selection policy." For Lobster, it's: accumulate a verdict ledger, retrieve it at diagnosis time, let the retrieval bias the next prescription.

This doesn't require fine-tuning. It requires schema work and a verdict-accumulation hook in the UoW close path.

---

## II. From Polled Clock to Event-Native Nervous System

**The problem it solves:** The 3-minute heartbeat poll is a clock pretending to be a nervous system. Real events — a GitHub issue created, a subagent completing work, a PR failing CI — arrive asynchronously, but the system responds to them only at the next heartbeat boundary. This introduces latency, wastes cycles polling for nothing, and makes the system feel mechanical rather than responsive.

**What we gut:** The steward-heartbeat.py and executor-heartbeat.py cron scripts as the primary dispatch mechanism. The 3-minute clock as the loop driver.

**What the new shape is:**

WOS becomes event-driven at its core. Three event types drive everything:

1. **Issue events** — GitHub webhooks (or a webhook poller that runs every 30s and emits inbox messages on delta). When a new issue appears or changes state, a germination event fires immediately. Not at the next 15-minute GardenCaretaker window — *immediately*.

2. **UoW state events** — When a subagent calls write_result, a state-transition event fires. The steward doesn't need to poll for completed UoWs; completions arrive as events. The steward becomes an event handler, not a scheduler.

3. **Capacity events** — When a subagent slot frees up (an executor completes), a dispatch event fires. The executor heartbeat becomes a capacity signal, not a clock.

The heartbeat scripts don't disappear — they become a *safety net*, not the primary mechanism. They run every 5 minutes as an orphan-recovery backstop. Primary dispatch is event-driven.

The implementation path: a lightweight event emitter that writes typed inbox messages on state transitions. The dispatcher already handles `wos_execute` — extend that to `wos_issue_created`, `wos_uow_completed`, `wos_capacity_available`. The main loop routes these to specialized handlers. No new infrastructure required beyond the inbox message protocol already in place.

The feel of this change: WOS stops being a clock and starts being a system that *responds*. Issue arrives → germination happens within 30 seconds. Subagent finishes → next UoW dispatches within 10 seconds. The loop closes faster and the system feels alive.

---

## III. From Single Executor to Executor Mesh: Distributed Autonomy

**The problem it solves:** WOS currently dispatches to subagents running on a single Lobster instance. "Leveraging the best of shared resources and individual autonomy, or complex organizational constructions" — that's not one machine. That's a network of execution contexts with different capabilities, resource profiles, and knowledge domains.

**What we gut:** The assumption that "executor" means "Claude subagent spawned by this instance." The current executor/dispatcher architecture as a single-site compute model.

**What the new shape is:**

An executor mesh — multiple execution contexts that WOS can route to based on task fit.

The three-tier model:

1. **Local Lobster executors** — current Claude subagents, for tasks requiring Lobster system access, memory, inbox tools. Fast, cheap for short tasks.

2. **Specialized domain agents** — long-running agents with deep context in a specific domain. A "code architecture" agent that has read every file in a codebase and maintains persistent context. A "writing" agent that has internalized Dan's style across hundreds of documents. These are not subagents spun up per-task; they are persistent specialized contexts that accumulate domain knowledge across tasks. The SIA parallel: this is the "weight update" equivalent — domain intuition encoded in persistent context, not re-derived from scratch each time.

3. **External executor contracts** — WOS issues a task spec (structured, typed) and any conforming executor can claim it. The executor signals capability match before claiming. This is how you get "organizational construction" — multiple Lobster instances, or non-Lobster agents, operating against the same UoW registry.

The implementation gate for tier 1 and 2: A persistent agent session that doesn't terminate between tasks. Lobster already has session management infrastructure. A specialized agent is just a session with a domain-scoped bootup file and persistent context state.

The implementation gate for tier 3: A UoW claim protocol. An executor reads proposed UoWs, scores them against its capability profile, and claims if match exceeds threshold. The steward becomes a marketplace operator, not just a dispatcher.

The audacious version: WOS becomes a protocol, not an application. The UoW schema is the interface. Any agent that can read a UoW and write a result.json is a valid executor. Human contributors too — a human can claim a `human-judgment` register UoW, complete the work externally, and write the result. The system closes the loop regardless of who executed.

---

## IV. From Human-Mediated Improvement to Closed-Loop Self-Amendment

**The problem it solves:** When WOS fails or underperforms, the improvement cycle runs through Dan. He reads the surface message, adjusts a bootup file or IFTTT rule, and the system reruns. The learning is happening in Dan, not in the system. Autopoiesis requires the system to close the improvement loop on its own — at least for the class of failures it can diagnose without human judgment.

**What we gut:** The assumption that bootup file edits and IFTTT rule changes require human authorship. The current one-way surface → human adjusts → redeploy cycle.

**What the new shape is:**

A two-class improvement protocol:

**Class A — System-diagnosable failures** (infra kills, orphan loops, prescription recycling, register misroutes that match known patterns): the system amends its own configuration automatically.

- The corrective trace mechanism (V3 Change 2) already writes prescription_delta into the next cycle. **Extend this to IFTTT rules and routing configuration.**
- A new `self-amendment` component reads patterns from the verdict accumulator (from Evolution I above) and proposes specific changes: "IFTTT rule X has been overridden by human correction 4 times in the last 30 days — propose deletion." "Register classifier assigns `operational` to issues tagged `architecture` — this has a 70% steward-escalation rate, propose adding `architecture` as a `philosophical` pre-filter."
- Self-amendments for Class A are applied automatically, logged, and surfaced to Dan in a weekly digest. Dan can veto retroactively. This is human-in-the-loop at a higher level of abstraction — Dan reviews patterns, not individual changes.

**Class B — Judgment-requiring failures** (new task types, ethical edge cases, novel domain problems): surface to Dan as before. But surface with a *proposed amendment*, not just a failure report. "This failed 3 times. Here's what I think the structural cause is. Here's the IFTTT rule change I'd make if you approve."

The key shift: **Dan's role moves from author to reviewer**. He's no longer writing behavioral rules from scratch; he's approving or rejecting the system's proposed self-amendments. This is a faster feedback loop, a lower cognitive load, and — crucially — it makes the system's self-improvement capacity visible as an artifact Dan can inspect.

The SIA framing: this is the "harness update automation" lever applied to Lobster's bootup layer. SIA automates scaffold edits based on trajectory analysis. Lobster automates IFTTT rule and routing config edits based on verdict accumulation. The Feedback-Agent in SIA is the self-amendment component here.

The guard against Goodhart collapse: Class A amendments are bounded — they can only modify IFTTT rules and routing config, not the amendment logic itself. The amendment logic is Class B (requires Dan). This creates a structural asymmetry that prevents the system from rewriting the rules that govern its own rewriting.

---

## V. Net New: The Orientation Layer (What WOS Currently Has No Equivalent Of)

**The problem it solves:** WOS has a task layer (UoWs), an execution layer (subagents), and a learning layer (in progress, via the above evolutions). What it doesn't have is an *orientation layer* — a component that asks: "what should the system be working on, and why?"

Currently, the answer to "what should WOS work on?" is: whatever GitHub issues exist. That's not orientation — it's inbox management. A truly autopoietic system has a telos — an ongoing sense of what it's trying to become — and selects work that serves that telos, not just work that's available.

**What we gut:** The assumption that the GitHub backlog is the WOS backlog. The current cultivator model where issues arrive and the system processes them in order.

**What the new shape is:**

A **Governor** component that sits above the cultivator and steward:

- Reads the active workstreams, current system health, capability gaps, and Dan's stated priorities
- Generates a *portfolio prescription* — not which UoW to run next, but which *class* of work the system should emphasize over the next N cycles
- Translates the portfolio prescription into germination biases: issues with `workstream:X` are promoted to `pending` faster; issues with `workstream:Y` are deprioritized

This is not prioritization. It's orientation — the system developing a sense of its own trajectory and selecting work that serves that trajectory. The metabolic taxonomy (seeds, pearls, heat, shit) is the substrate: the governor looks at the metabolic output composition over the last 30 days and asks "is this system producing enough seeds? Are the pearls landing in the right workstreams? Is the heat-to-pearl ratio sustainable?"

The Socratic advisor pattern (hexo-ai/socrates, Signal 4 from the SIA synthesis) lives here, not in the steward loop. The governor's portfolio prescription is a philosophical-register act — it requires a question-only advisor that prevents premature closure on "what we should work on." A governor that can give itself answers will converge on whatever workstream has the most open issues. A governor paired with a Socratic advisor will be forced to justify its portfolio prescription against the actual vision.

This is the "autopoietic" leap Dan is pointing at. Not "the system runs tasks efficiently" but "the system has an ongoing sense of what it's trying to become and works toward that."

---

## Priority Ordering

If we could only do one: **Evolution I (Adaptive Steward)**. It's the minimum viable self-improvement loop — turns one-directional prescription into accumulating evidence. Everything else builds on having a system that tracks its own diagnostic accuracy.

If we're doing two: **Evolution IV (Closed-Loop Self-Amendment)**. Combined with I, the system can now both accumulate evidence and act on it — amending its own behavior within bounded authority.

If we're doing all: **Evolution II (Event-Native)** should run in parallel with I and IV — it's mostly an infrastructure refactor, not a deep architectural change, and it makes everything else faster and more alive.

**Evolution III (Executor Mesh)** is the organizational-scale leap. It's the "complex organizational constructions" node in Dan's critique. Do it last — it requires the learning infrastructure of I and IV to be in place, or the mesh accumulates execution capacity without accumulating wisdom.

**Evolution V (Orientation Layer)** is the poietic leap. It's also the riskiest — a governor that doesn't have good orientation can actively harm system coherence by pulling work toward the wrong things. Build it after the learning infrastructure can support it.

---

## What We Keep

- The register taxonomy (operational / philosophical / human-judgment / iterative-convergent). It's the right abstraction. The Adaptive Steward makes it empirically improvable.
- The oracle gate. Non-negotiable. Every structural leap above produces more code, not less.
- The heartbeat architecture as an orphan safety net.
- The metabolic output layer (seeds, pearls, heat, shit). It's the vocabulary for Evolution V.
- Dan's role as reviewer, not author, at the structural level. The goal is not to remove Dan — it's to move him up the abstraction stack.

---

*Synthesis complete. Five structural leaps, one priority ordering, one set of non-negotiables. No implementation schedules — this is the orientation document, not the sprint plan.*
