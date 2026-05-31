# Issue Lifecycle Worker — Design

*WOS-UoW: uow_20260522_217c0a*

*May 2026*

---

## 1. Purpose and Scope

The issue lifecycle worker watches the GitHub issue backlog for state transitions and surfaces readiness signals or staleness signals for human attention. It is distinct from the negentropic sweep (issue #127), which performs hygiene — closing duplicates, archiving stale artifacts, and enforcing structural conventions. It is also distinct from the observation→PR loop, which acts on a specific observation by opening a concrete change. The lifecycle worker is **upstream propulsion**: it asks "what is ready to advance?" rather than "what needs cleaning?" or "what change should be applied?" It reads issue state, applies transition detectors, posts contextual comments, and sends a consolidated Telegram summary. It does not close issues, merge PRs, or trigger builds. Its job is to keep the backlog moving by making readiness and staleness visible at the moment they occur.

---

## 2. State Transitions the Worker Detects

### a. Design-settled → ready for implementation

An issue is classified `design-settled` when **all three** of the following machine-readable signals are present:

1. **Linked design doc exists** — the issue body or a comment contains a reference to a file path matching `docs/wos/design/*.md` (or `docs/**/*-design.md`), and that file exists in the `main` branch of `SiderealPress/lobster`.
2. **No open design questions** — the issue body contains no unchecked `- [ ]` list items inside a section whose heading contains the word "question" (case-insensitive, e.g. `## Design Questions`, `### Open Questions`). Checked items (`- [x]`) do not block.
3. **`design-settled` label is present** — a human or prior automated step has applied this label explicitly. The label is the authoritative gate; the doc-link and checkbox checks are supporting signals that the worker surfaces in its comment, but the label is required for the `implementation-ready` transition to fire.

Minimum viable signal: all three must be true. If only (1) and (2) are true but the label is absent, the worker posts a comment noting the doc is linked and questions are resolved, suggesting the `design-settled` label be applied — but does not itself apply the label or advance the issue.

### b. Stale-without-movement → needs nudge or close

An issue is classified `stale` when **all** of the following hold:

- No comment activity (excluding bot comments tagged `[issue-lifecycle-worker]`) for **21 days**
- No linked PR in the `open` or `merged` state (checked via `gh pr list --search "closes:#<N>"`)
- No `in-progress` label present
- Issue is not labeled `blocked-on-dan`, `needs-decision`, or `awaiting-sign-off` (those carry their own human gate and are excluded from staleness firing)

The stale threshold is **21 days**. This is long enough to avoid false positives from normal async work rhythms but short enough to surface genuinely forgotten issues before they become invisible.

### c. Design-in-progress → surface related work

An issue is classified as `design-in-progress` when it carries the `design-in-progress` label or its body/comments reference a design doc path that does not yet exist on `main` (i.e., the doc is referenced but not yet committed).

"Related" is determined by any one of:
- **Label overlap**: two or more labels in common with another open issue (excluding generic labels like `bug`, `enhancement`, `design-in-progress` themselves)
- **Explicit cross-reference**: the issue body contains `related: #<N>` or `see: #<N>` syntax
- **Title keyword overlap**: three or more non-stopword tokens in common between issue titles (naive tokenization, lowercase comparison)

When the worker finds related issues, it posts a comment listing them with their current state, so the author has context without searching manually.

### d. Implementation-ready → kick off signal

An issue is `implementation-ready` when all four conditions hold:

1. `design-settled` label is present
2. Linked design doc exists on `main`
3. No open issues are linked as blockers (no `blocked-by: #<N>` references in the body that resolve to open issues)
4. No `blocked-on-dan` / `needs-decision` / `awaiting-sign-off` label is present

When this fires, the worker posts a comment and sends a Telegram ping. If the issue body contains a `WOS-UoW:` reference, the worker includes that UoW ID in the ping so the dispatcher can route it. The worker does **not** auto-trigger a build or dispatch a UoW.

---

## 3. Output Mechanism

For each transition type, the worker produces exactly:

| Transition | GitHub action | Telegram |
|---|---|---|
| Design-settled | Post comment on issue summarizing which signals are satisfied and what is missing (if partial) | Consolidated ping listing all design-settled issues by number and title |
| Stale | Post comment flagging staleness with last-activity date and suggesting action; add `stale` label | Consolidated ping listing all stale issues |
| Related-work surfacing | Post comment linking related issues with their current state | None (informational only, not actionable enough to warrant a ping) |
| Implementation-ready | Post comment confirming all gates are satisfied; include UoW ID if present | Consolidated ping listing implementation-ready issues; include any UoW IDs found |

The Telegram summary is **one message per run**, not one ping per issue. The message groups findings by transition type, e.g.:

```
Issue Lifecycle Worker — nightly run

Ready for implementation (2):
  #214 Add vault-watcher validation [WOS-UoW: uow_20260519_abc]
  #198 Issue lifecycle worker design

Stale (1):
  #187 Observability refactor (last activity: 24 days ago)

Design-settled (no prior notification): 0
Related work surfaced: 3 comment(s) posted (no ping — see GitHub)
```

---

## 4. Autonomy Policy

The worker **never closes issues autonomously**. This is a hard constraint, not a default.

Permitted autonomous actions:
- Add `stale` label to issues meeting the staleness criteria
- Post comments (with idempotency guard — see §6)
- Send Telegram pings

Prohibited autonomous actions:
- Close or archive any issue
- Remove labels
- Merge PRs
- Dispatch a WOS executor UoW (flag only — dispatcher routes)
- Apply `design-settled`, `implementation-ready`, or any readiness label

All close and archive actions require a human decision or an explicitly dispatched agent with a separate oracle review cycle. The autonomy boundary exists here because incorrect auto-close causes information loss that is difficult to recover, while a missed ping is easily corrected on the next nightly run.

---

## 5. Cadence and Scheduling

- **Cadence**: nightly at `0 4 * * *` (4 AM), after the 3 AM nightly consolidation run
- **Trigger**: standalone systemd/cron scheduled job — not embedded in the negentropic sweep rotation
- **Job name**: `issue-lifecycle-worker`
- **Job type**: Type A (LLM subagent task, dispatched via inbox message)
- **Schedule**: `0 4 * * *`

The worker runs as a **standalone scheduled job**, not as a night-N rotation inside the negentropic sweep. Rationale: the sweep operates on memory artifacts, code hygiene, and structural conventions; the lifecycle worker operates on GitHub issue state and readiness signals. They share a nightly cadence but have different scopes, different detection logic, and different failure modes. Keeping them separate means each can fail, be disabled, or be debugged independently without affecting the other.

---

## 6. Architecture Sketch

```
1. Fetch all open issues from SiderealPress/lobster:
   gh issue list --repo SiderealPress/lobster --state open --limit 200 --json number,title,labels,body,comments,createdAt,updatedAt

2. For each issue, evaluate each transition detector in sequence:
   a. Check design-settled signals (doc link, open-question absence, label)
   b. Check staleness signals (days since last non-bot comment, linked PR, labels)
   c. Check design-in-progress + related work signals (label overlap, cross-refs, title overlap)
   d. Check implementation-ready signals (design-settled + no blockers + no human-gate labels)

3. Collect findings into a structured report:
   {
     "design_settled": [...],      # issues with all 3 signals satisfied
     "stale": [...],               # issues meeting staleness threshold
     "related_work": [...],        # (issue, [related_issues]) pairs
     "implementation_ready": [...] # issues meeting all 4 gates
   }

4. For each finding, post the appropriate GitHub comment:
   - Idempotency guard: before posting, check existing comments for a prior
     comment whose body starts with "<!-- [issue-lifecycle-worker]" and whose
     content matches the current transition type. If found and posted within
     the last 7 days, skip. If found but older than 7 days, post a new comment
     (issue may have changed state and returned to this transition).

5. Apply labels where permitted (stale only).

6. Send one consolidated Telegram summary to chat_id 6036:
   - Group by transition type
   - One message total, not one per issue
   - Include UoW IDs for implementation-ready issues where present

7. Write job output:
   write_task_output(job_name="issue-lifecycle-worker", output=<summary>, status="success")
```

**Idempotency marker format** (prepend to every posted comment):
```
<!-- [issue-lifecycle-worker] transition=<type> run=<ISO-date> -->
```

This allows the idempotency check to use a simple `startswith` match on comment body rather than parsing full content.

---

## 7. Relation to Adjacent Systems

- **Negentropic sweep (#127)**: performs hygiene — closing duplicates, archiving noise, enforcing structural conventions. This worker performs forward propulsion — detecting readiness and surfacing it. They share nightly cadence but not scope, implementation, or failure surface. Neither depends on the other's output.
- **Observation→PR loop**: the observation loop executes a specific, already-approved change. This worker is upstream: it surfaces the signal ("this issue is ready") that may eventually cause an observation to be written. The lifecycle worker never writes observations or dispatches PRs.
- **WOS executor**: if an implementation-ready issue contains a `WOS-UoW:` reference, the worker notes the UoW ID in its Telegram ping. The dispatcher may then route the UoW through the executor. The lifecycle worker does not call the executor directly — it produces a signal; the dispatcher acts on it.

---

## 8. Open Questions (Deferred)

1. **Comment rate limiting**: If 30+ issues are stale in a single run, posting 30 comments at 4 AM may trigger GitHub secondary rate limits. Should the worker batch or cap comment posting per run (e.g., max 10 new comments per night)?
2. **Label creation authorization**: The `stale` label may not exist in the repo. The worker needs to create it if absent — should this be done at job registration time (one-time setup) or inline at first use?
3. **Dan-decision escalation path for persistent stale issues**: If an issue is still stale after 3 nightly notifications, should the worker escalate (different Telegram message, different label, or a task creation)? The current design stops at flagging — the escalation threshold and action are not yet specified.
4. **Interaction with WOS UoW lifecycle**: If a stale issue has an associated `proposed` UoW in the registry, should the lifecycle worker flag the UoW as well, or only post on the GitHub issue? The interface between the two systems at the staleness boundary is not yet defined.
