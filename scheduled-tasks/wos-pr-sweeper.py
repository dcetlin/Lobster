#!/usr/bin/env -S uv run
"""
WOS PR Sweeper — surfaces open and recently-merged PRs associated with WOS Units of Work.

Runs every 6 hours. On each invocation:
1. Queries GitHub for PRs with branch names matching uow_YYYYMMDD_XXXXXX pattern
2. Correlates PRs with UoWs in the registry
3. Identifies stale open PRs (open >7 days)
4. Identifies newly merged PRs where the UoW is still in 'complete' (not 'done')
5. Writes structured summary to task-outputs
6. Emits inbox notification if action is needed (with per-PR cooldown to prevent flooding)

This is Option 2 from the WOS PR completion design: a lightweight sweeper that runs
on a schedule, separate from the executor state machine. Does not modify UoW state;
only reads and reports.

Cron schedule (every 6 hours):
    0 */6 * * * cd ~/lobster && uv run scheduled-tasks/wos-pr-sweeper.py >> ~/lobster-workspace/scheduled-jobs/logs/wos-pr-sweeper.log 2>&1

Type C dispatch: cron calls this script directly (no inbox message, no dispatcher
involvement). The jobs.json enabled gate is checked at the top of main() so that
runtime enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/wos-pr-sweeper.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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

from src.orchestration.registry import WOSRegistry, UoWStatus
from src.utils.inbox_write import _inbox_dir, _task_outputs_dir

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("wos-pr-sweeper")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STALE_OPEN_THRESHOLD_DAYS = 7
NOTIFICATION_COOLDOWN_HOURS = 24
UOW_ID_PATTERN = re.compile(r"_uow_(\d{8}_[a-f0-9]{6})")
REPOS_TO_SCAN = [
    "dcetlin/Lobster",
    "SiderealPress/lobster",
]

# ---------------------------------------------------------------------------
# jobs.json enabled gate — Type C dispatch path
# ---------------------------------------------------------------------------

def _is_job_enabled(job_name: str) -> bool:
    """
    Return True if the job is enabled in jobs.json, False if explicitly disabled.

    Defaults to True when:
    - The job entry does not exist
    - The job entry exists but has no 'enabled' field

    This allows jobs to run by default after being added to cron, before
    jobs.json is updated.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    jobs_file = workspace / "scheduled-jobs" / "jobs.json"

    if not jobs_file.exists():
        log.warning("jobs.json not found at %s — defaulting to enabled", jobs_file)
        return True

    try:
        with jobs_file.open() as f:
            data = json.load(f)
            jobs = data.get("jobs", {})
            job = jobs.get(job_name, {})
            enabled = job.get("enabled", True)
            log.info("Job %r enabled gate: %s", job_name, enabled)
            return enabled
    except Exception as exc:
        log.error("Failed to read jobs.json — %s: %s", type(exc).__name__, exc)
        return True  # Fail open


# ---------------------------------------------------------------------------
# Notification state — per-PR cooldown to prevent inbox flooding
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    """Return the path to the per-PR notification state file."""
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "wos-pr-sweeper-state.json"


def _load_state() -> dict:
    """Load per-PR notification state from disk. Returns empty dict on missing/corrupt file."""
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read state file %s: %s — starting fresh", path, exc)
        return {}


def _save_state(state: dict) -> None:
    """Atomically persist per-PR notification state (tmp-then-rename)."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _pr_key(repo: str, number: int) -> str:
    """Stable key for a PR in the notification state dict."""
    return f"{repo}#{number}"


def _should_notify(pr_key: str, state: dict) -> bool:
    """
    Return True if this PR has not been notified within NOTIFICATION_COOLDOWN_HOURS.

    A PR with no prior notification record always passes the cooldown gate.
    """
    last_notified_str = state.get(pr_key, {}).get("last_notified_at")
    if last_notified_str is None:
        return True
    try:
        last_notified = datetime.fromisoformat(last_notified_str)
        now = datetime.now(timezone.utc)
        return (now - last_notified) >= timedelta(hours=NOTIFICATION_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# GitHub PR query
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PRInfo:
    number: int
    title: str
    state: str
    branch_name: str
    url: str
    merged_at: str | None
    created_at: str
    repo: str
    uow_id: str | None


def _query_github_prs(repo: str, state: str = "all", limit: int = 100) -> list[PRInfo]:
    """Query GitHub for PRs in the given repo. Returns PRs with extracted UoW IDs."""
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", state,
        "--limit", str(limit),
        "--json", "number,title,state,headRefName,url,mergedAt,createdAt",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        prs_raw = json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        log.error("gh pr list failed for %s: %s", repo, e.stderr)
        return []
    except json.JSONDecodeError as e:
        log.error("Failed to parse gh pr list output for %s: %s", repo, e)
        return []

    prs = []
    for pr in prs_raw:
        # Extract UoW ID from branch name
        branch = pr.get("headRefName", "")
        match = UOW_ID_PATTERN.search(branch)
        uow_id = f"uow_{match.group(1)}" if match else None

        prs.append(PRInfo(
            number=pr["number"],
            title=pr["title"],
            state=pr["state"],
            branch_name=branch,
            url=pr["url"],
            merged_at=pr.get("mergedAt"),
            created_at=pr.get("createdAt", ""),
            repo=repo,
            uow_id=uow_id,
        ))

    return prs


def _days_since_created(iso_timestamp: str) -> int:
    """Calculate days since the PR was created."""
    try:
        created = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - created).days
    except (ValueError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# Sweep logic
# ---------------------------------------------------------------------------

def sweep_prs(dry_run: bool = False) -> dict:
    """
    Sweep WOS-associated PRs and categorize them.

    Returns a dict with:
        - stale_open: PRs open >7 days
        - merged_pending_close: Merged PRs where UoW is not in 'done' state
        - clean: PRs that are in expected state
    """
    log.info("Starting WOS PR sweep (dry_run=%s)", dry_run)

    # Load UoW registry
    registry = WOSRegistry()

    # Query all UoWs that have completed (status = 'done' or terminal)
    # We need to check which ones have associated PRs
    all_prs = []
    for repo in REPOS_TO_SCAN:
        log.info("Querying PRs from %s", repo)
        prs = _query_github_prs(repo, state="all", limit=100)
        log.info("Found %d PRs in %s", len(prs), repo)
        all_prs.extend(prs)

    # Filter to only WOS-associated PRs (those with uow_id in branch name)
    wos_prs = [pr for pr in all_prs if pr.uow_id]
    log.info("Found %d WOS-associated PRs (with uow_YYYYMMDD_XXXXXX in branch)", len(wos_prs))

    # Categorize PRs
    stale_open = []
    merged_pending_close = []
    clean = []

    for pr in wos_prs:
        # Check if UoW exists in registry
        try:
            uow = registry.get(pr.uow_id)
        except Exception:
            log.warning("UoW %s not found in registry (PR #%d)", pr.uow_id, pr.number)
            uow = None

        # Stale open PRs
        if pr.state == "OPEN":
            days_open = _days_since_created(pr.created_at)
            if days_open > STALE_OPEN_THRESHOLD_DAYS:
                stale_open.append({
                    "uow_id": pr.uow_id,
                    "pr_number": pr.number,
                    "pr_url": pr.url,
                    "repo": pr.repo,
                    "opened_days_ago": days_open,
                    "title": pr.title,
                })
                log.info("Stale open PR: #%d (%s) - open for %d days", pr.number, pr.uow_id, days_open)
            else:
                clean.append({
                    "uow_id": pr.uow_id,
                    "pr_number": pr.number,
                    "state": pr.state,
                    "repo": pr.repo,
                })

        # Merged PRs where UoW is not done
        elif pr.state == "MERGED":
            if uow and uow.status != UoWStatus.DONE:
                merged_pending_close.append({
                    "uow_id": pr.uow_id,
                    "pr_number": pr.number,
                    "pr_url": pr.url,
                    "repo": pr.repo,
                    "merged_at": pr.merged_at,
                    "uow_status": str(uow.status),
                    "title": pr.title,
                })
                log.info(
                    "Merged PR with non-done UoW: #%d (%s) - UoW status: %s",
                    pr.number, pr.uow_id, uow.status
                )
            else:
                clean.append({
                    "uow_id": pr.uow_id,
                    "pr_number": pr.number,
                    "state": pr.state,
                    "repo": pr.repo,
                })
        else:
            # Closed PRs (not merged) are considered clean
            clean.append({
                "uow_id": pr.uow_id,
                "pr_number": pr.number,
                "state": pr.state,
                "repo": pr.repo,
            })

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "stale_open": stale_open,
        "merged_pending_close": merged_pending_close,
        "clean": clean,
    }

    log.info(
        "Sweep complete: %d stale open, %d merged pending close, %d clean",
        len(stale_open), len(merged_pending_close), len(clean)
    )

    return summary


def write_output(summary: dict, dry_run: bool = False):
    """Write sweep results to task-outputs directory."""
    output_dir = _task_outputs_dir()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_file = output_dir / f"wos-pr-sweeper-{timestamp}.json"

    if dry_run:
        print("DRY RUN: Would write to", output_file)
        print(json.dumps(summary, indent=2))
    else:
        with output_file.open("w") as f:
            json.dump(summary, f, indent=2)
        log.info("Wrote output to %s", output_file)


def emit_inbox_notification(summary: dict, dry_run: bool = False):
    """
    Write inbox notification for PRs requiring attention, respecting per-PR cooldown.

    Each PR is tracked in wos-pr-sweeper-state.json. A PR that was notified
    within NOTIFICATION_COOLDOWN_HOURS is suppressed to prevent inbox flooding
    on every 6-hour sweep run.
    """
    needs_attention_all = summary["stale_open"] + summary["merged_pending_close"]
    if not needs_attention_all:
        log.info("No action needed — skipping inbox notification")
        return

    state = _load_state()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Filter to PRs not recently notified
    stale_to_notify = [
        item for item in summary["stale_open"]
        if _should_notify(_pr_key(item["repo"], item["pr_number"]), state)
    ]
    merged_to_notify = [
        item for item in summary["merged_pending_close"]
        if _should_notify(_pr_key(item["repo"], item["pr_number"]), state)
    ]

    suppressed = len(needs_attention_all) - len(stale_to_notify) - len(merged_to_notify)
    if suppressed:
        log.info("Suppressed %d PRs within cooldown window (%dh)", suppressed, NOTIFICATION_COOLDOWN_HOURS)

    if not stale_to_notify and not merged_to_notify:
        log.info("All PRs needing attention are within cooldown — no notification sent")
        return

    inbox_dir = _inbox_dir()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    message_id = f"wos-pr-sweep-{timestamp}"
    inbox_file = inbox_dir / f"{message_id}.json"

    # Build message text
    lines = ["WOS PR Sweep Results\n"]
    if stale_to_notify:
        lines.append(f"**{len(stale_to_notify)} stale open PRs** (>7 days):")
        for item in stale_to_notify[:5]:  # Limit to first 5
            lines.append(f"  * PR #{item['pr_number']} ({item['repo']}) - {item['opened_days_ago']} days")
        if len(stale_to_notify) > 5:
            lines.append(f"  ... and {len(stale_to_notify) - 5} more")

    if merged_to_notify:
        lines.append(f"\n**{len(merged_to_notify)} merged PRs** with non-done UoWs:")
        for item in merged_to_notify[:5]:
            lines.append(f"  * PR #{item['pr_number']} ({item['repo']}) - UoW: {item['uow_status']}")
        if len(merged_to_notify) > 5:
            lines.append(f"  ... and {len(merged_to_notify) - 5} more")

    if suppressed:
        lines.append(f"\n({suppressed} additional PRs suppressed — notified within {NOTIFICATION_COOLDOWN_HOURS}h)")

    message = {
        "message_id": message_id,
        "chat_id": int(os.environ.get("ADMIN_CHAT_ID", "0")),
        "source": "wos_pr_sweep",
        "type": "wos_pr_sweep_result",
        "text": "\n".join(lines),
        "timestamp": now_iso,
        "category": "wos_pr_sweep_result",
        "data": {
            "stale_open_count": len(stale_to_notify),
            "merged_pending_close_count": len(merged_to_notify),
        },
    }

    if dry_run:
        print("DRY RUN: Would write inbox notification:")
        print(json.dumps(message, indent=2))
        print(f"DRY RUN: Would update state for {len(stale_to_notify) + len(merged_to_notify)} PRs")
    else:
        tmp_path = Path(str(inbox_file) + ".tmp")
        tmp_path.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(inbox_file)
        log.info("Wrote inbox notification to %s", inbox_file)

        # Update state: record last_notified_at for each notified PR
        for item in stale_to_notify:
            key = _pr_key(item["repo"], item["pr_number"])
            state[key] = {"last_notified_at": now_iso}
        for item in merged_to_notify:
            key = _pr_key(item["repo"], item["pr_number"])
            state[key] = {"last_notified_at": now_iso}

        _save_state(state)
        log.info("Updated notification state for %d PRs", len(stale_to_notify) + len(merged_to_notify))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="WOS PR Sweeper")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing files")
    args = parser.parse_args()

    # Check enabled gate
    if not _is_job_enabled("wos-pr-sweeper"):
        log.info("Job disabled in jobs.json — exiting")
        return 0

    # Run sweep
    summary = sweep_prs(dry_run=args.dry_run)

    # Write output
    write_output(summary, dry_run=args.dry_run)

    # Emit inbox notification if needed
    emit_inbox_notification(summary, dry_run=args.dry_run)

    log.info("WOS PR sweep complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
