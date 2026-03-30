#!/usr/bin/env python3
"""
Steward Heartbeat — WOS Phase 2 cron-driven Steward agent.

Runs every 3 minutes. On each invocation executes three functions in order:

1. Startup sweep — find UoWs in `active` or `ready-for-executor` state that may
   be orphaned (crash recovery). Defined in full in #307; here a stub that
   logs any such UoWs for Phase 2.

2. Observation Loop — detect stalled `active` UoWs by checking `timeout_at`.
   Surfaces stalled UoWs back to the Steward via the 'ready-for-steward'
   transition with a 'stall_detected' audit entry. Full implementation is here.

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
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

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
_DEFAULT_STALL_SECONDS = 1800  # 30 minutes fallback when timeout_at is NULL


# ---------------------------------------------------------------------------
# Stall reason constants — canonical strings recognized by the Steward's
# re-entry classification table (#303).
# ---------------------------------------------------------------------------

class _StallReason(StrEnum):
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    STARTED_AT_NULL = "started_at_null"


# ---------------------------------------------------------------------------
# Named result type for the observation loop
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ObservationResult:
    """Pure result value returned by run_observation_loop."""
    checked: int
    stalled: int
    skipped_dry_run: int


# ---------------------------------------------------------------------------
# Clock protocol — injectable for tests
# ---------------------------------------------------------------------------

class _Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
        # UoW is a typed dataclass — use attribute access, not dict access.
        uow_id = uow.id

        # Stub classification: any active/rfe UoW without a recent heartbeat
        # is a candidate for orphan detection. Phase 3 will add proper timeout logic.
        classification = {
            "uow_id": uow_id,
            "status": uow.status,
            "classification": "executor_orphan" if uow.status == _STATUS_READY_FOR_EXECUTOR else "possibly_complete",
        }
        classified.append(classification)
        log.debug(
            "Startup sweep: UoW %s (status=%s) classified as %s",
            uow_id, uow.status, classification["classification"]
        )

    if classified and not dry_run:
        log.info(
            "Startup sweep: %d orphan candidate(s) found — see audit_log for details",
            len(classified)
        )

    return {"found": len(classified), "classified": classified}


# ---------------------------------------------------------------------------
# Phase 2: Observation Loop — stall detection for active UoWs (#306)
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime (UTC)."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _compute_elapsed(started_at: str | None, now: datetime) -> float | None:
    """Return elapsed seconds since started_at, or None if started_at is NULL."""
    if started_at is None:
        return None
    try:
        return (now - _parse_iso(started_at)).total_seconds()
    except (ValueError, TypeError):
        return None


def run_observation_loop(
    registry,
    dry_run: bool = False,
    clock: _Clock = _utc_now,
) -> ObservationResult:
    """
    Observation Loop — scan `active` UoWs for stalls and surface them to
    the Steward by transitioning to `ready-for-steward`.

    Stall detection logic (per #306 spec):

    - timeout_at not NULL: stall fires when now() >= timeout_at
    - timeout_at NULL, started_at not NULL: fall back to started_at + 1800s
    - timeout_at NULL, started_at NULL: immediate stall (started_at_null reason)

    Each detected stall:
    1. Writes a stall_detected audit entry (inside an atomic transaction).
    2. Transitions status active → ready-for-steward via optimistic lock.
    3. If rows_affected == 0 (race): skips silently — no duplicate audit entry.
    4. Idempotency guard: skips if audit_log already has stall_detected for
       the same timeout_at value.

    In dry_run mode: detects stalls but does NOT write audit entries or
    transition status. Returns stalled count as skipped_dry_run.

    Returns ObservationResult(checked, stalled, skipped_dry_run).
    """
    try:
        active_uows = registry.list_active_for_observation()
    except Exception as e:
        log.warning("Observation loop: failed to query registry — %s", e)
        return ObservationResult(checked=0, stalled=0, skipped_dry_run=0)

    now = clock()
    stalled = 0
    skipped_dry_run = 0

    for uow in active_uows:
        uow_id = uow.id
        timeout_at: str | None = uow.timeout_at
        started_at: str | None = None
        # started_at is not on the UoW dataclass — access raw via registry if needed.
        # The UoW dataclass doesn't expose started_at; we derive deadline from
        # timeout_at (preferred) or the fallback window from started_at (via DB query).
        # For the fallback path we need started_at — fetch it from DB directly.
        stall_reason: _StallReason | None = None
        deadline: datetime | None = None

        if timeout_at is not None:
            # Primary deadline: timeout_at set by Executor at claim time.
            try:
                deadline = _parse_iso(timeout_at)
            except (ValueError, TypeError):
                log.warning(
                    "Observation loop: UoW %s has unparseable timeout_at=%r — skipping",
                    uow_id, timeout_at,
                )
                continue

            if now >= deadline:
                stall_reason = _StallReason.TIMEOUT_EXCEEDED
        else:
            # Fallback: fetch started_at from DB for the 1800s window.
            # We fetch via registry to avoid opening a raw connection here.
            raw = _fetch_started_at(registry, uow_id)
            started_at = raw

            if started_at is None:
                # Both timeout_at and started_at are NULL — immediate stall.
                stall_reason = _StallReason.STARTED_AT_NULL
            else:
                try:
                    deadline = _parse_iso(started_at) + timedelta(seconds=_DEFAULT_STALL_SECONDS)
                except (ValueError, TypeError):
                    log.warning(
                        "Observation loop: UoW %s has unparseable started_at=%r — skipping",
                        uow_id, started_at,
                    )
                    continue

                if now >= deadline:
                    stall_reason = _StallReason.TIMEOUT_EXCEEDED

        if stall_reason is None:
            # UoW is running normally — no action, no log output.
            continue

        elapsed = _compute_elapsed(started_at, now)

        if dry_run:
            log.info(
                "Observation loop (DRY RUN): UoW %s would be surfaced as stalled "
                "(reason=%s, timeout_at=%s)",
                uow_id, stall_reason, timeout_at,
            )
            skipped_dry_run += 1
            continue

        rows = registry.record_stall_detected(
            uow_id=uow_id,
            stall_reason=str(stall_reason),
            started_at=started_at,
            timeout_at=timeout_at,
            output_ref=uow.output_ref,
            elapsed_seconds=elapsed,
        )

        if rows == 1:
            stalled += 1
            log.info(
                "Observation loop: stall detected — UoW %s transitioned to "
                "ready-for-steward (reason=%s, elapsed=%.0fs)",
                uow_id, stall_reason, elapsed if elapsed is not None else 0.0,
            )
        else:
            log.debug(
                "Observation loop: race on UoW %s — another component already "
                "advanced it (rows_affected=0)",
                uow_id,
            )

    return ObservationResult(checked=len(active_uows), stalled=stalled, skipped_dry_run=skipped_dry_run)


def _fetch_started_at(registry, uow_id: str) -> str | None:
    """
    Fetch started_at for a UoW by id. Returns None if the UoW is not found
    or started_at is NULL.

    started_at is not exposed on the UoW dataclass (it is an Executor-set
    field not needed by most callers). We read it directly from the Registry's
    connection here to avoid widening the UoW dataclass surface area.
    """
    conn = registry._connect()
    try:
        row = conn.execute(
            "SELECT started_at FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        if row is None:
            return None
        return row["started_at"]
    finally:
        conn.close()


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
            "Observation loop complete: %d active UoWs checked, %d stalled, "
            "%d skipped (dry-run)",
            obs_result.checked, obs_result.stalled, obs_result.skipped_dry_run,
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
