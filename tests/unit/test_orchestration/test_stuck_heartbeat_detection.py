"""
Tests for the stuck-agent detector (Phase 2c of steward-heartbeat.py, issue #994).

Behavior verified (derived from spec, not from implementation):

- test_consecutive_zero_delta_tail_no_zero:
  A sequence with positive deltas throughout returns 0.

- test_consecutive_zero_delta_tail_one_zero_at_end:
  A sequence whose last delta is zero returns 1.

- test_consecutive_zero_delta_tail_two_zeros_at_end:
  A sequence with two consecutive zero deltas at the end returns 2.

- test_consecutive_zero_delta_tail_zero_then_nonzero:
  An interior zero followed by a nonzero delta does not count in the tail.

- test_consecutive_zero_delta_tail_single_element:
  A single-element list returns 0 (no intervals to evaluate).

- test_consecutive_zero_delta_tail_negative_delta:
  A negative delta (token count went down) is treated as zero — counts.

- test_no_candidates_returns_zero_flagged:
  When no UoWs have enough token-bearing heartbeats, nothing is flagged.

- test_uow_with_positive_deltas_not_flagged:
  A UoW with consistently increasing token counts is not flagged as stuck.

- test_uow_with_consecutive_zero_deltas_flagged:
  A UoW with N consecutive zero-delta intervals at the threshold is flagged.

- test_uow_requires_N_plus_1_snapshots:
  A UoW with exactly N (not N+1) token snapshots is not evaluated (not enough).

- test_registry_get_heartbeat_log_failure_skipped:
  When get_heartbeat_log raises, the UoW is skipped without crashing the loop.

- test_dry_run_does_not_mutate_state:
  dry_run mode produces the same flagged count but does not append observations.

- test_registry_write_heartbeat_stores_token_snapshot_in_db:
  End-to-end: Registry.write_heartbeat with token_usage inserts a row into
  uow_heartbeat_log (integration test using an in-memory SQLite DB).

- test_registry_write_heartbeat_without_token_skips_log:
  Registry.write_heartbeat with token_usage=None inserts nothing into
  uow_heartbeat_log.

- test_registry_get_heartbeat_log_returns_only_non_null_rows:
  get_heartbeat_log filters out rows where token_usage IS NULL.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parents[3]
for _p in [str(_ROOT), str(_ROOT / "src"), str(_ROOT / "scheduled-tasks")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load steward-heartbeat.py as a module (the hyphen requires spec_from_file_location).
import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "steward_heartbeat",
    str(_ROOT / "scheduled-tasks" / "steward-heartbeat.py"),
)
_MODULE = importlib.util.module_from_spec(_SPEC)
# Patch heavy imports before executing the module so it doesn't require
# a live DB, started services, or production paths.
# Also register the module itself in sys.modules BEFORE exec_module so that
# dataclasses can resolve cls.__module__ (a known Python 3.12 requirement).
_PATCH_TARGETS = {
    "src.orchestration.steward": MagicMock(),
    "src.orchestration.github_sync": MagicMock(),
    "src.orchestration.paths": MagicMock(REGISTRY_DB=Path("/tmp/test_registry.db")),
    "startup_sweep": MagicMock(),
    "steward_heartbeat": _MODULE,
}
with patch.dict("sys.modules", _PATCH_TARGETS):
    _SPEC.loader.exec_module(_MODULE)

_consecutive_zero_delta_tail = _MODULE._consecutive_zero_delta_tail
detect_stuck_heartbeat_uows = _MODULE.detect_stuck_heartbeat_uows


# ---------------------------------------------------------------------------
# Pure function tests: _consecutive_zero_delta_tail
# ---------------------------------------------------------------------------

class TestConsecutiveZeroDeltaTail:
    """Pure function — no registry needed."""

    def test_no_zero_deltas(self) -> None:
        """All positive deltas: tail count is 0."""
        assert _consecutive_zero_delta_tail([100, 200, 300]) == 0

    def test_one_zero_at_end(self) -> None:
        """Last delta is zero: tail count is 1."""
        assert _consecutive_zero_delta_tail([100, 200, 200]) == 1

    def test_two_zeros_at_end(self) -> None:
        """Two consecutive zero deltas at end: tail count is 2."""
        assert _consecutive_zero_delta_tail([100, 200, 200, 200]) == 2

    def test_all_zeros(self) -> None:
        """All zero deltas: tail count equals number of intervals."""
        assert _consecutive_zero_delta_tail([100, 100, 100]) == 2

    def test_interior_zero_then_nonzero(self) -> None:
        """Interior zero does not count — tail resets on nonzero."""
        assert _consecutive_zero_delta_tail([100, 100, 200]) == 0

    def test_single_element(self) -> None:
        """Single element: no intervals, tail count is 0."""
        assert _consecutive_zero_delta_tail([100]) == 0

    def test_empty_list(self) -> None:
        """Empty list: tail count is 0."""
        assert _consecutive_zero_delta_tail([]) == 0

    def test_negative_delta_counts_as_zero(self) -> None:
        """Negative delta (token count went down) is treated as zero-delta."""
        # [100, 50] → delta = -50 ≤ 0 → counts
        assert _consecutive_zero_delta_tail([100, 50]) == 1

    def test_mixed_zero_and_positive(self) -> None:
        """Mixed sequence: only tail zeros count."""
        # [0, 100, 200, 200, 200] → deltas: 100, 100, 0, 0 → tail=2
        assert _consecutive_zero_delta_tail([0, 100, 200, 200, 200]) == 2


# ---------------------------------------------------------------------------
# Helpers: mock registry factory
# ---------------------------------------------------------------------------

def _make_registry(candidates: list[dict], log_by_uow: dict[str, list[dict]]) -> MagicMock:
    """Build a registry mock for detect_stuck_heartbeat_uows tests."""
    mock = MagicMock()
    mock.get_executing_uows_with_heartbeats.return_value = candidates
    mock.get_heartbeat_log.side_effect = lambda uow_id: log_by_uow.get(uow_id, [])
    return mock


def _make_log_rows(token_usages: list[int]) -> list[dict]:
    """Build synthetic heartbeat log rows from a list of token counts."""
    return [
        {"id": i + 1, "uow_id": "uow_x", "recorded_at": f"2026-04-27T00:0{i}:00+00:00", "token_usage": t}
        for i, t in enumerate(token_usages)
    ]


# ---------------------------------------------------------------------------
# Tests: detect_stuck_heartbeat_uows behavior
# ---------------------------------------------------------------------------

class TestDetectStuckHeartbeatUows:
    """detect_stuck_heartbeat_uows: integration with mocked registry."""

    def test_no_candidates_returns_zero_flagged(self) -> None:
        """No UoWs with enough heartbeats: nothing flagged."""
        registry = _make_registry([], {})
        result = detect_stuck_heartbeat_uows(registry)
        assert result.checked == 0
        assert result.flagged == 0

    def test_uow_with_positive_deltas_not_flagged(self) -> None:
        """UoW with consistently growing token count is not stuck."""
        candidates = [{"id": "uow_1", "status": "executing", "heartbeat_at": None, "heartbeat_ttl": 300}]
        log_rows = _make_log_rows([100, 300, 600])  # deltas: 200, 300 — all positive
        registry = _make_registry(candidates, {"uow_1": log_rows})
        result = detect_stuck_heartbeat_uows(registry)
        assert result.checked == 1
        assert result.flagged == 0

    def test_uow_with_consecutive_zero_deltas_flagged(self) -> None:
        """UoW with N consecutive zero deltas at threshold is flagged."""
        # STUCK_HEARTBEAT_CONSECUTIVE_INTERVALS == 2
        # Need 3 snapshots: [100, 100, 100] → deltas: 0, 0 → zero_tail=2 → flagged
        candidates = [{"id": "uow_stuck", "status": "executing", "heartbeat_at": None, "heartbeat_ttl": 300}]
        log_rows = _make_log_rows([100, 100, 100])
        registry = _make_registry(candidates, {"uow_stuck": log_rows})

        with patch.object(_MODULE, "_append_observation") as mock_obs:
            result = detect_stuck_heartbeat_uows(registry)
            assert result.flagged == 1
            # Side effect: observation appended to log
            mock_obs.assert_called_once()
            call_args = mock_obs.call_args[0][0]
            assert "stuck_heartbeat" in call_args
            assert "uow_stuck" in call_args

    def test_uow_requires_n_plus_1_snapshots(self) -> None:
        """UoW with exactly N snapshots (not N+1) is skipped — insufficient data."""
        # STUCK_HEARTBEAT_CONSECUTIVE_INTERVALS == 2 → need 3 snapshots for 2 intervals.
        # With only 2 snapshots there is only 1 interval — cannot evaluate 2.
        candidates = [{"id": "uow_2snap", "status": "executing", "heartbeat_at": None, "heartbeat_ttl": 300}]
        log_rows = _make_log_rows([100, 100])  # only 2 snapshots → 1 interval
        registry = _make_registry(candidates, {"uow_2snap": log_rows})
        result = detect_stuck_heartbeat_uows(registry)
        assert result.flagged == 0, (
            "With only 2 snapshots (1 interval) the UoW must not be flagged "
            "— not enough data to evaluate 2 consecutive intervals"
        )

    def test_one_zero_delta_below_threshold_not_flagged(self) -> None:
        """One zero-delta interval is below the consecutive threshold — not flagged."""
        # [100, 200, 200] → deltas: 100, 0 → zero_tail=1 < threshold=2
        candidates = [{"id": "uow_partial", "status": "executing", "heartbeat_at": None, "heartbeat_ttl": 300}]
        log_rows = _make_log_rows([100, 200, 200])
        registry = _make_registry(candidates, {"uow_partial": log_rows})
        result = detect_stuck_heartbeat_uows(registry)
        assert result.flagged == 0

    def test_registry_get_heartbeat_log_failure_skipped(self) -> None:
        """When get_heartbeat_log raises, the UoW is skipped without crashing."""
        candidates = [{"id": "uow_fail", "status": "executing", "heartbeat_at": None, "heartbeat_ttl": 300}]
        registry = MagicMock()
        registry.get_executing_uows_with_heartbeats.return_value = candidates
        registry.get_heartbeat_log.side_effect = RuntimeError("DB locked")
        # Must not raise
        result = detect_stuck_heartbeat_uows(registry)
        assert result.checked == 1
        assert result.flagged == 0

    def test_dry_run_does_not_append_observation(self) -> None:
        """dry_run=True: stuck UoW is flagged in return value but no observation appended."""
        candidates = [{"id": "uow_stuck_dry", "status": "executing", "heartbeat_at": None, "heartbeat_ttl": 300}]
        log_rows = _make_log_rows([100, 100, 100])
        registry = _make_registry(candidates, {"uow_stuck_dry": log_rows})

        with patch.object(_MODULE, "_append_observation") as mock_obs:
            result = detect_stuck_heartbeat_uows(registry, dry_run=True)
            assert result.flagged == 1
            mock_obs.assert_not_called()

    def test_get_executing_uows_failure_returns_zero(self) -> None:
        """When get_executing_uows_with_heartbeats raises, return (0, 0) safely."""
        registry = MagicMock()
        registry.get_executing_uows_with_heartbeats.side_effect = RuntimeError("DB error")
        result = detect_stuck_heartbeat_uows(registry)
        assert result.checked == 0
        assert result.flagged == 0

    def test_multiple_uows_counts_correctly(self) -> None:
        """Multiple UoWs: only those at the zero-delta threshold are flagged."""
        candidates = [
            {"id": "uow_ok", "status": "executing", "heartbeat_at": None, "heartbeat_ttl": 300},
            {"id": "uow_stuck", "status": "executing", "heartbeat_at": None, "heartbeat_ttl": 300},
        ]
        log_by_uow = {
            "uow_ok": _make_log_rows([100, 300, 600]),      # positive deltas
            "uow_stuck": _make_log_rows([100, 100, 100]),   # zero deltas
        }
        registry = _make_registry(candidates, log_by_uow)

        with patch.object(_MODULE, "_append_observation"):
            result = detect_stuck_heartbeat_uows(registry)

        assert result.checked == 2
        assert result.flagged == 1


# ---------------------------------------------------------------------------
# Registry integration tests (in-memory SQLite)
# ---------------------------------------------------------------------------

def _make_in_memory_registry():
    """Return a Registry instance backed by a temporary SQLite DB.

    Registry.__init__ applies all migrations automatically — no pre-seeding needed.
    """
    import tempfile
    from src.orchestration.registry import Registry
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    tmp_path.unlink()  # Remove so Registry creates it fresh
    return Registry(tmp_path), tmp_path


class TestRegistryHeartbeatTokenLog:
    """Registry integration: write_heartbeat with token_usage, get_heartbeat_log."""

    def setup_method(self) -> None:
        self.registry, self.db_path = _make_in_memory_registry()
        # Insert a minimal UoW row so write_heartbeat's UPDATE hits a row.
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, status, source, summary, created_at, updated_at)
            VALUES ('uow_test', 'executing', 'test', 'test uow', '2026-04-27T00:00:00+00:00', '2026-04-27T00:00:00+00:00')
            """
        )
        conn.commit()
        conn.close()

    def teardown_method(self) -> None:
        self.db_path.unlink(missing_ok=True)

    def test_write_heartbeat_with_token_usage_inserts_log_row(self) -> None:
        """write_heartbeat(token_usage=500) inserts a row into uow_heartbeat_log."""
        self.registry.write_heartbeat("uow_test", token_usage=500)
        rows = self.registry.get_heartbeat_log("uow_test")
        assert len(rows) == 1
        assert rows[0]["uow_id"] == "uow_test"
        assert rows[0]["token_usage"] == 500

    def test_write_heartbeat_without_token_does_not_insert_log_row(self) -> None:
        """write_heartbeat() with no token_usage inserts nothing into uow_heartbeat_log."""
        self.registry.write_heartbeat("uow_test")
        rows = self.registry.get_heartbeat_log("uow_test")
        assert rows == []

    def test_write_heartbeat_with_token_none_does_not_insert_log_row(self) -> None:
        """write_heartbeat(token_usage=None) inserts nothing into uow_heartbeat_log."""
        self.registry.write_heartbeat("uow_test", token_usage=None)
        rows = self.registry.get_heartbeat_log("uow_test")
        assert rows == []

    def test_multiple_token_heartbeats_accumulate(self) -> None:
        """Multiple write_heartbeat calls accumulate distinct log rows."""
        self.registry.write_heartbeat("uow_test", token_usage=100)
        self.registry.write_heartbeat("uow_test", token_usage=300)
        self.registry.write_heartbeat("uow_test", token_usage=600)
        rows = self.registry.get_heartbeat_log("uow_test")
        tokens = [r["token_usage"] for r in rows]
        assert tokens == [100, 300, 600]

    def test_get_heartbeat_log_filters_null_rows(self) -> None:
        """get_heartbeat_log only returns rows with non-NULL token_usage."""
        self.registry.write_heartbeat("uow_test")           # NULL
        self.registry.write_heartbeat("uow_test", token_usage=200)  # non-NULL
        rows = self.registry.get_heartbeat_log("uow_test")
        assert len(rows) == 1
        assert rows[0]["token_usage"] == 200

    def test_get_executing_uows_with_heartbeats_requires_two_snapshots(self) -> None:
        """get_executing_uows_with_heartbeats only returns UoWs with >= 2 log rows."""
        # One heartbeat — not enough
        self.registry.write_heartbeat("uow_test", token_usage=100)
        result = self.registry.get_executing_uows_with_heartbeats()
        assert result == []

        # Second heartbeat — now qualifies
        self.registry.write_heartbeat("uow_test", token_usage=200)
        result = self.registry.get_executing_uows_with_heartbeats()
        assert len(result) == 1
        assert result[0]["id"] == "uow_test"
