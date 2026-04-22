#!/usr/bin/env python3
"""PreToolUse hook: blocks 'gh pr merge' commands where the oracle has not approved
the PR.

The PR Merge Gate in CLAUDE.md requires that every code PR pass oracle review before
merge. This hook enforces that requirement mechanically: any 'gh pr merge <N>' command
is blocked unless oracle/decisions.md contains a 'VERDICT: APPROVED' entry for PR #N
as its most recent verdict.

If no oracle entry exists for the PR, a warning is printed but the merge is not
blocked — the PR may be infrastructure-level or pre-oracle.

Exit codes:
  0 — not a merge command, no PR number found, oracle APPROVED, or no oracle entry
  2 — hard block: oracle verdict is not APPROVED
"""
import json
import re
import sys
from pathlib import Path


DECISIONS_FILE = Path(__file__).parent.parent / "oracle" / "decisions.md"


def extract_pr_number(command: str) -> "int | None":
    """Extract the PR number from a gh pr merge command.

    Handles forms like:
      gh pr merge 123
      gh pr merge --squash 123
      gh pr merge 123 --squash
      gh pr merge https://github.com/owner/repo/pull/123
    """
    # Find the start of 'gh pr merge' in the command
    merge_match = re.search(r'gh\s+pr\s+merge\b', command)
    if not merge_match:
        return None

    # Work on the substring after 'gh pr merge'
    rest = command[merge_match.end():]

    # Try the specific regex from the spec first
    spec_match = re.search(r'\b(\d+)\b', rest)
    if spec_match:
        return int(spec_match.group(1))

    return None


def find_oracle_verdict(pr_number: int) -> "str | None":
    """Find the most recent oracle verdict for a PR number.

    Returns 'APPROVED', 'NEEDS_CHANGES', or None if no entry exists.
    The most recent entry is the topmost section in the file.
    """
    if not DECISIONS_FILE.exists():
        return None

    content = DECISIONS_FILE.read_text()

    # Split into sections by ### [
    sections = re.split(r'(?=### \[)', content)

    pr_token = f"PR #{pr_number}"
    pr_boundary_re = re.compile(rf'PR #{re.escape(str(pr_number))}(?!\d)')

    for section in sections:
        if not section.strip():
            continue
        # Check if the header line contains PR #<number> as a whole token
        header_line = section.split('\n', 1)[0]
        if not pr_boundary_re.search(header_line):
            continue
        # Found the most recent entry for this PR number
        if '**VERDICT: APPROVED**' in section:
            return 'APPROVED'
        if '**VERDICT: NEEDS_CHANGES**' in section:
            return 'NEEDS_CHANGES'
        # Some other verdict
        verdict_match = re.search(r'\*\*VERDICT:\s*([^*]+)\*\*', section)
        if verdict_match:
            return verdict_match.group(1).strip()
        return 'UNKNOWN'

    return None


try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

if tool_name != "Bash":
    sys.exit(0)

command = tool_input.get("command", "")

if "gh pr merge" not in command:
    sys.exit(0)

pr_number = extract_pr_number(command)
if pr_number is None:
    # Cannot determine PR number — do not block
    sys.exit(0)

verdict = find_oracle_verdict(pr_number)

if verdict is None:
    # No oracle entry — warn but do not block (may be pre-oracle or infrastructure)
    print(
        f"WARNING: gh pr merge attempted for PR #{pr_number} but no oracle entry found in "
        f"oracle/decisions.md. Allowing merge — add an oracle review if this is a code PR.",
        file=sys.stderr,
    )
    sys.exit(0)

if verdict == 'APPROVED':
    sys.exit(0)

print(
    f"BLOCKED: gh pr merge attempted for PR #{pr_number} but oracle verdict is {verdict} (not APPROVED).\n"
    "Run the oracle agent first and confirm APPROVED appears in oracle/decisions.md before merging.\n"
    "PR Merge Gate — see Tier-1 Gate Register in CLAUDE.md.",
    file=sys.stderr,
)
sys.exit(2)
