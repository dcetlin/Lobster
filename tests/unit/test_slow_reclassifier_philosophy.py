"""
Tests for philosophy_thread detection in slow_reclassifier.py.

Covers:
- detect_philosophy_thread requires 2+ philosophy events within 4h
- Produces philosophy_thread pattern with attunement posture
- Does NOT fire on meta_reflection events (different signal_type)
- detect_all_patterns includes philosophy_thread results
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from src.classifiers.slow_reclassifier import (
    EventRow,
    PatternObservation,
    detect_philosophy_thread,
    detect_all_patterns,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(
    event_id: int,
    *,
    hours_offset: float = 0.0,
    event_type: str = "user_message",
    source: str = "telegram",
    content: str = "philosophy content",
) -> EventRow:
    base_time = datetime(2026, 3, 27, 12, 0, 0, tzinfo=timezone.utc)
    return EventRow(
        id=event_id,
        timestamp=base_time + timedelta(hours=hours_offset),
        event_type=event_type,
        source=source,
        content=content,
        metadata={},
    )


def philosophy_quick_tags(event_ids: list[int]) -> dict[int, dict]:
    return {eid: {"signal_type": "philosophy"} for eid in event_ids}


# ---------------------------------------------------------------------------
# detect_philosophy_thread
# ---------------------------------------------------------------------------

class TestDetectPhilosophyThread:
    def test_two_philosophy_events_within_window_produces_observation(self):
        events = [
            make_event(1, hours_offset=0.0),
            make_event(2, hours_offset=1.0),
        ]
        quick_tags = philosophy_quick_tags([1, 2])
        observations = detect_philosophy_thread(events, quick_tags)
        assert len(observations) == 1

    def test_observation_has_correct_pattern_type(self):
        events = [make_event(1), make_event(2, hours_offset=0.5)]
        quick_tags = philosophy_quick_tags([1, 2])
        obs = detect_philosophy_thread(events, quick_tags)[0]
        assert obs.pattern_type == "philosophy_thread"

    def test_observation_has_correct_signal_type(self):
        events = [make_event(1), make_event(2, hours_offset=0.5)]
        quick_tags = philosophy_quick_tags([1, 2])
        obs = detect_philosophy_thread(events, quick_tags)[0]
        assert obs.signal_type == "philosophy_thread"

    def test_observation_has_attunement_posture(self):
        events = [make_event(1), make_event(2, hours_offset=0.5)]
        quick_tags = philosophy_quick_tags([1, 2])
        obs = detect_philosophy_thread(events, quick_tags)[0]
        assert obs.posture_hint == "attunement"

    def test_observation_has_normal_urgency(self):
        events = [make_event(1), make_event(2, hours_offset=1.0)]
        quick_tags = philosophy_quick_tags([1, 2])
        obs = detect_philosophy_thread(events, quick_tags)[0]
        assert obs.urgency == "normal"

    def test_observation_event_ids_included(self):
        events = [make_event(10), make_event(11, hours_offset=1.0)]
        quick_tags = philosophy_quick_tags([10, 11])
        obs = detect_philosophy_thread(events, quick_tags)[0]
        assert 10 in obs.event_ids
        assert 11 in obs.event_ids

    def test_single_philosophy_event_does_not_trigger(self):
        events = [make_event(1)]
        quick_tags = philosophy_quick_tags([1])
        observations = detect_philosophy_thread(events, quick_tags)
        assert observations == []

    def test_two_events_outside_window_do_not_trigger(self):
        # 4h window = 240 minutes; events 5 hours apart should not group
        events = [
            make_event(1, hours_offset=0.0),
            make_event(2, hours_offset=5.0),
        ]
        quick_tags = philosophy_quick_tags([1, 2])
        observations = detect_philosophy_thread(events, quick_tags)
        assert observations == []

    def test_meta_reflection_events_do_not_trigger_philosophy_thread(self):
        events = [make_event(1), make_event(2, hours_offset=1.0)]
        # Tagged meta_reflection, not philosophy
        quick_tags = {e.id: {"signal_type": "meta_reflection"} for e in events}
        observations = detect_philosophy_thread(events, quick_tags)
        assert observations == []

    def test_mixed_signals_only_philosophy_events_count(self):
        events = [
            make_event(1, hours_offset=0.0),
            make_event(2, hours_offset=0.5),
            make_event(3, hours_offset=1.0),
        ]
        # Only events 1 and 3 are philosophy; event 2 is meta_reflection
        quick_tags = {
            1: {"signal_type": "philosophy"},
            2: {"signal_type": "meta_reflection"},
            3: {"signal_type": "philosophy"},
        }
        observations = detect_philosophy_thread(events, quick_tags)
        # Events 1 and 3 are within 4h window — threshold met
        assert len(observations) == 1
        obs = observations[0]
        assert 1 in obs.event_ids
        assert 3 in obs.event_ids

    def test_no_events_returns_empty(self):
        observations = detect_philosophy_thread([], {})
        assert observations == []

    def test_events_without_quick_tags_do_not_trigger(self):
        events = [make_event(1), make_event(2, hours_offset=1.0)]
        # No quick_tags provided — events should not be counted as philosophy
        observations = detect_philosophy_thread(events, {})
        assert observations == []


# ---------------------------------------------------------------------------
# detect_all_patterns integration
# ---------------------------------------------------------------------------

class TestDetectAllPatternsIncludesPhilosophy:
    """detect_all_patterns delegates to detect_philosophy_thread."""

    def test_philosophy_thread_included_in_all_patterns(self):
        events = [
            make_event(1, hours_offset=0.0),
            make_event(2, hours_offset=1.0),
        ]
        quick_tags = philosophy_quick_tags([1, 2])
        all_observations = detect_all_patterns(events, quick_tags)
        philosophy_obs = [o for o in all_observations if o.pattern_type == "philosophy_thread"]
        assert len(philosophy_obs) == 1

    def test_non_philosophy_patterns_unaffected(self):
        """Philosophy detection does not suppress other pattern types."""
        from src.classifiers.slow_reclassifier import detect_design_session
        events = [
            make_event(10, hours_offset=0.0),
            make_event(11, hours_offset=0.5),
            make_event(12, hours_offset=1.0),
        ]
        # Tag all as design_question — should produce design_session, not philosophy_thread
        quick_tags = {e.id: {"signal_type": "design_question"} for e in events}
        design_obs = detect_design_session(events, quick_tags)
        philosophy_obs = detect_philosophy_thread(events, quick_tags)
        assert len(design_obs) >= 1
        assert len(philosophy_obs) == 0


# ---------------------------------------------------------------------------
# Valence
# ---------------------------------------------------------------------------

class TestPhilosophyThreadValence:
    """Philosophy thread observations default to neutral valence."""

    def test_philosophy_thread_valence_is_neutral(self):
        events = [make_event(1), make_event(2, hours_offset=1.0)]
        quick_tags = philosophy_quick_tags([1, 2])
        obs = detect_philosophy_thread(events, quick_tags)[0]
        assert obs.valence == "neutral"
