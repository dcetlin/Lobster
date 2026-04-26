# Documentation Canonicalization Report — 2026-04-26

**Scope:** workstreams/, assessments/, philosophy/frontier/, philosophy/weekly/, lobster-user-config/memory/canonical/
**Auditor:** Background subagent (doc-canonicalization-20260426)
**Philosophy:** Remove non-structure until only load-bearing remains. The test for any doc: does reading it feel like recognition, or translation?

---

## Sweep 1: Orphan Docs

Docs not referenced from any canonical index.

**Index state:** `workstreams/INDEX.md` lists only 2 of 9 workstreams (usage-observability, nighttime-directive-2026-04-16). The other 7 workstreams exist on disk but are absent from the index.

| Doc Path | Last Modified | Likely Category | Recommended Action |
|----------|--------------|-----------------|-------------------|
| `workstreams/issue-lifecycle-worker/design.md` | 2026-04-22 | Active — design settled, no README | update (add README, add to INDEX) |
| `workstreams/lobster-system/seeds/context-fractionation.md` | 2026-04-25 | Active seed — no README for workstream | update (add README, add to INDEX) |
| `workstreams/negentropic-sweep/upregulation-flow.md` | 2026-04-22 | Active design — no README for workstream | update (add README, add to INDEX) |
| `workstreams/vision-object/inlet-discriminator-design.md` | 2026-04-22 | Active design — no README for workstream | update (add README, add to INDEX) |
| `workstreams/linear-migration/README.md` (and research.md, rate-limit-log.md) | 2026-04-26 | Active contingency workstream — not in INDEX | update (add to INDEX) |
| `workstreams/upstream-precision-merge/README.md` | 2026-04-26 | Workstream marked COMPLETE — not in INDEX | archive (complete, remove from active) |
| `workstreams/wos/README.md` | 2026-04-23 | Active — not in INDEX | update (add to INDEX) |
| `lobster-user-config/memory/canonical/frontiers/frontier-orient.md` | unknown | Empty scaffold — never populated | delete (unpopulated scaffold, no value) |
| `lobster-user-config/memory/canonical/frontiers/frontier-tol-arc.md` | unknown | Empty scaffold — never populated | delete (unpopulated scaffold, no value) |
| `lobster-user-config/memory/canonical/frontiers/frontier-approximate-embodiment.md` | unknown | Empty scaffold — never populated | delete (unpopulated scaffold, no value) |
| `lobster-user-config/memory/canonical/frontiers/frontier-poiesis.md` | unknown | Empty scaffold — never populated | delete (unpopulated scaffold, no value) |
| `lobster-user-config/memory/canonical/frontiers/frontier-collapse-topology.md` | unknown | Empty scaffold — never populated | delete (unpopulated scaffold, no value) |
| `lobster/philosophy/frontier/STRUCTURE.md` | 2026-04-16 | Active index for frontier/ — no cross-reference from handoff or canonical | none (self-contained, appropriate) |
| `lobster/philosophy/frontier/ooda-retro-2026-04-16.md` | 2026-04-16 | Point-in-time retro — not referenced from anywhere | none (correctly named log, appropriate) |
| `lobster/philosophy/frontier/horizonal-kickoff-template.md` | 2026-04-16 | Canonical tool — not referenced from INDEX or canonical | none (operational template, appropriate) |

**Summary:** 7 workstreams missing from INDEX.md. 5 frontier canonical scaffolds are empty (never populated by Dan, should be removed or accepted as permanent scaffolds). The `upstream-precision-merge` workstream is marked COMPLETE but occupies an active slot in the filesystem without an archive signal.

---

## Sweep 2: Stale Docs

### RALPH References

RALPH was retired 2026-04-20. PR #806 removed it from active code/job descriptions. The following docs still contain RALPH naming:

| Doc Path | RALPH Count | Nature | Recommended Action |
|----------|-------------|--------|-------------------|
| `assessments/ralph-review-premise.md` | 16 | Apr 1 oracle review of RALPH loop design — historical artifact | archive |
| `assessments/ralph-review-dan-heuristics.md` | 13 | Apr 1 design review — historical artifact | archive |
| `assessments/ralph-review-pragmatist.md` | 12 | Apr 1 design review — historical artifact | archive |
| `assessments/ralph-review-corrections.md` | 6 | Apr 1 synthesis — historical artifact | archive |
| `assessments/audit-steward-posture-20260401.md` | 8 | Apr 1 audit using RALPH terminology — historical artifact | archive |
| `assessments/wos-starvation-diagnosis-20260422.md` | 2 | Apr 22 diagnosis using legacy RALPH references | update |
| `assessments/wos-audit-20260422.md` | 2 | Apr 22 audit mentioning RALPH — historical context only | none (point-in-time, appropriate) |
| `assessments/wos-metabolic-efficiency-2026-04-06.md` | 3 | Apr 6 assessment — historical artifact | archive |
| `assessments/mito-eval-session-decisions-2026-04-06.md` | 3 | Apr 6 mito eval — historical artifact | archive |
| `assessments/immediate-triage-2026-04-06.md` | 1 | Apr 6 triage — historical artifact | archive |
| `philosophy/frontier/governor-timing-structure.md` | 1 | Uses RALPH cadence term in one paragraph — living doc | update (rename reference) |
| `philosophy/weekly/2026-04-19-weekly.md` | 1 | Historical weekly — references RALPH cycles as operational term | none (point-in-time retro, appropriate) |
| `lobster-user-config/memory/canonical/priorities.md` | 1 | "KILL RALPH" task item — this is the action item tracking the retirement sweep | none (intentional reference) |
| `lobster-user-config/memory/canonical/sessions/` (multiple) | 1-3 each | Historical session logs | none (point-in-time records) |

### execution_enabled=true Claims

`execution_enabled` is currently `false` (paused 2026-04-26). The following docs claim or imply `true`:

| Doc Path | Issue | Recommended Action |
|----------|-------|-------------------|
| `workstreams/nighttime-directive-2026-04-16/log.md` | Multiple entries state "execution_enabled=true" (from Apr 16 night) | none (historical log, append-only) |
| `assessments/wos-sprint2-design.md` | References execution state as active | none (historical design doc) |
| `lobster-user-config/memory/canonical/sessions/20260416-002.md` | execution_enabled=true as session state | none (historical record) |
| `lobster-user-config/memory/canonical/daily-digest.md` | execution_enabled=true if not regenerated since pause | update (should reflect current false state) |
| `lobster/philosophy/frontier/ooda-retro-2026-04-16.md` | References execution as enabled | none (historical retro) |
| `lobster-user-config/memory/canonical/projects/convergence-domains.md` | Does not explicitly state current state | none (updated as of Apr 23, pre-pause) |

### wos.db Path References

Active DB is `orchestration/registry.db` (1094 rows, 14 migrations). `data/wos.db` is empty and not in use.

| Doc Path | Issue | Recommended Action |
|----------|-------|-------------------|
| `workstreams/wos/reports/db-reconciliation-20260426.md` | Correctly documents the empty `data/wos.db` and confirms `orchestration/registry.db` as active — this is the authoritative reference | none (accurate report) |
| `assessments/wos-impl-spec-2026-03-31.md` | Mar 31 spec predates registry migration — likely references old path | archive (superseded by V3 implementation) |
| `assessments/wos-seed-germination-design-2026-03-31.md` | Mar 31 design — pre-migration | archive |
| `assessments/garden-caretaker-design.md` | Apr 8 design doc — references wos.db or old path patterns | archive (GardenCaretaker is live; design doc superseded by code) |

### Old Migration Patterns / Superseded Executor Patterns (PRs #965-#974)

PRs #965-#974 are all merged. Docs that describe the pre-merge executor as "in progress" or propose these changes as future work are now stale:

| Doc Path | Issue | Recommended Action |
|----------|-------|-------------------|
| `workstreams/wos/assessments/dispatcher-escalation-design-20260426.md` | Status "Design proposal — for Dan's review"; PR #970 (wos_escalate) already MERGED | update (mark implemented) |
| `workstreams/wos/design/wos-execute-router-daemon.md` | Status "NEEDS_CHANGES resolved — awaiting re-oracle (Round 3)"; PR #943 MERGED | update (mark implemented/closed) |
| `workstreams/wos/reports/architectural-proposal-20260426.md` | Already marked "IMPLEMENTED — all 4 directions merged 2026-04-26" | none (accurate) |
| `lobster-user-config/memory/canonical/handoff.md` | "PR #5 (wos_escalate dispatcher handler) is the next work item — awaiting Dan's call to start" — PR #970 is already MERGED; this thread is closed | update (remove stale thread) |
| `assessments/wos-sprint2-design.md` | Pre-V3 design doc (Apr 7) — executor pattern fundamentally changed since | archive |

---

## Sweep 3: Duplication

### wos/reports/*.md — Overlap Analysis

3 reports exist, all dated 2026-04-26:

| Doc | Scope |
|-----|-------|
| `wos/reports/failed-retries-20260426.md` | 19-UoW failure cohort — input data and root cause diagnosis |
| `wos/reports/architectural-proposal-20260426.md` | Design proposals derived from failed-retries cohort — already marked IMPLEMENTED |
| `wos/reports/db-reconciliation-20260426.md` | DB identity audit — confirms active registry path and schema state |

**Finding:** These three are complementary, not duplicative. Each has a distinct purpose: data → diagnosis → db ground truth. No merge warranted. The `architectural-proposal` is now a historical record of decisions made — consider marking it as archive-grade after the resilience PRs settle.

### wos/assessments/*.md vs wos/reports/*.md

`assessments/` in the workstream directory contains only one file: `dispatcher-escalation-design-20260426.md`. This is actually a design doc, not an assessment — it belongs in `wos/design/` alongside `wos-execute-router-daemon.md`. The placement is incoherent.

**Finding:** `dispatcher-escalation-design-20260426.md` in `wos/assessments/` should move to `wos/design/`.

### assessments/ Directory — Mass Overlap with wos/ Design Docs

The `assessments/` directory (81 files) contains docs that span multiple epistemic categories without a coherent taxonomy:
- Pre-V3 WOS design specs (Mar 2026) — `wos-system-map`, `wos-impl-spec`, `wos-seed-germination-design`, `wos-integration-test-design` — all predating the implemented V3
- RALPH review docs (4 files) — historical oracle reviews for a retired feature
- UoW output docs scattered here (`uow_20260422_*`, `uow_20260401_*`) — individual UoW artifacts mixed with system-level design docs
- Active design work that should be in workstream directories (`vision-object-inlet-design.md`, `observation-pipeline-attentional-filter-design.md`, `meta-attentional-design.md`)

**Finding:** `assessments/` is functioning as a catch-all dump rather than a categorized archive. The most significant overlap is between:
- `assessments/vision-object-inlet-design.md` (Apr 22) and `workstreams/vision-object/inlet-discriminator-design.md` (Apr 22) — both are designs for the vision object inlet; the workstream copy is more appropriate; the assessments copy appears to be a near-duplicate from the same UoW

### oracle/learnings.md vs oracle/patterns.md vs oracle/golden-patterns.md

| Doc | Lines | Scope |
|-----|-------|-------|
| `oracle/learnings.md` | 1152 | PR-by-PR antipatterns, two-layer structure (index + archive), grows with each review |
| `oracle/patterns.md` | 75 | WOS loop pattern taxonomy (spiral/cascade/burst/dead-end) — behavioral signals for steward |
| `oracle/golden-patterns.md` | 312 | Positive patterns — successful approaches to encode and replicate |

**Finding:** These are complementary, not duplicative. `learnings.md` = antipatterns; `patterns.md` = WOS behavioral signals; `golden-patterns.md` = positive exemplars. The naming creates mild confusion (all three could be called "patterns"). The scope boundary is clear in content but not in names.

**Secondary finding:** `oracle/decisions.md` (160 lines, 989KB per handoff note — the line count is misleading, likely compressed) holds historical oracle verdicts that the handoff flags as needing a migration decision. It is neither an index nor a living doc — it is a growing audit log with no trim mechanism.

### handoff.md Open Threads vs Actual Issues

Cross-checking handoff Open Threads against `gh issue list`:

| Thread in handoff | Issue State |
|-------------------|-------------|
| Issues #817, #818 — "TRIAGE: Night 8 issues" | #817 OPEN (confirmed), #818 OPEN (confirmed) |
| Issues #801, #802, #803 — "TRIAGE: Night 7 issues" | #801 OPEN, #802 CLOSED, #803 CLOSED |
| Issues #780–#784 — "Night 5 issues still pending triage" | #780 (not found/closed), #781 CLOSED, #782 CLOSED, #783 CLOSED, #784 CLOSED |
| Issue #756 — "AWAIT design response" | OPEN |
| Issue #812 — "myr-system integration" | OPEN |
| "PR #5 (wos_escalate dispatcher handler) awaiting Dan's call" | PR #970 MERGED — thread is CLOSED/stale |
| Issues #962 — "fix retry accounting" | OPEN (even though PRs #965+ addressed it; likely the tracking issue) |

**Finding:** 4 of the 5 "Night 5" issues (780-784) are closed; the handoff thread is stale. Issues 802 and 803 from Night 7 are closed; only 801 remains open. The "PR #5 awaiting Dan's call" thread is stale — PR #970 implemented and merged this feature.

---

## Sweep 4: Index Coherence

### Workstream Directory — README Audit

| Workstream | Has README | README Status |
|-----------|------------|--------------|
| wos/ | Yes | Reasonably current (last updated Apr 23); Phase 2 section references closed issues #840/#841 as open questions without noting they may be resolved |
| usage-observability/ | Yes | Stale — Tier 1 and Tier 2 are COMPLETE (per handoff) but README shows all tiers as "Blocked" |
| nighttime-directive-2026-04-16/ | Yes | Accurate — completed workstream |
| upstream-precision-merge/ | Yes | Accurate — marked COMPLETE |
| linear-migration/ | Yes | Accurate — contingency status |
| issue-lifecycle-worker/ | No README | Only has design.md — orphaned design with no workstream framing |
| lobster-system/ | No README | Has seeds/context-fractionation.md only — unclear scope |
| negentropic-sweep/ | No README | Has upregulation-flow.md only — design exists but no workstream framing |
| vision-object/ | No README | Has inlet-discriminator-design.md only — design exists but no workstream framing |

### INDEX.md Coverage

`workstreams/INDEX.md` (last modified 2026-04-16) lists only:
- usage-observability (active, last active 2026-04-14)
- nighttime-directive-2026-04-16 (active, last active 2026-04-16)

Missing from INDEX: wos, issue-lifecycle-worker, lobster-system, negentropic-sweep, vision-object, upstream-precision-merge (completed), linear-migration.

**Finding:** INDEX covers 2 of 9 workstreams. It is functioning as a stub, not an index.

### handoff.md Open Threads Coherence

| Thread | Status |
|--------|--------|
| WOS pipeline restart decision | Legitimate open — Dan action needed |
| 19 UoWs re-evaluation | Legitimate open — Dan action needed |
| PR #5 (wos_escalate) awaiting Dan's call | Stale — PR #970 merged 2026-04-26 |
| negentropic-sweep cron broken | Legitimate open — unresolved |
| Night 5 issues #780–#784 still pending triage | Stale — #781-784 closed; #780 closed or not found |
| Night 7 issues #801–#803 route to WOS | Partially stale — #802 and #803 closed; only #801 open |
| Night 8 issues #817–#818 | Accurate — both open |
| Blocked UoWs 3cc6ca, c0a82e, 654519 | Unknown — not verifiable from issue list (UoW IDs, not issue numbers) |
| FILE wos_completion.py fragility | Legitimate open — no issue filed yet |
| KILL "RALPH" grep+replace sweep | Legitimate open — PR #806 removed from jobs but codebase still has it |

### frontier/ Docs — Live vs Draft vs Superseded

Per `STRUCTURE.md` convention: living docs (no date prefix), logs (date-prefixed), templates (descriptive noun).

| File | Kind (by convention) | Epistemic Status | Notes |
|------|---------------------|------------------|-------|
| `approximate-embodiment.md` | Living doc | Active — Apr 2 | Correlates with canonical frontier scaffold (unpopulated) |
| `collapse-topology.md` | Living doc | Active — Apr 2 | Correlates with canonical frontier scaffold (unpopulated) |
| `governor-timing-structure.md` | Living doc | Active — Apr 4; contains 1 RALPH reference in one para | Minor stale reference |
| `holographic-epistemology.md` + cluster (4 files) | Living doc series | Active — Apr 9 | Pre-convention cluster; STRUCTURE.md notes this |
| `horizonal-kickoff-template.md` | Template/tool | Active — Apr 16 | Correct placement |
| `metabolic-juice.md` | Living doc | Active — Apr 23 | Recently updated |
| `mito-modeling.md` | Living doc | Active — Apr 4 | No obvious stale indicators |
| `ooda-retro-2026-04-16.md` | Log/retro | Historical — Apr 16 | Correct placement |
| `orient.md` | Living doc | Active — Apr 2 | Correlates with canonical frontier scaffold (unpopulated) |
| `poiesis-poiema.md` | Living doc | Active — Apr 2 | Correlates with canonical frontier scaffold (unpopulated) |
| `registers.md` | Living doc | Active — Apr 2 | No obvious stale indicators |
| `STRUCTURE.md` | Index/tool | Active — Apr 16 | Correct placement |
| `system-metabolism.md` | Living doc | Active — Apr 23 | Recently updated |
| `tol-arc.md` | Living doc | Active — Apr 2 | Correlates with canonical frontier scaffold (unpopulated) |
| `wos-v3-convergence.md` | Living doc | Potentially superseded — Apr 4; WOS V3 is now implemented | update (mark as historical or confirm still-live) |

**Finding on frontier duplicate canonical scaffolds:** The 5 files in `lobster-user-config/memory/canonical/frontiers/` (frontier-orient.md, frontier-tol-arc.md, frontier-approximate-embodiment.md, frontier-poiesis.md, frontier-collapse-topology.md) are all empty scaffolds with the template text "Not yet populated." They duplicate the existence of the living docs in `philosophy/frontier/` without adding content. These scaffolds were created by the routing system and never populated by Dan.

---

## Top 5 Highest-Leverage Actions

**1. Update workstreams/INDEX.md to include all active workstreams (or acknowledge its scope)**

The INDEX covers 2 of 9 workstreams and is 10 days stale. Either extend it to be the authoritative map (add wos, issue-lifecycle-worker, negentropic-sweep, vision-object, linear-migration; mark upstream-precision-merge as complete) or rename it `active-sprint.md` to signal its narrower scope. This is the single highest-leverage action because INDEX is the entry point for orientation — its current state communicates "there are 2 workstreams" when there are 9.

**2. Prune stale handoff.md Open Threads**

3 threads are closed or substantially resolved: "PR #5 (wos_escalate) awaiting Dan's call" (PR #970 merged), Night 5 issues #780-784 triage (4 of 5 closed), Night 7 issues #801-803 (2 of 3 closed). Removing resolved threads reduces cognitive load on every session startup. These are not archive items — they are done; the thread should simply be removed.

**3. Delete the 5 empty frontier canonical scaffolds**

`lobster-user-config/memory/canonical/frontiers/` contains 5 files that are entirely boilerplate — "Not yet populated." They carry no information and create the false impression that the canonical frontier tracking is live. Either populate them or delete them. The living docs in `philosophy/frontier/` are the authoritative source; these scaffolds add nothing.

**4. Archive the 4 RALPH review docs and pre-V3 WOS design docs (8-10 files)**

`ralph-review-premise.md`, `ralph-review-dan-heuristics.md`, `ralph-review-pragmatist.md`, `ralph-review-corrections.md` are historical oracle reviews of a retired feature. They occupy the same directory as live design work and create translation cost when scanning assessments/. Similarly, `wos-system-map-2026-03-31.md`, `wos-impl-spec-2026-03-31.md`, `wos-seed-germination-design-2026-03-31.md`, `wos-integration-test-design-2026-03-31.md` describe a pre-V3 registry design that is fully superseded by the SQLite registry now at `orchestration/registry.db`. Moving these to an `assessments/archive/` subdirectory would make the live design surface area immediately legible.

**5. Update usage-observability/README.md to reflect Tier 1 and Tier 2 as complete**

The README shows all tiers as "Blocked on Tier 1." Tiers 1 and 2 are complete (PRs #753 and #764 per handoff). This is a minor fix but the README is the authoritative status document for that workstream — showing "Blocked" when it is not blocked creates friction for anyone orienting to this workstream. Also: update `workstreams/wos/README.md` section 5 Open Questions to note that issues #840 and #841 have been resolved or are executing in WOS (they are labeled `wos:executing`).

---

*Report generated: 2026-04-26*
*Findings: Sweep 1: 15 | Sweep 2: ~25 | Sweep 3: 8 | Sweep 4: 15*
*Total findings: ~63*
