"""
Tests for the _lobster_meta pre-classification envelope (issue #1023).

All tests are pure unit tests against lobster_meta.py — no MCP, Telegram,
or network calls. The module is dependency-free (like message_types.py) so
it can be imported and tested in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# lobster_meta.py lives in src/mcp/ alongside inbox_server.py.
# Add that directory to sys.path so we can import it directly.
_MCP_DIR = str(Path(__file__).resolve().parents[3] / "src" / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

from lobster_meta import (  # noqa: E402
    build_lobster_meta,
    _classify_intent,
    _classify_urgency,
    _is_user_facing,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(
    text: str = "",
    msg_type: str = "text",
    source: str = "telegram",
    chat_id: int = 12345,
) -> dict:
    """Build a minimal message dict for testing."""
    return {"text": text, "type": msg_type, "source": source, "chat_id": chat_id}


# ---------------------------------------------------------------------------
# _classify_intent
# ---------------------------------------------------------------------------


class TestClassifyIntent:
    """_classify_intent returns the right intent_class for various inputs."""

    def test_system_type_returns_system(self) -> None:
        """Any system message type maps to 'system'."""
        assert _classify_intent("whatever text", "wos_execute") == "system"
        assert _classify_intent("scheduled task", "scheduled_reminder") == "system"
        assert _classify_intent("done", "subagent_result") == "system"

    def test_reaction_type_returns_reaction(self) -> None:
        """reaction type maps to 'reaction'."""
        assert _classify_intent("", "reaction") == "reaction"

    def test_code_keywords(self) -> None:
        """Messages with code keywords map to 'code'."""
        assert _classify_intent("There's a bug in the auth module", "text") == "code"
        assert _classify_intent("open a pull request for this fix", "text") == "code"
        assert _classify_intent("deploy failed with traceback", "text") == "code"
        assert _classify_intent("refactor the user model class", "text") == "code"

    def test_question_keywords(self) -> None:
        """Messages that are questions map to 'question'."""
        assert _classify_intent("What is the status?", "text") == "question"
        assert _classify_intent("How do I restart the dispatcher?", "text") == "question"
        assert _classify_intent("Can you explain the WOS pipeline?", "text") == "question"
        assert _classify_intent("Is there a log for this?", "text") == "question"

    def test_emotional_keywords(self) -> None:
        """Messages with emotional content map to 'emotional'."""
        assert _classify_intent("I'm feeling really anxious about the launch", "text") == "emotional"
        assert _classify_intent("I'm overwhelmed with all these tasks", "text") == "emotional"
        assert _classify_intent("I'm grateful for your help", "text") == "emotional"

    def test_operational_keywords(self) -> None:
        """Messages with operational keywords map to 'operational'."""
        assert _classify_intent("Schedule a reminder for tomorrow", "text") == "operational"
        assert _classify_intent("Check the WOS status", "text") == "operational"
        assert _classify_intent("Update the config settings", "text") == "operational"

    def test_fallback_to_operational(self) -> None:
        """Messages with no recognized keywords fall back to 'operational'."""
        result = _classify_intent("hi there", "text")
        assert result == "operational"

    def test_code_takes_priority_over_question(self) -> None:
        """Code patterns are checked before question patterns."""
        # Has "bug" (code) AND ends with "?" (question) — code wins because it's first
        result = _classify_intent("Is there a bug in the deploy script?", "text")
        # "bug" matches code, "?" matches question — code pattern checked first
        assert result == "code"

    def test_empty_text_returns_operational(self) -> None:
        """Empty text defaults to 'operational' (not a crash)."""
        assert _classify_intent("", "text") == "operational"


# ---------------------------------------------------------------------------
# _classify_urgency
# ---------------------------------------------------------------------------


class TestClassifyUrgency:
    """_classify_urgency returns the right urgency level."""

    def test_high_urgency_keywords(self) -> None:
        """Messages with high-urgency keywords return 'high'."""
        assert _classify_urgency("This is urgent", "text") == "high"
        assert _classify_urgency("Fix this ASAP", "text") == "high"
        assert _classify_urgency("Production is down", "text") == "high"
        assert _classify_urgency("The service is broken", "text") == "high"
        assert _classify_urgency("Critical issue in prod", "text") == "high"

    def test_low_urgency_keywords(self) -> None:
        """Messages with low-urgency keywords return 'low'."""
        assert _classify_urgency("Whenever you get a chance", "text") == "low"
        assert _classify_urgency("No rush on this", "text") == "low"
        assert _classify_urgency("Low priority task for the backlog", "text") == "low"
        assert _classify_urgency("FYI, just letting you know", "text") == "low"

    def test_normal_urgency_is_default(self) -> None:
        """Messages with no urgency signals return 'normal'."""
        assert _classify_urgency("Can you check on this?", "text") == "normal"
        assert _classify_urgency("Update the documentation", "text") == "normal"

    def test_system_messages_are_normal(self) -> None:
        """System message types always return 'normal'."""
        assert _classify_urgency("urgent fix needed", "subagent_result") == "normal"
        assert _classify_urgency("broken", "wos_execute") == "normal"

    def test_reaction_messages_are_normal(self) -> None:
        """Reaction type always returns 'normal'."""
        assert _classify_urgency("broken", "reaction") == "normal"


# ---------------------------------------------------------------------------
# _is_user_facing
# ---------------------------------------------------------------------------


class TestIsUserFacing:
    """_is_user_facing correctly identifies user-facing vs system messages."""

    def test_telegram_user_message_is_user_facing(self) -> None:
        """Regular telegram messages with non-zero chat_id are user-facing."""
        assert _is_user_facing("telegram", 12345, "text") is True

    def test_slack_message_is_user_facing(self) -> None:
        """Slack messages with non-zero chat_id are user-facing."""
        assert _is_user_facing("slack", 99999, "text") is True

    def test_system_source_is_not_user_facing(self) -> None:
        """Messages from source='system' are never user-facing."""
        assert _is_user_facing("system", 12345, "text") is False

    def test_chat_id_zero_is_not_user_facing(self) -> None:
        """Messages with chat_id=0 are not user-facing (system/internal)."""
        assert _is_user_facing("telegram", 0, "text") is False

    def test_system_type_is_not_user_facing(self) -> None:
        """System message types are never user-facing regardless of source."""
        assert _is_user_facing("telegram", 12345, "wos_execute") is False
        assert _is_user_facing("telegram", 12345, "subagent_result") is False
        assert _is_user_facing("telegram", 12345, "scheduled_reminder") is False

    def test_unknown_source_is_not_user_facing(self) -> None:
        """Unknown sources are not user-facing."""
        assert _is_user_facing("unknown-channel", 12345, "text") is False

    def test_none_chat_id_is_user_facing_for_valid_source(self) -> None:
        """None chat_id (missing field) is not treated as zero."""
        # chat_id=None means the field is absent, not zero — should not block
        assert _is_user_facing("telegram", None, "text") is True


# ---------------------------------------------------------------------------
# build_lobster_meta — full integration
# ---------------------------------------------------------------------------


class TestBuildLobsterMeta:
    """build_lobster_meta returns a complete _lobster_meta dict."""

    def test_returns_all_required_fields(self) -> None:
        """Result always contains all four fields."""
        result = build_lobster_meta(_msg("hello"))
        assert "intent_class" in result
        assert "urgency" in result
        assert "is_user_facing" in result
        assert "preprocessed_at" in result

    def test_preprocessed_at_is_iso_string(self) -> None:
        """preprocessed_at is an ISO 8601 string."""
        result = build_lobster_meta(_msg("hello"))
        ts = result["preprocessed_at"]
        assert isinstance(ts, str)
        assert "T" in ts  # ISO format

    def test_user_message_is_user_facing(self) -> None:
        """Standard telegram user message has is_user_facing=True."""
        result = build_lobster_meta(_msg("hello", "text", "telegram", 12345))
        assert result["is_user_facing"] is True

    def test_system_message_is_not_user_facing(self) -> None:
        """System source message has is_user_facing=False."""
        result = build_lobster_meta(
            _msg("job done", "scheduled_reminder", "system", 0)
        )
        assert result["is_user_facing"] is False

    def test_system_type_intent_class(self) -> None:
        """System message type gets intent_class='system'."""
        result = build_lobster_meta(
            _msg("execute now", "wos_execute", "system", 0)
        )
        assert result["intent_class"] == "system"

    def test_urgent_user_message(self) -> None:
        """High-urgency user message is classified correctly."""
        result = build_lobster_meta(
            _msg("Production is broken, fix ASAP!", "text", "telegram", 12345)
        )
        assert result["urgency"] == "high"
        assert result["intent_class"] == "code"
        assert result["is_user_facing"] is True

    def test_uses_transcription_field_when_text_absent(self) -> None:
        """Falls back to 'transcription' field when 'text' is missing."""
        msg = {"type": "voice", "source": "telegram", "chat_id": 12345,
               "transcription": "urgent fix for production"}
        result = build_lobster_meta(msg)
        assert result["urgency"] == "high"

    def test_missing_fields_dont_crash(self) -> None:
        """build_lobster_meta does not crash on a minimal/empty message."""
        result = build_lobster_meta({})
        assert "intent_class" in result
        assert "is_user_facing" in result
        assert result["is_user_facing"] is False
