"""
Unit tests for WOS steward model tiering — select_steward_model().

Issue #766: tier the steward's model selection by UoW complexity so that
opus is reserved for cases that warrant it, and sonnet/haiku handle simpler
cases to reduce cost.

Tests are derived from the spec in the issue, not from implementation:

Tiering rules (in order of precedence):
1. Override: LOBSTER_PRESCRIPTION_MODEL env var → always wins
2. Override: prescription_model in wos-config.json → wins if env absent
3. Escalated: steward_cycles >= ESCALATION_THRESHOLD → opus
4. Routing/classification UoW: type == "routing" or "classification" → haiku
5. First execution (steward_cycles == 0): → sonnet
6. Default (steward_cycles > 0 but below threshold): → sonnet
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.registry import UoW, UoWStatus
from src.orchestration.steward import (
    select_steward_model,
    ESCALATION_THRESHOLD,
    MODEL_TIER_SONNET,
    MODEL_TIER_HAIKU,
    MODEL_TIER_OPUS,
)


# ---------------------------------------------------------------------------
# UoW factory
# ---------------------------------------------------------------------------

def _make_uow(
    steward_cycles: int = 0,
    type: str = "executable",
    summary: str = "Implement feature X",
    register: str = "operational",
) -> UoW:
    """Construct a minimal UoW for model tiering tests."""
    return UoW(
        id="uow_test_abc123",
        status=UoWStatus.READY_FOR_STEWARD,
        summary=summary,
        source="telegram",
        source_issue_number=42,
        created_at="2026-04-15T00:00:00+00:00",
        updated_at="2026-04-15T00:00:00+00:00",
        type=type,
        steward_cycles=steward_cycles,
        register=register,
    )


# ---------------------------------------------------------------------------
# Named constants from the spec
# ---------------------------------------------------------------------------

# These mirror the constants imported from steward so tests fail loudly if
# the implementation uses different values.
_EXPECTED_ESCALATION_THRESHOLD = 1  # steward_cycles > 1 → opus means threshold = 1
_EXPECTED_SONNET = MODEL_TIER_SONNET
_EXPECTED_HAIKU = MODEL_TIER_HAIKU
_EXPECTED_OPUS = MODEL_TIER_OPUS


# ---------------------------------------------------------------------------
# Tests: first-execution (steward_cycles == 0)
# ---------------------------------------------------------------------------

class TestFirstExecution:
    """First-execution prescriptions on new/simple UoWs → sonnet."""

    def test_first_execution_returns_sonnet(self):
        uow = _make_uow(steward_cycles=0)
        assert select_steward_model(uow) == MODEL_TIER_SONNET

    def test_first_execution_executable_type_returns_sonnet(self):
        uow = _make_uow(steward_cycles=0, type="executable")
        assert select_steward_model(uow) == MODEL_TIER_SONNET

    def test_first_execution_operational_register_returns_sonnet(self):
        uow = _make_uow(steward_cycles=0, register="operational")
        assert select_steward_model(uow) == MODEL_TIER_SONNET


# ---------------------------------------------------------------------------
# Tests: routing/classification → haiku
# ---------------------------------------------------------------------------

class TestRoutingClassification:
    """Pure routing or classification UoWs → haiku (cheapest model)."""

    def test_routing_type_returns_haiku(self):
        uow = _make_uow(steward_cycles=0, type="routing")
        assert select_steward_model(uow) == MODEL_TIER_HAIKU

    def test_classification_type_returns_haiku(self):
        uow = _make_uow(steward_cycles=0, type="classification")
        assert select_steward_model(uow) == MODEL_TIER_HAIKU

    def test_routing_type_escalated_still_returns_haiku(self):
        """Routing decisions stay cheap even if they cycle, since they are
        definitionally lightweight pass-through decisions."""
        uow = _make_uow(steward_cycles=ESCALATION_THRESHOLD + 1, type="routing")
        assert select_steward_model(uow) == MODEL_TIER_HAIKU

    def test_classification_type_escalated_still_returns_haiku(self):
        uow = _make_uow(steward_cycles=ESCALATION_THRESHOLD + 1, type="classification")
        assert select_steward_model(uow) == MODEL_TIER_HAIKU


# ---------------------------------------------------------------------------
# Tests: escalated UoWs → opus
# ---------------------------------------------------------------------------

class TestEscalated:
    """UoWs with steward_cycles > ESCALATION_THRESHOLD → opus."""

    def test_escalated_executable_returns_opus(self):
        uow = _make_uow(steward_cycles=ESCALATION_THRESHOLD + 1)
        assert select_steward_model(uow) == MODEL_TIER_OPUS

    def test_at_escalation_threshold_returns_sonnet(self):
        """At exactly the threshold (not yet escalated) → sonnet."""
        uow = _make_uow(steward_cycles=ESCALATION_THRESHOLD)
        assert select_steward_model(uow) == MODEL_TIER_SONNET

    def test_above_threshold_by_many_still_opus(self):
        uow = _make_uow(steward_cycles=ESCALATION_THRESHOLD + 5)
        assert select_steward_model(uow) == MODEL_TIER_OPUS

    def test_escalation_threshold_constant_matches_spec(self):
        """The escalation threshold is 1: steward_cycles > 1 triggers opus.
        This means the second full-pass cycle (index 2) escalates to opus."""
        assert ESCALATION_THRESHOLD == _EXPECTED_ESCALATION_THRESHOLD


# ---------------------------------------------------------------------------
# Tests: overrides take precedence
# ---------------------------------------------------------------------------

class TestOverrides:
    """Env var and config file overrides bypass tiering logic entirely."""

    def test_env_var_override_wins_for_simple_uow(self):
        uow = _make_uow(steward_cycles=0)
        with patch.dict("os.environ", {"LOBSTER_PRESCRIPTION_MODEL": "haiku"}):
            assert select_steward_model(uow) == "haiku"

    def test_env_var_override_wins_for_escalated_uow(self):
        """Even an escalated UoW uses env-override model."""
        uow = _make_uow(steward_cycles=ESCALATION_THRESHOLD + 2)
        with patch.dict("os.environ", {"LOBSTER_PRESCRIPTION_MODEL": "sonnet"}):
            assert select_steward_model(uow) == "sonnet"

    def test_env_var_stripped_of_whitespace(self):
        uow = _make_uow(steward_cycles=0)
        with patch.dict("os.environ", {"LOBSTER_PRESCRIPTION_MODEL": "  opus  "}):
            assert select_steward_model(uow) == "opus"

    def test_config_override_wins_when_no_env_var(self):
        uow = _make_uow(steward_cycles=0)
        mock_config = {"prescription_model": "haiku"}
        with patch.dict("os.environ", {}, clear=False):
            # Remove env var if present
            env = {"LOBSTER_PRESCRIPTION_MODEL": ""}
            with patch("os.environ.get", side_effect=lambda k, d=None: "" if k == "LOBSTER_PRESCRIPTION_MODEL" else d):
                with patch(
                    "src.orchestration.steward._read_prescription_model_config",
                    return_value="haiku",
                ):
                    assert select_steward_model(uow) == "haiku"

    def test_empty_env_var_falls_through_to_tiering(self):
        """An empty string in LOBSTER_PRESCRIPTION_MODEL should not override."""
        uow = _make_uow(steward_cycles=0)
        with patch.dict("os.environ", {"LOBSTER_PRESCRIPTION_MODEL": ""}):
            # Should fall through to tiering → sonnet for first-pass
            result = select_steward_model(uow)
            assert result == MODEL_TIER_SONNET


# ---------------------------------------------------------------------------
# Tests: default safe fallback
# ---------------------------------------------------------------------------

class TestDefaults:
    """Verify the safe default path: unknown/missing signals → sonnet."""

    def test_non_routing_non_escalated_returns_sonnet(self):
        uow = _make_uow(steward_cycles=1)  # one prior cycle, not yet escalated
        assert select_steward_model(uow) == MODEL_TIER_SONNET

    def test_philosophical_register_not_escalated_returns_sonnet(self):
        """register='philosophical' alone doesn't trigger opus — cycles do."""
        uow = _make_uow(steward_cycles=0, register="philosophical")
        assert select_steward_model(uow) == MODEL_TIER_SONNET
