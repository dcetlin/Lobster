"""
Unit tests for _read_cc_quota() in scheduled-tasks/executor-heartbeat.py.

The function reads ~/.claude/cc-budget/state.json (or the path in
LOBSTER_CC_BUDGET_STATE) and returns rate_limits.five_hour.pct if the
file exists and is fresh (last_updated within 60 minutes), else None.

State file shape written by cc-usage-poller.py:
    {
      "rate_limits": {
        "five_hour": {"pct": <float>, "resets_at": "<ISO8601>"},
        "seven_day":  {"pct": <float>, "resets_at": "<ISO8601>"}
      },
      "last_updated": "<ISO8601>",
      "ts": <unix-int>,
      "source": "cc-usage-poller"
    }

Named after behaviors, not mechanisms.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load the script module via importlib.
#
# executor-heartbeat.py is a standalone script, not a package module, so we
# load it via importlib. Heavy imports (src.*) are patched out to avoid
# needing a full runtime environment in unit tests.
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parents[3] / "scheduled-tasks" / "executor-heartbeat.py"
)


def _load_executor_heartbeat():
    """Load executor-heartbeat.py as a module, stubbing heavy imports."""
    import types as _types

    # Stub out src.* imports that require a live DB or full environment
    _src_stub = _types.ModuleType("src")
    _orchestration_stub = _types.ModuleType("src.orchestration")
    _paths_stub = _types.ModuleType("src.orchestration.paths")
    _paths_stub.REGISTRY_DB = Path("/tmp/nonexistent-registry.db")
    _src_stub.orchestration = _orchestration_stub
    _orchestration_stub.paths = _paths_stub

    MODULE_NAME = "executor_heartbeat"
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]

    # Register the module in sys.modules BEFORE exec_module so that
    # @dataclass and other decorators can resolve cls.__module__ correctly.
    with patch.dict(
        "sys.modules",
        {
            MODULE_NAME: mod,
            "src": _src_stub,
            "src.orchestration": _orchestration_stub,
            "src.orchestration.paths": _paths_stub,
        },
    ):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

    return mod


# Load once at module level; individual tests patch as needed.
executor_heartbeat = _load_executor_heartbeat()
_read_cc_quota = executor_heartbeat._read_cc_quota
CC_QUOTA_SKIP_THRESHOLD = executor_heartbeat.CC_QUOTA_SKIP_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_timestamp() -> str:
    """Return an ISO 8601 UTC last_updated timestamp 5 minutes ago (fresh, within 60 min)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


def _stale_timestamp() -> str:
    """Return an ISO 8601 UTC last_updated timestamp 90 minutes ago (stale, > 60 min)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()


def _write_state(path: Path, five_hour_pct: float, last_updated: str) -> None:
    """Write a state.json with the shape that cc-usage-poller.py produces."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "rate_limits": {
            "five_hour": {"pct": five_hour_pct, "resets_at": None},
            "seven_day": {"pct": 20.0, "resets_at": None},
        },
        "last_updated": last_updated,
        "ts": 0,
        "source": "cc-usage-poller",
    }))


# ---------------------------------------------------------------------------
# Tests — behaviors
# ---------------------------------------------------------------------------


def test_returns_none_when_state_file_missing(tmp_path):
    """Absent state file → None (fail-open: dispatch proceeds normally)."""
    missing = tmp_path / "nonexistent" / "state.json"
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(missing)}):
        result = _read_cc_quota()
    assert result is None


def test_returns_none_when_state_file_is_stale(tmp_path):
    """Stale state file (fetched_at > 60 min ago) → None (dispatch proceeds)."""
    state_path = tmp_path / "state.json"
    _write_state(state_path, five_hour_pct=95.0, last_updated=_stale_timestamp())
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result is None


def test_returns_float_when_fresh_and_below_threshold(tmp_path):
    """Fresh state with quota < 90% → returns the float (caller allows dispatch)."""
    state_path = tmp_path / "state.json"
    pct = 52.0
    _write_state(state_path, five_hour_pct=pct, last_updated=_fresh_timestamp())
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result == pct


def test_returns_float_when_fresh_and_at_or_above_threshold(tmp_path):
    """Fresh state with quota >= 90% → returns the float (caller decides to skip)."""
    state_path = tmp_path / "state.json"
    pct = 92.5
    _write_state(state_path, five_hour_pct=pct, last_updated=_fresh_timestamp())
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result == pct


def test_threshold_constant_is_ninety():
    """CC_QUOTA_SKIP_THRESHOLD is 90.0 as specified."""
    assert CC_QUOTA_SKIP_THRESHOLD == 90.0


def test_returns_none_when_file_is_malformed(tmp_path):
    """Malformed JSON → None (fail-open)."""
    state_path = tmp_path / "state.json"
    state_path.write_text("not valid json {{{")
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result is None


def test_returns_none_when_rate_limits_key_missing(tmp_path):
    """State file missing rate_limits key → None (fail-open)."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "last_updated": _fresh_timestamp(),
        "ts": 0,
        "source": "cc-usage-poller",
    }))
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result is None


def test_returns_none_when_last_updated_key_missing(tmp_path):
    """State file missing last_updated key → None (fail-open)."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "rate_limits": {
            "five_hour": {"pct": 50.0, "resets_at": None},
            "seven_day": {"pct": 20.0, "resets_at": None},
        },
        "ts": 0,
        "source": "cc-usage-poller",
    }))
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result is None


def test_exactly_at_threshold_returns_float(tmp_path):
    """Quota exactly at 90.0% → returns 90.0 (caller skips at >= threshold)."""
    state_path = tmp_path / "state.json"
    _write_state(state_path, five_hour_pct=90.0, last_updated=_fresh_timestamp())
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result == 90.0


def test_freshness_boundary_within_60_minutes(tmp_path):
    """State file last_updated 59 minutes ago is still fresh → returns float."""
    state_path = tmp_path / "state.json"
    last_updated = (datetime.now(timezone.utc) - timedelta(minutes=59)).isoformat()
    _write_state(state_path, five_hour_pct=75.0, last_updated=last_updated)
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result == 75.0


def test_freshness_boundary_over_60_minutes(tmp_path):
    """State file last_updated 61 minutes ago is stale → returns None."""
    state_path = tmp_path / "state.json"
    last_updated = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    _write_state(state_path, five_hour_pct=75.0, last_updated=last_updated)
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(state_path)}):
        result = _read_cc_quota()
    assert result is None


def test_uses_env_var_path_over_default(tmp_path):
    """LOBSTER_CC_BUDGET_STATE env var controls the file path."""
    custom_path = tmp_path / "custom" / "state.json"
    _write_state(custom_path, five_hour_pct=30.0, last_updated=_fresh_timestamp())
    with patch.dict("os.environ", {"LOBSTER_CC_BUDGET_STATE": str(custom_path)}):
        result = _read_cc_quota()
    assert result == 30.0
