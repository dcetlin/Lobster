"""
Tests for the write_progress MCP tool (agent-channel protocol v1, §2).

handle_write_progress is the thin MCP-tool wrapper around
agent_channel.write_progress() — argument sanitization, wiring the real
(module-level) _claims_db in, and formatting the TextContent response. The
authorization/debounce/message-after-complete *behavior* itself is covered
in depth at the unit level in tests/unit/test_mcp_server/test_agent_channel.py
(TestWriteProgress); these tests only verify the tool-boundary wiring.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401 — pre-load for patch.multiple


class _FakeClaimsDB:
    """Duck-typed stand-in for claims.AtomicClaimDB — handle_write_progress
    only calls get_claim_status() through agent_channel.write_progress()."""

    def __init__(self, statuses: dict | None = None):
        self._statuses = dict(statuses or {})

    def get_claim_status(self, message_id):
        return self._statuses.get(message_id)


class TestHandleWriteProgress:
    @pytest.fixture
    def agent_replies(self, temp_messages_dir: Path) -> Path:
        d = temp_messages_dir / "agent-replies"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_open_claim_writes_status(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB({"req-1": "processing"})
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            result = asyncio.run(handle_write_progress({
                "request_id": "req-1",
                "status_text": "3/5 tests passing",
            }))

        assert "Status written" in result[0].text
        assert "req-1.ack.json" in result[0].text
        ack = json.loads((agent_replies / "req-1.ack.json").read_text())
        assert ack["text"] == "3/5 tests passing"
        # Structured shape (protocol v1.1): phase/pct default to null when
        # the caller doesn't supply them, not absent.
        assert ack["phase"] is None
        assert ack["pct"] is None

    def test_phase_and_pct_are_forwarded_to_the_ack(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB({"req-phase": "processing"})
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            result = asyncio.run(handle_write_progress({
                "request_id": "req-phase",
                "status_text": "running tests",
                "phase": "testing",
                "pct": 60,
            }))

        assert "Status written" in result[0].text
        ack = json.loads((agent_replies / "req-phase.ack.json").read_text())
        assert ack["phase"] == "testing"
        assert ack["pct"] == 60.0
        assert ack["text"] == "running tests"

    def test_non_numeric_pct_is_rejected(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB({"req-badpct": "processing"})
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            result = asyncio.run(handle_write_progress({
                "request_id": "req-badpct",
                "status_text": "x",
                "pct": "sixty",
            }))

        assert "Error" in result[0].text
        assert "pct" in result[0].text
        assert not (agent_replies / "req-badpct.ack.json").exists()

    def test_no_open_claim_is_refused(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB()  # no row for "req-2"
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            result = asyncio.run(handle_write_progress({
                "request_id": "req-2",
                "status_text": "sneaking in",
            }))

        assert "Error" in result[0].text
        assert "no OPEN claim" in result[0].text
        assert not (agent_replies / "req-2.ack.json").exists()

    def test_already_complete_is_a_noop_not_an_error(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB({"req-3": "processing"})
        (agent_replies / "req-3.json").write_text(json.dumps({"request_id": "req-3", "text": "done"}))
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            result = asyncio.run(handle_write_progress({
                "request_id": "req-3",
                "status_text": "still going?",
            }))

        assert "No-op" in result[0].text
        assert "COMPLETE" in result[0].text
        assert not (agent_replies / "req-3.ack.json").exists()

    def test_debounced_second_call_is_accepted_not_error(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB({"req-4": "processing"})
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            first = asyncio.run(handle_write_progress({
                "request_id": "req-4",
                "status_text": "status A",
            }))
            second = asyncio.run(handle_write_progress({
                "request_id": "req-4",
                "status_text": "status B",
            }))

        assert "Status written" in first[0].text
        assert "Accepted (debounced)" in second[0].text
        assert "Error" not in second[0].text
        ack = json.loads((agent_replies / "req-4.ack.json").read_text())
        assert ack["text"] == "status A"  # second write was skipped

    def test_missing_request_id_returns_error(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB({"req-5": "processing"})
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            result = asyncio.run(handle_write_progress({
                "status_text": "x",
            }))

        assert "Error" in result[0].text
        assert "request_id" in result[0].text

    def test_missing_status_text_returns_error(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB({"req-6": "processing"})
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            result = asyncio.run(handle_write_progress({
                "request_id": "req-6",
            }))

        assert "Error" in result[0].text
        assert "status_text" in result[0].text

    def test_invalid_request_id_characters_rejected(self, agent_replies: Path):
        fake_claims = _FakeClaimsDB({"../etc/passwd": "processing"})
        with patch.multiple(
            "src.mcp.inbox_server",
            AGENT_REPLIES_DIR=agent_replies,
            _claims_db=fake_claims,
        ):
            from src.mcp.inbox_server import handle_write_progress

            result = asyncio.run(handle_write_progress({
                "request_id": "../etc/passwd",
                "status_text": "x",
            }))

        assert "Error" in result[0].text
        assert "invalid characters" in result[0].text


class TestWriteProgressIsSessionGuarded:
    """write_progress performs a real filesystem write and must be gated the
    same way claim_and_ack/send_reply are — only the main dispatcher-session
    process should be able to invoke it (see _SESSION_GUARDED_TOOLS)."""

    def test_write_progress_is_in_session_guarded_tools(self):
        assert "write_progress" in src.mcp.inbox_server._SESSION_GUARDED_TOOLS
