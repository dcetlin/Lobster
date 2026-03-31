"""
Integration tests for migration 0004 — source tracking fields.

Verifies:
- Migration 0004 runs without error (covered implicitly by the `db` fixture,
  which applies all migrations).
- The three new columns exist in uow_registry after migration.
- Registry.update_source_tracking() writes all three fields and they read back
  correctly via Registry.get().
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.migrate import run_migrations
from orchestration.registry import Registry, UpsertInserted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_uow(registry: Registry, issue_number: int = 1, title: str = "test uow") -> str:
    """Insert a proposed UoW and return its id."""
    result = registry.upsert(issue_number=issue_number, title=title)
    assert isinstance(result, UpsertInserted), f"Expected UpsertInserted, got: {result}"
    return result.id


# ---------------------------------------------------------------------------
# Schema presence tests
# ---------------------------------------------------------------------------


class TestMigration0004Schema:
    """Migration 0004 creates the expected columns and index."""

    def test_source_ref_column_exists(self, db_conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in db_conn.execute("PRAGMA table_info(uow_registry)")}
        assert "source_ref" in cols, "source_ref column missing from uow_registry"

    def test_source_last_seen_at_column_exists(self, db_conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in db_conn.execute("PRAGMA table_info(uow_registry)")}
        assert "source_last_seen_at" in cols, "source_last_seen_at column missing from uow_registry"

    def test_source_state_column_exists(self, db_conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in db_conn.execute("PRAGMA table_info(uow_registry)")}
        assert "source_state" in cols, "source_state column missing from uow_registry"

    def test_source_ref_index_exists(self, db_conn: sqlite3.Connection) -> None:
        indexes = {
            row["name"]
            for row in db_conn.execute("PRAGMA index_list(uow_registry)")
        }
        assert "idx_uow_source_ref" in indexes, "idx_uow_source_ref index missing"

    def test_new_rows_default_to_null(self, db: Path) -> None:
        """Existing insert paths leave source tracking columns NULL by default."""
        registry = Registry(db)
        uow_id = _seed_uow(registry)
        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.source_ref is None
        assert uow.source_last_seen_at is None
        assert uow.source_state is None


# ---------------------------------------------------------------------------
# update_source_tracking tests
# ---------------------------------------------------------------------------


class TestUpdateSourceTracking:
    """Registry.update_source_tracking() writes and reads back correctly."""

    def test_round_trip(self, db: Path) -> None:
        """All three fields survive a write → read cycle."""
        registry = Registry(db)
        uow_id = _seed_uow(registry)
        seen_at = _now_iso()

        registry.update_source_tracking(
            uow_id=uow_id,
            source_ref="github:issue/42",
            source_last_seen_at=seen_at,
            source_state="open",
        )

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.source_ref == "github:issue/42"
        assert uow.source_last_seen_at == seen_at
        assert uow.source_state == "open"

    def test_overwrite(self, db: Path) -> None:
        """Calling update_source_tracking twice overwrites previous values."""
        registry = Registry(db)
        uow_id = _seed_uow(registry)
        first_seen_at = _now_iso()

        registry.update_source_tracking(
            uow_id=uow_id,
            source_ref="github:issue/42",
            source_last_seen_at=first_seen_at,
            source_state="open",
        )

        second_seen_at = _now_iso()
        registry.update_source_tracking(
            uow_id=uow_id,
            source_ref="github:issue/42",
            source_last_seen_at=second_seen_at,
            source_state="closed",
        )

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.source_state == "closed"
        assert uow.source_last_seen_at == second_seen_at

    def test_source_ref_values(self, db: Path) -> None:
        """source_ref stores the canonical SourceRef string exactly."""
        registry = Registry(db)
        uow_id = _seed_uow(registry)

        for ref in ("github:issue/1", "github:issue/999", "gitlab:issue/7"):
            registry.update_source_tracking(
                uow_id=uow_id,
                source_ref=ref,
                source_last_seen_at=_now_iso(),
                source_state="open",
            )
            uow = registry.get(uow_id)
            assert uow is not None
            assert uow.source_ref == ref

    def test_closed_state(self, db: Path) -> None:
        """source_state accepts 'closed' (the common tend() transition value)."""
        registry = Registry(db)
        uow_id = _seed_uow(registry)
        registry.update_source_tracking(
            uow_id=uow_id,
            source_ref="github:issue/10",
            source_last_seen_at=_now_iso(),
            source_state="closed",
        )
        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.source_state == "closed"

    def test_deleted_state(self, db: Path) -> None:
        """source_state accepts 'deleted' for not-found source issues."""
        registry = Registry(db)
        uow_id = _seed_uow(registry)
        registry.update_source_tracking(
            uow_id=uow_id,
            source_ref="github:issue/11",
            source_last_seen_at=_now_iso(),
            source_state="deleted",
        )
        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.source_state == "deleted"

    def test_multiple_uows_isolated(self, db: Path) -> None:
        """update_source_tracking on one UoW does not affect another."""
        registry = Registry(db)
        uow_a = _seed_uow(registry, issue_number=10, title="UoW A")
        uow_b = _seed_uow(registry, issue_number=11, title="UoW B")

        registry.update_source_tracking(
            uow_id=uow_a,
            source_ref="github:issue/10",
            source_last_seen_at=_now_iso(),
            source_state="open",
        )

        uow_b_data = registry.get(uow_b)
        assert uow_b_data is not None
        assert uow_b_data.source_ref is None
        assert uow_b_data.source_last_seen_at is None
        assert uow_b_data.source_state is None

    def test_nonexistent_uow_raises_value_error(self, db: Path) -> None:
        """update_source_tracking raises ValueError when uow_id does not exist."""
        registry = Registry(db)
        with pytest.raises(ValueError, match="uow_id not found: nonexistent-id"):
            registry.update_source_tracking(
                uow_id="nonexistent-id",
                source_ref="github:issue/1",
                source_last_seen_at=_now_iso(),
                source_state="open",
            )
