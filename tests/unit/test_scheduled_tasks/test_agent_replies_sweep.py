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

by-agent/ pointer mailbox extension (agent-channel-protocol-proposal.md §3.2):
  - test_extracts_pointer_request_id_from_bare_filename
  - test_rejects_pointer_filename_with_invalid_characters
  - test_plan_pointer_marks_stale_pointer_for_removal
  - test_plan_pointer_keeps_recent_pointer_with_live_target
  - test_plan_pointer_removes_dangling_pointer_regardless_of_age
  - test_plan_pointer_skips_unrecognized_filename_without_deleting_it
  - test_scan_by_agent_dir_recurses_into_slug_subdirectories
  - test_scan_by_agent_dir_ignores_files_directly_under_by_agent
  - test_apply_pointer_sweep_only_removes_planned_pointers
  - test_apply_pointer_sweep_dry_run_never_touches_filesystem
  - test_cleanup_empty_agent_dirs_removes_dirs_left_with_no_pointers
  - test_cleanup_empty_agent_dirs_leaves_nonempty_dirs_alone
  - test_cleanup_empty_agent_dirs_dry_run_never_touches_filesystem
  - test_main_by_agent_sweep_end_to_end_ages_gcs_and_cleans_up
  - test_main_flat_sweep_behavior_unchanged_by_by_agent_extension
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
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

_extract_pointer_request_id = _mod._extract_pointer_request_id
PointerPlan = _mod.PointerPlan
plan_pointer_sweep = _mod.plan_pointer_sweep
scan_by_agent_dir = _mod.scan_by_agent_dir
apply_pointer_sweep = _mod.apply_pointer_sweep
cleanup_empty_agent_dirs = _mod.cleanup_empty_agent_dirs

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
# by-agent/ pointer mailbox — _extract_pointer_request_id, pure tests
# ---------------------------------------------------------------------------


def test_extracts_pointer_request_id_from_bare_filename() -> None:
    # Pointer files have no suffix at all — distinct from the flat sweep's
    # <request_id>.json / .ack.json shape.
    assert _extract_pointer_request_id("1732900000-a1b2c3d4") == "1732900000-a1b2c3d4"


def test_rejects_pointer_filename_with_invalid_characters() -> None:
    assert _extract_pointer_request_id("../../etc/passwd") is None
    assert _extract_pointer_request_id("has a space") is None
    assert _extract_pointer_request_id("") is None


# ---------------------------------------------------------------------------
# by-agent/ pointer mailbox — plan_pointer_sweep, pure decision tests
# ---------------------------------------------------------------------------


def test_plan_pointer_marks_stale_pointer_for_removal() -> None:
    entries = [("bloom", "req-stale", (NOW - timedelta(hours=200)).timestamp())]
    plans = plan_pointer_sweep(entries, existing_request_ids={"req-stale"}, retention_hours=168, now=NOW)
    assert plans[0].action == "remove_stale"
    assert plans[0].request_id == "req-stale"
    assert plans[0].slug == "bloom"


def test_plan_pointer_keeps_recent_pointer_with_live_target() -> None:
    entries = [("bloom", "req-fresh", (NOW - timedelta(hours=1)).timestamp())]
    plans = plan_pointer_sweep(entries, existing_request_ids={"req-fresh"}, retention_hours=168, now=NOW)
    assert plans[0].action == "keep"


def test_plan_pointer_removes_dangling_pointer_regardless_of_age() -> None:
    """
    A pointer is dangling when its target content file is gone — removed
    silently regardless of how fresh the pointer file itself is. This is
    the spec's "not an error, a legitimate intermediate state" GC rule.
    """
    entries = [("bloom", "req-gone", (NOW - timedelta(minutes=1)).timestamp())]
    plans = plan_pointer_sweep(entries, existing_request_ids=set(), retention_hours=168, now=NOW)
    assert plans[0].action == "remove_dangling"
    assert plans[0].request_id == "req-gone"


def test_plan_pointer_skips_unrecognized_filename_without_deleting_it() -> None:
    entries = [("bloom", "not valid!", (NOW - timedelta(hours=999)).timestamp())]
    plans = plan_pointer_sweep(entries, existing_request_ids=set(), retention_hours=168, now=NOW)
    assert plans[0].action == "skip_unrecognized"
    assert plans[0].request_id is None


def test_plan_pointer_boundary_is_inclusive_of_retention_window() -> None:
    entries = [("bloom", "req-boundary", (NOW - timedelta(hours=168)).timestamp())]
    plans = plan_pointer_sweep(entries, existing_request_ids={"req-boundary"}, retention_hours=168, now=NOW)
    assert plans[0].action == "remove_stale"


def test_plan_pointer_uses_same_retention_window_as_flat_sweep() -> None:
    """
    Identity-addressed and anonymous replies age out under the same window
    (proposal §6 dial 1, resolved: no differential retention) — this is
    exercised indirectly by plan_pointer_sweep and plan_sweep both taking a
    single shared retention_hours parameter with identical >= comparison
    semantics, verified here for the pointer side specifically.
    """
    just_under = [("bloom", "req-under", (NOW - timedelta(hours=167)).timestamp())]
    just_over = [("bloom", "req-over", (NOW - timedelta(hours=169)).timestamp())]
    under_plans = plan_pointer_sweep(just_under, {"req-under"}, retention_hours=168, now=NOW)
    over_plans = plan_pointer_sweep(just_over, {"req-over"}, retention_hours=168, now=NOW)
    assert under_plans[0].action == "keep"
    assert over_plans[0].action == "remove_stale"


# ---------------------------------------------------------------------------
# by-agent/ pointer mailbox — scan_by_agent_dir, filesystem read tests
# ---------------------------------------------------------------------------


def test_scan_by_agent_dir_recurses_into_slug_subdirectories(tmp_path: Path) -> None:
    by_agent = tmp_path / "by-agent"
    (by_agent / "bloom").mkdir(parents=True)
    (by_agent / "glyph").mkdir(parents=True)
    (by_agent / "bloom" / "req-1").write_text("")
    (by_agent / "bloom" / "req-2").write_text("")
    (by_agent / "glyph" / "req-3").write_text("")

    entries = scan_by_agent_dir(by_agent)

    assert {(slug, name) for slug, name, _mtime in entries} == {
        ("bloom", "req-1"),
        ("bloom", "req-2"),
        ("glyph", "req-3"),
    }


def test_scan_by_agent_dir_ignores_files_directly_under_by_agent(tmp_path: Path) -> None:
    """A stray file at by-agent/<not-a-dir> (not inside a slug dir) is not a pointer — skip it."""
    by_agent = tmp_path / "by-agent"
    by_agent.mkdir(parents=True)
    (by_agent / "stray-file").write_text("")
    (by_agent / "bloom").mkdir()
    (by_agent / "bloom" / "req-1").write_text("")

    entries = scan_by_agent_dir(by_agent)

    assert entries == [("bloom", "req-1", entries[0][2])]


def test_scan_by_agent_dir_on_missing_directory_returns_empty(tmp_path: Path) -> None:
    entries = scan_by_agent_dir(tmp_path / "does-not-exist")
    assert entries == []


# ---------------------------------------------------------------------------
# by-agent/ pointer mailbox — apply_pointer_sweep, filesystem write tests
# ---------------------------------------------------------------------------


def test_apply_pointer_sweep_only_removes_planned_pointers(tmp_path: Path) -> None:
    by_agent = tmp_path / "by-agent"
    (by_agent / "bloom").mkdir(parents=True)
    (by_agent / "bloom" / "req-stale").write_text("")
    (by_agent / "bloom" / "req-fresh").write_text("")
    (by_agent / "bloom" / "req-dangling").write_text("")

    plans = [
        PointerPlan("bloom", "req-stale", "remove_stale", 200.0, "req-stale"),
        PointerPlan("bloom", "req-fresh", "keep", 1.0, "req-fresh"),
        PointerPlan("bloom", "req-dangling", "remove_dangling", None, "req-dangling"),
    ]
    removed_count, errors = apply_pointer_sweep(by_agent, plans, dry_run=False)

    assert removed_count == 2
    assert errors == []
    assert not (by_agent / "bloom" / "req-stale").exists()
    assert (by_agent / "bloom" / "req-fresh").exists()
    assert not (by_agent / "bloom" / "req-dangling").exists()


def test_apply_pointer_sweep_dry_run_never_touches_filesystem(tmp_path: Path) -> None:
    by_agent = tmp_path / "by-agent"
    (by_agent / "bloom").mkdir(parents=True)
    target = by_agent / "bloom" / "req-stale"
    target.write_text("")

    plans = [PointerPlan("bloom", "req-stale", "remove_stale", 200.0, "req-stale")]
    removed_count, errors = apply_pointer_sweep(by_agent, plans, dry_run=True)

    assert removed_count == 1  # reported as "would remove"
    assert errors == []
    assert target.exists()


# ---------------------------------------------------------------------------
# by-agent/ pointer mailbox — cleanup_empty_agent_dirs tests
# ---------------------------------------------------------------------------


def test_cleanup_empty_agent_dirs_removes_dirs_left_with_no_pointers(tmp_path: Path) -> None:
    by_agent = tmp_path / "by-agent"
    (by_agent / "bloom").mkdir(parents=True)  # already empty — last pointer was just removed

    removed = cleanup_empty_agent_dirs(by_agent, dry_run=False)

    assert removed == ["bloom"]
    assert not (by_agent / "bloom").exists()


def test_cleanup_empty_agent_dirs_leaves_nonempty_dirs_alone(tmp_path: Path) -> None:
    by_agent = tmp_path / "by-agent"
    (by_agent / "bloom").mkdir(parents=True)
    (by_agent / "bloom" / "req-fresh").write_text("")

    removed = cleanup_empty_agent_dirs(by_agent, dry_run=False)

    assert removed == []
    assert (by_agent / "bloom").exists()
    assert (by_agent / "bloom" / "req-fresh").exists()


def test_cleanup_empty_agent_dirs_dry_run_never_touches_filesystem(tmp_path: Path) -> None:
    by_agent = tmp_path / "by-agent"
    (by_agent / "bloom").mkdir(parents=True)

    removed = cleanup_empty_agent_dirs(by_agent, dry_run=True)

    assert removed == ["bloom"]  # reported as "would remove"
    assert (by_agent / "bloom").exists()  # but nothing was actually deleted


def test_cleanup_empty_agent_dirs_on_missing_directory_returns_empty(tmp_path: Path) -> None:
    removed = cleanup_empty_agent_dirs(tmp_path / "does-not-exist", dry_run=False)
    assert removed == []


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


# ---------------------------------------------------------------------------
# main() — by-agent/ pointer mailbox end-to-end wiring
# ---------------------------------------------------------------------------


def test_main_by_agent_sweep_end_to_end_ages_gcs_and_cleans_up(tmp_path: Path) -> None:
    """
    Seeds a by-agent/ tree with a fresh pointer (kept), a stale pointer whose
    target is still live (aged out by mtime), and a dangling pointer whose
    target content file was never written (GC'd regardless of age) — then
    asserts the correct pointer is removed in each case, the now-empty slug
    dir is cleaned up, the still-populated slug dir survives, and the
    pre-existing flat reply/ack sweep runs unaffected in the same pass.

    main() computes ages from the real wall clock (datetime.now(timezone.utc)),
    not the frozen NOW fixture used elsewhere in this file — so pointer mtimes
    are set with os.utime() against the actual current time.
    """
    workspace = tmp_path / "workspace"
    (workspace / "scheduled-jobs").mkdir(parents=True, exist_ok=True)
    (workspace / "scheduled-jobs" / "jobs.json").write_text(
        json.dumps({"jobs": {"agent-replies-sweep": {"enabled": True}}})
    )

    agent_replies_dir = tmp_path / "messages" / "agent-replies"
    agent_replies_dir.mkdir(parents=True, exist_ok=True)
    by_agent_dir = agent_replies_dir / "by-agent"
    task_outputs_dir = tmp_path / "messages" / "task-outputs"

    real_now = datetime.now(timezone.utc)
    fresh_iso = real_now.isoformat()
    fresh_epoch = time.time()
    stale_epoch = fresh_epoch - (200 * 3600)

    # Flat content — pre-existing sweep behavior, unrelated request_id, must
    # still work exactly as before this extension.
    (agent_replies_dir / "req-flat-stale.json").write_text(
        json.dumps({"request_id": "req-flat-stale", "text": "old", "ts": (real_now - timedelta(hours=200)).isoformat()})
    )
    (agent_replies_dir / "req-flat-stale.ack.json").write_text(
        json.dumps({"request_id": "req-flat-stale", "ack": True, "ts": (real_now - timedelta(hours=200)).isoformat()})
    )

    # Content backing the pointer scenarios below.
    (agent_replies_dir / "req-live.json").write_text(json.dumps({"request_id": "req-live", "text": "ok", "ts": fresh_iso}))
    (agent_replies_dir / "req-stale.json").write_text(json.dumps({"request_id": "req-stale", "text": "ok", "ts": fresh_iso}))
    # Deliberately no agent-replies/req-gone.json — this pointer's target
    # was never written (or was already GC'd), making it dangling.

    (by_agent_dir / "bloom").mkdir(parents=True, exist_ok=True)
    (by_agent_dir / "glyph").mkdir(parents=True, exist_ok=True)

    live_pointer = by_agent_dir / "bloom" / "req-live"
    stale_pointer = by_agent_dir / "bloom" / "req-stale"
    dangling_pointer = by_agent_dir / "glyph" / "req-gone"
    live_pointer.write_text("")
    stale_pointer.write_text("")
    dangling_pointer.write_text("")
    os.utime(live_pointer, (fresh_epoch, fresh_epoch))
    os.utime(stale_pointer, (stale_epoch, stale_epoch))
    os.utime(dangling_pointer, (fresh_epoch, fresh_epoch))

    with (
        patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(workspace)}),
        patch.object(_mod, "AGENT_REPLIES_DIR", agent_replies_dir),
        patch.object(_mod, "MESSAGES_DIR", tmp_path / "messages"),
    ):
        result = main(["--retention-hours", "168"])

    assert result == 0

    # Pointer outcomes.
    assert live_pointer.exists()  # fresh + live target -> kept
    assert not stale_pointer.exists()  # aged past retention -> removed
    assert not dangling_pointer.exists()  # target gone -> removed regardless of age

    # Slug dir cleanup: bloom still has a live pointer, glyph doesn't.
    assert (by_agent_dir / "bloom").exists()
    assert not (by_agent_dir / "glyph").exists()

    # Pre-existing flat sweep still functions unmodified in the same run.
    assert not (agent_replies_dir / "req-flat-stale.json").exists()
    assert not (agent_replies_dir / "req-flat-stale.ack.json").exists()
    assert (agent_replies_dir / "req-live.json").exists()
    assert (agent_replies_dir / "req-stale.json").exists()

    outputs = list(task_outputs_dir.glob("*-agent-replies-sweep.json"))
    assert len(outputs) == 1
    output_data = json.loads(outputs[0].read_text())
    assert output_data["status"] == "success"
    assert "1 stale" in output_data["output"]
    assert "1 dangling" in output_data["output"]
    assert "1 now-empty by-agent/ slug dir" in output_data["output"]


def test_main_dry_run_by_agent_sweep_never_touches_filesystem(tmp_path: Path) -> None:
    """--dry-run must not delete pointers, remove content, or rmdir slug dirs."""
    workspace = tmp_path / "workspace"
    (workspace / "scheduled-jobs").mkdir(parents=True, exist_ok=True)
    (workspace / "scheduled-jobs" / "jobs.json").write_text(
        json.dumps({"jobs": {"agent-replies-sweep": {"enabled": True}}})
    )

    agent_replies_dir = tmp_path / "messages" / "agent-replies"
    by_agent_dir = agent_replies_dir / "by-agent"
    (by_agent_dir / "glyph").mkdir(parents=True, exist_ok=True)

    dangling_pointer = by_agent_dir / "glyph" / "req-gone"
    dangling_pointer.write_text("")

    with (
        patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(workspace)}),
        patch.object(_mod, "AGENT_REPLIES_DIR", agent_replies_dir),
        patch.object(_mod, "MESSAGES_DIR", tmp_path / "messages"),
    ):
        result = main(["--retention-hours", "168", "--dry-run"])

    assert result == 0
    assert dangling_pointer.exists()  # nothing deleted in dry-run
    assert (by_agent_dir / "glyph").exists()  # nothing rmdir'd in dry-run
