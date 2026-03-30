#!/usr/bin/env python3
"""
Steward Heartbeat — WOS Phase 2 cron-driven Steward agent.

Runs every 3 minutes. On each invocation executes three functions in order:

1. Startup sweep — find UoWs in `active` or `ready-for-executor` state that may
   be orphaned (crash recovery). Defined in full in #307; here a stub that
   logs any such UoWs for Phase 2.

2. Observation Loop — detect stalled `active` UoWs by checking `timeout_at`.
   Defined in full in #306; here a stub that logs any timeout candidates.

3. Steward main loop — diagnose and prescribe for all `ready-for-steward` UoWs.
   This is the primary Phase 2 deliverable.

Cron schedule (every 3 minutes):
    */3 * * * * uv run ~/lobster/scheduled-tasks/steward-heartbeat.py

Run standalone:
    uv run ~/lobster/scheduled-tasks/steward-heartbeat.py [--dry-run]

Phase 2 dependency: requires schema migration to have been applied:
    uv run ~/lobster/scripts/migrate_add_steward_fields.py
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.steward import BOOTUP_CANDIDATE_GATE, run_steward_cycle

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("steward-heartbeat")


# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_STATUS_ACTIVE = "active"
_STATUS_READY_FOR_EXECUTOR = "ready-for-executor"
_STARTUP_SWEEP_ACTOR = "steward-heartbeat"


# ---------------------------------------------------------------------------
# Phase helper
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_db_path() -> Path:
    workspace = Path(os.environ.get(
        "LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"
    ))
    return workspace / "data" / "registry.db"


# ---------------------------------------------------------------------------
# Phase 1: Startup sweep (stub — full implementation in #307)
# ---------------------------------------------------------------------------

def run_startup_sweep(registry, dry_run: bool = False) -> dict:
    """
    Scan for UoWs in `active` or `ready-for-executor` state that may be orphaned.

    Phase 2 stub: logs any such UoWs and emits audit events so they are
    visible to the Steward main loop. Full crash recovery logic is in #307.

    Returns dict with keys: found (int), classified (list of dicts).
    """
    try:
        active_uows = registry.list(status=_STATUS_ACTIVE)
        rfe_uows = registry.list(status=_STATUS_READY_FOR_EXECUTOR)
    except Exception as e:
        log.warning("Startup sweep: failed to query registry — %s", e)
        return {"found": 0, "classified": []}

    candidates = active_uows + rfe_uows
    classified = []

    for uow in candidates:
        uow_id = uow["id"]
        started_at = uow.get("started_at")

        # Stub classification: any active/rfe UoW without a recent heartbeat
        # is a candidate for orphan detection. Phase 3 will add proper timeout logic.
        classification = {
            "uow_id": uow_id,
            "status": uow["status"],
            "started_at": started_at,
            "classification": "executor_orphan" if uow["status"] == _STATUS_READY_FOR_EXECUTOR else "possibly_complete",
        }
        classified.append(classification)
        log.debug(
            "Startup sweep: UoW %s (status=%s) classified as %s",
            uow_id, uow["status"], classification["classification"]
        )

    if classified and not dry_run:
        log.info(
            "Startup sweep: %d orphan candidate(s) found — see audit_log for details",
            len(classified)
        )

    return {"found": len(classified), "classified": classified}


# ---------------------------------------------------------------------------
# Phase 2: Observation loop (stub — full implementation in #306)
# ---------------------------------------------------------------------------

def run_observation_loop(registry, dry_run: bool = False) -> dict:
    """
    Detect stalled `active` UoWs by checking `timeout_at`.

    Phase 2 stub: scans active UoWs for timeout_at in the past and logs
    them. Full stall detection and `ready-for-steward` transition logic is
    in #306.

    Returns dict with keys: checked (int), stalled (int).
    """
    try:
        active_uows = registry.list(status=_STATUS_ACTIVE)
    except Exception as e:
        log.warning("Observation loop: failed to query registry — %s", e)
        return {"checked": 0, "stalled": 0}

    now = _now_iso()
    stalled = 0

    for uow in active_uows:
        timeout_at = uow.get("timeout_at")
        if timeout_at and timeout_at < now:
            stalled += 1
            log.info(
                "Observation loop: UoW %s has timed out (timeout_at=%s) — "
                "full stall handling is in #306",
                uow["id"], timeout_at
            )

    return {"checked": len(active_uows), "stalled": stalled}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Run the full Steward heartbeat: startup sweep, observation loop, main loop.

    Returns exit code: 0 on success, 1 on unhandled error.
    """
    parser = argparse.ArgumentParser(description="Steward Heartbeat — WOS Phase 2")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Diagnose UoWs without writing artifacts or transitioning state",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        log.info("Steward heartbeat starting (DRY RUN)")
    else:
        log.info("Steward heartbeat starting")

    log.info("BOOTUP_CANDIDATE_GATE = %s", BOOTUP_CANDIDATE_GATE)

    from src.orchestration.registry import Registry

    db_path = _default_db_path()
    if not db_path.exists():
        log.error("Registry DB not found at %s — run install/migrate first", db_path)
        return 1

    registry = Registry(db_path)

    # Phase 1: Startup sweep
    log.info("--- Phase 1: Startup sweep ---")
    try:
        sweep_result = run_startup_sweep(registry, dry_run=dry_run)
        log.info(
            "Startup sweep complete: %d orphan candidate(s) found",
            sweep_result["found"]
        )
    except Exception:
        log.exception("Startup sweep failed — continuing to observation loop")

    # Phase 2: Observation loop
    log.info("--- Phase 2: Observation loop ---")
    try:
        obs_result = run_observation_loop(registry, dry_run=dry_run)
        log.info(
            "Observation loop complete: %d active UoWs checked, %d stalled",
            obs_result["checked"], obs_result["stalled"]
        )
    except Exception:
        log.exception("Observation loop failed — continuing to Steward main loop")

    # Phase 3: Steward main loop
    log.info("--- Phase 3: Steward main loop ---")
    try:
        result = run_steward_cycle(
            registry=registry,
            dry_run=dry_run,
        )
        log.info(
            "Steward cycle complete: evaluated=%d prescribed=%d done=%d "
            "surfaced=%d skipped=%d race_skipped=%d",
            result["evaluated"],
            result["prescribed"],
            result["done"],
            result["surfaced"],
            result["skipped"],
            result["race_skipped"],
        )
    except RuntimeError as e:
        # Schema migration not applied — hard exit with clear message
        log.error("%s", e)
        return 1
    except Exception:
        log.exception("Steward main loop failed")
        return 1

    log.info("Steward heartbeat complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
