"""
Unit tests for routing_classifier.py — WOS posture assignment via first-match-wins rules.

Tests verify behavior, not implementation detail:
- type=seed maps to sequential posture (design-first rule)
- risk=high maps to review-loop posture (high-risk-review rule)
- files_touched>5 AND type=executable maps to fan-out posture
- catch-all maps to solo posture
- Missing YAML falls back gracefully (never raises)
- Malformed YAML falls back gracefully
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_classifier_yaml(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


CANONICAL_CLASSIFIER_YAML = """\
    # WOS Routing Classifier — first-match-wins
    rules:
      - name: design-first
        priority: 10
        conditions:
          - field: type
            op: eq
            value: seed
        posture: sequential
        route_reason_template: "Rule 'design-first' matched: type=seed"

      - name: high-risk-review
        priority: 9
        conditions:
          - field: risk
            op: eq
            value: high
        posture: review-loop
        route_reason_template: "Rule 'high-risk-review' matched: risk=high"

      - name: parallelizable-multifile
        priority: 8
        conditions:
          - field: files_touched
            op: gt
            value: 5
          - field: type
            op: eq
            value: executable
        posture: fan-out
        route_reason_template: "Rule 'parallelizable-multifile' matched: files_touched>5 AND type=executable"

      - name: default
        priority: 0
        conditions: []
        posture: solo
        route_reason_template: "Rule 'default' (catch-all) matched"
"""


@pytest.fixture
def classifier_yaml_path(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "classifier.yaml"
    _write_classifier_yaml(yaml_path, CANONICAL_CLASSIFIER_YAML)
    return yaml_path


# ---------------------------------------------------------------------------
# Posture assignment tests — behavior named after the spec requirement
# ---------------------------------------------------------------------------

class TestPostureAssignment:
    def test_seed_type_assigns_sequential_posture(self, classifier_yaml_path):
        from src.orchestration.routing_classifier import classify_posture
        result = classify_posture({"type": "seed"}, classifier_path=classifier_yaml_path)
        assert result.posture == "sequential"
        assert result.rule_name == "design-first"

    def test_high_risk_assigns_review_loop_posture(self, classifier_yaml_path):
        from src.orchestration.routing_classifier import classify_posture
        result = classify_posture({"risk": "high"}, classifier_path=classifier_yaml_path)
        assert result.posture == "review-loop"
        assert result.rule_name == "high-risk-review"

    def test_multifile_executable_assigns_fan_out_posture(self, classifier_yaml_path):
        from src.orchestration.routing_classifier import classify_posture
        result = classify_posture(
            {"files_touched": 10, "type": "executable"},
            classifier_path=classifier_yaml_path,
        )
        assert result.posture == "fan-out"
        assert result.rule_name == "parallelizable-multifile"

    def test_default_catch_all_assigns_solo_posture(self, classifier_yaml_path):
        from src.orchestration.routing_classifier import classify_posture
        result = classify_posture({"type": "executable"}, classifier_path=classifier_yaml_path)
        assert result.posture == "solo"
        assert result.rule_name == "default"

    def test_first_match_wins_seed_takes_priority_over_high_risk(self, classifier_yaml_path):
        """When type=seed AND risk=high, design-first (priority 10) fires before high-risk-review (priority 9)."""
        from src.orchestration.routing_classifier import classify_posture
        result = classify_posture(
            {"type": "seed", "risk": "high"},
            classifier_path=classifier_yaml_path,
        )
        assert result.posture == "sequential"
        assert result.rule_name == "design-first"

    def test_multifile_condition_requires_both_fields(self, classifier_yaml_path):
        """files_touched>5 alone (without type=executable) should not trigger fan-out."""
        from src.orchestration.routing_classifier import classify_posture
        result = classify_posture(
            {"files_touched": 10},  # type not set
            classifier_path=classifier_yaml_path,
        )
        # Should fall through to catch-all
        assert result.posture == "solo"

    def test_route_reason_reflects_matched_rule(self, classifier_yaml_path):
        from src.orchestration.routing_classifier import classify_posture
        result = classify_posture({"type": "seed"}, classifier_path=classifier_yaml_path)
        assert "design-first" in result.route_reason

    def test_files_touched_boundary_exactly_5_does_not_trigger_fan_out(self, classifier_yaml_path):
        """files_touched must be strictly greater than 5 (op: gt)."""
        from src.orchestration.routing_classifier import classify_posture
        result = classify_posture(
            {"files_touched": 5, "type": "executable"},
            classifier_path=classifier_yaml_path,
        )
        assert result.posture == "solo"


# ---------------------------------------------------------------------------
# Fallback behavior tests — classifier must never block germination
# ---------------------------------------------------------------------------

class TestFallbackBehavior:
    def test_missing_yaml_returns_solo_posture(self, tmp_path):
        from src.orchestration.routing_classifier import classify_posture, FALLBACK_POSTURE
        absent_path = tmp_path / "nonexistent.yaml"
        result = classify_posture({"type": "seed"}, classifier_path=absent_path)
        assert result.posture == FALLBACK_POSTURE

    def test_missing_yaml_does_not_raise(self, tmp_path):
        from src.orchestration.routing_classifier import classify_posture
        absent_path = tmp_path / "nonexistent.yaml"
        # Must not raise — germination must never fail due to classifier absence
        result = classify_posture({}, classifier_path=absent_path)
        assert result is not None

    def test_malformed_yaml_returns_fallback(self, tmp_path):
        from src.orchestration.routing_classifier import classify_posture, FALLBACK_POSTURE
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(": this is: not: valid yaml: [[[")
        result = classify_posture({"type": "seed"}, classifier_path=bad_yaml)
        assert result.posture == FALLBACK_POSTURE

    def test_missing_field_in_metadata_fails_condition_safely(self, classifier_yaml_path):
        """A condition referencing a field not in the metadata dict should fail gracefully."""
        from src.orchestration.routing_classifier import classify_posture
        # risk field absent — high-risk-review rule should not match
        result = classify_posture({"type": "executable"}, classifier_path=classifier_yaml_path)
        assert result.posture == "solo"  # falls through to catch-all

    def test_env_var_overrides_default_classifier_path(self, tmp_path, monkeypatch):
        """WOS_CLASSIFIER_YAML env var should redirect classifier loading."""
        from src.orchestration.routing_classifier import classify_posture
        override_yaml = tmp_path / "custom_classifier.yaml"
        _write_classifier_yaml(override_yaml, CANONICAL_CLASSIFIER_YAML)
        monkeypatch.setenv("WOS_CLASSIFIER_YAML", str(override_yaml))
        # Re-import to pick up env var; pass classifier_path=None to use env default
        result = classify_posture({"type": "seed"})
        assert result.posture == "sequential"
