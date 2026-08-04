"""
Unit tests for scheduled-tasks/agent-replies-sweep.py.

Named after behaviors:
  - test_extracts_request_id_from_reply_filename
  - test_extracts_request_id_from_ack_filename
  - test_rejects_filename_with_path_traversal_characters
  - test_rejects_filename_that_is_not_json
  - test_age_prefers_payload_ts_over_mtime
  - test_age_falls_back_to_mtime_when_ts_absent_or_unparseable
  - test_plan_marks_old_file_for_removal
  - test_plan_keeps_recent_file
  - test_plan_skips_unrecognized_filename_without_deleting_it
  - test_apply_sweep_only_removes_files_planned_for_removal
  - test_apply_sweep_dry_run_never_touches_filesystem
  - test_in_flight_request_with_no_reply_file_is_never_touched
  - test_disabled_job_exits_without_scanning
  - test_main_removes_only_stale_files_end_to_end
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load the script under test via importlib (it lives in scheduled-tasks/,
# not a package, and the filename contains hyphens).
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scheduled-tasks"
    / "agent-replies-sweep.py"
)

spec = importlib.util.spec_from_file_location("agent_replies_sweep", SCRIPT_PATH)
_mod: ModuleType = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = _mod  # required for dataclass field resolution under `from __future__ import annotations`
spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_extract_request_id = _mod._extract_request_id
_file_age_hours = _mod._file_age_hours
plan_sweep = _mod.plan_sweep
apply_sweep = _mod.apply_sweep
scan_directory = _mod.scan_directory
SweepPlan = _mod.SweepPlan
main = _mod.main

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _extract_request_id — pure function tests
# ---------------------------------------------------------------------------


def test_extracts_request_id_from_reply_filename() -> None:
    assert _extract_request_id("1732900000-a1b2c3d4.json") == "1732900000-a1b2c3d4"


def test_extracts_request_id_from_ack_filename() -> None:
    assert _extract_request_id("1732900000-a1b2c3d4.ack.json") == "1732900000-a1b2c3d4"


def test_rejects_filename_with_path_traversal_characters() -> None:
    assert _extract_request_id("../../etc/passwd.json") is None


def test_rejects_filename_that_is_not_json() -> None:
    assert _extract_request_id("readme.txt") is None
    assert _extract_request_id("some-directory") is None


def test_rejects_filename_exceeding_max_length() -> None:
    assert _extract_request_id(("a" * 129) + ".json") is None


# ---------------------------------------------------------------------------
# _file_age_hours — pure function tests
# ---------------------------------------------------------------------------


def test_age_prefers_payload_ts_over_mtime() -> None:
    payload = {"ts": (NOW - timedelta(hours=10)).isoformat()}
    # mtime says 1 hour old, but ts (authoritative) says 10 hours old.
    mtime_epoch = (NOW - timedelta(hours=1)).timestamp()
    age = _file_age_hours(payload, mtime_epoch, NOW)
    assert age == pytest.approx(10.0, abs=0.01)


def test_age_falls_back_to_mtime_when_ts_absent_or_unparseable() -> None:
    mtime_epoch = (NOW - timedelta(hours=5)).timestamp()
    assert _file_age_hours(None, mtime_epoch, NOW) == pytest.approx(5.0, abs=0.01)
    assert _file_age_hours({"ts": "not-a-date"}, mtime_epoch, NOW) == pytest.approx(5.0, abs=0.01)
    assert _file_age_hours({}, mtime_epoch, NOW) == pytest.approx(5.0, abs=0.01)


# ---------------------------------------------------------------------------
# plan_sweep — pure decision function tests
# ---------------------------------------------------------------------------


def test_plan_marks_old_file_for_removal() -> None:
    entries = [("req-old.json", (NOW - timedelta(hours=200)).timestamp(), None)]
    plans = plan_sweep(entries, retention_hours=168, now=NOW)
    assert plans[0].action == "remove"
    assert plans[0].request_id == "req-old"


def test_plan_keeps_recent_file() -> None:
    entries = [("req-new.json", (NOW - timedelta(hours=1)).timestamp(), None)]
    plans = plan_sweep(entries, retention_hours=168, now=NOW)
    assert plans[0].action == "keep_recent"


def test_plan_skips_unrecognized_filename_without_deleting_it() -> None:
    entries = [("mystery-file.log", (NOW - timedelta(hours=999)).timestamp(), None)]
    plans = plan_sweep(entries, retention_hours=168, now=NOW)
    assert plans[0].action == "skip_unrecognized"
    assert plans[0].request_id is None


def test_plan_boundary_is_inclusive_of_retention_window() -> None:
    """A file exactly at the retention boundary is eligible for removal (>=)."""
    entries = [("req-boundary.json", (NOW - timedelta(hours=168)).timestamp(), None)]
    plans = plan_sweep(entries, retention_hours=168, now=NOW)
    assert plans[0].action == "remove"


# ---------------------------------------------------------------------------
# apply_sweep — filesystem side-effect tests
# ---------------------------------------------------------------------------


def test_apply_sweep_only_removes_files_planned_for_removal(tmp_path: Path) -> None:
    (tmp_path / "req-old.json").write_text("{}")
    (tmp_path / "req-new.json").write_text("{}")
    (tmp_path / "mystery-file.log").write_text("not json")

    plans = [
        SweepPlan(Path("req-old.json"), "remove", 200.0, "req-old"),
        SweepPlan(Path("req-new.json"), "keep_recent", 1.0, "req-new"),
        SweepPlan(Path("mystery-file.log"), "skip_unrecognized", None, None),
    ]
    removed_count, errors = apply_sweep(tmp_path, plans, dry_run=False)

    assert removed_count == 1
    assert errors == []
    assert not (tmp_path / "req-old.json").exists()
    assert (tmp_path / "req-new.json").exists()
    assert (tmp_path / "mystery-file.log").exists()


def test_apply_sweep_dry_run_never_touches_filesystem(tmp_path: Path) -> None:
    target = tmp_path / "req-old.json"
    target.write_text("{}")
    plans = [SweepPlan(Path("req-old.json"), "remove", 200.0, "req-old")]

    removed_count, errors = apply_sweep(tmp_path, plans, dry_run=True)

    assert removed_count == 1  # reported as "would remove"
    assert errors == []
    assert target.exists()  # but nothing was actually deleted


# ---------------------------------------------------------------------------
# In-flight safety — the core guarantee this job must uphold
# ---------------------------------------------------------------------------


def test_in_flight_request_with_no_reply_file_is_never_touched(tmp_path: Path) -> None:
    """
    A request still being worked on has no file in agent-replies/ at all —
    the answer/ack hasn't been written yet. Scanning an empty (or unrelated)
    directory must never manufacture a removal for something that isn't there.
    """
    entries = scan_directory(tmp_path)
    assert entries == []
    plans = plan_sweep(entries, retention_hours=168, now=NOW)
    assert plans == []


def test_recently_written_reply_survives_a_sweep_even_with_zero_retention(tmp_path: Path) -> None:
    """
    Sanity check on the age comparator: a file written "now" is age ~0h, so
    only a retention window of 0 (never configured in practice) would touch
    it — anything positive keeps it. Guards against an off-by-one that would
    let a live poller's answer get swept before it's read.
    """
    entries = [("req-fresh.json", NOW.timestamp(), {"ts": NOW.isoformat()})]
    plans = plan_sweep(entries, retention_hours=24, now=NOW)
    assert plans[0].action == "keep_recent"


# ---------------------------------------------------------------------------
# main() — end-to-end wiring
# ---------------------------------------------------------------------------


def test_disabled_job_exits_without_scanning(tmp_path: Path) -> None:
    jobs_json = tmp_path / "scheduled-jobs" / "jobs.json"
    jobs_json.parent.mkdir(parents=True, exist_ok=True)
    jobs_json.write_text(json.dumps({"jobs": {"agent-replies-sweep": {"enabled": False}}}))

    agent_replies_dir = tmp_path / "agent-replies"
    agent_replies_dir.mkdir(exist_ok=True)
    (agent_replies_dir / "req-old.json").write_text(json.dumps({"ts": "2020-01-01T00:00:00+00:00"}))

    with (
        patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(tmp_path)}),
        patch.object(_mod, "AGENT_REPLIES_DIR", agent_replies_dir),
    ):
        result = main([])

    assert result == 0
    assert (agent_replies_dir / "req-old.json").exists()  # untouched — job never ran


def test_main_removes_only_stale_files_end_to_end(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "scheduled-jobs").mkdir(parents=True, exist_ok=True)
    (workspace / "scheduled-jobs" / "jobs.json").write_text(
        json.dumps({"jobs": {"agent-replies-sweep": {"enabled": True}}})
    )

    agent_replies_dir = tmp_path / "messages" / "agent-replies"
    agent_replies_dir.mkdir(parents=True, exist_ok=True)
    task_outputs_dir = tmp_path / "messages" / "task-outputs"

    stale_ts = (NOW - timedelta(hours=200)).isoformat()
    fresh_ts = (NOW - timedelta(hours=1)).isoformat()
    (agent_replies_dir / "req-stale.json").write_text(json.dumps({"request_id": "req-stale", "text": "old", "ts": stale_ts}))
    (agent_replies_dir / "req-stale.ack.json").write_text(json.dumps({"request_id": "req-stale", "ack": True, "ts": stale_ts}))
    (agent_replies_dir / "req-fresh.json").write_text(json.dumps({"request_id": "req-fresh", "text": "new", "ts": fresh_ts}))
    (agent_replies_dir / "unrelated.log").write_text("not a reply file")

    with (
        patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(workspace)}),
        patch.object(_mod, "AGENT_REPLIES_DIR", agent_replies_dir),
        patch.object(_mod, "MESSAGES_DIR", tmp_path / "messages"),
    ):
        result = main(["--retention-hours", "168"])

    assert result == 0
    assert not (agent_replies_dir / "req-stale.json").exists()
    assert not (agent_replies_dir / "req-stale.ack.json").exists()
    assert (agent_replies_dir / "req-fresh.json").exists()
    assert (agent_replies_dir / "unrelated.log").exists()  # never touched — unrecognized name

    outputs = list(task_outputs_dir.glob("*-agent-replies-sweep.json"))
    assert len(outputs) == 1
    output_data = json.loads(outputs[0].read_text())
    assert output_data["status"] == "success"
    assert "Removed 2" in output_data["output"]
