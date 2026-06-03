"""
Unit tests for prescribing-state heartbeat-silence TTL auto-reset (issue #1388).

When a UoW is stuck in `prescribing` and its heartbeat has been silent for
PRESCRIBING_SILENCE_TTL_SECONDS (30 minutes), the steward heartbeat should
automatically reset it to `ready-for-steward` with a `heartbeat_silence_ttl`
audit event.

Tests are derived from the spec — each test name states the behavior being verified,
not the mechanism.

Test coverage:
- test_stale_prescribing_uow_returned_when_heartbeat_silent: UoW in prescribing with
  heartbeat older than 30 min IS returned by get_stale_prescribing_uows
- test_fresh_prescribing_uow_not_returned_when_heartbeat_recent: UoW in prescribing
  with recent heartbeat is NOT returned (healthy long-running case)
- test_prescribing_uow_with_null_heartbeat_returned: prescribing UoW with NULL
  heartbeat_at IS returned (null means never written — treat as stale)
- test_non_prescribing_status_not_returned: UoWs in active/executing/done are
  never returned, regardless of heartbeat age
- test_record_prescribing_silence_transitions_to_ready_for_steward: state transition
  succeeds and leaves the UoW in ready-for-steward
- test_record_prescribing_silence_writes_heartbeat_silence_ttl_audit_event: audit
  log contains heartbeat_silence_ttl event with structured payload
- test_record_prescribing_silence_returns_zero_on_race: optimistic lock prevents
  double-application when UoW has already been advanced
- test_recover_stale_prescribing_requeues_uow: recover_stale_prescribing_uows
  re-queues stale UoWs and returns correct result counts
- test_recover_stale_prescribing_dry_run_no_transition: dry-run does not mutate state
- test_recover_fresh_prescribing_not_requeued: UoW with recent heartbeat stays in
  prescribing after recovery loop runs
- test_only_prescribing_status_eligible_for_prescribing_recovery: active/executing
  UoWs with stale heartbeats are NOT touched by the prescribing recovery path
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
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
# Named constants from spec (issue #1388)
# ---------------------------------------------------------------------------

# Staleness threshold: 30 minutes of heartbeat silence triggers auto-reset
PRESCRIBING_SILENCE_TTL_SECONDS = 1800  # 30 minutes

# A buffer beyond the TTL to absorb scheduling jitter (mirrors heartbeat_stall pattern)
DEFAULT_BUFFER_SECONDS = 30


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    """Registry with all migrations applied."""
    return Registry(db_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_offset(seconds: float) -> str:
    """Return an ISO timestamp offset by `seconds` from now (negative = past)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _insert_prescribing_uow(
    db_path: Path,
    *,
    heartbeat_at: str | None = None,
    status: str = "prescribing",
    updated_at: str | None = None,
) -> str:
    """Insert a UoW in prescribing (or specified) status directly via SQLite.

    Returns the uow_id. Allows setting specific heartbeat_at/updated_at scenarios
    that the normal steward dispatch path does not expose (e.g. stale or NULL).

    When updated_at is None, uses the current time (a fresh UoW).
    Pass an old timestamp via _iso_offset() to simulate a UoW that has been
    in prescribing for longer than the TTL.
    """
    uow_id = f"uow_test_{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    effective_updated_at = updated_at if updated_at is not None else now
    issue_number = int(uuid.uuid4().int % 90000) + 10000

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, type, source, source_issue_number, sweep_date, status, posture,
                 created_at, updated_at, summary, success_criteria,
                 heartbeat_at, route_evidence, trigger, register, uow_mode)
            VALUES (?, 'executable', ?, ?, '2026-01-01', ?, 'solo',
                    ?, ?, 'Test UoW', 'Test done.',
                    ?, '{}', '{"type": "immediate"}', 'operational', 'operational')
            """,
            (
                uow_id,
                f"github:issue/{issue_number}",
                issue_number,
                status,
                now,
                effective_updated_at,
                heartbeat_at,
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
# Inline recover_stale_prescribing_uows — avoids importing steward-heartbeat.py
# which transitively requires src.ooda (not available in test environment).
#
# This is a faithful copy of the production function from steward-heartbeat.py.
# Any behavioral change to the production function must be reflected here.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _PrescribingSilenceResult:
    checked: int
    recovered: int
    skipped_dry_run: int


def _recover_stale_prescribing_uows(
    registry: Registry,
    dry_run: bool = False,
    ttl_seconds: int = PRESCRIBING_SILENCE_TTL_SECONDS,
) -> _PrescribingSilenceResult:
    """
    Test-local copy of recover_stale_prescribing_uows from steward-heartbeat.py.

    Verifies the behavior specified in issue #1388 without importing the full
    steward module chain. Kept in sync with the production implementation.
    """
    try:
        stale_uows = registry.get_stale_prescribing_uows(stale_after_seconds=ttl_seconds)
    except Exception:
        return _PrescribingSilenceResult(checked=0, recovered=0, skipped_dry_run=0)

    recovered = 0
    skipped_dry_run = 0

    for uow in stale_uows:
        uow_id = uow.id
        heartbeat_at = uow.heartbeat_at

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
            rows = registry.record_prescribing_silence_reset(
                uow_id=uow_id,
                heartbeat_at=heartbeat_at,
                silence_seconds=silence_seconds,
            )
        except Exception:
            continue

        if rows == 1:
            recovered += 1
        # rows == 0: race — another component already advanced this UoW

    return _PrescribingSilenceResult(
        checked=len(stale_uows),
        recovered=recovered,
        skipped_dry_run=skipped_dry_run,
    )


# ---------------------------------------------------------------------------
# Tests: get_stale_prescribing_uows — detection logic
# ---------------------------------------------------------------------------

class TestGetStalePrescribingUows:
    def test_stale_prescribing_uow_returned_when_heartbeat_silent(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A prescribing UoW whose heartbeat is older than the TTL is returned."""
        stale_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 60))
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=stale_heartbeat)

        result = registry.get_stale_prescribing_uows(
            stale_after_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )

        assert any(u.id == uow_id for u in result), (
            f"Expected stale prescribing UoW {uow_id} to be returned, got: "
            f"{[u.id for u in result]}"
        )

    def test_fresh_prescribing_uow_not_returned_when_heartbeat_recent(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A prescribing UoW with a recent heartbeat is NOT returned (healthy long-running case)."""
        fresh_heartbeat = _iso_offset(-60)  # 1 minute ago — well within 30-min TTL
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=fresh_heartbeat)

        result = registry.get_stale_prescribing_uows(
            stale_after_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )

        assert not any(u.id == uow_id for u in result), (
            f"Fresh prescribing UoW {uow_id} should NOT be returned, but was"
        )

    def test_prescribing_uow_with_null_heartbeat_returned_when_updated_at_is_old(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A prescribing UoW with NULL heartbeat_at and old updated_at is returned as stale.

        When no heartbeat was ever written, updated_at (when the UoW entered prescribing)
        is used as the staleness clock. A UoW that has been prescribing for more than
        stale_after_seconds without any heartbeat is treated as stale.
        """
        stale_updated_at = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 60))
        uow_id = _insert_prescribing_uow(
            db_path, heartbeat_at=None, updated_at=stale_updated_at
        )

        result = registry.get_stale_prescribing_uows(
            stale_after_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )

        assert any(u.id == uow_id for u in result), (
            f"Prescribing UoW with NULL heartbeat_at and old updated_at {uow_id} "
            f"should be returned as stale"
        )

    def test_prescribing_uow_with_null_heartbeat_not_returned_when_fresh(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A prescribing UoW with NULL heartbeat_at but fresh updated_at is NOT returned.

        The startup_sweep handles fresh NULL-heartbeat UoWs at 660s. This method
        does not fire before the 30-minute window elapses.
        """
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=None)
        # updated_at defaults to now — well within 30-minute window

        result = registry.get_stale_prescribing_uows(
            stale_after_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )

        assert not any(u.id == uow_id for u in result), (
            f"Fresh prescribing UoW with NULL heartbeat_at {uow_id} should NOT be returned"
        )

    def test_non_prescribing_status_not_returned(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoWs in non-prescribing status are not returned, regardless of heartbeat age."""
        stale_ts = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 120))
        active_id = _insert_prescribing_uow(
            db_path, heartbeat_at=stale_ts, updated_at=stale_ts, status="active"
        )
        executing_id = _insert_prescribing_uow(
            db_path, heartbeat_at=stale_ts, updated_at=stale_ts, status="executing"
        )
        rfs_id = _insert_prescribing_uow(
            db_path, heartbeat_at=stale_ts, updated_at=stale_ts, status="ready-for-steward"
        )

        result = registry.get_stale_prescribing_uows(
            stale_after_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )
        returned_ids = {u.id for u in result}

        assert active_id not in returned_ids, "active UoW should not be in stale-prescribing results"
        assert executing_id not in returned_ids, "executing UoW should not be in stale-prescribing results"
        assert rfs_id not in returned_ids, "ready-for-steward UoW should not be in stale-prescribing results"

    def test_boundary_exactly_at_ttl_is_stale(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A UoW with heartbeat at exactly TTL seconds ago is treated as stale."""
        # Use TTL + 5s to avoid sub-second clock edge cases in the test
        boundary_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 5))
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=boundary_heartbeat)

        result = registry.get_stale_prescribing_uows(
            stale_after_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )

        assert any(u.id == uow_id for u in result), (
            f"UoW at TTL boundary should be returned as stale"
        )

    def test_boundary_one_second_before_ttl_is_fresh(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A UoW with heartbeat just under the TTL threshold is NOT stale."""
        # 30 seconds before TTL boundary — safely fresh
        fresh_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS - 30))
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=fresh_heartbeat)

        result = registry.get_stale_prescribing_uows(
            stale_after_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )

        assert not any(u.id == uow_id for u in result), (
            f"UoW with heartbeat before TTL boundary should NOT be stale"
        )


# ---------------------------------------------------------------------------
# Tests: record_prescribing_silence_reset — atomic transition
# ---------------------------------------------------------------------------

class TestRecordPrescribingSilenceReset:
    def test_record_prescribing_silence_transitions_to_ready_for_steward(
        self, registry: Registry, db_path: Path
    ) -> None:
        """record_prescribing_silence_reset transitions prescribing → ready-for-steward."""
        stale_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 60))
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=stale_heartbeat)

        rows = registry.record_prescribing_silence_reset(
            uow_id=uow_id,
            heartbeat_at=stale_heartbeat,
            silence_seconds=PRESCRIBING_SILENCE_TTL_SECONDS + 60,
        )

        assert rows == 1, f"Expected 1 row affected, got {rows}"
        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "ready-for-steward", (
            f"Expected status ready-for-steward, got {row['status']}"
        )

    def test_record_prescribing_silence_writes_heartbeat_silence_ttl_audit_event(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Audit log contains event='heartbeat_silence_ttl' with structured payload."""
        stale_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 60))
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=stale_heartbeat)
        silence = PRESCRIBING_SILENCE_TTL_SECONDS + 60

        registry.record_prescribing_silence_reset(
            uow_id=uow_id,
            heartbeat_at=stale_heartbeat,
            silence_seconds=float(silence),
        )

        entries = _get_audit_entries(db_path, uow_id)
        ttl_entries = [e for e in entries if e["event"] == "heartbeat_silence_ttl"]
        assert len(ttl_entries) >= 1, (
            f"Expected at least one heartbeat_silence_ttl audit entry, got: "
            f"{[e['event'] for e in entries]}"
        )

        entry = ttl_entries[-1]
        assert entry["from_status"] == "prescribing"
        assert entry["to_status"] == "ready-for-steward"

        note = json.loads(entry["note"])
        assert note.get("reason") == "heartbeat_silence_ttl", (
            f"Expected reason=heartbeat_silence_ttl in note, got: {note}"
        )
        assert "silence_seconds" in note
        assert "heartbeat_at" in note

    def test_record_prescribing_silence_returns_zero_on_race(
        self, registry: Registry, db_path: Path
    ) -> None:
        """record_prescribing_silence_reset returns 0 when UoW is already advanced (optimistic lock)."""
        stale_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 60))
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=stale_heartbeat)

        # First call succeeds
        first = registry.record_prescribing_silence_reset(
            uow_id=uow_id,
            heartbeat_at=stale_heartbeat,
            silence_seconds=float(PRESCRIBING_SILENCE_TTL_SECONDS + 60),
        )
        assert first == 1

        # Second call on the same UoW (now in ready-for-steward) returns 0
        second = registry.record_prescribing_silence_reset(
            uow_id=uow_id,
            heartbeat_at=stale_heartbeat,
            silence_seconds=float(PRESCRIBING_SILENCE_TTL_SECONDS + 60),
        )
        assert second == 0, f"Expected 0 on race, got {second}"

        # Exactly one audit entry — no duplicate written for the race
        entries = _get_audit_entries(db_path, uow_id)
        ttl_entries = [e for e in entries if e["event"] == "heartbeat_silence_ttl"]
        assert len(ttl_entries) == 1, (
            f"Expected exactly 1 heartbeat_silence_ttl audit entry, got {len(ttl_entries)}"
        )


# ---------------------------------------------------------------------------
# Tests: recover_stale_prescribing_uows — integration (using test-local copy)
# ---------------------------------------------------------------------------

class TestRecoverStalePrescribingUows:
    def test_recover_stale_prescribing_requeues_uow(
        self, registry: Registry, db_path: Path
    ) -> None:
        """recover_stale_prescribing_uows transitions stale prescribing UoW to ready-for-steward."""
        stale_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 120))
        uow_id = _insert_prescribing_uow(
            db_path,
            heartbeat_at=stale_heartbeat,
            updated_at=stale_heartbeat,
        )

        result = _recover_stale_prescribing_uows(registry, ttl_seconds=PRESCRIBING_SILENCE_TTL_SECONDS)

        assert result.checked >= 1
        assert result.recovered >= 1
        assert result.skipped_dry_run == 0

        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "ready-for-steward", (
            f"Expected ready-for-steward after recovery, got {row['status']}"
        )

    def test_recover_stale_prescribing_writes_heartbeat_silence_ttl_audit(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Recovery writes heartbeat_silence_ttl audit entry."""
        stale_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 120))
        uow_id = _insert_prescribing_uow(
            db_path,
            heartbeat_at=stale_heartbeat,
            updated_at=stale_heartbeat,
        )

        _recover_stale_prescribing_uows(registry, ttl_seconds=PRESCRIBING_SILENCE_TTL_SECONDS)

        entries = _get_audit_entries(db_path, uow_id)
        ttl_entries = [e for e in entries if e["event"] == "heartbeat_silence_ttl"]
        assert len(ttl_entries) >= 1, (
            f"Expected heartbeat_silence_ttl audit entry after recovery"
        )

    def test_recover_stale_prescribing_dry_run_no_transition(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Dry-run mode detects stale UoWs but does NOT change their status."""
        stale_heartbeat = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 120))
        uow_id = _insert_prescribing_uow(
            db_path,
            heartbeat_at=stale_heartbeat,
            updated_at=stale_heartbeat,
        )

        result = _recover_stale_prescribing_uows(
            registry, dry_run=True, ttl_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )

        assert result.skipped_dry_run >= 1
        assert result.recovered == 0

        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "prescribing", (
            f"Dry-run should not change status, but got {row['status']}"
        )

    def test_recover_fresh_prescribing_not_requeued(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A prescribing UoW with a recent heartbeat is NOT requeued."""
        fresh_heartbeat = _iso_offset(-60)  # 1 minute ago — healthy
        uow_id = _insert_prescribing_uow(db_path, heartbeat_at=fresh_heartbeat)

        result = _recover_stale_prescribing_uows(
            registry, ttl_seconds=PRESCRIBING_SILENCE_TTL_SECONDS
        )

        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "prescribing", (
            f"Fresh prescribing UoW should stay in prescribing, got {row['status']}"
        )

    def test_only_prescribing_status_eligible_for_prescribing_recovery(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Active/executing UoWs with stale heartbeats are NOT touched by prescribing recovery."""
        stale_ts = _iso_offset(-(PRESCRIBING_SILENCE_TTL_SECONDS + 120))
        active_id = _insert_prescribing_uow(
            db_path, heartbeat_at=stale_ts, updated_at=stale_ts, status="active"
        )
        executing_id = _insert_prescribing_uow(
            db_path, heartbeat_at=stale_ts, updated_at=stale_ts, status="executing"
        )

        _recover_stale_prescribing_uows(registry, ttl_seconds=PRESCRIBING_SILENCE_TTL_SECONDS)

        active_row = _get_uow_row(db_path, active_id)
        executing_row = _get_uow_row(db_path, executing_id)

        assert active_row["status"] == "active", (
            f"active UoW should remain active, got {active_row['status']}"
        )
        assert executing_row["status"] == "executing", (
            f"executing UoW should remain executing, got {executing_row['status']}"
        )
