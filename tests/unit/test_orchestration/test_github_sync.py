"""
Tests for github_sync.py — post-completion GitHub issue closure.

Tests cover pure functions (build_closure_comment, _parse_issue_url) and
the sweep logic (run_post_completion_sync) with an injectable close function
so no subprocess calls are made during testing.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.orchestration.github_sync import (
    GitHubSyncError,
    PostCompletionSyncResult,
    _parse_issue_url,
    build_closure_comment,
    run_post_completion_sync,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path: Path):
    from src.orchestration.registry import Registry
    return Registry(tmp_path / "registry.db")


def _make_done_uow(registry, issue_number: int, issue_url: str | None = None) -> str:
    """Create a UoW in done status with optional issue_url."""
    today = datetime.now(timezone.utc).date().isoformat()
    result = registry.upsert(
        issue_number=issue_number,
        title=f"Test issue {issue_number}",
        sweep_date=today,
        success_criteria="Test completion.",
        issue_url=issue_url,
    )
    registry.set_status_direct(result.id, "done")
    return result.id


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestParseIssueUrl:
    def test_parses_standard_github_url(self):
        result = _parse_issue_url("https://github.com/dcetlin/Lobster/issues/42")
        assert result == ("dcetlin/Lobster", "42")

    def test_parses_org_repo_url(self):
        result = _parse_issue_url("https://github.com/SiderealPress/lobster/issues/100")
        assert result == ("SiderealPress/lobster", "100")

    def test_returns_none_for_non_github_url(self):
        assert _parse_issue_url("https://example.com/foo") is None

    def test_returns_none_for_empty_string(self):
        assert _parse_issue_url("") is None

    def test_returns_none_for_pr_url(self):
        # PR URLs use /pull/ not /issues/
        assert _parse_issue_url("https://github.com/dcetlin/Lobster/pull/42") is None


class TestBuildClosureComment:
    def test_returns_string(self, registry):
        uow_id = _make_done_uow(registry, 1, "https://github.com/dcetlin/Lobster/issues/1")
        uow = registry.get(uow_id)
        result = build_closure_comment(uow)
        assert isinstance(result, str)

    def test_contains_uow_id(self, registry):
        uow_id = _make_done_uow(registry, 2, "https://github.com/dcetlin/Lobster/issues/2")
        uow = registry.get(uow_id)
        result = build_closure_comment(uow)
        assert uow_id in result

    def test_contains_summary(self, registry):
        uow_id = _make_done_uow(registry, 3, "https://github.com/dcetlin/Lobster/issues/3")
        uow = registry.get(uow_id)
        result = build_closure_comment(uow)
        assert uow.summary in result

    def test_mentions_wos(self, registry):
        uow_id = _make_done_uow(registry, 4, "https://github.com/dcetlin/Lobster/issues/4")
        uow = registry.get(uow_id)
        result = build_closure_comment(uow)
        assert "WOS" in result or "Work Order" in result

    def test_pure_same_uow_produces_same_output(self, registry):
        """build_closure_comment is pure — same UoW always produces same comment."""
        uow_id = _make_done_uow(registry, 5, "https://github.com/dcetlin/Lobster/issues/5")
        uow = registry.get(uow_id)
        assert build_closure_comment(uow) == build_closure_comment(uow)


# ---------------------------------------------------------------------------
# Sweep function tests
# ---------------------------------------------------------------------------

class TestRunPostCompletionSync:
    def _noop_close(self, **kwargs) -> None:
        """Injectable close function that does nothing (simulates successful gh call)."""
        pass

    def _failing_close(self, **kwargs) -> None:
        """Injectable close function that always raises GitHubSyncError."""
        raise GitHubSyncError("simulated gh failure")

    def test_syncs_done_uow_with_issue_url(self, registry):
        """A done UoW with issue_url gets synced and github_synced_at is set."""
        uow_id = _make_done_uow(registry, 10, "https://github.com/dcetlin/Lobster/issues/10")
        result = run_post_completion_sync(registry, _close_fn=self._noop_close)
        assert result.synced == 1
        assert result.failed == 0
        # github_synced_at should now be set
        conn = registry._connect()
        row = conn.execute(
            "SELECT github_synced_at FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        conn.close()
        assert row["github_synced_at"] is not None

    def test_skips_done_uow_without_issue_url(self, registry):
        """A done UoW without issue_url produces no sync and no error.
        The SQL query filters it out (issue_url IS NOT NULL), so skipped_no_url=0
        and synced=0 — the UoW is simply not in scope for the sync sweep.
        """
        _make_done_uow(registry, 11, issue_url=None)
        result = run_post_completion_sync(registry, _close_fn=self._noop_close)
        assert result.synced == 0
        assert result.failed == 0
        # skipped_no_url=0 because the SQL query already filters null-url UoWs
        assert result.skipped_no_url == 0

    def test_skips_already_synced_uow(self, registry):
        """A UoW that is already synced (github_synced_at set) is not re-processed."""
        uow_id = _make_done_uow(registry, 12, "https://github.com/dcetlin/Lobster/issues/12")
        # First sync
        run_post_completion_sync(registry, _close_fn=self._noop_close)
        # Second sync — should find nothing to process
        result = run_post_completion_sync(registry, _close_fn=self._noop_close)
        assert result.synced == 0

    def test_does_not_sync_non_done_uow(self, registry):
        """Active/proposed UoWs are not touched by the sync sweep."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(
            issue_number=13,
            title="Active issue",
            sweep_date=today,
            success_criteria="Test done.",
            issue_url="https://github.com/dcetlin/Lobster/issues/13",
        )
        # Leave it in proposed status (not done)
        sync_result = run_post_completion_sync(registry, _close_fn=self._noop_close)
        assert sync_result.synced == 0

    def test_records_failure_when_gh_fails(self, registry):
        """When gh close fails, failed counter is incremented and github_synced_at stays NULL."""
        uow_id = _make_done_uow(registry, 14, "https://github.com/dcetlin/Lobster/issues/14")
        result = run_post_completion_sync(registry, _close_fn=self._failing_close)
        assert result.failed == 1
        assert result.synced == 0
        assert len(result.errors) == 1
        # github_synced_at must remain NULL so retry fires on next heartbeat
        conn = registry._connect()
        row = conn.execute(
            "SELECT github_synced_at FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        conn.close()
        assert row["github_synced_at"] is None

    def test_dry_run_does_not_write_to_registry(self, registry):
        """dry_run=True logs but does not write github_synced_at."""
        uow_id = _make_done_uow(registry, 15, "https://github.com/dcetlin/Lobster/issues/15")
        result = run_post_completion_sync(registry, dry_run=True, _close_fn=self._noop_close)
        assert result.synced == 1  # counts as synced in dry_run
        conn = registry._connect()
        row = conn.execute(
            "SELECT github_synced_at FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        conn.close()
        assert row["github_synced_at"] is None  # not written in dry_run

    def test_processes_multiple_done_uows(self, registry):
        """Multiple done+unsynced UoWs are all processed in one sweep."""
        for i in range(3):
            _make_done_uow(
                registry,
                20 + i,
                f"https://github.com/dcetlin/Lobster/issues/{20 + i}",
            )
        result = run_post_completion_sync(registry, _close_fn=self._noop_close)
        assert result.synced == 3

    def test_close_fn_receives_correct_repo_and_issue_number(self, registry):
        """The injectable close function receives repo and issue_number from the URL."""
        calls = []

        def recording_close(*, repo, issue_number, comment):
            calls.append({"repo": repo, "issue_number": issue_number})

        _make_done_uow(registry, 25, "https://github.com/dcetlin/Lobster/issues/25")
        run_post_completion_sync(registry, _close_fn=recording_close)
        assert len(calls) == 1
        assert calls[0]["repo"] == "dcetlin/Lobster"
        assert calls[0]["issue_number"] == "25"

    def test_close_fn_receives_comment_with_uow_id(self, registry):
        """The comment passed to the close function contains the UoW ID."""
        comments = []

        def capturing_close(*, repo, issue_number, comment):
            comments.append(comment)

        uow_id = _make_done_uow(registry, 26, "https://github.com/dcetlin/Lobster/issues/26")
        run_post_completion_sync(registry, _close_fn=capturing_close)
        assert len(comments) == 1
        assert uow_id in comments[0]
