#!/usr/bin/env python3
"""
WOS Metabolic Digest — daily pipeline health signal.

Runs once per day (default 09:00 UTC). On each invocation:
1. Reads UoWs completed (status changed to 'done' or 'failed') in the past 24h.
2. Classifies each as pearl/heat/seed/shit based on outcome heuristics.
3. Computes aggregates: total by classification, average duration, register breakdown.
4. Sends a Telegram message to the admin chat_id with the digest.
5. Writes output to the scheduled-jobs log.

Classification heuristics (from the audit doc and WOS architecture):
  pearl — UoW produced a PR or implementation (output_ref has content, outcome 'complete',
           close_reason mentions 'pr' or 'implementation')
  heat  — UoW produced a review or analysis (output_ref has content, outcome 'complete',
           close_reason mentions 'review' or 'analysis' or 'design')
  seed  — UoW created a new issue or follow-up (close_reason matches phrase-level patterns
           like 'opened issue #N', 'spawned issue', 'created uow', 'seeded', 'follow-up')
  shit  — UoW failed, expired, hit TTL, or produced no verifiable output

Cron schedule (daily at 09:00 UTC):
    0 9 * * * cd ~/lobster && uv run scheduled-tasks/wos-metabolic-digest.py >> ~/lobster-workspace/scheduled-jobs/logs/wos-metabolic-digest.log 2>&1 # LOBSTER-WOS-METABOLIC-DIGEST

Type C dispatch: cron calls this script directly (no inbox/ message, no dispatcher
involvement). The jobs.json enabled gate is checked at the top of main() so that
runtime enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/wos-metabolic-digest.py [--dry-run] [--hours N]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("wos-metabolic-digest")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME: str = "wos-metabolic-digest"

# Default lookback window for the digest
DEFAULT_LOOKBACK_HOURS: int = 24

# Admin chat_id for Telegram delivery (env-injected, falls back to hardcoded)
ADMIN_CHAT_ID: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")

# Terminal statuses that indicate a UoW completed in some form
TERMINAL_STATUSES: tuple[str, ...] = ("done", "failed", "expired", "cancelled")

# Outcome category names (from the audit doc and WOS architecture)
OUTCOME_PEARL = "pearl"
OUTCOME_HEAT = "heat"
OUTCOME_SEED = "seed"
OUTCOME_SHIT = "shit"


# ---------------------------------------------------------------------------
# Workspace / path helpers
# ---------------------------------------------------------------------------

def _workspace() -> Path:
    return Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))


def _registry_db_path() -> Path:
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    return _workspace() / "orchestration" / "registry.db"


def _jobs_file() -> Path:
    return _workspace() / "scheduled-jobs" / "jobs.json"


def _inbox_dir() -> Path:
    messages_base = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_base / "inbox"


# ---------------------------------------------------------------------------
# jobs.json enabled gate — Type C dispatch pattern
# ---------------------------------------------------------------------------

def _is_job_enabled() -> bool:
    """
    Return True if this job is enabled in jobs.json, False if explicitly disabled.

    Defaults to True when:
    - jobs.json is absent
    - the job entry is missing
    - the file is unreadable or malformed
    """
    try:
        data = json.loads(_jobs_file().read_text())
        return bool(data.get("jobs", {}).get(JOB_NAME, {}).get("enabled", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Classification heuristics — pure functions, testable without DB
# ---------------------------------------------------------------------------

def classify_uow(uow: dict) -> str:
    """
    Classify a completed UoW into one of: pearl, heat, seed, shit.

    Classification is based on close_reason, output_ref size, and status.
    The heuristics are intentionally simple and conservative — when in doubt,
    classify as 'shit' (no verifiable output). Callers can tune thresholds
    by modifying the keyword sets below.

    Args:
        uow: A dict with keys: id, status, close_reason, output_ref, register,
             steward_cycles, started_at, closed_at.

    Returns:
        One of: "pearl", "heat", "seed", "shit"
    """
    status = (uow.get("status") or "").lower()
    close_reason = (uow.get("close_reason") or "").lower()
    output_ref = uow.get("output_ref") or ""

    # shit: definitively failed, expired, or TTL-exceeded
    if status in ("failed", "expired", "cancelled"):
        return OUTCOME_SHIT
    if "ttl_exceeded" in close_reason or "ttl" in close_reason:
        return OUTCOME_SHIT
    if "hard_cap" in close_reason or "user_closed" in close_reason:
        return OUTCOME_SHIT

    # For 'done' UoWs, classify by close_reason and output content
    if status != "done":
        return OUTCOME_SHIT  # any non-terminal status that reached here is unexpected

    # seed: spawned a new issue or seeded future work.
    # Use phrase-level patterns to avoid false positives from close_reason values
    # that merely reference a source issue without creating a new one.
    # "issue" as a bare substring is too broad — "issue #456 was referenced in this
    # pr" or "issue analysis complete" would incorrectly classify as seed.
    _SEED_ISSUE_PATTERN = re.compile(
        r"(opened|created|filed|new|spawned)\s+issue\s*#?\d*",
        re.IGNORECASE,
    )
    _SEED_UOW_PATTERN = re.compile(
        r"(spawned|created|spawn)\s+(uow|new\s+uow)",
        re.IGNORECASE,
    )
    if (
        _SEED_ISSUE_PATTERN.search(close_reason)
        or _SEED_UOW_PATTERN.search(close_reason)
        or "seeded" in close_reason
        or "follow-up" in close_reason
        or "follow_up" in close_reason
    ):
        return OUTCOME_SEED

    # pearl: produced a PR, implementation, or code change
    pearl_keywords = {"pr", "pull request", "implementation", "commit", "merge", "patch", "fix"}
    if any(kw in close_reason for kw in pearl_keywords):
        return OUTCOME_PEARL

    # Check output_ref size — a substantial output file suggests real work
    if output_ref:
        try:
            output_path = Path(output_ref)
            if output_path.exists() and output_path.stat().st_size > 500:
                # heat: review/analysis/design output
                heat_keywords = {"review", "analysis", "design", "synthesis", "assessment", "report"}
                if any(kw in close_reason for kw in heat_keywords):
                    return OUTCOME_HEAT
                # Default for done + substantial output = pearl (best-effort)
                return OUTCOME_PEARL
        except Exception:
            pass

    # heat: review or analysis output (by close_reason alone)
    heat_keywords = {"review", "analysis", "design", "synthesis", "assessment", "report"}
    if any(kw in close_reason for kw in heat_keywords):
        return OUTCOME_HEAT

    # No clear signal — default to shit (no verifiable output)
    return OUTCOME_SHIT


def compute_duration_minutes(uow: dict) -> float | None:
    """
    Compute UoW duration in minutes from started_at to closed_at.

    Returns None if either timestamp is missing or unparseable.
    """
    started = uow.get("started_at") or uow.get("created_at")
    closed = uow.get("closed_at") or uow.get("updated_at")
    if not started or not closed:
        return None
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        close_dt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
        delta = (close_dt - start_dt).total_seconds()
        return round(delta / 60, 1) if delta >= 0 else None
    except (ValueError, TypeError):
        return None


def aggregate_by_classification(classified: list[tuple[dict, str]]) -> dict[str, list[dict]]:
    """
    Group (uow, classification) pairs by classification label.

    Returns a dict mapping classification → list of uow dicts.
    All four keys are always present (possibly empty lists).
    """
    groups: dict[str, list[dict]] = {
        OUTCOME_PEARL: [],
        OUTCOME_HEAT: [],
        OUTCOME_SEED: [],
        OUTCOME_SHIT: [],
    }
    for uow, label in classified:
        groups[label].append(uow)
    return groups


def compute_avg_duration(uows: list[dict]) -> str:
    """
    Compute average duration in minutes for a list of UoWs.

    Returns "N/A" if no durations available.
    """
    durations = [d for d in (compute_duration_minutes(u) for u in uows) if d is not None]
    if not durations:
        return "N/A"
    avg = round(sum(durations) / len(durations), 1)
    return f"{avg} min"


def aggregate_token_usage(uows: list[dict]) -> int | None:
    """
    Sum token_usage across UoWs that reported it.

    Returns the total token count, or None if no UoW reported usage.
    UoWs with NULL token_usage (pre-migration or unreported) are excluded from
    the sum but do not invalidate it — partial data is better than no data.
    """
    values = [u["token_usage"] for u in uows if u.get("token_usage") is not None]
    return sum(values) if values else None


def compute_wall_clock_stats(uows: list[dict]) -> dict:
    """
    Compute wall_clock_seconds statistics across UoWs.

    Returns a dict with keys: count (UoWs with data), total_seconds, avg_seconds.
    All values are None when no UoWs reported wall_clock_seconds.
    """
    values = [u["wall_clock_seconds"] for u in uows if u.get("wall_clock_seconds") is not None]
    if not values:
        return {"count": 0, "total_seconds": None, "avg_seconds": None}
    return {
        "count": len(values),
        "total_seconds": sum(values),
        "avg_seconds": round(sum(values) / len(values)),
    }


# ---------------------------------------------------------------------------
# Registry query — reads completed UoWs in the lookback window
# ---------------------------------------------------------------------------

def query_completed_uows(db_path: Path, lookback_hours: int) -> list[dict]:
    """
    Return UoWs that transitioned to a terminal status in the past lookback_hours.

    Uses audit_log to find UoWs that entered a terminal state (done, failed,
    expired, cancelled) within the window. Returns full UoW rows for each match.

    Returns [] if DB absent or table missing.
    """
    if not db_path.exists():
        log.warning("Registry DB not found at %s", db_path)
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    terminal_placeholders = ",".join("?" * len(TERMINAL_STATUSES))

    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            # Find UoW IDs that entered a terminal status within the window
            transition_rows = conn.execute(
                f"""
                SELECT DISTINCT uow_id FROM audit_log
                WHERE to_status IN ({terminal_placeholders})
                  AND ts >= ?
                """,
                (*TERMINAL_STATUSES, cutoff),
            ).fetchall()

            if not transition_rows:
                return []

            uow_ids = [r["uow_id"] for r in transition_rows]
            id_placeholders = ",".join("?" * len(uow_ids))

            # Fetch full UoW records for each matched ID.
            # token_usage: per-UoW cost telemetry (issue #990); NULL for pre-migration rows.
            # wall_clock_seconds: derived from completed_at - started_at delta at query time.
            rows = conn.execute(
                f"""
                SELECT id, status, summary, register, close_reason, output_ref,
                       started_at, closed_at, updated_at, created_at, steward_cycles,
                       token_usage,
                       CASE
                         WHEN completed_at IS NOT NULL AND started_at IS NOT NULL
                         THEN CAST(
                           (julianday(completed_at) - julianday(started_at)) * 86400.0
                           AS INTEGER
                         )
                         ELSE NULL
                       END AS wall_clock_seconds
                FROM uow_registry
                WHERE id IN ({id_placeholders})
                  AND status IN ({terminal_placeholders})
                """,
                (*uow_ids, *TERMINAL_STATUSES),
            ).fetchall()

            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Completed UoW query failed: %s", exc)
        return []


def query_still_running(db_path: Path, threshold_hours: int) -> list[dict]:
    """
    Return UoWs in 'active' or 'executing' that have been running >threshold_hours.
    Used to populate the 'Stalled' section of the digest.
    """
    if not db_path.exists():
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=threshold_hours)).isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, status, summary, started_at, created_at
                FROM uow_registry
                WHERE status IN ('active', 'executing')
                  AND (
                    (started_at IS NOT NULL AND started_at <= ?)
                    OR (started_at IS NULL AND created_at <= ?)
                  )
                ORDER BY created_at ASC
                """,
                (cutoff, cutoff),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Still-running query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Digest formatter — pure function, returns Telegram-ready text
# ---------------------------------------------------------------------------

def format_digest(
    groups: dict[str, list[dict]],
    stalled: list[dict],
    lookback_hours: int,
    now_iso: str,
) -> str:
    """
    Format the metabolic digest as a Telegram message.

    Args:
        groups:        Classification groups from aggregate_by_classification.
        stalled:       UoWs still running past the stall threshold.
        lookback_hours: The window used for the digest.
        now_iso:        ISO timestamp for the report header.

    Returns:
        A multi-line string ready to send via Telegram.
    """
    pearl_count = len(groups[OUTCOME_PEARL])
    heat_count = len(groups[OUTCOME_HEAT])
    seed_count = len(groups[OUTCOME_SEED])
    shit_count = len(groups[OUTCOME_SHIT])
    total = pearl_count + heat_count + seed_count + shit_count

    # Register breakdown — count by register across all classified UoWs
    register_counts: dict[str, int] = {}
    all_uows: list[dict] = []
    for label, uows in groups.items():
        all_uows.extend(uows)
        for uow in uows:
            reg = uow.get("register") or "operational"
            register_counts[reg] = register_counts.get(reg, 0) + 1

    register_parts = ", ".join(
        f"{count} {reg}" for reg, count in sorted(register_counts.items())
    ) if register_counts else "none"

    # Average durations per category
    pearl_avg = compute_avg_duration(groups[OUTCOME_PEARL])
    heat_avg = compute_avg_duration(groups[OUTCOME_HEAT])

    # Telemetry: aggregate token_usage and wall_clock_seconds (issue #990)
    total_tokens = aggregate_token_usage(all_uows)
    wall_clock = compute_wall_clock_stats(all_uows)

    # Stalled display
    stalled_line = ""
    if stalled:
        stalled_ids = ", ".join(u["id"] for u in stalled[:5])
        stalled_line = f"\nStalled (>6h): {stalled_ids}"
        if len(stalled) > 5:
            stalled_line += f" (+{len(stalled) - 5} more)"

    lines = [
        "WOS Daily Metabolic Report",
        f"Last {lookback_hours}h: {pearl_count} pearl / {heat_count} heat / {seed_count} seed / {shit_count} shit",
    ]

    if total > 0:
        lines.append(f"Register breakdown: {register_parts}")
        lines.append(f"Avg duration: {pearl_avg} (pearl), {heat_avg} (heat)")
        # Token and wall-clock telemetry — only show when data is available
        if total_tokens is not None:
            lines.append(f"Tokens: {total_tokens:,} total")
        if wall_clock["avg_seconds"] is not None:
            avg_min = round(wall_clock["avg_seconds"] / 60, 1)
            lines.append(f"Wall-clock avg: {avg_min} min ({wall_clock['count']} UoWs with data)")
    else:
        lines.append("No UoWs completed in this window.")

    if stalled_line:
        lines.append(stalled_line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Inbox message writer — sends digest via inbox → Telegram
# ---------------------------------------------------------------------------

def _write_inbox_digest(text: str, dry_run: bool = False) -> None:
    """
    Write a wos_metabolic_digest inbox message for the dispatcher to relay via Telegram.

    In dry_run mode: logs the message but does not write the file.
    """
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "type": "wos_metabolic_digest",
        "chat_id": ADMIN_CHAT_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
    }

    if dry_run:
        log.info("DRY RUN: would send digest:\n%s", text)
        return

    try:
        inbox = _inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        tmp_path = inbox / f"{msg_id}.json.tmp"
        dest_path = inbox / f"{msg_id}.json"
        tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp_path.rename(dest_path)
        log.info("Wrote metabolic digest inbox message %s", msg_id)
    except Exception as exc:
        log.warning("Failed to write digest inbox message: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_digest(db_path: Path, lookback_hours: int, dry_run: bool = False) -> dict:
    """
    Run the metabolic digest and return a structured result dict.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # Query completed UoWs
    completed = query_completed_uows(db_path, lookback_hours)
    log.info("Completed UoWs in last %dh: %d", lookback_hours, len(completed))

    # Classify each
    classified = [(uow, classify_uow(uow)) for uow in completed]
    groups = aggregate_by_classification(classified)

    for label, uows in groups.items():
        log.info("  %s: %d", label, len(uows))

    # Stalled UoWs (>6h in flight)
    stalled = query_still_running(db_path, threshold_hours=6)
    if stalled:
        log.warning("Still running >6h: %d", len(stalled))

    # Format and send digest
    digest_text = format_digest(groups, stalled, lookback_hours, now_iso)
    log.info("Digest:\n%s", digest_text)

    _write_inbox_digest(digest_text, dry_run=dry_run)

    all_uows = [uow for uows in groups.values() for uow in uows]
    total_tokens = aggregate_token_usage(all_uows)
    wall_stats = compute_wall_clock_stats(all_uows)

    return {
        "timestamp": now_iso,
        "lookback_hours": lookback_hours,
        "completed_count": len(completed),
        "pearl": len(groups[OUTCOME_PEARL]),
        "heat": len(groups[OUTCOME_HEAT]),
        "seed": len(groups[OUTCOME_SEED]),
        "shit": len(groups[OUTCOME_SHIT]),
        "stalled_count": len(stalled),
        "dry_run": dry_run,
        "digest_text": digest_text,
        # Telemetry (issue #990) — None when no UoWs reported data
        "total_tokens": total_tokens,
        "wall_clock_uow_count": wall_stats["count"],
        "wall_clock_avg_seconds": wall_stats["avg_seconds"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="WOS metabolic digest — daily pipeline health signal")
    parser.add_argument("--dry-run", action="store_true", help="Compute digest but do not send Telegram message")
    parser.add_argument("--hours", type=int, default=DEFAULT_LOOKBACK_HOURS,
                        help=f"Lookback window in hours (default: {DEFAULT_LOOKBACK_HOURS})")
    args = parser.parse_args()

    if not _is_job_enabled():
        log.info("Job '%s' is disabled in jobs.json — skipping", JOB_NAME)
        return 0

    db_path = _registry_db_path()
    log.info(
        "Running WOS metabolic digest — registry: %s lookback=%dh dry_run=%s",
        db_path, args.hours, args.dry_run,
    )

    result = run_digest(db_path, lookback_hours=args.hours, dry_run=args.dry_run)
    log.info(
        "Digest complete — pearl=%d heat=%d seed=%d shit=%d stalled=%d",
        result["pearl"], result["heat"], result["seed"], result["shit"], result["stalled_count"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
