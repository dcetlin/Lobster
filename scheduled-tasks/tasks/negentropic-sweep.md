# Negentropic Sweep

**Job**: negentropic-sweep
**Schedule**: Daily at 2:00 (`0 2 * * *`)
**Created**: 2026-03-25 08:07 PM UTC

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

### Vision Coherence Anchor

Before executing, read `~/lobster-user-config/vision.yaml` (if it exists) and extract:
- `current_focus.this_week.primary` — the active work intent
- `current_focus.current_constraint.statement` — the binding constraint
- `current_focus.what_not_to_touch` — items explicitly excluded this week

Hold these as a coherence filter on the outputs of this job: does what you produce, surface, or flag serve the active intent? Does anything you are about to do touch an excluded item? If so, skip it and note why.

If vision.yaml is missing, continue without it and note the absence in write_task_output.

---

Nightly Negentropic Sweep — runs via the lobster-meta agent type.

**Start here:** Read `~/lobster-workspace/hygiene/sweep-context.md` in full. That file is your complete operating context. Do not proceed until you have read it.

It contains: the negentropic principal framing, domain rotation logic (7-night cycle with state in `~/lobster-workspace/hygiene/rotation-state.json`), per-session structure (detection pass → refactor pass → escalation list), autonomy calibration rules (including code-layer counter-force requirements), output format, and the two-ping protocol.

Also read `~/lobster-workspace/.claude/agents/lobster-meta.md` for the lobster-meta epistemic posture (resist synthesis, surface what doesn't fit).

Output: `~/lobster-workspace/hygiene/YYYY-MM-DD-sweep.md`

After writing output, send two Telegram pings to Dan (chat_id: 8075091586) per the two-ping protocol in sweep-context.md.

### Convert escalation items to GitHub issues

**This step is mandatory. Do not call write_task_output until it is complete.**

After writing the sweep file, read the Escalation List section of your output. For each item in that section:

1. Check whether a GitHub issue already exists: `gh issue list --repo dcetlin/Lobster --state open --search "<item title keywords>"`
2. If no issue exists, file one: `gh issue create --repo dcetlin/Lobster --title "..." --label "bug" --body "..."`
   - Title: short description of the smell
   - Body: smell description, file/line reference if available, and what a fix would look like
   - Use label `bug` for behavioral/structural defects, `enhancement` for improvements
3. Record each issue number in your sweep file under the escalation item

An escalation item that has no GitHub issue when write_task_output is called is an incomplete OODA loop. The sweep is not done until every escalation item is either (a) linked to an existing issue, or (b) has a new issue filed in this run.

### Learnings Runtime Verification

**This step runs after the detection pass and before you write the sweep output file.**

Read `~/lobster-workspace/oracle/learnings.md`. If the file does not exist, note its absence and skip this step.

For each entry in learnings.md:

1. Parse the entry date (expected format: `YYYY-MM-DD` or similar ISO date in the entry header or metadata).
2. If the entry date is within the past 7 days (inclusive of today), it is a **recent entry** subject to verification.
3. For each recent entry, identify the **runtime symptom** it describes — what observable behavior, artifact, log pattern, or registry state the entry says was present.
4. Check whether that symptom is still present by inspecting actual runtime artifacts:
   - Sweep output files in `~/lobster-workspace/hygiene/`
   - Job logs in `~/lobster-workspace/scheduled-jobs/logs/`
   - Rotation state in `~/lobster-workspace/hygiene/rotation-state.json`
   - Any other file, log, or registry entry the learning entry references explicitly
5. Classify the result:
   - **Resolved** — symptom is no longer present in current artifacts
   - **Still present** — symptom persists; add this as an ESC item with label `learning-not-remediated` in the Escalation List of your sweep output, formatted as:
     ```
     ESC [learning-not-remediated] <title from learnings entry> — symptom still present as of <today's date>. Source: oracle/learnings.md entry <date>.
     ```

Include a **Learnings Verification** subsection in your sweep output file listing each recent entry checked, its classification (Resolved / Still present), and a one-sentence rationale.

If no entries in learnings.md are dated within the past 7 days, note "No recent learnings entries to verify" in the subsection.

---

### Push output to lobster-outputs

After writing the sweep file to `~/lobster-workspace/hygiene/YYYY-MM-DD-sweep.md`, also push it to GitHub:

```bash
SWEEP_FILE="YYYY-MM-DD-sweep.md"  # use the actual dated filename you wrote
OUTPUTS_DIR=~/lobster-workspace/projects/lobster-outputs

cp ~/lobster-workspace/hygiene/${SWEEP_FILE} ${OUTPUTS_DIR}/hygiene/${SWEEP_FILE}
cd ${OUTPUTS_DIR}
git add hygiene/${SWEEP_FILE}
git commit -m "sweep: ${SWEEP_FILE}"
git push origin main
```

After pushing, construct the GitHub URL:
```
https://github.com/dcetlin/lobster-outputs/blob/main/hygiene/YYYY-MM-DD-sweep.md
```

Include this URL in your `write_task_output` output field so it appears in the job history.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "negentropic-sweep"
- output: Your results/summary, including the GitHub URL of the pushed sweep file
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
