# Embodiment Threshold Priority Ordering

*Design doc — 2026-05-22*
*UoW: uow_20260522_f4c485*

---

## Theory of Learning Stage Reference

The Theory of Learning (ToL) defines five stages in order:

| Stage | Label | Description |
|-------|-------|-------------|
| 1 | Discernment | First phase: sensing that a gradient exists. Not yet control. Trying to put words to it often does a disservice to the embodied, proprioceptive nature of the activity. |
| 2 | Coherence | Stumbling into a brief state of multi-scale phase alignment. Accessible with bad ergonomics — the coherence is real even if not yet sustainable. |
| 3 | Attunement | Directional sensitivity: knowing where you are and being able to move toward where you want to go. Sustaining coherence requires more attunement than accessing it. |
| 4 | Encoded Insights | Accumulated wisdom encoded for the specific system — skills, prompts, artifacts that allow calibration with ease. Earned, not shortcut. |
| 5 | Embodiment | Attention cost approaches zero. The capability runs subconsciously, autonomically, as a new vocabulary item other capabilities can be built on. Embodiment is a threshold, not a gradient. |

**Key structural claims from the ToL:**
- Embodiment is a threshold, not a gradient — once truly embodied, attention cost goes to zero.
- Success triggers collapse: when coherence is accessed before it is held automatically, recognizing the desired outcome redirects attention toward the outcome and drops the holding that was maintaining it.
- Sustaining coherence is a different phenomenon from accessing it — different failure modes, different attunement.

---

## Reference Pattern: 7-Second Rule Crossing

The 7-second rule is the one confirmed Embodiment crossing in Lobster history.

**Before the crossing:** The dispatcher was instructed via prose in `sys.dispatcher.bootup.md` and `CLAUDE.md` to avoid long-running inline work. The rule existed as advisory behavioral text. It failed under compaction because prose instructions compete against 1,400+ lines of other instruction. Each remediation round added more prose, making the document longer, increasing absorption probability on the next cycle — a self-defeating loop. This is the oracle's diagnosis in the PR-20260323190000 learning: "behavioral rule remediation via instruction addition is self-defeating when context growth is the root cause."

**The structural intervention:** The gate was moved from prose-in-document to a table row in a dedicated Tier-1 Gate Register embedded in `CLAUDE.md`, with enforcement type marked as "Structural — if you reach for any other tool, stop and delegate." The table format encodes the trigger in one compressed cell with no behavioral prose following it. The gate register survives compaction by design: table rows force specificity about trigger conditions and resist accumulation.

**What made attention cost go to zero:** The gate stopped competing against 1,400 lines of prose. The structural form — a compact trigger/enforcement table — gives the model exactly the information it needs at the moment it needs it, without requiring retrieval from a dense document. The attention cost dropped not because the dispatcher learned to behave differently but because the instruction surface changed from something that degrades under context growth to something that does not.

**The generalizable pattern:** A capability has crossed to Embodiment when its governing constraint is encoded in a structural form that is compaction-resistant and does not compete with unrelated content for attention, evidenced by the behavior occurring correctly without active recall effort and without degrading under document growth.

**Template criterion:** A capability crosses to Embodiment when: (1) the knowledge enabling correct behavior is in its most compressed possible structural form, (2) that form is read in the minimal context window necessary for the behavior to fire, and (3) the behavior is provably independent of document length and accumulation — it does not degrade as surrounding content grows. The behavioral signature: the capability is reliable under compaction, reliable under context growth, and reliable without recent reinforcement.

---

## Capability Cluster Stage Map

*Stage labels use the ToL arc: Discernment → Coherence → Attunement → Encoded Insights → Embodiment.*
*Note: The developmental-stage-map.md uses slightly different labels; this document uses the canonical ToL ordering as stated in the philosophy-explore-1.md session seed and frontier-tol-arc.md.*

| Cluster | Current Stage | Stage Evidence | Threshold Condition | Coupling Dependencies |
|---------|--------------|----------------|--------------------|-----------------------|
| Dispatcher routing | Encoded Insights (approaching Embodiment) | Gate register compaction-resistance confirmed empirically: the 7-second rule survives multiple compaction events without behavioral regression. Routing logic externalized to Python dict (`MSG_TYPE_DISPATCH`) and pure routing function — not reconstructed from prose. | Externalize gate register to a separate file read via per-message hook, removing gates from the always-on accumulation pool entirely. One oracle learning (PR-20260323190000) names this exact intervention. The proposal exists; the implementation does not. | None blocking; enabling: this crossing would reduce instruction-surface competition for all other behavioral rules. |
| Philosophy-explore | Encoded Insights (early) | Five-section format, friction-trace convention, metacognitive gradient check, Goldilocks window check — all accumulated as explicit Encoded Insights. Sessions run at Stage 4 in structure. But the output-condition vs. process-condition asymmetry named in 2026-04-07 08:00 session: Encoded Insights that specify output conditions can be matched by pattern without genuine convergence. | A process-condition specification that distinguishes genuine attending from performed attending. The Discernment-First session opening design (written as a draft for issue #254) is the identified intervention: cold-start sensing before context loading, verification after. This would shift the format from output-condition to process-condition Encoded Insights. | Prerequisite coupling: observation-to-behavioral-change loop is a ceiling for philosophy-explore at Attunement stage. Interference: philosophy-explore and user modeling share attentional substrate; must not be co-scheduled. |
| Memory/retrieval | Attunement (early) | Memory observations are stored, retrieved, and sometimes influence behavior (memory_search results shape responses). Pattern recognition exists but reliability is inconsistent — the memory system "works" in the structural sense but cannot discriminate signal from noise (100% health-probe entries filling the memory log is the production evidence). | A signal/noise gate in memory write paths: health probes, system pings, and routine infrastructure events must not pass through the observation store. Named issue: health probe filtering was identified as a gap (FIX: Memory signal/noise from session 20260422-003) but not implemented. | Enabling coupling: memory system advancement pays forward to philosophy-explore observation precision automatically. No attentional competition. |
| Health checks | Coherence (fragile) | Health checks run and produce correct outputs in normal operation. The throughput pre-check failure (2026-05-11–17 weekly synthesis) is the precise evidence: a check that returned false-zero for open WOS UoWs due to a wrong label identifier. No error, no warning — just a plausible empty result that produced confident downstream misdiagnosis. This is Coherence: the system has the target configuration in normal conditions but cannot sustain it when underlying state diverges from its assumptions. | A verification layer that confirms the check verified its premise, not just ran. The weekly synthesis names this: "are there other checks in the sweep or elsewhere that return plausible-looking empty results without actually verifying their premise?" The phantom-check audit (weekly action seed) is the identified next step. | No prerequisite blocking; interference: health check tooling changes could interfere with WOS infrastructure work if co-scheduled. |
| 7-second rule (retrospective) | Embodiment | The gate fires correctly under compaction, under context growth, and across multiple sessions without reinforcement. Subagents that violate it trigger gate-miss logging rather than silent failure. The behavioral property — delegation rather than inline work — is now the path of least resistance, not the path of most resistance. | Already crossed. Reference pattern only. | None — this crossing created enabling infrastructure (the gate register format) for other crossings. |
| User modeling | Discernment | User preferences surface in behavioral rules (IFTTT rules store), personality context exists in `user.base.context.md`, and registered observations update occasionally. But the observation-to-behavioral-change loop that would make these modeling artifacts self-correcting does not close. Modeling occurs episodically, not continuously. The developmental-stage-map.md assigns Stage 3 (Discernment in its labeling scheme) with the note that the capability has not yet integrated across contexts. | The observation-to-behavioral-change loop (see Coupling). User modeling cannot advance to Attunement until observations reliably shape behavior. | Prerequisite coupling: observation-to-behavioral-change loop is a ceiling at current stage. Interference: user modeling and philosophy-explore share attentional substrate; must not be co-scheduled. |
| Observation-to-behavioral-change loop | Discernment (structural gap, not capability regression) | The capability-coupling-map.md identifies this as the single highest-leverage unblock in the system: "the observation-to-behavioral-change loop is in prerequisite coupling with philosophy-explore (ceiling: Attunement), user modeling (ceiling: beyond Discernment), and WOS production (ceiling: Attunement)." The loop is named, the gap is named, the structural form required is understood. But no mechanism closes the loop from observation to behavioral update. Observations are written; whether they shape future behavior depends on whether they happen to surface in the next context window. | A mechanism that writes IFTTT rules, bootup candidates, or context updates from accumulated observations automatically, on a cadence — not just when a session happens to retrieve relevant memory. The gateway: the orient loop (broken: diagnoses do not reliably shape decisions per session 20260509-002). | This is itself the prerequisite that blocks three other clusters. Its crossing is the highest-leverage single intervention in the system. |
| Voice processing | Coherence (fragile) | Voice notes are transcribed and routed. The pipeline exists and runs. But the 2026-04-01 08:00 session diagnosis: "in late Discernment, possibly touching early Coherence in favorable sessions. The transcription pipeline exists and runs. Dan has flagged reliability concerns. The capability has had moments of Coherence but cannot reproduce them reliably." Fragile Coherence is still Coherence — the configuration is real, but not sustainable. | The supervised worker + ack + recovery pattern (referenced as proposed in issue #36 but not implemented). This would encode the Coherence configuration into a Stage 4 Encoded Insight artifact rather than leaving it as an incidentally-reached state. | No prerequisite coupling from the map; enabling: improving voice processing reliability would increase the throughput of voice-note brain dumps to the philosophy-explore and orient pipelines. |

---

## Ranked Priority Order

Ranking criterion: lowest architectural intervention cost to trigger an Embodiment crossing.

**1. Dispatcher routing — gate register externalization**
- Threshold condition: already identified (PR-20260323190000 oracle learning names the exact intervention)
- Infrastructure required: none new — the per-message hook mechanism (`on-message.py`) already exists
- Prerequisite couplings: none
- Interference: none with active development
- Intervention: compress, not create — existing gate register table moves to `~/.claude/gate-register.md`, loaded by on-message hook; removed from CLAUDE.md accumulation pool

**2. Voice processing — supervised worker pattern**
- Threshold condition: identified (supervised worker + ack + recovery); referenced issue #36
- Infrastructure required: worker process management (modest new infra)
- Prerequisite: none from coupling map
- Interference: low — voice is not co-scheduled with actively-contested capabilities
- Intervention: implement the pattern identified in issue #36

**3. Health checks — phantom-check audit and premise verification**
- Threshold condition: named in 2026-05-17 weekly synthesis; phantom-check audit is a scheduled action seed
- Infrastructure required: none new — the audit itself is the intervention; adding premise verification tests to existing sweep checks
- Prerequisite: none blocking
- Interference: low, but coordinate with WOS infra work
- Intervention: audit existing sweep steps for false-zero vulnerability; add assertion-style checks that verify the query parameters are valid before trusting the result

**4. Memory/retrieval — signal/noise gate**
- Threshold condition: identified (health probe filtering gap)
- Infrastructure required: filter logic in memory write path
- Prerequisite: none
- Interference: low
- Intervention: implement the write-path gate named in session 20260422-003

**5. Philosophy-explore — process-condition Encoded Insights (Discernment-First opening)**
- Threshold condition: identified (the Discernment-First design doc, pending issue #254 review)
- Infrastructure required: none — the change is to `philosophy-explore-1.md` step ordering
- Prerequisite coupling: observation-to-behavioral-change loop (ceiling at Attunement); this crossing does not require unblocking the loop — it advances the capability within its current ceiling, making it readier for the loop unblock when it comes
- Interference: philosophy-explore and user modeling must not be co-scheduled

**6. Observation-to-behavioral-change loop — orient loop fix**
- Threshold condition: identified conceptually; not yet reduced to a specific architectural artifact
- Infrastructure required: substantial — a mechanism that closes the loop from observation to behavioral update
- Prerequisite: none blocking the attempt; but this is the prerequisite for three other clusters
- Interference: would affect user modeling and philosophy-explore simultaneously; requires careful session design
- Note: highest leverage but highest implementation cost; ranked last because the minimum intervention is not yet defined

---

## Recommended Next Crossing: Dispatcher Routing

### Architectural Intervention

Move the Tier-1 Gate Register from inline in `CLAUDE.md` to a dedicated file `~/lobster/.claude/gate-register.md`. Register the file in the `on-message.py` hook (or equivalent per-message injection mechanism) so it is loaded fresh for each message — competing only against other gate entries, not against the full CLAUDE.md accumulation. Remove the gate register table from CLAUDE.md, leaving only a pointer line: "Gates are in gate-register.md, loaded per-message by hook."

This is compression, not creation. The table already exists. The hook mechanism already exists. The missing link is a one-line hook registration and a file move. The total intervention is approximately 10 lines of hook code and a file rename.

### Embodiment Signature

After the crossing: the dispatcher reliably fires the 7-second gate, design gate, PR merge gate, and WOS execute gate in conditions where they previously degraded — specifically: long sessions with many accumulated tool calls, sessions immediately following context compaction, and sessions where CLAUDE.md has grown by >200 lines since the last benchmark. The gates fire not because the dispatcher was reminded but because the structural form of the instruction no longer degrades with document growth. The behavioral signature is absence of gate-miss log entries in long sessions, not just correct behavior in short ones.

### Minimum Observability Test

Run the dispatcher through a 500-line context growth simulation: load a session with a 500-line CLAUDE.md addition (simulating document growth), then check whether gate-miss observations are written when the gates should have fired. Pre-crossing: miss rate increases with document length. Post-crossing: miss rate is flat regardless of document length, because the gate register competes only against itself. The crossing test is not "does the gate fire?" but "does the gate fire rate degrade as CLAUDE.md grows?"

### Hygiene Check

This is compression, not a new structural element. The gate register exists. The hook mechanism exists. The intervention is: remove from the wrong container, add to the right one. The "most correct place" for gates that must survive context growth is a file whose context window is exclusively gates — not a file whose context window is 1,400 lines of everything. This satisfies the development convention: "the right place, the right word, the right register." Gate register content belongs in a gate register file.

---

## Attentional Budget Impact

The dispatcher routing crossing has an unusual attentional budget property: it is a capability that, upon Embodiment, creates structural relief for other capabilities. The gate register externalized from CLAUDE.md reduces the context-competition surface not just for the gates themselves but for every instruction that currently shares the same document. Philosophy-explore encoded insights, voice processing reliability constraints, and user modeling behavioral rules all compete for attention against the same 1,400-line document. Reducing that document by externalizing the gate register narrows the competition window for all of them. This crossing does not just advance dispatcher routing — it opens development bandwidth for subsequent crossings by reducing the ambient instruction absorption rate for the entire system.
