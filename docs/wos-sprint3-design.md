# WOS Sprint 3 — Design Document
*April 2026*

---

## Core Premises

**P1: The pipeline can execute UoWs cleanly, but it cannot govern itself.**
S2-C's clean run (one diagnosis, one prescription, one execution, seven minutes total) confirmed that the dispatch infrastructure, when correct, works. What it cannot do is regulate its own rate, detect its own failure modes at the lifecycle level, or make the commitment gate irreversible. Sprint 3 installs governing structures, not more execution capability.

*Falsifiable:* If, at Sprint 3's end, a UoW can hit the hard cap and be manually reset indefinitely without triggering resource recovery, this premise has not been addressed. If the early-warning threshold can be silently skipped by a non-sequential `lifetime_cycles` value, this premise has not been addressed.

**P2: Execution capability built ahead of governing structures requires human rescue at every scale boundary.**
Sprint 2 required 4–5 manual decide-retry resets per UoW (S2-A, S2-B) and three emergency mid-sprint bug fixes before the pipeline could run cleanly. The mitochondrial model identifies this as a structural consequence, not an operational one: the hard cap is a pause, not a gate; `steward_cycles` resets, so the lifetime commitment is not enforced; the corrective trace is logged but not enforced as a temporal gate; and the observation loop has no alert for either failure direction (starvation or toxicity). As long as these are absent, every sprint will require human rescue operations for UoWs that hit structural dispatch boundaries.

*Falsifiable:* If Sprint 3 installs no governing structures and the next sprint again requires manual resets beyond a single human correction, this premise is confirmed.

**P3: The oracle-for-docs gate was introduced in Sprint 2 but is structurally unenforced at 70% SLA.**
The Sprint 2 retro was oracle-reviewed. The design audit was oracle-reviewed. But the gate currently depends on convention: there is no YAML frontmatter protocol that makes a document's oracle status machine-readable, and there is no structural check that blocks delivery of a document that has not been oracle-reviewed. Sprint 3 must install this structural enforcement, or the gate degrades under sprint pressure.

*Falsifiable:* If, at Sprint 3's end, a substantial artifact (>500 words, synthesized from multiple sources) can be delivered to Dan without a machine-readable oracle-approval record, this premise has not been addressed.

**P4: The `==` vs `>=` inconsistency in lifecycle gates is a governing-structure defect, not a style issue.**
The hard-cap check in the Steward correctly uses `>= _HARD_CAP_CYCLES`. The early-warning check introduced in PR #690 uses `== _EARLY_WARNING_CYCLES`. Per the PR #690 oracle advisory: `==` is correct under normal operation (cycles increment by exactly 1) but fragile under manual data intervention — if `lifetime_cycles` or `steward_cycles` is set non-sequentially, the early warning fires zero times while the hard cap still catches runaway UoWs. Advisory notifications have a lower cost of false-positive than false-negative. Defensive form is `>=`.

*Falsifiable:* The expression `uow.lifetime_cycles + new_cycles == _EARLY_WARNING_CYCLES` can be made to silently skip by setting `steward_cycles` to a non-sequential value. A data repair that sets `steward_cycles=0` after `lifetime_cycles=3` causes the sum to increment from 3 to 4 to 5, skipping `== 4` if the check fires at step 5. `>=` closes this gap.

**P5: The github-issue-cultivator's absence is a starvation signal, not a backlog management note.**
The cultivator's task file is missing from `scheduled-jobs/tasks/`. Its entry is absent from `jobs.json` entirely — it is not disabled, it is gone. The V3 proposal identifies the Filing Cultivator as the actor that converts confirmed seeds to GitHub issues. Without it, the pipeline's upstream supply of UoWs depends entirely on manual issue creation. This is equivalent to the cristae junction being open: new work does not enter the system through the structured germination path. Restoring the cultivator is not maintenance — it is upstream supply restoration for a self-regulating pipeline.

*Falsifiable:* If the cultivator remains absent at Sprint 3's end, the system's UoW supply is manual by definition, and the "self-proposing work system" premise of WOS V3 remains aspirational.

---

## Expectations

By the end of Sprint 3, the following must be demonstrably true:

1. **Commitment gate is irreversible at the cleanup arc level.** A UoW that hits `_HARD_CAP_CYCLES` triggers: artifact archival, failure trace written to `orchestration/failure-traces/`, `close_reason = "hard_cap_cleanup"` set, source issue commented. Deciding to retry requires explicit operator override, not just `decide-retry`.

2. **Corrective trace absence blocks re-prescription for exactly one heartbeat.** When `trace.json` is absent on Steward re-entry, the Steward returns `WaitForTrace` and does not prescribe. On the next heartbeat: if trace is present, prescribe with trace context; if still absent after the configurable window, log and proceed. Zero `trace_gate_contract_violation` entries in the audit log for UoWs that have complete executor returns.

3. **Early-warning threshold uses `>=`.** The expression `uow.lifetime_cycles + new_cycles == _EARLY_WARNING_CYCLES` is replaced with `>= _EARLY_WARNING_CYCLES`. A test with non-sequential `lifetime_cycles` values confirms the advisory fires correctly.

4. **Asymmetric governor emits structured observations.** A cron-direct script queries queue depth over a rolling window and emits a `write_task_output` observation (not a Telegram alert) when backlog is empty for >6 consecutive hours or exceeds 10 UoWs for >3 consecutive cycles.

5. **YAML frontmatter oracle gate is structural.** Documents designated as substantial artifacts carry a machine-readable frontmatter block (`oracle_status: approved | pending | not_required`, `oracle_pr:`, `oracle_date:`). A pre-delivery check verifies `oracle_status: approved` before the document is surfaced to Dan.

6. **github-issue-cultivator is restored.** Task file exists at `scheduled-jobs/tasks/github-issue-cultivator.md`, job is registered in `jobs.json`, and the job runs at its configured schedule.

7. **`TestBackpressureSkipsRePrescription` — 4 pre-existing failures on main are resolved or explicitly assigned to backlog with a documented root cause.** Sprint 3 does not inherit a broken test suite.

---

## Assumptions

**A1: The startup-sweep claim-awareness gap (advisory from PR #688 oracle) is low-blast-radius in practice.**
PR #688 fixed the done-transition WHERE guard. The advisory noted that the startup sweep may still reclassify UoWs that are actively being processed if the sweep runs during a narrow window. This is assumed to be a low-frequency race condition that does not require Sprint 3 intervention. If it manifests during Sprint 3, it becomes a blocker.

**A2: The caretaker's in-flight detection (PR #683) is sufficient for Sprint 3 operations.**
The caretaker was fixed to protect `active` and `ready-for-executor` UoWs from expiry when their source issues close. Sprint 3 assumes this fix holds for its UoWs. The deeper design question (should Dan's explicit decision be required when source issue closes mid-execution?) is deferred to Sprint 4.

**A3: The `==` to `>=` fix in the early-warning threshold is a single-line change with no behavioral risk in normal operation.**
Under normal operation (cycles increment by 1), `==` and `>=` produce identical results at the threshold. The risk is exclusively in non-sequential data states. This assumption may be wrong if there is a code path that sets `steward_cycles` non-sequentially in normal operation — that path must be verified before the UoW is dispatched.

**A4: The oracle-docs gate can be implemented via YAML frontmatter without requiring a schema migration.**
The gate enforcement is designed as a pre-delivery check in the subagent prompt, not as a DB constraint. This is assumed to be sufficient for Sprint 3. A machine-enforced DB gate would require schema work and is deferred.

**A5: The `TestBackpressureSkipsRePrescription` failures are pre-existing and not load-bearing for Sprint 3 UoWs.**
The retro explicitly notes "4 pre-existing test failures on main, not introduced by Sprint 2." Sprint 3 assumes these failures do not cascade into new test failures when Sprint 3 PRs land. If Sprint 3 PRs interact with the backpressure re-prescription path, this assumption is wrong and the test must be fixed before the PR merges.

**A6: The github-issue-cultivator restoration requires writing a new task file, not debugging a broken existing one.**
The task file is absent from `scheduled-jobs/tasks/` and the job is absent from `jobs.json`. The restoration is assumed to be new authorship (write a new task file per the V3 Filing Cultivator design), not debugging a corrupt state.

---

## What Sprint 2 Taught Us (Inputs to Sprint 3)

**The commitment gate is structurally absent.** S2-A ran ~20 diagnosis cycles and shows `steward_cycles = 1` at closure. Hard cap is a pause mechanism: it surfaces to Dan, nothing is cleaned up, and the UoW can be reset indefinitely. PR #687 added `lifetime_cycles` as a cumulative counter — this is the prerequisite for a real commitment gate, but the cleanup arc (archive, failure trace, close_reason set) was not built. Sprint 3 must build the cleanup arc now that the prerequisite counter exists.

**Co-occurring bugs are invisible at the unit-test level.** The staleness gate bug (#667), the age anchor bug (#668), and the false-complete bug (#685) were each individually manageable. Together they meant: freshly-prescribed UoWs could never be dispatched, previously-orphaned UoWs were continuously reclassified, and UoWs marked complete before any work was done. This is the integration testing gap. Sprint 3 should include at least one integration test covering the full claim-dispatch-return cycle against the real state machine.

**Observability over an incorrect pipeline produces untrustworthy metrics.** The analytics.py metrics (convergence rate, diagnostic accuracy, execution fidelity) computed over Sprint 2's audit log include race conditions, orphan misclassifications, and false-complete records. Sprint 3 should not add more metrics — it should make the denominator trustworthy by fixing the governing structures that create the noise.

**The oracle-for-docs gate works but has no structural teeth.** The Sprint 2 retro and design audit were oracle-reviewed. The process held under sprint pressure. But the gate is entirely conventional — there is no frontmatter protocol, no machine-readable oracle status, and no pre-delivery check. A sprint under higher pressure (more artifacts, faster turnaround) will see the gate slip. Sprint 3 must install structural enforcement before it becomes a problem.

**Pattern: execution built before governing structures requires human rescue at each boundary.** This is the defining learning of Sprint 2. It is not a criticism of Sprint 2 — the dispatch bugs required fixing before governing structures could run cleanly. But with S2-C demonstrating a clean 7-minute run, the pipeline now has a working dispatch layer. Sprint 3's mandate is the governing layer.

**The corrective trace substrate is installed but not enforced.** PR #611 (Sprint 1) installed trace.json writing at all exit paths. The trace gate in the Steward logs a contract violation when trace is absent but does not block re-prescription. The feedback loop is observed but not closed. Sprint 3 closes it.

---

## Sprint 3 UoWs

### S3-A: Commitment Gate Cleanup Arc
**Issue:** https://github.com/dcetlin/Lobster/issues/678
**Why this sprint:** Premise P1 and P2 both identify the absent cleanup arc as the core governing structure gap. PR #687 installed `lifetime_cycles` as the prerequisite counter. The cleanup arc must be built now — it is the difference between a hard cap that pauses and a commitment gate that is genuinely irreversible. Every sprint that runs without this will require human rescue for any UoW that hits structural dispatch failure.
**Success criterion:** When a UoW transitions to `blocked` via `hard_cap`, the following occur atomically: (1) artifacts archived to `orchestration/artifacts/archived/<uow_id>/`, (2) failure trace written to `orchestration/failure-traces/<uow_id>.json` with `reason`, `final_return_reason`, `cycle_count_lifetime`, and `timestamp`, (3) executor context closed explicitly in the hard-cap path, (4) `close_reason = "hard_cap_cleanup"` and `closed_at` set in the registry so the caretaker does not re-open, (5) a comment posted to the source GitHub issue noting the failure trace. A decide-retry after hard_cap_cleanup requires an explicit operator flag — bare `decide-retry` is rejected. PR passes oracle review.
**Hook registration (scope from issue #613):** PR #613 proposed a PostToolUse validator hook (`hooks/validate-workflow-artifact.py`) that validates prescription front-matter before writes commit. The hook was designed in #613 but its registration in the hooks configuration was omitted from the original implementation. S3-A must include the hook registration step — adding `validate-workflow-artifact.py` as a PostToolUse hook in the Claude Code settings — in addition to the steward.py cleanup arc changes. The hook enforces schema at the commit boundary (executor_type, prescribed_skills, posture, estimated_runtime) so that hard-cap cleanup does not archive malformed prescription artifacts. Without hook registration, the validator exists on disk but never fires.
**Dependencies:** PR #687 (`lifetime_cycles` field) must be merged. It was — confirmed in the retro. **S3-B must be merged before S3-A** — see inter-UoW dependency note below.
**Risk:** The cleanup arc's archival step touches the filesystem at `orchestration/artifacts/`. If the outputs directory structure is nonstandard for a given UoW, the archive step may fail. The PR must include a fallback: log the archival failure but do not block the state transition. Cleanup arc failure is preferable to no cleanup arc.

> **Inter-UoW data dependency — S3-A and S3-B must merge in sequence (S3-B first):**
> S3-A's cleanup arc archives the UoW's artifact directory, which includes `trace.json` (the corrective trace written by the executor). S3-B enforces trace presence as a mandatory one-cycle temporal gate: when the Steward re-enters a UoW and `trace.json` is absent, it returns `WaitForTrace`. If S3-A is merged and live before S3-B, a cleanup arc can archive `trace.json` for a UoW that is subsequently retried (via explicit operator override). On the next Steward heartbeat, trace.json is missing from the active path — not because the executor omitted it, but because cleanup archived it — and S3-B's gate fires a false `WaitForTrace`. The correct merge order is: **S3-B lands first**, establishing the trace gate. Then S3-A lands, with the archival path explicitly documented to move `trace.json` into the failure trace record (step 2 above) so it is preserved at `orchestration/failure-traces/<uow_id>.json` and accessible to any post-cleanup audit. Development of S3-A and S3-B may proceed in parallel, but their PRs must be merged sequentially: S3-B → S3-A.

---

### S3-B: Corrective Trace as Mandatory One-Cycle Temporal Gate
**Issue:** https://github.com/dcetlin/Lobster/issues/680
**Why this sprint:** The corrective trace substrate exists (PR #611). The trace gate logs contract violations but does not block re-prescription. This means the cristae-junction analog — mandatory temporal spacing between execution and next prescription — is absent. Without it, the Steward re-prescribes immediately after executor dispatch without incorporating what the last execution learned. Premise P1 identifies this as a governing structure gap. The prerequisite (trace.json written at all exit paths) is already met. Sprint 3 closes the loop.
**Success criterion:** In `_process_uow`, when the prescribe branch is reached and the prior executor's `trace.json` is absent, the Steward returns a `WaitForTrace` outcome and transitions the UoW to a waiting state (not `ready-for-steward` — it stays in `diagnosing` for one heartbeat). On the next Steward heartbeat: if `trace.json` is present, proceed with trace-informed prescription; if still absent after one heartbeat interval, log a `trace_gate_timeout` event and proceed with prescription (non-blocking fallback). Zero `trace_gate_contract_violation` entries in the audit log for UoWs that have complete executor returns after this PR lands. PR passes oracle review.
**Dependencies:** PR #611 (trace.json written at all exit paths) — confirmed merged (Sprint 1). **S3-B must merge before S3-A** — see the inter-UoW dependency note in S3-A above. S3-B's trace-gate enforcement must be live before S3-A's cleanup arc can safely archive `trace.json` without triggering false `WaitForTrace` outcomes on retried UoWs.
**Risk:** The `WaitForTrace` state dwell adds one heartbeat (3 minutes) to every UoW cycle. For high-velocity UoWs, this is acceptable. If the heartbeat interval is changed in the future, the temporal gate's meaning changes too — the gate should be documented as "one heartbeat dwell" not "3 minutes" to make this explicit.

---

### S3-C: Early-Warning Threshold — `==` to `>=` Defensive Form
**Issue:** https://github.com/dcetlin/Lobster/issues/694
**Why this sprint:** Premise P4 identifies this as a governing-structure defect. The expression `uow.lifetime_cycles + new_cycles == _EARLY_WARNING_CYCLES` silently skips the advisory notification if `lifetime_cycles` is set non-sequentially (e.g., via data repair). The hard cap at `>= _HARD_CAP_CYCLES` uses the correct defensive form. The inconsistency means the early-warning notification — the advisory signal before commitment — can be silently absent under data intervention, while the hard commitment gate still fires. The cost of this fix is one character. The cost of the silent failure is a missed advisory notification before commitment. This must ship in Sprint 3 so it cannot be forgotten.
**Success criterion:** The expression `uow.lifetime_cycles + new_cycles == _EARLY_WARNING_CYCLES` is replaced with `>= _EARLY_WARNING_CYCLES` in `steward.py`. A test is added with non-sequential `lifetime_cycles` values (e.g., `lifetime_cycles=3`, `steward_cycles=0` → after prescription `3+1=4 >= 4` fires correctly, vs old form where `3+1 != 4` is false). A GitHub issue is filed first to track the advisory. PR passes oracle review.
**Dependencies:** PR #690 (lifetime_cycles field) must be merged. It was — confirmed in the retro.
**Risk:** Low. Under normal operation, `==` and `>=` produce identical results at the threshold. The only risk is if there is a hidden code path that sets `steward_cycles` non-sequentially in normal execution — this must be verified before dispatch.

---

### S3-D: Asymmetric Governor — Backlog Starvation and Toxicity Observation
**Issue:** https://github.com/dcetlin/Lobster/issues/681
**Why this sprint:** Premise P1 and the V3 proposal (Section 8, item 7: scaling governor) both identify the asymmetric governor as a required governing structure. The current pipeline treats zero queue depth as neutral. The mitochondrial model identifies extended zero queue as a starvation signal (cultivator has stopped proposing) and extended full queue as a toxicity signal (executor is under-capacity). Both are failure modes. Neither produces an automated signal. Sprint 3 installs a minimal observation pass — not an alert system, not autonomous action — that makes both failure directions visible.
**Success criterion:** A cron-direct script (`wos-queue-monitor.py` or equivalent) queries `uow_registry` for the count of `ready-for-steward + ready-for-executor + active` UoWs on each run. It appends the count and timestamp to `data/queue-depth-history.jsonl`. When queue depth has been 0 for more than 6 consecutive hours, it calls `write_task_output(job_name="wos-queue-monitor", output="STARVATION: queue depth 0 for 6+ hours", status="success")`. When queue depth exceeds 10 for 3 or more consecutive readings, it calls `write_task_output` with a toxicity observation. Dan reviews these during engagement windows; the script takes no autonomous action. The script is registered in `jobs.json` as a Type C (cron-direct) job. PR passes oracle review.
**Dependencies:** None. This is a standalone observation script.
**Risk:** The observation window (6 hours for starvation, 3 readings for toxicity) is a design parameter that may need tuning after real operation. Sprint 3 ships the mechanism with these defaults; Dan can adjust via `update_scheduled_job` after observing real behavior.

---

### S3-E: Oracle-Docs YAML Frontmatter Protocol
**Issue:** Not yet filed — identified as structural gap in Sprint 2 process review
**Why this sprint:** Premise P3 identifies the oracle-docs gate as structurally unenforced. The gate works under sprint pressure when followed by convention, but has no machine-readable oracle status and no pre-delivery check. A single artifact delivered without oracle review — under a future sprint with higher throughput — defeats the gate entirely. Sprint 3 installs the protocol now, before the gate slips.
**Success criterion:** (1) A YAML frontmatter schema is defined and documented: `oracle_status: approved | pending | not_required`, `oracle_pr: <PR_URL or null>`, `oracle_date: <ISO date or null>`. (2) The subagent prompt template for substantial artifact tasks includes a pre-delivery check: "Before calling send_reply, verify the document's frontmatter has `oracle_status: approved`. If not, call write_task_output with status=pending and stop." (3) This Sprint 3 design document itself is retroactively tagged with the frontmatter schema (its oracle_status will be `pending` until oracle review completes). (4) The oracle review template in `.claude/agents/` or equivalent is updated to include a "write frontmatter" step as the final action before APPROVED verdict. PR passes oracle review (meta: the oracle reviews the oracle gate protocol).
**Dependencies:** None. This is a documentation and prompt-engineering change, not a code change.
**Risk:** The pre-delivery check is in the subagent prompt, not a hard DB constraint. A subagent that does not read the prompt carefully can skip it. Full enforcement requires a hook or DB gate — that is Sprint 4 scope. Sprint 3 ships the protocol and the convention; Sprint 4 can harden it.

---

### S3-F: Restore github-issue-cultivator
**Issue:** Not yet filed — cultivator task file and jobs.json entry are absent
**Why this sprint:** Premise P5 identifies the cultivator's absence as a starvation signal. The Filing Cultivator is the upstream supply actor in the V3 pipeline: without it, UoW supply is entirely manual. The task file is absent from `scheduled-jobs/tasks/` and the job is absent from `jobs.json` (confirmed by inspection). This is not a disabled job — it does not exist. Sprint 3 restores it because the asymmetric governor (S3-D) cannot accurately diagnose starvation without knowing whether the cultivator is running.
**Success criterion:** (1) `scheduled-jobs/tasks/github-issue-cultivator.md` exists with a task description matching the Filing Cultivator design from the V3 proposal: reads a confirmed seed list, writes `success_criteria` at germination, files GitHub issues with the `wos` label on `dcetlin/Lobster`. (2) The job is registered in `jobs.json` with `enabled: true` and an appropriate schedule (weekly or bi-weekly). (3) The job runs at least once successfully before Sprint 3 closes — verified via `check_task_outputs`. PR passes oracle review.
**Dependencies:** S3-D (asymmetric governor) is a soft dependency — the governor's starvation detection is more meaningful when the cultivator is running. S3-F can proceed in parallel with S3-D.
**Risk:** The Filing Cultivator's task description must distinguish it from the Tending Cultivator (orientation-register, not automatable). If the task file conflates the two, it will attempt to automate orientation-register work. The task file must be scoped explicitly to: "take a confirmed seed from a designated list, write success_criteria, file a GitHub issue." The seeding mechanism (what goes into the confirmed seed list) is out of scope for Sprint 3 — the cultivator reads a manually-maintained list initially.

---

### S3-G: Resolve TestBackpressureSkipsRePrescription (4 pre-existing failures)
**Issue:** Pre-existing — noted in Sprint 2 retro, not yet filed as a standalone issue
**Why this sprint:** Premise A5 assumes these failures do not cascade. But shipping Sprint 3 PRs onto a main branch with 4 known test failures means oracle reviewers must manually distinguish new failures from pre-existing ones. This is a friction multiplier. Sprint 3 cannot claim to be installing governing structures while leaving the test suite in a known-broken state. The failures should be resolved or explicitly root-caused and filed with a blocking label — before Sprint 3 PRs land.
**Success criterion:** Either: (a) the 4 failing tests in `TestBackpressureSkipsRePrescription` pass on main after a targeted fix; or (b) the root cause is documented in a GitHub issue with a clear verdict ("pre-existing, fix deferred to Sprint 4, does not affect Sprint 3 dispatch paths") and the issue is linked from the Sprint 3 design doc. A PR that fixes the failures passes oracle review if path (a).
**Dependencies:** None — this is a standalone test investigation.
**Risk:** The backpressure re-prescription path may interact with the corrective trace temporal gate (S3-B). If so, S3-G and S3-B must be developed in sequence, not parallel. Verify whether `TestBackpressureSkipsRePrescription` exercises the `trace_gate_waited` path before dispatching S3-B independently.

---

## Process Evolution from Sprint 2

**Oracle-docs gate: from convention to protocol.**
Sprint 2 established the convention that substantial artifacts (>500 words, synthesized from multiple sources) require oracle review before delivery. Sprint 3 installs the structural protocol (S3-E): YAML frontmatter with machine-readable `oracle_status`, oracle template updated to include frontmatter write as a final step, and pre-delivery check in subagent prompts. The gate was 70% enforced by convention; it will be structurally enforced by protocol after Sprint 3.

**Governing structures before execution capability.**
Sprint 2's pattern was: fix dispatch bugs, add observability, run UoWs. Sprint 3 reverses the priority: install governing structures first (commitment gate cleanup arc, temporal gate enforcement, asymmetric governor), restore upstream supply (cultivator), fix the test suite — then run UoWs. No new observability metrics are proposed for Sprint 3. The analytics denominator must stabilize before new metrics are added.

**Single-PR, single-reviewer integration testing.**
Sprint 2's co-occurring bug problem (three bugs each invisible in isolation, catastrophic together) points to an integration testing gap. Sprint 3 should include at least one integration test per governing-structure PR that exercises the full state machine path from `ready-for-steward` through executor claim, return, and steward re-entry — not just unit tests of individual functions. This is a PR quality bar, not a separate UoW.

**Pre-flight checklist is a hard gate.**
Sprint 2's pre-flight checklist was advisory. Sprint 3 treats pre-flight as a hard gate: Sprint 3 does not start until all pre-flight items are confirmed. The items are listed in the Sprint 3 Pre-flight Checklist below.

**File a GitHub issue before dispatching any UoW.**
S3-C and S3-E require filing issues before dispatch (they have no existing issue numbers). This is a process requirement: every Sprint 3 UoW must have a GitHub issue at `dcetlin/Lobster` before its task is dispatched to a subagent. Issues provide the audit trail that the caretaker uses for lifecycle management.

---

## What We're Explicitly NOT Doing

**Vision Object integration (sc-1, sc-2, sc-3 from vision.yaml).**
The design audit correctly identified that vision.yaml's stated Phase 1 completion criteria (Registry query answering "what should I work on?", agents citing vision fields as routing basis, morning briefing staleness check) remain unbuilt and unadvanced by Sprint 2. Sprint 3 also does not address them. Reason: these require a stable, self-governing pipeline as their substrate. Installing governing structures first is the correct sequencing. Vision Object integration is Sprint 4 scope.

**Register-aware executor routing (PR B from the V3 proposal) and corrective trace injection into prescriptions (PR C).**
Both are named V3 features from the original PR sequence (wos-v3-steward-executor-spec.md). Neither is in Sprint 3. Reason: PR B (register-appropriate dispatch table) and PR C (Steward reads trace context at prescription time) both require the corrective trace temporal gate (S3-B) to be enforced first. If trace injection is added to prescriptions before the temporal gate enforces trace presence, the injection path will silently fall through to empty-trace prescriptions for the majority of UoWs. S3-B is the prerequisite; PR B and PR C are Sprint 4 scope.

**Dan interrupt feedback arm.**
The V3 proposal (Section 8, item 5) identifies the Dan interrupt path as a delivery system with no receiver: items are surfaced to Dan but his reply is not detected and does not close the UoW loop. This is a known design gap. It is not in Sprint 3 because it requires detecting and parsing Dan's reply in the dispatcher, routing it back to the Steward, and writing a closure record — this is a substantial new actor (the feedback arm) that belongs in a dedicated sprint. Sprint 3's scope is governing structures for the existing dispatch loop, not new actors.

**Scaling governor (S4 from the V3 convergence docs).**
The S4 seed in wos-v3-convergence.md proposes a scaling governor that modulates batch size or execution rate based on recent success signals. This is not S3-D (which is an observation pass, not a modulation mechanism). The scaling governor requires: (1) clean denominator in the analytics layer (requires stable dispatch), (2) proven asymmetric observation (S3-D must run for at least one sprint), (3) an autonomy gate design for governor-triggered throttling. All three are Sprint 4+ scope.

**Adding new analytics metrics.**
The analytics.py layer from S2-C (convergence_rate, diagnostic_accuracy, execution_fidelity) computes over an audit log that includes Sprint 2's race conditions, orphan misclassifications, and false-complete records. Sprint 3 does not add new metrics. The correct action after Sprint 3 is to re-run the existing analytics against the cleaner post-Sprint-3 audit log and assess whether the metrics become meaningful. New metrics are Sprint 4+ scope.

**Caretaker escalation redesign.**
The design audit (Item 5) raised the question of whether the caretaker should require Dan's explicit decision when a source issue closes mid-execution (not just a grace period or in-flight detection). This is a genuine design question — the current implementation (PR #683, in-flight protection) is a practical fix. The deeper design (caretaker escalation protocol under ambiguity) is deferred to Sprint 4. Sprint 3 assumes PR #683's fix holds.

---

## Sprint 3 Pre-flight Checklist

The following must be true before Sprint 3 starts:

```
[ ] WOS execution is disabled: wos-config.json has "execution_enabled": false
    Verify: cat ~/lobster-workspace/data/wos-config.json

[ ] PRs #687 and #690 are merged on main (prerequisites for S3-A and S3-C)
    Verify: gh pr list --repo dcetlin/Lobster --state merged | grep -E "687|690"

[ ] No active, stuck, or orphaned UoWs from Sprint 2 remain in the registry
    Verify:
      sqlite3 ~/lobster-workspace/orchestration/registry.db \
        "SELECT id, status, steward_cycles FROM uow_registry \
         WHERE status NOT IN ('done','expired','cancelled','failed') \
         ORDER BY created_at;"
    Expected: 0 rows

[ ] TestBackpressureSkipsRePrescription root-cause is documented (S3-G pre-flight)
    Verify: GitHub issue exists for the failures, or failures are fixed on main

[ ] GitHub issues filed for S3-C and S3-E (S3-C: #694; S3-E: no existing issue number)
    Verify: gh issue list --repo dcetlin/Lobster --state open | grep -E "early.warning|frontmatter"

[ ] orchestration/failure-traces/ directory exists (needed by S3-A cleanup arc)
    Verify: ls ~/lobster-workspace/orchestration/failure-traces/ 2>/dev/null || echo "needs creation"
    Action if missing: mkdir -p ~/lobster-workspace/orchestration/failure-traces/

[ ] orchestration/artifacts/archived/ directory exists (needed by S3-A)
    Verify: ls ~/lobster-workspace/orchestration/artifacts/archived/ 2>/dev/null || echo "needs creation"
    Action if missing: mkdir -p ~/lobster-workspace/orchestration/artifacts/archived/

[ ] data/queue-depth-history.jsonl does not pre-exist with stale data (S3-D)
    Verify: ls ~/lobster-workspace/data/queue-depth-history.jsonl 2>/dev/null || echo "clean"
    Action if exists: review contents and truncate if stale

[ ] Sprint 3 design doc has oracle_status: approved in frontmatter (oracle gate)
    This document itself must pass oracle review before Sprint 3 starts.
    Oracle review of this document IS the gate.

[ ] This sprint is run from a clean worktree on main:
    git -C ~/lobster status
    Expected: clean working tree, on main branch
```

**Enable WOS execution only after all pre-flight items are confirmed and at least S3-A, S3-B, and S3-C are merged.** The governing structures must be in place before UoWs run against them.

---

## Sprint 3 COMPLETE Definition

Sprint 3 is complete when all five gates below are satisfied. Partial completion does not count. If a gate cannot be stated as closed from memory, the sprint is not done.

**UoW gate.** All seven UoWs (S3-A through S3-G) have their corresponding PRs merged on main with an `oracle_status: approved` frontmatter entry recorded in `oracle/decisions.md`. A UoW whose PR is open, blocked, or oracle-pending holds the gate. S3-B must precede S3-A per the inter-UoW dependency — the gate verifies merge order, not just merge count. S3-G (pre-existing test failures) is satisfied by either a passing fix on main or a root-cause issue linked here with an explicit "does not block Sprint 3 dispatch paths" verdict from the oracle.

**Integration test gate.** The full claim-dispatch-return cycle integration test referenced in "What Sprint 2 Taught Us" passes on main. Specifically, a test that starts with a UoW in `ready-for-steward`, exercises prescription, executor claim, executor return with trace.json written, and Steward re-entry with trace-informed prescription — all against the real state machine, not mocks of individual functions. The test name or file path is recorded here before Sprint 3 closes. A passing unit test suite with no integration coverage of the end-to-end path does not satisfy this gate.

**Observability gate.** The wos-queue-monitor (S3-D) is registered in `jobs.json`, has run at least once at its configured schedule, and has written at least one successful `write_task_output` observation (even if that observation is "queue depth: 2, nominal"). A script that exists on disk but has never executed does not satisfy this gate. The gate is closed by verifying a non-empty entry in `check_task_outputs` for `job_name="wos-queue-monitor"`.

**Oracle gate.** This document carries `oracle_status: approved` in its frontmatter. If S3-E (the oracle-docs frontmatter protocol) lands first, its schema governs — this document must be retroactively tagged. The gate closes when the frontmatter is present and the corresponding oracle PR is recorded in `oracle/decisions.md`. An oracle review conducted but not recorded in machine-readable frontmatter does not satisfy this gate; that is precisely the structural gap S3-E exists to close.

**Retro gate.** A Sprint 3 retrospective document exists at `docs/wos-sprint3-retro.md`, has been oracle-reviewed (with `oracle_status: approved` in its frontmatter), and is committed on main. The retro is not a perfunctory sign-off — it must address the questions identified in the Meta-Monitoring section below. A retro written but not oracle-reviewed, or oracle-reviewed but not committed, does not close the gate.

---

## Meta-Monitoring

This section describes how Sprint 3 is monitored while it is in flight — not as a post-hoc review, but as a live operational discipline. The sprint is not a batch of PRs; it is a governed process. These signals determine whether the sprint is progressing or stuck.

**Signals that a UoW is progressing versus stuck.** A UoW is progressing if its GitHub issue has received a substantive comment (oracle advisory, PR link, implementation note, or merge confirmation) within the last 72 hours. A UoW is stuck if its issue is open, no PR has been filed, and there has been no comment for more than 72 hours. Stuck is not the same as blocked: a UoW waiting on a dependency (e.g., S3-A waiting for S3-B) is blocked by a known condition, not stuck. Blocked UoWs require no intervention unless the blocking UoW is itself stuck. A stuck UoW with no blocker requires Dan's attention within the next engagement window.

**Inter-UoW ordering is tracked by explicit merge-order verification.** The S3-B-before-S3-A dependency is the only hard sequencing constraint in Sprint 3. It is not enforced by the dispatch system — it must be enforced by the human who merges the PRs. The dispatcher should note, at the time S3-A's PR is submitted for oracle review, whether S3-B's PR has already been merged on main. If S3-B is not yet merged, the oracle's advisory on S3-A must note the dependency explicitly, and the merge must be held. Tracking this in flight means: when S3-A's PR opens, verify `gh pr list --repo dcetlin/Lobster --state merged | grep S3-B` before approving merge. All other UoWs have no hard ordering and may be developed in parallel.

**What constitutes a sprint blocker requiring Dan's attention versus dispatcher-handled.** The dispatcher can handle: filing a GitHub issue before dispatch (S3-C and S3-E have no issues yet), checking pre-flight items against the checklist, verifying merge counts, and writing task outputs. Dan's attention is required for: a UoW stuck for more than 72 hours with no known blocker, a merge-order violation (S3-A merged before S3-B), a test failure on main that the oracle cannot attribute to pre-existing failures, and any oracle verdict of NEEDS_CHANGES that has not been acted on within 48 hours. The asymmetric governor (S3-D) does not alert Dan directly — it writes observations to `check_task_outputs`. Dan reviews those during engagement windows; none of them are sprint blockers on their own.

**Post-completion reflection trigger.** The retro begins when both of the following are true: (1) all seven UoWs have oracle-approved PRs merged on main, and (2) the integration test gate passes. The integration test gate is the tighter constraint — it is possible for all UoWs to be merged while the integration test is still failing, in which case the retro waits. The retro is not triggered by the observability gate alone, because S3-D may produce meaningful observations only after a few days of operation, and waiting for it would stall the retro indefinitely. The retro may note that queue-monitor data is preliminary.

**What the retro must capture.** The Sprint 3 retro is not a summary of what was built — that is recorded in the UoW issues and PRs. The retro must answer four questions: (1) What held the sprint up, and was it a structural gap, a process gap, or a contingent problem? If structural, what governing structure would close it — and is that Sprint 4 scope? (2) What surprised us, positively or negatively, about how the governing structures interacted once live? Specifically: did the S3-B temporal gate produce any false `WaitForTrace` outcomes, and if so, under what conditions? (3) Which oracle advisory patterns appeared more than once — and should any of them be promoted to `oracle/learnings.md` as durable rules? (4) How should Sprint 4 be sequenced differently, given what Sprint 3 demonstrated about the relationship between governing structures and execution capability? A retro that cannot answer these four questions from the sprint record is not ready for oracle review.

---

*Document status: PENDING oracle review. Frontmatter oracle_status: pending.*
*Source documents: wos-sprint2-retro.md, wos-v3-proposal.md, wos-v3-sprint-001.md, wos-v3-steward-executor-spec.md, wos-sprint2-design.md, wos-design-audit-2026-04-08.md, oracle/learnings.md, oracle/decisions.md (last 100 lines). Open issues verified via gh issue list.*
