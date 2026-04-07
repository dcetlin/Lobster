"""
Tests for WOS executor register-appropriate routing dispatch table.

Updated for issue #664: the production dispatch path for functional-engineer,
lobster-ops, and general executor types is now _dispatch_via_inbox (event-driven
MCP inbox pattern) instead of _dispatch_via_claude_p (subprocess).

Coverage:
- functional-engineer executor_type routes to _dispatch_via_inbox (primary path)
- lobster-ops executor_type routes to _dispatch_via_inbox (same mechanism)
- general executor_type routes to _dispatch_via_inbox
- frontier-writer executor_type routes to _dispatch_via_frontier_writer (not inbox)
- design-review executor_type routes to _dispatch_via_design_review (not inbox)
- unknown executor_type falls back to _dispatch_via_inbox (safe default)
- injected dispatcher on Executor.__init__ takes precedence over dispatch table
- _dispatch_via_claude_p remains available as a named legacy fallback for CI/dev
- _dispatch_via_frontier_writer preamble differs from _FUNCTIONAL_ENGINEER_PREAMBLE
- _dispatch_via_design_review preamble differs from _FUNCTIONAL_ENGINEER_PREAMBLE
- dispatch table is a pure function of executor_type (deterministic, no side effects)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from orchestration.registry import Registry
from orchestration.workflow_artifact import WorkflowArtifact, to_json
from orchestration.executor import (
    Executor,
    ExecutorOutcome,
    _noop_dispatcher,
    _dispatch_via_inbox,
    _dispatch_via_claude_p,
    _FUNCTIONAL_ENGINEER_PREAMBLE,
    _FRONTIER_WRITER_PREAMBLE,
    _DESIGN_REVIEW_PREAMBLE,
    _dispatch_via_frontier_writer,
    _dispatch_via_design_review,
    _resolve_dispatcher,
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
    executor_type: str = "functional-engineer",
    instructions: str = "Do the thing",
) -> str:
    artifact: WorkflowArtifact = {
        "uow_id": uow_id,
        "executor_type": executor_type,
        "constraints": [],
        "prescribed_skills": [],
        "instructions": instructions,
    }
    return to_json(artifact)


def _insert_uow(
    db_path: Path,
    uow_id: str,
    executor_type: str = "functional-engineer",
    register: str = "operational",
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
                summary, success_criteria, workflow_artifact, estimated_runtime,
                register
            ) VALUES (?, 'executable', 'test', ?, 'solo', ?, ?, 'Test UoW', 'done', ?, NULL, ?)
            """,
            (uow_id, status, now, now, _make_artifact(uow_id, executor_type=executor_type), register),
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


# ---------------------------------------------------------------------------
# _resolve_dispatcher — pure dispatch table function
# ---------------------------------------------------------------------------

class TestResolveDispatcher:
    """_resolve_dispatcher maps executor_type to the correct dispatcher function.

    After issue #664: primary operational types route to _dispatch_via_inbox
    (event-driven MCP inbox path). _dispatch_via_claude_p is a legacy fallback
    only — it is not referenced by the dispatch table.
    """

    def test_functional_engineer_resolves_to_inbox(self) -> None:
        dispatcher = _resolve_dispatcher("functional-engineer")
        assert dispatcher is _dispatch_via_inbox

    def test_lobster_ops_resolves_to_inbox(self) -> None:
        dispatcher = _resolve_dispatcher("lobster-ops")
        assert dispatcher is _dispatch_via_inbox

    def test_general_resolves_to_inbox(self) -> None:
        dispatcher = _resolve_dispatcher("general")
        assert dispatcher is _dispatch_via_inbox

    def test_frontier_writer_resolves_to_frontier_writer_dispatcher(self) -> None:
        dispatcher = _resolve_dispatcher("frontier-writer")
        assert dispatcher is _dispatch_via_frontier_writer

    def test_design_review_resolves_to_design_review_dispatcher(self) -> None:
        dispatcher = _resolve_dispatcher("design-review")
        assert dispatcher is _dispatch_via_design_review

    def test_unknown_executor_type_falls_back_to_inbox(self) -> None:
        """Unknown executor_type must fall back to _dispatch_via_inbox (safe default)."""
        dispatcher = _resolve_dispatcher("unknown-type")
        assert dispatcher is _dispatch_via_inbox

    def test_empty_string_falls_back_to_inbox(self) -> None:
        dispatcher = _resolve_dispatcher("")
        assert dispatcher is _dispatch_via_inbox

    def test_claude_p_remains_importable_as_legacy_fallback(self) -> None:
        """_dispatch_via_claude_p must remain importable for CI/dev use."""
        assert callable(_dispatch_via_claude_p)


# ---------------------------------------------------------------------------
# Preamble constants — frontier-writer and design-review differ from functional-engineer
# ---------------------------------------------------------------------------

class TestPreambleConstants:
    """Preamble strings must be distinct per executor type."""

    def test_frontier_writer_preamble_differs_from_functional_engineer(self) -> None:
        assert _FRONTIER_WRITER_PREAMBLE != _FUNCTIONAL_ENGINEER_PREAMBLE

    def test_design_review_preamble_differs_from_functional_engineer(self) -> None:
        assert _DESIGN_REVIEW_PREAMBLE != _FUNCTIONAL_ENGINEER_PREAMBLE

    def test_frontier_writer_preamble_differs_from_design_review(self) -> None:
        assert _FRONTIER_WRITER_PREAMBLE != _DESIGN_REVIEW_PREAMBLE

    def test_frontier_writer_preamble_is_nonempty(self) -> None:
        assert len(_FRONTIER_WRITER_PREAMBLE.strip()) > 0

    def test_design_review_preamble_is_nonempty(self) -> None:
        assert len(_DESIGN_REVIEW_PREAMBLE.strip()) > 0

    def test_functional_engineer_preamble_is_nonempty(self) -> None:
        assert len(_FUNCTIONAL_ENGINEER_PREAMBLE.strip()) > 0


# ---------------------------------------------------------------------------
# Dispatch table routing via Executor.execute_uow — behavioral tests
# ---------------------------------------------------------------------------

class TestDispatchTableRoutingViaCapturedInstructions:
    """
    Verify that the dispatch table selects the correct dispatcher based on
    executor_type in the workflow artifact.

    Strategy: replace dispatchers with capturing stubs. Each stub records
    whether it was called. After execute_uow returns, assert that the
    correct stub was called and the wrong stubs were not.

    After issue #664: functional-engineer, lobster-ops, and general route to
    _dispatch_via_inbox. The stub patches _dispatch_via_inbox, not _dispatch_via_claude_p.
    """

    def test_functional_engineer_routes_to_inbox_dispatcher(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """functional-engineer uses the inbox dispatch path (event-driven, no subprocess)."""
        uow_id = "route_fe_001"
        _insert_uow(db_path, uow_id, executor_type="functional-engineer")

        called_with: list[tuple[str, str]] = []

        def capture_inbox(instructions: str, uid: str) -> str:
            called_with.append((instructions, uid))
            return "inbox-msg-id-fe"

        # Patch the module-level function the dispatch table references
        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox", capture_inbox)

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        assert len(called_with) == 1, "_dispatch_via_inbox must be called exactly once"
        assert called_with[0][1] == uow_id

    def test_lobster_ops_routes_to_inbox_dispatcher(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "route_lops_001"
        _insert_uow(db_path, uow_id, executor_type="lobster-ops")

        called_with: list[tuple[str, str]] = []

        def capture_inbox(instructions: str, uid: str) -> str:
            called_with.append((instructions, uid))
            return "inbox-msg-id-lops"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox", capture_inbox)

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        assert len(called_with) == 1
        assert called_with[0][1] == uow_id

    def test_frontier_writer_routes_to_frontier_writer_dispatcher(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """frontier-writer executor_type must route to _dispatch_via_frontier_writer, not inbox."""
        uow_id = "route_fw_001"
        _insert_uow(db_path, uow_id, executor_type="frontier-writer", register="philosophical")

        fw_called: list[tuple[str, str]] = []
        inbox_called: list[str] = []

        def capture_frontier_writer(instructions: str, uid: str) -> str:
            fw_called.append((instructions, uid))
            return "run-id-fw"

        def capture_inbox(instructions: str, uid: str) -> str:
            inbox_called.append(uid)
            return "inbox-msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_frontier_writer", capture_frontier_writer)
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox", capture_inbox)

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        assert len(fw_called) == 1, "_dispatch_via_frontier_writer must be called for frontier-writer"
        assert len(inbox_called) == 0, "_dispatch_via_inbox must NOT be called for frontier-writer"
        assert fw_called[0][1] == uow_id

    def test_design_review_routes_to_design_review_dispatcher(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """design-review executor_type must route to _dispatch_via_design_review, not inbox."""
        uow_id = "route_dr_001"
        _insert_uow(db_path, uow_id, executor_type="design-review", register="human-judgment")

        dr_called: list[tuple[str, str]] = []
        inbox_called: list[str] = []

        def capture_design_review(instructions: str, uid: str) -> str:
            dr_called.append((instructions, uid))
            return "run-id-dr"

        def capture_inbox(instructions: str, uid: str) -> str:
            inbox_called.append(uid)
            return "inbox-msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_design_review", capture_design_review)
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox", capture_inbox)

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        assert len(dr_called) == 1, "_dispatch_via_design_review must be called for design-review"
        assert len(inbox_called) == 0, "_dispatch_via_inbox must NOT be called for design-review"
        assert dr_called[0][1] == uow_id

    def test_general_routes_to_inbox_dispatcher(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "route_gen_001"
        _insert_uow(db_path, uow_id, executor_type="general")

        called_with: list[str] = []

        def capture_inbox(instructions: str, uid: str) -> str:
            called_with.append(uid)
            return "inbox-msg-id-gen"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox", capture_inbox)

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        assert len(called_with) == 1
        assert called_with[0] == uow_id


# ---------------------------------------------------------------------------
# Injected dispatcher takes precedence over dispatch table
# ---------------------------------------------------------------------------

class TestInjectedDispatcherPrecedence:
    """
    When a dispatcher is injected via Executor.__init__, it takes precedence
    over the dispatch table regardless of executor_type in the artifact.

    This preserves backward compatibility for tests and CI environments.
    """

    def test_injected_dispatcher_overrides_table_for_functional_engineer(
        self, registry: Registry, db_path: Path
    ) -> None:
        uow_id = "injected_fe_001"
        _insert_uow(db_path, uow_id, executor_type="functional-engineer")

        injected_called: list[str] = []

        def injected(instructions: str, uid: str) -> str:
            injected_called.append(uid)
            return "injected-run"

        executor = Executor(registry, dispatcher=injected)
        executor.execute_uow(uow_id)

        assert len(injected_called) == 1, "Injected dispatcher must be called"
        assert injected_called[0] == uow_id

    def test_injected_dispatcher_overrides_table_for_frontier_writer(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Even for frontier-writer, injected dispatcher wins."""
        uow_id = "injected_fw_001"
        _insert_uow(db_path, uow_id, executor_type="frontier-writer", register="philosophical")

        injected_called: list[str] = []

        def injected(instructions: str, uid: str) -> str:
            injected_called.append(uid)
            return "injected-run"

        executor = Executor(registry, dispatcher=injected)
        executor.execute_uow(uow_id)

        assert len(injected_called) == 1, "Injected dispatcher must be called even for frontier-writer"

    def test_injected_dispatcher_overrides_table_for_design_review(
        self, registry: Registry, db_path: Path
    ) -> None:
        uow_id = "injected_dr_001"
        _insert_uow(db_path, uow_id, executor_type="design-review", register="human-judgment")

        injected_called: list[str] = []

        def injected(instructions: str, uid: str) -> str:
            injected_called.append(uid)
            return "injected-run"

        executor = Executor(registry, dispatcher=injected)
        executor.execute_uow(uow_id)

        assert len(injected_called) == 1


# ---------------------------------------------------------------------------
# No behavioral change for operational UoWs
# ---------------------------------------------------------------------------

class TestOperationalUoWInboxDispatch:
    """
    For operational UoWs with functional-engineer executor_type, the dispatch
    path is now _dispatch_via_inbox. Outcome and result.json format are unchanged.
    """

    def test_operational_uow_complete_outcome(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "no_regression_001"
        _insert_uow(db_path, uow_id, executor_type="functional-engineer", register="operational")

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox", lambda i, u: "inbox-msg-id")

        executor = Executor(registry)
        result = executor.execute_uow(uow_id)

        assert result.outcome == ExecutorOutcome.COMPLETE
        assert result.success is True
        assert result.uow_id == uow_id

    def test_operational_uow_result_json_valid(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orchestration.executor import _result_json_path
        uow_id = "no_regression_002"
        _insert_uow(db_path, uow_id, executor_type="functional-engineer", register="operational")

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox", lambda i, u: "inbox-msg-id")

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        result_data = json.loads(_result_json_path(output_ref).read_text())
        assert result_data["outcome"] == "complete"
        assert result_data["success"] is True
        assert result_data["uow_id"] == uow_id


# ---------------------------------------------------------------------------
# Preamble injection tests — verify dispatcher receives preamble
# ---------------------------------------------------------------------------

class TestPreambleInjectionInInstructions:
    """
    Verify that the instructions string passed to each dispatcher is prefixed
    with the correct preamble for that executor_type.
    """

    def test_frontier_writer_instructions_contain_frontier_writer_preamble(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "preamble_fw_001"
        prescription = "Write a philosophical synthesis on consciousness."
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        artifact: WorkflowArtifact = {
            "uow_id": uow_id,
            "executor_type": "frontier-writer",
            "constraints": [],
            "prescribed_skills": [],
            "instructions": prescription,
        }
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, workflow_artifact, register
            ) VALUES (?, 'executable', 'test', 'ready-for-executor', 'solo', ?, ?, 'Test', 'done', ?, 'philosophical')
            """,
            (uow_id, now, now, to_json(artifact)),
        )
        conn.commit()
        conn.close()

        received_instructions: list[str] = []

        def capture_fw(instructions: str, uid: str) -> str:
            received_instructions.append(instructions)
            return "run-fw"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_frontier_writer", capture_fw)

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        assert len(received_instructions) == 1
        instr = received_instructions[0]
        assert _FRONTIER_WRITER_PREAMBLE in instr, (
            "Instructions must contain _FRONTIER_WRITER_PREAMBLE for frontier-writer executor_type"
        )
        assert prescription in instr, (
            "Instructions must contain the original prescription body"
        )

    def test_design_review_instructions_contain_design_review_preamble(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "preamble_dr_001"
        prescription = "Review the architectural decision for the new routing layer."
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        artifact: WorkflowArtifact = {
            "uow_id": uow_id,
            "executor_type": "design-review",
            "constraints": [],
            "prescribed_skills": [],
            "instructions": prescription,
        }
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, workflow_artifact, register
            ) VALUES (?, 'executable', 'test', 'ready-for-executor', 'solo', ?, ?, 'Test', 'done', ?, 'human-judgment')
            """,
            (uow_id, now, now, to_json(artifact)),
        )
        conn.commit()
        conn.close()

        received_instructions: list[str] = []

        def capture_dr(instructions: str, uid: str) -> str:
            received_instructions.append(instructions)
            return "run-dr"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_design_review", capture_dr)

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        assert len(received_instructions) == 1
        instr = received_instructions[0]
        assert _DESIGN_REVIEW_PREAMBLE in instr, (
            "Instructions must contain _DESIGN_REVIEW_PREAMBLE for design-review executor_type"
        )
        assert prescription in instr, (
            "Instructions must contain the original prescription body"
        )
