#!/usr/bin/env python3
"""
File Size Monitor — observability script for bootup/config files.

Checks the line count of key Lobster configuration and bootup files against
fixed thresholds. When any file exceeds its threshold, files a GitHub issue
so the operator knows to prune it before it silently breaks the Read tool
(2,000-line default limit).

De-duplication: if a GitHub issue with the same warning title is already open,
the script skips filing a new one to avoid issue spam.

Mode: Type C (cron-direct, local-code). No inbox write, no LLM round-trip.
Cron schedule (weekly, Monday 07:00 UTC):
    0 7 * * 1  cd ~/lobster && uv run scheduled-tasks/file-size-monitor.py >> ~/lobster-workspace/scheduled-jobs/logs/file-size-monitor.log 2>&1 # LOBSTER-FILE-SIZE-MONITOR

Run standalone:
    uv run scheduled-tasks/file-size-monitor.py [--dry-run]

Root cause this addresses: sys.dispatcher.bootup.md grew to 2,403 lines
(past the Read tool's 2,000-line default limit) with no alert. The last 403
lines were silently invisible on startup. See bug #9 in project board.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_LOGS_DIR = _WORKSPACE / "scheduled-jobs" / "logs"

REPO = "SiderealPress/lobster"

# Files to monitor and their line-count thresholds.
# The Read tool's default limit is 2,000 lines. Files approaching that limit
# need operator attention before sections silently disappear on startup.
FILE_THRESHOLDS: dict[str, int] = {
    ".claude/sys.dispatcher.bootup.md": 2000,
    ".claude/sys.subagent.bootup.md": 2000,
    "CLAUDE.md": 1500,
    "oracle/decisions.md": 500,
    "oracle/learnings.md": 300,
}

GITHUB_LABEL = "observability"
LOG_NAME = "file-size-monitor"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _configure_logging() -> logging.Logger:
    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


log = _configure_logging()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def count_lines(path: Path) -> int | None:
    """Return the line count of a file, or None if the file does not exist."""
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def build_issue_title(rel_path: str, threshold: int, actual: int) -> str:
    """Return the canonical warning title for a threshold violation."""
    return f"warn: {rel_path} exceeds {threshold}-line threshold ({actual} lines)"


def check_files(repo_root: Path, thresholds: dict[str, int]) -> list[dict]:
    """
    Check each monitored file against its threshold.

    Returns a list of violation dicts (one per file that exceeds its threshold):
        {"rel_path": str, "threshold": int, "actual": int, "title": str}
    """
    violations = []
    for rel_path, threshold in thresholds.items():
        abs_path = repo_root / rel_path
        actual = count_lines(abs_path)
        if actual is None:
            log.warning("File not found, skipping: %s", abs_path)
            continue
        log.info("%s: %d lines (threshold %d)", rel_path, actual, threshold)
        if actual > threshold:
            violations.append({
                "rel_path": rel_path,
                "threshold": threshold,
                "actual": actual,
                "title": build_issue_title(rel_path, threshold, actual),
            })
    return violations


def fetch_open_issue_titles(repo: str) -> set[str]:
    """
    Return the set of open GitHub issue titles for the given repo.

    Uses `gh` CLI. Returns an empty set on failure so the script degrades
    gracefully (and will attempt to file issues rather than silently skip).
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open",
             "--limit", "200", "--json", "title"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error("gh issue list failed: %s", result.stderr.strip())
            return set()
        issues = json.loads(result.stdout)
        return {issue["title"] for issue in issues}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log.error("Could not fetch open issues: %s", exc)
        return set()


def ensure_label_exists(repo: str, label: str) -> None:
    """Create the GitHub label if it does not already exist. Best-effort."""
    try:
        result = subprocess.run(
            ["gh", "label", "list", "--repo", repo, "--json", "name"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0:
            existing = {lbl["name"] for lbl in json.loads(result.stdout)}
            if label in existing:
                return
        subprocess.run(
            ["gh", "label", "create", label, "--repo", repo,
             "--color", "0075ca", "--description", "Monitoring and observability"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        log.info("Created label '%s' in %s", label, repo)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        log.warning("Could not ensure label '%s' exists: %s", label, exc)


def file_github_issue(repo: str, title: str, body: str, label: str) -> bool:
    """
    File a GitHub issue. Returns True on success, False on failure.
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "create", "--repo", repo,
             "--title", title, "--body", body, "--label", label],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            log.info("Filed issue: %s", url)
            return True
        log.error("gh issue create failed: %s", result.stderr.strip())
        return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.error("Could not file issue: %s", exc)
        return False


def build_issue_body(violation: dict, repo_root: Path) -> str:
    """Return a GitHub issue body for a threshold violation."""
    rel_path = violation["rel_path"]
    threshold = violation["threshold"]
    actual = violation["actual"]
    overage = actual - threshold
    return (
        f"**File:** `{rel_path}`\n"
        f"**Line count:** {actual} (threshold: {threshold}, overage: +{overage})\n\n"
        f"The Read tool's default limit is 2,000 lines. Files that exceed their "
        f"threshold risk having their tail silently invisible on agent startup.\n\n"
        f"**Action required:** Prune or split `{rel_path}` to bring it back under "
        f"{threshold} lines. Do not auto-truncate — review content and remove "
        f"stale or redundant sections manually.\n\n"
        f"_Filed automatically by `scheduled-tasks/file-size-monitor.py` at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check bootup/config file sizes.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check files and log violations without filing GitHub issues.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = _REPO_ROOT

    log.info(
        "file-size-monitor start | repo_root=%s dry_run=%s",
        repo_root, args.dry_run,
    )

    violations = check_files(repo_root, FILE_THRESHOLDS)

    if not violations:
        log.info("All files within thresholds. Done.")
        return 0

    log.warning("%d file(s) exceed threshold: %s",
                len(violations), [v["rel_path"] for v in violations])

    if args.dry_run:
        for v in violations:
            log.info("[dry-run] Would file: %s", v["title"])
        return 0

    open_titles = fetch_open_issue_titles(REPO)
    ensure_label_exists(REPO, GITHUB_LABEL)

    filed = 0
    skipped = 0
    failed = 0

    for violation in violations:
        title = violation["title"]
        if title in open_titles:
            log.info("Issue already open, skipping: %s", title)
            skipped += 1
            continue
        body = build_issue_body(violation, repo_root)
        success = file_github_issue(REPO, title, body, GITHUB_LABEL)
        if success:
            filed += 1
        else:
            failed += 1

    log.info(
        "file-size-monitor done | violations=%d filed=%d skipped=%d failed=%d",
        len(violations), filed, skipped, failed,
    )

    # Exit non-zero if we failed to file any issue we tried to file.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
