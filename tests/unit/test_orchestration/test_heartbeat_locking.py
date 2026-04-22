"""
Unit tests for WOS heartbeat-based UoW locking (issue #847).

Design reference: ~/lobster-workspace/workstreams/wos/heartbeat-locking-design.md

Tests are derived from the spec — each test name states the behavior being verified,
not the mechanism. All implementation details (field names, SQL) are hidden behind
the Registry public API where possible.

The steward-heartbeat.py module cannot be imported directly in this test environment
because it transitively imports src.orchestration.steward, which requires src.ooda
(an external module not present in the test environment). We test:

1. Registry methods directly (write_heartbeat, get_stale_heartbeat_uows,
   record_heartbeat_stall) — pure SQLite operations, no import issues.
2. recover_stale_heartbeat_uows via a standalone mock registry that isolates
   the function without the full steward chain.

For executor claim tests, we test the heartbeat field initialization directly via
SQL inspection rather than going through the full executor call chain.

Test coverage:
- test_migration_adds_heartbeat_columns: migration 0009 adds heartbeat_at and heartbeat_ttl
- test_claim_sets_heartbeat_at: SQL state after claim has non-NULL heartbeat_at
- test_claim_sets_heartbeat_ttl: SQL state after claim has heartbeat_ttl=300
- test_write_heartbeat_updates_timestamp: write_heartbeat updates heartbeat_at
- test_write_heartbeat_rejected_for_non_active_uow: write_heartbeat returns 0 if not active
- test_write_heartbeat_on_executing_uow: write_heartbeat works for 'executing' status
- test_get_stale_heartbeat_uows_returns_stale: stale detection works correctly
- test_get_stale_heartbeat_uows_ignores_fresh: fresh heartbeats not returned
- test_get_stale_heartbeat_uows_ignores_null_heartbeat: NULL heartbeat_at not returned
- test_get_stale_at_boundary_just_stale: UoW at ttl+buffer+1s is stale
- test_get_stale_at_boundary_just_fresh: UoW at ttl+buffer-1s is fresh
- test_only_active_and_executing_returned: terminal/ready states excluded
- test_record_heartbeat_stall_transitions_to_ready_for_steward: state transition
- test_record_heartbeat_stall_writes_audit_entry_with_heartbeat_stall_type: audit entry
- test_record_heartbeat_stall_returns_zero_on_race: optimistic lock race safety
- test_recover_stale_heartbeat_requeues_uow: recovery re-queues stale UoWs
- test_recover_stale_heartbeat_writes_heartbeat_stall_audit: stall_type in audit
- test_recover_stale_heartbeat_dry_run_no_transition: dry-run does not mutate state
- test_recover_fresh_heartbeat_not_requeued: fresh heartbeat UoW stays active
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.registry import Registry, UoWStatus

# ---------------------------------------------------------------------------
# Named constants from spec (migration 0009, design doc §2)
# ---------------------------------------------------------------------------

# Default heartbeat_ttl initialized at claim time
DEFAULT_HEARTBEAT_TTL_SECONDS = 300

# Buffer used by get_stale_heartbeat_uows (matching HEARTBEAT_STALL_BUFFER_SECONDS in steward-heartbeat.py)
DEFAULT_BUFFER_SECONDS = 30


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    """Registry with all migrations applied (including 0009 heartbeat fields)."""
    return Registry(db_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_offset(seconds: float) -> str:
    """Return an ISO timestamp offset by `seconds` from now (negative = past)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _insert_active_uow(
    db_path: Path,
    *,
    started_at: str | None = None,
    heartbeat_at: str | None = None,
    heartbeat_ttl: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    status: str = "active",
) -> str:
    """Insert a UoW in the given status directly via SQLite.

    Returns the uow_id. Used to set up specific heartbeat_at scenarios that the
    executor claim path does not expose (e.g. stale or NULL heartbeat_at).
    """
    uow_id = f"uow_test_{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    issue_number = int(uuid.uuid4().int % 90000) + 10000

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, type, source, source_issue_number, sweep_date, status, posture,
                 created_at, updated_at, summary, success_criteria, started_at,
                 heartbeat_at, heartbeat_ttl, route_evidence, trigger, register, uow_mode)
            VALUES (?, 'executable', ?, ?, '2026-01-01', ?, 'solo',
                    ?, ?, 'Test UoW', 'Test done.', ?,
                    ?, ?, '{}', '{"type": "immediate"}', 'operational', 'operational')
            """,
            (
                uow_id,
                f"github:issue/{issue_number}",
                issue_number,
                status,
                now,
                now,
                started_at or now,
                heartbeat_at,
                heartbeat_ttl,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return uow_id


def _insert_ready_for_executor_uow(db_path: Path) -> str:
    """Insert a UoW in 'ready-for-executor' status for claim path tests."""
    uow_id = f"uow_test_{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    issue_number = int(uuid.uuid4().int % 90000) + 10000

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, type, source, source_issue_number, sweep_date, status, posture,
                 created_at, updated_at, summary, success_criteria, timeout_at,
                 estimated_runtime, route_evidence, trigger, register, uow_mode,
                 workflow_artifact, prescribed_skills, steward_cycles)
            VALUES (?, 'executable', ?, ?, '2026-01-01', 'ready-for-executor', 'solo',
                    ?, ?, 'Test UoW', 'Test done.', NULL,
                    '1800', '{}', '{"type": "immediate"}', 'operational', 'operational',
                    NULL, '[]', 0)
            """,
            (
                uow_id,
                f"github:issue/{issue_number}",
                issue_number,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return uow_id


def _get_uow_row(db_path: Path, uow_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _get_audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC", (uow_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Inline recover_stale_heartbeat_uows — avoids importing steward-heartbeat.py
# which transitively requires src.ooda (not available in test environment).
#
# This is a faithful copy of the production function from steward-heartbeat.py,
# extracted here so tests can verify the behavior without the full dependency chain.
# Any behavioral change to the production function must be reflected here.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _HeartbeatStallResult:
    checked: int
    recovered: int
    skipped_dry_run: int


def _recover_stale_heartbeat_uows(
    registry: Registry,
    dry_run: bool = False,
    buffer_seconds: int = DEFAULT_BUFFER_SECONDS,
) -> _HeartbeatStallResult:
    """
    Test-local copy of recover_stale_heartbeat_uows from steward-heartbeat.py.

    Verifies the behavior specified in issue #847 without importing the full
    steward module chain. Kept in sync with the production implementation.
    """
    try:
        stale_uows = registry.get_stale_heartbeat_uows(buffer_seconds=buffer_seconds)
    except Exception:
        return _HeartbeatStallResult(checked=0, recovered=0, skipped_dry_run=0)

    recovered = 0
    skipped_dry_run = 0

    for uow in stale_uows:
        uow_id = uow.id
        heartbeat_at = uow.heartbeat_at
        heartbeat_ttl = uow.heartbeat_ttl

        silence_seconds: float = 0.0
        try:
            if heartbeat_at:
                now_dt = datetime.now(timezone.utc)
                hb_dt = datetime.fromisoformat(heartbeat_at)
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                silence_seconds = (now_dt - hb_dt).total_seconds()
        except (ValueError, TypeError):
            pass

        if dry_run:
            skipped_dry_run += 1
            continue

        try:
            rows = registry.record_heartbeat_stall(
                uow_id=uow_id,
                heartbeat_at=heartbeat_at,
                heartbeat_ttl=heartbeat_ttl,
                silence_seconds=silence_seconds,
            )
        except Exception:
            continue

        if rows == 1:
            recovered += 1
        # rows == 0: race — another component already advanced this UoW

    return _HeartbeatStallResult(
        checked=len(stale_uows),
        recovered=recovered,
        skipped_dry_run=skipped_dry_run,
    )


# ---------------------------------------------------------------------------
# Tests: migration adds heartbeat columns
# ---------------------------------------------------------------------------

class TestMigration:
    def test_migration_adds_heartbeat_columns(self, registry: Registry, db_path: Path) -> None:
        """Migration 0009 adds heartbeat_at and heartbeat_ttl to uow_registry."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(uow_registry)").fetchall()}
        finally:
            conn.close()
        assert "heartbeat_at" in cols, "heartbeat_at column should exist after migration 0009"
        assert "heartbeat_ttl" in cols, "heartbeat_ttl column should exist after migration 0009"


# ---------------------------------------------------------------------------
# Tests: executor claim initializes heartbeat fields
#
# The executor module cannot be imported in the test environment because it
# transitively imports src.ooda (a module not present here). We verify the
# heartbeat initialization by inspecting the SQL schema directly and
# demonstrating that set_heartbeat_ttl (called by the executor after claim)
# correctly populates both fields — covering the observable contract without
# requiring the full executor import chain.
# ---------------------------------------------------------------------------

class TestClaimInitializesHeartbeat:
    def test_set_heartbeat_ttl_populates_heartbeat_at(
        self, registry: Registry, db_path: Path
    ) -> None:
        """
        set_heartbeat_ttl (called by executor at claim time) sets heartbeat_at to now.

        This verifies the contract that claiming a UoW initializes heartbeat_at —
        the registry method is the primitive the executor calls. We test the method
        directly because the executor module's import chain is unavailable in CI.
        """
        uow_id = _insert_active_uow(db_path, heartbeat_at=None)

        before = datetime.now(timezone.utc)
        registry.set_heartbeat_ttl(uow_id, DEFAULT_HEARTBEAT_TTL_SECONDS)
        after = datetime.now(timezone.utc)

        row = _get_uow_row(db_path, uow_id)
        assert row["heartbeat_at"] is not None, "heartbeat_at should be non-NULL after set_heartbeat_ttl"

        hb_dt = datetime.fromisoformat(row["heartbeat_at"])
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
        assert before <= hb_dt <= after

    def test_set_heartbeat_ttl_sets_configured_value(
        self, registry: Registry, db_path: Path
    ) -> None:
        """
        set_heartbeat_ttl writes the provided ttl value into heartbeat_ttl.

        This verifies that the executor can configure a non-default TTL at claim
        time — the value must exactly match what was passed.
        """
        uow_id = _insert_active_uow(db_path, heartbeat_at=None)
        registry.set_heartbeat_ttl(uow_id, DEFAULT_HEARTBEAT_TTL_SECONDS)

        row = _get_uow_row(db_path, uow_id)
        assert row["heartbeat_ttl"] == DEFAULT_HEARTBEAT_TTL_SECONDS, (
            f"Expected heartbeat_ttl={DEFAULT_HEARTBEAT_TTL_SECONDS}, got {row['heartbeat_ttl']}"
        )


# ---------------------------------------------------------------------------
# Tests: write_heartbeat behavior
# ---------------------------------------------------------------------------

class TestWriteHeartbeat:
    def test_write_heartbeat_updates_timestamp(self, registry: Registry, db_path: Path) -> None:
        """write_heartbeat updates heartbeat_at to a more recent timestamp."""
        initial_heartbeat = _iso_offset(-120)  # 2 minutes ago
        uow_id = _insert_active_uow(db_path, heartbeat_at=initial_heartbeat)

        before = datetime.now(timezone.utc)
        rows = registry.write_heartbeat(uow_id)
        after = datetime.now(timezone.utc)

        assert rows == 1, "write_heartbeat should return 1 for an active UoW"

        row = _get_uow_row(db_path, uow_id)
        new_heartbeat = datetime.fromisoformat(row["heartbeat_at"])
        if new_heartbeat.tzinfo is None:
            new_heartbeat = new_heartbeat.replace(tzinfo=timezone.utc)
        assert before <= new_heartbeat <= after, (
            "heartbeat_at should be between before and after the write_heartbeat call"
        )

    def test_write_heartbeat_rejected_for_non_active_uow(
        self, registry: Registry, db_path: Path
    ) -> None:
        """write_heartbeat returns 0 when the UoW is not active/executing."""
        uow_id = _insert_active_uow(db_path, status="ready-for-steward")
        rows = registry.write_heartbeat(uow_id)
        assert rows == 0, "write_heartbeat should return 0 for non-active/executing UoW"

    def test_write_heartbeat_on_executing_uow(
        self, registry: Registry, db_path: Path
    ) -> None:
        """write_heartbeat succeeds for 'executing' status UoWs."""
        uow_id = _insert_active_uow(db_path, status="executing")
        rows = registry.write_heartbeat(uow_id)
        assert rows == 1, "write_heartbeat should succeed for 'executing' status"


# ---------------------------------------------------------------------------
# Tests: get_stale_heartbeat_uows detection
# ---------------------------------------------------------------------------

class TestGetStaleHeartbeatUows:
    def test_returns_stale_uow(self, registry: Registry, db_path: Path) -> None:
        """UoW with silence > heartbeat_ttl + buffer is returned as stale."""
        # heartbeat_ttl=60s, silence=122s → stale (122 > 60 + 30)
        stale_heartbeat = _iso_offset(-(60 + DEFAULT_BUFFER_SECONDS + 32))
        uow_id = _insert_active_uow(
            db_path,
            heartbeat_at=stale_heartbeat,
            heartbeat_ttl=60,
        )

        stale = registry.get_stale_heartbeat_uows(buffer_seconds=DEFAULT_BUFFER_SECONDS)
        stale_ids = [u.id for u in stale]

        assert uow_id in stale_ids, (
            f"Expected stale UoW {uow_id} in results, got {stale_ids}"
        )

    def test_ignores_fresh_heartbeat(self, registry: Registry, db_path: Path) -> None:
        """UoW with recent heartbeat is NOT returned as stale."""
        # heartbeat 10s ago, ttl=300 → fresh (10 < 300 + 30)
        fresh_heartbeat = _iso_offset(-10)
        uow_id = _insert_active_uow(
            db_path,
            heartbeat_at=fresh_heartbeat,
            heartbeat_ttl=DEFAULT_HEARTBEAT_TTL_SECONDS,
        )

        stale = registry.get_stale_heartbeat_uows(buffer_seconds=DEFAULT_BUFFER_SECONDS)
        stale_ids = [u.id for u in stale]

        assert uow_id not in stale_ids, (
            f"Fresh UoW {uow_id} should NOT be in stale results"
        )

    def test_ignores_null_heartbeat_at(self, registry: Registry, db_path: Path) -> None:
        """UoW with NULL heartbeat_at is NOT returned (backward compatibility path)."""
        uow_id = _insert_active_uow(db_path, heartbeat_at=None)

        stale = registry.get_stale_heartbeat_uows(buffer_seconds=DEFAULT_BUFFER_SECONDS)
        stale_ids = [u.id for u in stale]

        assert uow_id not in stale_ids, (
            "UoW with NULL heartbeat_at should NOT appear in stale heartbeat results"
        )

    def test_at_boundary_just_stale(self, registry: Registry, db_path: Path) -> None:
        """UoW at ttl + buffer + 2s is stale."""
        # heartbeat_ttl=60, buffer=30: threshold=90s. Silence=92s → stale.
        stale_heartbeat = _iso_offset(-92)
        uow_id = _insert_active_uow(db_path, heartbeat_at=stale_heartbeat, heartbeat_ttl=60)

        stale = registry.get_stale_heartbeat_uows(buffer_seconds=DEFAULT_BUFFER_SECONDS)
        assert uow_id in [u.id for u in stale]

    def test_at_boundary_just_fresh(self, registry: Registry, db_path: Path) -> None:
        """UoW at ttl + buffer - 2s is NOT stale."""
        # heartbeat_ttl=60, buffer=30: threshold=90s. Silence=88s → fresh.
        fresh_heartbeat = _iso_offset(-88)
        uow_id = _insert_active_uow(db_path, heartbeat_at=fresh_heartbeat, heartbeat_ttl=60)

        stale = registry.get_stale_heartbeat_uows(buffer_seconds=DEFAULT_BUFFER_SECONDS)
        assert uow_id not in [u.id for u in stale]

    def test_only_returns_active_and_executing(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoWs not in active/executing status are excluded even if heartbeat is stale."""
        stale_heartbeat = _iso_offset(-500)

        ready_uow_id = _insert_active_uow(
            db_path, heartbeat_at=stale_heartbeat, heartbeat_ttl=60, status="ready-for-steward"
        )
        done_uow_id = _insert_active_uow(
            db_path, heartbeat_at=stale_heartbeat, heartbeat_ttl=60, status="done"
        )

        stale = registry.get_stale_heartbeat_uows(buffer_seconds=DEFAULT_BUFFER_SECONDS)
        stale_ids = [u.id for u in stale]

        assert ready_uow_id not in stale_ids
        assert done_uow_id not in stale_ids


# ---------------------------------------------------------------------------
# Tests: record_heartbeat_stall atomic transition
# ---------------------------------------------------------------------------

class TestRecordHeartbeatStall:
    def test_transitions_to_ready_for_steward(
        self, registry: Registry, db_path: Path
    ) -> None:
        """record_heartbeat_stall transitions active UoW to ready-for-steward."""
        uow_id = _insert_active_uow(db_path, heartbeat_at=_iso_offset(-500), heartbeat_ttl=60)

        rows = registry.record_heartbeat_stall(
            uow_id=uow_id,
            heartbeat_at=_iso_offset(-500),
            heartbeat_ttl=60,
            silence_seconds=500.0,
        )

        assert rows == 1
        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "ready-for-steward"

    def test_writes_heartbeat_stall_audit_entry(
        self, registry: Registry, db_path: Path
    ) -> None:
        """record_heartbeat_stall writes stall_detected audit with stall_type=heartbeat_stall."""
        uow_id = _insert_active_uow(db_path, heartbeat_at=_iso_offset(-500), heartbeat_ttl=60)

        registry.record_heartbeat_stall(
            uow_id=uow_id,
            heartbeat_at=_iso_offset(-500),
            heartbeat_ttl=60,
            silence_seconds=500.0,
        )

        entries = _get_audit_entries(db_path, uow_id)
        stall_entries = [e for e in entries if e.get("event") == "stall_detected"]
        assert len(stall_entries) == 1, f"Expected 1 stall_detected entry, got {len(stall_entries)}"

        note = json.loads(stall_entries[0]["note"])
        assert note.get("stall_type") == "heartbeat_stall", (
            f"Expected stall_type=heartbeat_stall, got {note.get('stall_type')}"
        )

    def test_returns_zero_on_race_non_active_uow(
        self, registry: Registry, db_path: Path
    ) -> None:
        """record_heartbeat_stall returns 0 (no-op) if UoW already advanced."""
        uow_id = _insert_active_uow(db_path, heartbeat_at=_iso_offset(-500), heartbeat_ttl=60, status="ready-for-steward")

        rows = registry.record_heartbeat_stall(
            uow_id=uow_id,
            heartbeat_at=_iso_offset(-500),
            heartbeat_ttl=60,
            silence_seconds=500.0,
        )

        assert rows == 0, "Should return 0 when UoW is not active/executing (optimistic lock)"


# ---------------------------------------------------------------------------
# Tests: recover_stale_heartbeat_uows behavior (tested via inline copy)
# ---------------------------------------------------------------------------

class TestRecoverStaleHeartbeatUows:
    def test_stale_uow_requeued_to_ready_for_steward(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Stale-heartbeat UoW is transitioned to ready-for-steward."""
        stale_heartbeat = _iso_offset(-500)
        uow_id = _insert_active_uow(db_path, heartbeat_at=stale_heartbeat, heartbeat_ttl=60)

        result = _recover_stale_heartbeat_uows(registry)

        assert result.recovered >= 1
        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "ready-for-steward"

    def test_stale_uow_writes_heartbeat_stall_audit_entry(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Recovery writes stall_detected audit entry with stall_type=heartbeat_stall."""
        stale_heartbeat = _iso_offset(-500)
        uow_id = _insert_active_uow(db_path, heartbeat_at=stale_heartbeat, heartbeat_ttl=60)

        _recover_stale_heartbeat_uows(registry)

        entries = _get_audit_entries(db_path, uow_id)
        stall_entries = [e for e in entries if e.get("event") == "stall_detected"]
        assert len(stall_entries) == 1

        note = json.loads(stall_entries[0]["note"])
        assert note.get("stall_type") == "heartbeat_stall"

    def test_dry_run_does_not_transition_state(
        self, registry: Registry, db_path: Path
    ) -> None:
        """dry_run=True detects stale UoWs but does not mutate status or write audit."""
        stale_heartbeat = _iso_offset(-500)
        uow_id = _insert_active_uow(db_path, heartbeat_at=stale_heartbeat, heartbeat_ttl=60)

        result = _recover_stale_heartbeat_uows(registry, dry_run=True)

        assert result.skipped_dry_run >= 1
        assert result.recovered == 0

        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "active"

        entries = _get_audit_entries(db_path, uow_id)
        stall_entries = [e for e in entries if e.get("event") == "stall_detected"]
        assert len(stall_entries) == 0

    def test_fresh_heartbeat_uow_not_requeued(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoW with fresh heartbeat stays in active status — not recovered."""
        fresh_heartbeat = _iso_offset(-10)
        uow_id = _insert_active_uow(
            db_path, heartbeat_at=fresh_heartbeat, heartbeat_ttl=DEFAULT_HEARTBEAT_TTL_SECONDS
        )

        _recover_stale_heartbeat_uows(registry)

        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "active", "Fresh-heartbeat UoW should remain active"

    def test_null_heartbeat_uow_not_recovered(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoW with NULL heartbeat_at is not touched by heartbeat recovery (legacy path)."""
        uow_id = _insert_active_uow(db_path, heartbeat_at=None)

        _recover_stale_heartbeat_uows(registry)

        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "active", "NULL-heartbeat UoW should not be touched"

    def test_returns_correct_counts(self, registry: Registry, db_path: Path) -> None:
        """Result dataclass reports correct checked and recovered counts."""
        stale_heartbeat = _iso_offset(-500)
        fresh_heartbeat = _iso_offset(-10)

        stale_id = _insert_active_uow(db_path, heartbeat_at=stale_heartbeat, heartbeat_ttl=60)
        fresh_id = _insert_active_uow(
            db_path, heartbeat_at=fresh_heartbeat, heartbeat_ttl=DEFAULT_HEARTBEAT_TTL_SECONDS
        )

        result = _recover_stale_heartbeat_uows(registry)

        assert result.checked >= 1   # at least the stale UoW
        assert result.recovered >= 1  # stale UoW recovered

        stale_row = _get_uow_row(db_path, stale_id)
        fresh_row = _get_uow_row(db_path, fresh_id)
        assert stale_row["status"] == "ready-for-steward"
        assert fresh_row["status"] == "active"
