"""
LOS — Morning Digest Integration

Pure functions for building the todos section of the morning digest.
No DB access — callers pass the list of items.

Produces plain text (no inline buttons — buttons are for /todos command only).
Section is omitted if item list is empty (no clutter).
"""
from __future__ import annotations

from typing import Sequence

from .db import ActionItem

# Maximum items shown in morning digest (spec: top 3)
DIGEST_MAX_ITEMS = 3

_PRIORITY_LABEL = {
    range(1, 4): "urgent",
    range(4, 7): "medium",
    range(7, 11): "low",
}


def _priority_label(priority: int) -> str:
    for r, label in _PRIORITY_LABEL.items():
        if priority in r:
            return label
    return "low"


def format_digest_section(items: Sequence[ActionItem]) -> str:
    """Build the morning digest todos section as plain text.

    Returns an empty string if there are no items (caller omits the section).
    Shows top DIGEST_MAX_ITEMS items, sorted by priority ASC then mention_count DESC.
    """
    if not items:
        return ""

    top = sorted(items, key=lambda i: (i.priority, -i.mention_count))[:DIGEST_MAX_ITEMS]

    lines = ["Open todos:"]
    for item in top:
        label = _priority_label(item.priority)
        mention = f" (x{item.mention_count})" if item.mention_count > 1 else ""
        lines.append(f"  - [{label}] {item.text}{mention}")

    return "\n".join(lines)


def build_digest_footer(items: Sequence[ActionItem]) -> str:
    """Build the full footer text appended to the morning digest.

    Returns empty string when there are no open items.
    Includes a /todos prompt so Dan knows how to get the interactive view.
    """
    section = format_digest_section(items)
    if not section:
        return ""
    return f"\n---\n{section}\n\nType /todos for the full list with action buttons."
