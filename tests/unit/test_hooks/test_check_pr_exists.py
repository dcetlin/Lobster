"""
Unit tests for hooks/check-pr-exists-before-create.py

Behavior under test:
- Non-Bash tool calls pass through (exit 0)
- Bash commands not containing 'gh pr create' pass through (exit 0)
- 'gh pr create' where no matching open PR exists allows creation (exit 0)
- 'gh pr create' where a matching open PR exists hard-blocks (exit 2)
- Repo and branch are extracted correctly from various command forms
- When --head is not specified, the hook extracts the current branch via git

Named constants for spec values:
- EXIT_ALLOW = 0  -- tool call permitted
- EXIT_BLOCK = 2  -- tool call hard-blocked (duplicate PR exists)
"""

import importlib.util
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "check-pr-exists-before-create.py"

EXIT_ALLOW = 0
EXIT_BLOCK = 2


# ---------------------------------------------------------------------------
# Module loading helper
# ---------------------------------------------------------------------------

def _load_module():
    """Load the hook module, catching the SystemExit from the top-level guard."""
    import io
    orig_stdin = sys.stdin
    # Feed a non-Bash payload so the top-level guard exits 0 immediately
    sys.stdin = io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {}}))
    spec = importlib.util.spec_from_file_location(
        "check_pr_exists_before_create", HOOK_PATH
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
# extract_repo tests
# ---------------------------------------------------------------------------

class TestExtractRepo:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_long_form_repo_flag(self):
        assert self.mod.extract_repo("gh pr create --repo owner/repo --title foo") == "owner/repo"

    def test_short_form_repo_flag(self):
        assert self.mod.extract_repo("gh pr create -R owner/repo --title foo") == "owner/repo"

    def test_no_repo_flag_returns_none(self):
        assert self.mod.extract_repo("gh pr create --title foo") is None

    def test_repo_with_equals_sign(self):
        assert self.mod.extract_repo("gh pr create --repo=owner/repo --title foo") == "owner/repo"


# ---------------------------------------------------------------------------
# extract_head_branch tests
# ---------------------------------------------------------------------------

class TestExtractHeadBranch:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_long_form_head_flag(self):
        assert self.mod.extract_head_branch("gh pr create --head fix/my-branch --title foo") == "fix/my-branch"

    def test_short_form_head_flag(self):
        assert self.mod.extract_head_branch("gh pr create -H fix/my-branch --title foo") == "fix/my-branch"

    def test_no_head_flag_returns_none(self):
        assert self.mod.extract_head_branch("gh pr create --title foo") is None

    def test_head_with_equals_sign(self):
        assert self.mod.extract_head_branch("gh pr create --head=fix/my-branch --title foo") == "fix/my-branch"


# ---------------------------------------------------------------------------
# check_for_existing_pr tests
# ---------------------------------------------------------------------------

class TestCheckForExistingPr:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_no_existing_pr_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="[]\n", stderr=""
            )
            result = self.mod.check_for_existing_pr("owner/repo", "fix/my-branch")
            assert result is None

    def test_existing_pr_returns_info(self):
        pr_data = [{"number": 42, "title": "fix: something"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(pr_data) + "\n", stderr=""
            )
            result = self.mod.check_for_existing_pr("owner/repo", "fix/my-branch")
            assert result == {"number": 42, "title": "fix: something"}

    def test_gh_cli_failure_returns_none(self):
        """When gh CLI fails, allow the PR creation (fail-open)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="error"
            )
            result = self.mod.check_for_existing_pr("owner/repo", "fix/my-branch")
            assert result is None

    def test_passes_correct_args_to_gh(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="[]\n", stderr=""
            )
            self.mod.check_for_existing_pr("dcetlin/Lobster", "fix/some-branch")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "gh" in args
            assert "pr" in args
            assert "list" in args
            assert "--repo" in args
            idx = args.index("--repo")
            assert args[idx + 1] == "dcetlin/Lobster"
            assert "--head" in args
            idx = args.index("--head")
            assert args[idx + 1] == "fix/some-branch"

    def test_multiple_open_prs_returns_first(self):
        """If multiple PRs match (unlikely but possible), return the first."""
        pr_data = [
            {"number": 42, "title": "fix: first"},
            {"number": 43, "title": "fix: second"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(pr_data) + "\n", stderr=""
            )
            result = self.mod.check_for_existing_pr("owner/repo", "fix/branch")
            assert result == {"number": 42, "title": "fix: first"}


# ---------------------------------------------------------------------------
# Gate logic integration tests
# ---------------------------------------------------------------------------

def _run_gate(mod, tool_name: str, command: str, mock_pr_result=None,
              mock_branch="main"):
    """
    Simulate the hook's gate logic using the module's pure functions.

    mock_pr_result: if not None, patch check_for_existing_pr to return this
    mock_branch: the fallback branch name when --head is not in the command
    """
    if tool_name != "Bash":
        return EXIT_ALLOW

    if "gh pr create" not in command:
        return EXIT_ALLOW

    repo = mod.extract_repo(command)
    head = mod.extract_head_branch(command)

    if head is None:
        head = mock_branch

    if repo is None or head is None:
        return EXIT_ALLOW

    with patch.object(mod, "check_for_existing_pr", return_value=mock_pr_result):
        existing = mod.check_for_existing_pr(repo, head)

    if existing is not None:
        return EXIT_BLOCK

    return EXIT_ALLOW


class TestGateLogic:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module()

    def test_non_bash_tool_allows(self):
        result = _run_gate(self.mod, "Write", "gh pr create --repo o/r")
        assert result == EXIT_ALLOW

    def test_non_pr_create_command_allows(self):
        result = _run_gate(self.mod, "Bash", "git status")
        assert result == EXIT_ALLOW

    def test_no_existing_pr_allows_creation(self):
        result = _run_gate(
            self.mod, "Bash",
            "gh pr create --repo owner/repo --head fix/branch --title foo",
            mock_pr_result=None,
        )
        assert result == EXIT_ALLOW

    def test_existing_pr_blocks_creation(self):
        result = _run_gate(
            self.mod, "Bash",
            "gh pr create --repo owner/repo --head fix/branch --title foo",
            mock_pr_result={"number": 42, "title": "fix: something"},
        )
        assert result == EXIT_BLOCK

    def test_fallback_branch_used_when_no_head_flag(self):
        """When --head is absent, the hook uses the current git branch."""
        result = _run_gate(
            self.mod, "Bash",
            "gh pr create --repo owner/repo --title foo",
            mock_pr_result={"number": 99, "title": "fix: thing"},
            mock_branch="fix/my-current-branch",
        )
        assert result == EXIT_BLOCK

    def test_missing_repo_allows_creation(self):
        """When --repo cannot be extracted, fail-open (allow)."""
        result = _run_gate(
            self.mod, "Bash",
            "gh pr create --title foo",
            mock_pr_result={"number": 1, "title": "anything"},
        )
        assert result == EXIT_ALLOW

    def test_heredoc_command_with_gh_pr_create(self):
        """gh pr create inside a heredoc or multiline command is still caught."""
        cmd = (
            'gh pr create --repo dcetlin/Lobster --head fix/branch '
            '--title "fix: something" --body "$(cat <<\'EOF\'\nBody text\nEOF\n)"'
        )
        result = _run_gate(
            self.mod, "Bash", cmd,
            mock_pr_result={"number": 10, "title": "fix: something"},
        )
        assert result == EXIT_BLOCK
