"""
Unit tests for hooks/wos-execute-gate.py

Tests cover:
- check_gate(): passes for non-wos_execute message types
- check_gate(): passes for wos_execute with _processing_started_at present
- check_gate(): misses for wos_execute without _processing_started_at
- main(): exits 0 for non-mark_processed tool calls
- main(): exits 0 for mark_processed on non-wos_execute messages (no gate miss)
- main(): exits 0 for mark_processed on wos_execute with prior mark_processing (gate pass)
- main(): exits 0 even on gate miss (never blocks; logs instead)
- main(): calls write_observation on gate miss
- main(): exits 0 when message file not found
- main(): exits 0 on malformed stdin
"""

import importlib.util
import json
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest


# ---------------------------------------------------------------------------
# Helpers: load hook module
# ---------------------------------------------------------------------------

def _load_hook():
    """Load hooks/wos-execute-gate.py as a module without executing main()."""
    hooks_dir = Path(__file__).parent.parent / "hooks"
    hook_path = hooks_dir / "wos-execute-gate.py"
    spec = importlib.util.spec_from_file_location("wos_execute_gate", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers: message builders
# ---------------------------------------------------------------------------

def _make_wos_execute_msg(*, with_processing_started: bool = True) -> dict:
    """Build a wos_execute message dict."""
    msg: dict = {
        "id": "abc-123",
        "type": "wos_execute",
        "uow_id": "uow-456",
        "source": "system",
        "chat_id": "8075091586",
    }
    if with_processing_started:
        msg["_processing_started_at"] = "2026-04-22T10:00:00+00:00"
    return msg


def _make_text_msg() -> dict:
    """Build an ordinary text message dict."""
    return {
        "id": "def-789",
        "type": "text",
        "text": "hello",
        "source": "telegram",
        "chat_id": "8075091586",
        "_processing_started_at": "2026-04-22T10:00:00+00:00",
    }


def _make_subagent_notification_msg() -> dict:
    """Build a subagent_notification message dict."""
    return {
        "id": "ghi-012",
        "type": "subagent_notification",
        "source": "system",
        "_processing_started_at": "2026-04-22T10:01:00+00:00",
    }


def _hook_input(message_id: str, tool_name: str = "mcp__lobster-inbox__mark_processed") -> dict:
    """Build the JSON payload that Claude Code injects into PostToolUse hooks."""
    return {
        "tool_name": tool_name,
        "tool_input": {"message_id": message_id},
        "tool_response": {},
    }


def _run_main(hook_mod, hook_data: dict, messages_by_id: "dict | None" = None) -> int:
    """Call hook_mod.main() with mocked stdin and file I/O. Returns SystemExit code."""
    messages_by_id = messages_by_id or {}

    def fake_read_message(message_id: str) -> "dict | None":
        return messages_by_id.get(message_id)

    stdin_json = json.dumps(hook_data)
    with patch("sys.stdin", StringIO(stdin_json)), \
         patch.object(hook_mod, "_read_message", side_effect=fake_read_message), \
         patch.object(hook_mod, "_log_gate_miss"), \
         patch.object(hook_mod, "_call_write_observation"):
        try:
            hook_mod.main()
        except SystemExit as e:
            return e.code if e.code is not None else 0
    return 0


def _run_main_capture_calls(hook_mod, hook_data: dict, messages_by_id: "dict | None" = None):
    """Like _run_main but returns (exit_code, log_gate_miss_calls, write_obs_calls)."""
    messages_by_id = messages_by_id or {}

    def fake_read_message(message_id: str) -> "dict | None":
        return messages_by_id.get(message_id)

    stdin_json = json.dumps(hook_data)
    mock_log = MagicMock()
    mock_obs = MagicMock()

    with patch("sys.stdin", StringIO(stdin_json)), \
         patch.object(hook_mod, "_read_message", side_effect=fake_read_message), \
         patch.object(hook_mod, "_log_gate_miss", mock_log), \
         patch.object(hook_mod, "_call_write_observation", mock_obs):
        try:
            hook_mod.main()
            code = 0
        except SystemExit as e:
            code = e.code if e.code is not None else 0

    return code, mock_log.call_args_list, mock_obs.call_args_list


# ---------------------------------------------------------------------------
# Tests: check_gate (pure function — no I/O)
# ---------------------------------------------------------------------------

class TestCheckGate:
    """check_gate() classifies messages without any I/O."""

    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_passes_for_non_wos_execute_type(self):
        """Non-wos_execute messages always pass the gate regardless of other fields."""
        msg = _make_text_msg()
        del msg["_processing_started_at"]  # even without this, should pass
        passed, reason = self.mod.check_gate(msg)
        assert passed is True
        assert "not wos_execute" in reason

    def test_passes_for_subagent_notification_type(self):
        """subagent_notification messages pass the gate (not wos_execute)."""
        msg = _make_subagent_notification_msg()
        passed, reason = self.mod.check_gate(msg)
        assert passed is True

    def test_passes_for_wos_execute_with_processing_started(self):
        """wos_execute with _processing_started_at present means mark_processing ran — gate passes."""
        msg = _make_wos_execute_msg(with_processing_started=True)
        passed, reason = self.mod.check_gate(msg)
        assert passed is True
        assert "_processing_started_at" in reason or "mark_processing" in reason

    def test_misses_for_wos_execute_without_processing_started(self):
        """wos_execute without _processing_started_at is a gate miss."""
        msg = _make_wos_execute_msg(with_processing_started=False)
        passed, reason = self.mod.check_gate(msg)
        assert passed is False
        assert "without prior mark_processing" in reason

    def test_passes_for_empty_type_field(self):
        """A message with no type field is not wos_execute — gate passes."""
        msg = {"id": "xyz"}
        passed, _ = self.mod.check_gate(msg)
        assert passed is True

    def test_misses_only_when_type_is_exact_wos_execute(self):
        """Gate only misses for the exact type string 'wos_execute', not variations."""
        for non_matching_type in ("wos-execute", "WOS_EXECUTE", "wos_execute_result", ""):
            msg = {"type": non_matching_type}  # no _processing_started_at
            passed, _ = self.mod.check_gate(msg)
            assert passed is True, f"Expected gate to pass for type={non_matching_type!r}"


# ---------------------------------------------------------------------------
# Tests: main() routing
# ---------------------------------------------------------------------------

class TestMainRouting:
    """main() routes correctly based on tool_name and message type."""

    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_exits_0_for_non_mark_processed_tool(self):
        """Hook exits 0 immediately for tools other than mark_processed."""
        data = _hook_input("abc-123", tool_name="mcp__lobster-inbox__mark_processing")
        code = _run_main(self.mod, data)
        assert code == 0

    def test_exits_0_for_agent_tool(self):
        """Hook exits 0 for Agent tool calls (not a mark_processed call)."""
        data = {"tool_name": "Agent", "tool_input": {"prompt": "do something"}, "tool_response": {}}
        code = _run_main(self.mod, data)
        assert code == 0

    def test_exits_0_on_malformed_stdin(self):
        """Hook exits 0 (allow) when stdin is not valid JSON."""
        with patch("sys.stdin", StringIO("not valid json")):
            try:
                self.mod.main()
                code = 0
            except SystemExit as e:
                code = e.code if e.code is not None else 0
        assert code == 0

    def test_exits_0_when_message_id_missing(self):
        """Hook exits 0 when mark_processed is called with no message_id."""
        data = {"tool_name": "mcp__lobster-inbox__mark_processed", "tool_input": {}}
        code = _run_main(self.mod, data, messages_by_id={})
        assert code == 0

    def test_exits_0_when_message_not_found(self):
        """Hook exits 0 when the message file cannot be found (best-effort)."""
        data = _hook_input("nonexistent-id")
        code = _run_main(self.mod, data, messages_by_id={})
        assert code == 0


# ---------------------------------------------------------------------------
# Tests: gate pass scenarios (no gate miss, no side effects)
# ---------------------------------------------------------------------------

class TestGatePass:
    """Gate passes cleanly — no log entry, no write_observation call."""

    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_no_gate_miss_for_ordinary_text_message(self):
        """mark_processed on a text message never triggers gate miss."""
        msg = _make_text_msg()
        data = _hook_input(msg["id"])
        code, log_calls, obs_calls = _run_main_capture_calls(
            self.mod, data, messages_by_id={msg["id"]: msg}
        )
        assert code == 0
        assert log_calls == []
        assert obs_calls == []

    def test_no_gate_miss_for_subagent_notification(self):
        """mark_processed on subagent_notification never triggers gate miss."""
        msg = _make_subagent_notification_msg()
        data = _hook_input(msg["id"])
        code, log_calls, obs_calls = _run_main_capture_calls(
            self.mod, data, messages_by_id={msg["id"]: msg}
        )
        assert code == 0
        assert log_calls == []
        assert obs_calls == []

    def test_no_gate_miss_for_wos_execute_with_mark_processing(self):
        """wos_execute that went through mark_processing (_processing_started_at set) passes cleanly."""
        msg = _make_wos_execute_msg(with_processing_started=True)
        data = _hook_input(msg["id"])
        code, log_calls, obs_calls = _run_main_capture_calls(
            self.mod, data, messages_by_id={msg["id"]: msg}
        )
        assert code == 0
        assert log_calls == []
        assert obs_calls == []


# ---------------------------------------------------------------------------
# Tests: gate miss scenarios
# ---------------------------------------------------------------------------

class TestGateMiss:
    """Gate miss: wos_execute processed without prior mark_processing."""

    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_gate_miss_fires_for_wos_execute_without_mark_processing(self):
        """mark_processed on wos_execute without _processing_started_at triggers gate miss."""
        msg = _make_wos_execute_msg(with_processing_started=False)
        data = _hook_input(msg["id"])
        code, log_calls, obs_calls = _run_main_capture_calls(
            self.mod, data, messages_by_id={msg["id"]: msg}
        )
        # Hook must not block (exit 0) even on gate miss
        assert code == 0
        # Gate miss must be logged
        assert len(log_calls) == 1
        # write_observation must be called
        assert len(obs_calls) == 1

    def test_gate_miss_passes_message_id_to_log(self):
        """_log_gate_miss is called with the correct message_id."""
        msg = _make_wos_execute_msg(with_processing_started=False)
        data = _hook_input(msg["id"])
        _, log_calls, _ = _run_main_capture_calls(
            self.mod, data, messages_by_id={msg["id"]: msg}
        )
        assert log_calls[0].args[0] == msg["id"]

    def test_gate_miss_passes_message_id_to_write_observation(self):
        """_call_write_observation is called with the correct message_id."""
        msg = _make_wos_execute_msg(with_processing_started=False)
        data = _hook_input(msg["id"])
        _, _, obs_calls = _run_main_capture_calls(
            self.mod, data, messages_by_id={msg["id"]: msg}
        )
        # _call_write_observation(message_id) — first positional arg
        assert obs_calls[0].args[0] == msg["id"]

    def test_hook_does_not_block_even_on_gate_miss(self):
        """Exit code is 0 on gate miss — hook warns but never blocks mark_processed."""
        msg = _make_wos_execute_msg(with_processing_started=False)
        data = _hook_input(msg["id"])
        code, _, _ = _run_main_capture_calls(
            self.mod, data, messages_by_id={msg["id"]: msg}
        )
        assert code == 0

    def test_no_double_miss_when_processing_started_is_empty_string(self):
        """An empty _processing_started_at string is treated as absent — gate miss fires."""
        msg = _make_wos_execute_msg(with_processing_started=False)
        msg["_processing_started_at"] = ""  # empty string — falsy
        data = _hook_input(msg["id"])
        _, log_calls, obs_calls = _run_main_capture_calls(
            self.mod, data, messages_by_id={msg["id"]: msg}
        )
        assert len(log_calls) == 1
        assert len(obs_calls) == 1


# ---------------------------------------------------------------------------
# Tests: check_gate as pure function (no stdlib, exhaustive)
# ---------------------------------------------------------------------------

class TestCheckGatePureFunction:
    """check_gate() is a pure function — test it in isolation with varied inputs."""

    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_returns_tuple_of_bool_and_str(self):
        """check_gate always returns a (bool, str) tuple."""
        result = self.mod.check_gate({"type": "text"})
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)

    def test_passes_for_cron_reminder_type(self):
        """cron_reminder messages (not wos_execute) pass the gate."""
        passed, _ = self.mod.check_gate({"type": "cron_reminder"})
        assert passed is True

    def test_passes_for_wos_execute_result_type(self):
        """wos_execute_result (distinct from wos_execute) passes the gate."""
        passed, _ = self.mod.check_gate({"type": "wos_execute_result"})
        assert passed is True
