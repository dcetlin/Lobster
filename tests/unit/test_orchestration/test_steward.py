"""
Unit tests for the Steward — steward.py and steward-heartbeat.py

TDD: these tests are written before implementation.

Tests cover:
- Steward queries `ready-for-steward` UoWs, not `pending`
- Optimistic-lock claim: status → `diagnosing`; if rows == 0, skip
- Schema validation at startup raises if Phase 2 fields absent
- New UoW (steward_cycles=0): writes steward_agenda before prescribing
- Completion: UoW with valid output_ref + execution_complete → declares done
- Crash recovery: crashed_no_output + cycles < 2 → prescribes another pass
- Hard cap: steward_cycles >= 5 → surfaces to Dan, does not prescribe
- Crashed + cycles >= 2 → surfaces to Dan
- executor_orphan → treated as clean first execution (no crash threshold)
- Concurrent Steward invocations: only one claims a given UoW
- BOOTUP_CANDIDATE_GATE=True → bootup-candidate UoW is skipped
- BOOTUP_CANDIDATE_GATE=False → bootup-candidate UoW is processed
- re-entry: steward_agenda node updated; steward_log appended (not overwritten)
- WorkflowArtifact path is absolute (expanded via os.path.expanduser)
- Diagnosis audit entry written BEFORE prescription/transition
- workflow_artifact and prescribed_skills written before status transition
- steward_cycles incremented on each prescription
- route_reason written at prescription time
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers to build a Phase 2 DB
# ---------------------------------------------------------------------------

def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _apply_phase2_schema(conn: sqlite3.Connection) -> None:
    """Apply the complete Phase 1 + Phase 2 schema to a fresh DB."""
    conn.executescript("""
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

        CREATE VIEW IF NOT EXISTS executor_uow_view AS
        SELECT id, type, source, source_issue_number, sweep_date,
               status, posture, agent, children, parent,
               created_at, updated_at, started_at, completed_at,
               summary, output_ref, hooks_applied,
               route_reason, route_evidence, trigger, vision_ref,
               workflow_artifact, success_criteria, prescribed_skills,
               steward_cycles, timeout_at, estimated_runtime
        FROM uow_registry;
    """)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_uow_row(
    conn: sqlite3.Connection,
    uow_id: str | None = None,
    status: str = "ready-for-steward",
    steward_cycles: int = 0,
    output_ref: str | None = None,
    audit_log_entries: list[dict] | None = None,
    steward_agenda: str | None = None,
    steward_log: str | None = None,
    source_issue_number: int = 42,
    summary: str = "Test UoW",
    success_criteria: str | None = "Output file exists with non-empty content",
    labels: list[str] | None = None,
    prescribed_skills: str | None = None,
) -> str:
    """Insert a UoW row and optional audit entries. Returns the uow_id."""
    if uow_id is None:
        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, output_ref, steward_cycles,
             steward_agenda, steward_log, success_criteria, prescribed_skills,
             route_evidence, trigger)
        VALUES (?, 'executable', 'github:issue/42', ?, '2026-01-01', ?, 'solo',
                ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}')
        """,
        (uow_id, source_issue_number, status, now, now, summary,
         output_ref, steward_cycles, steward_agenda, steward_log,
         success_criteria, prescribed_skills),
    )
    if audit_log_entries:
        for entry in audit_log_entries:
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (_now_iso(), uow_id,
                 entry.get("event", "unknown"),
                 entry.get("from_status"),
                 entry.get("to_status"),
                 entry.get("agent"),
                 json.dumps(entry)),
            )
    conn.commit()
    return uow_id


def _audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = _open_db(db_path)
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id",
        (uow_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_uow(db_path: Path, uow_id: str) -> dict:
    conn = _open_db(db_path)
    row = conn.execute("SELECT * FROM uow_registry WHERE id = ?", (uow_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "registry.db"
    conn = _open_db(path)
    _apply_phase2_schema(conn)
    conn.close()
    return path


@pytest.fixture
def registry(db_path: Path):
    """Returns a Registry instance with Phase 2 schema applied."""
    from src.orchestration.registry import Registry
    return Registry(db_path)


# ---------------------------------------------------------------------------
# Inline stubs for Phase 2 methods not yet on main
# These allow tests to run before #324 and #327 PRs merge.
# ---------------------------------------------------------------------------

def _ensure_registry_has_phase2_methods(registry):
    """
    Patch registry with Phase 2 methods if not yet present (pre-merge).
    In production, these come from #324 and #327.
    """
    import types

    # validate_phase2_schema (from #324)
    if not hasattr(registry, 'validate_phase2_schema_static'):
        pass  # we call the standalone function

    # transition (from #327)
    if not hasattr(registry, 'transition'):
        def transition(self, uow_id: str, to_status: str, where_status: str) -> int:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = _now_iso()
                cursor = conn.execute(
                    "UPDATE uow_registry SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (to_status, now, uow_id, where_status),
                )
                rows = cursor.rowcount
                conn.commit()
                return rows
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        registry.transition = types.MethodType(transition, registry)

    # query (from #327)
    if not hasattr(registry, 'query'):
        def query(self, status: str) -> list[dict]:
            return self.list(status=status)
        registry.query = types.MethodType(query, registry)

    # append_audit_log (from #327)
    if not hasattr(registry, 'append_audit_log'):
        def append_audit_log(self, uow_id: str, entry: dict) -> None:
            event = entry.get("event", "unknown")
            note_json = json.dumps(entry)
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note) VALUES (?, ?, ?, NULL, NULL, NULL, ?)",
                    (_now_iso(), uow_id, event, note_json),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        registry.append_audit_log = types.MethodType(append_audit_log, registry)

    return registry


# ---------------------------------------------------------------------------
# Steward import helper — handles pre-merge path setup
# ---------------------------------------------------------------------------

def _import_steward():
    """Import the steward module. Raises ImportError if not yet created."""
    from src.orchestration import steward
    return steward


# ---------------------------------------------------------------------------
# Test: Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_validate_phase2_passes_with_phase2_schema(self, db_path):
        """validate_phase2_schema raises nothing when all Phase 2 fields are present."""
        steward = _import_steward()
        conn = _open_db(db_path)
        try:
            # Should not raise
            steward.validate_phase2_schema(conn)
        finally:
            conn.close()

    def test_validate_phase2_raises_on_missing_field(self, tmp_path):
        """validate_phase2_schema raises RuntimeError when a Phase 2 field is missing."""
        steward = _import_steward()
        # Create a Phase 1-only DB (no Phase 2 columns)
        db_path = tmp_path / "phase1.db"
        conn = _open_db(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS uow_registry (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL DEFAULT 'executable',
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                posture TEXT NOT NULL DEFAULT 'solo',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                uow_id TEXT NOT NULL,
                event TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                agent TEXT,
                note TEXT
            );
        """)
        conn.commit()
        with pytest.raises(RuntimeError, match="schema migration not applied"):
            steward.validate_phase2_schema(conn)
        conn.close()


# ---------------------------------------------------------------------------
# Test: Steward queries ready-for-steward, not pending
# ---------------------------------------------------------------------------

class TestStewardQueryScope:
    def test_steward_queries_ready_for_steward_not_pending(self, db_path, registry):
        """run_steward_cycle only processes ready-for-steward UoWs."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        rfs_id = _make_uow_row(conn, status="ready-for-steward", source_issue_number=42)
        pending_id = _make_uow_row(conn, status="pending", source_issue_number=43)
        conn.close()

        result = steward.run_steward_cycle(
            registry=registry,
            dry_run=True,
            github_client=_mock_github_client_open,
        )

        # Only the ready-for-steward UoW should be considered
        assert result["evaluated"] >= 1
        considered_ids = result.get("considered_ids", [])
        if considered_ids:
            assert rfs_id in considered_ids
            assert pending_id not in considered_ids

    def test_pending_uow_not_diagnosed(self, db_path, registry):
        """A pending UoW must not be transitioned by the Steward."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        pending_id = _make_uow_row(conn, status="pending", source_issue_number=44)
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
        )

        uow = _get_uow(db_path, pending_id)
        assert uow["status"] == "pending", "Steward must not touch pending UoWs"


# ---------------------------------------------------------------------------
# Test: Optimistic lock (claim)
# ---------------------------------------------------------------------------

class TestOptimisticLock:
    def test_claim_sets_status_to_diagnosing(self, db_path, registry):
        """Steward claims UoW by transitioning to 'diagnosing'."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward")
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
        )

        # After processing, status should have changed (to done, ready-for-executor, or blocked)
        uow = _get_uow(db_path, uow_id)
        assert uow["status"] != "ready-for-steward", "UoW should have been claimed and processed"

    def test_concurrent_claim_only_one_proceeds(self, db_path, registry):
        """When two Steward instances race to claim a UoW, only one succeeds."""
        _ensure_registry_has_phase2_methods(registry)

        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward")
        conn.close()

        # Simulate two concurrent claims
        rows1 = registry.transition(uow_id, "diagnosing", "ready-for-steward")
        rows2 = registry.transition(uow_id, "diagnosing", "ready-for-steward")

        assert rows1 == 1, "First claim should succeed"
        assert rows2 == 0, "Second claim should fail (already claimed)"


# ---------------------------------------------------------------------------
# Test: New UoW (steward_cycles == 0) — initialization ritual
# ---------------------------------------------------------------------------

class TestInitializationRitual:
    def test_new_uow_steward_agenda_written_before_prescription(self, db_path, registry, tmp_path):
        """New UoW: steward_agenda written before any prescription."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["steward_agenda"] is not None, "steward_agenda must be written on first contact"
        # Audit log must contain agenda_update event
        entries = _audit_entries(db_path, uow_id)
        events = [e["event"] for e in entries]
        assert "agenda_update" in events, "audit_log must contain agenda_update event"

    def test_new_uow_steward_log_written(self, db_path, registry, tmp_path):
        """New UoW: steward_log contains initial entry after first cycle."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["steward_log"] is not None, "steward_log must have an entry after first cycle"
        # Parse and verify structure
        log_lines = [l for l in uow["steward_log"].strip().split("\n") if l.strip()]
        assert len(log_lines) >= 1
        first_entry = json.loads(log_lines[0])
        assert "event" in first_entry


# ---------------------------------------------------------------------------
# Test: Completion path — execution_complete + valid output_ref
# ---------------------------------------------------------------------------

class TestCompletionPath:
    def test_uow_with_valid_output_declared_done(self, db_path, registry, tmp_path):
        """UoW with execution_complete + valid output_ref + result file is declared done."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        output_file = tmp_path / "output.txt"
        output_file.write_text("Task completed successfully. All acceptance criteria met.")

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "observation_complete", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=str(output_file),
            audit_log_entries=audit_entries,
            success_criteria="Task completed successfully.",
        )
        conn.close()

        # Write structured result file with uow_id and outcome (executor-contract.md §Schema)
        result_file = tmp_path / "output.result.json"
        result_file.write_text(json.dumps({
            "uow_id": uow_id,
            "outcome": "complete",
            "success": True,
            "reason": "all criteria met",
        }))

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "done", "UoW with valid output should be declared done"
        assert uow["completed_at"] is not None, "completed_at must be set on closure"

        # Closure event in audit_log
        entries = _audit_entries(db_path, uow_id)
        events = [e["event"] for e in entries]
        assert "steward_closure" in events

    def test_closure_marks_agenda_nodes_complete(self, db_path, registry, tmp_path):
        """On closure, steward_agenda nodes are marked complete."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        output_file = tmp_path / "output.txt"
        output_file.write_text("Task completed successfully.")

        agenda = json.dumps([
            {"posture": "solo", "context": "initial", "constraints": [], "status": "prescribed"},
        ])
        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "observation_complete", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=str(output_file),
            audit_log_entries=audit_entries,
            steward_agenda=agenda,
            success_criteria="Task completed successfully.",
        )
        conn.close()

        # Write structured result file with uow_id and outcome (executor-contract.md §Schema)
        result_file = tmp_path / "output.result.json"
        result_file.write_text(json.dumps({
            "uow_id": uow_id,
            "outcome": "complete",
            "success": True,
            "reason": "task done",
        }))

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        if uow["steward_agenda"]:
            agenda_nodes = json.loads(uow["steward_agenda"])
            for node in agenda_nodes:
                assert node["status"] == "complete", "All agenda nodes must be complete on closure"


# ---------------------------------------------------------------------------
# Test: Crash recovery — prescribes another pass (cycles < 2)
# ---------------------------------------------------------------------------

class TestCrashRecovery:
    def test_crashed_no_output_cycles_lt_2_prescribes(self, db_path, registry, tmp_path):
        """crashed_no_output with steward_cycles=1 → prescribes another pass."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        audit_entries = [
            {"event": "startup_sweep", "actor": "steward",
             "classification": "crashed_no_output",
             "return_reason": "crashed_no_output", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=None,
            audit_log_entries=audit_entries,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-executor", (
            "crashed_no_output with cycles < 2 should prescribe another pass"
        )
        assert uow["steward_cycles"] == 2, "steward_cycles must be incremented"

    def test_diagnosis_audit_written_before_prescription(self, db_path, registry, tmp_path):
        """Diagnosis audit entry is written BEFORE any prescription transition."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        entries = _audit_entries(db_path, uow_id)
        events = [e["event"] for e in entries]

        # diagnosis event must appear before prescription/transition events
        if "steward_diagnosis" in events and "steward_prescription" in events:
            diag_idx = events.index("steward_diagnosis")
            presc_idx = events.index("steward_prescription")
            assert diag_idx < presc_idx, "Diagnosis audit must be written before prescription"


# ---------------------------------------------------------------------------
# Test: Hard cap (steward_cycles >= 5)
# ---------------------------------------------------------------------------

class TestHardCap:
    def test_hard_cap_surfaces_to_dan_not_prescribes(self, db_path, registry, tmp_path):
        """steward_cycles >= 5 → surfaces to Dan, does not prescribe or close."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            uow_id = uow.id if hasattr(uow, "id") else uow["id"]
            notifications.append({"uow_id": uow_id, "condition": condition})

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=5,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "blocked", "hard cap must set status to blocked"
        assert len(notifications) == 1, "Dan must be notified exactly once"
        assert notifications[0]["condition"] == "hard_cap"

    def test_hard_cap_at_exactly_5(self, db_path, registry, tmp_path):
        """steward_cycles == 5 fires the hard cap (>=5)."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()
        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append(condition)

        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward", steward_cycles=5)
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        assert "hard_cap" in notifications

    def test_cycles_4_does_not_fire_hard_cap(self, db_path, registry, tmp_path):
        """steward_cycles == 4 does NOT fire the hard cap."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()
        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append(condition)

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=4,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        assert "hard_cap" not in notifications, "Cycles=4 must not fire hard cap"
        uow = _get_uow(db_path, uow_id)
        assert uow["status"] != "blocked"


# ---------------------------------------------------------------------------
# Test: crashed_no_output + cycles >= 2 → surface to Dan
# ---------------------------------------------------------------------------

class TestCrashedSurface:
    def test_crashed_no_output_cycles_ge_2_surfaces(self, db_path, registry, tmp_path):
        """crashed_no_output + steward_cycles >= 2 → surface to Dan."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()
        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append(condition)

        audit_entries = [
            {"event": "startup_sweep", "classification": "crashed_no_output",
             "return_reason": "crashed_no_output", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=2,
            output_ref=None,
            audit_log_entries=audit_entries,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        assert any("crash" in c for c in notifications), (
            f"Expected crash surface condition, got: {notifications}"
        )
        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "blocked"


# ---------------------------------------------------------------------------
# Test: executor_orphan → clean first execution posture
# ---------------------------------------------------------------------------

class TestExecutorOrphan:
    def test_executor_orphan_treated_as_clean_first_execution(self, db_path, registry, tmp_path):
        """executor_orphan return_reason → first-execution posture, not crash threshold."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()
        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append(condition)

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "executor_orphan", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=2,
            output_ref=None,
            audit_log_entries=audit_entries,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        # executor_orphan should not trigger crash surface (even at cycles >= 2)
        assert not any("crash" in c for c in notifications), (
            f"executor_orphan must not apply crash threshold. Notifications: {notifications}"
        )
        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-executor", (
            "executor_orphan should result in a clean first-execution prescription"
        )


# ---------------------------------------------------------------------------
# Test: BOOTUP_CANDIDATE_GATE
# ---------------------------------------------------------------------------

class TestBootupCandidateGate:
    def test_gate_true_skips_bootup_candidate(self, db_path, registry, tmp_path):
        """BOOTUP_CANDIDATE_GATE=True → bootup-candidate UoWs skipped."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            source_issue_number=271,  # bootup-candidate
        )
        conn.close()

        def github_client_with_bootup_label(issue_number):
            return {
                "status_code": 200,
                "state": "open",
                "labels": ["bootup-candidate"],
                "body": "Test issue body",
                "title": "Test issue",
            }

        result = steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=github_client_with_bootup_label,
            artifact_dir=tmp_path / "artifacts",
            bootup_candidate_gate=True,
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-steward", (
            "bootup-candidate UoW must not be processed when gate is True"
        )

    def test_gate_false_processes_bootup_candidate(self, db_path, registry, tmp_path):
        """BOOTUP_CANDIDATE_GATE=False → bootup-candidate UoWs are processed normally."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
        )
        conn.close()

        def github_client_with_bootup_label(issue_number):
            return {
                "status_code": 200,
                "state": "open",
                "labels": ["bootup-candidate"],
                "body": "Test issue body",
                "title": "Test issue",
            }

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=github_client_with_bootup_label,
            artifact_dir=tmp_path / "artifacts",
            bootup_candidate_gate=False,
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] != "ready-for-steward", (
            "bootup-candidate UoW should be processed when gate is False"
        )


# ---------------------------------------------------------------------------
# Test: Re-entry — agenda updated, log appended (not overwritten)
# ---------------------------------------------------------------------------

class TestReentry:
    def test_reentry_steward_log_appended_not_overwritten(self, db_path, registry, tmp_path):
        """Re-entry cycle appends to steward_log; does not overwrite."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        existing_log = json.dumps({"event": "diagnosis", "uow_id": "x", "steward_cycles": 1,
                                   "re_entry_posture": "normal", "timestamp": _now_iso()})
        agenda = json.dumps([
            {"posture": "solo", "context": "initial pass", "constraints": [],
             "status": "prescribed"},
        ])
        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "needs_steward_review", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=None,  # output not sufficient
            audit_log_entries=audit_entries,
            steward_agenda=agenda,
            steward_log=existing_log,
            success_criteria="Must produce artifact",
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["steward_log"] is not None
        log_text = uow["steward_log"]
        # Original entry must still be present
        assert existing_log.splitlines()[0][:30] in log_text or \
               '"event": "diagnosis"' in log_text, (
                   "Original steward_log entry must not be overwritten"
               )
        # New entry must have been appended
        log_lines = [l for l in log_text.strip().split("\n") if l.strip()]
        assert len(log_lines) >= 2, "steward_log must have at least 2 entries after re-entry"


# ---------------------------------------------------------------------------
# Test: WorkflowArtifact path is absolute
# ---------------------------------------------------------------------------

class TestWorkflowArtifactPath:
    def test_artifact_path_is_absolute(self, db_path, registry, tmp_path):
        """Workflow artifact written to absolute path, no tilde prefix."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        artifact_dir = tmp_path / "artifacts"
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
        )

        uow = _get_uow(db_path, uow_id)
        if uow.get("workflow_artifact"):
            path = uow["workflow_artifact"]
            assert path.startswith("/"), f"workflow_artifact path must be absolute, got: {path}"
            assert "~" not in path, "workflow_artifact path must not contain tilde"

    def test_artifact_file_exists_after_prescription(self, db_path, registry, tmp_path):
        """WorkflowArtifact JSON file is written to disk on prescription."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        artifact_dir = tmp_path / "artifacts"
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
        )

        uow = _get_uow(db_path, uow_id)
        if uow.get("workflow_artifact"):
            assert Path(uow["workflow_artifact"]).exists(), (
                f"WorkflowArtifact file must exist at {uow['workflow_artifact']}"
            )

    def test_workflow_artifact_before_status_transition(self, db_path, registry, tmp_path):
        """workflow_artifact and prescribed_skills are written before status → ready-for-executor."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()
        # Capture order of writes by patching transition
        call_log = []

        original_transition = registry.transition
        def logging_transition(uow_id, to_status, where_status):
            # Record DB state at transition time
            uow = _get_uow(db_path, uow_id)
            call_log.append({
                "event": "transition",
                "to_status": to_status,
                "workflow_artifact": uow.get("workflow_artifact"),
            })
            return original_transition(uow_id, to_status, where_status)
        import types
        registry.transition = types.MethodType(
            lambda self, uow_id, to_status, where_status: logging_transition(uow_id, to_status, where_status),
            registry
        )

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        executor_transitions = [c for c in call_log if c["to_status"] == "ready-for-executor"]
        for t in executor_transitions:
            assert t["workflow_artifact"] is not None, (
                "workflow_artifact must be written before status → ready-for-executor"
            )


# ---------------------------------------------------------------------------
# Test: Dry-run mode — no state changes
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_no_state_changes(self, db_path, registry, tmp_path):
        """Dry-run mode: diagnose without writing artifacts or transitioning state."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=True,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-steward", (
            "Dry-run must not change UoW status"
        )

    def test_dry_run_no_artifact_written(self, db_path, registry, tmp_path):
        """Dry-run mode: no WorkflowArtifact file is written to disk."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
        )
        conn.close()

        artifact_dir = tmp_path / "artifacts"
        steward.run_steward_cycle(
            registry=registry,
            dry_run=True,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
        )

        if artifact_dir.exists():
            artifacts = list(artifact_dir.glob("*.json"))
            assert len(artifacts) == 0, "Dry-run must not write any artifact files"


# ---------------------------------------------------------------------------
# Test: steward_cycles incremented on prescription
# ---------------------------------------------------------------------------

class TestStewardCycles:
    def test_steward_cycles_incremented_on_prescription(self, db_path, registry, tmp_path):
        """steward_cycles is incremented from 0 to 1 on first prescription."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        if uow["status"] == "ready-for-executor":
            assert uow["steward_cycles"] == 1, "steward_cycles must be incremented to 1"


# ---------------------------------------------------------------------------
# Test: route_reason written at prescription time
# ---------------------------------------------------------------------------

class TestRouteReason:
    def test_route_reason_written_on_prescription(self, db_path, registry, tmp_path):
        """route_reason is written at prescription time."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        if uow["status"] == "ready-for-executor":
            assert uow["route_reason"] is not None, "route_reason must be set on prescription"
            assert len(uow["route_reason"]) > 0


# ---------------------------------------------------------------------------
# Test: Observability — structured log events
# ---------------------------------------------------------------------------

class TestObservability:
    def test_diagnosis_event_in_audit_log(self, db_path, registry, tmp_path):
        """Every diagnosis pass writes a structured event to audit_log."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        entries = _audit_entries(db_path, uow_id)
        events = [e["event"] for e in entries]
        assert "steward_diagnosis" in events or "diagnosis" in events, (
            "diagnosis event must be written to audit_log"
        )

    def test_prescription_event_in_audit_log(self, db_path, registry, tmp_path):
        """Every prescription writes a structured event to audit_log."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        entries = _audit_entries(db_path, uow_id)
        events = [e["event"] for e in entries]
        if _get_uow(db_path, uow_id)["status"] == "ready-for-executor":
            assert "steward_prescription" in events or "prescription" in events, (
                "prescription event must be in audit_log"
            )


# ---------------------------------------------------------------------------
# Test: BOOTUP_CANDIDATE_GATE constant defined at module level
# ---------------------------------------------------------------------------

class TestModuleConstants:
    def test_bootup_candidate_gate_constant_exists(self):
        """BOOTUP_CANDIDATE_GATE is defined at module level and defaults to True."""
        steward = _import_steward()
        assert hasattr(steward, "BOOTUP_CANDIDATE_GATE"), (
            "BOOTUP_CANDIDATE_GATE must be defined at module level"
        )
        assert steward.BOOTUP_CANDIDATE_GATE is True, (
            "BOOTUP_CANDIDATE_GATE must default to True"
        )


# ---------------------------------------------------------------------------
# Test: Early warning at cycle 4 (item 12)
# ---------------------------------------------------------------------------

class TestEarlyWarningAt4:
    def test_early_warning_fires_when_prescription_reaches_cycle_4(self, db_path, registry, tmp_path):
        """When a prescription results in new steward_cycles == 4, an early warning is sent."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []

        def capture_early_warning(uow, return_reason):
            uow_id = uow.id if hasattr(uow, "id") else uow["id"]
            early_warnings.append({"uow_id": uow_id, "return_reason": return_reason})

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "needs_steward_review", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        # steward_cycles=3: after prescription it becomes 4 → early warning
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=3,
            output_ref=None,
            audit_log_entries=audit_entries,
            success_criteria="Must produce artifact",
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan_early_warning=capture_early_warning,
        )

        uow = _get_uow(db_path, uow_id)
        # Must have been prescribed (status ready-for-executor, cycles=4)
        assert uow["status"] == "ready-for-executor", (
            "UoW at cycle 3 with no output should be prescribed (status=ready-for-executor)"
        )
        assert uow["steward_cycles"] == 4, "steward_cycles must be 4 after prescription"
        assert len(early_warnings) == 1, (
            f"Early warning must fire exactly once when new_cycles == 4, got: {early_warnings}"
        )
        assert early_warnings[0]["uow_id"] == uow_id

    def test_early_warning_not_fired_at_cycle_3(self, db_path, registry, tmp_path):
        """No early warning when new steward_cycles is 3 (only fires at exactly 4)."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []

        def capture_early_warning(uow, return_reason):
            early_warnings.append(return_reason)

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "needs_steward_review", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        # steward_cycles=2: after prescription it becomes 3 → no early warning
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=2,
            output_ref=None,
            audit_log_entries=audit_entries,
            success_criteria="Must produce artifact",
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan_early_warning=capture_early_warning,
        )

        assert len(early_warnings) == 0, (
            f"Early warning must not fire at new_cycles=3, got: {early_warnings}"
        )

    def test_early_warning_not_fired_at_hard_cap(self, db_path, registry, tmp_path):
        """When steward_cycles == 5 (hard cap), early warning must not fire — surface fires instead."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []
        surface_calls = []

        def capture_early_warning(uow, return_reason):
            early_warnings.append(return_reason)

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            surface_calls.append(condition)

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=5,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
            notify_dan_early_warning=capture_early_warning,
        )

        assert len(early_warnings) == 0, (
            "Early warning must not fire at hard cap — surface fires instead"
        )
        assert "hard_cap" in surface_calls, "Hard cap surface must fire at cycles=5"


# ---------------------------------------------------------------------------
# Test: Hard cap surface message includes return_reason (item 12)
# ---------------------------------------------------------------------------

class TestHardCapSurfaceIncludesReturnReason:
    def test_hard_cap_notification_includes_return_reason(self, db_path, registry, tmp_path):
        """At hard cap (cycles >= 5), the surface notification carries return_reason."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append({"condition": condition, "return_reason": return_reason})

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "execution_failed", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=5,
            output_ref=None,
            audit_log_entries=audit_entries,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        assert len(notifications) == 1, "Hard cap must surface exactly once"
        n = notifications[0]
        assert n["condition"] == "hard_cap"
        assert n["return_reason"] == "execution_failed", (
            f"Hard cap surface must include return_reason='execution_failed', got: {n['return_reason']!r}"
        )

    def test_hard_cap_surface_return_reason_none_when_no_audit(self, db_path, registry, tmp_path):
        """Hard cap surface with no prior audit entries sends return_reason=None (not a crash)."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append({"condition": condition, "return_reason": return_reason})

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=5,
            output_ref=None,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        assert len(notifications) == 1
        assert notifications[0]["condition"] == "hard_cap"
        # return_reason is None when there are no audit entries recording a return_reason
        assert notifications[0]["return_reason"] is None


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _mock_github_client_open(issue_number: int) -> dict:
    """Mock GitHub client: issue is open, no labels."""
    return {
        "status_code": 200,
        "state": "open",
        "labels": [],
        "body": f"Issue #{issue_number}: implement this feature.\n\nAcceptance criteria:\n- Feature works",
        "title": f"Test issue {issue_number}",
    }
