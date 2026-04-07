"""
Tests for executor-heartbeat.py recovery-mode dispatch (issue #664).

The heartbeat is a recovery poller after issue #664 — not the primary dispatch
path. The primary path is _dispatch_via_inbox (event-driven), which the Steward
triggers immediately when a UoW transitions to ready-for-executor.

Coverage:
- RECOVERY_STALE_MINUTES constant is a positive integer
- _filter_stale_uows returns only UoWs older than the threshold
- _filter_stale_uows skips recently-written UoWs (primary dispatch expected)
- _filter_stale_uows treats missing/unparseable/non-string updated_at as stale (safe default)
- run_executor_cycle skips UoWs younger than RECOVERY_STALE_MINUTES
- run_executor_cycle dispatches UoWs older than RECOVERY_STALE_MINUTES
- run_executor_cycle returns correct result dict keys (evaluated, ready, stale, dispatched, ...)
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest


# ---------------------------------------------------------------------------
# Load executor-heartbeat.py via importlib (hyphenated filename, no __init__)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_HEARTBEAT_PATH = _REPO_ROOT / "scheduled-tasks" / "executor-heartbeat.py"


def _load_heartbeat():
    """Load executor-heartbeat module from file path (hyphen in name)."""
    spec = importlib.util.spec_from_file_location("executor_heartbeat", _HEARTBEAT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["executor_heartbeat"] = module
    spec.loader.exec_module(module)
    return module


_hb = _load_heartbeat()
_filter_stale_uows = _hb._filter_stale_uows
RECOVERY_STALE_MINUTES = _hb.RECOVERY_STALE_MINUTES


# ---------------------------------------------------------------------------
# Helpers — minimal UoW stub with updated_at
# ---------------------------------------------------------------------------

@dataclass
class _StubUoW:
    id: str
    updated_at: str


def _uow_age(minutes: float) -> str:
    """Return an ISO 8601 timestamp that is `minutes` old."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return ts.isoformat()


# ---------------------------------------------------------------------------
# RECOVERY_STALE_MINUTES — sanity check
# ---------------------------------------------------------------------------

class TestRecoveryStaleMintuesConstant:
    def test_is_positive_integer(self) -> None:
        assert isinstance(RECOVERY_STALE_MINUTES, int)
        assert RECOVERY_STALE_MINUTES > 0

    def test_is_at_least_one_minute(self) -> None:
        """Must be long enough that a freshly-written UoW is not immediately eligible."""
        assert RECOVERY_STALE_MINUTES >= 1


# ---------------------------------------------------------------------------
# _filter_stale_uows — pure function tests
# ---------------------------------------------------------------------------

class TestFilterStaleUoWs:
    """_filter_stale_uows correctly splits recent vs stale UoWs."""

    def test_empty_list_returns_empty(self) -> None:
        result = _filter_stale_uows([], stale_minutes=5)
        assert result == []

    def test_old_uow_is_returned(self) -> None:
        """A UoW older than the threshold must be included in the stale list."""
        uow = _StubUoW(id="old-001", updated_at=_uow_age(minutes=10))
        result = _filter_stale_uows([uow], stale_minutes=5)
        assert len(result) == 1
        assert result[0].id == "old-001"

    def test_recent_uow_is_excluded(self) -> None:
        """A UoW younger than the threshold must be excluded (primary dispatch expected)."""
        uow = _StubUoW(id="new-001", updated_at=_uow_age(minutes=1))
        result = _filter_stale_uows([uow], stale_minutes=5)
        assert result == []

    def test_mixed_uows_returns_only_stale(self) -> None:
        """Only stale UoWs are returned; recent ones are skipped."""
        stale = _StubUoW(id="stale-001", updated_at=_uow_age(minutes=20))
        recent = _StubUoW(id="recent-001", updated_at=_uow_age(minutes=2))
        result = _filter_stale_uows([stale, recent], stale_minutes=5)
        assert len(result) == 1
        assert result[0].id == "stale-001"

    def test_multiple_stale_uows_all_returned(self) -> None:
        uows = [
            _StubUoW(id=f"stale-{i}", updated_at=_uow_age(minutes=10 + i))
            for i in range(3)
        ]
        result = _filter_stale_uows(uows, stale_minutes=5)
        assert len(result) == 3

    def test_unparseable_updated_at_treated_as_stale(self) -> None:
        """If updated_at cannot be parsed as ISO 8601, treat as stale (safe default)."""
        uow = _StubUoW(id="bad-ts-001", updated_at="not-a-timestamp")
        result = _filter_stale_uows([uow], stale_minutes=5)
        assert len(result) == 1
        assert result[0].id == "bad-ts-001"

    def test_non_string_updated_at_treated_as_stale(self) -> None:
        """If updated_at is not a string (e.g. Mock object), treat as stale (safe default)."""
        uow = MagicMock()
        uow.id = "mock-ts-001"
        uow.updated_at = MagicMock()  # Not a string — fromisoformat will raise TypeError
        result = _filter_stale_uows([uow], stale_minutes=5)
        assert len(result) == 1

    def test_preserves_order(self) -> None:
        """Stale UoWs are returned in the same order as the input list."""
        uows = [
            _StubUoW(id="stale-b", updated_at=_uow_age(minutes=15)),
            _StubUoW(id="stale-a", updated_at=_uow_age(minutes=20)),
        ]
        result = _filter_stale_uows(uows, stale_minutes=5)
        assert [u.id for u in result] == ["stale-b", "stale-a"]


# ---------------------------------------------------------------------------
# run_executor_cycle — recovery scope (via SQLite-backed Registry)
# ---------------------------------------------------------------------------

class TestRunExecutorCycleRecoveryScope:
    """
    run_executor_cycle only dispatches stale UoWs, not recently-written ones.

    Uses a SQLite-backed Registry so the full claim sequence runs correctly.
    """

    @pytest.fixture
    def db_path(self, tmp_path: Path) -> Path:
        return tmp_path / "test_registry.db"

    @pytest.fixture
    def registry(self, db_path: Path):
        from orchestration.registry import Registry
        return Registry(db_path)

    def _insert_uow(self, db_path: Path, uow_id: str, age_minutes: float) -> None:
        from orchestration.workflow_artifact import WorkflowArtifact, to_json
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        updated_at = (now - timedelta(minutes=age_minutes)).isoformat()

        artifact: dict = {
            "uow_id": uow_id,
            "executor_type": "functional-engineer",
            "constraints": [],
            "prescribed_skills": [],
            "instructions": "Do the thing",
        }

        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute(
                """
                INSERT INTO uow_registry (
                    id, type, source, status, posture, created_at, updated_at,
                    summary, success_criteria, workflow_artifact
                ) VALUES (?, 'executable', 'test', 'ready-for-executor', 'solo', ?, ?, 'Test', 'done', ?)
                """,
                (uow_id, created_at, updated_at, to_json(artifact)),
            )
            conn.commit()
        finally:
            conn.close()

    def _patch_inbox(self, monkeypatch: pytest.MonkeyPatch, inbox_called: list) -> None:
        """
        Patch _dispatch_via_inbox in the module the heartbeat actually uses.

        The heartbeat imports from src.orchestration.executor (not
        orchestration.executor), so the patch must target that module object.
        """
        import importlib
        # Ensure src.orchestration.executor is loaded so we can patch it
        if "src.orchestration.executor" not in sys.modules:
            importlib.import_module("src.orchestration.executor")
        import src.orchestration.executor as src_executor_mod
        monkeypatch.setattr(
            src_executor_mod, "_dispatch_via_inbox",
            lambda i, u: inbox_called.append(u) or "msg-id",
        )

    def test_recent_uow_not_dispatched(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A UoW younger than RECOVERY_STALE_MINUTES must NOT be dispatched by the heartbeat."""
        self._insert_uow(db_path, "recent-uow-001", age_minutes=1)

        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        assert inbox_called == [], (
            "No dispatch should occur for UoWs younger than RECOVERY_STALE_MINUTES"
        )
        assert result["stale"] == 0
        assert result["dispatched"] == 0

    def test_stale_uow_is_dispatched(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A UoW older than RECOVERY_STALE_MINUTES must be dispatched by the heartbeat."""
        stale_minutes = RECOVERY_STALE_MINUTES + 10
        self._insert_uow(db_path, "stale-uow-001", age_minutes=stale_minutes)

        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        assert "stale-uow-001" in inbox_called, (
            "Stale UoW must be dispatched via inbox"
        )
        assert result["stale"] == 1
        assert result["dispatched"] == 1

    def test_result_dict_has_required_keys(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_executor_cycle result dict must include evaluated, ready, stale, dispatched, skipped, errors."""
        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        for key in ("evaluated", "ready", "stale", "dispatched", "skipped", "errors"):
            assert key in result, f"Expected key '{key}' in result dict"

    def test_mixed_uows_only_stale_dispatched(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With one stale and one recent UoW, only the stale one is dispatched."""
        stale_minutes = RECOVERY_STALE_MINUTES + 10
        self._insert_uow(db_path, "stale-uow-002", age_minutes=stale_minutes)
        self._insert_uow(db_path, "recent-uow-002", age_minutes=1)

        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        assert inbox_called == ["stale-uow-002"], (
            "Only the stale UoW must be dispatched"
        )
        assert result["ready"] == 2
        assert result["stale"] == 1
        assert result["dispatched"] == 1
