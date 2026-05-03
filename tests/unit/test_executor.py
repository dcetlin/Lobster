"""
Tests for the WOS Executor (orchestration.executor).

Coverage:
- Successful execution writes complete result.json
- Failed execution writes failed result.json
- Blocked execution writes blocked result.json
- Exception during execution still writes failed result.json (no orphaned UoW)
- uow_id in result.json matches the claimed UoW
- Optimistic lock rejects if UoW not in ready-for-executor
- Prescribed skills are activated before dispatch
- prescribed_skills=None and prescribed_skills=[] — activate_skill not called
- Execution failure does NOT set status to 'done' or 'ready-for-steward'
- Claim atomicity: status 'active', started_at written before execution starts
- output_ref is absolute path written at claim time
- audit_log contains 'claimed' entry with started_at, output_ref, timeout_at
- On success: 'execution_complete' in audit_log, status='ready-for-steward'
- On failure: 'execution_failed' in audit_log, status='failed'
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import concurrent.futures
from pathlib import Path

import pytest

from orchestration.registry import Registry, UoWStatus
from orchestration.workflow_artifact import WorkflowArtifact, to_json
from orchestration.executor import (
    Executor,
    ExecutorOutcome,
    ExecutorResult,
    _result_json_path,
    _dispatch_via_inbox,
    _dispatch_via_claude_p,
    _noop_dispatcher,
)


# ---------------------------------------------------------------------------
# Fixtures
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
    """Return JSON-encoded WorkflowArtifact."""
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
    estimated_runtime: int | None = None,
) -> None:
    """Directly insert a UoW into the registry for test setup."""
    import sqlite3
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
                summary, success_criteria, workflow_artifact, estimated_runtime
            ) VALUES (?, 'executable', 'test', ?, 'solo', ?, ?, 'Test UoW', 'test done', ?, ?)
            """,
            (uow_id, status, now, now, workflow_artifact, estimated_runtime),
        )
        conn.commit()
    finally:
        conn.close()


def _get_uow_status(db_path: Path, uow_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status FROM uow_registry WHERE id = ?", (uow_id,)).fetchone()
        return row["status"] if row else ""
    finally:
        conn.close()


def _get_audit_events(db_path: Path, uow_id: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id", (uow_id,)
        ).fetchall()
        return [r["event"] for r in rows]
    finally:
        conn.close()


def _get_uow_field(db_path: Path, uow_id: str, field: str) -> object:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(f"SELECT {field} FROM uow_registry WHERE id = ?", (uow_id,)).fetchone()
        return row[field] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_result_json(output_ref: str) -> dict:
    result_path = _result_json_path(output_ref)
    return json.loads(result_path.read_text())


def _get_output_ref(db_path: Path, uow_id: str) -> str:
    return str(_get_uow_field(db_path, uow_id, "output_ref"))


# ---------------------------------------------------------------------------
# Tests: successful execution
# ---------------------------------------------------------------------------

class TestSuccessfulExecution:
    def test_writes_complete_result_json(self, registry: Registry, db_path: Path, tmp_path: Path) -> None:
        """Successful execution writes result.json with outcome=complete, success=true."""
        uow_id = "uow_test_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        dispatched: list[str] = []
        def fake_dispatcher(instructions: str, uid: str) -> str:
            dispatched.append(uid)
            return "task-abc"

        executor = Executor(registry, dispatcher=fake_dispatcher)
        result = executor.execute_uow(uow_id)

        assert result.outcome == ExecutorOutcome.COMPLETE
        assert result.success is True
        assert result.uow_id == uow_id

        output_ref = _get_output_ref(db_path, uow_id)
        result_data = _read_result_json(output_ref)

        assert result_data["uow_id"] == uow_id
        assert result_data["outcome"] == "complete"
        assert result_data["success"] is True

    def test_uow_id_in_result_matches_claimed_uow(self, registry: Registry, db_path: Path) -> None:
        """uow_id in result.json must match the UoW that was claimed."""
        uow_id = "uow_test_002"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        result = executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result_data = _read_result_json(output_ref)

        assert result_data["uow_id"] == uow_id
        assert result.uow_id == uow_id

    def test_status_transitions_to_ready_for_steward(self, registry: Registry, db_path: Path) -> None:
        """On success, status must be 'ready-for-steward' — never 'done'."""
        uow_id = "uow_test_003"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        assert _get_uow_status(db_path, uow_id) == "ready-for-steward"

    def test_executor_never_transitions_to_done(self, registry: Registry, db_path: Path) -> None:
        """Executor MUST NOT transition to 'done' under any success path."""
        uow_id = "uow_test_004"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        status = _get_uow_status(db_path, uow_id)
        assert status != "done", "Executor must never set status to 'done' — that is the Steward's role"

    def test_execution_complete_in_audit_log(self, registry: Registry, db_path: Path) -> None:
        """'execution_complete' must appear in audit_log before ready-for-steward transition."""
        uow_id = "uow_test_005"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        events = _get_audit_events(db_path, uow_id)
        assert "execution_complete" in events

    def test_output_ref_is_absolute_path(self, registry: Registry, db_path: Path) -> None:
        """output_ref stored in registry must be an absolute path."""
        uow_id = "uow_test_006"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert Path(output_ref).is_absolute(), f"output_ref must be absolute: {output_ref!r}"

    def test_started_at_written_at_claim_time(self, registry: Registry, db_path: Path) -> None:
        """started_at must be non-NULL after a successful claim."""
        uow_id = "uow_test_007"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        started_at = _get_uow_field(db_path, uow_id, "started_at")
        assert started_at is not None, "started_at must be set at claim time"

    def test_timeout_at_written_at_claim_time(self, registry: Registry, db_path: Path) -> None:
        """timeout_at must be non-NULL after a successful claim."""
        uow_id = "uow_test_008"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id), estimated_runtime=600)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        timeout_at = _get_uow_field(db_path, uow_id, "timeout_at")
        assert timeout_at is not None, "timeout_at must be written at claim time"

    def test_audit_log_has_claimed_event(self, registry: Registry, db_path: Path) -> None:
        """'claimed' must appear in audit_log as part of the 6-step sequence."""
        uow_id = "uow_test_009"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        events = _get_audit_events(db_path, uow_id)
        assert "claimed" in events


# ---------------------------------------------------------------------------
# Tests: failed execution
# ---------------------------------------------------------------------------

class TestFailedExecution:
    def test_writes_failed_result_json(self, registry: Registry, db_path: Path) -> None:
        """When dispatcher raises, result.json must have outcome=failed, success=false."""
        uow_id = "uow_fail_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise RuntimeError("dispatch failed: subagent crashed")

        executor = Executor(registry, dispatcher=failing_dispatcher)

        with pytest.raises(RuntimeError, match="dispatch failed"):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result_data = _read_result_json(output_ref)

        assert result_data["outcome"] == "failed"
        assert result_data["success"] is False
        assert result_data["status"] == "failed"
        assert "summary" in result_data

    def test_exception_writes_result_json_before_reraise(self, registry: Registry, db_path: Path) -> None:
        """Exception during execution must still write result.json (no orphaned UoW)."""
        uow_id = "uow_fail_002"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def crashing_dispatcher(instructions: str, uid: str) -> str:
            raise ValueError("unexpected crash")

        executor = Executor(registry, dispatcher=crashing_dispatcher)

        with pytest.raises(ValueError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result_path = _result_json_path(output_ref)
        assert result_path.exists(), "result.json must exist even after exception"

        result_data = json.loads(result_path.read_text())
        assert result_data["outcome"] == "failed"

    def test_failure_sets_status_to_failed(self, registry: Registry, db_path: Path) -> None:
        """On exception, status must be 'failed' — not 'done' or 'ready-for-steward'."""
        uow_id = "uow_fail_003"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise RuntimeError("crash")

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(RuntimeError):
            executor.execute_uow(uow_id)

        status = _get_uow_status(db_path, uow_id)
        assert status == "failed"

    def test_failure_does_not_set_done_or_ready_for_steward(self, registry: Registry, db_path: Path) -> None:
        """Failure path must NOT set status to 'done' or 'ready-for-steward'."""
        uow_id = "uow_fail_004"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise RuntimeError("crash")

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(RuntimeError):
            executor.execute_uow(uow_id)

        status = _get_uow_status(db_path, uow_id)
        assert status not in {"done", "ready-for-steward"}

    def test_execution_failed_in_audit_log(self, registry: Registry, db_path: Path) -> None:
        """'execution_failed' must appear in audit_log on failure."""
        uow_id = "uow_fail_005"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise RuntimeError("crash")

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(RuntimeError):
            executor.execute_uow(uow_id)

        events = _get_audit_events(db_path, uow_id)
        assert "execution_failed" in events


# ---------------------------------------------------------------------------
# Tests: blocked execution
# ---------------------------------------------------------------------------

class TestBlockedExecution:
    def test_writes_blocked_result_json(self, registry: Registry, db_path: Path) -> None:
        """report_blocked() must write result.json with outcome=blocked, success=false."""
        uow_id = "uow_blocked_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        # Claim the UoW first, then report blocked
        executor = Executor(registry, dispatcher=_noop_dispatcher)
        claim = executor._claim(uow_id)

        assert hasattr(claim, "output_ref"), "Claim should succeed"
        output_ref = claim.output_ref  # type: ignore[union-attr]

        result = executor.report_blocked(uow_id, output_ref, reason="awaiting GitHub approval")

        assert result.outcome == ExecutorOutcome.BLOCKED
        assert result.success is False
        assert result.uow_id == uow_id
        assert result.reason == "awaiting GitHub approval"

        result_data = _read_result_json(output_ref)
        assert result_data["outcome"] == "blocked"
        assert result_data["success"] is False
        assert result_data["uow_id"] == uow_id

    def test_blocked_transitions_to_ready_for_steward(self, registry: Registry, db_path: Path) -> None:
        """Blocked UoW should return to ready-for-steward so Steward can surface to Dan."""
        uow_id = "uow_blocked_002"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        claim = executor._claim(uow_id)
        output_ref = claim.output_ref  # type: ignore[union-attr]
        executor.report_blocked(uow_id, output_ref, reason="needs Dan's /decide")

        # Blocked surfaces to Steward (which then moves to 'blocked' status)
        status = _get_uow_status(db_path, uow_id)
        assert status == "ready-for-steward"


# ---------------------------------------------------------------------------
# Tests: optimistic lock
# ---------------------------------------------------------------------------

class TestOptimisticLock:
    def test_rejects_if_not_in_ready_for_executor(self, registry: Registry, db_path: Path) -> None:
        """Claim must be rejected if UoW status is not 'ready-for-executor'.

        With the status filter active on executor_uow_view, a UoW in 'active'
        status is invisible to the view. Step 1 returns None → ClaimNotFound →
        ValueError, not RuntimeError from the optimistic lock.
        """
        uow_id = "uow_lock_001"
        _insert_uow(db_path, uow_id, status="active", workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        with pytest.raises(ValueError, match="not found in registry"):
            executor.execute_uow(uow_id)

    def test_rejects_if_status_is_pending(self, registry: Registry, db_path: Path) -> None:
        """Claim must be rejected for 'pending' status.

        With the status filter active on executor_uow_view, a UoW in 'pending'
        status is invisible to the view. Step 1 returns None → ClaimNotFound →
        ValueError, not RuntimeError from the optimistic lock.
        """
        uow_id = "uow_lock_002"
        _insert_uow(db_path, uow_id, status="pending", workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        with pytest.raises(ValueError, match="not found in registry"):
            executor.execute_uow(uow_id)

    def test_raises_value_error_for_unknown_uow(self, registry: Registry, db_path: Path) -> None:
        """Running an unknown UoW ID must raise ValueError."""
        executor = Executor(registry, dispatcher=_noop_dispatcher)
        with pytest.raises(ValueError, match="not found in registry"):
            executor.execute_uow("nonexistent-uow-id")

    def test_concurrent_claim_silently_aborts_second(self, registry: Registry, db_path: Path) -> None:
        """Simulate two executors claiming the same UoW: second must be rejected."""
        uow_id = "uow_lock_003"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        def _noop_dispatcher(instructions: str, uid: str) -> str:
            return "task-noop"

        # First executor claims successfully
        executor1 = Executor(registry, dispatcher=_noop_dispatcher)
        executor1.execute_uow(uow_id)  # transitions to ready-for-steward after success

        # Now status is 'ready-for-steward', not 'ready-for-executor'.
        # With the view filter active, 'ready-for-steward' UoWs are invisible →
        # Step 1 returns None → ClaimNotFound → ValueError (not RuntimeError).
        executor2 = Executor(registry, dispatcher=_noop_dispatcher)
        with pytest.raises(ValueError, match="not found in registry"):
            executor2.execute_uow(uow_id)

    def test_truly_concurrent_claim_exactly_one_succeeds(
        self, registry: Registry, db_path: Path
    ) -> None:
        """
        Two Executor instances racing to claim the same UoW via real threads must
        result in exactly one success (rowcount=1) and one race-skip (RuntimeError).

        This tests the optimistic lock under genuine concurrent access — not just
        sequential calls where order is deterministic.
        """
        uow_id = "uow_lock_004"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        results: list[str] = []  # "success" or exception class name
        errors: list[Exception] = []
        barrier = threading.Barrier(2)  # synchronize both threads to maximize contention

        def try_run(executor: Executor) -> None:
            barrier.wait()  # both threads reach this point before either proceeds
            try:
                executor.execute_uow(uow_id)
                results.append("success")
            except RuntimeError as exc:
                results.append("RuntimeError")
                errors.append(exc)
            except Exception as exc:
                results.append(type(exc).__name__)
                errors.append(exc)

        def _noop_dispatcher(instructions: str, uid: str) -> str:
            return "task-noop"

        executor_a = Executor(registry, dispatcher=_noop_dispatcher)
        executor_b = Executor(registry, dispatcher=_noop_dispatcher)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(try_run, executor_a)
            fut_b = pool.submit(try_run, executor_b)
            fut_a.result(timeout=10)
            fut_b.result(timeout=10)

        # Exactly one must succeed and one must get the optimistic lock rejection
        assert sorted(results) == ["RuntimeError", "success"], (
            f"Expected exactly one success and one RuntimeError, got: {results}"
        )
        # The error must be the optimistic lock rejection (not some other failure)
        assert len(errors) == 1
        assert "optimistic lock failed" in str(errors[0]), (
            f"Expected optimistic lock rejection, got: {errors[0]}"
        )


# ---------------------------------------------------------------------------
# Tests: skill activation
# ---------------------------------------------------------------------------

class TestSkillActivation:
    def test_prescribed_skills_activated_before_dispatch(self, registry: Registry, db_path: Path) -> None:
        """Each skill_id in prescribed_skills must be passed to activate_skill before dispatch."""
        uow_id = "uow_skill_001"
        artifact_json = _make_artifact(uow_id, prescribed_skills=["systematic-debugging"])
        _insert_uow(db_path, uow_id, workflow_artifact=artifact_json)

        activated_skills: list[str] = []
        dispatch_order: list[str] = []

        def tracking_activator(skill_id: str) -> None:
            dispatch_order.append(f"activate:{skill_id}")
            activated_skills.append(skill_id)

        def tracking_dispatcher(instructions: str, uid: str) -> str:
            dispatch_order.append("dispatch")
            return "task-xyz"

        executor = Executor(registry, skill_activator=tracking_activator, dispatcher=tracking_dispatcher)
        executor.execute_uow(uow_id)

        assert "systematic-debugging" in activated_skills
        # Skills must be activated before dispatch
        activate_idx = dispatch_order.index("activate:systematic-debugging")
        dispatch_idx = dispatch_order.index("dispatch")
        assert activate_idx < dispatch_idx, "Skills must be activated before dispatch"

    def test_prescribed_skills_none_does_not_call_activate(self, registry: Registry, db_path: Path) -> None:
        """prescribed_skills=None must not call activate_skill."""
        uow_id = "uow_skill_002"
        # Manually construct artifact with prescribed_skills=None stored as null
        artifact: WorkflowArtifact = {
            "uow_id": uow_id,
            "executor_type": "general",
            "constraints": [],
            "prescribed_skills": [],  # Empty list — no skills
            "instructions": "Do stuff",
        }
        import json as _json
        artifact_with_null = _json.dumps({**artifact, "prescribed_skills": None})
        _insert_uow(db_path, uow_id, workflow_artifact=artifact_with_null)

        activated_skills: list[str] = []

        def tracking_activator(skill_id: str) -> None:
            activated_skills.append(skill_id)

        executor = Executor(registry, skill_activator=tracking_activator, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        assert activated_skills == [], "activate_skill must not be called when prescribed_skills is None"

    def test_prescribed_skills_empty_list_does_not_call_activate(self, registry: Registry, db_path: Path) -> None:
        """prescribed_skills=[] must not call activate_skill."""
        uow_id = "uow_skill_003"
        artifact_json = _make_artifact(uow_id, prescribed_skills=[])
        _insert_uow(db_path, uow_id, workflow_artifact=artifact_json)

        activated_skills: list[str] = []

        def tracking_activator(skill_id: str) -> None:
            activated_skills.append(skill_id)

        executor = Executor(registry, skill_activator=tracking_activator, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        assert activated_skills == [], "activate_skill must not be called for empty prescribed_skills"

    def test_multiple_skills_all_activated(self, registry: Registry, db_path: Path) -> None:
        """All skills in prescribed_skills must be activated."""
        uow_id = "uow_skill_004"
        skills = ["skill-a", "skill-b", "skill-c"]
        artifact_json = _make_artifact(uow_id, prescribed_skills=skills)
        _insert_uow(db_path, uow_id, workflow_artifact=artifact_json)

        activated_skills: list[str] = []

        def tracking_activator(skill_id: str) -> None:
            activated_skills.append(skill_id)

        executor = Executor(registry, skill_activator=tracking_activator, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        assert set(activated_skills) == set(skills)


# ---------------------------------------------------------------------------
# Tests: crash recovery properties
# ---------------------------------------------------------------------------

class TestCrashRecoveryProperties:
    def test_crash_at_step2_leaves_status_active_started_at_null(
        self, registry: Registry, db_path: Path
    ) -> None:
        """
        After claim succeeds (step 2 UPDATE), started_at and output_ref are
        written in steps 3-4. Simulating a crash after step 2 is hard in unit
        tests without transaction interception; instead we verify the claim
        transaction writes started_at and output_ref atomically.

        Per issue #305: crash between steps 2-5 (status 'active', started_at NULL)
        is classified by the Observation Loop (#306) as immediate stall.

        This test verifies that started_at is non-NULL after a successful claim
        (i.e., it is written in the same transaction as status='active').
        """
        uow_id = "uow_crash_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        started_at = _get_uow_field(db_path, uow_id, "started_at")
        assert started_at is not None, "started_at must be written atomically with claim"

    def test_no_missing_workflow_artifact_results_in_graceful_failure(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Non-existent workflow_artifact is handled gracefully with audit_log entry."""
        uow_id = "uow_crash_002"
        # NULL workflow_artifact — Executor cannot proceed
        _insert_uow(db_path, uow_id, workflow_artifact=None)

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        with pytest.raises(RuntimeError, match="claim rejected"):
            executor.execute_uow(uow_id)

        # Status should be 'failed' after graceful handling
        status = _get_uow_status(db_path, uow_id)
        assert status == "failed"

    def test_result_json_written_when_parent_dir_missing(
        self, registry: Registry, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        result.json must be written successfully even when the parent directory
        does not yet exist. The executor must create it (mkdir parents=True).
        """
        uow_id = "uow_crash_003"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        # Point the output dir at a deep path that doesn't exist yet
        nonexistent_dir = tmp_path / "deep" / "nested" / "dir"
        assert not nonexistent_dir.exists(), "precondition: dir must not exist before run"

        # Monkeypatch the output dir template so output_ref lands in nonexistent_dir
        import orchestration.executor as _executor_mod
        original_template = _executor_mod._OUTPUT_DIR_TEMPLATE
        monkeypatch.setattr(_executor_mod, "_OUTPUT_DIR_TEMPLATE", str(nonexistent_dir))

        try:
            executor = Executor(registry, dispatcher=_noop_dispatcher)
            result = executor.execute_uow(uow_id)
        finally:
            monkeypatch.setattr(_executor_mod, "_OUTPUT_DIR_TEMPLATE", original_template)

        assert result.outcome == ExecutorOutcome.COMPLETE
        output_ref = _get_output_ref(db_path, uow_id)
        result_path = _result_json_path(output_ref)
        assert result_path.exists(), (
            f"result.json must exist even when parent dir was missing: {result_path}"
        )
        result_data = json.loads(result_path.read_text())
        assert result_data["outcome"] == "complete"
        assert result_data["uow_id"] == uow_id

    def test_result_json_path_primary_convention(self) -> None:
        """Primary convention: foo.json → foo.result.json."""
        output_ref = "/tmp/uow_abc.json"
        result_path = _result_json_path(output_ref)
        assert str(result_path) == "/tmp/uow_abc.result.json"

    def test_result_json_path_fallback_convention(self) -> None:
        """Fallback convention: /path/to/artifact → /path/to/artifact.result.json."""
        output_ref = "/tmp/uow_abc"
        result_path = _result_json_path(output_ref)
        assert str(result_path) == "/tmp/uow_abc.result.json"


# ---------------------------------------------------------------------------
# Tests: ExecutorResult dataclass
# ---------------------------------------------------------------------------

class TestExecutorResultDataclass:
    def test_complete_result_serializes_correctly(self) -> None:
        result = ExecutorResult(
            uow_id="abc-123",
            outcome=ExecutorOutcome.COMPLETE,
            success=True,
        )
        d = result.to_dict()
        assert d == {"uow_id": "abc-123", "outcome": "complete", "success": True}

    def test_failed_result_includes_reason(self) -> None:
        result = ExecutorResult(
            uow_id="abc-123",
            outcome=ExecutorOutcome.FAILED,
            success=False,
            reason="FileNotFoundError: /tmp/foo.json",
        )
        d = result.to_dict()
        assert d["outcome"] == "failed"
        assert d["success"] is False
        assert d["reason"] == "FileNotFoundError: /tmp/foo.json"

    def test_partial_result_includes_step_counts(self) -> None:
        result = ExecutorResult(
            uow_id="abc-123",
            outcome=ExecutorOutcome.PARTIAL,
            success=False,
            reason="decomposition requires architectural decision",
            steps_completed=3,
            steps_total=5,
        )
        d = result.to_dict()
        assert d["outcome"] == "partial"
        assert d["steps_completed"] == 3
        assert d["steps_total"] == 5

    def test_outcome_is_success_only_for_complete(self) -> None:
        assert ExecutorOutcome.COMPLETE.is_success() is True
        assert ExecutorOutcome.PARTIAL.is_success() is False
        assert ExecutorOutcome.FAILED.is_success() is False
        assert ExecutorOutcome.BLOCKED.is_success() is False

    def test_outcome_is_terminal_for_failed_and_blocked(self) -> None:
        assert ExecutorOutcome.FAILED.is_terminal() is True
        assert ExecutorOutcome.BLOCKED.is_terminal() is True
        assert ExecutorOutcome.COMPLETE.is_terminal() is False
        assert ExecutorOutcome.PARTIAL.is_terminal() is False

    def test_none_optional_fields_omitted_from_dict(self) -> None:
        result = ExecutorResult(
            uow_id="abc-123",
            outcome=ExecutorOutcome.COMPLETE,
            success=True,
        )
        d = result.to_dict()
        assert "reason" not in d
        assert "steps_completed" not in d
        assert "steps_total" not in d
        assert "output_artifact" not in d
        assert "executor_id" not in d


# ---------------------------------------------------------------------------
# Tests: _dispatch_via_inbox (production dispatcher)
# ---------------------------------------------------------------------------

class TestDispatchViaInbox:
    """Tests for the production inbox-based dispatch function."""

    def test_writes_json_file_to_inbox_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_dispatch_via_inbox must write a JSON file to the inbox directory."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        msg_id = _dispatch_via_inbox("Do the task", "uow-abc-123")

        inbox_dir = tmp_path / "inbox"
        assert inbox_dir.exists(), "inbox dir must be created"
        written_file = inbox_dir / f"{msg_id}.json"
        assert written_file.exists(), f"Expected {written_file} to be written"

    def test_returns_message_id_as_executor_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Return value must be a non-empty string (the message_id for audit correlation)."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        msg_id = _dispatch_via_inbox("instructions", "uow-xyz")

        assert isinstance(msg_id, str)
        assert len(msg_id) > 0

    def test_message_has_wos_execute_type(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The inbox message must have type='wos_execute' so the dispatcher routes it."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        msg_id = _dispatch_via_inbox("Do the task", "uow-type-test")

        msg_file = tmp_path / "inbox" / f"{msg_id}.json"
        msg = json.loads(msg_file.read_text(encoding="utf-8"))
        assert msg["type"] == "wos_execute"

    def test_message_contains_uow_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The inbox message must carry the uow_id for the dispatcher to correlate."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        uow_id = "uow-correlation-test"
        msg_id = _dispatch_via_inbox("instructions text", uow_id)

        msg_file = tmp_path / "inbox" / f"{msg_id}.json"
        msg = json.loads(msg_file.read_text(encoding="utf-8"))
        assert msg["uow_id"] == uow_id

    def test_message_contains_instructions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The inbox message must carry the instructions string for the subagent."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        instructions = "Fix the bug in foo.py and write a test"
        msg_id = _dispatch_via_inbox(instructions, "uow-instr-test")

        msg_file = tmp_path / "inbox" / f"{msg_id}.json"
        msg = json.loads(msg_file.read_text(encoding="utf-8"))
        assert msg["instructions"] == instructions

    def test_message_id_field_matches_filename(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """msg['id'] must match the filename base and the return value."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        msg_id = _dispatch_via_inbox("instructions", "uow-id-match")

        msg_file = tmp_path / "inbox" / f"{msg_id}.json"
        msg = json.loads(msg_file.read_text(encoding="utf-8"))
        assert msg["id"] == msg_id

    def test_message_has_source_system(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Message source must be 'system' (not user-sourced)."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        msg_id = _dispatch_via_inbox("instructions", "uow-source-test")

        msg_file = tmp_path / "inbox" / f"{msg_id}.json"
        msg = json.loads(msg_file.read_text(encoding="utf-8"))
        assert msg["source"] == "system"

    def test_message_has_timestamp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Message must include a timestamp for ordering and audit."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        msg_id = _dispatch_via_inbox("instructions", "uow-ts-test")

        msg_file = tmp_path / "inbox" / f"{msg_id}.json"
        msg = json.loads(msg_file.read_text(encoding="utf-8"))
        assert "timestamp" in msg
        # Must be parseable as ISO-8601
        from datetime import datetime
        datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))

    def test_two_dispatches_produce_unique_message_ids(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each dispatch call must produce a unique message_id (no collision)."""
        import orchestration.executor as _executor_mod
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(tmp_path / "inbox"))

        id1 = _dispatch_via_inbox("instructions A", "uow-unique-1")
        id2 = _dispatch_via_inbox("instructions B", "uow-unique-2")

        assert id1 != id2

    def test_inbox_dir_created_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Inbox directory must be created automatically if it does not exist."""
        import orchestration.executor as _executor_mod
        deep_inbox = tmp_path / "deep" / "nested" / "inbox"
        assert not deep_inbox.exists(), "precondition: dir must not exist"
        monkeypatch.setattr(_executor_mod, "_INBOX_DIR_TEMPLATE", str(deep_inbox))

        _dispatch_via_inbox("instructions", "uow-mkdir-test")

        assert deep_inbox.exists(), "inbox dir must be created even when deeply nested"

    def test_executor_defaults_to_dispatch_table_when_no_dispatcher_injected(
        self, registry: Registry
    ) -> None:
        """
        When no dispatcher is injected, Executor._dispatcher_override must be None
        so that _run_execution uses the dispatch table (_resolve_dispatcher).
        Callers that need a specific dispatcher pass it explicitly via the constructor
        (e.g. tests inject a no-op); the injected dispatcher takes precedence over the table.
        """
        executor = Executor(registry)
        assert executor._dispatcher_override is None, (
            f"Default _dispatcher_override must be None (use dispatch table), "
            f"got {executor._dispatcher_override!r}"
        )

    def test_executor_id_in_result_is_dispatcher_return_value(
        self, registry: Registry, db_path: Path
    ) -> None:
        """
        The executor_id in ExecutorResult and result.json must be the value
        returned by the dispatcher (for audit trail correlation).
        """
        expected_run_id = "uow_exec_id_correlation_001-20240101T000000Z"

        def fake_dispatcher(instructions: str, uow_id: str) -> str:
            return expected_run_id

        uow_id = "uow_exec_id_correlation_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=fake_dispatcher)
        result = executor.execute_uow(uow_id)

        assert result.executor_id == expected_run_id, (
            f"executor_id {result.executor_id!r} must equal the dispatcher return value"
        )

        # Also verify it's in the result.json file
        output_ref = _get_output_ref(db_path, uow_id)
        result_data = _read_result_json(output_ref)
        assert result_data.get("executor_id") == expected_run_id


# ---------------------------------------------------------------------------
# Dispatch boundary log tests
# ---------------------------------------------------------------------------


class TestDispatchBoundaryLog:
    """Tests for _log_dispatch_boundary and the dispatch boundary contract.

    The dispatch boundary log records every inbox dispatch attempt with
    structured JSONL records.  The contract is:
      - outcome is always "success" or "failure" (never "retry")
      - dispatch_attempt is always 1 (no retry loop exists)
    """

    def test_success_record_contains_required_fields(self, tmp_path: Path) -> None:
        """A successful dispatch writes a record with all required fields."""
        from orchestration.executor import _log_dispatch_boundary
        import orchestration.executor as executor_mod

        log_file = tmp_path / "dispatch-boundary.jsonl"
        orig = executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE
        executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE = str(log_file)
        try:
            _log_dispatch_boundary(
                uow_id="uow_test_001",
                dispatch_attempt=1,
                outcome="success",
                msg_id="msg-abc",
            )
            records = [json.loads(line) for line in log_file.read_text().splitlines()]
            assert len(records) == 1
            rec = records[0]
            assert rec["uow_id"] == "uow_test_001"
            assert rec["dispatch_attempt"] == 1
            assert rec["outcome"] == "success"
            assert rec["msg_id"] == "msg-abc"
            assert "failure_reason" not in rec
            assert "ts" in rec
        finally:
            executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE = orig

    def test_failure_record_contains_failure_reason(self, tmp_path: Path) -> None:
        """A failed dispatch writes a record with failure_reason."""
        from orchestration.executor import _log_dispatch_boundary
        import orchestration.executor as executor_mod

        log_file = tmp_path / "dispatch-boundary.jsonl"
        orig = executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE
        executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE = str(log_file)
        try:
            _log_dispatch_boundary(
                uow_id="uow_test_002",
                dispatch_attempt=1,
                outcome="failure",
                failure_reason="inbox_write_failed: disk full",
            )
            records = [json.loads(line) for line in log_file.read_text().splitlines()]
            assert len(records) == 1
            rec = records[0]
            assert rec["outcome"] == "failure"
            assert rec["failure_reason"] == "inbox_write_failed: disk full"
            assert "msg_id" not in rec
        finally:
            executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE = orig

    def test_dispatch_via_inbox_always_logs_attempt_1(self, tmp_path: Path) -> None:
        """_dispatch_via_inbox always logs dispatch_attempt=1 (no retry loop)."""
        import orchestration.executor as executor_mod

        log_file = tmp_path / "dispatch-boundary.jsonl"
        inbox_dir = tmp_path / "inbox"

        orig_log = executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE
        orig_inbox = executor_mod._INBOX_DIR_TEMPLATE
        executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE = str(log_file)
        executor_mod._INBOX_DIR_TEMPLATE = str(inbox_dir)
        try:
            _dispatch_via_inbox("test instructions", "uow_attempt_check")
            records = [json.loads(line) for line in log_file.read_text().splitlines()]
            assert len(records) == 1
            assert records[0]["dispatch_attempt"] == 1
            assert records[0]["outcome"] == "success"
        finally:
            executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE = orig_log
            executor_mod._INBOX_DIR_TEMPLATE = orig_inbox

    def test_log_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        """Boundary log write failures are non-blocking (best-effort)."""
        import orchestration.executor as executor_mod
        from orchestration.executor import _log_dispatch_boundary

        # Point to a path that cannot be created (file where dir expected)
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        bad_path = str(blocker / "subdir" / "dispatch-boundary.jsonl")

        orig = executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE
        executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE = bad_path
        try:
            # Should not raise — failure is swallowed and logged via logger
            _log_dispatch_boundary(
                uow_id="uow_test_003",
                dispatch_attempt=1,
                outcome="success",
                msg_id="msg-xyz",
            )
        finally:
            executor_mod._DISPATCH_BOUNDARY_LOG_TEMPLATE = orig
