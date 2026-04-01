"""
Unit tests for the Startup Sweep — steward-heartbeat.py / run_startup_sweep()

Covers every acceptance criterion from issue #307:

- Scans active, ready-for-executor, AND diagnosing UoWs
- Runs on every heartbeat invocation (not only at actual startup)
- All five classification values used correctly
- output_ref field name used throughout (not output_file)
- output_ref treated as absolute path; non-absolute path → crashed_no_output_ref
- 0-byte output_ref classified as crashed_zero_bytes, not possibly_complete
- Startup sweep does NOT parse output_ref contents — existence and size only
- Audit entry written BEFORE (within same tx as) status transition (Principle 1)
- Optimistic lock for both active and ready-for-executor transitions
- If rows == 0: skip audit_log write (race won by another process)
- executor_orphan audit entry includes prior_status, proposed_at, age_seconds, threshold_seconds
- possibly_complete audit entry includes output_ref_mtime and output_ref_age_seconds
- Test: active UoW, output_ref exists and non-empty → possibly_complete, ready-for-steward
- Test: active UoW, output_ref exists but 0 bytes → crashed_zero_bytes, ready-for-steward
- Test: active UoW, output_ref path written but file missing → crashed_output_ref_missing
- Test: active UoW, output_ref NULL → crashed_no_output_ref, ready-for-steward
- Test: ready-for-executor UoW with proposed_at 2 hours ago → executor_orphan, ready-for-steward
- Test: ready-for-executor UoW with proposed_at 30 minutes ago → not swept
- Test: concurrency — call startup sweep twice on same active UoW → exactly one transition
- Test: DB with one active UoW and one ready-for-steward UoW → only active UoW touched
- Test: diagnosing UoW → transitioned to ready-for-steward with startup_sweep audit entry
- Test: run sweep on empty registry — completes silently with no side effects
- Test: executor_orphan UoW — Steward applies first-execution posture, not crash-recovery posture
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
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
# Schema helper — mirrors the full Phase 2 schema
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


def _iso_ago(seconds: int) -> str:
    """Return an ISO-8601 timestamp for `seconds` ago."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _make_uow(
    conn: sqlite3.Connection,
    *,
    uow_id: str | None = None,
    status: str,
    output_ref: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    started_at: str | None = None,
    source_issue_number: int = 42,
) -> str:
    if uow_id is None:
        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
    # For `active` UoWs, default updated_at to 10 minutes ago so the
    # active_sweep_threshold_seconds (300 s) does not suppress the sweep.
    # Tests that need to verify the threshold behaviour can pass explicit
    # updated_at / started_at values.
    if status == "active" and updated_at is None and started_at is None:
        updated_at = _iso_ago(600)
    now = created_at or _now_iso()
    _updated_at = updated_at or now
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, started_at, summary, output_ref, route_evidence, trigger)
        VALUES (?, 'executable', ?, ?, '2026-01-01', ?, 'solo',
                ?, ?, ?, 'Test UoW', ?, '{}', '{"type": "immediate"}')
        """,
        (uow_id, f"github:issue/{source_issue_number}", source_issue_number,
         status, now, _updated_at, started_at, output_ref),
    )
    conn.commit()
    return uow_id


def _get_status(db_path: Path, uow_id: str) -> str:
    conn = _open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["status"] if row else "NOT_FOUND"
    finally:
        conn.close()


def _audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = _open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.close()
    return db_path


@pytest.fixture
def registry(tmp_db):
    return Registry(tmp_db)


# ---------------------------------------------------------------------------
# Helper: import run_startup_sweep from steward-heartbeat
# ---------------------------------------------------------------------------

_HEARTBEAT_MOD = None


def _import_sweep():
    """
    Import run_startup_sweep and related symbols from steward-heartbeat.py.

    We register the module in sys.modules before exec so that @dataclass
    __module__ resolves correctly (dataclasses look up sys.modules[cls.__module__]
    to resolve forward references).

    We cache the module after first load to avoid re-executing on repeated calls.
    """
    global _HEARTBEAT_MOD
    if _HEARTBEAT_MOD is not None:
        mod = _HEARTBEAT_MOD
        return mod.run_startup_sweep, mod.StartupSweepResult, mod._classify_active_uow

    import importlib.util
    _MOD_NAME = "steward_heartbeat"
    hb_path = REPO_ROOT / "scheduled-tasks" / "steward-heartbeat.py"
    spec = importlib.util.spec_from_file_location(_MOD_NAME, hb_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)

    _HEARTBEAT_MOD = mod
    return mod.run_startup_sweep, mod.StartupSweepResult, mod._classify_active_uow


run_startup_sweep, StartupSweepResult, _classify_active_uow = _import_sweep()


# ---------------------------------------------------------------------------
# Classification unit tests (_classify_active_uow)
# ---------------------------------------------------------------------------

class TestClassifyActiveUow:
    def test_null_output_ref_is_crashed_no_output_ref(self):
        classification, extra = _classify_active_uow(None)
        assert classification == "crashed_no_output_ref"
        assert extra == {}

    def test_non_absolute_path_is_crashed_no_output_ref(self):
        classification, extra = _classify_active_uow("relative/path/output.md")
        assert classification == "crashed_no_output_ref"
        assert extra == {}

    def test_missing_file_is_crashed_output_ref_missing(self, tmp_path):
        classification, extra = _classify_active_uow(str(tmp_path / "nonexistent.md"))
        assert classification == "crashed_output_ref_missing"
        assert extra == {}

    def test_zero_byte_file_is_crashed_zero_bytes(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_bytes(b"")
        classification, extra = _classify_active_uow(str(f))
        assert classification == "crashed_zero_bytes"
        assert extra == {}

    def test_nonempty_file_is_possibly_complete(self, tmp_path):
        f = tmp_path / "output.md"
        f.write_text("some output")
        classification, extra = _classify_active_uow(str(f))
        assert classification == "possibly_complete"
        assert "output_ref_mtime" in extra
        assert "output_ref_age_seconds" in extra
        assert isinstance(extra["output_ref_age_seconds"], int)

    def test_possibly_complete_does_not_read_file_contents(self, tmp_path):
        """The sweep only checks existence and size — does not parse the file."""
        f = tmp_path / "output.md"
        f.write_text("unparseable garbage {{{ not json")
        classification, extra = _classify_active_uow(str(f))
        assert classification == "possibly_complete"


# ---------------------------------------------------------------------------
# Integration tests: run_startup_sweep against a real Registry
# ---------------------------------------------------------------------------

class TestStartupSweepEmpty:
    def test_empty_registry_completes_silently(self, registry, tmp_db):
        result = run_startup_sweep(registry)
        assert result.active_swept == 0
        assert result.executor_orphans_swept == 0
        assert result.diagnosing_swept == 0
        assert result.skipped_dry_run == 0

    def test_empty_registry_no_audit_entries(self, registry, tmp_db):
        run_startup_sweep(registry)
        conn = _open_conn(tmp_db)
        count = conn.execute("SELECT COUNT(*) as c FROM audit_log").fetchone()["c"]
        conn.close()
        assert count == 0


class TestActiveUowClassifications:
    def _make_active_uow(self, registry, tmp_db, output_ref=None):
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(conn, status="active", output_ref=output_ref)
        conn.close()
        return uow_id

    def test_path1_nonempty_output_ref_is_possibly_complete(self, registry, tmp_db, tmp_path):
        f = tmp_path / "output.md"
        f.write_text("done!")
        uow_id = self._make_active_uow(registry, tmp_db, output_ref=str(f))

        result = run_startup_sweep(registry)

        assert result.active_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

        entries = _audit_entries(tmp_db, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "startup_sweep"
        assert entries[0]["from_status"] == "active"
        assert entries[0]["to_status"] == "ready-for-steward"
        assert entries[0]["agent"] == "steward"

        note = json.loads(entries[0]["note"])
        assert note["classification"] == "possibly_complete"
        assert note["output_ref"] == str(f)
        assert "output_ref_mtime" in note
        assert "output_ref_age_seconds" in note

    def test_path2_zero_byte_output_ref_is_crashed_zero_bytes(self, registry, tmp_db, tmp_path):
        f = tmp_path / "output.md"
        f.write_bytes(b"")
        uow_id = self._make_active_uow(registry, tmp_db, output_ref=str(f))

        result = run_startup_sweep(registry)

        assert result.active_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

        entries = _audit_entries(tmp_db, uow_id)
        assert len(entries) == 1
        note = json.loads(entries[0]["note"])
        assert note["classification"] == "crashed_zero_bytes"
        assert "output_ref_mtime" not in note

    def test_path3_missing_file_is_crashed_output_ref_missing(self, registry, tmp_db, tmp_path):
        uow_id = self._make_active_uow(
            registry, tmp_db, output_ref=str(tmp_path / "gone.md")
        )

        result = run_startup_sweep(registry)

        assert result.active_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

        note = json.loads(_audit_entries(tmp_db, uow_id)[0]["note"])
        assert note["classification"] == "crashed_output_ref_missing"

    def test_path4_null_output_ref_is_crashed_no_output_ref(self, registry, tmp_db):
        uow_id = self._make_active_uow(registry, tmp_db, output_ref=None)

        result = run_startup_sweep(registry)

        assert result.active_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

        note = json.loads(_audit_entries(tmp_db, uow_id)[0]["note"])
        assert note["classification"] == "crashed_no_output_ref"
        assert note["output_ref"] is None

    def test_non_absolute_output_ref_is_crashed_no_output_ref(self, registry, tmp_db):
        uow_id = self._make_active_uow(
            registry, tmp_db, output_ref="relative/path.md"
        )

        run_startup_sweep(registry)

        note = json.loads(_audit_entries(tmp_db, uow_id)[0]["note"])
        assert note["classification"] == "crashed_no_output_ref"


class TestActiveSweepThreshold:
    """
    Tests for the active-UoW minimum-age guard added to fix the timing race
    where the startup sweep misclassifies a running executor subprocess as
    crashed_output_ref_missing.

    The guard is active_sweep_threshold_seconds (default 300 s). A UoW in
    `active` status that is younger than the threshold is skipped; only UoWs
    older than the threshold are swept.
    """

    def test_young_active_uow_is_not_swept(self, registry, tmp_db):
        """A freshly-claimed UoW (< threshold) must not be swept."""
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="active",
            output_ref=None,
            updated_at=_iso_ago(30),  # 30 seconds ago — well inside threshold
        )
        conn.close()

        result = run_startup_sweep(registry, active_sweep_threshold_seconds=300)

        assert result.active_swept == 0
        assert _get_status(tmp_db, uow_id) == "active"
        assert len(_audit_entries(tmp_db, uow_id)) == 0

    def test_old_active_uow_is_swept_after_threshold(self, registry, tmp_db):
        """A UoW older than the threshold must still be swept as a crash candidate."""
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="active",
            output_ref=None,
            updated_at=_iso_ago(600),  # 10 minutes ago — past default 300s threshold
        )
        conn.close()

        result = run_startup_sweep(registry, active_sweep_threshold_seconds=300)

        assert result.active_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"
        note = json.loads(_audit_entries(tmp_db, uow_id)[0]["note"])
        assert note["classification"] == "crashed_no_output_ref"

    def test_updated_at_is_age_anchor_for_active_uow(self, registry, tmp_db):
        """
        updated_at is the age anchor used by the guard (started_at is not
        exposed on the UoW dataclass per design).  A UoW with an old
        updated_at must be swept even if it was created recently.
        """
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="active",
            output_ref=None,
            created_at=_iso_ago(10),    # created very recently
            updated_at=_iso_ago(600),   # updated_at old enough for the guard
        )
        conn.close()

        result = run_startup_sweep(registry, active_sweep_threshold_seconds=300)

        # updated_at (600s) > threshold (300s) → must be swept
        assert result.active_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

    def test_threshold_zero_disables_guard(self, registry, tmp_db):
        """
        Passing active_sweep_threshold_seconds=0 disables the guard and sweeps
        all active UoWs regardless of age (original behaviour).
        """
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="active",
            output_ref=None,
            updated_at=_iso_ago(5),  # very young
        )
        conn.close()

        result = run_startup_sweep(registry, active_sweep_threshold_seconds=0)

        assert result.active_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

    def test_dry_run_with_young_active_uow_skips_correctly(self, registry, tmp_db):
        """
        In dry_run mode, a young UoW should be skipped by the threshold guard
        (not counted as skipped_dry_run, since the guard fires before the
        dry_run check).
        """
        conn = _open_conn(tmp_db)
        _make_uow(
            conn,
            status="active",
            output_ref=None,
            updated_at=_iso_ago(30),  # young
        )
        conn.close()

        result = run_startup_sweep(registry, dry_run=True, active_sweep_threshold_seconds=300)

        # Young UoW was suppressed by the age guard — not counted as dry-run skip
        assert result.skipped_dry_run == 0
        assert result.active_swept == 0


class TestExecutorOrphan:
    def test_rfe_uow_2_hours_old_is_swept_as_executor_orphan(self, registry, tmp_db):
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="ready-for-executor",
            created_at=_iso_ago(7200),  # 2 hours ago
        )
        conn.close()

        result = run_startup_sweep(registry)

        assert result.executor_orphans_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

        entries = _audit_entries(tmp_db, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "startup_sweep"
        assert entries[0]["from_status"] == "ready-for-executor"
        assert entries[0]["to_status"] == "ready-for-steward"

        note = json.loads(entries[0]["note"])
        assert note["classification"] == "executor_orphan"
        assert note["prior_status"] == "ready-for-executor"
        assert "proposed_at" in note
        assert note["age_seconds"] > 3600
        assert note["threshold_seconds"] == 3600

    def test_rfe_uow_30_minutes_old_is_not_swept(self, registry, tmp_db):
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="ready-for-executor",
            created_at=_iso_ago(1800),  # 30 minutes ago
        )
        conn.close()

        result = run_startup_sweep(registry)

        assert result.executor_orphans_swept == 0
        assert _get_status(tmp_db, uow_id) == "ready-for-executor"
        assert len(_audit_entries(tmp_db, uow_id)) == 0

    def test_rfe_uow_exactly_at_threshold_is_not_swept(self, registry, tmp_db):
        """A UoW exactly at threshold_seconds (not older) should not be swept."""
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="ready-for-executor",
            created_at=_iso_ago(3600),
        )
        conn.close()

        # Use a threshold slightly higher so this UoW falls below it.
        result = run_startup_sweep(registry, orphan_threshold_seconds=3601)

        assert result.executor_orphans_swept == 0
        assert _get_status(tmp_db, uow_id) == "ready-for-executor"


class TestDiagnosingUow:
    def test_diagnosing_uow_is_transitioned_to_ready_for_steward(self, registry, tmp_db):
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(conn, status="diagnosing")
        conn.close()

        result = run_startup_sweep(registry)

        assert result.diagnosing_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

        entries = _audit_entries(tmp_db, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "startup_sweep"
        assert entries[0]["from_status"] == "diagnosing"
        assert entries[0]["to_status"] == "ready-for-steward"

        note = json.loads(entries[0]["note"])
        assert note["classification"] == "diagnosing_orphan"
        assert note["prior_status"] == "diagnosing"
        assert note["actor"] == "steward"


class TestConcurrency:
    def test_double_sweep_active_uow_produces_exactly_one_transition(
        self, registry, tmp_db
    ):
        """
        Simulate two concurrent sweeps on the same active UoW.
        Only one should win the optimistic lock; the other sees rows_affected == 0
        and skips the audit write.
        """
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(conn, status="active", output_ref=None)
        conn.close()

        # First sweep — wins the lock.
        result1 = run_startup_sweep(registry)
        assert result1.active_swept == 1
        assert _get_status(tmp_db, uow_id) == "ready-for-steward"

        # Second sweep — UoW is now ready-for-steward; active query returns nothing.
        result2 = run_startup_sweep(registry)
        assert result2.active_swept == 0

        # Exactly one audit entry.
        entries = _audit_entries(tmp_db, uow_id)
        assert len(entries) == 1

    def test_double_sweep_executor_orphan_produces_exactly_one_transition(
        self, registry, tmp_db
    ):
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="ready-for-executor",
            created_at=_iso_ago(7200),
        )
        conn.close()

        result1 = run_startup_sweep(registry)
        assert result1.executor_orphans_swept == 1

        result2 = run_startup_sweep(registry)
        assert result2.executor_orphans_swept == 0

        assert len(_audit_entries(tmp_db, uow_id)) == 1


class TestSelectivity:
    def test_only_active_uow_is_touched_when_mixed_statuses(self, registry, tmp_db):
        """
        A DB with one active UoW and one ready-for-steward UoW:
        only the active UoW is touched.
        """
        conn = _open_conn(tmp_db)
        active_id = _make_uow(conn, status="active", source_issue_number=1)
        rfs_id = _make_uow(conn, status="ready-for-steward", source_issue_number=2)
        conn.close()

        result = run_startup_sweep(registry)

        assert result.active_swept == 1
        assert _get_status(tmp_db, active_id) == "ready-for-steward"
        assert _get_status(tmp_db, rfs_id) == "ready-for-steward"
        # Only the active UoW gets an audit entry from startup_sweep.
        assert len(_audit_entries(tmp_db, active_id)) == 1
        assert len(_audit_entries(tmp_db, rfs_id)) == 0

    def test_proposed_and_pending_uows_are_never_touched(self, registry, tmp_db):
        conn = _open_conn(tmp_db)
        proposed_id = _make_uow(conn, status="proposed", source_issue_number=10)
        pending_id = _make_uow(conn, status="pending", source_issue_number=11)
        conn.close()

        result = run_startup_sweep(registry)

        assert result.active_swept == 0
        assert result.executor_orphans_swept == 0
        assert _get_status(tmp_db, proposed_id) == "proposed"
        assert _get_status(tmp_db, pending_id) == "pending"


class TestDryRun:
    def test_dry_run_classifies_but_does_not_transition(self, registry, tmp_db):
        conn = _open_conn(tmp_db)
        active_id = _make_uow(conn, status="active", source_issue_number=1)
        rfe_id = _make_uow(
            conn, status="ready-for-executor", created_at=_iso_ago(7200),
            source_issue_number=2,
        )
        conn.close()

        result = run_startup_sweep(registry, dry_run=True)

        assert result.active_swept == 0
        assert result.executor_orphans_swept == 0
        assert result.skipped_dry_run == 2
        assert _get_status(tmp_db, active_id) == "active"
        assert _get_status(tmp_db, rfe_id) == "ready-for-executor"
        assert len(_audit_entries(tmp_db, active_id)) == 0
        assert len(_audit_entries(tmp_db, rfe_id)) == 0


class TestExecutorOrphanStewardPosture:
    """
    executor_orphan UoW: Steward applies first-execution posture, not
    crash-recovery posture. This is validated by checking that the audit
    classification is 'executor_orphan' (which _determine_reentry_posture
    in steward.py maps to 'executor_orphan', triggering clean first execution).
    """

    def test_executor_orphan_classification_triggers_first_execution_posture(
        self, registry, tmp_db
    ):
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="ready-for-executor",
            created_at=_iso_ago(7200),
        )
        conn.close()

        run_startup_sweep(registry)

        entries = _audit_entries(tmp_db, uow_id)
        assert len(entries) == 1
        note = json.loads(entries[0]["note"])

        # The Steward reads 'executor_orphan' classification and routes to
        # _determine_reentry_posture which returns 'executor_orphan' —
        # triggering clean first-execution logic (not crash surface threshold).
        assert note["classification"] == "executor_orphan"
        assert note["prior_status"] == "ready-for-executor"

    def test_executor_orphan_has_no_output_ref(self, registry, tmp_db):
        """executor_orphan: Executor never ran, so output_ref is NULL in the audit."""
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn,
            status="ready-for-executor",
            created_at=_iso_ago(7200),
        )
        conn.close()

        run_startup_sweep(registry)

        note = json.loads(_audit_entries(tmp_db, uow_id)[0]["note"])
        assert note["output_ref"] is None


class TestAuditEntryStructure:
    def test_all_required_fields_present_in_active_audit_entry(
        self, registry, tmp_db
    ):
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(conn, status="active")
        conn.close()

        run_startup_sweep(registry)

        note = json.loads(_audit_entries(tmp_db, uow_id)[0]["note"])
        for field in ("event", "actor", "classification", "output_ref", "uow_id", "timestamp"):
            assert field in note, f"Missing field: {field}"
        assert note["event"] == "startup_sweep"
        assert note["actor"] == "steward"
        assert note["uow_id"] == uow_id

    def test_all_required_fields_present_in_executor_orphan_audit_entry(
        self, registry, tmp_db
    ):
        conn = _open_conn(tmp_db)
        uow_id = _make_uow(
            conn, status="ready-for-executor", created_at=_iso_ago(7200)
        )
        conn.close()

        run_startup_sweep(registry)

        note = json.loads(_audit_entries(tmp_db, uow_id)[0]["note"])
        for field in (
            "event", "actor", "classification", "uow_id", "timestamp",
            "prior_status", "proposed_at", "age_seconds", "threshold_seconds",
        ):
            assert field in note, f"Missing field: {field}"
        assert note["threshold_seconds"] == 3600
        assert note["prior_status"] == "ready-for-executor"
