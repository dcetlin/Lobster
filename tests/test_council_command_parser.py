"""
Unit tests for parse_council_command() in dispatcher_handlers.py.

Tests cover the behavior described in the spec:
- Matches "council: <topic>" (case-insensitive)
- Returns the topic string, stripped of leading/trailing whitespace
- Returns None for non-matching inputs
- Named constant for the "council: " prefix (COUNCIL_PREFIX) ensures the
  spec text is the single source of truth for what triggers deliberation.
"""

import pytest

from src.orchestration.dispatcher_handlers import parse_council_command

# Named constants mirror the spec language — any change to the trigger
# pattern should be reflected here, not scattered as magic strings.
COUNCIL_PREFIX_LOWER = "council:"
COUNCIL_PREFIX_CANONICAL = "council: "


class TestParseCouncilCommandMatches:
    """parse_council_command returns the topic string when the trigger matches."""

    def test_basic_topic(self):
        """Standard invocation: 'council: <topic>'"""
        result = parse_council_command("council: stiffness-toughness tradeoff")
        assert result == "stiffness-toughness tradeoff"

    def test_question_topic(self):
        """Topic can be a question."""
        result = parse_council_command("council: What does ergonomics say about API friction?")
        assert result == "What does ergonomics say about API friction?"

    def test_case_insensitive_prefix(self):
        """Prefix matching is case-insensitive per spec."""
        result = parse_council_command("Council: stiffness-toughness tradeoff")
        assert result == "stiffness-toughness tradeoff"

    def test_uppercase_prefix(self):
        result = parse_council_command("COUNCIL: bone composite structure")
        assert result == "bone composite structure"

    def test_leading_whitespace_stripped(self):
        """Leading whitespace before 'council:' is stripped."""
        result = parse_council_command("  council: crack propagation")
        assert result == "crack propagation"

    def test_topic_whitespace_stripped(self):
        """Topic trailing whitespace is stripped."""
        result = parse_council_command("council: tensegrity and distributed force  ")
        assert result == "tensegrity and distributed force"

    def test_multi_word_topic(self):
        result = parse_council_command("council: affordance theory and violin bow mechanics")
        assert result == "affordance theory and violin bow mechanics"

    def test_topic_with_special_chars(self):
        """Topics may contain punctuation and slashes."""
        result = parse_council_command("council: stiffness/toughness in bone vs. API design")
        assert result == "stiffness/toughness in bone vs. API design"


class TestParseCouncilCommandNoMatch:
    """parse_council_command returns None when the trigger does not match."""

    def test_wos_command_no_match(self):
        assert parse_council_command("wos status") is None

    def test_plain_text_no_match(self):
        assert parse_council_command("what is the stiffness-toughness tradeoff?") is None

    def test_council_without_colon_no_match(self):
        """'council' without ':' does not trigger deliberation."""
        assert parse_council_command("council meeting about ergonomics") is None

    def test_empty_string_no_match(self):
        assert parse_council_command("") is None

    def test_whitespace_only_no_match(self):
        assert parse_council_command("   ") is None

    def test_council_colon_no_topic(self):
        """'council:' with only whitespace after it does not match (no topic to deliberate)."""
        # The regex requires at least one non-whitespace character after 'council:'
        assert parse_council_command("council:   ") is None

    def test_todo_command_no_match(self):
        assert parse_council_command("/todo add review ergonomics notes") is None

    def test_council_in_body_no_match(self):
        """'council:' embedded mid-sentence is not a trigger."""
        assert parse_council_command("Dan said: the council: deliberation was good") is None


class TestParseCouncilCommandReturnType:
    """Return type is always str | None."""

    def test_returns_str_on_match(self):
        result = parse_council_command("council: biomechanics")
        assert isinstance(result, str)

    def test_returns_none_on_no_match(self):
        result = parse_council_command("not a council command")
        assert result is None
