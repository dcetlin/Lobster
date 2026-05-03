"""Tests for src.orchestration.principle_weights — epistemic principle weighting."""

from __future__ import annotations

import pytest

from src.orchestration.principle_weights import (
    PRINCIPLE_IMPLICATIONS,
    PRINCIPLE_NAMES,
    PRINCIPLE_WEIGHT_PROFILES,
    VISIBILITY_THRESHOLD,
    assign_principle_weights,
    format_principle_weights_block,
)


# ---------------------------------------------------------------------------
# Constants derived from the spec — not from the implementation
# ---------------------------------------------------------------------------

EXPECTED_PRINCIPLE_COUNT = 5
EXPECTED_PROFILES = {"debugging", "mobile_response", "design_inquiry", "default"}


class TestPrincipleConstants:
    """Verify the structural invariants of the weight system."""

    def test_five_canonical_principles(self):
        assert len(PRINCIPLE_NAMES) == EXPECTED_PRINCIPLE_COUNT

    def test_every_principle_has_implication(self):
        for name in PRINCIPLE_NAMES:
            assert name in PRINCIPLE_IMPLICATIONS, f"Missing implication for {name}"
            assert len(PRINCIPLE_IMPLICATIONS[name]) > 0

    def test_expected_profiles_exist(self):
        assert set(PRINCIPLE_WEIGHT_PROFILES.keys()) == EXPECTED_PROFILES

    def test_all_profiles_cover_all_principles(self):
        for profile_name, weights in PRINCIPLE_WEIGHT_PROFILES.items():
            for principle in PRINCIPLE_NAMES:
                assert principle in weights, (
                    f"Profile {profile_name!r} missing principle {principle!r}"
                )

    def test_all_weights_in_valid_range(self):
        for profile_name, weights in PRINCIPLE_WEIGHT_PROFILES.items():
            for principle, weight in weights.items():
                assert 0.0 <= weight <= 1.0, (
                    f"Profile {profile_name!r}, principle {principle!r}: "
                    f"weight {weight} out of [0.0, 1.0]"
                )

    def test_visibility_threshold_is_0_6(self):
        """Spec says: omit principles with weight < 0.6."""
        assert VISIBILITY_THRESHOLD == 0.6


class TestAssignPrincipleWeights:
    """assign_principle_weights returns the correct profile for each task type."""

    def test_known_task_type_returns_matching_profile(self):
        weights = assign_principle_weights("debugging")
        assert weights == PRINCIPLE_WEIGHT_PROFILES["debugging"]

    def test_unknown_task_type_falls_back_to_default(self):
        weights = assign_principle_weights("never_heard_of_this")
        assert weights == PRINCIPLE_WEIGHT_PROFILES["default"]

    def test_returns_fresh_copy_not_same_object(self):
        w1 = assign_principle_weights("debugging")
        w2 = assign_principle_weights("debugging")
        assert w1 == w2
        assert w1 is not w2, "Must return a copy, not the internal dict"

    def test_mutating_result_does_not_affect_source(self):
        weights = assign_principle_weights("debugging")
        original_value = PRINCIPLE_WEIGHT_PROFILES["debugging"]["pattern_perception"]
        weights["pattern_perception"] = 999.0
        assert PRINCIPLE_WEIGHT_PROFILES["debugging"]["pattern_perception"] == original_value

    @pytest.mark.parametrize("task_type", list(EXPECTED_PROFILES - {"default"}))
    def test_each_profile_returns_five_keys(self, task_type: str):
        weights = assign_principle_weights(task_type)
        assert len(weights) == EXPECTED_PRINCIPLE_COUNT

    def test_debugging_attunement_is_highest(self):
        """Spec: debugging has attunement_over_assumption at 0.9."""
        weights = assign_principle_weights("debugging")
        assert weights["attunement_over_assumption"] == 0.9

    def test_mobile_response_minimal_cognitive_friction_is_highest(self):
        """Spec: mobile_response has minimal_cognitive_friction at 0.9."""
        weights = assign_principle_weights("mobile_response")
        assert weights["minimal_cognitive_friction"] == 0.9

    def test_design_inquiry_pattern_perception_is_highest(self):
        """Spec: design_inquiry has pattern_perception at 0.9."""
        weights = assign_principle_weights("design_inquiry")
        assert weights["pattern_perception"] == 0.9


class TestFormatPrincipleWeightsBlock:
    """format_principle_weights_block renders visible principles with implications."""

    def test_omits_principles_below_threshold(self):
        weights = {"pattern_perception": 0.5, "elegant_economy": 0.3}
        block = format_principle_weights_block(weights)
        assert block == ""

    def test_includes_principles_at_threshold(self):
        weights = {"pattern_perception": 0.6}
        block = format_principle_weights_block(weights)
        assert "pattern_perception: 0.6" in block

    def test_includes_behavioral_implication(self):
        weights = {"attunement_over_assumption": 0.9}
        block = format_principle_weights_block(weights)
        assert "name causal vs. symptom layers explicitly" in block

    def test_full_debugging_profile_output(self):
        weights = assign_principle_weights("debugging")
        block = format_principle_weights_block(weights)
        # debugging has elegant_economy at 0.5, should be omitted
        assert "elegant_economy" not in block
        # debugging has attunement_over_assumption at 0.9, should be present
        assert "attunement_over_assumption: 0.9" in block
        # debugging has pattern_perception at 0.7
        assert "pattern_perception: 0.7" in block

    def test_mobile_response_omits_low_weight_principles(self):
        weights = assign_principle_weights("mobile_response")
        block = format_principle_weights_block(weights)
        # pattern_perception: 0.5, structural_coherence: 0.5 — both below 0.6
        assert "pattern_perception" not in block
        assert "structural_coherence" not in block
        # minimal_cognitive_friction: 0.9 — present
        assert "minimal_cognitive_friction: 0.9" in block

    def test_canonical_ordering_preserved(self):
        """Output lines follow PRINCIPLE_NAMES order, not dict insertion order."""
        weights = assign_principle_weights("default")  # all 0.7
        block = format_principle_weights_block(weights)
        lines = block.strip().split("\n")
        principle_names_in_output = [
            line.split(":")[0].lstrip("- ").strip() for line in lines
        ]
        assert principle_names_in_output == list(PRINCIPLE_NAMES)

    def test_empty_dict_returns_empty_string(self):
        assert format_principle_weights_block({}) == ""

    def test_missing_principle_treated_as_zero(self):
        """Principles not in the dict default to 0.0 (below threshold)."""
        weights = {"pattern_perception": 0.8}
        block = format_principle_weights_block(weights)
        lines = block.strip().split("\n")
        assert len(lines) == 1
        assert "pattern_perception" in lines[0]
