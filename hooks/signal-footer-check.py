#!/usr/bin/env python3
"""
Signal footer enforcement hook for send_reply.

Fires before mcp__lobster-inbox__send_reply tool calls.

Convention:
- When a message has side effects: end with a ```side-effects: ... ``` code block.
- When a message has no side effects: omit the footer entirely — write nothing.
- `side-effects: none` in any form is BANNED. Omit the footer instead.
- Any footer-like code block with a wrong label (signals:, effects:, etc.) is blocked.

Exit codes:
  0 - Allow the tool call
  2 - Block the tool call (Claude Code shows stderr to Claude)
"""

import json
import re
import sys


# Match a side-effects code block with the canonical label.
# Only "side-effects:" is accepted — no other label is valid.
# This enforces the canonical format from sys.subagent.bootup.md.
SIDE_EFFECTS_BLOCK_RE = re.compile(r"```side-effects:[^`]*```", re.DOTALL)

# Match "side-effects: none" in any form:
# 1. As a bare line: "side-effects: none"
# 2. As a code block: ```side-effects:\nnone\n```
# Both forms are banned — omit the footer entirely instead.
SIDE_EFFECTS_NONE_BARE_RE = re.compile(r"^side-effects:\s*none\s*$", re.MULTILINE | re.IGNORECASE)
SIDE_EFFECTS_NONE_BLOCK_RE = re.compile(r"```side-effects:\s*\nnone\s*\n```", re.DOTALL | re.IGNORECASE)

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


def has_side_effects_none(text: str) -> bool:
    """Returns True if the message contains a banned 'side-effects: none' in any form."""
    if SIDE_EFFECTS_NONE_BARE_RE.search(text):
        return True
    if SIDE_EFFECTS_NONE_BLOCK_RE.search(text):
        return True
    return False


def detect_wrong_label(text: str) -> str | None:
    """
    Returns a human-readable description of the wrong label found, or None if
    no wrong-label footer is detected.

    Only fires when no canonical side-effects block is present.
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

    # Check 1: "side-effects: none" in any form is banned.
    # The canonical convention is to omit the footer entirely when there are no side effects.
    if has_side_effects_none(text):
        print(
            "BLOCKED: `side-effects: none` is no longer valid. "
            "Omit the footer entirely when there are no side effects.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Check 2: If a footer-like code block is present, its label must be exactly "side-effects:".
    # A canonical side-effects block is valid — allow it.
    # A wrong-label block is blocked.
    if not SIDE_EFFECTS_BLOCK_RE.search(text):
        wrong_label = detect_wrong_label(text)
        if wrong_label is not None:
            print(
                f"BLOCKED: Wrong footer label — must be exactly `side-effects:` (got `{wrong_label}`). "
                "Use ```side-effects:\\n<signals>\\n``` for messages with side effects. "
                "Omit the footer entirely when there are no side effects.",
                file=sys.stderr,
            )
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
