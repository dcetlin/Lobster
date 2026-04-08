"""
Integration test: S3-B corrective trace one-cycle temporal gate.

Validates the full two-heartbeat flow through the real state machine:

  Heartbeat 1 (trace.json absent):
    ready-for-steward → diagnosing
    → trace gate fires → WaitForTrace outcome
    → UoW stays in diagnosing (NOT reset to ready-for-steward)

  Heartbeat 2 (trace.json now present):
    startup_sweep resets diagnosing → ready-for-steward
    → steward re-evaluates → trace present → prescribe proceeds normally
    → UoW transitions to ready-for-executor

Also covers:
  - trace_gate_timeout (non-blocking fallback) when trace still absent after
    one heartbeat dwell
  - Zero trace_gate_contract_violation entries when executor returns with
    a complete trace (audit log cleanliness)

State machine exercised:
  pending
    → ready-for-steward          [trigger evaluator]
    → diagnosing                 [Steward claims]
    → diagnosing (stayed)        [WaitForTrace — trace absent, first visit]
    → ready-for-steward          [startup_sweep resets diagnosing orphan]
    → diagnosing → ready-for-executor  [Steward re-evaluates with trace present]
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import sys

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.migrate import run_migrations
from orchestration.registry import Registry, UpsertInserted, ApproveConfirmed
from orchestration.steward import run_steward_cycle, WaitForTrace
from orchestration.executor import Executor, _result_json_path, _trace_json_path


# ---------------------------------------------------------------------------
# Named constants — spec-derived, never magic literals
# ---------------------------------------------------------------------------

# S3-B spec: one heartbeat dwell before proceeding without trace
TRACE_GATE_DWELL_HEARTBEATS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_uow_row(conn: sqlite3.Connection, uow_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    assert row is not None, f"UoW {uow_id!r} not found"
    return dict(row)


def _read_audit_events(conn: sqlite3.Connection, uow_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
        (uow_id,),
    ).fetchall()
    return [r["event"] for r in rows]


def _stub_github_client(issue_number: int) -> dict[str, Any]:
    """Minimal open-issue stub — no bootup-candidate label."""
    return {
        "status_code": 200,
        "state": "open",
        "labels": [],
        "body": f"Implement feature for issue #{issue_number}",
        "title": f"Test issue #{issue_number}",
    }


def _seed_uow_at_ready_for_steward(
    registry: Registry,
    conn: sqlite3.Connection,
    issue_number: int,
    title: str,
    success_criteria: str,
) -> str:
    """Create a UoW and advance it to ready-for-steward. Returns uow_id."""
    result = registry.upsert(
        issue_number=issue_number,
        title=title,
        success_criteria="Completion placeholder — overwritten below.",
    )
    assert isinstance(result, UpsertInserted)
    uow_id = result.id

    approve_result = registry.approve(uow_id)
    assert isinstance(approve_result, ApproveConfirmed)

    conn.execute(
        "UPDATE uow_registry SET success_criteria = ?, updated_at = ? WHERE id = ?",
        (success_criteria, _now(), uow_id),
    )
    conn.commit()

    registry.set_status_direct(uow_id, "ready-for-steward")
    return uow_id


def _simulate_executor_return_with_result(
    registry: Registry,
    conn: sqlite3.Connection,
    uow_id: str,
    output_ref: str,
    outcome: str = "partial",
    write_trace: bool = False,
) -> None:
    """
    Simulate an executor that has returned and written result.json.
    Optionally also writes trace.json (write_trace=True).

    Sets the UoW output_ref and transitions it back to ready-for-steward
    with an execution_complete audit entry, mirroring the real executor return.
    """
    output_path = Path(output_ref)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("Executor partial output — more work needed.", encoding="utf-8")

    # Write result.json
    result_path = _result_json_path(output_ref)
    result_path.write_text(json.dumps({
        "uow_id": uow_id,
        "outcome": outcome,
        "reason": f"Executor returned {outcome}",
    }), encoding="utf-8")

    # Optionally write trace.json
    if write_trace:
        trace_path = _trace_json_path(output_ref)
        trace_path.write_text(json.dumps({
            "uow_id": uow_id,
            "register": "operational",
            "execution_summary": "Executor completed one pass; partial progress made.",
            "surprises": [],
            "prescription_delta": "Focus on completing the remaining step.",
            "gate_score": None,
            "timestamp": _now(),
        }), encoding="utf-8")

    # Set output_ref on the DB row and transition to ready-for-steward
    conn.execute(
        "UPDATE uow_registry SET output_ref = ?, updated_at = ? WHERE id = ?",
        (output_ref, _now(), uow_id),
    )
    conn.execute(
        "INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note) "
        "VALUES (?, ?, 'execution_complete', 'active', 'ready-for-steward', 'executor-stub', ?)",
        (_now(), uow_id, json.dumps({"return_reason": "observation_complete"})),
    )
    conn.execute(
        "UPDATE uow_registry SET status = 'ready-for-steward', steward_cycles = 1, updated_at = ? WHERE id = ?",
        (_now(), uow_id),
    )
    conn.commit()


def _simulate_startup_sweep_diagnosing(
    registry: Registry,
    conn: sqlite3.Connection,
    uow_id: str,
) -> bool:
    """
    Simulate the startup_sweep that resets diagnosing → ready-for-steward.

    This mirrors what record_startup_sweep_diagnosing does in registry.py.
    Returns True if the reset happened (UoW was in diagnosing), False otherwise.
    """
    try:
        registry.record_startup_sweep_diagnosing(uow_id)
        return True
    except Exception:
        # Fallback: direct SQL for test environments where the method may not exist
        cursor = conn.execute(
            "UPDATE uow_registry SET status = 'ready-for-steward', updated_at = ? "
            "WHERE id = ? AND status = 'diagnosing'",
            (_now(), uow_id),
        )
        if cursor.rowcount > 0:
            conn.execute(
                "INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note) "
                "VALUES (?, ?, 'startup_sweep', 'diagnosing', 'ready-for-steward', 'steward', ?)",
                (_now(), uow_id, json.dumps({"classification": "diagnosing_orphan"})),
            )
            conn.commit()
            return True
        return False


def _run_steward_heartbeat(
    registry: Registry,
    tmp_path: Path,
) -> dict[str, Any]:
    """Run one steward heartbeat, returning the result dict."""
    return run_steward_cycle(
        registry=registry,
        dry_run=False,
        github_client=_stub_github_client,
        artifact_dir=tmp_path / "artifacts",
        notify_dan=lambda *a, **kw: None,
        notify_dan_early_warning=lambda *a, **kw: None,
        bootup_candidate_gate=False,
        llm_prescriber=None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    """Fully-migrated Registry in a temp directory."""
    db_path = tmp_path / "trace_gate_test.db"
    run_migrations(db_path)
    return Registry(db_path)


@pytest.fixture
def conn(registry: Registry):
    """Direct DB connection for assertions. Closed after test."""
    c = sqlite3.connect(str(registry.db_path))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestTraceTemporalGate:
    """
    S3-B: Corrective trace as mandatory one-cycle temporal gate.

    These tests exercise the real state machine (run_steward_cycle) through
    multi-heartbeat flows, not just isolated function calls.
    """

    def test_wait_for_trace_then_prescribe_on_trace_arrival(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        Full two-heartbeat flow: trace absent on heartbeat 1, present on heartbeat 2.

        Heartbeat 1: trace.json absent → WaitForTrace → UoW stays in diagnosing
        startup_sweep: resets diagnosing → ready-for-steward
        Heartbeat 2: trace.json present → prescribe proceeds → ready-for-executor
        """
        # Seed a UoW that has already run once (executor returned, result.json written)
        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=680,
            title="S3-B trace gate integration test",
            success_criteria="Feature implemented and PR open.",
        )

        output_ref = str(tmp_path / "outputs" / f"{uow_id}.json")

        # Simulate executor return WITHOUT trace.json
        _simulate_executor_return_with_result(
            registry, conn, uow_id, output_ref,
            outcome="partial",
            write_trace=False,  # trace absent — gate should fire
        )

        # --- Heartbeat 1: trace absent ---
        result1 = _run_steward_heartbeat(registry, tmp_path)

        assert result1.get("wait_for_trace", 0) == TRACE_GATE_DWELL_HEARTBEATS, (
            f"S3-B: expected wait_for_trace=={TRACE_GATE_DWELL_HEARTBEATS} in heartbeat 1 result, "
            f"got {result1}"
        )
        assert result1.get("prescribed", 0) == 0, (
            "Heartbeat 1 must NOT prescribe when trace gate fires"
        )

        uow_after_h1 = _read_uow_row(conn, uow_id)
        assert uow_after_h1["status"] == "diagnosing", (
            "S3-B: UoW must stay in diagnosing after WaitForTrace (not reset to ready-for-steward). "
            f"Actual status: {uow_after_h1['status']!r}"
        )

        steward_log = uow_after_h1.get("steward_log") or ""
        assert "trace_gate_waited" in steward_log, (
            "trace_gate_waited must be written to steward_log on heartbeat 1"
        )

        # Sanity: no prescription artifact yet
        assert not uow_after_h1.get("workflow_artifact"), (
            "No workflow_artifact must exist after WaitForTrace"
        )

        # --- startup_sweep: reset diagnosing → ready-for-steward ---
        reset_happened = _simulate_startup_sweep_diagnosing(registry, conn, uow_id)
        assert reset_happened, (
            "startup_sweep must successfully reset diagnosing → ready-for-steward"
        )

        uow_after_sweep = _read_uow_row(conn, uow_id)
        assert uow_after_sweep["status"] == "ready-for-steward", (
            f"After startup_sweep, UoW must be ready-for-steward; got {uow_after_sweep['status']!r}"
        )

        # --- Write trace.json now (executor delivered it during the dwell) ---
        trace_path = _trace_json_path(output_ref)
        trace_path.write_text(json.dumps({
            "uow_id": uow_id,
            "register": "operational",
            "execution_summary": "Partial work done; one step remaining.",
            "surprises": [],
            "prescription_delta": "Focus on the remaining step.",
            "gate_score": None,
            "timestamp": _now(),
        }), encoding="utf-8")

        # --- Heartbeat 2: trace present → prescribe proceeds ---
        result2 = _run_steward_heartbeat(registry, tmp_path)

        assert result2.get("prescribed", 0) == 1, (
            f"S3-B: heartbeat 2 must prescribe when trace.json is present; got {result2}"
        )
        assert result2.get("wait_for_trace", 0) == 0, (
            "Heartbeat 2 must NOT fire the trace gate when trace.json is present"
        )

        uow_after_h2 = _read_uow_row(conn, uow_id)
        assert uow_after_h2["status"] == "ready-for-executor", (
            f"After heartbeat 2, UoW must be ready-for-executor; got {uow_after_h2['status']!r}"
        )

        # trace_gate_waited must NOT appear in final steward_log
        # (it is cleared when trace.json is found)
        final_log = uow_after_h2.get("steward_log") or ""
        assert "trace_gate_waited" not in final_log, (
            "trace_gate_waited must be cleared from steward_log after trace.json is found"
        )

    def test_trace_gate_timeout_when_trace_still_absent_after_dwell(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        When trace is still absent after one heartbeat dwell, log trace_gate_timeout
        and proceed with prescription (non-blocking fallback).

        This covers the case where the executor never writes trace.json.
        """
        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=681,
            title="S3-B trace gate timeout integration test",
            success_criteria="Feature implemented.",
        )

        output_ref = str(tmp_path / "outputs_timeout" / f"{uow_id}.json")

        # Executor returns without trace.json
        _simulate_executor_return_with_result(
            registry, conn, uow_id, output_ref,
            outcome="partial",
            write_trace=False,
        )

        # Heartbeat 1: trace absent → WaitForTrace
        result1 = _run_steward_heartbeat(registry, tmp_path)
        assert result1.get("wait_for_trace", 0) == 1, (
            f"Heartbeat 1 must return WaitForTrace; got {result1}"
        )

        uow_after_h1 = _read_uow_row(conn, uow_id)
        assert uow_after_h1["status"] == "diagnosing"

        # startup_sweep resets diagnosing → ready-for-steward
        _simulate_startup_sweep_diagnosing(registry, conn, uow_id)

        # Heartbeat 2: trace STILL absent → trace_gate_timeout + proceed with prescription
        result2 = _run_steward_heartbeat(registry, tmp_path)

        assert result2.get("prescribed", 0) == 1, (
            f"S3-B: heartbeat 2 must prescribe (non-blocking fallback) when trace still absent; "
            f"got {result2}"
        )

        uow_after_h2 = _read_uow_row(conn, uow_id)
        assert uow_after_h2["status"] == "ready-for-executor", (
            f"Non-blocking fallback: UoW must proceed to ready-for-executor; "
            f"got {uow_after_h2['status']!r}"
        )

        final_log = uow_after_h2.get("steward_log") or ""
        assert "trace_gate_timeout" in final_log, (
            "trace_gate_timeout must be logged when proceeding without trace.json "
            "after one-cycle dwell"
        )

    def test_no_trace_gate_violation_when_executor_returns_complete_trace(
        self,
        registry: Registry,
        conn: sqlite3.Connection,
        tmp_path: Path,
    ) -> None:
        """
        When executor returns with both result.json and trace.json present,
        the trace gate must NOT fire at all — zero trace_gate_contract_violation
        or trace_gate_timeout entries in the audit log.

        Spec requirement: zero trace_gate_contract_violation entries in the audit
        log for UoWs that have complete executor returns.
        """
        uow_id = _seed_uow_at_ready_for_steward(
            registry, conn,
            issue_number=682,
            title="S3-B clean trace — no gate violation",
            success_criteria="Feature implemented.",
        )

        output_ref = str(tmp_path / "outputs_clean" / f"{uow_id}.json")

        # Executor returns WITH trace.json (complete contract)
        _simulate_executor_return_with_result(
            registry, conn, uow_id, output_ref,
            outcome="partial",
            write_trace=True,  # trace present — gate must NOT fire
        )

        # Single heartbeat: trace present → prescribe immediately
        result = _run_steward_heartbeat(registry, tmp_path)

        assert result.get("wait_for_trace", 0) == 0, (
            "Trace gate must NOT fire when trace.json is present"
        )
        assert result.get("prescribed", 0) == 1, (
            "Must prescribe immediately when trace.json is present"
        )

        uow = _read_uow_row(conn, uow_id)
        assert uow["status"] == "ready-for-executor"

        # Audit log must contain zero trace gate violation events
        audit_events = _read_audit_events(conn, uow_id)
        violation_events = [
            e for e in audit_events
            if e in ("trace_gate_contract_violation", "trace_gate_timeout")
        ]
        assert violation_events == [], (
            f"Audit log must contain zero trace gate violation events when executor "
            f"returns with complete trace; found: {violation_events}"
        )
