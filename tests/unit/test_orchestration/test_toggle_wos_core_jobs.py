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
- handle_wos_start re-enables systemd timers for WOS-core jobs that have them.
- handle_wos_stop returns an idempotent notice when execution is already disabled.
- handle_wos_stop calls toggle_wos_core_jobs(False) when stopping from running state.
- handle_wos_stop disables systemd timers for WOS-core jobs that have them.
- _toggle_systemd_timers enables/disables LOBSTER-MANAGED timers for WOS-core jobs.
- _toggle_systemd_timers skips jobs without a timer unit file.
- _toggle_systemd_timers skips timers missing the LOBSTER-MANAGED marker.
- _toggle_systemd_timers continues when systemctl fails (permission error or other).
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

    def _run_with_pause_reason(self, enabled: bool, pause_reason: str | None,
                               existing_wos_config: dict | None = None):
        """Run toggle_wos_core_jobs with a given pause_reason, return the written config."""
        jobs_data = _make_jobs_json({"executor-heartbeat": _wos_core_entry("executor-heartbeat", not enabled)})
        wos_config = existing_wos_config or {"execution_enabled": not enabled}
        written = {}

        toggle = self._import()
        with (
            patch("src.orchestration.dispatcher_handlers._read_jobs_json", return_value=jobs_data),
            patch("src.orchestration.dispatcher_handlers._write_jobs_json"),
            patch("src.orchestration.dispatcher_handlers.read_wos_config", return_value=wos_config),
            patch("src.orchestration.dispatcher_handlers._write_wos_config",
                  side_effect=lambda cfg: written.update({"config": cfg})),
        ):
            toggle(enabled=enabled, pause_reason=pause_reason)

        return written.get("config", {})

    def test_disable_with_user_command_writes_pause_reason(self):
        """toggle_wos_core_jobs(enabled=False, pause_reason='user_command') writes
        pause_reason to wos-config.json so the starvation guard can read it."""
        written_config = self._run_with_pause_reason(
            enabled=False, pause_reason="user_command",
            existing_wos_config={"execution_enabled": True},
        )
        assert written_config.get("pause_reason") == "user_command"
        assert written_config["execution_enabled"] is False

    def test_enable_clears_pause_reason(self):
        """toggle_wos_core_jobs(enabled=True) removes pause_reason from wos-config.json
        regardless of what was previously stored."""
        written_config = self._run_with_pause_reason(
            enabled=True, pause_reason=None,
            existing_wos_config={"execution_enabled": False, "pause_reason": "user_command"},
        )
        assert "pause_reason" not in written_config
        assert written_config["execution_enabled"] is True


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
        """Stopping WOS should call toggle_wos_core_jobs(enabled=False, pause_reason='user_command')."""
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
        mock_toggle.assert_called_once_with(enabled=False, pause_reason="user_command")
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

# ---------------------------------------------------------------------------
# _toggle_systemd_timers — unit tests with subprocess and file-system mocked
# ---------------------------------------------------------------------------

# Names the spec uses for the timer-backed WOS-core jobs.
# The exact set is in _WOS_CORE_TIMER_JOBS; we test a representative subset.
_TIMER_JOB = "issue-sweeper"
_TIMER_UNIT = f"lobster-{_TIMER_JOB}.timer"
_TIMER_PATH = f"/etc/systemd/system/{_TIMER_UNIT}"


class TestToggleSystemdTimers:
    """_toggle_systemd_timers enables/disables systemd timers for WOS-core jobs."""

    def _import(self):
        from src.orchestration.dispatcher_handlers import _toggle_systemd_timers
        return _toggle_systemd_timers

    def _run(self, enabled: bool, *, timer_file_exists: bool = True,
             timer_is_managed: bool = True, subprocess_returncode: int = 0):
        """Run _toggle_systemd_timers with mocked file system and subprocess."""
        _toggle_systemd_timers = self._import()

        import subprocess

        def fake_path_exists(path):
            # Only the one test timer exists (or not).
            return timer_file_exists and str(path).endswith(_TIMER_UNIT)

        def fake_path_read_text(path):
            if timer_is_managed:
                return "[Unit]\n# LOBSTER-MANAGED\n[Timer]\nOnCalendar=daily\n"
            return "[Unit]\n[Timer]\nOnCalendar=daily\n"

        fake_result = MagicMock()
        fake_result.returncode = subprocess_returncode

        toggled = []

        def fake_run(cmd, **kwargs):
            # Record what was called.
            toggled.append(cmd)
            return fake_result

        with (
            patch("src.orchestration.dispatcher_handlers._SYSTEMD_UNIT_DIR") as mock_dir,
            patch("subprocess.run", side_effect=fake_run),
        ):
            # Make the mock Path support / operator and iteration
            mock_unit_file = MagicMock()
            mock_unit_file.exists.return_value = timer_file_exists
            mock_unit_file.read_text.return_value = (
                "[Unit]\n# LOBSTER-MANAGED\n[Timer]\nOnCalendar=daily\n"
                if timer_is_managed
                else "[Unit]\n[Timer]\nOnCalendar=daily\n"
            )
            mock_dir.__truediv__ = lambda self, name: mock_unit_file
            result = _toggle_systemd_timers(enabled)

        return result, toggled

    def test_enable_calls_systemctl_enable_now_for_managed_timer(self):
        """When enabled=True and the timer file has LOBSTER-MANAGED, systemctl enable --now is called."""
        result, cmds = self._run(enabled=True)
        # At least one systemctl call must have been made with 'enable'
        enable_calls = [c for c in cmds if "enable" in c and "lobster-issue-sweeper.timer" in c]
        assert len(enable_calls) >= 1, f"Expected enable call, got: {cmds}"

    def test_disable_calls_systemctl_disable_now_for_managed_timer(self):
        """When enabled=False and the timer file has LOBSTER-MANAGED, systemctl disable --now is called."""
        result, cmds = self._run(enabled=False)
        disable_calls = [c for c in cmds if "disable" in c and "lobster-issue-sweeper.timer" in c]
        assert len(disable_calls) >= 1, f"Expected disable call, got: {cmds}"

    def test_returns_list_of_toggled_timer_names(self):
        """Return value is a list of timer unit names that were successfully toggled."""
        result, _ = self._run(enabled=True)
        assert isinstance(result, list)
        # At least one timer should have been returned if the file exists and is managed
        assert any("issue-sweeper" in name for name in result), f"Expected issue-sweeper in {result}"

    def test_skips_timer_when_unit_file_absent(self):
        """If the timer unit file does not exist, no systemctl call is made and name is excluded."""
        result, cmds = self._run(enabled=True, timer_file_exists=False)
        issue_sweeper_calls = [c for c in cmds if "issue-sweeper" in str(c)]
        assert len(issue_sweeper_calls) == 0
        assert not any("issue-sweeper" in name for name in result)

    def test_skips_timer_without_lobster_managed_marker(self):
        """Timers without the LOBSTER-MANAGED comment are not touched."""
        result, cmds = self._run(enabled=True, timer_is_managed=False)
        issue_sweeper_calls = [c for c in cmds if "issue-sweeper" in str(c)]
        assert len(issue_sweeper_calls) == 0
        assert not any("issue-sweeper" in name for name in result)

    def test_continues_after_systemctl_failure(self):
        """A non-zero return code from systemctl does not raise; function returns partial results."""
        # Should not raise even when systemctl fails
        result, cmds = self._run(enabled=True, subprocess_returncode=1)
        # The function should still return a list (possibly empty on failure)
        assert isinstance(result, list)

    def test_continues_after_subprocess_exception(self):
        """A subprocess exception (e.g. permission denied) does not propagate; returns empty list."""
        from src.orchestration.dispatcher_handlers import _toggle_systemd_timers

        with (
            patch("src.orchestration.dispatcher_handlers._SYSTEMD_UNIT_DIR") as mock_dir,
            patch("subprocess.run", side_effect=OSError("permission denied")),
        ):
            mock_unit_file = MagicMock()
            mock_unit_file.exists.return_value = True
            mock_unit_file.read_text.return_value = "[Unit]\n# LOBSTER-MANAGED\n"
            mock_dir.__truediv__ = lambda self, name: mock_unit_file
            result = _toggle_systemd_timers(True)

        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# handle_wos_start — systemd timer management
# ---------------------------------------------------------------------------

class TestHandleWosStartTimers:
    """handle_wos_start re-enables systemd timers for WOS-core timer-backed jobs."""

    def _mock_registry(self):
        """Return a MagicMock that satisfies the Registry protocol."""
        m = MagicMock()
        m.log_control_event = MagicMock()
        return m

    def test_start_calls_toggle_systemd_timers_with_enabled_true(self):
        """handle_wos_start calls _toggle_systemd_timers(True) to re-enable timers."""
        from src.orchestration.dispatcher_handlers import handle_wos_start

        mock_result = {"toggled": ["executor-heartbeat"], "not_found": [], "new_state": "enabled"}
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_result),
            patch("src.orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=["issue-sweeper"]) as mock_timers,
        ):
            handle_wos_start(registry=self._mock_registry())

        mock_timers.assert_called_once_with(True)

    def test_start_mentions_timer_count_when_timers_were_toggled(self):
        """When timers were re-enabled, the reply notes how many were toggled."""
        from src.orchestration.dispatcher_handlers import handle_wos_start

        mock_result = {"toggled": ["executor-heartbeat"], "not_found": [], "new_state": "enabled"}
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_result),
            patch("src.orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=["issue-sweeper", "github-issue-cultivator"]),
        ):
            result = handle_wos_start(registry=self._mock_registry())

        assert "2" in result or "timer" in result.lower()

    def test_start_also_calls_toggle_timers_on_partial_recovery(self):
        """Partial-recovery path also re-enables systemd timers."""
        from src.orchestration.dispatcher_handlers import handle_wos_start

        jobs_data = {
            "jobs": {
                "executor-heartbeat": {"wos_core": True, "enabled": True},
                "steward-heartbeat": {"wos_core": True, "enabled": False},
            }
        }
        mock_toggle_result = {
            "toggled": ["steward-heartbeat"], "not_found": [], "new_state": "enabled"
        }
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers._read_jobs_json",
                  return_value=jobs_data),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_toggle_result),
            patch("src.orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=["issue-sweeper"]) as mock_timers,
        ):
            handle_wos_start(registry=self._mock_registry())

        mock_timers.assert_called_once_with(True)


# ---------------------------------------------------------------------------
# handle_wos_stop — systemd timer management
# ---------------------------------------------------------------------------

class TestHandleWosStopTimers:
    """handle_wos_stop disables systemd timers for WOS-core timer-backed jobs."""

    def _mock_registry(self):
        m = MagicMock()
        m.log_control_event = MagicMock()
        return m

    def test_stop_calls_toggle_systemd_timers_with_enabled_false(self):
        """handle_wos_stop calls _toggle_systemd_timers(False) to disable timers."""
        from src.orchestration.dispatcher_handlers import handle_wos_stop

        mock_result = {"toggled": ["executor-heartbeat", "steward-heartbeat"],
                       "not_found": [], "new_state": "disabled"}
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_result),
            patch("src.orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=["issue-sweeper"]) as mock_timers,
        ):
            handle_wos_stop(registry=self._mock_registry())

        mock_timers.assert_called_once_with(False)

    def test_stop_mentions_timer_count_when_timers_were_disabled(self):
        """When timers were disabled, the reply notes how many were toggled."""
        from src.orchestration.dispatcher_handlers import handle_wos_stop

        mock_result = {"toggled": ["executor-heartbeat"], "not_found": [], "new_state": "disabled"}
        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": True}),
            patch("src.orchestration.dispatcher_handlers.toggle_wos_core_jobs",
                  return_value=mock_result),
            patch("src.orchestration.dispatcher_handlers._toggle_systemd_timers",
                  return_value=["issue-sweeper", "github-issue-cultivator"]),
        ):
            result = handle_wos_stop(registry=self._mock_registry())

        assert "2" in result or "timer" in result.lower()

    def test_stop_does_not_call_toggle_timers_when_already_stopped(self):
        """When already stopped, the idempotent path does not call _toggle_systemd_timers."""
        from src.orchestration.dispatcher_handlers import handle_wos_stop

        with (
            patch("src.orchestration.dispatcher_handlers.read_wos_config",
                  return_value={"execution_enabled": False}),
            patch("src.orchestration.dispatcher_handlers._toggle_systemd_timers") as mock_timers,
        ):
            handle_wos_stop(registry=self._mock_registry())

        mock_timers.assert_not_called()


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
