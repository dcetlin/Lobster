"""
Unit tests for scheduled-tasks/local-session-parser.py.

Tests verify behavior, not implementation details.  All filesystem I/O is
replaced by in-memory fakes.  Named after the behaviors they verify:

  - test_skips_non_assistant_entries
  - test_extracts_token_counts_from_assistant_entry
  - test_missing_timestamp_yields_no_date
  - test_malformed_timestamp_yields_no_date
  - test_aggregates_counts_across_files
  - test_drops_dates_before_cutoff
  - test_tokens_today_sums_input_and_output_for_given_date
  - test_tokens_this_week_sums_since_week_start
  - test_build_cc_usage_shape
  - test_merge_preserves_fresh_poller_rate_limits
  - test_merge_clears_stale_rate_limits
  - test_merge_always_writes_token_usage_section
  - test_merge_sets_source_to_local_parser_when_stale
  - test_poller_data_is_fresh_when_recent
  - test_poller_data_is_stale_when_old
  - test_poller_data_is_stale_when_source_is_local_parser
  - test_discover_returns_empty_list_for_missing_directory
  - test_parse_session_file_handles_read_error_gracefully
  - test_disabled_job_exits_without_parsing
  - test_dry_run_does_not_write_files
"""

from __future__ import annotations

import importlib.util
import json
import os
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the module under test via importlib (lives in scheduled-tasks/, not a package)
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scheduled-tasks"
    / "local-session-parser.py"
)

spec = importlib.util.spec_from_file_location("local_session_parser", SCRIPT_PATH)
_mod: ModuleType = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(_mod)  # type: ignore[union-attr]

# Pull names into local scope.
extract_usage_from_entry = _mod.extract_usage_from_entry
parse_session_file = _mod.parse_session_file
aggregate_daily_counts = _mod.aggregate_daily_counts
compute_tokens_today = _mod.compute_tokens_today
compute_tokens_this_week = _mod.compute_tokens_this_week
build_cc_usage = _mod.build_cc_usage
merge_token_usage_into_state = _mod.merge_token_usage_into_state
discover_session_files = _mod.discover_session_files
_poller_data_is_fresh = _mod._poller_data_is_fresh
main = _mod.main
SOURCE_TAG = _mod.SOURCE_TAG
POLLER_STALE_SECONDS = _mod.POLLER_STALE_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(dt: datetime) -> str:
    """Format a datetime as a Claude JSONL timestamp string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _assistant_entry(
    ts: str,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 200,
    cache_read: int = 1000,
) -> dict:
    """Build a minimal assistant JSONL entry with the given usage fields."""
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            }
        },
    }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# extract_usage_from_entry
# ---------------------------------------------------------------------------


def test_skips_non_assistant_entries():
    for entry_type in ("user", "system", "attachment", "queue-operation"):
        entry = {"type": entry_type, "timestamp": "2026-05-20T12:00:00.000Z"}
        date_str, counts = extract_usage_from_entry(entry)
        assert date_str is None, f"Expected None for type={entry_type}"
        assert all(v == 0 for v in counts.values())


def test_extracts_token_counts_from_assistant_entry():
    now = _now_utc()
    entry = _assistant_entry(
        ts=_ts(now),
        input_tokens=300,
        output_tokens=150,
        cache_creation=400,
        cache_read=2000,
    )
    date_str, counts = extract_usage_from_entry(entry)
    assert date_str == now.strftime("%Y-%m-%d")
    assert counts["input_tokens"] == 300
    assert counts["output_tokens"] == 150
    assert counts["cache_creation"] == 400
    assert counts["cache_read"] == 2000


def test_missing_timestamp_yields_no_date():
    entry = {
        "type": "assistant",
        "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    date_str, counts = extract_usage_from_entry(entry)
    assert date_str is None


def test_malformed_timestamp_yields_no_date():
    entry = {
        "type": "assistant",
        "timestamp": "not-a-date",
        "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    date_str, counts = extract_usage_from_entry(entry)
    assert date_str is None


# ---------------------------------------------------------------------------
# aggregate_daily_counts
# ---------------------------------------------------------------------------


def test_aggregates_counts_across_files():
    file1 = {"2026-05-20": {"input_tokens": 100, "output_tokens": 50, "cache_creation": 0, "cache_read": 0}}
    file2 = {"2026-05-20": {"input_tokens": 200, "output_tokens": 100, "cache_creation": 10, "cache_read": 500}}
    result = aggregate_daily_counts([file1, file2], cutoff_date="2026-05-01")
    assert result["2026-05-20"]["input_tokens"] == 300
    assert result["2026-05-20"]["output_tokens"] == 150
    assert result["2026-05-20"]["cache_creation"] == 10
    assert result["2026-05-20"]["cache_read"] == 500


def test_drops_dates_before_cutoff():
    data = {
        "2026-05-10": {"input_tokens": 100, "output_tokens": 50, "cache_creation": 0, "cache_read": 0},
        "2026-05-20": {"input_tokens": 200, "output_tokens": 100, "cache_creation": 0, "cache_read": 0},
    }
    result = aggregate_daily_counts([data], cutoff_date="2026-05-15")
    assert "2026-05-10" not in result
    assert "2026-05-20" in result


# ---------------------------------------------------------------------------
# compute_tokens_today / compute_tokens_this_week
# ---------------------------------------------------------------------------


def test_tokens_today_sums_input_and_output_for_given_date():
    daily = {
        "2026-05-20": {"input_tokens": 300, "output_tokens": 150, "cache_creation": 999, "cache_read": 9999},
    }
    assert compute_tokens_today(daily, "2026-05-20") == 450
    assert compute_tokens_today(daily, "2026-05-21") == 0


def test_tokens_this_week_sums_since_week_start():
    daily = {
        "2026-05-18": {"input_tokens": 100, "output_tokens": 50, "cache_creation": 0, "cache_read": 0},  # Sunday (before)
        "2026-05-19": {"input_tokens": 200, "output_tokens": 100, "cache_creation": 0, "cache_read": 0},  # Monday (week start)
        "2026-05-20": {"input_tokens": 300, "output_tokens": 150, "cache_creation": 0, "cache_read": 0},  # Tuesday
    }
    total = compute_tokens_this_week(daily, week_start="2026-05-19")
    assert total == (200 + 100) + (300 + 150)  # 750, excludes Sunday


# ---------------------------------------------------------------------------
# build_cc_usage
# ---------------------------------------------------------------------------


def test_build_cc_usage_shape():
    daily = {
        "2026-05-20": {"input_tokens": 100, "output_tokens": 50, "cache_creation": 200, "cache_read": 1000},
    }
    result = build_cc_usage(
        daily=daily,
        today="2026-05-20",
        week_start="2026-05-19",
        now_iso="2026-05-20T12:00:00+00:00",
    )
    assert result["tokens_today"] == 150  # 100 + 50
    assert result["tokens_this_week"] == 150
    assert "week_start" in result
    assert "daily" in result
    assert "last_updated" in result


# ---------------------------------------------------------------------------
# _poller_data_is_fresh
# ---------------------------------------------------------------------------


def test_poller_data_is_fresh_when_recent():
    recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    state = {
        "source": "cc-usage-poller",
        "last_updated": recent_ts,
        "rate_limits": {"five_hour": {"pct": 20.0}},
    }
    assert _poller_data_is_fresh(state) is True


def test_poller_data_is_stale_when_old():
    old_ts = (
        datetime.now(timezone.utc) - timedelta(seconds=POLLER_STALE_SECONDS + 600)
    ).isoformat()
    state = {
        "source": "cc-usage-poller",
        "last_updated": old_ts,
    }
    assert _poller_data_is_fresh(state) is False


def test_poller_data_is_stale_when_source_is_local_parser():
    # If state was already written by this parser, it's not "fresh poller data"
    recent_ts = datetime.now(timezone.utc).isoformat()
    state = {
        "source": SOURCE_TAG,
        "last_updated": recent_ts,
    }
    assert _poller_data_is_fresh(state) is False


def test_poller_data_is_stale_when_last_updated_absent():
    state = {"source": "cc-usage-poller"}
    assert _poller_data_is_fresh(state) is False


# ---------------------------------------------------------------------------
# merge_token_usage_into_state
# ---------------------------------------------------------------------------

FRESH_TS = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
STALE_TS = (
    datetime.now(timezone.utc) - timedelta(seconds=POLLER_STALE_SECONDS + 7200)
).isoformat()

DAILY_SAMPLE = {
    "2026-05-20": {"input_tokens": 100, "output_tokens": 50, "cache_creation": 0, "cache_read": 0},
}


def test_merge_preserves_fresh_poller_rate_limits():
    existing = {
        "source": "cc-usage-poller",
        "last_updated": FRESH_TS,
        "rate_limits": {
            "five_hour": {"pct": 42.0, "resets_at": "2026-05-20T18:00:00Z"},
            "seven_day": {"pct": 28.0, "resets_at": "2026-05-25T12:00:00Z"},
        },
    }
    result = merge_token_usage_into_state(
        existing=existing,
        daily=DAILY_SAMPLE,
        tokens_today=150,
        tokens_this_week=150,
        five_hour_tokens=50,
        week_start="2026-05-19",
        now_iso="2026-05-20T12:00:00+00:00",
        now_unix=1716206400,
    )
    # Poller data must be preserved
    assert result["rate_limits"]["five_hour"]["pct"] == 42.0
    assert result["rate_limits"]["seven_day"]["pct"] == 28.0
    # Local token usage is still added
    assert "token_usage" in result
    assert result["token_usage"]["tokens_today"] == 150


def test_merge_clears_stale_rate_limits():
    existing = {
        "source": "cc-usage-poller",
        "last_updated": STALE_TS,
        "rate_limits": {
            "five_hour": {"pct": 42.0, "resets_at": "2026-05-10T18:00:00Z"},
            "seven_day": {"pct": 28.0, "resets_at": "2026-05-17T12:00:00Z"},
        },
    }
    result = merge_token_usage_into_state(
        existing=existing,
        daily=DAILY_SAMPLE,
        tokens_today=150,
        tokens_this_week=150,
        five_hour_tokens=50,
        week_start="2026-05-19",
        now_iso="2026-05-20T12:00:00+00:00",
        now_unix=1716206400,
    )
    # Stale pct values must be cleared to None so the quota gate treats them as unavailable
    assert result["rate_limits"]["five_hour"]["pct"] is None
    assert result["rate_limits"]["seven_day"]["pct"] is None


def test_merge_always_writes_token_usage_section():
    for existing in [{}, {"source": "cc-usage-poller", "last_updated": FRESH_TS}]:
        result = merge_token_usage_into_state(
            existing=existing,
            daily=DAILY_SAMPLE,
            tokens_today=100,
            tokens_this_week=200,
            five_hour_tokens=30,
            week_start="2026-05-19",
            now_iso="2026-05-20T12:00:00+00:00",
            now_unix=1716206400,
        )
        assert "token_usage" in result, f"token_usage absent for existing={existing!r}"
        assert result["token_usage"]["five_hour_tokens"] == 30


def test_merge_sets_source_to_local_parser_when_stale():
    existing = {
        "source": "cc-usage-poller",
        "last_updated": STALE_TS,
    }
    result = merge_token_usage_into_state(
        existing=existing,
        daily=DAILY_SAMPLE,
        tokens_today=100,
        tokens_this_week=200,
        five_hour_tokens=30,
        week_start="2026-05-19",
        now_iso="2026-05-20T12:00:00+00:00",
        now_unix=1716206400,
    )
    assert result["source"] == SOURCE_TAG


# ---------------------------------------------------------------------------
# discover_session_files
# ---------------------------------------------------------------------------


def test_discover_returns_empty_list_for_missing_directory():
    result = discover_session_files(Path("/does/not/exist/anywhere"))
    assert result == []


def test_discover_finds_jsonl_files(tmp_path):
    (tmp_path / "abc.jsonl").write_text("")
    (tmp_path / "def.jsonl").write_text("")
    (tmp_path / "ignored.txt").write_text("")
    result = discover_session_files(tmp_path)
    assert len(result) == 2
    assert all(p.suffix == ".jsonl" for p in result)


# ---------------------------------------------------------------------------
# parse_session_file
# ---------------------------------------------------------------------------


def test_parse_session_file_handles_read_error_gracefully(tmp_path):
    # File does not exist — should return {} without raising
    result = parse_session_file(tmp_path / "nonexistent.jsonl")
    assert result == {}


def test_parse_session_file_aggregates_assistant_entries(tmp_path):
    now = _now_utc()
    today = now.strftime("%Y-%m-%d")
    lines = [
        json.dumps(_assistant_entry(_ts(now), input_tokens=100, output_tokens=50)),
        json.dumps(_assistant_entry(_ts(now), input_tokens=200, output_tokens=100)),
        json.dumps({"type": "user", "timestamp": _ts(now)}),  # non-assistant, skip
        "NOT VALID JSON",  # malformed, skip
    ]
    f = tmp_path / "session.jsonl"
    f.write_text("\n".join(lines) + "\n")
    result = parse_session_file(f)
    assert result[today]["input_tokens"] == 300
    assert result[today]["output_tokens"] == 150


# ---------------------------------------------------------------------------
# main() — integration-level behavior tests
# ---------------------------------------------------------------------------


def test_disabled_job_exits_without_parsing(tmp_path):
    with patch.object(_mod, "is_job_enabled", return_value=False):
        with patch.object(_mod, "discover_session_files") as mock_discover:
            ret = main(dry_run=False)
    assert ret == 0
    mock_discover.assert_not_called()


def test_dry_run_does_not_write_files(tmp_path):
    """Dry-run must log intent but not create or modify any files."""
    now = _now_utc()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        json.dumps(_assistant_entry(_ts(now), input_tokens=100, output_tokens=50)) + "\n"
    )

    cc_usage_out = tmp_path / "cc-usage.json"
    state_out = tmp_path / "state.json"

    with patch.object(_mod, "is_job_enabled", return_value=True):
        with patch.dict(
            os.environ,
            {
                "LOBSTER_SESSION_DIR": str(tmp_path),
                "LOBSTER_CC_USAGE_PATH": str(cc_usage_out),
                "LOBSTER_CC_BUDGET_STATE": str(state_out),
            },
        ):
            ret = main(dry_run=True)

    assert ret == 0
    assert not cc_usage_out.exists(), "dry-run must not write cc-usage.json"
    assert not state_out.exists(), "dry-run must not write state.json"


def test_main_writes_cc_usage_and_state_files(tmp_path):
    """Full run with valid session files produces both output files."""
    now = _now_utc()
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        json.dumps(_assistant_entry(_ts(now), input_tokens=100, output_tokens=50)) + "\n"
    )

    cc_usage_out = tmp_path / "cc-usage.json"
    state_dir = tmp_path / "state_dir"
    state_out = state_dir / "state.json"

    with patch.object(_mod, "is_job_enabled", return_value=True):
        with patch.dict(
            os.environ,
            {
                "LOBSTER_SESSION_DIR": str(tmp_path),
                "LOBSTER_CC_USAGE_PATH": str(cc_usage_out),
                "LOBSTER_CC_BUDGET_STATE": str(state_out),
            },
        ):
            ret = main(dry_run=False)

    assert ret == 0
    assert cc_usage_out.exists(), "cc-usage.json must be written"
    assert state_out.exists(), "state.json must be written"

    cc_usage = json.loads(cc_usage_out.read_text())
    assert cc_usage["tokens_today"] == 150  # 100 + 50
    assert "daily" in cc_usage

    state = json.loads(state_out.read_text())
    assert "token_usage" in state
    assert state["token_usage"]["tokens_today"] == 150
