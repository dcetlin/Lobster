"""
Unit tests for wos_completion.py — G2 gap from Sprint 4 test harness design.

Covers: maybe_complete_wos_uow — the deferred execution_complete transition for
the async inbox dispatch path.

Behavior under test:
- task_id not starting with "wos-" → no-op (non-WOS task)
- status != "success" → no-op (only successes advance the UoW)
- UoW not found in registry → no-op (logs and returns)
- DB not found → no-op (no WOS install or test env)
- UoW in "executing" + status="success" → transitions to "ready-for-steward"
- UoW not in "executing" status → skipped silently (duplicate write_result or
  TTL recovery already handled it)
- Registry error → logs warning, does not raise

Named constants mirror the names in wos_completion.py to anchor tests to the spec.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.wos_completion import (
    WOS_TASK_ID_PREFIX,
    WRITE_RESULT_SUCCESS_STATUS,
    _backpropagate_result_to_output_file,
    _build_closeout_comment,
    _extract_github_issue,
    classify_uow_output,
    maybe_complete_wos_uow,
)
from orchestration.registry import Registry, UoWStatus, UpsertInserted


# ---------------------------------------------------------------------------
# Constants — named after spec values so failures are self-documenting
# ---------------------------------------------------------------------------

_NON_WOS_TASK_ID = "some-other-task-123"
_WOS_TASK_ID_FOR_UNKNOWN = f"{WOS_TASK_ID_PREFIX}does-not-exist"
_FAILURE_STATUS = "error"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_uow_at_status(registry: Registry, target_status: str, output_dir: Path) -> str:
    """
    Seed a UoW and advance it to the given status using set_status_direct.

    Uses direct status manipulation to keep the test helper independent of
    the Executor's internal claim logic. The output_ref is written via a direct
    SQL UPDATE when needed for the executing path.

    Returns the uow_id.
    """
    import sqlite3

    result = registry.upsert(
        issue_number=9901,
        title="Completion test UoW",
        success_criteria="maybe_complete_wos_uow transitions it",
    )
    assert isinstance(result, UpsertInserted)
    uow_id = result.id

    registry.approve(uow_id)

    if target_status == "executing":
        # To call transition_to_executing we need the UoW to be in 'active' first.
        # Set output_ref directly so complete_uow has a valid value to use.
        output_ref = str(output_dir / f"{uow_id}.json")
        registry.set_status_direct(uow_id, "active")
        # Write output_ref directly — bypasses Executor internal logic for test isolation
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute(
            "UPDATE uow_registry SET output_ref = ? WHERE id = ?",
            (output_ref, uow_id),
        )
        conn.commit()
        conn.close()
        registry.transition_to_executing(uow_id, "mock-executor-001")
    elif target_status not in ("pending",):
        registry.set_status_direct(uow_id, target_status)

    return uow_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMaybeCompleteWosUow:
    """Behavioral tests for maybe_complete_wos_uow."""

    def test_non_wos_task_id_is_ignored(self, tmp_path: Path) -> None:
        """
        A task_id that does not start with WOS_TASK_ID_PREFIX must not touch
        the registry — this is the primary filtering gate.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            # No exception raised, registry untouched
            maybe_complete_wos_uow(_NON_WOS_TASK_ID, WRITE_RESULT_SUCCESS_STATUS)

        # DB was created by Registry() init, but no UoW rows should exist
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM uow_registry").fetchone()[0]
        conn.close()
        assert count == 0, "Non-WOS task_id must not create any UoW records"

    def test_error_status_does_not_advance_executing_uow(self, tmp_path: Path) -> None:
        """
        A write_result with status="error" must leave the UoW in 'executing'.

        Only successful completions trigger the executing → ready-for-steward
        transition. Failed write_results leave the UoW for TTL recovery.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, _FAILURE_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.EXECUTING, (
            f"Error write_result must leave UoW in executing, got {uow.status}"
        )

    def test_executing_uow_with_success_transitions_to_ready_for_steward(
        self, tmp_path: Path
    ) -> None:
        """
        Core behavior: a UoW in 'executing' status advances to 'ready-for-steward'
        when write_result arrives with status='success'.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"Executing UoW + success write_result must reach ready-for-steward, "
            f"got {uow.status}"
        )

    def test_execution_complete_audit_entry_is_written(self, tmp_path: Path) -> None:
        """
        After a successful completion, the audit_log must contain an
        'execution_complete' event for the UoW.
        """
        import sqlite3

        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        events = [
            row["event"]
            for row in conn.execute(
                "SELECT event FROM audit_log WHERE uow_id = ?", (uow_id,)
            ).fetchall()
        ]
        conn.close()

        assert "execution_complete" in events, (
            f"audit_log must contain 'execution_complete' after write_result success. "
            f"Found events: {events}"
        )

    def test_uow_not_found_is_silently_skipped(self, tmp_path: Path) -> None:
        """
        A WOS task_id that has no matching UoW in the registry must be skipped
        without raising an exception.
        """
        db_path = tmp_path / "registry.db"
        Registry(db_path)  # Initialize DB schema

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            # Must not raise
            maybe_complete_wos_uow(_WOS_TASK_ID_FOR_UNKNOWN, WRITE_RESULT_SUCCESS_STATUS)

    def test_uow_already_ready_for_steward_is_skipped(self, tmp_path: Path) -> None:
        """
        If the UoW is already in 'ready-for-steward' (e.g. TTL recovery already
        advanced it), a duplicate write_result must not change the status.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "ready-for-steward", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            # Must not raise, must not change status
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"Duplicate write_result on non-executing UoW must not change status, "
            f"got {uow.status}"
        )

    def test_uow_in_done_status_is_skipped(self, tmp_path: Path) -> None:
        """
        A UoW already in 'done' status must be silently skipped (not double-transitioned).
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        # Manually advance to done to simulate prior completion
        registry.set_status_direct(uow_id, "done")

        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status.value == "done", (
            f"Done UoW must not be transitioned by duplicate write_result, "
            f"got {uow.status}"
        )

    def test_missing_db_does_not_raise(self, tmp_path: Path) -> None:
        """
        When the registry DB does not exist (no WOS install), maybe_complete_wos_uow
        must return silently without raising.
        """
        nonexistent_db = tmp_path / "no_such.db"
        task_id = f"{WOS_TASK_ID_PREFIX}uow_20260101_abc123"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(nonexistent_db)}):
            # Must not raise
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

    def test_duplicate_success_write_result_does_not_double_transition(
        self, tmp_path: Path
    ) -> None:
        """
        Calling maybe_complete_wos_uow twice with the same task_id and status=success
        must be idempotent: the second call is silently skipped because the UoW is
        already in 'ready-for-steward'.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)
            # Second call — must not raise, must not change status further
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"Idempotency violated: status after duplicate call should be "
            f"ready-for-steward, got {uow.status}"
        )

    def test_registry_exception_does_not_propagate(self, tmp_path: Path) -> None:
        """
        If the registry raises an unexpected exception, maybe_complete_wos_uow must
        log a warning and return — write_result delivery must not be blocked by
        registry update failures.
        """
        db_path = tmp_path / "registry.db"

        task_id = f"{WOS_TASK_ID_PREFIX}uow_20260101_abc123"

        # wos_completion.py imports Registry lazily inside the function via
        # "from orchestration.registry import Registry", so we patch at the
        # source module level to intercept the instantiation.
        with patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}), \
             patch("orchestration.registry.Registry", side_effect=RuntimeError("test error")):
            # Must not raise
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS)

    def test_wos_task_id_prefix_constant_is_correct(self) -> None:
        """
        The WOS_TASK_ID_PREFIX constant must match the naming convention used by
        route_wos_message in dispatcher_handlers.py ("wos-").
        """
        assert WOS_TASK_ID_PREFIX == "wos-", (
            f"WOS_TASK_ID_PREFIX must be 'wos-', got {WOS_TASK_ID_PREFIX!r}"
        )

    def test_write_result_success_status_constant_is_correct(self) -> None:
        """
        The WRITE_RESULT_SUCCESS_STATUS constant must match the status string
        sent by a completing subagent ("success").
        """
        assert WRITE_RESULT_SUCCESS_STATUS == "success", (
            f"WRITE_RESULT_SUCCESS_STATUS must be 'success', got {WRITE_RESULT_SUCCESS_STATUS!r}"
        )


# ---------------------------------------------------------------------------
# classify_uow_output — pure function, no I/O
# ---------------------------------------------------------------------------

class TestClassifyUowOutput:
    """Behavioral tests for the output classification heuristic."""

    def test_pull_request_mention_is_pearl(self) -> None:
        result = classify_uow_output(output_ref=None, result_text="Opened PR #42 on dcetlin/Lobster.")
        assert result == "pearl"

    def test_pull_request_long_form_is_pearl(self) -> None:
        result = classify_uow_output(output_ref=None, result_text="Created a pull request with the fix.")
        assert result == "pearl"

    def test_merged_keyword_is_pearl(self) -> None:
        result = classify_uow_output(output_ref=None, result_text="PR merged successfully.")
        assert result == "pearl"

    def test_nothing_to_do_is_heat(self) -> None:
        result = classify_uow_output(output_ref=None, result_text="Nothing to do — issue is already resolved.")
        assert result == "heat"

    def test_no_changes_is_heat(self) -> None:
        result = classify_uow_output(output_ref=None, result_text="No changes required in the codebase.")
        assert result == "heat"

    def test_skipped_is_heat(self) -> None:
        result = classify_uow_output(output_ref=None, result_text="Skipped — precondition not met.")
        assert result == "heat"

    def test_empty_result_defaults_to_seed(self) -> None:
        result = classify_uow_output(output_ref=None, result_text=None)
        assert result == "seed"

    def test_analysis_without_pr_defaults_to_seed(self) -> None:
        result = classify_uow_output(
            output_ref=None,
            result_text="Completed analysis and wrote design doc to output_ref.",
        )
        assert result == "seed"


# ---------------------------------------------------------------------------
# _extract_github_issue — pure function
# ---------------------------------------------------------------------------

class TestExtractGithubIssue:
    """Behavioral tests for GitHub issue source parsing."""

    def test_github_issue_source_returns_repo_and_number(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_WOS_REPO": "dcetlin/Lobster"}):
            result = _extract_github_issue("github:issue/123")
        assert result == ("dcetlin/Lobster", 123)

    def test_telegram_source_returns_none(self) -> None:
        result = _extract_github_issue("telegram")
        assert result is None

    def test_system_source_returns_none(self) -> None:
        result = _extract_github_issue("system")
        assert result is None

    def test_empty_source_returns_none(self) -> None:
        result = _extract_github_issue("")
        assert result is None

    def test_malformed_github_source_returns_none(self) -> None:
        result = _extract_github_issue("github:issue/not-a-number")
        assert result is None

    def test_uses_lobster_wos_repo_env_var(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_WOS_REPO": "myorg/myrepo"}):
            result = _extract_github_issue("github:issue/42")
        assert result == ("myorg/myrepo", 42)

    def test_defaults_to_dcetlin_lobster_when_env_absent(self) -> None:
        env_without_repo = {k: v for k, v in os.environ.items() if k != "LOBSTER_WOS_REPO"}
        with patch.dict(os.environ, env_without_repo, clear=True):
            result = _extract_github_issue("github:issue/7")
        assert result is not None
        assert result[0] == "dcetlin/Lobster"
        assert result[1] == 7


# ---------------------------------------------------------------------------
# _build_closeout_comment — pure builder
# ---------------------------------------------------------------------------

class TestBuildCloseoutComment:
    """The close-out comment body follows the spec template exactly."""

    def test_comment_contains_uow_id(self) -> None:
        body = _build_closeout_comment(
            uow_id="uow_20260422_abc123",
            output_classification="pearl",
            result_text="PR #99 opened.",
            agent_type="functional-engineer",
            date_str="2026-04-22",
        )
        assert "uow_20260422_abc123" in body

    def test_comment_contains_output_type(self) -> None:
        body = _build_closeout_comment(
            uow_id="uow_x",
            output_classification="seed",
            result_text="Analysis complete.",
            agent_type="operational",
            date_str="2026-04-22",
        )
        assert "**Output type:** seed" in body

    def test_comment_contains_agent_type_and_date(self) -> None:
        body = _build_closeout_comment(
            uow_id="uow_x",
            output_classification="heat",
            result_text="Nothing to do.",
            agent_type="frontier-writer",
            date_str="2026-04-22",
        )
        assert "frontier-writer" in body
        assert "2026-04-22" in body

    def test_comment_contains_result_json_reference(self) -> None:
        body = _build_closeout_comment(
            uow_id="uow_abc",
            output_classification="pearl",
            result_text="Done.",
            agent_type="functional-engineer",
            date_str="2026-01-01",
        )
        assert "uow_abc.result.json" in body

    def test_long_result_text_is_truncated(self) -> None:
        long_text = "x" * 400
        body = _build_closeout_comment(
            uow_id="uow_x",
            output_classification="seed",
            result_text=long_text,
            agent_type="operational",
            date_str="2026-01-01",
        )
        # The body must not contain the full 400-char string verbatim
        assert long_text not in body


# ---------------------------------------------------------------------------
# Close-out protocol integration — source-routing and gh invocation
# ---------------------------------------------------------------------------

class TestCloseoutProtocolIntegration:
    """
    Verifies that maybe_complete_wos_uow posts a close-out comment to the
    source GitHub issue when source="github:issue/N", and skips silently for
    non-GitHub sources.
    """

    def _seed_uow_with_source(
        self,
        registry: Registry,
        output_dir: Path,
        source: str,
        issue_number: int = 9901,
    ) -> str:
        """Seed a UoW with a given source and advance to executing."""
        result = registry.upsert(
            issue_number=issue_number,
            title="Close-out test UoW",
            success_criteria="Closeout comment posted.",
        )
        assert isinstance(result, UpsertInserted)
        uow_id = result.id
        registry.approve(uow_id)

        output_ref = str(output_dir / f"{uow_id}.json")
        registry.set_status_direct(uow_id, "active")
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute(
            "UPDATE uow_registry SET output_ref = ?, source = ? WHERE id = ?",
            (output_ref, source, uow_id),
        )
        conn.commit()
        conn.close()
        registry.transition_to_executing(uow_id, "mock-executor-001")
        return uow_id

    def test_github_source_posts_comment(self, tmp_path: Path) -> None:
        """
        When source="github:issue/42", at least one gh issue comment subprocess is
        spawned with the correct repo and issue number.

        Note: after the lifecycle stamp addition (#874), GitHub sources produce
        multiple gh calls (close-out comment + lifecycle stamp). This test verifies
        that at least one comment reaches the right issue/repo — not the exact count.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = self._seed_uow_with_source(
            registry, output_dir, source="github:issue/42", issue_number=42
        )
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch("orchestration.wos_completion.subprocess.run") as mock_run, \
             patch.dict(os.environ, {
                 "REGISTRY_DB_PATH": str(db_path),
                 "LOBSTER_WOS_REPO": "dcetlin/Lobster",
             }):
            mock_run.return_value = MagicMock(returncode=0)
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS, result_text="PR #7 opened.")

        # gh must have been called at least once (close-out comment + lifecycle stamp)
        assert mock_run.called, "subprocess.run must be called for GitHub sources"

        # At least one call must be a comment targeting the correct issue/repo
        comment_calls = [
            c for c in mock_run.call_args_list
            if "comment" in c[0][0] and "42" in c[0][0] and "dcetlin/Lobster" in c[0][0]
        ]
        assert len(comment_calls) >= 1, (
            "At least one gh issue comment must target issue 42 in dcetlin/Lobster"
        )

    def test_telegram_source_skips_comment(self, tmp_path: Path) -> None:
        """
        When source="telegram", no subprocess is spawned — close-out comment
        is silently skipped for non-GitHub sources.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = self._seed_uow_with_source(
            registry, output_dir, source="telegram", issue_number=9901
        )
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch("orchestration.wos_completion.subprocess.run") as mock_run, \
             patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS, result_text="Done.")

        mock_run.assert_not_called()

    def test_gh_failure_does_not_block_registry_transition(self, tmp_path: Path) -> None:
        """
        If the gh subprocess fails (non-zero exit), the UoW must still transition
        to ready-for-steward — comment failure is non-fatal.
        """
        import subprocess as sp

        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = self._seed_uow_with_source(
            registry, output_dir, source="github:issue/99", issue_number=99
        )
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch("orchestration.wos_completion.subprocess.run",
                   side_effect=sp.CalledProcessError(1, "gh", stderr=b"error")), \
             patch.dict(os.environ, {
                 "REGISTRY_DB_PATH": str(db_path),
                 "LOBSTER_WOS_REPO": "dcetlin/Lobster",
             }):
            # Must not raise even when gh fails
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS, result_text="Done.")

        # Registry must have transitioned despite comment failure
        uow = registry.get(uow_id)
        assert uow is not None
        assert uow.status == UoWStatus.READY_FOR_STEWARD, (
            f"Registry transition must succeed even when gh comment fails. Got: {uow.status}"
        )

    def test_system_source_skips_comment(self, tmp_path: Path) -> None:
        """source='system' must not trigger a gh comment."""
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = self._seed_uow_with_source(
            registry, output_dir, source="system", issue_number=9901
        )
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with patch("orchestration.wos_completion.subprocess.run") as mock_run, \
             patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS, result_text="Done.")

        mock_run.assert_not_called()

    def test_result_text_with_pr_produces_pearl_classification_in_comment(
        self, tmp_path: Path
    ) -> None:
        """
        result_text containing a PR reference must flow through to the gh comment
        body with output_type 'pearl'.

        This verifies the end-to-end wiring: write_result text → maybe_complete_wos_uow
        result_text → classify_uow_output → comment body. Before this fix, inbox_server.py
        called maybe_complete_wos_uow without result_text, so classification always
        defaulted to 'seed' regardless of the actual subagent output.
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = self._seed_uow_with_source(
            registry, output_dir, source="github:issue/55", issue_number=55
        )
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        captured_comment_body: list[str] = []

        def _capture_run(cmd, **kwargs):
            # Capture the --body argument passed to gh issue comment
            body_idx = cmd.index("--body") + 1 if "--body" in cmd else None
            if body_idx is not None:
                captured_comment_body.append(cmd[body_idx])
            return MagicMock(returncode=0)

        with patch("orchestration.wos_completion.subprocess.run", side_effect=_capture_run), \
             patch.dict(os.environ, {
                 "REGISTRY_DB_PATH": str(db_path),
                 "LOBSTER_WOS_REPO": "dcetlin/Lobster",
             }):
            maybe_complete_wos_uow(
                task_id,
                WRITE_RESULT_SUCCESS_STATUS,
                result_text="PR #123 opened on dcetlin/Lobster.",
            )

        # After lifecycle stamp addition (#874), GitHub sources produce two comment calls:
        # 1. close-out comment with "**Output type:**" (from _post_closeout_comment_if_github)
        # 2. lifecycle stamp comment with "**Outcome:**" (from stamp_issue_complete)
        # Verify the close-out comment (the one with "**Output type:** pearl") is present.
        assert len(captured_comment_body) >= 1, "Expected at least one gh comment call"
        closeout_body = next(
            (b for b in captured_comment_body if "**Output type:**" in b),
            None,
        )
        assert closeout_body is not None, (
            "A close-out comment with '**Output type:**' must be posted for GitHub sources"
        )
        assert "**Output type:** pearl" in closeout_body, (
            f"result_text mentioning a PR must produce 'pearl' classification in the "
            f"close-out comment. Got close-out comment body:\n{closeout_body}"
        )

    def test_seed_classified_result_closes_issue(self, tmp_path: Path) -> None:
        """
        A UoW whose result_text classifies as 'seed' (the default — no PR mention,
        no heat signals) must still close the source GitHub issue.

        Rationale: metabolic classification (seed/pearl/heat) describes what the UoW
        PRODUCED, not whether the source issue was ADDRESSED. A seed UoW that filed a
        follow-up absolutely addressed the source issue. Lifecycle decisions must be
        driven by completion status (success vs fail), not by output category.

        Before this fix, seed-classified results called stamp_issue_unverifiable,
        leaving the issue OPEN. This test confirms the corrected behavior: any
        successful completion triggers stamp_issue_complete (which closes the issue).
        """
        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = self._seed_uow_with_source(
            registry, output_dir, source="github:issue/77", issue_number=77
        )
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        close_calls: list[list[str]] = []

        def _capture_run(cmd, **kwargs):
            # Track 'gh issue close' calls
            if "close" in cmd:
                close_calls.append(list(cmd))
            return MagicMock(returncode=0)

        # result_text has no PR mention and no heat signals → classifies as 'seed'
        seed_result_text = "Filed follow-up issue #88 for remaining work."

        with patch("orchestration.wos_completion.subprocess.run", side_effect=_capture_run), \
             patch.dict(os.environ, {
                 "REGISTRY_DB_PATH": str(db_path),
                 "LOBSTER_WOS_REPO": "dcetlin/Lobster",
             }):
            maybe_complete_wos_uow(
                task_id,
                WRITE_RESULT_SUCCESS_STATUS,
                result_text=seed_result_text,
            )

        # The source issue must be closed regardless of seed classification.
        assert len(close_calls) >= 1, (
            "A seed-classified UoW must still close the source GitHub issue. "
            "Metabolic category (seed/pearl/heat) does not determine whether the "
            "issue was addressed — completion status does."
        )
        # Confirm the close targeted the correct issue number
        assert any("77" in str(call) for call in close_calls), (
            f"gh issue close must target issue 77. Close calls observed: {close_calls}"
        )


# ---------------------------------------------------------------------------
# Back-propagation — issue #867
# ---------------------------------------------------------------------------

class TestBackpropagateResultToOutputFile:
    """
    Unit tests for _backpropagate_result_to_output_file (issue #867).

    This function writes a minimal conforming result.json when the subagent
    did not write one, so the Steward can verify UoW completion.
    """

    def test_writes_result_json_when_missing(self, tmp_path: Path) -> None:
        """
        When no result.json exists, a conforming file must be written with
        outcome='complete', success=True, and the uow_id matching the UoW.
        """
        output_ref = str(tmp_path / "abc123.json")
        result_text = "Opened PR #42 on dcetlin/Lobster."

        _backpropagate_result_to_output_file("abc123", output_ref, result_text)

        result_path = tmp_path / "abc123.result.json"
        assert result_path.exists(), "result.json must be created by back-propagation"
        payload = json.loads(result_path.read_text())
        assert payload["uow_id"] == "abc123"
        assert payload["outcome"] == "complete"
        assert payload["success"] is True
        assert payload["executor_id"] == "write_result_backprop"
        assert result_text[:500] in payload.get("reason", "")

    def test_does_not_overwrite_existing_result_json(self, tmp_path: Path) -> None:
        """
        When result.json already exists (written by the subagent), back-propagation
        must leave it untouched — the existing file has higher fidelity.
        """
        output_ref = str(tmp_path / "abc456.json")
        result_path = tmp_path / "abc456.result.json"
        original_payload = {
            "uow_id": "abc456",
            "outcome": "partial",
            "success": False,
            "reason": "only 3 of 5 steps completed",
        }
        result_path.write_text(json.dumps(original_payload))

        _backpropagate_result_to_output_file("abc456", output_ref, "some text")

        payload = json.loads(result_path.read_text())
        assert payload == original_payload, (
            "_backpropagate_result_to_output_file must not overwrite an existing result file"
        )

    def test_no_op_when_output_ref_is_empty(self, tmp_path: Path) -> None:
        """
        When output_ref is empty, the function must return silently without writing
        any result.json or primary output file.
        """
        # The function must not write any result file — verify no .result.json is created
        result_path = tmp_path / "abc789.result.json"
        primary_path = tmp_path / "abc789.json"
        _backpropagate_result_to_output_file("abc789", "", "some text")
        assert not result_path.exists(), "No result.json must be written for empty output_ref"
        assert not primary_path.exists(), "No primary output must be written for empty output_ref"

    def test_writes_result_text_to_primary_output_when_missing(self, tmp_path: Path) -> None:
        """
        When the primary output_ref file is missing (never written, not even a
        sentinel), the result_text must be written there so agent-status.sh
        has human-readable content.
        """
        output_ref = str(tmp_path / "abc999.json")
        result_text = "Task completed — no changes needed."

        _backpropagate_result_to_output_file("abc999", output_ref, result_text)

        primary_path = tmp_path / "abc999.json"
        assert primary_path.exists(), "Primary output file must be written when missing"
        assert primary_path.read_text() == result_text

    def test_writes_result_text_to_primary_output_when_sentinel_zero_bytes(
        self, tmp_path: Path
    ) -> None:
        """
        The executor writes a zero-byte sentinel to output_ref at dispatch time
        to guard against crashed_output_ref_missing detection. Back-propagation
        must overwrite it with the actual result text.
        """
        output_ref = str(tmp_path / "sentinel.json")
        sentinel_path = tmp_path / "sentinel.json"
        sentinel_path.write_text("")  # zero-byte sentinel
        result_text = "Work done."

        _backpropagate_result_to_output_file("sentinel", output_ref, result_text)

        assert sentinel_path.read_text() == result_text, (
            "Zero-byte sentinel must be replaced by result_text"
        )

    def test_result_json_conforms_to_executor_contract(self, tmp_path: Path) -> None:
        """
        The synthetic result.json must contain all required fields from
        executor-contract.md: uow_id, outcome, success. Optional fields
        (reason, executor_id) improve observability but are not contractually
        required here — this test validates required fields only.
        """
        output_ref = str(tmp_path / "contract_uow.json")
        _backpropagate_result_to_output_file("contract_uow", output_ref, "Done.")

        result_path = tmp_path / "contract_uow.result.json"
        payload = json.loads(result_path.read_text())
        assert "uow_id" in payload, "result.json must have uow_id"
        assert "outcome" in payload, "result.json must have outcome"
        assert "success" in payload, "result.json must have success"

    def test_maybe_complete_wos_uow_writes_result_json_via_backprop(
        self, tmp_path: Path
    ) -> None:
        """
        End-to-end: when a WOS subagent calls write_result and no result.json
        exists, maybe_complete_wos_uow must create one via back-propagation
        so the Steward can verify completion.
        """
        import sqlite3

        db_path = tmp_path / "registry.db"
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        registry = Registry(db_path)

        uow_id = _seed_uow_at_status(registry, "executing", output_dir)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"
        result_text = "PR #55 opened."

        with patch("orchestration.wos_completion.subprocess.run"), \
             patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}):
            maybe_complete_wos_uow(task_id, WRITE_RESULT_SUCCESS_STATUS, result_text=result_text)

        uow = registry.get(uow_id)
        output_ref = uow.output_ref
        assert output_ref, "UoW must have output_ref after execution"

        result_path = Path(output_ref).with_suffix(".result.json")
        assert result_path.exists(), (
            "result.json must be created by back-propagation when the subagent "
            "did not write one itself"
        )
        payload = json.loads(result_path.read_text())
        assert payload["uow_id"] == uow_id
        assert payload["outcome"] == "complete"
        assert payload["success"] is True
