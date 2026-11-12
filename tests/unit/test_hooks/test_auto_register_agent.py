"""
Unit tests for hooks/auto-register-agent.py

Tests cover:
- Non-Agent tool calls are ignored (exit 0, no DB write)
- Both "Agent" and "Task" tool_name values register (matcher-gap coverage)
- YAML frontmatter: task_id, chat_id, source, reply_to_message_id parsed correctly
- Legacy text format: task_id extracted from "task_id is: X"
- agentId extracted from tool_response dict and list forms
- output_file extracted from tool_response
- DB row inserted with correct values
- dispatcher_pid/pid_captured_at populated via process-tree PID capture (issue #2148)
- INSERT OR IGNORE: existing row not overwritten
- Missing agentId: exits 0 without DB write
- DB failure: logs to hook-failures.log and exits 0
- Malformed stdin JSON: logs and exits 0
"""

import importlib.util
import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "auto-register-agent.py"


# ---------------------------------------------------------------------------
# Direct imports of pure functions (no side effects)
# ---------------------------------------------------------------------------

def _load_module():
    spec = importlib.util.spec_from_file_location("auto_register_agent", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
extract_metadata = _mod.extract_metadata
extract_agent_id = _mod.extract_agent_id
extract_output_file = _mod.extract_output_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_hook(hook_input: dict, tmp_path: Path) -> tuple[int, str, str]:
    """Run the hook via exec, capturing stdout/stderr and exit code."""
    stdout_cap = StringIO()
    stderr_cap = StringIO()
    stdin_data = json.dumps(hook_input)

    exit_code = None
    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
        patch.dict("os.environ", {
            "LOBSTER_MESSAGES": str(tmp_path / "messages"),
            "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
        }),
    ):
        try:
            hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
            exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _make_hook_input(
    tool_name: str = "Agent",
    prompt: str = "",
    tool_response: object = None,
    session_id: str = "sess-123",
) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": {"prompt": prompt},
        "tool_response": tool_response,
    }


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "messages" / "config" / "agent_sessions.db"
    return sqlite3.connect(str(db_path))


def _get_row(tmp_path: Path, agent_id: str) -> dict | None:
    conn = _open_db(tmp_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (agent_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# extract_metadata: pure function tests
# ---------------------------------------------------------------------------

class TestExtractMetadata:
    def test_yaml_frontmatter_all_fields(self):
        prompt = "---\ntask_id: my-task\nchat_id: ADMIN_CHAT_ID_REDACTED\nsource: telegram\nreply_to_message_id: 10924\n---\nsome content"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "my-task"
        assert meta["chat_id"] == "ADMIN_CHAT_ID_REDACTED"
        assert meta["source"] == "telegram"
        assert meta["reply_to_message_id"] == "10924"

    def test_yaml_frontmatter_minimal(self):
        prompt = "---\ntask_id: slim-task\n---\n"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "slim-task"
        assert meta["source"] == "telegram"  # default
        assert meta["chat_id"] is None
        assert meta["reply_to_message_id"] is None

    def test_yaml_frontmatter_leading_whitespace(self):
        """Prompt may have leading whitespace before the ---."""
        prompt = "\n  \n---\ntask_id: ws-task\n---\n"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "ws-task"

    def test_legacy_text_format(self):
        prompt = "Your task_id is: legacy-task\nDo some work."
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "legacy-task"
        assert meta["chat_id"] is None

    def test_legacy_text_case_insensitive(self):
        prompt = "TASK_ID IS: upper-task"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "upper-task"

    def test_no_task_id(self):
        prompt = "Just a plain prompt with no id."
        meta = extract_metadata(prompt)
        assert meta["task_id"] is None

    def test_yaml_wins_over_legacy_text(self):
        """When both formats are present, frontmatter task_id wins."""
        prompt = "---\ntask_id: yaml-id\n---\nYour task_id is: legacy-id"
        meta = extract_metadata(prompt)
        assert meta["task_id"] == "yaml-id"

    def test_no_closing_delimiter_falls_back(self):
        """Unclosed frontmatter (no closing ---) is not treated as frontmatter."""
        prompt = "---\ntask_id: unclosed\n\nYour task_id is: textid"
        meta = extract_metadata(prompt)
        # Frontmatter parse fails, legacy text used
        assert meta["task_id"] == "textid"

    def test_request_id_from_frontmatter(self):
        """request_id (agent-channel field) is parsed like any other frontmatter key."""
        prompt = "---\ntask_id: t-001\nchat_id: local-claude\nsource: local-claude\nrequest_id: 1732900000-a1b2c3d4\n---\nbody"
        meta = extract_metadata(prompt)
        assert meta["request_id"] == "1732900000-a1b2c3d4"

    def test_request_id_absent_is_none(self):
        """Normal telegram/slack tasks have no request_id — must be None, not empty string."""
        prompt = "---\ntask_id: t-002\nchat_id: 123\nsource: telegram\n---\nbody"
        meta = extract_metadata(prompt)
        assert meta["request_id"] is None


# ---------------------------------------------------------------------------
# extract_agent_id: pure function tests
# ---------------------------------------------------------------------------

class TestExtractAgentId:
    def test_dict_response(self):
        assert extract_agent_id({"agentId": "agt-001"}) == "agt-001"

    def test_list_response(self):
        assert extract_agent_id([{"agentId": "agt-002"}]) == "agt-002"

    def test_list_first_match(self):
        response = [{"type": "text", "text": "..."}, {"agentId": "agt-003"}]
        assert extract_agent_id(response) == "agt-003"

    def test_missing_agent_id(self):
        assert extract_agent_id({"result": "ok"}) is None

    def test_none_response(self):
        assert extract_agent_id(None) is None

    def test_empty_list(self):
        assert extract_agent_id([]) is None


# ---------------------------------------------------------------------------
# extract_output_file: pure function tests
# ---------------------------------------------------------------------------

class TestExtractOutputFile:
    def test_snake_case_key(self):
        assert extract_output_file({"output_file": "/tmp/out.txt"}) == "/tmp/out.txt"

    def test_camel_case_key(self):
        assert extract_output_file({"outputFile": "/tmp/out2.txt"}) == "/tmp/out2.txt"

    def test_missing(self):
        assert extract_output_file({"agentId": "x"}) is None

    def test_in_list(self):
        assert extract_output_file([{"output_file": "/tmp/f.txt"}]) == "/tmp/f.txt"


# ---------------------------------------------------------------------------
# Integration: hook execution
# ---------------------------------------------------------------------------

class TestHookNonAgentTool:
    def test_non_agent_exits_0_no_db(self, tmp_path):
        """Non-Agent tool calls are ignored entirely — no rows written to agent_sessions."""
        hook_input = _make_hook_input(
            tool_name="Bash",
            prompt="ls",
            tool_response={"output": "file.txt"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0
        db_path = tmp_path / "messages" / "config" / "agent_sessions.db"
        # The DB file may be created by the test isolator (conftest AtomicClaimDB),
        # but the hook must not have inserted any rows.
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()
                assert rows[0] == 0, "Non-Agent tool must not write any agent_sessions rows"
            except sqlite3.OperationalError:
                pass  # table doesn't exist — no rows written, test passes
            finally:
                conn.close()


class TestHookTaskToolName:
    """tool_name == "Task" must register exactly like "Agent" (matcher-gap fix)."""

    def test_task_tool_name_registers_row(self, tmp_path):
        """A Task-tool call inserts a row, not just an Agent-tool call."""
        prompt = "---\ntask_id: t-task-tool\nchat_id: 55555\nsource: telegram\n---\nDo task-tool work."
        hook_input = _make_hook_input(
            tool_name="Task",
            prompt=prompt,
            tool_response={"agentId": "agent-task-tool"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-task-tool")
        assert row is not None
        assert row["task_id"] == "t-task-tool"
        assert row["status"] == "running"

    def test_task_tool_name_missing_agent_id_no_write(self, tmp_path):
        """Task-tool calls still respect the missing-agentId no-op path."""
        hook_input = _make_hook_input(
            tool_name="Task",
            prompt="---\ntask_id: t-task-noid\n---",
            tool_response={"result": "no id here"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0
        db_path = tmp_path / "messages" / "config" / "agent_sessions.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()
                assert rows[0] == 0
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()


class TestHookDispatcherPidCapture:
    """dispatcher_pid/pid_captured_at are populated via process-tree PID capture (issue #2148)."""

    def test_dispatcher_pid_and_captured_at_populated_when_ancestor_found(self, tmp_path):
        """When find_dispatcher_ancestor_pid() finds a real PID, both columns are set."""
        prompt = "---\ntask_id: t-pid\nchat_id: 12345\n---\nDo work."
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-pid"},
        )
        with patch("agents.pid_liveness.find_dispatcher_ancestor_pid", return_value=4242):
            exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-pid")
        assert row is not None
        assert row["dispatcher_pid"] == 4242
        assert row["pid_captured_at"] is not None

    def test_dispatcher_pid_null_when_ancestor_not_found(self, tmp_path):
        """When no dispatcher ancestor is found, both columns stay NULL (never blocks registration)."""
        prompt = "---\ntask_id: t-nopid\nchat_id: 12345\n---\nDo work."
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-nopid"},
        )
        with patch("agents.pid_liveness.find_dispatcher_ancestor_pid", return_value=None):
            exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-nopid")
        assert row is not None
        assert row["dispatcher_pid"] is None
        assert row["pid_captured_at"] is None

    def test_pid_capture_failure_never_blocks_registration(self, tmp_path):
        """A raising PID-capture call is swallowed — registration still succeeds."""
        prompt = "---\ntask_id: t-pidfail\nchat_id: 12345\n---\nDo work."
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-pidfail"},
        )
        with patch(
            "agents.pid_liveness.find_dispatcher_ancestor_pid",
            side_effect=OSError("no /proc"),
        ):
            exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-pidfail")
        assert row is not None
        assert row["dispatcher_pid"] is None

    def test_dispatcher_pid_column_added_to_pre_existing_db(self, tmp_path):
        """A DB created before dispatcher_pid/pid_captured_at existed gets the columns via ALTER TABLE."""
        db_dir = tmp_path / "messages" / "config"
        db_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_dir / "agent_sessions.db"))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id TEXT PRIMARY KEY, task_id TEXT, agent_type TEXT,
                description TEXT NOT NULL, chat_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'telegram',
                status TEXT NOT NULL DEFAULT 'running',
                output_file TEXT, timeout_minutes INTEGER,
                input_summary TEXT, result_summary TEXT, parent_id TEXT,
                spawned_at TEXT NOT NULL, completed_at TEXT,
                last_seen_at TEXT, notified_at TEXT,
                trigger_message_id TEXT, trigger_snippet TEXT,
                reply_message_ids TEXT, request_id TEXT
            )
        """)
        conn.commit()
        conn.close()

        prompt = "---\ntask_id: t-pidmigrate\nchat_id: 12345\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-pidmigrate"},
        )
        with patch("agents.pid_liveness.find_dispatcher_ancestor_pid", return_value=9999):
            exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-pidmigrate")
        assert row is not None
        assert row["dispatcher_pid"] == 9999


class TestHookAgentWithAgentId:
    def test_inserts_row_with_frontmatter(self, tmp_path):
        """Full frontmatter inserts a complete row."""
        prompt = "---\ntask_id: t-001\nchat_id: 99999\nsource: slack\n---\nDo stuff."
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-abc"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-abc")
        assert row is not None
        assert row["task_id"] == "t-001"
        assert row["chat_id"] == "99999"
        assert row["source"] == "slack"
        assert row["status"] == "running"

    def test_inserts_row_with_legacy_text(self, tmp_path):
        """Legacy task_id text format inserts a row."""
        prompt = "Your task_id is: t-legacy\nDo work."
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-legacy"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-legacy")
        assert row is not None
        assert row["task_id"] == "t-legacy"

    def test_insert_or_ignore_preserves_existing_row(self, tmp_path):
        """A pre-existing row (from register_agent) is NOT overwritten."""
        # Pre-populate with a richer row
        db_dir = tmp_path / "messages" / "config"
        db_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_dir / "agent_sessions.db"))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id TEXT PRIMARY KEY, task_id TEXT, agent_type TEXT,
                description TEXT NOT NULL, chat_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'telegram',
                status TEXT NOT NULL DEFAULT 'running',
                output_file TEXT, timeout_minutes INTEGER,
                input_summary TEXT, result_summary TEXT, parent_id TEXT,
                spawned_at TEXT NOT NULL, completed_at TEXT,
                last_seen_at TEXT, notified_at TEXT,
                trigger_message_id TEXT, trigger_snippet TEXT,
                reply_message_ids TEXT
            )
        """)
        conn.execute(
            "INSERT INTO agent_sessions (id, description, chat_id, status, spawned_at)"
            " VALUES ('agent-dup', 'richer description', '12345', 'running', '2026-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        prompt = "---\ntask_id: dup-task\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-dup"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-dup")
        # Description should still be the original richer one
        assert row["description"] == "richer description"
        assert row["status"] == "running"  # INSERT OR IGNORE — existing row not overwritten

    def test_output_file_stored(self, tmp_path):
        """output_file from tool response is stored in DB."""
        prompt = "---\ntask_id: t-of\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-of", "output_file": "/tmp/result.json"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-of")
        assert row["output_file"] == "/tmp/result.json"

    def test_request_id_stored_for_local_claude_task(self, tmp_path):
        """request_id from frontmatter is persisted to the agent_sessions row (audit trail)."""
        prompt = (
            "---\ntask_id: t-agent-channel\nchat_id: local-claude\n"
            "source: local-claude\nrequest_id: req-abc123\n---\nDo the local-claude task."
        )
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-local-claude"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-local-claude")
        assert row is not None
        assert row["request_id"] == "req-abc123"
        assert row["source"] == "local-claude"

    def test_request_id_null_for_non_agent_channel_task(self, tmp_path):
        """Ordinary telegram tasks store NULL request_id, not an empty string."""
        prompt = "---\ntask_id: t-plain\nchat_id: 12345\nsource: telegram\n---\nDo normal work."
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-plain"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-plain")
        assert row is not None
        assert row["request_id"] is None

    def test_request_id_column_added_to_pre_existing_db(self, tmp_path):
        """A DB created before request_id existed gets the column via additive ALTER TABLE."""
        db_dir = tmp_path / "messages" / "config"
        db_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_dir / "agent_sessions.db"))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id TEXT PRIMARY KEY, task_id TEXT, agent_type TEXT,
                description TEXT NOT NULL, chat_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'telegram',
                status TEXT NOT NULL DEFAULT 'running',
                output_file TEXT, timeout_minutes INTEGER,
                input_summary TEXT, result_summary TEXT, parent_id TEXT,
                spawned_at TEXT NOT NULL, completed_at TEXT,
                last_seen_at TEXT, notified_at TEXT,
                trigger_message_id TEXT, trigger_snippet TEXT,
                reply_message_ids TEXT
            )
        """)
        conn.commit()
        conn.close()

        prompt = "---\ntask_id: t-migrate\nsource: local-claude\nrequest_id: req-migrated\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-migrated"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-migrated")
        assert row is not None
        assert row["request_id"] == "req-migrated"

    def test_no_chat_id_defaults_to_zero(self, tmp_path):
        """Missing chat_id stores '0' to satisfy NOT NULL constraint."""
        prompt = "---\ntask_id: t-nochat\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-nochat"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-nochat")
        assert row["chat_id"] == "0"

    def test_spawned_at_is_sqlite_compatible_format(self, tmp_path):
        """spawned_at must use 'YYYY-MM-DD HH:MM:SS' so SQLite datetime() comparisons work.

        SQLite's datetime('now', '-30 minutes') produces a timezone-naive string
        like '2026-03-17 20:00:00'. If spawned_at uses ISO 8601 with a timezone
        suffix (e.g. '2026-03-17T20:00:00+00:00'), string comparison with SQLite's
        output fails silently and stale-row cleanup never fires.
        """
        import re

        prompt = "---\ntask_id: t-ts\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-ts"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-ts")
        spawned_at = row["spawned_at"]

        # Must match 'YYYY-MM-DD HH:MM:SS' exactly — no 'T' separator, no timezone offset
        sqlite_naive_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        assert sqlite_naive_pattern.match(spawned_at), (
            f"spawned_at '{spawned_at}' does not match SQLite-compatible "
            f"'YYYY-MM-DD HH:MM:SS' format"
        )

        # Verify SQLite itself can compare it against datetime('now', '-30 minutes')
        conn = _open_db(tmp_path)
        try:
            result = conn.execute(
                "SELECT spawned_at > datetime('now', '-30 minutes') FROM agent_sessions"
                " WHERE id = 'agent-ts'"
            ).fetchone()
            # The just-inserted row was spawned within the last 30 minutes
            assert result is not None and result[0] == 1, (
                "SQLite age comparison returned unexpected result — format mismatch likely"
            )
        finally:
            conn.close()


    def test_input_summary_stored_from_prompt(self, tmp_path):
        """input_summary stores the first 500 chars of the agent prompt (issue #669).

        This enables ghost-detector and the dispatcher to reconstruct context
        if the agent fails or disappears without calling write_result.
        """
        long_prompt = "---\ntask_id: t-summary\nchat_id: 12345\n---\n" + "X" * 600
        hook_input = _make_hook_input(
            prompt=long_prompt,
            tool_response={"agentId": "agent-summary"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-summary")
        assert row is not None
        assert row["input_summary"] is not None
        # Must be truncated to 500 chars
        assert len(row["input_summary"]) == 500
        assert row["input_summary"] == long_prompt[:500]

    def test_input_summary_short_prompt_stored_in_full(self, tmp_path):
        """Short prompts are stored without truncation."""
        short_prompt = "---\ntask_id: t-short\n---\nDo a small thing."
        hook_input = _make_hook_input(
            prompt=short_prompt,
            tool_response={"agentId": "agent-short-prompt"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-short-prompt")
        assert row is not None
        assert row["input_summary"] == short_prompt

    def test_input_summary_empty_prompt_is_none(self, tmp_path):
        """Empty prompt stores None for input_summary (not an empty string)."""
        hook_input = _make_hook_input(
            prompt="",
            tool_response={"agentId": "agent-empty-prompt"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0

        row = _get_row(tmp_path, "agent-empty-prompt")
        assert row is not None
        assert row["input_summary"] is None


class TestHookNoAgentId:
    def test_missing_agent_id_exits_0_no_write(self, tmp_path):
        """If tool response has no agentId, exit 0 without writing agent_sessions rows."""
        prompt = "---\ntask_id: t-noid\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"result": "some output"},
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0
        db_path = tmp_path / "messages" / "config" / "agent_sessions.db"
        # The DB file may be created by the test isolator (conftest AtomicClaimDB),
        # but the hook must not have inserted any rows.
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()
                assert rows[0] == 0, "Missing agentId must not write any agent_sessions rows"
            except sqlite3.OperationalError:
                pass  # table doesn't exist — no rows written, test passes
            finally:
                conn.close()

    def test_none_response_exits_0(self, tmp_path):
        prompt = "---\ntask_id: t-none\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response=None,
        )
        exit_code, _, _ = _run_hook(hook_input, tmp_path)
        assert exit_code == 0


class TestHookFailureSafety:
    def test_malformed_json_exits_0(self, tmp_path):
        """Malformed stdin JSON must never crash the hook (exit 0)."""
        stdout_cap = StringIO()
        stderr_cap = StringIO()
        exit_code = None
        with (
            patch("sys.stdin", StringIO("not-valid-json{")),
            patch("sys.stdout", stdout_cap),
            patch("sys.stderr", stderr_cap),
            patch.dict("os.environ", {
                "LOBSTER_MESSAGES": str(tmp_path / "messages"),
                "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
            }),
        ):
            try:
                hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
                exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
            except SystemExit as e:
                exit_code = e.code

        assert exit_code == 0

    def test_db_failure_logs_and_exits_0(self, tmp_path):
        """DB write failure is logged to hook-failures.log, not re-raised."""
        # Point DB to a read-only directory to force failure
        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        (ro_dir / "config").mkdir()
        import os
        os.chmod(str(ro_dir / "config"), 0o444)

        prompt = "---\ntask_id: t-fail\n---"
        hook_input = _make_hook_input(
            prompt=prompt,
            tool_response={"agentId": "agent-fail"},
        )
        exit_code = None
        stdout_cap = StringIO()
        stderr_cap = StringIO()
        with (
            patch("sys.stdin", StringIO(json.dumps(hook_input))),
            patch("sys.stdout", stdout_cap),
            patch("sys.stderr", stderr_cap),
            patch.dict("os.environ", {
                "LOBSTER_MESSAGES": str(ro_dir),
                "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
            }),
        ):
            try:
                hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
                exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
            except SystemExit as e:
                exit_code = e.code

        os.chmod(str(ro_dir / "config"), 0o755)  # restore for cleanup
        assert exit_code == 0

        log_path = tmp_path / "workspace" / "logs" / "hook-failures.log"
        assert log_path.exists(), "Expected failure to be logged"
        log_content = log_path.read_text()
        assert "auto-register-agent" in log_content
