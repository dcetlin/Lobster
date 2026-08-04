"""
Unit tests for scheduled-tasks/main-staleness-check.py.

Tests are named after the behaviors they verify, not the implementation
mechanisms. Pure classification/messaging functions are tested with
in-memory RepoState fixtures. Git boundary functions (fetch_origin,
get_current_branch, compute_ahead_behind, get_repo_state) are exercised
against real, disposable git repositories created under tmp_path — this
proves the four required drift states (clean / behind / diverged /
not-on-main) are detected correctly against actual git behavior, not just
mocked assumptions about it.

Named after behaviors:
  - test_classify_clean_when_on_main_with_no_drift
  - test_classify_behind_when_origin_has_new_commits
  - test_classify_diverged_when_local_has_unpushed_commits
  - test_classify_diverged_when_both_ahead_and_behind
  - test_classify_not_on_main_for_feature_branch
  - test_classify_not_on_main_for_detached_head
  - test_alert_text_mentions_behind_count_and_pull_command
  - test_alert_text_mentions_diverged_commit_count
  - test_alert_text_mentions_current_branch_when_not_on_main
  - test_alert_text_mentions_detached_head_when_branch_is_none
  - test_real_repo_clean_state_detected
  - test_real_repo_behind_state_detected
  - test_real_repo_diverged_state_detected
  - test_real_repo_not_on_main_state_detected_for_feature_branch
  - test_disabled_job_skips_check_entirely
  - test_clean_run_writes_no_alert
  - test_drift_run_writes_alert_to_inbox_for_admin
  - test_second_alert_same_day_is_suppressed
  - test_dry_run_writes_no_alert_file
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Load the module under test from its script path (hyphenated filename)
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scheduled-tasks"
    / "main-staleness-check.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("main_staleness_check", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


msc = _load_module()

RepoState = msc.RepoState


# ---------------------------------------------------------------------------
# Pure function tests — classify_state
# ---------------------------------------------------------------------------


class TestClassifyState:
    def test_classify_clean_when_on_main_with_no_drift(self):
        state = RepoState(current_branch="main", ahead=0, behind=0)
        assert msc.classify_state(state) == msc.CLEAN

    def test_classify_behind_when_origin_has_new_commits(self):
        state = RepoState(current_branch="main", ahead=0, behind=3)
        assert msc.classify_state(state) == msc.BEHIND

    def test_classify_diverged_when_local_has_unpushed_commits(self):
        state = RepoState(current_branch="main", ahead=2, behind=0)
        assert msc.classify_state(state) == msc.DIVERGED

    def test_classify_diverged_when_both_ahead_and_behind(self):
        state = RepoState(current_branch="main", ahead=1, behind=5)
        assert msc.classify_state(state) == msc.DIVERGED

    def test_classify_not_on_main_for_feature_branch(self):
        state = RepoState(current_branch="feature/x", ahead=0, behind=0)
        assert msc.classify_state(state) == msc.NOT_ON_MAIN

    def test_classify_not_on_main_for_detached_head(self):
        state = RepoState(current_branch=None, ahead=0, behind=0)
        assert msc.classify_state(state) == msc.NOT_ON_MAIN

    def test_classify_not_on_main_takes_priority_over_drift_counts(self):
        # Even if ahead/behind happen to be nonzero, being off main is the
        # deeper anti-pattern and must win the classification.
        state = RepoState(current_branch="feature/x", ahead=1, behind=1)
        assert msc.classify_state(state) == msc.NOT_ON_MAIN


# ---------------------------------------------------------------------------
# Pure function tests — build_alert_text
# ---------------------------------------------------------------------------


class TestBuildAlertText:
    def test_alert_text_mentions_behind_count_and_pull_command(self, tmp_path):
        state = RepoState(current_branch="main", ahead=0, behind=4)
        text = msc.build_alert_text(msc.BEHIND, state, tmp_path)
        assert "4" in text
        assert "BEHIND" in text
        assert "git pull" in text
        assert str(tmp_path) in text

    def test_alert_text_mentions_diverged_commit_count(self, tmp_path):
        state = RepoState(current_branch="main", ahead=2, behind=0)
        text = msc.build_alert_text(msc.DIVERGED, state, tmp_path)
        assert "2" in text
        assert "DIVERGED" in text

    def test_alert_text_mentions_both_counts_when_diverged_and_behind(self, tmp_path):
        state = RepoState(current_branch="main", ahead=1, behind=3)
        text = msc.build_alert_text(msc.DIVERGED, state, tmp_path)
        assert "1" in text
        assert "3" in text

    def test_alert_text_mentions_current_branch_when_not_on_main(self, tmp_path):
        state = RepoState(current_branch="feature/foo", ahead=0, behind=0)
        text = msc.build_alert_text(msc.NOT_ON_MAIN, state, tmp_path)
        assert "feature/foo" in text
        assert "NOT on main" in text

    def test_alert_text_mentions_detached_head_when_branch_is_none(self, tmp_path):
        state = RepoState(current_branch=None, ahead=0, behind=0)
        text = msc.build_alert_text(msc.NOT_ON_MAIN, state, tmp_path)
        assert "detached HEAD" in text

    def test_alert_text_clean_state_is_reassuring(self, tmp_path):
        state = RepoState(current_branch="main", ahead=0, behind=0)
        text = msc.build_alert_text(msc.CLEAN, state, tmp_path)
        assert "clean" in text.lower()


# ---------------------------------------------------------------------------
# Real-git integration helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def _make_repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """
    Build a disposable origin (bare) + local (clone) repo pair with one
    commit on main. Returns (origin_bare_dir, local_clone_dir).
    """
    origin_src = tmp_path / "origin_src"
    origin_src.mkdir()
    _git(origin_src, "init", "-b", "main")
    _git(origin_src, "config", "user.email", "test@example.com")
    _git(origin_src, "config", "user.name", "Test")
    (origin_src / "file.txt").write_text("v1\n")
    _git(origin_src, "add", ".")
    _git(origin_src, "commit", "-m", "init")

    origin_bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(origin_src), str(origin_bare)],
        capture_output=True, text=True, check=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(origin_bare), str(local)],
        capture_output=True, text=True, check=True,
    )
    _git(local, "config", "user.email", "test@example.com")
    _git(local, "config", "user.name", "Test")
    _git(local, "checkout", "-B", "main")
    return origin_bare, local


def _push_new_commit_to_bare_origin(origin_bare: Path, tmp_path: Path) -> None:
    """Simulate 'origin has commits the local checkout doesn't' (BEHIND)."""
    work = tmp_path / "origin_work"
    subprocess.run(
        ["git", "clone", str(origin_bare), str(work)],
        capture_output=True, text=True, check=True,
    )
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "file.txt").write_text("v2\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "origin-only commit")
    _git(work, "push", "origin", "main")


def _commit_locally_without_pushing(local: Path) -> None:
    """Simulate 'local main has commits not on origin' (DIVERGED)."""
    (local / "local-only.txt").write_text("local\n")
    _git(local, "add", ".")
    _git(local, "commit", "-m", "local-only commit")


# ---------------------------------------------------------------------------
# Real-git integration tests — the four required states
# ---------------------------------------------------------------------------


class TestRealRepoStateDetection:
    def test_real_repo_clean_state_detected(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        ok, err = msc.fetch_origin(local)
        assert ok, err
        state = msc.get_repo_state(local)
        assert msc.classify_state(state) == msc.CLEAN

    def test_real_repo_behind_state_detected(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        _push_new_commit_to_bare_origin(origin_bare, tmp_path)
        ok, err = msc.fetch_origin(local)
        assert ok, err
        state = msc.get_repo_state(local)
        assert msc.classify_state(state) == msc.BEHIND
        assert state.behind == 1
        assert state.ahead == 0

    def test_real_repo_diverged_state_detected(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        _commit_locally_without_pushing(local)
        ok, err = msc.fetch_origin(local)
        assert ok, err
        state = msc.get_repo_state(local)
        assert msc.classify_state(state) == msc.DIVERGED
        assert state.ahead == 1
        assert state.behind == 0

    def test_real_repo_diverged_state_detected_when_also_behind(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        _push_new_commit_to_bare_origin(origin_bare, tmp_path)
        _commit_locally_without_pushing(local)
        ok, err = msc.fetch_origin(local)
        assert ok, err
        state = msc.get_repo_state(local)
        assert msc.classify_state(state) == msc.DIVERGED
        assert state.ahead == 1
        assert state.behind == 1

    def test_real_repo_not_on_main_state_detected_for_feature_branch(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        _git(local, "checkout", "-b", "feature/whatever")
        ok, err = msc.fetch_origin(local)
        assert ok, err
        state = msc.get_repo_state(local)
        assert msc.classify_state(state) == msc.NOT_ON_MAIN
        assert state.current_branch == "feature/whatever"

    def test_real_repo_not_on_main_state_detected_for_detached_head(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        head_sha = _git(local, "rev-parse", "HEAD").stdout.strip()
        _git(local, "checkout", head_sha)
        ok, err = msc.fetch_origin(local)
        assert ok, err
        state = msc.get_repo_state(local)
        assert msc.classify_state(state) == msc.NOT_ON_MAIN
        assert state.current_branch is None


# ---------------------------------------------------------------------------
# main() integration tests — job gate, alerting, rate limiting
# ---------------------------------------------------------------------------


class TestMainIntegration:
    def test_disabled_job_skips_check_entirely(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        with (
            patch.object(msc, "is_job_enabled", return_value=False),
            patch.object(msc, "fetch_origin") as mock_fetch,
        ):
            result = msc.main(repo_dir=local)
        assert result == 0
        mock_fetch.assert_not_called()

    def test_clean_run_writes_no_alert(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        inbox = tmp_path / "inbox"
        with (
            patch.object(msc, "is_job_enabled", return_value=True),
            patch.object(msc, "_inbox_dir", return_value=inbox),
        ):
            result = msc.main(repo_dir=local)
        assert result == 0
        assert not inbox.exists() or len(list(inbox.glob("*.json"))) == 0

    def test_drift_run_writes_alert_to_inbox_for_admin(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        _push_new_commit_to_bare_origin(origin_bare, tmp_path)
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()
        with (
            patch.object(msc, "is_job_enabled", return_value=True),
            patch.object(msc, "_inbox_dir", return_value=inbox),
            patch.object(msc, "STALENESS_SENTINEL_PREFIX", str(sentinel_dir) + "/alert-"),
            patch.object(msc, "ADMIN_CHAT_ID", 999),
        ):
            result = msc.main(repo_dir=local)
        assert result == 0
        json_files = list(inbox.glob("*.json"))
        assert len(json_files) == 1
        msg = json.loads(json_files[0].read_text())
        assert msg["chat_id"] == 999
        assert msg["source"] == "system"
        assert msg["type"] == "message"
        assert "BEHIND" in msg["text"]

    def test_not_on_main_run_writes_alert(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        _git(local, "checkout", "-b", "some-other-branch")
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()
        with (
            patch.object(msc, "is_job_enabled", return_value=True),
            patch.object(msc, "_inbox_dir", return_value=inbox),
            patch.object(msc, "STALENESS_SENTINEL_PREFIX", str(sentinel_dir) + "/alert-"),
        ):
            result = msc.main(repo_dir=local)
        assert result == 0
        json_files = list(inbox.glob("*.json"))
        assert len(json_files) == 1
        msg = json.loads(json_files[0].read_text())
        assert "some-other-branch" in msg["text"]

    def test_second_alert_same_day_is_suppressed(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        _push_new_commit_to_bare_origin(origin_bare, tmp_path)
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()
        with (
            patch.object(msc, "is_job_enabled", return_value=True),
            patch.object(msc, "_inbox_dir", return_value=inbox),
            patch.object(msc, "STALENESS_SENTINEL_PREFIX", str(sentinel_dir) + "/alert-"),
        ):
            msc.main(repo_dir=local)
            msc.main(repo_dir=local)
        # A second cron run on the same day (drift still present) must not
        # write a second inbox message.
        assert len(list(inbox.glob("*.json"))) == 1

    def test_dry_run_writes_no_alert_file(self, tmp_path):
        origin_bare, local = _make_repo_pair(tmp_path)
        _push_new_commit_to_bare_origin(origin_bare, tmp_path)
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()
        with (
            patch.object(msc, "is_job_enabled", return_value=True),
            patch.object(msc, "_inbox_dir", return_value=inbox),
            patch.object(msc, "STALENESS_SENTINEL_PREFIX", str(sentinel_dir) + "/alert-"),
        ):
            result = msc.main(dry_run=True, repo_dir=local)
        assert result == 0
        assert not inbox.exists() or len(list(inbox.glob("*.json"))) == 0
        assert len(list(sentinel_dir.iterdir())) == 0

    def test_missing_repo_dir_does_not_crash(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        with patch.object(msc, "is_job_enabled", return_value=True):
            result = msc.main(repo_dir=missing)
        assert result == 0
