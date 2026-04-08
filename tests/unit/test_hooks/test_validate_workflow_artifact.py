"""
Unit tests for hooks/validate-workflow-artifact.py (S3-A, issue #678).

Tests cover:
- Valid artifact passes validation (exit 0)
- Invalid JSON is rejected (exit 2)
- Missing required fields are rejected (exit 2)
- Invalid executor_type is rejected (exit 2)
- Non-list prescribed_skills/constraints are rejected (exit 2)
- Non-string instructions is rejected (exit 2)
- Non-artifact Write paths are ignored (exit 0)
- Archived artifact paths are ignored (exit 0)
- Non-Write tool calls are ignored (exit 0)
"""

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path

import pytest


HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "validate-workflow-artifact.py"


def _load_hook():
    """Load validate-workflow-artifact.py as a fresh module for each test."""
    spec = importlib.util.spec_from_file_location("validate_workflow_artifact", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hook_input(
    tool_name: str = "Write",
    file_path: str = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc123.json",
    content: str = "",
) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {
            "file_path": file_path,
            "content": content,
        },
    }


def _valid_artifact(uow_id: str = "uow_abc123") -> dict:
    return {
        "uow_id": uow_id,
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": [],
        "instructions": "Do the thing.",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestValidArtifact:
    def test_valid_artifact_passes(self):
        """A fully valid WorkflowArtifact JSON returns exit 0."""
        mod = _load_hook()
        errors = mod._validate_artifact(
            json.dumps(_valid_artifact()), "/some/artifacts/uow_abc123.json"
        )
        assert errors == []

    def test_valid_artifact_with_functional_engineer_executor_type(self):
        mod = _load_hook()
        artifact = _valid_artifact()
        artifact["executor_type"] = "functional-engineer"
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow_abc.json")
        assert errors == []

    def test_valid_artifact_with_lobster_ops_executor_type(self):
        mod = _load_hook()
        artifact = _valid_artifact()
        artifact["executor_type"] = "lobster-ops"
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow_abc.json")
        assert errors == []


# ---------------------------------------------------------------------------
# Path filtering
# ---------------------------------------------------------------------------


class TestPathFiltering:
    def test_non_artifact_path_ignored(self):
        """Write to a non-artifact path returns exit 0 immediately."""
        mod = _load_hook()
        assert not mod._is_artifact_path("/home/lobster/some-other-file.json")

    def test_archived_path_ignored(self):
        """Write to orchestration/artifacts/archived/ is excluded — cleanup arc output."""
        mod = _load_hook()
        archived_path = "/home/lobster/lobster-workspace/orchestration/artifacts/archived/uow_abc123/output.json"
        assert not mod._is_artifact_path(archived_path)

    def test_active_artifact_path_matches(self):
        """Write to orchestration/artifacts/<uow_id>.json is validated."""
        mod = _load_hook()
        active_path = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc123.json"
        assert mod._is_artifact_path(active_path)

    def test_non_write_tool_ignored(self, monkeypatch, capsys):
        """Non-Write tool calls (e.g. Edit) always pass immediately."""
        mod = _load_hook()
        data = _hook_input(tool_name="Edit")
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_invalid_json_rejected(self):
        """Non-JSON content returns an error."""
        mod = _load_hook()
        errors = mod._validate_artifact("not json at all", "/some/artifacts/uow.json")
        assert len(errors) == 1
        assert "Invalid JSON" in errors[0]

    def test_missing_required_field_uow_id(self):
        """Missing uow_id is rejected."""
        mod = _load_hook()
        artifact = _valid_artifact()
        del artifact["uow_id"]
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow.json")
        assert any("uow_id" in e for e in errors)

    def test_missing_required_field_executor_type(self):
        """Missing executor_type is rejected."""
        mod = _load_hook()
        artifact = _valid_artifact()
        del artifact["executor_type"]
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow.json")
        assert any("executor_type" in e for e in errors)

    def test_missing_required_field_instructions(self):
        """Missing instructions is rejected."""
        mod = _load_hook()
        artifact = _valid_artifact()
        del artifact["instructions"]
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow.json")
        assert any("instructions" in e for e in errors)

    def test_missing_multiple_required_fields(self):
        """Multiple missing required fields are all reported."""
        mod = _load_hook()
        errors = mod._validate_artifact("{}", "/some/artifacts/uow.json")
        # All 5 required fields should be missing
        assert len(errors) >= 1
        assert any("missing required fields" in e for e in errors)

    def test_invalid_executor_type_rejected(self):
        """executor_type='unknown-executor' is not in the allowed set."""
        mod = _load_hook()
        artifact = _valid_artifact()
        artifact["executor_type"] = "unknown-executor"
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow.json")
        assert any("executor_type" in e or "Invalid" in e for e in errors)

    def test_prescribed_skills_must_be_list(self):
        """prescribed_skills as a string (not list) is rejected."""
        mod = _load_hook()
        artifact = _valid_artifact()
        artifact["prescribed_skills"] = "wos"  # Should be a list
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow.json")
        assert any("prescribed_skills" in e for e in errors)

    def test_constraints_must_be_list(self):
        """constraints as a dict (not list) is rejected."""
        mod = _load_hook()
        artifact = _valid_artifact()
        artifact["constraints"] = {"no-network": True}  # Should be a list
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow.json")
        assert any("constraints" in e for e in errors)

    def test_instructions_must_be_string(self):
        """instructions as a list (not string) is rejected."""
        mod = _load_hook()
        artifact = _valid_artifact()
        artifact["instructions"] = ["step 1", "step 2"]  # Should be a string
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow.json")
        assert any("instructions" in e for e in errors)

    def test_json_list_rejected(self):
        """JSON array at root is rejected."""
        mod = _load_hook()
        errors = mod._validate_artifact(
            '[{"uow_id": "x"}]', "/some/artifacts/uow.json"
        )
        assert any("JSON object" in e for e in errors)

    def test_unknown_extra_fields_are_silently_ignored(self):
        """Extra fields beyond required set are silently ignored (forward compat)."""
        mod = _load_hook()
        artifact = _valid_artifact()
        artifact["extra_field_from_future"] = "some value"
        errors = mod._validate_artifact(json.dumps(artifact), "/some/artifacts/uow.json")
        assert errors == []


# ---------------------------------------------------------------------------
# Integration: main() exit codes
# ---------------------------------------------------------------------------


class TestMainExitCode:
    def test_valid_artifact_write_exits_zero(self, monkeypatch):
        """main() returns 0 for a valid artifact Write."""
        mod = _load_hook()
        artifact_path = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc.json"
        data = _hook_input(
            tool_name="Write",
            file_path=artifact_path,
            content=json.dumps(_valid_artifact()),
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0

    def test_invalid_artifact_write_exits_two(self, monkeypatch, capsys):
        """main() returns 2 for an invalid artifact Write."""
        mod = _load_hook()
        artifact_path = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc.json"
        data = _hook_input(
            tool_name="Write",
            file_path=artifact_path,
            content='{"uow_id": "uow_abc"}',  # Missing required fields
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 2

    def test_non_artifact_path_exits_zero(self, monkeypatch):
        """main() returns 0 for a Write to a non-artifact path (not our concern)."""
        mod = _load_hook()
        data = _hook_input(
            tool_name="Write",
            file_path="/home/lobster/lobster/some-config.json",
            content='{"key": "value"}',
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0

    def test_archived_artifact_path_exits_zero(self, monkeypatch):
        """main() returns 0 for a Write to the archived/ subdirectory."""
        mod = _load_hook()
        data = _hook_input(
            tool_name="Write",
            file_path="/home/lobster/lobster-workspace/orchestration/artifacts/archived/uow_abc/output.json",
            content='{}',
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0
