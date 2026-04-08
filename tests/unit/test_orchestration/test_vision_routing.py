"""
Unit tests for vision-anchored routing (vision_routing.py).

Tests cover:
- resolve_vision_route with valid vision_ref produces vision-anchored route_reason
- resolve_vision_route with null vision_ref produces heuristic fallback (explicit)
- resolve_vision_route with malformed vision_ref produces heuristic fallback
- Staleness detection based on layer thresholds
- sc-4 verification: routing outcomes differ with/without vision_ref
- check_vision_ref_staleness helper for morning briefing
- format_staleness_report output formatting
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path


@pytest.fixture(autouse=True)
def import_vision_routing():
    """Ensure src is on sys.path for all tests in this module."""
    import sys
    repo_root = Path(__file__).parent.parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


# ---------------------------------------------------------------------------
# Test fixtures — create UoW instances with/without vision_ref
# ---------------------------------------------------------------------------

@pytest.fixture
def uow_with_vision_ref():
    """UoW with a valid vision_ref."""
    from src.orchestration.registry import UoW, UoWStatus

    return UoW(
        id="uow_20260408_abc123",
        status=UoWStatus.PENDING,
        summary="Wire vision_ref to classifier",
        source="github:issue/509",
        source_issue_number=509,
        created_at="2026-04-08T10:00:00+00:00",
        updated_at="2026-04-08T10:00:00+00:00",
        register="operational",
        vision_ref={
            "layer": "active_project",
            "field": "phase_intent",
            "statement": "Build the substrate that lets every agent make intent-anchored decisions.",
            "anchored_at": datetime.now(timezone.utc).isoformat(),
        },
    )


@pytest.fixture
def uow_without_vision_ref():
    """UoW with no vision_ref (null)."""
    from src.orchestration.registry import UoW, UoWStatus

    return UoW(
        id="uow_20260408_def456",
        status=UoWStatus.PENDING,
        summary="Legacy UoW without vision anchor",
        source="github:issue/100",
        source_issue_number=100,
        created_at="2026-04-08T10:00:00+00:00",
        updated_at="2026-04-08T10:00:00+00:00",
        register="operational",
        vision_ref=None,
    )


@pytest.fixture
def uow_with_stale_vision_ref():
    """UoW with a stale vision_ref (older than threshold)."""
    from src.orchestration.registry import UoW, UoWStatus

    # Anchor from 60 days ago — exceeds current_focus threshold (7 days)
    stale_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

    return UoW(
        id="uow_20260408_ghi789",
        status=UoWStatus.PENDING,
        summary="UoW with stale current_focus anchor",
        source="github:issue/200",
        source_issue_number=200,
        created_at="2026-04-08T10:00:00+00:00",
        updated_at="2026-04-08T10:00:00+00:00",
        register="operational",
        vision_ref={
            "layer": "current_focus",
            "field": "this_week.primary",
            "statement": "Old focus statement",
            "anchored_at": stale_date,
        },
    )


# ---------------------------------------------------------------------------
# resolve_vision_route tests
# ---------------------------------------------------------------------------

class TestResolveVisionRoute:
    """Tests for the main resolve_vision_route function."""

    def test_valid_vision_ref_produces_anchored_route_reason(self, uow_with_vision_ref):
        from src.orchestration.vision_routing import resolve_vision_route, VISION_ROUTE_PREFIX

        result = resolve_vision_route(uow_with_vision_ref)

        assert result.anchored is True
        assert result.route_reason.startswith(VISION_ROUTE_PREFIX)
        assert "vision.active_project.phase_intent" in result.route_reason
        assert "Build the substrate" in result.route_reason
        assert result.vision_layer == "active_project"
        assert result.vision_field == "phase_intent"
        assert result.fallback_logged is False

    def test_null_vision_ref_produces_fallback_route_reason(self, uow_without_vision_ref):
        from src.orchestration.vision_routing import resolve_vision_route, FALLBACK_ROUTE_PREFIX

        result = resolve_vision_route(uow_without_vision_ref, log_fallback=False)

        assert result.anchored is False
        assert result.route_reason.startswith(FALLBACK_ROUTE_PREFIX)
        assert "vision_ref null" in result.route_reason
        assert "register (operational)" in result.route_reason
        assert result.vision_layer is None
        assert result.vision_field is None

    def test_fallback_is_logged_when_enabled(self, uow_without_vision_ref, caplog):
        import logging
        from src.orchestration.vision_routing import resolve_vision_route

        with caplog.at_level(logging.INFO):
            result = resolve_vision_route(uow_without_vision_ref, log_fallback=True)

        assert result.fallback_logged is True
        assert "falling back to heuristic routing" in caplog.text

    def test_malformed_vision_ref_produces_fallback(self):
        """vision_ref with missing layer/field triggers fallback."""
        from src.orchestration.registry import UoW, UoWStatus
        from src.orchestration.vision_routing import resolve_vision_route, FALLBACK_ROUTE_PREFIX

        uow = UoW(
            id="uow_20260408_jkl012",
            status=UoWStatus.PENDING,
            summary="Malformed vision_ref UoW",
            source="github:issue/300",
            source_issue_number=300,
            created_at="2026-04-08T10:00:00+00:00",
            updated_at="2026-04-08T10:00:00+00:00",
            register="operational",
            vision_ref={"statement": "Missing layer and field"},
        )

        result = resolve_vision_route(uow, log_fallback=False)

        assert result.anchored is False
        assert "malformed" in result.route_reason
        assert result.route_reason.startswith(FALLBACK_ROUTE_PREFIX)

    def test_stale_vision_ref_is_flagged(self, uow_with_stale_vision_ref):
        from src.orchestration.vision_routing import resolve_vision_route

        result = resolve_vision_route(uow_with_stale_vision_ref)

        assert result.anchored is True
        assert result.stale is True
        assert "[STALE]" in result.route_reason


# ---------------------------------------------------------------------------
# sc-4 verification: routing outcomes differ with/without vision_ref
# ---------------------------------------------------------------------------

class TestSC4RoutingDifference:
    """
    Verify Phase 1 success criterion sc-4:
    "Removing vision.yaml would cause structurally different routing outcomes."

    These tests demonstrate that the presence/absence of vision_ref produces
    measurably different route_reason values.
    """

    def test_routing_differs_with_and_without_vision_ref(
        self, uow_with_vision_ref, uow_without_vision_ref
    ):
        """Core sc-4 test: route_reason values are structurally different."""
        from src.orchestration.vision_routing import (
            resolve_vision_route,
            VISION_ROUTE_PREFIX,
            FALLBACK_ROUTE_PREFIX,
        )

        result_with = resolve_vision_route(uow_with_vision_ref)
        result_without = resolve_vision_route(uow_without_vision_ref, log_fallback=False)

        # Route reasons must be structurally different
        assert result_with.route_reason != result_without.route_reason

        # Anchored vs fallback distinction must be visible in output
        assert result_with.route_reason.startswith(VISION_ROUTE_PREFIX)
        assert result_without.route_reason.startswith(FALLBACK_ROUTE_PREFIX)

        # Anchored status must differ
        assert result_with.anchored is True
        assert result_without.anchored is False

    def test_same_uow_different_vision_ref_produces_different_routing(self):
        """Same UoW content with different vision_ref values routes differently."""
        from src.orchestration.registry import UoW, UoWStatus
        from src.orchestration.vision_routing import resolve_vision_route

        base_kwargs = dict(
            id="uow_20260408_same01",
            status=UoWStatus.PENDING,
            summary="Same summary for both",
            source="github:issue/400",
            source_issue_number=400,
            created_at="2026-04-08T10:00:00+00:00",
            updated_at="2026-04-08T10:00:00+00:00",
            register="operational",
        )

        uow_a = UoW(
            **base_kwargs,
            vision_ref={
                "layer": "current_focus",
                "field": "this_week.primary",
                "statement": "First priority",
                "anchored_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        uow_b = UoW(
            **base_kwargs,
            vision_ref={
                "layer": "core",
                "field": "inviolable_constraints[0]",
                "statement": "Constraint one",
                "anchored_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        result_a = resolve_vision_route(uow_a)
        result_b = resolve_vision_route(uow_b)

        # Both are anchored but to different vision layers
        assert result_a.anchored is True
        assert result_b.anchored is True
        assert result_a.vision_layer == "current_focus"
        assert result_b.vision_layer == "core"
        assert result_a.route_reason != result_b.route_reason

    def test_disabling_vision_ref_changes_routing_outcome(self):
        """
        Explicit test for sc-4: if we "disable" vision_ref (set to None),
        the routing outcome is measurably different.
        """
        from src.orchestration.registry import UoW, UoWStatus
        from src.orchestration.vision_routing import resolve_vision_route
        import dataclasses

        # Create UoW with vision_ref
        uow_enabled = UoW(
            id="uow_20260408_sc4test",
            status=UoWStatus.PENDING,
            summary="sc-4 test UoW",
            source="github:issue/509",
            source_issue_number=509,
            created_at="2026-04-08T10:00:00+00:00",
            updated_at="2026-04-08T10:00:00+00:00",
            register="operational",
            vision_ref={
                "layer": "active_project",
                "field": "phase_intent",
                "statement": "Phase 1 completion",
                "anchored_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # "Disable" vision_ref by creating identical UoW with vision_ref=None
        uow_disabled = dataclasses.replace(uow_enabled, vision_ref=None)

        result_enabled = resolve_vision_route(uow_enabled)
        result_disabled = resolve_vision_route(uow_disabled, log_fallback=False)

        # Routing outcomes must differ
        assert result_enabled.anchored != result_disabled.anchored
        assert result_enabled.route_reason != result_disabled.route_reason

        # The enabled version references the vision layer
        assert "vision.active_project.phase_intent" in result_enabled.route_reason

        # The disabled version explicitly notes the fallback
        assert "vision_ref null" in result_disabled.route_reason


# ---------------------------------------------------------------------------
# Staleness detection tests
# ---------------------------------------------------------------------------

class TestStalenessDetection:
    """Tests for staleness threshold enforcement."""

    def test_current_focus_threshold_is_7_days(self):
        from src.orchestration.vision_routing import STALENESS_THRESHOLDS
        assert STALENESS_THRESHOLDS["current_focus"] == 7

    def test_active_project_threshold_is_30_days(self):
        from src.orchestration.vision_routing import STALENESS_THRESHOLDS
        assert STALENESS_THRESHOLDS["active_project"] == 30

    def test_core_threshold_is_90_days(self):
        from src.orchestration.vision_routing import STALENESS_THRESHOLDS
        assert STALENESS_THRESHOLDS["core"] == 90

    def test_fresh_anchor_is_not_stale(self):
        from src.orchestration.vision_routing import _check_anchor_staleness

        fresh_timestamp = datetime.now(timezone.utc).isoformat()
        assert _check_anchor_staleness(fresh_timestamp, "current_focus") is False

    def test_old_anchor_is_stale(self):
        from src.orchestration.vision_routing import _check_anchor_staleness

        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        assert _check_anchor_staleness(old_timestamp, "current_focus") is True

    def test_missing_anchor_timestamp_is_stale(self):
        from src.orchestration.vision_routing import _check_anchor_staleness
        assert _check_anchor_staleness(None, "current_focus") is True


# ---------------------------------------------------------------------------
# Morning briefing helpers
# ---------------------------------------------------------------------------

class TestMorningBriefingHelpers:
    """Tests for check_vision_ref_staleness and format_staleness_report."""

    def test_check_vision_ref_staleness_finds_stale_anchors(
        self, uow_with_stale_vision_ref, uow_with_vision_ref
    ):
        from src.orchestration.vision_routing import check_vision_ref_staleness

        result = check_vision_ref_staleness([uow_with_stale_vision_ref, uow_with_vision_ref])

        # Only the stale UoW should be in the result
        assert len(result) == 1
        assert result[0]["uow_id"] == uow_with_stale_vision_ref.id
        assert result[0]["vision_layer"] == "current_focus"

    def test_check_vision_ref_staleness_skips_null_vision_ref(self, uow_without_vision_ref):
        from src.orchestration.vision_routing import check_vision_ref_staleness

        result = check_vision_ref_staleness([uow_without_vision_ref])
        assert len(result) == 0

    def test_format_staleness_report_empty(self):
        from src.orchestration.vision_routing import format_staleness_report

        result = format_staleness_report([])
        assert "No stale vision anchors found" in result

    def test_format_staleness_report_with_entries(self):
        from src.orchestration.vision_routing import format_staleness_report

        stale_anchors = [
            {
                "uow_id": "uow_test_001",
                "summary": "Test UoW",
                "vision_layer": "current_focus",
                "vision_field": "this_week.primary",
                "anchored_at": "2026-01-01T00:00:00+00:00",
                "threshold_days": 7,
            }
        ]

        result = format_staleness_report(stale_anchors)

        assert "Stale Vision Anchors" in result
        assert "uow_test_001" in result
        assert "vision.current_focus.this_week.primary" in result
        assert "threshold 7d" in result


# ---------------------------------------------------------------------------
# VisionRouteResult immutability
# ---------------------------------------------------------------------------

class TestVisionRouteResultImmutability:
    def test_result_is_frozen(self, uow_with_vision_ref):
        from src.orchestration.vision_routing import resolve_vision_route

        result = resolve_vision_route(uow_with_vision_ref)

        with pytest.raises((AttributeError, TypeError)):
            result.anchored = False  # type: ignore[misc]
