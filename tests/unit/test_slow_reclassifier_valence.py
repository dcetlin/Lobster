"""
Tests for the valence classification added to slow_reclassifier.py.

Covers:
- classify_valence heuristic (golden / smell / neutral)
- PatternObservation carries valence
- detect_* functions produce observations with expected valence
"""
from __future__ import annotations

import pytest
from src.classifiers.slow_reclassifier import (
    PatternObservation,
    classify_valence,
    detect_brainstorm_mode,
    detect_complex_request,
    detect_design_session,
    detect_meta_thread,
)


# ---------------------------------------------------------------------------
# classify_valence — pure heuristic
# ---------------------------------------------------------------------------

class TestClassifyValence:
    def test_golden_keyword_in_pattern_type(self):
        assert classify_valence("golden_pattern", "") == "golden"

    def test_win_keyword_in_description(self):
        assert classify_valence("design_session", "This is a win for the team") == "golden"

    def test_strength_keyword_in_description(self):
        assert classify_valence("meta_thread", "Identified a core strength") == "golden"

    def test_smell_keyword_in_pattern_type(self):
        assert classify_valence("code_smell", "") == "smell"

    def test_drift_keyword_in_description(self):
        assert classify_valence("complex_request", "noticeable drift in direction") == "smell"

    def test_failure_keyword_in_description(self):
        assert classify_valence("brainstorm_mode", "repeated failure pattern") == "smell"

    def test_error_keyword_in_description(self):
        assert classify_valence("meta_thread", "error in the pipeline") == "smell"

    def test_neutral_when_no_keywords(self):
        assert classify_valence("design_session", "discussing architecture") == "neutral"

    def test_neutral_empty_inputs(self):
        assert classify_valence("", "") == "neutral"

    def test_case_insensitive(self):
        assert classify_valence("GOLDEN_PATTERN", "STRONG WIN") == "golden"
        assert classify_valence("CODE_SMELL", "DRIFT DETECTED") == "smell"

    def test_golden_takes_priority_over_smell_when_both_present(self):
        # golden keywords appear first in the combined string since pattern_type is prepended
        result = classify_valence("golden_item", "has a smell too")
        assert result == "golden"


# ---------------------------------------------------------------------------
# PatternObservation default valence
# ---------------------------------------------------------------------------

class TestPatternObservationValence:
    def _make(self, pattern_type: str = "design_session", valence: str = "neutral") -> PatternObservation:
        return PatternObservation(
            pattern_type=pattern_type,
            source="test_source",
            event_ids=[1, 2, 3],
            signal_type="design_session",
            urgency="normal",
            posture_hint="structural_coherence",
            valence=valence,
        )

    def test_default_valence_is_neutral(self):
        obs = PatternObservation(
            pattern_type="design_session",
            source="src",
            event_ids=[1],
            signal_type="design_session",
            urgency="normal",
            posture_hint="structural_coherence",
        )
        assert obs.valence == "neutral"

    def test_valence_golden(self):
        obs = self._make(valence="golden")
        assert obs.valence == "golden"

    def test_valence_smell(self):
        obs = self._make(valence="smell")
        assert obs.valence == "smell"


# ---------------------------------------------------------------------------
# Detect functions produce observations with correct valence
# ---------------------------------------------------------------------------

class TestDetectFunctionsValence:
    """
    All four pattern types map to neutral valence via the current keyword heuristic
    (none of design_session / brainstorm_mode / complex_request / meta_thread contain
    golden or smell keywords). This tests that the valence field is wired through
    and that the default is neutral.
    """

    from datetime import datetime, timezone

    def _make_event(self, event_id: int, event_type: str, signal_type: str):
        from datetime import datetime, timezone
        from src.classifiers.slow_reclassifier import EventRow
        return EventRow(
            id=event_id,
            timestamp=datetime(2026, 3, 27, 12, 0, event_id % 60, tzinfo=timezone.utc),
            event_type=event_type,
            source="test",
            content="x",
            metadata={},
        )

    def test_design_session_valence_neutral(self):
        events = [self._make_event(i, "user_message", "design_question") for i in range(3)]
        quick_tags = {e.id: {"signal_type": "design_question"} for e in events}
        observations = detect_design_session(events, quick_tags)
        assert len(observations) >= 1
        assert all(obs.valence == "neutral" for obs in observations)

    def test_brainstorm_mode_valence_neutral(self):
        events = [self._make_event(i, "voice_note", "voice_note") for i in range(3)]
        quick_tags = {e.id: {"signal_type": "voice_note"} for e in events}
        observations = detect_brainstorm_mode(events, quick_tags)
        assert len(observations) >= 1
        assert all(obs.valence == "neutral" for obs in observations)

    def test_complex_request_valence_neutral(self):
        from src.classifiers.slow_reclassifier import EventRow
        from datetime import datetime, timezone
        events = [
            EventRow(
                id=i,
                timestamp=datetime(2026, 3, 27, 12, 0, i, tzinfo=timezone.utc),
                event_type="user_message",
                source="test",
                content="short",  # len < 50
                metadata={},
            )
            for i in range(2)
        ]
        quick_tags = {e.id: {"signal_type": "task_request"} for e in events}
        observations = detect_complex_request(events, quick_tags)
        assert len(observations) >= 1
        assert all(obs.valence == "neutral" for obs in observations)

    def test_meta_thread_valence_neutral(self):
        events = [self._make_event(i, "user_message", "meta_reflection") for i in range(2)]
        quick_tags = {e.id: {"signal_type": "meta_reflection"} for e in events}
        observations = detect_meta_thread(events, quick_tags)
        assert len(observations) >= 1
        assert all(obs.valence == "neutral" for obs in observations)
