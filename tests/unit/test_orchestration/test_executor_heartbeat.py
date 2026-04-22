"""
Tests for executor-heartbeat.py dispatch eligibility filter.

Background: The heartbeat is both a first-dispatch path (for UoWs missed entirely
by the primary inbox dispatch) and a recovery path (for UoWs the primary path
tried and missed due to orphaning). The staleness gate must only apply to the
recovery path, not the first-dispatch path.

Root cause of the bug fixed here: the original _filter_stale_uows applied a
5-minute staleness gate to ALL ready-for-executor UoWs. The steward re-prescribes
every ~2 minutes and resets updated_at each time, so a fresh UoW never ages past
2 minutes — the gate never opened and no UoW was ever dispatched.

The fix: apply the staleness gate only to UoWs that have a prior executor_orphan
audit entry. Fresh UoWs (never orphaned) pass through immediately.

Coverage:
- RECOVERY_STALE_MINUTES constant is a positive integer
- _filter_stale_uows: fresh UoW (no orphan history) passes through immediately
- _filter_stale_uows: orphaned UoW passes staleness gate when old enough
- _filter_stale_uows: orphaned UoW is blocked when too recent
- _filter_stale_uows: mixed fresh + orphaned UoWs handled correctly
- _filter_stale_uows: missing/unparseable updated_at on orphaned UoW treated as stale
- _filter_stale_uows: is_orphan_fn error treated as fresh (safe default)
- run_executor_cycle: fresh UoW dispatched immediately (no staleness gate)
- run_executor_cycle: orphaned recent UoW not dispatched (staleness gate active)
- run_executor_cycle: orphaned stale UoW dispatched (staleness gate clears)
- run_executor_cycle: result dict has required keys
- run_executor_cycle: mixed fresh + orphaned UoWs — correct dispatch set
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

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


# Canonical orphan/non-orphan callables for test clarity
_always_orphaned = lambda uow_id: True
_never_orphaned = lambda uow_id: False


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
# _filter_stale_uows — fresh UoW path (no orphan history)
# ---------------------------------------------------------------------------

class TestFilterStaleUoWsFreshPath:
    """Fresh UoWs (no prior executor_orphan entry) pass through immediately."""

    def test_empty_list_returns_empty(self) -> None:
        result = _filter_stale_uows([], stale_minutes=5)
        assert result == []

    def test_fresh_uow_passes_immediately_regardless_of_age(self) -> None:
        """A UoW with no orphan history passes through even if written just now."""
        uow = _StubUoW(id="fresh-001", updated_at=_uow_age(minutes=0.1))
        result = _filter_stale_uows([uow], stale_minutes=5, is_orphan_fn=_never_orphaned)
        assert len(result) == 1
        assert result[0].id == "fresh-001"

    def test_fresh_uow_passes_when_is_orphan_fn_is_none(self) -> None:
        """Default (no is_orphan_fn) treats all UoWs as fresh — all pass through."""
        uow = _StubUoW(id="fresh-002", updated_at=_uow_age(minutes=0.1))
        result = _filter_stale_uows([uow], stale_minutes=5)
        assert len(result) == 1

    def test_multiple_fresh_uows_all_pass(self) -> None:
        uows = [_StubUoW(id=f"fresh-{i}", updated_at=_uow_age(minutes=i)) for i in range(3)]
        result = _filter_stale_uows(uows, stale_minutes=5, is_orphan_fn=_never_orphaned)
        assert len(result) == 3

    def test_is_orphan_fn_error_treated_as_fresh(self) -> None:
        """If is_orphan_fn raises, the UoW is treated as fresh and passes immediately."""
        def _raises(uow_id: str) -> bool:
            raise RuntimeError("db error")

        uow = _StubUoW(id="err-001", updated_at=_uow_age(minutes=0.1))
        result = _filter_stale_uows([uow], stale_minutes=5, is_orphan_fn=_raises)
        assert len(result) == 1
        assert result[0].id == "err-001"


# ---------------------------------------------------------------------------
# _filter_stale_uows — orphaned UoW path (prior executor_orphan entry)
# ---------------------------------------------------------------------------

class TestFilterStaleUoWsOrphanPath:
    """Orphaned UoWs (prior executor_orphan audit entry) require the staleness gate."""

    def test_orphaned_old_uow_is_returned(self) -> None:
        """An orphaned UoW older than the threshold must be included."""
        uow = _StubUoW(id="orphan-old-001", updated_at=_uow_age(minutes=10))
        result = _filter_stale_uows([uow], stale_minutes=5, is_orphan_fn=_always_orphaned)
        assert len(result) == 1
        assert result[0].id == "orphan-old-001"

    def test_orphaned_recent_uow_is_excluded(self) -> None:
        """An orphaned UoW younger than the threshold must be excluded."""
        uow = _StubUoW(id="orphan-new-001", updated_at=_uow_age(minutes=1))
        result = _filter_stale_uows([uow], stale_minutes=5, is_orphan_fn=_always_orphaned)
        assert result == []

    def test_orphaned_mixed_uows_returns_only_stale(self) -> None:
        """Only stale orphaned UoWs are returned; recent orphaned ones are skipped."""
        stale = _StubUoW(id="orphan-stale-001", updated_at=_uow_age(minutes=20))
        recent = _StubUoW(id="orphan-recent-001", updated_at=_uow_age(minutes=2))
        result = _filter_stale_uows([stale, recent], stale_minutes=5, is_orphan_fn=_always_orphaned)
        assert len(result) == 1
        assert result[0].id == "orphan-stale-001"

    def test_orphaned_multiple_stale_uows_all_returned(self) -> None:
        uows = [
            _StubUoW(id=f"orphan-{i}", updated_at=_uow_age(minutes=10 + i))
            for i in range(3)
        ]
        result = _filter_stale_uows(uows, stale_minutes=5, is_orphan_fn=_always_orphaned)
        assert len(result) == 3

    def test_orphaned_unparseable_updated_at_treated_as_stale(self) -> None:
        """If updated_at cannot be parsed on an orphaned UoW, treat as stale (safe default)."""
        uow = _StubUoW(id="orphan-bad-ts-001", updated_at="not-a-timestamp")
        result = _filter_stale_uows([uow], stale_minutes=5, is_orphan_fn=_always_orphaned)
        assert len(result) == 1
        assert result[0].id == "orphan-bad-ts-001"

    def test_orphaned_non_string_updated_at_treated_as_stale(self) -> None:
        """If updated_at is not a string on an orphaned UoW, treat as stale (safe default)."""
        uow = MagicMock()
        uow.id = "orphan-mock-ts-001"
        uow.updated_at = MagicMock()  # Not a string — fromisoformat will raise TypeError
        result = _filter_stale_uows([uow], stale_minutes=5, is_orphan_fn=_always_orphaned)
        assert len(result) == 1

    def test_preserves_order(self) -> None:
        """Eligible UoWs are returned in the same order as the input list."""
        uows = [
            _StubUoW(id="orphan-b", updated_at=_uow_age(minutes=15)),
            _StubUoW(id="orphan-a", updated_at=_uow_age(minutes=20)),
        ]
        result = _filter_stale_uows(uows, stale_minutes=5, is_orphan_fn=_always_orphaned)
        assert [u.id for u in result] == ["orphan-b", "orphan-a"]


# ---------------------------------------------------------------------------
# _filter_stale_uows — mixed fresh + orphaned UoWs
# ---------------------------------------------------------------------------

class TestFilterStaleUoWsMixed:
    """Fresh and orphaned UoWs in the same list are handled per their respective rules."""

    def test_fresh_and_orphaned_recent_mixed(self) -> None:
        """Fresh UoW passes immediately; recent orphaned UoW is blocked."""
        fresh = _StubUoW(id="fresh-mix-001", updated_at=_uow_age(minutes=0.5))
        orphan_recent = _StubUoW(id="orphan-mix-recent-001", updated_at=_uow_age(minutes=1))

        orphan_ids = {"orphan-mix-recent-001"}
        is_orphan = lambda uow_id: uow_id in orphan_ids

        result = _filter_stale_uows([fresh, orphan_recent], stale_minutes=5, is_orphan_fn=is_orphan)
        assert [u.id for u in result] == ["fresh-mix-001"]

    def test_fresh_and_orphaned_stale_mixed(self) -> None:
        """Fresh UoW passes immediately; stale orphaned UoW also passes."""
        fresh = _StubUoW(id="fresh-mix-002", updated_at=_uow_age(minutes=0.5))
        orphan_stale = _StubUoW(id="orphan-mix-stale-001", updated_at=_uow_age(minutes=10))

        orphan_ids = {"orphan-mix-stale-001"}
        is_orphan = lambda uow_id: uow_id in orphan_ids

        result = _filter_stale_uows([fresh, orphan_stale], stale_minutes=5, is_orphan_fn=is_orphan)
        assert len(result) == 2
        ids = {u.id for u in result}
        assert "fresh-mix-002" in ids
        assert "orphan-mix-stale-001" in ids


# ---------------------------------------------------------------------------
# run_executor_cycle — dispatch eligibility (via SQLite-backed Registry)
# ---------------------------------------------------------------------------

class TestRunExecutorCycleDispatchEligibility:
    """
    run_executor_cycle dispatches fresh UoWs immediately and applies the
    staleness gate only to previously-orphaned UoWs.

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

    def _insert_executor_orphan_audit(self, db_path: Path, uow_id: str) -> None:
        """Write an executor_orphan startup_sweep audit entry for a UoW."""
        now = datetime.now(timezone.utc).isoformat()
        note_json = json.dumps({
            "event": "startup_sweep",
            "actor": "steward",
            "classification": "executor_orphan",
            "uow_id": uow_id,
            "timestamp": now,
            "prior_status": "ready-for-executor",
            "proposed_at": now,
            "age_seconds": 3700,
            "threshold_seconds": 3600,
        })
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'startup_sweep', 'ready-for-executor', 'ready-for-steward', 'steward', ?)
                """,
                (now, uow_id, note_json),
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
            lambda i, u, **kw: inbox_called.append(u) or "msg-id",
        )

    def test_fresh_uow_dispatched_immediately(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A fresh UoW (no prior executor_orphan entry) must be dispatched immediately,
        even if it was written just seconds ago.

        This is the primary fix: the original code blocked all dispatch because the
        steward keeps re-prescribing and resetting updated_at, so a fresh UoW never
        aged past 2 minutes and the 5-minute gate never opened.
        """
        self._insert_uow(db_path, "fresh-uow-001", age_minutes=0.1)
        # No audit entry — this UoW has never been orphaned

        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        assert "fresh-uow-001" in inbox_called, (
            "Fresh UoW (no prior orphan) must be dispatched immediately"
        )
        assert result["dispatched"] == 1

    def test_orphaned_recent_uow_not_dispatched(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A UoW with prior executor_orphan history that is too recent must NOT be
        dispatched — the staleness gate applies to the recovery path.
        """
        self._insert_uow(db_path, "orphan-recent-001", age_minutes=1)
        self._insert_executor_orphan_audit(db_path, "orphan-recent-001")

        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        assert inbox_called == [], (
            "No dispatch should occur for orphaned UoWs younger than RECOVERY_STALE_MINUTES"
        )
        assert result["stale"] == 0
        assert result["dispatched"] == 0

    def test_orphaned_stale_uow_is_dispatched(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A UoW with prior executor_orphan history that has aged past RECOVERY_STALE_MINUTES
        must be dispatched by the heartbeat (recovery path).
        """
        stale_minutes = RECOVERY_STALE_MINUTES + 10
        self._insert_uow(db_path, "orphan-stale-001", age_minutes=stale_minutes)
        self._insert_executor_orphan_audit(db_path, "orphan-stale-001")

        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        assert "orphan-stale-001" in inbox_called, (
            "Stale orphaned UoW must be dispatched via inbox"
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

    def test_mixed_fresh_and_orphaned_stale(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With one fresh UoW and one stale orphaned UoW:
        - fresh UoW: dispatched immediately
        - stale orphaned UoW: dispatched (staleness gate cleared)
        """
        stale_minutes = RECOVERY_STALE_MINUTES + 10
        self._insert_uow(db_path, "fresh-mix-001", age_minutes=0.1)
        self._insert_uow(db_path, "orphan-stale-mix-001", age_minutes=stale_minutes)
        self._insert_executor_orphan_audit(db_path, "orphan-stale-mix-001")

        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        assert set(inbox_called) == {"fresh-mix-001", "orphan-stale-mix-001"}, (
            "Both the fresh UoW and the stale orphaned UoW must be dispatched"
        )
        assert result["ready"] == 2
        assert result["dispatched"] == 2

    def test_mixed_fresh_and_orphaned_recent(
        self, registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With one fresh UoW and one recent orphaned UoW:
        - fresh UoW: dispatched immediately
        - recent orphaned UoW: blocked (staleness gate active)
        """
        self._insert_uow(db_path, "fresh-mix-002", age_minutes=0.1)
        self._insert_uow(db_path, "orphan-recent-mix-001", age_minutes=1)
        self._insert_executor_orphan_audit(db_path, "orphan-recent-mix-001")

        inbox_called: list[str] = []
        self._patch_inbox(monkeypatch, inbox_called)

        result = _hb.run_executor_cycle(registry)

        assert inbox_called == ["fresh-mix-002"], (
            "Only the fresh UoW must be dispatched; recent orphaned UoW must be blocked"
        )
        assert result["ready"] == 2
        assert result["dispatched"] == 1
