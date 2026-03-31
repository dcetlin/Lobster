#!/usr/bin/env python3
"""
Signal footer enforcement hook for send_reply.

Fires before mcp__lobster-inbox__send_reply tool calls.
Blocks messages that reference completed work but have no signal footer code block.

Exit codes:
  0 - Allow the tool call
  2 - Block the tool call (Claude Code shows stderr to Claude)
"""

import json
import re
import sys


# Keywords indicating completed actions (case-insensitive)
ACTION_KEYWORDS = [
    r"\bmerged\b",
    r"\bmerge\b",
    r" PR #\d+",
    r"\bpull request\b",
    r"\bspawned\b",
    r"\bbuilt\b",
    r"\bwrote\b",
    r"\bscheduled\b",
    r"\bdeleted\b",
    r"\bcreated\b",
    r"\bfixed\b",
    r"\bimplemented\b",
    r"\bdeployed\b",
    r"\binstalled\b",
]

ACTION_RES = [re.compile(p, re.IGNORECASE) for p in ACTION_KEYWORDS]

# Match a side-effects code block with the canonical label.
# Only "side-effects:" is accepted — no other label is valid.
# This enforces the canonical format from sys.subagent.bootup.md.
SIDE_EFFECTS_BLOCK_RE = re.compile(r"```side-effects:[^`]*```", re.DOTALL)

# Match the explicit null case: a bare "side-effects: none" line (not a code block).
# This is the canonical way to declare that a message has no side effects.
SIDE_EFFECTS_NONE_RE = re.compile(r"^side-effects:\s*none\s*$", re.MULTILINE | re.IGNORECASE)


def has_action_keywords(text: str) -> bool:
    return any(r.search(text) for r in ACTION_RES)


def has_signal_footer(text: str) -> bool:
    """
    Returns True if the message contains either:
    1. A ```side-effects: ... ``` code block (for messages with side effects)
    2. A bare "side-effects: none" line (explicit null — for messages with no side effects)

    The label must be exactly "side-effects:" — no other label is accepted.
    This matches the canonical format enforced by sys.subagent.bootup.md and
    sys.dispatcher.bootup.md.
    """
    if SIDE_EFFECTS_BLOCK_RE.search(text):
        return True

    if SIDE_EFFECTS_NONE_RE.search(text):
        return True

    return False


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # If we can't parse the input, allow the call
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Only check send_reply calls
    if tool_name != "mcp__lobster-inbox__send_reply":
        sys.exit(0)

    text = tool_input.get("text", "")
    if not text:
        sys.exit(0)

    if has_action_keywords(text) and not has_signal_footer(text):
        print(
            "BLOCKED: Message references completed work but has no signal footer. "
            "Either add a ```side-effects: ... ``` code block listing emoji signals, "
            "or write 'side-effects: none' on its own line if there are truly no side effects. "
            "Label must be exactly 'side-effects:' (not 'signals:' or anything else).",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
