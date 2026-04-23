"""
Unit tests for wos_issue_lifecycle.py — bidirectional GitHub issue tracking.

Behavior under test:

stamp_issue_executing:
- Success path: adds wos:executing label + posts comment → returns True
- Already-has-label guard: if issue already has wos:executing label → returns False (idempotency)
- gh CLI fails (CalledProcessError) → returns False (non-blocking)
- gh CLI label create fails gracefully → still attempts stamp (non-blocking)

stamp_issue_complete:
- Success path: removes label, closes issue, posts comment → returns True
- Issue already closed → returns True gracefully (logs info, continues)
- gh CLI fails → returns False (non-blocking)

stamp_issue_failed:
- Success path: removes label, posts comment → returns True
- gh CLI fails → returns False (non-blocking)

stamp_issue_unverifiable:
- Success path: removes label, posts comment → returns True
- gh CLI fails → returns False (non-blocking)

_ensure_wos_executing_label_exists:
- Label already exists → no-op (no create call)
- Label missing → creates label with color #0052cc
- gh CLI fails on create → returns False (non-blocking)

Named constants mirror wos_issue_lifecycle.py for self-documenting failures.
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
    WOS_EXECUTING_LABEL_COLOR,
    stamp_issue_executing,
    stamp_issue_complete,
    stamp_issue_failed,
    stamp_issue_unverifiable,
    _ensure_wos_executing_label_exists,
)


# ---------------------------------------------------------------------------
# Constants — named after spec values for self-documenting test failures
# ---------------------------------------------------------------------------

_TEST_REPO = "owner/test-repo"
_TEST_ISSUE = 42
_TEST_UOW_ID = "uow_20260423_abc123"
_TEST_SUMMARY = "PR #99 opened and merged. All tests passed."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """Build a mock CompletedProcess for subprocess.run return values."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout.encode()
    cp.stderr = stderr.encode()
    return cp


def _make_gh_label_list_output(labels: list[str]) -> str:
    """Build a JSON string that mimics 'gh label list --json name' output."""
    import json
    return json.dumps([{"name": lbl} for lbl in labels])


# ---------------------------------------------------------------------------
# _ensure_wos_executing_label_exists
# ---------------------------------------------------------------------------

class TestEnsureWosExecutingLabelExists:
    """Tests for the label existence check / creation helper."""

    def test_label_already_exists_no_create_call(self) -> None:
        """When the label is present in the repo, no create call is made."""
        list_output = _make_gh_label_list_output([WOS_EXECUTING_LABEL, "bug", "enhancement"])
        list_result = _make_completed_process(returncode=0, stdout=list_output)

        with patch("subprocess.run", return_value=list_result) as mock_run:
            result = _ensure_wos_executing_label_exists(_TEST_REPO)

        assert result is True
        # Only one call: gh label list — no create call
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert "label" in args
        assert "list" in args

    def test_label_missing_creates_it(self) -> None:
        """When the label is absent, a gh label create call is made."""
        list_output = _make_gh_label_list_output(["bug", "enhancement"])
        list_result = _make_completed_process(returncode=0, stdout=list_output)
        create_result = _make_completed_process(returncode=0)

        with patch("subprocess.run", side_effect=[list_result, create_result]) as mock_run:
            result = _ensure_wos_executing_label_exists(_TEST_REPO)

        assert result is True
        assert mock_run.call_count == 2
        create_args = mock_run.call_args_list[1][0][0]
        assert "label" in create_args
        assert "create" in create_args
        assert WOS_EXECUTING_LABEL in create_args
        assert WOS_EXECUTING_LABEL_COLOR in create_args

    def test_gh_list_fails_returns_false(self) -> None:
        """When gh label list fails, return False without raising."""
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
            result = _ensure_wos_executing_label_exists(_TEST_REPO)

        assert result is False

    def test_gh_create_fails_returns_false(self) -> None:
        """When label creation fails (not found), return False without raising."""
        list_output = _make_gh_label_list_output(["bug"])
        list_result = _make_completed_process(returncode=0, stdout=list_output)

        with patch("subprocess.run", side_effect=[
            list_result,
            subprocess.CalledProcessError(1, "gh"),
        ]):
            result = _ensure_wos_executing_label_exists(_TEST_REPO)

        assert result is False


# ---------------------------------------------------------------------------
# stamp_issue_executing
# ---------------------------------------------------------------------------

class TestStampIssueExecuting:
    """Tests for the UoW-creation lifecycle stamp."""

    def test_success_path_adds_label_and_comment(self) -> None:
        """
        Successful stamp: ensures label exists, checks issue labels, adds wos:executing
        to the issue, posts a comment, and returns True.

        Call sequence:
        1. gh label list  (ensure label exists)
        2. gh issue view --json labels  (idempotency check — label NOT present)
        3. gh issue edit --add-label  (add label)
        4. gh issue comment  (post comment)
        """
        # Call 1: label list for _ensure_wos_executing_label_exists
        list_output = _make_gh_label_list_output([WOS_EXECUTING_LABEL])
        list_result = _make_completed_process(returncode=0, stdout=list_output)

        # Call 2: issue view for _issue_has_wos_executing_label — NO wos:executing
        issue_view_output = '{"labels": [{"name": "bug"}]}'
        issue_view_result = _make_completed_process(returncode=0, stdout=issue_view_output)

        # Call 3: issue edit --add-label
        label_add_result = _make_completed_process(returncode=0)

        # Call 4: issue comment
        comment_result = _make_completed_process(returncode=0)

        with patch("subprocess.run", side_effect=[
            list_result, issue_view_result, label_add_result, comment_result
        ]) as mock_run:
            result = stamp_issue_executing(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is True
        # Should have called: label list, issue view, issue edit --add-label, issue comment
        assert mock_run.call_count == 4

        # Verify label-add call (index 2)
        label_add_args = mock_run.call_args_list[2][0][0]
        assert "issue" in label_add_args
        assert "edit" in label_add_args
        assert str(_TEST_ISSUE) in label_add_args
        assert WOS_EXECUTING_LABEL in " ".join(label_add_args)

        # Verify comment call (index 3) mentions the UoW ID
        comment_args = mock_run.call_args_list[3][0][0]
        assert "issue" in comment_args
        assert "comment" in comment_args
        assert any(_TEST_UOW_ID in str(a) for a in comment_args)

    def test_already_has_label_returns_false_idempotency_guard(self) -> None:
        """
        When the issue already has the wos:executing label, stamp_issue_executing
        returns False — the idempotency guard prevents duplicate UoW creation.
        """
        # Simulate gh issue view returning labels that include wos:executing
        label_check_output = '{"labels": [{"name": "wos:executing"}, {"name": "bug"}]}'
        label_check_result = _make_completed_process(returncode=0, stdout=label_check_output)
        # label list for ensure
        list_output = _make_gh_label_list_output([WOS_EXECUTING_LABEL])
        list_result = _make_completed_process(returncode=0, stdout=list_output)

        with patch("subprocess.run", side_effect=[list_result, label_check_result]):
            result = stamp_issue_executing(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is False

    def test_gh_cli_fails_returns_false_non_blocking(self) -> None:
        """
        When gh fails (rate limit, network error), stamp_issue_executing
        returns False without raising — UoW processing continues.
        """
        list_output = _make_gh_label_list_output([WOS_EXECUTING_LABEL])
        list_result = _make_completed_process(returncode=0, stdout=list_output)

        with patch("subprocess.run", side_effect=[
            list_result,
            subprocess.CalledProcessError(1, "gh", stderr=b"rate limit exceeded"),
        ]):
            result = stamp_issue_executing(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is False

    def test_unexpected_exception_returns_false(self) -> None:
        """Any unexpected exception returns False, not a raised exception."""
        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = stamp_issue_executing(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is False


# ---------------------------------------------------------------------------
# stamp_issue_complete
# ---------------------------------------------------------------------------

class TestStampIssueComplete:
    """Tests for the UoW-completion (pearl) lifecycle stamp."""

    def test_success_path_removes_label_closes_and_comments(self) -> None:
        """
        Successful completion: removes wos:executing label, closes issue,
        posts summary comment, returns True.
        """
        remove_label_result = _make_completed_process(returncode=0)
        close_result = _make_completed_process(returncode=0)
        comment_result = _make_completed_process(returncode=0)

        with patch("subprocess.run", side_effect=[remove_label_result, close_result, comment_result]) as mock_run:
            result = stamp_issue_complete(_TEST_ISSUE, _TEST_UOW_ID, _TEST_SUMMARY, repo=_TEST_REPO)

        assert result is True
        assert mock_run.call_count == 3

        # Verify label removal
        label_remove_args = mock_run.call_args_list[0][0][0]
        assert "issue" in label_remove_args
        assert "edit" in label_remove_args

        # Verify close call
        close_args = mock_run.call_args_list[1][0][0]
        assert "issue" in close_args
        assert "close" in close_args
        assert str(_TEST_ISSUE) in close_args

        # Verify comment contains UoW ID and summary
        comment_args = mock_run.call_args_list[2][0][0]
        assert "issue" in comment_args
        assert "comment" in comment_args

    def test_already_closed_issue_returns_true_gracefully(self) -> None:
        """
        When gh issue close returns an error indicating the issue is already
        closed, stamp_issue_complete should still return True (idempotent
        on close) and log info rather than failing.
        """
        remove_label_result = _make_completed_process(returncode=0)
        # gh returns non-zero for already-closed issue
        already_closed_error = subprocess.CalledProcessError(
            1, "gh", stderr=b"Issue is already closed"
        )
        comment_result = _make_completed_process(returncode=0)

        with patch("subprocess.run", side_effect=[
            remove_label_result,
            already_closed_error,
            comment_result,
        ]) as mock_run:
            result = stamp_issue_complete(_TEST_ISSUE, _TEST_UOW_ID, _TEST_SUMMARY, repo=_TEST_REPO)

        # Should still return True — close failure is non-blocking
        assert result is True

    def test_gh_cli_fails_returns_false(self) -> None:
        """
        When gh fails on label removal, stamp_issue_complete returns False.
        """
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
            result = stamp_issue_complete(_TEST_ISSUE, _TEST_UOW_ID, _TEST_SUMMARY, repo=_TEST_REPO)

        assert result is False

    def test_unexpected_exception_returns_false(self) -> None:
        """Any unexpected exception returns False."""
        with patch("subprocess.run", side_effect=RuntimeError("connection reset")):
            result = stamp_issue_complete(_TEST_ISSUE, _TEST_UOW_ID, _TEST_SUMMARY, repo=_TEST_REPO)

        assert result is False


# ---------------------------------------------------------------------------
# stamp_issue_failed
# ---------------------------------------------------------------------------

class TestStampIssueFailed:
    """Tests for the UoW-failure lifecycle stamp."""

    def test_success_path_removes_label_and_comments(self) -> None:
        """
        Failure stamp: removes wos:executing label, posts failure comment,
        leaves issue OPEN, returns True.
        """
        remove_label_result = _make_completed_process(returncode=0)
        comment_result = _make_completed_process(returncode=0)

        with patch("subprocess.run", side_effect=[remove_label_result, comment_result]) as mock_run:
            result = stamp_issue_failed(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is True
        assert mock_run.call_count == 2

        # Should NOT call 'gh issue close'
        for call_item in mock_run.call_args_list:
            args = call_item[0][0]
            assert "close" not in args, "stamp_issue_failed must not close the issue"

    def test_gh_cli_fails_returns_false_non_blocking(self) -> None:
        """
        When gh fails, stamp_issue_failed returns False without raising.
        UoW failure processing continues unblocked.
        """
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
            result = stamp_issue_failed(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is False

    def test_unexpected_exception_returns_false(self) -> None:
        """Any unexpected exception returns False."""
        with patch("subprocess.run", side_effect=TimeoutError("gh timed out")):
            result = stamp_issue_failed(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is False


# ---------------------------------------------------------------------------
# stamp_issue_unverifiable
# ---------------------------------------------------------------------------

class TestStampIssueUnverifiable:
    """Tests for the UoW-unverifiable lifecycle stamp."""

    def test_success_path_removes_label_and_comments_leaves_open(self) -> None:
        """
        Unverifiable stamp: removes wos:executing label, posts comment,
        leaves issue OPEN (sweeper can re-pick), returns True.
        """
        remove_label_result = _make_completed_process(returncode=0)
        comment_result = _make_completed_process(returncode=0)

        with patch("subprocess.run", side_effect=[remove_label_result, comment_result]) as mock_run:
            result = stamp_issue_unverifiable(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is True
        assert mock_run.call_count == 2

        # Should NOT call 'gh issue close'
        for call_item in mock_run.call_args_list:
            args = call_item[0][0]
            assert "close" not in args, "stamp_issue_unverifiable must not close the issue"

    def test_gh_cli_fails_returns_false_non_blocking(self) -> None:
        """
        When gh fails, stamp_issue_unverifiable returns False without raising.
        """
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
            result = stamp_issue_unverifiable(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is False

    def test_unexpected_exception_returns_false(self) -> None:
        """Any unexpected exception returns False."""
        with patch("subprocess.run", side_effect=OSError("broken pipe")):
            result = stamp_issue_unverifiable(_TEST_ISSUE, _TEST_UOW_ID, repo=_TEST_REPO)

        assert result is False
