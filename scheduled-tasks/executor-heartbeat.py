#!/usr/bin/env python3
"""
Executor Heartbeat — WOS Phase 2 cron-driven Executor agent.

Runs every 3 minutes. On each invocation:
1. Recovers UoWs stuck in 'active' state for more than TTL_EXCEEDED_HOURS (4h)
   by marking them 'failed' so the Steward can re-diagnose.
2. Claims and dispatches all UoWs in `ready-for-executor` state via the
   6-step atomic claim sequence defined in src/orchestration/executor.py.

Each UoW is processed independently. A claim rejection (optimistic lock
failure) or runtime error on one UoW is logged and skipped — processing
continues for remaining UoWs.

Dispatch spawns a functional-engineer subagent via `claude -p` (subprocess,
synchronous). The Executor waits for the subprocess to complete before
transitioning the UoW to 'ready-for-steward' or 'failed'. The Steward picks
up the result on its next heartbeat cycle.

Cron schedule (every 3 minutes, offset by 90s from steward-heartbeat):
    */3 * * * * sleep 90 && cd ~/lobster && uv run scheduled-tasks/executor-heartbeat.py >> ~/lobster-workspace/scheduled-jobs/logs/executor-heartbeat.log 2>&1

Type B dispatch: cron calls this script directly (no inbox/ message, no dispatcher
involvement). The jobs.json enabled gate is checked at the top of main() so that
runtime enable/disable (wos start/stop) is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/executor-heartbeat.py [--dry-run]

Design reference: docs/wos-v2-design.md § Executor, § Phase 2 build plan PR4 (#305)
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
log = logging.getLogger("executor-heartbeat")


# ---------------------------------------------------------------------------
# jobs.json enabled gate — Type B dispatch path
# ---------------------------------------------------------------------------

def _is_job_enabled(job_name: str) -> bool:
    """
    Return True if the job is enabled in jobs.json, False if explicitly disabled.

    Defaults to True when:
    - jobs.json is absent
    - the job entry is missing
    - the file is unreadable or malformed

    This mirrors the gate logic in dispatch-job.sh so Type B (cron → script)
    jobs respect the same runtime enable/disable toggle as Type A jobs.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    jobs_file = workspace / "scheduled-jobs" / "jobs.json"
    try:
        data = json.loads(jobs_file.read_text())
        return bool(data.get("jobs", {}).get(job_name, {}).get("enabled", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# DB path helper — mirrors steward-heartbeat.py
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    workspace = Path(os.environ.get(
        "LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"
    ))
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    return workspace / "orchestration" / "registry.db"


# ---------------------------------------------------------------------------
# Executor cycle — claim and dispatch all ready-for-executor UoWs
# ---------------------------------------------------------------------------

def run_ttl_recovery(registry, dry_run: bool = False) -> list[str]:
    """
    Recover UoWs stuck in 'active' state for more than TTL_EXCEEDED_HOURS.

    In dry_run mode: queries but does NOT transition any UoW.
    Returns the list of recovered uow_ids (empty on dry_run or nothing to recover).
    """
    from src.orchestration.executor import TTL_EXCEEDED_HOURS, recover_ttl_exceeded_uows
    import sqlite3
    from datetime import datetime, timezone, timedelta

    if dry_run:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=TTL_EXCEEDED_HOURS)
        cutoff_iso = cutoff.isoformat()
        try:
            conn = sqlite3.connect(str(registry.db_path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT id FROM uow_registry
                    WHERE status = 'active'
                      AND started_at IS NOT NULL
                      AND started_at < ?
                    """,
                    (cutoff_iso,),
                ).fetchall()
                stalled = [r["id"] for r in rows]
            finally:
                conn.close()
        except Exception as e:
            log.warning("TTL recovery (DRY RUN): query failed — %s", e)
            return []
        if stalled:
            log.info(
                "TTL recovery (DRY RUN): %d stalled UoWs would be recovered — %s",
                len(stalled), stalled,
            )
        else:
            log.info("TTL recovery (DRY RUN): no stalled UoWs found")
        return []

    try:
        recovered = recover_ttl_exceeded_uows(registry)
    except Exception as e:
        log.warning("TTL recovery: unexpected error — %s", e)
        return []

    if recovered:
        log.info("TTL recovery: marked %d stalled UoW(s) as failed — %s", len(recovered), recovered)
    else:
        log.debug("TTL recovery: no stalled UoWs found")

    return recovered


def run_executor_cycle(registry, dry_run: bool = False) -> dict:
    """
    Claim and dispatch all UoWs in `ready-for-executor` state.

    Each UoW is processed independently. Errors on individual UoWs are
    caught, logged, and skipped — the cycle continues for remaining UoWs.

    In dry_run mode: queries ready-for-executor UoWs but does NOT claim
    or dispatch any of them.

    Returns a dict with keys: evaluated, dispatched, skipped, errors.
    """
    from src.orchestration.registry import UoWStatus

    try:
        ready_uows = registry.list(status=UoWStatus.READY_FOR_EXECUTOR)
    except Exception as e:
        log.warning("Executor cycle: failed to query ready-for-executor UoWs — %s", e)
        return {"evaluated": 0, "dispatched": 0, "skipped": 0, "errors": 0}

    evaluated = len(ready_uows)
    dispatched = 0
    skipped = 0
    errors = 0

    if dry_run:
        log.info(
            "Executor cycle (DRY RUN): %d ready-for-executor UoWs found — "
            "skipping all (dry-run mode)",
            evaluated,
        )
        return {"evaluated": evaluated, "dispatched": 0, "skipped": evaluated, "errors": 0}

    from src.orchestration.executor import Executor, _dispatch_via_inbox

    executor = Executor(registry, dispatcher=_dispatch_via_inbox)

    for uow in ready_uows:
        uow_id = uow.id
        try:
            result = executor.execute_uow(uow_id)
            dispatched += 1
            log.info(
                "Executor cycle: dispatched UoW %s (outcome=%s, executor_id=%s)",
                uow_id,
                result.outcome,
                result.executor_id,
            )
        except RuntimeError as e:
            # ClaimRejected — optimistic lock lost (another executor claimed first,
            # or status changed since we listed). Not an error — skip silently.
            log.debug(
                "Executor cycle: claim rejected for UoW %s — %s (skipping)",
                uow_id, e,
            )
            skipped += 1
        except Exception as e:
            log.error(
                "Executor cycle: unexpected error on UoW %s — %s",
                uow_id, e,
                exc_info=True,
            )
            errors += 1

    return {
        "evaluated": evaluated,
        "dispatched": dispatched,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Run the executor heartbeat: claim and dispatch all ready-for-executor UoWs.

    Returns exit code: 0 on success, 1 on unhandled error.
    """
    parser = argparse.ArgumentParser(description="Executor Heartbeat — WOS Phase 2")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query ready-for-executor UoWs without claiming or dispatching",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        log.info("Executor heartbeat starting (DRY RUN)")
    else:
        log.info("Executor heartbeat starting")

    # jobs.json enabled gate — respect runtime enable/disable toggled via
    # the dispatcher 'wos start/stop' commands or direct jobs.json edits.
    if not _is_job_enabled("executor-heartbeat"):
        log.info("Executor heartbeat: skipped (disabled in jobs.json)")
        return 0

    from src.orchestration.steward import is_bootup_candidate_gate_active
    log.info("BOOTUP_CANDIDATE_GATE = %s", is_bootup_candidate_gate_active())

    from src.orchestration.dispatcher_handlers import is_execution_enabled
    execution_enabled = is_execution_enabled()
    log.info("WOS execution_enabled = %s", execution_enabled)

    from src.orchestration.registry import Registry

    db_path = _default_db_path()
    if not db_path.exists():
        log.error("Registry DB not found at %s — run install/migrate first", db_path)
        return 1

    registry = Registry(db_path)

    # Phase 1: TTL recovery — always runs regardless of execution_enabled so
    # that stalled active UoWs are recovered even when dispatch is paused.
    run_ttl_recovery(registry, dry_run=dry_run)

    if not execution_enabled:
        log.info(
            "Executor heartbeat: execution disabled (wos-config.json execution_enabled=false) "
            "— skipping dispatch. Use 'wos start' to enable."
        )
        log.info("Executor heartbeat complete")
        return 0

    try:
        result = run_executor_cycle(registry, dry_run=dry_run)
        log.info(
            "Executor cycle complete: evaluated=%d dispatched=%d skipped=%d errors=%d",
            result["evaluated"],
            result["dispatched"],
            result["skipped"],
            result["errors"],
        )
    except Exception:
        log.exception("Executor cycle failed")
        return 1

    log.info("Executor heartbeat complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
