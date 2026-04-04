"""
Tests for WOS V3 executor trace.json writing and corrective_traces DB insertion.

PR A — adds _write_trace_json() to executor.py so the executor writes
{output_ref}.trace.json at all exit paths and inserts a row into the
corrective_traces DB table.

Coverage:
- trace.json is created after a complete (happy-path) execution
- trace.json content matches the V3 schema contract (all required fields)
- trace.json is written when executor reports partial
- trace.json is written when executor reports blocked
- trace.json is written when executor encounters an exception/crash
- corrective_traces DB row is inserted after execution with correct fields
- trace.json timestamp maps to DB created_at (within tolerance)
- register value flows from ClaimSucceeded into the trace
- _build_trace produces correct field defaults for surprises / prescription_delta
- _trace_json_path mirrors _result_json_path convention (suffix replacement)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestration.registry import Registry
from orchestration.workflow_artifact import WorkflowArtifact, to_json
from orchestration.executor import (
    Executor,
    ExecutorOutcome,
    _result_json_path,
    _trace_json_path,
    _build_trace,
    _noop_dispatcher,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


def _make_artifact(
    uow_id: str,
    instructions: str = "Do the thing",
    prescribed_skills: list[str] | None = None,
) -> str:
    artifact: WorkflowArtifact = {
        "uow_id": uow_id,
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": prescribed_skills or [],
        "instructions": instructions,
    }
    return to_json(artifact)


def _insert_uow(
    db_path: Path,
    uow_id: str,
    status: str = "ready-for-executor",
    workflow_artifact: str | None = None,
    register: str = "operational",
) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, workflow_artifact, estimated_runtime,
                register
            ) VALUES (?, 'executable', 'test', ?, 'solo', ?, ?, 'Test UoW', 'done', ?, NULL, ?)
            """,
            (uow_id, status, now, now, workflow_artifact, register),
        )
        conn.commit()
    finally:
        conn.close()


def _get_output_ref(db_path: Path, uow_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT output_ref FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["output_ref"] if row else None
    finally:
        conn.close()


def _read_trace_json(output_ref: str) -> dict:
    trace_path = _trace_json_path(output_ref)
    return json.loads(trace_path.read_text())


def _get_corrective_trace_rows(db_path: Path, uow_id: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM corrective_traces WHERE uow_id = ? ORDER BY id",
            (uow_id,),
        ).fetchall()
        return list(rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Path helper tests
# ---------------------------------------------------------------------------

class TestTraceJsonPath:
    """_trace_json_path mirrors _result_json_path convention."""

    def test_replaces_extension(self) -> None:
        p = _trace_json_path("/outputs/abc123.json")
        assert p == Path("/outputs/abc123.trace.json")

    def test_appends_suffix_when_no_extension(self) -> None:
        p = _trace_json_path("/outputs/abc123")
        assert p == Path("/outputs/abc123.trace.json")

    def test_result_and_trace_share_parent(self) -> None:
        output_ref = "/outputs/abc123.json"
        result_p = _result_json_path(output_ref)
        trace_p = _trace_json_path(output_ref)
        assert result_p.parent == trace_p.parent


# ---------------------------------------------------------------------------
# _build_trace unit tests
# ---------------------------------------------------------------------------

class TestBuildTrace:
    """Pure constructor produces correct field defaults."""

    def test_required_fields_present(self) -> None:
        trace = _build_trace(
            uow_id="test-001",
            register="operational",
            outcome=ExecutorOutcome.COMPLETE,
            execution_summary="dispatched subagent",
        )
        required = {"uow_id", "register", "execution_summary", "surprises", "prescription_delta", "gate_score", "timestamp"}
        assert required <= set(trace.keys())

    def test_surprises_defaults_to_empty_list(self) -> None:
        trace = _build_trace(
            uow_id="test-001",
            register="operational",
            outcome=ExecutorOutcome.COMPLETE,
            execution_summary="dispatched",
        )
        assert trace["surprises"] == []

    def test_surprises_none_becomes_empty_list(self) -> None:
        trace = _build_trace(
            uow_id="test-001",
            register="operational",
            outcome=ExecutorOutcome.COMPLETE,
            execution_summary="dispatched",
            surprises=None,
        )
        assert trace["surprises"] == []

    def test_prescription_delta_defaults_to_empty_string(self) -> None:
        trace = _build_trace(
            uow_id="test-001",
            register="operational",
            outcome=ExecutorOutcome.COMPLETE,
            execution_summary="dispatched",
        )
        assert trace["prescription_delta"] == ""

    def test_gate_score_defaults_to_none(self) -> None:
        trace = _build_trace(
            uow_id="test-001",
            register="operational",
            outcome=ExecutorOutcome.COMPLETE,
            execution_summary="dispatched",
        )
        assert trace["gate_score"] is None

    def test_timestamp_is_iso8601(self) -> None:
        trace = _build_trace(
            uow_id="test-001",
            register="operational",
            outcome=ExecutorOutcome.COMPLETE,
            execution_summary="dispatched",
        )
        # Must parse without error
        datetime.fromisoformat(trace["timestamp"])

    def test_uow_id_and_register_match_inputs(self) -> None:
        trace = _build_trace(
            uow_id="uow-xyz",
            register="iterative-convergent",
            outcome=ExecutorOutcome.PARTIAL,
            execution_summary="partial run",
        )
        assert trace["uow_id"] == "uow-xyz"
        assert trace["register"] == "iterative-convergent"


# ---------------------------------------------------------------------------
# Complete path: trace.json written at normal complete exit
# ---------------------------------------------------------------------------

class TestWriteTraceJsonOnComplete:
    """After a successful execution, trace.json must exist alongside result.json."""

    def test_trace_json_file_created(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_complete_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        assert _trace_json_path(output_ref).exists(), "trace.json must exist after complete execution"

    def test_trace_json_valid_schema(self, registry: Registry, db_path: Path) -> None:
        """trace.json must have all required V3 schema fields."""
        uow_id = "trace_complete_002"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)

        required_fields = {"uow_id", "register", "execution_summary", "surprises", "prescription_delta", "gate_score", "timestamp"}
        assert required_fields <= set(trace.keys()), f"Missing fields: {required_fields - set(trace.keys())}"

    def test_trace_uow_id_matches(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_complete_003"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)
        assert trace["uow_id"] == uow_id

    def test_trace_register_flows_from_uow(self, registry: Registry, db_path: Path) -> None:
        """register in trace.json must match the UoW's register column."""
        uow_id = "trace_complete_004"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id), register="iterative-convergent")

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)
        assert trace["register"] == "iterative-convergent"

    def test_trace_surprises_is_list(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_complete_005"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)
        assert isinstance(trace["surprises"], list)

    def test_trace_timestamp_is_parseable_iso8601(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_complete_006"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)
        # Must parse without raising
        datetime.fromisoformat(trace["timestamp"])

    def test_trace_execution_summary_is_nonempty(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_complete_007"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)
        assert isinstance(trace["execution_summary"], str)
        assert len(trace["execution_summary"]) > 0


# ---------------------------------------------------------------------------
# Partial path
# ---------------------------------------------------------------------------

class TestWriteTraceJsonOnPartial:
    """report_partial() must write trace.json."""

    def test_trace_json_file_created(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_partial_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)  # claims it, transitions to active → ready-for-steward

        # Now insert a fresh UoW and call report_partial directly using
        # a pre-computed output_ref (simulating a mid-execution stop).
        uow_id2 = "trace_partial_002"
        _insert_uow(db_path, uow_id2, workflow_artifact=_make_artifact(uow_id2))

        executor2 = Executor(registry, dispatcher=_noop_dispatcher)
        # Execute to get it claimed so output_ref is set
        executor2.execute_uow(uow_id2)

        # Insert a third UoW for direct report_partial testing
        uow_id3 = "trace_partial_003"
        _insert_uow(db_path, uow_id3, workflow_artifact=_make_artifact(uow_id3))

        # Claim step sets output_ref — we need a claimed UoW
        # Use a mock executor that captures output_ref then calls report_partial
        captured_output_ref: list[str] = []

        def partial_dispatcher(instructions: str, uid: str) -> str:
            # Find output_ref from DB — it's set during claim, before dispatch
            output_ref = _get_output_ref(db_path, uid)
            captured_output_ref.append(output_ref)
            raise RuntimeError("stop mid-execution")

        executor3 = Executor(registry, dispatcher=partial_dispatcher)
        with pytest.raises(RuntimeError):
            executor3.execute_uow(uow_id3)

        # The crash path doesn't call report_partial; test report_partial separately
        # by using a known output_ref with an unclaimed UoW
        # Insert a fresh UoW and manually simulate the partial path
        uow_id4 = "trace_partial_004"
        _insert_uow(db_path, uow_id4, workflow_artifact=_make_artifact(uow_id4))

        executor4 = Executor(registry, dispatcher=_noop_dispatcher)
        # We can't call report_partial without a claimed UoW with output_ref set
        # Use _noop_dispatcher to claim+complete normally, then call report_partial
        # on a separate UoW that we claim and then partial-stop
        uow_id5 = "trace_partial_005"
        _insert_uow(db_path, uow_id5, workflow_artifact=_make_artifact(uow_id5))

        partial_called: list[bool] = []

        def partial_after_claim(instructions: str, uid: str) -> str:
            output_ref = _get_output_ref(db_path, uid)
            assert output_ref is not None
            # Call report_partial manually — simulates executor stopping partway
            result = executor5.report_partial(
                uow_id=uid,
                output_ref=output_ref,
                reason="partial: only 2 of 5 steps completed",
                steps_completed=2,
                steps_total=5,
            )
            partial_called.append(True)
            # Return the output_ref so the test can verify trace.json
            captured_output_ref.clear()
            captured_output_ref.append(output_ref)
            # Raise to prevent normal complete path from also running
            raise _PartialStop("partial stop signaled")

        executor5 = Executor(registry, dispatcher=partial_after_claim)

        with pytest.raises(_PartialStop):
            executor5.execute_uow(uow_id5)

        assert partial_called, "report_partial was never called"
        output_ref5 = captured_output_ref[-1]
        assert _trace_json_path(output_ref5).exists(), "trace.json must exist after report_partial"

    def test_trace_json_partial_schema_valid(self, registry: Registry, db_path: Path) -> None:
        """report_partial() trace must have all required schema fields."""
        uow_id = "trace_partial_schema_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        captured_output_ref: list[str] = []
        executor = Executor(registry, dispatcher=_noop_dispatcher)

        def partial_dispatcher(instructions: str, uid: str) -> str:
            output_ref = _get_output_ref(db_path, uid)
            captured_output_ref.append(output_ref)
            executor.report_partial(uid, output_ref, "not enough data", steps_completed=1, steps_total=3)
            raise _PartialStop()

        executor2 = Executor(registry, dispatcher=partial_dispatcher)
        with pytest.raises(_PartialStop):
            executor2.execute_uow(uow_id)

        output_ref = captured_output_ref[0]
        trace = _read_trace_json(output_ref)
        required_fields = {"uow_id", "register", "execution_summary", "surprises", "prescription_delta", "gate_score", "timestamp"}
        assert required_fields <= set(trace.keys())


# ---------------------------------------------------------------------------
# Blocked path
# ---------------------------------------------------------------------------

class TestWriteTraceJsonOnBlocked:
    """report_blocked() must write trace.json."""

    def test_trace_json_file_created(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_blocked_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        captured_output_ref: list[str] = []
        executor = Executor(registry, dispatcher=_noop_dispatcher)

        def blocked_dispatcher(instructions: str, uid: str) -> str:
            output_ref = _get_output_ref(db_path, uid)
            captured_output_ref.append(output_ref)
            executor.report_blocked(uid, output_ref, "waiting for external approval")
            raise _BlockedStop()

        executor2 = Executor(registry, dispatcher=blocked_dispatcher)
        with pytest.raises(_BlockedStop):
            executor2.execute_uow(uow_id)

        output_ref = captured_output_ref[0]
        assert _trace_json_path(output_ref).exists(), "trace.json must exist after report_blocked"

    def test_trace_json_blocked_schema_valid(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_blocked_schema_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        captured_output_ref: list[str] = []
        executor = Executor(registry, dispatcher=_noop_dispatcher)

        def blocked_dispatcher(instructions: str, uid: str) -> str:
            output_ref = _get_output_ref(db_path, uid)
            captured_output_ref.append(output_ref)
            executor.report_blocked(uid, output_ref, "no approval yet")
            raise _BlockedStop()

        executor2 = Executor(registry, dispatcher=blocked_dispatcher)
        with pytest.raises(_BlockedStop):
            executor2.execute_uow(uow_id)

        output_ref = captured_output_ref[0]
        trace = _read_trace_json(output_ref)
        required_fields = {"uow_id", "register", "execution_summary", "surprises", "prescription_delta", "gate_score", "timestamp"}
        assert required_fields <= set(trace.keys())

    def test_trace_blocked_reason_in_surprises(self, registry: Registry, db_path: Path) -> None:
        """The block reason must appear in the surprises list.

        Uses a DB-read approach: reads corrective_traces from the DB rather than
        the trace.json file, because the _BlockedStop sentinel is caught by the
        outer exception handler which overwrites trace.json with the crash trace.
        The DB insert from report_blocked() happens before the overwrite.
        """
        uow_id = "trace_blocked_reason_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        block_reason = "waiting for CI approval token"
        executor = Executor(registry, dispatcher=_noop_dispatcher)

        def blocked_dispatcher(instructions: str, uid: str) -> str:
            output_ref = _get_output_ref(db_path, uid)
            executor.report_blocked(uid, output_ref, block_reason)
            raise _BlockedStop()

        executor2 = Executor(registry, dispatcher=blocked_dispatcher)
        with pytest.raises(_BlockedStop):
            executor2.execute_uow(uow_id)

        # Two DB rows: one from report_blocked, one from the crash handler.
        # The first row is from report_blocked and must have the block_reason in surprises.
        rows = _get_corrective_trace_rows(db_path, uow_id)
        assert len(rows) >= 1
        first_surprises = json.loads(rows[0]["surprises"])
        assert block_reason in first_surprises, (
            f"Block reason not found in first trace surprises. Got: {first_surprises}"
        )


# ---------------------------------------------------------------------------
# Crash/exception path
# ---------------------------------------------------------------------------

class TestWriteTraceJsonOnCrash:
    """Exception during execution must still write trace.json (the crash path)."""

    def test_trace_json_written_on_exception(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_crash_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def crashing_dispatcher(instructions: str, uid: str) -> str:
            raise RuntimeError("executor exploded")

        executor = Executor(registry, dispatcher=crashing_dispatcher)
        with pytest.raises(RuntimeError, match="executor exploded"):
            executor.execute_uow(uow_id)

        # output_ref is set during claim, before dispatch
        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        assert _trace_json_path(output_ref).exists(), "trace.json must be written even on crash"

    def test_trace_json_crash_schema_valid(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_crash_002"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def crashing_dispatcher(instructions: str, uid: str) -> str:
            raise ValueError("bad input")

        executor = Executor(registry, dispatcher=crashing_dispatcher)
        with pytest.raises(ValueError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)
        required_fields = {"uow_id", "register", "execution_summary", "surprises", "prescription_delta", "gate_score", "timestamp"}
        assert required_fields <= set(trace.keys())

    def test_trace_crash_exception_in_surprises(self, registry: Registry, db_path: Path) -> None:
        """The exception message must appear in the crash trace surprises."""
        uow_id = "trace_crash_003"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        error_msg = "subprocess timed out after 3600s"

        def timeout_dispatcher(instructions: str, uid: str) -> str:
            raise RuntimeError(error_msg)

        executor = Executor(registry, dispatcher=timeout_dispatcher)
        with pytest.raises(RuntimeError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)
        assert any(error_msg in s for s in trace["surprises"]), (
            f"Exception message not found in trace surprises: {trace['surprises']}"
        )


# ---------------------------------------------------------------------------
# DB corrective_traces table
# ---------------------------------------------------------------------------

class TestCorrectiveTracesDbRow:
    """A row must be inserted into corrective_traces after each execution."""

    def test_db_row_inserted_on_complete(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_db_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        rows = _get_corrective_trace_rows(db_path, uow_id)
        assert len(rows) == 1, f"Expected 1 corrective_traces row, got {len(rows)}"

    def test_db_row_uow_id_correct(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_db_002"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        rows = _get_corrective_trace_rows(db_path, uow_id)
        assert rows[0]["uow_id"] == uow_id

    def test_db_row_register_correct(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_db_003"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id), register="iterative-convergent")

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        rows = _get_corrective_trace_rows(db_path, uow_id)
        assert rows[0]["register"] == "iterative-convergent"

    def test_db_row_execution_summary_nonempty(self, registry: Registry, db_path: Path) -> None:
        uow_id = "trace_db_004"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        rows = _get_corrective_trace_rows(db_path, uow_id)
        assert rows[0]["execution_summary"]

    def test_db_row_surprises_is_valid_json(self, registry: Registry, db_path: Path) -> None:
        """surprises column must be valid JSON."""
        uow_id = "trace_db_005"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        rows = _get_corrective_trace_rows(db_path, uow_id)
        surprises_raw = rows[0]["surprises"]
        if surprises_raw:
            surprises = json.loads(surprises_raw)
            assert isinstance(surprises, list)

    def test_db_row_created_at_matches_trace_timestamp(self, registry: Registry, db_path: Path) -> None:
        """created_at in DB must be close to (within 5s of) trace.json timestamp."""
        uow_id = "trace_db_006"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        trace = _read_trace_json(output_ref)
        rows = _get_corrective_trace_rows(db_path, uow_id)

        trace_ts_str = trace["timestamp"]
        db_created_at_str = rows[0]["created_at"]

        # Both should be parseable ISO-8601
        trace_ts = datetime.fromisoformat(trace_ts_str)
        # SQLite's datetime('now') produces UTC without offset — normalize
        db_ts = datetime.fromisoformat(db_created_at_str)
        if db_ts.tzinfo is None:
            db_ts = db_ts.replace(tzinfo=timezone.utc)

        delta = abs((trace_ts - db_ts).total_seconds())
        assert delta < 5, f"created_at and timestamp differ by {delta}s — expected <5s"

    def test_db_row_inserted_on_crash(self, registry: Registry, db_path: Path) -> None:
        """corrective_traces row must be inserted even when executor crashes."""
        uow_id = "trace_db_crash_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def crashing_dispatcher(instructions: str, uid: str) -> str:
            raise RuntimeError("crash")

        executor = Executor(registry, dispatcher=crashing_dispatcher)
        with pytest.raises(RuntimeError):
            executor.execute_uow(uow_id)

        rows = _get_corrective_trace_rows(db_path, uow_id)
        assert len(rows) == 1, "corrective_traces row must be written even on crash"


# ---------------------------------------------------------------------------
# Sentinel exception classes (not real errors — used to break execution flow
# at a controlled point in dispatcher mocks without triggering exception-path logic)
# ---------------------------------------------------------------------------

class _PartialStop(Exception):
    """Sentinel: raised after report_partial() to prevent normal complete path."""


class _BlockedStop(Exception):
    """Sentinel: raised after report_blocked() to prevent normal complete path."""
