"""
Unit tests for src/routing/observation_proposals.py.

Behavior coverage:
- _is_pattern_observation: detects tag, event_type, and type fields; rejects non-matching events
- _infer_agent: keyword matching maps to correct agent; fallback returns general-purpose
- scan_pattern_observations: qualifying groups (count >= threshold, confidence >= threshold),
  below-threshold groups excluded, empty log produces synthetic fallback, synthetic fallback path
- format_proposal_message: non-empty proposals, empty proposals, synthetic proposals
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure repo root is on path so src.routing is importable
_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.routing.observation_proposals import (
    ROUTING_PREF_MIN_CONFIDENCE,
    ROUTING_PREF_MIN_OCCURRENCES,
    _infer_agent,
    _is_pattern_observation,
    format_proposal_message,
    scan_pattern_observations,
)


# ---------------------------------------------------------------------------
# _is_pattern_observation
# ---------------------------------------------------------------------------


class TestIsPatternObservation:
    def test_true_when_tag_contains_pattern_observation(self):
        event = {"tags": ["pattern_observation", "other"], "content": "x"}
        assert _is_pattern_observation(event) is True

    def test_true_when_event_type_is_pattern_observation(self):
        event = {"event_type": "pattern_observation", "content": "x"}
        assert _is_pattern_observation(event) is True

    def test_true_when_type_is_pattern_observation(self):
        event = {"type": "pattern_observation", "content": "x"}
        assert _is_pattern_observation(event) is True

    def test_false_when_no_matching_field(self):
        event = {"tags": ["general"], "type": "note", "content": "x"}
        assert _is_pattern_observation(event) is False

    def test_false_when_tags_is_none(self):
        event = {"tags": None, "type": "note"}
        assert _is_pattern_observation(event) is False

    def test_false_when_tags_is_empty(self):
        event = {"tags": [], "type": "note"}
        assert _is_pattern_observation(event) is False

    def test_false_when_event_is_empty_dict(self):
        assert _is_pattern_observation({}) is False


# ---------------------------------------------------------------------------
# _infer_agent
# ---------------------------------------------------------------------------


class TestInferAgent:
    def test_brain_dump_keyword(self):
        assert _infer_agent("brain dump messages arriving at 7am") == "brain-dumps"

    def test_voice_keyword(self):
        assert _infer_agent("voice notes from user") == "brain-dumps"

    def test_philosophy_keyword(self):
        assert _infer_agent("philosophy and poietic thought") == "lobster-meta"

    def test_morning_keyword(self):
        assert _infer_agent("morning briefing patterns") == "morning-briefing"

    def test_code_keyword(self):
        assert _infer_agent("code review requests from github") == "functional-engineer"

    def test_pr_keyword(self):
        assert _infer_agent("pr opened for feature") == "functional-engineer"

    def test_memory_keyword(self):
        assert _infer_agent("memory retrieval failures") == "lobster-generalist"

    def test_fallback_when_no_keyword_matches(self):
        assert _infer_agent("random unrelated text about cooking") == "general-purpose"

    def test_case_insensitive_matching(self):
        assert _infer_agent("BRAIN DUMP uppercase") == "brain-dumps"


# ---------------------------------------------------------------------------
# scan_pattern_observations
# ---------------------------------------------------------------------------


class TestScanPatternObservations:
    def _make_event(self, text: str, confidence: float = 0.8, ts: str = "2026-05-25T10:00:00Z"):
        return {
            "timestamp": ts,
            "content": text,
            "type": "pattern_observation",
            "tags": ["pattern_observation"],
            "confidence": confidence,
        }

    def test_qualifying_group_produces_proposal(self, tmp_path):
        """Events with count >= MIN_OCCURRENCES and avg confidence >= MIN_CONFIDENCE yield a proposal."""
        log_file = tmp_path / "events.jsonl"
        text = "brain dump messages arriving between 06:00-09:00"
        events = [self._make_event(text, confidence=0.85) for _ in range(ROUTING_PREF_MIN_OCCURRENCES)]
        log_file.write_text("\n".join(json.dumps(e) for e in events))

        proposals = scan_pattern_observations(event_log_path=log_file)

        assert len(proposals) == 1
        p = proposals[0]
        assert p["condition"] == text[:80]
        assert p["route_to"] == "brain-dumps"
        assert p["confidence"] >= ROUTING_PREF_MIN_CONFIDENCE
        assert p["count"] == ROUTING_PREF_MIN_OCCURRENCES
        assert p["synthetic"] is False

    def test_below_count_threshold_excluded(self, tmp_path):
        """Groups with fewer events than MIN_OCCURRENCES don't produce real proposals."""
        log_file = tmp_path / "events.jsonl"
        text = "some observation"
        # One fewer than the threshold
        events = [self._make_event(text) for _ in range(ROUTING_PREF_MIN_OCCURRENCES - 1)]
        log_file.write_text("\n".join(json.dumps(e) for e in events))

        proposals = scan_pattern_observations(event_log_path=log_file)

        # Should get a synthetic fallback instead
        assert all(p["synthetic"] for p in proposals)

    def test_below_confidence_threshold_excluded(self, tmp_path):
        """Groups with avg confidence below MIN_CONFIDENCE don't produce real proposals."""
        log_file = tmp_path / "events.jsonl"
        text = "low confidence pattern"
        low_conf = ROUTING_PREF_MIN_CONFIDENCE - 0.2
        events = [self._make_event(text, confidence=low_conf) for _ in range(ROUTING_PREF_MIN_OCCURRENCES)]
        log_file.write_text("\n".join(json.dumps(e) for e in events))

        proposals = scan_pattern_observations(event_log_path=log_file)

        # Should get synthetic fallback, not a real proposal
        assert all(p["synthetic"] for p in proposals)

    def test_empty_log_file_returns_empty(self, tmp_path):
        """An empty log file produces no proposals (not even synthetic — no events to derive from)."""
        log_file = tmp_path / "events.jsonl"
        log_file.write_text("")

        proposals = scan_pattern_observations(event_log_path=log_file)

        assert proposals == []

    def test_nonexistent_log_file_returns_empty(self, tmp_path):
        """A nonexistent log file produces no proposals."""
        proposals = scan_pattern_observations(event_log_path=tmp_path / "missing.jsonl")
        assert proposals == []

    def test_synthetic_fallback_from_most_recent_event(self, tmp_path):
        """When no qualifying groups exist, returns a single synthetic proposal from the last event."""
        log_file = tmp_path / "events.jsonl"
        # One event (below count threshold) triggers synthetic fallback
        event = self._make_event("voice transcription patterns", confidence=0.9)
        log_file.write_text(json.dumps(event))

        proposals = scan_pattern_observations(event_log_path=log_file)

        assert len(proposals) == 1
        p = proposals[0]
        assert p["synthetic"] is True
        assert p["confidence"] == 0.5
        assert p["count"] == 1
        assert "voice" in p["condition"]

    def test_events_outside_window_excluded(self, tmp_path):
        """Events older than the window are excluded from grouping."""
        log_file = tmp_path / "events.jsonl"
        text = "old pattern"
        # Timestamps well outside the 24h window
        old_ts = "2020-01-01T00:00:00Z"
        events = [self._make_event(text, ts=old_ts) for _ in range(ROUTING_PREF_MIN_OCCURRENCES)]
        log_file.write_text("\n".join(json.dumps(e) for e in events))

        proposals = scan_pattern_observations(event_log_path=log_file)

        # Old events are excluded → no qualifying group → synthetic fallback from last event
        assert all(p["synthetic"] for p in proposals)

    def test_malformed_json_lines_skipped(self, tmp_path):
        """Lines that are not valid JSON are silently skipped."""
        log_file = tmp_path / "events.jsonl"
        text = "valid pattern"
        valid_events = [self._make_event(text) for _ in range(ROUTING_PREF_MIN_OCCURRENCES)]
        lines = ["not valid json{{{"] + [json.dumps(e) for e in valid_events]
        log_file.write_text("\n".join(lines))

        proposals = scan_pattern_observations(event_log_path=log_file)

        assert len(proposals) == 1
        assert proposals[0]["synthetic"] is False

    def test_multiple_qualifying_groups_ranked_by_count_times_confidence(self, tmp_path):
        """Multiple qualifying groups are sorted by count * confidence descending."""
        log_file = tmp_path / "events.jsonl"
        text_a = "voice note patterns observed"
        text_b = "code review patterns observed"
        # Group A: 3 events at 0.8 → score = 3 * 0.8 = 2.4
        events_a = [self._make_event(text_a, confidence=0.8) for _ in range(ROUTING_PREF_MIN_OCCURRENCES)]
        # Group B: 5 events at 0.9 → score = 5 * 0.9 = 4.5
        events_b = [self._make_event(text_b, confidence=0.9) for _ in range(5)]
        log_file.write_text("\n".join(json.dumps(e) for e in events_a + events_b))

        proposals = scan_pattern_observations(event_log_path=log_file)

        assert len(proposals) == 2
        # Higher score first
        assert proposals[0]["condition"] == text_b[:80]
        assert proposals[1]["condition"] == text_a[:80]

    def test_events_with_unparseable_timestamps_included(self, tmp_path):
        """Events with unparseable timestamps are included (not excluded)."""
        log_file = tmp_path / "events.jsonl"
        text = "pattern with bad timestamp"
        events = [
            {
                "timestamp": "not-a-date",
                "content": text,
                "type": "pattern_observation",
                "tags": ["pattern_observation"],
                "confidence": 0.85,
            }
            for _ in range(ROUTING_PREF_MIN_OCCURRENCES)
        ]
        log_file.write_text("\n".join(json.dumps(e) for e in events))

        proposals = scan_pattern_observations(event_log_path=log_file)

        assert len(proposals) == 1
        assert proposals[0]["synthetic"] is False
        assert proposals[0]["count"] == ROUTING_PREF_MIN_OCCURRENCES


# ---------------------------------------------------------------------------
# format_proposal_message
# ---------------------------------------------------------------------------


class TestFormatProposalMessage:
    def test_empty_proposals_returns_empty_string(self):
        assert format_proposal_message([]) == ""

    def test_non_empty_proposal_formatted(self):
        proposals = [
            {
                "condition": "brain dump messages",
                "route_to": "brain-dumps",
                "confidence": 0.85,
                "count": 4,
                "synthetic": False,
            }
        ]
        result = format_proposal_message(proposals)

        assert "brain dump messages" in result
        assert "brain-dumps" in result
        assert "85%" in result
        assert "4x" in result
        assert "synthetic" not in result.lower() or "synthetic" not in result

    def test_synthetic_proposal_includes_synthetic_note(self):
        proposals = [
            {
                "condition": "voice note patterns",
                "route_to": "brain-dumps",
                "confidence": 0.5,
                "count": 1,
                "synthetic": True,
            }
        ]
        result = format_proposal_message(proposals)

        assert "synthetic" in result.lower()
        assert "voice note patterns" in result
        assert "50%" in result

    def test_multiple_proposals_separated_by_double_newline(self):
        proposals = [
            {
                "condition": "pattern A",
                "route_to": "agent-a",
                "confidence": 0.9,
                "count": 5,
                "synthetic": False,
            },
            {
                "condition": "pattern B",
                "route_to": "agent-b",
                "confidence": 0.8,
                "count": 3,
                "synthetic": False,
            },
        ]
        result = format_proposal_message(proposals)

        assert "\n\n" in result
        assert "pattern A" in result
        assert "pattern B" in result

    def test_non_synthetic_proposal_has_no_synthetic_marker(self):
        proposals = [
            {
                "condition": "test condition",
                "route_to": "test-agent",
                "confidence": 0.75,
                "count": 3,
                "synthetic": False,
            }
        ]
        result = format_proposal_message(proposals)

        # The synthetic note marker should NOT be present
        assert "no qualifying patterns" not in result
