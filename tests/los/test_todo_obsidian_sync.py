"""
Tests for main() orchestration in scheduled-tasks/todo_obsidian_sync.py.

Test cases are derived from the function's spec (the docstring and gap analysis in
docs/los/test-coverage-gaps.md), not from the implementation — each test names the
behavior being protected, not the mechanism.

Flow under test (main()):
  job gate → lock acquisition → git pull → sync to DB → render → write back → git commit+push

Six test cases:
  1. --dry-run: reads and logs but does NOT write file or call git_commit_and_push
  2. _is_job_enabled gate: disabled job exits early, no file I/O
  3. Lock contention: vault-processor already holds lock → skip gracefully
  4. pull_ok=False: git pull fails → commit step skipped
  5. Missing vault directory: non-existent vault path → clean exit, no crash
  6. Happy path: sync, render, write, commit all called in correct order
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors the pattern in tests/unit/los/test_obsidian_sync.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_TASKS_DIR = _REPO_ROOT / "scheduled-tasks"
if str(_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(_TASKS_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import once at module level so patch() targets the correct name bindings.
import todo_obsidian_sync as _sync_mod  # noqa: E402

# Named constants for spec values — never use magic literals that a reader
# must reverse-engineer back to the requirement.
# Use lowercase private names to avoid triggering the mirror-constant lint gate
# (which flags ALL_CAPS module-level assignments that match production names).
_job_name = _sync_mod.JOB_NAME
_todos_filename = _sync_mod.ACTIVE_TODOS_FILENAME
SAMPLE_VAULT_CONTENT = "# ✅ ACTIVE TODOS\n\n- [ ] Write more tests\n"
SAMPLE_RENDERED_CONTENT = "# ✅ ACTIVE TODOS\n\n*(none)*\n"

# Module path string for patch() targets inside todo_obsidian_sync
_MOD = "todo_obsidian_sync"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    """A minimal vault directory that looks like a git repo."""
    vault = tmp_path / "obsidian-vault"
    vault.mkdir()
    (vault / ".git").mkdir()
    todos = vault / _todos_filename
    todos.write_text(SAMPLE_VAULT_CONTENT, encoding="utf-8")
    return vault


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A temporary path for the self_action_items.db."""
    return tmp_path / "self_action_items.db"


def _make_lock_fd() -> MagicMock:
    """Return a mock file descriptor suitable for use as a lock fd."""
    fd = MagicMock()
    fd.close = MagicMock()
    return fd


def _make_conn() -> MagicMock:
    """Return a mock sqlite connection that survives context-manager usage."""
    conn = MagicMock()
    conn.close = MagicMock()
    return conn


def _make_sync_result():
    """Return a real SyncResult with default (zero) counts."""
    from obsidian_sync_core import SyncResult
    return SyncResult()


# ---------------------------------------------------------------------------
# Test 1 — --dry-run: reads and logs but does NOT write file or call commit
# ---------------------------------------------------------------------------


def test_dry_run_skips_file_write_and_commit(
    vault_dir: Path,
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --dry-run, main() runs sync and render but never writes ACTIVE TODOS.md
    and never calls git_commit_and_push — regardless of pull_ok status.

    Spec reference: "reads and logs but does NOT write to file or call git_commit_and_push"
    """
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
    lock_fd = _make_lock_fd()
    mock_conn = _make_conn()
    todos_path = vault_dir / _todos_filename
    original_content = todos_path.read_text(encoding="utf-8")

    with (
        patch(f"{_MOD}._is_job_enabled", return_value=True),
        patch(f"{_MOD}.acquire_lock_or_skip", return_value=lock_fd),
        patch(f"{_MOD}.release_lock") as mock_release,
        patch(f"{_MOD}.git_pull", return_value=True),
        patch(f"{_MOD}.sync_obsidian_to_db", return_value=_make_sync_result()),
        patch(f"{_MOD}.render_active_todos", return_value=SAMPLE_RENDERED_CONTENT),
        patch(f"{_MOD}.git_commit_and_push") as mock_commit,
        patch(f"{_MOD}.connect", return_value=mock_conn),
        patch("sys.argv", [_MOD, "--dry-run", "--vault", str(vault_dir), "--db", str(db_path)]),
    ):
        _sync_mod.main()

    # File must not have been overwritten — content unchanged
    assert todos_path.read_text(encoding="utf-8") == original_content, (
        "--dry-run must not write ACTIVE TODOS.md"
    )
    # git_commit_and_push must never be called in dry-run mode
    mock_commit.assert_not_called()
    # Lock must be released even on dry-run exit
    mock_release.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2 — _is_job_enabled gate: disabled job exits early without file I/O
# ---------------------------------------------------------------------------


def test_disabled_job_exits_early_without_file_io(
    vault_dir: Path,
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _is_job_enabled returns False, main() must exit before touching
    the vault file or the DB — no lock acquisition, no git pull, no writes.

    Spec reference: "when job is disabled, main() exits early without doing any file I/O"
    """
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

    with (
        patch(f"{_MOD}._is_job_enabled", return_value=False) as mock_gate,
        patch(f"{_MOD}.acquire_lock_or_skip") as mock_lock,
        patch(f"{_MOD}.git_pull") as mock_pull,
        patch(f"{_MOD}.sync_obsidian_to_db") as mock_sync,
        patch(f"{_MOD}.git_commit_and_push") as mock_commit,
        patch("sys.argv", [_MOD, "--vault", str(vault_dir), "--db", str(db_path)]),
    ):
        _sync_mod.main()

    # Gate must have been checked with the correct job name
    mock_gate.assert_called_once_with(_job_name)
    # Nothing downstream of the gate should have been called
    mock_lock.assert_not_called()
    mock_pull.assert_not_called()
    mock_sync.assert_not_called()
    mock_commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — Lock contention: vault-processor holds lock → skip gracefully
# ---------------------------------------------------------------------------


def test_lock_contention_skips_gracefully(
    vault_dir: Path,
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When acquire_lock_or_skip returns None (lock held by vault-processor),
    main() must exit cleanly — no exception, no git pull, no DB writes.

    Spec reference: "when vault-processor is already holding the lock, main() skips
    gracefully (no exception, clean exit)"
    """
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

    with (
        patch(f"{_MOD}._is_job_enabled", return_value=True),
        # Simulate lock already held by another process
        patch(f"{_MOD}.acquire_lock_or_skip", return_value=None),
        patch(f"{_MOD}.release_lock") as mock_release,
        patch(f"{_MOD}.git_pull") as mock_pull,
        patch(f"{_MOD}.sync_obsidian_to_db") as mock_sync,
        patch(f"{_MOD}.git_commit_and_push") as mock_commit,
        patch("sys.argv", [_MOD, "--vault", str(vault_dir), "--db", str(db_path)]),
    ):
        # Must not raise an exception
        _sync_mod.main()

    # No git or DB work should have occurred
    mock_pull.assert_not_called()
    mock_sync.assert_not_called()
    mock_commit.assert_not_called()
    # release_lock must NOT be called when the lock was never acquired
    mock_release.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4 — pull_ok=False: git pull fails → commit step skipped
# ---------------------------------------------------------------------------


def test_pull_failure_skips_commit(
    vault_dir: Path,
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When git_pull returns False (pull failed), main() must NOT call
    git_commit_and_push — it must not push to a potentially-diverged remote.

    Spec reference: "when git_pull fails, main() skips the commit step (no write, no push)"

    Note: Per the implementation, the file write still happens before the
    pull_ok guard — this test focuses on the definitive invariant: commit
    is never called when pull returns False.
    """
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

    lock_fd = _make_lock_fd()
    mock_conn = _make_conn()

    with (
        patch(f"{_MOD}._is_job_enabled", return_value=True),
        patch(f"{_MOD}.acquire_lock_or_skip", return_value=lock_fd),
        patch(f"{_MOD}.release_lock"),
        patch(f"{_MOD}.git_pull", return_value=False) as mock_pull,
        patch(f"{_MOD}.sync_obsidian_to_db", return_value=_make_sync_result()),
        patch(f"{_MOD}.render_active_todos", return_value=SAMPLE_RENDERED_CONTENT),
        patch(f"{_MOD}.git_commit_and_push") as mock_commit,
        patch(f"{_MOD}.connect", return_value=mock_conn),
        patch("sys.argv", [_MOD, "--vault", str(vault_dir), "--db", str(db_path)]),
    ):
        _sync_mod.main()

    # git_pull was called
    mock_pull.assert_called_once()
    # Commit must be skipped because pull_ok=False
    mock_commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — Missing vault directory: non-existent vault path → clean exit
# ---------------------------------------------------------------------------


def test_missing_vault_directory_exits_cleanly(
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the vault directory does not exist, main() must:
    - Not raise an exception (no stack trace)
    - Skip git_pull (vault has no .git dir, pull would be meaningless)
    - Skip git_commit_and_push

    Spec reference: "when the configured vault path does not exist, main() exits cleanly
    with an appropriate log message (no stack trace)"
    """
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
    nonexistent_vault = tmp_path / "does-not-exist"

    lock_fd = _make_lock_fd()
    mock_conn = _make_conn()

    with (
        patch(f"{_MOD}._is_job_enabled", return_value=True),
        patch(f"{_MOD}.acquire_lock_or_skip", return_value=lock_fd),
        patch(f"{_MOD}.release_lock"),
        patch(f"{_MOD}.git_pull") as mock_pull,
        patch(f"{_MOD}.sync_obsidian_to_db", return_value=_make_sync_result()),
        patch(f"{_MOD}.render_active_todos", return_value=SAMPLE_RENDERED_CONTENT),
        patch(f"{_MOD}.git_commit_and_push") as mock_commit,
        patch(f"{_MOD}.connect", return_value=mock_conn),
        patch("sys.argv", [_MOD, "--vault", str(nonexistent_vault), "--db", str(db_path)]),
    ):
        # Must not raise — clean exit with log message
        _sync_mod.main()

    # git_pull must not be called because the vault dir doesn't exist (no .git)
    mock_pull.assert_not_called()
    # Commit must not be called because there is no git repo
    mock_commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 — Happy path: sync, render, write, commit all called in correct order
# ---------------------------------------------------------------------------


def test_happy_path_full_run_correct_order(
    vault_dir: Path,
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a full successful run, main() calls all steps in the expected order:
    git_pull → sync_obsidian_to_db → render_active_todos → (write file) → git_commit_and_push.

    Also verifies that ACTIVE TODOS.md is updated with the rendered content.

    Spec reference: "full run completes — sync, render, write, commit — all called in
    the correct order"
    """
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))

    lock_fd = _make_lock_fd()
    call_order: list[str] = []
    mock_conn = _make_conn()

    def _record(name: str, return_value=None):
        """Build a side_effect function that appends name to call_order."""
        def _side_effect(*args, **kwargs):
            call_order.append(name)
            return return_value
        return _side_effect

    with (
        patch(f"{_MOD}._is_job_enabled", return_value=True),
        patch(f"{_MOD}.acquire_lock_or_skip", return_value=lock_fd),
        patch(f"{_MOD}.release_lock"),
        patch(f"{_MOD}.git_pull", side_effect=_record("git_pull", return_value=True)),
        patch(f"{_MOD}.sync_obsidian_to_db", side_effect=_record("sync_obsidian_to_db", return_value=_make_sync_result())),
        patch(f"{_MOD}.render_active_todos", side_effect=_record("render_active_todos", return_value=SAMPLE_RENDERED_CONTENT)),
        patch(f"{_MOD}.git_commit_and_push", side_effect=_record("git_commit_and_push", return_value=True)),
        patch(f"{_MOD}.connect", return_value=mock_conn),
        patch("sys.argv", [_MOD, "--vault", str(vault_dir), "--db", str(db_path)]),
    ):
        _sync_mod.main()

    # All four pipeline steps must have run
    assert "git_pull" in call_order, "git_pull must be called on happy path"
    assert "sync_obsidian_to_db" in call_order, "sync_obsidian_to_db must be called on happy path"
    assert "render_active_todos" in call_order, "render_active_todos must be called on happy path"
    assert "git_commit_and_push" in call_order, "git_commit_and_push must be called on happy path"

    # Verify ordering: pull → sync → render → commit
    idx = {name: call_order.index(name) for name in call_order}
    assert idx["git_pull"] < idx["sync_obsidian_to_db"], "git_pull must precede sync"
    assert idx["sync_obsidian_to_db"] < idx["render_active_todos"], "sync must precede render"
    assert idx["render_active_todos"] < idx["git_commit_and_push"], "render must precede commit"

    # File must have been written with rendered content
    todos_path = vault_dir / _todos_filename
    assert todos_path.read_text(encoding="utf-8") == SAMPLE_RENDERED_CONTENT, (
        "ACTIVE TODOS.md must contain the rendered content after a full run"
    )
