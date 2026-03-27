# Dialectic Design Protocol (DDP)

*A structured pipeline for design questions that have genuine philosophical load — not tactical choices, but questions about what the system fundamentally should be or do.*

*First instance: [Issue #194 — Philosophy pipeline: multi-register coupling and behavioral gate architecture](https://github.com/dcetlin/Lobster/issues/194)*

---

## When to invoke

Invoke DDP when:
- A design question cannot be resolved by checking the spec — it requires generating the answer from first principles
- The question has philosophical depth (epistemics, systems architecture, cognitive models)
- Multiple reasonable approaches exist and the choice between them is load-bearing
- A foundational dissonance has been identified and needs structured resolution

Do NOT invoke for:
- Tactical choices with clear existing constraints
- Implementation details of an already-settled design
- Questions answerable by reading the codebase

---

## The 5 stages

**Stage 1 — Philosophy exploration**
*Input:* The foundational dissonance, clearly stated.
*Process:* Broad, associative exploration. First principles, cognitive science, systems theory, Dan's epistemic frameworks. No solutions yet.
*Output:* Written exploration (~1000 words) surfacing conceptual dimensions of the problem.
*Handoff:* Creates Stage 2 GitHub issue with Stage 1 output as context.
*Agent:* lobster-meta

**Stage 2 — First-principles diverge**
*Input:* Stage 1 output + core question restated.
*Process:* Generate 5-8 distinct approaches without evaluation. Span architectural, instructional, epistemic, and hybrid quadrants.
*Output:* Numbered list of approaches with brief descriptions. No judgment.
*Handoff:* Creates Stage 3 GitHub issue.

**Stage 3 — First converge**
*Input:* Stage 2 diverge output.
*Process:* Evaluate approaches. What's load-bearing in each? What are the decision points? What's the ranking and why?
*Output:* Ranked synthesis with explicit reasoning.
*Handoff:* Creates Stage 4 GitHub issue.

**Stage 4 — Second diverge / battle-test**
*Input:* Stage 3 converge output.
*Process:* Adversarial stress-test. What premises is the top approach resting on? Are they solid? What vocabulary is doing hidden work? What failure modes exist?
*Output:* Adversarial review — identifies what's fragile, what's load-bearing, what's underspecified.
*Handoff:* Creates Stage 5 GitHub issue.

**Stage 5 — Final converge**
*Input:* All previous stages.
*Process:* Produce the actionable brief. Concrete changes, their layer (advisory vs. structural), sequencing.
*Output:* Course of action document — the downstream artifact that informs what gets built.
*Handoff:* Delivered to Dan.

---

## Sequencing guarantees

Each stage creates the next stage's GitHub issue as its final act. The dispatcher picks up the issue when it arrives and spawns the next agent. This provides:
- Sequential execution (no stage runs until the previous is complete)
- Persistent state (each stage's output is in the GitHub issue context)
- Failure visibility (if a stage stalls, the next issue is never created)

Current limitation: no automatic failure recovery. If a stage crashes, the pipeline stalls. Structural pipeline primitives (WOS Phase 2+) will eventually provide recovery guarantees.

---

## Naming convention

Pipeline tracking issues should be titled: `Philosophy pipeline: [topic]`
Stage output files: `~/lobster-workspace/philosophy/pipeline-[topic]-stage[N].md`
