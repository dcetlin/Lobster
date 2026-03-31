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

# Wrong-label patterns: fenced code blocks that look like footers but use the wrong label.
# Ordered from most specific to most general to return the most useful error message.
WRONG_LABEL_PATTERNS = [
    # Explicit common wrong labels
    (re.compile(r"```(signals):[^`]*```", re.DOTALL), None),
    (re.compile(r"```(effects):[^`]*```", re.DOTALL), None),
    (re.compile(r"```(side_effects):[^`]*```", re.DOTALL), None),
    # "side-effects" without colon (malformed — missing colon)
    (re.compile(r"```(side-effects)\s[^`]*```", re.DOTALL), "side-effects (missing colon — label must be `side-effects:`)"),
    # Any fenced code block whose label contains "signal", "effect", or "side"
    (re.compile(r"```([a-z_-]*(?:signal|effect|side)[a-z_-]*):[^`]*```", re.DOTALL | re.IGNORECASE), None),
]

# Wrong null-form patterns: bare "label: none" lines with wrong label
WRONG_NULL_PATTERNS = [
    re.compile(r"^(signals|effects|side_effects):\s*none\s*$", re.MULTILINE | re.IGNORECASE),
]


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


def detect_wrong_label(text: str) -> str | None:
    """
    Returns a human-readable description of the wrong label found, or None if
    no wrong-label footer is detected.

    Only fires when has_signal_footer() is False — i.e., no canonical footer is present.
    """
    for pattern, override_label in WRONG_LABEL_PATTERNS:
        m = pattern.search(text)
        if m:
            label = override_label if override_label is not None else m.group(1)
            return label

    for pattern in WRONG_NULL_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1) + ": none"

    return None


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
        wrong_label = detect_wrong_label(text)
        if wrong_label is not None:
            print(
                f"BLOCKED: Wrong footer label — must be exactly `side-effects:` (got `{wrong_label}`). "
                "Use ```side-effects:\\n<signals>\\n``` for messages with side effects, "
                "or `side-effects: none` on its own line for messages with no side effects.",
                file=sys.stderr,
            )
        else:
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
