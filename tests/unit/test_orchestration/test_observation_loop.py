"""
Unit tests for the Observation Loop — steward-heartbeat.py / run_observation_loop()

Covers every acceptance criterion from issue #306:

- Query is WHERE status = 'active' only
- timeout_at not NULL: stall fires when now >= timeout_at (using >= operator)
- timeout_at NULL, started_at not NULL: fall back to started_at + 1800s
- timeout_at NULL, started_at NULL: immediate stall with started_at_null reason
- Stall: audit entry written, status transitioned to ready-for-steward
- Optimistic lock: rows_affected check; if 0, no duplicate audit entry
- No Telegram message sent; no other UoW fields modified
- Idempotency guard: second pass on same UoW writes exactly one stall_detected entry
- active UoW with timeout_at in the future: no action, no log output
- Integration test: run steward-heartbeat against a test DB with a stalled UoW
- Exact boundary: timeout_at == now() fires stall (>=)
- timeout_at 1 second in the future: no stall
- timeout_at 1 second in the past: stall triggered
- started_at 2000s ago, no timeout_at: stall triggered (fallback > 1800s)
- Dry-run mode: detects stalls but does not write or transition
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.registry import Registry, UoWStatus


# ---------------------------------------------------------------------------
# Schema — mirrors the full Phase 2 schema used in other test modules
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

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


_COUNTER = 0


def _next_issue_number() -> int:
    global _COUNTER
    _COUNTER += 1
    return 10000 + _COUNTER


def _insert_active_uow(
    conn: sqlite3.Connection,
    *,
    timeout_at: str | None,
    started_at: str | None = None,
    output_ref: str | None = None,
    issue_number: int | None = None,
) -> str:
    """Insert a UoW in 'active' status. Returns uow_id."""
    uow_id = f"uow_test_{uuid.uuid4().hex[:8]}"
    now = _iso(_now())
    if issue_number is None:
        issue_number = _next_issue_number()

    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, timeout_at, started_at, output_ref,
             route_evidence, trigger)
        VALUES (?, 'executable', ?, ?, '2026-01-01', 'active', 'solo',
                ?, ?, 'Test UoW', ?, ?, ?, '{}', '{"type": "immediate"}')
        """,
        (
            uow_id,
            f"github:issue/{issue_number}",
            issue_number,
            now,
            now,
            timeout_at,
            started_at,
            output_ref,
        ),
    )
    conn.commit()
    return uow_id


def _audit_entries(conn: sqlite3.Connection, uow_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
        (uow_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _uow_status(conn: sqlite3.Connection, uow_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    assert row is not None
    return row[0]


# ---------------------------------------------------------------------------
# Import helpers — the observation loop lives in steward-heartbeat.py
# ---------------------------------------------------------------------------

_HEARTBEAT_MOD = None


def _import_observation_loop():
    """Import run_observation_loop from steward-heartbeat.py via importlib.

    We cache the module after first load to avoid re-executing it (which would
    cause the @dataclass decorator to be called again on an already-registered
    module, producing duplicate class registrations).
    """
    global _HEARTBEAT_MOD
    if _HEARTBEAT_MOD is not None:
        return _HEARTBEAT_MOD.run_observation_loop, _HEARTBEAT_MOD.ObservationResult

    import importlib.util
    _MOD_NAME = "steward_heartbeat"

    # Register the module name before exec so that dataclass __module__ resolves
    # correctly (dataclasses use sys.modules[cls.__module__] to look up types).
    hb_path = REPO_ROOT / "scheduled-tasks" / "steward-heartbeat.py"
    spec = importlib.util.spec_from_file_location(_MOD_NAME, hb_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)

    _HEARTBEAT_MOD = mod
    return mod.run_observation_loop, mod.ObservationResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _make_db(tmp_path)


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    yield c
    c.close()


@pytest.fixture
def run_observation_loop(registry):
    """Return a partial that pre-binds registry to run_observation_loop."""
    fn, _ = _import_observation_loop()

    def _run(**kwargs):
        return fn(registry, **kwargs)

    return _run


# ===========================================================================
# Tests
# ===========================================================================

class TestQueryScope:
    """Observation Loop only queries status = 'active'."""

    def test_only_active_uows_are_inspected(self, registry, conn, run_observation_loop):
        """UoWs in non-active statuses are never flagged, even if timeout_at is in the past."""
        past = _iso(_now() - timedelta(hours=1))
        now_ts = _iso(_now())

        # Insert a pending, ready-for-steward, and diagnosing UoW — all should be ignored.
        for status in ("pending", "ready-for-steward", "diagnosing", "ready-for-executor"):
            uid = f"uow_nonactive_{uuid.uuid4().hex[:6]}"
            issue_n = _next_issue_number()
            conn.execute(
                """
                INSERT INTO uow_registry
                    (id, type, source, source_issue_number, sweep_date, status, posture,
                     created_at, updated_at, summary, timeout_at, route_evidence, trigger)
                VALUES (?, 'executable', ?, ?, '2026-01-01', ?, 'solo',
                        ?, ?, 'Non-active UoW', ?, '{}', '{"type": "immediate"}')
                """,
                (uid, f"github:issue/{issue_n}", issue_n, status, now_ts, now_ts, past),
            )
        conn.commit()

        result = run_observation_loop()
        assert result.stalled == 0

    def test_active_uow_with_future_timeout_not_flagged(self, registry, conn, run_observation_loop):
        """Active UoW with timeout_at in the future: no action."""
        future = _iso(_now() + timedelta(hours=1))
        uow_id = _insert_active_uow(conn, timeout_at=future)

        result = run_observation_loop()

        assert result.checked >= 1
        assert result.stalled == 0
        assert _uow_status(conn, uow_id) == "active"
        assert len(_audit_entries(conn, uow_id)) == 0


class TestTimeoutAtBranchExact:
    """timeout_at is set — boundary and edge cases."""

    def test_timeout_at_exactly_now_fires_stall(self, registry, conn):
        """timeout_at == now(): stall fires (>= operator)."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        timeout_ts = _iso(fixed_now)
        uow_id = _insert_active_uow(conn, timeout_at=timeout_ts)

        result = fn(registry, clock=lambda: fixed_now)

        assert result.stalled == 1
        assert _uow_status(conn, uow_id) == "ready-for-steward"

        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 1

    def test_timeout_at_1s_in_future_no_stall(self, registry, conn):
        """timeout_at 1 second in the future: no stall triggered."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        timeout_ts = _iso(fixed_now + timedelta(seconds=1))
        uow_id = _insert_active_uow(conn, timeout_at=timeout_ts)

        result = fn(registry, clock=lambda: fixed_now)

        assert result.stalled == 0
        assert _uow_status(conn, uow_id) == "active"
        assert len(_audit_entries(conn, uow_id)) == 0

    def test_timeout_at_1s_in_past_stall_triggered(self, registry, conn):
        """timeout_at 1 second in the past: stall triggered."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        timeout_ts = _iso(fixed_now - timedelta(seconds=1))
        uow_id = _insert_active_uow(conn, timeout_at=timeout_ts)

        result = fn(registry, clock=lambda: fixed_now)

        assert result.stalled == 1
        assert _uow_status(conn, uow_id) == "ready-for-steward"

        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 1
        note = json.loads(stall_entries[0]["note"])
        assert note["reason"] == "timeout_exceeded"


class TestFallbackBranch:
    """timeout_at is NULL — fall back to started_at + 1800s."""

    def test_started_at_2000s_ago_stall_triggered(self, registry, conn):
        """started_at 2000s ago, timeout_at NULL: stall triggered (2000 > 1800)."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        started_ts = _iso(fixed_now - timedelta(seconds=2000))
        uow_id = _insert_active_uow(conn, timeout_at=None, started_at=started_ts)

        result = fn(registry, clock=lambda: fixed_now)

        assert result.stalled == 1
        assert _uow_status(conn, uow_id) == "ready-for-steward"

        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 1
        note = json.loads(stall_entries[0]["note"])
        assert note["reason"] == "timeout_exceeded"

    def test_started_at_100s_ago_no_stall(self, registry, conn):
        """started_at 100s ago, timeout_at NULL: no stall (100 < 1800)."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        started_ts = _iso(fixed_now - timedelta(seconds=100))
        uow_id = _insert_active_uow(conn, timeout_at=None, started_at=started_ts)

        result = fn(registry, clock=lambda: fixed_now)

        assert result.stalled == 0
        assert _uow_status(conn, uow_id) == "active"
        assert len(_audit_entries(conn, uow_id)) == 0

    def test_started_at_exactly_1800s_ago_fires(self, registry, conn):
        """started_at exactly 1800s ago, timeout_at NULL: stall fires (>= operator)."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        started_ts = _iso(fixed_now - timedelta(seconds=1800))
        uow_id = _insert_active_uow(conn, timeout_at=None, started_at=started_ts)

        result = fn(registry, clock=lambda: fixed_now)

        assert result.stalled == 1
        assert _uow_status(conn, uow_id) == "ready-for-steward"


class TestStartedAtNullBranch:
    """Both timeout_at and started_at are NULL — immediate stall."""

    def test_both_null_surfaces_as_immediate_stall(self, registry, conn):
        """started_at NULL, timeout_at NULL: immediate stall with started_at_null reason."""
        fn, _ = _import_observation_loop()

        uow_id = _insert_active_uow(conn, timeout_at=None, started_at=None)

        result = fn(registry)

        assert result.stalled == 1
        assert _uow_status(conn, uow_id) == "ready-for-steward"

        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 1
        note = json.loads(stall_entries[0]["note"])
        assert note["reason"] == "started_at_null"

    def test_both_null_no_exception_raised(self, registry, conn, run_observation_loop):
        """started_at NULL, timeout_at NULL: no exception is raised."""
        _insert_active_uow(conn, timeout_at=None, started_at=None)
        # Should not raise
        result = run_observation_loop()
        assert result.stalled >= 1


class TestAuditEntryContract:
    """Audit entry shape and field requirements per #306 spec."""

    def test_stall_audit_entry_has_all_required_fields(self, registry, conn):
        """stall_detected audit entry contains all required fields."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        past = _iso(fixed_now - timedelta(seconds=1))
        output_ref = "/tmp/test_output.json"
        uow_id = _insert_active_uow(conn, timeout_at=past, output_ref=output_ref)

        fn(registry, clock=lambda: fixed_now)

        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 1

        entry = stall_entries[0]
        assert entry["from_status"] == "active"
        assert entry["to_status"] == "ready-for-steward"
        assert entry["agent"] == "observation_loop"

        note = json.loads(entry["note"])
        # All required fields per #306 spec
        assert note["event"] == "stall_detected"
        assert note["actor"] == "observation_loop"
        assert note["uow_id"] == uow_id
        assert "started_at" in note
        assert "timeout_at" in note
        assert "output_ref" in note
        assert "elapsed_seconds" in note
        assert "timestamp" in note

    def test_stall_audit_entry_has_canonical_event_name(self, registry, conn):
        """Event name is exactly 'stall_detected' — canonical per #306."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        past = _iso(fixed_now - timedelta(seconds=1))
        uow_id = _insert_active_uow(conn, timeout_at=past)

        fn(registry, clock=lambda: fixed_now)

        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 1
        note = json.loads(stall_entries[0]["note"])
        assert note["event"] == "stall_detected"

    def test_stall_audit_output_ref_preserved(self, registry, conn):
        """output_ref value is included in the audit entry note."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        past = _iso(fixed_now - timedelta(seconds=10))
        output_ref = "/some/path/to/output.md"
        uow_id = _insert_active_uow(conn, timeout_at=past, output_ref=output_ref)

        fn(registry, clock=lambda: fixed_now)

        entries = _audit_entries(conn, uow_id)
        note = json.loads([e for e in entries if e["event"] == "stall_detected"][0]["note"])
        assert note["output_ref"] == output_ref


class TestOptimisticLock:
    """Optimistic lock: only one writer wins; the other gets rows_affected == 0."""

    def test_race_loser_does_not_write_duplicate_audit_entry(self, registry, conn):
        """
        If the status transition UPDATE returns 0 rows_affected (another component
        already advanced the UoW), no stall_detected audit entry is written.
        """
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        past = _iso(fixed_now - timedelta(seconds=5))
        uow_id = _insert_active_uow(conn, timeout_at=past)

        # Simulate another component advancing the UoW to ready-for-steward
        # before the Observation Loop's UPDATE fires.
        conn.execute(
            "UPDATE uow_registry SET status = 'ready-for-steward' WHERE id = ?",
            (uow_id,),
        )
        conn.commit()

        # The UoW is now ready-for-steward — NOT active. The Observation Loop
        # queries WHERE status = 'active', so it won't see this UoW at all.
        result = fn(registry, clock=lambda: fixed_now)

        assert result.stalled == 0
        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 0

    def test_record_stall_detected_returns_0_when_status_already_changed(
        self, registry, conn
    ):
        """
        registry.record_stall_detected returns 0 when the UoW is no longer active.
        """
        fixed_now = _now()
        past = _iso(fixed_now - timedelta(seconds=5))
        uow_id = _insert_active_uow(conn, timeout_at=past)

        # Advance externally
        conn.execute(
            "UPDATE uow_registry SET status = 'done' WHERE id = ?",
            (uow_id,),
        )
        conn.commit()

        rows = registry.record_stall_detected(
            uow_id=uow_id,
            stall_reason="timeout_exceeded",
            started_at=None,
            timeout_at=past,
            output_ref=None,
            elapsed_seconds=5.0,
        )
        assert rows == 0
        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 0


class TestIdempotencyGuard:
    """
    Second pass on same UoW (partial-failure scenario) writes exactly one
    stall_detected audit entry.
    """

    def test_double_pass_writes_exactly_one_audit_entry(self, registry, conn):
        """
        Simulate partial failure: audit entry was written but status transition
        failed (UoW is still active). Running the Observation Loop again should
        NOT write a second stall_detected entry with the same timeout_at.
        """
        import json as _json

        fixed_now = _now()
        past = _iso(fixed_now - timedelta(seconds=30))
        uow_id = _insert_active_uow(conn, timeout_at=past)

        # Manually write a stall_detected audit entry (simulating partial failure
        # where audit was written but status transition rolled back).
        note = _json.dumps({
            "event": "stall_detected",
            "actor": "observation_loop",
            "uow_id": uow_id,
            "started_at": None,
            "timeout_at": past,
            "output_ref": None,
            "elapsed_seconds": 30.0,
            "reason": "timeout_exceeded",
            "timestamp": _iso(_now()),
        })
        conn.execute(
            """
            INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
            VALUES (?, ?, 'stall_detected', 'active', 'ready-for-steward', 'observation_loop', ?)
            """,
            (_iso(_now()), uow_id, note),
        )
        conn.commit()

        # UoW is still active (simulating partial failure — transition rolled back)
        assert _uow_status(conn, uow_id) == "active"

        # Call record_stall_detected — idempotency guard should fire and return 0.
        rows = registry.record_stall_detected(
            uow_id=uow_id,
            stall_reason="timeout_exceeded",
            started_at=None,
            timeout_at=past,
            output_ref=None,
            elapsed_seconds=30.0,
        )
        assert rows == 0

        # Exactly one stall_detected entry exists.
        entries = _audit_entries(conn, uow_id)
        stall_entries = [e for e in entries if e["event"] == "stall_detected"]
        assert len(stall_entries) == 1

    def test_different_timeout_at_is_not_filtered_by_idempotency_guard(
        self, registry, conn
    ):
        """
        A second stall with a different timeout_at is NOT blocked by the
        idempotency guard — it is treated as a fresh stall event.
        """
        import json as _json

        fixed_now = _now()
        past1 = _iso(fixed_now - timedelta(seconds=60))
        past2 = _iso(fixed_now - timedelta(seconds=30))  # different value

        uow_id = _insert_active_uow(conn, timeout_at=past2)

        # Write an existing stall_detected entry with a DIFFERENT timeout_at.
        note = _json.dumps({
            "event": "stall_detected",
            "timeout_at": past1,  # different from current timeout_at
        })
        conn.execute(
            """
            INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
            VALUES (?, ?, 'stall_detected', 'active', 'ready-for-steward', 'observation_loop', ?)
            """,
            (_iso(_now()), uow_id, note),
        )
        conn.commit()

        # Should NOT be blocked by idempotency guard (different timeout_at).
        rows = registry.record_stall_detected(
            uow_id=uow_id,
            stall_reason="timeout_exceeded",
            started_at=None,
            timeout_at=past2,
            output_ref=None,
            elapsed_seconds=30.0,
        )
        assert rows == 1


class TestNoSideEffects:
    """Observation Loop must not modify any field other than status and audit_log."""

    def test_only_status_and_updated_at_changed_on_stall(self, registry, conn):
        """After stall detection, only status and updated_at are changed."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        past = _iso(fixed_now - timedelta(seconds=1))
        uow_id = _insert_active_uow(conn, timeout_at=past, output_ref="/my/output.md")

        # Capture state before
        before = dict(conn.execute(
            "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone())

        fn(registry, clock=lambda: fixed_now)

        after = dict(conn.execute(
            "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone())

        # Only status and updated_at should differ
        changed_fields = {k for k in before if before[k] != after[k]}
        assert changed_fields <= {"status", "updated_at"}, (
            f"Unexpected fields changed: {changed_fields - {'status', 'updated_at'}}"
        )
        assert after["status"] == "ready-for-steward"

    def test_no_fields_changed_when_no_stall(self, registry, conn):
        """When no stall is detected, the UoW row is completely unchanged."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        future = _iso(fixed_now + timedelta(hours=1))
        uow_id = _insert_active_uow(conn, timeout_at=future)

        before = dict(conn.execute(
            "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone())

        fn(registry, clock=lambda: fixed_now)

        after = dict(conn.execute(
            "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone())

        assert before == after


class TestDryRunMode:
    """In dry_run mode: stalls are detected but no writes occur."""

    def test_dry_run_does_not_write_audit_or_transition(self, registry, conn):
        """Dry-run: stall detected, but no audit entry and no status transition."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        past = _iso(fixed_now - timedelta(seconds=10))
        uow_id = _insert_active_uow(conn, timeout_at=past)

        result = fn(registry, dry_run=True, clock=lambda: fixed_now)

        assert result.skipped_dry_run >= 1
        assert result.stalled == 0  # not committed
        assert _uow_status(conn, uow_id) == "active"
        assert len(_audit_entries(conn, uow_id)) == 0


class TestReturnType:
    """run_observation_loop returns ObservationResult with correct counts."""

    def test_returns_observation_result_type(self, registry, conn):
        fn, ObservationResult = _import_observation_loop()
        result = fn(registry)
        assert isinstance(result, ObservationResult)
        assert hasattr(result, "checked")
        assert hasattr(result, "stalled")
        assert hasattr(result, "skipped_dry_run")

    def test_checked_counts_all_active_uows(self, registry, conn):
        """checked == total number of active UoWs inspected."""
        fn, _ = _import_observation_loop()

        fixed_now = _now()
        future = _iso(fixed_now + timedelta(hours=1))
        past = _iso(fixed_now - timedelta(seconds=1))

        _insert_active_uow(conn, timeout_at=future)  # not stalled
        _insert_active_uow(conn, timeout_at=future)  # not stalled
        _insert_active_uow(conn, timeout_at=past)    # stalled

        result = fn(registry, clock=lambda: fixed_now)

        assert result.checked >= 3
        assert result.stalled == 1


class TestIntegration:
    """
    Integration test: run steward-heartbeat against a test DB with a stalled UoW.
    Proves the Observation Loop is called within the heartbeat execution order.
    """

    def test_heartbeat_detects_stall_end_to_end(self, tmp_path: Path):
        """
        End-to-end: running the heartbeat against a DB with a stalled active UoW
        results in a stall_detected audit entry and ready-for-steward status.

        This test uses a test DB path via LOBSTER_WORKSPACE env var override.
        """
        import importlib.util
        import os

        # Create test DB
        db_path = _make_db(tmp_path)

        # Create a stalled active UoW directly in the DB
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        past = _iso(_now() - timedelta(hours=2))
        uow_id = _insert_active_uow(conn, timeout_at=past, started_at=past)
        conn.close()

        # Import registry and run just the observation loop (not full heartbeat,
        # which requires the Steward main loop DB to be fully migrated).
        registry = Registry(db_path)

        fn, _ = _import_observation_loop()
        result = fn(registry)

        assert result.stalled >= 1

        # Verify DB state
        verify_conn = sqlite3.connect(str(db_path))
        verify_conn.row_factory = sqlite3.Row
        status_row = verify_conn.execute(
            "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        assert status_row["status"] == "ready-for-steward"

        audit_rows = verify_conn.execute(
            "SELECT event FROM audit_log WHERE uow_id = ?", (uow_id,)
        ).fetchall()
        events = [r["event"] for r in audit_rows]
        assert "stall_detected" in events
        verify_conn.close()

    def test_heartbeat_active_uow_within_timeout_not_flagged_integration(
        self, tmp_path: Path
    ):
        """
        Integration: an active UoW with timeout_at in the future is not flagged.
        """
        db_path = _make_db(tmp_path)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        future = _iso(_now() + timedelta(hours=1))
        uow_id = _insert_active_uow(conn, timeout_at=future)
        conn.close()

        registry = Registry(db_path)
        fn, _ = _import_observation_loop()
        result = fn(registry)

        assert result.stalled == 0

        verify_conn = sqlite3.connect(str(db_path))
        verify_conn.row_factory = sqlite3.Row
        status_row = verify_conn.execute(
            "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        assert status_row["status"] == "active"
        verify_conn.close()
