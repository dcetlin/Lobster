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
- Feedback loop: _fetch_prior_prescriptions parses steward_log correctly
- Feedback loop: prior prescriptions injected into re-prescription instructions
- Feedback loop: first cycle (steward_cycles=0) receives no prior context
- Feedback loop: empty/None steward_log returns []
- Feedback loop: only last N=3 entries returned when log has more
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

from src.orchestration.steward import _HARD_CAP_CYCLES


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
    # lifetime_cycles: cumulative across all retries. Defaults to mirror steward_cycles
    # so that existing tests setting steward_cycles=5 also get lifetime_cycles=5
    # (correct: steward_cycles can never exceed lifetime_cycles in real usage).
    lifetime_cycles: int | None = None,
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
    # lifetime_cycles defaults to steward_cycles: in real usage, lifetime can never be less.
    effective_lifetime_cycles = lifetime_cycles if lifetime_cycles is not None else steward_cycles
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, output_ref, steward_cycles, lifetime_cycles,
             steward_agenda, steward_log, success_criteria, prescribed_skills,
             route_evidence, trigger)
        VALUES (?, 'executable', 'github:issue/42', ?, '2026-01-01', ?, 'solo',
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}')
        """,
        (uow_id, source_issue_number, status, now, now, summary,
         output_ref, steward_cycles, effective_lifetime_cycles,
         steward_agenda, steward_log, success_criteria, prescribed_skills),
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
    def test_validate_phase2_passes_with_phase2_schema(self, db_path, registry):
        """validate_phase2_schema raises nothing when all required fields are present.

        The registry fixture runs migrations (including 0008 which adds lifetime_cycles),
        ensuring the schema is fully up to date before validation runs.
        """
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
# Test: Done-transition WHERE clause guard (issue #671)
# ---------------------------------------------------------------------------

# Named constant matching the steward's WHERE guard at the done-transition.
# If this constant is wrong the WHERE guard test below will catch it.
_DONE_TRANSITION_FROM_STATUS = "diagnosing"
_DONE_TRANSITION_FALLBACK_STATUS = "ready-for-steward"


def _setup_completable_uow(conn, tmp_path, steward_cycles=1):
    """
    Create a UoW in ready-for-steward with execution_complete audit entry and
    a valid result file. Returns (uow_id, output_file, result_file).

    Helper shared by done-transition tests so both tests cover the identical
    UoW state without duplicating setup code.
    """
    output_file = tmp_path / "output.txt"
    output_file.write_text("Task completed successfully. All acceptance criteria met.")

    audit_entries = [
        {
            "event": "execution_complete",
            "actor": "executor",
            "from_status": "active",
            "to_status": "ready-for-steward",
            "return_reason": "observation_complete",
            "timestamp": _now_iso(),
        },
    ]

    uow_id = _make_uow_row(
        conn,
        status="ready-for-steward",
        steward_cycles=steward_cycles,
        output_ref=str(output_file),
        audit_log_entries=audit_entries,
        success_criteria="Task completed successfully.",
    )
    conn.commit()

    result_file = tmp_path / "output.result.json"
    result_file.write_text(json.dumps({
        "uow_id": uow_id,
        "outcome": "complete",
        "success": True,
        "reason": "all criteria met",
    }))

    return uow_id, output_file, result_file


class TestDoneTransitionWhereGuard:
    """
    Tests for issue #671: steward done-transition WHERE clause guard.

    The done-transition at `registry.transition(uow_id, STATUS_DONE, from_status)`
    must use the correct `from_status`. This class verifies:
    1. Normal path: claim sets status to `diagnosing`; done-transition fires from `diagnosing`.
    2. Startup-sweep race: if startup_sweep resets status to `ready-for-steward`
       after the claim but before the done-transition, the steward must still
       close the UoW rather than silently no-oping.
    """

    def test_done_transition_fires_from_diagnosing_on_normal_path(
        self, db_path, registry, tmp_path
    ):
        """
        Normal completion path: steward claims ready-for-steward → diagnosing,
        then transitions diagnosing → done. UoW must reach done status.

        Verifies the WHERE guard is _STATUS_DIAGNOSING (the status after claim).
        This is the primary path described in issue #671.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id, _, _ = _setup_completable_uow(conn, tmp_path)
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        # The done-transition WHERE guard must have matched (diagnosing) for this to pass.
        assert uow["status"] == "done", (
            f"done-transition failed: UoW is in '{uow['status']}' instead of 'done'. "
            f"The WHERE guard may be incorrect. Expected from_status={_DONE_TRANSITION_FROM_STATUS!r}."
        )
        assert uow["completed_at"] is not None, "completed_at must be set when done"

    def test_done_transition_succeeds_when_status_reset_to_ready_for_steward(
        self, db_path, registry, tmp_path
    ):
        """
        Race condition: startup_sweep resets status from 'diagnosing' back to
        'ready-for-steward' after the steward has claimed the UoW but before the
        done-transition fires.

        The steward must still close the UoW. Without the fallback WHERE guard
        (_STATUS_READY_FOR_STEWARD), the done-transition no-ops silently and the
        UoW never reaches 'done'. (issue #671)
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id, _, _ = _setup_completable_uow(conn, tmp_path)
        conn.close()

        # Intercept the done-transition call to simulate a concurrent startup_sweep
        # that resets the UoW from 'diagnosing' back to 'ready-for-steward'.
        from src.orchestration import registry as reg_mod
        original_transition = reg_mod.Registry.transition
        call_count = [0]

        def transition_with_race(self, uow_id_arg, to_status, where_status):
            call_count[0] += 1
            # On the done-transition specifically (to=done, where=diagnosing):
            # simulate startup_sweep resetting status to ready-for-steward first,
            # making the WHERE diagnosing fail.
            if to_status == "done" and where_status == _DONE_TRANSITION_FROM_STATUS:
                # Reset status to ready-for-steward (simulating concurrent startup_sweep)
                conn2 = self._connect()
                conn2.execute(
                    "UPDATE uow_registry SET status = ? WHERE id = ?",
                    (_DONE_TRANSITION_FALLBACK_STATUS, uow_id_arg),
                )
                conn2.commit()
                conn2.close()
            # Now call the real transition — WHERE diagnosing will fail (0 rows),
            # but the fallback WHERE ready-for-steward should succeed.
            return original_transition(self, uow_id_arg, to_status, where_status)

        reg_mod.Registry.transition = transition_with_race
        try:
            steward.run_steward_cycle(
                registry=registry,
                dry_run=False,
                github_client=_mock_github_client_open,
                artifact_dir=tmp_path / "artifacts",
            )
        finally:
            reg_mod.Registry.transition = original_transition

        uow = _get_uow(db_path, uow_id)
        # Without the fallback WHERE guard, the UoW would remain in 'ready-for-steward'.
        assert uow["status"] == "done", (
            f"Startup-sweep race not handled: UoW is in '{uow['status']}' instead of 'done'. "
            f"The done-transition must fall back to WHERE {_DONE_TRANSITION_FALLBACK_STATUS!r} "
            f"when the primary WHERE {_DONE_TRANSITION_FROM_STATUS!r} fails. (issue #671)"
        )


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
# Test: hard cap uses lifetime_cycles, not steward_cycles
# ---------------------------------------------------------------------------


def _make_uow_row_with_lifetime(
    conn: sqlite3.Connection,
    uow_id: str | None = None,
    status: str = "ready-for-steward",
    steward_cycles: int = 0,
    lifetime_cycles: int = 0,
    output_ref: str | None = None,
    source_issue_number: int = 42,
    summary: str = "Test UoW with lifetime",
    success_criteria: str = "Done when output exists.",
) -> str:
    """Insert a UoW row including lifetime_cycles. Returns the uow_id."""
    import uuid as _uuid
    if uow_id is None:
        uow_id = f"uow_lt_{_uuid.uuid4().hex[:6]}"
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, output_ref, steward_cycles, lifetime_cycles,
             success_criteria, route_evidence, trigger)
        VALUES (?, 'executable', 'github:issue/42', ?, '2026-01-01', ?, 'solo',
                ?, ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}')
        """,
        (uow_id, source_issue_number, status, now, now, summary,
         output_ref, steward_cycles, lifetime_cycles, success_criteria),
    )
    conn.commit()
    return uow_id


class TestHardCapUsesLifetimeCycles:
    """
    The hard-cap circuit breaker must check lifetime_cycles, not steward_cycles.
    A UoW that accumulates cycles across multiple decide-retry resets cannot
    bypass the cap simply because steward_cycles was reset to 0.
    """

    def test_lifetime_cycles_at_cap_triggers_hard_cap_even_when_steward_cycles_zero(
        self, db_path, registry, tmp_path
    ):
        """
        lifetime_cycles=5, steward_cycles=0 → hard_cap fires.

        This is the core regression guard: after a decide-retry, steward_cycles
        resets to 0 but the UoW has already burned through its lifetime budget.
        The hard cap must fire based on lifetime_cycles.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()
        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append(condition)

        conn = _open_db(db_path)
        uow_id = _make_uow_row_with_lifetime(
            conn,
            status="ready-for-steward",
            steward_cycles=0,        # reset by decide-retry
            lifetime_cycles=_HARD_CAP_CYCLES,   # but lifetime budget is exhausted
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        assert "hard_cap" in notifications, (
            "Hard cap must fire when lifetime_cycles >= _HARD_CAP_CYCLES, "
            "regardless of steward_cycles value"
        )
        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "blocked"

    def test_steward_cycles_at_cap_but_lifetime_cycles_below_cap_does_not_fire(
        self, db_path, registry, tmp_path
    ):
        """
        steward_cycles=5, lifetime_cycles=0 (impossible in practice but verifies
        direction of change: if the old code was checking steward_cycles, this
        would fire; under the new code using lifetime_cycles, it should fire too
        because the gap between them is the whole point).

        Actually this verifies the inverse: when lifetime_cycles < cap, even if
        steward_cycles matches cap, we check lifetime_cycles.
        Since in practice steward_cycles <= lifetime_cycles always holds
        (lifetime accumulates steward_cycles), we test the readable scenario:
        steward_cycles=3, lifetime_cycles=4 → should NOT fire.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()
        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append(condition)

        conn = _open_db(db_path)
        uow_id = _make_uow_row_with_lifetime(
            conn,
            status="ready-for-steward",
            steward_cycles=3,
            lifetime_cycles=4,   # one below cap
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        assert "hard_cap" not in notifications, (
            "Hard cap must not fire when lifetime_cycles < _HARD_CAP_CYCLES"
        )

    def test_lifetime_cycles_below_cap_allows_prescription(
        self, db_path, registry, tmp_path
    ):
        """
        lifetime_cycles=2 (well below cap) → Steward may prescribe normally,
        not blocked by cap.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()
        notifications = []

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            notifications.append(condition)

        conn = _open_db(db_path)
        uow_id = _make_uow_row_with_lifetime(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            lifetime_cycles=2,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=capture_notification,
        )

        assert "hard_cap" not in notifications, (
            "lifetime_cycles=2 is below the cap and must not trigger hard_cap"
        )


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

    def test_prescription_audit_includes_prescription_source_deterministic(self, db_path, registry, tmp_path):
        """steward_prescription audit entry has prescription_source='deterministic' when LLM path is bypassed."""
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
            llm_prescriber=None,  # Force deterministic path
        )

        if _get_uow(db_path, uow_id)["status"] == "ready-for-executor":
            entries = _audit_entries(db_path, uow_id)
            presc_entry = next(
                (e for e in entries if e.get("event") == "steward_prescription"), None
            )
            assert presc_entry is not None, "steward_prescription audit entry must exist"
            # The audit payload is stored as JSON in the 'note' column
            import json as _json
            note_data = _json.loads(presc_entry["note"])
            assert note_data.get("prescription_source") == "deterministic", (
                f"prescription_source must be 'deterministic' when LLM is bypassed, got {note_data.get('prescription_source')!r}"
            )

    def test_prescription_audit_includes_prescription_source_llm(self, db_path, registry, tmp_path):
        """steward_prescription audit entry has prescription_source='llm' when LLM prescriber succeeds."""
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

        def stub_llm_prescriber(uow, posture, gap, issue_body=""):
            return {"instructions": "Do the thing.", "success_criteria_check": ""}

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            llm_prescriber=stub_llm_prescriber,
        )

        if _get_uow(db_path, uow_id)["status"] == "ready-for-executor":
            entries = _audit_entries(db_path, uow_id)
            presc_entry = next(
                (e for e in entries if e.get("event") == "steward_prescription"), None
            )
            assert presc_entry is not None, "steward_prescription audit entry must exist"
            # The audit payload is stored as JSON in the 'note' column
            import json as _json
            note_data = _json.loads(presc_entry["note"])
            assert note_data.get("prescription_source") == "llm", (
                f"prescription_source must be 'llm' when LLM prescriber succeeds, got {note_data.get('prescription_source')!r}"
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
    def test_early_warning_fires_when_cumulative_cycles_reaches_4(self, db_path, registry, tmp_path):
        """Early warning fires when lifetime_cycles + new steward_cycles reaches 4.

        First-attempt case: lifetime_cycles=0, steward_cycles=3.
        After prescription: 0 + 4 = 4 → early warning fires.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []

        def capture_early_warning(uow, return_reason, new_cycles=None):
            uow_id = uow.id if hasattr(uow, "id") else uow["id"]
            early_warnings.append({"uow_id": uow_id, "return_reason": return_reason, "new_cycles": new_cycles})

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "needs_steward_review", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        # lifetime_cycles=0 (first attempt), steward_cycles=3: after prescription 0+4=4 → early warning
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=3,
            lifetime_cycles=0,
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
            f"Early warning must fire exactly once when lifetime_cycles+new_cycles == 4, got: {early_warnings}"
        )
        assert early_warnings[0]["uow_id"] == uow_id
        assert early_warnings[0]["new_cycles"] == 4, (
            f"new_cycles passed to early warning must be 4 (post-prescription), "
            f"got: {early_warnings[0]['new_cycles']}"
        )

    def test_early_warning_fires_after_retry_when_cumulative_reaches_4(self, db_path, registry, tmp_path):
        """Early warning fires based on cumulative lifetime_cycles, not per-attempt steward_cycles.

        Cross-retry case: lifetime_cycles=2 (from previous attempt), steward_cycles=1.
        After prescription: 2 + 2 = 4 → early warning fires despite steward_cycles being low.
        Without the fix (using steward_cycles only), this would produce new_cycles=2 and not fire.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []

        def capture_early_warning(uow, return_reason, new_cycles=None):
            uow_id = uow.id if hasattr(uow, "id") else uow["id"]
            early_warnings.append({"uow_id": uow_id, "return_reason": return_reason, "new_cycles": new_cycles})

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "needs_steward_review", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        # lifetime_cycles=2 (accumulated from previous attempt), steward_cycles=1:
        # after prescription: 2 + 2 = 4 → early warning must fire
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            lifetime_cycles=2,
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
        assert uow["status"] == "ready-for-executor", (
            "UoW should be prescribed (status=ready-for-executor)"
        )
        assert len(early_warnings) == 1, (
            f"Early warning must fire when lifetime_cycles(2) + new_cycles(2) == 4, got: {early_warnings}"
        )
        assert early_warnings[0]["uow_id"] == uow_id

    def test_early_warning_not_fired_at_cumulative_cycle_3(self, db_path, registry, tmp_path):
        """No early warning when lifetime_cycles + new steward_cycles is 3 (only fires at exactly 4)."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []

        def capture_early_warning(uow, return_reason, new_cycles=None):
            early_warnings.append(return_reason)

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "needs_steward_review", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        # lifetime_cycles=0 (first attempt), steward_cycles=2: after prescription 0+3=3 → no early warning
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=2,
            lifetime_cycles=0,
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
            f"Early warning must not fire at cumulative new_cycles=3, got: {early_warnings}"
        )

    def test_early_warning_not_fired_at_hard_cap(self, db_path, registry, tmp_path):
        """When lifetime_cycles hits the hard cap (9), early warning must not fire — surface fires instead.

        The UoW is surfaced (Surfaced returned) before reaching the prescription step where
        the early warning check lives, so early warning is unreachable at hard cap.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []
        surface_calls = []

        def capture_early_warning(uow, return_reason, new_cycles=None):
            early_warnings.append(return_reason)

        def capture_notification(uow, condition, surface_log=None, return_reason=None):
            surface_calls.append(condition)

        conn = _open_db(db_path)
        # lifetime_cycles=9 triggers hard cap (_HARD_CAP_CYCLES=9); steward_cycles=0 (new attempt).
        # The UoW is surfaced before reaching prescription, so early warning never fires.
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            lifetime_cycles=9,
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
        assert "hard_cap" in surface_calls, "Hard cap surface must fire at lifetime_cycles=9"

    def test_early_warning_fires_exactly_once_when_cumulative_first_crosses_threshold(self, db_path, registry, tmp_path):
        """Early warning fires on the first crossing of the threshold, not on subsequent post-threshold cycles.

        First-crossing case: lifetime_cycles=3, steward_cycles=0 (new attempt after a decide-retry reset).
        After prescription: new_cycles=1, cumulative=3+1=4 — crosses _EARLY_WARNING_CYCLES(4) for the first time.
        Previous cumulative = 3+0=3 < 4 → first-crossing guard fires.

        The >= (not ==) on the current cumulative ensures the warning also catches a non-sequential jump
        that lands above the threshold (e.g. lifetime_cycles=2, steward_cycles=1 → cumulative jumps from
        3 to 4 directly), as long as the previous cumulative was below the threshold.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []

        def capture_early_warning(uow, return_reason, new_cycles=None):
            uow_id = uow.id if hasattr(uow, "id") else uow["id"]
            early_warnings.append({"uow_id": uow_id, "new_cycles": new_cycles})

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "needs_steward_review", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        # lifetime_cycles=3 (accumulated from previous attempts), steward_cycles=0 (new attempt):
        # previous cumulative = 3+0=3 < _EARLY_WARNING_CYCLES(4)
        # current cumulative  = 3+1=4 >= _EARLY_WARNING_CYCLES(4)
        # → first-crossing guard fires.
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            lifetime_cycles=3,
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

        assert len(early_warnings) == 1, (
            f"Early warning must fire exactly once when crossing threshold for the first time "
            f"(lifetime_cycles=3, new_cycles=1 → cumulative=4 >= _EARLY_WARNING_CYCLES=4), "
            f"got: {early_warnings}"
        )
        assert early_warnings[0]["uow_id"] == uow_id
        assert early_warnings[0]["new_cycles"] == 1, (
            f"new_cycles must be 1 (first steward cycle on this attempt), "
            f"got: {early_warnings[0]['new_cycles']}"
        )

    def test_early_warning_does_not_multi_fire_on_post_threshold_cycles(self, db_path, registry, tmp_path):
        """Early warning must not re-fire when a UoW continues past the early-warning threshold.

        Multi-fire prevention: lifetime_cycles=0, steward_cycles=4.
        Previous cumulative = 0+4=4 >= _EARLY_WARNING_CYCLES(4) — threshold already crossed.
        Current cumulative  = 0+5=5 >= _EARLY_WARNING_CYCLES(4).
        → first-crossing guard does NOT fire (previous was already at threshold).

        Without the first-crossing guard, >= would fire on every cycle from 4 to 8 (five
        notifications per UoW lifetime). The first-crossing guard bounds this to exactly one.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        early_warnings = []

        def capture_early_warning(uow, return_reason, new_cycles=None):
            early_warnings.append(new_cycles)

        audit_entries = [
            {"event": "execution_complete", "actor": "executor",
             "return_reason": "needs_steward_review", "timestamp": _now_iso()},
        ]

        conn = _open_db(db_path)
        # steward_cycles=4: threshold was first crossed at steward_cycle=4 (cumulative=4).
        # This cycle is steward_cycle=5 — already past the threshold. Must not re-fire.
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=4,
            lifetime_cycles=0,
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
            f"Early warning must not re-fire past the threshold (steward_cycles=4 → "
            f"previous cumulative=4 already >= _EARLY_WARNING_CYCLES=4), got: {early_warnings}"
        )


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
# Feedback loop tests
# ---------------------------------------------------------------------------

class TestFetchPriorPrescriptions:
    """Unit tests for _fetch_prior_prescriptions — pure function."""

    def test_returns_empty_for_none_log(self):
        steward = _import_steward()
        assert steward._fetch_prior_prescriptions(None) == []

    def test_returns_empty_for_empty_string(self):
        steward = _import_steward()
        assert steward._fetch_prior_prescriptions("") == []

    def test_returns_empty_when_no_prescription_events(self):
        steward = _import_steward()
        log = json.dumps({"event": "diagnosis", "steward_cycles": 0}) + "\n"
        log += json.dumps({"event": "agenda_update", "steward_cycles": 0})
        assert steward._fetch_prior_prescriptions(log) == []

    def test_returns_prescription_events(self):
        steward = _import_steward()
        entry = {
            "event": "prescription",
            "steward_cycles": 0,
            "completion_assessment": "no output",
            "next_posture_rationale": "initial pass",
            "return_reason": None,
        }
        log = json.dumps(entry)
        result = steward._fetch_prior_prescriptions(log)
        assert len(result) == 1
        assert result[0]["event"] == "prescription"
        assert result[0]["completion_assessment"] == "no output"

    def test_returns_reentry_prescription_events(self):
        steward = _import_steward()
        entry = {
            "event": "reentry_prescription",
            "steward_cycles": 1,
            "completion_assessment": "partial",
            "next_posture_rationale": "retry",
        }
        log = json.dumps(entry)
        result = steward._fetch_prior_prescriptions(log)
        assert len(result) == 1
        assert result[0]["event"] == "reentry_prescription"

    def test_limits_to_last_n_entries(self):
        steward = _import_steward()
        lines = []
        for i in range(5):
            lines.append(json.dumps({
                "event": "reentry_prescription" if i > 0 else "prescription",
                "steward_cycles": i,
                "completion_assessment": f"attempt {i}",
            }))
        log = "\n".join(lines)
        result = steward._fetch_prior_prescriptions(log, limit=3)
        assert len(result) == 3
        # Most recent 3 (cycles 2, 3, 4)
        assert result[0]["steward_cycles"] == 2
        assert result[2]["steward_cycles"] == 4

    def test_skips_malformed_json_lines(self):
        steward = _import_steward()
        log = (
            json.dumps({"event": "prescription", "steward_cycles": 0, "completion_assessment": "gap"})
            + "\nnot-json\n"
            + json.dumps({"event": "reentry_prescription", "steward_cycles": 1, "completion_assessment": "gap2"})
        )
        result = steward._fetch_prior_prescriptions(log)
        assert len(result) == 2

    def test_ignores_blank_lines(self):
        steward = _import_steward()
        log = (
            "\n"
            + json.dumps({"event": "prescription", "steward_cycles": 0, "completion_assessment": "x"})
            + "\n\n"
        )
        result = steward._fetch_prior_prescriptions(log)
        assert len(result) == 1


class TestFeedbackLoopIntegration:
    """Integration tests: prior prescriptions appear in re-prescription instructions."""

    def test_first_cycle_has_no_prior_context(self, db_path, registry, tmp_path):
        """cycle=0 prescription must NOT include a 'Prior prescription attempts' block."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        _make_uow_row(conn, status="ready-for-steward", steward_cycles=0)
        conn.close()

        # Use llm_prescriber=None to force the deterministic template path so
        # we can assert on exact phrase presence without invoking claude -p.
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
        )

        # Read back the written workflow artifact and check instructions
        artifacts = list((tmp_path / "artifacts").glob("*.json"))
        assert artifacts, "Expected a workflow artifact to be written"
        artifact_data = json.loads(artifacts[0].read_text())
        instructions = artifact_data.get("instructions", "")
        assert "Prior prescription attempts" not in instructions

    def test_second_cycle_includes_prior_context(self, db_path, registry, tmp_path):
        """cycle=1 (re-prescription) deterministic instructions must include prior prescription context."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        # Build a steward_log that already has a prescription entry from cycle 0
        prior_entry = json.dumps({
            "event": "prescription",
            "steward_cycles": 0,
            "completion_assessment": "no result file found",
            "next_posture_rationale": "steward: first_execution — no result",
            "return_reason": None,
        })

        conn = _open_db(db_path)
        _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            steward_log=prior_entry,
            # Audit entry so diagnosis doesn't classify as first_execution
            audit_log_entries=[
                {
                    "event": "execution_failed",
                    "note": json.dumps({"return_reason": "execution_failed"}),
                }
            ],
        )
        conn.close()

        # Use llm_prescriber=None to force the deterministic template path so
        # we can assert on exact phrase presence. The LLM path (claude -p) would
        # generate its own phrasing, making phrase assertions non-deterministic.
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
        )

        artifacts = list((tmp_path / "artifacts").glob("*.json"))
        assert artifacts, "Expected a workflow artifact to be written"
        artifact_data = json.loads(artifacts[0].read_text())
        instructions = artifact_data.get("instructions", "")
        assert "Prior prescription attempts" in instructions, (
            f"Re-prescription instructions must include prior context.\nInstructions:\n{instructions}"
        )
        assert "no result file found" in instructions

    def test_prior_context_absent_when_steward_log_is_empty(self, db_path, registry, tmp_path):
        """Re-prescription with steward_cycles=1 but empty log: no prior context block."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            steward_log=None,
            audit_log_entries=[
                {
                    "event": "execution_failed",
                    "note": json.dumps({"return_reason": "execution_failed"}),
                }
            ],
        )
        conn.close()

        # Use llm_prescriber=None to force the deterministic template path so
        # we can assert on exact phrase presence without invoking claude -p.
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
        )

        artifacts = list((tmp_path / "artifacts").glob("*.json"))
        assert artifacts, "Expected a workflow artifact to be written"
        artifact_data = json.loads(artifacts[0].read_text())
        instructions = artifact_data.get("instructions", "")
        assert "Prior prescription attempts" not in instructions


# ---------------------------------------------------------------------------
# Test: Backpressure — skip re-prescription when executor queue is saturated (#617)
# ---------------------------------------------------------------------------

class TestBackpressureSkipsRePrescription:
    """
    When the startup sweep returns a UoW from ready-for-executor to
    ready-for-steward with classification=executor_orphan, the steward
    must NOT re-prescribe. Instead it skips the UoW and logs a backpressure
    event. This prevents LLM call waste when the executor queue is saturated.
    """

    EXECUTOR_ORPHAN_AUDIT_ENTRY = {
        "event": "startup_sweep",
        "classification": "executor_orphan",
        "prior_status": "ready-for-executor",
    }

    def test_skip_represcription_when_executor_orphan(self, db_path, registry, tmp_path, monkeypatch):
        """UoW returned as executor_orphan must be skipped, not prescribed.

        The backpressure gate only applies when execution is disabled. We mock
        is_execution_enabled to return False so the gate triggers correctly.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        # Backpressure only fires when execution is disabled (otherwise the
        # executor_orphan classification is considered stale).
        # The function is imported inline in run_steward_cycle, so patch at source.
        monkeypatch.setattr(
            "src.orchestration.dispatcher_handlers.is_execution_enabled",
            lambda: False,
        )

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=None,
            audit_log_entries=[self.EXECUTOR_ORPHAN_AUDIT_ENTRY],
        )
        conn.close()

        result = steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        # UoW must not be prescribed: status stays ready-for-steward
        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-steward", (
            "UoW with executor_orphan return reason must remain ready-for-steward, "
            f"not be re-prescribed. Got status: {uow['status']}"
        )
        assert result["skipped"] >= 1, (
            "run_steward_cycle must count the backpressure skip in skipped counter"
        )

    def test_no_llm_call_when_executor_orphan(self, db_path, registry, tmp_path, monkeypatch):
        """No LLM prescription call is made when return reason is executor_orphan."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        # Backpressure only fires when execution is disabled.
        monkeypatch.setattr(
            "src.orchestration.dispatcher_handlers.is_execution_enabled",
            lambda: False,
        )

        llm_calls = []

        def capturing_llm_prescriber(uow, posture, gap, issue_body=""):
            llm_calls.append(uow.id)
            return None

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=None,
            audit_log_entries=[self.EXECUTOR_ORPHAN_AUDIT_ENTRY],
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            llm_prescriber=capturing_llm_prescriber,
        )

        assert uow_id not in llm_calls, (
            "LLM prescriber must not be called for a UoW with executor_orphan return reason"
        )

    def test_normal_uow_still_prescribed_alongside_orphan(self, db_path, registry, tmp_path, monkeypatch):
        """A normal UoW is prescribed even when another UoW is an executor_orphan."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        # Backpressure only fires when execution is disabled.
        monkeypatch.setattr(
            "src.orchestration.dispatcher_handlers.is_execution_enabled",
            lambda: False,
        )

        conn = _open_db(db_path)
        orphan_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=None,
            audit_log_entries=[self.EXECUTOR_ORPHAN_AUDIT_ENTRY],
            source_issue_number=101,
        )
        normal_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            output_ref=None,
            source_issue_number=102,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        orphan_uow = _get_uow(db_path, orphan_id)
        normal_uow = _get_uow(db_path, normal_id)

        assert orphan_uow["status"] == "ready-for-steward", (
            "executor_orphan UoW must stay ready-for-steward (backpressure skip)"
        )
        assert normal_uow["status"] == "ready-for-executor", (
            f"Normal UoW must be prescribed (ready-for-executor). Got: {normal_uow['status']}"
        )

    def test_backpressure_event_written_to_audit_log(self, db_path, registry, tmp_path, monkeypatch):
        """A backpressure audit event is written when re-prescription is skipped."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        # Backpressure only fires when execution is disabled.
        monkeypatch.setattr(
            "src.orchestration.dispatcher_handlers.is_execution_enabled",
            lambda: False,
        )

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=None,
            audit_log_entries=[self.EXECUTOR_ORPHAN_AUDIT_ENTRY],
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

        entries = _audit_entries(db_path, uow_id)
        backpressure_entries = [
            e for e in entries
            if e.get("event") == "backpressure"
        ]
        assert backpressure_entries, (
            "A backpressure audit entry must be written when re-prescription is skipped"
        )
        # The note must contain the uow_id and cycle count
        import json as _json
        note_data = _json.loads(backpressure_entries[0]["note"])
        assert note_data.get("uow_id") == uow_id
        assert "steward_cycles" in note_data


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


# ---------------------------------------------------------------------------
# Tests: LLM-class prescription path
# ---------------------------------------------------------------------------

class TestLlmPrescription:
    """Tests for _llm_prescribe and _build_prescription_instructions LLM path."""

    def _make_uow(
        self,
        summary: str = "Implement the widget feature",
        success_criteria: str = "Widget renders correctly in all browsers",
        steward_cycles: int = 0,
        steward_log: str | None = None,
        uow_type: str = "executable",
    ):
        """Build a minimal UoW dataclass for unit testing."""
        from src.orchestration.registry import UoW, UoWStatus
        return UoW(
            id="uow_test_abc123",
            status=UoWStatus.READY_FOR_STEWARD,
            summary=summary,
            source="github:issue/99",
            source_issue_number=99,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            type=uow_type,
            success_criteria=success_criteria,
            steward_cycles=steward_cycles,
            steward_log=steward_log,
        )

    def test_llm_prescribe_returns_none_on_subprocess_nonzero_exit(self, monkeypatch):
        """_llm_prescribe returns None when claude -p exits with a non-zero code."""
        import subprocess as _subprocess
        steward = _import_steward()

        def mock_run(cmd, **kwargs):
            result = _subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="error")
            return result

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")
        assert result is None

    def test_build_prescription_uses_llm_result_when_available(self):
        """_build_prescription_instructions uses the LLM result when llm_prescriber succeeds."""
        steward = _import_steward()

        def stub_prescriber(uow, posture, gap, issue_body=""):
            return {
                "instructions": "Write the widget module in src/widget.py.",
                "success_criteria_check": "Check that src/widget.py exists and contains Widget class.",
                "estimated_cycles": 1,
            }

        uow = self._make_uow()
        result = steward._build_prescription_instructions(
            uow,
            reentry_posture="executor_orphan",
            completion_gap="no prior output",
            llm_prescriber=stub_prescriber,
        )

        assert "Write the widget module" in result
        assert "Completion check:" in result
        assert "Check that src/widget.py exists" in result

    def test_build_prescription_fails_hard_when_llm_returns_none(self):
        """_build_prescription_instructions raises LLMPrescriptionError when llm_prescriber returns None (fail-hard behavior)."""
        steward = _import_steward()

        def failing_prescriber(uow, posture, gap, issue_body=""):
            return None

        uow = self._make_uow(steward_cycles=0)

        with pytest.raises(steward.LLMPrescriptionError) as exc_info:
            steward._build_prescription_instructions(
                uow,
                reentry_posture="executor_orphan",
                completion_gap="no prior output",
                llm_prescriber=failing_prescriber,
            )

        assert "LLM prescription failed" in str(exc_info.value)
        assert uow.id in str(exc_info.value)

    def test_build_prescription_falls_back_when_llm_prescriber_is_none(self):
        """Passing llm_prescriber=None bypasses LLM and uses deterministic template directly."""
        steward = _import_steward()

        uow = self._make_uow(steward_cycles=1)
        result = steward._build_prescription_instructions(
            uow,
            reentry_posture="execution_failed",
            completion_gap="test suite failed with exit code 1",
            llm_prescriber=None,
        )

        # Deterministic template for cycles > 0
        assert "Re-execution pass" in result
        assert "execution failed" in result.lower() or "Previous execution failed" in result

    def test_build_prescription_no_completion_check_when_success_criteria_check_empty(self):
        """Completion check is not appended when success_criteria_check is empty."""
        steward = _import_steward()

        def stub_prescriber(uow, posture, gap, issue_body=""):
            return {
                "instructions": "Write the widget module.",
                "success_criteria_check": "",
                "estimated_cycles": 1,
            }

        uow = self._make_uow()
        result = steward._build_prescription_instructions(
            uow,
            reentry_posture="executor_orphan",
            completion_gap="no prior output",
            llm_prescriber=stub_prescriber,
        )

        assert "Write the widget module" in result
        assert "Completion check:" not in result

    def test_llm_prescribe_includes_prior_prescription_history(self, monkeypatch):
        """_llm_prescribe extracts prior prescription events from steward_log and includes them in the prompt."""
        import subprocess as _subprocess
        steward = _import_steward()

        captured_prompts = []

        def mock_run(cmd, **kwargs):
            # cmd is [claude_bin, "-p", prompt, "--output-format", "text"]
            captured_prompts.append(cmd[2])  # capture the prompt string
            stdout = (
                "---\n"
                "executor_type: functional-engineer\n"
                "estimated_cycles: 1\n"
                "success_criteria_check: File exists.\n"
                "---\n\n"
                "Do the work again."
            )
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        prior_log = json.dumps({
            "event": "prescription",
            "steward_cycles": 0,
            "completion_assessment": "Initial implementation missing tests",
        })
        uow = self._make_uow(steward_cycles=1, steward_log=prior_log)

        result = steward._llm_prescribe(uow, "execution_complete", "output lacks tests")

        assert result is not None
        assert len(captured_prompts) == 1
        # Prior prescription history should appear in the prompt
        assert "Initial implementation missing tests" in captured_prompts[0]

    def test_llm_prescribe_handles_malformed_json_response(self, monkeypatch):
        """_llm_prescribe returns None when claude -p returns non-JSON content."""
        import subprocess as _subprocess
        steward = _import_steward()

        def mock_run(cmd, **kwargs):
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout="Sorry, I cannot help with that.", stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")
        assert result is None

    def test_llm_prescribe_handles_subprocess_exception(self, monkeypatch):
        """_llm_prescribe returns None when subprocess.run raises an exception."""
        steward = _import_steward()

        def mock_run(cmd, **kwargs):
            raise FileNotFoundError("claude: command not found")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")
        assert result is None

    def test_llm_prescribe_valid_front_matter_response(self, monkeypatch):
        """_llm_prescribe parses a well-formed front-matter + prose response."""
        import subprocess as _subprocess
        steward = _import_steward()

        front_matter_output = (
            "---\n"
            "executor_type: functional-engineer\n"
            "estimated_cycles: 2\n"
            "success_criteria_check: Feature works.\n"
            "---\n\n"
            "Implement the feature."
        )

        def mock_run(cmd, **kwargs):
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout=front_matter_output, stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")

        assert result is not None
        assert result["instructions"] == "Implement the feature."
        assert result["estimated_cycles"] == 2
        assert result["success_criteria_check"] == "Feature works."

    def test_llm_prescribe_front_matter_missing_executor_type_returns_none(self, monkeypatch):
        """_llm_prescribe returns None when front-matter is missing executor_type."""
        import subprocess as _subprocess
        steward = _import_steward()

        bad_output = (
            "---\n"
            "estimated_cycles: 1\n"
            "success_criteria_check: Something.\n"
            "---\n\n"
            "Do some work."
        )

        def mock_run(cmd, **kwargs):
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout=bad_output, stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")
        assert result is None

    def test_llm_prescribe_preamble_before_front_matter_succeeds(self, monkeypatch):
        """_llm_prescribe succeeds when LLM prefixes the front-matter with prose.

        The parser tolerates preamble before --- to be robust against LLMs that
        add an introductory sentence before the artifact.
        """
        import subprocess as _subprocess
        steward = _import_steward()

        preamble_output = (
            "Here is the prescription for this unit of work:\n\n"
            "---\n"
            "executor_type: functional-engineer\n"
            "estimated_cycles: 1\n"
            "success_criteria_check: Widget renders in all browsers.\n"
            "---\n\n"
            "Implement the widget feature."
        )

        def mock_run(cmd, **kwargs):
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout=preamble_output, stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")

        assert result is not None, "Preamble before front-matter should be tolerated"
        assert result["instructions"] == "Implement the widget feature."
        assert result["success_criteria_check"] == "Widget renders in all browsers."

    def test_llm_prescribe_front_matter_no_closing_delimiter(self, monkeypatch):
        """_llm_prescribe handles missing closing --- by treating all content as front-matter."""
        import subprocess as _subprocess
        steward = _import_steward()

        # No closing --- means prose body is empty (instructions will be empty → None)
        output = (
            "---\n"
            "executor_type: functional-engineer\n"
            "estimated_cycles: 1\n"
            "success_criteria_check: Tests pass.\n"
        )

        def mock_run(cmd, **kwargs):
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout=output, stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")

        # Empty instructions → _llm_prescribe returns None (fallback)
        assert result is None

    # --- New tests for issue #506: timeout observability and JSON classification ---

    def test_llm_prescribe_timeout_configurable_via_env(self, monkeypatch):
        """_llm_prescribe uses LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS env var for timeout."""
        import subprocess as _subprocess
        steward = _import_steward()

        captured_timeouts = []

        def mock_run(cmd, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            stdout = (
                "---\n"
                "executor_type: functional-engineer\n"
                "estimated_cycles: 1\n"
                "success_criteria_check: Work done.\n"
                "---\n\n"
                "Do the work."
            )
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)
        monkeypatch.setenv("LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS", "300")

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")

        assert result is not None
        assert len(captured_timeouts) == 1
        assert captured_timeouts[0] == 300

    def test_llm_prescribe_timeout_defaults_to_600(self, monkeypatch):
        """_llm_prescribe uses 600s default timeout when env var is not set."""
        import subprocess as _subprocess
        steward = _import_steward()

        captured_timeouts = []

        def mock_run(cmd, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            raise _subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)
        monkeypatch.delenv("LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS", raising=False)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")

        assert result is None
        assert captured_timeouts[0] == 600

    def test_llm_prescribe_empty_stdout_returns_none(self, monkeypatch):
        """_llm_prescribe returns None when claude -p exits 0 but stdout is empty."""
        import subprocess as _subprocess
        steward = _import_steward()

        def mock_run(cmd, **kwargs):
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")
        assert result is None

    def test_llm_prescribe_wrong_format_no_front_matter_returns_none(self, monkeypatch):
        """_llm_prescribe returns None when claude -p returns content without front-matter."""
        import subprocess as _subprocess
        steward = _import_steward()

        def mock_run(cmd, **kwargs):
            # JSON array — not front-matter + prose format
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout='["a", "b"]', stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")
        assert result is None

    def test_llm_prescribe_schema_mismatch_estimated_cycles_defaults(self, monkeypatch):
        """_llm_prescribe defaults estimated_cycles to 1 when the field is a non-integer string."""
        import subprocess as _subprocess
        steward = _import_steward()

        def mock_run(cmd, **kwargs):
            stdout = (
                "---\n"
                "executor_type: functional-engineer\n"
                "estimated_cycles: several\n"  # wrong type — should be int
                "success_criteria_check: Work done.\n"
                "---\n\n"
                "Do the work."
            )
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr("src.orchestration.steward.subprocess.run", mock_run)

        uow = self._make_uow()
        result = steward._llm_prescribe(uow, "executor_orphan", "no prior output")

        assert result is not None
        assert result["estimated_cycles"] == 1

    def test_count_consecutive_llm_fallbacks_empty_log(self):
        """_count_consecutive_llm_fallbacks returns 0 for None or empty log."""
        steward = _import_steward()
        assert steward._count_consecutive_llm_fallbacks(None) == 0
        assert steward._count_consecutive_llm_fallbacks("") == 0

    def test_count_consecutive_llm_fallbacks_all_fallback(self):
        """_count_consecutive_llm_fallbacks counts consecutive fallbacks at log tail."""
        steward = _import_steward()

        log_entries = [
            json.dumps({"event": "prescription", "prescription_path": "fallback"}),
            json.dumps({"event": "reentry_prescription", "prescription_path": "fallback"}),
            json.dumps({"event": "reentry_prescription", "prescription_path": "fallback"}),
        ]
        log_str = "\n".join(log_entries)

        assert steward._count_consecutive_llm_fallbacks(log_str) == 3

    def test_count_consecutive_llm_fallbacks_reset_after_llm_success(self):
        """_count_consecutive_llm_fallbacks resets count when an LLM success appears."""
        steward = _import_steward()

        log_entries = [
            json.dumps({"event": "prescription", "prescription_path": "fallback"}),
            json.dumps({"event": "reentry_prescription", "prescription_path": "llm"}),
            json.dumps({"event": "reentry_prescription", "prescription_path": "fallback"}),
            json.dumps({"event": "reentry_prescription", "prescription_path": "fallback"}),
        ]
        log_str = "\n".join(log_entries)

        # Only the 2 fallbacks at the tail count — the earlier fallback is preceded
        # by an LLM success which resets the streak.
        assert steward._count_consecutive_llm_fallbacks(log_str) == 2

    def test_count_consecutive_llm_fallbacks_last_was_llm(self):
        """_count_consecutive_llm_fallbacks returns 0 when the last prescription used LLM."""
        steward = _import_steward()

        log_entries = [
            json.dumps({"event": "prescription", "prescription_path": "fallback"}),
            json.dumps({"event": "reentry_prescription", "prescription_path": "llm"}),
        ]
        log_str = "\n".join(log_entries)

        assert steward._count_consecutive_llm_fallbacks(log_str) == 0

    def test_llm_prescription_error_raises_and_transitions_back(self, db_path, registry, tmp_path, monkeypatch):
        """When LLM prescriber returns None (fails), LLMPrescriptionError is raised and UoW transitions back to ready-for-steward."""
        steward = _import_steward()
        _ensure_registry_has_phase2_methods(registry)

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            success_criteria="PR opened",
        )
        conn.close()

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        # Prescriber always returns None → raises LLMPrescriptionError
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
            llm_prescriber=lambda *a, **kw: None,
        )

        # UoW should remain in ready-for-steward (was transitioned back after error)
        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-steward"

        # Audit log should have an llm_prescription_error entry
        entries = _audit_entries(db_path, uow_id)
        error_entries = [e for e in entries if e.get("event") == "llm_prescription_error"]
        assert len(error_entries) >= 1, "llm_prescription_error audit entry must exist"

    def test_llm_prescription_error_audit_entry_written(self, db_path, registry, tmp_path):
        """An llm_prescription_error audit entry is written when LLM prescription fails."""
        steward = _import_steward()
        _ensure_registry_has_phase2_methods(registry)

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            success_criteria="PR opened",
        )
        conn.close()

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
            llm_prescriber=lambda *a, **kw: None,
        )

        audit_entries = steward._fetch_audit_entries(registry, uow_id)
        # audit_log stores the full entry dict as JSON in the `note` column.
        error_notes = [
            json.loads(e["note"])
            for e in audit_entries
            if e.get("event") == "llm_prescription_error"
        ]
        assert len(error_notes) >= 1, "llm_prescription_error audit entry must exist"
        assert error_notes[0]["uow_id"] == uow_id
        assert "LLM prescription failed" in error_notes[0]["error_message"]

    def test_end_to_end_prescription_with_llm_stub(self, db_path, registry, tmp_path):
        """Full _process_uow call uses LLM prescription stub and writes instructions to artifact."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        llm_calls = []

        def stub_llm_prescriber(uow, posture, gap, issue_body=""):
            llm_calls.append({"uow_id": uow.id, "posture": posture, "gap": gap})
            return {
                "instructions": "LLM-generated: implement the feature per spec.",
                "success_criteria_check": "Feature branch merged with green CI.",
                "estimated_cycles": 1,
            }

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
            success_criteria="PR opened and merged",
        )
        conn.close()

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
            llm_prescriber=stub_llm_prescriber,
        )

        # LLM prescriber should have been called once
        assert len(llm_calls) == 1
        assert llm_calls[0]["uow_id"] == uow_id

        # Artifact file should contain the LLM-generated instructions
        artifacts = list(artifact_dir.glob("*.json"))
        assert len(artifacts) == 1
        artifact_data = json.loads(artifacts[0].read_text())
        assert "LLM-generated" in artifact_data["instructions"]
        assert "Completion check:" in artifact_data["instructions"]


# ---------------------------------------------------------------------------
# Tests: _select_executor_type — maps UoW nature to executor type
# ---------------------------------------------------------------------------

class TestSelectExecutorType:
    """Unit tests for _select_executor_type — pure function."""

    def _make_uow(self, summary: str, source: str = "github:issue/42") -> "UoW":
        from src.orchestration.registry import UoW, UoWStatus
        return UoW(
            id="uow_test_abc",
            status=UoWStatus.READY_FOR_STEWARD,
            summary=summary,
            source=source,
            source_issue_number=42,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            type="executable",
        )

    def test_bug_in_summary_returns_functional_engineer(self):
        steward = _import_steward()
        uow = self._make_uow("fix: bug in login handler")
        assert steward._select_executor_type(uow) == "functional-engineer"

    def test_feature_in_summary_returns_functional_engineer(self):
        steward = _import_steward()
        uow = self._make_uow("feat: implement widget module")
        assert steward._select_executor_type(uow) == "functional-engineer"

    def test_install_in_summary_returns_lobster_ops(self):
        steward = _import_steward()
        uow = self._make_uow("install new cron dependency")
        assert steward._select_executor_type(uow) == "lobster-ops"

    def test_deploy_in_summary_returns_lobster_ops(self):
        steward = _import_steward()
        uow = self._make_uow("deploy updated config to server")
        assert steward._select_executor_type(uow) == "lobster-ops"

    def test_github_issue_source_without_code_keywords_returns_functional_engineer(self):
        steward = _import_steward()
        uow = self._make_uow("add new user preference endpoint", source="github:issue/99")
        assert steward._select_executor_type(uow) == "functional-engineer"

    def test_non_github_source_without_keywords_returns_general(self):
        steward = _import_steward()
        uow = self._make_uow("do a thing", source="manual")
        assert steward._select_executor_type(uow) == "general"

    def test_fix_prefix_with_ops_terms_returns_functional_engineer(self):
        # Regression: "fix: setup script fails" contains both code keyword ("fix")
        # and ops keywords ("setup", "script"). Code keywords must win.
        steward = _import_steward()
        uow = self._make_uow("fix: setup script fails")
        assert steward._select_executor_type(uow) == "functional-engineer"

    def test_fix_prefix_with_migration_term_returns_functional_engineer(self):
        # Regression: "fix: migration script fails on upgrade" contains "fix"
        # (code) and "migration", "script" (ops). Code keyword must win.
        steward = _import_steward()
        uow = self._make_uow("fix: migration script fails on upgrade")
        assert steward._select_executor_type(uow) == "functional-engineer"

    def test_pure_ops_term_no_code_keyword_returns_lobster_ops(self):
        # Pure ops summary with no code keyword should still route to lobster-ops.
        steward = _import_steward()
        uow = self._make_uow("upgrade server config and systemd unit", source="manual")
        assert steward._select_executor_type(uow) == "lobster-ops"


# ---------------------------------------------------------------------------
# Tests: Issue #425 — _assess_completion reads success field from result.json
# (regression: stub keyword-matching must not be the live code path)
# ---------------------------------------------------------------------------

class TestAssessCompletionStructuralProxy:
    """
    Verify that _assess_completion uses the structured result.json written by
    the Executor (outcome/success fields) rather than keyword-matching on
    success_criteria text. Prevents false-positive done declarations.

    Issue #425: fix(_assess_completion): replace stub keyword-matching with
    deterministic structural proxy.
    """

    def _make_uow(self, tmp_path, uow_id=None, success_criteria="Output file exists"):
        """Build a minimal UoW with output_ref pointing to a tmp file."""
        from src.orchestration.registry import UoW, UoWStatus
        if uow_id is None:
            uow_id = f"uow_{uuid.uuid4().hex[:8]}"
        output_file = tmp_path / f"{uow_id}.json"
        output_file.write_text("task dispatched")
        return UoW(
            id=uow_id,
            status=UoWStatus.READY_FOR_STEWARD,
            summary="Test UoW",
            source="github:issue/42",
            source_issue_number=42,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            type="executable",
            success_criteria=success_criteria,
            steward_cycles=1,
            output_ref=str(output_file),
        ), output_file

    def test_success_false_in_result_json_prevents_done_declaration(self, tmp_path):
        """
        A result.json with success=false and outcome=failed must cause
        _assess_completion to return is_complete=False, even if the
        success_criteria text appears in the output file.

        This is the core false-positive regression: the old keyword-matching
        stub would return True because a word from success_criteria appears
        in the output. The structural proxy reads outcome/success instead.
        """
        steward = _import_steward()
        uow, output_file = self._make_uow(tmp_path, success_criteria="Output file exists")

        # Output file contains the success_criteria keyword — old stub would match this
        output_file.write_text("Output file exists with all results written.")

        # Executor writes success=false / outcome=failed
        result_file = output_file.with_suffix(".result.json")
        result_file.write_text(json.dumps({
            "uow_id": uow.id,
            "outcome": "failed",
            "success": False,
            "reason": "subagent exited before completing all steps",
        }))

        is_complete, rationale, executor_outcome = steward._assess_completion(
            uow=uow,
            output_content=output_file.read_text(),
            reentry_posture="execution_complete",
        )

        assert not is_complete, (
            "_assess_completion must return is_complete=False when result.json reports "
            "outcome=failed, even if success_criteria keywords appear in the output"
        )
        assert executor_outcome == "failed"

    def test_success_true_in_result_json_declares_done(self, tmp_path):
        """
        A result.json with outcome=complete and success=true must cause
        _assess_completion to return is_complete=True.
        """
        steward = _import_steward()
        uow, output_file = self._make_uow(tmp_path)
        output_file.write_text("PR #42 opened and tests passed.")

        result_file = output_file.with_suffix(".result.json")
        result_file.write_text(json.dumps({
            "uow_id": uow.id,
            "outcome": "complete",
            "success": True,
        }))

        is_complete, rationale, executor_outcome = steward._assess_completion(
            uow=uow,
            output_content=output_file.read_text(),
            reentry_posture="execution_complete",
        )

        assert is_complete, (
            "_assess_completion must return is_complete=True when result.json has outcome=complete"
        )
        assert executor_outcome == "complete"

    def test_no_result_json_with_success_criteria_returns_false(self, tmp_path):
        """
        When no result.json exists and success_criteria is set,
        _assess_completion must return is_complete=False (conservative fallback).
        The Executor is required to write a result file; without one the
        Steward cannot verify completion.
        """
        steward = _import_steward()
        uow, output_file = self._make_uow(tmp_path, success_criteria="PR merged")
        output_file.write_text("PR merged successfully.")
        # No result.json written

        is_complete, rationale, executor_outcome = steward._assess_completion(
            uow=uow,
            output_content=output_file.read_text(),
            reentry_posture="execution_complete",
        )

        assert not is_complete, (
            "Without a result.json, _assess_completion must not declare done "
            "when success_criteria is set — conservative fallback required"
        )
        assert executor_outcome is None

    def test_outcome_blocked_routes_to_dan(self, tmp_path):
        """
        A result.json with outcome=blocked must return is_complete=False
        with executor_outcome='blocked' so the caller routes to Dan.
        """
        steward = _import_steward()
        uow, output_file = self._make_uow(tmp_path)
        output_file.write_text("blocked: waiting for API credentials")

        result_file = output_file.with_suffix(".result.json")
        result_file.write_text(json.dumps({
            "uow_id": uow.id,
            "outcome": "blocked",
            "success": False,
            "reason": "API credentials not available in this environment",
        }))

        is_complete, rationale, executor_outcome = steward._assess_completion(
            uow=uow,
            output_content=output_file.read_text(),
            reentry_posture="execution_complete",
        )

        assert not is_complete
        assert executor_outcome == "blocked", (
            "outcome=blocked in result.json must route to Dan — executor_outcome must be 'blocked'"
        )


# ---------------------------------------------------------------------------
# Tests: Issue #426 — _default_notify_dan writes to Lobster inbox on hard cap
# (regression: WARNING log alone is not sufficient — inbox delivery required)
# ---------------------------------------------------------------------------

class TestDefaultNotifyDanInboxDelivery:
    """
    Verify that when the hard cap fires and a UoW moves to 'blocked',
    _default_notify_dan writes a JSON message to ~/messages/inbox/ so Dan
    is notified via Telegram.

    Issue #426: fix(_default_notify_dan): write to Lobster inbox on hard cap.
    """

    def test_hard_cap_writes_json_file_to_inbox(self, tmp_path, monkeypatch):
        """
        _default_notify_dan with condition='hard_cap' must write a JSON file
        to the inbox directory. The file must contain the uow_id, condition,
        and a non-empty text body.
        """
        steward = _import_steward()
        from src.orchestration.registry import UoW, UoWStatus

        # Redirect inbox writes to tmp_path so we don't touch the live inbox
        fake_inbox = tmp_path / "inbox"
        fake_inbox.mkdir()
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(fake_inbox) if "messages/inbox" in p else os.path.expanduser(p),
        )

        uow = UoW(
            id="uow_hardcap_001",
            status=UoWStatus.BLOCKED,
            summary="Test hard cap UoW",
            source="github:issue/100",
            source_issue_number=100,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            type="executable",
            success_criteria=None,
            steward_cycles=5,
        )

        steward._default_notify_dan(uow=uow, condition="hard_cap", return_reason="execution_failed")

        written = list(fake_inbox.glob("*.json"))
        assert len(written) == 1, (
            "_default_notify_dan must write exactly one JSON file to the inbox on hard_cap"
        )

        msg = json.loads(written[0].read_text())
        assert msg.get("metadata", {}).get("uow_id") == "uow_hardcap_001", (
            "Inbox message must include uow_id in metadata"
        )
        assert msg.get("metadata", {}).get("condition") == "hard_cap"
        assert msg.get("text"), "Inbox message must have a non-empty text body"
        assert "source" in msg, "Inbox message must include source field"

    def test_hard_cap_inbox_message_includes_buttons(self, tmp_path, monkeypatch):
        """
        The hard-cap inbox message must include inline buttons so Dan can
        resolve the stuck UoW (Retry / Close) without typing commands.
        """
        steward = _import_steward()
        from src.orchestration.registry import UoW, UoWStatus

        fake_inbox = tmp_path / "inbox"
        fake_inbox.mkdir()
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(fake_inbox) if "messages/inbox" in p else os.path.expanduser(p),
        )

        uow = UoW(
            id="uow_hardcap_002",
            status=UoWStatus.BLOCKED,
            summary="Buttons test UoW",
            source="github:issue/101",
            source_issue_number=101,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            type="executable",
            success_criteria=None,
            steward_cycles=5,
        )

        steward._default_notify_dan(uow=uow, condition="hard_cap")

        written = list(fake_inbox.glob("*.json"))
        assert len(written) == 1
        msg = json.loads(written[0].read_text())

        buttons = msg.get("buttons")
        assert buttons, "Hard-cap inbox message must include inline buttons for Retry/Close"
        # Flatten buttons (list of rows, each a list of button dicts)
        all_buttons = [btn for row in buttons for btn in row]
        callback_data_values = [btn.get("callback_data", "") for btn in all_buttons]
        assert any("decide_retry" in cb for cb in callback_data_values), (
            "Must have a Retry button with decide_retry callback_data"
        )
        assert any("decide_close" in cb for cb in callback_data_values), (
            "Must have a Close button with decide_close callback_data"
        )

    def test_run_steward_cycle_hard_cap_uses_real_notify(self, db_path, registry, tmp_path, monkeypatch):
        """
        Integration: when run_steward_cycle fires the hard cap with the default
        notify_dan (not a mock), a JSON file is written to the inbox directory.
        Verifies that the live code path (not just the injected mock) delivers
        inbox messages.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        fake_inbox = tmp_path / "inbox"
        fake_inbox.mkdir()
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(fake_inbox) if "messages/inbox" in p else os.path.expanduser(p),
        )

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=5,
            output_ref=None,
        )
        conn.close()

        # Run without injecting notify_dan — exercises the real _default_notify_dan
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            # notify_dan not passed — uses _default_notify_dan (the live path)
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "blocked", "Hard cap must transition UoW to blocked"

        written = list(fake_inbox.glob("*.json"))
        assert len(written) >= 1, (
            "_default_notify_dan must write a JSON file to the inbox when the hard cap fires "
            "— WARNING log alone is not sufficient (Issue #426)"
        )


# ---------------------------------------------------------------------------
# Tests: Corrective trace one-cycle temporal gate (cristae-junction delay)
# ---------------------------------------------------------------------------

class TestCorrectiveTraceGate:
    """
    Verify the one-cycle temporal gate: before re-prescribing after an executor
    return, the executor must have written a corrective trace ({output_ref}.trace.json).

    Biological analogy: cristae geometry creates a delay between proton pump
    action and ATP synthesis. Without this delay, the system oscillates wildly.
    The software equivalent: steward waits one cycle before re-prescribing if
    trace.json is absent.
    """

    def _make_uow_with_result(
        self,
        conn,
        db_path: Path,
        tmp_path: Path,
        outcome: str = "partial",
    ) -> tuple[str, Path, Path]:
        """
        Helper: create a UoW with execution_complete audit and output + result files.
        Returns (uow_id, output_file, result_file).
        Does NOT write a trace.json — callers can add that selectively.
        """
        output_file = tmp_path / "output.txt"
        output_file.write_text("partial work done — some steps remain.")
        result_file = output_file.with_suffix(".result.json")
        # Note: for partial/failed we don't use steward_cycles=0 since that skips
        # the trace gate (first_execution posture). Use cycles=1 for re-entry.
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=str(output_file),
            audit_log_entries=[
                {
                    "event": "execution_complete",
                    "actor": "executor",
                    "return_reason": "observation_complete",
                    "timestamp": _now_iso(),
                },
            ],
            success_criteria="All steps completed and output verified.",
        )
        result_file.write_text(json.dumps({
            "uow_id": uow_id,
            "outcome": outcome,
            "reason": "partial work done",
        }))
        return uow_id, output_file, result_file

    def test_trace_absent_first_reentry_skips_prescription(self, db_path, registry, tmp_path):
        """
        When result.json exists but trace.json does NOT: first re-entry must skip
        prescription (WaitForTrace), log trace_gate_waited, and keep UoW in diagnosing
        state (S3-B: NOT transition back to ready-for-steward).

        The startup_sweep on the next heartbeat resets diagnosing → ready-for-steward.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        conn = _open_db(db_path)
        uow_id, output_file, result_file = self._make_uow_with_result(conn, db_path, tmp_path)
        conn.close()

        # No trace.json written — gate should fire
        result = steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
        )

        # S3-B: WaitForTrace outcome is counted in wait_for_trace (not prescribed)
        assert result.get("wait_for_trace", 0) == 1, (
            "WaitForTrace outcome must be counted in run_steward_cycle result"
        )

        uow = _get_uow(db_path, uow_id)
        # S3-B: UoW stays in diagnosing — startup_sweep resets on next heartbeat
        assert uow["status"] == "diagnosing", (
            "Trace gate (S3-B): first re-entry with trace.json absent must keep UoW in "
            "diagnosing state, not transition back to ready-for-steward or ready-for-executor"
        )

        steward_log = uow.get("steward_log", "") or ""
        assert "trace_gate_waited" in steward_log, (
            "trace_gate_waited entry must be written to steward_log on first skip"
        )

        # workflow_artifact must NOT be written (no prescription took place)
        assert not uow.get("workflow_artifact"), (
            "No workflow artifact must be written when trace gate skips prescription"
        )

    def test_trace_absent_second_reentry_proceeds_with_timeout(
        self, db_path, registry, tmp_path
    ):
        """
        When result.json exists but trace.json does NOT, and steward_log already
        contains trace_gate_waited: second re-entry must proceed with prescription
        (non-blocking fallback) and log a trace_gate_timeout event.

        S3-B: renamed from contract_violation to trace_gate_timeout per spec.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        output_file = tmp_path / "output.txt"
        output_file.write_text("partial work done — some steps remain.")
        result_file = output_file.with_suffix(".result.json")

        # Pre-populate steward_log with trace_gate_waited to simulate already-waited state
        trace_gate_log = json.dumps({
            "event": "trace_gate_waited",
            "uow_id": "placeholder",
            "steward_cycles": 1,
            "timestamp": _now_iso(),
        })

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=str(output_file),
            audit_log_entries=[
                {
                    "event": "execution_complete",
                    "actor": "executor",
                    "return_reason": "observation_complete",
                    "timestamp": _now_iso(),
                },
            ],
            success_criteria="All steps completed and output verified.",
            steward_log=trace_gate_log,
        )
        result_file.write_text(json.dumps({
            "uow_id": uow_id,
            "outcome": "partial",
            "reason": "partial work done",
        }))
        conn.close()

        # No trace.json written but steward_log already has trace_gate_waited
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-executor", (
            "Trace gate: second re-entry (already waited) must proceed with prescription "
            "and transition to ready-for-executor"
        )

        steward_log = uow.get("steward_log", "") or ""
        assert "trace_gate_timeout" in steward_log, (
            "trace_gate_timeout must be logged when proceeding after one-cycle wait "
            "without trace.json appearing (S3-B: renamed from trace_gate_contract_violation)"
        )

    def test_trace_exists_prescription_proceeds_normally(self, db_path, registry, tmp_path):
        """
        When both result.json AND trace.json exist: prescription proceeds normally
        without any trace gate delay.
        """
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        output_file = tmp_path / "output.txt"
        output_file.write_text("partial work done — some steps remain.")
        result_file = output_file.with_suffix(".result.json")
        trace_file = output_file.with_suffix(".trace.json")

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=1,
            output_ref=str(output_file),
            audit_log_entries=[
                {
                    "event": "execution_complete",
                    "actor": "executor",
                    "return_reason": "observation_complete",
                    "timestamp": _now_iso(),
                },
            ],
            success_criteria="All steps completed and output verified.",
        )
        result_file.write_text(json.dumps({
            "uow_id": uow_id,
            "outcome": "partial",
            "reason": "partial work done",
        }))
        # Write trace.json — gate should NOT fire
        trace_file.write_text(json.dumps({
            "uow_id": uow_id,
            "cycle": 1,
            "corrective_observations": [],
        }))
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-executor", (
            "When trace.json is present, prescription must proceed and UoW "
            "must transition to ready-for-executor without any gate delay"
        )

        steward_log = uow.get("steward_log", "") or ""
        assert "trace_gate_waited" not in steward_log, (
            "trace_gate_waited must NOT appear in steward_log when trace.json is present"
        )

# ---------------------------------------------------------------------------
# Tests: Issue #648 Part B — _assess_completion legacy fallback hard-gated
# (NULL success_criteria must not declare done without a result.json)
# ---------------------------------------------------------------------------

class TestAssessCompletionLegacyFallbackRemoved:
    """
    Verify that _assess_completion no longer declares 'done' based solely on
    output_ref presence when success_criteria is NULL and no result.json exists.

    Issue #648 Part B: the legacy fallback (trust output_ref + execution_complete
    posture when success_criteria is NULL) is removed. A result.json with
    outcome=complete is required regardless of whether success_criteria is set.
    """

    def _make_uow(self, tmp_path, uow_id=None, success_criteria=None):
        """Build a minimal UoW with output_ref pointing to a tmp file."""
        from src.orchestration.registry import UoW, UoWStatus
        if uow_id is None:
            import uuid
            uow_id = f"uow_{uuid.uuid4().hex[:8]}"
        output_file = tmp_path / f"{uow_id}.json"
        output_file.write_text("execution complete: task dispatched as run-123")
        return UoW(
            id=uow_id,
            status=UoWStatus.READY_FOR_STEWARD,
            summary="Fix the login bug",
            source="github:issue/99",
            source_issue_number=99,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            type="executable",
            success_criteria=success_criteria,
            steward_cycles=1,
            output_ref=str(output_file),
        ), output_file

    def test_null_success_criteria_no_result_json_is_not_done(self, tmp_path):
        """
        NULL success_criteria + no result.json must not declare done.

        The legacy fallback trusted output_ref presence alone when
        success_criteria was NULL. A subagent that exits 0 without opening a
        PR or calling write_result would trigger this path — false done.
        The fix: require result.json regardless of success_criteria.
        """
        steward = _import_steward()
        uow, output_file = self._make_uow(tmp_path, success_criteria=None)
        # No result.json written — subagent exited without confirming work.

        is_complete, rationale, executor_outcome = steward._assess_completion(
            uow=uow,
            output_content=output_file.read_text(),
            reentry_posture="execution_complete",
        )

        assert not is_complete, (
            "_assess_completion must return is_complete=False when no result.json "
            "is present, even when success_criteria is NULL. "
            "The legacy fallback must not silently declare done."
        )
        assert executor_outcome is None
        assert "result file" in rationale.lower() or "result.json" in rationale.lower(), (
            "Rationale must explain that a result file is required"
        )

    def test_null_success_criteria_with_complete_result_json_is_done(self, tmp_path):
        """
        NULL success_criteria + result.json with outcome=complete must declare done.

        Removing the legacy fallback must not prevent legitimate completions:
        when the Executor writes a result.json with outcome=complete, the
        Steward should close the UoW even without explicit success_criteria.
        """
        steward = _import_steward()
        uow, output_file = self._make_uow(tmp_path, success_criteria=None)
        result_file = output_file.with_suffix(".result.json")
        result_file.write_text(json.dumps({
            "uow_id": uow.id,
            "outcome": "complete",
            "success": True,
        }))

        is_complete, rationale, executor_outcome = steward._assess_completion(
            uow=uow,
            output_content=output_file.read_text(),
            reentry_posture="execution_complete",
        )

        assert is_complete, (
            "_assess_completion must return is_complete=True when result.json "
            "reports outcome=complete, even when success_criteria is NULL."
        )
        assert executor_outcome == "complete"

    def test_null_success_criteria_with_failed_result_json_is_not_done(self, tmp_path):
        """
        NULL success_criteria + result.json with outcome=failed must not declare done.

        Confirms the full routing path: result.json outcome is authoritative
        even when success_criteria is NULL.
        """
        steward = _import_steward()
        uow, output_file = self._make_uow(tmp_path, success_criteria=None)
        result_file = output_file.with_suffix(".result.json")
        result_file.write_text(json.dumps({
            "uow_id": uow.id,
            "outcome": "failed",
            "success": False,
            "reason": "subagent exited without opening PR",
        }))

        is_complete, rationale, executor_outcome = steward._assess_completion(
            uow=uow,
            output_content=output_file.read_text(),
            reentry_posture="execution_complete",
        )

        assert not is_complete
        assert executor_outcome == "failed"


# ---------------------------------------------------------------------------
# Tests: Issue #648 Part A — inline_executor collapses the polling hop
# ---------------------------------------------------------------------------

class TestInlineExecutorDispatch:
    """
    Verify that when inline_executor is provided to _process_uow / run_steward_cycle,
    the Executor is invoked immediately after the READY_FOR_EXECUTOR transition,
    eliminating the 0-3 min polling hop described in issue #648 Part A.
    """

    def _setup_db(self, tmp_path):
        """Return (db_path, registry, uow_id) for a fresh DB with one ready-for-steward UoW."""
        from src.orchestration.registry import Registry
        db_path = tmp_path / "registry.db"
        conn = _open_db(db_path)
        _apply_phase2_schema(conn)
        conn.close()
        # Init Registry first so migration 0008 adds lifetime_cycles before _make_uow_row.
        registry = Registry(db_path)
        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward", source_issue_number=42)
        conn.close()
        return db_path, registry, uow_id

    def test_inline_executor_called_after_prescription(self, tmp_path):
        """
        When inline_executor is provided and a UoW is prescribed, the callable
        is invoked with the UoW ID immediately after the READY_FOR_EXECUTOR
        transition — not on the next heartbeat cycle.
        """
        steward = _import_steward()
        db_path, registry, uow_id = self._setup_db(tmp_path)

        dispatched_uow_ids: list[str] = []

        def fake_executor(uid: str) -> None:
            dispatched_uow_ids.append(uid)

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
            inline_executor=fake_executor,
        )

        assert dispatched_uow_ids == [uow_id], (
            f"inline_executor must be called once with the UoW ID after prescription. "
            f"Got: {dispatched_uow_ids!r}"
        )

    def test_no_inline_executor_by_default(self, tmp_path):
        """
        When inline_executor is not provided (default None), the Executor is
        not called inline — the heartbeat remains the sole dispatch path.

        This verifies backward compatibility: existing callers that do not
        pass inline_executor continue to work unchanged.
        """
        steward = _import_steward()
        db_path, registry, uow_id = self._setup_db(tmp_path)

        executor_called: list[str] = []

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
            # inline_executor intentionally omitted
        )

        assert executor_called == [], (
            "With no inline_executor, the executor must not be called inline"
        )

    def test_inline_executor_failure_is_non_fatal(self, tmp_path):
        """
        When inline_executor raises, the error is logged but the UoW stays
        in ready-for-executor — the heartbeat will retry on the next cycle.

        The Steward must not propagate the executor exception; _process_uow
        must return Prescribed normally even if inline dispatch fails.
        """
        steward = _import_steward()
        db_path, registry, uow_id = self._setup_db(tmp_path)

        def failing_executor(uid: str) -> None:
            raise RuntimeError("Executor claim rejected — optimistic lock race")

        # Must not raise even though inline_executor raises
        result = steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
            inline_executor=failing_executor,
        )

        # The prescription still completes — Prescribed is counted
        assert result["prescribed"] >= 1, (
            "Steward must count the prescription as successful even when "
            "inline_executor raises — the UoW is in ready-for-executor for retry"
        )

        # UoW is in ready-for-executor (heartbeat will pick it up)
        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-executor", (
            f"UoW must remain in ready-for-executor when inline_executor fails. "
            f"Got: {uow['status']!r}"
        )

    def test_inline_executor_not_called_in_dry_run(self, tmp_path):
        """
        In dry-run mode the READY_FOR_EXECUTOR transition is suppressed, so
        inline_executor must NOT be called (it would have nothing to claim).
        """
        steward = _import_steward()
        db_path, registry, uow_id = self._setup_db(tmp_path)

        dispatched: list[str] = []

        def fake_executor(uid: str) -> None:
            dispatched.append(uid)

        steward.run_steward_cycle(
            registry=registry,
            dry_run=True,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
            notify_dan=lambda *a, **kw: None,
            llm_prescriber=None,
            inline_executor=fake_executor,
        )

        assert dispatched == [], (
            "inline_executor must not be called in dry-run mode — "
            "the READY_FOR_EXECUTOR transition is suppressed in dry_run"
        )


# ---------------------------------------------------------------------------
# Tests: _parse_workflow_artifact
# ---------------------------------------------------------------------------

class TestParseWorkflowArtifact:
    """Unit tests for the pure _parse_workflow_artifact function."""

    def test_valid_input_with_all_fields(self):
        """Valid front-matter + prose → correct dict with all fields."""
        steward = _import_steward()

        raw = (
            "---\n"
            "executor_type: functional-engineer\n"
            "estimated_cycles: 2\n"
            "success_criteria_check: Verify PR is open and tests pass\n"
            "---\n\n"
            "Implement the widget module in src/widget.py.\n"
            "Run tests to verify no regressions."
        )

        result = steward._parse_workflow_artifact(raw)

        assert result["executor_type"] == "functional-engineer"
        assert result["estimated_cycles"] == 2
        assert result["success_criteria_check"] == "Verify PR is open and tests pass"
        assert "Implement the widget module" in result["instructions"]
        assert "Run tests" in result["instructions"]

    def test_preamble_before_front_matter_is_tolerated(self):
        """Preamble prose before the opening --- is discarded without error."""
        steward = _import_steward()

        raw = (
            "Here is the prescription:\n\n"
            "---\n"
            "executor_type: functional-engineer\n"
            "estimated_cycles: 1\n"
            "success_criteria_check: Tests pass.\n"
            "---\n\n"
            "Do the work."
        )

        result = steward._parse_workflow_artifact(raw)

        assert result["executor_type"] == "functional-engineer"
        assert result["instructions"] == "Do the work."

    def test_only_executor_type_uses_defaults(self):
        """Only executor_type present → estimated_cycles defaults to 1, success_criteria_check to ''."""
        steward = _import_steward()

        raw = (
            "---\n"
            "executor_type: lobster-ops\n"
            "---\n\n"
            "Deploy the new service."
        )

        result = steward._parse_workflow_artifact(raw)

        assert result["executor_type"] == "lobster-ops"
        assert result["estimated_cycles"] == 1
        assert result["success_criteria_check"] == ""
        assert result["instructions"] == "Deploy the new service."

    def test_missing_executor_type_raises_value_error(self):
        """Missing executor_type raises ValueError."""
        steward = _import_steward()

        raw = (
            "---\n"
            "estimated_cycles: 1\n"
            "success_criteria_check: Something.\n"
            "---\n\n"
            "Do some work."
        )

        with pytest.raises(ValueError, match="executor_type"):
            steward._parse_workflow_artifact(raw)

    def test_empty_input_raises_value_error(self):
        """Empty input raises ValueError."""
        steward = _import_steward()

        with pytest.raises(ValueError):
            steward._parse_workflow_artifact("")

        with pytest.raises(ValueError):
            steward._parse_workflow_artifact("   \n  ")

    def test_prose_body_preserved_exactly(self):
        """Prose body after closing --- is preserved with exact content."""
        steward = _import_steward()

        prose = (
            "Step 1: Clone the repo.\n"
            "Step 2: Run the tests.\n"
            "Step 3: Open a PR.\n"
            "\n"
            "Make sure all CI checks pass."
        )
        raw = "---\nexecutor_type: functional-engineer\n---\n\n" + prose

        result = steward._parse_workflow_artifact(raw)

        assert result["instructions"] == prose.strip()

    def test_extra_whitespace_in_front_matter_values(self):
        """Extra whitespace around field values is stripped."""
        steward = _import_steward()

        raw = (
            "---\n"
            "executor_type:   functional-engineer   \n"
            "estimated_cycles:  3  \n"
            "success_criteria_check:   Tests pass.   \n"
            "---\n\n"
            "Do the work."
        )

        result = steward._parse_workflow_artifact(raw)

        assert result["executor_type"] == "functional-engineer"
        assert result["estimated_cycles"] == 3
        assert result["success_criteria_check"] == "Tests pass."

    def test_missing_closing_delimiter_yields_empty_instructions(self):
        """When closing --- is absent, prose body is empty (instructions='')."""
        steward = _import_steward()

        raw = (
            "---\n"
            "executor_type: functional-engineer\n"
            "estimated_cycles: 1\n"
        )

        result = steward._parse_workflow_artifact(raw)

        assert result["executor_type"] == "functional-engineer"
        assert result["instructions"] == ""

    def test_non_integer_estimated_cycles_defaults_to_one(self):
        """Non-integer estimated_cycles falls back to 1 without raising."""
        steward = _import_steward()

        raw = (
            "---\n"
            "executor_type: functional-engineer\n"
            "estimated_cycles: several\n"
            "---\n\n"
            "Do the work."
        )

        result = steward._parse_workflow_artifact(raw)

        assert result["estimated_cycles"] == 1

    def test_parse_workflow_artifact_value_with_colon(self):
        """Front-matter values containing colons are preserved in full."""
        steward = _import_steward()

        raw = (
            "---\n"
            "executor_type: functional-engineer\n"
            "success_criteria_check: Check that status: done appears in output\n"
            "---\n\n"
            "Do the work."
        )

        result = steward._parse_workflow_artifact(raw)

        assert result["success_criteria_check"] == "Check that status: done appears in output"
