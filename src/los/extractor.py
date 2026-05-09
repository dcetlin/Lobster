"""
LOS — Action Item Extractor

Extracts first-person action commitments from text using Claude claude-haiku-4-5.
Side effects (DB writes) happen at the end — pure extraction logic is testable
without a DB.

Usage:
    from src.los.db import connect
    from src.los.extractor import extract_action_items

    conn = connect()
    items = extract_action_items(
        conn=conn,
        text="I need to call Sarah about the contract.",
        source="telegram",
        source_message_id="msg_123",
    )
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

import anthropic

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

EXTRACTION_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 512
PRIORITY_MIN = 1
PRIORITY_MAX = 10
PRIORITY_DEFAULT = 5

_SYSTEM_PROMPT = (
    "You are an action-commitment detector. Given text from Dan's voice note, "
    "journal entry, or Telegram message, extract only first-person commitments "
    "Dan has made to do something himself.\n\n"
    "Rules:\n"
    "- Include: 'I need to', 'I should', 'I want to', 'I'm going to', "
    "'I have to', 'remind me to', 'I promised to'\n"
    "- Exclude: thoughts, questions, observations, things Lobster should do, "
    "system tasks, pure emotions\n"
    "- Each commitment becomes one todo item with a short, imperative title "
    "(e.g. 'Call Sarah about the contract')\n"
    "- Assign priority as an integer from 1 (urgent) to 10 (low/aspirational): "
    "1-3 = explicit urgency/deadline, 4-6 = clear intent, 7-10 = vague/aspirational\n\n"
    "Return JSON only: [{\"text\": \"...\", \"priority\": <int>}]\n"
    "If no action commitments found, return []"
)


# ---------------------------------------------------------------------------
# Pure extraction helpers
# ---------------------------------------------------------------------------


def parse_llm_response(raw: str) -> list[dict]:
    """Parse the LLM JSON response into a list of validated item dicts.

    Returns an empty list on any error — callers must handle [] gracefully.
    Each returned dict is guaranteed to have 'text' (str) and 'priority' (int).
    """
    try:
        parsed = json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        log.warning("LOS extractor: failed to parse LLM response as JSON")
        return []

    if not isinstance(parsed, list):
        log.warning("LOS extractor: LLM response was not a list")
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


def _call_llm(text: str) -> list[dict]:
    """Call Claude haiku to extract action items. Returns parsed list or []."""
    client = anthropic.Anthropic()
    message = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = message.content[0].text
    return parse_llm_response(raw)


# ---------------------------------------------------------------------------
# Side-effectful entry point (DB writes at the boundary)
# ---------------------------------------------------------------------------


def extract_action_items(
    conn: sqlite3.Connection,
    text: str,
    source: str,
    source_message_id: Optional[str],
) -> list[ActionItem]:
    """Extract action items from text and persist them to the DB.

    For each extracted item:
    - If a duplicate exists (open/snoozed), increments mention_count
    - Otherwise, inserts a new row

    Returns the list of inserted or updated ActionItem objects.
    Returns [] if the LLM finds nothing or the API call fails.
    """
    try:
        llm_items = _call_llm(text)
    except Exception as exc:
        log.warning("LOS extractor: LLM call failed (%s), skipping extraction", exc)
        return []

    from .db import get_item_by_id

    result: list[ActionItem] = []
    for item_dict in llm_items:
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
