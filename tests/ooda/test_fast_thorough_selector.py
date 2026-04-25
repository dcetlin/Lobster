"""
tests/ooda/test_fast_thorough_selector.py

Unit tests for the Fast/Thorough Path meta-selector.

Coverage:
- Fast Path selected when vision anchor present and stakes low
- Fast Path selected when prior decision of same class exists and stakes low
- Thorough Path selected when no anchor and no prior
- Thorough Path selected when stakes are high even if anchor exists
- cite_basis() returns the anchor field name on Fast Path
- cite_basis() returns None on Thorough Path
"""

import pytest

from src.ooda.fast_thorough_selector import cite_basis, select_path
from src.orchestration.shard_dispatch import PathSelection


# ---------------------------------------------------------------------------
# select_path tests
# ---------------------------------------------------------------------------

class TestSelectPath:
    def test_fast_path_vision_anchor_low_stakes(self):
        """Fast Path when vision anchor present and stakes low."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [],
            "vision_anchor": "vision.core.fundamental_intent",
        }
        assert select_path(ctx) == PathSelection.FAST

    def test_fast_path_prior_decision_same_class_low_stakes(self):
        """Fast Path when prior decision of same class exists and stakes low."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [
                {"situation_class": "task_routing", "id": "pd-001"},
            ],
            "vision_anchor": None,
        }
        assert select_path(ctx) == PathSelection.FAST

    def test_thorough_path_no_anchor_no_prior(self):
        """Thorough Path when no anchor and no prior decision."""
        ctx = {
            "situation_class": "novel_situation",
            "stakes": "low",
            "prior_decisions": [],
            "vision_anchor": None,
        }
        assert select_path(ctx) == PathSelection.THOROUGH

    def test_thorough_path_high_stakes_even_with_anchor(self):
        """Thorough Path when stakes are high even if vision anchor exists."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "high",
            "prior_decisions": [],
            "vision_anchor": "vision.core.inviolable_constraints",
        }
        assert select_path(ctx) == PathSelection.THOROUGH

    def test_thorough_path_high_stakes_with_prior(self):
        """Thorough Path when stakes high even with matching prior decision."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "high",
            "prior_decisions": [
                {"situation_class": "task_routing", "id": "pd-002"},
            ],
            "vision_anchor": None,
        }
        assert select_path(ctx) == PathSelection.THOROUGH

    def test_fast_path_prior_decision_no_explicit_id(self):
        """Fast Path with prior decision that has no explicit ID uses synthetic ref."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [
                {"situation_class": "task_routing"},  # no id field
            ],
            "vision_anchor": None,
        }
        assert select_path(ctx) == PathSelection.FAST

    def test_thorough_path_prior_different_class(self):
        """Thorough Path when prior decision exists but is a different class."""
        ctx = {
            "situation_class": "novel_routing",
            "stakes": "low",
            "prior_decisions": [
                {"situation_class": "task_routing", "id": "pd-003"},
            ],
            "vision_anchor": None,
        }
        assert select_path(ctx) == PathSelection.THOROUGH

    def test_fast_path_prefers_vision_anchor_over_prior(self):
        """When both anchor and prior exist (low stakes), Fast Path is selected."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [
                {"situation_class": "task_routing", "id": "pd-004"},
            ],
            "vision_anchor": "vision.active_project.phase_intent",
        }
        assert select_path(ctx) == PathSelection.FAST

    def test_empty_string_anchor_treated_as_absent(self):
        """An empty string vision_anchor is treated as absent (Thorough Path)."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [],
            "vision_anchor": "",
        }
        assert select_path(ctx) == PathSelection.THOROUGH

    def test_whitespace_only_anchor_treated_as_absent(self):
        """A whitespace-only vision_anchor is treated as absent (Thorough Path)."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [],
            "vision_anchor": "   ",
        }
        assert select_path(ctx) == PathSelection.THOROUGH


# ---------------------------------------------------------------------------
# cite_basis tests
# ---------------------------------------------------------------------------

class TestCiteBasis:
    def test_returns_anchor_field_name_on_fast_path(self):
        """cite_basis returns vision.yaml field name when Fast Path selected."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [],
            "vision_anchor": "vision.core.fundamental_intent",
        }
        basis = cite_basis(ctx)
        assert basis == "vision.core.fundamental_intent"

    def test_returns_none_on_thorough_path_no_anchor_no_prior(self):
        """cite_basis returns None when Thorough Path applies (no anchor, no prior)."""
        ctx = {
            "situation_class": "novel_situation",
            "stakes": "low",
            "prior_decisions": [],
            "vision_anchor": None,
        }
        assert cite_basis(ctx) is None

    def test_returns_none_on_thorough_path_high_stakes(self):
        """cite_basis returns None when Thorough Path due to high stakes."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "high",
            "prior_decisions": [],
            "vision_anchor": "vision.core.fundamental_intent",
        }
        assert cite_basis(ctx) is None

    def test_returns_prior_decision_id_when_no_anchor(self):
        """cite_basis returns prior decision ID when no anchor but prior exists."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [
                {"situation_class": "task_routing", "id": "pd-001"},
            ],
            "vision_anchor": None,
        }
        basis = cite_basis(ctx)
        assert basis == "pd-001"

    def test_returns_synthetic_ref_when_no_id_in_prior(self):
        """cite_basis returns a synthetic ref when prior has no explicit id."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [
                {"situation_class": "task_routing"},
            ],
            "vision_anchor": None,
        }
        basis = cite_basis(ctx)
        assert basis == "prior_decision:task_routing"

    def test_prefers_vision_anchor_over_prior_decision(self):
        """cite_basis returns vision anchor when both anchor and prior exist."""
        ctx = {
            "situation_class": "task_routing",
            "stakes": "low",
            "prior_decisions": [
                {"situation_class": "task_routing", "id": "pd-001"},
            ],
            "vision_anchor": "vision.active_project.phase_intent",
        }
        basis = cite_basis(ctx)
        # Vision anchor should take precedence
        assert basis == "vision.active_project.phase_intent"
