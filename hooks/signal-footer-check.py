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

# Emoji signals from the legend (used in signal footers)
SIGNAL_EMOJIS = [
    "\U0001f916",  # 🤖
    "\u2705",      # ✅
    "\U0001f419",  # 🐙
    "\U0001f500",  # 🔀
    "\U0001f5d1",  # 🗑️
    "\u26a0",      # ⚠️
    "\U0001f4dd",  # 📝
    "\U0001f50d",  # 🔍
    "\U0001f527",  # 🔧
    "\U0001f4ac",  # 💬
]

ACTION_RES = [re.compile(p, re.IGNORECASE) for p in ACTION_KEYWORDS]

# Match a closing code fence (``` possibly with trailing whitespace/newline)
CODE_BLOCK_END_RE = re.compile(r"```\s*$", re.MULTILINE)

# Match a line that contains only emoji signal characters and optional multipliers like "x2"
# This line must be at or near the end of the message
SIGNAL_LINE_RE = re.compile(
    r"^[" + "".join(re.escape(e) for e in SIGNAL_EMOJIS) + r"\s️xX\d]+$",
    re.MULTILINE,
)


def has_action_keywords(text: str) -> bool:
    return any(r.search(text) for r in ACTION_RES)


def has_signal_footer(text: str) -> bool:
    """
    Returns True if the message ends with either:
    1. A markdown code block (``` ... ```)
    2. A trailing line containing only emoji signal characters
    """
    stripped = text.strip()

    # Check for closing code block at the end
    if stripped.endswith("```"):
        return True

    # Check for a code block anywhere near the end (last 200 chars)
    tail = stripped[-200:]
    if CODE_BLOCK_END_RE.search(tail):
        return True

    # Check last non-empty line for emoji-only signal line
    lines = stripped.splitlines()
    non_empty_lines = [l for l in lines if l.strip()]
    if non_empty_lines:
        last_line = non_empty_lines[-1].strip()
        if SIGNAL_LINE_RE.match(last_line):
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
            "Add a code block at the end listing your side-effect signals (🤖 ✅ 🐙 🔀 etc.).",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
