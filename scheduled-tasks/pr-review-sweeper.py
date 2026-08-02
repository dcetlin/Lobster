#!/usr/bin/env -S uv run
"""
PR Review Sweeper — polls open PRs and dispatches oracle review for those without a review comment.

Runs every 15 minutes. On each invocation:
1. Lists open PRs on dcetlin/Lobster
2. Skips PRs newer than MIN_AGE_MINUTES (too new — avoid races with CI)
3. Skips PRs already dispatched in the current 2-hour window (state file gate)
4. Skips PRs that already have a committed terminal oracle verdict (has_final_verdict check)
5. Skips PRs that already have a Lobster review comment (has_review_comment check)
6. Classifies each eligible PR via src.agents.pr_classifier
7. Writes a pr_review_request inbox message so the dispatcher spawns oracle review

Note on step 3 vs step 4: the state-file gate (step 3) is short-lived (pruned after
STATE_PRUNE_HOURS) and only covers dispatches made by *this* sweeper. It does not know
about oracle reviews that completed and were committed to `oracle/verdicts/pr-{n}.md` —
whether triggered by this sweeper in a prior, now-pruned cycle, or by any other path
(e.g. a manual dispatch). Step 4 closes that gap by checking the durable, git-committed
verdict file directly, so a PR that already has a terminal `VERDICT: APPROVED` is never
re-dispatched regardless of state-file pruning (see issue #1491).

This is a Type B cron-direct script. It does not interact with the dispatcher directly —
it writes inbox messages that the dispatcher consumes via route_wos_message /
handle_pr_review_request.

Cron schedule (every 15 minutes):
    */15 * * * * cd ~/lobster && uv run scheduled-tasks/pr-review-sweeper.py >> ~/lobster-workspace/scheduled-jobs/logs/pr-review-sweeper.log 2>&1 # LOBSTER-PR-REVIEW-SWEEPER

Run standalone:
    uv run ~/lobster/scheduled-tasks/pr-review-sweeper.py [--dry-run]

WOS-UoW: uow_20260522_89c782
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from src.utils.inbox_write import _inbox_dir, _task_outputs_dir, write_crash_alert
from src.utils.jobs import is_job_enabled

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("pr-review-sweeper")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO = "dcetlin/Lobster"
MIN_AGE_MINUTES = 10
REVIEW_MARKER = "<!-- lobster-review -->"
STATE_FILE = Path("~/lobster-workspace/data/pr-review-sweeper-state.json").expanduser()
STATE_PRUNE_HOURS = 2

# Terminal oracle verdict line — see CLAUDE.md "PR Merge Gate" and
# .claude/agents/lobster-oracle.md. The oracle writes exactly this string as the
# first line of oracle/verdicts/pr-{number}.md when a PR is approved to merge.
VERDICT_APPROVED_LINE = "VERDICT: APPROVED"
ORACLE_VERDICTS_DIR = _REPO_ROOT / "oracle" / "verdicts"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load dispatch state from disk. Returns empty dict on missing/corrupt file.
    Prunes entries older than STATE_PRUNE_HOURS on every load."""
    if not STATE_FILE.exists():
        return {"dispatched": {}}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state = raw if isinstance(raw, dict) else {"dispatched": {}}
        if "dispatched" not in state:
            state["dispatched"] = {}
        # Prune stale entries
        cutoff = datetime.now(timezone.utc) - timedelta(hours=STATE_PRUNE_HOURS)
        pruned = {
            pr_num: ts
            for pr_num, ts in state["dispatched"].items()
            if datetime.fromisoformat(ts) > cutoff
        }
        state["dispatched"] = pruned
        return state
    except Exception as exc:
        log.warning("Could not read state file %s: %s — starting fresh", STATE_FILE, exc)
        return {"dispatched": {}}


def save_state(state: dict) -> None:
    """Atomically persist dispatch state."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# PR helpers
# ---------------------------------------------------------------------------

def pr_age_minutes(created_at: str) -> float:
    """Return age of PR in minutes from its ISO creation timestamp."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 60
    except (ValueError, AttributeError):
        return float("inf")


def has_final_verdict(pr_number: int) -> bool:
    """Return True iff a terminal oracle verdict (VERDICT: APPROVED) is already
    committed for this PR at oracle/verdicts/pr-{number}.md.

    This is the durable, git-committed source of truth for "has this PR already
    been reviewed to completion" — unlike the state file (pruned after
    STATE_PRUNE_HOURS) or has_review_comment() (depends on a PR comment marker
    that is not posted in every review configuration), the verdict file persists
    for as long as the PR stays open and is unaffected by sweeper-cycle timing.

    A missing file, an unreadable file, or a non-terminal verdict (e.g.
    VERDICT: NEEDS_CHANGES, which means a fix round is in progress and a fresh
    review may legitimately be needed) all return False — i.e. do not skip.
    """
    verdict_path = ORACLE_VERDICTS_DIR / f"pr-{pr_number}.md"
    if not verdict_path.exists():
        return False
    try:
        first_line = verdict_path.read_text(encoding="utf-8").splitlines()[0].strip()
    except Exception as exc:
        log.warning("has_final_verdict: could not read %s — %s — treating as no verdict", verdict_path, exc)
        return False
    return first_line == VERDICT_APPROVED_LINE


def has_review_comment(pr_number: int) -> bool:
    """Return True if the PR already has a Lobster review comment (fail-safe: True on error)."""
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view", str(pr_number),
                "--repo", REPO,
                "--comments",
                "--json", "comments",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        comments = data.get("comments", [])
        return any(REVIEW_MARKER in (c.get("body") or "") for c in comments)
    except Exception as exc:
        log.warning("has_review_comment: gh pr view failed for PR #%d — %s — fail-safe True", pr_number, exc)
        return True  # fail-safe: don't double-dispatch on error


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch_review(pr_number: int, pr_title: str, review_type: str, prescribed_skills: list[str], dry_run: bool) -> None:
    """Write a pr_review_request inbox message for the dispatcher to consume."""
    now_iso = datetime.now(timezone.utc).isoformat()
    message_id = f"{int(time.time() * 1000)}_{uuid4().hex[:8]}"
    chat_id = int(os.environ.get("ADMIN_CHAT_ID", os.environ.get("LOBSTER_ADMIN_CHAT_ID", "0")))

    msg = {
        "message_id": message_id,
        "chat_id": chat_id,
        "source": "pr_review_sweeper",
        "type": "pr_review_request",
        "text": f"Review PR #{pr_number}: {pr_title}",
        "timestamp": now_iso,
        "data": {
            "pr_number": pr_number,
            "pr_title": pr_title,
            "repo": REPO,
            "review_type": review_type,
            "prescribed_skills": prescribed_skills,
        },
    }

    if dry_run:
        log.info("DRY RUN: would write pr_review_request for PR #%d (%s)", pr_number, review_type)
        print(json.dumps(msg, indent=2))
        return

    inbox_dir = _inbox_dir()
    inbox_file = inbox_dir / f"{message_id}.json"
    tmp_path = Path(str(inbox_file) + ".tmp")
    tmp_path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(inbox_file)
    log.info("Dispatched review for PR #%d → %s (review_type=%s)", pr_number, inbox_file.name, review_type)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> int:
    if not is_job_enabled("pr-review-sweeper"):
        log.info("Job disabled in jobs.json — exiting")
        return 0

    # List open PRs
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", REPO,
                "--state", "open",
                "--json", "number,title,createdAt",
                "--limit", "50",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        prs = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        log.error("gh pr list failed: %s", exc)
        return 1

    state = load_state()

    scanned = len(prs)
    dispatched = 0
    skipped_has_review = 0
    skipped_too_new = 0
    skipped_already_dispatched = 0
    skipped_final_verdict = 0

    for pr in prs:
        pr_number = pr["number"]
        pr_title = pr.get("title", "")
        created_at = pr.get("createdAt", "")

        age_min = pr_age_minutes(created_at)
        if age_min < MIN_AGE_MINUTES:
            log.debug("PR #%d too new (%.1f min) — skipping", pr_number, age_min)
            skipped_too_new += 1
            continue

        pr_key = str(pr_number)
        if pr_key in state["dispatched"]:
            log.debug("PR #%d already dispatched in this window — skipping", pr_number)
            skipped_already_dispatched += 1
            continue

        if has_final_verdict(pr_number):
            log.debug("PR #%d already has a terminal oracle verdict (%s) — skipping", pr_number, VERDICT_APPROVED_LINE)
            skipped_final_verdict += 1
            continue

        if has_review_comment(pr_number):
            log.debug("PR #%d already has a review comment — skipping", pr_number)
            skipped_has_review += 1
            continue

        # Classify the PR for routing
        try:
            from src.agents.pr_classifier import classify
            classification = classify(pr_number, REPO)
            review_type = classification.review_type
            prescribed_skills = list(classification.prescribed_skills)
        except Exception as exc:
            log.warning("classify failed for PR #%d: %s — defaulting to oracle_only", pr_number, exc)
            review_type = "oracle_only"
            prescribed_skills = ["lobster-oracle"]

        dispatch_review(pr_number, pr_title, review_type, prescribed_skills, dry_run)
        state["dispatched"][pr_key] = datetime.now(timezone.utc).isoformat()
        dispatched += 1

    if not dry_run:
        save_state(state)

    # Write task output
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_line = (
        f"pr-review-sweeper run at {run_at}\n"
        f"Scanned: {scanned} PRs | Dispatched: {dispatched} reviews | "
        f"Skipped (has review): {skipped_has_review} | "
        f"Skipped (too new): {skipped_too_new} | "
        f"Skipped (already dispatched): {skipped_already_dispatched} | "
        f"Skipped (final verdict): {skipped_final_verdict}"
    )
    log.info(summary_line)

    if not dry_run:
        output_dir = _task_outputs_dir()
        date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_file = output_dir / f"pr-review-sweeper-{date_prefix}.txt"
        output_file.write_text(summary_line, encoding="utf-8")
        log.info("Wrote task output to %s", output_file)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PR Review Sweeper")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing files")
    args = parser.parse_args()
    try:
        sys.exit(main(dry_run=args.dry_run))
    except Exception as exc:
        log.exception("pr-review-sweeper crashed")
        write_crash_alert("pr-review-sweeper", exc, "")
        sys.exit(1)
