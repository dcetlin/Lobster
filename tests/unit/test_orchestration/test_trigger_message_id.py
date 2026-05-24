"""
Unit tests for trigger_message_id column on uow_registry (issue #1108).

Behavior under test:

Migration:
- Migration 0022 adds trigger_message_id TEXT NULL column to uow_registry

Registry.upsert:
- trigger_message_id is stored in the INSERT when provided
- trigger_message_id is NULL when not provided (default behavior)
- All existing callers that omit trigger_message_id are unaffected

Registry.get_uow_trigger:
- Returns the stored trigger_message_id for a known UoW
- Returns None when trigger_message_id was not provided at creation
- Returns None for an unknown uow_id

Named constants:
- TRIGGER_MESSAGE_ID_COLUMN = 'trigger_message_id' — column name in uow_registry
- EXAMPLE_TRIGGER_MESSAGE_ID — representative inbox message_id string
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
# Named constants (spec §trigger_message_id)
# ---------------------------------------------------------------------------

TRIGGER_MESSAGE_ID_COLUMN = "trigger_message_id"

# Representative inbox message_id: "{unix_ts_ms}_{telegram_msg_id}"
EXAMPLE_TRIGGER_MESSAGE_ID = "1778365681821_8563"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(tmp_path: Path):
    """Create a fresh Registry backed by a temp DB with all migrations applied."""
    from orchestration.registry import Registry

    db_path = str(tmp_path / "test_registry.db")
    os.environ["REGISTRY_DB_PATH"] = db_path
    return Registry(db_path=db_path)


def _upsert_minimal(registry, issue_number: int, *, trigger_message_id: str | None = None):
    """Upsert a UoW with minimal required fields and optional trigger_message_id."""
    from orchestration.registry import UpsertInserted

    result = registry.upsert(
        issue_number=issue_number,
        title=f"Test issue #{issue_number}",
        success_criteria="Tests pass with zero failures",
        register="operational",
        trigger_message_id=trigger_message_id,
    )
    assert isinstance(result, UpsertInserted), f"Expected UpsertInserted, got {result}"
    return result.id


# ---------------------------------------------------------------------------
# Migration: trigger_message_id column schema
# ---------------------------------------------------------------------------

class TestTriggerMessageIdMigration:
    """Migration 0022 adds trigger_message_id TEXT NULL to uow_registry."""

    def test_column_exists_after_registry_init(self, tmp_path):
        """Registry() auto-applies migrations; trigger_message_id must be present."""
        registry = _make_registry(tmp_path)
        conn = registry._connect()
        try:
            cols = conn.execute(
                "PRAGMA table_info(uow_registry)"
            ).fetchall()
        finally:
            conn.close()
        col_names = {row["name"] for row in cols}
        assert TRIGGER_MESSAGE_ID_COLUMN in col_names

    def test_column_is_nullable(self, tmp_path):
        """trigger_message_id column accepts NULL (new UoWs without a Telegram origin)."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_minimal(registry, issue_number=1, trigger_message_id=None)
        conn = registry._connect()
        try:
            row = conn.execute(
                f"SELECT {TRIGGER_MESSAGE_ID_COLUMN} FROM uow_registry WHERE id = ?",
                (uow_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[TRIGGER_MESSAGE_ID_COLUMN] is None

    def test_column_accepts_text_value(self, tmp_path):
        """trigger_message_id column stores TEXT values without error."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_minimal(
            registry,
            issue_number=2,
            trigger_message_id=EXAMPLE_TRIGGER_MESSAGE_ID,
        )
        conn = registry._connect()
        try:
            row = conn.execute(
                f"SELECT {TRIGGER_MESSAGE_ID_COLUMN} FROM uow_registry WHERE id = ?",
                (uow_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[TRIGGER_MESSAGE_ID_COLUMN] == EXAMPLE_TRIGGER_MESSAGE_ID


# ---------------------------------------------------------------------------
# Registry.upsert — trigger_message_id storage
# ---------------------------------------------------------------------------

class TestUpsertStoresTriggerMessageId:
    """upsert() stores trigger_message_id in the INSERT."""

    def test_trigger_message_id_stored_when_provided(self, tmp_path):
        """When trigger_message_id is passed to upsert(), it is written to the DB."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_minimal(
            registry,
            issue_number=10,
            trigger_message_id=EXAMPLE_TRIGGER_MESSAGE_ID,
        )
        conn = registry._connect()
        try:
            row = conn.execute(
                f"SELECT {TRIGGER_MESSAGE_ID_COLUMN} FROM uow_registry WHERE id = ?",
                (uow_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[TRIGGER_MESSAGE_ID_COLUMN] == EXAMPLE_TRIGGER_MESSAGE_ID

    def test_trigger_message_id_null_when_omitted(self, tmp_path):
        """When trigger_message_id is omitted, NULL is stored (cultivator path)."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_minimal(registry, issue_number=11)
        conn = registry._connect()
        try:
            row = conn.execute(
                f"SELECT {TRIGGER_MESSAGE_ID_COLUMN} FROM uow_registry WHERE id = ?",
                (uow_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[TRIGGER_MESSAGE_ID_COLUMN] is None

    def test_trigger_message_id_null_when_explicitly_none(self, tmp_path):
        """When trigger_message_id=None is passed explicitly, NULL is stored."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_minimal(registry, issue_number=12, trigger_message_id=None)
        conn = registry._connect()
        try:
            row = conn.execute(
                f"SELECT {TRIGGER_MESSAGE_ID_COLUMN} FROM uow_registry WHERE id = ?",
                (uow_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[TRIGGER_MESSAGE_ID_COLUMN] is None

    def test_different_uows_have_independent_trigger_message_ids(self, tmp_path):
        """trigger_message_id is per-UoW; two UoWs can have different values."""
        registry = _make_registry(tmp_path)
        msg_id_a = "1778365681821_1001"
        msg_id_b = "1778365681821_1002"
        uow_id_a = _upsert_minimal(registry, issue_number=20, trigger_message_id=msg_id_a)
        uow_id_b = _upsert_minimal(registry, issue_number=21, trigger_message_id=msg_id_b)
        assert registry.get_uow_trigger(uow_id_a) == msg_id_a
        assert registry.get_uow_trigger(uow_id_b) == msg_id_b


# ---------------------------------------------------------------------------
# Registry.get_uow_trigger
# ---------------------------------------------------------------------------

class TestGetUowTrigger:
    """get_uow_trigger() returns trigger_message_id or None."""

    def test_returns_stored_trigger_message_id(self, tmp_path):
        """get_uow_trigger returns the trigger_message_id written by upsert."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_minimal(
            registry,
            issue_number=30,
            trigger_message_id=EXAMPLE_TRIGGER_MESSAGE_ID,
        )
        result = registry.get_uow_trigger(uow_id)
        assert result == EXAMPLE_TRIGGER_MESSAGE_ID

    def test_returns_none_when_trigger_not_set(self, tmp_path):
        """get_uow_trigger returns None for a UoW created without trigger_message_id."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_minimal(registry, issue_number=31)
        result = registry.get_uow_trigger(uow_id)
        assert result is None

    def test_returns_none_for_unknown_uow_id(self, tmp_path):
        """get_uow_trigger returns None for a uow_id that does not exist in the DB."""
        registry = _make_registry(tmp_path)
        result = registry.get_uow_trigger("uow_nonexistent_abc123")
        assert result is None

    def test_trigger_message_id_survives_status_transitions(self, tmp_path):
        """trigger_message_id is immutable through UoW lifecycle; status changes do not clear it."""
        registry = _make_registry(tmp_path)
        uow_id = _upsert_minimal(
            registry,
            issue_number=32,
            trigger_message_id=EXAMPLE_TRIGGER_MESSAGE_ID,
        )
        # Transition to done via set_status_direct (simulates lifecycle progression)
        registry.set_status_direct(uow_id, "done")
        result = registry.get_uow_trigger(uow_id)
        assert result == EXAMPLE_TRIGGER_MESSAGE_ID
