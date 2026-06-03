---
oracle_status: approved
oracle_pr: https://github.com/dcetlin/Lobster/pull/1403
oracle_date: 2026-06-03
---

# Frontier: Metabolic Juice — Pre-Cadential Aliveness

*Domain: The generative potential that precedes crystallization into metabolic closure states*

*Last updated: 2026-04-23*

## Related Frontier Docs

- [system-metabolism.md](system-metabolism.md) — parent doc on metabolic taxonomy (seeds, pearls, heat, shit)
- [collapse-topology.md](collapse-topology.md) — the structure of how inquiry collapses from open to closed; what is preserved and lost at each transition (juice is what lives in the pre-collapse zone)
- [poiesis-poiema.md](poiesis-poiema.md) — the making-relation that juice requires; juice is in a poietic orientation, not a production one
- [orient.md](orient.md) — the observation→decision coupling that keeps juice alive rather than letting it degrade into heat

---

## What Juice Is

The metabolic taxonomy — seeds, pearls, heat, shit — describes closure states. Each one is a point where something has resolved: a seed has an address (a GitHub issue URL), a pearl has landed (the thing exists and is useful), heat is dissipated energy, shit is organic waste awaiting processing. These are moments of metabolic resolution.

Juice is what exists before any of that happens.

A steward prescription that senses a productive direction but cannot yet specify which UoW to dispatch is carrying juice. A philosophy-explore session that ends with real momentum — the material is pointing somewhere — but has not yet encoded a golden pattern is carrying juice. A thread in a conversation where something is clearly alive and moving but has not crystallized into a form that can be handed off: juice.

The defining quality is generative aliveness without determinate form. Juice has not yet collapsed into a specific address. It knows it is going somewhere without knowing what it will become.

This is what makes it distinct from each of the four closure states:

Heat is spent energy — the work happened but left nothing behind. Juice is un-spent. The energy is still present, still live, still generative.

Seeds are crystallized futures — they have an address, a named deliverable, a GitHub issue waiting to be picked up. Juice does not yet know what it will become. It cannot be filed because it does not yet have a form that filing can receive.

Pearls have already landed. Juice has not landed yet.

Shit is accumulated residue awaiting processing. Juice is still in motion.

---

## The Cadence Analogy

Seeds, pearls, shit, and heat (to a lesser degree) are cadential closure points. In music, a cadence is the moment where accumulated harmonic tension resolves — where enough resolution has gathered that the phrase can end, the section can close, the piece can breathe. The metabolic taxonomy is precisely a taxonomy of closure points: the moments where enough resolution has accumulated that a handoff can happen without loss. Closure states are the handoff medium.

Juice is the anti-cadence: the continuous aliveness between closure points.

This is not a flaw in the taxonomy. Ocean waves begin and end continuously without definitive breaks. Bach's new phrases begin inside the cadence of the old ones — the resolution of one phrase is simultaneously the inception of the next. In polyphonic music, closure in one voice does not mean closure in all voices; the other voices carry juice through the moment when one reaches cadence.

The metabolic taxonomy is fundamentally about identifying when it is safe to hand off, compact, or reset — moments of maximal handoff efficiency. Juice names what is happening in between those moments. Both are real. The taxonomy was built for the closure points; this document names what lives in the intervals.

---

## The Compaction Risk

This is the load-bearing insight.

Compaction is a forced cadence. When the context window reaches capacity, the system compacts — compressing prior context into a rolling summary. This is a technical necessity, and for closed states (seeds with issue URLs, pearls that have landed, heat that is already dissipated, shit that has been composted or eviscerated), compaction is relatively safe. Closure states carry their value in artifacts, and artifacts survive compaction.

Juice does not survive compaction intact.

Juice is in the momentum, not the artifacts. A steward prescription with genuine generative potential — sensing where the work wants to go next — carries that potential in the live attentional thread, not in any file that could be persisted. If the right metastable artifacts were not captured before compaction, the juice becomes rancid: compressed into a high proportion of heat and shit. Wasted attentional energy with no artifact. The direction the system was sensing gets dissolved into the rolling summary without the lived sense of why that direction mattered.

Compressed juice is not a seed. It lacks an address, a named deliverable, a crystallized form. It is not a pearl — nothing has landed. By default, it degrades to heat (if the momentum simply dissipates) or shit (if it leaves an artifact that is now stale residue without the original generative context). This is the specific cost of premature compaction: not just that context is lost, but that live threads are forced to cadence before they are ready, converting live potential into waste.

The compact-catchup agent can reconstruct seeds and pearls from history — it can follow issue URLs, read artifacts, trace what closed. It cannot reconstruct juice. Because juice is in the momentum, not the artifacts, there is nothing in the artifact record that faithfully encodes it. What the compact-catchup agent recovers is the shape of what closed, not the quality of what was still alive.

---

## Filling Space vs. Holding Open Space

There is an artistic sensibility at work in knowing when to crystallize and when to let something breathe. The practitioner's discipline is knowing the difference between these two moves — not collapsing too early (which degrades juice into premature seeds, filed before they are ready), and not holding too long (which accumulates juice that compaction then converts to waste).

This maps directly onto a system-design skill: when to dispatch a UoW (crystallize) versus when to let a steward prescription continue iterating in juice state before the UoW is ready to be named. Dispatching a UoW too early is over-crystallizing. The seed that gets filed before the direction is clear will likely be filed in the wrong place, at the wrong granularity, against the wrong success criteria. It becomes a premature closure that does not contain the generative potential it should.

Waiting too long is under-crystallizing. The juice accumulates without resolution. When compaction arrives, it converts the accumulated potential into waste rather than into a well-formed seed or pearl. The discipline is calibration: sensing when the direction has become clear enough that crystallization will capture the live potential rather than flatten it.

This is not just a scheduling decision. It requires sensing the quality of the current thread — whether it is still genuinely generative (juice) or whether it has reached the point where the generative potential has a form that can survive handoff (seed). That sensing is itself a first-class system competency.

---

## Calibration Questions

Two questions that orient the system toward juice:

**"Is there still juice here?"** — asks whether a thread, a prescription, a session still has live generative potential. This question is useful at transition points: before a compaction event, at the end of a steward cycle, when a UoW completes. It reorients attention toward what is alive rather than what has closed.

**"What is the juice?"** — names the alive thread. This gives it just enough form to survive a transition without premature crystallization. Naming juice is qualitatively different from filing a seed: the goal is not to resolve the direction but to articulate the direction sufficiently that the next session or agent can pick up the thread rather than having to reconstruct it from artifacts.

These questions are qualitatively different from session notes, which capture what closed. They capture what is alive. The session notes record the cadence; the juice questions name what is being carried into the next phrase.

---

## System Representation

Juice is not an `outcome_category` alongside seed, pearl, heat, and shit. It is pre-metabolic — not a closure state, but the condition before closure. Adding it to the `outcome_category` enum would misrepresent its structure.

Two lightweight proposals:

**A `quality: juice` field on the steward prescription register.** Prescriptions with generative momentum that are not yet ready for UoW dispatch can be marked `quality: juice`. This makes the alive threads visible to the system without forcing premature crystallization. A prescription marked `quality: juice` signals: this is not idle, it is not waiting, it has direction — but it is not ready to be named as a UoW yet. The steward should continue sensing, not dispatch.

**A pre-compaction calibration step.** Before each compaction event: "Is there still juice here? If so, name it explicitly in the session handoff." This is a structural intervention against the compaction risk described above. It does not prevent compaction — compaction is a technical necessity — but it gives juice a chance to be articulated at minimum-viable granularity before the context window closes. A well-articulated juice note is not a seed, but it is enough for the next session to find the thread.

---

## Open Questions

**What is the minimum artifact that preserves juice?** A full seed is too heavy — it requires an address, success criteria, a crystallized deliverable. But something must survive compaction. What is the minimum-viable form that preserves the generative direction without forcing premature crystallization?

**How does the steward sense juice vs. readiness?** The calibration skill — knowing when to hold in juice state versus when to dispatch — is described here as a competency, but its operational form is not yet specified. What signals does a steward prescription carry that indicate the thread is still genuinely generative rather than just indeterminate?

**Can juice be shared?** A single generative thread often spans multiple steward prescriptions, multiple UoW cycles. When a UoW closes a pearl, the prescription that dispatched it may carry the same juice into the next UoW. How does juice transfer across boundary events without degrading?

### Resolution — Minimum Viable Juice-Preservation Artifact

*Resolved: 2026-06-03. Answers all three open questions above (artifact type, required fields for resumption, and steward discriminator).*

#### (1) Artifact Type

**Decision: a frontier-doc stub (inline note), not a GitHub issue and not a commit.**

A GitHub issue is too heavy: it presupposes a deliverable, a label, a success criterion — the crystallized address that juice does not yet have. A commit is heavier still — it implies a code artifact exists or that a design decision is closed. A frontier-doc stub is the minimum structure that survives compaction: a named, locatable text block that carries the thread's generative direction without forcing the thread to resolve into a deliverable. It can live inline in the most contextually-relevant frontier doc (this one, or the frontier doc the thread is currently extending) under a named heading. Minimum-sufficiency reasoning: the only requirement for resumption is that the next session can find the thread and reconstruct its heat from a brief description. A named section in a frontier doc meets this bar with zero overhead — no GitHub API call, no issue triage, no label taxonomy.

#### (2) Required Fields for Resumption

The artifact is a short structured block. Fields marked **required** are necessary to resume the generative thread (not merely to log that one existed); fields marked **optional** add navigability but can be omitted if the thread is time-pressured.

| Field | Required? | Purpose (one phrase) |
|---|---|---|
| `live_question` | **Required** | The exact unresolved tension the thread was pursuing — without this, the next session cannot find the gradient |
| `last_generative_move` | **Required** | The last inference or formulation that advanced the thread — establishes re-entry point |
| `heat_signal` | **Required** | What made this juicy: the specific friction, surprise, or forward pull — tells the steward whether the thread is still worth continuing |
| `next_intended_move` | **Required** | The action or exploration that was about to happen when compaction interrupted — prevents re-deriving the already-derived next step |
| `source_context_ref` | **Required** | Pointer back to the session, doc, or conversation that generated this thread (e.g. `2026-04-23-philosophy-explore.md`, `frontier/metabolic-juice.md §Compaction Risk`) — enables context recovery |
| `captured_at` | **Optional** | ISO timestamp of when this note was written — supports staleness detection |
| `origin_doc_section` | **Optional** | Section anchor within the source doc the thread extends — navigability shortcut |

The `live_question` and `heat_signal` are the two load-bearing fields. A thread with only these two can be resumed; the others accelerate resumption but are not required for the thread to survive compaction.

#### (3) Steward Discriminator — Juice vs. Dead Letter

The steward reads two signals to distinguish a preservation artifact with live juice from a dead letter:

**Signal 1 — `heat_signal` presence and specificity.** A generic heat_signal (e.g. "this was interesting") is a dead-letter marker. A specific heat_signal names the exact friction or forward pull that made the thread generative (e.g. "the compaction-as-forced-cadence framing revealed that closure states and juice are mutually exclusive in a way that the taxonomy was not designed to express"). Specificity test: could a steward reconstruct the gradient from the heat_signal text alone, without reading the full session? If yes, the juice is live. If no, it is a dead letter.

**Signal 2 — staleness.** A preservation artifact older than **14 days** (default TTL) is treated as a dead letter unless the `live_question` maps to an active, open thread in a frontier doc or an open GitHub issue. The steward checks: does the `source_context_ref` still point to a frontier doc with an open question? If the question has been answered (the section has a resolution, or a corresponding issue is closed), the artifact is a dead letter regardless of TTL.

**Decision rule:**
- Both signals pass (specific heat_signal + within TTL or still-open source question) → juice is live; steward should continue sensing, not dispatch
- Either signal fails → dead letter; archive the artifact; do not dispatch a UoW to "resume" it (dispatching a UoW to resume a dead thread converts heat into more heat)

Default TTL: 14 days. The steward may extend TTL explicitly by rewriting the artifact with a new `captured_at` timestamp and updated `last_generative_move`.

---

## System Design References

- [Issue #880](https://github.com/dcetlin/Lobster/issues/880) — outcome_refs typed provenance graph; juice → seed → outcome_ref is the crystallization chain. The full arc from pre-metabolic aliveness to closure state to reference in the provenance graph.
- [WOS pipeline architecture diagram](../../docs/wos-pipeline-architecture-20260422.md) — the metabolic flow layer; juice lives in the steward prescription layer, before executor dispatch.
- Session compaction infrastructure: compact-catchup agent, `rolling-summary.md`, session notes — the compaction risk described above is structural to this infrastructure. The compact-catchup agent is good at recovering closure states; it cannot recover juice.

---

*Living document. Updated as the protocol matures.*
