"""
Integration test: WOS happy-path arc — Tier 6 item #13.

Proves the full pipeline wiring from pending UoW to done, using:
- In-memory SQLite DB with all migrations applied via Registry.__init__
- Mocked LLM prescriber (no real GitHub or Claude calls)
- Mocked Executor dispatcher (no real subagent spawned)
- Subagent result.json written manually to simulate a completing subagent

Arc under test:
    pending
      → ready-for-steward    (direct set — simulates trigger evaluator)
      → ready-for-executor   (Steward cycle 1: prescribes work)
      → active               (Executor claims UoW)
      → ready-for-steward    (Executor completes, writes result.json)
      → done                 (Steward cycle 2: sees outcome=complete, declares done)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Repo path setup — make src/ importable without editable install
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.registry import Registry, UpsertInserted, ApproveConfirmed
from orchestration.executor import Executor, ExecutorOutcome
from orchestration.steward import run_steward_cycle, Prescribed, Done


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _noop_github_client(issue_number: int) -> dict[str, Any]:
    """Mocked GitHub client — returns a minimal open issue with no special labels."""
    return {
        "status_code": 200,
        "state": "open",
        "labels": [],
        "body": "Integration test issue body.",
        "title": "Integration test UoW",
    }


def _noop_notify_dan(uow, condition, surface_log=None, return_reason=None) -> None:
    """Mocked Dan notifier — captures surface calls without writing to inbox."""
    pass


def _noop_notify_dan_early_warning(uow, return_reason=None, new_cycles=None) -> None:
    """Mocked early-warning notifier."""
    pass


def _make_mock_dispatcher(result_outcome: str = "complete") -> tuple[Any, list[str]]:
    """
    Return a (dispatcher_fn, calls_log) pair.

    The dispatcher records calls in calls_log and returns a deterministic
    executor_id. It does NOT write result.json — the test does that explicitly
    to reflect how a real subagent operates (writes result.json before returning).
    """
    calls: list[str] = []

    def _dispatch(instructions: str, uow_id: str) -> str:
        calls.append(uow_id)
        return f"mock-executor-{uow_id[:8]}"

    return _dispatch, calls


# ---------------------------------------------------------------------------
# Fixture: isolated registry with temp dir for artifacts and outputs
# ---------------------------------------------------------------------------

@pytest.fixture
def arc_env(tmp_path: Path):
    """
    Provide an isolated environment for the happy-path arc test:
    - A fresh Registry backed by a temp-dir SQLite file (not in-memory, so
      multiple connections can share it, matching production behavior).
    - artifact_dir and output_dir under tmp_path so no filesystem pollution.
    """
    db_path = tmp_path / "registry.db"
    registry = Registry(db_path)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()

    return {
        "registry": registry,
        "db_path": db_path,
        "artifact_dir": artifact_dir,
        "output_dir": output_dir,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# Test: full happy-path arc
# ---------------------------------------------------------------------------

class TestWosSimpleArc:
    """Happy-path arc: pending → ready-for-steward → ready-for-executor
    → active → ready-for-steward → done."""

    def test_full_arc_reaches_done(self, arc_env: dict) -> None:
        """
        End-to-end arc: seed a UoW at pending, run Steward + Executor cycles,
        simulate subagent writing result.json, assert final status = done.
        """
        registry: Registry = arc_env["registry"]
        artifact_dir: Path = arc_env["artifact_dir"]
        tmp_path: Path = arc_env["tmp_path"]

        # ----------------------------------------------------------------
        # Step 1: Seed UoW at pending status.
        # Use upsert (proposed) + approve (pending) to follow the real path,
        # then set_status_direct to ready-for-steward (simulates trigger evaluator).
        # ----------------------------------------------------------------
        upsert_result = registry.upsert(
            issue_number=1001,
            title="Simple arc integration test UoW",
            success_criteria="result.json written with outcome=complete",
        )
        assert isinstance(upsert_result, UpsertInserted), (
            f"Expected UpsertInserted, got {upsert_result!r}"
        )
        uow_id = upsert_result.id

        approve_result = registry.approve(uow_id)
        assert isinstance(approve_result, ApproveConfirmed), (
            f"Expected ApproveConfirmed, got {approve_result!r}"
        )

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == "pending", f"Expected pending, got {uow.status}"

        # ----------------------------------------------------------------
        # Step 2: Advance pending → ready-for-steward.
        # In production the trigger evaluator does this; here we use
        # set_status_direct to keep the test self-contained.
        # ----------------------------------------------------------------
        registry.set_status_direct(uow_id, "ready-for-steward")
        uow = registry.get(uow_id)
        assert uow.status == "ready-for-steward", (
            f"Expected ready-for-steward, got {uow.status}"
        )

        # ----------------------------------------------------------------
        # Step 3: Run Steward cycle 1 — mocked GitHub client, no LLM call.
        # The Steward should prescribe work (no result.json exists yet →
        # output_ref invalid → not complete → prescribe).
        # ----------------------------------------------------------------
        steward_result_1 = run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=_noop_notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        assert steward_result_1.prescribed == 1, (
            f"Steward cycle 1 should have prescribed 1 UoW, got: {steward_result_1}"
        )
        assert steward_result_1.done == 0, (
            f"Steward cycle 1 should not have closed any UoW, got: {steward_result_1}"
        )

        uow = registry.get(uow_id)
        assert uow.status == "ready-for-executor", (
            f"Expected ready-for-executor after Steward cycle 1, got {uow.status}"
        )
        assert uow.workflow_artifact is not None, (
            "Steward should have written workflow_artifact path"
        )
        assert uow.steward_cycles == 1, (
            f"Expected steward_cycles=1, got {uow.steward_cycles}"
        )

        # ----------------------------------------------------------------
        # Step 4: Run Executor with mocked dispatcher.
        # Executor claims UoW (→ active), dispatches (mock), writes output_ref
        # content and result.json, then transitions → ready-for-steward.
        #
        # The Executor's built-in _run_execution writes result.json with
        # outcome=complete. We override output_ref to a path under tmp_path
        # so the Steward can find the file on the next cycle.
        # ----------------------------------------------------------------
        mock_dispatcher, dispatch_calls = _make_mock_dispatcher("complete")

        # Override the Executor's default output_ref path to use tmp_path.
        # The Executor computes output_ref as ~/lobster-workspace/orchestration/outputs/{uow_id}.json.
        # We patch the module-level constant so our temp dir is used instead.
        import orchestration.executor as executor_module
        original_template = executor_module._OUTPUT_DIR_TEMPLATE
        executor_module._OUTPUT_DIR_TEMPLATE = str(tmp_path / "outputs")
        (tmp_path / "outputs").mkdir(exist_ok=True)

        try:
            executor = Executor(
                registry=registry,
                dispatcher=mock_dispatcher,
            )
            exec_result = executor.execute_uow(uow_id)
        finally:
            executor_module._OUTPUT_DIR_TEMPLATE = original_template

        assert exec_result.outcome == ExecutorOutcome.COMPLETE, (
            f"Expected outcome=complete, got {exec_result.outcome}"
        )
        assert exec_result.uow_id == uow_id
        assert len(dispatch_calls) == 1, (
            f"Expected 1 dispatch call, got {dispatch_calls}"
        )

        uow = registry.get(uow_id)
        assert uow.status == "ready-for-steward", (
            f"After Executor.execute_uow, expected ready-for-steward, got {uow.status}"
        )
        assert uow.output_ref is not None, "output_ref should be set after Executor claim"

        # Verify result.json exists and contains outcome=complete
        output_ref = uow.output_ref
        result_json_path = Path(output_ref).with_suffix(".result.json")
        assert result_json_path.exists(), (
            f"result.json not found at {result_json_path}"
        )
        result_data = json.loads(result_json_path.read_text())
        assert result_data["outcome"] == "complete", (
            f"Expected outcome=complete in result.json, got {result_data}"
        )
        assert result_data["uow_id"] == uow_id, (
            f"result.json uow_id mismatch: expected {uow_id}, got {result_data.get('uow_id')}"
        )

        # ----------------------------------------------------------------
        # Step 5: Run Steward cycle 2.
        # The Steward re-reads the UoW (now ready-for-steward), finds
        # output_ref is valid and result.json has outcome=complete → declares done.
        # ----------------------------------------------------------------
        steward_result_2 = run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=_noop_notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        assert steward_result_2.done == 1, (
            f"Steward cycle 2 should have closed 1 UoW, got: {steward_result_2}"
        )
        assert steward_result_2.prescribed == 0, (
            f"Steward cycle 2 should not have prescribed anything, got: {steward_result_2}"
        )

        # ----------------------------------------------------------------
        # Step 6: Assert final status = done.
        # ----------------------------------------------------------------
        uow = registry.get(uow_id)
        assert uow.status == "done", (
            f"Final status should be done, got {uow.status}"
        )

    def test_arc_status_sequence_is_correct(self, arc_env: dict) -> None:
        """
        Assert that the UoW visits exactly the expected status sequence:
        pending → ready-for-steward → diagnosing → ready-for-executor
        → active → ready-for-steward → diagnosing → done

        Verified via audit_log event sequence, not just final status.
        """
        import sqlite3

        registry: Registry = arc_env["registry"]
        artifact_dir: Path = arc_env["artifact_dir"]
        tmp_path: Path = arc_env["tmp_path"]
        db_path: Path = arc_env["db_path"]

        # Seed
        upsert_result = registry.upsert(
            issue_number=1002,
            title="Arc status sequence test UoW",
            success_criteria="audit log tells the right story",
        )
        uow_id = upsert_result.id
        registry.approve(uow_id)
        registry.set_status_direct(uow_id, "ready-for-steward")

        # Steward cycle 1
        run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=_noop_notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        # Executor
        import orchestration.executor as executor_module
        original_template = executor_module._OUTPUT_DIR_TEMPLATE
        executor_module._OUTPUT_DIR_TEMPLATE = str(tmp_path / "outputs")
        (tmp_path / "outputs").mkdir(exist_ok=True)

        mock_dispatcher, _ = _make_mock_dispatcher("complete")
        try:
            executor = Executor(registry=registry, dispatcher=mock_dispatcher)
            executor.execute_uow(uow_id)
        finally:
            executor_module._OUTPUT_DIR_TEMPLATE = original_template

        # Steward cycle 2
        run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=_noop_notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        # Inspect audit log to verify the arc's key events are present
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        conn.close()

        events = [r["event"] for r in rows]

        # Must contain these events in order (subset check)
        required_events_in_order = [
            "created",           # upsert creates proposed record
            "status_change",     # approve: proposed → pending
            "status_change",     # set_status_direct: → ready-for-steward
            "steward_prescription",   # Steward cycle 1 prescribes
            "claimed",           # Executor claims → active
            "execution_complete", # Executor completes → ready-for-steward
            "steward_closure",   # Steward cycle 2 closes → done
        ]

        # Build an iterator check: each required event must appear in sequence
        events_iter = iter(events)
        for required in required_events_in_order:
            found = any(e == required for e in events_iter)
            assert found, (
                f"Event {required!r} not found in audit_log sequence. "
                f"Full sequence: {events}"
            )

    def test_done_is_terminal(self, arc_env: dict) -> None:
        """
        After reaching done, a second Steward cycle finds zero ready-for-steward
        UoWs and produces evaluated=0.
        """
        registry: Registry = arc_env["registry"]
        artifact_dir: Path = arc_env["artifact_dir"]
        tmp_path: Path = arc_env["tmp_path"]

        # Seed and run full arc
        upsert_result = registry.upsert(
            issue_number=1003,
            title="Terminal state test UoW",
            success_criteria="done is terminal",
        )
        uow_id = upsert_result.id
        registry.approve(uow_id)
        registry.set_status_direct(uow_id, "ready-for-steward")

        run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=_noop_notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        import orchestration.executor as executor_module
        original_template = executor_module._OUTPUT_DIR_TEMPLATE
        executor_module._OUTPUT_DIR_TEMPLATE = str(tmp_path / "outputs")
        (tmp_path / "outputs").mkdir(exist_ok=True)

        mock_dispatcher, _ = _make_mock_dispatcher("complete")
        try:
            executor = Executor(registry=registry, dispatcher=mock_dispatcher)
            executor.execute_uow(uow_id)
        finally:
            executor_module._OUTPUT_DIR_TEMPLATE = original_template

        # Steward cycle 2 — closes the UoW
        run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=_noop_notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )

        uow = registry.get(uow_id)
        assert uow.status == "done"

        # Steward cycle 3 — nothing to process
        steward_result_3 = run_steward_cycle(
            registry=registry,
            github_client=_noop_github_client,
            artifact_dir=artifact_dir,
            notify_dan=_noop_notify_dan,
            notify_dan_early_warning=_noop_notify_dan_early_warning,
            bootup_candidate_gate=False,
        )
        assert steward_result_3.evaluated == 0, (
            f"Steward cycle 3 should find 0 UoWs to evaluate (done is terminal), "
            f"got: {steward_result_3}"
        )
