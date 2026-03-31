#!/usr/bin/env python3
"""
GardenCaretaker Heartbeat — cron-driven scan-and-tend cycle for WOS registry.

Runs every 15 minutes. On each invocation:
1. Checks the enabled gate in jobs.json (Type C job — cron-direct dispatch).
2. Instantiates GardenCaretaker with the GitHubIssueSource for dcetlin/Lobster.
3. Calls run_reconciliation_cycle() — which runs scan() then tend() in sequence.
4. Logs a structured summary of seeded/qualified/archived/surfaced/reactivated counts.

Replaces the split responsibility of cultivator.py and issues-sweeper.py with
a single infrastructure polling loop. This is a Type C job (pure Python script
invoked directly by cron — no inbox message written, no LLM round-trip). The
enabled gate in jobs.json controls runtime enable/disable without touching cron.

Cron schedule (every 15 minutes):
    */15 * * * * cd $HOME && uv run ~/lobster/scheduled-tasks/garden-caretaker.py >> ~/lobster-workspace/logs/garden-caretaker.log 2>&1

Run standalone:
    uv run ~/lobster/scheduled-tasks/garden-caretaker.py [--dry-run]

Design reference: docs/wos-v2-design.md § GardenCaretaker
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("garden-caretaker")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO = "dcetlin/Lobster"
_JOB_NAME = "garden-caretaker"


# ---------------------------------------------------------------------------
# Path helpers — mirrors executor-heartbeat.py convention
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    return workspace / "orchestration" / "registry.db"


# ---------------------------------------------------------------------------
# jobs.json enabled gate — Type C dispatch path
# ---------------------------------------------------------------------------

def _is_job_enabled(job_name: str) -> bool:
    """Return True if the job is enabled in jobs.json, False if explicitly disabled.

    Defaults to True when:
    - jobs.json is absent
    - the job entry is missing
    - the file is unreadable or malformed

    Mirrors the gate logic in executor-heartbeat.py so Type C (cron-direct)
    jobs respect the same runtime enable/disable toggle as Type A/B jobs.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    jobs_file = workspace / "scheduled-jobs" / "jobs.json"
    try:
        data = json.loads(jobs_file.read_text())
        return bool(data.get("jobs", {}).get(job_name, {}).get("enabled", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the GardenCaretaker scan-and-tend cycle.

    Returns exit code: 0 on success, 1 on unhandled error.
    """
    parser = argparse.ArgumentParser(description="GardenCaretaker Heartbeat — WOS scan-and-tend cycle")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run qualification logic without writing to registry",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        log.info("GardenCaretaker heartbeat starting (DRY RUN)")
    else:
        log.info("GardenCaretaker heartbeat starting")

    # Gate 1: jobs.json enabled check
    if not _is_job_enabled(_JOB_NAME):
        log.info(
            "GardenCaretaker: job disabled in jobs.json (enabled=false) "
            "— skipping cycle. Set enabled=true in jobs.json to re-enable."
        )
        return 0

    # Resolve DB path and verify it exists
    db_path = _default_db_path()
    if not db_path.exists():
        log.error("Registry DB not found at %s — run install/migrate first", db_path)
        return 1

    if dry_run:
        log.info(
            "GardenCaretaker (DRY RUN): would run scan-and-tend cycle against %s "
            "using repo=%s, db=%s",
            _REPO, _REPO, db_path,
        )
        log.info("GardenCaretaker heartbeat complete (DRY RUN)")
        return 0

    # Instantiate dependencies
    from src.orchestration.github_issue_source import GitHubIssueSource
    from src.orchestration.garden_caretaker import GardenCaretaker
    from src.orchestration.registry import Registry

    registry = Registry(db_path)
    source = GitHubIssueSource(repo=_REPO)
    caretaker = GardenCaretaker(source=source, registry=registry)

    # Run the unified scan-and-tend cycle
    try:
        result = caretaker.run()
        log.info(
            "GardenCaretaker cycle complete: "
            "seeded=%d qualified=%d archived=%d surfaced_to_steward=%d reactivated=%d no_change=%d",
            result.get("seeded", 0),
            result.get("qualified", 0),
            result.get("archived", 0),
            result.get("surfaced_to_steward", 0),
            result.get("reactivated", 0),
            result.get("no_change", 0),
        )
    except Exception:
        log.exception("GardenCaretaker cycle failed")
        return 1

    log.info("GardenCaretaker heartbeat complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
