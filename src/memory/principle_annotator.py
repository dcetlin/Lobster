#!/usr/bin/env python3
"""
src/memory/principle_annotator.py — Principle annotation utilities for memory.db events

A principle annotation records that a dispatcher decision diverged from the smooth default
because a specific epistemic principle was actively constraining the output. Over time, these
annotations build an empirical record of which principles are load-bearing (produce outputs
that would not have occurred under the smooth default) vs. ornamental (stated but not operative).

This is the structural trace described in dcetlin/Lobster#29: not self-report ("I applied
principle P"), but a verifiable record of what path was taken vs. what would have been taken.

Annotation JSON schema (stored in principle_annotation column of events table):
    {
        "principle": str,          # snake_case principle name, e.g. "attunement_over_assumption"
        "divergence": str,         # one sentence: what smooth default was resisted
        "confidence": str          # "high" | "medium" | "low"
    }

Public API:
    annotate_event(db_path, event_id, principle, divergence, confidence='medium') -> None
    get_annotated_events(db_path, principle=None, limit=50) -> list[dict]

CLI:
    uv run src/memory/principle_annotator.py --summary
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

DEFAULT_DB = Path.home() / "lobster-workspace" / "data" / "memory.db"

# Valid confidence values — kept as a frozenset so membership tests are O(1)
VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _build_annotation(principle: str, divergence: str, confidence: str) -> str:
    """Serialize an annotation to a JSON string. Pure function."""
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(
            f"confidence must be one of {sorted(VALID_CONFIDENCE)}, got {confidence!r}"
        )
    return json.dumps(
        {"principle": principle, "divergence": divergence, "confidence": confidence},
        ensure_ascii=False,
    )


def _parse_annotation(raw: str | None) -> dict[str, Any] | None:
    """Deserialize an annotation JSON string. Returns None on failure. Pure function."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a read-write connection with row_factory set. Pure factory."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _thirty_days_ago() -> str:
    """Return ISO-8601 timestamp for 30 days ago in UTC. Pure function."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def annotate_event(
    db_path: str | Path,
    event_id: int,
    principle: str,
    divergence: str,
    confidence: str = "medium",
) -> None:
    """
    Write a principle annotation to an existing event row.

    Args:
        db_path:    Path to memory.db.
        event_id:   Integer primary key of the target events row.
        principle:  Snake-case principle name, e.g. "attunement_over_assumption".
        divergence: One sentence describing what smooth default was resisted.
        confidence: "high", "medium", or "low" (default "medium").

    Raises:
        ValueError: If confidence is not one of the valid values.
        sqlite3.OperationalError: If the principle_annotation column does not exist
                                   (run migrate_principle_annotations.py first).
    """
    annotation = _build_annotation(principle, divergence, confidence)
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "UPDATE events SET principle_annotation = ? WHERE id = ?",
            (annotation, event_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_annotated_events(
    db_path: str | Path,
    principle: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Return recent annotated events, optionally filtered by principle name.

    Each returned dict has keys: id, timestamp, type, source, content,
    principle_annotation (parsed as a dict).

    Args:
        db_path:   Path to memory.db.
        principle: If provided, only return events annotated with this principle.
        limit:     Maximum number of events to return (default 50).

    Returns:
        List of dicts ordered by timestamp descending.
    """
    conn = _get_connection(db_path)
    try:
        if principle is not None:
            # JSON extract via json_extract — available in SQLite >= 3.38.0,
            # fallback to LIKE for older versions.
            try:
                rows = conn.execute(
                    """
                    SELECT id, timestamp, type, source, content, principle_annotation
                    FROM events
                    WHERE principle_annotation IS NOT NULL
                      AND json_extract(principle_annotation, '$.principle') = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (principle, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # Older SQLite without json_extract — fall back to LIKE
                rows = conn.execute(
                    """
                    SELECT id, timestamp, type, source, content, principle_annotation
                    FROM events
                    WHERE principle_annotation LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (f'%"principle": "{principle}"%', limit),
                ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, timestamp, type, source, content, principle_annotation
                FROM events
                WHERE principle_annotation IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "type": row["type"],
                "source": row["source"],
                "content": row["content"],
                "principle_annotation": _parse_annotation(row["principle_annotation"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def get_annotation_summary(
    db_path: str | Path,
    days: int = 30,
) -> dict[str, int]:
    """
    Return a count of annotations per principle over the last *days* days.

    This is the "load-bearing vs. ornamental" readout: principles that appear
    frequently are actively constraining outputs; principles that never appear
    may be ornamental.

    Args:
        db_path: Path to memory.db.
        days:    Lookback window in days (default 30).

    Returns:
        Dict mapping principle name → annotation count, sorted by count descending.
    """
    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%S")

    conn = _get_connection(db_path)
    try:
        # Try json_extract first; fall back to full scan with Python parsing
        try:
            rows = conn.execute(
                """
                SELECT json_extract(principle_annotation, '$.principle') AS principle,
                       COUNT(*) AS cnt
                FROM events
                WHERE principle_annotation IS NOT NULL
                  AND timestamp >= ?
                GROUP BY principle
                ORDER BY cnt DESC
                """,
                (cutoff,),
            ).fetchall()
            return {row["principle"]: row["cnt"] for row in rows if row["principle"]}
        except sqlite3.OperationalError:
            # Older SQLite — parse in Python
            rows = conn.execute(
                """
                SELECT principle_annotation
                FROM events
                WHERE principle_annotation IS NOT NULL
                  AND timestamp >= ?
                """,
                (cutoff,),
            ).fetchall()
            counts: dict[str, int] = {}
            for row in rows:
                parsed = _parse_annotation(row["principle_annotation"])
                if parsed and "principle" in parsed:
                    p = parsed["principle"]
                    counts[p] = counts.get(p, 0) + 1
            return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Principle annotation utilities for memory.db"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to memory.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print annotation counts per principle over the last 30 days",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback window for --summary (default: 30)",
    )
    parser.add_argument(
        "--principle",
        type=str,
        default=None,
        help="Filter --summary or list output to a specific principle",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List recent annotated events (up to --limit)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max events to show with --list (default: 20)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 1

    if args.summary:
        counts = get_annotation_summary(args.db, days=args.days)
        if not counts:
            print(f"No principle annotations in the last {args.days} days.")
            return 0
        print(f"Principle annotations — last {args.days} days")
        print("-" * 50)
        for principle, count in counts.items():
            marker = " (load-bearing)" if count >= 3 else ""
            print(f"  {principle:<40} {count:>4}{marker}")
        print("-" * 50)
        print(f"  Total annotations: {sum(counts.values())}")
        return 0

    if args.list:
        events = get_annotated_events(args.db, principle=args.principle, limit=args.limit)
        if not events:
            msg = "No annotated events found"
            if args.principle:
                msg += f" for principle '{args.principle}'"
            print(msg)
            return 0
        for ev in events:
            ann = ev["principle_annotation"] or {}
            print(
                f"[{ev['timestamp']}] id={ev['id']} "
                f"principle={ann.get('principle', '?')} "
                f"confidence={ann.get('confidence', '?')}"
            )
            print(f"  divergence: {ann.get('divergence', '?')}")
        return 0

    # Default: print usage hint
    print("Use --summary to see annotation counts, --list to show recent annotated events.")
    print("Run with --help for full usage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
