"""
Unit tests for Change 1b: sidecar-masked UoW detection (issue #1238).

Behavior verified (derived from spec, not from implementation):

get_sidecar_masked_uows:
- test_executing_uow_older_than_grace_window_returned:
  A UoW in 'executing' status older than min_age_seconds with no
  uow_heartbeat_log rows is returned as a candidate.
- test_uow_with_agent_heartbeat_excluded:
  A UoW with a non-NULL token_usage row in uow_heartbeat_log after the
  grace window is not returned (agent is alive).
- test_uow_newer_than_grace_window_excluded:
  A UoW younger than min_age_seconds is not returned (within startup
  grace window).
- test_active_status_uow_returned:
  A UoW in 'active' status (not 'executing') is also returned when it
  matches all other conditions.
- test_sidecar_only_heartbeat_not_excluded:
  A uow_heartbeat_log row where token_usage IS NULL (sidecar write) does
  not exclude the UoW — NULL token_usage is not the discriminating signal.
- test_grace_window_consistent:
  A UoW older than min_age_seconds but with an agent heartbeat written
  before the grace window expires (recorded_at <= updated_at +
  min_age_seconds) is still returned — only heartbeats after the grace
  window are the discriminator.
- test_terminal_status_uow_excluded:
  UoWs not in ('active', 'executing') are never returned.

record_sidecar_masked_stall:
- test_transitions_to_ready_for_steward:
  Successful call transitions the UoW to 'ready-for-steward'.
- test_writes_sidecar_masked_audit_entry:
  Audit log entry is written with event='stall_detected' and
  stall_type='sidecar_masked'.
- test_audit_from_status_matches_actual_status_executing:
  When UoW was in 'executing' status, from_status in audit log is 'executing'.
- test_audit_from_status_matches_actual_status_active:
  When UoW was in 'active' status, from_status in audit log is 'active'.
- test_returns_zero_on_race:
  Returns 0 without writing an audit entry when UoW already advanced
  (optimistic lock race safety).

detect_sidecar_masked_uows:
- test_dry_run_returns_skipped_count_no_state_change:
  dry_run=True returns skipped_dry_run count without writing state transitions
  or audit entries.
- test_live_mode_calls_record_sidecar_masked_stall:
  Live mode calls registry.record_sidecar_masked_stall for each candidate.
- test_registry_query_failure_returns_empty_result:
  If registry.get_sidecar_masked_uows raises, function returns
  SidecarMaskedResult(checked=0, recovered=0, skipped_dry_run=0) without
  crashing.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parents[3]
for _p in [str(REPO_ROOT), str(REPO_ROOT / "src"), str(REPO_ROOT / "scheduled-tasks")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.orchestration.registry import Registry

# ---------------------------------------------------------------------------
# Load steward-heartbeat.py as a module (hyphen in filename requires spec).
# Patch heavy production imports before exec so tests run without live services.
# ---------------------------------------------------------------------------

_SPEC = importlib.util.spec_from_file_location(
    "steward_heartbeat",
    str(REPO_ROOT / "scheduled-tasks" / "steward-heartbeat.py"),
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_PATCH_TARGETS = {
    "src.orchestration.steward": MagicMock(),
    "src.orchestration.github_sync": MagicMock(),
    "src.orchestration.paths": MagicMock(REGISTRY_DB=Path("/tmp/test_registry.db")),
    "startup_sweep": MagicMock(),
    "steward_heartbeat": _MODULE,
}
with patch.dict("sys.modules", _PATCH_TARGETS):
    _SPEC.loader.exec_module(_MODULE)

detect_sidecar_masked_uows = _MODULE.detect_sidecar_masked_uows
SidecarMaskedResult = _MODULE.SidecarMaskedResult


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    """Registry with all migrations applied (including 0017 uow_heartbeat_log)."""
    return Registry(db_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_offset(seconds: float) -> str:
    """Return an ISO timestamp offset by `seconds` from now (negative = past)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _insert_uow(
    db_path: Path,
    *,
    status: str = "executing",
    updated_at_offset: float = -400,
) -> str:
    """Insert a UoW directly via SQLite with updated_at set to an offset from now.

    Returns the uow_id.
    """
    uow_id = f"uow_test_{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    updated_at = _iso_offset(updated_at_offset)
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
                    ?, 300, '{}', '{"type": "immediate"}', 'operational', 'operational')
            """,
            (
                uow_id,
                f"github:issue/{issue_number}",
                issue_number,
                status,
                now,
                updated_at,
                updated_at,
                _iso_offset(-10),  # heartbeat_at — recent (sidecar-written)
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return uow_id


def _insert_heartbeat_log_row(
    db_path: Path,
    uow_id: str,
    *,
    recorded_at_offset: float = -50,
    token_usage: int | None = 1000,
) -> None:
    """Insert a row into uow_heartbeat_log for the given UoW."""
    recorded_at = _iso_offset(recorded_at_offset)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO uow_heartbeat_log (uow_id, recorded_at, token_usage) VALUES (?, ?, ?)",
            (uow_id, recorded_at, token_usage),
        )
        conn.commit()
    finally:
        conn.close()


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
# Tests: get_sidecar_masked_uows
# ---------------------------------------------------------------------------

class TestGetSidecarMaskedUows:

    def test_executing_uow_older_than_grace_window_returned(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoW in 'executing' status older than grace window with no heartbeat log rows is returned."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-400)

        candidates = registry.get_sidecar_masked_uows(min_age_seconds=300)
        candidate_ids = [u.id for u in candidates]

        assert uow_id in candidate_ids, (
            f"Expected sidecar-masked UoW {uow_id} in candidates, got {candidate_ids}"
        )

    def test_uow_with_agent_heartbeat_excluded(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoW with a non-NULL token_usage heartbeat log row after grace window is excluded."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-400)
        # Agent-originated heartbeat logged 50s ago — after the grace window
        _insert_heartbeat_log_row(db_path, uow_id, recorded_at_offset=-50, token_usage=2000)

        candidates = registry.get_sidecar_masked_uows(min_age_seconds=300)
        candidate_ids = [u.id for u in candidates]

        assert uow_id not in candidate_ids, (
            f"UoW {uow_id} with agent heartbeat should NOT be a candidate"
        )

    def test_uow_newer_than_grace_window_excluded(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoW updated only 100s ago (within grace window of 300s) is not returned."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-100)

        candidates = registry.get_sidecar_masked_uows(min_age_seconds=300)
        candidate_ids = [u.id for u in candidates]

        assert uow_id not in candidate_ids, (
            f"UoW {uow_id} inside grace window should NOT be a candidate"
        )

    def test_active_status_uow_returned(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoW in 'active' status (not just 'executing') is returned when conditions match."""
        uow_id = _insert_uow(db_path, status="active", updated_at_offset=-400)

        candidates = registry.get_sidecar_masked_uows(min_age_seconds=300)
        candidate_ids = [u.id for u in candidates]

        assert uow_id in candidate_ids, (
            f"Active UoW {uow_id} should be returned as sidecar-masked candidate"
        )

    def test_sidecar_only_heartbeat_not_excluded(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A heartbeat log row where token_usage IS NULL does not exclude the UoW."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-400)
        # Sidecar-written row: NULL token_usage
        _insert_heartbeat_log_row(db_path, uow_id, recorded_at_offset=-50, token_usage=None)

        candidates = registry.get_sidecar_masked_uows(min_age_seconds=300)
        candidate_ids = [u.id for u in candidates]

        assert uow_id in candidate_ids, (
            f"UoW {uow_id} with only NULL-token heartbeat should still be a sidecar-masked candidate"
        )

    def test_grace_window_consistent_both_clauses(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Non-default min_age_seconds is applied consistently in both the outer WHERE and NOT EXISTS.

        A UoW updated 400s ago with a heartbeat log row recorded 350s ago should
        be excluded only if min_age_seconds <= 350 (heartbeat is after the grace
        window). Using min_age_seconds=600 means the heartbeat at 350s ago is still
        within the grace window — so the UoW should still be returned.
        """
        # UoW updated 700s ago; no heartbeat log row
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-700)
        # Heartbeat recorded 350s ago: after a 300s grace window but before a 600s one
        _insert_heartbeat_log_row(db_path, uow_id, recorded_at_offset=-350, token_usage=1000)

        # With min_age_seconds=300 (default): grace window ends at updated_at + 300s.
        # Heartbeat at -350s (absolute) was recorded when the UoW is 350s old — after
        # the 300s grace window → heartbeat IS after grace window → UoW excluded.
        candidates_300 = registry.get_sidecar_masked_uows(min_age_seconds=300)
        assert uow_id not in [u.id for u in candidates_300], (
            f"UoW {uow_id} should be excluded with min_age_seconds=300 (heartbeat after grace window)"
        )

        # With min_age_seconds=600: grace window ends at updated_at + 600s.
        # Heartbeat was recorded when the UoW was 350s old — still within the 600s
        # grace window → heartbeat IS NOT after grace window → UoW returned.
        candidates_600 = registry.get_sidecar_masked_uows(min_age_seconds=600)
        assert uow_id in [u.id for u in candidates_600], (
            f"UoW {uow_id} should be returned with min_age_seconds=600 (heartbeat within grace window)"
        )

    def test_terminal_status_uow_excluded(
        self, registry: Registry, db_path: Path
    ) -> None:
        """UoWs not in ('active', 'executing') are excluded even if they are old."""
        uow_id_done = _insert_uow(db_path, status="done", updated_at_offset=-400)
        uow_id_ready = _insert_uow(db_path, status="ready-for-steward", updated_at_offset=-400)

        candidates = registry.get_sidecar_masked_uows(min_age_seconds=300)
        candidate_ids = [u.id for u in candidates]

        assert uow_id_done not in candidate_ids, "done UoW should not be a candidate"
        assert uow_id_ready not in candidate_ids, "ready-for-steward UoW should not be a candidate"


# ---------------------------------------------------------------------------
# Tests: record_sidecar_masked_stall
# ---------------------------------------------------------------------------

class TestRecordSidecarMaskedStall:

    def test_transitions_to_ready_for_steward(
        self, registry: Registry, db_path: Path
    ) -> None:
        """record_sidecar_masked_stall transitions the UoW to 'ready-for-steward'."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-400)

        rows = registry.record_sidecar_masked_stall(uow_id=uow_id, age_seconds=400.0)

        assert rows == 1
        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "ready-for-steward"

    def test_writes_sidecar_masked_audit_entry(
        self, registry: Registry, db_path: Path
    ) -> None:
        """record_sidecar_masked_stall writes a stall_detected audit entry with stall_type='sidecar_masked'."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-400)

        registry.record_sidecar_masked_stall(uow_id=uow_id, age_seconds=400.0)

        entries = _get_audit_entries(db_path, uow_id)
        stall_entries = [e for e in entries if e.get("event") == "stall_detected"]
        assert len(stall_entries) == 1, f"Expected 1 stall_detected entry, got {len(stall_entries)}"

        note = json.loads(stall_entries[0]["note"])
        assert note.get("stall_type") == "sidecar_masked", (
            f"Expected stall_type='sidecar_masked', got {note.get('stall_type')}"
        )

    def test_audit_from_status_matches_actual_status_executing(
        self, registry: Registry, db_path: Path
    ) -> None:
        """When UoW is in 'executing' status, audit log from_status is 'executing'."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-400)

        registry.record_sidecar_masked_stall(uow_id=uow_id, age_seconds=400.0)

        entries = _get_audit_entries(db_path, uow_id)
        stall_entries = [e for e in entries if e.get("event") == "stall_detected"]
        assert len(stall_entries) == 1
        assert stall_entries[0]["from_status"] == "executing", (
            f"Expected from_status='executing', got {stall_entries[0]['from_status']}"
        )

    def test_audit_from_status_matches_actual_status_active(
        self, registry: Registry, db_path: Path
    ) -> None:
        """When UoW is in 'active' status, audit log from_status is 'active' (not 'executing')."""
        uow_id = _insert_uow(db_path, status="active", updated_at_offset=-400)

        registry.record_sidecar_masked_stall(uow_id=uow_id, age_seconds=400.0)

        entries = _get_audit_entries(db_path, uow_id)
        stall_entries = [e for e in entries if e.get("event") == "stall_detected"]
        assert len(stall_entries) == 1
        assert stall_entries[0]["from_status"] == "active", (
            f"Expected from_status='active', got {stall_entries[0]['from_status']}"
        )

    def test_returns_zero_on_race(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Returns 0 and writes no audit entry when UoW has already advanced (optimistic lock)."""
        uow_id = _insert_uow(db_path, status="ready-for-steward", updated_at_offset=-400)

        rows = registry.record_sidecar_masked_stall(uow_id=uow_id, age_seconds=400.0)

        assert rows == 0, "Should return 0 on race (UoW not in active/executing)"

        entries = _get_audit_entries(db_path, uow_id)
        stall_entries = [e for e in entries if e.get("event") == "stall_detected"]
        assert len(stall_entries) == 0, "No audit entry should be written on a race"


# ---------------------------------------------------------------------------
# Tests: detect_sidecar_masked_uows (tested via inline mock — avoids importing
# the full steward chain, following the pattern from test_heartbeat_locking.py)
# ---------------------------------------------------------------------------

class TestDetectSidecarMaskedUows:

    def test_dry_run_returns_skipped_count_no_state_change(
        self, registry: Registry, db_path: Path
    ) -> None:
        """dry_run=True reports skipped_dry_run without writing state transitions or audit entries."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-400)

        with patch.object(_MODULE, "_append_observation"):
            result = detect_sidecar_masked_uows(registry, dry_run=True, min_age_seconds=300)

        assert result.skipped_dry_run >= 1
        assert result.recovered == 0

        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "executing", (
            "dry_run must not transition UoW status"
        )

        entries = _get_audit_entries(db_path, uow_id)
        stall_entries = [e for e in entries if e.get("event") == "stall_detected"]
        assert len(stall_entries) == 0, "dry_run must not write audit entries"

    def test_live_mode_calls_record_sidecar_masked_stall(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Live mode transitions sidecar-masked candidates to ready-for-steward."""
        uow_id = _insert_uow(db_path, status="executing", updated_at_offset=-400)

        with patch.object(_MODULE, "_append_observation"):
            result = detect_sidecar_masked_uows(registry, dry_run=False, min_age_seconds=300)

        assert result.recovered >= 1
        assert result.checked >= 1
        assert result.skipped_dry_run == 0

        row = _get_uow_row(db_path, uow_id)
        assert row["status"] == "ready-for-steward", (
            "Live mode should transition sidecar-masked UoW to ready-for-steward"
        )

    def test_registry_query_failure_returns_empty_result(self) -> None:
        """If registry.get_sidecar_masked_uows raises, function returns zero counts without crashing."""
        mock_registry = MagicMock()
        mock_registry.get_sidecar_masked_uows.side_effect = RuntimeError("DB error")

        result = detect_sidecar_masked_uows(mock_registry)

        assert result == SidecarMaskedResult(checked=0, recovered=0, skipped_dry_run=0)
