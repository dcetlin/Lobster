# WOS Constitution: The Founding Metaphor

*Status: Active — 2026-03-31*

---

## The Metaphor as Constraint

The biological naming of this system is not decoration. It is not a coat of paint applied after the design was finished. It is the design.

The garden is patient. The steward is wise. The executor is obedient. The caretaker is continuous. These are not personality labels — they are design commitments with teeth. They constrain what proposals are admissible.

When a proposal makes the steward impatient — prescribing work before evaluating it, skipping diagnosis to save time — it violates the name. Stewards do not rush. When a proposal makes the caretaker transactional — running once a day, or only on demand — it violates the name. Caretakers do not batch. When a proposal makes the garden urgent — forcing seeds to germinate before they are ready — it violates the name. Gardens do not hurry.

This is the keeper-of-names function: when a design decision is unclear, ask what the name demands. The name is not a suggestion. It is the constitution.

---

## Pre-Metabolic, Not Broken

The system is currently pre-metabolic. This is an important distinction. "Pre-metabolic" means the pipeline exists and is correctly designed, but the organism has not yet begun to breathe. "Broken" would mean something is wrong with the design. Nothing is wrong with the design.

188 seeds exist. The registry is populated. The Steward/Executor loop is implemented. But `execution_enabled=false`, and GardenCaretaker has not been built, so nothing flows. The system is a circulatory architecture waiting for a heartbeat.

Three things make it real:

First, build GardenCaretaker. This is the heartbeat component — the continuous process that scans the registry, advances lifecycle state, and ensures nothing goes stale. Without it, seeds do not move. They sit correctly classified in a correctly structured registry, going nowhere.

Second, enable execution. Flip `wos start` and let the Executor run actual work. This is a deliberate gate — it exists because running real work has real consequences, and the system should not run until the operator is ready to observe it.

Third, observe throughput. The cycle log and morning briefing reveal the actual seed-to-done rate. This is the metric that matters: not how many issues were filed, but how many seeds completed their arc. The divergence between those two numbers is the problem WOS was built to close.

---

## The Tension That Isn't a Flaw

The system holds a tension: it is simultaneously a patient garden and a systematic pipeline. This looks like a contradiction. It is not.

WOS is patient with individual units of work — seeds move at their own pace, mature when ready, and are not hurried by the system. But WOS is systematic at the whole level: the caretaker runs on schedule, the steward evaluates consistently, the executor follows prescriptions. No individual seed is rushed. The overall process is reliable and rhythmic.

This is metabolism. An organism does not rush individual biochemical reactions. But the overall metabolic process is not optional, not occasional, and not bursty. It runs continuously because the organism must run continuously. WOS borrows exactly this structure: patience at the granular level, reliability at the system level.

The tension is the design. Do not resolve it.

---

## The Pearl Bypass

Not everything that enters WOS is a seed. Some things are already done — recognition events, not execution events. A philosophy session that produced a settled frontier document. A design conversation that resolved a question permanently. These are pearls.

If pearls are routed through the seed pipeline, the pipeline breaks. There is nothing to prescribe — the work is already complete. The Steward cannot diagnose something that needs no treatment. Forcing pearls through germination is like asking a gardener to plant a fully grown tree.

Pearls need a bypass route: a write-path that takes them directly to wherever their output belongs (frontier docs, bootup candidates, the archive) without touching the UoWRegistry. This is a known design gap. The concept is named; the formal implementation is not yet built. The constitution records it here because every mature system must know the shape of its own incomplete edges.

---

## The Dual Register

This system lives in two naming registers simultaneously, and both are always active.

The biological register — seeds, garden, pearls, harvest, germination — is for design conversations, philosophy sessions, and status reports. It conveys the system's essential character: that work is living, that timing matters, that not everything grows on the same schedule.

The operational register — UoW, GardenCaretaker, Steward, Executor, UoWRegistry — is for code, logs, and error messages. It prioritizes precision over evocativeness, because when a log line says `executor_failed` you need to know exactly what failed.

Neither register colonizes the other. A design document that says "the Cultivator classifies philosophy session outputs as pearls or seeds before they enter the UoWRegistry" is using both registers correctly in the same sentence. The biological term names the concept; the operational term names the substrate. They are not competing — they are complementary lenses on the same system at different altitudes.

---

The names are the constitution. When in doubt, ask what the name demands.
