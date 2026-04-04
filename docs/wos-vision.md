# WOS Vision and Premises

*Status: Active — 2026-04-04*

---

## Vision

In full flourishing, WOS is the asymmetry flip: Dan never audits steps, only outcomes. Seeds arrive from any source — philosophy session, voice note, nightly sweep, Telegram message — and move through classification, registration, execution, and closure without requiring Dan's attention at each transition. The system surfaces to Dan only when a decision genuinely requires human judgment: a philosophical UoW that can't be machine-evaluated, a gate that has failed three cycles, a register mismatch that only Dan can resolve. Everything else closes on its own. The measure of success is not the activity metric (seeds filed, UoWs created) but the throughput metric: how many seeds completed their full arc from observation to verified closure. A system where the archive grows faster than work closes is not functioning. A system where Dan reviews outcomes rather than steps is.

---

## Core Premises

**Why WOS exists:**
- Conversations are non-convergent. A conversation that surfaces a problem, files a GitHub issue, and then stalls has discharged its surface energy without converting it to kinetic change. WOS is the mechanism that converts observations into closed loops.
- The failure mode being interrupted is specific: the activity metric (issues added) diverges from the outcome metric (work closed). The system appears busy while delivering nothing.
- Durable tracking that survives session boundaries is prerequisite, not optional. An agent that carries task state in-context loses it on session end. The UoWRegistry is the crash-safe substrate: it exists outside any session, and work in it does not disappear when context is lost.
- "Done" must be demonstrable, not just reportable. A UoW that transitions to done without a written, verifiable artifact is not done — it is asserted. The result.json contract and gate commands enforce this.

**What makes WOS structurally different from a task list:**
- Different registers of work require different evaluation paths. A bug fix is done when tests pass. A philosophy session output is done when Dan says so. Routing both through the same completion-evaluation logic produces category errors. Register classification is the mechanism that prevents this.
- Work must have a consumption gate, not just an accumulation gate. A sweeper that creates UoWs nobody picks up is not functioning — it is accumulating. Every pipeline phase must specify how downstream consumption is verified.
- Autonomy increases are explicit gate crossings, not side effects. The system begins in propose-only mode. Each increase in autonomy is a named decision point, not an emergent behavior.

**What the system amplifies, and what it doesn't replace:**
- The system amplifies Dan's attention: it presents work that is ready for human judgment at the moment judgment is needed, with maximum context density.
- The system does not replace Dan's judgment: philosophical and human-judgment UoWs always surface to Dan. The Steward cannot declare them done.
- The system is a living argument for what Dan is building toward. Its own structure — patient with individual seeds, reliable at the whole level — embodies the metabolism metaphor.

---

## Vocabulary (Brief Glossary)

**UoW (Unit of Work)** — The atomic unit of tracked, auditable work. Every piece of work in the execution substrate is a UoW. Has a state, audit trail, and closure condition.

**Register** — The attentional configuration a UoW requires for correct completion evaluation. Not tone or complexity — the category of evaluation: operational, iterative-convergent, philosophical, or human-judgment. Register mismatch produces completion failure even when execution succeeds.

**Steward** — The diagnosis-and-prescription agent. Reads each UoW, determines what it needs, prescribes the appropriate execution context. Evaluates completeness on re-entry. Does not execute work.

**Cultivator** — The philosophy pipeline's classification agent. Runs after a philosophy session and distinguishes pearls (recognition events) from seeds (executable work). Routes each to the appropriate path.

**Corrective trace** — A structured artifact written by the Executor on every return — complete, partial, or failed. Captures surprises, prescription delta, and gate score. Accumulates in the garden. The Steward reads these at diagnosis time. The learning mechanism that doesn't require a training loop.

**Pearl** — A philosophy session output that is a recognition event, not an action item. Already complete. Routes to the write-path (frontier docs, bootup candidates) rather than the UoWRegistry.

**Seed** — Unclassified potential. An idea, observation, or open question that may become executable work. Not yet in the UoWRegistry.

**Garden** — The living knowledge layer. Pearls, corrective traces, and attunement records. Circulate via re-encounter rather than re-execution.

**Germination** — The classification event at which a seed's output type is resolved and it becomes a GitHub issue. The moment a seed enters the formal pipeline.

**Gate command** — A machine-executable command that verifies completion without human reading. Required for operational and iterative-convergent UoWs.

**Success criteria** — A human-readable statement of what completion looks like, written at germination and immutable. The Steward evaluates every re-entry against this anchor.

---

## See Also

- [wos-v2-design.md](wos-v2-design.md) — full V2 specification: Steward/Executor loop, lifecycle, actors
- [wos-v3-proposal.md](wos-v3-proposal.md) — V3 proposal: register taxonomy, corrective traces, OODA loop
- [wos-constitution.md](wos-constitution.md) — founding metaphor and naming constraints
- [wos-golden-pattern.md](wos-golden-pattern.md) — canonical Python patterns for WOS implementation
- [WOS-INDEX.md](WOS-INDEX.md) — doc ecosystem map and reading order
