#!/usr/bin/env python3
"""
Executor Heartbeat — WOS Phase 2 cron-driven Executor agent.

Runs every 3 minutes. On each invocation, claims and dispatches all UoWs
in `ready-for-executor` state via the 6-step atomic claim sequence defined
in src/orchestration/executor.py.

Each UoW is processed independently. A claim rejection (optimistic lock
failure) or runtime error on one UoW is logged and skipped — processing
continues for remaining UoWs.

Dispatch is fire-and-forget: the Executor writes a `wos_execute` message to
~/messages/inbox/ so the Lobster dispatcher (main Claude loop) spawns a
subagent. The subagent executes and writes a result.json. The Steward
picks up the result on its next heartbeat cycle.

Cron schedule (every 3 minutes, offset by 90s from steward-heartbeat):
    */3 * * * * sleep 90 && uv run ~/lobster/scheduled-tasks/executor-heartbeat.py

Run standalone:
    uv run ~/lobster/scheduled-tasks/executor-heartbeat.py [--dry-run]

Design reference: docs/wos-v2-design.md § Executor, § Phase 2 build plan PR4 (#305)
"""

from __future__ import annotations

import argparse
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

    from src.orchestration.executor import Executor

    executor = Executor(registry)

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

    from src.orchestration.registry import Registry

    db_path = _default_db_path()
    if not db_path.exists():
        log.error("Registry DB not found at %s — run install/migrate first", db_path)
        return 1

    registry = Registry(db_path)

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
