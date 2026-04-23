#!/usr/bin/env python3
"""
Executor Heartbeat — WOS recovery poller for missed or stuck UoWs.

Runs every 3 minutes. On each invocation:
1. Recovers UoWs stuck in 'active' state for more than TTL_EXCEEDED_HOURS (4h)
   by marking them 'failed' so the Steward can re-diagnose.
2. Recovery-dispatches UoWs in `ready-for-executor` state that have been waiting
   longer than RECOVERY_STALE_MINUTES — these are UoWs that were NOT picked up
   by the primary event-driven inbox dispatch path (e.g. due to a race condition,
   restart during the dispatch window, or dispatcher downtime).

Primary dispatch path: the executor writes a wos_execute message to ~/messages/inbox/
via _dispatch_via_inbox when a UoW transitions to ready-for-executor. The Lobster
dispatcher picks it up on the next dispatcher cycle (~seconds). The heartbeat
is a recovery net, not the primary trigger.

Each UoW is processed independently. A claim rejection (optimistic lock
failure) or runtime error on one UoW is logged and skipped — processing
continues for remaining UoWs.

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

from src.orchestration.paths import REGISTRY_DB

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


def _warn_if_legacy_registry_exists() -> None:
    """Log a warning if the deprecated legacy registry path exists and is non-empty.

    The canonical registry lives at ~/lobster-workspace/orchestration/registry.db.
    The legacy path (data/wos-registry.db) is removed by upgrade.sh Migration 86
    when its uow_registry table is empty. This function fires only if the file
    survived migration (non-empty or migration not yet run).
    """
    workspace = Path(os.environ.get(
        "LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"
    ))
    legacy_path = workspace / "data" / "wos-registry.db"
    if not legacy_path.exists():
        return
    size = legacy_path.stat().st_size
    if size == 0:
        # Zero-byte file — safe to ignore; Migration 86 handles non-zero files.
        return
    # Count UoWs to distinguish an empty-schema file from one with actual data.
    try:
        import sqlite3
        conn = sqlite3.connect(str(legacy_path), timeout=5.0)
        uow_count = conn.execute("SELECT COUNT(*) FROM uow_registry").fetchone()[0]
        conn.close()
    except Exception:
        uow_count = -1
    if uow_count == 0:
        log.info(
            "Legacy registry DB at %s exists (%d bytes) but has 0 UoWs — "
            "run upgrade.sh to apply Migration 86 and remove it.",
            legacy_path, size,
        )
    else:
        log.warning(
            "Legacy registry DB at %s exists and is non-empty (%d bytes, %d UoWs). "
            "Canonical path is %s. Run upgrade.sh (Migration 86) to safely relocate.",
            legacy_path, size, uow_count,
            workspace / "orchestration" / "registry.db",
        )


# ---------------------------------------------------------------------------
# Recovery threshold — UoWs not yet dispatched after this many minutes are
# considered missed by the primary event-driven inbox path and are eligible
# for recovery dispatch by the heartbeat.
#
# The primary inbox dispatch should pick up a UoW within seconds; 5 minutes
# is a generous window that covers dispatcher downtime or restart races
# while avoiding false-positive recovery of newly-written UoWs.
# ---------------------------------------------------------------------------

RECOVERY_STALE_MINUTES: int = 5


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
                    WHERE status IN ('active', 'executing')
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


def _filter_stale_uows(
    ready_uows: list,
    stale_minutes: int,
    is_orphan_fn=None,
) -> list:
    """
    Return UoWs eligible for heartbeat dispatch.

    Two dispatch paths are distinguished:

    1. Fresh UoWs (never been executor_orphan): pass through immediately.
       The primary event-driven inbox dispatch may have been missed entirely
       (e.g. dispatcher was down when the UoW was prescribed). The heartbeat
       should pick these up without waiting — the steward re-prescribes every
       ~2 min, so a time-based staleness gate would never open for a fresh UoW.

    2. Previously-orphaned UoWs (prior executor_orphan audit entry): apply
       the stale_minutes gate. These UoWs had a dispatch attempt that the
       primary path missed; the heartbeat is the recovery path and we wait
       to confirm the primary path is truly blocked.

    Args:
        ready_uows: List of UoW objects with an id and updated_at attribute.
        stale_minutes: Minimum age in minutes before an orphaned UoW is
            eligible for recovery dispatch. Not applied to fresh UoWs.
        is_orphan_fn: Optional callable(uow_id: str) -> bool. Returns True
            if the UoW has prior executor_orphan history. When None, all UoWs
            are treated as fresh (immediate dispatch).

    Returns:
        Filtered list of UoW objects eligible for dispatch (may be empty).
    """
    from datetime import datetime, timezone, timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    eligible = []
    for uow in ready_uows:
        # Determine whether this UoW has prior executor_orphan history.
        # If is_orphan_fn is None or raises, treat as fresh (safe default).
        try:
            previously_orphaned = is_orphan_fn(uow.id) if is_orphan_fn is not None else False
        except Exception:
            previously_orphaned = False

        if not previously_orphaned:
            # Fresh UoW — pass through immediately for dispatch.
            eligible.append(uow)
            continue

        # Previously-orphaned — apply the staleness gate.
        try:
            updated = datetime.fromisoformat(uow.updated_at)
            # Ensure timezone-aware comparison
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated < cutoff:
                eligible.append(uow)
        except (ValueError, TypeError, AttributeError):
            # If updated_at is missing or unparseable, treat as stale (safe default)
            eligible.append(uow)
    return eligible


def run_executor_cycle(registry, dry_run: bool = False) -> dict:
    """
    Dispatch UoWs in `ready-for-executor` state that were not picked up by the
    primary event-driven inbox path.

    Two eligibility rules are applied by _filter_stale_uows:

    - Fresh UoWs (no prior executor_orphan audit entry): dispatched immediately.
      These were never attempted — the primary inbox dispatch may have been missed
      entirely (e.g. dispatcher downtime when the UoW was prescribed).

    - Previously-orphaned UoWs (prior executor_orphan audit entry): dispatched
      only after RECOVERY_STALE_MINUTES have elapsed since updated_at. These had
      a missed dispatch attempt and need the staleness gate to confirm the primary
      path is truly blocked before re-dispatching.

    Each UoW is processed independently. Errors on individual UoWs are
    caught, logged, and skipped — the cycle continues for remaining UoWs.

    In dry_run mode: queries ready-for-executor UoWs but does NOT claim
    or dispatch any of them.

    Returns a dict with keys: evaluated, ready, stale, dispatched, skipped, errors.
    """
    from src.orchestration.registry import UoWStatus

    try:
        ready_uows = registry.list(status=UoWStatus.READY_FOR_EXECUTOR)
    except Exception as e:
        log.warning("Executor cycle: failed to query ready-for-executor UoWs — %s", e)
        return {"evaluated": 0, "ready": 0, "stale": 0, "dispatched": 0, "skipped": 0, "errors": 0}

    ready_count = len(ready_uows)
    eligible_uows = _filter_stale_uows(
        ready_uows,
        RECOVERY_STALE_MINUTES,
        is_orphan_fn=registry.has_executor_orphan_history,
    )
    eligible_count = len(eligible_uows)
    ineligible_count = ready_count - eligible_count

    if ineligible_count > 0:
        log.debug(
            "Executor cycle: %d ready-for-executor UoW(s) not yet eligible "
            "(orphaned but <=%d min old) — skipping (primary inbox dispatch expected)",
            ineligible_count, RECOVERY_STALE_MINUTES,
        )

    # Scaling governor — cap dispatch when no Attunement evidence at this scale.
    from src.orchestration.scaling_governor import ScalingGovernor
    governor = ScalingGovernor(registry.db_path)
    decision = governor.check(proposed_n=eligible_count)

    dispatched = 0
    skipped = 0
    errors = 0

    if dry_run:
        log.info(
            "Executor cycle (DRY RUN): %d ready-for-executor UoWs found, "
            "%d eligible — skipping all (dry-run mode)",
            ready_count, eligible_count,
        )
        if decision.capped:
            log.info(
                "ScalingGovernor (DRY RUN): would cap from %d to %d (reason: %s)",
                decision.proposed_n, decision.allowed_n, decision.cap_reason,
            )
        return {
            "evaluated": ready_count,
            "ready": ready_count,
            "stale": eligible_count,
            "dispatched": 0,
            "skipped": ready_count,
            "errors": 0,
            "governor_cap": decision.allowed_n if decision.capped else None,
            "attunement_scale": decision.attunement_scale,
        }

    if decision.capped:
        eligible_uows = eligible_uows[:decision.allowed_n]
        eligible_count = decision.allowed_n
        log.info(
            "ScalingGovernor: cap applied — %d eligible, dispatching %d (reason: %s)",
            decision.proposed_n, decision.allowed_n, decision.cap_reason,
        )

    if eligible_count == 0:
        log.debug("Executor cycle: no eligible UoWs to dispatch")
        return {
            "evaluated": ready_count,
            "ready": ready_count,
            "stale": 0,
            "dispatched": 0,
            "skipped": 0,
            "errors": 0,
        }

    log.info(
        "Executor cycle: %d eligible UoW(s) found — dispatching",
        eligible_count,
    )

    from src.orchestration.executor import Executor

    # Pass dispatcher=None so the dispatch table (_EXECUTOR_TYPE_TO_DISPATCHER)
    # activates — routes to _dispatch_via_inbox (event-driven path) for
    # functional-engineer, lobster-ops, and general executor types.
    executor = Executor(registry, dispatcher=None)

    for uow in eligible_uows:
        uow_id = uow.id
        try:
            result = executor.execute_uow(uow_id)
            dispatched += 1
            log.info(
                "Executor cycle (recovery): dispatched stale UoW %s "
                "(outcome=%s, executor_id=%s)",
                uow_id,
                result.outcome,
                result.executor_id,
            )
            # Cleanup: unregister the agent after successful dispatch to prevent
            # the "completed but still registered" state that causes agent backlog.
            # The agent is responsible for calling write_result on completion;
            # this cleanup removes the registration to prevent indefinite accumulation.
            if result.executor_id:
                try:
                    from src.agents.session_store import session_end
                    session_end(
                        id_or_task_id=result.executor_id,
                        status="completed",
                        result_summary=f"UoW {uow_id} dispatched successfully",
                    )
                    log.info(
                        "Executor cycle: unregistered agent %s (UoW %s completed)",
                        result.executor_id,
                        uow_id,
                    )
                except Exception as cleanup_err:
                    log.warning(
                        "Executor cycle: failed to unregister agent %s — %s "
                        "(UoW %s may be visible in agent backlog)",
                        result.executor_id,
                        cleanup_err,
                        uow_id,
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
        "evaluated": ready_count,
        "ready": ready_count,
        "stale": eligible_count,
        "dispatched": dispatched,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Run the executor heartbeat: recover TTL-exceeded UoWs and re-dispatch
    any ready-for-executor UoWs that were missed by the primary inbox dispatch.

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

    _warn_if_legacy_registry_exists()

    from src.orchestration.steward import is_bootup_candidate_gate_active
    log.info("BOOTUP_CANDIDATE_GATE = %s", is_bootup_candidate_gate_active())

    from src.orchestration.dispatcher_handlers import is_execution_enabled
    execution_enabled = is_execution_enabled()
    log.info("WOS execution_enabled = %s", execution_enabled)

    from src.orchestration.registry import Registry

    db_path = REGISTRY_DB
    if not db_path.exists():
        log.error("Registry DB not found at %s — run install/migrate first", db_path)
        return 1

    registry = Registry(db_path)

    # Phase 1: TTL recovery — always runs regardless of execution_enabled so
    # that stalled active UoWs are recovered even when dispatch is paused.
    run_ttl_recovery(registry, dry_run=dry_run)

    # Phase 1b: Heartbeat sidecar — write heartbeats for all in-flight UoWs.
    # Structural enforcement: heartbeats are written by the cron-driven executor
    # heartbeat regardless of whether the executing subagent calls write_heartbeat()
    # itself. This prevents false stall detection in the observation loop.
    # Runs always (regardless of execution_enabled) because active/executing UoWs
    # need heartbeats even when new dispatch is paused.
    if not dry_run:
        try:
            from src.orchestration.heartbeat_sidecar import write_heartbeats_for_active_uows
            hb_sidecar = write_heartbeats_for_active_uows(registry)
            if hb_sidecar.checked > 0 or hb_sidecar.errors > 0:
                log.info(
                    "Heartbeat sidecar: checked=%d written=%d skipped=%d errors=%d",
                    hb_sidecar.checked, hb_sidecar.written, hb_sidecar.skipped, hb_sidecar.errors,
                )
        except Exception:
            log.exception("Heartbeat sidecar failed — continuing (heartbeats are best-effort)")
    else:
        log.info("Heartbeat sidecar (DRY RUN): skipped")

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
            "Executor cycle complete: ready=%d stale=%d dispatched=%d skipped=%d errors=%d",
            result["ready"],
            result["stale"],
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
