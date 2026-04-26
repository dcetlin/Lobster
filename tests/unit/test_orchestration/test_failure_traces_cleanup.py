"""
Unit tests for cleanup_failure_traces() in steward-heartbeat.py.

Coverage:
- Old files (> 30 days) are deleted; recent files are kept
- Files whose stem matches an active UoW ID are protected from deletion
- Count cap: when > 500 files remain after age cleanup, oldest are deleted
  until count <= 500
- Count cap respects active-UoW guard
- Returns correct deleted count
- Returns 0 when traces dir does not exist
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Load steward-heartbeat.py via importlib (hyphenated filename, no __init__)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_HEARTBEAT_PATH = _REPO_ROOT / "scheduled-tasks" / "steward-heartbeat.py"


def _load_heartbeat():
    spec = importlib.util.spec_from_file_location("steward_heartbeat", _HEARTBEAT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["steward_heartbeat"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hb():
    return _load_heartbeat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_trace(traces_dir: Path, name: str, age_days: float) -> Path:
    """Write a dummy trace JSON file and backdate its mtime."""
    f = traces_dir / f"{name}.json"
    f.write_text(json.dumps({"uow_id": name, "reason": "hard_cap"}))
    mtime = time.time() - age_days * 86400
    import os
    os.utime(str(f), (mtime, mtime))
    return f


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_traces_dir_returns_zero(hb, tmp_path, monkeypatch):
    """cleanup_failure_traces returns 0 when the traces directory does not exist."""
    absent_dir = tmp_path / "nonexistent" / "failure-traces"
    monkeypatch.setattr(hb, "TRACES_DIR", absent_dir)
    assert hb.cleanup_failure_traces(set()) == 0


def test_old_files_deleted_recent_kept(hb, tmp_path, monkeypatch):
    """Files older than MAX_TRACE_AGE_DAYS are deleted; recent files are kept."""
    traces_dir = tmp_path / "failure-traces"
    traces_dir.mkdir()
    monkeypatch.setattr(hb, "TRACES_DIR", traces_dir)

    old = _write_trace(traces_dir, "uow_old_001", age_days=31)
    recent = _write_trace(traces_dir, "uow_recent_001", age_days=1)

    deleted = hb.cleanup_failure_traces(set())

    assert deleted == 1
    assert not old.exists()
    assert recent.exists()


def test_active_uow_files_protected(hb, tmp_path, monkeypatch):
    """Files whose stem is in active_uow_ids are not deleted even if old."""
    traces_dir = tmp_path / "failure-traces"
    traces_dir.mkdir()
    monkeypatch.setattr(hb, "TRACES_DIR", traces_dir)

    protected = _write_trace(traces_dir, "uow_active_001", age_days=40)
    unprotected = _write_trace(traces_dir, "uow_old_002", age_days=40)

    deleted = hb.cleanup_failure_traces({"uow_active_001"})

    assert deleted == 1
    assert protected.exists()
    assert not unprotected.exists()


def test_count_cap_deletes_oldest(hb, tmp_path, monkeypatch):
    """After age cleanup, if > MAX_TRACE_COUNT files remain, oldest are trimmed."""
    traces_dir = tmp_path / "failure-traces"
    traces_dir.mkdir()
    monkeypatch.setattr(hb, "TRACES_DIR", traces_dir)
    monkeypatch.setattr(hb, "MAX_TRACE_COUNT", 5)

    # Write 7 recent files (won't be caught by age cap)
    for i in range(7):
        _write_trace(traces_dir, f"uow_cnt_{i:04d}", age_days=i * 0.1)

    deleted = hb.cleanup_failure_traces(set())

    assert deleted == 2
    remaining = list(traces_dir.glob("*.json"))
    assert len(remaining) == 5


def test_count_cap_respects_active_guard(hb, tmp_path, monkeypatch):
    """Count cap skips files belonging to active UoWs."""
    traces_dir = tmp_path / "failure-traces"
    traces_dir.mkdir()
    monkeypatch.setattr(hb, "TRACES_DIR", traces_dir)
    monkeypatch.setattr(hb, "MAX_TRACE_COUNT", 3)

    # 5 recent files; age_days = i * 0.1, so i=4 is the oldest, i=0 is newest.
    # Sorted by mtime ascending (oldest first): i=4, i=3, i=2, i=1, i=0.
    for i in range(5):
        _write_trace(traces_dir, f"uow_g_{i:04d}", age_days=i * 0.1)

    # Protect only the single oldest file (uow_g_0004). The next oldest (uow_g_0003)
    # can still be deleted. Excess = 5 - 3 = 2 candidates: [uow_g_0004, uow_g_0003].
    # uow_g_0004 is active → skip; uow_g_0003 is not active → delete.
    # Only 1 deleted (guard prevents reaching the cap fully).
    active_ids = {"uow_g_0004"}
    deleted = hb.cleanup_failure_traces(active_ids)

    assert deleted == 1
    assert (traces_dir / "uow_g_0004.json").exists()  # active — protected
    assert not (traces_dir / "uow_g_0003.json").exists()  # oldest non-active — deleted


def test_returns_zero_when_nothing_to_clean(hb, tmp_path, monkeypatch):
    """Returns 0 when all files are recent and count is under cap."""
    traces_dir = tmp_path / "failure-traces"
    traces_dir.mkdir()
    monkeypatch.setattr(hb, "TRACES_DIR", traces_dir)

    _write_trace(traces_dir, "uow_fresh_001", age_days=1)
    _write_trace(traces_dir, "uow_fresh_002", age_days=2)

    assert hb.cleanup_failure_traces(set()) == 0
