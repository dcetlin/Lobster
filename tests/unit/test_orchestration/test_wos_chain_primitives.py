"""
Unit tests for WOS chain dispatch primitives.

Coverage:
- Fan-out: given a UoW with chain_type="fan_out" and 2 perspectives, assert
  that 2 subagent dispatch calls are made (mock the dispatch function).
- Sub-UoW spawning: given a decomposition dispatch, assert the correct
  instructions and chain state are written.
- Diverge→converge: given 2 approaches, assert approach dispatches happen
  before synthesis dispatch (ordering matters).
- Fallback: given a UoW with no chain_type, assert the existing single-subagent
  path is taken unchanged.
- _create_child_uows: given 3 child specs, assert 3 child UoW records are
  created with correct parent linkage.

WOS-UoW: uow_20260601_424433
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from orchestration.chain_dispatch import (
    CHAIN_FAN_OUT,
    CHAIN_SPEC_BREAKDOWN,
    CHAIN_DIVERGE_CONVERGE,
    ChainResult,
    run_fan_out,
    run_spec_breakdown,
    run_diverge_converge,
    _create_child_uows,
    _perspective_output_path,
    _approach_output_path,
)
from orchestration.executor import Executor, ExecutorOutcome
from orchestration.registry import Registry, UoWStatus
from orchestration.workflow_artifact import WorkflowArtifact, to_json


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


@pytest.fixture
def output_ref(tmp_path: Path) -> str:
    return str(tmp_path / "test_uow.json")


def _make_artifact(**kwargs) -> WorkflowArtifact:
    base: WorkflowArtifact = {
        "uow_id": kwargs.pop("uow_id", "uow_test_001"),
        "executor_type": kwargs.pop("executor_type", "functional-engineer"),
        "constraints": [],
        "prescribed_skills": [],
        "instructions": kwargs.pop("instructions", "Do the thing"),
    }
    base.update(kwargs)  # type: ignore[typeddict-item]
    return base


def _insert_uow(
    db_path: Path,
    uow_id: str,
    workflow_artifact: str | None = None,
    status: str = "ready-for-executor",
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
                summary, success_criteria, workflow_artifact, estimated_runtime
            ) VALUES (?, 'executable', 'test', ?, 'solo', ?, ?, 'Test UoW', 'done', ?, NULL)
            """,
            (uow_id, status, now, now, workflow_artifact),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RecordingDispatcher:
    """Records all dispatch calls in order."""

    def __init__(self, return_id_prefix: str = "exec"):
        self.calls: list[tuple[str, str]] = []  # (instructions, uow_id)
        self._prefix = return_id_prefix

    def __call__(self, instructions: str, uow_id: str) -> str:
        self.calls.append((instructions, uow_id))
        return f"{self._prefix}:{uow_id}"

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def instructions_for(self, index: int) -> str:
        return self.calls[index][0]

    def uow_id_for(self, index: int) -> str:
        return self.calls[index][1]


# ---------------------------------------------------------------------------
# Primitive A: Fan-out
# ---------------------------------------------------------------------------

class TestFanOut:
    """run_fan_out dispatches one subagent per perspective and collects outputs."""

    def test_dispatches_one_agent_per_perspective(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_FAN_OUT,
            perspectives=["security", "performance"],
            instructions="Analyze this codebase.",
        )
        output_ref = str(tmp_path / "uow_fan.json")

        result = run_fan_out("uow_fan_001", output_ref, artifact, dispatcher)

        assert dispatcher.call_count == 2

    def test_perspective_framing_included_in_instructions(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_FAN_OUT,
            perspectives=["security", "maintainability"],
            instructions="Review the auth module.",
        )
        output_ref = str(tmp_path / "uow_fan2.json")

        run_fan_out("uow_fan_002", output_ref, artifact, dispatcher)

        first_instrs = dispatcher.instructions_for(0)
        second_instrs = dispatcher.instructions_for(1)
        assert "security" in first_instrs
        assert "maintainability" in second_instrs
        # base instructions are included in both
        assert "Review the auth module." in first_instrs
        assert "Review the auth module." in second_instrs

    def test_result_is_complete_with_all_perspectives(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_FAN_OUT,
            perspectives=["security", "performance"],
            instructions="Analyze.",
        )
        output_ref = str(tmp_path / "uow_fan3.json")

        result = run_fan_out("uow_fan_003", output_ref, artifact, dispatcher)

        assert result.outcome == "complete"
        assert result.success is True
        data = json.loads(result.output_text)
        assert data["chain_type"] == CHAIN_FAN_OUT
        assert set(data["outputs"].keys()) == {"security", "performance"}

    def test_fails_with_fewer_than_two_perspectives(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_FAN_OUT,
            perspectives=["only_one"],
        )
        output_ref = str(tmp_path / "uow_fan4.json")

        result = run_fan_out("uow_fan_004", output_ref, artifact, dispatcher)

        assert result.outcome == "failed"
        assert result.success is False
        assert dispatcher.call_count == 0

    def test_each_perspective_gets_distinct_sub_uow_id(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_FAN_OUT,
            perspectives=["alpha", "beta"],
        )
        output_ref = str(tmp_path / "uow_fan5.json")

        run_fan_out("uow_fan_005", output_ref, artifact, dispatcher)

        uow_id_0 = dispatcher.uow_id_for(0)
        uow_id_1 = dispatcher.uow_id_for(1)
        assert uow_id_0 != uow_id_1
        assert "alpha" in uow_id_0
        assert "beta" in uow_id_1


# ---------------------------------------------------------------------------
# Primitive B: Sub-UoW spawning
# ---------------------------------------------------------------------------

class TestSpecBreakdown:
    """run_spec_breakdown dispatches a decomposition agent."""

    def test_dispatches_decomposition_agent(self, tmp_path: Path, registry: Registry) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_SPEC_BREAKDOWN,
            decomposition_prompt="Break this into sub-tasks.",
        )
        output_ref = str(tmp_path / "uow_spec.json")
        uow_id = "uow_spec_001"
        _insert_uow(registry.db_path, uow_id, status="active")

        result = run_spec_breakdown(uow_id, output_ref, artifact, dispatcher, registry)

        assert dispatcher.call_count == 1
        instrs = dispatcher.instructions_for(0)
        assert "Break this into sub-tasks." in instrs

    def test_fails_without_decomposition_prompt(self, tmp_path: Path, registry: Registry) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(chain_type=CHAIN_SPEC_BREAKDOWN)
        output_ref = str(tmp_path / "uow_spec2.json")
        uow_id = "uow_spec_002"
        _insert_uow(registry.db_path, uow_id, status="active")

        result = run_spec_breakdown(uow_id, output_ref, artifact, dispatcher, registry)

        assert result.outcome == "failed"
        assert dispatcher.call_count == 0

    def test_output_references_decomposition(self, tmp_path: Path, registry: Registry) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_SPEC_BREAKDOWN,
            decomposition_prompt="Decompose everything.",
        )
        output_ref = str(tmp_path / "uow_spec3.json")
        uow_id = "uow_spec_003"
        _insert_uow(registry.db_path, uow_id, status="active")

        result = run_spec_breakdown(uow_id, output_ref, artifact, dispatcher, registry)

        data = json.loads(result.output_text)
        assert data["chain_type"] == CHAIN_SPEC_BREAKDOWN
        assert "decomposition_executor_id" in data


class TestCreateChildUows:
    """_create_child_uows inserts child UoW records with correct parent linkage."""

    def test_creates_correct_number_of_children(self, registry: Registry) -> None:
        parent_id = "uow_parent_001"
        _insert_uow(registry.db_path, parent_id, status="active")

        child_specs = [
            {"summary": "Child A", "success_criteria": "A done", "type": "executable"},
            {"summary": "Child B", "success_criteria": "B done", "type": "executable"},
            {"summary": "Child C", "success_criteria": "C done", "type": "executable"},
        ]
        child_ids = _create_child_uows(registry, parent_id, child_specs)

        assert len(child_ids) == 3

    def test_children_have_correct_parent_linkage(self, registry: Registry) -> None:
        parent_id = "uow_parent_002"
        _insert_uow(registry.db_path, parent_id, status="active")

        child_specs = [
            {"summary": "Child X", "success_criteria": "X done", "type": "executable"},
            {"summary": "Child Y", "success_criteria": "Y done", "type": "executable"},
        ]
        child_ids = _create_child_uows(registry, parent_id, child_specs)

        conn = sqlite3.connect(str(registry.db_path))
        conn.row_factory = sqlite3.Row
        try:
            for child_id in child_ids:
                row = conn.execute(
                    "SELECT parent, summary FROM uow_registry WHERE id = ?", (child_id,)
                ).fetchone()
                assert row is not None
                assert row["parent"] == parent_id
        finally:
            conn.close()

    def test_children_are_pending_status(self, registry: Registry) -> None:
        parent_id = "uow_parent_003"
        _insert_uow(registry.db_path, parent_id, status="active")

        child_specs = [{"summary": "Child Z", "success_criteria": "done", "type": "executable"}]
        child_ids = _create_child_uows(registry, parent_id, child_specs)

        conn = sqlite3.connect(str(registry.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM uow_registry WHERE id = ?", (child_ids[0],)
            ).fetchone()
            assert row["status"] == "pending"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Primitive C: Diverge→converge
# ---------------------------------------------------------------------------

class TestDivergeConverge:
    """run_diverge_converge dispatches approach agents then synthesis agent."""

    def test_dispatches_approach_agents_before_synthesis(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_DIVERGE_CONVERGE,
            approaches=["approach_a", "approach_b"],
            synthesis_prompt="Combine the approaches.",
            instructions="Solve the problem.",
        )
        output_ref = str(tmp_path / "uow_dc.json")

        result = run_diverge_converge("uow_dc_001", output_ref, artifact, dispatcher)

        # 2 approaches + 1 synthesis = 3 total
        assert dispatcher.call_count == 3

        # First two calls are for approaches
        instrs_0 = dispatcher.instructions_for(0)
        instrs_1 = dispatcher.instructions_for(1)
        assert "approach_a" in instrs_0
        assert "approach_b" in instrs_1

        # Last call is synthesis
        instrs_synth = dispatcher.instructions_for(2)
        assert "Combine the approaches." in instrs_synth

    def test_synthesis_receives_approach_outputs(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_DIVERGE_CONVERGE,
            approaches=["fast", "thorough"],
            synthesis_prompt="Pick the best.",
        )
        output_ref = str(tmp_path / "uow_dc2.json")

        run_diverge_converge("uow_dc_002", output_ref, artifact, dispatcher)

        # Synthesis instructions must reference both approach executor IDs or output paths
        synth_instrs = dispatcher.instructions_for(2)
        assert "fast" in synth_instrs
        assert "thorough" in synth_instrs

    def test_result_complete_with_both_approaches(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_DIVERGE_CONVERGE,
            approaches=["a1", "a2"],
            synthesis_prompt="Synthesize.",
        )
        output_ref = str(tmp_path / "uow_dc3.json")

        result = run_diverge_converge("uow_dc_003", output_ref, artifact, dispatcher)

        assert result.outcome == "complete"
        assert result.success is True
        data = json.loads(result.output_text)
        assert data["chain_type"] == CHAIN_DIVERGE_CONVERGE
        assert set(data["approaches"]) == {"a1", "a2"}
        assert "synthesis_executor_id" in data

    def test_fails_with_fewer_than_two_approaches(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_DIVERGE_CONVERGE,
            approaches=["only_one"],
            synthesis_prompt="Synthesize.",
        )
        output_ref = str(tmp_path / "uow_dc4.json")

        result = run_diverge_converge("uow_dc_004", output_ref, artifact, dispatcher)

        assert result.outcome == "failed"
        assert dispatcher.call_count == 0

    def test_fails_without_synthesis_prompt(self, tmp_path: Path) -> None:
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            chain_type=CHAIN_DIVERGE_CONVERGE,
            approaches=["a", "b"],
        )
        output_ref = str(tmp_path / "uow_dc5.json")

        result = run_diverge_converge("uow_dc_005", output_ref, artifact, dispatcher)

        assert result.outcome == "failed"
        assert dispatcher.call_count == 0

    def test_approach_ordering_before_synthesis(self, tmp_path: Path) -> None:
        """Synthesis must fire only after all approach dispatches complete."""
        call_log: list[str] = []

        def recording_dispatcher(instructions: str, uow_id: str) -> str:
            # Classify by uow_id suffix: diverge.* = approach, converge.* = synthesis
            if ".diverge." in uow_id:
                call_log.append(f"approach:{uow_id}")
            elif ".converge." in uow_id:
                call_log.append(f"synthesis:{uow_id}")
            else:
                call_log.append(f"other:{uow_id}")
            return f"exec:{uow_id}"

        artifact = _make_artifact(
            chain_type=CHAIN_DIVERGE_CONVERGE,
            approaches=["approach_x", "approach_y"],
            synthesis_prompt="Synthesize all.",
            instructions="Do the work.",
        )
        output_ref = str(tmp_path / "uow_dc6.json")

        run_diverge_converge("uow_dc_006", output_ref, artifact, recording_dispatcher)

        # Both approach calls must appear before the synthesis call
        approach_indices = [i for i, c in enumerate(call_log) if c.startswith("approach:")]
        synthesis_indices = [i for i, c in enumerate(call_log) if c.startswith("synthesis:")]

        assert len(approach_indices) == 2
        assert len(synthesis_indices) == 1
        assert max(approach_indices) < synthesis_indices[0]


# ---------------------------------------------------------------------------
# Fallback: no chain_type → single-subagent path
# ---------------------------------------------------------------------------

class TestFallbackNoChainType:
    """When chain_type is absent, the existing single-subagent path is taken."""

    def test_no_chain_type_uses_single_dispatch(self, db_path: Path, tmp_path: Path) -> None:
        registry = Registry(db_path)
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            uow_id="uow_fallback_001",
            instructions="Single dispatch task.",
        )
        # No chain_type field
        assert "chain_type" not in artifact

        uow_id = "uow_fallback_001"
        _insert_uow(
            db_path, uow_id,
            workflow_artifact=to_json(artifact),
            status="ready-for-executor",
        )

        executor = Executor(registry, dispatcher=dispatcher)
        result = executor.execute_uow(uow_id)

        assert result.outcome == ExecutorOutcome.COMPLETE
        assert dispatcher.call_count == 1

    def test_chain_type_none_uses_single_dispatch(self, db_path: Path, tmp_path: Path) -> None:
        registry = Registry(db_path)
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            uow_id="uow_fallback_002",
            instructions="Single dispatch task.",
        )
        artifact["chain_type"] = None  # type: ignore[typeddict-unknown-key]

        uow_id = "uow_fallback_002"
        _insert_uow(
            db_path, uow_id,
            workflow_artifact=to_json(artifact),
            status="ready-for-executor",
        )

        executor = Executor(registry, dispatcher=dispatcher)
        result = executor.execute_uow(uow_id)

        assert result.outcome == ExecutorOutcome.COMPLETE
        assert dispatcher.call_count == 1

    def test_unrecognized_chain_type_falls_back(self, db_path: Path, tmp_path: Path) -> None:
        registry = Registry(db_path)
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            uow_id="uow_fallback_003",
            instructions="Fallback task.",
        )
        artifact["chain_type"] = "unknown_future_type"  # type: ignore[typeddict-unknown-key]

        uow_id = "uow_fallback_003"
        _insert_uow(
            db_path, uow_id,
            workflow_artifact=to_json(artifact),
            status="ready-for-executor",
        )

        executor = Executor(registry, dispatcher=dispatcher)
        result = executor.execute_uow(uow_id)

        assert result.outcome == ExecutorOutcome.COMPLETE
        # Single dispatch call despite unrecognized chain_type
        assert dispatcher.call_count == 1


# ---------------------------------------------------------------------------
# Executor integration: chain routing
# ---------------------------------------------------------------------------

class TestExecutorChainRouting:
    """Executor._run_execution routes to chain primitives when chain_type is set."""

    def test_fan_out_dispatches_n_agents(self, db_path: Path, tmp_path: Path) -> None:
        registry = Registry(db_path)
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            uow_id="uow_exec_fan_001",
            chain_type=CHAIN_FAN_OUT,
            perspectives=["security", "performance"],
            instructions="Analyze everything.",
        )
        uow_id = "uow_exec_fan_001"
        _insert_uow(db_path, uow_id, workflow_artifact=to_json(artifact), status="ready-for-executor")

        executor = Executor(registry, dispatcher=dispatcher)
        result = executor.execute_uow(uow_id)

        # Fan-out dispatches 2 perspective subagents
        assert dispatcher.call_count == 2
        assert result.outcome == ExecutorOutcome.COMPLETE

    def test_diverge_converge_dispatches_approaches_then_synthesis(self, db_path: Path, tmp_path: Path) -> None:
        registry = Registry(db_path)
        dispatcher = _RecordingDispatcher()
        artifact = _make_artifact(
            uow_id="uow_exec_dc_001",
            chain_type=CHAIN_DIVERGE_CONVERGE,
            approaches=["fast_path", "safe_path"],
            synthesis_prompt="Pick the winner.",
            instructions="Implement the feature.",
        )
        uow_id = "uow_exec_dc_001"
        _insert_uow(db_path, uow_id, workflow_artifact=to_json(artifact), status="ready-for-executor")

        executor = Executor(registry, dispatcher=dispatcher)
        result = executor.execute_uow(uow_id)

        # 2 approaches + 1 synthesis
        assert dispatcher.call_count == 3
        assert result.outcome == ExecutorOutcome.COMPLETE
