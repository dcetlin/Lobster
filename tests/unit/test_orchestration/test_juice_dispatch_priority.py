"""
Tests for the juice dispatch priority signal in the Registry and steward cycle.

Verifies:
1. registry.list(status='ready-for-steward') returns juice UoWs first.
2. registry.write_juice() writes juice_quality and juice_rationale correctly.
3. registry.write_juice() raises when juice_quality='juice' but rationale is empty.
4. Migration 0013 adds juice_quality and juice_rationale columns.
5. UoW dataclass carries juice_quality and juice_rationale fields.
"""

from __future__ import annotations

import sys
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.registry import Registry, UoW, UoWStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(tmp_path: Path) -> Registry:
    """Create a Registry backed by a temp DB path with all migrations applied."""
    db = tmp_path / "registry.db"
    return Registry(db_path=db)


def _insert_ready_for_steward(
    registry: Registry,
    issue_number: int,
    juice_quality: str | None = None,
    juice_rationale: str | None = None,
) -> str:
    """Insert a UoW and advance it to ready-for-steward. Returns the uow_id."""
    result = registry.upsert(
        issue_number=issue_number,
        title=f"Test UoW {issue_number}",
        success_criteria=f"Issue {issue_number} resolved",
    )
    registry.approve(result.id)

    # Optionally write juice fields directly so we can test ordering.
    if juice_quality is not None:
        registry.write_juice(result.id, juice_quality, juice_rationale)

    return result.id


# ---------------------------------------------------------------------------
# Tests: juice dispatch priority ordering
# ---------------------------------------------------------------------------

class TestJuiceDispatchOrdering:
    """Juice UoWs sort first in registry.list(status='ready-for-steward').

    Spec: ORDER BY CASE WHEN juice_quality='juice' THEN 0 ELSE 1 END ASC, created_at DESC
    """

    def test_juice_uow_sorts_before_non_juice(self, tmp_path: Path):
        """A juiced UoW must appear first in ready-for-steward results."""
        registry = _make_registry(tmp_path)

        non_juice_id = _insert_ready_for_steward(registry, issue_number=1001)
        juice_id = _insert_ready_for_steward(
            registry,
            issue_number=1002,
            juice_quality="juice",
            juice_rationale="oracle approval rate 100% over 3 cycles",
        )

        uows = registry.list(status="ready-for-steward")
        ids = [u.id for u in uows]

        assert juice_id in ids
        assert non_juice_id in ids
        assert ids.index(juice_id) < ids.index(non_juice_id), (
            "Juice UoW must sort before non-juice UoW in ready-for-steward results"
        )

    def test_multiple_juice_uows_sort_before_non_juice(self, tmp_path: Path):
        """All juiced UoWs must appear before all non-juiced UoWs."""
        registry = _make_registry(tmp_path)

        non_juice_a = _insert_ready_for_steward(registry, issue_number=2001)
        non_juice_b = _insert_ready_for_steward(registry, issue_number=2002)
        juice_a = _insert_ready_for_steward(
            registry, issue_number=2003,
            juice_quality="juice",
            juice_rationale="live thread: oracle rate 80%; 2 completed prereqs",
        )
        juice_b = _insert_ready_for_steward(
            registry, issue_number=2004,
            juice_quality="juice",
            juice_rationale="live thread: recent oracle approval",
        )

        uows = registry.list(status="ready-for-steward")
        ids = [u.id for u in uows]

        juice_positions = [ids.index(juice_a), ids.index(juice_b)]
        non_juice_positions = [ids.index(non_juice_a), ids.index(non_juice_b)]

        assert max(juice_positions) < min(non_juice_positions), (
            "All juice UoWs must appear before all non-juice UoWs"
        )

    def test_no_juice_uows_preserves_lifo_ordering(self, tmp_path: Path):
        """Without juice UoWs, ordering falls back to created_at DESC (LIFO)."""
        registry = _make_registry(tmp_path)

        id_older = _insert_ready_for_steward(registry, issue_number=3001)
        id_newer = _insert_ready_for_steward(registry, issue_number=3002)

        uows = registry.list(status="ready-for-steward")
        ids = [u.id for u in uows]

        # Newer UoW (higher created_at) should appear first in LIFO.
        assert ids.index(id_newer) < ids.index(id_older), (
            "Without juice, newer UoWs must appear before older UoWs (LIFO)"
        )

    def test_juice_ordering_does_not_affect_other_statuses(self, tmp_path: Path):
        """list(status='active') must NOT be affected by juice ordering changes."""
        registry = _make_registry(tmp_path)

        result = registry.upsert(
            issue_number=4001,
            title="Active UoW",
            success_criteria="test",
        )
        registry.approve(result.id)
        registry.transition(result.id, "active", "ready-for-steward")

        # Should not raise and should return results
        uows = registry.list(status="active")
        assert any(u.id == result.id for u in uows)


# ---------------------------------------------------------------------------
# Tests: write_juice
# ---------------------------------------------------------------------------

class TestWriteJuice:
    """registry.write_juice() writes juice fields and enforces rationale requirement."""

    def test_write_juice_stores_quality_and_rationale(self, tmp_path: Path):
        """write_juice('juice', rationale) writes both fields to the DB."""
        registry = _make_registry(tmp_path)
        uow_id = _insert_ready_for_steward(registry, issue_number=5001)

        rationale = "live thread: oracle approval rate 100% over 3 cycles"
        registry.write_juice(uow_id, "juice", rationale)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.juice_quality == "juice"
        assert uow.juice_rationale == rationale

    def test_write_juice_clears_juice_when_none(self, tmp_path: Path):
        """write_juice(None, None) clears both fields."""
        registry = _make_registry(tmp_path)
        uow_id = _insert_ready_for_steward(registry, issue_number=5002)

        registry.write_juice(uow_id, "juice", "initial rationale")
        registry.write_juice(uow_id, None, None)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.juice_quality is None
        assert uow.juice_rationale is None

    def test_write_juice_raises_when_juice_quality_without_rationale(self, tmp_path: Path):
        """Spec: juice_rationale is mandatory when juice_quality='juice'."""
        registry = _make_registry(tmp_path)
        uow_id = _insert_ready_for_steward(registry, issue_number=5003)

        with pytest.raises(ValueError, match="juice_rationale is required"):
            registry.write_juice(uow_id, "juice", None)

    def test_write_juice_raises_when_juice_quality_with_empty_rationale(self, tmp_path: Path):
        """Empty string rationale is also rejected when juice_quality='juice'."""
        registry = _make_registry(tmp_path)
        uow_id = _insert_ready_for_steward(registry, issue_number=5004)

        with pytest.raises(ValueError, match="juice_rationale is required"):
            registry.write_juice(uow_id, "juice", "")

    def test_write_juice_none_quality_with_none_rationale_is_valid(self, tmp_path: Path):
        """write_juice(None, None) is the valid clear operation."""
        registry = _make_registry(tmp_path)
        uow_id = _insert_ready_for_steward(registry, issue_number=5005)

        # Should not raise
        registry.write_juice(uow_id, None, None)


# ---------------------------------------------------------------------------
# Tests: UoW dataclass carries juice fields
# ---------------------------------------------------------------------------

class TestUoWJuiceFields:
    """UoW dataclass exposes juice_quality and juice_rationale."""

    def test_uow_has_juice_quality_field_defaulting_to_none(self):
        """New UoW objects default juice_quality to None."""
        uow = UoW(
            id="uow_test_juice_001",
            status=UoWStatus.READY_FOR_STEWARD,
            summary="Test",
            source="test",
            source_issue_number=1,
            created_at="2026-04-24T00:00:00+00:00",
            updated_at="2026-04-24T00:00:00+00:00",
        )
        assert uow.juice_quality is None

    def test_uow_has_juice_rationale_field_defaulting_to_none(self):
        """New UoW objects default juice_rationale to None."""
        uow = UoW(
            id="uow_test_juice_002",
            status=UoWStatus.READY_FOR_STEWARD,
            summary="Test",
            source="test",
            source_issue_number=2,
            created_at="2026-04-24T00:00:00+00:00",
            updated_at="2026-04-24T00:00:00+00:00",
        )
        assert uow.juice_rationale is None

    def test_uow_juice_fields_roundtrip_through_registry(self, tmp_path: Path):
        """juice_quality and juice_rationale survive a DB write-read roundtrip."""
        registry = _make_registry(tmp_path)
        uow_id = _insert_ready_for_steward(registry, issue_number=6001)

        rationale = "live thread: oracle rate 75%; recent approval"
        registry.write_juice(uow_id, "juice", rationale)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.juice_quality == "juice"
        assert uow.juice_rationale == rationale


# ---------------------------------------------------------------------------
# Tests: migration 0013 columns
# ---------------------------------------------------------------------------

class TestMigration0013:
    """Migration 0013 adds juice_quality and juice_rationale columns."""

    def test_juice_quality_column_exists_after_migration(self, tmp_path: Path):
        """juice_quality column must be present in uow_registry after migrations."""
        registry = _make_registry(tmp_path)
        conn = registry._connect()
        try:
            rows = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
            columns = {row[1] for row in rows}
            assert "juice_quality" in columns, (
                "Migration 0013 must add juice_quality column to uow_registry"
            )
        finally:
            conn.close()

    def test_juice_rationale_column_exists_after_migration(self, tmp_path: Path):
        """juice_rationale column must be present in uow_registry after migrations."""
        registry = _make_registry(tmp_path)
        conn = registry._connect()
        try:
            rows = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
            columns = {row[1] for row in rows}
            assert "juice_rationale" in columns, (
                "Migration 0013 must add juice_rationale column to uow_registry"
            )
        finally:
            conn.close()

    def test_juice_quality_defaults_to_null_for_existing_rows(self, tmp_path: Path):
        """Existing UoWs must get juice_quality=NULL by default."""
        registry = _make_registry(tmp_path)
        uow_id = _insert_ready_for_steward(registry, issue_number=7001)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.juice_quality is None

    def test_juice_rationale_defaults_to_null_for_existing_rows(self, tmp_path: Path):
        """Existing UoWs must get juice_rationale=NULL by default."""
        registry = _make_registry(tmp_path)
        uow_id = _insert_ready_for_steward(registry, issue_number=7002)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.juice_rationale is None
