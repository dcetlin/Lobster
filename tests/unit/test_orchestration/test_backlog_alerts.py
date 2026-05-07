"""
Tests for the backlog alert governor (Phase 2d of steward-heartbeat.py).

Behavior verified (derived from spec, not implementation):

- test_load_backlog_state_returns_fresh_when_absent:
  _load_backlog_state() returns {"depths": [], "last_updated": None} when
  state file does not exist.

- test_load_backlog_state_resets_when_stale:
  State with last_updated 1000s ago (> _BACKLOG_STATE_MAX_AGE_SECONDS=900)
  returns fresh state with empty depths.

- test_load_backlog_state_preserves_recent_state:
  State with last_updated 60s ago is returned as-is.

- test_save_and_load_roundtrip:
  _save_backlog_state followed by _load_backlog_state returns the same depths.

- test_no_alert_on_insufficient_history:
  registry.list returns 3 UoWs on first call — no alert fires (not enough history).

- test_toxicity_alert_fires_after_n_consecutive_increases:
  Seed depths [1,2,3], current depth=4 — toxicity alert fires; _append_observation
  called with "backlog_toxicity" in the message.

- test_toxicity_alert_does_not_fire_on_plateau:
  Seed depths [1,2,2], current depth=3 — last 4 values [1,2,2,3], second delta
  is 0 — NOT strictly increasing — toxicity does not fire.

- test_toxicity_alert_does_not_fire_on_decrease:
  Seed depths [3,4,5], current depth=4 — not strictly increasing — no toxicity.

- test_starvation_alert_fires_after_n_zero_cycles:
  Seed depths [0,0], current depth=0 — starvation fires (3 consecutive zeros).

- test_starvation_alert_does_not_fire_on_single_zero:
  Seed depths [0,0], current depth=1 — last 3 values [0,0,1] — starvation
  does not fire.

- test_both_alerts_independent:
  Toxicity and starvation are evaluated independently and don't interfere.

- test_dry_run_does_not_write_state_or_observation:
  With toxicity condition met, dry_run=True — _save_backlog_state and
  _append_observation are NOT called; toxicity_alert=True is still returned.

- test_registry_failure_returns_safe_default:
  registry.list raises — returns BacklogAlertResult(0, False, False).

- test_history_trimmed_to_max_length:
  Seed state with 10 depths, run one cycle — saved depths have length <=
  max(TOXICITY_CONSECUTIVE_CYCLES+1, STARVATION_CONSECUTIVE_CYCLES).

- test_observation_message_includes_depth_and_growth:
  When toxicity fires, _append_observation call includes depth and growth count.
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

# Load steward-heartbeat.py as a module (the hyphen requires spec_from_file_location).
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

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _make_registry(depth: int = 0) -> MagicMock:
    mock = MagicMock()
    mock.list.return_value = [MagicMock() for _ in range(depth)]
    return mock


# ---------------------------------------------------------------------------
# Pure state helper tests (no registry)
# ---------------------------------------------------------------------------

class TestLoadBacklogState:

    def test_returns_fresh_when_absent(self, tmp_path) -> None:
        """_load_backlog_state returns fresh state when file does not exist."""
        missing = tmp_path / "no-such-file.json"
        with patch.object(_MODULE, "_backlog_state_path", return_value=missing):
            result = _load_backlog_state()
        assert result == {"depths": [], "last_updated": None}

    def test_resets_when_stale(self, tmp_path) -> None:
        """State older than _BACKLOG_STATE_MAX_AGE_SECONDS is discarded."""
        state_file = tmp_path / "backlog-alert-state.json"
        stale_state = {"depths": [1, 2, 3], "last_updated": _iso_ago(_BACKLOG_STATE_MAX_AGE_SECONDS + 100)}
        state_file.write_text(json.dumps(stale_state))
        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            result = _load_backlog_state()
        assert result["depths"] == []
        assert result["last_updated"] is None

    def test_preserves_recent_state(self, tmp_path) -> None:
        """State written 60s ago is returned unchanged."""
        state_file = tmp_path / "backlog-alert-state.json"
        recent_state = {"depths": [5, 6, 7], "last_updated": _iso_ago(60)}
        state_file.write_text(json.dumps(recent_state))
        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            result = _load_backlog_state()
        assert result["depths"] == [5, 6, 7]

    def test_save_and_load_roundtrip(self, tmp_path) -> None:
        """_save_backlog_state followed by _load_backlog_state returns identical depths."""
        state_file = tmp_path / "backlog-alert-state.json"
        original = {"depths": [1, 2, 3], "last_updated": _now_iso()}
        with patch.object(_MODULE, "_backlog_state_path", return_value=state_file):
            _save_backlog_state(original)
            result = _load_backlog_state()
        assert result["depths"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# check_backlog_alerts behavior tests
# ---------------------------------------------------------------------------

class TestCheckBacklogAlerts:

    def _run(self, registry, seed_depths: list[int], current_depth: int, dry_run: bool = False):
        """Helper: seed state, set registry depth, run check_backlog_alerts."""
        state = {"depths": seed_depths[:], "last_updated": _now_iso() if seed_depths else None}
        registry.list.return_value = [MagicMock() for _ in range(current_depth)]

        with patch.object(_MODULE, "_load_backlog_state", return_value=state), \
             patch.object(_MODULE, "_save_backlog_state") as mock_save, \
             patch.object(_MODULE, "_append_observation") as mock_obs:
            result = check_backlog_alerts(registry, dry_run=dry_run)
            return result, mock_save, mock_obs

    def test_no_alert_on_insufficient_history(self) -> None:
        """First invocation: not enough history to fire any alert."""
        registry = _make_registry()
        result, _, _ = self._run(registry, seed_depths=[], current_depth=3)
        assert result.backlog_depth == 3
        assert result.toxicity_alert is False
        assert result.starvation_alert is False

    def test_toxicity_alert_fires_after_n_consecutive_increases(self) -> None:
        """Seed [1,2,3], current=4 → strictly increasing over 4 values → toxicity fires."""
        registry = _make_registry()
        # After append: [1,2,3,4] — 4 values all strictly increasing
        result, _, mock_obs = self._run(registry, seed_depths=[1, 2, 3], current_depth=4)
        assert result.toxicity_alert is True
        mock_obs.assert_called_once()
        assert "backlog_toxicity" in mock_obs.call_args[0][0]

    def test_toxicity_alert_does_not_fire_on_plateau(self) -> None:
        """Seed [1,2,2], current=3 → [1,2,2,3]: delta at index 2→3 is 0 → not strictly increasing."""
        registry = _make_registry()
        result, _, mock_obs = self._run(registry, seed_depths=[1, 2, 2], current_depth=3)
        assert result.toxicity_alert is False
        mock_obs.assert_not_called()

    def test_toxicity_alert_does_not_fire_on_decrease(self) -> None:
        """Seed [3,4,5], current=4 → not strictly increasing → no toxicity."""
        registry = _make_registry()
        result, _, mock_obs = self._run(registry, seed_depths=[3, 4, 5], current_depth=4)
        assert result.toxicity_alert is False

    def test_starvation_alert_fires_after_n_zero_cycles(self) -> None:
        """Seed [0,0], current=0 → 3 consecutive zeros → starvation fires."""
        registry = _make_registry()
        result, _, mock_obs = self._run(registry, seed_depths=[0, 0], current_depth=0)
        assert result.starvation_alert is True
        assert "backlog_starvation" in mock_obs.call_args[0][0]

    def test_starvation_alert_does_not_fire_on_single_zero(self) -> None:
        """Seed [0,0], current=1 → last 3 are [0,0,1] → starvation does not fire."""
        registry = _make_registry()
        result, _, mock_obs = self._run(registry, seed_depths=[0, 0], current_depth=1)
        assert result.starvation_alert is False

    def test_both_alerts_independent(self) -> None:
        """Toxicity and starvation detection are independent of each other."""
        registry = _make_registry()
        # Toxicity condition: strictly increasing
        result_tox, _, _ = self._run(registry, seed_depths=[1, 2, 3], current_depth=4)
        assert result_tox.toxicity_alert is True
        assert result_tox.starvation_alert is False  # depth=4, not zero

        # Starvation condition
        result_starv, _, _ = self._run(registry, seed_depths=[0, 0], current_depth=0)
        assert result_starv.starvation_alert is True
        assert result_starv.toxicity_alert is False  # [0,0,0] not strictly increasing

    def test_dry_run_does_not_write_state_or_observation(self) -> None:
        """dry_run=True: toxicity still detected but no state write or observation."""
        registry = _make_registry()
        result, mock_save, mock_obs = self._run(
            registry, seed_depths=[1, 2, 3], current_depth=4, dry_run=True
        )
        assert result.toxicity_alert is True
        mock_save.assert_not_called()
        mock_obs.assert_not_called()

    def test_registry_failure_returns_safe_default(self) -> None:
        """When registry.list raises, returns BacklogAlertResult(0, False, False)."""
        registry = MagicMock()
        registry.list.side_effect = RuntimeError("DB locked")
        result = check_backlog_alerts(registry)
        assert result == BacklogAlertResult(backlog_depth=0, toxicity_alert=False, starvation_alert=False)

    def test_history_trimmed_to_max_length(self) -> None:
        """After one cycle, saved depths length <= max(TOXICITY_CONSECUTIVE_CYCLES+1, STARVATION_CONSECUTIVE_CYCLES)."""
        registry = _make_registry()
        seed = list(range(10))  # 10 depths
        expected_max = max(TOXICITY_CONSECUTIVE_CYCLES + 1, STARVATION_CONSECUTIVE_CYCLES)

        saved_state = {}

        def capture_save(state: dict) -> None:
            saved_state.update(state)

        state_in = {"depths": seed[:], "last_updated": _now_iso()}
        registry.list.return_value = [MagicMock()]  # depth=1

        with patch.object(_MODULE, "_load_backlog_state", return_value=state_in), \
             patch.object(_MODULE, "_save_backlog_state", side_effect=capture_save), \
             patch.object(_MODULE, "_append_observation"):
            check_backlog_alerts(registry)

        assert len(saved_state["depths"]) <= expected_max

    def test_observation_message_includes_depth_and_growth(self) -> None:
        """When toxicity fires, _append_observation message includes current_depth and growth."""
        registry = _make_registry()
        # Seed [1,2,3], current=4 → growth = 4-1 = 3
        state = {"depths": [1, 2, 3], "last_updated": _now_iso()}
        registry.list.return_value = [MagicMock() for _ in range(4)]

        with patch.object(_MODULE, "_load_backlog_state", return_value=state), \
             patch.object(_MODULE, "_save_backlog_state"), \
             patch.object(_MODULE, "_append_observation") as mock_obs:
            result = check_backlog_alerts(registry)

        assert result.toxicity_alert is True
        msg = mock_obs.call_args[0][0]
        assert "current_depth=4" in msg
        assert "growth=+3" in msg
