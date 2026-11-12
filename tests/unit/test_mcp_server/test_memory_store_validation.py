"""
Tests for handle_memory_store validation of signal_type_hint (PR #1032).

Covers:
- An invalid signal_type_hint value is rejected with a descriptive error message
- A valid signal_type_hint value is accepted and stored
- No signal_type_hint (omitted) is accepted as valid (field is optional)

Pattern follows test_debug_alerts.py: patch _memory_provider and MemoryEvent,
call handle_memory_store directly, inspect the TextContent result.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/mcp is on sys.path (mirrors the pattern used throughout this package).
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401 — pre-load for patch resolution


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeMemoryProvider:
    """Minimal fake memory provider — store() always succeeds."""

    def store(self, event) -> int:
        return 42


class _FakeMemoryEvent:
    """Stand-in MemoryEvent that handle_memory_store can construct."""

    def __init__(self, *, id, timestamp, type, source, project, content,
                 metadata, valence="neutral", subject=None, signal_type_hint=None):
        self.id = id
        self.timestamp = timestamp
        self.type = type
        self.source = source
        self.project = project
        self.content = content
        self.metadata = metadata
        self.valence = valence
        self.subject = subject
        self.signal_type_hint = signal_type_hint


def _call_handle_memory_store(arguments: dict) -> list:
    """Invoke handle_memory_store with path isolation and fake memory provider."""
    with patch.multiple(
        "src.mcp.inbox_server",
        _memory_provider=_FakeMemoryProvider(),
        MemoryEvent=_FakeMemoryEvent,
        _emit_event=MagicMock(),
    ):
        from src.mcp.inbox_server import handle_memory_store
        return asyncio.run(handle_memory_store(arguments))


# ---------------------------------------------------------------------------
# Validation: invalid signal_type_hint
# ---------------------------------------------------------------------------

class TestSignalTypeHintValidation:
    """handle_memory_store rejects invalid signal_type_hint values."""

    def test_invalid_signal_type_hint_returns_error(self):
        """An unrecognized signal_type_hint value produces a TextContent error."""
        result = _call_handle_memory_store({
            "content": "Some event",
            "signal_type_hint": "not_a_real_type",
        })
        assert len(result) == 1
        text = result[0].text
        assert "Error" in text or "error" in text or "invalid" in text.lower()

    def test_invalid_signal_type_hint_error_names_the_bad_value(self):
        """The error message names the rejected value so the caller can fix it."""
        result = _call_handle_memory_store({
            "content": "Some event",
            "signal_type_hint": "badvalue",
        })
        assert "badvalue" in result[0].text

    def test_valid_signal_type_hint_is_accepted(self):
        """A valid signal_type_hint does not produce an error."""
        result = _call_handle_memory_store({
            "content": "A real design question",
            "signal_type_hint": "design_question",
        })
        # A successful store returns a TextContent that does NOT start with "Error"
        assert len(result) == 1
        assert not result[0].text.startswith("Error")

    def test_absent_signal_type_hint_is_accepted(self):
        """Omitting signal_type_hint is valid — the field is optional."""
        result = _call_handle_memory_store({"content": "No hint provided"})
        assert len(result) == 1
        assert not result[0].text.startswith("Error")

    def test_invalid_hint_does_not_call_store(self):
        """When signal_type_hint is invalid, store() is never called."""
        store_calls: list = []

        class _TrackingProvider:
            def store(self, event) -> int:
                store_calls.append(event)
                return 1

        with patch.multiple(
            "src.mcp.inbox_server",
            _memory_provider=_TrackingProvider(),
            MemoryEvent=_FakeMemoryEvent,
            _emit_event=MagicMock(),
        ):
            from src.mcp.inbox_server import handle_memory_store
            asyncio.run(handle_memory_store({
                "content": "Should not be stored",
                "signal_type_hint": "invalid_hint",
            }))

        assert len(store_calls) == 0, "store() must not be called when signal_type_hint is invalid"
