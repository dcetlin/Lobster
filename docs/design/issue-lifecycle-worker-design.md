# Issue Lifecycle Worker — Design

*Status: DRAFT — UoW uow_20260522_217c0a*
*Written: 2026-05-22*

---

## 1. Purpose

The GitHub issue backlog is subject to two distinct failure modes: structural decay (duplicates, template-empty bodies, stale labels) and motivational stasis (issues that are technically valid but that nobody has moved forward). The negentropic sweep handles the first failure mode — it runs nightly on a rotation, identifies hygiene problems, and files or closes issues as a structural cleanup pass. What the sweep does not do is propel issues through their intended lifecycle stages: from observed need, through design, through implementation-ready, to triggered execution or deliberate closure.

The issue lifecycle worker fills that gap. Its job is forward propulsion, not hygiene. It reads the current state of open issues — their labels, age, linked documents, linked PRs, and comment history — and fires state transitions when the signals for those transitions are present. It does not clean up malformed issues; it advances well-formed ones. An issue with no assignee and no activity for 21 days is a hygiene concern owned by the negentropic sweep. An issue labeled `design-settled` with a linked design doc but no `implementation-ready` label is a lifecycle concern owned by this worker.

This worker sits upstream of the observation-to-PR loop (which fires when an issue enters `implementation-ready` and triggers execution). It sits downstream of the negentropic sweep, which produces the clean issue corpus this worker operates on. The boundary is explicit: sweep output is lifecycle worker input. The lifecycle worker does not re-examine hygiene signals (empty bodies, template noise, exact duplicates) — those are the sweep's domain. The lifecycle worker only advances issues whose hygiene is already acceptable.

---

## 2. State Transitions Managed

| Transition | Entry Condition | Worker Action | Output |
|---|---|---|---|
| `design-settled` → `implementation-ready` | Issue carries `design-settled` label AND a linked design doc exists (see Section 3) | Add label `implementation-ready`; remove `needs-design` if present; post comment with transition rationale | Issue comment + Telegram ping to Dan |
| `stale-without-movement` → `stale-candidate` | No activity for 21 days, no assignee, no `on-hold` label, no linked open PR | Add label `stale-candidate`; post comment with staleness summary | Issue comment + Telegram ping to Dan |
| `design-in-progress` → flag related work | Issue carries `design-in-progress` label AND a linked PR or sibling issue exists | Post comment listing related open work; call `record_link` for each relationship | Issue comment only (informational, not decision-requiring) |
| `implementation-ready` → trigger | Issue carries `implementation-ready` label AND no linked open PR | Telegram ping to Dan; post comment noting the issue is ready for execution dispatch | Issue comment + Telegram ping (no autonomous dispatch in v1; see Section 5) |

---

## 3. Signal Definitions

### What makes an issue "design-settled"?

An issue is design-settled when **all three** of the following hold:

1. The label `design-settled` is present on the issue.
2. A design document exists in the repo at a path matching `docs/design/<issue-title-slug>.md` OR a comment on the issue contains a bare link to a file under `docs/design/` or `docs/` ending in `.md`.
3. No open question in the issue body remains unresolved. An open question is any line matching `?` that is not struck through (i.e., does not appear inside `~~...~~`). If the issue body contains zero `?` characters, condition 3 is trivially satisfied.

The label `design-settled` is the primary gate. Conditions 2 and 3 are confirmatory checks. If the label is present but conditions 2 or 3 fail, the worker posts a comment noting the gap ("design-settled label present but no linked doc found — is this resolved?") and does not advance the issue. It does not remove the label; that is Dan's call.

The title-slug matching rule: lowercase the title, replace spaces and punctuation with hyphens, strip leading/trailing hyphens. For example, "PR Review Layer — Design" → `pr-review-layer-design`. A fuzzy match (longest common subsequence > 0.8) is acceptable as a secondary check when the exact slug is not found.

### What makes an issue "stale"?

An issue is stale when **all** of the following hold:

1. The issue has been open for at least 21 days.
2. No comment, label change, or linked PR event has occurred in the last 21 days.
3. The issue has no assignee.
4. The labels `on-hold` and `blocked-on-dan` are absent.
5. No open PR is linked to the issue (checked via GitHub `timeline` events for `cross-referenced` items).

N = 21 days. This threshold is lower than the decay detector's 60-day threshold. The lifecycle worker catches pre-decay stagnation; the decay detector catches fully frozen intentions. The two thresholds deliberately overlap in the 21–60 day window: the lifecycle worker adds `stale-candidate`; the decay detector later escalates or closes. The labels distinguish the two passes so Dan knows which pass fired.

---

## 4. Output Surface

The worker surfaces findings on two channels, with a routing rule:

**Channel 1 — Issue comment (all findings):** Every transition the worker fires results in a comment on the issue. Comments create a permanent record, are visible in GitHub, and are citable by downstream workers (the connective tissue linker, the PR review layer). Comments use the format in Section 9.

**Channel 2 — Telegram ping to Dan (actionable findings only):** The following finding categories generate a Telegram ping:

| Finding category | Telegram ping? |
|---|---|
| `design-settled` → `implementation-ready` transition | Yes — issue is ready for execution decision |
| `implementation-ready` issue with no linked PR | Yes — issue is waiting for Dan to dispatch or defer |
| `stale-candidate` newly labeled | Yes — item needs decision: close, on-hold, or assign |
| `design-in-progress` related work cluster | No — informational; comment only |

Design-in-progress link clusters are comment-only because they are informational, not decision-requiring. Dan does not need to act; the cluster is recorded for context.

This routing rule is the design's final position for v1. It may be loosened in a future version to allow Dan to suppress pings for specific labels (e.g., "don't ping me on stale-candidate — I'll check the weekly digest").

---

## 5. Autonomy Boundary

**The worker may, without Dan's approval:**
- Post comments on issues
- Apply labels: `implementation-ready`, `stale-candidate`, `lifecycle-reviewed`
- Remove labels: `needs-design` (only when simultaneously applying `implementation-ready`)
- Call `record_link` to register cross-document relationships

**The worker must NOT, without explicit escalation and Dan's response:**
- Close issues
- Remove labels set by Dan (labels not applied by this worker)
- Open PRs or trigger the WOS execution pipeline directly
- Apply `blocked-on-dan` or `needs-decision` labels (those are human-gate labels, not worker labels)

This is a conservative starting point, not the design's final position. After two weeks of operation, if the `stale-candidate` and `implementation-ready` label applications are consistently accurate (no false positives escalated by Dan), the boundary may be extended to allow autonomous issue close for items meeting the stale-candidate criteria for more than 14 additional days without Dan action.

The rationale for starting conservative: label applications are trivially reversible; issue closes require a manual reopen and lose comment thread context. The asymmetry in cost justifies asymmetry in autonomy.

---

## 6. Cadence and Integration

**Recommended cadence: standalone scheduled job, nightly at 03:30.**

The negentropic sweep runs nightly at 02:00 on a rotation, with the UoW sweeper running at 03:00. The lifecycle worker should run at 03:30, after the sweep and after the UoW sweeper, so that:

1. The sweep has already applied or removed hygiene-related labels (the lifecycle worker reads these as input signals).
2. The UoW sweeper has already created new UoW proposals for any issues that entered `ready-to-execute` state (the lifecycle worker does not duplicate this check).
3. The lifecycle worker's output (new labels, comments) is available for the morning digest at 08:00.

**Job registration:**

```python
create_scheduled_job(
    name="issue-lifecycle-worker",
    schedule="30 3 * * *",
    context="Run the issue lifecycle worker. Read ~/lobster/scheduled-tasks/tasks/issue-lifecycle-worker.md for the task prompt."
)
```

**Relationship to the negentropic sweep:** The lifecycle worker does not replace any sweep night. It is a standalone job that runs every night, not on a rotation. It is lightweight (no LLM reasoning per issue; purely label/signal based) and should complete in under 60 seconds for a backlog under 200 issues. If GitHub API rate limits are a concern, it can be converted to a Type B (cron-direct) job using the same pattern as `decay-detector.py`.

**Standalone vs. embedded:** A standalone job is preferred because the lifecycle worker's output (label transitions, Telegram pings) should be visually distinct from sweep output in the Telegram digest. Embedding it in the sweep would merge two conceptually distinct passes into one output, making it harder to audit which pass fired which action.

---

## 7. GitHub API Requirements

The following GitHub API operations are required:

| Operation | API | Notes |
|---|---|---|
| List open issues | `gh issue list --repo dcetlin/Lobster --state open --json number,title,labels,assignees,createdAt,updatedAt,body,url` | Paginate via `--limit 200` initially; add pagination if backlog grows |
| Get issue timeline | `gh api repos/dcetlin/Lobster/issues/{N}/timeline` | Used to detect linked PRs (cross-referenced events), label change events, and comment activity |
| Get issue comments | `gh api repos/dcetlin/Lobster/issues/{N}/comments` | Used to detect linked design doc URLs |
| Apply label | `gh issue edit {N} --repo dcetlin/Lobster --add-label {label}` | Called per transition |
| Remove label | `gh issue edit {N} --repo dcetlin/Lobster --remove-label {label}` | Called per transition |
| Post comment | `gh issue comment {N} --repo dcetlin/Lobster --body "..."` | Called per transition |

**Rate limit concern:** For a backlog of 50–150 open issues, the timeline fetch is the most expensive call (one per issue). At 150 issues, that is ~150 timeline API calls per run. GitHub's REST API allows 5,000 authenticated requests per hour for `gh` CLI. This is well within budget for nightly runs. No rate-limit mitigation is required for v1. If the backlog grows beyond 500 issues, implement incremental scanning (only check issues updated since last run, using `--updated-after` or equivalent).

---

## 8. State File

The worker maintains a state file at:

```
~/lobster-workspace/data/issue-lifecycle-worker-state.json
```

Schema:

```json
{
  "last_run_at": "2026-05-22T03:30:12Z",
  "last_run_issues_scanned": 47,
  "last_run_transitions_fired": 3,
  "acted_issues": {
    "123": {
      "last_action": "implementation-ready",
      "last_action_at": "2026-05-22T03:30:14Z"
    },
    "87": {
      "last_action": "stale-candidate",
      "last_action_at": "2026-05-21T03:31:00Z"
    }
  }
}
```

The `acted_issues` map prevents the worker from re-commenting on the same issue on consecutive runs. Before firing any action on issue N, the worker checks whether `acted_issues[N].last_action_at` is within the past 7 days and the same action is being proposed. If so, it skips the comment and label application (the label is already present; re-commenting adds noise). After 7 days, the worker may re-evaluate and re-comment if the signal is still present.

On first run (state file absent), default `last_run_at` to 30 days ago and `acted_issues` to empty.

---

## 9. Integration Points

### With the negentropic sweep

The sweep is the upstream hygiene pass. The lifecycle worker reads labels produced by the sweep (`stale`, `needs-design`, `design-in-progress`, `design-settled`) as input signals. The lifecycle worker does not write these labels — it reads them and advances to the next stage. The boundary: sweep writes source labels; lifecycle worker reads them and writes transition labels.

**Interface:** Label set on GitHub issues. No direct IPC or shared state file.

### With the observation-to-PR loop (WOS UoW sweeper)

The lifecycle worker is upstream of WOS execution. When the worker applies `implementation-ready`, the UoW sweeper (which runs at 03:00, before the lifecycle worker) will not pick it up until the following night. This is intentional: the lifecycle worker runs at 03:30, after the sweeper. The one-night lag between `implementation-ready` label application and UoW creation is acceptable for v1.

**Interface:** Label `implementation-ready` on GitHub issues. The UoW sweeper's existing condition (`ready-to-execute` label, no linked PR, age > 3 days) should be extended to also match `implementation-ready` label. This is a one-line change to the sweeper's scan criteria.

**Note:** `ready-to-execute` and `implementation-ready` may be unified as a single label in a future pass. For v1, they remain separate: `ready-to-execute` is applied by the negentropic sweep for issues that were always implementation-ready; `implementation-ready` is applied by the lifecycle worker for issues that just crossed the `design-settled` threshold.

### With the connective tissue linker

When the worker fires a `design-in-progress → flag related work` transition, it calls `record_link` for each related issue or PR discovered:

```python
record_link(
    source_path=f"github://issues/{source_issue_number}",
    target_path=f"github://issues/{related_issue_number}",
    link_type="references",
    rationale="Detected by issue lifecycle worker: both issues share labels X and Y and were opened within 7 days of each other.",
    worker_id="issue-lifecycle-worker"
)
```

The connective tissue linker's `record_link` interface accepts any string as `source_path` and `target_path` — GitHub issue URLs are a valid extension of the corpus-relative path convention.

### Comment format

All worker comments follow this template:

```
**Issue Lifecycle Worker**

Transition: {transition_name}
Signals detected: {signal_list}
Action taken: {action_description}

*Fired by: issue-lifecycle-worker — {timestamp}*
```

Example:

```
**Issue Lifecycle Worker**

Transition: design-settled → implementation-ready
Signals detected: label `design-settled` present; linked doc found at docs/design/pr-review-layer.md; no unresolved open questions in body.
Action taken: Applied label `implementation-ready`. Removed label `needs-design`.

*Fired by: issue-lifecycle-worker — 2026-05-22T03:30:14Z*
```

### Telegram message template

```
Issue lifecycle worker — {N} transition(s) fired.
  • #{issue_number}: {title} → {transition_label} ({url})
  • ...
```

If zero actionable transitions fired, no Telegram ping is sent. If zero total transitions fired, the worker writes its state file and exits silently (no ping, no digest entry).

---

## 10. Open Questions (not blocking first implementation)

1. **Label unification (`ready-to-execute` vs. `implementation-ready`):** Should these be merged into one label? Deferred — requires coordination with the UoW sweeper and any existing GitHub automation that keys on `ready-to-execute`. Decision: revisit after v1 has run for two weeks and the overlap is observed empirically.

2. **Fuzzy slug matching for design doc detection:** The title-slug matching rule in Section 3 uses a 0.8 LCS threshold as a secondary check. This threshold has not been calibrated against the actual issue title corpus. Deferred — calibrate after first run by reviewing the "no linked doc found" cases.

3. **Rate-limit strategy for large backlogs:** Section 7 notes that timeline fetches become expensive beyond 500 issues. The incremental scan strategy (only check issues updated since last run) should be designed explicitly before the backlog reaches that size. Deferred with a concrete trigger: implement when the backlog exceeds 300 open issues.

4. **Dan-suppressible pings:** Some `stale-candidate` pings may be expected and low-signal (e.g., design-seed issues that are intentionally parked). A per-label ping suppression preference would reduce noise. Deferred — collect operational data first.

5. **Integration with the philosophy harvest and cultivator workers:** If those workers apply labels or comments that the lifecycle worker should read as design-settled signals, the signal definitions in Section 3 need to be extended. Deferred — the cultivator worker's label conventions are not yet documented.
