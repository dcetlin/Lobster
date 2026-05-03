"""
Tests for heartbeat-based kill classification of executing orphans.

Issue #963: When startup_sweep finds a UoW stuck in `executing`, use the
heartbeat signal to distinguish two kill scenarios:
  - orphan_kill_before_start: agent was killed before writing any heartbeat
    after dispatch (no evidence of work having begun)
  - orphan_kill_during_execution: agent wrote heartbeats after dispatch
    (was actively working before being killed)

Coverage:
- DISPATCH_WINDOW_SECONDS constant: positive integer
- classify_executing_orphan_kill_type:
    no heartbeat (NULL) → orphan_kill_before_start
    heartbeat exists but at or before dispatch time → orphan_kill_before_start
    heartbeat exists after dispatch + DISPATCH_WINDOW_SECONDS → orphan_kill_during_execution
    heartbeat is non-parseable → orphan_kill_before_start (safe default)
    dispatch timestamp is None (no executor_dispatch audit) → orphan_kill_before_start
- registry.get_executor_dispatch_timestamp:
    returns None when no audit_log entries exist
    returns None when no executor_dispatch entry exists
    returns ISO timestamp from executor_dispatch entry
    returns most recent executor_dispatch timestamp when multiple exist
- startup_sweep Population 4 (executing UoWs):
    no heartbeat written after dispatch → kill_classification = orphan_kill_before_start
    heartbeat written after dispatch → kill_classification = orphan_kill_during_execution
    kill_classification field present in audit note JSON
- steward._determine_reentry_posture:
    startup_sweep note with classification=orphan_kill_before_start → returns orphan_kill_before_start
    startup_sweep note with classification=orphan_kill_during_execution → returns orphan_kill_during_execution
    backward compat: startup_sweep note with classification=executing_orphan still returns executing_orphan
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Load startup_sweep module (lives in scheduled-tasks/)
# ---------------------------------------------------------------------------

_SWEEP_MOD_NAME = "startup_sweep_kill_test_mod"  # isolated sys.modules key
_SWEEP_PATH = REPO_ROOT / "scheduled-tasks" / "startup_sweep.py"


def _load_startup_sweep():
    spec = importlib.util.spec_from_file_location(_SWEEP_MOD_NAME, _SWEEP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_SWEEP_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_sweep_mod = _load_startup_sweep()
run_startup_sweep = _sweep_mod.run_startup_sweep
_classify_executing_orphan_kill_type = _sweep_mod._classify_executing_orphan_kill_type


# ---------------------------------------------------------------------------
# Registry import (full schema via Registry)
# ---------------------------------------------------------------------------

from src.orchestration.registry import Registry


# ---------------------------------------------------------------------------
# Schema helper — matches the test_startup_sweep.py pattern but includes
# heartbeat_at, heartbeat_ttl, and the full schema needed for these tests.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uow_registry (
    id                  TEXT    PRIMARY KEY,
    type                TEXT    NOT NULL DEFAULT 'executable',
    source              TEXT    NOT NULL,
    source_issue_number INTEGER,
    sweep_date          TEXT,
    status              TEXT    NOT NULL DEFAULT 'proposed',
    posture             TEXT    NOT NULL DEFAULT 'solo',
    agent               TEXT,
    children            TEXT    DEFAULT '[]',
    parent              TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    summary             TEXT    NOT NULL,
    output_ref          TEXT,
    hooks_applied       TEXT    DEFAULT '[]',
    route_reason        TEXT,
    route_evidence      TEXT    DEFAULT '{}',
    trigger             TEXT    DEFAULT '{"type": "immediate"}',
    vision_ref          TEXT    DEFAULT NULL,
    workflow_artifact   TEXT    NULL,
    success_criteria    TEXT    NULL,
    prescribed_skills   TEXT    NULL,
    steward_cycles      INTEGER NOT NULL DEFAULT 0,
    timeout_at          TEXT    NULL,
    estimated_runtime   INTEGER NULL,
    steward_agenda      TEXT    NULL,
    steward_log         TEXT    NULL,
    UNIQUE(source_issue_number, sweep_date)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    uow_id      TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    agent       TEXT,
    note        TEXT
);
"""


def _open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _iso_future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _make_executing_uow(
    conn: sqlite3.Connection,
    *,
    uow_id: str,
    started_at: str | None = None,
    heartbeat_at: str | None = None,
    source_issue_number: int = 42,
) -> str:
    """Insert an `executing` UoW into the registry for tests."""
    now = _now_iso()
    _started_at = started_at or _iso_ago(1800)  # Default: 30 min ago (past threshold)
    conn.execute(
        """
        INSERT INTO uow_registry (
            id, type, source, source_issue_number, sweep_date, status, posture,
            created_at, updated_at, started_at, summary, route_evidence, trigger,
            heartbeat_at
        ) VALUES (?, 'executable', ?, ?, '2026-01-01', 'executing', 'solo',
                  ?, ?, ?, 'Test UoW', '{}', '{"type": "immediate"}', ?)
        """,
        (
            uow_id,
            f"github:issue/{source_issue_number}",
            source_issue_number,
            now,
            _started_at,
            _started_at,
            heartbeat_at,
        ),
    )
    conn.commit()
    return uow_id


def _insert_executor_dispatch_audit(
    conn: sqlite3.Connection,
    uow_id: str,
    timestamp: str,
) -> None:
    """Insert an executor_dispatch audit entry simulating inbox dispatch."""
    note = json.dumps({
        "actor": "executor",
        "executor_id": f"agent-{uow_id[:8]}",
        "timestamp": timestamp,
    })
    conn.execute(
        """
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
        VALUES (?, ?, 'executor_dispatch', 'active', 'executing', 'executor', ?)
        """,
        (timestamp, uow_id, note),
    )
    conn.commit()


def _get_audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = _open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_status(db_path: Path, uow_id: str) -> str:
    conn = _open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["status"] if row else "NOT_FOUND"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.close()
    return db_path


@pytest.fixture
def registry(tmp_db: Path) -> Registry:
    return Registry(tmp_db)


# ---------------------------------------------------------------------------
# DISPATCH_WINDOW_SECONDS constant sanity check
# ---------------------------------------------------------------------------

class TestDispatchWindowConstant:
    def test_is_positive_integer(self) -> None:
        assert isinstance(_sweep_mod.DISPATCH_WINDOW_SECONDS, int)
        assert _sweep_mod.DISPATCH_WINDOW_SECONDS > 0

    def test_is_at_least_60_seconds(self) -> None:
        """Must be long enough to absorb agent startup jitter."""
        assert _sweep_mod.DISPATCH_WINDOW_SECONDS >= 60


# ---------------------------------------------------------------------------
# _classify_executing_orphan_kill_type — pure function unit tests
# ---------------------------------------------------------------------------

class TestClassifyExecutingOrphanKillType:
    """
    Pure function tests for kill classification.

    classify_executing_orphan_kill_type(dispatch_ts, heartbeat_at) -> str

    Returns:
    - 'orphan_kill_before_start': no evidence of heartbeat after dispatch
    - 'orphan_kill_during_execution': heartbeat exists after dispatch window
    """

    def test_no_heartbeat_null_is_kill_before_start(self) -> None:
        """NULL heartbeat_at → agent never wrote a heartbeat → kill-before-start."""
        dispatch_ts = _iso_ago(1800)
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=dispatch_ts,
            heartbeat_at=None,
        )
        assert result == "orphan_kill_before_start"

    def test_no_dispatch_timestamp_is_kill_before_start(self) -> None:
        """No executor_dispatch audit entry → cannot confirm work started → kill-before-start."""
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=None,
            heartbeat_at=_iso_ago(1200),
        )
        assert result == "orphan_kill_before_start"

    def test_both_none_is_kill_before_start(self) -> None:
        """Both None → kill-before-start (safe default)."""
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=None,
            heartbeat_at=None,
        )
        assert result == "orphan_kill_before_start"

    def test_heartbeat_before_dispatch_is_kill_before_start(self) -> None:
        """
        Heartbeat written BEFORE dispatch (stale initial value from set_heartbeat_ttl).
        This is the key case: set_heartbeat_ttl writes the initial heartbeat_at at
        claim time (before dispatch). If heartbeat_at <= dispatch_ts + window, the
        subagent never updated it.
        """
        dispatch_ts = _iso_ago(1800)
        # Heartbeat written 10 seconds BEFORE dispatch (initial set_heartbeat_ttl)
        heartbeat_before_dispatch = _iso_ago(1810)
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=dispatch_ts,
            heartbeat_at=heartbeat_before_dispatch,
        )
        assert result == "orphan_kill_before_start"

    def test_heartbeat_at_exact_dispatch_time_is_kill_before_start(self) -> None:
        """Heartbeat at exactly dispatch time → within window → kill-before-start."""
        dispatch_ts = _iso_ago(1800)
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=dispatch_ts,
            heartbeat_at=dispatch_ts,  # Same timestamp
        )
        assert result == "orphan_kill_before_start"

    def test_heartbeat_within_dispatch_window_is_kill_before_start(self) -> None:
        """
        Heartbeat written just after dispatch but within the window is still
        kill-before-start — the window absorbs agent startup jitter.
        """
        dispatch_ts = _iso_ago(1800)
        # Heartbeat written 30 seconds after dispatch (within window)
        heartbeat_just_after = _iso_ago(1800 - 30)
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=dispatch_ts,
            heartbeat_at=heartbeat_just_after,
        )
        assert result == "orphan_kill_before_start"

    def test_heartbeat_after_dispatch_window_is_kill_during_execution(self) -> None:
        """
        Heartbeat written after dispatch + DISPATCH_WINDOW_SECONDS →
        agent was actively working before being killed.
        """
        dispatch_ts = _iso_ago(1800)
        # Heartbeat written DISPATCH_WINDOW_SECONDS + 60 after dispatch (clearly working)
        heartbeat_after_window = _iso_ago(1800 - _sweep_mod.DISPATCH_WINDOW_SECONDS - 60)
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=dispatch_ts,
            heartbeat_at=heartbeat_after_window,
        )
        assert result == "orphan_kill_during_execution"

    def test_recent_heartbeat_is_kill_during_execution(self) -> None:
        """A very recent heartbeat (agent was actively working) → kill-during-execution."""
        dispatch_ts = _iso_ago(600)
        heartbeat_recent = _iso_ago(30)  # 30 seconds ago — agent was running
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=dispatch_ts,
            heartbeat_at=heartbeat_recent,
        )
        assert result == "orphan_kill_during_execution"

    def test_unparseable_heartbeat_is_kill_before_start(self) -> None:
        """Unparseable heartbeat_at → cannot confirm work started → kill-before-start (safe default)."""
        dispatch_ts = _iso_ago(1800)
        result = _classify_executing_orphan_kill_type(
            dispatch_ts=dispatch_ts,
            heartbeat_at="not-a-timestamp",
        )
        assert result == "orphan_kill_before_start"

    def test_unparseable_dispatch_ts_is_kill_before_start(self) -> None:
        """Unparseable dispatch_ts → cannot compare → kill-before-start (safe default)."""
        result = _classify_executing_orphan_kill_type(
            dispatch_ts="not-a-timestamp",
            heartbeat_at=_iso_ago(600),
        )
        assert result == "orphan_kill_before_start"


# ---------------------------------------------------------------------------
# registry.get_executor_dispatch_timestamp
# ---------------------------------------------------------------------------

class TestGetExecutorDispatchTimestamp:
    def test_returns_none_when_no_audit_entries(self, registry: Registry, tmp_db: Path) -> None:
        conn = _open_conn(tmp_db)
        _make_executing_uow(conn, uow_id="uow-no-audit-001")
        conn.close()

        result = registry.get_executor_dispatch_timestamp("uow-no-audit-001")
        assert result is None

    def test_returns_none_when_no_executor_dispatch_event(self, registry: Registry, tmp_db: Path) -> None:
        conn = _open_conn(tmp_db)
        _make_executing_uow(conn, uow_id="uow-no-dispatch-001")
        # Write an unrelated audit entry (not executor_dispatch)
        conn.execute(
            "INSERT INTO audit_log (ts, uow_id, event, agent) VALUES (?, ?, 'startup_sweep', 'steward')",
            (_iso_ago(600), "uow-no-dispatch-001"),
        )
        conn.commit()
        conn.close()

        result = registry.get_executor_dispatch_timestamp("uow-no-dispatch-001")
        assert result is None

    def test_returns_timestamp_from_executor_dispatch_entry(self, registry: Registry, tmp_db: Path) -> None:
        dispatch_ts = _iso_ago(1800)
        conn = _open_conn(tmp_db)
        _make_executing_uow(conn, uow_id="uow-dispatch-001")
        _insert_executor_dispatch_audit(conn, "uow-dispatch-001", dispatch_ts)
        conn.close()

        result = registry.get_executor_dispatch_timestamp("uow-dispatch-001")
        assert result is not None
        # Should match the dispatch timestamp (may have minor precision diff)
        result_dt = datetime.fromisoformat(result)
        expected_dt = datetime.fromisoformat(dispatch_ts)
        assert abs((result_dt - expected_dt).total_seconds()) < 1.0

    def test_returns_most_recent_executor_dispatch_when_multiple(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """Multiple executor_dispatch entries → return the most recent one."""
        older_dispatch = _iso_ago(3600)
        newer_dispatch = _iso_ago(1800)

        conn = _open_conn(tmp_db)
        _make_executing_uow(conn, uow_id="uow-multi-dispatch-001")
        _insert_executor_dispatch_audit(conn, "uow-multi-dispatch-001", older_dispatch)
        _insert_executor_dispatch_audit(conn, "uow-multi-dispatch-001", newer_dispatch)
        conn.close()

        result = registry.get_executor_dispatch_timestamp("uow-multi-dispatch-001")
        assert result is not None
        result_dt = datetime.fromisoformat(result)
        newer_dt = datetime.fromisoformat(newer_dispatch)
        older_dt = datetime.fromisoformat(older_dispatch)
        # Result should be closer to newer_dispatch than to older_dispatch
        assert abs((result_dt - newer_dt).total_seconds()) < abs((result_dt - older_dt).total_seconds())

    def test_returns_none_for_unknown_uow(self, registry: Registry) -> None:
        result = registry.get_executor_dispatch_timestamp("uow-does-not-exist")
        assert result is None


# ---------------------------------------------------------------------------
# startup_sweep Population 4 — kill_classification in audit note
# ---------------------------------------------------------------------------

class TestStartupSweepKillClassification:
    """
    Integration tests for Population 4 of startup_sweep.

    When startup_sweep processes an `executing` UoW, the audit note must include
    a `kill_classification` field indicating whether the agent was killed before
    starting work or mid-execution.
    """

    def _make_uow_with_dispatch(
        self,
        db_path: Path,
        *,
        uow_id: str,
        dispatch_ago_seconds: float,
        heartbeat_at: str | None = None,
    ) -> None:
        """Create an executing UoW with an executor_dispatch audit entry."""
        dispatch_ts = _iso_ago(dispatch_ago_seconds)
        conn = _open_conn(db_path)
        _make_executing_uow(
            conn,
            uow_id=uow_id,
            started_at=_iso_ago(dispatch_ago_seconds + 30),
            heartbeat_at=heartbeat_at,
        )
        _insert_executor_dispatch_audit(conn, uow_id, dispatch_ts)
        conn.close()

    def test_executing_orphan_with_no_heartbeat_classified_as_kill_before_start(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        An executing UoW with NULL heartbeat_at gets kill_classification=orphan_kill_before_start.
        """
        self._make_uow_with_dispatch(
            tmp_db,
            uow_id="uow-kill-before-001",
            dispatch_ago_seconds=1800,
            heartbeat_at=None,
        )

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
        )

        assert result.executing_swept == 1
        entries = _get_audit_entries(tmp_db, "uow-kill-before-001")
        sweep_entries = [e for e in entries if e["event"] == "startup_sweep"]
        assert len(sweep_entries) == 1

        note = json.loads(sweep_entries[0]["note"])
        assert note["kill_classification"] == "orphan_kill_before_start"
        assert note["classification"] == "orphan_kill_before_start"

    def test_executing_orphan_with_heartbeat_after_dispatch_classified_as_kill_during_execution(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        An executing UoW with heartbeat_at after dispatch + DISPATCH_WINDOW_SECONDS
        gets kill_classification=orphan_kill_during_execution.
        """
        dispatch_ago = 1800
        # Heartbeat written well after dispatch + window (agent was working)
        heartbeat_at = _iso_ago(dispatch_ago - _sweep_mod.DISPATCH_WINDOW_SECONDS - 120)
        self._make_uow_with_dispatch(
            tmp_db,
            uow_id="uow-kill-during-001",
            dispatch_ago_seconds=dispatch_ago,
            heartbeat_at=heartbeat_at,
        )

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
        )

        assert result.executing_swept == 1
        entries = _get_audit_entries(tmp_db, "uow-kill-during-001")
        sweep_entries = [e for e in entries if e["event"] == "startup_sweep"]
        assert len(sweep_entries) == 1

        note = json.loads(sweep_entries[0]["note"])
        assert note["kill_classification"] == "orphan_kill_during_execution"
        assert note["classification"] == "orphan_kill_during_execution"

    def test_kill_classification_field_always_present_in_executing_orphan_note(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        The kill_classification field must be present in the audit note for all
        executing orphan transitions (even when it defaults to kill_before_start).
        """
        # No heartbeat, no dispatch audit → defaults to kill_before_start
        conn = _open_conn(tmp_db)
        _make_executing_uow(
            conn,
            uow_id="uow-kill-field-001",
            started_at=_iso_ago(1800),
            heartbeat_at=None,
        )
        conn.close()

        run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
        )

        entries = _get_audit_entries(tmp_db, "uow-kill-field-001")
        sweep_entries = [e for e in entries if e["event"] == "startup_sweep"]
        assert len(sweep_entries) == 1

        note = json.loads(sweep_entries[0]["note"])
        assert "kill_classification" in note, (
            "kill_classification must always be present in executing_orphan audit note"
        )

    def test_executing_orphan_transition_still_occurs_regardless_of_kill_type(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        Both kill types result in status transition to ready-for-steward.
        The classification is metadata; it doesn't affect the transition itself.
        """
        self._make_uow_with_dispatch(
            tmp_db,
            uow_id="uow-transition-001",
            dispatch_ago_seconds=1800,
            heartbeat_at=None,
        )

        run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
        )

        assert _get_status(tmp_db, "uow-transition-001") == "ready-for-steward"

    def test_backward_compat_no_dispatch_audit_defaults_to_kill_before_start(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        For UoWs where no executor_dispatch audit entry exists (e.g. older records
        before this migration), the classification defaults to kill_before_start.

        The heartbeat is stale (> HEARTBEAT_SKIP_THRESHOLD_SECONDS) so the
        heartbeat recency gate (#992) does not protect this UoW — the kill
        fires and the PR #968 classification logic is exercised.
        """
        conn = _open_conn(tmp_db)
        _make_executing_uow(
            conn,
            uow_id="uow-no-dispatch-audit-001",
            started_at=_iso_ago(1800),
            heartbeat_at=_iso_ago(300),  # Stale heartbeat (> 120 s threshold) — kill fires
        )
        conn.close()

        run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
        )

        entries = _get_audit_entries(tmp_db, "uow-no-dispatch-audit-001")
        sweep_entries = [e for e in entries if e["event"] == "startup_sweep"]
        assert len(sweep_entries) == 1
        note = json.loads(sweep_entries[0]["note"])
        assert note["kill_classification"] == "orphan_kill_before_start"


# ---------------------------------------------------------------------------
# steward._determine_reentry_posture — new classification values
# ---------------------------------------------------------------------------

class TestReentryPostureForNewClassifications:
    """
    _determine_reentry_posture reads the startup_sweep audit note and returns
    the classification as the reentry posture.

    New values:
    - orphan_kill_before_start: kill before work began
    - orphan_kill_during_execution: kill mid-execution

    Backward compat: executing_orphan still works (for existing records).
    """

    def _make_audit_entries_with_sweep(self, classification: str) -> list[dict]:
        """Build a minimal audit_entries list with a startup_sweep entry."""
        note = json.dumps({
            "event": "startup_sweep",
            "actor": "steward",
            "classification": classification,
            "output_ref": None,
            "uow_id": "test-uow",
            "timestamp": _now_iso(),
            "prior_status": "executing",
        })
        return [
            {
                "event": "startup_sweep",
                "note": note,
                "from_status": "executing",
                "to_status": "ready-for-steward",
            }
        ]

    def test_orphan_kill_before_start_returns_correct_posture(self) -> None:
        from src.orchestration.steward import _determine_reentry_posture
        entries = self._make_audit_entries_with_sweep("orphan_kill_before_start")
        posture = _determine_reentry_posture(entries, return_reason=None)
        assert posture == "orphan_kill_before_start"

    def test_orphan_kill_during_execution_returns_correct_posture(self) -> None:
        from src.orchestration.steward import _determine_reentry_posture
        entries = self._make_audit_entries_with_sweep("orphan_kill_during_execution")
        posture = _determine_reentry_posture(entries, return_reason=None)
        assert posture == "orphan_kill_during_execution"

    def test_executing_orphan_still_works_for_backward_compat(self) -> None:
        """Pre-existing records with classification=executing_orphan continue to work."""
        from src.orchestration.steward import _determine_reentry_posture
        entries = self._make_audit_entries_with_sweep("executing_orphan")
        posture = _determine_reentry_posture(entries, return_reason=None)
        assert posture == "executing_orphan"

    def test_return_reason_orphan_kill_before_start_maps_to_correct_posture(self) -> None:
        """When return_reason is directly set to the new value, it maps correctly."""
        from src.orchestration.steward import _determine_reentry_posture
        # Empty audit entries — posture comes from return_reason
        posture = _determine_reentry_posture(
            audit_entries=[],
            return_reason="orphan_kill_before_start",
        )
        assert posture == "orphan_kill_before_start"

    def test_return_reason_orphan_kill_during_execution_maps_to_correct_posture(self) -> None:
        """When return_reason is directly set to the new value, it maps correctly."""
        from src.orchestration.steward import _determine_reentry_posture
        posture = _determine_reentry_posture(
            audit_entries=[],
            return_reason="orphan_kill_during_execution",
        )
        assert posture == "orphan_kill_during_execution"

    def test_new_classifications_are_in_orphan_classification_table(self) -> None:
        """Both new values must be in _RETURN_REASON_CLASSIFICATIONS as orphan type."""
        from src.orchestration.steward import _RETURN_REASON_CLASSIFICATIONS
        assert "orphan_kill_before_start" in _RETURN_REASON_CLASSIFICATIONS
        assert "orphan_kill_during_execution" in _RETURN_REASON_CLASSIFICATIONS
        # Both should map to the orphan classification bucket
        assert _RETURN_REASON_CLASSIFICATIONS["orphan_kill_before_start"] == "orphan"
        assert _RETURN_REASON_CLASSIFICATIONS["orphan_kill_during_execution"] == "orphan"
