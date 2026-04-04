"""
Unit tests for the WOS V3 Germinator — register classification at germination.

Tests cover:
- Gate 1: machine-executable gate command → operational
- Gate 2: iterative convergence signal → iterative-convergent
- Gate 3: philosophical vocabulary → philosophical
- Gate 4: hedge words in success_criteria → human-judgment
- Default: no gate fires → operational
- Gate ordering: Gate 1 takes precedence over Gate 3
- Empty body → falls through to Gate 4 / default
- Register is written to UoW at upsert time
"""

from __future__ import annotations

import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def import_germinator():
    """Ensure src is on sys.path for all tests in this module."""
    import sys
    repo_root = Path(__file__).parent.parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


# ---------------------------------------------------------------------------
# Gate 1: machine-executable gate command → operational
# ---------------------------------------------------------------------------

class TestGate1MachineExecutable:
    def test_pytest_command_in_body(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Fix broken test",
            body="Run `pytest tests/unit/` and confirm all pass.",
            success_criteria="All tests pass.",
        )
        assert result.register == "operational"
        assert result.gate_matched == "1"
        assert result.confidence == "high"

    def test_uv_run_command_in_body(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Run linter",
            body="Execute `uv run ruff check src/` and fix any issues.",
            success_criteria="No ruff warnings.",
        )
        assert result.register == "operational"
        assert result.gate_matched == "1"

    def test_fenced_bash_block(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Add index",
            body="```bash\nsqlite3 registry.db 'CREATE INDEX ...'\n```",
            success_criteria="Index created.",
        )
        assert result.register == "operational"
        assert result.gate_matched == "1"

    def test_gh_pr_command(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Merge PR",
            body="Use `gh pr merge 123` to merge the fix.",
            success_criteria="PR merged.",
        )
        assert result.register == "operational"
        assert result.gate_matched == "1"


# ---------------------------------------------------------------------------
# Gate 2: iterative convergence → iterative-convergent
# ---------------------------------------------------------------------------

class TestGate2Iterative:
    def test_fix_all_tests(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Fix all failing tests",
            body="Run pytest and fix all test failures until the suite is passing.",
            success_criteria="All tests passing.",
        )
        assert result.register == "iterative-convergent"
        assert result.gate_matched == "2"
        assert result.confidence == "high"

    def test_100_percent_coverage(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Reach 100% test coverage",
            body="Run pytest --cov until coverage reaches 100%.",
            success_criteria="100% passing.",
        )
        assert result.register == "iterative-convergent"
        assert result.gate_matched == "2"

    def test_mypy_clean(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Make mypy clean",
            body="Run `uv run mypy src/` and fix errors until mypy is clean.",
            success_criteria="Zero mypy errors.",
        )
        assert result.register == "iterative-convergent"
        assert result.gate_matched == "2"

    def test_gate1_without_iteration_is_operational(self):
        from src.orchestration.germinator import classify_register
        # Gate 1 fires but no iteration signal → operational (not iterative)
        result = classify_register(
            title="Write migration",
            body="Create a SQL migration and run `uv run migrate.py`.",
            success_criteria="Migration applied.",
        )
        assert result.register == "operational"
        assert result.gate_matched == "1"


# ---------------------------------------------------------------------------
# Gate 3: philosophical vocabulary → philosophical
# ---------------------------------------------------------------------------

class TestGate3Philosophical:
    def test_poiesis_in_body(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Explore poiesis in the Steward",
            body="The Steward's diagnostic act is itself a form of poiesis.",
            success_criteria="Synthesis document written.",
        )
        assert result.register == "philosophical"
        assert result.gate_matched == "3"

    def test_frontier_in_title(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Update frontier doc on register",
            body="Synthesize the session into a frontier document.",
            success_criteria="Frontier doc updated.",
        )
        assert result.register == "philosophical"
        assert result.gate_matched == "3"

    def test_phenomenology_in_body(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Session synthesis",
            body="From a phenomenological standpoint, the clearing precedes presence.",
            success_criteria="Notes written.",
        )
        assert result.register == "philosophical"
        assert result.gate_matched == "3"

    def test_philosophy_session_origin(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Philosophy session output",
            body="This issue originates from a philosophy session on attunement.",
            success_criteria="Archive entry created.",
        )
        assert result.register == "philosophical"
        assert result.gate_matched == "3"

    def test_gate1_takes_precedence_over_gate3(self):
        from src.orchestration.germinator import classify_register
        # Gate 1 fires even when philosophical vocabulary is present
        result = classify_register(
            title="Fix poiesis module tests",
            body="Run `pytest tests/unit/test_poiesis.py` and fix failures.",
            success_criteria="Tests pass.",
        )
        assert result.register == "operational"
        assert result.gate_matched == "1"


# ---------------------------------------------------------------------------
# Gate 4: human-judgment signal in success_criteria
# ---------------------------------------------------------------------------

class TestGate4HumanJudgment:
    def test_hedge_word_appropriate(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Improve error messages",
            body="The error messages should be clearer.",
            success_criteria="Error messages are appropriate for the context.",
        )
        assert result.register == "human-judgment"
        assert result.gate_matched == "4"

    def test_hedge_word_improve(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Improve steward notes",
            body="Refactor the steward notes format.",
            success_criteria="Notes are improved and easier to read.",
        )
        assert result.register == "human-judgment"
        assert result.gate_matched == "4"

    def test_empty_success_criteria_is_human_judgment(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Review architecture",
            body="Review the current system architecture.",
            success_criteria="",
        )
        assert result.register == "human-judgment"
        assert result.gate_matched == "4"

    def test_clear_criteria_is_operational_default(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Write log entry",
            body="Write a timestamped log entry to logs/audit.log.",
            success_criteria="Log entry written to logs/audit.log.",
        )
        assert result.register == "operational"
        assert result.gate_matched == "default"


# ---------------------------------------------------------------------------
# Default: no gate fires → operational
# ---------------------------------------------------------------------------

class TestDefault:
    def test_plain_task_defaults_to_operational(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Add missing field to dataclass",
            body="Add the `source_ref` field to the UoW dataclass.",
            success_criteria="UoW dataclass has source_ref field.",
        )
        assert result.register == "operational"
        assert result.gate_matched == "default"
        assert result.confidence == "low"

    def test_rationale_is_non_empty(self):
        from src.orchestration.germinator import classify_register
        result = classify_register(
            title="Rename variable",
            body="Rename x to more_descriptive_name.",
            success_criteria="Variable renamed.",
        )
        assert result.rationale
        assert len(result.rationale) > 10


# ---------------------------------------------------------------------------
# RegisterClassification is frozen/immutable
# ---------------------------------------------------------------------------

class TestClassificationImmutability:
    def test_classification_is_frozen(self):
        from src.orchestration.germinator import classify_register
        result = classify_register("title", "body", "criteria")
        with pytest.raises((AttributeError, TypeError)):
            result.register = "philosophical"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration: register flows into Registry.upsert
# ---------------------------------------------------------------------------

class TestRegisterFlowsToRegistry:
    def test_upsert_stores_germinator_register(self, tmp_path):
        import sqlite3
        from src.orchestration.registry import Registry
        from src.orchestration.germinator import classify_register

        db_path = tmp_path / "test.db"
        registry = Registry(db_path)

        # Classify as iterative-convergent
        reg = classify_register(
            title="Fix all test failures",
            body="Run pytest and fix all failures until passing.",
            success_criteria="All tests passing.",
        )
        assert reg.register == "iterative-convergent"

        result = registry.upsert(
            issue_number=42,
            title="Fix all test failures",
            success_criteria="All tests passing.",
            register=reg.register,
        )
        from src.orchestration.registry import UpsertInserted
        assert isinstance(result, UpsertInserted)

        # Verify register was persisted
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT register, uow_mode FROM uow_registry WHERE id = ?",
            (result.id,),
        ).fetchone()
        conn.close()

        assert row["register"] == "iterative-convergent"
        assert row["uow_mode"] == "iterative-convergent"

    def test_uow_value_object_carries_register(self, tmp_path):
        from src.orchestration.registry import Registry

        db_path = tmp_path / "test.db"
        registry = Registry(db_path)

        registry.upsert(
            issue_number=99,
            title="Philosophical exploration",
            success_criteria="Synthesis written.",
            register="philosophical",
        )

        uow = registry.get(registry.list(status="proposed")[0].id)
        assert uow is not None
        assert uow.register == "philosophical"
        assert uow.uow_mode == "philosophical"
