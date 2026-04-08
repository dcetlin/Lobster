# GitHub Issue Cultivator (Filing Cultivator)

**Job**: github-issue-cultivator
**Schedule**: Weekly on Mondays at 9:00 UTC (`0 9 * * 1`)
**Created**: 2026-04-08

## Context

You are running as a scheduled task. The main Lobster instance created this job.

This is the **Filing Cultivator** — the upstream supply actor in the V3 WOS pipeline. It reads a manually-maintained confirmed seed list and files GitHub issues with the `wos` label on `dcetlin/Lobster`.

**Scope boundary:** This job reads a pre-approved seed list and files issues. It does NOT generate new seeds autonomously. Seeds are manually added to the seed file by Dan.

## Instructions

### Step 1: Load the seed list

Read all seeds from `~/lobster-workspace/data/cultivator-seeds.jsonl`. Each line is a JSON object:

```json
{"title": "Issue title", "body": "Issue body text", "labels": ["wos"]}
```

If the file does not exist or is empty (no non-comment lines), write task output with `"No seeds to file"` and exit successfully.

### Step 2: Fetch existing wos issues

Get all open and closed issues on `dcetlin/Lobster` that have the `wos` label, to avoid filing duplicates:

```bash
gh issue list --repo dcetlin/Lobster --label wos --state all --limit 500 --json title
```

Collect the set of existing issue titles (case-insensitive comparison).

### Step 3: File new issues

For each seed in the seed list:

1. Check if the title already exists in the set of existing issue titles (case-insensitive). If it does, skip it and note it as a duplicate.

2. If it does not exist, file the issue:
   ```bash
   gh issue create --repo dcetlin/Lobster \
     --title "<seed title>" \
     --body "<seed body>" \
     --label "<each label from seed labels array>"
   ```
   Note the created issue URL.

3. After filing, add the title to the known-titles set (prevents duplicates within a single run if the seed list has near-duplicates).

### Step 4: Write output

Call `write_task_output` with:
- `job_name`: `"github-issue-cultivator"`
- `output`: Summary including:
  - Total seeds loaded
  - Issues filed (count + URLs)
  - Issues skipped as duplicates (count + titles)
  - Any errors encountered
- `status`: `"success"` if no fatal errors, `"failed"` if the seed file could not be read or more than half the filings failed

Then call `write_result` with:
- `chat_id`: 0
- `sent_reply_to_user`: True
- `text`: Brief summary, e.g. `"github-issue-cultivator: filed=N, skipped=N duplicates"`

## Output

Keep output concise. The main Lobster instance will review this later.
