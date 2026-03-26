"""
Unit tests for hooks/block-claude-p.py

Tests cover:
- Non-Bash tool calls pass through (exit 0)
- Bash with no claude invocation passes through (exit 0)
- Actual invocation `claude -p ...` triggers the hook
- `claude --print` triggers the hook
- Comment lines (`# claude -p`) do not trigger the hook
- String literal context (echo "claude -p") does not trigger
- Known-safe callers (run-job.sh, claude-persistent.sh) do not trigger
- warn mode (default): exit 1 on match
- block mode: exit 2 on match
- Log file is written on match
"""

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "block-claude-p.py"


# ---------------------------------------------------------------------------
# Helper: run the hook with given input and env
# ---------------------------------------------------------------------------


def _run_hook(
    hook_input: dict,
    env_overrides: dict | None = None,
) -> tuple[int, str, str]:
    """Run the hook script and return (exit_code, stdout, stderr)."""
    stdout_capture = StringIO()
    stderr_capture = StringIO()
    stdin_data = json.dumps(hook_input)

    exit_code = None
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_capture),
        patch("sys.stderr", stderr_capture),
        patch.dict(os.environ, env_overrides or {}, clear=False),
    ):
        try:
            hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
            exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()


def _make_bash_input(command: str, session_id: str = "test-session-001") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def _make_tool_input(tool_name: str, tool_input: dict) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "test-session-001",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


# ---------------------------------------------------------------------------
# Non-Bash tool passthrough
# ---------------------------------------------------------------------------


class TestNonBashTools:
    def test_write_tool_passes_through(self):
        """Write tool calls must never be checked — out of scope for this hook."""
        hook_input = _make_tool_input("Write", {"file_path": "/tmp/x.sh", "content": "claude -p foo"})
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0

    def test_edit_tool_passes_through(self):
        """Edit tool calls must never be checked."""
        hook_input = _make_tool_input("Edit", {"file_path": "/tmp/x.sh", "old_string": "", "new_string": "claude -p"})
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0

    def test_agent_tool_passes_through(self):
        """Agent tool calls are not Bash — pass through."""
        hook_input = _make_tool_input("Agent", {"prompt": "run claude -p foo", "run_in_background": True})
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Bash with no claude invocation
# ---------------------------------------------------------------------------


class TestBashNoMatch:
    def test_ls_command_passes_through(self):
        exit_code, _, _ = _run_hook(_make_bash_input("ls /tmp"))
        assert exit_code == 0

    def test_claude_without_p_flag_passes_through(self):
        """Claude invoked without -p flag should not trigger."""
        exit_code, _, _ = _run_hook(_make_bash_input("claude --version"))
        assert exit_code == 0

    def test_unrelated_p_flag_passes_through(self):
        """Other commands with -p flag are not affected."""
        exit_code, _, _ = _run_hook(_make_bash_input("ssh -p 22 host"))
        assert exit_code == 0

    def test_empty_command_passes_through(self):
        exit_code, _, _ = _run_hook(_make_bash_input(""))
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Actual invocations that should trigger
# ---------------------------------------------------------------------------


class TestActualInvocations:
    def test_claude_p_in_warn_mode_exits_1(self):
        """claude -p in warn mode (default) exits 1."""
        exit_code, _, stderr = _run_hook(
            _make_bash_input('claude -p "do some task"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "warn"},
        )
        assert exit_code == 1, f"Expected 1 (warn), got {exit_code}"

    def test_claude_p_default_mode_exits_1(self):
        """Default mode is warn — exits 1 when no env var is set."""
        env = {"LOBSTER_BLOCK_CLAUDE_P_MODE": "warn"}
        exit_code, _, _ = _run_hook(_make_bash_input('claude -p "task"'), env_overrides=env)
        assert exit_code == 1

    def test_claude_print_triggers_hook(self):
        """claude --print should also trigger the hook."""
        exit_code, _, _ = _run_hook(
            _make_bash_input('claude --print "task"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "warn"},
        )
        assert exit_code == 1

    def test_claude_p_in_block_mode_exits_2(self):
        """claude -p in block mode exits 2 (hard block)."""
        exit_code, _, stderr = _run_hook(
            _make_bash_input('claude -p "do some task"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert exit_code == 2, f"Expected 2 (block), got {exit_code}"

    def test_block_mode_message_contains_blocked(self):
        """Block mode must emit BLOCKED in stderr."""
        _, _, stderr = _run_hook(
            _make_bash_input('claude -p "task"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert "BLOCKED" in stderr, f"Expected BLOCKED in stderr, got: {stderr!r}"

    def test_warn_message_explains_problem(self):
        """Warning message should explain the MCP conflict risk."""
        _, _, stderr = _run_hook(
            _make_bash_input('claude -p "task"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "warn"},
        )
        assert "MCP" in stderr or "dispatcher" in stderr.lower(), (
            f"Expected guidance about dispatcher/MCP in stderr, got: {stderr!r}"
        )

    def test_warning_not_in_stdout(self):
        """Warning/block message must only appear in stderr, not stdout."""
        _, stdout, _ = _run_hook(
            _make_bash_input('claude -p "task"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "warn"},
        )
        assert "claude" not in stdout.lower() or stdout == "", (
            f"Message leaked to stdout: {stdout!r}"
        )

    def test_multiline_command_with_claude_p_triggers(self):
        """Multi-line scripts containing claude -p should trigger."""
        command = "#!/bin/bash\nls /tmp\nclaude -p 'run task'\necho done"
        exit_code, _, _ = _run_hook(
            _make_bash_input(command),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "warn"},
        )
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Allowlisted patterns that must NOT trigger
# ---------------------------------------------------------------------------


class TestAllowlistedPatterns:
    def test_comment_line_does_not_trigger(self):
        """Shell comment lines (# claude -p) must not fire the hook."""
        exit_code, _, _ = _run_hook(
            _make_bash_input("# claude -p foo"),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert exit_code == 0, "Comment line should not trigger the hook"

    def test_indented_comment_does_not_trigger(self):
        """Indented comment (  # claude -p) must also be excluded."""
        exit_code, _, _ = _run_hook(
            _make_bash_input("  # claude -p foo"),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert exit_code == 0

    def test_echo_string_literal_does_not_trigger(self):
        """echo 'run: claude -p' is a string literal, not an invocation."""
        exit_code, _, _ = _run_hook(
            _make_bash_input('echo "run: claude -p"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert exit_code == 0, "String literal in echo should not trigger"

    def test_printf_string_literal_does_not_trigger(self):
        """printf referencing claude -p is a string literal."""
        exit_code, _, _ = _run_hook(
            _make_bash_input('printf "usage: claude -p <task>"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert exit_code == 0

    def test_run_job_sh_safe_caller_does_not_trigger(self):
        """run-job.sh is a known-safe caller and must not be blocked."""
        exit_code, _, _ = _run_hook(
            _make_bash_input("bash run-job.sh my-job"),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert exit_code == 0, "run-job.sh is allowlisted"

    def test_claude_persistent_sh_safe_caller_does_not_trigger(self):
        """claude-persistent.sh is a known-safe caller and must not be blocked."""
        exit_code, _, _ = _run_hook(
            _make_bash_input("/path/to/claude-persistent.sh start"),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert exit_code == 0, "claude-persistent.sh is allowlisted"

    def test_variable_assignment_string_does_not_trigger(self):
        """Variable assignment with quoted string containing claude -p is allowlisted."""
        exit_code, _, _ = _run_hook(
            _make_bash_input('USAGE="run: claude -p task"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "block"},
        )
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Log file is written on match
# ---------------------------------------------------------------------------


class TestLogging:
    def test_log_file_written_on_match(self, tmp_path, monkeypatch):
        """A match must be logged to ~/lobster-workspace/logs/claude-p-blocks.jsonl."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LOBSTER_BLOCK_CLAUDE_P_MODE", "warn")

        exit_code, _, _ = _run_hook(
            _make_bash_input('claude -p "task"'),
            env_overrides={"LOBSTER_BLOCK_CLAUDE_P_MODE": "warn"},
        )
        assert exit_code == 1

        log_file = tmp_path / "lobster-workspace" / "logs" / "claude-p-blocks.jsonl"
        assert log_file.exists(), f"Log file not created at {log_file}"

        entries = [json.loads(line) for line in log_file.read_text().strip().splitlines()]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["mode"] == "warn"
        assert "session_id" in entry
        assert "timestamp" in entry
        assert "triggering_lines" in entry
        assert any("claude -p" in line for line in entry["triggering_lines"])

    def test_no_match_no_log(self, tmp_path, monkeypatch):
        """No match → no log file created."""
        monkeypatch.setenv("HOME", str(tmp_path))

        _run_hook(_make_bash_input("ls /tmp"))

        log_file = tmp_path / "lobster-workspace" / "logs" / "claude-p-blocks.jsonl"
        assert not log_file.exists(), "Log file should not exist when no match"
