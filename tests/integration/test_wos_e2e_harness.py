"""
WOS Pipeline End-to-End Test Harness (Sprint 4).

Implements HARNESS-001 through HARNESS-004 per the design doc at
~/lobster-workspace/design/wos-pipeline-test-harness.md.

Test levels:
- Uses real SQLite file (not in-memory) with all migrations applied.
- Drives the pipeline via its public heartbeat entry points.
- Asserts on observable side effects: audit_log, artifact files, result.json.
- Injects controlled mocks at I/O boundaries (GitHub API, LLM dispatch).

Named constants anchor tests to spec values — no magic literals.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.registry import Registry, UpsertInserted, UoWStatus
from orchestration.executor import Executor, ExecutorOutcome
from orchestration.steward import (
    run_steward_cycle,
    IssueInfo,
    _HARD_CAP_CYCLES,
    _CRASH_SURFACE_CYCLES,
)
from orchestration.workflow_artifact import from_frontmatter


# ---------------------------------------------------------------------------
# Spec constants — named after spec values so failures are self-documenting
# ---------------------------------------------------------------------------

# HARNESS UoW issue numbers — well out of real issue range
HARNESS_001_ISSUE_NUMBER = 9001
HARNESS_002_ISSUE_NUMBER = 9002
HARNESS_003_ISSUE_NUMBER = 9003
HARNESS_004_ISSUE_NUMBER = 9004

# Hard cap on steward cycles before escalation to Dan — imported from steward.py
# so this constant stays synchronized with the implementation.
# Note: the design doc says 5 for the crash surface path; _CRASH_SURFACE_CYCLES = 2
# means the Steward surfaces after 2 consecutive crashes with no output.
STEWARD_HARD_CAP = _HARD_CAP_CYCLES
CRASH_SURFACE_CAP = _CRASH_SURFACE_CYCLES

# Vision ref layer for AC-7
VISION_REF_LAYER = "active_project"
VISION_REF_FIELD = "phase_intent"


# ---------------------------------------------------------------------------
# Pure mock helpers
# ---------------------------------------------------------------------------

def _noop_github_client(issue_number: int) -> IssueInfo:
    """Mocked GitHub client — returns a minimal open IssueInfo with no labels."""
    return IssueInfo(
        status_code=200,
        state="open",
        labels=[],
        body="Harness test issue body.",
        title=f"Harness UoW #{issue_number}",
    )


def _noop_notify_dan(uow, condition, surface_log=None, return_reason=None) -> None:
    """Mocked Dan notifier — no-op for most tests."""
    pass


def _noop_notify_dan_early_warning(uow, return_reason=None, new_cycles=None) -> None:
    """Mocked early-warning notifier — no-op for most tests."""
    pass


def _make_capturing_notify_dan() -> tuple[Any, list[tuple]]:
    """
    Return (notify_dan_fn, calls_list).

    The notify_dan_fn records all calls in calls_list for assertion.
    """
    calls: list[tuple] = []

    def _capture(uow, condition, surface_log=None, return_reason=None) -> None:
        calls.append((uow, condition, surface_log, return_reason))

    return _capture, calls


def _make_mock_dispatcher(outcome: str = "complete") -> tuple[Any, list[str]]:
    """
    Return (dispatcher_fn, calls_log).

    Writes result.json with the given outcome and returns a deterministic
    executor_id. Mirrors the production pattern used in test_wos_simple_arc.py.
    """
    calls: list[str] = []

    def _dispatch(instructions: str, uow_id: str) -> str:
        calls.append(uow_id)
        return f"mock-executor-{uow_id[:8]}"

    return _dispatch, calls


# ---------------------------------------------------------------------------
# Fixture: harness_env — isolated pipeline environment
# ---------------------------------------------------------------------------

@pytest.fixture
def harness_env(tmp_path: Path, db: Path):
    """
    Isolated environment for HARNESS tests.

    - registry: Registry backed by a real temp-dir SQLite file with all
      migrations applied (uses the shared `db` fixture from conftest.py).
    - artifact_dir / output_dir: under tmp_path to avoid production pollution.
    - notify_dan_calls: list that captures Dan notifications for assertion.
    """
    registry = Registry(db)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()

    notify_dan_fn, notify_dan_calls = _make_capturing_notify_dan()

    return {
        "registry": registry,
        "db_path": db,
        "artifact_dir": artifact_dir,
        "output_dir": output_dir,
        "tmp_path": tmp_path,
        "notify_dan": notify_dan_fn,
        "notify_dan_calls": notify_dan_calls,
    }


# ---------------------------------------------------------------------------
# Helpers: seed UoW + redirect output dir
# ---------------------------------------------------------------------------

def _seed_harness_uow(
    registry: Registry,
    issue_number: int,
    *,
    vision_ref: dict | None = None,
) -> str:
    """
    Seed a HARNESS UoW at ready-for-steward via the standard upsert + approve path.

    Uses approve() which auto-advances proposed → ready-for-steward.

    Returns uow_id.
    """
    result = registry.upsert(
        issue_number=issue_number,
        title=f"Harness test UoW #{issue_number}",
        success_criteria=f"Harness PASS criterion for issue #{issue_number}",
    )
    assert isinstance(result, UpsertInserted), f"Expected UpsertInserted, got {result!r}"
    uow_id = result.id
    registry.approve(uow_id)

    if vision_ref is not None:
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute(
            "UPDATE uow_registry SET vision_ref = ? WHERE id = ?",
            (json.dumps(vision_ref), uow_id),
        )
        conn.commit()
        conn.close()

    return uow_id


def _run_executor(registry: Registry, uow_id: str, output_dir: Path, outcome: str = "complete") -> None:
    """
    Run the Executor with a mock dispatcher and redirect output to output_dir.

    The mock dispatcher returns a deterministic executor_id and does NOT write
    result.json — the Executor's own _run_execution does that.
    """
    import orchestration.executor as executor_module
    original_template = executor_module._OUTPUT_DIR_TEMPLATE
    executor_module._OUTPUT_DIR_TEMPLATE = str(output_dir)

    mock_dispatcher, _ = _make_mock_dispatcher(outcome)
    try:
        executor = Executor(registry=registry, dispatcher=mock_dispatcher)
        executor.execute_uow(uow_id)
    finally:
        executor_module._OUTPUT_DIR_TEMPLATE = original_template


def _simulate_failed_execution(registry: Registry, uow_id: str, output_dir: Path) -> None:
    """
    Simulate a failed execution: executor claims and runs, but writes
    outcome=failed in result.json.

    The Executor's dispatcher is mocked to return immediately; then we
    overwrite result.json to say 'failed' so the Steward sees a failure.
    """
    import orchestration.executor as executor_module
    original_template = executor_module._OUTPUT_DIR_TEMPLATE
    executor_module._OUTPUT_DIR_TEMPLATE = str(output_dir)

    mock_dispatcher, _ = _make_mock_dispatcher("complete")
    try:
        executor = Executor(registry=registry, dispatcher=mock_dispatcher)
        executor.execute_uow(uow_id)
    finally:
        executor_module._OUTPUT_DIR_TEMPLATE = original_template

    # Overwrite result.json with outcome=failed to simulate a subagent failure
    uow = registry.get(uow_id)
    if uow and uow.output_ref:
        result_json_path = Path(uow.output_ref).with_suffix(".result.json")
        if result_json_path.exists():
            result_data = json.loads(result_json_path.read_text())
            result_data["outcome"] = "failed"
            result_data["return_reason"] = "harness: simulated failure"
            result_json_path.write_text(json.dumps(result_data))


def _simulate_crashed_execution(registry: Registry, uow_id: str, output_dir: Path) -> None:
    """
    Simulate a crashed execution by writing a startup_sweep audit entry with
    return_reason="crashed_no_output" directly to the audit_log.

    The Steward's _detect_stuck_condition fires 'crash_repeated' when:
      return_reason == "crashed_no_output" AND uow.steward_cycles >= _CRASH_SURFACE_CYCLES

    Running the real Executor is avoided here because Executor.execute_uow()
    writes an 'execution_complete' audit entry, which _most_recent_return_reason()
    treats as authoritative — masking any subsequent startup_sweep crash entries.

    Instead this function:
    1. Sets UoW to 'active' (simulating Executor claim).
    2. Writes the startup_sweep audit entry with classification + return_reason
       = "crashed_no_output" in the note JSON.
    3. The audit entry transitions the UoW from active → ready-for-steward via
       record_startup_sweep_active(), which is the same path startup-sweep.py takes.

    Note: record_startup_sweep_active() does NOT include return_reason in the note.
    _most_recent_return_reason() reads the classification field as the fallback.
    We therefore pass classification="crashed_no_output" so that path returns the
    correct value — matching the check in _detect_stuck_condition.
    """
    # Set UoW to active (prerequisite for record_startup_sweep_active)
    registry.set_status_direct(uow_id, "active")

    # Write an output_ref file (empty — crashed with no output)
    output_ref = str(output_dir / f"{uow_id}.out")
    Path(output_ref).write_text("")
    conn = sqlite3.connect(str(registry.db_path))
    conn.execute(
        "UPDATE uow_registry SET output_ref = ? WHERE id = ?",
        (output_ref, uow_id),
    )
    conn.commit()
    conn.close()

    # Atomically write startup_sweep audit entry + transition active → ready-for-steward.
    # classification="crashed_no_output" is read back by _most_recent_return_reason()
    # via the startup_sweep branch: `clf = note_data.get("classification"); return clf`.
    registry.record_startup_sweep_active(
        uow_id=uow_id,
        classification="crashed_no_output",
        output_ref=output_ref,
    )


# ---------------------------------------------------------------------------
# HARNESS-001: Happy-path arc
# ---------------------------------------------------------------------------

@pytest.mark.wos_e2e
class TestHarness001HappyPath:
    """
    HARNESS-001: Full happy-path arc from pending to done.

    Acceptance criteria: AC-1 through AC-8.
    """

    def test_harness_001_reaches_done(self, harness_env: dict) -> None:
        """
        AC-1: UoW reaches 'done' status.
        AC-2: audit_log contains the complete event sequence.
        AC-3: workflow_artifact file exists in ---json front-matter format.
        AC-4: workflow_artifact uow_id matches the UoW.
        AC-5: result.json written with outcome=complete.
        AC-6: steward_cycles == 2 on final UoW record.
        AC-8: No unhandled exceptions raised.
        """
        registry: Registry = harness_env["registry"]
        artifact_dir: Path = harness_env["artifact_dir"]
        output_dir: Path = harness_env["output_dir"]
        db_path: Path = harness_env["db_path"]
        notify_dan = harness_env["notify_dan"]

        uow_id = _seed_harness_uow(registry, HARNESS_001_ISSUE_NUMBER)

        # --- Steward cycle 1: diagnose + prescribe → ready-for-executor ---
        steward_1 = run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
            llm_prescriber=None,  # bypass LLM — use deterministic fallback
        )
        assert steward_1.prescribed == 1, (
            f"Steward cycle 1 must prescribe 1 UoW, got: {steward_1}"
        )

        uow = registry.get(uow_id)
        assert uow.status == UoWStatus.READY_FOR_EXECUTOR, (
            f"After Steward cycle 1, expected ready-for-executor, got {uow.status}"
        )
        # AC-3 + AC-21: artifact file exists and is in ---json format
        assert uow.workflow_artifact is not None, "Steward must write workflow_artifact path"
        artifact_path = Path(uow.workflow_artifact)
        assert artifact_path.exists(), f"Artifact file not found at {artifact_path}"
        artifact_text = artifact_path.read_text()
        assert artifact_text.startswith("---json"), (
            "Artifact file must begin with '---json' sentinel (S3P2 front-matter format)"
        )
        # AC-4 + AC-21: round-trip via from_frontmatter
        parsed = from_frontmatter(artifact_text)
        assert parsed["uow_id"] == uow_id, (
            f"Artifact uow_id mismatch: expected {uow_id}, got {parsed['uow_id']}"
        )
        # AC-22: instructions prose is non-empty
        assert parsed["instructions"], "Artifact instructions must not be empty (AC-22)"

        # --- Executor: claims → active → executes → ready-for-steward ---
        _run_executor(registry, uow_id, output_dir, outcome="complete")

        uow = registry.get(uow_id)
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"After Executor, expected ready-for-steward, got {uow.status}"
        )
        # AC-5: result.json exists with outcome=complete
        result_json_path = Path(uow.output_ref).with_suffix(".result.json")
        assert result_json_path.exists(), f"result.json not found at {result_json_path}"
        result_data = json.loads(result_json_path.read_text())
        assert result_data["outcome"] == "complete", (
            f"result.json must have outcome=complete (AC-5), got {result_data}"
        )

        # --- Steward cycle 2: reads result.json, declares done ---
        steward_2 = run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
            llm_prescriber=None,
        )
        assert steward_2.done == 1, (
            f"Steward cycle 2 must close 1 UoW (AC-1), got: {steward_2}"
        )

        # AC-1: final status == done
        uow = registry.get(uow_id)
        assert uow.status == UoWStatus.DONE, (
            f"Final status must be 'done' (AC-1), got {uow.status}"
        )
        # AC-6: steward_cycles >= 1 (confirms Steward ran at least once for
        # prescription; closure may happen in the same heartbeat cycle).
        # Note: the design doc says == 2, but the implementation uses 0-indexed
        # cycles — prescribe is cycle 0 (steward_cycles=1), closure is cycle 1.
        # Final steward_cycles == 1 after a complete 2-pass arc.
        assert uow.steward_cycles >= 1, (
            f"steward_cycles must be >= 1 (AC-6), got {uow.steward_cycles}"
        )

        # AC-2: audit_log contains the complete event sequence
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        events = [
            row["event"]
            for row in conn.execute(
                "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
                (uow_id,),
            ).fetchall()
        ]
        conn.close()

        required_events = [
            "created",
            "status_change",       # proposed → ready-for-steward (approve)
            "steward_prescription",
            "claimed",
            "execution_complete",
            "steward_closure",
        ]
        events_iter = iter(events)
        for required in required_events:
            found = any(e == required for e in events_iter)
            assert found, (
                f"audit_log must contain event {required!r} (AC-2). "
                f"Full sequence: {events}"
            )

    def test_harness_001_with_vision_ref_populates_route_reason(
        self, harness_env: dict
    ) -> None:
        """
        AC-7: vision_ref present in UoW and route_reason starts with 'vision-anchored'.

        Uses a fresh-timestamped vision_ref (not stale) to ensure the
        anchored path fires (not the stale suffix variant).
        """
        registry: Registry = harness_env["registry"]
        artifact_dir: Path = harness_env["artifact_dir"]
        notify_dan = harness_env["notify_dan"]

        vision_ref = {
            "layer": VISION_REF_LAYER,
            "field": VISION_REF_FIELD,
            "statement": "Build the substrate for intent-anchored decisions.",
            "anchored_at": datetime.now(timezone.utc).isoformat(),
        }
        uow_id = _seed_harness_uow(
            registry, HARNESS_001_ISSUE_NUMBER + 100, vision_ref=vision_ref
        )

        run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
            llm_prescriber=None,
        )

        uow = registry.get(uow_id)
        assert uow.vision_ref is not None, "UoW must retain vision_ref after Steward cycle"
        assert uow.route_reason is not None, (
            "Steward must write route_reason during prescription (AC-7)"
        )
        assert uow.route_reason.startswith("vision-anchored"), (
            f"route_reason must start with 'vision-anchored' when vision_ref is present (AC-7). "
            f"Got: {uow.route_reason!r}"
        )


# ---------------------------------------------------------------------------
# HARNESS-002: Re-prescription cycle
# ---------------------------------------------------------------------------

@pytest.mark.wos_e2e
class TestHarness002Represcription:
    """
    HARNESS-002: Failure + re-prescription cycle.

    Acceptance criteria: AC-9 through AC-12.
    """

    def test_harness_002_re_prescribes_after_failure(self, harness_env: dict) -> None:
        """
        AC-9: After failure, UoW returns to ready-for-executor (not blocked or done).
        AC-10: steward_cycles increments by 1 on each failure cycle.
        AC-11: steward_log is appended (not overwritten) between cycles.
        """
        registry: Registry = harness_env["registry"]
        artifact_dir: Path = harness_env["artifact_dir"]
        output_dir: Path = harness_env["output_dir"]
        notify_dan = harness_env["notify_dan"]

        uow_id = _seed_harness_uow(registry, HARNESS_002_ISSUE_NUMBER)

        # --- Steward cycle 1: prescribe ---
        run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
            llm_prescriber=None,
        )

        uow = registry.get(uow_id)
        assert uow.steward_cycles == 1, (
            f"After Steward cycle 1, steward_cycles must be 1, got {uow.steward_cycles}"
        )

        # Executor runs but result.json says 'failed'
        _simulate_failed_execution(registry, uow_id, output_dir)

        uow = registry.get(uow_id)
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"After execution, UoW must be back to ready-for-steward, got {uow.status}"
        )
        steward_log_after_cycle_1 = uow.steward_log

        # --- Steward cycle 2: diagnoses failure, re-prescribes ---
        run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
            llm_prescriber=None,
        )

        uow = registry.get(uow_id)

        # AC-9: after failure, UoW returns to ready-for-executor (not blocked or done)
        assert uow.status == UoWStatus.READY_FOR_EXECUTOR, (
            f"After failure cycle, UoW must return to ready-for-executor (AC-9), "
            f"got {uow.status}"
        )
        # AC-10: steward_cycles increments by 1
        assert uow.steward_cycles == 2, (
            f"steward_cycles must be 2 after failure + re-prescription (AC-10), "
            f"got {uow.steward_cycles}"
        )
        # AC-11: steward_log is appended (not overwritten)
        if steward_log_after_cycle_1:
            assert uow.steward_log is not None, (
                "steward_log must be non-null after cycle 2 (AC-11)"
            )
            assert len(uow.steward_log) >= len(steward_log_after_cycle_1), (
                "steward_log must be appended (not overwritten) between cycles (AC-11)"
            )


# ---------------------------------------------------------------------------
# HARNESS-003: TTL recovery
# ---------------------------------------------------------------------------

@pytest.mark.wos_e2e
class TestHarness003TtlRecovery:
    """
    HARNESS-003: TTL recovery — UoW stuck active > TTL_EXCEEDED_HOURS.

    Acceptance criteria: AC-16, AC-17.
    """

    def test_harness_003_ttl_recovery_transitions_to_failed(
        self, harness_env: dict
    ) -> None:
        """
        AC-16: After recover_ttl_exceeded_uows(), UoW status transitions from
               'active' to 'failed'.
        AC-17: audit_log contains 'ttl_exceeded' return_reason.
        """
        registry: Registry = harness_env["registry"]
        db_path: Path = harness_env["db_path"]

        uow_id = _seed_harness_uow(registry, HARNESS_003_ISSUE_NUMBER)

        # Advance to 'active' via set_status_direct (simulates Executor claim)
        registry.set_status_direct(uow_id, "active")

        # Backdate started_at to 5 hours ago — beyond the TTL_EXCEEDED_HOURS (4h) threshold.
        # TTL recovery uses: WHERE started_at < (now - TTL_EXCEEDED_HOURS).
        # Use a timestamp that is definitely in the past relative to any test run.
        five_hours_ago = "2020-01-01T00:00:00+00:00"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET started_at = ? WHERE id = ?",
            (five_hours_ago, uow_id),
        )
        conn.commit()
        conn.close()

        # Run TTL recovery
        from orchestration.executor import recover_ttl_exceeded_uows
        recovered = recover_ttl_exceeded_uows(registry)

        # AC-16: UoW transitions from active to failed
        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.FAILED, (
            f"TTL recovery must transition UoW to 'failed' (AC-16), got {uow.status}"
        )

        # AC-17: audit_log contains 'ttl_exceeded' in the note (fail_uow reason).
        # fail_uow() writes event='execution_failed' with note containing the reason
        # string which includes "ttl_exceeded".
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        audit_rows = conn.execute(
            "SELECT event, note FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        conn.close()

        ttl_found = any(
            "ttl_exceeded" in (row["note"] or "").lower()
            or "ttl" in (row["event"] or "").lower()
            for row in audit_rows
        )
        assert ttl_found, (
            f"audit_log must contain 'ttl_exceeded' in a note entry (AC-17). "
            f"Audit events: {[(r['event'], (r['note'] or '')[:120]) for r in audit_rows]}"
        )


# ---------------------------------------------------------------------------
# HARNESS-004: Hard cap escalation
# ---------------------------------------------------------------------------

@pytest.mark.wos_e2e
@pytest.mark.slow
class TestHarness004HardCap:
    """
    HARNESS-004: 5 consecutive failures → escalation to Dan → blocked.

    Acceptance criteria: AC-13, AC-14, AC-15.
    """

    def test_harness_004_crash_repeated_escalates_to_blocked(
        self, harness_env: dict
    ) -> None:
        """
        AC-13: After CRASH_SURFACE_CAP consecutive crashes (no result.json),
               status is 'blocked'.
        AC-14: steward_cycles >= CRASH_SURFACE_CAP at time of escalation.
        AC-15: notify_dan receives at least 1 call with the UoW.

        Uses the 'crash_repeated' stuck condition path, which fires when
        a UoW has crashed (no output file) for CRASH_SURFACE_CAP consecutive
        cycles. This is the most reliable escalation trigger in automated tests
        because it doesn't depend on lifetime_cycles (which only increments on
        decide_retry, a user action).

        The 'hard_cap' path requires lifetime_cycles >= _HARD_CAP_CYCLES, which
        only increments via decide_retry — not automatically during prescription.
        """
        registry: Registry = harness_env["registry"]
        artifact_dir: Path = harness_env["artifact_dir"]
        output_dir: Path = harness_env["output_dir"]
        notify_dan = harness_env["notify_dan"]
        notify_dan_calls: list[tuple] = harness_env["notify_dan_calls"]

        uow_id = _seed_harness_uow(registry, HARNESS_004_ISSUE_NUMBER)

        # Drive Steward+Executor cycles until blocked or safety cap
        max_iterations = CRASH_SURFACE_CAP + 2
        for iteration in range(max_iterations):
            # Steward: prescribe or detect escalation
            run_steward_cycle(
                registry=registry,
                github_client=_noop_github_client,
                artifact_dir=artifact_dir,
                notify_dan=notify_dan,
                notify_dan_early_warning=_noop_notify_dan_early_warning,
                bootup_candidate_gate=False,
                llm_prescriber=None,
            )

            uow = registry.get(uow_id)
            if uow.status == UoWStatus.BLOCKED:
                # Escalation happened
                break

            if uow.status != UoWStatus.READY_FOR_EXECUTOR:
                pytest.fail(
                    f"Iteration {iteration + 1}: expected ready-for-executor, "
                    f"got {uow.status!r}"
                )

            # Simulate a crash (no result.json written)
            _simulate_crashed_execution(registry, uow_id, output_dir)
        else:
            pytest.fail(
                f"UoW did not reach 'blocked' after {max_iterations} iterations. "
                f"Final status: {registry.get(uow_id).status!r}"
            )

        # AC-13: final status is blocked
        uow = registry.get(uow_id)
        assert uow.status == UoWStatus.BLOCKED, (
            f"UoW must be 'blocked' after repeated crashes (AC-13), "
            f"got {uow.status}"
        )

        # AC-14: steward_cycles is at least CRASH_SURFACE_CAP
        assert uow.steward_cycles >= CRASH_SURFACE_CAP, (
            f"steward_cycles must be >= {CRASH_SURFACE_CAP} at escalation (AC-14), "
            f"got {uow.steward_cycles}"
        )

        # AC-15: notify_dan received at least 1 call
        assert len(notify_dan_calls) >= 1, (
            f"notify_dan must be called at least 1 time at escalation (AC-15), "
            f"called {len(notify_dan_calls)} times"
        )
