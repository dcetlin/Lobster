#!/usr/bin/env python3
"""PreToolUse hook: blocks 'gh pr create' when an open PR already exists for the
same branch on the same repo.

WOS executor re-dispatches UoWs, and agents may attempt to create duplicate PRs.
This hook enforces idempotency structurally: any 'gh pr create' command is
checked against the GitHub API before execution. If a matching open PR exists,
the tool call is hard-blocked.

The hook uses branch-name matching only (via --head), not title matching.
Title-prefix matching was rejected in the oracle review of PR #1148 due to
false-positive risk across unrelated UoWs with similar titles.

Exit codes:
  0 -- not a PR create command, or no existing PR found (allow)
  2 -- hard block: an open PR already exists for this branch/repo
"""
import json
import re
import subprocess
import sys


def extract_repo(command: str) -> "str | None":
    """Extract the --repo / -R value from a gh pr create command.

    Handles:
      --repo owner/repo
      --repo=owner/repo
      -R owner/repo
    """
    match = re.search(r'(?:--repo(?:=|\s+)|-R\s+)([\w./-]+)', command)
    return match.group(1) if match else None


def extract_head_branch(command: str) -> "str | None":
    """Extract the --head / -H value from a gh pr create command.

    Handles:
      --head branch-name
      --head=branch-name
      -H branch-name
    """
    match = re.search(r'(?:--head(?:=|\s+)|-H\s+)([\w./-]+)', command)
    return match.group(1) if match else None


def get_current_branch() -> "str | None":
    """Get the current git branch name as a fallback when --head is not specified."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch and branch != "HEAD" else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def check_for_existing_pr(repo: str, head_branch: str) -> "dict | None":
    """Query GitHub for an open PR matching the given repo and head branch.

    Returns the first matching PR as a dict with 'number' and 'title' keys,
    or None if no match is found. Fails open (returns None) on any error.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--head", head_branch,
                "--state", "open",
                "--json", "number,title",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    try:
        prs = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    if not prs:
        return None

    return prs[0]


# ---------------------------------------------------------------------------
# Top-level hook execution
# ---------------------------------------------------------------------------

try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

if tool_name != "Bash":
    sys.exit(0)

command = tool_input.get("command", "")

if "gh pr create" not in command:
    sys.exit(0)

repo = extract_repo(command)
head = extract_head_branch(command)

# Fallback: if --head is not in the command, detect the current branch
if head is None:
    head = get_current_branch()

# If we cannot determine repo or branch, fail open (allow the call)
if repo is None or head is None:
    sys.exit(0)

existing = check_for_existing_pr(repo, head)

if existing is None:
    # No matching PR found -- allow creation
    sys.exit(0)

pr_number = existing.get("number", "?")
pr_title = existing.get("title", "(untitled)")

print(
    f"BLOCKED: PR already exists for branch '{head}' on {repo}: "
    f"PR #{pr_number} -- {pr_title}.\n"
    f"To proceed, close the existing PR first or use a different branch.\n"
    f"Idempotency hook -- see hooks/check-pr-exists-before-create.py.",
    file=sys.stderr,
)
sys.exit(2)
