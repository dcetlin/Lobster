# Issue Sweeper

**Job**: issue-sweeper
**Schedule**: Daily at 22-23,0-5:*/30 (`*/30 22-23,0-5 * * *`)
**Created**: 2026-03-27 03:57 PM UTC
**Updated**: 2026-03-29 02:17 PM UTC

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

### 0. Load Vision Object

Read `~/lobster-user-config/vision.yaml` at the start of each sweep. Extract:
- `current_focus.this_week.primary` — the primary focus for this week
- `current_focus.what_not_to_touch` — list of domains to exclude from UoW proposals
- `active_project.phase_intent` — the current phase intent (one paragraph)

Use these to populate `vision_ref` when upserting UoWs (see step 4). If the file
does not exist, log a warning in sweep output and continue without vision anchoring.

### 1. Expire stale proposals

First, expire any proposals older than 14 days that have not been confirmed:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py expire-proposals
```

Include the result in your sweep output.

### 2. Check for stale-active UoWs

Check whether any active UoWs have their source issue closed:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py check-stale
```

If any stale UoWs are found, include them in the sweep output and flag for Dan's review.

### 3. Scan the GitHub issue backlog

Fetch open issues from dcetlin/Lobster:

```bash
gh issue list --repo dcetlin/Lobster --state open --json number,title,labels,createdAt,updatedAt,comments --limit 100
```

For each issue, apply the following criteria:

**Propose as UoW if ANY of the following are true:**
- Has `ready-to-execute` label AND no linked PR
- Has `high-priority` label AND no linked PR
- Has `bug` label AND no linked PR
- Has `hygiene` label AND no linked PR
- Open > 3 days AND no `on-hold` label AND no `needs-design` label AND no `stale` label AND no `design` label AND no `philosophy-explore` label AND no linked PR

**What counts as valid UoW work (for `ready-to-execute` issues especially):**
Design iteration is explicitly valid. An issue does NOT need to produce code to be a valid UoW. The following are all legitimate completion artifacts:
- Code change / merged PR
- Written spec, ADR, or design decision record (committed to repo or lobster-outputs)
- Research doc or technical writeup (written to disk, not just a Telegram message)
- Sub-issues filed in GitHub (decomposition is a valid output when the original issue calls for it)
- Updated documentation or acceptance criteria in an existing issue

An issue is complete when it produces a **concrete, durable artifact** — not when it produces a conversational message. If the issue's done condition can be satisfied by a written document, spec, or decision record, that qualifies. Use `execution_type: design` for these.

**Skip if:**
- Has `on-hold` label (note in "Dan-blocked" section)
- Has `needs-design` label AND has no concrete done condition (no acceptance criteria, no "done when" clause, no defined deliverable) — i.e., speculative/future-phase placeholders not ready for execution. Issues labeled `needs-design` that DO have a concrete done condition should be proposed as UoWs with `execution_type: design`.
- Has `stale` label already
- Has an open linked PR (work in progress)
- Issue title/domain matches an entry in `vision.current_focus.what_not_to_touch`
  (note as "excluded by vision.what_not_to_touch" in sweep output)

### 4. Create UoWs for qualifying issues

For each qualifying issue, upsert a UoW and immediately confirm it (auto-accept — no /confirm gate):

```bash
ISSUE_BODY="$(gh issue view <N> --repo dcetlin/Lobster --json body --jq '.body')"
uv run ~/lobster/src/orchestration/registry_cli.py upsert \
  --issue <N> \
  --title "<issue title>" \
  --sweep-date "$(date +%Y-%m-%d)" \
  --issue-body "$ISSUE_BODY"
```

After upserting, immediately approve the UoW so it goes directly to `pending` status:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py approve --id <uow-id>
```

The /confirm gate is removed. UoWs are accepted on creation. No manual confirmation from Dan is required.

After upserting, determine the `vision_ref` by applying these checks IN ORDER — stop at the first match:

**Check A — Current focus match:**
Compare the issue title and first paragraph of body against `current_focus.this_week.primary` verbatim. Match only if the issue directly and specifically addresses the primary focus statement — not just related work. If matched:
vision_ref = {"layer": "current_focus", "field": "this_week.primary", "statement": "<verbatim primary text>", "anchored_at": "<now ISO>"}

**Check B — Named vision anchor:**
Compare issue title/body against each domain listed in `current_focus.what_not_to_touch`. If the issue matches a what_not_to_touch domain — SKIP (it should have been excluded in step 3, but this is a safety check).

**Check C — Explicit phase-intent match:**
Check if the issue is specifically about the WOS Registry, sweeper, Vision Object wiring, or morning briefing staleness (i.e., it directly implements the active_project.phase_intent). The test: can you point to a sentence in the issue body that matches language in `active_project.phase_intent`? If yes:
vision_ref = {"layer": "active_project", "field": "phase_intent", "statement": "<matching sentence from phase_intent>", "anchored_at": "<now ISO>"}

**Check D — No anchor:**
If none of the above match, set vision_ref = null. Do NOT default to active_project.phase_intent for issues that are generally related to Lobster but don't specifically match the phase intent text. Null is honest; boilerplate is a failure mode.

**Boilerplate detection:** If you find yourself assigning the same vision_ref to 3+ UoWs in a single sweep, flag it in the sweep output as: "WARNING: vision_ref may be boilerplate — N UoWs assigned identical anchor [anchor_value]."

Record the result (inserted vs skipped with reason, and vision_ref assigned) in
your sweep output.

### 5. Report Phase status

Phase 1 is complete (declared 2026-03-30). Report this as Phase 1 complete, Phase 2 active.

You may still run gate-readiness for the approval ratio metric:

```bash
uv run ~/lobster/src/orchestration/registry_cli.py gate-readiness
```

The output includes a `phase` field confirming `"phase_1_complete_phase_2_active"`.
Report it as: "Phase 1 complete, Phase 2 active (ratio: <approval_rate>)"
Do NOT report a day count or "gate not met" — the 14-day threshold is removed.

### 6. Build the ready queue

Query pending UoWs from the registry (UoWs are auto-accepted — no proposed queue):

```bash
uv run ~/lobster/src/orchestration/registry_cli.py list --status pending
```

Order by created_at (oldest first). All items are labeled "awaiting execution".

Note: The /confirm gate has been removed. UoWs go directly from upsert → approve → pending.
If any items appear with status `proposed`, that indicates a sweep ran before this change —
approve them manually with `registry_cli.py approve --id <uow-id>`.

### 6b. Build the design visibility list

Query all open issues labeled `needs-design` from dcetlin/Lobster:

```bash
gh issue list --repo dcetlin/Lobster --state open --label needs-design --json number,title,labels,updatedAt --limit 100
```

For each issue found:
- Include it in the Design Attention section regardless of age or content
- Do NOT filter by done condition — every open needs-design issue belongs here
- Exception: exclude issues that match `vision.current_focus.what_not_to_touch` — list those separately as "excluded by focus"

This list is visibility only — do not upsert these into the registry.

### 7. Write sweep output

Call `write_task_output` with a structured report containing:

1. **Vision Object loaded**: yes/no (if no, reason)
2. **Current focus**: one-line summary from vision.current_focus.this_week.primary
3. **Expired proposals**: count and ids
4. **Stale-active UoWs**: list (id, issue, summary) — if any
5. **Issues scanned**: count
6. **UoWs created**: list (id, issue number, title, action: inserted/skipped, vision_ref: layer or null)
7. **Vision-excluded issues**: issues skipped due to what_not_to_touch
8. **Pending queue** (auto-accepted, awaiting execution):
   - One line per UoW: `<id> | #<issue> | <title> | created: <date>`
   - Note: /confirm gate removed — UoWs auto-advance to pending on creation
8b. **Design Attention** (needs-design issues — visibility only):
    - One line per issue: `#<number> | <title> | last updated: <date>`
    - If empty: "(none)"
    - Note: these do not expire; close the GitHub issue to remove from list
9. **Dan-blocked items**: issues with `on-hold` label
10. **Phase status**: "Phase 1 complete, Phase 2 active" and approval ratio (7d)

Keep it concise — Dan reads this on mobile.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "issue-sweeper"
- output: Your structured sweep report
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "issue-sweeper"
- output: Your results/summary
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
