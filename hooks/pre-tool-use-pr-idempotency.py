#!/usr/bin/env python3
"""PreToolUse hook: blocks duplicate `gh pr create` calls for the same branch.

Fires before every Bash tool call. When the command contains `gh pr create`,
extracts the `--repo` value (or falls back to the current git remote) and the
`--head` branch (or the current branch), then queries GitHub for existing open
PRs. If a match is found, the tool call is hard-blocked.

## Why this exists

WOS executor can re-dispatch the same UoW without knowing an open PR already
exists for its branch. Without this guard, each re-dispatch opens a new PR,
producing duplicate PR clusters that pollute the review queue.

Advisory enforcement in markdown instruction files was identified by the oracle
as insufficient (oracle NEEDS_CHANGES on PR #1148). This hook replaces that
advisory approach with a structural block.

## Block condition

A `gh pr create` call is blocked when:
  `gh pr list --repo <repo> --head <branch> --state open` returns at least one result.

The hook never blocks when:
- The tool is not Bash
- The command does not contain `gh pr create`
- The branch cannot be determined (allow-on-error, to avoid blocking legitimate PRs)
- The `gh` subprocess fails (allow-on-error)

## Exit codes

  0 — not applicable, or no existing PR found
  2 — hard block: open PR already exists for this branch

## settings.json configuration

Add to ~/.claude/settings.json under "hooks" -> "PreToolUse":

    {
      "matcher": "Bash",
      "hooks": [
        {
          "type": "command",
          "command": "python3 /home/lobster/lobster/hooks/pre-tool-use-pr-idempotency.py",
          "timeout": 15
        }
      ]
    }
"""

import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Parsing helpers (pure — no I/O)
# ---------------------------------------------------------------------------

_GH_PR_CREATE_RE = re.compile(r'\bgh\s+pr\s+create\b')
_REPO_RE = re.compile(r'--repo\s+(\S+)')
_HEAD_RE = re.compile(r'--head\s+(\S+)')


def contains_gh_pr_create(command: str) -> bool:
    """Return True if the command contains a `gh pr create` invocation."""
    return bool(_GH_PR_CREATE_RE.search(command))


def extract_repo(command: str) -> "str | None":
    """Extract the --repo value from the command string, or None if absent."""
    match = _REPO_RE.search(command)
    return match.group(1) if match else None


def extract_head_branch(command: str) -> "str | None":
    """Extract the --head branch from the command string, or None if absent."""
    match = _HEAD_RE.search(command)
    return match.group(1) if match else None


def _detect_repo_from_remote(cwd: "str | None" = None) -> "str | None":
    """Infer the GitHub repo (owner/name) from the git remote URL.

    Handles both HTTPS and SSH remotes:
      https://github.com/owner/repo.git  -> owner/repo
      git@github.com:owner/repo.git      -> owner/repo

    Returns None if the remote cannot be determined.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        # HTTPS: https://github.com/owner/repo[.git]
        https_match = re.search(r'github\.com/([^/]+/[^/]+?)(?:\.git)?$', url)
        if https_match:
            return https_match.group(1)
        # SSH: git@github.com:owner/repo[.git]
        ssh_match = re.search(r'github\.com:([^/]+/[^/]+?)(?:\.git)?$', url)
        if ssh_match:
            return ssh_match.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def _detect_current_branch(cwd: "str | None" = None) -> "str | None":
    """Return the current git branch name, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# GitHub query (has I/O — thin wrapper for testability)
# ---------------------------------------------------------------------------


def find_existing_pr(repo: str, branch: str) -> "dict | None":
    """Query GitHub for an open PR matching repo + branch.

    Returns the first matching PR dict (with 'number' and 'title' keys),
    or None if none found or the query fails.

    Failure policy: return None (allow-on-error) so a transient gh failure
    never blocks a legitimate PR creation.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--head", branch,
                "--state", "open",
                "--json", "number,title",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        prs = json.loads(result.stdout)
        return prs[0] if prs else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Decision (pure — no I/O)
# ---------------------------------------------------------------------------


def should_block(
    existing_pr: "dict | None",
    branch: str,
    repo: str,
) -> "tuple[bool, str]":
    """Return (block, reason) given the existing PR lookup result.

    Pure function — all I/O is done before calling this.
    """
    if existing_pr is None:
        return False, "no existing open PR found"
    pr_number = existing_pr.get("number", "?")
    pr_title = existing_pr.get("title", "")
    return True, (
        f"Idempotency check: PR already exists for branch '{branch}' on {repo}: "
        f"#{pr_number} '{pr_title}'. "
        f"Close or merge it before creating a new one."
    )


# ---------------------------------------------------------------------------
# Entry point — executed at module level (matches codebase hook convention)
# ---------------------------------------------------------------------------

try:
    _data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

_tool_name = _data.get("tool_name", "")
if _tool_name != "Bash":
    sys.exit(0)

_tool_input = _data.get("tool_input", {})
_command = _tool_input.get("command", "")

if not contains_gh_pr_create(_command):
    sys.exit(0)

# Extract repo — prefer explicit --repo flag, fall back to git remote
_repo = extract_repo(_command) or _detect_repo_from_remote()
if not _repo:
    # Cannot determine repo — allow-on-error
    sys.exit(0)

# Extract branch — prefer explicit --head flag, fall back to current branch
_branch = extract_head_branch(_command) or _detect_current_branch()
if not _branch:
    # Cannot determine branch — allow-on-error
    sys.exit(0)

_existing_pr = find_existing_pr(_repo, _branch)
_block, _reason = should_block(_existing_pr, _branch, _repo)

if not _block:
    sys.exit(0)

print(_reason, file=sys.stderr)
sys.exit(2)
