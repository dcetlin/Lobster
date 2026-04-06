"""
Tests for steward heartbeat execution_enabled gate.

Covers:
- When execution_enabled=false, steward skips LLM prescription (Phase 3)
- When execution_enabled=false, phases 0-2 (stale agent cleanup, startup sweep,
  observation loop) still run
- When execution_enabled=true, steward proceeds to Phase 3 normally
- When wos-config.json is absent, defaults to false (safe default) — steward skips
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, call
from contextlib import contextmanager


@contextmanager
def _clean_argv():
    """Reset sys.argv to just the script name so argparse does not consume pytest args."""
    original = sys.argv
    sys.argv = ["steward-heartbeat"]
    try:
        yield
    finally:
        sys.argv = original

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import importlib.util

_STEWARD_PATH = _REPO_ROOT / "scheduled-tasks" / "steward-heartbeat.py"


def _load_steward_heartbeat():
    """Load steward-heartbeat.py via importlib (same approach as test_agent_cleanup.py)."""
    spec = importlib.util.spec_from_file_location("steward_heartbeat_gate_test", _STEWARD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["steward_heartbeat_gate_test"] = module
    spec.loader.exec_module(module)
    return module


# Shared mock registry returned by Registry() constructor
def _make_mock_registry():
    registry = Mock()
    registry.list_active_for_observation.return_value = []
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

EXECUTION_DISABLED = False
EXECUTION_ENABLED = True


class TestStewardExecutionGate:
    """Steward heartbeat respects wos-config.json execution_enabled flag."""

    def test_skips_llm_prescription_when_execution_disabled(self, tmp_path):
        """
        When execution_enabled=false, run_steward_cycle must NOT be called.
        Phases 0-2 (cleanup, startup sweep, observation loop) still run.
        """
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()  # make the file exist so the path check passes

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled",
                   return_value=EXECUTION_DISABLED), \
             patch.object(steward_heartbeat, "_default_db_path", return_value=db_path), \
             patch("src.orchestration.registry.Registry", return_value=_make_mock_registry()), \
             patch.object(steward_heartbeat, "run_stale_agent_cleanup",
                          return_value={"evaluated": 0, "cleaned": 0, "skipped": 0, "running_total": 0}), \
             patch.object(steward_heartbeat, "run_startup_sweep",
                          return_value=Mock(active_swept=0, executor_orphans_swept=0,
                                           diagnosing_swept=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "run_observation_loop",
                          return_value=Mock(checked=0, stalled=0, skipped_dry_run=0)) as mock_obs, \
             patch.object(steward_heartbeat, "run_steward_cycle") as mock_steward_cycle, \
             patch.object(steward_heartbeat, "run_post_completion_sync") as mock_sync:

            with _clean_argv():
                result = steward_heartbeat.main()

        assert result == 0
        mock_steward_cycle.assert_not_called()
        mock_sync.assert_not_called()
        # Phases 0-2 must still run
        mock_obs.assert_called_once()

    def test_runs_llm_prescription_when_execution_enabled(self, tmp_path):
        """
        When execution_enabled=true, run_steward_cycle is called normally.
        """
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()

        mock_steward_result = {
            "evaluated": 1, "prescribed": 1, "done": 0,
            "surfaced": 0, "skipped": 0, "race_skipped": 0,
        }

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled",
                   return_value=EXECUTION_ENABLED), \
             patch.object(steward_heartbeat, "_default_db_path", return_value=db_path), \
             patch("src.orchestration.registry.Registry", return_value=_make_mock_registry()), \
             patch.object(steward_heartbeat, "run_stale_agent_cleanup",
                          return_value={"evaluated": 0, "cleaned": 0, "skipped": 0, "running_total": 0}), \
             patch.object(steward_heartbeat, "run_startup_sweep",
                          return_value=Mock(active_swept=0, executor_orphans_swept=0,
                                           diagnosing_swept=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "run_observation_loop",
                          return_value=Mock(checked=0, stalled=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "run_steward_cycle",
                          return_value=mock_steward_result) as mock_steward_cycle, \
             patch.object(steward_heartbeat, "run_post_completion_sync",
                          return_value=Mock(synced=0, skipped_no_url=0, failed=0, errors=[])):

            with _clean_argv():
                result = steward_heartbeat.main()

        assert result == 0
        mock_steward_cycle.assert_called_once()

    def test_phases_0_1_2_run_when_execution_disabled(self, tmp_path):
        """
        When execution_enabled=false, phases 0 (stale agent cleanup), 1 (startup sweep),
        and 2 (observation loop) must still run — they are cheap and maintain state
        consistency even while execution is paused.
        """
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled",
                   return_value=EXECUTION_DISABLED), \
             patch.object(steward_heartbeat, "_default_db_path", return_value=db_path), \
             patch("src.orchestration.registry.Registry", return_value=_make_mock_registry()), \
             patch.object(steward_heartbeat, "run_stale_agent_cleanup",
                          return_value={"evaluated": 0, "cleaned": 0, "skipped": 0, "running_total": 0}) as mock_cleanup, \
             patch.object(steward_heartbeat, "run_startup_sweep",
                          return_value=Mock(active_swept=0, executor_orphans_swept=0,
                                           diagnosing_swept=0, skipped_dry_run=0)) as mock_sweep, \
             patch.object(steward_heartbeat, "run_observation_loop",
                          return_value=Mock(checked=0, stalled=0, skipped_dry_run=0)) as mock_obs, \
             patch.object(steward_heartbeat, "run_steward_cycle") as mock_steward_cycle:

            with _clean_argv():
                steward_heartbeat.main()

        mock_cleanup.assert_called_once()
        mock_sweep.assert_called_once()
        mock_obs.assert_called_once()
        mock_steward_cycle.assert_not_called()

    def test_jobs_json_disabled_gate_fires_before_execution_gate(self, tmp_path):
        """
        When disabled in jobs.json, steward exits early — before even checking
        execution_enabled. The jobs.json gate is the outermost guard.
        """
        steward_heartbeat = _load_steward_heartbeat()

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled") as mock_exec_check, \
             patch.object(steward_heartbeat, "run_stale_agent_cleanup") as mock_cleanup, \
             patch.object(steward_heartbeat, "run_steward_cycle") as mock_steward_cycle:

            with _clean_argv():
                result = steward_heartbeat.main()

        assert result == 0
        mock_cleanup.assert_not_called()
        mock_steward_cycle.assert_not_called()
        # is_execution_enabled should not even be called when jobs.json says disabled
        mock_exec_check.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
