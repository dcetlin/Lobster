---
oracle_status: approved
oracle_pr: https://github.com/dcetlin/Lobster/pull/772
oracle_date: 2026-04-16
---

# System Metabolism

*Frontier document — living, evolves as the protocol matures.*

## The Frame

Every action the system takes produces something. The metabolic taxonomy is a classification of what that something is — not by task type, but by outcome. An oracle cycle can be a pearl (found a real bug) or heat (clean pass, nothing to surface). The same job, different result, different category.

This is not a grading system. It is a vocabulary for understanding resource flow — where tokens go, what they produce, and whether the system is accumulating waste it cannot process.

---

## Metabolic Taxonomy

### Heat

Pure dissipation. Gone, no residue. Not harmful — heat is the cost of doing work at all.

Examples: empty subagent calls, healthy self-checks that find nothing, compacted-without-capture context, oracle cycles that confirm green.

Heat is only a problem in excess. If most cycles are heat, the scheduling cadence is wrong.

### Shit

Organic waste that persists and must be processed. Unlike heat, shit does not disappear — it accumulates. Unprocessed shit becomes clutter, then debt.

Examples: session notes never distilled, memory-events.jsonl accumulation, stale open issue backlog, unread thread accumulation, frontier docs that were filed but never referenced.

Shit has two processing paths:

**Compost path** — low-intensity extraction pass. Crystallize pearls, learnings, and seeds into the canonical layer. Kill the raw source. This is what nightly consolidation does: take the raw shit of daily session notes and extract whatever is worth keeping.

**Evisceration path** — if composting finds nothing of value, deliberately eliminate. Do not let empty artifacts persist. The test: does this feed a decision, hold a seed, or encode a learning? If not, eviscerate.

### Seeds

Intentional investment in future capability. Cost now, compounding return later.

Examples: infra fixes, new instrumentation, tooling improvements, this document. Seeds are not immediately valuable — their value is the option they create.

Seeds rot if never germinated. A seed that stays a seed for six months is probably shit now.

### Pearls

Direct high-value output. The artifact deserves to exist and is immediately useful.

Examples: philosophy sessions that encode a new framework, bugs caught before production, analysis that feeds a decision, a diagnosis that changes behavior. Pearls are what the system is for.

<<<<<<< Updated upstream
### Category Transitions

Categories are not permanent. An artifact's classification reflects its current role, not its origin.

**Seed → Pearl**: occurs when the artifact is actively embedded in the runtime decision path — included in bootup context, cited in a running agent's working context, or actively driving dispatcher behavior. The trigger is *operational reference*, not mere existence. A frontier doc that sits in the filesystem is a seed; the same doc injected into bootup is a pearl.

**Pearl → Shit**: occurs when the pearl is superseded and no longer the active source of truth — a doc replaced by a newer version, a decision overridden, a canonical entry that has gone stale. Superseded pearls that are not eviscerated become clutter.

**Seed → Shit (rot)**: occurs when a seed has not been germinated in 60-90 days and is no longer viable. See also: Open Questions below, where this threshold is discussed. Hygiene sweeps should flag old seeds for human review.

=======
>>>>>>> Stashed changes
---

## Grounding Table

| Concept | Lobster structure / process |
|---|---|
| Heat | Empty subagent completions, healthy health-check passes, oracle cycles that find nothing |
| Shit (raw) | memory-events.jsonl, unread session files in philosophy/sessions/, stale GitHub issues, unacted hygiene findings |
| Compost path | Nightly consolidation job (extracts from session notes → canonical memory), philosophy harvest job |
| Evisceration path | Hygiene sweep (issues opened but never resolved), manual evisceration on stale frontier docs |
| Seeds | Infra PRs, new MCP tools, flamegraph Tier 2/3 work, this document |
<<<<<<< Updated upstream
| Pearls | philosophy/frontier/ docs actively referenced in bootup *(seed until bootup-embedded; see Category Transitions)*, bugs caught by oracle, canonical memory entries the dispatcher reads |
=======
| Pearls | philosophy/frontier/ docs actively referenced in bootup, bugs caught by oracle, canonical memory entries the dispatcher reads |
>>>>>>> Stashed changes
| Accumulation threshold | Shit backlog growing faster than composting throughput → escalate |

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

**Flamegraph** — Tier 2: token spend × outcome_category. Tier 3: budget gate signal derived from heat% exceeding threshold.

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

*Last updated: 2026-04-15*
