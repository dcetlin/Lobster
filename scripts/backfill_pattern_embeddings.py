#!/usr/bin/env python3
"""
scripts/backfill_pattern_embeddings.py

One-time backfill: generate and store embeddings for pattern_observation events
that were written before the fix in write_pattern_event() was applied.

Root cause: slow_reclassifier.write_pattern_event() inserted directly into the
events table without also inserting into events_vec. This left all
pattern_observation events invisible to vector search.

Usage:
    uv run scripts/backfill_pattern_embeddings.py [--dry-run] [--db PATH]

Options:
    --dry-run   Report how many events need backfilling without writing.
    --db PATH   Override memory.db path (default: ~/lobster-workspace/data/memory.db).
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


def _serialize_vector(floats: list[float]) -> bytes:
    return struct.pack(f"{len(floats)}f", *floats)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill embeddings for pattern_observation events")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument(
        "--db",
        default=str(Path.home() / "lobster-workspace" / "data" / "memory.db"),
        help="Path to memory.db",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"memory.db not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    import sqlite3
    try:
        import sqlite_vec
    except ImportError:
        print("sqlite_vec not installed — cannot run backfill", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Find pattern_observation events missing a vec entry.
    rows = conn.execute("""
        SELECT e.id, e.content
        FROM events e
        LEFT JOIN events_vec v ON e.id = v.rowid
        WHERE e.type = 'pattern_observation'
          AND v.rowid IS NULL
        ORDER BY e.id
    """).fetchall()

    if not rows:
        print("No pattern_observation events missing embeddings — nothing to do.")
        conn.close()
        return

    print(f"Found {len(rows)} pattern_observation events missing embeddings.")
    if args.dry_run:
        print("--dry-run: no changes written.")
        conn.close()
        return

    # Load embedding model.
    print("Loading embedding model (all-MiniLM-L6-v2)...")
    from fastembed import TextEmbedding
    model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")

    # Batch embed.
    contents = [r["content"] for r in rows]
    ids = [r["id"] for r in rows]
    embeddings = list(model.embed(contents))
    print(f"Embedded {len(embeddings)} events. Writing to events_vec...")

    inserted = 0
    failed = 0
    for event_id, emb in zip(ids, embeddings):
        floats = emb.tolist() if hasattr(emb, "tolist") else list(emb)
        blob = _serialize_vector(floats)
        try:
            conn.execute(
                "INSERT INTO events_vec(rowid, embedding) VALUES (?, ?)",
                (event_id, blob),
            )
            inserted += 1
        except Exception as exc:
            print(f"  Failed for event {event_id}: {exc}", file=sys.stderr)
            failed += 1

    conn.commit()
    conn.close()
    print(f"Done: {inserted} embeddings written, {failed} failed.")


if __name__ == "__main__":
    main()
