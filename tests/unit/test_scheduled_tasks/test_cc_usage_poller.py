"""
Unit tests for scheduled-tasks/cc-usage-poller.py.

Tests are named after the behaviors they verify, not the implementation
mechanisms. All network I/O and filesystem writes are absent from pure
function tests — behaviors are isolated using in-memory data.

Named after behaviors:
  - test_skips_run_when_cookie_file_absent
  - test_skips_run_when_cookie_contains_only_comments
  - test_reads_cookie_from_first_non_comment_line
  - test_parse_standard_rate_limits_response
  - test_parse_response_with_alternate_used_percentage_key
  - test_parse_response_raises_when_rate_limits_absent
  - test_merge_preserves_existing_snapshots_and_cost
  - test_merge_overwrites_rate_limits_and_timestamps
  - test_merge_sets_source_tag_to_poller
  - test_disabled_job_exits_without_polling
  - test_auth_error_returns_0_not_1
  - test_network_error_returns_0_not_1
  - test_parse_error_returns_0_not_1
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import textwrap
import urllib.error
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load the script under test via importlib (it lives in scheduled-tasks/,
# not a package). The script uses only stdlib — no stubs needed.
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scheduled-tasks"
    / "cc-usage-poller.py"
)

spec = importlib.util.spec_from_file_location("cc_usage_poller", SCRIPT_PATH)
_mod: ModuleType = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(_mod)  # type: ignore[union-attr]

# Pull names into local scope.
read_session_cookie = _mod.read_session_cookie
fetch_usage = _mod.fetch_usage
parse_usage_response = _mod.parse_usage_response
merge_into_state = _mod.merge_into_state
write_state_atomically = _mod.write_state_atomically
load_existing_state = _mod.load_existing_state
main = _mod.main
SOURCE_TAG = _mod.SOURCE_TAG

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STANDARD_API_RESPONSE = {
    "rate_limits": {
        "five_hour": {"used_percentage": 24.5, "resets_at": 1712345678},
        "seven_day": {"used_percentage": 31.2, "resets_at": 1712987654},
    }
}

EXISTING_STATE = {
    "v": 1,
    "ts": 1777859892,
    "rate_limits": {
        "five_hour": {"pct": 9, "resets_at": 1777873200},
        "seven_day": {"pct": 57.0, "resets_at": 1778256000},
    },
    "session_cost_usd": 1.75,
    "snapshots": {
        "abc123": {
            "five_hour_pct": 10,
            "seven_day_pct": 41,
            "session_cost_usd": 0.31,
            "ts": 1777824895000,
        }
    },
}


# ---------------------------------------------------------------------------
# read_session_cookie — pure function tests
# ---------------------------------------------------------------------------


def test_skips_run_when_cookie_file_absent(tmp_path: Path) -> None:
    """Missing cookie file returns None — treated as graceful unconfigured state."""
    nonexistent = tmp_path / "cc-usage-session-cookie"
    assert read_session_cookie(nonexistent) is None


def test_skips_run_when_cookie_contains_only_comments(tmp_path: Path) -> None:
    """Cookie file with only comment lines returns None."""
    cookie_file = tmp_path / "cc-usage-session-cookie"
    cookie_file.write_text(
        textwrap.dedent("""\
            # Paste your claude.ai session cookie here
            # Get it from: DevTools → Application → Cookies → sessionKey
        """)
    )
    assert read_session_cookie(cookie_file) is None


def test_skips_run_when_cookie_file_is_empty(tmp_path: Path) -> None:
    """Empty cookie file returns None."""
    cookie_file = tmp_path / "cc-usage-session-cookie"
    cookie_file.write_text("")
    assert read_session_cookie(cookie_file) is None


def test_reads_cookie_from_first_non_comment_line(tmp_path: Path) -> None:
    """Valid cookie value is returned from the first non-comment line."""
    cookie_file = tmp_path / "cc-usage-session-cookie"
    cookie_file.write_text(
        textwrap.dedent("""\
            # Paste your claude.ai session cookie here
            sk-ant-session-abc123xyz
        """)
    )
    assert read_session_cookie(cookie_file) == "sk-ant-session-abc123xyz"


def test_reads_cookie_ignoring_leading_whitespace(tmp_path: Path) -> None:
    """Cookie value is stripped of leading/trailing whitespace."""
    cookie_file = tmp_path / "cc-usage-session-cookie"
    cookie_file.write_text("  sk-ant-session-whitespace  \n")
    assert read_session_cookie(cookie_file) == "sk-ant-session-whitespace"


# ---------------------------------------------------------------------------
# parse_usage_response — pure function tests
# ---------------------------------------------------------------------------


def test_parse_standard_rate_limits_response() -> None:
    """Standard API response with five_hour and seven_day is parsed correctly."""
    result = parse_usage_response(STANDARD_API_RESPONSE)
    assert result["five_hour_pct"] == 24.5
    assert result["seven_day_pct"] == 31.2
    assert result["five_hour_resets_at"] == 1712345678
    assert result["seven_day_resets_at"] == 1712987654


def test_parse_response_with_used_percentage_key() -> None:
    """'used_percentage' key in API response maps to *_pct in parsed output."""
    response = {
        "rate_limits": {
            "five_hour": {"used_percentage": 50.0, "resets_at": 999},
            "seven_day": {"used_percentage": 75.0, "resets_at": 888},
        }
    }
    result = parse_usage_response(response)
    assert result["five_hour_pct"] == 50.0
    assert result["seven_day_pct"] == 75.0


def test_parse_response_with_pct_key_fallback() -> None:
    """If 'used_percentage' is absent, 'pct' key is accepted as fallback."""
    response = {
        "rate_limits": {
            "five_hour": {"pct": 33.0, "resets_at": 111},
            "seven_day": {"pct": 66.0, "resets_at": 222},
        }
    }
    result = parse_usage_response(response)
    assert result["five_hour_pct"] == 33.0
    assert result["seven_day_pct"] == 66.0


def test_parse_response_raises_when_rate_limits_absent() -> None:
    """Response without 'rate_limits' key raises ValueError with a descriptive message."""
    with pytest.raises(ValueError, match="rate_limits"):
        parse_usage_response({"some_other_key": "value"})


def test_parse_response_tolerates_missing_five_hour() -> None:
    """If five_hour is absent from rate_limits, pct is None rather than raising."""
    response = {
        "rate_limits": {
            "seven_day": {"used_percentage": 42.0, "resets_at": 333},
        }
    }
    result = parse_usage_response(response)
    assert result["five_hour_pct"] is None
    assert result["seven_day_pct"] == 42.0


# ---------------------------------------------------------------------------
# merge_into_state — pure function tests
# ---------------------------------------------------------------------------

PARSED_USAGE = {
    "five_hour_pct": 24.5,
    "five_hour_resets_at": 1712345678,
    "seven_day_pct": 31.2,
    "seven_day_resets_at": 1712987654,
}


def test_merge_preserves_existing_snapshots_and_cost() -> None:
    """Merge does not destroy session snapshots or cost data from the hook-written state."""
    result = merge_into_state(EXISTING_STATE, PARSED_USAGE)
    assert result["snapshots"] == EXISTING_STATE["snapshots"]
    assert result["session_cost_usd"] == EXISTING_STATE["session_cost_usd"]


def test_merge_overwrites_rate_limits_with_fresh_data() -> None:
    """Rate limit percentages and resets_at are replaced with poller-fetched values."""
    result = merge_into_state(EXISTING_STATE, PARSED_USAGE)
    assert result["rate_limits"]["five_hour"]["pct"] == 24.5
    assert result["rate_limits"]["seven_day"]["pct"] == 31.2
    assert result["rate_limits"]["five_hour"]["resets_at"] == 1712345678
    assert result["rate_limits"]["seven_day"]["resets_at"] == 1712987654


def test_merge_sets_source_tag_to_poller() -> None:
    """Merged state carries source='cc-usage-poller' to distinguish from hook writes."""
    result = merge_into_state(EXISTING_STATE, PARSED_USAGE)
    assert result["source"] == SOURCE_TAG


def test_merge_sets_last_updated_iso_timestamp() -> None:
    """Merged state carries last_updated as an ISO 8601 UTC string."""
    result = merge_into_state(EXISTING_STATE, PARSED_USAGE)
    assert "last_updated" in result
    # Must be parseable as ISO 8601
    dt = datetime.fromisoformat(result["last_updated"])
    assert dt.tzinfo is not None


def test_merge_updates_ts_unix_timestamp() -> None:
    """Merged state has ts updated to approximately now (within 5 seconds)."""
    import time
    before = int(time.time())
    result = merge_into_state(EXISTING_STATE, PARSED_USAGE)
    after = int(time.time())
    assert before <= result["ts"] <= after


def test_merge_on_empty_existing_state() -> None:
    """Merge works correctly when called with an empty existing state (first run)."""
    result = merge_into_state({}, PARSED_USAGE)
    assert result["v"] == 1
    assert result["rate_limits"]["five_hour"]["pct"] == 24.5
    assert result["source"] == SOURCE_TAG


# ---------------------------------------------------------------------------
# main() integration tests — mock all I/O
# ---------------------------------------------------------------------------


def test_disabled_job_exits_without_polling(tmp_path: Path) -> None:
    """main() returns 0 without hitting the network when job is disabled in jobs.json."""
    jobs_json = tmp_path / "scheduled-jobs" / "jobs.json"
    jobs_json.parent.mkdir(parents=True)
    jobs_json.write_text(json.dumps({
        "jobs": {
            "cc-usage-poller": {"enabled": False}
        }
    }))
    with (
        patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(tmp_path)}),
        patch.object(_mod, "fetch_usage") as mock_fetch,
    ):
        result = main()
    assert result == 0
    mock_fetch.assert_not_called()


def test_missing_cookie_exits_gracefully(tmp_path: Path) -> None:
    """main() returns 0 (not an error) when cookie file is absent."""
    cookie_path = tmp_path / "cc-usage-session-cookie"
    with (
        patch.object(_mod, "COOKIE_CONFIG_PATH", cookie_path),
        patch.object(_mod, "fetch_usage") as mock_fetch,
    ):
        result = main()
    assert result == 0
    mock_fetch.assert_not_called()


def test_auth_error_returns_0_not_1(tmp_path: Path) -> None:
    """A 401 auth error is handled gracefully — returns 0 so cron does not stop retrying."""
    cookie_path = tmp_path / "cc-usage-session-cookie"
    cookie_path.write_text("sk-ant-fake-cookie\n")

    http_error = urllib.error.HTTPError(
        url="https://claude.ai/api/usage",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(b""),
    )
    with (
        patch.object(_mod, "COOKIE_CONFIG_PATH", cookie_path),
        patch.object(_mod, "fetch_usage", side_effect=http_error),
    ):
        result = main()
    assert result == 0


def test_network_error_returns_0_not_1(tmp_path: Path) -> None:
    """A network error is handled gracefully — returns 0 so cron does not stop retrying."""
    cookie_path = tmp_path / "cc-usage-session-cookie"
    cookie_path.write_text("sk-ant-fake-cookie\n")

    url_error = urllib.error.URLError(reason="Name or service not known")
    with (
        patch.object(_mod, "COOKIE_CONFIG_PATH", cookie_path),
        patch.object(_mod, "fetch_usage", side_effect=url_error),
    ):
        result = main()
    assert result == 0


def test_parse_error_returns_0_not_1(tmp_path: Path) -> None:
    """A parse failure (unexpected response schema) returns 0 — cron retries next interval."""
    cookie_path = tmp_path / "cc-usage-session-cookie"
    cookie_path.write_text("sk-ant-fake-cookie\n")

    with (
        patch.object(_mod, "COOKIE_CONFIG_PATH", cookie_path),
        patch.object(_mod, "fetch_usage", return_value={"unexpected": "shape"}),
    ):
        result = main()
    assert result == 0


def test_successful_run_writes_state_file(tmp_path: Path) -> None:
    """A successful poll writes updated rate limits and source tag to state.json."""
    cookie_path = tmp_path / "cc-usage-session-cookie"
    cookie_path.write_text("sk-ant-fake-cookie\n")
    state_path = tmp_path / ".claude" / "cc-budget" / "state.json"

    with (
        patch.object(_mod, "COOKIE_CONFIG_PATH", cookie_path),
        patch.object(_mod, "STATE_FILE_PATH", state_path),
        patch.object(_mod, "fetch_usage", return_value=STANDARD_API_RESPONSE),
    ):
        result = main()

    assert result == 0
    assert state_path.exists()
    written = json.loads(state_path.read_text())
    assert written["source"] == SOURCE_TAG
    assert written["rate_limits"]["five_hour"]["pct"] == 24.5
    assert written["rate_limits"]["seven_day"]["pct"] == 31.2


def test_dry_run_skips_network_and_disk(tmp_path: Path) -> None:
    """--dry-run returns 0 without making HTTP calls or writing files."""
    cookie_path = tmp_path / "cc-usage-session-cookie"
    cookie_path.write_text("sk-ant-fake-cookie\n")
    state_path = tmp_path / ".claude" / "cc-budget" / "state.json"

    with (
        patch.object(_mod, "COOKIE_CONFIG_PATH", cookie_path),
        patch.object(_mod, "STATE_FILE_PATH", state_path),
        patch.object(_mod, "fetch_usage") as mock_fetch,
    ):
        result = main(dry_run=True)

    assert result == 0
    assert not state_path.exists()
    mock_fetch.assert_not_called()
