"""
Tests for WOS cross-system linkage — UoW ID injection into subagent prompt (issue #868)
and write_result back-propagation to output file (issue #867).

Behavior under test (issue #868):
- Every dispatched subagent prompt contains "UoW ID: {uow_id}" in the WOS context block
- Every dispatched subagent prompt contains PR description footer stamping instructions
- Every dispatched subagent prompt contains issue label stamping instructions
- UoW ID injection applies to all preambles: functional-engineer, lobster-ops, general,
  frontier-writer, design-review (and lobster-generalist, lobster-meta via functional-engineer preamble)
- The UoW ID injected matches the actual uow_id being dispatched
- Injection works for both the production dispatch table path and injected dispatcher path

Behavior under test (issue #867 — write_result back-propagation):
- maybe_complete_wos_uow enriches the result.json (at output_ref.result.json) with a
  summary field from write_result text when it fires
- maybe_complete_wos_uow populates a refs field in result.json with extracted artifact refs
- Registry transition to ready-for-steward fires even when result.json is missing (non-blocking)

Named constants mirror the names in the implementation to anchor tests to the spec.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.registry import Registry
from orchestration.workflow_artifact import WorkflowArtifact, to_json
from orchestration.executor import (
    Executor,
    _FUNCTIONAL_ENGINEER_PREAMBLE,
    _FRONTIER_WRITER_PREAMBLE,
    _DESIGN_REVIEW_PREAMBLE,
    _build_wos_context_block,
)


# ---------------------------------------------------------------------------
# Constants — named after spec terms so test failures are self-documenting
# ---------------------------------------------------------------------------

#: Spec: every dispatched prompt must contain "UoW ID:" in the context block
UOW_ID_LABEL = "UoW ID:"

#: Spec: every dispatched prompt must include PR footer stamping instructions
PR_FOOTER_INSTRUCTION = "WOS-UoW:"

#: Spec: every dispatched prompt must include issue label stamping instructions
ISSUE_LABEL_INSTRUCTION = "wos:uow_"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


def _insert_uow(
    db_path: Path,
    uow_id: str,
    executor_type: str = "functional-engineer",
    register: str = "operational",
    instructions: str = "Implement the feature.",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    artifact: WorkflowArtifact = {
        "uow_id": uow_id,
        "executor_type": executor_type,
        "constraints": [],
        "prescribed_skills": [],
        "instructions": instructions,
    }
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, workflow_artifact, register
            ) VALUES (?, 'executable', 'test', 'ready-for-executor', 'solo', ?, ?, 'Test UoW', 'done', ?, ?)
            """,
            (uow_id, now, now, to_json(artifact), register),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _build_wos_context_block — pure function tests
# ---------------------------------------------------------------------------

class TestBuildWosContextBlock:
    """
    _build_wos_context_block is a pure function that constructs the WOS context
    block injected into every subagent prompt.

    The block must contain:
    - "UoW ID: {uow_id}" so the subagent knows which UoW it is executing
    - Instructions to add WOS-UoW footer to PR descriptions
    - Instructions to add wos:uow_{uow_id} label to issues
    """

    def test_block_contains_uow_id_label(self) -> None:
        block = _build_wos_context_block("uow_test123")
        assert UOW_ID_LABEL in block
        assert "uow_test123" in block

    def test_block_contains_pr_footer_instruction(self) -> None:
        block = _build_wos_context_block("uow_abc")
        assert PR_FOOTER_INSTRUCTION in block
        assert "uow_abc" in block

    def test_block_contains_issue_label_instruction(self) -> None:
        block = _build_wos_context_block("uow_xyz")
        assert ISSUE_LABEL_INSTRUCTION in block
        assert "uow_xyz" in block

    def test_block_is_deterministic(self) -> None:
        """Pure function: same input always produces same output."""
        uow_id = "uow_determinism_test"
        assert _build_wos_context_block(uow_id) == _build_wos_context_block(uow_id)

    def test_different_uow_ids_produce_different_blocks(self) -> None:
        block_a = _build_wos_context_block("uow_aaa")
        block_b = _build_wos_context_block("uow_bbb")
        assert block_a != block_b
        assert "uow_aaa" in block_a
        assert "uow_bbb" in block_b
        assert "uow_aaa" not in block_b

    def test_block_is_nonempty(self) -> None:
        block = _build_wos_context_block("uow_nonempty")
        assert len(block.strip()) > 0


# ---------------------------------------------------------------------------
# UoW ID injected into dispatched prompt (issue #868)
# ---------------------------------------------------------------------------

class TestUowIdInjectedIntoDispatchedPrompt:
    """
    When _run_execution dispatches a subagent, the instructions string received by
    the dispatcher must contain the UoW ID in a context block.

    This verifies the spec requirement: subagents have the UoW ID available at
    execution time so they can stamp artifacts (PR descriptions, issue labels).
    """

    def test_functional_engineer_prompt_contains_uow_id(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "uow_inject_fe_001"
        _insert_uow(db_path, uow_id, executor_type="functional-engineer")

        received: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", capture)

        Executor(registry).execute_uow(uow_id)

        assert len(received) == 1
        assert UOW_ID_LABEL in received[0], "Dispatched prompt must contain 'UoW ID:'"
        assert uow_id in received[0], f"Dispatched prompt must contain the actual uow_id '{uow_id}'"

    def test_functional_engineer_prompt_contains_pr_footer_instruction(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "uow_inject_fe_002"
        _insert_uow(db_path, uow_id, executor_type="functional-engineer")

        received: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", capture)

        Executor(registry).execute_uow(uow_id)

        assert PR_FOOTER_INSTRUCTION in received[0], (
            "Dispatched prompt must contain PR footer stamping instruction 'WOS-UoW:'"
        )

    def test_functional_engineer_prompt_contains_issue_label_instruction(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "uow_inject_fe_003"
        _insert_uow(db_path, uow_id, executor_type="functional-engineer")

        received: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", capture)

        Executor(registry).execute_uow(uow_id)

        assert ISSUE_LABEL_INSTRUCTION in received[0], (
            "Dispatched prompt must contain issue label stamping instruction 'wos:uow_'"
        )

    def test_frontier_writer_prompt_contains_uow_id(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "uow_inject_fw_001"
        _insert_uow(db_path, uow_id, executor_type="frontier-writer", register="philosophical")

        received: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "run-fw"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_frontier_writer", capture)

        Executor(registry).execute_uow(uow_id)

        assert UOW_ID_LABEL in received[0]
        assert uow_id in received[0]

    def test_design_review_prompt_contains_uow_id(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "uow_inject_dr_001"
        _insert_uow(db_path, uow_id, executor_type="design-review", register="human-judgment")

        received: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "run-dr"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_design_review", capture)

        Executor(registry).execute_uow(uow_id)

        assert UOW_ID_LABEL in received[0]
        assert uow_id in received[0]

    def test_lobster_ops_prompt_contains_uow_id(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "uow_inject_lops_001"
        _insert_uow(db_path, uow_id, executor_type="lobster-ops")

        received: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_lobster_ops", capture)

        Executor(registry).execute_uow(uow_id)

        assert UOW_ID_LABEL in received[0]
        assert uow_id in received[0]

    def test_uow_id_in_prompt_matches_dispatched_uow(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The UoW ID in the prompt must match the UoW being dispatched, not a placeholder."""
        uow_id = "uow_match_verify_abc123"
        _insert_uow(db_path, uow_id, executor_type="functional-engineer")

        received: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", capture)

        Executor(registry).execute_uow(uow_id)

        # Verify the exact UoW ID appears in the prompt (not just any UoW ID fragment)
        assert uow_id in received[0], (
            f"Expected exact uow_id '{uow_id}' in dispatched instructions, got:\n{received[0][:500]}"
        )

    def test_prescription_body_still_present_with_uow_id_injected(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UoW ID injection must not displace the prescription body."""
        uow_id = "uow_inject_body_check_001"
        prescription = "Implement the unique feature XYZ-sentinel-12345."
        _insert_uow(db_path, uow_id, executor_type="functional-engineer", instructions=prescription)

        received: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", capture)

        Executor(registry).execute_uow(uow_id)

        assert prescription in received[0], "Original prescription body must be present in dispatched prompt"
        assert UOW_ID_LABEL in received[0], "UoW ID block must also be present"


# ---------------------------------------------------------------------------
# UoW ID injection for injected dispatcher path
# ---------------------------------------------------------------------------

class TestUowIdInjectionWithInjectedDispatcher:
    """
    When a dispatcher is injected via Executor.__init__ (test/CI path), the
    WOS context block must still be injected before the raw_instructions.
    """

    def test_injected_dispatcher_receives_prompt_with_uow_id(
        self, registry: Registry, db_path: Path
    ) -> None:
        uow_id = "uow_injected_disp_001"
        prescription = "Do something specific with sentinel-87654."
        _insert_uow(db_path, uow_id, executor_type="functional-engineer", instructions=prescription)

        received: list[str] = []

        def injected_dispatcher(instructions: str, uid: str) -> str:
            received.append(instructions)
            return "injected-run"

        Executor(registry, dispatcher=injected_dispatcher).execute_uow(uow_id)

        assert len(received) == 1
        assert UOW_ID_LABEL in received[0], "Injected dispatcher must receive prompt with UoW ID"
        assert uow_id in received[0]
        assert prescription in received[0]


# ---------------------------------------------------------------------------
# write_result back-propagation to result.json (issue #867)
# ---------------------------------------------------------------------------

class TestWriteResultBackPropagation:
    """
    When a subagent calls write_result, maybe_complete_wos_uow must:
    1. Enrich the UoW result.json (at output_ref.result.json) with a summary field
    2. Populate a refs field with extracted artifact references (PR numbers, issue numbers)
    3. Complete the registry transition (existing behavior, unchanged)
    4. Not block on file write failure (non-blocking enrichment)

    The result file lives at output_ref.result.json (executor contract artifact).
    """

    def _seed_uow_at_executing(
        self,
        db_path: Path,
        uow_id: str,
        output_ref: str,
    ) -> None:
        """Insert a UoW already in 'executing' state with a known output_ref."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute(
                """
                INSERT INTO uow_registry (
                    id, type, source, status, posture, created_at, updated_at,
                    summary, success_criteria, workflow_artifact, output_ref,
                    started_at, register
                ) VALUES (
                    ?, 'executable', 'test', 'executing', 'solo', ?, ?,
                    'Test UoW', 'done', '{}', ?,
                    ?, 'operational'
                )
                """,
                (uow_id, now, now, output_ref, now),
            )
            conn.commit()
        finally:
            conn.close()

    def _result_json_path(self, output_ref: str) -> Path:
        """Derive result.json path — mirrors executor._result_json_path convention."""
        p = Path(output_ref)
        if p.suffix:
            return p.with_suffix(".result.json")
        return Path(output_ref + ".result.json")

    def test_result_summary_written_to_result_json(
        self, tmp_path: Path
    ) -> None:
        """write_result payload text must be written to the UoW result.json file."""
        from orchestration.wos_completion import maybe_complete_wos_uow

        db_path = tmp_path / "registry.db"
        Registry(db_path)  # initialize schema

        uow_id = "uow_backprop_001"
        output_ref = str(tmp_path / f"{uow_id}.json")
        # Pre-write a result.json (executor writes this before transitioning to executing)
        result_json = self._result_json_path(output_ref)
        result_json.write_text(json.dumps({"outcome": "complete", "success": True, "uow_id": uow_id}))

        self._seed_uow_at_executing(db_path, uow_id, output_ref)

        result_text = "PR #999 opened successfully. All tests passed."
        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(
                task_id=f"wos-{uow_id}",
                status="success",
                result_text=result_text,
            )

        content = result_json.read_text()
        data = json.loads(content)
        # The enriched result.json must contain a summary field with the write_result text
        assert "summary" in data, f"result.json must have summary field, got keys: {list(data.keys())}"
        assert result_text[:50] in data["summary"], (
            f"summary must contain the write_result text, got: {data['summary']!r}"
        )

    def test_registry_transition_still_fires_when_result_file_missing(
        self, tmp_path: Path
    ) -> None:
        """
        Back-propagation is non-blocking: if the result.json does not exist,
        the registry transition to ready-for-steward must still complete.
        """
        from orchestration.wos_completion import maybe_complete_wos_uow

        db_path = tmp_path / "registry.db"
        Registry(db_path)  # initialize schema

        uow_id = "uow_backprop_no_file_001"
        # output_ref points to a file whose .result.json does not exist on disk
        output_ref = str(tmp_path / f"{uow_id}.json")

        self._seed_uow_at_executing(db_path, uow_id, output_ref)

        # Should not raise even though result.json doesn't exist
        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(
                task_id=f"wos-{uow_id}",
                status="success",
                result_text="Done.",
            )

        # Registry transition must have fired despite missing result.json
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM uow_registry WHERE id = ?", (uow_id,)).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "ready-for-steward", (
            f"Registry must transition to ready-for-steward even when result.json is missing, "
            f"got status={row['status']!r}"
        )

    def test_refs_field_populated_from_pr_reference(
        self, tmp_path: Path
    ) -> None:
        """
        When write_result text contains a PR reference, the result.json
        refs field must be populated with that reference.
        """
        from orchestration.wos_completion import maybe_complete_wos_uow

        db_path = tmp_path / "registry.db"
        Registry(db_path)  # initialize schema

        uow_id = "uow_artifacts_001"
        output_ref = str(tmp_path / f"{uow_id}.json")
        result_json = self._result_json_path(output_ref)
        result_json.write_text(json.dumps({"outcome": "complete", "success": True, "uow_id": uow_id}))

        self._seed_uow_at_executing(db_path, uow_id, output_ref)

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(
                task_id=f"wos-{uow_id}",
                status="success",
                result_text="PR #123 opened. See https://github.com/dcetlin/Lobster/pull/123",
            )

        content = result_json.read_text()
        data = json.loads(content)
        # refs field must exist and contain the PR #123 reference
        assert "refs" in data, f"result.json must have refs field, got keys: {list(data.keys())}"
        refs = data["refs"]
        assert "pr_numbers" in refs, f"refs must have pr_numbers, got: {refs}"
        assert 123 in refs["pr_numbers"], f"refs.pr_numbers must contain 123, got: {refs['pr_numbers']}"
