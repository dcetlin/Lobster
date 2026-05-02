# Option A: GitHub Review Token Setup

Lobster's PR merge gate has two enforcement modes:

- **Option B (default):** Lobster-enforced soft gate. The oracle agent writes `oracle/verdicts/pr-{n}.md`; the dispatcher checks that file for `VERDICT: APPROVED` before merging. No GitHub branch protection rules involved.
- **Option A (upgrade):** A second GitHub PAT (`LOBSTER_REVIEW_TOKEN`) scoped to review approvals on a separate reviewer account. When set, the oracle posts an official GitHub `APPROVED` review on the PR via the GitHub API, satisfying branch protection rules that require a non-author approving reviewer.

Option B remains active regardless of whether Option A is configured. Option A adds GitHub-enforced approval on top.

## PAT Requirements

- **Scope:** `repo` (for private repos) or `public_repo` (for public repos). Specifically, the token needs "Pull requests: Read and write" under repository permissions.
- **Account constraint:** The PAT **must belong to a GitHub account that is not the bot account that opens PRs.** GitHub blocks self-approval — a review posted by the same account that opened the PR does not count toward branch protection requirements. Use a dedicated reviewer account (e.g. a personal account or a second bot account).

## Setup Steps

1. **Generate the PAT** on a reviewer GitHub account (not the bot account):
   - Go to GitHub Settings > Developer settings > Personal access tokens
   - Classic token: select `repo` scope
   - Fine-grained token: grant "Pull requests: Read and write" for the target repo(s)
   - Copy the generated token

2. **Add to the server env file:**
   ```
   LOBSTER_REVIEW_TOKEN=<your-token-here>
   ```
   The env file is typically `~/lobster/config/config.env`.

3. **Restart the MCP server** to pick up the new env var:
   ```bash
   ~/lobster/scripts/restart-mcp.sh
   ```
   Do not run `systemctl restart` directly — use the safe wrapper script.

## Verifying It Works

1. Open a test PR from the bot account.
2. Trigger an oracle review (the dispatcher dispatches the oracle agent after a PR is opened).
3. After the oracle writes `VERDICT: APPROVED` to `oracle/verdicts/pr-{n}.md`, check the PR on GitHub — a GitHub "Approved" review should appear from the reviewer account.
4. Confirm the PR shows "Approved" in the review status, satisfying any branch protection rule requiring a reviewer.

## Failure Handling

If the `gh api` call fails (e.g. token invalid, wrong scope, self-approval attempt), the oracle appends a warning line to the verdict file and continues. Option B (the `oracle/verdicts/pr-{n}.md` soft gate) still governs dispatcher behavior. No oracle run is blocked by a token failure.

Check the warning output in the verdict file if the GitHub review does not appear after an APPROVED verdict:

```
oracle/verdicts/pr-{n}.md
```

## Relationship to Option B

Option A does not replace Option B. The dispatcher still reads `oracle/verdicts/pr-{n}.md` to gate merges. Option A adds a GitHub-native approval on top, so teams that enable branch protection rules requiring a non-author reviewer will have those rules satisfied automatically by the oracle agent.
