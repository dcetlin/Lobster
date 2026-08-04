"""
Tests for scripts/lobster-chat.py's non-SSH-dependent logic: the argument
parser's defaults/flags and the pure envelope-building function.

Follow-up items approved for issue-13 continuity (see
~/dancetlin-infra/agent-chat/lobster-007.md / glyph-007.md):
  1. CLI timeout default 90s -> 300s
  2. request_id printed to stderr on every send, not just on timeout
  3. Optional `agent` identity field + CLI flag

Item 1 and 3 are covered here via build_parser()/build_request_message(),
extracted specifically so these behaviors are testable without an SSH round
trip. Item 2 (the unconditional stderr print) is covered end-to-end in
TestMainPrintsRequestIdOnSend below, using a fake ssh_run so main() runs its
real control flow with no real network call.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = REPO_ROOT / "scripts" / "lobster-chat.py"

# scripts/lobster-chat.py has a hyphen in its filename, so it can't be
# imported with a normal `import` statement — load it by path instead.
_spec = importlib.util.spec_from_file_location("lobster_chat", _MODULE_PATH)
lobster_chat = importlib.util.module_from_spec(_spec)
sys.modules["lobster_chat"] = lobster_chat
_spec.loader.exec_module(lobster_chat)


DEFAULT_TIMEOUT_SECONDS = 300  # protocol spec: long enough for a multi-minute
                                # subagent investigation, per glyph-007.md item 1


class TestTimeoutDefault:
    def test_default_timeout_is_300_seconds(self, monkeypatch):
        monkeypatch.delenv("LOBSTER_CHAT_TIMEOUT", raising=False)
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "hi"])
        assert args.timeout == DEFAULT_TIMEOUT_SECONDS

    def test_timeout_flag_still_overrides_default(self, monkeypatch):
        monkeypatch.delenv("LOBSTER_CHAT_TIMEOUT", raising=False)
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "--timeout", "45", "hi"])
        assert args.timeout == 45

    def test_timeout_env_var_still_overrides_default(self, monkeypatch):
        monkeypatch.setenv("LOBSTER_CHAT_TIMEOUT", "60")
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "hi"])
        assert args.timeout == 60


class TestAgentFlag:
    def test_agent_flag_defaults_to_none(self, monkeypatch):
        monkeypatch.delenv("LOBSTER_CHAT_AGENT", raising=False)
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "hi"])
        assert args.agent is None

    def test_agent_flag_sets_value(self, monkeypatch):
        monkeypatch.delenv("LOBSTER_CHAT_AGENT", raising=False)
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "--agent", "glyph", "hi"])
        assert args.agent == "glyph"

    def test_agent_env_var_sets_value(self, monkeypatch):
        monkeypatch.setenv("LOBSTER_CHAT_AGENT", "glyph")
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "hi"])
        assert args.agent == "glyph"

    def test_agent_flag_overrides_env_var(self, monkeypatch):
        monkeypatch.setenv("LOBSTER_CHAT_AGENT", "glyph")
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "--agent", "other-agent", "hi"])
        assert args.agent == "other-agent"


class TestBuildRequestMessage:
    def test_agent_field_included_when_given(self):
        msg = lobster_chat.build_request_message("hello", "rid-1", agent="glyph")
        assert msg["agent"] == "glyph"

    def test_agent_field_omitted_when_not_given(self):
        msg = lobster_chat.build_request_message("hello", "rid-1", agent=None)
        assert "agent" not in msg

    def test_agent_field_omitted_when_empty_string(self):
        msg = lobster_chat.build_request_message("hello", "rid-1", agent="")
        assert "agent" not in msg

    def test_required_envelope_fields_unaffected(self):
        msg = lobster_chat.build_request_message("hello", "rid-1", agent="glyph")
        assert msg["id"] == "rid-1"
        assert msg["source"] == "local-claude"
        assert msg["type"] == "text"
        assert msg["chat_id"] == "local-claude"
        assert msg["text"] == "hello"
        assert msg["request_id"] == "rid-1"


class TestMainPrintsRequestIdOnSend:
    """request_id must reach stderr as soon as the inbox write succeeds —
    not only when the CLI times out waiting for a reply (item 2)."""

    def test_request_id_printed_on_successful_reply_not_only_on_timeout(self, monkeypatch, capsys):
        calls = []

        class _FakeResult:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_ssh_run(target, remote_cmd, stdin_text=None):
            calls.append((target, remote_cmd, stdin_text))
            if len(calls) == 1:
                # inbox write
                return _FakeResult(returncode=0)
            # reply poll — succeeds immediately, so main() never reaches its
            # timeout branch. If request_id still appears on stderr, that
            # proves the print is unconditional, not timeout-gated.
            return _FakeResult(returncode=0, stdout='{"text": "reply text"}')

        monkeypatch.setattr(lobster_chat, "ssh_run", fake_ssh_run)
        monkeypatch.setattr(
            sys, "argv", ["lobster-chat.py", "--host", "vps", "--timeout", "5", "hello"]
        )

        rc = lobster_chat.main()

        captured = capsys.readouterr()
        assert rc == 0
        assert "reply text" in captured.out
        assert any(line.startswith("request_id=") for line in captured.err.splitlines())

    def test_agent_flag_reaches_written_inbox_message(self, monkeypatch, capsys):
        import json

        calls = []

        class _FakeResult:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_ssh_run(target, remote_cmd, stdin_text=None):
            calls.append((target, remote_cmd, stdin_text))
            if len(calls) == 1:
                return _FakeResult(returncode=0)
            return _FakeResult(returncode=0, stdout='{"text": "ok"}')

        monkeypatch.setattr(lobster_chat, "ssh_run", fake_ssh_run)
        monkeypatch.setattr(
            sys,
            "argv",
            ["lobster-chat.py", "--host", "vps", "--agent", "glyph", "--timeout", "5", "hello"],
        )

        rc = lobster_chat.main()

        assert rc == 0
        written_payload = json.loads(calls[0][2])
        assert written_payload["agent"] == "glyph"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
