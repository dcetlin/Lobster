#!/usr/bin/env python3
"""
Main Staleness Check — detect when the live ~/lobster checkout's local
`main` has drifted from `origin/main`.

Problem context: the dispatcher and MCP server run off the live ~/lobster
checkout. That checkout is expected to always be a clean, up-to-date `main`.
A prior incident: a `git checkout` was run against a stale local `main`,
causing a sub-second file-flip on the live system because the checkout
briefly moved off the expected state. Staleness (or being on the wrong
branch entirely) was only discovered when it caused a risky operation to
misbehave — not proactively.

This script fetches origin, then classifies the live checkout into one of
four states:
    - clean        origin/main == local main, checkout is on main. Silent.
    - behind        origin/main has commits the local checkout lacks.
    - diverged      local main has commits not on origin/main (with or
                     without also being behind — either way, local state
                     cannot be trusted to match origin).
    - not_on_main   the checkout is not on `main` at all (detached HEAD or
                     a feature branch). This is the deeper anti-pattern
                     that caused the staleness incident.

On anything other than "clean", an alert is written to the Lobster inbox
addressed to ADMIN_CHAT_ID, rate-limited to one alert per calendar day (UTC)
so a persistent drift does not flood the inbox on every cron run.

Cron schedule (every 15 minutes — git fetch is cheap and this is a health
check that should catch drift promptly):
    */15 * * * * cd ~/lobster && uv run scheduled-tasks/main-staleness-check.py >> ~/lobster-workspace/scheduled-jobs/logs/main-staleness-check.log 2>&1 # LOBSTER-MAIN-STALENESS-CHECK

Type B dispatch: cron calls this script directly (no inbox/ message, no
dispatcher involvement, no LLM round-trip — this is a deterministic git
state comparison). The jobs.json enabled gate is checked at the top of
main() so runtime enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/main-staleness-check.py [--dry-run] [--repo-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

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
log = logging.getLogger("main-staleness-check")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "main-staleness-check"

# The live checkout being monitored — the dispatcher and MCP server run off this.
LIVE_REPO_DIR = Path(os.environ.get("LOBSTER_LIVE_REPO_DIR", str(Path.home() / "lobster")))

EXPECTED_BRANCH = "main"
REMOTE_NAME = "origin"
REMOTE_REF = f"{REMOTE_NAME}/{EXPECTED_BRANCH}"

# Telegram chat_id for drift alert delivery
ADMIN_CHAT_ID: int = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

# Sentinel file prefix — one file per calendar date prevents repeated alerts
# while drift persists across many 15-minute cron runs.
# Format: /tmp/main-staleness-alert-YYYY-MM-DD
STALENESS_SENTINEL_PREFIX = "/tmp/main-staleness-alert-"

# Classification labels
CLEAN = "clean"
BEHIND = "behind"
DIVERGED = "diverged"
NOT_ON_MAIN = "not_on_main"


class RepoState(NamedTuple):
    """Immutable snapshot of the live checkout's git state relative to origin/main."""

    current_branch: str | None  # None means detached HEAD
    ahead: int  # commits on local main not on origin/main
    behind: int  # commits on origin/main not on local main


# ---------------------------------------------------------------------------
# Git boundary functions — isolated side effects (subprocess calls)
# ---------------------------------------------------------------------------


def _run_git(repo_dir: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command scoped to repo_dir. Never raises on non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def fetch_origin(repo_dir: Path) -> tuple[bool, str]:
    """
    Fetch the latest origin/main ref. Returns (success, error_message).

    Does not fetch all branches — only main is relevant to this check, and
    scoping the fetch keeps it fast and avoids unrelated ref updates.
    """
    result = _run_git(repo_dir, ["fetch", REMOTE_NAME, EXPECTED_BRANCH])
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


def get_current_branch(repo_dir: Path) -> str | None:
    """
    Return the current branch name, or None if HEAD is detached.

    `git rev-parse --abbrev-ref HEAD` returns the literal string "HEAD"
    when detached — normalized to None here.
    """
    result = _run_git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch


def compute_ahead_behind(repo_dir: Path) -> tuple[int, int]:
    """
    Return (ahead, behind) commit counts between local main and origin/main.

    ahead:  commits reachable from local main but not from origin/main
    behind: commits reachable from origin/main but not from local main

    Raises RuntimeError if the git command fails (e.g. origin/main ref
    missing because fetch never ran).
    """
    result = _run_git(
        repo_dir, ["rev-list", "--left-right", "--count", f"{EXPECTED_BRANCH}...{REMOTE_REF}"]
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-list failed: {result.stderr.strip()}")
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        raise RuntimeError(f"unexpected git rev-list output: {result.stdout!r}")
    return int(parts[0]), int(parts[1])


def get_repo_state(repo_dir: Path) -> RepoState:
    """
    Build a RepoState snapshot for repo_dir. Caller must fetch_origin() first.

    ahead/behind are only computed when on `main` — off-main branches are
    already classified as NOT_ON_MAIN regardless of commit counts, so the
    comparison (which assumes local `main` exists and is meaningful) is
    skipped.
    """
    current_branch = get_current_branch(repo_dir)
    if current_branch != EXPECTED_BRANCH:
        return RepoState(current_branch=current_branch, ahead=0, behind=0)
    ahead, behind = compute_ahead_behind(repo_dir)
    return RepoState(current_branch=current_branch, ahead=ahead, behind=behind)


# ---------------------------------------------------------------------------
# Pure classification and messaging — no I/O
# ---------------------------------------------------------------------------


def classify_state(state: RepoState) -> str:
    """
    Classify a RepoState into one of: clean, behind, diverged, not_on_main.

    not_on_main takes priority — it is the deeper anti-pattern (the checkout
    isn't even tracking the expected branch), independent of ahead/behind.
    diverged takes priority over behind because any local commit not on
    origin means local state cannot be trusted to match origin, even if it
    is also behind.
    """
    if state.current_branch != EXPECTED_BRANCH:
        return NOT_ON_MAIN
    if state.ahead > 0:
        return DIVERGED
    if state.behind > 0:
        return BEHIND
    return CLEAN


def build_alert_text(classification: str, state: RepoState, repo_dir: Path) -> str:
    """Compose a human-readable alert body for a non-clean classification."""
    repo_label = str(repo_dir)

    if classification == NOT_ON_MAIN:
        branch_desc = state.current_branch or "detached HEAD"
        return (
            f"Live checkout drift: {repo_label} is NOT on {EXPECTED_BRANCH} "
            f"(currently: {branch_desc}).\n\n"
            "The dispatcher and MCP server run off this checkout — running "
            "off a non-main branch or detached HEAD is the anti-pattern that "
            "previously caused a stale-state file-flip.\n\n"
            f"Investigate before running any git operations against {repo_label}."
        )

    if classification == DIVERGED:
        detail = f"{state.ahead} local commit(s) not on {REMOTE_REF}"
        if state.behind > 0:
            detail += f", and {state.behind} commit(s) behind {REMOTE_REF}"
        return (
            f"Live checkout drift: {repo_label} has DIVERGED from {REMOTE_REF} "
            f"({detail}).\n\n"
            "Local main contains commits origin does not have. Do not "
            f"`git checkout` or reset {repo_label} without investigating "
            "first — this state cannot be trusted to match origin."
        )

    if classification == BEHIND:
        return (
            f"Live checkout drift: {repo_label} is BEHIND {REMOTE_REF} by "
            f"{state.behind} commit(s).\n\n"
            "The live dispatcher/MCP server may be running stale code. "
            f"Safe fast-forward: cd {repo_label} && git pull"
        )

    return f"{repo_label} is clean and up to date with {REMOTE_REF}."


# ---------------------------------------------------------------------------
# Alerting — inbox injection with per-day rate limiting
# (mirrors cc-usage-poller.py's cookie-expiry alert pattern)
# ---------------------------------------------------------------------------


def _inbox_dir() -> Path:
    """Return the inbox directory path, respecting LOBSTER_MESSAGES env override."""
    messages_base = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_base / "inbox"


def _staleness_alert_already_sent_today() -> bool:
    """
    Return True if a staleness-alert sentinel exists for today's date.

    Sentinel file format: /tmp/main-staleness-alert-YYYY-MM-DD
    One sentinel per calendar day prevents alert floods when drift persists
    across many 15-minute cron runs.
    """
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return Path(f"{STALENESS_SENTINEL_PREFIX}{today}").exists()


def _write_staleness_alert(text: str, dry_run: bool = False) -> None:
    """
    Write a staleness alert to the Lobster inbox and touch the daily sentinel.

    The dispatcher picks up the inbox message on its next cycle and delivers
    it to the user via Telegram. Fire-and-forget — no delivery confirmation.

    Rate-limited to one alert per calendar day via /tmp sentinel file.
    In dry_run mode: logs the intent but does not write files.
    """
    if _staleness_alert_already_sent_today():
        log.info("Staleness alert already sent today — skipping duplicate")
        return

    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "type": "message",
        "chat_id": ADMIN_CHAT_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
    }

    if dry_run:
        log.info("[dry-run] Would write staleness alert to inbox: %s", text[:80])
        return

    try:
        inbox = _inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        tmp_path = inbox / f"{msg_id}.json.tmp"
        dest_path = inbox / f"{msg_id}.json"
        tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp_path.rename(dest_path)
        log.info("Wrote staleness alert %s to inbox", msg_id)
    except Exception as exc:
        log.warning("Failed to write staleness alert to inbox: %s", exc)
        return

    try:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        Path(f"{STALENESS_SENTINEL_PREFIX}{today}").touch()
    except Exception as exc:
        log.warning("Failed to write staleness sentinel: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(dry_run: bool = False, repo_dir: Path | None = None) -> int:
    """
    Check the live checkout for drift from origin/main and alert if needed.

    Returns 0 on success (including "drift detected, alert sent/skipped").
    Returns 1 only on unexpected hard failures.
    """
    if not is_job_enabled(JOB_NAME):
        log.info("%s is disabled in jobs.json — skipping", JOB_NAME)
        return 0

    repo_dir = repo_dir or LIVE_REPO_DIR
    if not repo_dir.exists():
        log.error("Repo dir does not exist: %s", repo_dir)
        return 0  # transient/misconfiguration — do not hard-fail cron

    ok, err = fetch_origin(repo_dir)
    if not ok:
        log.error("git fetch failed for %s: %s — will retry on next cron run", repo_dir, err)
        return 0

    try:
        state = get_repo_state(repo_dir)
    except RuntimeError as exc:
        log.error("Failed to compute repo state for %s: %s", repo_dir, exc)
        return 0

    classification = classify_state(state)

    if classification == CLEAN:
        log.info("%s is clean and up to date with %s", repo_dir, REMOTE_REF)
        return 0

    text = build_alert_text(classification, state, repo_dir)
    log.warning("Drift detected (%s): %s", classification, text.splitlines()[0])
    _write_staleness_alert(text, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Detect drift between the live ~/lobster checkout and origin/main"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be done without writing any inbox alert or sentinel",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=None,
        help="Override the repo directory to check (default: $LOBSTER_LIVE_REPO_DIR or ~/lobster)",
    )
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run, repo_dir=args.repo_dir))
