"""
Tests for WOS executor failure handling improvements:

1. PATH fix — _build_claude_env() must include ~/.local/bin in PATH so the
   claude binary is found when running from cron (which strips the user's PATH).

2. CalledProcessError capture — when _dispatch_via_claude_p raises
   CalledProcessError, the stderr/stdout must be included in the result.json
   summary so future analysis can diagnose "agent logic failed" vs
   "authentication error" vs "network error" etc.

Tests are derived from the failure modes observed in 872 result files
(issue: 19.8% failure rate, dominant cause CalledProcessError with lost stderr).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Constants matching the spec
CLAUDE_LOCAL_BIN_DIR = str(Path.home() / ".local" / "bin")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def registry(db_path: Path):
    from orchestration.registry import Registry
    return Registry(db_path)


def _make_artifact(uow_id: str, instructions: str = "Implement the thing") -> str:
    from orchestration.workflow_artifact import WorkflowArtifact, to_json
    artifact: WorkflowArtifact = {
        "uow_id": uow_id,
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": [],
        "instructions": instructions,
    }
    return to_json(artifact)


def _insert_uow(db_path: Path, uow_id: str, workflow_artifact: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, workflow_artifact, estimated_runtime
            ) VALUES (?, 'executable', 'test', 'ready-for-executor', 'solo', ?, ?, 'Test UoW', 'done', ?, NULL)
            """,
            (uow_id, now, now, workflow_artifact),
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


def _read_result_json(output_ref: str) -> dict:
    from orchestration.executor import _result_json_path
    result_path = _result_json_path(output_ref)
    return json.loads(result_path.read_text())


# ===========================================================================
# PR 1: PATH fix — _build_claude_env() must include ~/.local/bin
# ===========================================================================

class TestBuildClaudeEnvIncludesLocalBin:
    """
    _build_claude_env() must include ~/.local/bin in PATH.

    When cron invokes the executor, the inherited PATH is typically just
    /usr/bin:/bin — the user's ~/.local/bin (where `claude` lives) is absent.
    The fix augments PATH in the returned env dict so the claude subprocess
    can be found regardless of how the executor was invoked.
    """

    def test_local_bin_present_when_cron_strips_path(self, monkeypatch):
        """
        Simulates cron: PATH is only /usr/bin:/bin.
        After _build_claude_env(), PATH must include ~/.local/bin.
        """
        from orchestration.steward import _build_claude_env
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-test")

        env = _build_claude_env()
        path_entries = env.get("PATH", "").split(":")
        assert CLAUDE_LOCAL_BIN_DIR in path_entries, (
            f"~/.local/bin must be in PATH for cron environments. "
            f"Got: {env.get('PATH')}"
        )

    def test_local_bin_not_duplicated_when_already_present(self, monkeypatch):
        """
        When ~/.local/bin is already in PATH, it must not be duplicated.
        """
        from orchestration.steward import _build_claude_env
        existing_path = f"{CLAUDE_LOCAL_BIN_DIR}:/usr/bin:/bin"
        monkeypatch.setenv("PATH", existing_path)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-test")

        env = _build_claude_env()
        path_entries = env.get("PATH", "").split(":")
        count = path_entries.count(CLAUDE_LOCAL_BIN_DIR)
        assert count == 1, (
            f"~/.local/bin must appear exactly once in PATH, got {count} times. "
            f"PATH: {env.get('PATH')}"
        )

    def test_local_bin_prepended_not_appended(self, monkeypatch):
        """
        ~/.local/bin must be early in PATH (prepended or at position 0 or 1)
        so the user's claude binary takes precedence over any system-installed one.
        """
        from orchestration.steward import _build_claude_env
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-test")

        env = _build_claude_env()
        path_entries = env.get("PATH", "").split(":")
        idx = path_entries.index(CLAUDE_LOCAL_BIN_DIR)
        assert idx <= 1, (
            f"~/.local/bin should be among the first entries in PATH "
            f"(got index {idx}). PATH: {env.get('PATH')}"
        )

    def test_fast_path_also_includes_local_bin(self, monkeypatch):
        """
        Even on the fast path (CLAUDE_CODE_OAUTH_TOKEN already set),
        _build_claude_env() must augment PATH.
        """
        from orchestration.steward import _build_claude_env
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "already-set")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        env = _build_claude_env()
        path_entries = env.get("PATH", "").split(":")
        assert CLAUDE_LOCAL_BIN_DIR in path_entries


# ===========================================================================
# PR 2: CalledProcessError stderr/stdout captured in result.json
# ===========================================================================

class TestCalledProcessErrorSummaryIncludesStderr:
    """
    When _dispatch_via_claude_p raises CalledProcessError, the result.json
    summary must include stderr/stdout from the subprocess.

    The failing pattern: executor logged the error but the result.json only
    contained "executor error before subagent dispatch: CalledProcessError: ..."
    — no stderr, making post-hoc failure diagnosis impossible.

    The fix: in _run_step_sequence's exception handler, detect CalledProcessError
    and include its stderr/stdout in the result.json summary.
    """

    def test_result_summary_includes_stderr_on_called_process_error(
        self, registry, db_path: Path
    ):
        """
        When the dispatcher raises CalledProcessError with stderr,
        the result.json summary must contain that stderr text.
        """
        from orchestration.executor import Executor, _result_json_path

        uow_id = "uow_cpe_stderr_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        expected_stderr = "Claude: authentication failed: token expired"

        def failing_dispatcher(instructions: str, uid: str) -> str:
            exc = subprocess.CalledProcessError(
                returncode=1,
                cmd=["claude", "-p", "..."],
                output="",  # stdout
                stderr=expected_stderr,
            )
            raise exc

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(subprocess.CalledProcessError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result = _read_result_json(output_ref)
        assert expected_stderr in result["summary"], (
            f"stderr must appear in result summary for diagnosability. "
            f"summary was: {result['summary']!r}"
        )

    def test_result_summary_includes_stdout_when_stderr_empty(
        self, registry, db_path: Path
    ):
        """
        When stderr is empty but stdout has content, stdout must be in the summary.
        Some claude failures write diagnostics to stdout not stderr.
        """
        from orchestration.executor import Executor, _result_json_path

        uow_id = "uow_cpe_stdout_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        expected_stdout = "Error: max retries exceeded connecting to API"

        def failing_dispatcher(instructions: str, uid: str) -> str:
            exc = subprocess.CalledProcessError(
                returncode=2,
                cmd=["claude", "-p", "..."],
                output=expected_stdout,  # stdout
                stderr="",
            )
            raise exc

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(subprocess.CalledProcessError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result = _read_result_json(output_ref)
        assert expected_stdout in result["summary"], (
            f"stdout must appear in result summary when stderr is empty. "
            f"summary was: {result['summary']!r}"
        )

    def test_result_summary_includes_exit_code(
        self, registry, db_path: Path
    ):
        """
        The exit code from CalledProcessError must be captured in the summary.
        This allows distinguishing auth failures (exit 1) from logic failures (exit 2+).
        """
        from orchestration.executor import Executor, _result_json_path

        uow_id = "uow_cpe_exitcode_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            exc = subprocess.CalledProcessError(
                returncode=42,
                cmd=["claude", "-p", "..."],
                output="",  # stdout
                stderr="some error",
            )
            raise exc

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(subprocess.CalledProcessError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result = _read_result_json(output_ref)
        # Exit code 42 must appear somewhere in the summary
        assert "42" in result["summary"], (
            f"Exit code must appear in result summary. "
            f"summary was: {result['summary']!r}"
        )

    def test_result_summary_still_written_when_no_stderr(
        self, registry, db_path: Path
    ):
        """
        When CalledProcessError has no stderr/stdout, summary must still be
        written (no regression from current behavior).
        """
        from orchestration.executor import Executor, _result_json_path

        uow_id = "uow_cpe_nosrc_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["claude", "-p", "..."],
            )

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(subprocess.CalledProcessError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        result = _read_result_json(output_ref)
        assert result["outcome"] == "failed"
        assert result["success"] is False

    def test_non_called_process_error_summary_unchanged(
        self, registry, db_path: Path
    ):
        """
        For non-CalledProcessError exceptions (e.g. FileNotFoundError),
        the summary format must not regress — it still uses str(exc).
        """
        from orchestration.executor import Executor, _result_json_path

        uow_id = "uow_other_exc_001"
        _insert_uow(db_path, uow_id, _make_artifact(uow_id))

        def failing_dispatcher(instructions: str, uid: str) -> str:
            raise FileNotFoundError(2, "No such file or directory: 'claude'")

        executor = Executor(registry, dispatcher=failing_dispatcher)
        with pytest.raises(FileNotFoundError):
            executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        result = _read_result_json(output_ref)
        assert result["outcome"] == "failed"
        # The error type name must appear in the summary
        assert "FileNotFoundError" in result["summary"]
