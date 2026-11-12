"""
Routing preference loader and matcher.

Rules are stored in ~/lobster-user-config/routing-preferences.yaml.
Each active rule has a condition (plain-English time/type description)
and a route_to field naming the agent hint.

Schema:
  rules:
    - id: "rule_20260523_001"
      condition: "brain-dump messages arriving between 06:00-09:00"
      route_to: "voice-note-agent"
      confidence: 0.82
      observation_count: 7
      confirmed_by: "dan"
      confirmed_at: "2026-05-23T08:14:00Z"
      active: true
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PREFS_PATH = Path.home() / "lobster-user-config" / "routing-preferences.yaml"


def load_routing_preferences(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load active routing rules from routing-preferences.yaml.

    Returns only rules where active == True. Returns [] if file absent.
    """
    prefs_path = Path(path) if path else _DEFAULT_PREFS_PATH
    if not prefs_path.exists():
        return []

    try:
        raw = yaml.safe_load(prefs_path.read_text()) or {}
    except Exception:
        return []

    rules = raw.get("rules") or raw.get("preferences") or []
    return [r for r in rules if isinstance(r, dict) and r.get("active", True)]


def match_routing_preference(
    message: dict[str, Any], rules: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the first active rule whose condition matches the message, or None.

    Matching is string + time-range based — no ML in cycle 1.

    Condition strings are checked against:
    - message type (e.g. "voice_note", "brain_dump")
    - message source (e.g. "telegram")
    - current time-of-day (for "HH:MM-HH:MM" or "HH:00-HH:00" patterns)
    """
    if not rules:
        return None

    msg_type = str(message.get("type", "")).lower()
    msg_source = str(message.get("source", "")).lower()
    now_utc = datetime.now(timezone.utc)
    now_minutes = now_utc.hour * 60 + now_utc.minute

    for rule in rules:
        condition = str(rule.get("condition", "")).lower()
        if _condition_matches(condition, msg_type, msg_source, now_minutes):
            return rule

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})")


def _condition_matches(
    condition: str,
    msg_type: str,
    msg_source: str,
    now_minutes: int,
) -> bool:
    # Check message type keywords
    type_hit = (
        (msg_type and msg_type in condition)
        or ("brain" in condition and "brain" in msg_type)
        or ("voice" in condition and "voice" in msg_type)
    )

    # Check source keywords
    source_hit = not msg_source or (msg_source in condition) or ("telegram" in condition and "telegram" in msg_source)

    # Check time-range
    match = _TIME_RANGE_RE.search(condition)
    if match:
        start_h, start_m, end_h, end_m = (int(g) for g in match.groups())
        start_min = start_h * 60 + start_m
        end_min = end_h * 60 + end_m
        in_range = start_min <= now_minutes <= end_min
        # Condition requires both a type/source keyword match AND time match
        return (type_hit or source_hit) and in_range

    # No time range in condition — keyword match alone is sufficient
    return type_hit
