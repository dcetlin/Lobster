#!/usr/bin/env python3
"""
PostToolUse hook: routes decision: footer blocks to the decisions ledger.

When a send_reply message contains a ```decision: ... ``` code block,
extract the content and append it to ~/lobster-workspace/data/decisions-ledger.md.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone


DECISION_BLOCK_RE = re.compile(r"```decision:\s*\n(.*?)```", re.DOTALL)
LEDGER_PATH = os.path.expanduser("~/lobster-workspace/data/decisions-ledger.md")


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if data.get("tool_name") != "mcp__lobster-inbox__send_reply":
        sys.exit(0)

    text = data.get("tool_input", {}).get("text", "")
    if not text:
        sys.exit(0)

    match = DECISION_BLOCK_RE.search(text)
    if not match:
        sys.exit(0)

    decision_text = match.group(1).strip()
    if not decision_text:
        sys.exit(0)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)

    if not os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, "w") as f:
            f.write(
                "# Decisions Ledger\n\n"
                "Append-only log of choices made as captured from `decision:` footer blocks.\n"
                "One entry per decision. Routed automatically by `hooks/decision-router.py`.\n\n"
            )

    with open(LEDGER_PATH, "a") as f:
        f.write(f"\n---\n**{date_str}** — {decision_text}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
