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

**Start here:** Read `~/lobster/memory/canonical-templates/sweep-context.md` in full. That file is your complete operating context. Do not proceed until you have read it.

It contains: the negentropic principal framing, domain rotation logic (7-night cycle with state in `~/lobster-workspace/hygiene/rotation-state.json`), per-session structure (detection pass → refactor pass → escalation list), autonomy calibration rules (including code-layer counter-force requirements and artifact-type sub-classification), Night 7 vision drift check, output format, and the two-ping protocol.

> Note: sweep-context.md was moved from `~/lobster-workspace/hygiene/sweep-context.md` (runtime data, unversioned) to `~/lobster/memory/canonical-templates/sweep-context.md` (repo, versioned) so that improvements to the sweep process are tracked in git. The runtime copy at the old path is no longer authoritative.

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

Read `~/lobster/oracle/learnings.md`. If the file does not exist, note its absence and skip this step.

**Before filing any escalation items with the `learning-not-remediated` label, ensure the label exists in the repo:**
```bash
gh label create "learning-not-remediated" --color "e11d48" --description "Learning from oracle review not yet remediated" --repo dcetlin/Lobster 2>/dev/null || true
```

For each entry in learnings.md:

1. Parse the entry date (expected format: `YYYY-MM-DD` or similar ISO date in the entry header or metadata).
2. **Minimum-age guard:** If the entry date is within the past 24 hours (i.e., the entry was written today or since the last 24-hour mark), skip it — these entries are too fresh to expect remediation. Note skipped entries in the Learnings Verification subsection as "Skipped — written within past 24 hours."
3. If the entry date is within the past 7 days (inclusive of today, excluding entries skipped by the minimum-age guard), it is a **recent entry** subject to verification.
4. For each recent entry, identify the **runtime symptom** it describes — what observable behavior, artifact, log pattern, or registry state the entry says was present.
5. Check whether that symptom is still present using the following logic — **in order**:
   - **(a) Named file check:** Does the entry text explicitly name a specific source file (e.g., `executor.py`, `token-ledger-collect.sh`, a script path)? If yes, read that file and look for the symptom described (e.g., a logic inversion, wrong variable name, counter ordering defect). Do not substitute hygiene sweep files or logs for this check.
   - **(b) Behavioral/operational symptom check:** Does the entry describe a behavioral or operational symptom that would be observable in runtime artifacts (e.g., wrong output directory, missing log entry, rotation state corruption)? If yes, check:
     - Sweep output files in `~/lobster-workspace/hygiene/`
     - Job logs in `~/lobster-workspace/scheduled-jobs/logs/`
     - Rotation state in `~/lobster-workspace/hygiene/rotation-state.json`
   - **(c) Neither source available:** If neither (a) nor (b) applies — the entry describes a design-level defect not tied to a named file and not observable in logs or sweep files — classify as: **Cannot verify from available artifacts — manual review required.** Do not classify as "Resolved."
6. Classify the result:
   - **Resolved** — symptom is no longer present in the checked artifacts (only applicable when (a) or (b) produced an observable result)
   - **Still present** — symptom persists; add this as an ESC item with label `learning-not-remediated` in the Escalation List of your sweep output, formatted as:
     ```
     ESC [learning-not-remediated] <title from learnings entry> — symptom still present as of <today's date>. Source: oracle/learnings.md entry <date>.
     ```
   - **Cannot verify from available artifacts — manual review required** — the entry documents a defect not observable in hygiene files, logs, or a named file; do not escalate as "Still present" but include in the Learnings Verification subsection

Include a **Learnings Verification** subsection in your sweep output file listing each recent entry checked, its classification (Resolved / Still present / Cannot verify / Skipped), and a one-sentence rationale.

If no entries in learnings.md are dated within the past 7 days (after applying the minimum-age guard), note "No recent learnings entries to verify" in the subsection.

**Learnings stall detection:**

After completing the per-entry verification above, also check for a learnings cadence stall:

1. Find the date of the most recent entry in learnings.md (the newest entry regardless of the 7-day window).
2. Compute the number of days since that entry: `days_since_last_entry = (today - most_recent_entry_date).days`
3. Apply the stall signal rules:
   - If `days_since_last_entry >= 7` AND `days_since_last_entry < 14`: include a note in the Learnings Verification subsection: "⚠️ LEARNINGS CADENCE WARNING: No oracle learnings entries written in [N] days — monitor for stall."
   - If `days_since_last_entry >= 14`: surface as a signal in the sweep output AND add to the Escalation List:
     > ⚠️ LEARNINGS STALL: No oracle learnings entries written in [N] days. This indicates oracle review cadence has stalled — not a routine null result. Check whether WOS queue is being dispatched and oracle reviews are completing.

If learnings.md does not exist, the absence itself is a stall signal — treat as stall duration unknown and escalate.

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

### Resolution rate metric

**This step runs after escalation items are filed as GitHub issues and before calling write_task_output.**

Compute a resolution rate for prior sweep escalations:

1. **Count newly filed issues in this sweep:** Record `filed_count` — the number of new GitHub issues you filed in the "Convert escalation items" step above (not issues that already existed).

2. **Find the last sweep date:** Read the most recent sweep file in `~/lobster-workspace/hygiene/` before today's (e.g., `ls -t ~/lobster-workspace/hygiene/20*-sweep.md | sed -n '2p'` to get the second-most-recent). Extract the date from the filename as `last_sweep_date`.

3. **Count closed prior escalations:** Query GitHub for issues closed since the last sweep that carry the `bug` OR `hygiene` label (two separate queries to avoid AND semantics):
   ```bash
   RESOLVED_HYGIENE=$(gh issue list --repo dcetlin/Lobster --state closed --label hygiene \
     --search "closed:>YYYY-MM-DD" --json number --jq 'length')
   RESOLVED_BUG=$(gh issue list --repo dcetlin/Lobster --state closed --label bug \
     --search "closed:>YYYY-MM-DD" --json number --jq 'length')
   closed_prior_count=$((RESOLVED_HYGIENE + RESOLVED_BUG))
   ```
   Record `closed_prior_count` — the sum of the two counts. Note: issues with both labels are double-counted, but that is acceptable for this rate metric. Note: this is a proxy metric — not all closed issues necessarily came from sweep escalations, but over time the rate reflects whether sweep-filed issues are being actioned.

4. **Compute resolution rate:** If `filed_count > 0` from the prior sweep (use the prior sweep's filed_count if available in its output summary, otherwise use `closed_prior_count` directly as an approximation): `resolution_rate = closed_prior_count / prior_filed_count * 100`

5. **Write to write_task_output output field:**
   ```
   Resolution rate: [closed_prior_count] of [prior_filed_count] prior escalations closed since last sweep ([YYYY-MM-DD])
   ```

6. **Entropy accumulation signal:** Track whether the resolution rate has been 0% across 3 or more consecutive sweeps. Check the last 3 sweep output files for a "Resolution rate: 0 of N" line. If 3 or more consecutive sweeps show zero closed items with non-zero filings, add to the Escalation List:
   > ⚠️ ENTROPY ACCUMULATION: Sweep has filed N items across the last 3 cycles with 0 resolved. The sweep is describing entropy, not reducing it. The escalation pipeline may be broken — issues may not be reaching Dan's attention.

---

## Output

When you complete your task, call `write_task_output` with:
- job_name: "negentropic-sweep"
- output: Your results/summary, including the GitHub URL of the pushed sweep file
- status: "success" or "failed"

Then call `write_result` with:
- task_id: your task_id
- chat_id: 0
- sent_reply_to_user: True
- outcome_category: "seed" (this job grows compost — structured residue that feeds future synthesis)

Keep output concise. The main Lobster instance will review this later.
