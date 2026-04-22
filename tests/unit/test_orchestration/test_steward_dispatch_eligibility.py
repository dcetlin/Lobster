"""
Tests for _check_dispatch_eligibility() — loop-pattern-aware dispatch gating.

Patterns defined in oracle/patterns.md:
- Spiral:   oracle_pass_count >= SPIRAL_ORACLE_PASS_THRESHOLD (3) → escalate
- Dead-end: failed/blocked transitions >= DEAD_END_FAILURE_THRESHOLD (2) → pause
- Burst:    queue_depth spike → throttle (batches of BURST_BATCH_SIZE = 3)
- Default:  no pattern detected → dispatch

Tests are named after the behavior they verify, not the mechanism.
All threshold values are referenced from named constants, not magic literals.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.steward import (
    _check_dispatch_eligibility,
    _count_oracle_passes,
    _count_failed_or_blocked_transitions,
    SPIRAL_ORACLE_PASS_THRESHOLD,
    DEAD_END_FAILURE_THRESHOLD,
    BURST_BATCH_SIZE,
    BURST_BASELINE_QUEUE_DEPTH,
)
from src.orchestration.registry import UoW


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_uow(**kwargs) -> UoW:
    """Build a minimal UoW with sensible defaults for eligibility tests."""
    defaults = dict(
        id="uow_20260421_aabbcc",
        status="ready-for-steward",
        summary="Test UoW",
        source="test",
        source_issue_number=42,
        created_at="2026-04-21T00:00:00+00:00",
        updated_at="2026-04-21T00:00:00+00:00",
        steward_cycles=1,
        lifetime_cycles=1,
        register="operational",
    )
    defaults.update(kwargs)
    return UoW(**defaults)


def _audit_entry(event: str, to_status: str | None = None, from_status: str | None = None) -> dict[str, Any]:
    """Build a minimal audit_log entry dict."""
    return {
        "ts": "2026-04-21T00:00:00+00:00",
        "uow_id": "uow_20260421_aabbcc",
        "event": event,
        "from_status": from_status,
        "to_status": to_status,
        "agent": "steward",
        "note": None,
    }


def _oracle_approved_entries(n: int) -> list[dict[str, Any]]:
    """Return n oracle_approved audit entries."""
    return [_audit_entry("oracle_approved") for _ in range(n)]


def _blocked_or_failed_entries(n_failed: int = 0, n_blocked: int = 0) -> list[dict[str, Any]]:
    """Return audit entries for failed and blocked transitions."""
    entries = []
    for _ in range(n_failed):
        entries.append(_audit_entry("execution_failed", to_status="failed", from_status="active"))
    for _ in range(n_blocked):
        entries.append(_audit_entry("steward_surface", to_status="blocked", from_status="diagnosing"))
    return entries


# ---------------------------------------------------------------------------
# Unit tests: _count_oracle_passes (pure function)
# ---------------------------------------------------------------------------

class TestCountOraclePasses:
    """_count_oracle_passes counts oracle_approved audit entries."""

    def test_returns_zero_for_empty_audit_log(self):
        assert _count_oracle_passes([]) == 0

    def test_counts_oracle_approved_events(self):
        entries = _oracle_approved_entries(3)
        assert _count_oracle_passes(entries) == 3

    def test_ignores_non_oracle_events(self):
        entries = [
            _audit_entry("steward_diagnosis"),
            _audit_entry("prescription"),
            _audit_entry("execution_complete"),
        ]
        assert _count_oracle_passes(entries) == 0

    def test_counts_only_oracle_approved_not_other_oracle_events(self):
        entries = [
            _audit_entry("oracle_approved"),
            _audit_entry("oracle_review"),   # different event — not counted
            _audit_entry("oracle_approved"),
        ]
        assert _count_oracle_passes(entries) == 2


# ---------------------------------------------------------------------------
# Unit tests: _count_failed_or_blocked_transitions (pure function)
# ---------------------------------------------------------------------------

class TestCountFailedOrBlockedTransitions:
    """_count_failed_or_blocked_transitions counts to_status in {failed, blocked}."""

    def test_returns_zero_for_empty_audit_log(self):
        assert _count_failed_or_blocked_transitions([]) == 0

    def test_counts_failed_transitions(self):
        entries = _blocked_or_failed_entries(n_failed=2)
        assert _count_failed_or_blocked_transitions(entries) == 2

    def test_counts_blocked_transitions(self):
        entries = _blocked_or_failed_entries(n_blocked=2)
        assert _count_failed_or_blocked_transitions(entries) == 2

    def test_counts_mixed_failed_and_blocked(self):
        entries = _blocked_or_failed_entries(n_failed=1, n_blocked=1)
        assert _count_failed_or_blocked_transitions(entries) == 2

    def test_ignores_entries_without_to_status(self):
        entries = [
            _audit_entry("steward_diagnosis"),   # to_status=None
            _audit_entry("prescription"),
        ]
        assert _count_failed_or_blocked_transitions(entries) == 0

    def test_ignores_other_terminal_statuses(self):
        # done, expired are not failed/blocked
        entries = [
            {"event": "steward_closure", "to_status": "done", "from_status": "diagnosing"},
            {"event": "expire", "to_status": "expired", "from_status": "proposed"},
        ]
        assert _count_failed_or_blocked_transitions(entries) == 0


# ---------------------------------------------------------------------------
# Unit tests: _check_dispatch_eligibility — Spiral pattern
# ---------------------------------------------------------------------------

class TestDispatchEligibilitySpiral:
    """Spiral pattern: oracle_pass_count >= SPIRAL_ORACLE_PASS_THRESHOLD → escalate."""

    def test_escalate_when_oracle_passes_at_threshold(self):
        """UoW with exactly SPIRAL_ORACLE_PASS_THRESHOLD oracle passes → escalate."""
        uow = _make_uow()
        entries = _oracle_approved_entries(SPIRAL_ORACLE_PASS_THRESHOLD)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "escalate"

    def test_escalate_when_oracle_passes_exceed_threshold(self):
        """UoW with more than SPIRAL_ORACLE_PASS_THRESHOLD oracle passes → escalate."""
        uow = _make_uow()
        entries = _oracle_approved_entries(SPIRAL_ORACLE_PASS_THRESHOLD + 2)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "escalate"

    def test_no_escalate_when_oracle_passes_below_threshold(self):
        """UoW with fewer than SPIRAL_ORACLE_PASS_THRESHOLD oracle passes → not escalate."""
        uow = _make_uow()
        entries = _oracle_approved_entries(SPIRAL_ORACLE_PASS_THRESHOLD - 1)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result != "escalate"

    def test_spiral_threshold_constant_matches_patterns_md(self):
        """Named constant SPIRAL_ORACLE_PASS_THRESHOLD matches patterns.md value of 3."""
        assert SPIRAL_ORACLE_PASS_THRESHOLD == 3


# ---------------------------------------------------------------------------
# Unit tests: _check_dispatch_eligibility — Dead-end pattern
# ---------------------------------------------------------------------------

class TestDispatchEligibilityDeadEnd:
    """Dead-end pattern: failed/blocked >= DEAD_END_FAILURE_THRESHOLD → pause."""

    def test_pause_when_failures_at_threshold(self):
        """UoW with exactly DEAD_END_FAILURE_THRESHOLD failures → pause."""
        uow = _make_uow()
        entries = _blocked_or_failed_entries(n_failed=DEAD_END_FAILURE_THRESHOLD)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "pause"

    def test_pause_when_blocked_plus_failed_at_threshold(self):
        """One failed + one blocked = DEAD_END_FAILURE_THRESHOLD → pause."""
        uow = _make_uow()
        entries = _blocked_or_failed_entries(n_failed=1, n_blocked=1)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "pause"

    def test_pause_when_failures_exceed_threshold(self):
        """UoW with more than DEAD_END_FAILURE_THRESHOLD failures → pause."""
        uow = _make_uow()
        entries = _blocked_or_failed_entries(n_failed=DEAD_END_FAILURE_THRESHOLD + 1)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "pause"

    def test_no_pause_when_one_failure(self):
        """UoW with a single failure is below threshold → not pause (dispatch continues)."""
        uow = _make_uow()
        entries = _blocked_or_failed_entries(n_failed=1)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result != "pause"

    def test_dead_end_threshold_constant_matches_patterns_md(self):
        """Named constant DEAD_END_FAILURE_THRESHOLD matches patterns.md value of 2."""
        assert DEAD_END_FAILURE_THRESHOLD == 2


# ---------------------------------------------------------------------------
# Unit tests: _check_dispatch_eligibility — Burst pattern
# ---------------------------------------------------------------------------

class TestDispatchEligibilityBurst:
    """Burst pattern: queue_depth spike → throttle."""

    def test_throttle_when_queue_depth_exceeds_twice_baseline(self):
        """Queue depth >= 2x BURST_BASELINE_QUEUE_DEPTH → throttle."""
        uow = _make_uow()
        queue_depth = BURST_BASELINE_QUEUE_DEPTH * 2
        result = _check_dispatch_eligibility(uow, [], queue_depth=queue_depth)
        assert result == "throttle"

    def test_throttle_when_queue_depth_well_above_baseline(self):
        """Queue depth far above baseline also → throttle."""
        uow = _make_uow()
        queue_depth = BURST_BASELINE_QUEUE_DEPTH * 5
        result = _check_dispatch_eligibility(uow, [], queue_depth=queue_depth)
        assert result == "throttle"

    def test_no_throttle_when_queue_depth_below_spike_threshold(self):
        """Queue depth below 2x baseline → not throttle."""
        uow = _make_uow()
        queue_depth = BURST_BASELINE_QUEUE_DEPTH - 1
        result = _check_dispatch_eligibility(uow, [], queue_depth=queue_depth)
        assert result != "throttle"

    def test_burst_batch_size_constant_matches_patterns_md(self):
        """Named constant BURST_BATCH_SIZE matches patterns.md value of 3."""
        assert BURST_BATCH_SIZE == 3

    def test_burst_baseline_constant_matches_patterns_md(self):
        """Named constant BURST_BASELINE_QUEUE_DEPTH matches patterns.md hard lower bound of 6."""
        assert BURST_BASELINE_QUEUE_DEPTH == 6


# ---------------------------------------------------------------------------
# Unit tests: _check_dispatch_eligibility — Default (dispatch) path
# ---------------------------------------------------------------------------

class TestDispatchEligibilityDefault:
    """No pattern detected → dispatch."""

    def test_dispatch_when_no_patterns_detected(self):
        """Clean UoW with no failures, no oracle passes, normal queue → dispatch."""
        uow = _make_uow()
        result = _check_dispatch_eligibility(uow, [], queue_depth=1)
        assert result == "dispatch"

    def test_dispatch_for_fresh_uow_with_empty_audit_log(self):
        """Brand-new UoW (steward_cycles=0) → dispatch."""
        uow = _make_uow(steward_cycles=0, lifetime_cycles=0)
        result = _check_dispatch_eligibility(uow, [], queue_depth=1)
        assert result == "dispatch"


# ---------------------------------------------------------------------------
# Unit tests: precedence when multiple patterns fire
# ---------------------------------------------------------------------------

class TestDispatchEligibilityPrecedence:
    """When multiple patterns fire simultaneously, escalate > pause > throttle > dispatch."""

    def test_escalate_takes_precedence_over_pause(self):
        """UoW with spiral AND dead-end patterns → escalate (not pause)."""
        uow = _make_uow()
        entries = (
            _oracle_approved_entries(SPIRAL_ORACLE_PASS_THRESHOLD)
            + _blocked_or_failed_entries(n_failed=DEAD_END_FAILURE_THRESHOLD)
        )
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "escalate"

    def test_escalate_takes_precedence_over_throttle(self):
        """UoW with spiral AND burst queue → escalate (not throttle)."""
        uow = _make_uow()
        entries = _oracle_approved_entries(SPIRAL_ORACLE_PASS_THRESHOLD)
        queue_depth = BURST_BASELINE_QUEUE_DEPTH * 2
        result = _check_dispatch_eligibility(uow, entries, queue_depth=queue_depth)
        assert result == "escalate"

    def test_pause_takes_precedence_over_throttle(self):
        """UoW with dead-end AND burst queue → pause (not throttle)."""
        uow = _make_uow()
        entries = _blocked_or_failed_entries(n_failed=DEAD_END_FAILURE_THRESHOLD)
        queue_depth = BURST_BASELINE_QUEUE_DEPTH * 2
        result = _check_dispatch_eligibility(uow, entries, queue_depth=queue_depth)
        assert result == "pause"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
