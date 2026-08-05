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

Agent-channel protocol v1 (client-side piece, see
~/lobster-workspace/assessments/agent-channel-protocol-proposal.md §2 item 3
and §3.2/§5):
  4. Client-side read loop prints .ack.json status changes to stderr while
     polling for the terminal reply (print-on-change, dedupe repeats).
  5. `--for <agent>` discovery mode lists+cats agent-replies/by-agent/<slug>/.

Items 4 and 5 are covered via the pure helpers extracted for testability
(parse_ack_status, normalize_agent_slug, format_for_agent_entry) plus
end-to-end tests of main()/run_for_agent_discovery() using a fake ssh_run,
following the same pattern as TestMainPrintsRequestIdOnSend.
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


class TestParseAckStatus:
    """parse_ack_status extracts a .ack.json's progress-note text, tolerating
    every non-happy-path input a real ssh `cat ... 2>/dev/null` can produce."""

    def test_extracts_text_from_valid_ack(self):
        raw = '{"request_id": "rid-1", "text": "working on it", "ts": "2026-01-01T00:00:00Z"}'
        assert lobster_chat.parse_ack_status(raw) == "working on it"

    def test_strips_whitespace(self):
        raw = '{"text": "  still going  "}'
        assert lobster_chat.parse_ack_status(raw) == "still going"

    def test_none_for_missing_file(self):
        # cat on a nonexistent file with 2>/dev/null yields empty stdout
        assert lobster_chat.parse_ack_status("") is None

    def test_none_for_none_input(self):
        assert lobster_chat.parse_ack_status(None) is None

    def test_none_for_malformed_json(self):
        assert lobster_chat.parse_ack_status("{not valid json") is None

    def test_none_for_non_object_json(self):
        assert lobster_chat.parse_ack_status("[1, 2, 3]") is None

    def test_none_for_empty_text_field(self):
        assert lobster_chat.parse_ack_status('{"text": ""}') is None

    def test_none_for_missing_text_field(self):
        assert lobster_chat.parse_ack_status('{"request_id": "rid-1"}') is None


class TestAgentSlugNormalization:
    """normalize_agent_slug per protocol proposal §6 dial 4: lowercase-fold,
    strict-reject everything else (no silent stripping/substitution)."""

    def test_lowercases(self):
        assert lobster_chat.normalize_agent_slug("Bloom") == "bloom"

    def test_already_lowercase_passthrough(self):
        assert lobster_chat.normalize_agent_slug("glyph") == "glyph"

    def test_allows_digits_underscore_hyphen(self):
        assert lobster_chat.normalize_agent_slug("Agent_007-X") == "agent_007-x"

    def test_rejects_whitespace(self):
        with pytest.raises(ValueError):
            lobster_chat.normalize_agent_slug("bloom ")

    def test_rejects_embedded_space(self):
        with pytest.raises(ValueError):
            lobster_chat.normalize_agent_slug("bloom agent")

    def test_rejects_path_separator(self):
        with pytest.raises(ValueError):
            lobster_chat.normalize_agent_slug("../etc/passwd")

    def test_rejects_special_characters(self):
        with pytest.raises(ValueError):
            lobster_chat.normalize_agent_slug("bloom@agent")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            lobster_chat.normalize_agent_slug("")


class TestFormatForAgentEntry:
    """format_for_agent_entry — pure formatting for --for discovery output."""

    def test_prefers_reply_when_present(self):
        reply_raw = '{"request_id": "rid-1", "text": "the answer", "ts": "t", "in_reply_to": "rid-1"}'
        ack_raw = '{"text": "stale progress note"}'
        out = lobster_chat.format_for_agent_entry("rid-1", reply_raw, ack_raw)
        assert "rid-1" in out
        assert "[reply] the answer" in out
        assert "stale progress note" not in out

    def test_falls_back_to_ack_when_no_reply_yet(self):
        out = lobster_chat.format_for_agent_entry("rid-2", "", '{"text": "still working"}')
        assert "[status] still working" in out
        assert "no reply yet" in out

    def test_falls_back_to_nothing_yet_when_neither_present(self):
        out = lobster_chat.format_for_agent_entry("rid-3", "", "")
        assert "no reply or status yet" in out

    def test_tolerates_malformed_reply_json(self):
        out = lobster_chat.format_for_agent_entry("rid-4", "{not json", '{"text": "working"}')
        assert "[status] working" in out


class TestAckReadLoopPrintsOnChange:
    """main()'s poll loop must print .ack.json status changes to stderr,
    deduped so a repeated identical status is only printed once."""

    def test_prints_status_changes_deduped(self, monkeypatch, capsys):
        calls = []

        class _FakeResult:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        # Sequence: inbox write, then repeated (reply-miss, ack) pairs, then
        # a final reply hit. Ack status: "working" (x2, deduped to 1 print),
        # then "almost done" (1 print), then reply lands.
        ack_sequence = [
            '{"text": "working"}',
            '{"text": "working"}',
            '{"text": "almost done"}',
        ]

        def fake_ssh_run(target, remote_cmd, stdin_text=None):
            calls.append((target, remote_cmd, stdin_text))
            if len(calls) == 1:
                return _FakeResult(returncode=0)  # inbox write
            # Alternate: reply poll (miss) then ack poll, per iteration of
            # main()'s loop, until the ack sequence is exhausted — then let
            # the next reply poll succeed.
            is_reply_poll = (len(calls) % 2) == 0
            if is_reply_poll:
                ack_index = (len(calls) // 2) - 1
                if ack_index >= len(ack_sequence):
                    return _FakeResult(returncode=0, stdout='{"text": "final answer"}')
                return _FakeResult(returncode=0, stdout="")  # no reply yet
            ack_index = (len(calls) // 2) - 1
            if 0 <= ack_index < len(ack_sequence):
                return _FakeResult(returncode=0, stdout=ack_sequence[ack_index])
            return _FakeResult(returncode=0, stdout="")

        monkeypatch.setattr(lobster_chat, "ssh_run", fake_ssh_run)
        monkeypatch.setattr(lobster_chat.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            sys, "argv", ["lobster-chat.py", "--host", "vps", "--timeout", "30", "hello"]
        )

        rc = lobster_chat.main()

        captured = capsys.readouterr()
        assert rc == 0
        assert "final answer" in captured.out
        status_lines = [line for line in captured.err.splitlines() if line.startswith("[status]")]
        assert status_lines == ["[status] working", "[status] almost done"]


class TestForAgentFlag:
    def test_for_flag_defaults_to_none(self):
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "hi"])
        assert args.for_agent is None

    def test_for_flag_sets_value(self):
        args = lobster_chat.build_parser().parse_args(["--host", "vps", "--for", "bloom"])
        assert args.for_agent == "bloom"


class TestRunForAgentDiscovery:
    """run_for_agent_discovery — end-to-end with a fake ssh_run, no real SSH."""

    def test_lists_and_cats_by_agent_directory(self, monkeypatch, capsys):
        calls = []

        class _FakeResult:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_ssh_run(target, remote_cmd, stdin_text=None):
            calls.append(remote_cmd)
            if remote_cmd.startswith("ls -1"):
                return _FakeResult(returncode=0, stdout="rid-1\nrid-2\n")
            if "rid-1.json" in remote_cmd and ".ack" not in remote_cmd:
                return _FakeResult(
                    returncode=0,
                    stdout='{"request_id": "rid-1", "text": "done answer", "ts": "t", "in_reply_to": "rid-1"}',
                )
            if "rid-1.ack.json" in remote_cmd:
                return _FakeResult(returncode=0, stdout="")
            if "rid-2.json" in remote_cmd and ".ack" not in remote_cmd:
                return _FakeResult(returncode=0, stdout="")
            if "rid-2.ack.json" in remote_cmd:
                return _FakeResult(returncode=0, stdout='{"text": "in progress"}')
            return _FakeResult(returncode=0, stdout="")

        monkeypatch.setattr(lobster_chat, "ssh_run", fake_ssh_run)

        rc = lobster_chat.run_for_agent_discovery("lobster@vps", "Bloom")

        captured = capsys.readouterr()
        assert rc == 0
        assert "rid-1" in captured.out
        assert "[reply] done answer" in captured.out
        assert "rid-2" in captured.out
        assert "[status] in progress" in captured.out
        # slug is lowercase-normalized before hitting the filesystem
        assert any("/by-agent/bloom/" in c for c in calls)

    def test_empty_directory_handled_cleanly(self, monkeypatch, capsys):
        class _FakeResult:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_ssh_run(target, remote_cmd, stdin_text=None):
            return _FakeResult(returncode=0, stdout="")

        monkeypatch.setattr(lobster_chat, "ssh_run", fake_ssh_run)

        rc = lobster_chat.run_for_agent_discovery("lobster@vps", "nobody")

        captured = capsys.readouterr()
        assert rc == 0
        assert "No messages found" in captured.err

    def test_invalid_agent_identity_rejected(self, monkeypatch, capsys):
        def fake_ssh_run(target, remote_cmd, stdin_text=None):
            raise AssertionError("should not reach ssh_run for an invalid identity")

        monkeypatch.setattr(lobster_chat, "ssh_run", fake_ssh_run)

        rc = lobster_chat.run_for_agent_discovery("lobster@vps", "bad agent")

        captured = capsys.readouterr()
        assert rc == 1
        assert "Error" in captured.err

    def test_main_routes_for_flag_to_discovery(self, monkeypatch, capsys):
        class _FakeResult:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_ssh_run(target, remote_cmd, stdin_text=None):
            if remote_cmd.startswith("ls -1"):
                return _FakeResult(returncode=0, stdout="")
            return _FakeResult(returncode=0, stdout="")

        monkeypatch.setattr(lobster_chat, "ssh_run", fake_ssh_run)
        monkeypatch.setattr(sys, "argv", ["lobster-chat.py", "--host", "vps", "--for", "glyph"])

        rc = lobster_chat.main()

        captured = capsys.readouterr()
        assert rc == 0
        assert "No messages found for agent 'glyph'" in captured.err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
