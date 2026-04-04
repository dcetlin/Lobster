# WOS Orchestration Landscape

*Status: Active — 2026-04-04*
*Purpose: Survey of existing agentic orchestration frameworks and their relevance to WOS design.*

---

## Summary

The field has converged on a set of structural patterns for long-running agentic work: task queues, orchestrator/executor splits, and external state persistence. What almost no system handles well is the WOS problem: **mixed work types with ontologically distinct completion criteria**. Most frameworks treat "done" as a single category (tests pass, objective met, task list exhausted). WOS's register taxonomy addresses a gap that the broader field has either not recognized or not published solutions for.

---

## Framework Survey

### BabyAGI

**What it is:** Task-queue agent that decomposes a high-level objective into subtasks, executes each, and uses the result to reprioritize and generate new tasks. One of the original open-source autonomous agent implementations (2023).

**How it handles done:** The loop terminates when the task queue is exhausted. There is no external success criterion — "done" is "no tasks remain." Later versions (BabyAGI 2, 3) added a reflection step that could add or remove tasks from the queue, but the termination condition is still queue-emptiness.

**Relevance to WOS:** BabyAGI's task-queue pattern is essentially the UoWRegistry without a Register taxonomy. The missing piece: BabyAGI cannot distinguish between "this task is done because tests pass" and "this task is done because I exhausted the queue." WOS's `success_criteria` + gate command is the principled answer to BabyAGI's implicit termination logic.

**Session durability:** Minimal. BabyAGI 3 added DB persistence for skills, but task state is in-memory within a session. Crash = restart from objective.

---

### AutoGPT

**What it is:** Autonomous agent that decomposes a high-level goal into subtasks, maintains a planning loop, and uses vector memory for persistence. Evolved from experimental project (2023) into production-ready platform (Forge, 2025).

**How it handles done:** AutoGPT's primary completion mechanism is goal satisfaction as assessed by the model itself — no external gate. The model declares the objective met. Production deployments configure external vector databases for session persistence, but the done-condition remains model-asserted.

**Relevance to WOS:** AutoGPT's evolution from experimental to production-ready illustrates exactly the problem WOS is designed to solve. The system "works" in a narrow sense (tasks complete) but has no mechanism to distinguish completion from abandonment. The 0.8% success rate in WOS V2's overnight run is the same failure mode: the executor declared done without a verifiable gate.

**Session durability:** Production AutoGPT uses external vector DB + checkpointing. State can survive session boundaries. This is the right infrastructure pattern — WOS uses SQLite for the same reason.

---

### OpenDevin / OpenHands

**What it is:** Agentic platform for software engineering tasks — code, command line, web navigation. Evaluated on SWE-Bench (real-world GitHub issues). Achieves ~21% solve rate on SWE-Bench Lite.

**How it handles done:** External test-based verification. The agent's output is evaluated by running the repository's existing unit tests. Done = tests pass. The agent also uses a countdown mechanism (borrowed from MINT) that applies time pressure to prevent infinite loops.

**Relevance to WOS:** OpenDevin's test-based done condition is the canonical example of WOS's "operational register" gate command. The insight that matters: OpenDevin works because its task domain is inherently machine-verifiable. It would not work on philosophical UoWs — and it makes no attempt to handle them. The register distinction WOS formalizes is implicitly present in how OpenDevin scopes itself.

**Session durability:** OpenDevin operates within a single session context. Long-term memory across sessions is not maintained as of mid-2025. The agent is fast and stateless — the opposite of WOS's durability requirements.

---

### Cognition Devin

**What it is:** Commercial AI software engineer from Cognition AI. Handles multi-step engineering tasks autonomously, with planning, code writing, testing. 67% PR merge rate in 2025 (up from 34% in 2024).

**How it handles done:** Structured task handoff: the user reviews a preliminary plan before execution begins, then Devin works autonomously. The implicit done condition is PR merged (verifiable) or user acceptance.

**Relevance to WOS:** Devin's design pattern maps directly to WOS's human-judgment register: plan proposed → human reviews and confirms → autonomous execution → verifiable output. The insight: Devin's success (67% merge rate) comes from explicit human judgment at the plan stage, not from autonomous done-detection. WOS formalizes this as a register — Devin operationalizes it in product design.

**Session durability:** Single-session bounded. Devin does not maintain memory across sessions as of mid-2025.

---

### SWE-Bench / AgentBench (Evaluation Frameworks)

**What they are:** Benchmarks for evaluating LLM-as-agent performance. SWE-Bench: 500 real-world GitHub issues, success = unit tests pass in isolated Docker container. AgentBench: 8 diverse environments, tasks across OS, DB, web, game domains.

**How they define done:** Exclusively machine-observable. SWE-Bench: unit tests pass. AgentBench: environment-specific completion criteria (e.g., correct DB query result, task objective in game environment).

**Relevance to WOS:** These benchmarks are the field's best answer to "what does verifiable done look like?" — and they reveal the field's blind spot. Every benchmark is machine-verifiable by construction. No benchmark evaluates whether an agent correctly identified that a task requires human judgment before proceeding. The philosophical and human-judgment registers have no benchmark analog. This is a genuine gap.

**Design implication:** WOS's register taxonomy fills a space that the current evaluation framework landscape does not address. If WOS were evaluated on SWE-Bench-style criteria, the philosophical and human-judgment registers would look like failures (no machine gate passes). The benchmark would be measuring the wrong thing.

---

### RALPH Loop / OODA-Style Agents

**What it is:** Agent execution pattern: Reflect, Act, Learn, Plan, Halt. Variants appear in BabyAGI reflection step, OpenDevin's MINT countdown, and WOS V3's OODA formalization. Not a single framework — a convergent pattern.

**How it handles done:** The RALPH/OODA pattern produces a natural halt condition when the Orient phase determines that success criteria are met. But "halt" requires an explicit trigger — OODA without a done-condition is an infinite loop.

**Relevance to WOS:** WOS V3's dispatch loop is OODA instantiated. The relevant insight: the Orient step (Steward's diagnosis) is the schwerpunkt. All downstream decisions — prescribe, surface to Dan, declare done — depend on the quality of orientation. This is why the Steward reads corrective traces and garden context before diagnosing: more evidence at Orient time improves all subsequent decisions.

---

### Production Agentic Orchestration Patterns (Azure, Google Cloud, Confluent)

**What they are:** Enterprise guidance on multi-agent orchestration. Key convergent recommendations: persist shared state externally (SQLite, PostgreSQL, Redis); use checkpointing for crash recovery; orchestrator owns goal lifecycle, workers own well-defined subtasks.

**How they handle done:** External state + checkpoint verification. "Done" is detectable from persisted state, not from in-memory assertion.

**Relevance to WOS:** The infrastructure pattern is identical to WOS (external DB, crash-safe state machine). What's absent in enterprise guidance: any treatment of mixed work types or register-aware routing. The frameworks assume homogeneous task types and machine-verifiable completion throughout. WOS's register taxonomy is solving a problem the enterprise frameworks assume away.

---

## Key Contrasts: WOS vs. Field

| Dimension | Field (typical) | WOS |
|-----------|----------------|-----|
| Done condition | Queue empty, model-asserted, or tests pass | Register-matched: machine gate for operational, human surface for philosophical |
| Mixed work types | Not handled — assumed homogeneous | First-class: register taxonomy + routing |
| Session durability | External DB in production systems | SQLite UoWRegistry from V1 |
| Completion criterion | Implicit or test-based | Explicit `success_criteria` at germination, immutable |
| Learning mechanism | Retraining or vector memory | Corrective traces accumulating in garden |
| Human judgment trigger | User approval step (Devin model) | Structural: human-judgment register always surfaces; plus 3 Steward-triggered conditions |

---

## Most Relevant Insights for WOS V3

1. **The done-condition gap is real and unaddressed.** Every major framework either uses machine-verifiable gates (SWE-Bench, OpenDevin) or model-asserted completion (AutoGPT, BabyAGI). None handles the philosophical/human-judgment case structurally. WOS V3's register taxonomy is a genuine contribution to the design space.

2. **Session durability requires external state from day one.** Devin and BabyAGI both suffer from session-bounded memory. Production AutoGPT and the enterprise patterns converge on external DB + checkpointing — exactly what WOS V1 built with SQLite. The WOS architecture was ahead of the experimental curve on this.

3. **The orient step is the leverage point.** OODA analysis confirms that downstream decision quality is bounded by orientation quality. WOS V3's corrective traces feeding the Steward's garden context at diagnosis time is the correct architectural response — not a feature, a structural requirement.

4. **Benchmarks measure the wrong thing for mixed-register work.** SWE-Bench-style evaluation selects for machine-verifiable completion and invisibilizes the philosophical and human-judgment registers. Any evaluation of WOS must be register-aware to be valid.

---

## See Also

- [wos-v3-proposal.md](wos-v3-proposal.md) — V3 design: register taxonomy, corrective traces, OODA loop
- [wos-vision.md](wos-vision.md) — core premises and vocabulary
