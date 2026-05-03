"""
Tests for Phase 2d backlog alerts (toxicity + starvation governor) in steward-heartbeat.py.

Behavior verified (derived from spec, not from implementation):

- test_load_backlog_state_returns_fresh_when_absent:
  _load_backlog_state() returns {"depths": [], "last_updated": None} when state file
  does not exist.

- test_load_backlog_state_resets_when_stale:
  State with last_updated 1000s ago (> _BACKLOG_STATE_MAX_AGE_SECONDS=900) returns
  fresh state with empty depths.

- test_load_backlog_state_preserves_recent_state:
  State with last_updated 60s ago is returned as-is.

- test_save_and_load_roundtrip:
  _save_backlog_state followed by _load_backlog_state returns the same depths.

- test_no_alert_on_insufficient_history:
  First call (no prior state) — no alert fires (not enough history yet).

- test_toxicity_alert_fires_after_n_consecutive_increases:
  Seed state with depths [1, 2, 3], current depth=4 — toxicity alert fires.

- test_toxicity_alert_does_not_fire_on_plateau:
  Seed state with depths [1, 2, 2], current depth=3 — NOT strictly increasing.

- test_toxicity_alert_does_not_fire_on_decrease:
  Seed state with depths [3, 4, 5], current depth=4 — not strictly increasing.

- test_starvation_alert_fires_after_n_zero_cycles:
  Seed state with depths [0, 0], current depth=0 — starvation alert fires.

- test_starvation_alert_does_not_fire_on_single_zero:
  Seed state with depths [0, 0], current depth=1 — starvation does not fire.

- test_both_alerts_can_fire_independently:
  Toxicity and starvation evaluations are independent.

- test_dry_run_does_not_write_state_or_observation:
  dry_run=True — state not written, observation not called; result still correct.

- test_registry_failure_returns_safe_default:
  registry.list raises — returns BacklogAlertResult(0, False, False).

- test_history_trimmed_to_max_length:
  Seed with 10 depths — saved depths have length <= max(TOXICITY+1, STARVATION).

- test_observation_message_includes_depth_and_growth:
  When toxicity fires, _append_observation includes current depth and growth count.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parents[3]
for _p in [str(_ROOT), str(_ROOT / "src"), str(_ROOT / "scheduled-tasks")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load steward-heartbeat.py as a module (hyphen requires spec_from_file_location).
import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "steward_heartbeat",
    str(_ROOT / "scheduled-tasks" / "steward-heartbeat.py"),
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_PATCH_TARGETS = {
    "src.orchestration.steward": MagicMock(),
    "src.orchestration.github_sync": MagicMock(),
    "src.orchestration.paths": MagicMock(REGISTRY_DB=Path("/tmp/test_registry.db")),
    "startup_sweep": MagicMock(),
    "steward_heartbeat": _MODULE,
}
with patch.dict("sys.modules", _PATCH_TARGETS):
    _SPEC.loader.exec_module(_MODULE)

_load_backlog_state = _MODULE._load_backlog_state
_save_backlog_state = _MODULE._save_backlog_state
_backlog_state_path = _MODULE._backlog_state_path
check_backlog_alerts = _MODULE.check_backlog_alerts
BacklogAlertResult = _MODULE.BacklogAlertResult
TOXICITY_CONSECUTIVE_CYCLES = _MODULE.TOXICITY_CONSECUTIVE_CYCLES
STARVATION_CONSECUTIVE_CYCLES = _MODULE.STARVATION_CONSECUTIVE_CYCLES
STARVATION_MIN_DEPTH = _MODULE.STARVATION_MIN_DEPTH
_BACKLOG_STATE_MAX_AGE_SECONDS = _MODULE._BACKLOG_STATE_MAX_AGE_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(depth: int) -> MagicMock:
    mock = MagicMock()
    mock.list.return_value = [MagicMock()] * depth
    return mock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ---------------------------------------------------------------------------
# Pure state helper tests (no registry)
# ---------------------------------------------------------------------------

class TestLoadBacklogState:

    def test_returns_fresh_when_absent(self, tmp_path):
        """_load_backlog_state returns fresh state when file does not exist."""
        missing = tmp_path / "no-such-file.json"
        with patch.object(_MODULE, "_backlog_state_path", return_value=missing):
            state = _load_backlog_state()
        assert state == {"depths": [], "last_updated": None}

    def test_resets_when_stale(self, tmp_path):
        """State with last_updated 1000s ago (> max age) returns empty depths."""
        state_file = tmp_path / "backlog-alert-state.json"
        stale_state = {
            "depths": [1, 2, 3],
            "last_updated": _iso_ago(1000),
        }
        state_file.write_text(json.dumps(stale_state))
        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            result = _load_backlog_state()
        assert result["depths"] == []

    def test_preserves_recent_state(self, tmp_path):
        """State with last_updated 60s ago is returned unchanged."""
        state_file = tmp_path / "backlog-alert-state.json"
        recent_state = {
            "depths": [1, 2, 3],
            "last_updated": _iso_ago(60),
        }
        state_file.write_text(json.dumps(recent_state))
        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            result = _load_backlog_state()
        assert result["depths"] == [1, 2, 3]


class TestSaveLoadRoundtrip:

    def test_save_and_load_roundtrip(self, tmp_path):
        """_save_backlog_state + _load_backlog_state preserves depths."""
        state_file = tmp_path / "backlog-alert-state.json"
        original = {"depths": [1, 2, 3], "last_updated": _now_iso()}
        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            _save_backlog_state(original)
            restored = _load_backlog_state()
        assert restored["depths"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# check_backlog_alerts behavior tests
# ---------------------------------------------------------------------------

class TestCheckBacklogAlerts:

    def _run(self, registry, state: dict, tmp_path, dry_run: bool = False):
        """Run check_backlog_alerts with a patched state file."""
        state_file = tmp_path / "backlog-alert-state.json"
        if state is not None:
            state_file.write_text(json.dumps(state))

        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            with patch.object(_MODULE, "_append_observation") as mock_obs:
                result = check_backlog_alerts(registry, dry_run=dry_run)
        return result, mock_obs, state_file

    def test_no_alert_on_insufficient_history(self, tmp_path):
        """First invocation with no prior state — no alert fires."""
        registry = _make_registry(depth=3)
        result, mock_obs, _ = self._run(registry, state=None, tmp_path=tmp_path)
        assert result.backlog_depth == 3
        assert result.toxicity_alert is False
        assert result.starvation_alert is False
        mock_obs.assert_not_called()

    def test_toxicity_alert_fires_after_n_consecutive_increases(self, tmp_path):
        """Seed [1,2,3], current=4 → 4 strictly increasing values → toxicity fires."""
        # TOXICITY_CONSECUTIVE_CYCLES=3, need 4 strictly increasing values
        registry = _make_registry(depth=4)
        seed = {"depths": [1, 2, 3], "last_updated": _iso_ago(60)}
        result, mock_obs, _ = self._run(registry, state=seed, tmp_path=tmp_path)
        assert result.toxicity_alert is True
        mock_obs.assert_called_once()
        assert "backlog_toxicity" in mock_obs.call_args[0][0]

    def test_toxicity_alert_does_not_fire_on_plateau(self, tmp_path):
        """Seed [1,2,2], current=3 → [1,2,2,3] has delta 0 at position 2 → not strictly increasing."""
        registry = _make_registry(depth=3)
        seed = {"depths": [1, 2, 2], "last_updated": _iso_ago(60)}
        result, mock_obs, _ = self._run(registry, state=seed, tmp_path=tmp_path)
        assert result.toxicity_alert is False
        mock_obs.assert_not_called()

    def test_toxicity_alert_does_not_fire_on_decrease(self, tmp_path):
        """Seed [3,4,5], current=4 → last 4 are [3,4,5,4] — not strictly increasing."""
        registry = _make_registry(depth=4)
        seed = {"depths": [3, 4, 5], "last_updated": _iso_ago(60)}
        result, mock_obs, _ = self._run(registry, state=seed, tmp_path=tmp_path)
        assert result.toxicity_alert is False
        mock_obs.assert_not_called()

    def test_starvation_alert_fires_after_n_zero_cycles(self, tmp_path):
        """Seed [0,0], current=0 → 3 consecutive zeros → starvation fires."""
        registry = _make_registry(depth=0)
        seed = {"depths": [0, 0], "last_updated": _iso_ago(60)}
        result, mock_obs, _ = self._run(registry, state=seed, tmp_path=tmp_path)
        assert result.starvation_alert is True
        mock_obs.assert_called_once()
        assert "backlog_starvation" in mock_obs.call_args[0][0]

    def test_starvation_alert_does_not_fire_on_single_zero(self, tmp_path):
        """Seed [0,0], current=1 → last 3 are [0,0,1] — not all at threshold."""
        registry = _make_registry(depth=1)
        seed = {"depths": [0, 0], "last_updated": _iso_ago(60)}
        result, mock_obs, _ = self._run(registry, state=seed, tmp_path=tmp_path)
        assert result.starvation_alert is False
        mock_obs.assert_not_called()

    def test_both_alerts_independent(self, tmp_path):
        """Toxicity and starvation are evaluated independently — neither blocks the other."""
        # Test that starvation can fire without toxicity, and vice versa
        # (already proven by other tests); here confirm both can be False independently
        registry = _make_registry(depth=2)
        seed = {"depths": [1, 2], "last_updated": _iso_ago(60)}
        result, _, _ = self._run(registry, state=seed, tmp_path=tmp_path)
        # [1, 2, 2] — not strictly increasing, not starvation
        assert result.toxicity_alert is False
        assert result.starvation_alert is False

    def test_dry_run_does_not_write_state_or_observation(self, tmp_path):
        """dry_run=True: state not saved, no observation; result still reflects alert."""
        registry = _make_registry(depth=4)
        seed = {"depths": [1, 2, 3], "last_updated": _iso_ago(60)}
        state_file = tmp_path / "backlog-alert-state.json"
        state_file.write_text(json.dumps(seed))

        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            with patch.object(_MODULE, "_save_backlog_state") as mock_save:
                with patch.object(_MODULE, "_append_observation") as mock_obs:
                    result = check_backlog_alerts(registry, dry_run=True)

        assert result.toxicity_alert is True
        mock_save.assert_not_called()
        mock_obs.assert_not_called()

    def test_registry_failure_returns_safe_default(self, tmp_path):
        """registry.list raises → returns BacklogAlertResult(0, False, False)."""
        registry = MagicMock()
        registry.list.side_effect = RuntimeError("DB error")
        state_file = tmp_path / "backlog-alert-state.json"
        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            result = check_backlog_alerts(registry)
        assert result == BacklogAlertResult(backlog_depth=0, toxicity_alert=False, starvation_alert=False)

    def test_history_trimmed_to_max_length(self, tmp_path):
        """Seed with 10 depths → saved depths length <= max(TOXICITY+1, STARVATION)."""
        max_expected = max(TOXICITY_CONSECUTIVE_CYCLES + 1, STARVATION_CONSECUTIVE_CYCLES)
        registry = _make_registry(depth=5)
        seed = {"depths": list(range(10)), "last_updated": _iso_ago(60)}
        state_file = tmp_path / "backlog-alert-state.json"
        state_file.write_text(json.dumps(seed))

        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            with patch.object(_MODULE, "_append_observation"):
                check_backlog_alerts(registry, dry_run=False)
            saved = json.loads(state_file.read_text())

        assert len(saved["depths"]) <= max_expected

    def test_observation_message_includes_depth_and_growth(self, tmp_path):
        """When toxicity fires, message includes current_depth and growth."""
        registry = _make_registry(depth=5)
        seed = {"depths": [2, 3, 4], "last_updated": _iso_ago(60)}
        result, mock_obs, _ = self._run(registry, state=seed, tmp_path=tmp_path)
        assert result.toxicity_alert is True
        msg = mock_obs.call_args[0][0]
        assert "current_depth=5" in msg
        # growth = 5 - 2 = 3
        assert "growth=+3" in msg
