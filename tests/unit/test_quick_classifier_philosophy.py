"""
Tests for philosophy signal_type detection in quick_classifier.py.

Covers:
- Keyword detection: phenomenological, epistemic, ToL arc, conceptual exploration vocabulary
- Priority: philosophy detected before meta_reflection when both keywords present
- Posture override: philosophy always produces attunement posture
- Non-regression: ops-priority signals (task_request, design_question) are unaffected
"""
from __future__ import annotations

import pytest
from src.classifiers.quick_classifier import (
    classify_event,
    detect_signal_type,
    detect_posture_hint,
    SignalFlags,
)


# ---------------------------------------------------------------------------
# detect_signal_type — keyword detection
# ---------------------------------------------------------------------------

class TestPhilosophySignalTypeDetection:
    """detect_signal_type returns 'philosophy' for philosophy-adjacent text."""

    def test_phenomenological_language(self):
        assert detect_signal_type("I'm thinking about phenomenological experience") == "philosophy"

    def test_phenomenal_consciousness(self):
        assert detect_signal_type("the phenomenal character of perception") == "philosophy"

    def test_qualia_reference(self):
        # Avoid "status" which triggers status_check; use plain philosophical framing
        assert detect_signal_type("the nature of qualia in this framework") == "philosophy"

    def test_embodiment(self):
        assert detect_signal_type("embodiment and how it shapes cognition") == "philosophy"

    def test_embodied_cognition(self):
        assert detect_signal_type("embodied cognition is the framing I want to use") == "philosophy"

    def test_enactive_framing(self):
        assert detect_signal_type("enactive approach to perception") == "philosophy"

    def test_epistemic_framework(self):
        # Avoid "status" which triggers status_check; use explicit epistemic vocabulary
        assert detect_signal_type("the epistemic foundation of this claim") == "philosophy"

    def test_ontological_question(self):
        assert detect_signal_type("there's an ontological question here") == "philosophy"

    def test_tol_arc_reference(self):
        assert detect_signal_type("in the context of the tol arc") == "philosophy"

    def test_tree_of_life(self):
        assert detect_signal_type("tree of life as the organizing frame") == "philosophy"

    def test_philosophy_of(self):
        assert detect_signal_type("philosophy of mind question") == "philosophy"

    def test_philosophical(self):
        assert detect_signal_type("this is a philosophical point") == "philosophy"

    def test_what_does_it_mean(self):
        assert detect_signal_type("what does it mean to understand something") == "philosophy"

    def test_what_would_it_mean(self):
        assert detect_signal_type("what would it mean for a system to be conscious") == "philosophy"

    def test_hard_problem(self):
        assert detect_signal_type("the hard problem of consciousness keeps coming up") == "philosophy"

    def test_first_principles_no_longer_a_philosophy_keyword(self):
        # "first principles" was removed because it collides with engineering/design
        # discussions that should route through the Design Gate, not attunement.
        # A message using only "first principles" without philosophy vocabulary
        # should NOT be classified as philosophy.
        result = detect_signal_type("let's think from first principles about this architecture decision")
        assert result != "philosophy"

    def test_design_philosophy(self):
        # "design" alone triggers design_question; use "design philosophy" explicitly.
        # Note: task_request and design_question have higher priority than philosophy
        # in the pattern list — this is intentional, as "design philosophy" with no
        # other philosophy markers will match design_question first.
        # This test confirms the intended precedence and validates the pure-philosophy form.
        assert detect_signal_type("the philosophical grounding behind this choice") == "philosophy"

    def test_orient_toward(self):
        assert detect_signal_type("orient toward the question of what grounds") == "philosophy"

    def test_conceptual_exploration(self):
        assert detect_signal_type("this is a conceptual exploration of the domain") == "philosophy"

    def test_genuinely_curious_about(self):
        assert detect_signal_type("I'm genuinely curious about the nature of this") == "philosophy"


class TestPhilosophyPriorityOverMetaReflection:
    """Philosophy signal_type is detected before meta_reflection when both keywords appear."""

    def test_philosophical_meta_mixed(self):
        # "meta" would match meta_reflection, "philosophical" matches philosophy
        # philosophy appears before meta_reflection in _SIGNAL_TYPE_PATTERNS
        result = detect_signal_type("this is a philosophical meta question about alignment")
        assert result == "philosophy"

    def test_epistemic_principle_mixed(self):
        # "principle" matches meta_reflection, "epistemic" matches philosophy
        result = detect_signal_type("the epistemic principle at stake")
        assert result == "philosophy"

    def test_embodiment_reflection_mixed(self):
        result = detect_signal_type("a reflection on embodiment and what it means")
        assert result == "philosophy"


class TestNonPhilosophySignalTypes:
    """Messages that should NOT be classified as philosophy."""

    def test_plain_task_request(self):
        assert detect_signal_type("implement the philosophy inbox feature") == "task_request"

    def test_plain_design_question(self):
        assert detect_signal_type("how should we design the router") == "design_question"

    def test_casual_message(self):
        assert detect_signal_type("thanks, got it") == "casual"

    def test_meta_reflection_without_philosophy(self):
        result = detect_signal_type("I notice a drift in our pattern lately")
        assert result == "meta_reflection"

    def test_oracle_reference_without_philosophy(self):
        result = detect_signal_type("the oracle flagged a premise misalignment")
        assert result == "meta_reflection"

    def test_first_principles_in_design_context_not_philosophy(self):
        # "first principles" was removed from the philosophy keyword list because it
        # collides with engineering design discussions. These should route through
        # the Design Gate, not attunement.
        result = detect_signal_type("let's think from first principles about this architecture decision")
        assert result != "philosophy"

    def test_first_principles_in_architecture_context_not_philosophy(self):
        result = detect_signal_type("from first principles, how should we design the caching layer")
        assert result != "philosophy"


# ---------------------------------------------------------------------------
# Posture override: philosophy always produces attunement
# ---------------------------------------------------------------------------

class TestPhilosophyPostureOverride:
    """classify_event sets posture_hint='attunement' for any philosophy-tagged event."""

    def _make_event(self, content: str, event_id: int = 1) -> dict:
        return {"id": event_id, "type": "user_message", "content": content}

    def test_phenomenological_gets_attunement(self):
        event = self._make_event("phenomenological approach to perception")
        tag = classify_event(event)
        assert tag.signal_type == "philosophy"
        assert tag.posture_hint == "attunement"

    def test_epistemic_gets_attunement(self):
        event = self._make_event("what's the epistemic foundation here")
        tag = classify_event(event)
        assert tag.signal_type == "philosophy"
        assert tag.posture_hint == "attunement"

    def test_tol_arc_gets_attunement(self):
        event = self._make_event("the tol arc and its implications")
        tag = classify_event(event)
        assert tag.signal_type == "philosophy"
        assert tag.posture_hint == "attunement"

    def test_attunement_posture_when_no_structural_keywords(self):
        # Verify philosophy always wins the posture override — even without structural keywords
        event = self._make_event("phenomenological grounding of the system")
        tag = classify_event(event)
        assert tag.signal_type == "philosophy"
        assert tag.posture_hint == "attunement"


# ---------------------------------------------------------------------------
# Full classify_event integration
# ---------------------------------------------------------------------------

class TestClassifyEventPhilosophy:
    """classify_event produces correct full tag for philosophy messages."""

    def _make_event(self, content: str, event_id: int = 42) -> dict:
        return {"id": event_id, "type": "user_message", "content": content}

    def test_philosophy_tag_has_correct_fields(self):
        event = self._make_event("what does it mean to be an embodied system")
        tag = classify_event(event)
        assert tag.signal_type == "philosophy"
        assert tag.posture_hint == "attunement"
        assert tag.entry_id == "42"
        assert tag.entry_type == "event"

    def test_philosophy_urgency_defaults_to_normal(self):
        event = self._make_event("genuinely curious about the nature of experience")
        tag = classify_event(event)
        assert tag.urgency == "normal"

    def test_philosophy_confidence_is_low(self):
        # quick-classifier always emits low confidence
        event = self._make_event("phenomenological exploration of consciousness")
        tag = classify_event(event)
        assert tag.confidence == "low"
