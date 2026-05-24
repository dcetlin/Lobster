---
oracle_status: approved
oracle_pr: https://github.com/dcetlin/Lobster/pull/772
oracle_date: 2026-04-16
---

# System Metabolism

*Frontier document — living, evolves as the protocol matures.*

See also: [metabolic-juice.md](metabolic-juice.md) — pre-cadential aliveness and the compaction risk.

## The Frame

Every action the system takes produces something. The metabolic taxonomy is a classification of what that something is — not by task type, but by outcome. An oracle cycle can be a pearl (found a real bug) or heat (clean pass, nothing to surface). The same job, different result, different category.

This is not a grading system. It is a vocabulary for understanding resource flow — where tokens go, what they produce, and whether the system is accumulating waste it cannot process.

---

## Metabolic Taxonomy

The full taxonomy has eight distinct metabolic states (attractors), organized into in-process states, terminals, and threshold events. The taxonomy is structural, not metaphorical — each state has a specific causal role and specific transition conditions.

### In-Process States

**Juice** — Generative potential in motion, not yet resolved. The undifferentiated energy of a system that has begun moving but has not committed to a form.

**Transmutation** — Juice changing character through resistance registers. An in-process state, not a terminal — the resistance register (whether the load is generative or dissipative) determines which fork transmutation takes. The transmutation fork is the hormetic window operationalized: the range where Threshold-register load produces transmutation rather than shit-produced.

**Seeds** — Stable origination spec, ready for execution. A seed is not yet juice — it is a resolved form waiting to be activated. Seeds are intentional investment in future capability: cost now, compounding return later.

Examples: infra fixes, new instrumentation, tooling improvements, this document. Seeds are not immediately valuable — their value is the option they create.

Seeds rot if never germinated. A seed that stays a seed for six months is probably shit now.

### Terminal States

**Pearls** — Stabilized insight. Becomes canon. A pearl is what transmutation produces when it holds under load. Direct high-value output — the artifact deserves to exist and is immediately useful.

Examples: philosophy sessions that encode a new framework, bugs caught before production, analysis that feeds a decision, a diagnosis that changes behavior. Pearls are what the system is for.

**Heat** — Pure dissipation. Gone, no residue. Not harmful — heat is the cost of doing work at all. What transmutation produces when it breaks under load, and what seeds become when they decay without execution.

Examples: empty subagent calls, healthy self-checks that find nothing, compacted-without-capture context, oracle cycles that confirm green.

Heat is only a problem in excess. If most cycles are heat, the scheduling cadence is wrong.

### Threshold / Event States

**Void** — A threshold recognition event with two complementary accounts that describe the same moment from different observational positions.

**Structural account (maximum Threshold hysteresis):** Void follows when the Threshold register fires: the old configuration has ended, the new one has not formed. Void's irreversibility is the metabolic expression of the Threshold register's hysteresis (resistance-registers-lexicon, Tier 2 -> Hysteresis). The void state is the specific case where Threshold hysteresis is maximal — the pre-threshold configuration is not recoverable within the same cycle; restoration requires completing the generative arc (scaffold -> seed -> juice) before a new Threshold-eligible state is reached. The transformation was written permanently into the system (coupling the Threshold event to the Inscription register), so the return path cannot retrace the loading path. Not all Threshold events produce void: low-hysteresis Threshold events (e.g., tone onset at bow hair rosin threshold) allow return to sub-threshold behavior by load reversal alone.

**Generative account (seed-that-cannot-be-planted):** Void is also the generative face of the same moment — the raw arrival of juice processed into revelation of a new gap, with the beginnings of articulation to identify, with strong acknowledgement that something is here uncharted, but awareness that there IS something here. Void is "a seed that cannot be planted." This is the phenomenological face: pre-metabolic awareness of a new gap before the gap has edges. The void-seed is distinct from metabolic seeds (resolved origination specs) — the void-seed precedes even the seed stage, because the gap does not yet have the definition needed for planting.

**Why both accounts are needed:** The structural account describes the boundary conditions and return-path topology (cannot retrace loading path, requires new generative cycle) but leaves the internal structure of that cycle underspecified. The generative account names the steps of the mandatory forward arc. Each is incomplete without the other: structural explains why restoration requires a forward arc; generative specifies what that arc is.

**The void-event is three distinct steps, not one:**
1. **Perception (void-event):** the thing becomes visible that was not before — the raw arrival of a new gap; pre-metabolic awareness before the gap has edges
2. **Conceptual scaffold (void-seed):** a concept or word attaches to the perception — beginnings of articulation, strong acknowledgement that something is here uncharted, awareness that there IS something here. This is where the "seed that cannot be planted" lives. The void-seed is distinct from metabolic seeds — it is pre-metabolic, lacking the edges needed to be planted. This step is where most conceptual work stalls: not at recognition, but at volitional resolution.
3. **Seeding (volitional):** the named gap acquires edges sharp enough to become an origination spec — it becomes a metabolic seed, ready for execution.

Collapsing these three into "having an insight" loses the causal structure — specifically, it loses the volitional step (2->3), which is where stalls occur.

**Shit (revealed)** — Latent entropy surfaced by sweeps. The sweep is a sensor, not a metabolic engine — it surfaces shit that was already there, it does not produce it. Conflating the sensor with the source is a systemic error: it produces misattribution (punishing the sweep mechanism) and obscures actual origin.

**Shit (produced)** — Entropy generated by execution itself. Conflict markers, broken migrations, bad decisions — the metabolic byproduct of work that goes wrong in execution.

**Shit (accumulated)** — Organic waste that persists and must be processed. Unlike heat, shit does not disappear — it accumulates. Unprocessed shit becomes clutter, then debt.

Examples: session notes never distilled, memory-events.jsonl accumulation, stale open issue backlog, unread thread accumulation, frontier docs that were filed but never referenced.

Shit has two processing paths:

**Compost path** — low-intensity extraction pass. Crystallize pearls, learnings, and seeds into the canonical layer. Kill the raw source. This is what nightly consolidation does: take the raw shit of daily session notes and extract whatever is worth keeping.

**Evisceration path** — if composting finds nothing of value, deliberately eliminate. Do not let empty artifacts persist. The test: does this feed a decision, hold a seed, or encode a learning? If not, eviscerate.

### Transition Graph

```
juice -> transmutation -> [holds under load] -> pearl -> canon
juice -> transmutation -> [breaks under load] -> shit-produced -> heat
juice -> [sweeps] -> shit-revealed -> [addressed] -> seed OR heat
juice -> [recognition] -> void-event -> [named] -> conceptual scaffold (void-seed) -> [volitional] -> seed
seed -> execution -> juice   [cycle closes -- seeds re-generate juice]
pearl -> canon              [terminal]
heat                        [terminal, no residue]
```

**Seed -> juice closes the generative cycle.** Seeds are not endpoints. A seed that executes produces juice — generative potential in motion again. The cycle is closed, not linear. Systems that treat seeds as final outputs accumulate seed-debt: potential energy that never re-enters the generative loop.

### Category Transitions

Categories are not permanent. An artifact's classification reflects its current role, not its origin.

**Seed -> Pearl**: occurs when the artifact is actively embedded in the runtime decision path — included in bootup context, cited in a running agent's working context, or actively driving dispatcher behavior. The trigger is *operational reference*, not mere existence. A frontier doc that sits in the filesystem is a seed; the same doc injected into bootup is a pearl.

**Pearl -> Shit**: occurs when the pearl is superseded and no longer the active source of truth — a doc replaced by a newer version, a decision overridden, a canonical entry that has gone stale. Superseded pearls that are not eviscerated become clutter.

**Seed -> Shit (rot)**: occurs when a seed has not been germinated in 60-90 days and is no longer viable. See also: Open Questions below, where this threshold is discussed. Hygiene sweeps should flag old seeds for human review.

---

## Grounding Table

| Concept | Lobster structure / process |
|---|---|
| Heat | Empty subagent completions, healthy health-check passes, oracle cycles that find nothing |
| Void | Post-Threshold liminal state: gap recognized but not yet seeded; pre-metabolic |
| Shit (raw) | memory-events.jsonl, unread session files in philosophy/sessions/, stale GitHub issues, unacted hygiene findings |
| Compost path | Nightly consolidation job (extracts from session notes -> canonical memory), philosophy harvest job |
| Evisceration path | Hygiene sweep (issues opened but never resolved), manual evisceration on stale frontier docs |
| Seeds | Infra PRs, new MCP tools, flamegraph Tier 2/3 work, this document |
| Pearls | philosophy/frontier/ docs actively referenced in bootup *(seed until bootup-embedded; see Category Transitions)*, bugs caught by oracle, canonical memory entries the dispatcher reads |
| Accumulation threshold | Shit backlog growing faster than composting throughput -> escalate |

---

## The Glymphatic Frame

The brain clears metabolic waste (beta-amyloid, etc.) primarily during sleep — low-activity states when clearance mechanisms can run without interference from active processing. When sleep is insufficient, waste accumulates faster than clearance, and cognitive function degrades.

Lobster has an analogous structure:

- **Clearance states**: context compaction events, nightly consolidation runs, hygiene sweeps. These are the system's sleep. They process shit that accumulated during active operation.
- **Context debt**: when clearance is insufficient, session notes pile up, memory-events.jsonl grows unbounded, and the context window fills with stale material rather than canonical signal. This is beta-amyloid accumulation — not acutely harmful, but degrading over time.
- **Compaction as sleep**: a compaction event is not just a technical reset. It is a clearance event. The question after compaction is: what was extracted before sleep? If the answer is "nothing," the system ran through a sleep cycle without clearing waste.

The implication: clearance jobs (nightly consolidation, hygiene sweeps) are not optional maintenance. They are the mechanism that keeps the system from accumulating debt that degrades future performance.

---

## Embedding Points

Where this taxonomy should appear in the system:

**write_result** — Optional `outcome_category` field: `heat | shit | seed | pearl`. Self-assessed by the completing subagent, stored in the ledger alongside token counts. Enables flamegraph second axis. See issue #754.

**jobs.json** — Each scheduled job can carry an expected outcome category. A job that consistently produces heat when it was expected to produce pearls is a scheduling problem.

**Flamegraph** — Tier 2: token spend x outcome_category. Tier 3: budget gate signal derived from heat% exceeding threshold.

**OODA protocol** — Waste-state (shit accumulation rate vs. composting throughput) as a formal O1 signal. If backlog is growing, the O phase surfaces it; O phase orients on whether it is normal; D phase decides whether to adjust cadence; A phase throttles, eviscerate, or reschedules. See companion issue for OODA integration.

**Hygiene sweeps** — Sweeps that find unprocessed shit should tag it as compost candidates or evisceration candidates, not just surface them as findings.

**Nightly consolidation** — The canonical compost path. Should log: how many items processed, how many pearls/seeds extracted, how many items eviscerated.

---

## The Artifact Deserving-to-Exist Test

Before filing or persisting anything, ask: does this artifact

1. Feed a decision (now or later)?
2. Hold a seed (future capability)?
3. Encode a learning (will be referenced in bootup or canonical memory)?

If none of the above, it is already shit. Either compost it immediately (extract whatever is worth keeping) or eviscerate it. Filing it as a frontier doc or session note is not a neutral action — it is a deferral that creates future composting debt.

---

## Open Questions

**What triggers composting vs. evisceration?**

Current working answer: try compost first. If the extraction pass finds nothing that meets the pearl/seed bar, eviscerate. The compost pass is not expensive — it is a short LLM scan. Evisceration without a compost pass risks discarding latent value.

Candidate threshold: if the artifact is older than 30 days and has not been referenced, it is probably safe to eviscerate without a compost pass. Age + non-reference is a strong signal that no decision is downstream of it.

**What is the accumulation threshold that escalates?**

Open. Candidate signals:
- memory-events.jsonl line count growth rate
- number of hygiene findings not acted on within N days
- ratio of heat cycles to pearl/seed cycles over a rolling window

The escalation action is not yet defined. Options: alert Dan, throttle low-value job cadences, force a consolidation run, pause new capability work until hygiene debt clears.

**When does a seed rot into shit?**

A seed that has not been germinated in 60-90 days is probably no longer viable. Candidate protocol: flag old seeds in hygiene sweeps for human review — Dan decides whether to germinate or eviscerate.

---

*Last updated: 2026-05-24 (council deliberation: metabolic-taxonomy void promotion)*
