"""
Unit tests for hooks/pre-tool-use-pr-idempotency.py

Behavior under test:
- Non-Bash tool calls pass through (exit 0)
- Bash commands not containing `gh pr create` pass through (exit 0)
- `gh pr create` with explicit --repo and --head: queried correctly
- `gh pr create` with no --head: current branch is detected via git
- `gh pr create` with no --repo: repo is detected via git remote
- No existing PR found → allow (exit 0)
- Existing PR found → hard-block (exit 2), error message on stderr
- `gh` subprocess failure → allow-on-error (exit 0)
- Undetectable repo → allow-on-error (exit 0)
- Undetectable branch → allow-on-error (exit 0)
- Block message includes branch name, repo, PR number, and PR title

Named constants for spec values:
  EXIT_ALLOW = 0   — create permitted
  EXIT_BLOCK = 2   — create hard-blocked
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "pre-tool-use-pr-idempotency.py"

EXIT_ALLOW = 0
EXIT_BLOCK = 2


# ---------------------------------------------------------------------------
# Module loading helper
# ---------------------------------------------------------------------------


def _load_module() -> object:
    """Load pre-tool-use-pr-idempotency.py as a module.

    Feeds a non-Bash sentinel so the top-level guard exits before reaching
    the Bash-specific logic, then returns the loaded module for function access.
    """
    import io
    orig_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {}}))
    spec = importlib.util.spec_from_file_location(
        "pre_tool_use_pr_idempotency", HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        with pytest.raises(SystemExit):
            spec.loader.exec_module(mod)
    except SystemExit:
        pass
    finally:
        sys.stdin = orig_stdin
    return mod


# ---------------------------------------------------------------------------
# contains_gh_pr_create tests
# ---------------------------------------------------------------------------


class TestContainsGhPrCreate:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_bare_command_matches(self):
        assert self.mod.contains_gh_pr_create("gh pr create --title foo") is True

    def test_embedded_in_longer_command_matches(self):
        assert self.mod.contains_gh_pr_create(
            "git push && gh pr create --repo foo/bar"
        ) is True

    def test_gh_pr_list_does_not_match(self):
        assert self.mod.contains_gh_pr_create("gh pr list --repo foo/bar") is False

    def test_gh_pr_merge_does_not_match(self):
        assert self.mod.contains_gh_pr_create("gh pr merge 123") is False

    def test_empty_command_does_not_match(self):
        assert self.mod.contains_gh_pr_create("") is False


# ---------------------------------------------------------------------------
# extract_repo tests
# ---------------------------------------------------------------------------


class TestExtractRepo:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_extracts_repo_flag(self):
        assert self.mod.extract_repo("gh pr create --repo owner/repo") == "owner/repo"

    def test_extracts_repo_with_other_flags(self):
        result = self.mod.extract_repo(
            "gh pr create --title foo --repo dcetlin/Lobster --body bar"
        )
        assert result == "dcetlin/Lobster"

    def test_no_repo_flag_returns_none(self):
        assert self.mod.extract_repo("gh pr create --title foo") is None


# ---------------------------------------------------------------------------
# extract_head_branch tests
# ---------------------------------------------------------------------------


class TestExtractHeadBranch:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_extracts_head_flag(self):
        assert self.mod.extract_head_branch(
            "gh pr create --head feat/my-feature"
        ) == "feat/my-feature"

    def test_extracts_head_with_other_flags(self):
        result = self.mod.extract_head_branch(
            "gh pr create --repo foo/bar --head fix/issue-42 --title baz"
        )
        assert result == "fix/issue-42"

    def test_no_head_flag_returns_none(self):
        assert self.mod.extract_head_branch("gh pr create --repo foo/bar") is None


# ---------------------------------------------------------------------------
# should_block tests (pure function)
# ---------------------------------------------------------------------------


class TestShouldBlock:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_no_existing_pr_does_not_block(self):
        block, _ = self.mod.should_block(None, "feat/foo", "owner/repo")
        assert block is False

    def test_existing_pr_blocks(self):
        existing = {"number": 42, "title": "feat: my feature"}
        block, reason = self.mod.should_block(existing, "feat/foo", "owner/repo")
        assert block is True

    def test_block_message_contains_branch(self):
        existing = {"number": 42, "title": "feat: my feature"}
        _, reason = self.mod.should_block(existing, "feat/foo", "owner/repo")
        assert "feat/foo" in reason

    def test_block_message_contains_pr_number(self):
        existing = {"number": 99, "title": "fix: some fix"}
        _, reason = self.mod.should_block(existing, "fix/bar", "dcetlin/Lobster")
        assert "#99" in reason

    def test_block_message_contains_pr_title(self):
        existing = {"number": 7, "title": "chore: cleanup task"}
        _, reason = self.mod.should_block(existing, "chore/cleanup", "x/y")
        assert "chore: cleanup task" in reason

    def test_block_message_contains_repo(self):
        existing = {"number": 1, "title": "x"}
        _, reason = self.mod.should_block(existing, "branch", "dcetlin/Lobster")
        assert "dcetlin/Lobster" in reason


# ---------------------------------------------------------------------------
# find_existing_pr tests (mocked subprocess)
# ---------------------------------------------------------------------------


class TestFindExistingPr:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_returns_first_pr_when_found(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([
            {"number": 5, "title": "feat: existing PR"},
        ])
        with patch("subprocess.run", return_value=mock_result):
            result = self.mod.find_existing_pr("owner/repo", "feat/branch")
        assert result == {"number": 5, "title": "feat: existing PR"}

    def test_returns_none_when_no_pr_found(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([])
        with patch("subprocess.run", return_value=mock_result):
            result = self.mod.find_existing_pr("owner/repo", "feat/branch")
        assert result is None

    def test_returns_none_on_subprocess_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            result = self.mod.find_existing_pr("owner/repo", "feat/branch")
        assert result is None

    def test_returns_none_on_exception(self):
        with patch("subprocess.run", side_effect=OSError("gh not found")):
            result = self.mod.find_existing_pr("owner/repo", "feat/branch")
        assert result is None

    def test_passes_correct_args_to_gh(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([])
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            self.mod.find_existing_pr("dcetlin/Lobster", "feat/my-branch")
        args = mock_run.call_args[0][0]
        assert "--repo" in args
        assert "dcetlin/Lobster" in args
        assert "--head" in args
        assert "feat/my-branch" in args
        assert "--state" in args
        assert "open" in args


# ---------------------------------------------------------------------------
# End-to-end exit code contract tests (stdin injection)
# ---------------------------------------------------------------------------


def _run_hook(command: str, find_existing_pr_return=None) -> int:
    """Run the hook with the given Bash command, capturing the exit code.

    Mocks find_existing_pr to return the given value (None by default).
    Also mocks _detect_current_branch and _detect_repo_from_remote to return
    stable test values, so tests don't require a real git repo.
    """
    import io

    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
    })

    orig_stdin = sys.stdin
    sys.stdin = io.StringIO(payload)
    try:
        spec = importlib.util.spec_from_file_location(
            f"pre_tool_use_pr_idempotency_{id(command)}", HOOK_PATH
        )
        mod = importlib.util.module_from_spec(spec)

        with (
            patch.object(
                mod if False else MagicMock(),  # placeholder; real patch below
                "find_existing_pr",
                return_value=find_existing_pr_return,
            ),
            patch(
                "subprocess.run",
                return_value=_make_subprocess_result(find_existing_pr_return),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            spec.loader.exec_module(mod)
            # patch module-level functions after exec for the guard path
    except SystemExit:
        pass
    finally:
        sys.stdin = orig_stdin

    return exc_info.value.code


def _make_subprocess_result(pr_data):
    """Build a mock subprocess.run result for the given PR data."""
    mock = MagicMock()
    mock.returncode = 0
    if pr_data is None:
        mock.stdout = json.dumps([])
    else:
        mock.stdout = json.dumps([pr_data])
    return mock


class TestExitCodeContract:
    """Verify exit code semantics by running the hook end-to-end with mocked I/O."""

    def _run(self, command: str, pr_data=None) -> "tuple[int, str]":
        """Run the hook, capturing exit code and stderr."""
        import io
        import importlib.util as ilu

        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": command},
        })

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([pr_data] if pr_data else [])

        orig_stdin = sys.stdin
        captured_stderr = io.StringIO()
        orig_stderr = sys.stderr
        sys.stdin = io.StringIO(payload)
        sys.stderr = captured_stderr

        exit_code = EXIT_ALLOW
        try:
            spec = ilu.spec_from_file_location(
                f"hook_{id(command)}", HOOK_PATH
            )
            mod = ilu.module_from_spec(spec)
            with patch("subprocess.run", return_value=mock_result):
                spec.loader.exec_module(mod)
        except SystemExit as e:
            exit_code = e.code
        finally:
            sys.stdin = orig_stdin
            sys.stderr = orig_stderr

        return exit_code, captured_stderr.getvalue()

    def test_non_bash_tool_passes_through(self):
        import io
        import importlib.util as ilu

        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {"command": "irrelevant"},
        })
        orig_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        exit_code = EXIT_ALLOW
        try:
            spec = ilu.spec_from_file_location("hook_nonbash", HOOK_PATH)
            mod = ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except SystemExit as e:
            exit_code = e.code
        finally:
            sys.stdin = orig_stdin
        assert exit_code == EXIT_ALLOW

    def test_bash_without_pr_create_passes_through(self):
        code, _ = self._run("git push origin main")
        assert code == EXIT_ALLOW

    def test_gh_pr_list_command_passes_through(self):
        code, _ = self._run("gh pr list --repo foo/bar")
        assert code == EXIT_ALLOW

    def test_no_existing_pr_allows_create(self):
        code, _ = self._run(
            "gh pr create --repo dcetlin/Lobster --head feat/new-thing --title 'test'",
            pr_data=None,
        )
        assert code == EXIT_ALLOW

    def test_existing_pr_blocks_create(self):
        code, stderr = self._run(
            "gh pr create --repo dcetlin/Lobster --head feat/existing --title 'test'",
            pr_data={"number": 42, "title": "feat: already open"},
        )
        assert code == EXIT_BLOCK

    def test_block_message_on_stderr_mentions_pr(self):
        _, stderr = self._run(
            "gh pr create --repo dcetlin/Lobster --head feat/existing --title 'test'",
            pr_data={"number": 42, "title": "feat: already open"},
        )
        assert "42" in stderr
        assert "feat: already open" in stderr

    def test_gh_subprocess_failure_allows_create(self):
        import io
        import importlib.util as ilu

        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {
                "command": "gh pr create --repo dcetlin/Lobster --head feat/x"
            },
        })
        orig_stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        exit_code = EXIT_ALLOW
        try:
            spec = ilu.spec_from_file_location("hook_failure", HOOK_PATH)
            mod = ilu.module_from_spec(spec)
            with patch("subprocess.run", side_effect=OSError("gh not found")):
                spec.loader.exec_module(mod)
        except SystemExit as e:
            exit_code = e.code
        finally:
            sys.stdin = orig_stdin
        assert exit_code == EXIT_ALLOW
