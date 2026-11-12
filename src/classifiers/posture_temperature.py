"""
src/classifiers/posture_temperature.py — Postural proportion reader.

Reads recent classification_tags from memory.db and computes the current
"temperature" across the five behavioral postures. Used by the dispatcher
before formulating a response.

The five postures and their signal mappings
-------------------------------------------
pattern_perception        ← posture_hint='pattern_perception'  OR signal_a=1
structural_coherence      ← posture_hint='structural_coherence' OR signal_b=1
attunement                ← posture_hint='attunement'           OR signal_c=1
elegant_economy           ← posture_hint='elegant_economy'      OR signal_d=1
minimal_cognitive_friction← posture_hint='minimal_cognitive_friction' OR signal_e=1

Both the posture_hint column and the relevant signal flag contribute one count
per tag. A single tag can contribute to at most two postures (one from hint,
one from signal), which is intentional: the classifiers chose the hint AND
detected a specific signal independently.

Output shape
------------
{
    "pattern_perception": 35,          # % of total votes
    "structural_coherence": 20,
    "attunement": 15,
    "elegant_economy": 15,
    "minimal_cognitive_friction": 15,
    "dominant": "pattern_perception",  # posture with highest share
    "temperature": "high",             # "high" (>50%), "medium" (30-50%), "low" (<30%)
    "window_hours": 2,
    "tag_count": 8,                    # number of tags read
    "total_votes": 11,                 # sum of all posture-weighted votes
}

Usage
-----
    # As a library:
    from src.classifiers.posture_temperature import get_posture_temperature
    result = get_posture_temperature(db_path, window_hours=2)

    # As a CLI tool:
    uv run src/classifiers/posture_temperature.py --current
    uv run src/classifiers/posture_temperature.py --current --window 4

See Also
--------
- src/classifiers/quick_classifier.py  (writes classification_tags)
- .claude/sys.dispatcher.bootup.md     (consumes this reading at message receipt)
- GitHub: dcetlin/Lobster#39
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOBSTER_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
MEMORY_DB_PATH = LOBSTER_WORKSPACE / "data" / "memory.db"

POSTURES = [
    "pattern_perception",
    "structural_coherence",
    "attunement",
    "elegant_economy",
    "minimal_cognitive_friction",
]

# Each posture maps to: (posture_hint_value, signal_column)
# A tag contributes a vote for the posture if EITHER the hint matches OR the signal is set.
POSTURE_SIGNAL_MAP: dict[str, tuple[str, str]] = {
    "pattern_perception":         ("pattern_perception",         "signal_a"),
    "structural_coherence":       ("structural_coherence",        "signal_b"),
    "attunement":                 ("attunement",                  "signal_c"),
    "elegant_economy":            ("elegant_economy",             "signal_d"),
    "minimal_cognitive_friction": ("minimal_cognitive_friction",  "signal_e"),
}

HIGH_THRESHOLD = 50    # dominant posture % above which temperature is "high"
MEDIUM_THRESHOLD = 30  # dominant posture % above which temperature is "medium"


# ---------------------------------------------------------------------------
# Core computation — pure functions
# ---------------------------------------------------------------------------

def _read_recent_tags(conn: sqlite3.Connection, window_hours: float) -> list[dict]:
    """
    Return classification_tags rows from the last `window_hours` hours.
    Returns an empty list if the table is empty or doesn't exist.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    try:
        cursor = conn.execute(
            """
            SELECT posture_hint, signal_a, signal_b, signal_c, signal_d, signal_e
            FROM classification_tags
            WHERE classified_at >= ?
            ORDER BY classified_at DESC
            """,
            (cutoff,),
        )
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        return []


def _count_votes(tags: list[dict]) -> dict[str, int]:
    """
    Count posture votes across tags.

    For each tag, a posture earns a vote if:
    - The tag's `posture_hint` equals that posture's hint value, OR
    - The tag's corresponding signal column is 1 (truthy).

    Returns a dict mapping each posture name to its raw vote count.
    """
    counts: dict[str, int] = {p: 0 for p in POSTURES}
    for tag in tags:
        for posture, (hint_value, signal_col) in POSTURE_SIGNAL_MAP.items():
            voted_via_hint = tag.get("posture_hint") == hint_value
            voted_via_signal = bool(tag.get(signal_col))
            if voted_via_hint or voted_via_signal:
                counts[posture] += 1
    return counts


def _normalize(counts: dict[str, int]) -> dict[str, int]:
    """
    Normalize raw vote counts to integer percentages that sum to 100.
    Returns equal distribution (20% each) when total is zero.
    """
    total = sum(counts.values())
    if total == 0:
        return {p: 20 for p in POSTURES}

    # Compute floor percentages and track remainders for largest-remainder rounding.
    raw = {p: (counts[p] / total) * 100 for p in POSTURES}
    floors = {p: int(raw[p]) for p in POSTURES}
    floor_sum = sum(floors.values())
    remainder = 100 - floor_sum

    # Distribute remainder points to postures with largest fractional parts.
    sorted_by_remainder = sorted(POSTURES, key=lambda p: raw[p] - floors[p], reverse=True)
    for i, posture in enumerate(sorted_by_remainder):
        if i < remainder:
            floors[posture] += 1

    return floors


def _classify_temperature(dominant_pct: int) -> str:
    if dominant_pct >= HIGH_THRESHOLD:
        return "high"
    if dominant_pct >= MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def compute_temperature(tags: list[dict], window_hours: float) -> dict:
    """
    Pure function: given a list of tag dicts, return the posture temperature reading.
    """
    counts = _count_votes(tags)
    total_votes = sum(counts.values())
    percentages = _normalize(counts)

    dominant = max(POSTURES, key=lambda p: percentages[p])
    temperature = _classify_temperature(percentages[dominant])

    return {
        **percentages,
        "dominant": dominant,
        "temperature": temperature,
        "window_hours": window_hours,
        "tag_count": len(tags),
        "total_votes": total_votes,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_posture_temperature(
    db_path: Path = MEMORY_DB_PATH,
    window_hours: float = 2.0,
) -> dict:
    """
    Read recent classification_tags and return the current posture temperature.

    Returns a dict with posture percentages, dominant posture, temperature level,
    and metadata. Safe to call even when memory.db does not exist or has no rows
    — returns an equal-distribution reading in that case.

    Parameters
    ----------
    db_path:
        Path to memory.db. Defaults to the standard Lobster workspace location.
    window_hours:
        How many hours back to look for tags. Defaults to 2.
    """
    if not Path(db_path).exists():
        return compute_temperature([], window_hours)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tags = _read_recent_tags(conn, window_hours)
        return compute_temperature(tags, window_hours)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read current postural temperature from classification_tags."
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="Print the current posture temperature reading as JSON.",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=2.0,
        metavar="HOURS",
        help="How many hours back to look for tags (default: 2).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=MEMORY_DB_PATH,
        metavar="PATH",
        help="Path to memory.db (default: $LOBSTER_WORKSPACE/data/memory.db).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.current:
        result = get_posture_temperature(db_path=args.db, window_hours=args.window)
        print(json.dumps(result, indent=2))
    else:
        print("Use --current to print the posture temperature reading.")
        print("Example: uv run src/classifiers/posture_temperature.py --current")
