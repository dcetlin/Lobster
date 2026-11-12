"""
Scan pattern_observation events and generate routing preference proposals.

Event log format (pending-observations.jsonl / memory-events.jsonl):
  {"timestamp": "...", "content": "...", "type": "note",
   "tags": ["pattern_observation"], "source": "...", "confidence": 0.8}

Events qualify as pattern_observation if:
  - tags contains "pattern_observation", OR
  - event_type == "pattern_observation", OR
  - type == "pattern_observation"
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Candidate event log paths in priority order.
_EVENT_LOG_CANDIDATES = [
    Path.home() / "lobster-workspace" / "data" / "memory-events.jsonl",
    Path.home() / "lobster-workspace" / "data" / "pending-observations.jsonl",
]

ROUTING_PREF_MIN_OCCURRENCES = 3
ROUTING_PREF_MIN_CONFIDENCE = 0.7

_AGENT_KEYWORDS: list[tuple[list[str], str]] = [
    (["brain", "dump", "brain-dump"], "brain-dumps"),
    (["voice", "audio", "transcri"], "brain-dumps"),
    (["philosophy", "poieti", "semantic mirror"], "lobster-meta"),
    (["morning", "briefing", "daily"], "morning-briefing"),
    (["code", "pr ", "pull request", "github", "issue"], "functional-engineer"),
    (["memory", "retrieval", "vector"], "lobster-generalist"),
]


def _resolve_event_log() -> Path | None:
    env_dir = os.environ.get("LOBSTER_DATA_DIR")
    if env_dir:
        candidate = Path(env_dir) / "memory-events.jsonl"
        if candidate.exists():
            return candidate

    for p in _EVENT_LOG_CANDIDATES:
        if p.exists():
            return p

    return None


def _is_pattern_observation(event: dict[str, Any]) -> bool:
    tags = event.get("tags") or []
    return (
        "pattern_observation" in tags
        or event.get("event_type") == "pattern_observation"
        or event.get("type") == "pattern_observation"
    )


def _event_text(event: dict[str, Any]) -> str:
    return event.get("observation") or event.get("content") or event.get("text") or ""


def _infer_agent(text: str) -> str:
    lower = text.lower()
    for keywords, agent in _AGENT_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return agent
    return "general-purpose"


def scan_pattern_observations(
    event_log_path: str | Path | None = None,
    window_hours: int = 24,
    min_count: int = ROUTING_PREF_MIN_OCCURRENCES,
    min_confidence: float = ROUTING_PREF_MIN_CONFIDENCE,
) -> list[dict[str, Any]]:
    """Read pattern_observation events and return candidate routing proposals.

    Returns proposals where observation_count >= min_count AND
    mean confidence >= min_confidence within the last window_hours.

    Each proposal: {condition, route_to, confidence, count, sample_event}

    If no qualifying groups exist, returns a single synthetic proposal
    derived from the most recent observation event (marked synthetic=True).
    """
    log_path = Path(event_log_path) if event_log_path else _resolve_event_log()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    events: list[dict[str, Any]] = []

    if log_path and log_path.exists():
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not _is_pattern_observation(event):
                    continue
                ts_raw = event.get("timestamp") or ""
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                except (ValueError, AttributeError):
                    pass  # include events with unparseable timestamps
                events.append(event)

    # Group by observation text (exact match)
    groups: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        key = _event_text(ev)[:80]
        if not key:
            continue
        groups.setdefault(key, []).append(ev)

    proposals: list[dict[str, Any]] = []
    for condition, group in groups.items():
        if len(group) < min_count:
            continue
        confidences = [float(e.get("confidence", 0.75)) for e in group]
        avg_conf = sum(confidences) / len(confidences)
        if avg_conf < min_confidence:
            continue
        proposals.append(
            {
                "condition": condition,
                "route_to": _infer_agent(condition),
                "confidence": round(avg_conf, 3),
                "count": len(group),
                "sample_event": group[-1],
                "synthetic": False,
            }
        )

    if not proposals:
        # Synthetic fallback: most recent observation of any type in the log
        synthetic = _make_synthetic_proposal(log_path)
        if synthetic:
            proposals.append(synthetic)

    # Rank by count * confidence descending
    proposals.sort(key=lambda p: p["count"] * p["confidence"], reverse=True)
    return proposals


def _make_synthetic_proposal(log_path: Path | None) -> dict[str, Any] | None:
    if not log_path or not log_path.exists():
        return None

    last_event: dict[str, Any] | None = None
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                last_event = ev
            except json.JSONDecodeError:
                continue

    if not last_event:
        return None

    text = _event_text(last_event)
    if not text:
        return None

    condition = text[:80]
    return {
        "condition": condition,
        "route_to": _infer_agent(condition),
        "confidence": 0.5,
        "count": 1,
        "sample_event": last_event,
        "synthetic": True,
    }


def format_proposal_message(proposals: list[dict[str, Any]]) -> str:
    """Format proposals as Telegram messages (one block per proposal).

    Returns empty string if no proposals.
    """
    if not proposals:
        return ""

    lines: list[str] = []
    for p in proposals:
        synthetic_note = " *(synthetic — no qualifying patterns yet)*" if p.get("synthetic") else ""
        lines.append(
            f"Routing suggestion{synthetic_note}: "
            f"Route messages matching '{p['condition']}' to `{p['route_to']}`. "
            f"Confirm? (seen {p['count']}x, confidence {p['confidence']:.0%})"
        )

    return "\n\n".join(lines)
