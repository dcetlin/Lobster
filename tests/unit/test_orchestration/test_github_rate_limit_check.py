"""
Tests for the GitHub API rate limit pre-dispatch check in executor-heartbeat.py.

The rate limit check fires once per dispatch cycle (in main(), after the context
pressure throttle and before run_executor_cycle). It is a pure-ish function
(check_github_rate_limit) that shells out to `gh api rate_limit --jq '.rate | {remaining, reset}'`
and returns a named result so the caller can log and skip without knowing the details.

Coverage:
- GITHUB_RATE_LIMIT_DISPATCH_THRESHOLD constant is a positive integer
- check_github_rate_limit: returns ok=True when remaining >= threshold
- check_github_rate_limit: returns ok=False when remaining < threshold
- check_github_rate_limit: returns ok=True when remaining == threshold (boundary)
- check_github_rate_limit: returns ok=True with remaining=None when gh CLI fails
  (fail-open: do not block dispatch on tool failure)
- check_github_rate_limit: remaining and reset_at are surfaced in the result
- main(): dispatch is skipped when rate limit check returns ok=False
- main(): dispatch proceeds when rate limit check returns ok=True
- main(): rate limit check is called once per cycle (not per UoW)
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import json

import pytest


# ---------------------------------------------------------------------------
# Load executor-heartbeat.py via importlib (hyphenated filename, no __init__)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_HEARTBEAT_PATH = _REPO_ROOT / "scheduled-tasks" / "executor-heartbeat.py"


def _load_heartbeat():
    """Load executor-heartbeat module from file path (hyphen in name)."""
    spec = importlib.util.spec_from_file_location("executor_heartbeat", _HEARTBEAT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["executor_heartbeat"] = module
    spec.loader.exec_module(module)
    return module


_hb = _load_heartbeat()
check_github_rate_limit = _hb.check_github_rate_limit
GITHUB_RATE_LIMIT_DISPATCH_THRESHOLD = _hb.GITHUB_RATE_LIMIT_DISPATCH_THRESHOLD


# ---------------------------------------------------------------------------
# Constant sanity check
# ---------------------------------------------------------------------------

def test_threshold_is_positive_integer():
    assert isinstance(GITHUB_RATE_LIMIT_DISPATCH_THRESHOLD, int)
    assert GITHUB_RATE_LIMIT_DISPATCH_THRESHOLD > 0


# ---------------------------------------------------------------------------
# check_github_rate_limit unit tests (subprocess mocked)
# ---------------------------------------------------------------------------

def _mock_subprocess_result(stdout: str, returncode: int = 0):
    """Return a mock CompletedProcess-like object."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = ""
    return result


def _rate_json(remaining: int, reset: int = 1745748000) -> str:
    """Return the JSON string that gh api rate_limit --jq '.rate | {remaining, reset}' emits."""
    return json.dumps({"remaining": remaining, "reset": reset})


def test_rate_limit_ok_when_remaining_above_threshold():
    """ok=True when remaining > threshold."""
    with patch("subprocess.run", return_value=_mock_subprocess_result(_rate_json(500))) as mock_run:
        result = check_github_rate_limit()
    assert result.ok is True
    assert result.remaining == 500
    mock_run.assert_called_once()


def test_rate_limit_not_ok_when_remaining_below_threshold():
    """ok=False when remaining < threshold."""
    with patch("subprocess.run", return_value=_mock_subprocess_result(_rate_json(50))):
        result = check_github_rate_limit()
    assert result.ok is False
    assert result.remaining == 50


def test_rate_limit_ok_at_exact_threshold():
    """ok=True when remaining == threshold (boundary: threshold is the minimum allowed)."""
    threshold = GITHUB_RATE_LIMIT_DISPATCH_THRESHOLD
    with patch("subprocess.run", return_value=_mock_subprocess_result(_rate_json(threshold))):
        result = check_github_rate_limit()
    assert result.ok is True
    assert result.remaining == threshold


def test_rate_limit_ok_when_gh_cli_fails():
    """Fail-open: ok=True when gh CLI returns non-zero exit code.

    A broken gh CLI must not block dispatch — the subagent will hit the
    rate limit and fail on its own terms, but blocking dispatch on a CLI
    failure would be worse than letting it proceed.
    """
    with patch("subprocess.run", return_value=_mock_subprocess_result("", returncode=1)):
        result = check_github_rate_limit()
    assert result.ok is True
    assert result.remaining is None


def test_rate_limit_ok_when_gh_cli_raises():
    """Fail-open: ok=True when subprocess.run raises (e.g. gh not on PATH)."""
    with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
        result = check_github_rate_limit()
    assert result.ok is True
    assert result.remaining is None


def test_rate_limit_ok_when_output_unparseable():
    """Fail-open: ok=True when gh output is not valid JSON."""
    with patch("subprocess.run", return_value=_mock_subprocess_result("not-json\n")):
        result = check_github_rate_limit()
    assert result.ok is True
    assert result.remaining is None


def test_rate_limit_result_surfaces_reset_at():
    """reset_at is a non-None ISO string when gh output includes a reset epoch."""
    with patch("subprocess.run", return_value=_mock_subprocess_result(_rate_json(500, reset=1745748000))):
        result = check_github_rate_limit()
    assert result.reset_at is not None
    # Should be a parseable ISO timestamp
    from datetime import datetime
    datetime.fromisoformat(result.reset_at)  # raises if not valid


# ---------------------------------------------------------------------------
# Integration with main() — dispatch skipped when rate limit is low
# ---------------------------------------------------------------------------

def _make_mock_registry(tmp_path):
    """Create a minimal mock registry that satisfies main()'s usage."""
    from orchestration.registry import Registry
    db_path = tmp_path / "test_registry.db"
    return Registry(db_path)



def test_dispatch_skipped_when_rate_limit_low(tmp_path):
    """run_executor_cycle is NOT called when check_github_rate_limit returns ok=False."""
    db_path = tmp_path / "registry.db"
    db_path.touch()

    _hb_fresh = _load_heartbeat()

    low_limit_result = MagicMock()
    low_limit_result.ok = False
    low_limit_result.remaining = 42
    low_limit_result.reset_at = "2026-04-27T10:00:00Z"

    mock_cycle = MagicMock()

    with (
        patch("sys.argv", ["executor-heartbeat.py"]),
        patch.object(_hb_fresh, "_is_job_enabled", return_value=True),
        patch.object(_hb_fresh, "check_github_rate_limit", return_value=low_limit_result),
        patch.object(_hb_fresh, "run_executor_cycle", mock_cycle),
        patch.object(_hb_fresh, "run_ttl_recovery"),
        patch.object(_hb_fresh, "REGISTRY_DB", db_path),
        patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True),
        patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False),
        patch("src.agents.session_store.get_active_sessions", return_value=[]),
        patch("src.orchestration.heartbeat_sidecar.write_heartbeats_for_active_uows",
              return_value=MagicMock(checked=0, written=0, skipped=0, errors=0)),
    ):
        exit_code = _hb_fresh.main()

    mock_cycle.assert_not_called()
    assert exit_code == 0


def test_dispatch_proceeds_when_rate_limit_ok(tmp_path):
    """run_executor_cycle IS called when check_github_rate_limit returns ok=True."""
    db_path = tmp_path / "registry.db"
    db_path.touch()

    _hb_fresh = _load_heartbeat()

    ok_limit_result = MagicMock()
    ok_limit_result.ok = True
    ok_limit_result.remaining = 500
    ok_limit_result.reset_at = None

    cycle_result = {
        "evaluated": 1, "ready": 1, "stale": 1,
        "dispatched": 1, "skipped": 0, "errors": 0,
    }
    mock_cycle = MagicMock(return_value=cycle_result)

    with (
        patch("sys.argv", ["executor-heartbeat.py"]),
        patch.object(_hb_fresh, "_is_job_enabled", return_value=True),
        patch.object(_hb_fresh, "check_github_rate_limit", return_value=ok_limit_result),
        patch.object(_hb_fresh, "run_executor_cycle", mock_cycle),
        patch.object(_hb_fresh, "run_ttl_recovery"),
        patch.object(_hb_fresh, "REGISTRY_DB", db_path),
        patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True),
        patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False),
        patch("src.agents.session_store.get_active_sessions", return_value=[]),
        patch("src.orchestration.heartbeat_sidecar.write_heartbeats_for_active_uows",
              return_value=MagicMock(checked=0, written=0, skipped=0, errors=0)),
    ):
        exit_code = _hb_fresh.main()

    mock_cycle.assert_called_once()
    assert exit_code == 0


def test_rate_limit_check_called_once_per_cycle(tmp_path):
    """check_github_rate_limit is called exactly once per dispatch cycle, not per UoW."""
    db_path = tmp_path / "registry.db"
    db_path.touch()

    _hb_fresh = _load_heartbeat()

    ok_limit_result = MagicMock()
    ok_limit_result.ok = True
    ok_limit_result.remaining = 500
    ok_limit_result.reset_at = None

    cycle_result = {
        "evaluated": 3, "ready": 3, "stale": 3,
        "dispatched": 3, "skipped": 0, "errors": 0,
    }

    mock_check = MagicMock(return_value=ok_limit_result)

    with (
        patch("sys.argv", ["executor-heartbeat.py"]),
        patch.object(_hb_fresh, "_is_job_enabled", return_value=True),
        patch.object(_hb_fresh, "check_github_rate_limit", mock_check),
        patch.object(_hb_fresh, "run_executor_cycle", return_value=cycle_result),
        patch.object(_hb_fresh, "run_ttl_recovery"),
        patch.object(_hb_fresh, "REGISTRY_DB", db_path),
        patch("src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True),
        patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False),
        patch("src.agents.session_store.get_active_sessions", return_value=[]),
        patch("src.orchestration.heartbeat_sidecar.write_heartbeats_for_active_uows",
              return_value=MagicMock(checked=0, written=0, skipped=0, errors=0)),
    ):
        _hb_fresh.main()

    # Regardless of how many UoWs run_executor_cycle dispatched (3),
    # the rate limit check is called exactly once.
    assert mock_check.call_count == 1
