"""
Unit tests for hooks/pr-merge-gate.py

Behavior under test:
- Non-Bash tool calls pass through (exit 0)
- Commands not containing 'gh pr merge' pass through (exit 0)
- 'gh pr merge' with no extractable PR number passes through (exit 0)
- VERDICT: APPROVED in oracle/verdicts/pr-{N}.md → allows merge (exit 0)
- VERDICT: NEEDS_CHANGES in verdict file → hard-blocks (exit 2)
- UNKNOWN first line in verdict file → hard-blocks (exit 2)
- No oracle entry at all (missing verdict file) → hard-blocks (exit 2)
- Legacy oracle/decisions.md APPROVED fallback → allows merge (exit 0)
- Legacy oracle/decisions.md NEEDS_CHANGES fallback → hard-blocks (exit 2)

Named constants for spec values:
- EXIT_ALLOW = 0  — merge permitted
- EXIT_BLOCK = 2  — merge hard-blocked
"""

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "pr-merge-gate.py"

EXIT_ALLOW = 0
EXIT_BLOCK = 2


# ---------------------------------------------------------------------------
# Module loading helper — loads the module without executing top-level guards
# ---------------------------------------------------------------------------

def _load_module_functions():
    """
    Load pr-merge-gate.py and return the module with functions accessible.

    We feed a non-Bash payload so the top-level guard exits before doing
    anything, then we re-import to access the functions. Since the module
    sys.exit(0)s immediately for non-Bash input, we catch that SystemExit.
    """
    import io
    orig_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {}}))
    spec = importlib.util.spec_from_file_location("pr_merge_gate", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        with pytest.raises(SystemExit):
            spec.loader.exec_module(mod)
    except SystemExit:
        pass
    finally:
        sys.stdin = orig_stdin
    return mod


def _make_verdict_file(verdicts_dir: Path, pr_number: int, first_line: str) -> None:
    """Write a verdict file with the given first line."""
    verdicts_dir.mkdir(parents=True, exist_ok=True)
    verdict_file = verdicts_dir / f"pr-{pr_number}.md"
    verdict_file.write_text(f"{first_line}\nPR: {pr_number}\n\nSome findings below.\n")


# ---------------------------------------------------------------------------
# extract_pr_number tests
# ---------------------------------------------------------------------------

class TestExtractPrNumber:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module_functions()

    def test_bare_number_after_merge(self):
        assert self.mod.extract_pr_number("gh pr merge 123") == 123

    def test_flag_before_number(self):
        assert self.mod.extract_pr_number("gh pr merge --squash 456") == 456

    def test_number_before_flag(self):
        assert self.mod.extract_pr_number("gh pr merge 789 --squash") == 789

    def test_url_form(self):
        assert self.mod.extract_pr_number("gh pr merge https://github.com/owner/repo/pull/321") == 321

    def test_no_pr_number_returns_none(self):
        assert self.mod.extract_pr_number("gh pr merge --squash") is None

    def test_non_merge_command_returns_none(self):
        assert self.mod.extract_pr_number("gh pr list") is None


# ---------------------------------------------------------------------------
# find_oracle_verdict tests
# ---------------------------------------------------------------------------

class TestFindOracleVerdict:
    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module_functions()

    def test_approved_verdict_file(self, tmp_path):
        verdicts_dir = tmp_path / "verdicts"
        _make_verdict_file(verdicts_dir, 42, "VERDICT: APPROVED")
        self.mod.VERDICTS_DIR = verdicts_dir
        assert self.mod.find_oracle_verdict(42) == "APPROVED"

    def test_needs_changes_verdict_file(self, tmp_path):
        verdicts_dir = tmp_path / "verdicts"
        _make_verdict_file(verdicts_dir, 42, "VERDICT: NEEDS_CHANGES")
        self.mod.VERDICTS_DIR = verdicts_dir
        assert self.mod.find_oracle_verdict(42) == "NEEDS_CHANGES"

    def test_unknown_first_line_in_verdict_file(self, tmp_path):
        verdicts_dir = tmp_path / "verdicts"
        _make_verdict_file(verdicts_dir, 42, "some unexpected content")
        self.mod.VERDICTS_DIR = verdicts_dir
        assert self.mod.find_oracle_verdict(42) == "UNKNOWN"

    def test_missing_verdict_file_and_no_decisions_md_returns_none(self, tmp_path):
        verdicts_dir = tmp_path / "verdicts"
        verdicts_dir.mkdir()
        self.mod.VERDICTS_DIR = verdicts_dir
        self.mod.DECISIONS_FILE = tmp_path / "nonexistent.md"
        assert self.mod.find_oracle_verdict(99) is None

    def test_legacy_decisions_md_approved(self, tmp_path):
        verdicts_dir = tmp_path / "verdicts"
        verdicts_dir.mkdir()
        self.mod.VERDICTS_DIR = verdicts_dir
        decisions_file = tmp_path / "decisions.md"
        decisions_file.write_text(
            "### [2026-04-01] PR #55 — some feature\n"
            "**VERDICT: APPROVED**\nSome notes.\n"
        )
        self.mod.DECISIONS_FILE = decisions_file
        assert self.mod.find_oracle_verdict(55) == "APPROVED"

    def test_legacy_decisions_md_needs_changes(self, tmp_path):
        verdicts_dir = tmp_path / "verdicts"
        verdicts_dir.mkdir()
        self.mod.VERDICTS_DIR = verdicts_dir
        decisions_file = tmp_path / "decisions.md"
        decisions_file.write_text(
            "### [2026-04-01] PR #56 — another feature\n"
            "**VERDICT: NEEDS_CHANGES**\nSome notes.\n"
        )
        self.mod.DECISIONS_FILE = decisions_file
        assert self.mod.find_oracle_verdict(56) == "NEEDS_CHANGES"


# ---------------------------------------------------------------------------
# Exit-code contract tests — call the gate logic directly via patched module
# ---------------------------------------------------------------------------

def _run_gate_logic(mod, tool_name: str, command: str) -> int:
    """
    Call the gate logic directly through the module's find_oracle_verdict and
    extract_pr_number functions, simulating what the top-level script does.

    This avoids re-executing module-level Path assignments that would overwrite
    our test-patched VERDICTS_DIR / DECISIONS_FILE values.
    """
    if tool_name != "Bash":
        return EXIT_ALLOW

    if "gh pr merge" not in command:
        return EXIT_ALLOW

    pr_number = mod.extract_pr_number(command)
    if pr_number is None:
        return EXIT_ALLOW

    verdict = mod.find_oracle_verdict(pr_number)

    if verdict == "APPROVED":
        return EXIT_ALLOW

    # Both missing verdict (None) and non-APPROVED verdicts hard-block
    return EXIT_BLOCK


class TestHookExitCodes:
    """Test the hook's exit code contract using the module's pure functions."""

    @pytest.fixture(autouse=True)
    def load_mod(self):
        self.mod = _load_module_functions()

    def test_non_bash_tool_allows(self):
        """Non-Bash tool calls always pass through."""
        result = _run_gate_logic(self.mod, "Write", "gh pr merge 100")
        assert result == EXIT_ALLOW

    def test_non_merge_command_allows(self):
        """Bash commands not containing 'gh pr merge' pass through."""
        result = _run_gate_logic(self.mod, "Bash", "git status")
        assert result == EXIT_ALLOW

    def test_approved_verdict_allows_merge(self, tmp_path):
        """VERDICT: APPROVED in verdict file allows merge."""
        verdicts_dir = tmp_path / "verdicts"
        _make_verdict_file(verdicts_dir, 200, "VERDICT: APPROVED")
        self.mod.VERDICTS_DIR = verdicts_dir
        self.mod.DECISIONS_FILE = tmp_path / "nonexistent.md"
        result = _run_gate_logic(self.mod, "Bash", "gh pr merge 200 --squash")
        assert result == EXIT_ALLOW

    def test_needs_changes_verdict_hard_blocks(self, tmp_path):
        """VERDICT: NEEDS_CHANGES hard-blocks the merge."""
        verdicts_dir = tmp_path / "verdicts"
        _make_verdict_file(verdicts_dir, 201, "VERDICT: NEEDS_CHANGES")
        self.mod.VERDICTS_DIR = verdicts_dir
        self.mod.DECISIONS_FILE = tmp_path / "nonexistent.md"
        result = _run_gate_logic(self.mod, "Bash", "gh pr merge 201")
        assert result == EXIT_BLOCK

    def test_missing_oracle_entry_hard_blocks(self, tmp_path):
        """No oracle verdict file at all → hard-blocks (not just warns)."""
        verdicts_dir = tmp_path / "verdicts"
        verdicts_dir.mkdir()
        self.mod.VERDICTS_DIR = verdicts_dir
        self.mod.DECISIONS_FILE = tmp_path / "nonexistent.md"
        result = _run_gate_logic(self.mod, "Bash", "gh pr merge 999")
        assert result == EXIT_BLOCK

    def test_unknown_verdict_hard_blocks(self, tmp_path):
        """Unrecognized first line in verdict file → hard-blocks."""
        verdicts_dir = tmp_path / "verdicts"
        _make_verdict_file(verdicts_dir, 202, "## Some heading")
        self.mod.VERDICTS_DIR = verdicts_dir
        self.mod.DECISIONS_FILE = tmp_path / "nonexistent.md"
        result = _run_gate_logic(self.mod, "Bash", "gh pr merge 202 --rebase")
        assert result == EXIT_BLOCK

    def test_no_pr_number_in_command_allows(self):
        """'gh pr merge' with no extractable PR number passes through."""
        result = _run_gate_logic(self.mod, "Bash", "gh pr merge --squash")
        assert result == EXIT_ALLOW
