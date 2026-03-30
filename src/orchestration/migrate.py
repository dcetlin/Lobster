"""
DB migration runner for the WOS orchestration layer.

Design:
- Migrations live in src/orchestration/migrations/ as numbered .sql files:
    0001_initial_schema.sql, 0002_add_notes_column.sql, ...
- The _migrations table tracks which versions have been applied.
- run_migrations(db_path) is idempotent: already-applied versions are skipped.
- Any SQL error raises immediately — no silent swallowing.
- Uses busy_timeout=5000 and WAL mode, consistent with Registry._connect().

Entry point:
    uv run src/orchestration/migrate.py [DB_PATH]

    DB_PATH defaults to $REGISTRY_DB_PATH if not supplied as an argument.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS _migrations (
    id          INTEGER PRIMARY KEY,
    version     INTEGER UNIQUE NOT NULL,
    applied_at  TEXT    NOT NULL,
    filename    TEXT    NOT NULL
);
"""

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """
    Return all migration files in ascending version order.

    Each file must match the pattern NNNN_description.sql.
    Files that do not match are silently ignored (e.g. README, __init__).

    Returns: sorted list of (version, path) tuples.
    """
    results: list[tuple[int, Path]] = []
    for path in migrations_dir.iterdir():
        m = _MIGRATION_FILENAME_RE.match(path.name)
        if m:
            version = int(m.group(1))
            results.append((version, path))
    results.sort(key=lambda pair: pair[0])
    return results


def _applied_versions(conn: sqlite3.Connection) -> frozenset[int]:
    """Return the set of migration versions already recorded in _migrations."""
    rows = conn.execute("SELECT version FROM _migrations").fetchall()
    return frozenset(row["version"] for row in rows)


def _record_migration(
    conn: sqlite3.Connection,
    version: int,
    filename: str,
    applied_at: str,
) -> None:
    conn.execute(
        "INSERT INTO _migrations (version, applied_at, filename) VALUES (?, ?, ?)",
        (version, applied_at, filename),
    )


def run_migrations(db_path: Path) -> list[int]:
    """
    Apply all unapplied migrations in order.

    Creates the database and its parent directory if they do not exist.
    Creates the _migrations table if it does not exist.
    Skips any migration whose version is already recorded.
    Each migration is applied and recorded in its own transaction.
    Raises sqlite3.OperationalError (or any subclass) on SQL failure.

    Returns: list of version numbers that were applied (empty if nothing to do).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    migrations = _discover_migrations(_MIGRATIONS_DIR)
    if not migrations:
        return []

    conn = _connect(db_path)
    try:
        conn.execute(_CREATE_MIGRATIONS_TABLE)
        conn.commit()

        applied = _applied_versions(conn)
        newly_applied: list[int] = []

        for version, path in migrations:
            if version in applied:
                continue

            sql = path.read_text()
            now = datetime.now(timezone.utc).isoformat()

            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executescript(sql)
                # executescript issues an implicit COMMIT, so we open a new
                # transaction to atomically record the migration version.
                conn.execute("BEGIN IMMEDIATE")
                _record_migration(conn, version, path.name, now)
                conn.commit()
            except Exception:
                # executescript commits implicitly, so a partial DDL run may
                # have succeeded. We do not attempt a rollback of DDL (SQLite
                # does not support transactional DDL for schema changes like
                # ALTER TABLE). The version is NOT recorded, so the next run
                # will retry. The caller is responsible for diagnosing.
                conn.rollback()
                raise

            newly_applied.append(version)

        return newly_applied
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) > 1:
        db_path = Path(sys.argv[1])
    else:
        import os
        env_path = os.environ.get("REGISTRY_DB_PATH")
        if not env_path:
            print(
                "Usage: uv run src/orchestration/migrate.py <db_path>\n"
                "       or set REGISTRY_DB_PATH environment variable",
                file=sys.stderr,
            )
            sys.exit(1)
        db_path = Path(env_path)

    try:
        applied = run_migrations(db_path)
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if applied:
        print(f"Applied {len(applied)} migration(s): {applied}")
    else:
        print("No migrations needed.")


if __name__ == "__main__":
    main()
