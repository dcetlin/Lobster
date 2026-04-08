# Sprint 2 UoW Pipeline Report

**Sprint 2 Issues:** [#652](https://github.com/dcetlin/Lobster/issues/652) (S2-A) · [#613](https://github.com/dcetlin/Lobster/issues/613) (S2-B) · [#583](https://github.com/dcetlin/Lobster/issues/583) (S2-C)  
**PRs:** [#672](https://github.com/dcetlin/Lobster/pull/672) · [#673](https://github.com/dcetlin/Lobster/pull/673) · [#674](https://github.com/dcetlin/Lobster/pull/674)

**Generated:** 2026-04-08  
**Registry:** `/home/lobster/lobster-workspace/orchestration/registry.db`  
**Scope:** Three Sprint 2 UoWs — S2-A, S2-B, S2-C

---

## Executive Summary

All three Sprint 2 UoWs are now `done` and their PRs are merged on `dcetlin/Lobster`. However, the path to completion was far from clean end-to-end pipeline execution. The dominant story across all three is that the executor dispatch mechanism was broken in multiple ways, requiring 4–5 rounds of manual `decide-retry` resets plus three pipeline bug fixes before execution could proceed. The actual execution and PR opening were ultimately pipeline-driven (via `wos_execute` inbox messages dispatched by the executor-heartbeat), but the pipeline required extensive manual rescue operations and two mid-sprint bug fix PRs (#667, #668) before it could function.

**Summary verdict by UoW:**

| UoW | Pipeline-driven? | Manual interventions | Cycles (final run) | PR | Merged |
|-----|-----------------|---------------------|-------------------|-----|--------|
| S2-A (issue #652) | Partially | 4 resets + garden-caretaker reset | 1 | #672 | 2026-04-08T03:08 UTC |
| S2-B (issue #613) | Partially | 4 resets + garden-caretaker reset | 1 | #673 | 2026-04-08T03:22 UTC |
| S2-C (issue #583) | Mostly yes | 1 manual approval (proposed→pending) | 1 | #674 | 2026-04-08T03:51 UTC |

---

## UoW Registry Records

### S2-A: per-cycle steward trace logging

| Field | Value |
|-------|-------|
| id | `uow_20260407_c128a4` |
| source | `github:issue/652` |
| status | `done` |
| register | `operational` |
| type | `executable` |
| uow_mode | `operational` |
| steward_cycles (final) | 1 |
| posture | `solo` |
| created_at | 2026-04-07T19:15:04 UTC |
| updated_at | 2026-04-08T02:48:03 UTC |
| started_at | 2026-04-08T02:46:32 UTC |
| completed_at | 2026-04-08T02:48:03 UTC |
| closed_at | — |
| close_reason | — |
| output_ref | `orchestration/outputs/uow_20260407_c128a4.json` |
| prescribed_skills | `["verification-before-completion"]` |
| summary | feat: per-cycle steward trace logging in WOS UoW loop |
| success_criteria | PR open on dcetlin/Lobster adding `_append_cycle_trace()` to steward.py; 2-cycle UoW produces 2 `.cycles.jsonl` entries with required fields |
| route_reason | `steward: execution_complete — output_ref is null or file does not exist or is empty` |

### S2-B: prescription format front-matter + prose split

| Field | Value |
|-------|-------|
| id | `uow_20260405_e54b97` |
| source | `github:issue/613` |
| status | `done` |
| register | `operational` |
| type | `executable` |
| uow_mode | `operational` |
| steward_cycles (final) | 1 |
| posture | `solo` |
| created_at | 2026-04-05T00:30:04 UTC |
| updated_at | 2026-04-08T02:51:02 UTC |
| started_at | 2026-04-08T02:49:31 UTC |
| completed_at | 2026-04-08T02:51:02 UTC |
| closed_at | — |
| close_reason | — |
| output_ref | `orchestration/outputs/uow_20260405_e54b97.json` |
| prescribed_skills | `["verification-before-completion"]` |
| summary | feat(wos-v3): prescription format — front-matter + prose split |
| success_criteria | PR open on dcetlin/Lobster that refactors `_llm_prescribe`; `_parse_workflow_artifact` exists and parses valid/invalid inputs |
| route_reason | `steward: executor_orphan — output_ref is null or file does not exist or is empty` |

### S2-C: observability metrics

| Field | Value |
|-------|-------|
| id | `uow_20260402_1e0f29` |
| source | `github:issue/583` |
| status | `done` |
| register | `operational` |
| type | `executable` |
| uow_mode | `operational` |
| steward_cycles (final) | 1 |
| posture | `solo` |
| created_at | 2026-04-02T19:15:04 UTC |
| updated_at | 2026-04-08T03:30:02 UTC |
| started_at | 2026-04-08T03:28:31 UTC |
| completed_at | 2026-04-08T03:30:02 UTC |
| closed_at | 2026-04-06T17:21:49 UTC (prior cancel, overridden) |
| close_reason | `manual_reset` (from prior cancel) |
| output_ref | `orchestration/outputs/uow_20260402_1e0f29.json` |
| prescribed_skills | `["verification-before-completion"]` |
| summary | feat: WOS prescription pipeline observability metrics |
| success_criteria | PR on dcetlin/Lobster adding `convergence_metrics()` and `diagnostic_accuracy()` to analytics.py with CLI runner |
| route_reason | `steward: first_execution — first_execution: awaiting executor dispatch` |

---

## Per-UoW Audit Trails

### S2-A Audit Trail (uow_20260407_c128a4)

Total audit events: 127 (28 `skipped`, 99 significant)

**Key events in chronological order:**

| Timestamp (UTC) | Event | Transition | Notes |
|----------------|-------|------------|-------|
| 2026-04-07T19:15 | `created` | → proposed | Sprint 2 design approved by oracle at ~23:45 Apr 7 |
| 2026-04-08T00:02 | `status_change` | proposed → pending → ready-for-steward | Auto-advanced by pipeline |
| 2026-04-08T00:03 | `steward_diagnosis` | — | Cycle 0: `first_execution`, no prior entries |
| 2026-04-08T00:04 | `steward_prescription` | — | Cycle 1: LLM prescription, workflow_primitive=functional-engineer |
| 2026-04-08T00:06 | `startup_sweep` | ready-for-executor → ready-for-steward | `executor_orphan`: executor never claimed |
| 2026-04-08T00:06–00:21 | Cycles 1–5: diagnosis + prescription | — | All `executor_orphan`: executor staleness gate defeating re-prescription |
| 2026-04-08T00:21 | `steward_surface` | — | Hard cap hit at cycle 5; UoW blocked |
| 2026-04-08T00:48 | `decide_retry` (1st) | blocked → ready-for-steward | User-initiated reset, steward_cycles reset to 0 |
| 2026-04-08T00:50–01:03 | Cycles 0–5 again | — | Same pattern: executor_orphan → hard cap |
| 2026-04-08T01:03 | `steward_surface` | — | Hard cap again |
| 2026-04-08T01:16 | `decide_retry` (2nd) | blocked → ready-for-steward | User reset again |
| 2026-04-08T01:18–01:33 | Cycles 0–5 again | — | Same executor_orphan pattern |
| 2026-04-08T01:33 | `steward_surface` | — | Hard cap third time |
| 2026-04-08T01:46 | `decide_retry` (3rd) | blocked → ready-for-steward | User reset; PR #667 (staleness gate fix) now being worked |
| 2026-04-08T01:48–01:52 | Cycles 0–1 | — | Still executor_orphan |
| 2026-04-08T01:52 | `decide_retry` (4th) | blocked → ready-for-steward | Manual reset after PR #668 (startup-sweep fix) merged |
| 2026-04-08T01:53 | `manual_reset` | diagnosing → ready-for-executor | Direct skip to ready-for-executor by ops agent |
| 2026-04-08T01:58 | `claimed` | ready-for-executor → active | Executor claimed UoW |
| 2026-04-08T01:58 | `execution_complete` | active → ready-for-steward | Executor marked complete at dispatch time (false-complete bug #669) |
| 2026-04-08T02:00 | `steward_diagnosis` | — | Cycle 2: `execution_complete`, output_ref null → not actually done |
| 2026-04-08T02:00 | `steward_closure` | — | False closure: output_ref `uow_20260407_c128a4.result.json` believed present |
| 2026-04-08T02:14 | `decide_retry` (5th, user) | blocked → ready-for-steward | Sprint completion check showed no PR opened |
| 2026-04-08T02:30 | `archived_by_caretaker` | — | Garden caretaker expired UoW because issue #652 was closed |
| 2026-04-08T02:36 | `manual_reset` (ops_agent) | expired → ready-for-steward | Ops agent override |
| 2026-04-08T02:39 | `steward_diagnosis` | — | Cycle 0: `execution_complete` posture, output_ref null |
| 2026-04-08T02:40 | `steward_prescription` | — | Cycle 1: LLM prescription (real this time, with verification skill) |
| 2026-04-08T02:46 | `claimed` | ready-for-executor → active | Executor claimed for real execution |
| 2026-04-08T02:46 | `execution_complete` | active → ready-for-steward | PR opened, result.json written |
| 2026-04-08T02:48 | `steward_diagnosis` | — | Cycle 1: `execution_complete`, output_ref present |
| 2026-04-08T02:48 | `steward_closure` | — | Genuine closure: `outcome=complete: uow_20260407_c128a4.result.json` |
| 2026-04-08T02:00 | `github_sync` | — | GitHub issue synced |

**Note on steward_cycles discrepancy:** The registry shows `steward_cycles = 1` at completion. This reflects the final reset run, not the cumulative cycle count across all attempts. Across all reset rounds, the steward ran approximately 20+ diagnosis/prescription cycles total.

---

### S2-B Audit Trail (uow_20260405_e54b97)

Total audit events: 422 (294 `skipped`, 128 significant)

**Key events:**

| Timestamp (UTC) | Event | Transition | Notes |
|----------------|-------|------------|-------|
| 2026-04-05T00:30 | `created` | → proposed | Created during earlier WOS sweep |
| 2026-04-06T17:21 | `manual_cancel` | proposed → cancelled | Dan-approved mass cancel to stop steward usage drain |
| 2026-04-08T00:02 | `sprint2_reset` | cancelled → proposed | Reset for Sprint 2 execution |
| 2026-04-08T00:02 | `status_change` | proposed → pending → ready-for-steward | Auto-advanced |
| 2026-04-08T00:04 | `steward_diagnosis` | — | Cycle 0: `first_execution` |
| 2026-04-08T00:06–01:12 | Cycles 0–5 (×3 reset rounds) | — | All executor_orphan; 3 hard caps hit |
| 2026-04-08T00:48, 01:16, 01:46 | `decide_retry` ×3 | blocked → ready-for-steward | User-initiated resets |
| 2026-04-08T01:53 | `manual_reset` | diagnosing → ready-for-executor | Direct ops-agent push to executor queue |
| 2026-04-08T01:58 | `claimed` | ready-for-executor → active | Executor claimed |
| 2026-04-08T01:58 | `execution_complete` | active → ready-for-steward | False-complete bug: dispatch-time not work-done |
| 2026-04-08T02:00 | `steward_closure` | — | False closure (no PR opened) |
| 2026-04-08T02:14 | `decide_retry` | blocked → ready-for-steward | After false-complete discovered |
| 2026-04-08T02:30 | `archived_by_caretaker` | — | Garden caretaker expired (issue #613 closed) |
| 2026-04-08T02:36 | `manual_reset` (ops_agent) | expired → ready-for-steward | Override |
| 2026-04-08T02:40 | `steward_diagnosis` | — | Cycle 0: execution_complete posture, output_ref null |
| 2026-04-08T02:42 | `startup_sweep` | diagnosing → ready-for-steward | diagnosing_orphan classification |
| 2026-04-08T02:42–02:44 | `steward_diagnosis` + `steward_prescription` | — | Cycle 0: executor_orphan; re-prescription issued |
| 2026-04-08T02:49 | `claimed` | ready-for-executor → active | Executor claimed for real work |
| 2026-04-08T02:49 | `execution_complete` | active → ready-for-steward | PR opened, result.json written |
| 2026-04-08T02:51 | `steward_closure` | — | Genuine: `outcome=complete: uow_20260405_e54b97.result.json` |

---

### S2-C Audit Trail (uow_20260402_1e0f29)

Total audit events: 524 (508 `skipped`, 15 significant)

**Key events:**

| Timestamp (UTC) | Event | Transition | Notes |
|----------------|-------|------------|-------|
| 2026-04-02T19:15 | `created` | → proposed | Created 6 days before sprint |
| 2026-04-03T03:11 | `status_transition` | proposed → inactive | Reset past-24h units |
| 2026-04-03T03:11 | `status_transition` | inactive → ready_for_stewart | Selected for early execution |
| 2026-04-06T17:21 | `manual_cancel` | ready_for_stewart → cancelled | Dan-approved mass cancel |
| 2026-04-07T05:00 | `status_change` | failed → proposed | Garden caretaker reactivated (source issue reopened) |
| 2026-04-08T03:23 | `status_change` | proposed → pending → ready-for-steward | Manual sprint2-approve-s2c action |
| 2026-04-08T03:24 | `steward_diagnosis` | — | Cycle 0: `first_execution` (no prior audit entries) |
| 2026-04-08T03:26 | `steward_prescription` | — | Cycle 1: LLM prescription issued |
| 2026-04-08T03:28 | `claimed` | ready-for-executor → active | Executor claimed |
| 2026-04-08T03:28 | `execution_complete` | active → ready-for-steward | PR opened |
| 2026-04-08T03:30 | `steward_diagnosis` | — | Cycle 1: `execution_complete`, output_ref present |
| 2026-04-08T03:30 | `steward_closure` | — | `outcome=complete: uow_20260402_1e0f29.result.json` |
| 2026-04-08T03:30 | `github_sync` | — | GitHub synced |

S2-C had by far the cleanest run: 1 diagnosis, 1 prescription, 1 execution, closure in ~7 minutes. It benefited from S2-A and S2-B's struggles having already exposed and fixed the executor dispatch bugs.

---

## Steward Cycles Detail

### S2-A Steward Log (final run only, from registry)

The `steward_log` field captures the final reset round only:

| Event | Cycles | Posture | is_complete | Notes |
|-------|--------|---------|-------------|-------|
| agenda_update | 0 | — | — | Initial agenda set |
| diagnosis | 0 | execution_complete | false | output_ref null |
| prescription | 0→1 | — | — | LLM path, verification-before-completion skill |
| diagnosis | 1 | execution_complete | true | output_ref present: result.json |
| steward_closure | 1 | — | — | `outcome=complete` |

### S2-B Steward Log (final run only)

| Event | Cycles | Posture | is_complete | Notes |
|-------|--------|---------|-------------|-------|
| agenda_update | 0 | — | — | Initial |
| diagnosis | 0 | execution_complete | false | output_ref null |
| agenda_update | 0 | — | — | Second initial (diagnosing_orphan recovery) |
| diagnosis | 0 | executor_orphan | false | diagnosing_orphan re-entry |
| prescription | 0→1 | — | — | LLM path |
| diagnosis | 1 | execution_complete | true | result.json present |
| steward_closure | 1 | — | — | `outcome=complete` |

### S2-C Steward Log (complete — only one run)

| Event | Cycles | Posture | is_complete | Notes |
|-------|--------|---------|-------------|-------|
| agenda_update | 0 | — | — | Initial |
| diagnosis | 0 | first_execution | false | No prior audit entries |
| prescription | 0→1 | — | — | LLM path |
| diagnosis | 1 | execution_complete | true | result.json present |
| steward_closure | 1 | — | — | `outcome=complete` |

All three UoWs used the LLM prescription path (`prescription_path: "llm"`) — no fallback prescriptions. The `dod_revised: false` and `agenda_revised: false` in all entries confirm no mid-stream scope changes by the steward.

---

## Cycle Traces

### S2-A: No cycle trace file present

S2-A implements the trace logging feature itself (PR #672). The feature writes `<uow_id>.cycles.jsonl` via `_append_cycle_trace()` in `steward.py`. However, because S2-A's steward cycles completed before PR #672 was merged (2026-04-08T03:08 UTC; S2-A's final completion was at 02:48 UTC), the feature was not yet deployed when S2-A ran. No `.cycles.jsonl` file exists for `uow_20260407_c128a4`.

### S2-B: No cycle trace file present

S2-B completed at 02:51 UTC, also before PR #672 merged at 03:08 UTC. No trace file.

### S2-C: Cycle trace file present

S2-C is the first production UoW to run after PR #672 merged. A trace file exists at:
`~/lobster-workspace/orchestration/artifacts/uow_20260402_1e0f29.cycles.jsonl`

Contents:
```jsonl
{"cycle_num": 0, "subagent_excerpt": "", "return_reason": "", "next_action": "prescribed", "timestamp": "2026-04-08T03:26:08.732861+00:00"}
{"cycle_num": 1, "subagent_excerpt": "execution complete: task dispatched as 788e023b-6209-42d8-9dd1-7fbf2363fea1", "return_reason": "execution_complete", "next_action": "done", "timestamp": "2026-04-08T03:30:02.884151+00:00"}
```

This is exactly the format S2-A was built to produce: two entries for a 2-cycle UoW, with `cycle_num`, `subagent_excerpt`, `return_reason`, `next_action`, and `timestamp`.

---

## Executor Dispatch and PR Outcomes

### S2-A: PR #672 (dcetlin/Lobster)

- **Executor message ID:** `f2aaaf3f-ee91-49b9-a3a9-1ef7e65ed38c`
- **Dispatch timestamp:** 2026-04-08T02:46:32 UTC
- **Result written:** ~02:47 UTC (via `write_result` to inbox)
- **PR opened:** `https://github.com/dcetlin/Lobster/pull/672`
- **PR title:** feat(wos): per-cycle steward trace logging in UoW loop
- **PR author:** dcetlin (Dan Cetlin) — executor ran as dcetlin's Claude Code session
- **PR created:** 2026-04-08T03:03:55 UTC
- **Oracle review:** APPROVED (first review; adversarial prior: "instrumenting complexity rather than reducing it" — did not survive)
- **Oracle message:** `1775617680426_oracle-pr-672.json`
- **Merged:** 2026-04-08T03:08:23 UTC
- **Merge method:** Fast-forward (21cf9a7..0963afb)

### S2-B: PR #673 (dcetlin/Lobster)

- **Executor message ID:** `f33aeba7-3865-436a-bc06-e8df82dc15b3`
- **Dispatch timestamp:** 2026-04-08T02:49:31 UTC
- **Result written:** ~02:50 UTC
- **PR opened:** `https://github.com/dcetlin/Lobster/pull/673`
- **PR title:** feat(wos): prescription format — front-matter + prose split
- **PR author:** dcetlin
- **PR created:** 2026-04-08T03:17:30 UTC
- **Oracle R1:** NEEDS_CHANGES — silent colon truncation in `partition(":")` parser (values with colons silently truncated)
- **Fix:** Committed 726f1b7 — changed to `split(":", 1)`, added test `test_parse_workflow_artifact_value_with_colon`
- **Fix agent:** `1775618468433_fix-pr-673-colon.json`
- **Oracle R2:** APPROVED
- **Merged:** 2026-04-08T03:22:33 UTC (fast-forward to 4f1fa9c)

### S2-C: PR #674 (dcetlin/Lobster)

- **Executor message ID:** `788e023b-6209-42d8-9dd1-7fbf2363fea1`
- **Dispatch timestamp:** 2026-04-08T03:28:31 UTC
- **Result written:** 2026-04-08T03:33:31 UTC
- **PR opened:** `https://github.com/dcetlin/Lobster/pull/674`
- **PR title:** feat(wos): prescription pipeline observability metrics
- **PR author:** dcetlin
- **PR created:** 2026-04-08T03:33:11 UTC
- **Oracle review:** APPROVED (adversarial prior: "metrics built over a pipeline with unresolved semantics" — survived as opportunity-cost observation but not verdict against PR)
- **Oracle message:** `1775620291998_oracle-pr-674.json`
- **Merged:** 2026-04-08T03:51:50 UTC (squash merge)

---

## Timeline Comparison Across All Three UoWs

```
Date        Time (UTC)  Event
──────────────────────────────────────────────────────────────────
Apr 2       19:15       S2-C created (issue #583)
Apr 5       00:30       S2-B created (issue #613)
Apr 6       17:21       S2-B, S2-C manually cancelled (usage drain)
Apr 7       05:00       S2-C reactivated by garden caretaker
Apr 7       19:15       S2-A created (issue #652)
Apr 7       23:59       Sprint 2 launched ("all hands on deck")
──────────────────────────────────────────────────────────────────
Apr 8       00:02       S2-A, S2-B activated (proposed → ready-for-steward)
Apr 8       00:03–21    S2-A/B: 5 executor_orphan cycles each → hard cap
Apr 8       00:48       1st decide_retry (S2-A + S2-B)
Apr 8       00:51       Sprint 2 reset subagent confirms pipeline flowing
Apr 8       01:01       Root cause diagnosed: staleness gate defeating re-prescription
Apr 8       01:03–12    S2-A/B: 5 more orphan cycles → hard cap again
Apr 8       01:07       PR #667 opened (executor staleness gate fix)
Apr 8       01:12       PR #667 oracle APPROVED
Apr 8       01:16       2nd decide_retry; PR #667 merged
Apr 8       01:12–33    S2-A/B: 5 more orphan cycles → hard cap again
Apr 8       01:46       PR #668 opened (startup-sweep age anchor fix)
Apr 8       01:51       PR #668 oracle APPROVED
Apr 8       01:53       PR #668 merged; both UoWs direct-reset to ready-for-executor
Apr 8       01:58       S2-A + S2-B claimed and marked execution_complete (false-complete bug #669)
Apr 8       02:00       Steward detects missing output_ref → false closures
Apr 8       02:07       Root cause confirmed: executor marks complete at dispatch, not work-done
Apr 8       02:14       4th retry; issue #669 filed
Apr 8       02:30       Garden caretaker expires S2-A + S2-B (closed GitHub issues)
Apr 8       02:36       Ops agent resets expired UoWs → ready-for-steward
Apr 8       02:39–48    S2-A: steward diagnoses, prescribes, executor runs → PR #672 opened
Apr 8       02:49–51    S2-B: steward diagnoses, prescribes, executor runs → PR #673 opened
Apr 8       03:04       S2-A result delivered (PR #672 URL in write_result)
Apr 8       03:08       PR #672 oracle APPROVED; merged
Apr 8       03:17       S2-B result delivered (PR #673 URL)
Apr 8       03:19       PR #673 oracle R1 NEEDS_CHANGES (colon truncation)
Apr 8       03:21       Fix committed (726f1b7)
Apr 8       03:22       PR #673 oracle R2 APPROVED; merged
Apr 8       03:23       S2-C manually approved (proposed → pending); dispatch triggered
Apr 8       03:24–30    S2-C: steward diagnosis, prescription, executor, closure (~7 min)
Apr 8       03:33       S2-C result delivered (PR #674 URL)
Apr 8       03:51       PR #674 oracle APPROVED; merged
```

---

## Manual vs. Automated Breakdown

### Automated (pipeline-driven)

- UoW creation via sweep from GitHub issues
- Status transitions: proposed → pending → ready-for-steward (auto-advance)
- Steward activation on each heartbeat
- Steward diagnosis and LLM prescription (all three UoWs used `prescription_path: "llm"`)
- Executor claiming UoWs from the queue
- `wos_execute` inbox messages dispatched by executor-heartbeat
- Executor subagents opening PRs on dcetlin/Lobster
- Steward closure when output_ref confirmed present
- GitHub sync after closure
- Oracle review (subagent dispatched per PR Merge Gate)
- Merge execution after oracle approval

### Manual (human or manually-dispatched operations)

**S2-A and S2-B (heavy manual involvement):**
- 4× `decide_retry` resets per UoW (manually initiated via sprint2-uow-reset subagent)
- Pipeline bug diagnosis (sprint2-orphan-diagnose subagent)
- Two emergency PRs opened manually (#667, #668) to fix executor dispatch bugs
- Direct ops-agent override: `diagnosing → ready-for-executor` at 01:53 UTC (bypassing normal flow)
- Sprint completion check revealing false-complete bug (#669)
- Ops-agent reset from `expired → ready-for-steward` after garden caretaker expired the UoWs
- Fix-pr-673-colon subagent (oracle-required fix before merge)

**S2-C (light manual involvement):**
- One manual `sprint2-approve-s2c` action to move from `proposed → pending` (S2-C was being held pending S2-A completion)
- Otherwise fully pipeline-driven from ready-for-steward onward

### Root Causes of Manual Intervention

Three distinct bugs prevented autonomous execution of S2-A and S2-B:

1. **Executor staleness gate bug (PR #667):** The executor's 5-minute staleness gate checked `updated_at` on all UoWs uniformly. The steward's re-prescription updates `updated_at` every 3 minutes, resetting the clock. Result: freshly-prescribed UoWs were never stale enough to claim. Fix: only apply the staleness gate to UoWs with prior `executor_orphan` audit history.

2. **Startup-sweep age anchor bug (PR #668):** The startup sweep used `created_at` as the age anchor for orphan detection. UoWs created days earlier (S2-B was from Apr 5) were being continuously reclassified. Fix: use `updated_at` not `created_at` as the staleness anchor.

3. **False-complete bug (issue #669, not fixed during sprint):** The executor was writing `execution_complete` to the registry at dispatch time (when the `wos_execute` message was queued), not when the subagent returned work. This caused the steward to close UoWs before any work was done. This bug was identified but not fixed during Sprint 2; it was worked around by the final manual reset and corrected executor prescription format.

4. **Garden caretaker expiring in-flight UoWs:** The garden caretaker was configured to expire UoWs whose source GitHub issues were closed. Both issues #652 and #613 were closed during the sprint (before PRs were opened), triggering expiry. This required manual override via ops-agent. Not strictly a bug but an interaction that requires a grace period or in-flight detection.

---

## Notes on Registry State

The `wos-registry.db` file at `~/lobster-workspace/data/wos-registry.db` is empty (0 rows). The active production registry is at `~/lobster-workspace/orchestration/registry.db`. This appears to be an artifact of the registry migration or a path configuration difference. All data in this report comes from `orchestration/registry.db`.

The `steward_cycles` field in the registry reflects only the final reset round's cycle count (1 for all three UoWs). The cumulative cycle counts across all reset rounds are higher — approximately 20 cycles for S2-A, 25+ cycles for S2-B, versus 1 cycle for S2-C (which had no executor dispatch failures).
