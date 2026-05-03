# Structural Hygiene Audit — 2026-03-31

*Run: 2026-03-31 at 12:00 UTC (scheduled one-shot job)*
*Scope: WOS-era Lobster system — pattern language, structure, connective tissue, redundancy, drift, observability*
*Auditor: background subagent*

---

## Findings by dimension

### 1. Pattern Language Consistency

**Finding 1.1 — MINOR: `issue-sweeper.py` script name diverges from canonical `UoW Registrar` role name**

The design doc (`wos-v2-design.md`) explicitly renamed "Sweeper" to "UoW Registrar" with rationale. The scheduled job is named `issue-sweeper`, and the script is `scheduled-tasks/issue-sweeper.py`. The inline comment in `issue-sweeper.py` line 3 says "Issue Sweeper — UoW Registrar sweep script" — so the dual name is at least acknowledged. No functional issue, but the script name is a naming inconsistency.

Action: Defer — acknowledged in code; renaming would require crontab update and is not a correctness risk.

**Finding 1.2 — MODERATE: `wos-v2-design.md` schema table uses stale field names (`issue_id`, `claimed_at`, `output_file`) not matching live schema**

The design doc's UoW Record Schema table (lines ~142–171) uses `issue_id`, `claimed_at`, `output_file`. The live schema uses `source_issue_number`, `started_at`, `output_ref`. The doc documents these divergences in a note at lines 168–171 — so this is known. However, the main schema table body still uses the old names, which will confuse implementers who read the table without reading the footnote.

Action: File a doc-sync issue if one doesn't already exist (the 2026-03-30 audit called for this — check whether it was filed).

**Finding 1.3 — MINOR: `Cultivator` job name mismatch**

The design doc calls the philosophy pipeline classification agent "the Cultivator." The scheduled job is named `github-issue-cultivator`. This is reasonably consistent but the mapping is implicit — the job cultivates GitHub issues, not philosophy outputs. The `Cultivator` in design is the philosophy pipeline classifier; the `github-issue-cultivator` job runs `src/orchestration/cultivator.py`, which promotes GitHub issues into the WOS registry — a different actor.

Action: Minor terminology confusion; no correctness risk. Could be clarified in job context.

**Finding 1.4 — MINOR: `claimed_at` vs `started_at` used inconsistently in design prose vs schema**

Design prose (e.g., line 148) says "When an Executor claimed this UoW" for `claimed_at`. The actual column is `started_at`. The doc documents this discrepancy in a migration note but the prose above it still uses `claimed_at`. Ambiguous for any implementer cross-referencing.

Action: Same doc-sync issue as 1.2.

---

### 2. Directory Structure Integrity

**Finding 2.1 — MODERATE: `~/lobster-workspace/orchestration/` is not in CLAUDE.md Key Directories**

The directory exists and contains `registry.db` (production WOS data), plus status artifacts. This is a load-bearing runtime directory but is absent from CLAUDE.md's Key Directories section. The 2026-03-30 audit raised this; it has not been fixed.

Action: Add `orchestration/` to CLAUDE.md Key Directories. One-line addition.

**Finding 2.2 — MINOR: Dual philosophy session storage**

Philosophy sessions exist in two locations:
- `~/lobster/philosophy/` — 55 session files (committed to dcetlin/Lobster)
- `~/lobster-workspace/philosophy-explore/` — 48 session files (runtime workspace)

The handoff says "Philosophy files in dcetlin/Lobster only." The workspace copies appear to be duplicates or older originals. The `philosophy-explore-1` scheduled job presumably writes to `~/lobster-workspace/philosophy-explore/` (runtime). This creates two canons.

Action: Clarify in handoff which directory is authoritative at runtime vs. which is the committed archive. The scheduled job task file should specify the write path.

**Finding 2.3 — MINOR: `pending-bootup-candidates/` in philosophy-explore still contains 33 files**

`~/lobster-workspace/philosophy-explore/pending-bootup-candidates/` has 33 files (candidate markdown files generated during philosophy sessions). The audit spec said to check if this directory is "empty/retired as intended." It is not empty — it contains active bootup candidate drafts. Whether these have been promoted to GitHub issues or are orphaned is unclear.

Action: Audit whether these files correspond to existing GitHub issues (#271–#298 range). If all have been promoted, directory can be cleared.

**Finding 2.4 — MINOR: `~/lobster-workspace/design/` contains 7 files, not documented in CLAUDE.md**

The `design/` directory exists (listed in workspace ls) but is not in CLAUDE.md Key Directories. Contents are design artifacts that overlap with `~/lobster/docs/`. Potential duplication risk.

Action: Verify contents; add to CLAUDE.md or merge into `~/lobster/docs/`.

---

### 3. Connective Tissue / Linkage

**Finding 3.1 — MODERATE: `wos-v2-design.md` does not reference `oracle/decisions.md` or `oracle/golden-patterns.md`**

A grep of `wos-v2-design.md` for "oracle" returns zero results. The oracle files exist at `~/lobster/oracle/decisions.md` and `~/lobster/oracle/golden-patterns.md` and are load-bearing (the handoff references "Oracle audit: decisions.md + learnings.md confirm design is excellent enough to implement"). The design doc and oracle are disconnected — an implementer reading `wos-v2-design.md` has no pointer to the oracle.

Action: Add a "See also" or "Oracle" section to `wos-v2-design.md` linking to `oracle/decisions.md` and `oracle/golden-patterns.md`.

**Finding 3.2 — MODERATE: `oracle/decisions.md` does not link back to `wos-v2-design.md`**

`oracle/decisions.md` references specific design decisions but contains no back-link to the design doc that contextualizes them. Reading decisions in isolation loses the "why."

Action: Add a header note in `oracle/decisions.md` pointing to `docs/wos-v2-design.md` as the design context.

**Finding 3.3 — MINOR: Frontier docs not referenced from `user.base.bootup.md`**

`~/lobster/philosophy/frontier/` contains 6 files (approximate-embodiment.md, collapse-topology.md, orient.md, poiesis-poiema.md, registers.md, tol-arc.md). A grep of `user.base.bootup.md` for "frontier" returns no results. These files are accumulating but have no mechanism to surface them into bootup context.

Action: This is a known architectural gap (the Cultivator is the intended mechanism). The frontier dir exists but the pathway back into bootup is aspirational. Accept as deferred pending Cultivator implementation.

**Finding 3.4 — MINOR: Issues #301–#307 are linked via the umbrella issue #301 body**

Issue #301 enumerates sub-issues #302–#307 explicitly. This linkage exists in the body text, which is sufficient for human navigation. No GitHub sub-issue linking (parent/child API) is in use. Acceptable given current tooling.

**Finding 3.5 — MODERATE: Handoff stale entry — "WOS Audit: all 10 UoWs reached done" vs registry showing 1 done**

The handoff (Current State section, "WOS Audit 2026-03-31") says "All 10 UoWs reached done via recovery paths." The live registry shows 1 UoW in `done` and 17 in `pending` plus 184 in `proposed`. This is a significant discrepancy — either the audit result was from a test/dry-run registry, or UoWs were reset after the audit, or the handoff entry is inaccurate. Dan flagged he "may want to reset the registry and start fresh."

Action: Clarify registry state in next handoff consolidation. If the registry was reset, the handoff entry is historical and should be moved to archive or dated clearly.

---

### 4. Redundancy

**Finding 4.1 — RESOLVED: `work-orchestration-system.md` correctly superseded**

The v1 WOS design doc (`~/lobster/docs/work-orchestration-system.md`) correctly opens with "> **Status: SUPERSEDED** — This is the v1 WOS design document. The canonical design is now `wos-v2-design.md`." This is well-handled.

**Finding 4.2 — MODERATE: Two async-deep-work advisor UoWs with identical purpose**

The UoW registry contains two pending UoWs for the same fix:
- `uow_20260331_212b63` (issue #299) — "async-deep-work: fix advisor-register drift — observer posture, not coach posture"
- `uow_20260331_f13081` (issue #300) — "async-deep-work: fix advisor-register drift — observer posture not coach posture" (identical, minor wording difference)

This is a duplicate entry. Issues #299 and #300 appear to be the same bug filed twice.

Action: Close one of the issues as duplicate; delete or expire the duplicate UoW.

**Finding 4.3 — MINOR: `quick-classifier-loop` and `slow-reclassifier-loop` both disabled but their task files retained**

Both jobs are disabled (marked "(disabled)" in list_scheduled_jobs output). They last ran 2026-03-31 01:xx. Task files still exist. No cleanup has occurred.

Action: If permanently retired, mark task files as archived or delete them to reduce noise.

**Finding 4.4 — MINOR: `philosophy-explore` files exist in both `~/lobster/philosophy/` (55 files) and `~/lobster-workspace/philosophy-explore/` (48 files)**

As noted in Finding 2.2 — duplication of philosophy session outputs across two directories with no clear delineation of ownership.

---

### 5. Drift Detection

**Finding 5.1 — RESOLVED: All active scheduled job scripts verified to exist**

Scripts checked:
- `~/lobster/scheduled-tasks/executor-heartbeat.py` — exists
- `~/lobster/scheduled-tasks/steward-heartbeat.py` — exists
- `~/lobster/scheduled-tasks/surface-queue-delivery.py` — exists
- `~/lobster/src/orchestration/cultivator.py` — exists
- `~/lobster/src/orchestration/registry_cli.py` — exists
- `~/lobster/src/classifiers/quick_classifier.py` — exists (disabled job)
- `~/lobster/src/classifiers/slow_reclassifier.py` — exists (disabled job)
- `~/lobster/src/harvest/weekly_harvester.py` — exists

No missing script paths found for active jobs. The previously-noted `quick-classifier-loop.py` path issue is moot since both classifier jobs are now disabled.

**Finding 5.2 — MODERATE: `registry_cli.py` fails when invoked via `uv run` from outside `~/lobster/`**

Running `uv run ~/lobster/src/orchestration/registry_cli.py` directly fails with `ModuleNotFoundError: No module named 'src'`. The tool requires running from within the `~/lobster/` directory (`cd ~/lobster && uv run ...`). The `issue-sweeper.md` task file uses `uv run ~/lobster/src/orchestration/registry_cli.py` without a preceding `cd ~/lobster`, which will fail.

Action: Update `issue-sweeper.md` task file to use `cd ~/lobster && uv run src/orchestration/registry_cli.py` pattern consistently. Also affects `morning-briefing.md` and `uow-reflection.md`.

**Finding 5.3 — MODERATE: 184 UoWs in `proposed` state — registry health**

The registry has 184 proposed UoWs. At the nightly cultivator cadence, this is large backlog accumulation. With BOOTUP_CANDIDATE_GATE=True blocking #271–#298, the 28 open bootup-candidate issues are gated. The remaining proposed UoWs are from the nightly cultivator runs promoting eligible issues. Whether the cultivator is correctly applying idempotency checks (skipping already-proposed records) is unclear — the count seems high if idempotency is working.

Action: Run `registry_cli.py list --status proposed` (with `cd ~/lobster`) to inspect whether this is normal accumulation or runaway duplication. Flag for Dan if duplication is detected.

**Finding 5.4 — MODERATE: Open design decisions in `wos-v2-design.md` that may have been resolved**

`wos-v2-design.md` marks the Cultivator trigger as an "open implementation question." The `github-issue-cultivator` job was created 2026-03-31 and runs daily at 06:00 — this resolves the trigger question (scheduled, daily). The design doc should be updated to reflect the resolution.

Also: the Steward/Executor loop is labeled "Phase 2 — not yet built" in the pipeline diagram. As of 2026-03-31, `steward-heartbeat.py` and `executor-heartbeat.py` exist and are running. The "not yet built" label is stale.

Action: Update `wos-v2-design.md` pipeline diagram to reflect current operational status of Steward/Executor. File or update a doc-sync issue.

---

### 6. Observability

**Finding 6.1 — MODERATE: Registry state is only knowable by querying SQLite directly — no static summary**

The UoWRegistry state (184 proposed, 17 pending, 1 done) is only knowable via `registry_cli.py` or direct SQLite. There is no regularly-updated static summary artifact (e.g., a nightly `orchestration/wos-status.md` write). The `wos-status.md` file exists but its freshness is unclear.

Action: The `steward-heartbeat` or `uow-reflection` jobs should write a nightly status snapshot to `orchestration/wos-status.md`. Check whether `uow-reflection` already does this.

**Finding 6.2 — MODERATE: Handoff contains outdated WOS audit entry**

As noted in Finding 3.5 — the handoff entry about "10 UoWs done" does not match the live registry (1 done). An external reader of the handoff gets a materially incorrect picture of WOS execution history.

Action: Next nightly consolidation should reconcile this entry.

**Finding 6.3 — MINOR: From handoff + wos-v2-design + issue list, overall state IS understandable**

A reader can reconstruct: Phase 2 Steward/Executor is built and running (steward-heartbeat + executor-heartbeat jobs active); 17 UoWs are in `pending` awaiting gate passage; BOOTUP_CANDIDATE_GATE = True blocks #271–#298; implementation PRs #302–#307 are not yet merged (issues open). The state is legible with some effort. No critical observability gap other than the registry summary issue (6.1).

---

## Summary: Top 5 items to address before Phase 2 begins

1. **`registry_cli.py` path invocation failure (Finding 5.2)** — MODERATE, immediate fix. Three scheduled job task files invoke `registry_cli.py` without `cd ~/lobster`, causing `ModuleNotFoundError`. These jobs will fail silently or loudly. Fix: update `issue-sweeper.md`, `morning-briefing.md`, `uow-reflection.md` to use `cd ~/lobster &&` prefix.

2. **Handoff entry accuracy (Finding 3.5 / 6.2)** — MODERATE. The "10 UoWs done" entry in handoff does not match the live registry (1 done). A new collaborator or future session reading the handoff gets a wrong picture of WOS execution state. Fix: update in next nightly consolidation.

3. **`wos-v2-design.md` pipeline diagram stale (Finding 5.4)** — MODERATE. The diagram still says Steward/Executor is "not yet built" — it is built and running. Also the Cultivator trigger (scheduled daily) is resolved. Fix: update the operational status annotations in the diagram. No structural rework needed.

4. **`orchestration/` missing from CLAUDE.md Key Directories (Finding 2.1)** — MODERATE. A load-bearing production directory (`registry.db` lives here) is invisible in the system map. Raised in 2026-03-30 audit and not yet fixed. Fix: one-line addition to CLAUDE.md.

5. **Duplicate UoWs for async-deep-work advisor fix (Finding 4.2)** — MODERATE. Issues #299 and #300 are the same bug filed twice; two UoWs exist for the same work. Fix: close one issue as duplicate, expire the corresponding UoW.

---

## Deferred / Accept

- **Script naming: `issue-sweeper.py` vs `UoW Registrar`** (1.1) — acknowledged in code, not a correctness risk, not worth renaming.
- **`wos-v2-design.md` field name table** (1.2) — the doc already documents the divergences in footnotes. A doc-sync issue was previously called for; check if filed before re-filing.
- **Frontier docs not surfaced in bootup** (3.3) — architectural gap, pending Cultivator implementation. Not actionable now.
- **`pending-bootup-candidates/` not empty** (2.3) — accept for now; auditing 33 files against GitHub issues is deferred to Cultivator implementation.
- **Disabled classifier job task files retained** (4.3) — cosmetic; defer cleanup.
- **Dual philosophy session storage** (2.2 / 4.4) — accept ambiguity until Cultivator defines the canonical write path.
- **`github-issue-cultivator` vs `Cultivator` terminology** (1.3) — minor; not a correctness risk.
