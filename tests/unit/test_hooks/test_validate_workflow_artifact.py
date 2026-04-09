"""
Unit tests for hooks/validate-workflow-artifact.py (S3P2-B, issue #613).

The hook validates WorkflowArtifact front-matter + prose format (.md) when
Claude writes to ~/lobster-workspace/orchestration/artifacts/*.md.

Exit code 2 causes Claude Code to surface the error to Claude, which must
correct and retry. Exit code 0 = pass or not our concern.

Disk format validated:
    ---json
    {"uow_id": "...", "executor_type": "...", "constraints": [], "prescribed_skills": []}
    ---
    <instructions prose>

Tests cover:
- Valid artifact passes (exit 0)
- Non-Write tool calls are ignored (exit 0)
- Non-artifact Write paths are ignored (exit 0)
- Archived artifact paths are ignored (exit 0)
- Legacy .json artifact paths are ignored (exit 0) — not our concern
- Missing ---json opener is rejected (exit 2)
- Missing closing --- is rejected (exit 2)
- Invalid JSON in envelope is rejected (exit 2)
- Missing required envelope fields are rejected (exit 2)
- Invalid executor_type is rejected (exit 2)
- Non-list prescribed_skills/constraints are rejected (exit 2)
- Valid executor_type values: general, functional-engineer, lobster-ops
- Unknown extra fields in envelope are silently ignored
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
    file_path: str = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc123.md",
    content: str = "",
) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {
            "file_path": file_path,
            "content": content,
        },
    }


def _valid_artifact_content(uow_id: str = "uow_abc123") -> str:
    """Return a valid front-matter + prose artifact string."""
    envelope = json.dumps({
        "uow_id": uow_id,
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": [],
    }, separators=(",", ":"))
    return f"---json\n{envelope}\n---\nDo the thing."


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestValidArtifact:
    def test_valid_artifact_passes(self):
        """A fully valid WorkflowArtifact front-matter returns no errors."""
        mod = _load_hook()
        errors = mod._validate_artifact(
            _valid_artifact_content(), "/some/artifacts/uow_abc123.md"
        )
        assert errors == []

    def test_valid_artifact_with_functional_engineer_executor_type(self):
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "functional-engineer",
            "constraints": [],
            "prescribed_skills": [],
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nImplement the feature."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert errors == []

    def test_valid_artifact_with_lobster_ops_executor_type(self):
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "lobster-ops",
            "constraints": [],
            "prescribed_skills": [],
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nRun the ops task."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert errors == []

    def test_valid_artifact_with_general_executor_type(self):
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "general",
            "constraints": [],
            "prescribed_skills": [],
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nGeneral task."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert errors == []

    def test_unknown_extra_fields_in_envelope_are_silently_ignored(self):
        """Extra keys in the JSON envelope are ignored for forward compatibility."""
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "general",
            "constraints": [],
            "prescribed_skills": [],
            "future_field": "some_value",
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert errors == []

    def test_multiline_instructions_are_accepted(self):
        """Instructions prose can span multiple lines."""
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "general",
            "constraints": [],
            "prescribed_skills": [],
        }, separators=(",", ":"))
        instructions = "Step 1: Do this.\nStep 2: Do that.\nStep 3: Verify."
        content = f"---json\n{envelope}\n---\n{instructions}"
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert errors == []


# ---------------------------------------------------------------------------
# Path filtering
# ---------------------------------------------------------------------------

class TestPathFiltering:
    def test_non_artifact_path_not_matched(self):
        """Write to a non-artifact path is not our concern."""
        mod = _load_hook()
        assert not mod._is_artifact_path("/home/lobster/some-other-file.md")

    def test_json_artifact_path_not_matched(self):
        """Legacy .json artifact files are NOT validated by this hook."""
        mod = _load_hook()
        json_path = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc123.json"
        assert not mod._is_artifact_path(json_path)

    def test_archived_path_not_matched(self):
        """Write to orchestration/artifacts/archived/ is excluded — cleanup arc output."""
        mod = _load_hook()
        archived_path = "/home/lobster/lobster-workspace/orchestration/artifacts/archived/uow_abc123/output.md"
        assert not mod._is_artifact_path(archived_path)

    def test_active_md_artifact_path_matches(self):
        """Write to orchestration/artifacts/<uow_id>.md is validated."""
        mod = _load_hook()
        active_path = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc123.md"
        assert mod._is_artifact_path(active_path)

    def test_non_write_tool_ignored(self, monkeypatch):
        """Non-Write tool calls (e.g. Edit) always pass immediately."""
        mod = _load_hook()
        data = _hook_input(tool_name="Edit")
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0

    def test_non_artifact_path_exits_zero(self, monkeypatch):
        """main() returns 0 for a Write to a non-artifact path."""
        mod = _load_hook()
        data = _hook_input(
            tool_name="Write",
            file_path="/home/lobster/lobster/some-config.json",
            content="{}",
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0

    def test_archived_artifact_path_exits_zero(self, monkeypatch):
        """main() returns 0 for a Write to the archived/ subdirectory."""
        mod = _load_hook()
        data = _hook_input(
            tool_name="Write",
            file_path="/home/lobster/lobster-workspace/orchestration/artifacts/archived/uow_abc/output.md",
            content="{}",
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0

    def test_json_artifact_path_exits_zero(self, monkeypatch):
        """main() returns 0 for a Write to a legacy .json path (not our concern)."""
        mod = _load_hook()
        data = _hook_input(
            tool_name="Write",
            file_path="/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc.json",
            content='{"key": "value"}',
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0


# ---------------------------------------------------------------------------
# Front-matter structure validation
# ---------------------------------------------------------------------------

class TestFrontMatterStructure:
    def test_missing_json_opener_rejected(self):
        """Content without ---json opener is rejected."""
        mod = _load_hook()
        content = '{"uow_id": "uow_abc", "executor_type": "general", "constraints": [], "prescribed_skills": []}'
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert len(errors) >= 1
        assert any("---json" in e for e in errors)

    def test_bare_yaml_opener_rejected(self):
        """Content with bare --- opener (LLM stdout format) is rejected — wrong format."""
        mod = _load_hook()
        content = "---\nexecutor_type: general\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert len(errors) >= 1
        assert any("---json" in e for e in errors)

    def test_missing_closing_delimiter_rejected(self):
        """Content with ---json opener but no closing --- is rejected."""
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "general",
            "constraints": [],
            "prescribed_skills": [],
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\nInstructions without closing delimiter."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert len(errors) >= 1
        assert any("---" in e for e in errors)

    def test_invalid_json_in_envelope_rejected(self):
        """Non-JSON on the envelope line is rejected."""
        mod = _load_hook()
        content = "---json\nnot valid json at all\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert len(errors) >= 1
        assert any("Invalid JSON" in e or "JSON" in e for e in errors)

    def test_json_array_envelope_rejected(self):
        """JSON array on the envelope line is rejected — must be an object."""
        mod = _load_hook()
        content = '---json\n[{"uow_id": "uow_abc"}]\n---\nInstructions.'
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert len(errors) >= 1
        assert any("object" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Envelope field validation
# ---------------------------------------------------------------------------

class TestEnvelopeFieldValidation:
    def test_missing_uow_id_rejected(self):
        """Missing uow_id in envelope is rejected."""
        mod = _load_hook()
        envelope = json.dumps({
            "executor_type": "general",
            "constraints": [],
            "prescribed_skills": [],
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert any("uow_id" in e for e in errors)

    def test_missing_executor_type_rejected(self):
        """Missing executor_type in envelope is rejected."""
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "constraints": [],
            "prescribed_skills": [],
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert any("executor_type" in e for e in errors)

    def test_missing_multiple_required_fields_rejected(self):
        """Missing multiple required fields are all reported."""
        mod = _load_hook()
        content = "---json\n{}\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert len(errors) >= 1
        assert any("missing required fields" in e for e in errors)

    def test_invalid_executor_type_rejected(self):
        """executor_type not in the valid set is rejected."""
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "unknown-executor",
            "constraints": [],
            "prescribed_skills": [],
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert any("executor_type" in e or "Invalid" in e for e in errors)

    def test_prescribed_skills_must_be_list(self):
        """prescribed_skills as a string (not list) is rejected."""
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "general",
            "constraints": [],
            "prescribed_skills": "wos",
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert any("prescribed_skills" in e for e in errors)

    def test_constraints_must_be_list(self):
        """constraints as a dict (not list) is rejected."""
        mod = _load_hook()
        envelope = json.dumps({
            "uow_id": "uow_abc",
            "executor_type": "general",
            "constraints": {"no-network": True},
            "prescribed_skills": [],
        }, separators=(",", ":"))
        content = f"---json\n{envelope}\n---\nInstructions."
        errors = mod._validate_artifact(content, "/some/artifacts/uow_abc.md")
        assert any("constraints" in e for e in errors)


# ---------------------------------------------------------------------------
# Integration: main() exit codes
# ---------------------------------------------------------------------------

class TestMainExitCode:
    def test_valid_artifact_write_exits_zero(self, monkeypatch):
        """main() returns 0 for a valid artifact Write."""
        mod = _load_hook()
        artifact_path = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc.md"
        data = _hook_input(
            tool_name="Write",
            file_path=artifact_path,
            content=_valid_artifact_content(),
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 0

    def test_invalid_artifact_write_exits_two(self, monkeypatch, capsys):
        """main() returns 2 for an invalid artifact Write."""
        mod = _load_hook()
        artifact_path = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc.md"
        # Missing ---json opener
        data = _hook_input(
            tool_name="Write",
            file_path=artifact_path,
            content='{"uow_id": "uow_abc"}',
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 2

    def test_missing_required_fields_exits_two(self, monkeypatch):
        """main() returns 2 when required envelope fields are missing."""
        mod = _load_hook()
        artifact_path = "/home/lobster/lobster-workspace/orchestration/artifacts/uow_abc.md"
        content = '---json\n{"uow_id": "uow_abc"}\n---\nInstructions.'
        data = _hook_input(
            tool_name="Write",
            file_path=artifact_path,
            content=content,
        )
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps(data)))
        result = mod.main()
        assert result == 2
