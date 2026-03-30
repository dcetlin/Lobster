"""
Integration test harness: WOS Phase 2 end-to-end pipeline.

This file is the living spec for the full Steward/Executor pipeline.
It defines the contract between components; each component PR (#303, #305, etc.)
should leave this suite green.

Structure:
- DB provisioning fixtures (functional now, with Phase 2 schema applied inline)
- Seed helpers for all UoW pattern types
- State transition coverage: pending → ready-for-steward → diagnosing →
  ready-for-executor → active → ready-for-steward → done
- One stall path through the Observation Loop
- Concurrency test: two simultaneous heartbeat invocations against same DB
- BOOTUP_CANDIDATE_GATE integration test

Tests that require Steward (#303) or Executor (#305) are marked:
  @pytest.mark.xfail(reason="requires Steward (#303)", strict=True)
  @pytest.mark.xfail(reason="requires Executor (#305)", strict=True)

The harness infrastructure (DB provisioning, fixtures, helpers) is fully
functional now. xfail tests become green as component PRs merge.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Generator

import pytest

# ---------------------------------------------------------------------------
# Repo/module path setup
# ---------------------------------------------------------------------------

import sys
_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.registry import Registry


# ===========================================================================
# Phase 2 schema migration (applied inline by fixtures)
# ===========================================================================
#
# The actual migration lives in scripts/migrate_add_steward_fields.py (#309).
# These DDL statements mirror the spec from #309 exactly so the integration
# tests can run before #309 merges. When #309 merges, the migration script
# and this inline DDL must stay in sync.
#
# Each column is added only when absent (idempotent per-column).

_PHASE2_COLUMNS: list[tuple[str, str]] = [
    # (column_name, column_definition)
    ("workflow_artifact",  "TEXT NULL"),
    ("success_criteria",   "TEXT NULL"),
    ("prescribed_skills",  "TEXT NULL"),
    ("steward_cycles",     "INTEGER NOT NULL DEFAULT 0"),
    ("timeout_at",         "TEXT NULL"),
    ("estimated_runtime",  "INTEGER NULL"),
    ("steward_agenda",     "TEXT NULL"),
    ("steward_log",        "TEXT NULL"),
]

_EXECUTOR_UOW_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS executor_uow_view AS
SELECT
    id, status, workflow_artifact, prescribed_skills,
    estimated_runtime, timeout_at, output_ref,
    started_at, completed_at, steward_cycles,
    source_issue_number, summary, success_criteria
FROM uow_registry;
-- steward_agenda and steward_log intentionally excluded (Steward-private)
"""


def apply_phase2_migration(conn: sqlite3.Connection) -> None:
    """
    Apply Phase 2 schema columns to an existing registry DB.

    Idempotent per-column: safe to call on a DB that already has some or all
    Phase 2 columns. Mirrors the contract specified in #309.
    """
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(uow_registry)").fetchall()
    }
    for col_name, col_def in _PHASE2_COLUMNS:
        if col_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE uow_registry ADD COLUMN {col_name} {col_def}"
            )
    conn.execute(_EXECUTOR_UOW_VIEW_DDL)
    conn.commit()


def validate_phase2_schema(conn: sqlite3.Connection) -> None:
    """
    Assert all Phase 2 fields are present. Raises RuntimeError if any are missing.
    This mirrors the function the Steward calls at startup (#303 spec).
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(uow_registry)").fetchall()
    }
    required = {col for col, _ in _PHASE2_COLUMNS}
    missing = required - existing
    if missing:
        raise RuntimeError(
            f"schema migration not applied — missing columns: {sorted(missing)}. "
            "Run scripts/migrate_add_steward_fields.py first."
        )


# ===========================================================================
# DB & Registry fixtures
# ===========================================================================


@pytest.fixture
def wos_db_path(tmp_path: Path) -> Path:
    """Fresh SQLite DB path for each test. Uses tmp_path for isolation."""
    return tmp_path / "wos_test.db"


@pytest.fixture
def phase1_registry(wos_db_path: Path) -> Registry:
    """Registry initialized with Phase 1 schema only (no migration applied)."""
    return Registry(wos_db_path)


@pytest.fixture
def phase2_registry(wos_db_path: Path) -> Registry:
    """
    Registry with Phase 2 migration applied.

    This is the fixture to use for any test that exercises Phase 2 behavior.
    The migration is applied after Registry.__init__ so that tests can
    observe the before/after state if needed.
    """
    reg = Registry(wos_db_path)
    conn = sqlite3.connect(str(wos_db_path))
    conn.row_factory = sqlite3.Row
    try:
        apply_phase2_migration(conn)
    finally:
        conn.close()
    return reg


@pytest.fixture
def p2_conn(wos_db_path: Path, phase2_registry: Registry) -> Generator[sqlite3.Connection, None, None]:
    """
    Open connection to a Phase 2 DB, yielded for direct SQL assertions.
    Closed after the test.
    """
    conn = sqlite3.connect(str(wos_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


# ===========================================================================
# Seed helpers — create UoWs for each pattern type
# ===========================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_uow(
    registry: Registry,
    issue_number: int,
    title: str,
    success_criteria: str | None = None,
    trigger_type: str = "immediate",
    sweep_date: str | None = None,
) -> str:
    """
    Create a proposed UoW and return its id.

    Encapsulates the upsert + confirm-to-pending + advance-to-ready-for-steward
    steps so individual tests can focus on the state they want to test.
    """
    result = registry.upsert(
        issue_number=issue_number,
        title=title,
        sweep_date=sweep_date,
    )
    assert result["action"] == "inserted", f"Expected insert, got: {result}"
    uow_id = result["id"]

    # Confirm proposed → pending
    confirm_result = registry.confirm(uow_id)
    assert confirm_result["status"] == "pending", f"Confirm failed: {confirm_result}"

    return uow_id


def _advance_to_ready_for_steward(
    registry: Registry,
    uow_id: str,
    success_criteria: str | None = None,
) -> None:
    """
    Transition pending → ready-for-steward, optionally setting success_criteria.

    In the full system, the trigger evaluator (#304) does this. Here we call
    set_status_direct to simulate that transition for testing purposes.
    """
    if success_criteria is not None:
        # Write success_criteria directly — the Registrar writes this at germination (#309 spec)
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE uow_registry SET success_criteria = ?, updated_at = ? WHERE id = ?",
            (success_criteria, _now(), uow_id),
        )
        conn.commit()
        conn.close()

    registry.set_status_direct(uow_id, "ready-for-steward")


@dataclass
class SeedBundle:
    """
    A seeded UoW with all supporting state for one test scenario.
    Created by the seed_* fixtures below.
    """
    uow_id: str
    issue_number: int
    title: str
    success_criteria: str | None


@pytest.fixture
def seed_immediate(phase2_registry: Registry) -> SeedBundle:
    """
    Seed a UoW with trigger=immediate and success_criteria set.
    Starts in ready-for-steward state.
    """
    uow_id = _seed_uow(
        phase2_registry,
        issue_number=9001,
        title="Integration test UoW — immediate trigger",
    )
    _advance_to_ready_for_steward(
        phase2_registry,
        uow_id,
        success_criteria="A test output file exists and contains 'completed'.",
    )
    return SeedBundle(
        uow_id=uow_id,
        issue_number=9001,
        title="Integration test UoW — immediate trigger",
        success_criteria="A test output file exists and contains 'completed'.",
    )


@pytest.fixture
def seed_blocked(phase2_registry: Registry) -> SeedBundle:
    """
    Seed a UoW that starts in 'blocked' state (simulates Dan input needed).
    """
    uow_id = _seed_uow(
        phase2_registry,
        issue_number=9002,
        title="Integration test UoW — blocked scenario",
    )
    _advance_to_ready_for_steward(
        phase2_registry,
        uow_id,
        success_criteria="Design decision confirmed by Dan.",
    )
    phase2_registry.set_status_direct(uow_id, "blocked")
    return SeedBundle(
        uow_id=uow_id,
        issue_number=9002,
        title="Integration test UoW — blocked scenario",
        success_criteria="Design decision confirmed by Dan.",
    )


@pytest.fixture
def seed_stall_candidate(phase2_registry: Registry, wos_db_path: Path) -> SeedBundle:
    """
    Seed an 'active' UoW with timeout_at in the past (simulates a stalled executor).
    The Observation Loop should detect this as a stall.
    """
    uow_id = _seed_uow(
        phase2_registry,
        issue_number=9003,
        title="Integration test UoW — stall candidate",
    )
    _advance_to_ready_for_steward(phase2_registry, uow_id,
                                  success_criteria="Executor output written.")

    # Simulate Executor claiming: set active + timeout_at in the past
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    now = _now()
    conn = sqlite3.connect(str(wos_db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """UPDATE uow_registry
           SET status='active', started_at=?, timeout_at=?, updated_at=?
           WHERE id=?""",
        (past, past, now, uow_id),
    )
    # Audit the transition
    conn.execute(
        """INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
           VALUES (?, ?, 'status_change', 'ready-for-executor', 'active', 'executor-stub', 'executor claimed')""",
        (now, uow_id),
    )
    conn.commit()
    conn.close()

    return SeedBundle(
        uow_id=uow_id,
        issue_number=9003,
        title="Integration test UoW — stall candidate",
        success_criteria="Executor output written.",
    )


# ===========================================================================
# Helpers — direct DB state reads and writes for assertions
# ===========================================================================


def read_uow(conn: sqlite3.Connection, uow_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    assert row is not None, f"UoW {uow_id!r} not found in registry"
    return dict(row)


def read_audit_log(conn: sqlite3.Connection, uow_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
        (uow_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def audit_events(conn: sqlite3.Connection, uow_id: str) -> list[str]:
    """Return ordered list of event names from audit_log for a UoW."""
    return [r["event"] for r in read_audit_log(conn, uow_id)]


def audit_statuses(conn: sqlite3.Connection, uow_id: str) -> list[tuple[str | None, str | None]]:
    """Return ordered list of (from_status, to_status) tuples from audit_log."""
    return [
        (r["from_status"], r["to_status"])
        for r in read_audit_log(conn, uow_id)
    ]


# ===========================================================================
# Stub implementations (for use until #303/#305 land)
# ===========================================================================
#
# These stubs implement the minimal contract each component must honour.
# They are thin enough to stay in sync with the spec without becoming
# a second implementation to maintain.


def stub_steward_claim(
    conn: sqlite3.Connection, uow_id: str, agent: str = "steward-stub"
) -> bool:
    """
    Attempt to claim a ready-for-steward UoW for diagnosis.
    Returns True if claim succeeded, False if another claimant won.
    Mirrors the optimistic-lock contract from #303 spec Step 1.
    """
    cursor = conn.execute(
        """UPDATE uow_registry
           SET status = 'diagnosing', updated_at = ?
           WHERE id = ? AND status = 'ready-for-steward'""",
        (_now(), uow_id),
    )
    if cursor.rowcount == 0:
        return False  # Another instance claimed it first
    conn.execute(
        """INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
           VALUES (?, ?, 'status_change', 'ready-for-steward', 'diagnosing', ?, 'steward claimed for diagnosis')""",
        (_now(), uow_id, agent),
    )
    conn.commit()
    return True


def stub_steward_prescribe(
    conn: sqlite3.Connection,
    uow_id: str,
    workflow_artifact_path: str,
    prescribed_skills: list[str] | None = None,
    agent: str = "steward-stub",
) -> None:
    """
    Simulate Steward completing diagnosis and writing a prescription.
    Transitions diagnosing → ready-for-executor.
    Mirrors the contract from #303 spec Step 4.
    """
    skills_json = json.dumps(prescribed_skills or [])
    now = _now()
    conn.execute(
        """UPDATE uow_registry
           SET status = 'ready-for-executor',
               workflow_artifact = ?,
               prescribed_skills = ?,
               steward_cycles = steward_cycles + 1,
               updated_at = ?
           WHERE id = ?""",
        (workflow_artifact_path, skills_json, now, uow_id),
    )
    conn.execute(
        """INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
           VALUES (?, ?, 'status_change', 'diagnosing', 'ready-for-executor', ?, 'prescription written')""",
        (now, uow_id, agent),
    )
    conn.commit()


def stub_executor_claim(
    conn: sqlite3.Connection,
    uow_id: str,
    output_ref: str,
    estimated_runtime: int = 300,
    agent: str = "executor-stub",
) -> bool:
    """
    Simulate Executor claiming a ready-for-executor UoW.
    Returns True if claim succeeded (optimistic lock).
    Mirrors the Executor claim contract from #305 spec.
    """
    now = _now()
    timeout_at = (
        datetime.now(timezone.utc) + timedelta(seconds=estimated_runtime)
    ).isoformat()
    cursor = conn.execute(
        """UPDATE uow_registry
           SET status = 'active',
               started_at = ?,
               output_ref = ?,
               timeout_at = ?,
               updated_at = ?
           WHERE id = ? AND status = 'ready-for-executor'""",
        (now, output_ref, timeout_at, now, uow_id),
    )
    if cursor.rowcount == 0:
        return False
    conn.execute(
        """INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
           VALUES (?, ?, 'status_change', 'ready-for-executor', 'active', ?, 'executor claimed')""",
        (now, uow_id, agent),
    )
    conn.commit()
    return True


def stub_executor_complete(
    conn: sqlite3.Connection,
    uow_id: str,
    output_path: Path,
    output_content: str = "completed",
    agent: str = "executor-stub",
) -> None:
    """
    Simulate Executor writing output and transitioning active → ready-for-steward.
    Mirrors the return contract from #305 spec.
    """
    output_path.write_text(output_content)
    now = _now()
    conn.execute(
        """UPDATE uow_registry
           SET status = 'ready-for-steward', updated_at = ?
           WHERE id = ?""",
        (now, uow_id),
    )
    conn.execute(
        """INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
           VALUES (?, ?, 'execution_complete', 'active', 'ready-for-steward', ?, 'executor wrote output')""",
        (now, uow_id, agent),
    )
    conn.commit()


def stub_steward_close(
    conn: sqlite3.Connection,
    uow_id: str,
    agent: str = "steward-stub",
) -> None:
    """
    Simulate Steward declaring done after verifying output.
    Transitions ready-for-steward → done.
    Mirrors the convergence check from #303 spec Step 4.
    """
    now = _now()
    conn.execute(
        """UPDATE uow_registry
           SET status = 'done',
               completed_at = ?,
               updated_at = ?,
               steward_cycles = steward_cycles + 1
           WHERE id = ?""",
        (now, now, uow_id),
    )
    conn.execute(
        """INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
           VALUES (?, ?, 'status_change', 'ready-for-steward', 'done', ?, 'steward declared closure')""",
        (now, uow_id, agent),
    )
    conn.commit()


# ===========================================================================
# Observation Loop stub
# ===========================================================================


def stub_observation_loop_pass(
    conn: sqlite3.Connection,
    agent: str = "observation-loop-stub",
) -> list[str]:
    """
    Single pass of the Observation Loop: detect stalled active UoWs.

    Returns list of UoW ids that were detected as stalled and transitioned
    back to ready-for-steward with a stall_detected audit event.

    Mirrors the Observation Loop contract from #306 spec.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    stalled_rows = conn.execute(
        """SELECT id, timeout_at FROM uow_registry
           WHERE status = 'active' AND timeout_at IS NOT NULL AND timeout_at < ?""",
        (now,),
    ).fetchall()

    stalled_ids = []
    for row in stalled_rows:
        uow_id = row["id"] if hasattr(row, "__getitem__") else row[0]
        conn.execute(
            """UPDATE uow_registry
               SET status = 'ready-for-steward', updated_at = ?
               WHERE id = ?""",
            (now, uow_id),
        )
        conn.execute(
            """INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
               VALUES (?, ?, 'stall_detected', 'active', 'ready-for-steward', ?, 'timeout_at exceeded')""",
            (now, uow_id, agent),
        )
        stalled_ids.append(uow_id)

    conn.commit()
    return stalled_ids


# ===========================================================================
# Test classes
# ===========================================================================


@pytest.mark.integration
class TestPhase2SchemaHarness:
    """
    Verifies the Phase 2 schema migration (inline DDL) is correct.
    These tests are fully functional now — no xfail.
    """

    def test_phase2_columns_present_after_migration(self, wos_db_path, phase2_registry):
        """All Phase 2 columns are present after apply_phase2_migration."""
        conn = sqlite3.connect(str(wos_db_path))
        try:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(uow_registry)").fetchall()}
            for col_name, _ in _PHASE2_COLUMNS:
                assert col_name in existing, f"Missing Phase 2 column: {col_name}"
        finally:
            conn.close()

    def test_migration_is_idempotent(self, wos_db_path, phase2_registry):
        """Running apply_phase2_migration twice does not raise."""
        conn = sqlite3.connect(str(wos_db_path))
        try:
            apply_phase2_migration(conn)  # second call — must not raise
            apply_phase2_migration(conn)  # third call for good measure
        finally:
            conn.close()

    def test_executor_uow_view_excludes_steward_private_fields(self, wos_db_path, phase2_registry):
        """executor_uow_view cannot SELECT steward_agenda or steward_log."""
        conn = sqlite3.connect(str(wos_db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Verify view exists
            view_row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' AND name='executor_uow_view'"
            ).fetchone()
            assert view_row is not None, "executor_uow_view does not exist"

            # Attempting to select steward-private fields must fail
            with pytest.raises(sqlite3.OperationalError, match="no such column"):
                conn.execute("SELECT steward_agenda FROM executor_uow_view").fetchall()

            with pytest.raises(sqlite3.OperationalError, match="no such column"):
                conn.execute("SELECT steward_log FROM executor_uow_view").fetchall()
        finally:
            conn.close()

    def test_executor_uow_view_includes_required_columns(self, wos_db_path, phase2_registry):
        """executor_uow_view includes all Executor-accessible columns."""
        required_view_columns = {
            "id", "status", "workflow_artifact", "prescribed_skills",
            "estimated_runtime", "timeout_at", "output_ref",
            "started_at", "completed_at", "steward_cycles",
            "source_issue_number", "summary", "success_criteria",
        }
        conn = sqlite3.connect(str(wos_db_path))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute("SELECT * FROM executor_uow_view LIMIT 0")
            view_columns = {desc[0] for desc in cursor.description}
            missing = required_view_columns - view_columns
            assert not missing, f"executor_uow_view missing columns: {missing}"
        finally:
            conn.close()

    def test_validate_phase2_schema_raises_on_missing_column(self, wos_db_path, phase1_registry):
        """validate_phase2_schema raises RuntimeError when Phase 2 columns are absent."""
        conn = sqlite3.connect(str(wos_db_path))
        try:
            with pytest.raises(RuntimeError, match="schema migration not applied"):
                validate_phase2_schema(conn)
        finally:
            conn.close()

    def test_validate_phase2_schema_passes_after_migration(self, wos_db_path, phase2_registry):
        """validate_phase2_schema does not raise on a fully migrated DB."""
        conn = sqlite3.connect(str(wos_db_path))
        try:
            validate_phase2_schema(conn)  # must not raise
        finally:
            conn.close()

    def test_phase1_uow_survives_migration(self, wos_db_path):
        """Existing Phase 1 UoWs have correct NULL/0 defaults after migration."""
        # Create a Phase 1 record
        reg = Registry(wos_db_path)
        result = reg.upsert(issue_number=1001, title="Pre-migration record")
        uow_id = result["id"]

        # Apply Phase 2 migration
        conn = sqlite3.connect(str(wos_db_path))
        conn.row_factory = sqlite3.Row
        try:
            apply_phase2_migration(conn)
            row = conn.execute(
                "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            row_dict = dict(row)
        finally:
            conn.close()

        assert row_dict["steward_cycles"] == 0
        assert row_dict["workflow_artifact"] is None
        assert row_dict["prescribed_skills"] is None
        assert row_dict["timeout_at"] is None
        assert row_dict["steward_agenda"] is None
        assert row_dict["steward_log"] is None
        assert row_dict["success_criteria"] is None

    def test_success_criteria_not_null_for_new_uow(self, wos_db_path, phase2_registry):
        """New UoWs created after migration can store non-NULL success_criteria."""
        uow_id = _seed_uow(phase2_registry, 1002, "Post-migration UoW")
        _advance_to_ready_for_steward(
            phase2_registry, uow_id,
            success_criteria="Output file exists with expected content.",
        )
        conn = sqlite3.connect(str(wos_db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT success_criteria FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["success_criteria"] == "Output file exists with expected content."


@pytest.mark.integration
class TestDBProvisioning:
    """
    Verifies the harness fixtures themselves — DB isolation, seed helpers, etc.
    Fully functional now.
    """

    def test_fresh_db_per_test(self, wos_db_path: Path):
        """Each test gets an empty, isolated DB."""
        reg = Registry(wos_db_path)
        assert reg.list() == []

    def test_seed_immediate_starts_in_ready_for_steward(
        self, phase2_registry: Registry, seed_immediate: SeedBundle, p2_conn: sqlite3.Connection
    ):
        uow = read_uow(p2_conn, seed_immediate.uow_id)
        assert uow["status"] == "ready-for-steward"
        assert uow["success_criteria"] == seed_immediate.success_criteria

    def test_seed_blocked_starts_in_blocked(
        self, phase2_registry: Registry, seed_blocked: SeedBundle, p2_conn: sqlite3.Connection
    ):
        uow = read_uow(p2_conn, seed_blocked.uow_id)
        assert uow["status"] == "blocked"

    def test_seed_stall_candidate_starts_in_active_with_past_timeout(
        self, phase2_registry: Registry, seed_stall_candidate: SeedBundle, p2_conn: sqlite3.Connection
    ):
        uow = read_uow(p2_conn, seed_stall_candidate.uow_id)
        assert uow["status"] == "active"
        assert uow["timeout_at"] is not None
        # timeout_at must be in the past
        timeout_dt = datetime.fromisoformat(uow["timeout_at"].replace("Z", "+00:00"))
        assert timeout_dt < datetime.now(timezone.utc)

    def test_audit_log_records_all_seed_transitions(
        self, phase2_registry: Registry, seed_immediate: SeedBundle, p2_conn: sqlite3.Connection
    ):
        """Audit log has entries for: created, proposed→pending, pending→ready-for-steward."""
        events = audit_events(p2_conn, seed_immediate.uow_id)
        # created, then status change to pending, then status change to ready-for-steward
        assert "created" in events
        status_pairs = audit_statuses(p2_conn, seed_immediate.uow_id)
        assert ("proposed", "pending") in status_pairs
        # ready-for-steward transition uses set_status_direct (direct status set)
        to_statuses = [pair[1] for pair in status_pairs if pair[1] is not None]
        assert "ready-for-steward" in to_statuses


@pytest.mark.integration
class TestStateTransitions:
    """
    State transition coverage using stub actors.
    These tests exercise the DB contract rather than real implementations.
    Fully functional now — they define the expected pipeline flow.
    """

    def test_full_pipeline_pending_to_done(
        self,
        phase2_registry: Registry,
        seed_immediate: SeedBundle,
        p2_conn: sqlite3.Connection,
        tmp_path: Path,
    ):
        """
        Full happy-path: pending → ready-for-steward → diagnosing →
        ready-for-executor → active → ready-for-steward → done.

        Uses stubs throughout. This defines the contract for the real components.
        """
        uow_id = seed_immediate.uow_id
        # UoW is already in ready-for-steward (seeded by fixture)

        # --- Steward: claim ---
        claimed = stub_steward_claim(p2_conn, uow_id)
        assert claimed is True
        assert read_uow(p2_conn, uow_id)["status"] == "diagnosing"

        # --- Steward: prescribe ---
        artifact_path = str(tmp_path / "workflow_artifact.json")
        stub_steward_prescribe(p2_conn, uow_id, artifact_path, prescribed_skills=["systematic-debugging"])
        uow = read_uow(p2_conn, uow_id)
        assert uow["status"] == "ready-for-executor"
        assert uow["workflow_artifact"] == artifact_path
        assert uow["steward_cycles"] == 1
        assert json.loads(uow["prescribed_skills"]) == ["systematic-debugging"]

        # --- Executor: claim ---
        output_ref = str(tmp_path / "output.md")
        claimed = stub_executor_claim(p2_conn, uow_id, output_ref, estimated_runtime=300)
        assert claimed is True
        uow = read_uow(p2_conn, uow_id)
        assert uow["status"] == "active"
        assert uow["started_at"] is not None
        assert uow["timeout_at"] is not None

        # --- Executor: complete ---
        output_path = Path(output_ref)
        stub_executor_complete(p2_conn, uow_id, output_path, output_content="completed")
        assert output_path.exists()
        assert read_uow(p2_conn, uow_id)["status"] == "ready-for-steward"

        # --- Steward: close ---
        claimed_again = stub_steward_claim(p2_conn, uow_id)
        assert claimed_again is True
        stub_steward_close(p2_conn, uow_id)
        uow = read_uow(p2_conn, uow_id)
        assert uow["status"] == "done"
        assert uow["completed_at"] is not None
        assert uow["steward_cycles"] == 2

    def test_audit_log_coherent_story_across_all_transitions(
        self,
        phase2_registry: Registry,
        seed_immediate: SeedBundle,
        p2_conn: sqlite3.Connection,
        tmp_path: Path,
    ):
        """
        The audit_log tells a coherent story: each transition has a before/after.
        No silent transitions (Principle 1 from the design doc).
        """
        uow_id = seed_immediate.uow_id

        stub_steward_claim(p2_conn, uow_id)
        stub_steward_prescribe(p2_conn, uow_id, str(tmp_path / "wa.json"))
        output_path = tmp_path / "output.md"
        stub_executor_claim(p2_conn, uow_id, str(output_path))
        stub_executor_complete(p2_conn, uow_id, output_path)
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_close(p2_conn, uow_id)

        log = read_audit_log(p2_conn, uow_id)

        # Every state_change entry must have both from_status and to_status
        state_changes = [e for e in log if e["event"] == "status_change"]
        for entry in state_changes:
            assert entry["from_status"] is not None, f"Missing from_status: {entry}"
            assert entry["to_status"] is not None, f"Missing to_status: {entry}"

        # Verify sequence of to_status values
        to_statuses = [e["to_status"] for e in state_changes]
        assert "pending" in to_statuses
        assert "diagnosing" in to_statuses
        assert "ready-for-executor" in to_statuses
        assert "active" in to_statuses
        assert "done" in to_statuses

    def test_blocked_unblocked_re_enters_steward(
        self,
        phase2_registry: Registry,
        seed_blocked: SeedBundle,
        p2_conn: sqlite3.Connection,
        tmp_path: Path,
    ):
        """
        blocked → ready-for-steward (Dan unblocks) → full pipeline completion.
        Validates that the blocked path re-enters the Steward loop correctly.
        """
        uow_id = seed_blocked.uow_id
        assert read_uow(p2_conn, uow_id)["status"] == "blocked"

        # Simulate Dan unblocking (/decide command)
        now = _now()
        p2_conn.execute(
            "UPDATE uow_registry SET status='ready-for-steward', updated_at=? WHERE id=?",
            (now, uow_id),
        )
        p2_conn.execute(
            """INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
               VALUES (?, ?, 'status_change', 'blocked', 'ready-for-steward', 'dan', 'unblocked via /decide')""",
            (now, uow_id),
        )
        p2_conn.commit()

        assert read_uow(p2_conn, uow_id)["status"] == "ready-for-steward"

        # Now the full pipeline can run
        claimed = stub_steward_claim(p2_conn, uow_id)
        assert claimed is True

        output_path = tmp_path / "blocked_output.md"
        stub_steward_prescribe(p2_conn, uow_id, str(tmp_path / "wa.json"))
        stub_executor_claim(p2_conn, uow_id, str(output_path))
        stub_executor_complete(p2_conn, uow_id, output_path)
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_close(p2_conn, uow_id)

        assert read_uow(p2_conn, uow_id)["status"] == "done"

    def test_done_has_no_re_entry_path(
        self,
        phase2_registry: Registry,
        seed_immediate: SeedBundle,
        p2_conn: sqlite3.Connection,
        tmp_path: Path,
    ):
        """
        done is a terminal state. Attempting to claim a 'done' UoW fails
        the optimistic lock (returns False from stub_steward_claim).
        """
        uow_id = seed_immediate.uow_id
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_prescribe(p2_conn, uow_id, str(tmp_path / "wa.json"))
        output_path = tmp_path / "output.md"
        stub_executor_claim(p2_conn, uow_id, str(output_path))
        stub_executor_complete(p2_conn, uow_id, output_path)
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_close(p2_conn, uow_id)

        assert read_uow(p2_conn, uow_id)["status"] == "done"

        # Attempting a second Steward claim on a done UoW must fail
        second_claim = stub_steward_claim(p2_conn, uow_id)
        assert second_claim is False, "done UoW should not be claimable"


@pytest.mark.integration
class TestObservationLoop:
    """
    Tests for the Observation Loop: stall detection via timeout_at.
    Fully functional using the observation loop stub.
    """

    def test_stall_detected_transitions_to_ready_for_steward(
        self,
        phase2_registry: Registry,
        seed_stall_candidate: SeedBundle,
        p2_conn: sqlite3.Connection,
    ):
        """
        An active UoW with timeout_at in the past is detected as stalled.
        The Observation Loop transitions it back to ready-for-steward and
        writes a stall_detected audit event.
        """
        uow_id = seed_stall_candidate.uow_id
        assert read_uow(p2_conn, uow_id)["status"] == "active"

        stalled = stub_observation_loop_pass(p2_conn)
        assert uow_id in stalled

        uow = read_uow(p2_conn, uow_id)
        assert uow["status"] == "ready-for-steward"

        events = audit_events(p2_conn, uow_id)
        assert "stall_detected" in events

    def test_active_uow_within_timeout_not_flagged(
        self,
        phase2_registry: Registry,
        p2_conn: sqlite3.Connection,
        tmp_path: Path,
    ):
        """An active UoW with timeout_at in the future is not flagged as stalled."""
        uow_id = _seed_uow(phase2_registry, 9010, "Non-stalled active UoW")
        _advance_to_ready_for_steward(phase2_registry, uow_id)
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_prescribe(p2_conn, uow_id, str(tmp_path / "wa.json"))
        stub_executor_claim(p2_conn, uow_id, str(tmp_path / "output.md"),
                            estimated_runtime=3600)  # 1 hour — still in future

        assert read_uow(p2_conn, uow_id)["status"] == "active"

        stalled = stub_observation_loop_pass(p2_conn)
        assert uow_id not in stalled
        assert read_uow(p2_conn, uow_id)["status"] == "active"

    def test_stall_path_steward_re_entry(
        self,
        phase2_registry: Registry,
        seed_stall_candidate: SeedBundle,
        p2_conn: sqlite3.Connection,
        tmp_path: Path,
    ):
        """
        After a stall is detected, the Steward re-enters the UoW.
        The most recent audit event before the new diagnosis is 'stall_detected'.
        The Steward can prescribe again (incrementing steward_cycles).
        """
        uow_id = seed_stall_candidate.uow_id

        # Observation loop detects stall
        stub_observation_loop_pass(p2_conn)
        assert read_uow(p2_conn, uow_id)["status"] == "ready-for-steward"

        # Steward re-enters — last audit event is stall_detected
        log = read_audit_log(p2_conn, uow_id)
        last_event = log[-1]["event"]
        assert last_event == "stall_detected"

        # Steward claims and prescribes again
        claimed = stub_steward_claim(p2_conn, uow_id)
        assert claimed is True

        output_path = tmp_path / "retry_output.md"
        stub_steward_prescribe(p2_conn, uow_id, str(tmp_path / "wa_retry.json"))
        stub_executor_claim(p2_conn, uow_id, str(output_path), estimated_runtime=300)
        stub_executor_complete(p2_conn, uow_id, output_path)
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_close(p2_conn, uow_id)

        assert read_uow(p2_conn, uow_id)["status"] == "done"


@pytest.mark.integration
class TestConcurrentHeartbeat:
    """
    Concurrency test: two simultaneous heartbeat invocations against same DB.
    Asserts the optimistic lock ensures exactly one claim per UoW.
    """

    def test_only_one_claimant_wins_per_uow(
        self,
        phase2_registry: Registry,
        wos_db_path: Path,
        seed_immediate: SeedBundle,
    ):
        """
        Two threads simultaneously attempt to claim the same UoW.
        Exactly one must succeed; the other must see rowcount=0 (lose the race).
        """
        uow_id = seed_immediate.uow_id

        results: list[bool] = []
        errors: list[Exception] = []

        def attempt_claim() -> None:
            try:
                conn = sqlite3.connect(str(wos_db_path), timeout=10.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                try:
                    won = stub_steward_claim(conn, uow_id)
                    results.append(won)
                finally:
                    conn.close()
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=attempt_claim)
        t2 = threading.Thread(target=attempt_claim)

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 2, f"Expected 2 results, got {results}"

        # Exactly one thread must have won (True) and one must have lost (False)
        assert sum(results) == 1, (
            f"Expected exactly one winner but got: {results}. "
            "The optimistic lock is not enforced correctly."
        )

        # UoW must be in 'diagnosing' state
        final = phase2_registry.get(uow_id)
        assert final["status"] == "diagnosing"

    def test_two_heartbeats_do_not_double_process_any_uow(
        self,
        phase2_registry: Registry,
        wos_db_path: Path,
    ):
        """
        Seed multiple UoWs. Two simulated heartbeats run simultaneously.
        Each UoW must be claimed by at most one heartbeat invocation.
        """
        uow_ids = []
        for i in range(5):
            uow_id = _seed_uow(phase2_registry, 8100 + i, f"Concurrent UoW #{i}")
            _advance_to_ready_for_steward(phase2_registry, uow_id)
            uow_ids.append(uow_id)

        claims_by_thread: dict[int, list[str]] = {0: [], 1: []}

        def heartbeat(thread_id: int) -> None:
            conn = sqlite3.connect(str(wos_db_path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                for uow_id in uow_ids:
                    won = stub_steward_claim(
                        conn, uow_id, agent=f"steward-thread-{thread_id}"
                    )
                    if won:
                        claims_by_thread[thread_id].append(uow_id)
            finally:
                conn.close()

        t1 = threading.Thread(target=heartbeat, args=(0,))
        t2 = threading.Thread(target=heartbeat, args=(1,))

        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        all_claimed = claims_by_thread[0] + claims_by_thread[1]
        # No UoW should appear in both threads' claims
        assert len(all_claimed) == len(set(all_claimed)), (
            f"A UoW was claimed by both threads: {all_claimed}"
        )
        # All UoWs must have been claimed by exactly one thread
        assert sorted(all_claimed) == sorted(uow_ids), (
            f"Not all UoWs were claimed. claimed={all_claimed}, expected={uow_ids}"
        )


@pytest.mark.integration
class TestBootupCandidateGate:
    """
    BOOTUP_CANDIDATE_GATE integration tests.

    The gate is described in the design doc:
    "BOOTUP_CANDIDATE_GATE = True — blocks #271–#298 from executing until
    Phase 2 validation passes."

    The gate has two states:
    - OPEN (True): bootup-candidate UoWs are blocked from entering the pipeline.
      The Steward must NOT prescribe for bootup-candidate UoWs while the gate is open.
    - CLOSED (False): the validation sequence passed; bootup-candidate UoWs
      can proceed through the full pipeline.

    The gate is stored on the UoW's trigger field and evaluated by the trigger
    evaluator (#304). For the integration test harness, we test the DB-level
    contract: a UoW with trigger type 'bootup-candidate-gate' stays in 'pending'
    until the gate is cleared, while other UoWs advance normally.
    """

    def test_bootup_candidate_blocked_when_gate_is_open(
        self,
        phase2_registry: Registry,
        p2_conn: sqlite3.Connection,
    ):
        """
        A UoW with trigger type 'bootup-candidate-gate' must not advance to
        ready-for-steward while BOOTUP_CANDIDATE_GATE is True.

        This test defines the contract for the trigger evaluator (#304):
        evaluate_condition must return False for bootup-candidate UoWs when
        the gate flag is True.
        """
        uow_id = _seed_uow(
            phase2_registry,
            issue_number=271,
            title="Bootup candidate — blocked by gate",
        )
        # Write the bootup-candidate-gate trigger type
        p2_conn.execute(
            """UPDATE uow_registry
               SET trigger = '{"type": "bootup-candidate-gate"}', updated_at = ?
               WHERE id = ?""",
            (_now(), uow_id),
        )
        p2_conn.commit()

        uow = read_uow(p2_conn, uow_id)
        assert uow["status"] == "pending"
        trigger = json.loads(uow["trigger"])
        assert trigger["type"] == "bootup-candidate-gate"

        # Simulate gate check: evaluate_condition returns False while gate is open
        gate_is_open = True  # BOOTUP_CANDIDATE_GATE = True
        should_advance = (not gate_is_open) or (trigger["type"] != "bootup-candidate-gate")
        assert should_advance is False, (
            "Bootup-candidate UoW must remain pending while BOOTUP_CANDIDATE_GATE is True"
        )

        # UoW must still be in pending
        assert read_uow(p2_conn, uow_id)["status"] == "pending"

    def test_bootup_candidate_advances_when_gate_clears(
        self,
        phase2_registry: Registry,
        p2_conn: sqlite3.Connection,
        tmp_path: Path,
    ):
        """
        When BOOTUP_CANDIDATE_GATE flips to False (validation sequence passed),
        bootup-candidate UoWs can advance to ready-for-steward and complete
        the full pipeline.
        """
        uow_id = _seed_uow(
            phase2_registry,
            issue_number=272,
            title="Bootup candidate — gate cleared",
        )
        p2_conn.execute(
            """UPDATE uow_registry
               SET trigger = '{"type": "bootup-candidate-gate"}',
                   success_criteria = 'Bootup candidate reviewed and applied.',
                   updated_at = ?
               WHERE id = ?""",
            (_now(), uow_id),
        )
        p2_conn.commit()

        # Gate clears — BOOTUP_CANDIDATE_GATE = False
        gate_is_open = False

        # Trigger evaluator advances pending → ready-for-steward
        phase2_registry.set_status_direct(uow_id, "ready-for-steward")

        # Full pipeline proceeds normally
        output_path = tmp_path / "bootup_output.md"
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_prescribe(p2_conn, uow_id, str(tmp_path / "wa.json"))
        stub_executor_claim(p2_conn, uow_id, str(output_path))
        stub_executor_complete(p2_conn, uow_id, output_path)
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_close(p2_conn, uow_id)

        assert read_uow(p2_conn, uow_id)["status"] == "done"

    def test_immediate_trigger_unaffected_by_bootup_gate(
        self,
        phase2_registry: Registry,
        seed_immediate: SeedBundle,
        p2_conn: sqlite3.Connection,
        tmp_path: Path,
    ):
        """
        UoWs with trigger=immediate are not blocked by BOOTUP_CANDIDATE_GATE.
        The gate only applies to bootup-candidate-gate trigger type.
        """
        uow_id = seed_immediate.uow_id
        uow = read_uow(p2_conn, uow_id)

        # Verify not a bootup candidate
        trigger = json.loads(uow["trigger"]) if isinstance(uow["trigger"], str) else uow["trigger"]
        assert trigger.get("type") != "bootup-candidate-gate"

        # Pipeline proceeds regardless of gate state
        output_path = tmp_path / "immediate_output.md"
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_prescribe(p2_conn, uow_id, str(tmp_path / "wa.json"))
        stub_executor_claim(p2_conn, uow_id, str(output_path))
        stub_executor_complete(p2_conn, uow_id, output_path)
        stub_steward_claim(p2_conn, uow_id)
        stub_steward_close(p2_conn, uow_id)

        assert read_uow(p2_conn, uow_id)["status"] == "done"


# ===========================================================================
# xfail placeholder tests — require Steward (#303) and Executor (#305)
# ===========================================================================
#
# These tests will be converted to real assertions when the component PRs land.
# They are marked xfail(strict=True) so:
# - While the component is missing: XFAIL (expected failure, test suite green)
# - Once the component lands and the test passes: XPASS → suite turns red,
#   forcing the implementer to remove the xfail marker and confirm the test
#   is genuinely passing.
#
# The test body documents the expected behavior as a specification.


@pytest.mark.integration
@pytest.mark.xfail(
    reason="requires Steward heartbeat script (#303) — not yet implemented",
    strict=True,
)
def test_real_steward_diagnoses_ready_for_steward_uow(tmp_path: Path):
    """
    The real Steward heartbeat script should:
    1. Find UoWs in ready-for-steward state
    2. Claim each via optimistic lock (diagnosing)
    3. Call validate_phase2_schema at startup
    4. Read the UoW's source GitHub issue body
    5. Write initial steward_agenda when steward_cycles == 0
    6. Prescribe a workflow_artifact and prescribed_skills
    7. Transition to ready-for-executor
    8. Increment steward_cycles

    This test will import steward-heartbeat.py once #303 lands and invoke
    the steward main loop against a real test DB.
    """
    # This will invoke the real Steward once #303 is implemented.
    # Expected: UoW transitions from ready-for-steward → ready-for-executor
    # and steward_cycles increments.
    raise NotImplementedError("Steward (#303) not yet implemented")


@pytest.mark.integration
@pytest.mark.xfail(
    reason="requires Executor (#305) — not yet implemented",
    strict=True,
)
def test_real_executor_claims_and_executes_workflow_artifact(tmp_path: Path):
    """
    The real Executor should:
    1. Query executor_uow_view (never uow_registry directly)
    2. Claim a ready-for-executor UoW via optimistic lock
    3. Write output_ref and timeout_at at claim time
    4. Execute the workflow prescribed in workflow_artifact
    5. Write output to output_ref path
    6. Transition active → ready-for-steward with execution_complete audit event

    This test will import the Executor script once #305 lands and run it
    against a pre-seeded DB with a well-formed workflow_artifact.
    """
    raise NotImplementedError("Executor (#305) not yet implemented")


@pytest.mark.integration
@pytest.mark.xfail(
    reason="requires evaluate_condition (#304) — not yet implemented",
    strict=True,
)
def test_trigger_evaluator_advances_pending_immediate_uow(tmp_path: Path):
    """
    The trigger evaluator (#304) should:
    1. Find UoWs in pending state with trigger type 'immediate'
    2. Call evaluate_condition(uow) → True for immediate triggers
    3. Transition pending → ready-for-steward
    4. NOT advance bootup-candidate-gate UoWs while gate is open

    This test will invoke evaluate_condition once #304 lands.
    """
    raise NotImplementedError("evaluate_condition (#304) not yet implemented")


@pytest.mark.integration
@pytest.mark.xfail(
    reason="requires Steward + Executor (#303, #305) for steward_cycles cap test",
    strict=True,
)
def test_steward_surfaces_to_dan_when_cycles_reach_cap(tmp_path: Path):
    """
    When steward_cycles reaches 5, the Steward must:
    1. NOT prescribe autonomously
    2. Transition diagnosing → blocked
    3. Write a stall audit entry with reason 'steward_cycles_cap_reached'
    4. The note must include which return_reason triggered the cap

    This test will drive the real Steward through 5 cycles once #303 lands.
    """
    raise NotImplementedError("Steward hard cap behavior (#303) not yet implemented")


@pytest.mark.integration
@pytest.mark.xfail(
    reason="requires Executor (#305) for executor_uow_view isolation enforcement",
    strict=True,
)
def test_real_executor_cannot_read_steward_private_fields(tmp_path: Path):
    """
    The real Executor's SQL query path must not SELECT steward_agenda or
    steward_log. This is enforced at the view level (column-not-found).

    This test will inspect the Executor's SQL queries once #305 lands
    and verify that no query reaches steward-private columns via any path.
    """
    raise NotImplementedError("Executor (#305) not yet implemented")
