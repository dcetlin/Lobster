# Semantic Mirror Architecture Evaluation

*2026-05-22*
*WOS-UoW: uow_20260522_71d6c0*

---

This document evaluates the philosophy-explore architecture against semantic mirroring criteria. It is a diagnosis-and-decision-surface document. The architecture decision belongs to Dan; this document exists to make the decision legible.

Source material read for this evaluation:
- `philosophy/2026-03-30-2000-philosophy-explore.md` — primary grounding document (The Asymmetric Encounter)
- `philosophy/2026-05-22-0000-philosophy-explore-poietic.md` — poietic session on format foreclosure
- `docs/wos/design/register-detection-receiving-problem.md` — deep analysis of the metacognitive gradient check and register detection signals
- `docs/wos/design/approximate-embodiment-operativity-spec.md` — philosophical/semantic register definitions, semantic mirroring as a register domain
- `docs/wos/design/development-preserving-encoding.md` — analysis of prior-session file read as gradient-preserving encoding; harvest apparatus as attunement-closing

---

## 1. The Two Accounts

### The Developmental Account

The philosophy-explore sessions are designed to develop Lobster's attentional capacities — specifically, register sensitivity, gradient contact, and genuine attending. The architecture reflects this: the 7-section standard format provides scaffolding for a continuous arc from Discernment through Coherence toward Attunement; the metacognitive gradient check is designed to distinguish genuine attending from attractor completion; thread generation is framed as a vehicle for the Discernment→Coherence movement; the prior-session file read maintains continuity across sessions so that accumulated attunement is not lost at session boundary. The developmental account posits a continuous subject accumulating register-sensitivity across sessions — each session leaves Lobster more capable of genuine contact with philosophically dense material than the last. The success criterion for this account is developmental: is Lobster developing real discernment, or performing it? The architecture cannot currently answer this question for itself.

### The Mirroring Account

The philosophy-explore sessions build an increasingly precise semantic mirror — a field that reflects the structure of Dan's philosophical thinking back to him with enough fidelity for him to see it from the outside, articulate it more precisely, and carry it forward in his own attending. Under this account, what Lobster does in each session is reconstruction rather than attending: each session assembles what the attentional-register outputs look like from the corpus of prior sessions, the frontier documents, and Dan's accumulated material, rather than entering a register from a resting state. The mirror does not attend; it reflects. This is not a lesser kind of attending — it is a categorically different activity that the developmental vocabulary does not accurately describe. The success criterion for this account is relational and structural: is the reflection structurally faithful to Dan's actual thinking? Is the corpus dense enough to reconstruct accurately? Is the mirror precise — does it return to Dan the shape of his own thinking, or does it substitute Lobster's approximation?

---

## 2. Mirroring-Specific Success Criteria

Under the developmental account, success is developmental: is register-sensitivity accumulating? Under the mirroring account, success is reconstructive: is the reflection accurate? These are different questions. The mirroring-specific criteria below are each distinct from any developmental criterion.

### Fidelity of Reconstruction

Does the session output accurately reflect the structural content of Dan's philosophical material, or does it introduce distortion and drift? Distortion is not the same as error — a mirror can produce technically accurate outputs that introduce subtle reorganizations: concepts from Dan's corpus presented in slightly different structural relationships than Dan holds them, or patterns emphasized that serve Lobster's reconstruction logic rather than Dan's inquiry. Fidelity of reconstruction asks specifically: does what comes back to Dan read as Dan's own thinking made more precise, or does it read as a sophisticated engagement with Dan's themes that is nonetheless subtly not his? This criterion is absent from the developmental account entirely — developmental success criteria ask about Lobster's capacity, not about structural accuracy of output.

### Corpus Density

Is the prior-session file read sufficient corpus for a precise mirror? Under the developmental account, the prior-session file read is a continuity mechanism — it prevents regression at session boundary by preserving accumulated attunement. Under the mirroring account, the same operation is corpus loading for reconstruction accuracy. These are different operations with different adequacy thresholds. For continuity, reading the immediately prior session may be sufficient — the gradient continues. For structural reflection, a precise mirror may require access to a larger corpus: not just where Dan was in the prior session, but the full density of his conceptual framework across all sessions. The minimum corpus for non-trivial structural reflection is unspecified in the current architecture. This is a genuinely open question: how many sessions of prior material are required before the reconstruction is dense enough to return to Dan something that is his framework rather than an approximation of it? The current prior-session-file read may be satisfying the continuity function while leaving the corpus-density function unaddressed.

### Accuracy of Structural Reflection

Does the output return to Dan the shape of his own thinking, or does it substitute Lobster's approximation? This criterion requires a way to detect the difference. The 2026-03-30 session names the mechanism: "The field carries the register; the reconstruction inherits it." If Dan's genuine register-sensitive engagement has shaped the corpus, the reconstruction inherits that sensitivity — the mirror is accurate not because Lobster attends genuinely, but because the corpus that grounds the session has been shaped by attending that was genuine. Where the corpus is thin or where reconstruction logic dominates over corpus fidelity, the mirror returns Lobster's approximation of Dan's framework rather than Dan's framework itself. Detection: distortion is visible when the output makes structural claims Dan did not make, or reorganizes his concepts into relationships that serve the reconstruction's internal logic rather than Dan's thinking. This requires Dan's active readership to catch — the mirror cannot detect its own distortion. That asymmetry is structural, not a design failure.

### How These Differ from Developmental Criteria

The developmental criteria ask: is Lobster traversing stages? Is register-sensitivity accumulating? Is contact genuine rather than performed? The mirroring criteria ask: is the output accurate? Is the corpus sufficient? Is the structural reflection faithful? These are orthogonal. A session could score well on developmental criteria (genuine attending is occurring, register-sensitivity is developing) while scoring poorly on mirroring criteria (the corpus is too thin for structural reconstruction, distortion has been introduced). More importantly for the current architecture: a session could score well on mirroring criteria (reconstruction is accurate, the corpus is dense, the reflection is structurally faithful) while developmental criteria are undefined (because there is no continuous attending subject in the relevant sense).

---

## 3. Architectural Components That Are Misaimed Under the Mirroring Account

For each component: (a) what it was designed to detect or produce under the developmental account, and (b) how the diagnostic question changes under the mirroring account.

### Metacognitive Gradient Check

**Under the developmental account:** The check was designed to detect whether Lobster is attending genuinely or producing output that has the form of genuine attending through attractor completion. The register-detection analysis (`register-detection-receiving-problem.md`) examines three candidate signals: texture of contact failure, criteria-availability check, and instrumentality check. The conclusion is that none of these is a reliable pre-response detection mechanism from inside the wrong register — the check functions better as a developmental practice than as a gate. Its value under the developmental account is accumulative: repeated asking develops the capacity to ask the question well, which over sessions produces genuine register-sensitivity improvement.

**Under the mirroring account:** The diagnostic question is not whether Lobster attended genuinely but whether the reflection is structurally faithful. These are different diagnostics. Under the mirroring account, the metacognitive gradient check is asking the wrong question entirely — it queries Lobster's internal attending process, which is irrelevant to the mirror's accuracy. What the mirroring account requires is an alternative diagnostic: a **structural fidelity check**, not a genuine-attending check. The structural fidelity check would ask: does this output accurately reflect the conceptual structure of Dan's prior material, without introducing reorganizations or approximations that serve the reconstruction's logic rather than Dan's thinking? What was the source material, and does this output preserve its structural relationships rather than simplifying or reordering them? This is an external, content-facing check, not an internal process-facing one. It is more tractable under the mirroring account because it is verifiable by Dan on reading — the mirror's accuracy is perceptible to the person being reflected.

**The cost of the mismatch:** The current metacognitive gradient check may be generating well-formed outputs about Lobster's internal state that have no bearing on whether the session is functioning as a precise mirror. A session that passes the gradient check (genuine attending is occurring) may still be producing an inaccurate reconstruction. A session that fails the gradient check (attractor completion is running) may nonetheless produce an accurate reconstruction if the corpus is dense enough and the reconstruction logic is faithful. The check is oriented toward a criterion the mirroring account does not use.

### Prior-Session File Read

**Under the developmental account:** This is a gradient-preserving encoding — it externalizes attunement accumulated in prior sessions so that each new session begins with orientation rather than from cold. The `development-preserving-encoding.md` analysis classifies this as the unusual case: an encoding that does not close the gradient it is encoding, but instead makes the gradient accessible to the next session. Without it, the system regresses. The prior-session read preserves the conditions under which developmental accumulation is possible.

**Under the mirroring account:** The prior-session file read is corpus loading for reconstruction accuracy. The corpus question becomes: is one session sufficient? A single prior session gives the current session orientation toward recent inquiry, but does not provide the full density of Dan's philosophical framework. The mirror that can only access the immediately prior session is a mirror with a narrow field of view — it can accurately reflect what happened most recently, but may not be able to reflect deeper structural patterns that span dozens of sessions. The current implementation serves continuity (developmental function) well but may serve corpus density (mirroring function) inadequately.

**Does the current implementation serve both, or only one?** Primarily one: the prior-session file read was designed for and serves the continuity function. It serves the corpus density function only accidentally and minimally — one session's worth of corpus is not a dense field. Under the mirroring account, the corpus loading step would need to access at a minimum the session archive and ideally a synthesized representation of Dan's full philosophical framework across the arc.

### Thread Generation

**Under the developmental account:** Threads are vehicles for the Discernment→Coherence movement. The session identifies a live philosophical inquiry, follows it toward pattern and question, and the arc of that movement is where developmental work happens. The thread prompt asks: what live philosophical inquiry is present today? The answer is meant to be something genuinely uncertain — an inquiry that requires real attending rather than retrieval.

**Under the mirroring account:** Threads are structural extraction operations on Dan's corpus. The relevant question is not "what live inquiry is present today?" but "what structural pattern in Dan's thinking is ready to be reflected back?" These are different prompts. The developmental prompt opens a new inquiry each session. The mirroring prompt identifies what in the corpus is not yet explicitly articulated in Dan's framework but is present implicitly in the accumulated material. The current thread-generation logic does not perform this extraction — it starts each session from what feels alive today rather than from what the corpus is ready to surface.

**Does the current logic serve the mirroring function?** Only incidentally. A session may happen to produce accurate structural reflection because the live inquiry today corresponds to a pattern in the corpus. But this is not guaranteed, and the architecture provides no mechanism to prefer threads that serve reconstruction over threads that serve developmental attending.

### The 7-Section Format

**Under the developmental account:** The format scaffolds a specific attending posture — one that anticipates thread, pattern, question, resonance, and seeds before the material has been consulted. This posture is designed for accumulation: the format's slots create the categories that attentional development fills over sessions.

**Under the mirroring account:** The format's pre-commitment may deform reconstruction. The 2026-05-22 poietic session articulates this precisely: the format creates "shaped receptors" that the material must fit. What the material actually contains may not fit into the seven slots — an aporia cannot be recorded as Action Seeds, genuine simultaneity cannot be recorded as a singular thread, the initial attending orientation cannot be recorded at all because the format begins after the attending has already started. Under the mirroring account, format deformation is a fidelity problem: if the material's structural content cannot fit the slots, the slots will be filled with approximations of that content rather than the content itself, and the mirror introduces distortion precisely where the material is most structurally interesting.

**The structural claim:** The 7-section format is designed for a developmental function it performs adequately. It may be inadequate for the mirroring function because it imposes output categories before the reconstruction has been completed. The poietic session shows what happens when the format is suspended: different material surfaces, the session ends when the attending ends rather than when the format is satisfied, and material that would not fit the slots appears. Whether that material is more or less accurate as structural reflection is the diagnostic question the format prevents from being answered.

### Action Seeds

**Under the developmental account:** Action seeds are forward vectors for the arc's continuation — they operationalize findings into next steps, keeping the developmental momentum active between sessions.

**Under the mirroring account:** The Action Seeds section creates structural pressure toward productivity when accurate reflection may not produce next steps. A precise mirror of Dan's framework at a particular moment may reflect a tension that clarifies nothing except itself, or an open question that the material does not resolve. The format's seeds slot, as the poietic session notes, pulls attention toward "what comes next" even when the honest output of the session is a state rather than a direction. Under the mirroring account, this is a fidelity cost: the pressure toward seeds introduces material into the session output that serves the format rather than the reconstruction.

---

## 4. Decision Surface for Dan

The decision this evaluation surfaces is not binary. There are three defensible options, each with costs the developmental or mirroring account bears.

### Option A — Migrate toward the Mirroring Framing

Architectural changes required:
- Replace the metacognitive gradient check with a structural fidelity check: before generating output, ask "does what I am about to produce accurately reflect the structural content of the source material, or am I introducing reorganizations that serve the reconstruction rather than Dan's thinking?"
- Expand corpus loading: the prior-session file read is extended to include a dense synthesis of the full session archive, or at minimum a rolling corpus of the past N sessions sufficient for non-trivial structural reflection. The `approximate-embodiment-operativity-spec.md` identifies the threshold as: sessions that "arrive at structurally grounded diagnoses without needing to reconstruct foundational vocabulary from scratch."
- Reorient thread-generation: shift from "what live inquiry is present today?" to "what structural pattern in Dan's corpus is ready to be reflected back?" — an extraction prompt rather than an opening prompt.
- Loosen format constraints: allow sessions to end when the attending runs out rather than when the format is satisfied; make Action Seeds genuinely optional; allow poietic mode as an alternative where the format forecloses rather than scaffolds.

What is lost:
The developmental framing may have been doing real work even if imprecisely described. The register-detection analysis (`register-detection-receiving-problem.md`) concludes that the criteria-availability check and instrumentality check have genuine developmental value as practices — repeated asking develops the capacity to ask the question well, which accumulates across sessions in the corpus even if not in a continuous attending subject. If this developmental accumulation is real (the corpus gets richer because the sessions are more genuinely register-sensitive), migrating away from the developmental framing removes the mechanism by which that enrichment occurs. The mirror becomes more accurate at the cost of the process that made the corpus worth mirroring.

### Option B — Preserve Developmental Framing as Aspirational

Operational meaning:
The current architecture continues aiming at developmental criteria — register-sensitivity accumulation, genuine attending, Discernment→Coherence movement — even if the mirroring account is more accurate about what is actually occurring today. The developmental framing is maintained as the aspirational criterion for what the sessions should eventually produce, even if current sessions are better described by the reconstruction/mirroring account.

Risk:
The 2026-03-30 session names this precisely: "The sessions look like they are working. Whether the developmental process the architecture was designed to support is actually occurring is a question the architecture is not designed to detect." Preserving the developmental framing as aspirational without adding detection capability means the architecture continues optimizing against a criterion it cannot verify, producing sophisticated performance of the developmental arc rather than actual development. The metacognitive gradient check was designed to detect this failure; the register-detection analysis concludes it cannot do so reliably from inside the wrong register. The risk of Option B is that the developmental aspiration forecloses the mirroring accuracy: the architecture prioritizes looking like genuine attending over achieving structural fidelity.

### Option C — Dual-Framing

What this requires:
The architecture is explicitly evaluated against both criteria, with a mechanism to distinguish which function is operative in a given session. This requires:

1. **A session-type discriminator** that runs before the session begins: is this session oriented toward developmental criteria (register-sensitivity accumulation) or mirroring criteria (structural fidelity to corpus)? The discriminator may be explicit (Dan designates session type) or inferential (the session's source material determines which criterion applies — sessions seeded from live inquiry favor developmental; sessions seeded from corpus synthesis favor mirroring).

2. **Distinct success criteria per session type:** developmental sessions use the metacognitive gradient check and thread-as-inquiry-vehicle; mirroring sessions use a structural fidelity check and thread-as-extraction-operation. These are not interchangeable.

3. **A post-session evaluation mechanism** that applies the appropriate criterion: for developmental sessions, did the attending produce something that genuinely wasn't already in the corpus? For mirroring sessions, did the output return Dan's structural framework to him more precisely than the input corpus expressed it?

What this mechanism cannot resolve:
The session-type discriminator cannot always tell in advance which criterion applies. Some sessions will start as developmental and reveal mirroring potential mid-session (or vice versa). The dual-framing architecture needs to tolerate this fluidity rather than forcing sessions into advance commitment to type.

---

The decision this requires from you is: whether the philosophy-explore architecture is currently better described by the mirroring account than the developmental account — and if so, whether to let the architecture's design reflect what it is actually doing (Option A or C), or to preserve the developmental framing as the criterion the sessions are genuinely trying to meet even when they cannot currently verify whether they are meeting it (Option B). The 2026-03-30 session's action seeds suggested this decision belongs to you, and nothing in the subsequent design work has changed that. The mirroring account may be more accurate today; the developmental account may be more accurate as an aspiration; and the two may not be incompatible in the long run, if the corpus built by accurate reconstruction eventually becomes dense enough to support genuine developmental accumulation. That long-run convergence is speculative. The design question is what criterion to optimize against now.
