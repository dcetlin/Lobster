"""
Unit tests for scheduled-tasks/agent-channel-abandonment-sweep.py — the
sliding-timer abandonment auto-complete guardrail for the agent channel
(source="local-claude"), issue #1525.

Named after behaviors:
  - test_resolve_last_signal_is_max_of_available_signals
  - test_resolve_last_signal_none_when_no_signals
  - test_plan_auto_completes_when_deadline_lapsed_with_no_signal
  - test_plan_keeps_fresh_exchange_within_window
  - test_plan_skips_exchange_that_already_has_a_terminal_reply
  - test_plan_does_not_auto_complete_with_no_resolvable_signal
  - test_ack_write_progress_call_resets_the_deadline (timer resets on progress)
  - test_reclaim_with_fresh_claimed_at_resets_the_deadline (reclaimer active)
  - test_stale_claim_with_no_reclaimer_auto_completes (the guardrail's actual job)
  - test_apply_writes_synthetic_reply_for_auto_complete_plans
  - test_apply_never_writes_for_non_auto_complete_plans
  - test_apply_lost_race_to_concurrent_real_reply_is_not_an_error
  - test_apply_dry_run_never_touches_filesystem
  - test_scan_ack_candidates_only_matches_ack_suffix
  - test_scan_existing_replies_only_matches_reply_suffix_not_ack
  - test_scan_processing_candidates_filters_by_source
  - test_scan_processing_candidates_ignores_non_local_claude_source
  - test_window_hours_env_var_override
  - test_window_hours_cli_override_takes_precedence_over_env
  - test_window_below_floor_is_clamped_up
  - test_disabled_job_exits_without_scanning
  - test_main_auto_completes_abandoned_exchange_end_to_end
  - test_main_does_not_touch_fresh_exchange_end_to_end
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Load the script under test via importlib (it lives in scheduled-tasks/,
# not a package, and the filename contains hyphens) — same pattern as
# test_agent_replies_sweep.py.
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scheduled-tasks"
    / "agent-channel-abandonment-sweep.py"
)

spec = importlib.util.spec_from_file_location("agent_channel_abandonment_sweep", SCRIPT_PATH)
_mod: ModuleType = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = _mod  # required for dataclass field resolution under `from __future__ import annotations`
spec.loader.exec_module(_mod)  # type: ignore[union-attr]

CandidateSignals = _mod.CandidateSignals
AbandonmentPlan = _mod.AbandonmentPlan
resolve_last_signal = _mod.resolve_last_signal
plan_abandonment_sweep = _mod.plan_abandonment_sweep
apply_abandonment_sweep = _mod.apply_abandonment_sweep
scan_ack_candidates = _mod.scan_ack_candidates
scan_existing_replies = _mod.scan_existing_replies
scan_processing_candidates = _mod.scan_processing_candidates
build_candidates = _mod.build_candidates
_resolve_window_hours = _mod._resolve_window_hours
main = _mod.main
DEFAULT_ABANDONMENT_WINDOW_HOURS = _mod.DEFAULT_ABANDONMENT_WINDOW_HOURS
MIN_ABANDONMENT_WINDOW_HOURS = _mod.MIN_ABANDONMENT_WINDOW_HOURS

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src" / "mcp"))
from claims import AtomicClaimDB  # noqa: E402

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_HOURS = 24.0


# ---------------------------------------------------------------------------
# resolve_last_signal — pure function tests
# ---------------------------------------------------------------------------


def test_resolve_last_signal_is_max_of_available_signals() -> None:
    earlier = NOW - timedelta(hours=10)
    later = NOW - timedelta(hours=1)
    assert resolve_last_signal(CandidateSignals("r1", ack_ts=later, claimed_at_ts=earlier)) == later
    assert resolve_last_signal(CandidateSignals("r1", ack_ts=earlier, claimed_at_ts=later)) == later


def test_resolve_last_signal_none_when_no_signals() -> None:
    assert resolve_last_signal(CandidateSignals("r1", ack_ts=None, claimed_at_ts=None)) is None


# ---------------------------------------------------------------------------
# plan_abandonment_sweep — pure decision function tests
# ---------------------------------------------------------------------------


def test_plan_auto_completes_when_deadline_lapsed_with_no_signal() -> None:
    stale = NOW - timedelta(hours=WINDOW_HOURS + 1)
    candidates = [CandidateSignals("r1", ack_ts=stale, claimed_at_ts=None)]
    plans = plan_abandonment_sweep(candidates, reply_exists=set(), window_hours=WINDOW_HOURS, now=NOW)
    assert plans[0].action == "auto_complete"


def test_plan_keeps_fresh_exchange_within_window() -> None:
    fresh = NOW - timedelta(hours=1)
    candidates = [CandidateSignals("r1", ack_ts=fresh, claimed_at_ts=None)]
    plans = plan_abandonment_sweep(candidates, reply_exists=set(), window_hours=WINDOW_HOURS, now=NOW)
    assert plans[0].action == "keep_fresh"


def test_plan_skips_exchange_that_already_has_a_terminal_reply() -> None:
    stale = NOW - timedelta(hours=WINDOW_HOURS + 1)
    candidates = [CandidateSignals("r1", ack_ts=stale, claimed_at_ts=None)]
    plans = plan_abandonment_sweep(candidates, reply_exists={"r1"}, window_hours=WINDOW_HOURS, now=NOW)
    assert plans[0].action == "already_complete"


def test_plan_does_not_auto_complete_with_no_resolvable_signal() -> None:
    candidates = [CandidateSignals("r1", ack_ts=None, claimed_at_ts=None)]
    plans = plan_abandonment_sweep(candidates, reply_exists=set(), window_hours=WINDOW_HOURS, now=NOW)
    assert plans[0].action == "no_signal"


# ---------------------------------------------------------------------------
# The three core acceptance behaviors (issue #1525)
# ---------------------------------------------------------------------------


def test_ack_write_progress_call_resets_the_deadline() -> None:
    """
    A write_progress call overwrites .ack.json, moving its ts forward — the
    sliding timer must reflect that, not the original claim time.
    """
    original_claim = NOW - timedelta(hours=30)  # older than the window on its own
    recent_progress = NOW - timedelta(hours=1)  # well within the window
    candidates = [CandidateSignals("r1", ack_ts=recent_progress, claimed_at_ts=original_claim)]
    plans = plan_abandonment_sweep(candidates, reply_exists=set(), window_hours=WINDOW_HOURS, now=NOW)
    assert plans[0].action == "keep_fresh"
    assert plans[0].last_signal_ts == recent_progress


def test_reclaim_with_fresh_claimed_at_resets_the_deadline() -> None:
    """
    A stale .ack.json from the original (crashed) claimant, but a fresh
    claimed_at from a new claimant that reclaimed the exchange — the new
    claim's recency must win (max), so an active reclaimer is never
    auto-completed out from under it.
    """
    stale_ack = NOW - timedelta(hours=WINDOW_HOURS + 5)
    fresh_reclaim = NOW - timedelta(minutes=5)
    candidates = [CandidateSignals("r1", ack_ts=stale_ack, claimed_at_ts=fresh_reclaim)]
    plans = plan_abandonment_sweep(candidates, reply_exists=set(), window_hours=WINDOW_HOURS, now=NOW)
    assert plans[0].action == "keep_fresh"


def test_stale_claim_with_no_reclaimer_auto_completes() -> None:
    """
    The actual gap this guardrail closes: claimant crashed, claim was
    released back to inbox/ by the 600s reclaim timeout (so claimed_at_ts is
    None — the claim row is gone), nobody ever re-claimed it, and the last
    .ack.json write is older than the abandonment window.
    """
    stale_ack = NOW - timedelta(hours=WINDOW_HOURS + 1)
    candidates = [CandidateSignals("r1", ack_ts=stale_ack, claimed_at_ts=None)]
    plans = plan_abandonment_sweep(candidates, reply_exists=set(), window_hours=WINDOW_HOURS, now=NOW)
    assert plans[0].action == "auto_complete"


# ---------------------------------------------------------------------------
# apply_abandonment_sweep — side-effecting write behavior
# ---------------------------------------------------------------------------


def test_apply_writes_synthetic_reply_for_auto_complete_plans(tmp_path: Path) -> None:
    plans = [AbandonmentPlan("r1", "auto_complete", NOW - timedelta(hours=30), 30.0)]
    count, errors = apply_abandonment_sweep(tmp_path, plans, WINDOW_HOURS, dry_run=False)
    assert count == 1
    assert errors == []
    written = json.loads((tmp_path / "r1.json").read_text())
    assert written["request_id"] == "r1"
    assert "timed out" in written["text"].lower()


def test_apply_never_writes_for_non_auto_complete_plans(tmp_path: Path) -> None:
    plans = [
        AbandonmentPlan("r1", "keep_fresh", NOW, 1.0),
        AbandonmentPlan("r2", "already_complete", None, None),
        AbandonmentPlan("r3", "no_signal", None, None),
    ]
    count, errors = apply_abandonment_sweep(tmp_path, plans, WINDOW_HOURS, dry_run=False)
    assert count == 0
    assert errors == []
    assert list(tmp_path.glob("*.json")) == []


def test_apply_lost_race_to_concurrent_real_reply_is_not_an_error(tmp_path: Path) -> None:
    """
    write_reply() is first-writer-wins (atomic_create_json). If a real reply
    already landed for this request_id before apply runs, the synthetic
    write must be a silent no-op, not an error, and must not clobber the
    real answer.
    """
    (tmp_path / "r1.json").write_text(json.dumps({"request_id": "r1", "text": "the real answer", "ts": NOW.isoformat(), "in_reply_to": "r1"}))
    plans = [AbandonmentPlan("r1", "auto_complete", NOW - timedelta(hours=30), 30.0)]
    count, errors = apply_abandonment_sweep(tmp_path, plans, WINDOW_HOURS, dry_run=False)
    assert count == 0
    assert errors == []
    assert json.loads((tmp_path / "r1.json").read_text())["text"] == "the real answer"


def test_apply_dry_run_never_touches_filesystem(tmp_path: Path) -> None:
    plans = [AbandonmentPlan("r1", "auto_complete", NOW - timedelta(hours=30), 30.0)]
    count, errors = apply_abandonment_sweep(tmp_path, plans, WINDOW_HOURS, dry_run=True)
    assert count == 1
    assert list(tmp_path.glob("*.json")) == []


# ---------------------------------------------------------------------------
# scan_* — candidate discovery
# ---------------------------------------------------------------------------


def test_scan_ack_candidates_only_matches_ack_suffix(tmp_path: Path) -> None:
    (tmp_path / "r1.ack.json").write_text(json.dumps({"ts": NOW.isoformat()}))
    (tmp_path / "r2.json").write_text(json.dumps({"ts": NOW.isoformat()}))  # a reply, not an ack
    (tmp_path / "not-a-request-id!.ack.json").write_text("{}")  # fails the charset allowlist
    found = scan_ack_candidates(tmp_path)
    assert set(found.keys()) == {"r1"}


def test_scan_existing_replies_only_matches_reply_suffix_not_ack(tmp_path: Path) -> None:
    (tmp_path / "r1.json").write_text(json.dumps({"text": "done"}))
    (tmp_path / "r2.ack.json").write_text(json.dumps({"ack": True}))
    found = scan_existing_replies(tmp_path)
    assert found == {"r1"}


def test_scan_processing_candidates_filters_by_source(tmp_path: Path) -> None:
    (tmp_path / "msg1.json").write_text(json.dumps({"source": "local-claude", "request_id": "r1"}))
    found = scan_processing_candidates(tmp_path)
    assert found == {"r1"}


def test_scan_processing_candidates_ignores_non_local_claude_source(tmp_path: Path) -> None:
    (tmp_path / "msg1.json").write_text(json.dumps({"source": "telegram", "id": "tg-1"}))
    found = scan_processing_candidates(tmp_path)
    assert found == set()


# ---------------------------------------------------------------------------
# build_candidates — targeted (never bulk) claims DB lookup
# ---------------------------------------------------------------------------


def test_build_candidates_queries_claims_db_only_for_discovered_ids(tmp_path: Path) -> None:
    claims_db = AtomicClaimDB(path=tmp_path / "claims.db")
    claims_db.claim("r1", "some-session")
    claims_db.claim("r-unrelated-telegram-claim", "some-session")  # must never surface as a candidate

    ack_candidates = {"r1": ({"ts": NOW.isoformat()}, NOW.timestamp())}
    candidates = build_candidates(ack_candidates, processing_request_ids=set(), claims_db=claims_db)

    assert [c.request_id for c in candidates] == ["r1"]
    assert candidates[0].claimed_at_ts is not None


# ---------------------------------------------------------------------------
# Window resolution — env var, CLI override, floor clamp
# ---------------------------------------------------------------------------


def test_window_hours_env_var_override() -> None:
    with patch.dict("os.environ", {_mod.ABANDONMENT_WINDOW_HOURS_ENV: "48"}):
        assert _resolve_window_hours(None) == 48.0


def test_window_hours_cli_override_takes_precedence_over_env() -> None:
    with patch.dict("os.environ", {_mod.ABANDONMENT_WINDOW_HOURS_ENV: "48"}):
        assert _resolve_window_hours(12.0) == 12.0


def test_window_below_floor_is_clamped_up() -> None:
    assert _resolve_window_hours(0.01) == MIN_ABANDONMENT_WINDOW_HOURS


def test_default_window_is_used_absent_cli_and_env() -> None:
    with patch.dict("os.environ", {}, clear=False):
        import os as _os

        _os.environ.pop(_mod.ABANDONMENT_WINDOW_HOURS_ENV, None)
        assert _resolve_window_hours(None) == DEFAULT_ABANDONMENT_WINDOW_HOURS


# ---------------------------------------------------------------------------
# main() — end-to-end wiring
# ---------------------------------------------------------------------------


def test_disabled_job_exits_without_scanning(tmp_path: Path) -> None:
    jobs_json = tmp_path / "scheduled-jobs" / "jobs.json"
    jobs_json.parent.mkdir(parents=True, exist_ok=True)
    jobs_json.write_text(json.dumps({"jobs": {_mod.JOB_NAME: {"enabled": False}}}))

    agent_replies_dir = tmp_path / "agent-replies"
    agent_replies_dir.mkdir(exist_ok=True)
    stale_ts = (NOW - timedelta(hours=WINDOW_HOURS + 1)).isoformat()
    (agent_replies_dir / "r1.ack.json").write_text(json.dumps({"request_id": "r1", "ack": True, "ts": stale_ts}))

    with (
        patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(tmp_path)}),
        patch.object(_mod, "AGENT_REPLIES_DIR", agent_replies_dir),
    ):
        result = main([])

    assert result == 0
    assert not (agent_replies_dir / "r1.json").exists()  # untouched — job never ran


def test_main_auto_completes_abandoned_exchange_end_to_end(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "scheduled-jobs").mkdir(parents=True, exist_ok=True)
    (workspace / "scheduled-jobs" / "jobs.json").write_text(
        json.dumps({"jobs": {_mod.JOB_NAME: {"enabled": True}}})
    )

    messages_dir = tmp_path / "messages"
    agent_replies_dir = messages_dir / "agent-replies"
    processing_dir = messages_dir / "processing"
    agent_replies_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)

    # Timestamps computed relative to real wall-clock time (not the fixed
    # NOW constant) so main()'s own datetime.now(timezone.utc) call needs no
    # mocking — hermetic without patching stdlib datetime.
    real_now = datetime.now(timezone.utc)
    stale_ts = (real_now - timedelta(hours=48)).isoformat()
    (agent_replies_dir / "r-abandoned.ack.json").write_text(
        json.dumps({"request_id": "r-abandoned", "ack": True, "ts": stale_ts})
    )

    with (
        patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(workspace)}),
        patch.object(_mod, "AGENT_REPLIES_DIR", agent_replies_dir),
        patch.object(_mod, "PROCESSING_DIR", processing_dir),
        patch.object(_mod, "MESSAGES_DIR", messages_dir),
        patch.object(_mod, "CLAIMS_DB_PATH", tmp_path / "claims.db"),
    ):
        result = main(["--window-hours", str(WINDOW_HOURS)])

    assert result == 0
    written = json.loads((agent_replies_dir / "r-abandoned.json").read_text())
    assert written["request_id"] == "r-abandoned"
    assert (workspace / "scheduled-jobs").exists()


def test_main_does_not_touch_fresh_exchange_end_to_end(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "scheduled-jobs").mkdir(parents=True, exist_ok=True)
    (workspace / "scheduled-jobs" / "jobs.json").write_text(
        json.dumps({"jobs": {_mod.JOB_NAME: {"enabled": True}}})
    )

    messages_dir = tmp_path / "messages"
    agent_replies_dir = messages_dir / "agent-replies"
    processing_dir = messages_dir / "processing"
    agent_replies_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)

    fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (agent_replies_dir / "r-fresh.ack.json").write_text(
        json.dumps({"request_id": "r-fresh", "ack": True, "ts": fresh_ts})
    )

    with (
        patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(workspace)}),
        patch.object(_mod, "AGENT_REPLIES_DIR", agent_replies_dir),
        patch.object(_mod, "PROCESSING_DIR", processing_dir),
        patch.object(_mod, "MESSAGES_DIR", messages_dir),
        patch.object(_mod, "CLAIMS_DB_PATH", tmp_path / "claims.db"),
    ):
        result = main(["--window-hours", str(WINDOW_HOURS)])

    assert result == 0
    assert not (agent_replies_dir / "r-fresh.json").exists()
