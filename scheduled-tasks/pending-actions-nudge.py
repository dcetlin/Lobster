#!/usr/bin/env python3
"""
Pending-actions nudge job.

Queries open action-item GitHub issues owned by Dan, buckets by age,
and sends a Telegram ping if any bucket is non-empty.

Cron schedule (daily at 15:00 UTC):
    0 15 * * * cd ~/lobster && uv run scheduled-tasks/pending-actions-nudge.py >> ~/lobster-workspace/scheduled-jobs/logs/pending-actions-nudge.log 2>&1 # LOBSTER-PENDING-ACTIONS-NUDGE

Type B dispatch: cron calls this script directly (no inbox/ message, no dispatcher
involvement). The jobs.json enabled gate is checked at the top of main() so that
runtime enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/pending-actions-nudge.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.inbox_write import write_inbox_message  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("pending-actions-nudge")

# ---------------------------------------------------------------------------
# jobs.json enabled gate
# ---------------------------------------------------------------------------


def _is_job_enabled(job_name: str) -> bool:
    """
    Return True if the job is enabled in jobs.json, False if explicitly disabled.

    Defaults to True when jobs.json is absent, the entry is missing, or the
    file is unreadable — mirrors the gate logic in other Type B jobs.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    jobs_file = workspace / "scheduled-jobs" / "jobs.json"
    try:
        data = json.loads(jobs_file.read_text())
        return bool(data.get("jobs", {}).get(job_name, {}).get("enabled", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Pluggable source interface
# ---------------------------------------------------------------------------


@dataclass
class PendingAction:
    title: str
    url: str
    created_at: datetime
    owner: str


def query_github_action_items(owner: str) -> list[PendingAction]:
    """Query open action-item issues from GitHub, filtered by owner."""
    repo = os.environ.get("ACTION_ITEMS_REPO", "dcetlin/Lobster")
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", repo,
                "--label", "action-item",
                "--state", "open",
                "--json", "number,title,url,body,createdAt",
                "--limit", "100",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        log.warning("gh issue list failed (exit %d): %s", exc.returncode, exc.stderr)
        return []

    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.warning("Failed to parse gh output: %s", exc)
        return []

    actions = []
    for issue in issues:
        body = issue.get("body") or ""
        issue_owner = "dan"  # default — untagged issues belong to Dan
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("owner:"):
                issue_owner = stripped.split(":", 1)[1].strip().lower()
                break
        if issue_owner != owner:
            continue
        try:
            created_at = datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            log.warning("Skipping issue with bad createdAt: %s", exc)
            continue
        actions.append(PendingAction(
            title=issue["title"],
            url=issue["url"],
            created_at=created_at,
            owner=issue_owner,
        ))
    return actions


# Sources list — append query_journal_todos here when journal-fed todo ships
SOURCES: list = [query_github_action_items]


def get_pending_actions(owner: str) -> list[PendingAction]:
    results = []
    for source in SOURCES:
        results.extend(source(owner))
    return results


# ---------------------------------------------------------------------------
# Age bucketing
# ---------------------------------------------------------------------------


def bucket_by_age(
    actions: list[PendingAction],
    now: datetime,
) -> dict[str, list[PendingAction]]:
    buckets: dict[str, list[PendingAction]] = {"14d": [], "7d": [], "3d": []}
    for action in actions:
        age_days = (now - action.created_at).days
        if age_days >= 14:
            buckets["14d"].append(action)
        elif age_days >= 7:
            buckets["7d"].append(action)
        elif age_days >= 3:
            buckets["3d"].append(action)
    return buckets


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------


def compose_message(buckets: dict[str, list[PendingAction]]) -> str | None:
    parts = []
    if buckets["14d"]:
        titles = ", ".join(f"\"{a.title}\"" for a in buckets["14d"])
        parts.append(f"Long-stale (14d+): {titles}")
    if buckets["7d"]:
        titles = ", ".join(f"\"{a.title}\"" for a in buckets["7d"])
        parts.append(f"Still pending after a week: {titles}")
    if buckets["3d"]:
        n = len(buckets["3d"])
        oldest = min(buckets["3d"], key=lambda a: a.created_at)
        parts.append(
            f"You have {n} item{'s' if n > 1 else ''} pending >3 days"
            f" — oldest: \"{oldest.title}\""
        )
    if not parts:
        return None
    return "Pending actions nudge:\n" + "\n".join(f"• {p}" for p in parts)


# ---------------------------------------------------------------------------
# Telegram delivery — same inbox_write pattern as other cron-direct jobs
# ---------------------------------------------------------------------------

JOB_NAME = "pending-actions-nudge"


def send_telegram(text: str, dry_run: bool = False) -> None:
    chat_id = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
    if dry_run:
        print(f"[dry-run] Would send to {chat_id}:\n{text}")
        return
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    msg_id = write_inbox_message(JOB_NAME, chat_id, text, timestamp)
    log.info("Telegram message queued (msg_id=%s)", msg_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Pending-actions nudge — cron-direct Type B")
    parser.add_argument("--dry-run", action="store_true", help="Print output without sending")
    args = parser.parse_args()

    if not args.dry_run and not _is_job_enabled(JOB_NAME):
        log.info("%s disabled — skipping", JOB_NAME)
        return 0

    now = datetime.now(tz=timezone.utc)
    actions = get_pending_actions(owner="dan")
    buckets = bucket_by_age(actions, now)

    log.info("Pending actions: %d total", len(actions))
    for label, items in buckets.items():
        log.info("  %s: %d items", label, len(items))

    msg = compose_message(buckets)
    if msg:
        send_telegram(msg, dry_run=args.dry_run)
    else:
        log.info("No age thresholds met — no ping sent")
        if args.dry_run:
            print("No age thresholds met — no ping sent")

    return 0


if __name__ == "__main__":
    sys.exit(main())
