#!/usr/bin/env python3
"""
Local Session Parser — extract token usage from local Claude Code session files.

Reads JSONL session files from ~/.claude/projects/-home-lobster-lobster-workspace/
and accumulates per-day token counts without requiring a claude.ai session cookie.

Writes two outputs:
  1. ~/lobster-workspace/data/cc-usage.json — tokens_today, tokens_this_week,
     per-day breakdown (input, output, cache_creation, cache_read).
  2. ~/.claude/cc-budget/state.json — appends a ``token_usage`` section with the
     same data, so the quota gate and morning briefing have a single state file to
     consult. Does NOT overwrite ``rate_limits`` if they already exist and are fresh.

Problem context: cc-usage-poller.py fetches quota percentages via a cookie that
expired on 2026-05-17.  After expiry the poller is blind.  Session JSONL files at
~/.claude/projects/-home-lobster-lobster-workspace/*.jsonl contain token usage in
every ``assistant`` entry under ``message.usage``.  This script aggregates those
counts and writes them to the cc-usage and cc-budget state files so the morning
briefing and quota gate can surface real usage data even when the cookie is absent.

Token fields extracted from each assistant entry:
  - input_tokens
  - output_tokens
  - cache_creation_input_tokens
  - cache_read_input_tokens

Date attribution: uses the ``timestamp`` field (ISO 8601 UTC) on each entry.
Falls back to the file's mtime if the entry has no timestamp.

Cron schedule (every 30 minutes, offset from cc-usage-poller):
    15,45 * * * * cd ~/lobster && uv run scheduled-tasks/local-session-parser.py >> ~/lobster-workspace/scheduled-jobs/logs/local-session-parser.log 2>&1 # LOBSTER-LOCAL-SESSION-PARSER

Type B dispatch: cron calls this script directly (no inbox/ message, no
dispatcher involvement). The jobs.json enabled gate is checked at the top
of main() so that runtime enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/local-session-parser.py [--dry-run] [--verbose]

Related issue: dcetlin/Lobster#740
"""

# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.jobs import is_job_enabled  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("local-session-parser")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directory containing Claude Code session JSONL files for this workspace.
SESSION_DIR: Path = (
    Path.home() / ".claude" / "projects" / "-home-lobster-lobster-workspace"
)

# Output: token counts (independent of quota percentages)
CC_USAGE_PATH: Path = Path.home() / "lobster-workspace" / "data" / "cc-usage.json"

# Output: cc-budget state file (shared with cc-usage-poller and cc-usage-collect.sh)
STATE_FILE_PATH: Path = Path.home() / ".claude" / "cc-budget" / "state.json"

# Source tag written into state.json to identify local-parser data.
SOURCE_TAG = "local-session-parser"

# How many calendar days of history to include in the per-day breakdown.
HISTORY_DAYS: int = 14

# Seconds before state.json's rate_limits data is considered stale and
# eligible to be cleared/replaced by local token_usage data.
# 2 hours matches the threshold used by read_quota_state() / format_quota_message().
POLLER_STALE_SECONDS: int = 2 * 60 * 60

# 5-hour rolling window size in seconds — used for 5h token window computation.
FIVE_HOUR_WINDOW_SECONDS: int = 5 * 60 * 60


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


def _empty_day_counts() -> dict[str, int]:
    """Return a zeroed token count dict for one calendar day."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    }


# ---------------------------------------------------------------------------
# Session file discovery — pure, no side effects beyond filesystem reads
# ---------------------------------------------------------------------------


def discover_session_files(session_dir: Path) -> list[Path]:
    """Return all *.jsonl session files in session_dir, sorted by name.

    Returns an empty list if the directory does not exist or no files match.
    This function never raises — missing directory is not an error.
    """
    if not session_dir.exists():
        return []
    return sorted(session_dir.glob("*.jsonl"))


# ---------------------------------------------------------------------------
# Single-entry extraction — pure function
# ---------------------------------------------------------------------------


def extract_usage_from_entry(entry: dict[str, Any]) -> tuple[str | None, dict[str, int]]:
    """Extract date and token counts from one JSONL entry.

    Returns ``(date_str, counts)`` where:
    - ``date_str`` is a ``YYYY-MM-DD`` string (UTC) or None if the entry has
      no date and should be skipped.
    - ``counts`` is a token count dict (all values default to 0 for missing keys).

    Only ``assistant`` entries carry usage data.  All other entry types return
    ``(None, zeros)`` — the caller should skip these.

    Pure function: no filesystem I/O, no side effects.
    """
    zeros = _empty_day_counts()

    if entry.get("type") != "assistant":
        return None, zeros

    usage = entry.get("message", {}).get("usage", {})
    if not usage:
        return None, zeros

    # Attribute to the UTC date from the entry's timestamp.
    timestamp_str = entry.get("timestamp")
    if timestamp_str:
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            date_str = ts.strftime("%Y-%m-%d")
        except ValueError:
            return None, zeros
    else:
        return None, zeros

    counts = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cache_creation": int(usage.get("cache_creation_input_tokens", 0)),
        "cache_read": int(usage.get("cache_read_input_tokens", 0)),
    }
    return date_str, counts


# ---------------------------------------------------------------------------
# Session file parsing — reads one file, returns per-day counts
# ---------------------------------------------------------------------------


def parse_session_file(path: Path) -> dict[str, dict[str, int]]:
    """Parse one JSONL session file and return per-day token counts.

    Returns a dict mapping ``YYYY-MM-DD`` → token counts.  Lines that are
    not valid JSON or not assistant entries are silently skipped.

    Isolated side effect: reads one file from the filesystem.
    """
    daily: dict[str, dict[str, int]] = defaultdict(lambda: _empty_day_counts())

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Could not read session file %s: %s", path, exc)
        return {}

    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            log.debug("Skipping malformed JSON at %s:%d", path.name, lineno)
            continue

        date_str, counts = extract_usage_from_entry(entry)
        if date_str is None:
            continue

        day = daily[date_str]
        for key in ("input_tokens", "output_tokens", "cache_creation", "cache_read"):
            day[key] += counts[key]

    return dict(daily)


# ---------------------------------------------------------------------------
# Aggregation across session files — pure transformation
# ---------------------------------------------------------------------------


def aggregate_daily_counts(
    per_file: list[dict[str, dict[str, int]]],
    cutoff_date: str,
) -> dict[str, dict[str, int]]:
    """Merge per-file daily counts into a single dict, filtering old dates.

    Args:
        per_file:    List of dicts from ``parse_session_file`` (one per file).
        cutoff_date: ``YYYY-MM-DD`` string.  Dates strictly before this are dropped.

    Returns a dict ``YYYY-MM-DD`` → merged token counts covering only dates
    on or after ``cutoff_date``.

    Pure function: no side effects.
    """
    merged: dict[str, dict[str, int]] = defaultdict(lambda: _empty_day_counts())

    for file_daily in per_file:
        for date_str, counts in file_daily.items():
            if date_str < cutoff_date:
                continue
            day = merged[date_str]
            for key in ("input_tokens", "output_tokens", "cache_creation", "cache_read"):
                day[key] += counts[key]

    return dict(merged)


# ---------------------------------------------------------------------------
# Summary statistics — pure computations over aggregated daily counts
# ---------------------------------------------------------------------------


def compute_tokens_today(daily: dict[str, dict[str, int]], today: str) -> int:
    """Return the total token count (input + output) for today's UTC date."""
    counts = daily.get(today, _empty_day_counts())
    return counts["input_tokens"] + counts["output_tokens"]


def compute_tokens_this_week(
    daily: dict[str, dict[str, int]],
    week_start: str,
) -> int:
    """Return the total token count (input + output) since ``week_start`` (inclusive).

    ``week_start`` is a ``YYYY-MM-DD`` string.
    """
    total = 0
    for date_str, counts in daily.items():
        if date_str >= week_start:
            total += counts["input_tokens"] + counts["output_tokens"]
    return total


def compute_five_hour_tokens(
    session_files: list[Path],
    cutoff_ts: datetime,
) -> int:
    """Return total tokens (input + output) from entries after ``cutoff_ts``.

    Parses all session files and sums tokens for entries with timestamps
    strictly after the cutoff.  This gives a rolling 5-hour window total.

    Pure with respect to the filesystem: reads files but does not write.

    Args:
        session_files: List of JSONL file paths to scan.
        cutoff_ts:     Timezone-aware datetime.  Entries before this are ignored.

    Returns total int token count.
    """
    total = 0
    for path in session_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            ts_str = entry.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts <= cutoff_ts:
                continue
            usage = entry.get("message", {}).get("usage", {})
            total += int(usage.get("input_tokens", 0))
            total += int(usage.get("output_tokens", 0))
    return total


# ---------------------------------------------------------------------------
# cc-usage.json writer — isolated side effect
# ---------------------------------------------------------------------------


def build_cc_usage(
    daily: dict[str, dict[str, int]],
    today: str,
    week_start: str,
    now_iso: str,
) -> dict[str, Any]:
    """Build the cc-usage.json payload from aggregated daily counts.

    Pure function: all inputs are arguments; no file reads.
    """
    tokens_today = compute_tokens_today(daily, today)
    tokens_this_week = compute_tokens_this_week(daily, week_start)

    return {
        "last_updated": now_iso,
        "tokens_today": tokens_today,
        "tokens_this_week": tokens_this_week,
        "week_start": week_start,
        "daily": {
            date_str: {
                "input_tokens": counts["input_tokens"],
                "output_tokens": counts["output_tokens"],
                "cache_creation": counts["cache_creation"],
                "cache_read": counts["cache_read"],
            }
            for date_str, counts in sorted(daily.items())
        },
    }


# ---------------------------------------------------------------------------
# state.json merger — pure function
# ---------------------------------------------------------------------------


def _poller_data_is_fresh(existing: dict[str, Any]) -> bool:
    """Return True if the poller-written rate_limits data is still fresh.

    Fresh means: ``last_updated`` is within ``POLLER_STALE_SECONDS`` AND
    ``source`` is NOT ``local-session-parser`` (i.e. the poller or hook wrote it).

    If last_updated is absent or unparseable, defaults to stale=True so local
    data is surfaced rather than suppressed.
    """
    source = existing.get("source", "")
    if source == SOURCE_TAG:
        # Already our data — not "fresh poller data"
        return False

    last_updated_str = existing.get("last_updated")
    if not last_updated_str:
        return False

    try:
        ts = datetime.fromisoformat(last_updated_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - ts
        return age.total_seconds() <= POLLER_STALE_SECONDS
    except Exception:
        return False


def merge_token_usage_into_state(
    existing: dict[str, Any],
    daily: dict[str, dict[str, int]],
    tokens_today: int,
    tokens_this_week: int,
    five_hour_tokens: int,
    week_start: str,
    now_iso: str,
    now_unix: int,
) -> dict[str, Any]:
    """Merge local token counts into the existing state.json content.

    Preserves ``rate_limits`` if poller data is still fresh (within 2 hours).
    Always adds or replaces the ``token_usage`` section with local counts.
    Updates ``source`` to ``local-session-parser`` only when rate_limits are stale.

    Pure function: all inputs are arguments; no file reads.
    """
    updated = dict(existing)
    updated["v"] = existing.get("v", 1)
    updated["ts"] = now_unix

    # Preserve fresh poller/hook rate_limits — the local parser only supplements.
    # When poller data is stale, clear rate_limits so the quota gate knows not to
    # use stale percentages.  (The gate is fail-open: None → proceed normally.)
    poller_fresh = _poller_data_is_fresh(existing)
    if not poller_fresh and existing.get("rate_limits"):
        # Clear stale percentages — the local parser cannot supply them.
        # Keeping stale values would mislead the quota gate.
        updated["rate_limits"] = {
            "five_hour": {"pct": None, "resets_at": None},
            "seven_day": {"pct": None, "resets_at": None},
        }
        updated["source"] = SOURCE_TAG
        updated["last_updated"] = now_iso
    elif not poller_fresh:
        updated["source"] = SOURCE_TAG
        updated["last_updated"] = now_iso

    # Always write token_usage section — supplemental to rate_limits.
    updated["token_usage"] = {
        "tokens_today": tokens_today,
        "tokens_this_week": tokens_this_week,
        "five_hour_tokens": five_hour_tokens,
        "week_start": week_start,
        "last_updated": now_iso,
        "source": SOURCE_TAG,
    }

    return updated


# ---------------------------------------------------------------------------
# Atomic file writer — isolated side effect
# ---------------------------------------------------------------------------


def write_atomically(data: dict[str, Any], dest_path: Path) -> None:
    """Write data as JSON to dest_path via an atomic temp-file rename.

    Creates parent directories if needed.  Raises OSError on write failure
    (caller owns error handling).
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest_path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, dest_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# State file loader — isolated read, returns {} on failure
# ---------------------------------------------------------------------------


def load_existing_state(state_path: Path) -> dict[str, Any]:
    """Return the current state.json contents, or {} if absent or unreadable."""
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(dry_run: bool = False, verbose: bool = False) -> int:
    """Parse local session JSONL files and write token usage data.

    Returns 0 on success, 1 on hard failure.  Soft failures (no files found,
    read errors on individual files) are logged and return 0.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not is_job_enabled("local-session-parser"):
        log.info("local-session-parser is disabled in jobs.json — skipping")
        return 0

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()
    now_unix = int(now.timestamp())

    # Week start: Monday of the current week (ISO calendar).
    monday = now - timedelta(days=now.weekday())
    week_start = monday.strftime("%Y-%m-%d")

    # Cutoff for per-day history: HISTORY_DAYS ago.
    cutoff = now - timedelta(days=HISTORY_DAYS)
    cutoff_date = cutoff.strftime("%Y-%m-%d")

    # 5-hour window: entries after this timestamp.
    five_hour_cutoff = now - timedelta(seconds=FIVE_HOUR_WINDOW_SECONDS)

    # Step 1: Discover session files.
    session_dir = Path(os.environ.get("LOBSTER_SESSION_DIR", SESSION_DIR))
    session_files = discover_session_files(session_dir)

    if not session_files:
        log.warning(
            "No session JSONL files found in %s — nothing to parse", session_dir
        )
        return 0

    log.info("Parsing %d session file(s) from %s", len(session_files), session_dir)

    # Step 2: Parse all files and aggregate daily counts.
    per_file = [parse_session_file(f) for f in session_files]
    daily = aggregate_daily_counts(per_file, cutoff_date)

    if not daily:
        log.warning("No token usage entries found in recent session files")
        return 0

    # Step 3: Compute summary statistics.
    tokens_today = compute_tokens_today(daily, today)
    tokens_this_week = compute_tokens_this_week(daily, week_start)

    # 5-hour rolling window — re-reads files with timestamp filter.
    five_hour_tokens = compute_five_hour_tokens(session_files, five_hour_cutoff)

    log.info(
        "Aggregated: today=%d, this_week=%d, 5h_window=%d tokens",
        tokens_today,
        tokens_this_week,
        five_hour_tokens,
    )

    if dry_run:
        log.info("[dry-run] Would write to %s and %s", CC_USAGE_PATH, STATE_FILE_PATH)
        log.info("[dry-run] Daily breakdown: %s", json.dumps(daily, indent=2))
        return 0

    # Step 4: Write cc-usage.json.
    cc_usage_path = Path(os.environ.get("LOBSTER_CC_USAGE_PATH", CC_USAGE_PATH))
    cc_usage = build_cc_usage(daily, today, week_start, now_iso)
    try:
        write_atomically(cc_usage, cc_usage_path)
        log.info("Wrote cc-usage.json to %s", cc_usage_path)
    except OSError as exc:
        log.error("Failed to write cc-usage.json to %s: %s", cc_usage_path, exc)
        return 1

    # Step 5: Merge token_usage into state.json.
    state_path = Path(
        os.environ.get("LOBSTER_CC_BUDGET_STATE", STATE_FILE_PATH)
    )
    existing = load_existing_state(state_path)
    new_state = merge_token_usage_into_state(
        existing=existing,
        daily=daily,
        tokens_today=tokens_today,
        tokens_this_week=tokens_this_week,
        five_hour_tokens=five_hour_tokens,
        week_start=week_start,
        now_iso=now_iso,
        now_unix=now_unix,
    )
    try:
        write_atomically(new_state, state_path)
        log.info("Updated state.json at %s (token_usage section)", state_path)
    except OSError as exc:
        log.error("Failed to write state.json to %s: %s", state_path, exc)
        return 1

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Parse local Claude Code session JSONL files and write token usage data"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be written without modifying any files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run, verbose=args.verbose))
