"""
Tests for steward heartbeat prescriptions-per-cycle metric and queue-depth alert (#618).

Covers:
1. prescriptions_this_cycle is included in task output
2. Alert fires to observations.log when prescriptions_this_cycle > HIGH_PRESCRIPTION_THRESHOLD
3. No alert when prescriptions_this_cycle <= HIGH_PRESCRIPTION_THRESHOLD
4. Queue-depth alert fires when execution_enabled=false and eligible UoWs > 0
5. No queue-depth alert when execution_enabled=false and queue is empty
6. No queue-depth alert when execution_enabled=true (regardless of queue depth)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_STEWARD_PATH = _REPO_ROOT / "scheduled-tasks" / "steward-heartbeat.py"


def _load_steward_heartbeat():
    """Load steward-heartbeat.py via importlib."""
    spec = importlib.util.spec_from_file_location("steward_heartbeat_prescriptions_test", _STEWARD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["steward_heartbeat_prescriptions_test"] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _clean_argv():
    """Reset sys.argv to just the script name so argparse does not consume pytest args."""
    original = sys.argv
    sys.argv = ["steward-heartbeat"]
    try:
        yield
    finally:
        sys.argv = original


def _make_mock_registry(ready_for_steward_uows=None):
    """Build a mock registry with configurable ready-for-steward UoW list."""
    registry = Mock()
    registry.list_active_for_observation.return_value = []
    registry.list.return_value = ready_for_steward_uows if ready_for_steward_uows is not None else []
    return registry


def _make_steward_result(prescribed=0):
    from src.orchestration.steward import CycleResult
    return CycleResult(
        evaluated=prescribed,
        prescribed=prescribed,
        done=0,
        surfaced=0,
        skipped=0,
        race_skipped=0,
        wait_for_trace=0,
        considered_ids=(),
    )


# ---------------------------------------------------------------------------
# Load the module once at module level for constant access
# ---------------------------------------------------------------------------

_steward_hb = _load_steward_heartbeat()


# ---------------------------------------------------------------------------
# Tests: task output includes prescriptions_this_cycle
# ---------------------------------------------------------------------------

class TestPrescriptionsTaskOutput:
    """Task output written at end of cycle includes prescriptions_this_cycle."""

    def test_task_output_includes_prescription_count(self, tmp_path):
        """write_task_output is called with prescriptions_this_cycle in the output string."""
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()
        prescribed_count = 3

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True), \
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
                          return_value=_make_steward_result(prescribed=prescribed_count)), \
             patch.object(steward_heartbeat, "run_post_completion_sync",
                          return_value=Mock(synced=0, skipped_no_url=0, failed=0, errors=[])), \
             patch.object(steward_heartbeat, "_write_task_output") as mock_write_output:

            with _clean_argv():
                result = steward_heartbeat.main()

        assert result == 0
        mock_write_output.assert_called_once()
        output_str, status, _ = mock_write_output.call_args[0]
        assert f"prescriptions_this_cycle={prescribed_count}" in output_str
        assert status == "success"

    def test_task_output_zero_prescriptions(self, tmp_path):
        """Task output is written even when prescribed=0."""
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True), \
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
                          return_value=_make_steward_result(prescribed=0)), \
             patch.object(steward_heartbeat, "run_post_completion_sync",
                          return_value=Mock(synced=0, skipped_no_url=0, failed=0, errors=[])), \
             patch.object(steward_heartbeat, "_write_task_output") as mock_write_output:

            with _clean_argv():
                steward_heartbeat.main()

        mock_write_output.assert_called_once()
        output_str, _, _ = mock_write_output.call_args[0]
        assert "prescriptions_this_cycle=0" in output_str


# ---------------------------------------------------------------------------
# Tests: high prescription count alert
# ---------------------------------------------------------------------------

class TestHighPrescriptionAlert:
    """Alert fires to observations.log when prescription count exceeds threshold."""

    def test_alert_fires_when_count_exceeds_threshold(self, tmp_path):
        """_append_observation called when prescribed > HIGH_PRESCRIPTION_THRESHOLD."""
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()
        prescribed_count = _steward_hb.HIGH_PRESCRIPTION_THRESHOLD + 1  # 11

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True), \
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
                          return_value=_make_steward_result(prescribed=prescribed_count)), \
             patch.object(steward_heartbeat, "run_post_completion_sync",
                          return_value=Mock(synced=0, skipped_no_url=0, failed=0, errors=[])), \
             patch.object(steward_heartbeat, "_write_task_output"), \
             patch.object(steward_heartbeat, "_append_observation") as mock_append:

            with _clean_argv():
                steward_heartbeat.main()

        # At least one call must contain the high prescription count warning
        alert_calls = [
            c for c in mock_append.call_args_list
            if "high prescription count" in c[0][0]
        ]
        assert len(alert_calls) == 1, (
            f"Expected exactly 1 high-prescription-count alert, got {len(alert_calls)}"
        )
        alert_msg = alert_calls[0][0][0]
        assert str(prescribed_count) in alert_msg
        assert "possible queue buildup" in alert_msg

    def test_no_alert_when_count_equals_threshold(self, tmp_path):
        """No alert when prescribed == HIGH_PRESCRIPTION_THRESHOLD (boundary: strictly greater than)."""
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True), \
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
                          return_value=_make_steward_result(prescribed=_steward_hb.HIGH_PRESCRIPTION_THRESHOLD)), \
             patch.object(steward_heartbeat, "run_post_completion_sync",
                          return_value=Mock(synced=0, skipped_no_url=0, failed=0, errors=[])), \
             patch.object(steward_heartbeat, "_write_task_output"), \
             patch.object(steward_heartbeat, "_append_observation") as mock_append:

            with _clean_argv():
                steward_heartbeat.main()

        alert_calls = [
            c for c in mock_append.call_args_list
            if "high prescription count" in c[0][0]
        ]
        assert len(alert_calls) == 0, (
            "No alert expected when count equals threshold (strictly greater-than check)"
        )

    def test_no_alert_when_count_below_threshold(self, tmp_path):
        """No alert when prescribed < HIGH_PRESCRIPTION_THRESHOLD."""
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True), \
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
                          return_value=_make_steward_result(prescribed=5)), \
             patch.object(steward_heartbeat, "run_post_completion_sync",
                          return_value=Mock(synced=0, skipped_no_url=0, failed=0, errors=[])), \
             patch.object(steward_heartbeat, "_write_task_output"), \
             patch.object(steward_heartbeat, "_append_observation") as mock_append:

            with _clean_argv():
                steward_heartbeat.main()

        alert_calls = [
            c for c in mock_append.call_args_list
            if "high prescription count" in c[0][0]
        ]
        assert len(alert_calls) == 0


# ---------------------------------------------------------------------------
# Tests: queue-depth alert when execution disabled
# ---------------------------------------------------------------------------

class TestQueueDepthAlert:
    """Alert fires when execution_enabled=false and eligible UoWs are queued."""

    def test_alert_fires_when_queue_nonempty_and_execution_disabled(self, tmp_path):
        """When execution_enabled=false and ready-for-steward > 0, alert is appended."""
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()

        # Simulate 3 eligible UoWs in the queue
        eligible_uows = [Mock(), Mock(), Mock()]
        registry = _make_mock_registry(ready_for_steward_uows=eligible_uows)

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=False), \
             patch.object(steward_heartbeat, "_default_db_path", return_value=db_path), \
             patch("src.orchestration.registry.Registry", return_value=registry), \
             patch.object(steward_heartbeat, "run_stale_agent_cleanup",
                          return_value={"evaluated": 0, "cleaned": 0, "skipped": 0, "running_total": 0}), \
             patch.object(steward_heartbeat, "run_startup_sweep",
                          return_value=Mock(active_swept=0, executor_orphans_swept=0,
                                           diagnosing_swept=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "run_observation_loop",
                          return_value=Mock(checked=0, stalled=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "_append_observation") as mock_append:

            with _clean_argv():
                result = steward_heartbeat.main()

        assert result == 0
        queue_alerts = [
            c for c in mock_append.call_args_list
            if "execution_enabled=false" in c[0][0] and "queue will not drain" in c[0][0]
        ]
        assert len(queue_alerts) == 1, (
            f"Expected exactly 1 queue-depth alert, got {len(queue_alerts)}"
        )
        alert_msg = queue_alerts[0][0][0]
        assert "3" in alert_msg  # UoW count

    def test_no_alert_when_queue_empty_and_execution_disabled(self, tmp_path):
        """When execution_enabled=false and queue is empty, no queue-depth alert fires."""
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()

        registry = _make_mock_registry(ready_for_steward_uows=[])

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=False), \
             patch.object(steward_heartbeat, "_default_db_path", return_value=db_path), \
             patch("src.orchestration.registry.Registry", return_value=registry), \
             patch.object(steward_heartbeat, "run_stale_agent_cleanup",
                          return_value={"evaluated": 0, "cleaned": 0, "skipped": 0, "running_total": 0}), \
             patch.object(steward_heartbeat, "run_startup_sweep",
                          return_value=Mock(active_swept=0, executor_orphans_swept=0,
                                           diagnosing_swept=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "run_observation_loop",
                          return_value=Mock(checked=0, stalled=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "_append_observation") as mock_append:

            with _clean_argv():
                result = steward_heartbeat.main()

        assert result == 0
        queue_alerts = [
            c for c in mock_append.call_args_list
            if "execution_enabled=false" in c[0][0]
        ]
        assert len(queue_alerts) == 0

    def test_no_queue_alert_when_execution_enabled(self, tmp_path):
        """When execution_enabled=true, no queue-depth alert fires (WOS is running normally)."""
        steward_heartbeat = _load_steward_heartbeat()
        db_path = tmp_path / "registry.db"
        db_path.touch()

        # Even with a large queue, no alert when execution is on
        eligible_uows = [Mock() for _ in range(5)]
        registry = _make_mock_registry(ready_for_steward_uows=eligible_uows)

        with patch.object(steward_heartbeat, "_is_job_enabled", return_value=True), \
             patch.object(steward_heartbeat, "is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True), \
             patch.object(steward_heartbeat, "_default_db_path", return_value=db_path), \
             patch("src.orchestration.registry.Registry", return_value=registry), \
             patch.object(steward_heartbeat, "run_stale_agent_cleanup",
                          return_value={"evaluated": 0, "cleaned": 0, "skipped": 0, "running_total": 0}), \
             patch.object(steward_heartbeat, "run_startup_sweep",
                          return_value=Mock(active_swept=0, executor_orphans_swept=0,
                                           diagnosing_swept=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "run_observation_loop",
                          return_value=Mock(checked=0, stalled=0, skipped_dry_run=0)), \
             patch.object(steward_heartbeat, "run_steward_cycle",
                          return_value=_make_steward_result(prescribed=0)), \
             patch.object(steward_heartbeat, "run_post_completion_sync",
                          return_value=Mock(synced=0, skipped_no_url=0, failed=0, errors=[])), \
             patch.object(steward_heartbeat, "_write_task_output"), \
             patch.object(steward_heartbeat, "_append_observation") as mock_append:

            with _clean_argv():
                result = steward_heartbeat.main()

        assert result == 0
        queue_alerts = [
            c for c in mock_append.call_args_list
            if "execution_enabled=false" in c[0][0]
        ]
        assert len(queue_alerts) == 0


# ---------------------------------------------------------------------------
# Tests: _append_observation helper writes to correct path
# ---------------------------------------------------------------------------

class TestAppendObservationHelper:
    """_append_observation writes to the expected observations.log path."""

    def test_appends_message_to_observations_log(self, tmp_path):
        """_append_observation creates and appends to observations.log under LOBSTER_WORKSPACE."""
        steward_heartbeat = _load_steward_heartbeat()
        import os
        orig_env = os.environ.get("LOBSTER_WORKSPACE")
        try:
            os.environ["LOBSTER_WORKSPACE"] = str(tmp_path)
            steward_heartbeat._append_observation("test observation message")
        finally:
            if orig_env is None:
                os.environ.pop("LOBSTER_WORKSPACE", None)
            else:
                os.environ["LOBSTER_WORKSPACE"] = orig_env

        obs_log = tmp_path / "logs" / "observations.log"
        assert obs_log.exists(), "observations.log must be created"
        content = obs_log.read_text()
        assert "test observation message" in content

    def test_append_is_additive(self, tmp_path):
        """Multiple _append_observation calls each add a line — no overwrite."""
        steward_heartbeat = _load_steward_heartbeat()
        import os
        orig_env = os.environ.get("LOBSTER_WORKSPACE")
        try:
            os.environ["LOBSTER_WORKSPACE"] = str(tmp_path)
            steward_heartbeat._append_observation("first message")
            steward_heartbeat._append_observation("second message")
        finally:
            if orig_env is None:
                os.environ.pop("LOBSTER_WORKSPACE", None)
            else:
                os.environ["LOBSTER_WORKSPACE"] = orig_env

        obs_log = tmp_path / "logs" / "observations.log"
        content = obs_log.read_text()
        assert "first message" in content
        assert "second message" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
