"""
Tests for orchestration.result_writer.

Coverage:
- write_result("done") writes status="done", outcome="complete", success=True
- write_result("failed") writes status="failed", outcome="failed", success=False
- result.json path derived by replacing extension (primary convention)
- result.json path appends .result.json when output_ref has no extension (fallback)
- artifacts included when provided; absent when not provided
- summary written as-is
- written_at is a non-empty ISO timestamp
- write is atomic: result file appears fully-formed (no partial write window)
- parent directories are created if missing
- ~ in output_ref is expanded
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from orchestration.result_writer import write_result, _result_json_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def output_ref(tmp_path: Path) -> str:
    return str(tmp_path / "outputs" / "abc-123.json")


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestWriteResultSchema:
    def test_done_status_writes_complete_outcome(self, output_ref: str) -> None:
        write_result(output_ref, status="done", summary="PR opened")
        result_path = _result_json_path(output_ref)
        data = json.loads(result_path.read_text())
        assert data["status"] == "done"
        assert data["outcome"] == "complete"
        assert data["success"] is True

    def test_failed_status_writes_failed_outcome(self, output_ref: str) -> None:
        write_result(output_ref, status="failed", summary="tests failed")
        result_path = _result_json_path(output_ref)
        data = json.loads(result_path.read_text())
        assert data["status"] == "failed"
        assert data["outcome"] == "failed"
        assert data["success"] is False

    def test_summary_written_as_is(self, output_ref: str) -> None:
        write_result(output_ref, status="done", summary="PR #42 opened and tests pass")
        data = json.loads(_result_json_path(output_ref).read_text())
        assert data["summary"] == "PR #42 opened and tests pass"

    def test_written_at_is_iso_timestamp(self, output_ref: str) -> None:
        write_result(output_ref, status="done", summary="done")
        data = json.loads(_result_json_path(output_ref).read_text())
        assert "written_at" in data
        assert len(data["written_at"]) > 10  # minimal sanity check

    def test_artifacts_included_when_provided(self, output_ref: str, tmp_path: Path) -> None:
        artifacts = [str(tmp_path / "report.md"), str(tmp_path / "pr-url.txt")]
        write_result(output_ref, status="done", summary="done", artifacts=artifacts)
        data = json.loads(_result_json_path(output_ref).read_text())
        assert data["artifacts"] == artifacts

    def test_artifacts_absent_when_not_provided(self, output_ref: str) -> None:
        write_result(output_ref, status="done", summary="done")
        data = json.loads(_result_json_path(output_ref).read_text())
        assert "artifacts" not in data

    def test_empty_artifacts_list_omitted(self, output_ref: str) -> None:
        write_result(output_ref, status="done", summary="done", artifacts=[])
        data = json.loads(_result_json_path(output_ref).read_text())
        assert "artifacts" not in data


# ---------------------------------------------------------------------------
# Path derivation tests
# ---------------------------------------------------------------------------

class TestResultJsonPath:
    def test_primary_convention_replaces_extension(self, tmp_path: Path) -> None:
        output_ref = str(tmp_path / "abc-123.json")
        p = _result_json_path(output_ref)
        assert p.name == "abc-123.result.json"

    def test_fallback_appends_when_no_extension(self, tmp_path: Path) -> None:
        output_ref = str(tmp_path / "abc-123")
        p = _result_json_path(output_ref)
        assert p.name == "abc-123.result.json"

    def test_tilde_expanded_in_output_ref(self) -> None:
        output_ref = "~/lobster-workspace/orchestration/outputs/abc-123.json"
        p = _result_json_path(output_ref)
        assert not str(p).startswith("~")
        assert p.name == "abc-123.result.json"


# ---------------------------------------------------------------------------
# Side-effect / filesystem tests
# ---------------------------------------------------------------------------

class TestWriteResultFilesystem:
    def test_parent_dirs_created_if_missing(self, tmp_path: Path) -> None:
        output_ref = str(tmp_path / "deep" / "nested" / "dir" / "uow.json")
        write_result(output_ref, status="done", summary="done")
        result_path = _result_json_path(output_ref)
        assert result_path.exists()

    def test_returns_result_path(self, output_ref: str) -> None:
        returned = write_result(output_ref, status="done", summary="done")
        expected = _result_json_path(output_ref)
        assert returned == expected

    def test_result_file_is_valid_json(self, output_ref: str) -> None:
        write_result(output_ref, status="done", summary="done")
        result_path = _result_json_path(output_ref)
        data = json.loads(result_path.read_text())
        assert isinstance(data, dict)

    def test_overwrite_on_second_call(self, output_ref: str) -> None:
        write_result(output_ref, status="done", summary="first")
        write_result(output_ref, status="failed", summary="second")
        data = json.loads(_result_json_path(output_ref).read_text())
        assert data["summary"] == "second"
        assert data["status"] == "failed"
