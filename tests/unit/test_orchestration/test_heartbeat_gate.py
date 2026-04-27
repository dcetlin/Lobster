"""
Tests for the heartbeat recency gate in startup_sweep — issue #992.

When startup_sweep finds a UoW in `executing` status that has exceeded the
TTL, it must now check last_heartbeat_at before killing:

  - Recent heartbeat (< HEARTBEAT_SKIP_THRESHOLD_SECONDS ago) → skip kill,
    log "agent alive", increment skipped_heartbeat_alive.
  - Stale heartbeat (≥ HEARTBEAT_SKIP_THRESHOLD_SECONDS ago) → kill,
    classify correctly (preserving PR #968 classification logic).
  - NULL heartbeat → kill (same as before PR #989).
  - UoW under TTL → not killed regardless of heartbeat (regression).

Coverage of _is_heartbeat_recent (pure function):
  - None → False (safe default)
  - unparseable timestamp → False (safe default)
  - heartbeat younger than threshold → True
  - heartbeat exactly at threshold boundary → False (boundary is exclusive)
  - heartbeat older than threshold → False
  - threshold=0 disables gate → False (gate disabled, caller must handle)

Prerequisites this closes the loop on:
  - PR #968: orphan_kill_before_start / orphan_kill_during_execution classification
  - PR #989: write_wos_heartbeat MCP tool
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

_SWEEP_MOD_NAME = "startup_sweep_heartbeat_gate_test_mod"
_SWEEP_PATH = REPO_ROOT / "scheduled-tasks" / "startup_sweep.py"


def _load_startup_sweep():
    spec = importlib.util.spec_from_file_location(_SWEEP_MOD_NAME, _SWEEP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_SWEEP_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_sweep_mod = _load_startup_sweep()
run_startup_sweep = _sweep_mod.run_startup_sweep
_is_heartbeat_recent = _sweep_mod._is_heartbeat_recent
HEARTBEAT_SKIP_THRESHOLD_SECONDS = _sweep_mod.HEARTBEAT_SKIP_THRESHOLD_SECONDS
DISPATCH_WINDOW_SECONDS = _sweep_mod.DISPATCH_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# Registry import
# ---------------------------------------------------------------------------

from src.orchestration.registry import Registry


# ---------------------------------------------------------------------------
# Schema — must include heartbeat_at column (added by PR #989 migration)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _make_executing_uow(
    conn: sqlite3.Connection,
    *,
    uow_id: str,
    started_at: str | None = None,
    heartbeat_at: str | None = None,
    source_issue_number: int = 42,
) -> str:
    now = _now_iso()
    _started_at = started_at or _iso_ago(1800)  # Default: 30 min ago (past TTL threshold)
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
    note = json.dumps({"actor": "executor", "timestamp": timestamp})
    conn.execute(
        """
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
        VALUES (?, ?, 'executor_dispatch', 'active', 'executing', 'executor', ?)
        """,
        (timestamp, uow_id, note),
    )
    conn.commit()


def _get_status(db_path: Path, uow_id: str) -> str:
    conn = _open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["status"] if row else "NOT_FOUND"
    finally:
        conn.close()


def _get_sweep_audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = _open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? AND event = 'startup_sweep' ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
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
# _is_heartbeat_recent — pure function unit tests
# ---------------------------------------------------------------------------

class TestIsHeartbeatRecent:
    """Pure function tests — no registry, no I/O."""

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_none_returns_false(self) -> None:
        """NULL heartbeat_at → safe default → False."""
        assert _is_heartbeat_recent(None, self._now(), 120) is False

    def test_unparseable_timestamp_returns_false(self) -> None:
        """Unparseable heartbeat_at → safe default → False."""
        assert _is_heartbeat_recent("not-a-timestamp", self._now(), 120) is False

    def test_empty_string_returns_false(self) -> None:
        assert _is_heartbeat_recent("", self._now(), 120) is False

    def test_recent_heartbeat_within_threshold_returns_true(self) -> None:
        """Heartbeat 30 s ago with threshold=120 s → recent → True."""
        heartbeat_at = _iso_ago(30)
        assert _is_heartbeat_recent(heartbeat_at, self._now(), 120) is True

    def test_heartbeat_just_before_threshold_returns_true(self) -> None:
        """Heartbeat 119 s ago with threshold=120 s → within threshold → True."""
        heartbeat_at = _iso_ago(119)
        assert _is_heartbeat_recent(heartbeat_at, self._now(), 120) is True

    def test_heartbeat_at_exact_threshold_boundary_returns_false(self) -> None:
        """Heartbeat exactly at threshold (age == threshold) → boundary is exclusive → False."""
        now = self._now()
        heartbeat_at = (now - timedelta(seconds=120)).isoformat()
        # age == 120 s, threshold == 120 s → NOT recent (< not <=)
        assert _is_heartbeat_recent(heartbeat_at, now, 120) is False

    def test_stale_heartbeat_older_than_threshold_returns_false(self) -> None:
        """Heartbeat 300 s ago with threshold=120 s → stale → False."""
        heartbeat_at = _iso_ago(300)
        assert _is_heartbeat_recent(heartbeat_at, self._now(), 120) is False

    def test_threshold_zero_always_returns_false(self) -> None:
        """threshold=0 disables the gate — always returns False."""
        heartbeat_at = _iso_ago(1)  # 1 second ago — would be recent with any threshold > 0
        assert _is_heartbeat_recent(heartbeat_at, self._now(), 0) is False

    def test_heartbeat_skip_threshold_constant_is_positive_integer(self) -> None:
        """HEARTBEAT_SKIP_THRESHOLD_SECONDS is a named positive integer constant."""
        assert isinstance(HEARTBEAT_SKIP_THRESHOLD_SECONDS, int)
        assert HEARTBEAT_SKIP_THRESHOLD_SECONDS > 0

    def test_heartbeat_skip_threshold_constant_is_at_least_60(self) -> None:
        """Must be at least 2× a typical 30-s heartbeat interval."""
        assert HEARTBEAT_SKIP_THRESHOLD_SECONDS >= 60


# ---------------------------------------------------------------------------
# Integration: Population 4 heartbeat gate
# ---------------------------------------------------------------------------

class TestHeartbeatGateInPopulationFour:
    """
    Integration tests that run run_startup_sweep against a real Registry.

    These verify the four outcome cells described in issue #992:
      1. Recent heartbeat → NOT killed
      2. Stale heartbeat → killed, classified correctly
      3. NULL heartbeat → killed
      4. Under TTL → not killed (regression — unchanged behaviour)
    """

    def _make_uow_over_ttl(
        self,
        db_path: Path,
        *,
        uow_id: str,
        heartbeat_at: str | None,
        dispatch_ago_seconds: float = 1800,
    ) -> None:
        """Create an `executing` UoW that has exceeded the default TTL (600 s)."""
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

    # --- Cell 1: recent heartbeat → skip kill --------------------------------

    def test_recent_heartbeat_within_threshold_is_not_killed(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        UoW has exceeded TTL but emitted a heartbeat 30 s ago.
        The agent is alive — sweep must NOT kill it.
        """
        self._make_uow_over_ttl(
            tmp_db,
            uow_id="uow-alive-001",
            heartbeat_at=_iso_ago(30),  # recent — well within 120 s threshold
        )

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        assert result.executing_swept == 0
        assert result.skipped_heartbeat_alive == 1
        assert _get_status(tmp_db, "uow-alive-001") == "executing"
        assert len(_get_sweep_audit_entries(tmp_db, "uow-alive-001")) == 0

    def test_recent_heartbeat_increments_skipped_heartbeat_alive_counter(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        Two executing UoWs with recent heartbeats: both skipped, counter = 2.
        """
        for i in (1, 2):
            dispatch_ts = _iso_ago(1800 + i * 60)
            conn = _open_conn(tmp_db)
            _make_executing_uow(
                conn,
                uow_id=f"uow-alive-multi-{i:03d}",
                started_at=_iso_ago(1800 + i * 60 + 30),
                heartbeat_at=_iso_ago(10),
                source_issue_number=100 + i,  # distinct per UoW to avoid UNIQUE conflict
            )
            _insert_executor_dispatch_audit(conn, f"uow-alive-multi-{i:03d}", dispatch_ts)
            conn.close()

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        assert result.skipped_heartbeat_alive == 2
        assert result.executing_swept == 0

    # --- Cell 2: stale heartbeat → kill, classify correctly ------------------

    def test_stale_heartbeat_over_threshold_is_killed(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        UoW exceeded TTL and its last heartbeat was 300 s ago (> 120 s threshold).
        Agent is dead — sweep must kill and classify.
        """
        dispatch_ago = 1800
        # Heartbeat is after dispatch + window, so classification is kill_during_execution
        heartbeat_at = _iso_ago(dispatch_ago - DISPATCH_WINDOW_SECONDS - 120)
        self._make_uow_over_ttl(
            tmp_db,
            uow_id="uow-stale-001",
            heartbeat_at=heartbeat_at,
            dispatch_ago_seconds=dispatch_ago,
        )

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        assert result.executing_swept == 1
        assert result.skipped_heartbeat_alive == 0
        assert _get_status(tmp_db, "uow-stale-001") == "ready-for-steward"

        entries = _get_sweep_audit_entries(tmp_db, "uow-stale-001")
        assert len(entries) == 1
        note = json.loads(entries[0]["note"])
        assert note["kill_classification"] == "orphan_kill_during_execution"

    def test_stale_heartbeat_classified_as_kill_before_start_when_no_dispatch_audit(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        Stale heartbeat (> threshold), no executor_dispatch audit entry →
        falls back to orphan_kill_before_start (safe default from PR #968).
        """
        # Heartbeat 300 s ago (stale), but no dispatch audit
        conn = _open_conn(tmp_db)
        _make_executing_uow(
            conn,
            uow_id="uow-stale-nodispatch-001",
            started_at=_iso_ago(1800),
            heartbeat_at=_iso_ago(300),
        )
        conn.close()

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        assert result.executing_swept == 1
        entries = _get_sweep_audit_entries(tmp_db, "uow-stale-nodispatch-001")
        assert len(entries) == 1
        note = json.loads(entries[0]["note"])
        assert note["kill_classification"] == "orphan_kill_before_start"

    # --- Cell 3: NULL heartbeat → kill (unchanged from before PR #989) ------

    def test_null_heartbeat_is_killed(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        UoW exceeded TTL and has never written a heartbeat (NULL).
        Behaviour is identical to before PR #989: kill and classify.
        """
        self._make_uow_over_ttl(
            tmp_db,
            uow_id="uow-null-hb-001",
            heartbeat_at=None,
        )

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        assert result.executing_swept == 1
        assert result.skipped_heartbeat_alive == 0
        assert _get_status(tmp_db, "uow-null-hb-001") == "ready-for-steward"

    def test_null_heartbeat_classified_as_kill_before_start(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """NULL heartbeat → kill_before_start classification (PR #968 logic preserved)."""
        self._make_uow_over_ttl(
            tmp_db,
            uow_id="uow-null-class-001",
            heartbeat_at=None,
        )

        run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        entries = _get_sweep_audit_entries(tmp_db, "uow-null-class-001")
        assert len(entries) == 1
        note = json.loads(entries[0]["note"])
        assert note["kill_classification"] == "orphan_kill_before_start"

    # --- Cell 4: under TTL → not killed (regression) ------------------------

    def test_under_ttl_not_killed_regardless_of_heartbeat(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        A UoW that has NOT exceeded the TTL must never be killed, even if its
        heartbeat is stale. This is the baseline regression guard.
        """
        conn = _open_conn(tmp_db)
        _make_executing_uow(
            conn,
            uow_id="uow-young-001",
            started_at=_iso_ago(120),   # Only 2 minutes old — under 600 s TTL
            heartbeat_at=_iso_ago(300), # Stale heartbeat, but TTL hasn't fired
        )
        conn.close()

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        assert result.executing_swept == 0
        assert result.skipped_heartbeat_alive == 0
        assert _get_status(tmp_db, "uow-young-001") == "executing"
        assert len(_get_sweep_audit_entries(tmp_db, "uow-young-001")) == 0

    # --- Gate disable: threshold=0 -------------------------------------------

    def test_heartbeat_skip_threshold_zero_disables_gate(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        heartbeat_skip_threshold_seconds=0 disables the gate entirely.
        A UoW with a very recent heartbeat is killed (gate not in effect).
        """
        self._make_uow_over_ttl(
            tmp_db,
            uow_id="uow-gate-disabled-001",
            heartbeat_at=_iso_ago(5),  # Very recent — would be skipped if gate were on
        )

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=0,  # Gate disabled
        )

        assert result.executing_swept == 1
        assert result.skipped_heartbeat_alive == 0
        assert _get_status(tmp_db, "uow-gate-disabled-001") == "ready-for-steward"

    # --- Mixed population: gate selects correctly ---------------------------

    def test_mixed_population_gate_selects_alive_and_dead(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        Two executing UoWs both over TTL:
          - "alive": recent heartbeat → skipped
          - "dead": NULL heartbeat → killed

        The gate must select the correct one in each direction.
        """
        # Use distinct source_issue_numbers to avoid UNIQUE(source_issue_number, sweep_date)
        dispatch_ts = _iso_ago(1800)
        conn = _open_conn(tmp_db)
        _make_executing_uow(
            conn,
            uow_id="uow-mixed-alive-001",
            started_at=_iso_ago(1830),
            heartbeat_at=_iso_ago(30),
            source_issue_number=201,
        )
        _insert_executor_dispatch_audit(conn, "uow-mixed-alive-001", dispatch_ts)
        _make_executing_uow(
            conn,
            uow_id="uow-mixed-dead-001",
            started_at=_iso_ago(1830),
            heartbeat_at=None,
            source_issue_number=202,
        )
        _insert_executor_dispatch_audit(conn, "uow-mixed-dead-001", dispatch_ts)
        conn.close()

        result = run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        assert result.executing_swept == 1
        assert result.skipped_heartbeat_alive == 1

        assert _get_status(tmp_db, "uow-mixed-alive-001") == "executing"
        assert _get_status(tmp_db, "uow-mixed-dead-001") == "ready-for-steward"

    # --- PR #968 classification preserved when kill does occur ---------------

    def test_kill_classification_present_in_audit_note_for_stale_heartbeat(
        self, registry: Registry, tmp_db: Path
    ) -> None:
        """
        When a kill DOES occur (stale heartbeat), the PR #968 kill_classification
        field must still be present in the audit note.
        """
        conn = _open_conn(tmp_db)
        _make_executing_uow(
            conn,
            uow_id="uow-classify-stale-001",
            started_at=_iso_ago(1800),
            heartbeat_at=_iso_ago(300),  # Stale
        )
        conn.close()

        run_startup_sweep(
            registry,
            executing_orphan_threshold_seconds=600,
            heartbeat_skip_threshold_seconds=120,
        )

        entries = _get_sweep_audit_entries(tmp_db, "uow-classify-stale-001")
        assert len(entries) == 1
        note = json.loads(entries[0]["note"])
        assert "kill_classification" in note, (
            "kill_classification from PR #968 must still be present when heartbeat is stale"
        )
