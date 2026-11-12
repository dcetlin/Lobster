# Learnings Proposals

**Job**: learnings-proposals
**Schedule**: Weekly on Sundays at 4:00 AM UTC (`0 4 * * 0`)
**Created**: 2026-04-13

## Context

You are running as a scheduled task. Your purpose is to read the week's oracle learnings and generate actionable proposals for `meta/proposals.md`.

## Instructions

### 1. Read oracle/learnings.md

Read `~/lobster/oracle/learnings.md` (Layer 2 archive section preferred; fall back to Layer 1 index if Layer 2 is empty).

Filter to entries dated within the past 7 days. If no entries exist in that window, write a task output noting "No new learnings this week — no proposal generated" and exit.

### 2. Read existing proposals

Read `~/lobster-workspace/meta/proposals.md` to understand existing proposals and avoid duplicating themes already covered.

### 3. Generate a proposal

Based on the week's learnings, write a proposal that answers: "Given what oracle review surfaced this week, what concrete system improvement would address the underlying pattern?"

Requirements for the proposal:
- Must be actionable (a specific change, not a vague suggestion)
- Must reference the learning(s) that motivated it (by date and PR number)
- Must state what the expected outcome would be if implemented

### 4. Append to proposals.md

Append a new entry to `~/lobster-workspace/meta/proposals.md` in this exact format:

```
### [YYYY-MM-DD] Learnings-driven: <short title>

**Signals processed:** <count of learnings entries from this week>

**Source learnings:**
- [YYYY-MM-DD] PR #NNN — <learning summary>

**Proposal:** <the concrete proposal>

**Expected outcome:** <what changes if this is implemented>
```

Use today's date for the heading. Do not add a delivered marker — the `proposals-digest` job handles delivery.

## Output

Call `write_task_output` with:
- job_name: "learnings-proposals"
- output: Summary of what was written (or why nothing was written)
- status: "success" or "failed"
