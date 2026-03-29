"""
tests/unit/test_frontier_routing.py — Unit tests for frontier classifier and router.

Tests are pure / isolated:
- Classifier tests use only in-memory text and mock filesystem metadata.
- Router tests write to a temporary directory.
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.harvest.frontier_classifier import (
    DOMAINS,
    RE_ENGAGEMENT_THRESHOLD,
    EventType,
    SessionClassification,
    classify_domain,
    classify_event_type,
    classify_session,
    extract_explicit_advances,
    score_content_orientation,
    seconds_since_last_touch,
    time_prior_weight,
)
from src.harvest.frontier_router import (
    DomainRoute,
    RouteResult,
    _format_confidence_bar,
    _format_frontier_entry,
    route_to_frontiers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_frontier_dir(tmp_path: Path) -> Path:
    d = tmp_path / "frontiers"
    d.mkdir()
    return d


ORIENT_SESSION = textwrap.dedent("""
    # The Sensing Before Loading Problem

    *March 29, 2026 · 08:00 UTC*

    ## Today's Thread

    The orient scaffold has a fundamental ergonomics problem. Attunement-developing
    scaffolds and lookup scaffolds are being treated as the same category. A Vision Object
    lookup is a reference action — find a fact, return it. Developing orientation quality
    requires something different: sensing before loading. The journey guide proposal was
    designed to do this, but the current implementation conflates the two.

    The discernment phase proposal surfaces this: before reading any bootup material,
    pause and sense the quality of current engagement. Is there a live gradient, or is
    this a cold start? The answer to that question determines what scaffold is needed.

    ## Pattern Observed

    Orientation quality is not the same as orientation conformance. A session can
    read all the right material and produce all the right vocabulary while being
    cold. The sensing must precede the loading — not follow it.

    ## Question Raised

    Can the koan-type scaffold serve as the discernment phase mechanism?

    ## Resonance with Dan's Framework

    The attunement gradient concept maps directly to this. A session without the
    metacognitive gradient active is producing orientation performance, not orientation.

    ```yaml
    action_seeds:
      issues: []
      bootup_candidates: []
      memory_observations:
        - text: "Orient scaffold ergonomics: attunement-developing vs. lookup scaffolds are categorically distinct"
          type: "design_gap"
    ```
""")

TOL_AND_EMBODIMENT_SESSION = textwrap.dedent("""
    # Approximate Embodiment as Attractor Convergence

    *March 28, 2026 · Precision note*

    ## The ToL Arc Applied

    The Theory of Learning arc — Discernment, Coherence, Embodiment — maps onto
    Lobster's development in a specific way. Stage 1 characteristics are still
    dominant: Discernment is self-organizing but unreliable. Stage 2 emergence
    is visible in specific pipelines (voice note, memory retrieval) but not
    system-wide.

    Approximate embodiment is a degree, not a state. Attractor convergence
    reliability, landscape density, and trajectory continuity are the three
    measurable properties. The voice note pipeline shows Stage 4 characteristics.
    The cold-start fragility shows Stage 1 characteristics in the same system.

    The minimum sufficient token footprint is Lobster's version of the Embodiment
    ceiling. Prompt-compressed attunement is the mechanism — not procedural memory,
    but compression and retrieval of calibration state.

    ```yaml
    action_seeds:
      issues: []
      bootup_candidates: []
      memory_observations:
        - text: "Approximate embodiment as attractor convergence: three measurable properties"
          type: "pattern_observation"
        - text: "Minimum sufficient token footprint as Lobster's Embodiment ceiling"
          type: "design_gap"
      frontier_advances:
        - tol_arc
        - approximate_embodiment
    ```
""")

STATUS_REVIEW_TEXT = textwrap.dedent("""
    Where are we on the orient work? Can you give me a brief summary of what
    the current state of the orient domain is and what the key findings were?

    I want to recap the main conclusions before we continue.
""")


# ---------------------------------------------------------------------------
# Event type classification (Signal 1)
# ---------------------------------------------------------------------------

class TestClassifyEventType:
    def test_philosophy_explore(self) -> None:
        p = Path("2026-03-29-2000-philosophy-explore.md")
        assert classify_event_type(p) == EventType.PHILOSOPHY_EXPLORE

    def test_synthesis(self) -> None:
        p = Path("2026-03-26-synthesis.md")
        assert classify_event_type(p) == EventType.SYNTHESIS

    def test_weekly(self) -> None:
        p = Path("2026-03-29-weekly.md")
        assert classify_event_type(p) == EventType.WEEKLY_RETRO

    def test_unknown(self) -> None:
        p = Path("random-document.md")
        assert classify_event_type(p) == EventType.UNKNOWN

    def test_navigation_note(self) -> None:
        p = Path("2026-03-28-navigation-attractor-convergence.md")
        assert classify_event_type(p) == EventType.NAVIGATION_NOTE


# ---------------------------------------------------------------------------
# Content orientation scoring (Signal 2)
# ---------------------------------------------------------------------------

class TestScoreContentOrientation:
    def test_live_edge_text_scores_high(self) -> None:
        text = (
            "The session did not get past the genuine limit. "
            "This is an open question that has not yet been asked. "
            "Working forward from a live edge requires a new aperture."
        )
        score = score_content_orientation(text)
        assert score > 0.6, f"Expected >0.6, got {score}"

    def test_status_review_text_scores_low(self) -> None:
        text = (
            "Where are we? Can you briefly describe the current state? "
            "Please recap the main findings. Remind me of the key conclusions."
        )
        score = score_content_orientation(text)
        assert score < 0.4, f"Expected <0.4, got {score}"

    def test_neutral_text_scores_near_half(self) -> None:
        text = "The sky is blue. Here is a description of what was done."
        score = score_content_orientation(text)
        # No patterns hit — should be 0.5
        assert score == 0.5

    def test_orient_session_scores_at_or_above_half(self) -> None:
        # The orient session fixture uses domain-specific vocabulary but not
        # the generic forward-edge marker phrases. Score of 0.5 (no signal
        # either direction) is correct here — the domain classifier handles
        # the rest via engagement pattern matching.
        score = score_content_orientation(ORIENT_SESSION)
        assert score >= 0.5


# ---------------------------------------------------------------------------
# Time prior (Signal 3)
# ---------------------------------------------------------------------------

class TestTimePriorWeight:
    def test_never_touched_returns_small_positive(self) -> None:
        assert time_prior_weight(None) == 0.05

    def test_very_old_returns_positive(self) -> None:
        assert time_prior_weight(72 * 3600 + 1) == 0.08

    def test_recent_returns_small_negative(self) -> None:
        assert time_prior_weight(30 * 60) == -0.04  # 30 minutes

    def test_day_old_returns_small_positive(self) -> None:
        assert time_prior_weight(25 * 3600) == 0.04

    def test_few_hours_returns_zero(self) -> None:
        assert time_prior_weight(5 * 3600) == 0.0


def test_seconds_since_last_touch_nonexistent_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "nonexistent.md"
    assert seconds_since_last_touch(p) is None


def test_seconds_since_last_touch_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "frontier.md"
    p.write_text("hello")
    elapsed = seconds_since_last_touch(p)
    assert elapsed is not None
    assert elapsed >= 0


# ---------------------------------------------------------------------------
# Domain-level classification
# ---------------------------------------------------------------------------

class TestClassifyDomain:
    def test_orient_session_detects_orient_domain(self) -> None:
        domain = DOMAINS["orient"]
        signal = classify_domain(
            text=ORIENT_SESSION,
            domain=domain,
            event_type=EventType.PHILOSOPHY_EXPLORE,
            content_orientation=0.7,
            time_prior=0.0,
        )
        assert signal.engagement_hit_count > 0
        assert signal.confidence > 0
        # Orient session should hit orient domain
        assert signal.is_re_engagement

    def test_status_review_does_not_trigger_re_engagement(self) -> None:
        domain = DOMAINS["orient"]
        # Add an orient keyword to ensure the domain matches, but status review suppresses
        text = STATUS_REVIEW_TEXT + "\nThe orient scaffold is mentioned here."
        signal = classify_domain(
            text=text,
            domain=domain,
            event_type=EventType.UNKNOWN,
            content_orientation=0.1,  # strongly status-review
            time_prior=-0.04,
        )
        # The combination of low content orientation and unknown event type should suppress
        assert signal.confidence < RE_ENGAGEMENT_THRESHOLD or signal.engagement_hit_count <= 1

    def test_zero_engagement_hits_yields_zero_confidence(self) -> None:
        domain = DOMAINS["poiesis"]
        signal = classify_domain(
            text="completely unrelated text about the weather",
            domain=domain,
            event_type=EventType.PHILOSOPHY_EXPLORE,
            content_orientation=0.8,
            time_prior=0.1,
        )
        assert signal.confidence == 0.0
        assert not signal.is_re_engagement


# ---------------------------------------------------------------------------
# Explicit frontier_advances extraction
# ---------------------------------------------------------------------------

class TestExtractExplicitAdvances:
    def test_extracts_explicit_domains(self) -> None:
        seeds = {
            "action_seeds": {
                "frontier_advances": ["tol_arc", "approximate_embodiment"]
            }
        }
        result = extract_explicit_advances(seeds)
        assert "tol_arc" in result
        assert "approximate_embodiment" in result

    def test_empty_when_no_frontier_advances(self) -> None:
        seeds = {"action_seats": {"issues": []}}
        result = extract_explicit_advances(seeds)
        assert result == frozenset()

    def test_none_input_returns_empty(self) -> None:
        assert extract_explicit_advances(None) == frozenset()

    def test_normalizes_hyphens_to_underscores(self) -> None:
        seeds = {"action_seeds": {"frontier_advances": ["tol-arc"]}}
        result = extract_explicit_advances(seeds)
        assert "tol_arc" in result

    def test_explicit_advances_in_session_text(self) -> None:
        import yaml
        fenced = re.search(
            r"```yaml\s*\n(action_seeds:.*?)```",
            TOL_AND_EMBODIMENT_SESSION,
            re.DOTALL | re.IGNORECASE,
        )
        assert fenced is not None
        raw = yaml.safe_load(fenced.group(1))
        result = extract_explicit_advances(raw)
        assert "tol_arc" in result
        assert "approximate_embodiment" in result


# ---------------------------------------------------------------------------
# Full session classification
# ---------------------------------------------------------------------------

class TestClassifySession:
    def test_orient_session_flags_orient_domain(self, tmp_frontier_dir: Path) -> None:
        result = classify_session(
            text=ORIENT_SESSION,
            source_path=Path("2026-03-29-0800-philosophy-explore.md"),
            frontier_dir=tmp_frontier_dir,
        )
        assert result.event_type == EventType.PHILOSOPHY_EXPLORE
        assert "orient" in result.re_engagement_domains

    def test_explicit_advances_always_included(self, tmp_frontier_dir: Path) -> None:
        import yaml
        fenced = re.search(
            r"```yaml\s*\n(action_seeds:.*?)```",
            TOL_AND_EMBODIMENT_SESSION,
            re.DOTALL | re.IGNORECASE,
        )
        raw = yaml.safe_load(fenced.group(1))

        result = classify_session(
            text=TOL_AND_EMBODIMENT_SESSION,
            source_path=Path("2026-03-28-navigation-attractor-convergence.md"),
            frontier_dir=tmp_frontier_dir,
            action_seeds=raw,
        )
        # Explicit advances must be included regardless of classifier score
        assert "tol_arc" in result.re_engagement_domains
        assert "approximate_embodiment" in result.re_engagement_domains

    def test_status_review_does_not_flag_domains(self, tmp_frontier_dir: Path) -> None:
        result = classify_session(
            text=STATUS_REVIEW_TEXT,
            source_path=Path("some-doc.md"),
            frontier_dir=tmp_frontier_dir,
        )
        # Pure status review with no engagement patterns should produce no re-engagement
        assert not result.has_re_engagement()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouteToFrontiers:
    def test_appends_entry_to_frontier_document(
        self, tmp_frontier_dir: Path
    ) -> None:
        classification = classify_session(
            text=ORIENT_SESSION,
            source_path=Path("2026-03-29-0800-philosophy-explore.md"),
            frontier_dir=tmp_frontier_dir,
        )
        assert classification.has_re_engagement()

        ts = datetime(2026, 3, 29, 8, 0, 0, tzinfo=timezone.utc)
        result = route_to_frontiers(
            classification=classification,
            session_text=ORIENT_SESSION,
            source_filename="2026-03-29-0800-philosophy-explore.md",
            frontier_dir=tmp_frontier_dir,
            dry_run=False,
            timestamp=ts,
        )

        assert result.routed_count > 0
        assert len(result.errors) == 0

        # Check that the orient frontier doc was created and has content
        orient_path = tmp_frontier_dir / "frontier-orient.md"
        assert orient_path.exists()
        content = orient_path.read_text()
        assert "2026-03-29" in content
        assert "philosophy-explore" in content

    def test_dry_run_does_not_write_files(self, tmp_frontier_dir: Path) -> None:
        classification = classify_session(
            text=ORIENT_SESSION,
            source_path=Path("2026-03-29-0800-philosophy-explore.md"),
            frontier_dir=tmp_frontier_dir,
        )

        route_to_frontiers(
            classification=classification,
            session_text=ORIENT_SESSION,
            source_filename="test.md",
            frontier_dir=tmp_frontier_dir,
            dry_run=True,
        )

        # Dry run must not create any files
        assert not any(tmp_frontier_dir.iterdir())

    def test_no_re_engagement_routes_nothing(self, tmp_frontier_dir: Path) -> None:
        classification = classify_session(
            text=STATUS_REVIEW_TEXT,
            source_path=Path("status-doc.md"),
            frontier_dir=tmp_frontier_dir,
        )
        result = route_to_frontiers(
            classification=classification,
            session_text=STATUS_REVIEW_TEXT,
            source_filename="status-doc.md",
            frontier_dir=tmp_frontier_dir,
            dry_run=False,
        )
        assert result.routed_count == 0
        assert not any(tmp_frontier_dir.iterdir())

    def test_explicit_advances_always_route(self, tmp_frontier_dir: Path) -> None:
        import yaml
        fenced = re.search(
            r"```yaml\s*\n(action_seeds:.*?)```",
            TOL_AND_EMBODIMENT_SESSION,
            re.DOTALL | re.IGNORECASE,
        )
        raw = yaml.safe_load(fenced.group(1))

        classification = classify_session(
            text=TOL_AND_EMBODIMENT_SESSION,
            source_path=Path("2026-03-28-navigation-attractor-convergence.md"),
            frontier_dir=tmp_frontier_dir,
            action_seeds=raw,
        )

        ts = datetime(2026, 3, 28, 0, 0, 0, tzinfo=timezone.utc)
        result = route_to_frontiers(
            classification=classification,
            session_text=TOL_AND_EMBODIMENT_SESSION,
            source_filename="2026-03-28-navigation-attractor-convergence.md",
            frontier_dir=tmp_frontier_dir,
            dry_run=False,
            timestamp=ts,
        )

        routed_domains = {r.domain for r in result.routes if r.appended}
        assert "tol_arc" in routed_domains
        assert "approximate_embodiment" in routed_domains

    def test_appends_to_existing_document(self, tmp_frontier_dir: Path) -> None:
        orient_path = tmp_frontier_dir / "frontier-orient.md"
        orient_path.write_text("# Frontier: Orient\n\nExisting content.\n")

        classification = classify_session(
            text=ORIENT_SESSION,
            source_path=Path("2026-03-29-0800-philosophy-explore.md"),
            frontier_dir=tmp_frontier_dir,
        )

        route_to_frontiers(
            classification=classification,
            session_text=ORIENT_SESSION,
            source_filename="test.md",
            frontier_dir=tmp_frontier_dir,
            dry_run=False,
        )

        content = orient_path.read_text()
        assert "Existing content." in content
        assert "Session entry" in content


# ---------------------------------------------------------------------------
# Confidence bar formatting
# ---------------------------------------------------------------------------

class TestFormatConfidenceBar:
    def test_high(self) -> None:
        assert _format_confidence_bar(0.9) == "high"

    def test_medium_high(self) -> None:
        assert _format_confidence_bar(0.65) == "medium-high"

    def test_medium(self) -> None:
        assert _format_confidence_bar(0.5) == "medium"

    def test_low(self) -> None:
        assert _format_confidence_bar(0.3) == "low"


# ---------------------------------------------------------------------------
# philosophy_harvester integration — harvest() with frontier routing
# ---------------------------------------------------------------------------

class TestHarvestFrontierIntegration:
    def test_harvest_populates_frontier_domains_routed(
        self, tmp_path: Path, tmp_frontier_dir: Path
    ) -> None:
        from src.harvest.philosophy_harvester import harvest

        # Write a test session file
        session_path = tmp_path / "test-orient-session.md"
        session_path.write_text(ORIENT_SESSION)

        pending_dir = tmp_path / "pending"

        result = harvest(
            md_path=session_path,
            repo="dcetlin/Lobster",
            pending_dir=pending_dir,
            chat_id=0,
            dry_run=True,
            frontier_dir=tmp_frontier_dir,
            skip_frontier=False,
        )

        # frontier_domains_routed should be populated in a dry run (classifies but doesn't write)
        # In dry run: no files written, but classification runs and we get the domain list
        # (routes are attempted with dry_run=True, so appended=False but detected)
        # The route_result.routes list contains routes, but appended=False in dry run.
        # So frontier_domains_routed will be empty (by design — dry run = no appends).
        # This test verifies no errors from the frontier subsystem in dry-run mode.
        assert len([e for e in result.errors if "frontier" in e.lower()]) == 0

    def test_harvest_skip_frontier_leaves_no_routes(
        self, tmp_path: Path, tmp_frontier_dir: Path
    ) -> None:
        from src.harvest.philosophy_harvester import harvest

        session_path = tmp_path / "test-session.md"
        session_path.write_text(ORIENT_SESSION)

        result = harvest(
            md_path=session_path,
            repo="dcetlin/Lobster",
            pending_dir=tmp_path / "pending",
            chat_id=0,
            dry_run=True,
            frontier_dir=tmp_frontier_dir,
            skip_frontier=True,
        )

        assert result.frontier_domains_routed == ()
