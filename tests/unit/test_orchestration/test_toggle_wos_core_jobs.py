"""
Tests for toggle_wos_core_jobs() and the updated handle_wos_start/stop handlers.

Behavior under test:
- toggle_wos_core_jobs(enabled=True) sets enabled=True on every wos_core job in jobs.json
  and sets execution_enabled=True in wos-config.json.
- toggle_wos_core_jobs(enabled=False) does the opposite.
- Jobs without wos_core:true are left untouched.
- Jobs in _WOS_CORE_JOBS but absent from jobs.json appear in not_found (not toggled).
- handle_wos_start returns an idempotent notice when execution is already enabled.
- handle_wos_start calls toggle_wos_core_jobs(True) when starting from stopped state.
- handle_wos_stop returns an idempotent notice when execution is already disabled.
- handle_wos_stop calls toggle_wos_core_jobs(False) when stopping from running state.
- COMMAND_HELP mentions all 14 WOS-core jobs in its wos start/stop description.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WOS_CORE_JOB_COUNT = 14  # canonical count from the assessment doc


def _make_jobs_json(jobs: dict) -> dict:
    """Wrap a jobs dict in the standard jobs.json envelope."""
    return {"jobs": jobs}


def _wos_core_entry(name: str, enabled: bool) -> dict:
    return {"name": name, "enabled": enabled, "wos_core": True}


def _non_core_entry(name: str, enabled: bool) -> dict:
    return {"name": name, "enabled": enabled}


# ---------------------------------------------------------------------------
# toggle_wos_core_jobs — unit tests with file I/O mocked
# ---------------------------------------------------------------------------

class TestToggleWosCoreJobs:
    """toggle_wos_core_jobs is importable from dispatcher_handlers."""

    def _import(self):
        from src.orchestration.dispatcher_handlers import toggle_wos_core_jobs
        return toggle_wos_core_jobs

    def _run(self, enabled: bool, jobs: dict, existing_wos_config: dict | None = None):
        """Run toggle_wos_core_jobs with mocked file I/O.

        Returns (result, written_jobs, written_config) where written_* are the
        dicts that would have been written to disk.
        """
        jobs_data = _make_jobs_json(jobs)
        wos_config = existing_wos_config or {"execution_enabled": not enabled}

        written = {}

        def fake_read_jobs():
            return jobs_data

        def fake_write_jobs(data):
            written["jobs"] = data

        def fake_read_config():
            return wos_config

        def fake_write_config(cfg):
            written["config"] = cfg

        toggle = self._import()
        with (
            patch("src.orchestration.dispatcher_handlers._read_jobs_json", side_effect=fake_read_jobs),
            patch("src.orchestration.dispatcher_handlers._write_jobs_json", side_effect=fake_write_jobs),
            patch("src.orchestration.dispatcher_handlers.read_wos_config", side_effect=fake_read_config),
            patch("src.orchestration.dispatcher_handlers._write_wos_config", side_effect=fake_write_config),
        ):
            result = toggle(enabled=enabled)

        return result, written.get("jobs"), written.get("config")

    def test_enable_sets_wos_core_jobs_enabled(self):
        """Enabling should flip all wos_core jobs to enabled=True."""
        jobs = {
            "executor-heartbeat": _wos_core_entry("executor-heartbeat", False),
            "steward-heartbeat": _wos_core_entry("steward-heartbeat", False),
        }
        result, written_jobs, _ = self._run(enabled=True, jobs=jobs)
        assert written_jobs["jobs"]["executor-heartbeat"]["enabled"] is True
        assert written_jobs["jobs"]["steward-heartbeat"]["enabled"] is True

    def test_disable_sets_wos_core_jobs_disabled(self):
        """Disabling should flip all wos_core jobs to enabled=False."""
        jobs = {
            "executor-heartbeat": _wos_core_entry("executor-heartbeat", True),
            "steward-heartbeat": _wos_core_entry("steward-heartbeat", True),
        }
        result, written_jobs, _ = self._run(enabled=False, jobs=jobs)
        assert written_jobs["jobs"]["executor-heartbeat"]["enabled"] is False
        assert written_jobs["jobs"]["steward-heartbeat"]["enabled"] is False

    def test_non_core_jobs_are_untouched(self):
        """Jobs without wos_core:true must not be modified."""
        jobs = {
            "executor-heartbeat": _wos_core_entry("executor-heartbeat", False),
            "morning-briefing": _non_core_entry("morning-briefing", True),
        }
        _, written_jobs, _ = self._run(enabled=True, jobs=jobs)
        # morning-briefing has no wos_core flag — must remain enabled=True
        assert written_jobs["jobs"]["morning-briefing"]["enabled"] is True
        assert "wos_core" not in written_jobs["jobs"]["morning-briefing"]

    def test_execution_enabled_written_to_config_on_start(self):
        """Enabling should write execution_enabled=True to wos-config.json."""
        jobs = {"executor-heartbeat": _wos_core_entry("executor-heartbeat", False)}
        _, _, written_config = self._run(enabled=True, jobs=jobs)
        assert written_config["execution_enabled"] is True

    def test_execution_enabled_written_to_config_on_stop(self):
        """Disabling should write execution_enabled=False to wos-config.json."""
        jobs = {"executor-heartbeat": _wos_core_entry("executor-heartbeat", True)}
        _, _, written_config = self._run(
            enabled=False, jobs=jobs, existing_wos_config={"execution_enabled": True}
        )
        assert written_config["execution_enabled"] is False

    def test_toggled_list_contains_only_wos_core_jobs(self):
        """Result toggled list should only include jobs with wos_core:true."""
        jobs = {
            "executor-heartbeat": _wos_core_entry("executor-heartbeat", False),
            "morning-briefing": _non_core_entry("morning-briefing", True),
        }
        result, _, _ = self._run(enabled=True, jobs=jobs)
        assert "executor-heartbeat" in result["toggled"]
        assert "morning-briefing" not in result["toggled"]

    def test_not_found_contains_wos_core_jobs_absent_from_jobs_json(self):
        """WOS-core jobs not present in jobs.json should appear in not_found."""
        # Only one of the 14 WOS-core jobs is in jobs.json
        jobs = {"executor-heartbeat": _wos_core_entry("executor-heartbeat", False)}
        result, _, _ = self._run(enabled=True, jobs=jobs)
        # All the remaining 13 WOS-core jobs are absent and must be in not_found
        assert "steward-heartbeat" in result["not_found"]
        assert "wos-health-monitor" in result["not_found"]
        assert "executor-heartbeat" not in result["not_found"]

    def test_new_state_is_enabled_when_enabling(self):
        result, _, _ = self._run(enabled=True, jobs={})
        assert result["new_state"] == "enabled"

    def test_new_state_is_disabled_when_disabling(self):
        result, _, _ = self._run(
            enabled=False, jobs={}, existing_wos_config={"execution_enabled": True}
        )
        assert result["new_state"] == "disabled"

    def test_empty_jobs_json_produces_empty_toggled_list(self):
        """With no jobs in jobs.json all WOS-core jobs end up in not_found."""
        result, _, _ = self._run(enabled=True, jobs={})
        assert result["toggled"] == []
        assert len(result["not_found"]) == WOS_CORE_JOB_COUNT

    def test_idempotent_enable_already_enabled_jobs(self):
        """Re-enabling already-enabled wos_core jobs writes enabled=True again (no-op effect)."""
        jobs = {"executor-heartbeat": _wos_core_entry("executor-heartbeat", True)}
        result, written_jobs, _ = self._run(enabled=True, jobs=jobs)
        assert written_jobs["jobs"]["executor-heartbeat"]["enabled"] is True
        assert "executor-heartbeat" in result["toggled"]

    def test_wos_core_false_field_is_not_toggled(self):
        """A job with wos_core:false must not be included in the toggle."""
        jobs = {
            "executor-heartbeat": {**_wos_core_entry("executor-heartbeat", False), "wos_core": False},
        }
        result, written_jobs, _ = self._run(enabled=True, jobs=jobs)
        # executor-heartbeat has wos_core:false, so it should not be enabled
        assert written_jobs["jobs"]["executor-heartbeat"]["enabled"] is False
        assert "executor-heartbeat" not in result["toggled"]


# ---------------------------------------------------------------------------
# handle_wos_start — idempotency and delegation
# ---------------------------------------------------------------------------

class TestHandleWosStart:
    def _import(self):
        from src.orchestration.dispatcher_handlers import handle_wos_start
        return handle_wos_start

    def _make_jobs_json_all_core_enabled(self) -> dict:
        """Return a jobs.json with all wos_core jobs enabled."""
        return {
            "jobs": {
                "executor-heartbeat": {"wos_core": True, "enabled": True},
                "steward-heartbeat": {"wos_core": True, "enabled": True},
            }
        }

    def _make_jobs_json_steward_disabled(self) -> dict:
        """Return a jobs.json where steward-heartbeat is disabled but execution_enabled=True."""
        return {
            "jobs": {
                "executor-heartbeat": {"wos_core": True, "enabled": True},
                "steward-heartbeat": {"wos_core": True, "enabled": False},
            }
        }

    def test_idempotent_when_already_started_and_all_core_jobs_enabled(self):
        """When execution_enabled is True and all wos_core jobs are enabled, return a notice."""
        handle_wos_start = self._import()
        jobs_data = self._make_jobs_json_all_core_enabled()
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers._read_jobs_json",
                  return_value=jobs_data),
        ):
            result = handle_wos_start()
        assert "already" in result.lower()
        assert "running" in result.lower()

    def test_partial_recovery_when_execution_enabled_but_core_jobs_disabled(self):
        """When execution_enabled=True but some wos_core jobs are disabled, re-enable them."""
        handle_wos_start = self._import()
        jobs_data = self._make_jobs_json_steward_disabled()
        written = {}

        def fake_write_jobs(data):
            written["jobs"] = data

        def fake_write_config(cfg):
            written["config"] = cfg

        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers._read_jobs_json",
                  return_value=jobs_data),
            patch("src.orchestration.dispatcher_handlers._write_jobs_json",
                  side_effect=fake_write_jobs),
            patch("src.orchestration.dispatcher_handlers._write_wos_config",
                  side_effect=fake_write_config),
        ):
            result = handle_wos_start()

        # steward-heartbeat must be re-enabled
        assert written["jobs"]["jobs"]["steward-heartbeat"]["enabled"] is True
        # executor-heartbeat must remain enabled
        assert written["jobs"]["jobs"]["executor-heartbeat"]["enabled"] is True
        # Response must indicate a recovery was performed (not "already running")
        assert "already" not in result.lower()
        # Response must mention what was fixed
        assert "steward-heartbeat" in result

    def test_partial_recovery_does_not_fire_when_all_core_jobs_enabled(self):
        """No toggle call occurs when execution_enabled=True and all wos_core jobs are enabled."""
        handle_wos_start = self._import()
        jobs_data = self._make_jobs_json_all_core_enabled()
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers._read_jobs_json",
                  return_value=jobs_data),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs") as mock_toggle,
        ):
            handle_wos_start()
        mock_toggle.assert_not_called()

    def test_start_calls_toggle_with_enabled_true(self):
        """Starting WOS should call toggle_wos_core_jobs(enabled=True)."""
        handle_wos_start = self._import()
        mock_result = {"toggled": ["executor-heartbeat"], "not_found": [], "new_state": "enabled"}
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_result) as mock_toggle,
        ):
            result = handle_wos_start()
        mock_toggle.assert_called_once_with(enabled=True)
        assert "1" in result  # toggled count

    def test_start_reports_not_found_jobs(self):
        """When some WOS-core jobs are absent from jobs.json, the reply mentions them."""
        handle_wos_start = self._import()
        mock_result = {
            "toggled": ["executor-heartbeat"],
            "not_found": ["wos-health-monitor"],
            "new_state": "enabled",
        }
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_result),
        ):
            result = handle_wos_start()
        assert "wos-health-monitor" in result

    def test_start_surfaces_os_error(self):
        """An OSError from toggle_wos_core_jobs is surfaced to the user."""
        handle_wos_start = self._import()
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  side_effect=OSError("disk full")),
        ):
            result = handle_wos_start()
        assert "Failed" in result or "failed" in result


# ---------------------------------------------------------------------------
# handle_wos_stop — idempotency and delegation
# ---------------------------------------------------------------------------

class TestHandleWosStop:
    def _import(self):
        from src.orchestration.dispatcher_handlers import handle_wos_stop
        return handle_wos_stop

    def test_idempotent_when_already_stopped(self):
        """When execution_enabled is False, return a notice without calling toggle."""
        handle_wos_stop = self._import()
        with patch("src.orchestration.dispatcher_handlers.read_wos_config",
                   return_value={"execution_enabled": False}):
            result = handle_wos_stop()
        assert "already" in result.lower()
        assert "paused" in result.lower()

    def test_stop_calls_toggle_with_enabled_false(self):
        """Stopping WOS should call toggle_wos_core_jobs(enabled=False)."""
        handle_wos_stop = self._import()
        mock_result = {"toggled": ["executor-heartbeat", "steward-heartbeat"],
                       "not_found": [], "new_state": "disabled"}
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_result) as mock_toggle,
        ):
            result = handle_wos_stop()
        mock_toggle.assert_called_once_with(enabled=False)
        assert "2" in result  # toggled count

    def test_stop_reports_not_found_jobs(self):
        """When some WOS-core jobs are absent from jobs.json, the reply mentions them."""
        handle_wos_stop = self._import()
        mock_result = {
            "toggled": ["executor-heartbeat"],
            "not_found": ["wos-health-monitor"],
            "new_state": "disabled",
        }
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_result),
        ):
            result = handle_wos_stop()
        assert "wos-health-monitor" in result

    def test_stop_surfaces_os_error(self):
        """An OSError from toggle_wos_core_jobs is surfaced to the user."""
        handle_wos_stop = self._import()
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  side_effect=OSError("disk full")),
        ):
            result = handle_wos_stop()
        assert "Failed" in result or "failed" in result


# ---------------------------------------------------------------------------
# COMMAND_HELP — help text reflects the gating scope
# ---------------------------------------------------------------------------

class TestCommandHelp:
    def test_help_mentions_14_wos_core_jobs(self):
        """COMMAND_HELP describes wos start/stop as gating all WOS-core jobs."""
        from src.orchestration.dispatcher_handlers import COMMAND_HELP
        assert "14" in COMMAND_HELP
        assert "wos start" in COMMAND_HELP.lower()
        assert "wos stop" in COMMAND_HELP.lower()

    def test_handle_help_returns_command_help(self):
        """handle_help() returns the COMMAND_HELP constant."""
        from src.orchestration.dispatcher_handlers import handle_help, COMMAND_HELP
        assert handle_help() is COMMAND_HELP
