"""
LOS — Action Item Extractor

Persists pre-extracted action commitments to the DB. Extraction itself is
performed by the calling subagent (Claude natively) — this module handles
only the parsing and persistence boundary.

Usage (from a subagent that has already identified action items):

    from src.los.db import connect
    from src.los.extractor import extract_action_items, parse_llm_response

    conn = connect()
    # The subagent produces a JSON string such as:
    #   '[{"text": "Call Sarah about the contract", "priority": 3}]'
    items = parse_llm_response(raw_json)
    saved = extract_action_items(
        conn=conn,
        items=items,
        source="telegram",
        source_message_id="msg_123",
    )
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from .db import (
    ActionItem,
    compute_dedup_key,
    find_duplicate,
    increment_mention_count,
    insert_action_item,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (named after spec requirements — don't use magic literals)
# ---------------------------------------------------------------------------

PRIORITY_MIN = 1
PRIORITY_MAX = 10
PRIORITY_DEFAULT = 5


# ---------------------------------------------------------------------------
# Pure parsing helper
# ---------------------------------------------------------------------------


def parse_llm_response(raw: str) -> list[dict]:
    """Parse a subagent's JSON extraction output into a list of validated item dicts.

    The subagent is expected to produce JSON of the form:
        [{"text": "...", "priority": <int>}]

    Returns an empty list on any error — callers must handle [] gracefully.
    Each returned dict is guaranteed to have 'text' (str) and 'priority' (int).
    """
    try:
        parsed = json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        log.warning("LOS extractor: failed to parse subagent response as JSON")
        return []

    if not isinstance(parsed, list):
        log.warning("LOS extractor: subagent response was not a list")
        return []

    valid = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        raw_priority = item.get("priority", PRIORITY_DEFAULT)
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            priority = PRIORITY_DEFAULT
        # Clamp to [PRIORITY_MIN, PRIORITY_MAX]
        priority = max(PRIORITY_MIN, min(PRIORITY_MAX, priority))
        valid.append({"text": text.strip(), "priority": priority})

    return valid


# ---------------------------------------------------------------------------
# Side-effectful entry point (DB writes at the boundary)
# ---------------------------------------------------------------------------


def extract_action_items(
    conn: sqlite3.Connection,
    items: list[dict],
    source: str,
    source_message_id: Optional[str],
) -> list[ActionItem]:
    """Persist pre-extracted action items to the DB.

    `items` is a list of dicts with 'text' (str) and 'priority' (int) keys,
    as produced by parse_llm_response. The calling subagent is responsible
    for extracting these from the source text using its native intelligence.

    For each item:
    - If a duplicate exists (open/snoozed), increments mention_count
    - Otherwise, inserts a new row

    Returns the list of inserted or updated ActionItem objects.
    Returns [] when items is empty.
    """
    from .db import get_item_by_id

    result: list[ActionItem] = []
    for item_dict in items:
        item_text = item_dict["text"]
        priority = item_dict["priority"]

        existing = find_duplicate(conn, item_text)
        if existing is not None:
            increment_mention_count(conn, existing.id)
            updated = get_item_by_id(conn, existing.id)
            if updated:
                result.append(updated)
        else:
            row_id = insert_action_item(
                conn=conn,
                text=item_text,
                source=source,
                source_message_id=source_message_id,
                priority=priority,
            )
            inserted = get_item_by_id(conn, row_id)
            if inserted:
                result.append(inserted)

    return result
