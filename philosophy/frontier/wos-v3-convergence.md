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
