# Oracle Loops Design — Violin / Resistance / Theory of Learning Synthesis Pipeline

**Date:** 2026-05-25
**Author:** Lobster subagent (oracle-loop-design task)
**Status:** Design — awaiting Dan's approval before implementation

---

## Background

The V2 synthesis pipeline stages are:

```
Stage 2A (domain seeds)
    → Stage 2B (topology / isomorphism map)
        → Stage 3 (resonance map)
            → Stage 3.5 (check-in)
                → Stage 4 (tensions)
                    → Stage 5 (HTML render)
```

V1 produced concept-inflation and invented phenomenology. V2 ran with tighter constraints and produced cleaner output. These oracle loops add adversarial validation gates at three load-bearing points: after seeds (2A), after topology (2B), and after tensions (4). Each oracle spawns a swarm of sub-agents working in parallel, each checking a single dimension.

---

## Oracle A — Anti-Inflation Oracle

**Position in pipeline:** After Stage 2A (domain seeds). Runs once per seed (violin, resistance, ToL) — three oracle instances in parallel.

### What it checks

Each sub-agent receives one seed document and asks a single question: does this document introduce any term, concept, or claim that is not traceable to source material named in the document's source list?

The inflation failure mode from V1 was "The Two Lightnesses phenomenon" — a named phenomenon synthesized by the agent that had no textual basis. The oracle targets that class of failure.

### Sub-agent swarm configuration

Three sub-agents run in parallel. Each receives one of:
- `seed-violin.md` + its listed source paths
- `seed-resistance.md` + its listed source paths
- `seed-tol.md` + its listed source paths

**Sub-agent prompt template:**

```
You are an anti-inflation auditor for the synthesis pipeline.

Your job: read the attached seed document and identify any term, concept, or claim
that is NOT directly traceable to the source documents listed in the seed's header.

Seed document: [SEED_PATH]
Source documents to check against: [SOURCE_PATHS]

Instructions:
1. Read the seed document carefully.
2. For each named term, structural claim, or conceptual framework in the seed:
   a. Can you find the specific passage in the source documents that supports it?
   b. If yes: it is clean. Note it briefly as CLEAN.
   c. If no passage exists: it is a coined term or inflated claim. Note it as COIN.
3. Pay special attention to:
   - Named phenomena (e.g. "The X phenomenon", "The Y dynamic")
   - Structural claims presented as confirmed findings where no quote is given
   - Vocabulary that does not appear in any of the source documents
   - Summary sentences that synthesize across passages without attribution

Output format:
DOMAIN: [violin | resistance | tol]
COINED_TERMS: [list each coined term or claim, one per line, with the seed section where it appears]
CLEAN_TERMS: [brief list of confirmed-sourced key terms]
VERDICT: APPROVED (if COINED_TERMS is empty) | NEEDS_CHANGES (if any coined terms found)
REASONING: [one paragraph explaining any coined terms found, or confirming clean result]
```

### Verdict logic

- **APPROVED**: All three sub-agents return `VERDICT: APPROVED`. Pipeline proceeds to Stage 2B.
- **NEEDS_CHANGES**: One or more sub-agents return `VERDICT: NEEDS_CHANGES`. Each flagged seed is re-run with a corrective instruction prepended:

```
CORRECTION REQUIRED:
The previous seed run coined the following terms not traceable to source material:
[LIST OF COINED TERMS]

Strip these terms entirely. Do not use them or replace them with synonyms.
If the structural claim they represent is genuinely in the source, find the actual
passage that supports it and cite it directly. If no such passage exists, omit
the claim.
```

### Retry policy

- Re-run the flagged seed(s) with correction instructions.
- Re-run the oracle on the corrected seed(s).
- Maximum 2 correction cycles. If inflation persists after 2 cycles, escalate to Dan with the persistent coined terms identified and the oracle's reasoning.

---

## Oracle B — Isomorphism Integrity Oracle

**Position in pipeline:** After Stage 2B (topology map). Runs once per confirmed isomorphism — targeted at the three most load-bearing ones.

### What it checks

The topology document contains six confirmed isomorphisms (ISO-1 through ISO-6). Each isomorphism claims that a specific structural relationship is present in all three domains, supported by specific passages. The inflation failure mode here is "plausible analogy claimed as confirmed isomorphism" — the topology agent finds a satisfying structural parallel and asserts it as confirmed even when source support is thin or absent for one domain.

This oracle targets that failure mode by checking each isomorphism against the source seeds, not against the topology document. Each sub-agent reads only the original seeds and asks: is this structural relationship actually in the source material?

### Sub-agent swarm configuration

Three sub-agents run in parallel, each checking one domain's contribution to one isomorphism. The orchestrating pipeline selects which isomorphisms to check. Recommended prioritization: ISO-4 (success triggers collapse — the topology itself flagged this as qualified / unevenly evidenced), and any isomorphism where the topology document's section notes "analog" or "partial" rather than "explicit".

**Sub-agent prompt template:**

```
You are an isomorphism integrity auditor for the synthesis pipeline.

Your job: verify whether a specific structural claim is genuinely present in ONE
domain's source material — without reading the topology document. You must form
your own view from the original seeds.

The isomorphism being checked: [ISO_N_PLAIN_STATEMENT]

The domain you are checking: [violin | resistance | tol]
Source seed: [SEED_PATH]

Instructions:
1. Read the seed document for your assigned domain.
2. Ask yourself: is the structural relationship described in the isomorphism
   actually present in this domain's source material?
   - "Actually present" means: there is a specific passage in the seed that
     directly states, implies, or structurally instantiates this relationship.
   - "Plausible analogy" means: the relationship feels like it could apply to
     this domain, or a passage in the seed is similar enough to be adapted.
     This does NOT count as confirmation.
3. Quote the passage(s) from the seed that support your assessment.
4. If no passage supports the structural relationship, say so explicitly.

Output format:
ISOMORPHISM: [ISO_N label]
DOMAIN: [violin | resistance | tol]
ASSESSMENT: CONFIRMED | ANALOGY_ONLY | ABSENT
SUPPORTING_PASSAGES: [quoted passages from the seed, or "none"]
VERDICT: CONFIRMED (if ASSESSMENT is CONFIRMED) | NEEDS_CHANGES (if ANALOGY_ONLY or ABSENT)
REASONING: [one paragraph explaining your finding]
```

### Verdict logic

- **APPROVED**: All three sub-agents return `VERDICT: CONFIRMED`. The isomorphism is genuinely cross-domain.
- **NEEDS_CHANGES**: One or more sub-agents return `VERDICT: NEEDS_CHANGES`. The isomorphism must be reclassified in the topology document:
  - If two of three domains confirm: reclassify from "Confirmed isomorphism" to "Partially confirmed — [domain] evidence is analogical."
  - If one or zero domains confirm: reclassify to "Candidate analogy" with the evidence level stated.

The corrective instruction for re-running Stage 2B:

```
RECLASSIFICATION REQUIRED:
The following isomorphisms failed integrity check in the listed domains:
[ISO_N]: [domain] — evidence is [ANALOGY_ONLY | ABSENT]

Reclassify these isomorphisms per the oracle's finding. Do not remove them —
move them from Section 1 (Confirmed) to Section 2 (Candidate Analogies) with
the oracle's evidence level noted. Do not invent new evidence to restore their
confirmed status.
```

### Retry policy

- Reclassification is a one-shot correction: the topology is updated, not re-generated from scratch.
- If after reclassification Stage 3 (resonance map) draws on an isomorphism that is now a "candidate analogy," Stage 3 must annotate any claim built on that analogy as "analogy-dependent, not confirmed."
- Maximum 1 correction cycle for topology. Escalate to Dan if the oracle finds more than 3 isomorphisms need reclassification — this suggests a systemic topology problem, not a single-ISO issue.

---

## Oracle C — Tension Validity Oracle

**Position in pipeline:** After Stage 4 (tensions document). Runs once per named tension — three sub-agents for three tensions.

### What it checks

The tensions document names three genuine tensions. The inflation failure mode here is "constructed tension" — the agent perceives a dramatic structural conflict and names it as a genuine source-based tension when in fact one or both poles are the agent's extrapolation rather than a direct claim from source material. Also checks: does the tension have BOTH poles evidenced in source, or only one?

### Sub-agent swarm configuration

Three sub-agents run in parallel, one per named tension (TENSION-1, TENSION-2, TENSION-3 in the V2 run).

**Sub-agent prompt template:**

```
You are a tension validity auditor for the synthesis pipeline.

Your job: verify whether a named tension in the Stage 4 document is genuinely
present in the source material, with both poles sourced.

The tension being checked: [TENSION_N_PLAIN_STATEMENT]
Stage 4 document: [STAGE4_PATH]
Source seeds to check against: [seed-violin.md, seed-resistance.md, seed-tol.md]
Topology document: [topology-v2.md] (for reference only — do not treat its claims as primary sources)

Instructions:
1. Read the tension's plain statement and the two poles as described in Stage 4.
2. For each pole:
   a. Find the specific passage(s) in the source seeds (not the Stage 4 document itself)
      that actually support this pole.
   b. Is the pole directly stated or clearly implied by the source? Or is it the
      Stage 4 agent's extrapolation from adjacent claims?
   c. Note: citing the Stage 4 document or topology document as the source of a
      pole does not count. You need the passage in the original seed.
3. Check: does the tension require incompatible claims from the source to hold?
   Or is it only dramatic because of how Stage 4 framed it?
4. Check: does the tension have BOTH poles evidenced in source material, or does
   only one pole have direct support?

Output format:
TENSION: [TENSION_N label]
POLE_A_STATUS: SOURCE_CONFIRMED | EXTRAPOLATED | ABSENT
POLE_A_EVIDENCE: [quoted passage(s) from seeds, or "none"]
POLE_B_STATUS: SOURCE_CONFIRMED | EXTRAPOLATED | ABSENT
POLE_B_EVIDENCE: [quoted passage(s) from seeds, or "none"]
BOTH_POLES_PRESENT: YES | NO
TENSION_IS_GENUINE: YES (if both poles source-confirmed and incompatible) | PARTIAL (if one pole extrapolated) | NO (if tension is constructed)
VERDICT: APPROVED | NEEDS_CHANGES
REASONING: [one paragraph]
```

### Verdict logic

- **APPROVED**: All three sub-agents return `VERDICT: APPROVED`. Stage 4 is sound. Pipeline proceeds to Stage 5.
- **NEEDS_CHANGES**: One or more sub-agents return `VERDICT: NEEDS_CHANGES`. The tension in question must be revised:
  - If `TENSION_IS_GENUINE: PARTIAL`: revise the tension to acknowledge that one pole is an extrapolation, not a source claim. Label it explicitly in the Stage 4 document.
  - If `TENSION_IS_GENUINE: NO`: remove the tension from Stage 4. It is not a genuine source-based tension and should not be carried into Stage 5.

Corrective instruction for Stage 4 re-run (partial case):

```
REVISION REQUIRED:
The oracle found that [TENSION_N] has an extrapolated pole. Specifically:
[POLE_A | POLE_B] is the pipeline's inference, not a direct source claim.

Revise the tension to:
1. Clearly label the extrapolated pole as "Synthesis inference (not directly stated
   in source)" rather than presenting it as a source-confirmed position.
2. Retain the sourced pole and its evidence.
3. Adjust the tension's framing: the conflict is between a source-confirmed position
   and an emergent synthesis inference, not a conflict between two source-confirmed positions.
Do not remove the tension entirely — it may still be analytically useful if framed honestly.
```

Corrective instruction for Stage 4 re-run (constructed case):

```
REMOVAL REQUIRED:
The oracle found that [TENSION_N] is a constructed tension: both poles are the
pipeline's extrapolation rather than direct source claims. This tension should be
removed from Stage 4. Do not replace it with another tension unless one is directly
evidenced in the source seeds.
```

### Retry policy

- Maximum 1 correction cycle. If the revised tension still fails the oracle, escalate to Dan with the oracle's finding.
- If all three tensions fail: do not proceed to Stage 5. This indicates the Stage 4 agent is operating beyond the source material. Escalate to Dan with a summary of all three failures.

---

## Integration Notes

### Where each oracle fits in the pipeline

```
Stage 2A seeds produced
    → Oracle A runs (3 sub-agents in parallel, one per seed)
        → APPROVED: proceed to 2B
        → NEEDS_CHANGES: correct flagged seeds, re-run oracle, then proceed

Stage 2B topology produced
    → Oracle B runs (3 sub-agents per load-bearing isomorphism, parallel)
        → APPROVED: proceed to Stage 3
        → NEEDS_CHANGES: reclassify flagged isomorphisms, re-run oracle on reclassified doc

Stage 3, 3.5 run (no oracle gate here — resonance map is descriptive, not claim-making)

Stage 4 tensions produced
    → Oracle C runs (3 sub-agents in parallel, one per tension)
        → APPROVED: proceed to Stage 5 HTML render
        → NEEDS_CHANGES: revise or remove flagged tensions, re-run oracle

Stage 5 HTML render
```

### Design principles carried through all three oracles

1. **Sub-agents read source, not pipeline docs.** Each auditor goes back to the seeds, not to the document being checked. This is the adversarial posture: the oracle cannot be fooled by circular reasoning within the pipeline.

2. **Single-question focus.** Each sub-agent asks one question. Oracle A: is this sourced? Oracle B: is this isomorphism actually in this domain? Oracle C: does this pole have evidence? Multi-question oracles lose discrimination.

3. **Verdicts are binary.** APPROVED or NEEDS_CHANGES. No partial approvals. If a sub-agent finds even one coin (Oracle A) or one analogy-only isomorphism (Oracle B), the verdict is NEEDS_CHANGES.

4. **Correction instructions are specific.** The oracle's NEEDS_CHANGES verdict includes the exact corrective instruction to pass to the re-run. The pipeline does not re-run blind — it re-runs with explicit "strip this term" or "reclassify this ISO" instructions.

5. **Escalation caps.** All three oracles have maximum retry counts and explicit escalation conditions. Dan is notified rather than running indefinitely.

### What these oracles do NOT check

- **Stylistic quality.** The oracles check faithfulness to source, not whether the output is well-written.
- **Stage 3 (resonance map).** The resonance map describes which isomorphisms are most structurally central — this is a descriptive, not claim-making stage. A separate oracle could be added here if Stage 3 starts to overclaim, but it is not in scope for this design.
- **Stage 5 (HTML render).** The render is a formatting operation on Stage 4 output. Oracle C already validates Stage 4 content before rendering begins.
- **Source selection.** These oracles assume the sources listed in the seeds are the right sources. They do not check whether important sources were omitted.

---

## Open Questions for Dan

1. For Oracle B: should every isomorphism be checked, or only the flagged ones (ISO-4 and partial ISOs)? Checking all six per run adds cost; checking only flagged ones is faster but assumes the topology agent's self-flagging is reliable.

2. For Oracle C's partial case: when a tension pole is extrapolated, should Stage 4 retain it with explicit labeling (as the current design specifies) or should the pipeline drop it entirely? The current design favors retention-with-honest-framing over removal, but this is a judgment call about how much inference Stage 4 is allowed.

3. Should these oracles run on the V2 outputs retrospectively (to validate what was already produced), or hold until the next pipeline run?

---

*Design doc complete. No code implemented — awaiting Dan's approval.*
*All oracle prompt templates, verdict logic, and retry policies specified.*
*Three oracle types: Anti-Inflation (2A), Isomorphism Integrity (2B), Tension Validity (4).*
