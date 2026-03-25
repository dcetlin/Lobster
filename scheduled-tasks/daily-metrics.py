#!/usr/bin/env python3
"""
Daily Artifact Metrics Report — Lobster Scheduled Job
======================================================

Collects a 24-hour snapshot of Lobster's artifact output and delivers a
Telegram-friendly summary to Dan each morning.

Metrics collected (no new instrumentation needed):
- GitHub issues: opened today, closed today, total open
- Agent sessions: launched, completed, still running (from agent_sessions.db)
- Git activity on ~/lobster/: commits, files changed, lines added/removed

Design: v1-minimal. Pure data collectors compose into a single formatted message.
All side effects (Telegram delivery, task output) are isolated to the final step.

Run standalone:
    uv run ~/lobster/scheduled-tasks/daily-metrics.py
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Pure data helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def since_iso(hours: int = 24) -> str:
    """Return an ISO 8601 UTC timestamp for N hours ago."""
    return (now_utc() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return now_utc().strftime("%Y-%m-%d")


def run_cmd(args: list[str], cwd: str | None = None, timeout: int = 30) -> str:
    """Run a subprocess and return stdout. Returns empty string on failure."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# GitHub metrics
# ---------------------------------------------------------------------------

def collect_github_metrics(repo: str, since: str) -> dict:
    """
    Collect GitHub issue metrics for the past 24 hours.

    Returns:
        opened_count: issues created in the window
        closed_count: issues closed in the window
        total_open: current open issue count
    """
    # Issues opened since the cutoff (state=open OR recently opened)
    opened_raw = run_cmd([
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,createdAt",
        "--limit", "100",
    ])
    opened_count = _count_issues_since(opened_raw, since, "createdAt")

    # Issues closed since the cutoff
    closed_raw = run_cmd([
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "closed",
        "--json", "number,closedAt",
        "--limit", "100",
    ])
    closed_count = _count_issues_since(closed_raw, since, "closedAt")

    # Total open issues (current snapshot)
    total_open_raw = run_cmd([
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number",
        "--limit", "500",
    ])
    total_open = _count_total(total_open_raw)

    return {
        "opened": opened_count,
        "closed": closed_count,
        "total_open": total_open,
    }


def _parse_issue_list(raw: str) -> list[dict]:
    """Parse a JSON array from gh issue list. Returns empty list on failure."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _count_issues_since(raw: str, since: str, field: str) -> int:
    """Count issues where `field` timestamp is >= `since`."""
    issues = _parse_issue_list(raw)
    cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
    count = 0
    for issue in issues:
        ts_str = issue.get(field)
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts >= cutoff:
                count += 1
        except ValueError:
            continue
    return count


def _count_total(raw: str) -> int:
    """Count items in a JSON array."""
    items = _parse_issue_list(raw)
    return len(items)


# ---------------------------------------------------------------------------
# Agent session metrics
# ---------------------------------------------------------------------------

def collect_session_metrics(since: str) -> dict:
    """
    Query agent_sessions.db for session counts in the past 24 hours.

    Returns:
        launched: sessions spawned in the window
        completed: sessions that finished successfully in the window
        failed: sessions that failed in the window
        still_running: sessions currently in 'running' status
    """
    db_path = Path(
        os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")
    ) / "config" / "agent_sessions.db"

    if not db_path.is_file():
        return {"launched": 0, "completed": 0, "failed": 0, "still_running": 0, "unavailable": True}

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Sessions spawned in the past 24h
        row = conn.execute(
            "SELECT COUNT(*) as n FROM agent_sessions WHERE spawned_at >= ?",
            (since,),
        ).fetchone()
        launched = row["n"] if row else 0

        # Sessions completed in the past 24h
        row = conn.execute(
            "SELECT COUNT(*) as n FROM agent_sessions WHERE status = 'completed' AND completed_at >= ?",
            (since,),
        ).fetchone()
        completed = row["n"] if row else 0

        # Sessions failed in the past 24h
        row = conn.execute(
            "SELECT COUNT(*) as n FROM agent_sessions WHERE status = 'failed' AND completed_at >= ?",
            (since,),
        ).fetchone()
        failed = row["n"] if row else 0

        # Currently running (no completed_at, status = running)
        row = conn.execute(
            "SELECT COUNT(*) as n FROM agent_sessions WHERE status = 'running'",
        ).fetchone()
        still_running = row["n"] if row else 0

        conn.close()
        return {
            "launched": launched,
            "completed": completed,
            "failed": failed,
            "still_running": still_running,
            "unavailable": False,
        }
    except Exception:
        return {"launched": 0, "completed": 0, "failed": 0, "still_running": 0, "unavailable": True}


# ---------------------------------------------------------------------------
# Git activity metrics
# ---------------------------------------------------------------------------

def collect_git_metrics(repo_path: str, since: str) -> dict:
    """
    Collect git activity from the lobster repo for the past 24 hours.

    Returns:
        commits: number of commits
        files_changed: total unique files changed
        lines_added: total insertions
        lines_deleted: total deletions
        authors: distinct committer names
    """
    # List commits since the cutoff
    log_raw = run_cmd(
        ["git", "log", f"--since={since}", "--oneline", "--no-merges"],
        cwd=repo_path,
    )
    commits = len([l for l in log_raw.splitlines() if l.strip()]) if log_raw else 0

    if commits == 0:
        return {
            "commits": 0,
            "files_changed": 0,
            "lines_added": 0,
            "lines_deleted": 0,
            "authors": [],
        }

    # Aggregated diff stats for all commits in the window
    stat_raw = run_cmd(
        ["git", "log", f"--since={since}", "--no-merges", "--numstat", "--pretty=format:"],
        cwd=repo_path,
    )
    files_changed, lines_added, lines_deleted = _parse_numstat(stat_raw)

    # Distinct authors
    authors_raw = run_cmd(
        ["git", "log", f"--since={since}", "--no-merges", "--format=%an"],
        cwd=repo_path,
    )
    authors = sorted(set(a.strip() for a in authors_raw.splitlines() if a.strip()))

    return {
        "commits": commits,
        "files_changed": files_changed,
        "lines_added": lines_added,
        "lines_deleted": lines_deleted,
        "authors": authors,
    }


def _parse_numstat(raw: str) -> tuple[int, int, int]:
    """
    Parse git --numstat output into (files_changed, lines_added, lines_deleted).
    Lines look like: '12\t3\tpath/to/file' or '-\t-\tpath' (binary files).
    """
    files: set[str] = set()
    added = 0
    deleted = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ins_str, del_str, filepath = parts[0], parts[1], parts[2]
        files.add(filepath)
        try:
            added += int(ins_str)
        except ValueError:
            pass  # binary file marker '-'
        try:
            deleted += int(del_str)
        except ValueError:
            pass
    return len(files), added, deleted


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_report(
    date: str,
    github: dict,
    sessions: dict,
    git: dict,
) -> str:
    """
    Compose a Telegram-friendly summary from the collected metrics.
    Uses plain text formatting suitable for Telegram (no markdown headers).
    """
    lines = [
        f"Daily metrics — {date}",
        "",
    ]

    # GitHub section
    lines.append("GitHub (dcetlin/Lobster, past 24h)")
    lines.append(f"  Issues opened: {github['opened']}")
    lines.append(f"  Issues closed: {github['closed']}")
    lines.append(f"  Total open:    {github['total_open']}")

    lines.append("")

    # Agent sessions section
    lines.append("Agent sessions (past 24h)")
    if sessions.get("unavailable"):
        lines.append("  (session store not available)")
    else:
        lines.append(f"  Launched:      {sessions['launched']}")
        lines.append(f"  Completed:     {sessions['completed']}")
        if sessions["failed"] > 0:
            lines.append(f"  Failed:        {sessions['failed']}")
        lines.append(f"  Still running: {sessions['still_running']}")

    lines.append("")

    # Git section
    lines.append("Git activity — ~/lobster/ (past 24h)")
    if git["commits"] == 0:
        lines.append("  No commits")
    else:
        lines.append(f"  Commits:       {git['commits']}")
        lines.append(f"  Files changed: {git['files_changed']}")
        lines.append(f"  Lines:         +{git['lines_added']} / -{git['lines_deleted']}")
        if git["authors"]:
            lines.append(f"  Authors:       {', '.join(git['authors'])}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery via Claude subagent
# ---------------------------------------------------------------------------

def deliver(summary: str, date: str) -> None:
    """
    Deliver the metrics summary to Telegram and write task output.
    Both calls are made in a single Claude invocation to avoid partial delivery.
    """
    chat_id = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
    prompt = f"""You are delivering a daily metrics report. Make exactly these two calls and then stop:

1. Call send_reply with:
   - chat_id: {chat_id}
   - source: "telegram"
   - text: {json.dumps(summary)}

2. Call write_task_output with:
   - job_name: "daily-metrics"
   - output: "Daily metrics for {date} delivered to Telegram."
   - status: "success"

Make both calls, then stop. No commentary.
"""
    subprocess.run(
        [
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
            "--max-turns", "5",
        ],
        timeout=120,
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run() -> int:
    """
    Execute the daily metrics pipeline.
    Pure data collection feeds into a single formatted message.
    Returns exit code: 0 for success, 1 for failure.
    """
    date = today_iso()
    since = since_iso(hours=24)

    print(f"[{date}] Starting daily metrics report (since {since})")

    repo = "dcetlin/Lobster"
    lobster_path = str(Path.home() / "lobster")

    print("Collecting GitHub metrics...")
    github = collect_github_metrics(repo, since)

    print("Collecting agent session metrics...")
    sessions = collect_session_metrics(since)

    print("Collecting git activity metrics...")
    git = collect_git_metrics(lobster_path, since)

    summary = format_report(date, github, sessions, git)

    print("--- Report ---")
    print(summary)
    print("--------------")

    print("Delivering to Telegram...")
    deliver(summary, date)

    print(f"[{date}] Daily metrics complete")
    return 0


if __name__ == "__main__":
    sys.exit(run())
