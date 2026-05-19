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

import json
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
    _is_infra_kill_audit_entry,
    SPIRAL_ORACLE_PASS_THRESHOLD,
    DEAD_END_FAILURE_THRESHOLD,
    BURST_BATCH_SIZE,
    BURST_BASELINE_QUEUE_DEPTH,
    DEAD_END_INFRA_KILL_REASONS,
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
    """Return audit entries for genuine failed and blocked transitions (no infra note)."""
    entries = []
    for _ in range(n_failed):
        entries.append(_audit_entry("execution_failed", to_status="failed", from_status="active"))
    for _ in range(n_blocked):
        entries.append(_audit_entry("steward_surface", to_status="blocked", from_status="diagnosing"))
    return entries


def _infra_kill_entry(reason_code: str) -> dict[str, Any]:
    """Build an audit entry that represents an infrastructure kill via Registry.fail_uow.

    The note format mirrors what Registry.fail_uow writes:
        {"actor": "executor", "reason": "<code>: <detail>", "timestamp": "..."}
    """
    note = json.dumps({
        "actor": "executor",
        "reason": f"{reason_code}: infrastructure detail",
        "timestamp": "2026-04-21T00:00:00+00:00",
    })
    return {
        "ts": "2026-04-21T00:00:00+00:00",
        "uow_id": "uow_20260421_aabbcc",
        "event": "execution_failed",
        "from_status": "active",
        "to_status": "failed",
        "agent": "executor",
        "note": note,
    }


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

    def test_infra_kills_do_not_count_toward_dead_end(self):
        """Infrastructure kill entries (to_status=failed, infra reason) are excluded."""
        entries = [_infra_kill_entry(code) for code in DEAD_END_INFRA_KILL_REASONS]
        assert _count_failed_or_blocked_transitions(entries) == 0

    def test_genuine_failure_counts_even_when_infra_kills_present(self):
        """A genuine failure still counts when mixed with infrastructure kills."""
        entries = (
            [_infra_kill_entry("executing_orphan"), _infra_kill_entry("ttl_exceeded")]
            + _blocked_or_failed_entries(n_failed=1)
        )
        assert _count_failed_or_blocked_transitions(entries) == 1

    def test_mixed_infra_and_real_only_real_ones_count(self):
        """Only genuine failures count; infra kills in the same audit log are skipped."""
        entries = (
            [_infra_kill_entry("executing_orphan")] * 3
            + _blocked_or_failed_entries(n_failed=2)
        )
        # 3 infra kills + 2 genuine = only 2 should count
        assert _count_failed_or_blocked_transitions(entries) == 2


# ---------------------------------------------------------------------------
# Unit tests: _is_infra_kill_audit_entry (pure function)
# ---------------------------------------------------------------------------

class TestIsInfraKillAuditEntry:
    """_is_infra_kill_audit_entry discriminates infrastructure kills from genuine failures."""

    def test_executing_orphan_reason_is_infra_kill(self):
        entry = _infra_kill_entry("executing_orphan")
        assert _is_infra_kill_audit_entry(entry) is True

    def test_orphan_kill_before_start_is_infra_kill(self):
        entry = _infra_kill_entry("orphan_kill_before_start")
        assert _is_infra_kill_audit_entry(entry) is True

    def test_orphan_kill_during_execution_is_infra_kill(self):
        entry = _infra_kill_entry("orphan_kill_during_execution")
        assert _is_infra_kill_audit_entry(entry) is True

    def test_ttl_exceeded_is_infra_kill(self):
        entry = _infra_kill_entry("ttl_exceeded")
        assert _is_infra_kill_audit_entry(entry) is True

    def test_called_process_error_is_not_infra_kill(self):
        """CalledProcessError is a genuine execution failure, not an infrastructure kill."""
        note = json.dumps({
            "actor": "executor",
            "reason": "CalledProcessError: Command returned non-zero exit status 1",
            "timestamp": "2026-04-21T00:00:00+00:00",
        })
        entry = _audit_entry("execution_failed", to_status="failed")
        entry["note"] = note
        assert _is_infra_kill_audit_entry(entry) is False

    def test_entry_without_note_is_not_infra_kill(self):
        entry = _audit_entry("execution_failed", to_status="failed")
        assert _is_infra_kill_audit_entry(entry) is False

    def test_entry_with_non_json_note_is_not_infra_kill(self):
        entry = _audit_entry("execution_failed", to_status="failed")
        entry["note"] = "plain text reason"
        assert _is_infra_kill_audit_entry(entry) is False

    def test_entry_without_reason_field_is_not_infra_kill(self):
        note = json.dumps({"actor": "executor", "timestamp": "2026-04-21T00:00:00+00:00"})
        entry = _audit_entry("execution_failed", to_status="failed")
        entry["note"] = note
        assert _is_infra_kill_audit_entry(entry) is False

    def test_dead_end_infra_kill_reasons_constant_covers_all_known_codes(self):
        """DEAD_END_INFRA_KILL_REASONS covers the four infrastructure kill codes from the data audit."""
        expected = {
            "executing_orphan",
            "orphan_kill_before_start",
            "orphan_kill_during_execution",
            "ttl_exceeded",
        }
        assert expected.issubset(DEAD_END_INFRA_KILL_REASONS)


# ---------------------------------------------------------------------------
# Integration: dead-end gate with infra-kill discrimination
# ---------------------------------------------------------------------------

class TestDeadEndGateInfraKillDiscrimination:
    """Dead-end gate must not fire when all failures are infrastructure kills."""

    def test_all_infra_kills_do_not_trigger_dead_end_gate(self):
        """DEAD_END_FAILURE_THRESHOLD infra kills → dispatch (gate does not fire)."""
        uow = _make_uow()
        entries = [_infra_kill_entry("executing_orphan")] * DEAD_END_FAILURE_THRESHOLD
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "dispatch"

    def test_many_infra_kills_alone_do_not_trigger_dead_end_gate(self):
        """More infra kills than the threshold → gate still does not fire."""
        uow = _make_uow()
        entries = [_infra_kill_entry("ttl_exceeded")] * (DEAD_END_FAILURE_THRESHOLD + 5)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "dispatch"

    def test_real_failures_still_trigger_dead_end_gate(self):
        """Genuine failures (no infra note) at threshold → gate fires as before."""
        uow = _make_uow()
        entries = _blocked_or_failed_entries(n_failed=DEAD_END_FAILURE_THRESHOLD)
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "pause"

    def test_infra_kills_plus_one_real_below_threshold_does_not_pause(self):
        """Many infra kills + one genuine failure (below threshold) → dispatch, not pause."""
        uow = _make_uow()
        entries = (
            [_infra_kill_entry("orphan_kill_during_execution")] * 5
            + _blocked_or_failed_entries(n_failed=DEAD_END_FAILURE_THRESHOLD - 1)
        )
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result != "pause"

    def test_infra_kills_plus_real_failures_at_threshold_triggers_pause(self):
        """Infra kills do not dilute: DEAD_END_FAILURE_THRESHOLD real failures → pause."""
        uow = _make_uow()
        entries = (
            [_infra_kill_entry("executing_orphan")] * 10
            + _blocked_or_failed_entries(n_failed=DEAD_END_FAILURE_THRESHOLD)
        )
        result = _check_dispatch_eligibility(uow, entries, queue_depth=1)
        assert result == "pause"


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


# ---------------------------------------------------------------------------
# Integration: dispatch_eligibility_skip routed to dispatch_skip_log, not audit_log
# ---------------------------------------------------------------------------

class TestDispatchSkipRoutedToSeparateTable:
    """dispatch_eligibility_skip events go to dispatch_skip_log, not audit_log.

    Separation ensures the main audit_log retains its forensic signal-to-noise
    ratio. These records previously constituted ~88% of audit_log volume.
    """

    @pytest.fixture
    def registry(self, tmp_path):
        """Registry backed by a temporary SQLite DB."""
        from src.orchestration.registry import Registry
        return Registry(tmp_path / "test_skip_log.db")

    def test_throttle_skip_writes_to_dispatch_skip_log_not_audit_log(self, registry, tmp_path):
        """A throttle-pattern skip routes to dispatch_skip_log, leaving audit_log clean."""
        import sqlite3

        uow_id = "uow_test_throttle_skip"
        registry.write_dispatch_skip(uow_id, {
            "actor": "steward",
            "uow_id": uow_id,
            "steward_cycles": 3,
            "eligibility": "throttle",
            "timestamp": "2026-05-16T00:00:00+00:00",
        })

        conn = sqlite3.connect(str(tmp_path / "test_skip_log.db"))

        # audit_log must have no dispatch_eligibility_skip entries
        audit_skip_count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event = 'dispatch_eligibility_skip'"
        ).fetchone()[0]
        assert audit_skip_count == 0, (
            f"Expected 0 dispatch_eligibility_skip entries in audit_log, got {audit_skip_count}"
        )

        # dispatch_skip_log must have exactly one entry with eligibility='throttle'
        skip_rows = conn.execute(
            "SELECT eligibility FROM dispatch_skip_log WHERE uow_id = ?",
            (uow_id,),
        ).fetchall()
        assert len(skip_rows) == 1, f"Expected 1 row in dispatch_skip_log, got {len(skip_rows)}"
        assert skip_rows[0][0] == "throttle"

        conn.close()

    def test_pause_skip_writes_to_dispatch_skip_log_not_audit_log(self, registry, tmp_path):
        """A pause-pattern skip (dead-end) routes to dispatch_skip_log, leaving audit_log clean."""
        import sqlite3

        uow_id = "uow_test_pause_skip"
        registry.write_dispatch_skip(uow_id, {
            "actor": "steward",
            "uow_id": uow_id,
            "steward_cycles": 5,
            "eligibility": "pause",
            "timestamp": "2026-05-16T00:00:00+00:00",
        })

        conn = sqlite3.connect(str(tmp_path / "test_skip_log.db"))

        audit_skip_count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event = 'dispatch_eligibility_skip'"
        ).fetchone()[0]
        assert audit_skip_count == 0

        skip_rows = conn.execute(
            "SELECT eligibility FROM dispatch_skip_log WHERE uow_id = ?",
            (uow_id,),
        ).fetchall()
        assert len(skip_rows) == 1
        assert skip_rows[0][0] == "pause"

        conn.close()

    def test_multiple_skips_accumulate_in_dispatch_skip_log(self, registry, tmp_path):
        """Multiple skip writes all land in dispatch_skip_log; audit_log remains empty of skips."""
        import sqlite3

        SKIP_COUNT = 5
        for i in range(SKIP_COUNT):
            registry.write_dispatch_skip(f"uow_multi_{i}", {
                "actor": "steward",
                "uow_id": f"uow_multi_{i}",
                "steward_cycles": i + 1,
                "eligibility": "throttle",
                "timestamp": "2026-05-16T00:00:00+00:00",
            })

        conn = sqlite3.connect(str(tmp_path / "test_skip_log.db"))

        audit_skip_count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event = 'dispatch_eligibility_skip'"
        ).fetchone()[0]
        assert audit_skip_count == 0

        skip_total = conn.execute(
            "SELECT COUNT(*) FROM dispatch_skip_log"
        ).fetchone()[0]
        assert skip_total == SKIP_COUNT

        conn.close()

    def test_dispatch_skip_log_stores_eligibility_field(self, registry, tmp_path):
        """Each dispatch_skip_log row carries the eligibility value for diagnostics."""
        import sqlite3

        for eligibility in ("throttle", "pause", "escalate"):
            registry.write_dispatch_skip(f"uow_{eligibility}", {
                "actor": "steward",
                "uow_id": f"uow_{eligibility}",
                "steward_cycles": 1,
                "eligibility": eligibility,
                "timestamp": "2026-05-16T00:00:00+00:00",
            })

        conn = sqlite3.connect(str(tmp_path / "test_skip_log.db"))
        rows = conn.execute(
            "SELECT eligibility FROM dispatch_skip_log ORDER BY eligibility"
        ).fetchall()
        conn.close()

        stored = {r[0] for r in rows}
        assert stored == {"throttle", "pause", "escalate"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
