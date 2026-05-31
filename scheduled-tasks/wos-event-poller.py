#!/usr/bin/env -S uv run
"""
WOS Event Poller — 30-second delta poller for the Event-Native Nervous System (Stage 2).

Runs every 30 seconds (via cron at */1 * * * * with a 30-second sleep). On each invocation:

1. Checks is_job_enabled("wos-event-poller") — exits immediately if disabled.
2. Reads the since-cursor from ~/.lobster-workspace/data/wos-event-poller-cursor.json.
3. Queries GitHub for new issues with label wos:uow created after the cursor.
4. Queries registry.db for UoWs that transitioned to done/failed after the cursor.
5. Emits typed inbox events for each new item:
   - wos_issue_created  → for each new GitHub issue found
   - wos_uow_completed  → for each newly terminal UoW
   - wos_capacity_available → when active UoW count < max_parallel
6. Writes event_log rows (via wos_events.py emit functions).
7. Advances the since-cursor to now.

Deduplication is enforced at the DB layer via event_log UNIQUE(event_type, dedup_key).
Re-running with the same cursor window is safe — duplicate events are silently dropped.

Type B (cron-direct): cron calls this script directly. No inbox message, no LLM round-trip.
The jobs.json enabled gate is checked at the top of main() before any DB work.

Cron entry (every 30 seconds via two staggered per-minute entries):
    * * * * * cd ~/lobster && uv run scheduled-tasks/wos-event-poller.py >> ~/lobster-workspace/scheduled-jobs/logs/wos-event-poller.log 2>&1 # LOBSTER-WOS-EVENT-POLLER
    * * * * * sleep 30 && cd ~/lobster && uv run scheduled-tasks/wos-event-poller.py >> ~/lobster-workspace/scheduled-jobs/logs/wos-event-poller.log 2>&1 # LOBSTER-WOS-EVENT-POLLER-30S

Run standalone:
    uv run ~/lobster/scheduled-tasks/wos-event-poller.py [--dry-run]

Design reference: wos-evolution-spec.md §3-II Event-Native Nervous System
Issue: https://github.com/dcetlin/Lobster/issues/1351
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from src.utils.jobs import is_job_enabled
from src.orchestration.paths import REGISTRY_DB, WOS_CONFIG

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("wos-event-poller")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

#: Job name registered in jobs.json — must match the entry there exactly.
JOB_NAME: str = "wos-event-poller"

#: GitHub repo to scan for new wos:uow issues.
WOS_REPO: str = "dcetlin/Lobster"

#: Label that marks an issue as a WOS Unit of Work.
WOS_UOW_LABEL: str = "wos:uow"

#: Maximum number of issues to retrieve per gh CLI call.
GH_ISSUE_LIMIT: int = 50

#: Default since-cursor lookback when no cursor file exists (seconds).
#: First run scans the last 60 seconds to avoid emitting events for old issues.
DEFAULT_CURSOR_LOOKBACK_SECONDS: int = 60

#: Terminal UoW statuses that trigger wos_uow_completed events.
TERMINAL_UOW_STATUSES: frozenset[str] = frozenset({"done", "failed", "expired"})

# ---------------------------------------------------------------------------
# Cursor persistence — tracks the "since" timestamp across invocations
# ---------------------------------------------------------------------------


def _cursor_path() -> Path:
    """Return the path to the since-cursor JSON file."""
    workspace = Path(
        os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    )
    return workspace / "data" / "wos-event-poller-cursor.json"


def _read_cursor() -> str:
    """
    Read the since-cursor from disk.

    Returns the ISO-8601 timestamp of the last successful poll, or a timestamp
    DEFAULT_CURSOR_LOOKBACK_SECONDS in the past if no cursor exists.
    """
    path = _cursor_path()
    try:
        data = json.loads(path.read_text())
        return data["since"]
    except (OSError, KeyError, json.JSONDecodeError):
        fallback = datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_CURSOR_LOOKBACK_SECONDS)
        return fallback.isoformat()


def _write_cursor(since: str) -> None:
    """Write the since-cursor to disk atomically."""
    path = _cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"since": since, "updated_at": datetime.now(timezone.utc).isoformat()}))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# WOS config reader — reads max_parallel for capacity events
# ---------------------------------------------------------------------------


def _read_max_parallel() -> int:
    """Return max_parallel from wos-config.json, defaulting to 2."""
    try:
        with WOS_CONFIG.open() as fh:
            return int(json.load(fh).get("max_parallel", 2))
    except (OSError, json.JSONDecodeError, ValueError):
        return 2


# ---------------------------------------------------------------------------
# GitHub issue delta query
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewIssue:
    """A new GitHub issue detected since the cursor timestamp."""
    issue_number: int
    issue_url: str
    title: str
    labels: list[str]
    created_at: str


def _fetch_new_wos_issues(since: str, repo: str = WOS_REPO) -> list[NewIssue]:
    """
    Return new GitHub issues with label ``wos:uow`` created after ``since``.

    Uses the gh CLI with ``--search`` filter. Returns an empty list on any
    subprocess or parse error (non-fatal; delta poller continues).

    Args:
        since: ISO-8601 timestamp cutoff. Only issues created after this time.
        repo: GitHub repo slug (owner/repo).

    Returns:
        List of NewIssue value objects, sorted ascending by issue number.
    """
    # gh supports `created:>YYYY-MM-DD` in --search but not full ISO-8601 timestamps.
    # We use --json and filter in Python to get sub-minute precision.
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", repo,
                "--state", "open",
                "--label", WOS_UOW_LABEL,
                "--json", "number,title,labels,url,createdAt",
                "--limit", str(GH_ISSUE_LIMIT),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            env={**os.environ, "GH_TOKEN": "", "GITHUB_TOKEN": ""},
        )
    except subprocess.CalledProcessError as exc:
        log.warning("_fetch_new_wos_issues: gh CLI returned %d: %s", exc.returncode, exc.stderr.strip())
        return []
    except subprocess.TimeoutExpired:
        log.warning("_fetch_new_wos_issues: gh CLI timed out")
        return []
    except FileNotFoundError:
        log.warning("_fetch_new_wos_issues: gh CLI not found")
        return []

    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.warning("_fetch_new_wos_issues: JSON parse error: %s", exc)
        return []

    since_dt = _parse_iso(since)
    new_issues: list[NewIssue] = []
    for issue in issues:
        created_dt = _parse_iso(issue.get("createdAt", ""))
        if created_dt is None or created_dt <= since_dt:
            continue
        label_names = [lbl.get("name", "") for lbl in issue.get("labels", [])]
        new_issues.append(NewIssue(
            issue_number=issue["number"],
            issue_url=issue.get("url", ""),
            title=issue.get("title", ""),
            labels=label_names,
            created_at=issue.get("createdAt", ""),
        ))

    new_issues.sort(key=lambda i: i.issue_number)
    return new_issues


# ---------------------------------------------------------------------------
# Registry delta query — UoW terminal transitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletedUoW:
    """A UoW that transitioned to a terminal state since the cursor timestamp."""
    uow_id: str
    outcome: str       # done | failed | expired
    register: str
    output_ref: str | None
    completed_at: str


def _fetch_newly_completed_uows(since: str, db_path: Path | None = None) -> list[CompletedUoW]:
    """
    Return UoWs that transitioned to a terminal status after ``since``.

    Queries registry.db for rows where ``completed_at > since``. Returns an
    empty list if the DB is absent or unreadable (non-fatal).

    Args:
        since: ISO-8601 timestamp cutoff.
        db_path: Override DB path (tests).

    Returns:
        List of CompletedUoW value objects, sorted ascending by completed_at.
    """
    path = db_path or REGISTRY_DB
    if not path.exists():
        log.debug("_fetch_newly_completed_uows: registry DB not found at %s", path)
        return []

    placeholders = ",".join("?" * len(TERMINAL_UOW_STATUSES))
    query = f"""
        SELECT id, status, route_reason AS register, output_ref, completed_at
          FROM uow_registry
         WHERE status IN ({placeholders})
           AND completed_at > ?
         ORDER BY completed_at ASC
    """

    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(query, (*TERMINAL_UOW_STATUSES, since)).fetchall()
        conn.close()
    except Exception as exc:
        log.warning("_fetch_newly_completed_uows: DB query failed: %s", exc)
        return []

    return [
        CompletedUoW(
            uow_id=row["id"],
            outcome=row["status"],
            register=row["register"] or "unknown",
            output_ref=row["output_ref"],
            completed_at=row["completed_at"],
        )
        for row in rows
    ]


def _count_active_uows(db_path: Path | None = None) -> int:
    """Return the count of UoWs currently in 'executing' state."""
    path = db_path or REGISTRY_DB
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        count = conn.execute(
            "SELECT COUNT(*) FROM uow_registry WHERE status = 'executing'"
        ).fetchone()[0]
        conn.close()
        return count
    except Exception as exc:
        log.warning("_count_active_uows: DB query failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# ISO-8601 parsing helper
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime | None:
    """
    Parse an ISO-8601 timestamp to a timezone-aware datetime.

    Returns None if the string is empty or unparseable. Handles both Z-suffix
    (GitHub API format) and +00:00 offset (Python isoformat).
    """
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------


def run_poll_cycle(
    *,
    since: str,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> tuple[int, int, int]:
    """
    Execute one poll cycle and emit typed events for new items found.

    Pure side-effectful function: queries GitHub + registry, emits inbox events,
    records to event_log. Returns counts of events emitted for each type.

    Args:
        since: ISO-8601 since-cursor timestamp.
        dry_run: If True, log what would be emitted but do not write anything.
        db_path: Override registry DB path (tests).

    Returns:
        Tuple of (issue_events_emitted, uow_events_emitted, capacity_events_emitted).
    """
    from src.orchestration.wos_events import (  # noqa: PLC0415
        emit_issue_created,
        emit_uow_completed,
        emit_capacity_available,
    )

    issue_count = 0
    uow_count = 0
    capacity_count = 0

    # --- New GitHub issues ---
    new_issues = _fetch_new_wos_issues(since)
    for issue in new_issues:
        log.info(
            "run_poll_cycle: new wos:uow issue #%d %r (created=%s)",
            issue.issue_number, issue.title, issue.created_at,
        )
        if not dry_run:
            event_id = emit_issue_created(
                issue_number=issue.issue_number,
                issue_url=issue.issue_url,
                title=issue.title,
                labels=issue.labels,
                triggered_at=issue.created_at,
                db_path=db_path,
            )
            if event_id:
                issue_count += 1

    # --- Newly terminal UoWs ---
    completed = _fetch_newly_completed_uows(since, db_path=db_path)
    max_parallel = _read_max_parallel()
    active_count = _count_active_uows(db_path=db_path)

    for uow in completed:
        log.info(
            "run_poll_cycle: UoW %r completed outcome=%s (completed_at=%s)",
            uow.uow_id, uow.outcome, uow.completed_at,
        )
        if not dry_run:
            event_id = emit_uow_completed(
                uow_id=uow.uow_id,
                outcome=uow.outcome,
                register=uow.register,
                output_ref=uow.output_ref,
                triggered_at=uow.completed_at,
                db_path=db_path,
            )
            if event_id:
                uow_count += 1

            # Emit capacity event if this completion freed a slot
            if active_count < max_parallel:
                cap_event_id = emit_capacity_available(
                    freed_uow_id=uow.uow_id,
                    freed_at=uow.completed_at,
                    current_active_count=active_count,
                    max_parallel=max_parallel,
                    db_path=db_path,
                )
                if cap_event_id:
                    capacity_count += 1

    if dry_run:
        log.info(
            "run_poll_cycle [dry-run]: would emit %d issue / %d uow / %d capacity events",
            len(new_issues), len(completed), sum(1 for u in completed if active_count < max_parallel),
        )

    return issue_count, uow_count, capacity_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="WOS 30s event delta poller")
    parser.add_argument("--dry-run", action="store_true", help="Log without writing events")
    args = parser.parse_args()

    # jobs.json gate — respect runtime enable/disable via wos start/stop
    if not is_job_enabled(JOB_NAME):
        log.debug("wos-event-poller: disabled in jobs.json — exiting")
        return

    since = _read_cursor()
    poll_start = datetime.now(timezone.utc)

    log.info("wos-event-poller: poll cycle starting (since=%s, dry_run=%s)", since, args.dry_run)

    try:
        issue_count, uow_count, capacity_count = run_poll_cycle(
            since=since,
            dry_run=args.dry_run,
        )
        log.info(
            "wos-event-poller: cycle complete — emitted issue=%d uow=%d capacity=%d",
            issue_count, uow_count, capacity_count,
        )
    except Exception as exc:
        log.error("wos-event-poller: poll cycle failed: %s: %s", type(exc).__name__, exc)
        # Do NOT advance the cursor on failure — next run will re-scan the same window
        raise

    # Advance cursor to poll start (not now) so any events that happened during
    # this poll window are captured on the next run rather than silently skipped.
    if not args.dry_run:
        _write_cursor(poll_start.isoformat())
        log.debug("wos-event-poller: cursor advanced to %s", poll_start.isoformat())


if __name__ == "__main__":
    main()
