"""
Tests for write_wos_heartbeat token_usage parameter (issue #994).

Behavior verified (derived from spec, not from implementation):

- test_heartbeat_with_token_usage_stores_snapshot:
  When token_usage is passed to write_wos_heartbeat, the registry receives it
  and write_heartbeat is called with the correct integer value.

- test_heartbeat_without_token_usage_passes_none:
  When token_usage is omitted, write_heartbeat is called with token_usage=None
  (backwards compatible — no regression for agents that don't track tokens).

- test_heartbeat_token_usage_zero_is_accepted:
  token_usage=0 is valid (agent tracked tokens but consumed none yet).

- test_heartbeat_negative_token_usage_is_rejected:
  Negative token_usage is rejected — write_heartbeat is called with None.

- test_heartbeat_string_token_usage_is_rejected:
  String token_usage is rejected — write_heartbeat is called with None.

- test_heartbeat_float_token_usage_is_coerced_to_int:
  Float token_usage (e.g. from JSON deserialisation) is coerced to int.

- test_heartbeat_tool_schema_has_token_usage_property:
  The tool's inputSchema defines token_usage as an optional integer property.

- test_heartbeat_token_usage_not_required:
  The tool's inputSchema does NOT include token_usage in the 'required' list.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

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
    """Call handle_write_wos_heartbeat with a mocked WOSRegistry."""
    with patch.dict("sys.modules", {
        "orchestration.registry": MagicMock(WOSRegistry=MagicMock(return_value=registry_mock)),
    }):
        from src.mcp.inbox_server import handle_write_wos_heartbeat
        return asyncio.run(handle_write_wos_heartbeat(args))


def _make_registry_mock(rowcount: int = 1) -> MagicMock:
    mock = MagicMock()
    mock.write_heartbeat.return_value = rowcount
    return mock


def _parse_result(result: list) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# Tests: token_usage forwarding
# ---------------------------------------------------------------------------

class TestHeartbeatTokenUsageForwarding:
    """write_wos_heartbeat passes token_usage to Registry.write_heartbeat correctly."""

    def test_heartbeat_with_token_usage_stores_snapshot(self) -> None:
        """token_usage is forwarded to Registry.write_heartbeat as an integer."""
        registry_mock = _make_registry_mock(rowcount=1)
        result = _run_heartbeat({"uow_id": "uow_abc123", "token_usage": 500}, registry_mock)
        data = _parse_result(result)
        assert data == {"rowcount": 1}
        registry_mock.write_heartbeat.assert_called_once_with("uow_abc123", token_usage=500)

    def test_heartbeat_without_token_usage_passes_none(self) -> None:
        """Omitting token_usage passes token_usage=None to Registry (backwards compatible)."""
        registry_mock = _make_registry_mock(rowcount=1)
        _run_heartbeat({"uow_id": "uow_abc123"}, registry_mock)
        registry_mock.write_heartbeat.assert_called_once_with("uow_abc123", token_usage=None)

    def test_heartbeat_token_usage_zero_is_accepted(self) -> None:
        """token_usage=0 is valid and forwarded as 0."""
        registry_mock = _make_registry_mock(rowcount=1)
        _run_heartbeat({"uow_id": "uow_abc123", "token_usage": 0}, registry_mock)
        registry_mock.write_heartbeat.assert_called_once_with("uow_abc123", token_usage=0)

    def test_heartbeat_negative_token_usage_is_rejected(self) -> None:
        """Negative token_usage is invalid and passed as None."""
        registry_mock = _make_registry_mock(rowcount=1)
        _run_heartbeat({"uow_id": "uow_abc123", "token_usage": -1}, registry_mock)
        registry_mock.write_heartbeat.assert_called_once_with("uow_abc123", token_usage=None)

    def test_heartbeat_string_token_usage_is_rejected(self) -> None:
        """String token_usage is invalid and passed as None."""
        registry_mock = _make_registry_mock(rowcount=1)
        _run_heartbeat({"uow_id": "uow_abc123", "token_usage": "500"}, registry_mock)
        registry_mock.write_heartbeat.assert_called_once_with("uow_abc123", token_usage=None)

    def test_heartbeat_float_token_usage_is_coerced_to_int(self) -> None:
        """Float token_usage (e.g. from JSON) is coerced to int."""
        registry_mock = _make_registry_mock(rowcount=1)
        _run_heartbeat({"uow_id": "uow_abc123", "token_usage": 500.9}, registry_mock)
        registry_mock.write_heartbeat.assert_called_once_with("uow_abc123", token_usage=500)


# ---------------------------------------------------------------------------
# Tests: tool schema
# ---------------------------------------------------------------------------

class TestHeartbeatToolSchemaTokenUsage:
    """The write_wos_heartbeat tool schema correctly advertises token_usage."""

    def test_heartbeat_tool_schema_has_token_usage_property(self) -> None:
        """The tool's inputSchema defines token_usage as an integer property."""
        from src.mcp.inbox_server import list_tools
        tools = asyncio.run(list_tools())
        heartbeat_tool = next((t for t in tools if t.name == "write_wos_heartbeat"), None)
        assert heartbeat_tool is not None
        props = heartbeat_tool.inputSchema.get("properties", {})
        assert "token_usage" in props, (
            "token_usage must be in inputSchema.properties so agents know to pass it"
        )
        assert props["token_usage"].get("type") == "integer"

    def test_heartbeat_token_usage_not_required(self) -> None:
        """token_usage must NOT be in the required list — it is optional for backwards compat."""
        from src.mcp.inbox_server import list_tools
        tools = asyncio.run(list_tools())
        heartbeat_tool = next((t for t in tools if t.name == "write_wos_heartbeat"), None)
        assert heartbeat_tool is not None
        required = heartbeat_tool.inputSchema.get("required", [])
        assert "token_usage" not in required, (
            "token_usage must not be required — agents that omit it must not break"
        )
