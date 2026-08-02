"""
Unit tests for the duplicate-dispatch fix in scheduled-tasks/pr-review-sweeper.py
(issue #1491).

Root cause: the sweeper's only real dedup was a state file
(pr-review-sweeper-state.json) pruned every STATE_PRUNE_HOURS. Once a dispatched
PR's state entry pruned but the PR was still open, the sweeper re-dispatched an
oracle review even though a terminal `VERDICT: APPROVED` was already committed
to oracle/verdicts/pr-{number}.md — burning a full oracle cycle (~104k tokens,
~230s) for nothing (confirmed recurring on PR #1489, ~every 2h).

These tests exercise the fix: has_final_verdict() reads the durable,
git-committed verdict file directly, and main() skips dispatch when it returns
True — independent of state-file pruning.

Named after behaviors, not mechanisms.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Load the script module via importlib.
#
# pr-review-sweeper.py is a standalone script, not a package module. Its
# module-level imports (src.utils.inbox_write, src.utils.jobs) are lightweight
# and safe to import for real — no stubbing required (see
# tests/unit/test_scheduled_tasks/test_executor_heartbeat.py for the pattern
# used when heavier stubbing is needed).
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parents[3] / "scheduled-tasks" / "pr-review-sweeper.py"
)


def _load_sweeper():
    MODULE_NAME = "pr_review_sweeper"
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    with patch.dict("sys.modules", {MODULE_NAME: mod}):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


sweeper = _load_sweeper()

VERDICT_APPROVED_LINE = sweeper.VERDICT_APPROVED_LINE  # "VERDICT: APPROVED"
PR_NUMBER = 1489  # matches the PR named in issue #1491's confirmed recurrence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_verdict(verdicts_dir: Path, pr_number: int, first_line: str) -> None:
    verdicts_dir.mkdir(parents=True, exist_ok=True)
    (verdicts_dir / f"pr-{pr_number}.md").write_text(
        f"{first_line}\n\nSome rationale.\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# has_final_verdict() — unit behavior
# ---------------------------------------------------------------------------

def test_has_final_verdict_true_for_committed_approved_verdict(tmp_path, monkeypatch):
    """A committed VERDICT: APPROVED file is recognized as terminal."""
    verdicts_dir = tmp_path / "oracle" / "verdicts"
    _write_verdict(verdicts_dir, PR_NUMBER, VERDICT_APPROVED_LINE)
    monkeypatch.setattr(sweeper, "ORACLE_VERDICTS_DIR", verdicts_dir)

    assert sweeper.has_final_verdict(PR_NUMBER) is True


def test_has_final_verdict_false_when_no_verdict_file_exists(tmp_path, monkeypatch):
    """No verdict file at all (PR never reviewed) → not terminal, proceed to dispatch."""
    verdicts_dir = tmp_path / "oracle" / "verdicts"
    monkeypatch.setattr(sweeper, "ORACLE_VERDICTS_DIR", verdicts_dir)

    assert sweeper.has_final_verdict(PR_NUMBER) is False


def test_has_final_verdict_false_for_needs_changes_verdict(tmp_path, monkeypatch):
    """A NEEDS_CHANGES verdict is a fix round in progress, not terminal — do not skip."""
    verdicts_dir = tmp_path / "oracle" / "verdicts"
    _write_verdict(verdicts_dir, PR_NUMBER, "VERDICT: NEEDS_CHANGES")
    monkeypatch.setattr(sweeper, "ORACLE_VERDICTS_DIR", verdicts_dir)

    assert sweeper.has_final_verdict(PR_NUMBER) is False


def test_has_final_verdict_false_when_file_unreadable(tmp_path, monkeypatch):
    """An unreadable verdict path (e.g. a directory, not a file) fails open — do not skip."""
    verdicts_dir = tmp_path / "oracle" / "verdicts"
    verdicts_dir.mkdir(parents=True, exist_ok=True)
    # Create a directory where the verdict file is expected, so read_text() raises.
    (verdicts_dir / f"pr-{PR_NUMBER}.md").mkdir()
    monkeypatch.setattr(sweeper, "ORACLE_VERDICTS_DIR", verdicts_dir)

    assert sweeper.has_final_verdict(PR_NUMBER) is False


# ---------------------------------------------------------------------------
# main() — dedup path exercised end-to-end
# ---------------------------------------------------------------------------

def _pr_created_at(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _fake_gh_pr_list(prs: list[dict]):
    """Build a subprocess.run side_effect that answers `gh pr list` with prs
    and `gh pr view --comments` with no comments (simulating the dead
    REVIEW_MARKER path — no agent posts that marker in this configuration)."""

    class _Result:
        def __init__(self, stdout: str):
            self.stdout = stdout
            self.returncode = 0

    def _run(cmd, **kwargs):
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return _Result(json.dumps(prs))
        if cmd[:2] == ["gh", "pr"] and "view" in cmd:
            return _Result(json.dumps({"comments": []}))
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    return _run


def test_dispatch_skipped_for_pr_with_stale_state_but_committed_approved_verdict(
    tmp_path, monkeypatch
):
    """Reproduces issue #1491: state entry pruned (or never existed for this cycle),
    but a terminal verdict is already committed — main() must not re-dispatch."""
    verdicts_dir = tmp_path / "oracle" / "verdicts"
    _write_verdict(verdicts_dir, PR_NUMBER, VERDICT_APPROVED_LINE)
    monkeypatch.setattr(sweeper, "ORACLE_VERDICTS_DIR", verdicts_dir)
    monkeypatch.setattr(sweeper, "STATE_FILE", tmp_path / "state.json")  # empty/no prior state
    monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path / "messages"))
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path / "workspace"))  # no jobs.json -> enabled by default

    prs = [{"number": PR_NUMBER, "title": "Some PR", "createdAt": _pr_created_at(60)}]
    with patch("subprocess.run", side_effect=_fake_gh_pr_list(prs)):
        rc = sweeper.main(dry_run=False)

    assert rc == 0
    inbox_dir = tmp_path / "messages" / "inbox"
    dispatched_files = list(inbox_dir.glob("*.json")) if inbox_dir.exists() else []
    assert dispatched_files == [], (
        "sweeper re-dispatched a review for a PR that already has a "
        "terminal oracle verdict committed"
    )


def test_dispatch_proceeds_for_pr_with_no_verdict_and_no_state_entry(tmp_path, monkeypatch):
    """Sanity check: a genuinely unreviewed PR (no verdict, no state entry) is
    still dispatched — the fix must not make the sweeper inert."""
    verdicts_dir = tmp_path / "oracle" / "verdicts"  # left empty, no verdict written
    monkeypatch.setattr(sweeper, "ORACLE_VERDICTS_DIR", verdicts_dir)
    monkeypatch.setattr(sweeper, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path / "messages"))
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("LOBSTER_ADMIN_CHAT_ID", "12345")

    other_pr = 1600
    prs = [{"number": other_pr, "title": "New PR", "createdAt": _pr_created_at(60)}]
    with patch("subprocess.run", side_effect=_fake_gh_pr_list(prs)):
        rc = sweeper.main(dry_run=False)

    assert rc == 0
    inbox_dir = tmp_path / "messages" / "inbox"
    dispatched_files = list(inbox_dir.glob("*.json"))
    assert len(dispatched_files) == 1
    written = json.loads(dispatched_files[0].read_text())
    assert written["data"]["pr_number"] == other_pr
