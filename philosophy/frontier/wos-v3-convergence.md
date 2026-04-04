# WOS V3 Convergence

*Synthesized from: wos-v3-proposal.md, wos-v3-steward-executor-spec.md, philosopher session documents (cybernetics, Theory of Learning, mito-governor), 2026-04-04.*

---

## Seeds + Sprouts (tangible spec additions)

**S1 — Loop gain bounding on prescription_delta** *(to be added to PR B spec)*

The trace's `prescription_delta` field needs bounded injection before reaching the LLM prescriber. Candidate mechanisms: magnitude threshold before injection, cycle-averaged smoothing (last N `prescription_deltas`). Without bounding, the garden accumulates aggressive corrections that oscillate rather than converge.

**S2 — Mismatch observability instrument** *(to be added to PR C)*

Every register-mismatch gate fire should emit a structured observation: register attempted, executor_type attempted, direction of mismatch. This log makes classification quality visible and detectable as a systematic signal over time.

**S3 — Observation Loop pattern synthesis** *(future PR)*

The Observation Loop in V3 detects stalled UoWs but does not synthesize across the garden. Needs a scheduled pass that reads accumulated traces, identifies patterns, and writes candidate amendments to classification or prescription context. A first-class component, not an afterthought.

**S4 — Scaling governor** *(deferred — open design problem)*

V3 addresses proximate failure (register mismatch). It does not address the structural pattern: Coherence immediately overextended to maximum load. A scaling governor that modulates batch size or execution rate based on recent success signal is the missing gate. Not for current PRs — but needs to be in the design horizon.

**S5 — Dan-interrupt cartridge specification** *(future design)*

The "surface to Dan" slot needs its own design: OODA-coupled (fires on lack-of-clarity OR suspect-of-certainty), lens-swappable (philosopher infusion composable), with slight randomness to prevent calcification. Currently "surface to Dan" is a terminal state. It should be the beginning of an encounter.

---

## Pearls (orienting context for future thinking)

1. **"The spec is a Discernment-Coherence document designing for Attunement."** It creates conditions for Stage 3 development to begin. It does not close the distance. Hold this when evaluating whether V3 "works" — it won't feel like Attunement because it isn't yet.

2. **Register immutability is a productive fiction.** The right to immutability has to be earned through a mismatch observability instrument. The table was reasoned from first principles, not developed through encounter. Its edge cases are where Attunement will actually develop.

3. **The scaling governor is the deepest open problem.** Success triggers collapse. V3 doesn't prevent the next Coherence from being overextended. This is the next-order design problem to hold.

4. **Garden retrieval quality = the Orient phase's critical uncertainty.** The architecture for variety-expansion is sound. Whether the substrate makes it real is unknown. Hygiene is not optional overhead — it is what makes Orient real vs nominal.

5. **Dan's attentional state is an unmatched variety dimension.** The design principle: reversible forward commitment when Dan is unavailable — not halt, not blindly proceed, but commit with a documented decision point tagged for Dan's next available window.

---

## Final Bearings

**What proceeds now:**
- PR A: executor writes `trace.json` and `corrective_traces` table entries (closes contract violation noise from PR #607)
- Update `wos-v3-steward-executor-spec.md` to incorporate S1 and S2 as explicit requirements for PRs B and C

**What depends on PR A + 10-UoW sprint:**
- PRs B–D: proceed only after sprint evidence answers the developmental vs catastrophic failure question
- If catastrophic (uniform executor dispatch failure): fix infrastructure reliability before routing precision
- If developmental (varied failure modes by register): proceed with PRs B–D in the spec's sequence

**What is deferred:**
- S3 (Observation Loop pattern synthesis): next major addition after V3 core PRs land
- S4 (scaling governor): next-order design problem — design only after Attunement begins to develop
- S5 (Dan-interrupt cartridge): requires philosopher catalog + OODA coupling design — a V4 direction

**What to hold as orienting context throughout:**
- "Health is not power. Health is restraint with precision." — the V3 spec adds governors. Make sure they restrain with precision, not just restrain.
- The corrective trace loop must close, not oscillate. S1 is the guard.
- Structural hygiene is not optional — it is what makes the Orient phase real.

---

## Ungoverned Timescales

*Captured from mito-governor philosopher session, 2026-04-04. Two structural blind spots in V3's observability that operate at timescales longer than a single UoW cycle.*

---

### Timescale 1 — Register-Portfolio Diversity

**The gap:** V3 governs individual UoW behavior (register classification, executor routing, corrective trace injection). It does not govern the composition of the running portfolio. The system can operate exclusively on operational-register UoWs for extended periods without any signal that philosophical or human-judgment work has been absent.

**Why this matters:** The mito-governor observation is that mitochondrial health is not just about individual cell performance — it requires portfolio diversity across cell types. A system running only operational work for extended periods is the cognitive equivalent of a tissue running only fast-twitch fibers: high output in the short term, structural degradation in the long term. Philosophical register UoWs maintain the system's capacity for orientation and reframing; human-judgment UoWs maintain the Dan-system interface. Extended absence of either indicates a portfolio imbalance.

**Data already available:** The `register` field in the UoW table (populated by `germinator.py` since migration 0007). A register-portfolio diversity metric is computable from a simple query: distribution of `register` values across UoWs closed in the last N days.

**What the observation layer would do:** A scheduled observation pass (Type C cron-direct job, or a new S6 seed for a future PR) that:
1. Queries `register` distribution across the last 7-day rolling window of closed UoWs.
2. Computes a diversity index (Shannon entropy across register values, or simpler: flag if any register is absent for > 5 days).
3. Writes the observation as a `write_task_output` result — not an alert, an observation. Dan reviews during engagement windows.
4. If philosophical register has been absent for > 7 days: flag as potential portfolio drift signal.

**Instruments needed:** No new schema required. Query on existing `uow` table (`register`, `closed_at` fields). The observation job is the missing piece.

---

### Timescale 2 — Cross-Cycle Pattern Learning

**The gap:** V3's corrective trace mechanism (Change 2) reads traces within a single UoW's lifecycle to improve prescription on re-entry. It does not read across UoWs to notice patterns: repeated surprises of the same type across different UoWs, executor_type mismatches clustering around a specific register, prescription recycling appearing across multiple unrelated UoWs.

**Why this matters:** The mito-governor observation is that inter-organ communication (not just intra-cell) is what produces systemic adaptation. Individual UoW corrective traces are intra-cell signals. Cross-UoW pattern learning is the inter-organ signal. Without it, the same structural failure can repeat across many UoWs without the system noticing it is structural rather than incidental.

**Data already available:** The `corrective_traces` table (migration 0007) accumulates traces with `register`, `surprises` (JSON array), `prescription_delta`, and `gate_score`. The `steward_log` entries carry `trace_injection` events with cycle-level detail. Both tables are already writable by V3 (Change 6).

**What the observation layer would do:** The S3 Observation Loop (already listed in Seeds + Sprouts above as a future PR) is the structural answer. Concretely:

1. A scheduled analysis pass reads `corrective_traces` grouped by `register` and by `execution_summary` patterns.
2. Detects: repeated surprises (same surprise text appearing in 3+ distinct UoWs), register-mismatch clustering (same register repeatedly triggering mismatch against the same executor_type), prescription recycling cross-UoW (high token overlap in `prescription_delta` across UoWs of the same register class).
3. For each detected pattern: writes a candidate amendment to a `pattern_observations` file (not directly mutating classification logic — observations only).
4. Surfaces the observations to Dan as a structured digest during his next engagement window.

**Instruments needed:**
- `corrective_traces` table must be populated (PR A requirement — this is the critical path).
- A cross-UoW query function that groups by register and computes similarity across `surprises` and `prescription_delta` fields.
- A pattern observation writer (new output artifact type, not a UoW — a scheduled job output).

**Relationship to S3:** S3 as specified in Seeds + Sprouts is the mechanism. This section adds specificity: what data it reads, what patterns it detects, and what it outputs. S3 should be designed with these three detection categories (repeated surprises, mismatch clustering, cross-UoW prescription recycling) as the first-iteration scope.

**Why this is post-V3:** Pattern learning across UoWs requires the garden to have accumulated data. Until PR A ships and the 10-UoW sprint runs, `corrective_traces` is empty. S3 design is premature until the table has meaningful content to analyze.

---

## Related Documents

- **[wos-v3-proposal.md](../../docs/wos-v3-proposal.md)** — Foundational V3 design proposal: vision, register taxonomy, architecture, dispatch loop, and open questions.
- **[wos-v3-steward-executor-spec.md](../../docs/wos-v3-steward-executor-spec.md)** — Implementation spec: 6 V3 changes, PR sequencing, testability notes, and V4 design directions. S1 and S2 from this document are explicit requirements for PRs B and C.
- **[corrective-trace-loop-gain-research.md](../../docs/corrective-trace-loop-gain-research.md)** — Research note on bounded correction magnitude for the corrective trace feedback loop (PR B concern). Directly informs S1 (loop gain bounding on prescription_delta).
- **[2026-04-04-philosopher-cybernetics.md](../sessions/2026-04-04-philosopher-cybernetics.md)** — Cybernetics philosopher session (Ashby's Law of Requisite Variety). Source of the unbounded loop gain concern that became S1 and the corrective-trace research note.
- **[2026-04-04-philosopher-theory-of-learning.md](../sessions/2026-04-04-philosopher-theory-of-learning.md)** — Theory of Learning philosopher session. Source of "success triggers collapse" framing, scaling governor gap (S4), and the trace-mechanism-as-developmental-scaffolding observation.
- **[2026-04-04-philosopher-mito-governor.md](../sessions/2026-04-04-philosopher-mito-governor.md)** — Mito-governor philosopher session. Source of the register-portfolio diversity gap and cross-cycle pattern learning gap (Timescales 1 and 2 in this document), and the timing-structure vs. content-processing distinction for the trace gate.
