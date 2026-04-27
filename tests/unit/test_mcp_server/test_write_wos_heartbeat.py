"""
Tests for the write_wos_heartbeat MCP tool (issue #849).

Behavior verified (derived from spec, not from implementation):

- test_heartbeat_success_returns_rowcount_1:
  When the registry write succeeds for an active UoW, the tool returns
  {"rowcount": 1}.

- test_heartbeat_returns_rowcount_0_when_uow_already_transitioned:
  When the UoW has already been transitioned (observation loop re-queued it),
  write_heartbeat returns 0 and the tool surfaces {"rowcount": 0}.

- test_heartbeat_missing_uow_id_returns_error:
  Calling the tool without uow_id returns {"error": ...}.

- test_heartbeat_empty_uow_id_returns_error:
  Calling the tool with an empty uow_id string returns {"error": ...}.

- test_heartbeat_registry_exception_returns_error_not_raise:
  When the registry raises (e.g. DB locked), the tool returns {"error": ...}
  without raising — agents must not crash due to a heartbeat failure.

- test_heartbeat_tool_is_registered_in_list_tools:
  The write_wos_heartbeat tool appears in the tool list returned by list_tools.

- test_heartbeat_tool_requires_uow_id_field:
  The tool's inputSchema marks uow_id as required.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parents[4]
for _p in [
    str(_ROOT / "src" / "mcp"),
    str(_ROOT / "src" / "agents"),
    str(_ROOT / "src"),
    str(_ROOT / "src" / "utils"),
    str(_ROOT),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import src.mcp.inbox_server  # noqa: F401 — pre-load so patch resolves it


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_heartbeat(args: dict, registry_mock: MagicMock) -> list:
    """
    Call handle_write_wos_heartbeat with a mocked WOSRegistry.

    Patches the WOSRegistry import inside inbox_server so the handler uses
    the mock instead of a real DB connection.
    """
    with patch(
        "src.mcp.inbox_server.handle_write_wos_heartbeat.__module__",
        "src.mcp.inbox_server",
    ):
        # Patch the WOSRegistry import path used inside handle_write_wos_heartbeat.
        with patch.dict("sys.modules", {
            "orchestration.registry": MagicMock(WOSRegistry=MagicMock(return_value=registry_mock)),
        }):
            from src.mcp.inbox_server import handle_write_wos_heartbeat
            return asyncio.run(handle_write_wos_heartbeat(args))


def _make_registry_mock(rowcount: int = 1) -> MagicMock:
    """Return a mock Registry instance where write_heartbeat returns rowcount."""
    mock = MagicMock()
    mock.write_heartbeat.return_value = rowcount
    return mock


def _parse_result(result: list) -> dict:
    """Parse the TextContent result list into a dict."""
    assert len(result) == 1
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# Tests: response format
# ---------------------------------------------------------------------------

class TestWriteWOSHeartbeatResponse:
    """The tool returns well-formed JSON in all cases."""

    def test_heartbeat_success_returns_rowcount_1(self) -> None:
        """When write_heartbeat succeeds, the tool returns {rowcount: 1}."""
        registry_mock = _make_registry_mock(rowcount=1)
        result = _run_heartbeat({"uow_id": "uow_abc123"}, registry_mock)
        data = _parse_result(result)
        assert data == {"rowcount": 1}

    def test_heartbeat_returns_rowcount_0_when_uow_already_transitioned(self) -> None:
        """When the UoW has transitioned, write_heartbeat returns 0 and the tool surfaces it."""
        registry_mock = _make_registry_mock(rowcount=0)
        result = _run_heartbeat({"uow_id": "uow_abc123"}, registry_mock)
        data = _parse_result(result)
        assert data == {"rowcount": 0}, (
            "rowcount=0 is the signal that the Steward re-queued the UoW — "
            "agents must see this to know to stop"
        )

    def test_heartbeat_missing_uow_id_returns_error(self) -> None:
        """Calling without uow_id returns an error response."""
        registry_mock = _make_registry_mock()
        result = _run_heartbeat({}, registry_mock)
        data = _parse_result(result)
        assert "error" in data

    def test_heartbeat_empty_uow_id_returns_error(self) -> None:
        """Calling with an empty uow_id returns an error response."""
        registry_mock = _make_registry_mock()
        result = _run_heartbeat({"uow_id": ""}, registry_mock)
        data = _parse_result(result)
        assert "error" in data

    def test_heartbeat_registry_exception_returns_error_not_raise(self) -> None:
        """When the registry raises, the tool returns an error dict without raising."""
        registry_mock = MagicMock()
        registry_mock.write_heartbeat.side_effect = RuntimeError("DB locked")
        # Should not raise
        result = _run_heartbeat({"uow_id": "uow_abc123"}, registry_mock)
        data = _parse_result(result)
        assert "error" in data
        assert "RuntimeError" in data["error"] or "DB locked" in data["error"]


# ---------------------------------------------------------------------------
# Tests: tool registration
# ---------------------------------------------------------------------------

class TestWriteWOSHeartbeatToolRegistration:
    """The tool is properly registered in the MCP server tool list."""

    def test_heartbeat_tool_is_registered_in_list_tools(self) -> None:
        """write_wos_heartbeat appears in the tool list returned by the server."""
        from src.mcp.inbox_server import list_tools
        tools = asyncio.run(list_tools())
        tool_names = [t.name for t in tools]
        assert "write_wos_heartbeat" in tool_names, (
            "write_wos_heartbeat must be in the tool list so subagents can "
            "discover and call it via the MCP protocol"
        )

    def test_heartbeat_tool_requires_uow_id_field(self) -> None:
        """The tool's inputSchema marks uow_id as required."""
        from src.mcp.inbox_server import list_tools
        tools = asyncio.run(list_tools())
        heartbeat_tool = next(
            (t for t in tools if t.name == "write_wos_heartbeat"), None
        )
        assert heartbeat_tool is not None
        schema = heartbeat_tool.inputSchema
        assert "uow_id" in schema.get("required", []), (
            "uow_id must be marked required so MCP clients enforce its presence"
        )

    def test_heartbeat_tool_schema_has_uow_id_property(self) -> None:
        """The tool's inputSchema defines uow_id as a string property."""
        from src.mcp.inbox_server import list_tools
        tools = asyncio.run(list_tools())
        heartbeat_tool = next(
            (t for t in tools if t.name == "write_wos_heartbeat"), None
        )
        assert heartbeat_tool is not None
        props = heartbeat_tool.inputSchema.get("properties", {})
        assert "uow_id" in props
        assert props["uow_id"].get("type") == "string"
