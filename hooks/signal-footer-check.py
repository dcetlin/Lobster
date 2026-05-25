#!/usr/bin/env python3
"""
Signal footer enforcement hook for send_reply.

Fires before mcp__lobster-inbox__send_reply tool calls.

Convention:
- When a message has side effects: end with a ```side-effects: ... ``` code block.
- When a decision is being surfaced: end with a ```decision: ... ``` code block.
- Both `side-effects:` and `decision:` are valid footer labels and may coexist.
- When a message has no side effects: omit the footer entirely — write nothing.
- `side-effects: none` in any form is BANNED. Omit the footer instead.
- Any footer-like code block with a wrong label (signals:, effects:, etc.) is blocked.

Exit codes:
  0 - Allow the tool call
  2 - Block the tool call (Claude Code shows stderr to Claude)
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone


# Match a side-effects code block with the canonical label.
# Only "side-effects:" is accepted — no other label is valid.
# This enforces the canonical format from sys.subagent.bootup.md.
SIDE_EFFECTS_BLOCK_RE = re.compile(r"```side-effects:[^`]*```", re.DOTALL)

# Match a decision code block with the canonical label.
# "decision:" is the second accepted footer type alongside "side-effects:".
DECISION_BLOCK_RE = re.compile(r"```decision:[^`]*```", re.DOTALL)

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

# Extracts content inside a side-effects block.
SIDE_EFFECTS_CONTENT_RE = re.compile(r"```side-effects:\s*\n(.*?)```", re.DOTALL)

# Matches "decided" (optionally preceded by ⚖️ and whitespace) at the start of a trimmed line.
DECIDED_LINE_RE = re.compile(r"^(?:⚖️\s*)?decided\b", re.IGNORECASE)

DB_PATH = os.environ.get("DECIDED_DB_PATH", os.path.expanduser("~/lobster-workspace/data/memory.db"))
DECISIONS_LEDGER_PATH = os.environ.get("DECIDED_LEDGER_PATH", os.path.expanduser("~/lobster-workspace/data/decisions-ledger.md"))


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


def route_decided(text: str, task_id: str | None = None) -> None:
    """
    If the message's side-effects block contains a 'decided' signal line,
    write the decision to memory.db and append a dated entry to decisions-ledger.md.
    Failures are swallowed — routing must not block the send_reply.
    """
    m = SIDE_EFFECTS_CONTENT_RE.search(text)
    if not m:
        return

    description = None
    for raw_line in m.group(1).splitlines():
        line = raw_line.strip()
        if DECIDED_LINE_RE.match(line):
            description = DECIDED_LINE_RE.sub("", line).strip() or "decision reached"
            break

    if description is None:
        return

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    date       TEXT NOT NULL,
                    category   TEXT,
                    task_id    TEXT,
                    summary    TEXT NOT NULL,
                    source     TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_dedup
                    ON decisions(date, summary);
                CREATE INDEX IF NOT EXISTS idx_decisions_category
                    ON decisions(category);
                CREATE INDEX IF NOT EXISTS idx_decisions_task_id
                    ON decisions(task_id);
                CREATE INDEX IF NOT EXISTS idx_decisions_date
                    ON decisions(date);
            """)
            conn.execute(
                "INSERT OR IGNORE INTO decisions (date, category, task_id, summary, source) VALUES (?, ?, ?, ?, ?)",
                (date_str, "decision", task_id, description, "decided-signal"),
            )
            conn.commit()
    except Exception:
        pass

    try:
        entry = f"\n---\n**{date_str}** — {description}\n"
        with open(DECISIONS_LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


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

    # Check 2: If a footer-like code block is present, its label must be "side-effects:" or "decision:".
    # A canonical side-effects or decision block is valid — allow it.
    # A wrong-label block is blocked.
    has_valid_footer = SIDE_EFFECTS_BLOCK_RE.search(text) or DECISION_BLOCK_RE.search(text)
    if not has_valid_footer:
        wrong_label = detect_wrong_label(text)
        if wrong_label is not None:
            print(
                f"BLOCKED: Wrong footer label — must be `side-effects:` or `decision:` (got `{wrong_label}`). "
                "Use ```side-effects:\\n<signals>\\n``` for messages with side effects. "
                "Use ```decision:\\n<choice>\\n``` when surfacing a decision. "
                "Omit the footer entirely when there are no side effects.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Route any 'decided' signal found in the side-effects block.
    route_decided(text, tool_input.get("task_id"))

    sys.exit(0)


if __name__ == "__main__":
    main()
