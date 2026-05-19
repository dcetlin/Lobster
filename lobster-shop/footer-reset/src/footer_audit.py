#!/usr/bin/env python3
"""
Footer drift audit for recent outbox messages.

Scans ~/messages/outbox/ for messages that reference completed work but
are missing or have malformed side-effects footers.

Output is pre-formatted for Telegram.
"""

import json
import re
import sys
from pathlib import Path


OUTBOX_DIR = Path.home() / "messages" / "outbox"

# Mirrors ACTION_KEYWORDS from signal-footer-check.py
ACTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
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
]

# Valid form: fenced code block with exactly "side-effects:" label.
# NOTE: bare "side-effects: none" is BANNED per PR #480 — omit the footer entirely instead.
SIDE_EFFECTS_BLOCK_RE = re.compile(r"```side-effects:[^`]*```", re.DOTALL)

# Wrong-label patterns — drift signatures that look footer-like but use the wrong label
WRONG_LABEL_PATTERNS = [
    (re.compile(r"```signals:[^`]*```", re.DOTALL), "wrong label: `signals:` instead of `side-effects:`"),
    (re.compile(r"```effects:[^`]*```", re.DOTALL), "wrong label: `effects:` instead of `side-effects:`"),
    (re.compile(r"```side-effects[^:\n][^`]*```", re.DOTALL), "malformed: `side-effects` missing colon"),
    (re.compile(r"^signals:\s*none\s*$", re.MULTILINE | re.IGNORECASE), "wrong label: `signals: none` instead of `side-effects: none`"),
    # Banned null forms — per PR #480, omit the footer entirely when there are no side effects
    (re.compile(r"^side-effects:\s*none\s*$", re.MULTILINE | re.IGNORECASE), "side-effects: none is not a valid declaration — omit the footer entirely when there are no side effects"),
    (re.compile(r"```side-effects:\s*\nnone\s*\n```", re.DOTALL | re.IGNORECASE), "side-effects: none is not a valid declaration — omit the footer entirely when there are no side effects"),
]


def has_action_keywords(text: str) -> bool:
    return any(p.search(text) for p in ACTION_PATTERNS)


def has_valid_footer(text: str) -> bool:
    return bool(SIDE_EFFECTS_BLOCK_RE.search(text))


def detect_wrong_labels(text: str) -> list[str]:
    return [label for pattern, label in WRONG_LABEL_PATTERNS if pattern.search(text)]


def load_outbox_messages() -> list[dict]:
    if not OUTBOX_DIR.exists():
        return []
    messages = []
    for path in sorted(OUTBOX_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            data["_path"] = path.name
            messages.append(data)
        except (json.JSONDecodeError, OSError):
            pass
    return messages


def audit_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Returns (missing_footer, wrong_label) anomaly lists.
    Each entry is a dict with keys: id, snippet, issues.
    Pure function — no side effects.
    """
    missing_footer = []
    wrong_label = []

    for msg in messages:
        text = msg.get("text", "")
        if not text:
            continue

        wrong_labels = detect_wrong_labels(text)
        if wrong_labels:
            wrong_label.append({
                "id": msg.get("id", msg["_path"]),
                "snippet": text[:80].replace("\n", " "),
                "issues": wrong_labels,
            })

        if has_action_keywords(text) and not has_valid_footer(text):
            # Only flag as missing if not already captured as wrong-label
            if not wrong_labels:
                missing_footer.append({
                    "id": msg.get("id", msg["_path"]),
                    "snippet": text[:80].replace("\n", " "),
                    "issues": ["action keywords present but no side-effects footer"],
                })

    return missing_footer, wrong_label


def format_report(messages: list[dict], missing: list[dict], wrong: list[dict]) -> str:
    """Pure function — transforms audit results into a Telegram-formatted string."""
    total = len(messages)
    anomaly_count = len(missing) + len(wrong)

    if total == 0:
        return "Footer audit: outbox is empty — nothing to scan."

    lines = [f"**Footer audit** — {total} outbox message(s) scanned"]

    if anomaly_count == 0:
        lines.append("No anomalies found. All footers look correct.")
        return "\n".join(lines)

    lines.append(f"{anomaly_count} anomaly(ies) found:\n")

    if wrong:
        lines.append("**Wrong label (drift):**")
        for entry in wrong:
            snippet = entry["snippet"][:60] + ("..." if len(entry["snippet"]) > 60 else "")
            for issue in entry["issues"]:
                lines.append(f"  - `{entry['id']}` — {issue}")
                lines.append(f"    Preview: _{snippet}_")

    if missing:
        lines.append("**Missing footer (action message with no footer):**")
        for entry in missing:
            snippet = entry["snippet"][:60] + ("..." if len(entry["snippet"]) > 60 else "")
            lines.append(f"  - `{entry['id']}` — {entry['issues'][0]}")
            lines.append(f"    Preview: _{snippet}_")

    lines.append("\nCanonical forms:")
    lines.append("  With effects: ` ```side-effects:\\n✅ 🐙\\n``` `")
    lines.append("  No effects: omit the footer entirely (`side-effects: none` is banned)")

    return "\n".join(lines)


def main() -> int:
    messages = load_outbox_messages()
    missing, wrong = audit_messages(messages)
    report = format_report(messages, missing, wrong)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
