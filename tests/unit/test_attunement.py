"""
Tests for the Attunement Over Assumption module (src/memory/attunement.py).

Covers:
- Causal vs. surface signal detection
- Layer inference
- Depth classification (surface_only, causal, both, indeterminate)
- Annotation generation for surface-only results
- Why-requested detection from original request
"""

import sys
from pathlib import Path

import pytest

# Ensure src/ is on the path for direct imports
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from memory.attunement import (
    DEPTH_VALUES,
    LAYERS,
    AttunementResult,
    _count_matches,
    _infer_cause_layer,
    _infer_layer,
    _CAUSAL_PHRASES,
    _SURFACE_PHRASES,
    evaluate_attunement,
)


# ============================================================================
# Signal detection tests
# ============================================================================


class TestSignalDetection:
    def test_causal_root_cause(self):
        text = "The root cause is a missing null check in the parser."
        assert _count_matches(text, _CAUSAL_PHRASES) >= 1

    def test_causal_because_the(self):
        text = "This happens because the connection pool is exhausted."
        assert _count_matches(text, _CAUSAL_PHRASES) >= 1

    def test_causal_underlying(self):
        text = "The underlying issue is in the schema migration."
        assert _count_matches(text, _CAUSAL_PHRASES) >= 1

    def test_causal_the_reason(self):
        text = "The reason this fails is that the timeout is too short."
        assert _count_matches(text, _CAUSAL_PHRASES) >= 1

    def test_surface_workaround(self):
        text = "Applied a workaround by retrying the request."
        assert _count_matches(text, _SURFACE_PHRASES) >= 1

    def test_surface_quick_fix(self):
        text = "Quick fix: set the timeout to 30 seconds."
        assert _count_matches(text, _SURFACE_PHRASES) >= 1

    def test_surface_for_now(self):
        text = "For now I just changed the config value."
        assert _count_matches(text, _SURFACE_PHRASES) >= 1

    def test_surface_just_change(self):
        text = "Just update the import path to fix the error."
        assert _count_matches(text, _SURFACE_PHRASES) >= 1

    def test_no_signals(self):
        text = "Updated the README with the new API documentation."
        assert _count_matches(text, _CAUSAL_PHRASES) == 0
        assert _count_matches(text, _SURFACE_PHRASES) == 0


# ============================================================================
# Layer inference tests
# ============================================================================


class TestLayerInference:
    def test_presentation_layer(self):
        text = "Fixed the message template and display format for the UI output."
        assert _infer_layer(text) == "presentation"

    def test_behavior_layer(self):
        text = "The function returns an error when the parameter is null."
        assert _infer_layer(text) == "behavior"

    def test_integration_layer(self):
        text = "The API endpoint handler dispatches the request to the wrong route."
        assert _infer_layer(text) == "integration"

    def test_architecture_layer(self):
        text = "The module boundary creates a dependency coupling between components."
        assert _infer_layer(text) == "architecture"

    def test_intent_layer(self):
        text = "The purpose and goal of this requirement is unclear for the use case."
        assert _infer_layer(text) == "intent"

    def test_default_layer(self):
        text = "xyz abc 123"
        assert _infer_layer(text) == "behavior"


class TestCauseLayerInference:
    def test_with_causal_signals_stays_same(self):
        assert _infer_cause_layer("behavior", has_causal_signals=True) == "behavior"

    def test_without_causal_goes_deeper(self):
        assert _infer_cause_layer("behavior", has_causal_signals=False) == "integration"

    def test_presentation_without_causal(self):
        assert _infer_cause_layer("presentation", has_causal_signals=False) == "behavior"

    def test_intent_stays_at_intent(self):
        # Can't go deeper than intent
        assert _infer_cause_layer("intent", has_causal_signals=False) == "intent"


# ============================================================================
# evaluate_attunement tests
# ============================================================================


class TestEvaluateAttunement:
    def test_empty_text(self):
        result = evaluate_attunement("")
        assert result.depth == "indeterminate"
        assert not result.needs_annotation()

    def test_whitespace_only(self):
        result = evaluate_attunement("   \n  ")
        assert result.depth == "indeterminate"

    def test_surface_only_result(self):
        text = (
            "Applied a quick fix: just changed the config value to 30 seconds. "
            "This is a temporary patch for now."
        )
        result = evaluate_attunement(text)
        assert result.depth == "surface_only"
        assert result.needs_annotation()
        assert "Surface addressed" in result.annotation
        assert result.symptom_layer in LAYERS
        assert result.likely_cause_layer in LAYERS

    def test_causal_result(self):
        text = (
            "The root cause is that the connection pool has a hard limit of 10 "
            "connections. This happens because the database driver defaults to a "
            "conservative pool size. The architectural decision to share a single "
            "pool across all request handlers means the system saturates under load."
        )
        result = evaluate_attunement(text)
        assert result.depth == "causal"
        assert not result.needs_annotation()
        assert result.annotation == ""

    def test_both_layers(self):
        text = (
            "Applied a temporary workaround by increasing the timeout, but the "
            "root cause is the upstream service's retry logic creating cascading "
            "failures. The architectural issue is that there's no circuit breaker."
        )
        result = evaluate_attunement(text)
        assert result.depth == "both"
        assert not result.needs_annotation()

    def test_indeterminate_result(self):
        text = "Updated the README with new installation instructions."
        result = evaluate_attunement(text)
        assert result.depth == "indeterminate"
        assert not result.needs_annotation()

    def test_why_requested_but_not_answered(self):
        text = "Updated the config file and restarted the service."
        result = evaluate_attunement(text, original_request="Why is the service crashing?")
        assert result.depth == "surface_only"
        assert result.needs_annotation()
        assert "why" in result.reason.lower()

    def test_why_requested_and_answered(self):
        text = (
            "The service crashes because the memory limit is set too low. "
            "The root cause is that the container spec was copied from a "
            "smaller service template."
        )
        result = evaluate_attunement(text, original_request="Why is the service crashing?")
        assert result.depth == "causal"
        assert not result.needs_annotation()

    def test_annotation_format(self):
        text = "Quick fix: just set the environment variable."
        result = evaluate_attunement(text)
        assert result.depth == "surface_only"
        assert result.annotation.startswith("[Surface addressed.")
        assert "symptom at" in result.annotation
        assert "cause likely at" in result.annotation
        assert result.annotation.endswith("]")

    def test_layer_attribution_counts_as_causal(self):
        text = (
            "This is a structural issue in the architecture layer. "
            "The symptom is at the behavior layer but the cause is at the "
            "integration layer."
        )
        result = evaluate_attunement(text)
        assert result.depth in ("causal", "both")
        assert not result.needs_annotation()


# ============================================================================
# Dataclass tests
# ============================================================================


class TestAttunementResult:
    def test_needs_annotation_surface_only(self):
        r = AttunementResult(
            depth="surface_only",
            symptom_layer="behavior",
            likely_cause_layer="integration",
            reason="test",
            annotation="[Surface addressed.]",
        )
        assert r.needs_annotation() is True

    def test_needs_annotation_causal(self):
        r = AttunementResult(
            depth="causal",
            symptom_layer="behavior",
            likely_cause_layer="behavior",
            reason="test",
        )
        assert r.needs_annotation() is False

    def test_needs_annotation_indeterminate(self):
        r = AttunementResult(
            depth="indeterminate",
            symptom_layer="behavior",
            likely_cause_layer="behavior",
            reason="test",
        )
        assert r.needs_annotation() is False
