"""
Unit tests for the wos:executing ↔ wos:paused bulk label swap.

Behavior under test:

bulk_swap_executing_to_paused:
- Calls gh issue edit --remove-label wos:executing --add-label wos:paused for each issue
- Returns (success_count, failure_count) counting per-issue outcomes
- A single gh CLI failure does not abort the loop — remaining issues are processed
- Returns (0, 0) immediately for an empty issue_numbers list

bulk_swap_paused_to_executing:
- Calls the reverse gh issue edit for each issue
- Returns (success_count, failure_count) counting per-issue outcomes
- A single gh CLI failure does not abort the loop — remaining issues are processed
- Returns (0, 0) immediately for an empty issue_numbers list

handle_wos_stop:
- Includes label swap stats in its return message
- Includes label_swap key in the log_control_event payload

handle_wos_start:
- Includes label restore stats in its return message (normal start path)
- Includes label restore stats in its return message (partial-recovery path)
- Includes label_swap key in the log_control_event payload

Named constants mirror spec values for self-documenting test failures.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.wos_issue_lifecycle import (
    WOS_EXECUTING_LABEL,
    WOS_PAUSED_LABEL,
    WOS_PAUSED_LABEL_COLOR,
    bulk_swap_executing_to_paused,
    bulk_swap_paused_to_executing,
    _ensure_wos_paused_label_exists,
)


# ---------------------------------------------------------------------------
# Named constants from spec
# ---------------------------------------------------------------------------

#: Issues per the spec description that were "executing when paused"
_TEST_ISSUE_NUMBERS = [100, 200, 300]
_TEST_REPO = "owner/test-repo"

#: How many issues should succeed in the happy path
_ALL_SUCCESS = len(_TEST_ISSUE_NUMBERS)
_NO_FAILURES = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout.encode()
    cp.stderr = stderr.encode()
    return cp


def _label_list_result(labels: list[str]) -> subprocess.CompletedProcess:
    import json
    return _make_completed_process(stdout=json.dumps([{"name": l} for l in labels]))


def _ok() -> subprocess.CompletedProcess:
    return _make_completed_process(returncode=0)


def _gh_error(msg: str = "API error") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, "gh", stderr=msg.encode())


# ---------------------------------------------------------------------------
# _ensure_wos_paused_label_exists
# ---------------------------------------------------------------------------

class TestEnsureWosPausedLabelExists:
    """Tests for the wos:paused label creation helper."""

    def test_label_already_exists_no_create_call(self) -> None:
        """When wos:paused exists on the repo, no create call is made."""
        list_result = _label_list_result([WOS_PAUSED_LABEL, "bug"])

        with patch("subprocess.run", return_value=list_result) as mock_run:
            result = _ensure_wos_paused_label_exists(_TEST_REPO)

        assert result is True
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert "label" in args
        assert "list" in args

    def test_label_missing_creates_with_correct_color(self) -> None:
        """When wos:paused is absent, creates it with WOS_PAUSED_LABEL_COLOR."""
        list_result = _label_list_result(["bug", "enhancement"])
        create_result = _ok()

        with patch("subprocess.run", side_effect=[list_result, create_result]) as mock_run:
            result = _ensure_wos_paused_label_exists(_TEST_REPO)

        assert result is True
        assert mock_run.call_count == 2
        create_args = mock_run.call_args_list[1][0][0]
        assert "label" in create_args
        assert "create" in create_args
        assert WOS_PAUSED_LABEL in create_args
        assert WOS_PAUSED_LABEL_COLOR in create_args

    def test_gh_list_failure_returns_false(self) -> None:
        with patch("subprocess.run", side_effect=_gh_error()):
            result = _ensure_wos_paused_label_exists(_TEST_REPO)
        assert result is False


# ---------------------------------------------------------------------------
# bulk_swap_executing_to_paused
# ---------------------------------------------------------------------------

class TestBulkSwapExecutingToPaused:
    """Tests for pause-time bulk label swap (wos:executing → wos:paused)."""

    def test_empty_list_returns_zero_zero(self) -> None:
        """No gh calls are made and (0, 0) is returned for an empty list."""
        with patch("subprocess.run") as mock_run:
            success, failure = bulk_swap_executing_to_paused([], repo=_TEST_REPO)

        assert (success, failure) == (0, 0)
        assert mock_run.call_count == 0

    def test_calls_correct_gh_command_for_each_issue(self) -> None:
        """Each issue gets a gh issue edit call to remove executing + add paused."""
        # First call: ensure paused label exists (label list)
        list_result = _label_list_result([WOS_PAUSED_LABEL])
        # Subsequent calls: one per issue
        issue_results = [_ok() for _ in _TEST_ISSUE_NUMBERS]

        with patch("subprocess.run", side_effect=[list_result] + issue_results) as mock_run:
            success, failure = bulk_swap_executing_to_paused(
                _TEST_ISSUE_NUMBERS, repo=_TEST_REPO
            )

        assert (success, failure) == (_ALL_SUCCESS, _NO_FAILURES)

        # Verify each issue edit call contains the right label operations
        # Call at index 0 is the label-list for _ensure_wos_paused_label_exists
        for idx, issue_number in enumerate(_TEST_ISSUE_NUMBERS):
            call_args = mock_run.call_args_list[idx + 1][0][0]
            assert "issue" in call_args
            assert "edit" in call_args
            assert str(issue_number) in call_args
            assert "--remove-label" in call_args
            assert WOS_EXECUTING_LABEL in call_args
            assert "--add-label" in call_args
            assert WOS_PAUSED_LABEL in call_args

    def test_single_failure_does_not_abort_loop(self) -> None:
        """A gh CLI failure on one issue does not stop processing of remaining issues."""
        list_result = _label_list_result([WOS_PAUSED_LABEL])
        # Second issue fails; first and third succeed
        side_effects = [
            list_result,
            _ok(),           # issue 100 — success
            _gh_error(),     # issue 200 — failure
            _ok(),           # issue 300 — success
        ]

        with patch("subprocess.run", side_effect=side_effects):
            success, failure = bulk_swap_executing_to_paused(
                _TEST_ISSUE_NUMBERS, repo=_TEST_REPO
            )

        assert success == 2
        assert failure == 1

    def test_all_failures_returns_zero_all_fail(self) -> None:
        """When all issues fail, success_count=0, failure_count=N."""
        list_result = _label_list_result([WOS_PAUSED_LABEL])
        error_side_effects = [list_result] + [_gh_error() for _ in _TEST_ISSUE_NUMBERS]

        with patch("subprocess.run", side_effect=error_side_effects):
            success, failure = bulk_swap_executing_to_paused(
                _TEST_ISSUE_NUMBERS, repo=_TEST_REPO
            )

        assert success == 0
        assert failure == _ALL_SUCCESS

    def test_unexpected_exception_counted_as_failure(self) -> None:
        """An unexpected non-subprocess exception is counted in failure_count."""
        list_result = _label_list_result([WOS_PAUSED_LABEL])

        with patch("subprocess.run", side_effect=[list_result, OSError("network error")]):
            success, failure = bulk_swap_executing_to_paused([42], repo=_TEST_REPO)

        assert success == 0
        assert failure == 1


# ---------------------------------------------------------------------------
# bulk_swap_paused_to_executing
# ---------------------------------------------------------------------------

class TestBulkSwapPausedToExecuting:
    """Tests for resume-time bulk label swap (wos:paused → wos:executing)."""

    def test_empty_list_returns_zero_zero(self) -> None:
        """No gh calls are made and (0, 0) is returned for an empty list."""
        with patch("subprocess.run") as mock_run:
            success, failure = bulk_swap_paused_to_executing([], repo=_TEST_REPO)

        assert (success, failure) == (0, 0)
        assert mock_run.call_count == 0

    def test_calls_reverse_gh_command_for_each_issue(self) -> None:
        """Each issue gets a gh issue edit call to remove paused + add executing."""
        # First call: ensure executing label exists (label list)
        list_result = _label_list_result([WOS_EXECUTING_LABEL])
        issue_results = [_ok() for _ in _TEST_ISSUE_NUMBERS]

        with patch("subprocess.run", side_effect=[list_result] + issue_results) as mock_run:
            success, failure = bulk_swap_paused_to_executing(
                _TEST_ISSUE_NUMBERS, repo=_TEST_REPO
            )

        assert (success, failure) == (_ALL_SUCCESS, _NO_FAILURES)

        for idx, issue_number in enumerate(_TEST_ISSUE_NUMBERS):
            call_args = mock_run.call_args_list[idx + 1][0][0]
            assert "issue" in call_args
            assert "edit" in call_args
            assert str(issue_number) in call_args
            assert "--remove-label" in call_args
            assert WOS_PAUSED_LABEL in call_args
            assert "--add-label" in call_args
            assert WOS_EXECUTING_LABEL in call_args

    def test_single_failure_does_not_abort_loop(self) -> None:
        """A gh CLI failure on one issue does not stop processing of remaining issues."""
        list_result = _label_list_result([WOS_EXECUTING_LABEL])
        side_effects = [
            list_result,
            _gh_error(),     # issue 100 — failure
            _ok(),           # issue 200 — success
            _ok(),           # issue 300 — success
        ]

        with patch("subprocess.run", side_effect=side_effects):
            success, failure = bulk_swap_paused_to_executing(
                _TEST_ISSUE_NUMBERS, repo=_TEST_REPO
            )

        assert success == 2
        assert failure == 1

    def test_all_failures_returns_zero_all_fail(self) -> None:
        """When all issues fail, success_count=0, failure_count=N."""
        list_result = _label_list_result([WOS_EXECUTING_LABEL])
        error_side_effects = [list_result] + [_gh_error() for _ in _TEST_ISSUE_NUMBERS]

        with patch("subprocess.run", side_effect=error_side_effects):
            success, failure = bulk_swap_paused_to_executing(
                _TEST_ISSUE_NUMBERS, repo=_TEST_REPO
            )

        assert success == 0
        assert failure == _ALL_SUCCESS

    def test_unexpected_exception_counted_as_failure(self) -> None:
        """An unexpected non-subprocess exception is counted in failure_count."""
        list_result = _label_list_result([WOS_EXECUTING_LABEL])

        with patch("subprocess.run", side_effect=[list_result, RuntimeError("timeout")]):
            success, failure = bulk_swap_paused_to_executing([42], repo=_TEST_REPO)

        assert success == 0
        assert failure == 1


# ---------------------------------------------------------------------------
# handle_wos_stop — label swap stats in response
# ---------------------------------------------------------------------------

class TestHandleWosStopLabelSwap:
    """Tests that handle_wos_stop includes label swap stats in its response."""

    def _make_mock_registry(self, executing_issue_numbers: list[int]) -> MagicMock:
        """Build a mock Registry with executing UoWs that have source_issue_numbers."""
        from unittest.mock import MagicMock

        mock_uow_list = []
        for n in executing_issue_numbers:
            u = MagicMock()
            u.source_issue_number = n
            mock_uow_list.append(u)

        reg = MagicMock()
        reg.list.return_value = mock_uow_list
        return reg

    def test_stop_includes_label_swap_line_in_response(self, tmp_path) -> None:
        """handle_wos_stop includes 'Labels swapped: N wos:executing → wos:paused' in response."""
        from orchestration.dispatcher_handlers import handle_wos_stop

        mock_reg = self._make_mock_registry([101, 202])

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value={"toggled": ["executor-heartbeat"], "not_found": []}),
            patch("orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=[]),
            patch("orchestration.dispatcher_handlers._bulk_swap_executing_to_paused",
                  return_value=(2, 0)) as mock_swap,
        ):
            response = handle_wos_stop(registry=mock_reg)

        assert "Labels swapped: 2 wos:executing → wos:paused" in response
        assert "(failures: 0)" in response
        mock_swap.assert_called_once_with([101, 202], repo="dcetlin/Lobster")

    def test_stop_includes_label_swap_in_control_event(self, tmp_path) -> None:
        """handle_wos_stop includes label_swap key in the log_control_event payload."""
        from orchestration.dispatcher_handlers import handle_wos_stop

        mock_reg = self._make_mock_registry([55])

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value={"toggled": [], "not_found": []}),
            patch("orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=[]),
            patch("orchestration.dispatcher_handlers._bulk_swap_executing_to_paused",
                  return_value=(1, 0)),
        ):
            handle_wos_stop(registry=mock_reg)

        mock_reg.log_control_event.assert_called_once()
        _, payload = mock_reg.log_control_event.call_args[0]
        assert "label_swap" in payload
        assert payload["label_swap"] == {"success": 1, "failed": 0}

    def test_stop_with_no_executing_issues_still_includes_label_line(self) -> None:
        """When no issues are executing, swap still reports '0 swapped'."""
        from orchestration.dispatcher_handlers import handle_wos_stop

        mock_reg = self._make_mock_registry([])

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value={"toggled": [], "not_found": []}),
            patch("orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=[]),
            patch("orchestration.dispatcher_handlers._bulk_swap_executing_to_paused",
                  return_value=(0, 0)),
        ):
            response = handle_wos_stop(registry=mock_reg)

        assert "Labels swapped: 0" in response


# ---------------------------------------------------------------------------
# handle_wos_start — label restore stats in response
# ---------------------------------------------------------------------------

class TestHandleWosStartLabelSwap:
    """Tests that handle_wos_start includes label restore stats in its response."""

    def _make_mock_registry(self, executing_issue_numbers: list[int]) -> MagicMock:
        mock_uow_list = []
        for n in executing_issue_numbers:
            u = MagicMock()
            u.source_issue_number = n
            mock_uow_list.append(u)

        reg = MagicMock()
        reg.list.return_value = mock_uow_list
        return reg

    def test_start_includes_label_restore_line_in_response(self) -> None:
        """handle_wos_start includes 'Labels restored: N wos:paused → wos:executing' in response."""
        from orchestration.dispatcher_handlers import handle_wos_start

        mock_reg = self._make_mock_registry([101, 202, 303])

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value={"toggled": ["executor-heartbeat"], "not_found": []}),
            patch("orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=[]),
            patch("orchestration.dispatcher_handlers._bulk_swap_paused_to_executing",
                  return_value=(3, 0)) as mock_swap,
        ):
            response = handle_wos_start(registry=mock_reg)

        assert "Labels restored: 3 wos:paused → wos:executing" in response
        assert "(failures: 0)" in response
        mock_swap.assert_called_once_with([101, 202, 303], repo="dcetlin/Lobster")

    def test_start_includes_label_swap_in_control_event(self) -> None:
        """handle_wos_start includes label_swap key in the log_control_event payload."""
        from orchestration.dispatcher_handlers import handle_wos_start

        mock_reg = self._make_mock_registry([77])

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value={"toggled": [], "not_found": []}),
            patch("orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=[]),
            patch("orchestration.dispatcher_handlers._bulk_swap_paused_to_executing",
                  return_value=(1, 0)),
        ):
            handle_wos_start(registry=mock_reg)

        mock_reg.log_control_event.assert_called_once()
        _, payload = mock_reg.log_control_event.call_args[0]
        assert "label_swap" in payload
        assert payload["label_swap"] == {"success": 1, "failed": 0}

    def test_start_partial_recovery_path_includes_label_restore(self) -> None:
        """Partial-recovery path of handle_wos_start also includes label restore stats."""
        from orchestration.dispatcher_handlers import handle_wos_start

        mock_reg = self._make_mock_registry([500])

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("orchestration.dispatcher_handlers.get_disabled_wos_core_jobs",
                  return_value=["issue-sweeper"]),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value={"toggled": ["issue-sweeper"], "not_found": []}),
            patch("orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=[]),
            patch("orchestration.dispatcher_handlers._bulk_swap_paused_to_executing",
                  return_value=(1, 0)) as mock_swap,
        ):
            response = handle_wos_start(registry=mock_reg)

        assert "Labels restored: 1 wos:paused → wos:executing" in response
        mock_swap.assert_called_once()

    def test_start_with_no_executing_issues_still_includes_label_line(self) -> None:
        """When no issues are executing, restore still reports '0 restored'."""
        from orchestration.dispatcher_handlers import handle_wos_start

        mock_reg = self._make_mock_registry([])

        with (
            patch("orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value={"toggled": [], "not_found": []}),
            patch("orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=[]),
            patch("orchestration.dispatcher_handlers._bulk_swap_paused_to_executing",
                  return_value=(0, 0)),
        ):
            response = handle_wos_start(registry=mock_reg)

        assert "Labels restored: 0" in response
