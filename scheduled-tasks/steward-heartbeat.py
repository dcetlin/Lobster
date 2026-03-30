#!/usr/bin/env python3
"""
Steward Heartbeat — WOS Phase 2 cron-driven Steward agent.

Runs every 3 minutes. On each invocation executes three functions in order:

1. Startup sweep — crash recovery. Scans `active`, `ready-for-executor`, and
   `diagnosing` UoWs and surfaces orphans back to the Steward via
   'ready-for-steward' transitions with 'startup_sweep' audit entries.
   Full implementation is here (#307).

2. Observation Loop — detect stalled `active` UoWs by checking `timeout_at`.
   Surfaces stalled UoWs back to the Steward via the 'ready-for-steward'
   transition with a 'stall_detected' audit entry. Full implementation is here.

3. Steward main loop — diagnose and prescribe for all `ready-for-steward` UoWs.
   This is the primary Phase 2 deliverable.

Note: A separate 15-minute periodic sweep process (as mentioned in the design
doc) is deferred to Phase 3. The startup sweep running on every 3-minute
heartbeat invocation achieves finer coverage.

Cron schedule (every 3 minutes):
    */3 * * * * uv run ~/lobster/scheduled-tasks/steward-heartbeat.py

Run standalone:
    uv run ~/lobster/scheduled-tasks/steward-heartbeat.py [--dry-run]

Phase 2 dependency: requires schema migration to have been applied:
    uv run ~/lobster/scripts/migrate_add_steward_fields.py
"""

from __future__ import annotations

import argparse
import json
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
_STATUS_DIAGNOSING = "diagnosing"
_STARTUP_SWEEP_ACTOR = "steward_startup"
_DEFAULT_STALL_SECONDS = 1800  # 30 minutes fallback when timeout_at is NULL
_EXECUTOR_ORPHAN_THRESHOLD_SECONDS = 3600  # 1 hour: ready-for-executor age threshold


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
# Startup sweep result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StartupSweepResult:
    """Pure result value returned by run_startup_sweep."""
    active_swept: int
    executor_orphans_swept: int
    diagnosing_swept: int
    skipped_dry_run: int


# ---------------------------------------------------------------------------
# Phase 1: Startup sweep — crash recovery (#307)
# ---------------------------------------------------------------------------

def _classify_active_uow(output_ref: str | None) -> tuple[str, dict]:
    """
    Classify an active UoW by examining output_ref.

    Returns (classification, extra_fields) where extra_fields contains
    file mtime info for possibly_complete, or is empty for other cases.

    Classification values:
    - possibly_complete: output_ref exists and is non-empty
    - crashed_zero_bytes: output_ref exists but is 0 bytes
    - crashed_output_ref_missing: output_ref path written but file missing
    - crashed_no_output_ref: output_ref IS NULL
    """
    if output_ref is None:
        return "crashed_no_output_ref", {}

    if not os.path.isabs(output_ref):
        log.warning(
            "Startup sweep: output_ref %r is not an absolute path — "
            "classifying as crashed_no_output_ref",
            output_ref,
        )
        return "crashed_no_output_ref", {}

    try:
        exists = os.path.exists(output_ref)
    except (OSError, ValueError):
        exists = False

    if not exists:
        return "crashed_output_ref_missing", {}

    try:
        size = os.path.getsize(output_ref)
    except OSError:
        return "crashed_output_ref_missing", {}

    if size == 0:
        return "crashed_zero_bytes", {}

    # Non-empty file — possibly_complete; include mtime signal for Steward.
    try:
        mtime = os.path.getmtime(output_ref)
        mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        mtime_iso = mtime_dt.isoformat()
        age_seconds = int((datetime.now(timezone.utc) - mtime_dt).total_seconds())
    except OSError:
        mtime_iso = None
        age_seconds = None

    return "possibly_complete", {
        "output_ref_mtime": mtime_iso,
        "output_ref_age_seconds": age_seconds,
    }


def run_startup_sweep(
    registry,
    dry_run: bool = False,
    orphan_threshold_seconds: int = _EXECUTOR_ORPHAN_THRESHOLD_SECONDS,
) -> StartupSweepResult:
    """
    Startup sweep — crash recovery for orphaned UoWs (#307).

    Runs on every heartbeat invocation (step 1 of 3, before Observation Loop
    and Steward main loop). Scans three populations:

    1. `active` UoWs: Executors that may have crashed mid-execution.
       Classified by output_ref state and surfaced to ready-for-steward.

    2. `ready-for-executor` UoWs older than orphan_threshold_seconds:
       Executors that crashed before step 1 of the claim sequence.
       Classified as executor_orphan. Steward treats as clean first execution.

    3. `diagnosing` UoWs: Steward crashed mid-diagnosis.
       Reset to ready-for-steward for re-diagnosis on the next heartbeat.

    Each transition uses the optimistic-lock + audit pattern (Principle 1):
    - Audit entry written in same transaction as status UPDATE.
    - If rows_affected == 0: another process won the race — skip silently.
    - In dry_run mode: classify but do not write or transition.

    Periodic 15-minute sweep (design doc pattern) is deferred to Phase 3.
    The 3-minute heartbeat cadence achieves finer coverage.

    Returns StartupSweepResult with counts for each population swept.
    """
    try:
        active_uows = registry.list(status=_STATUS_ACTIVE)
    except Exception as e:
        log.warning("Startup sweep: failed to query active UoWs — %s", e)
        active_uows = []

    try:
        rfe_uows = registry.list(status=_STATUS_READY_FOR_EXECUTOR)
    except Exception as e:
        log.warning("Startup sweep: failed to query ready-for-executor UoWs — %s", e)
        rfe_uows = []

    try:
        diagnosing_uows = registry.list(status=_STATUS_DIAGNOSING)
    except Exception as e:
        log.warning("Startup sweep: failed to query diagnosing UoWs — %s", e)
        diagnosing_uows = []

    now = datetime.now(timezone.utc)
    active_swept = 0
    executor_orphans_swept = 0
    diagnosing_swept = 0
    skipped_dry_run = 0

    # --- Population 1: active UoWs (Executor crash during execution) ---
    for uow in active_uows:
        uow_id = uow.id
        output_ref = uow.output_ref
        classification, extra = _classify_active_uow(output_ref)

        if dry_run:
            log.info(
                "Startup sweep (DRY RUN): active UoW %s would be classified as %s "
                "and transitioned to ready-for-steward",
                uow_id, classification,
            )
            skipped_dry_run += 1
            continue

        rows = registry.record_startup_sweep_active(
            uow_id=uow_id,
            classification=classification,
            output_ref=output_ref,
            extra=extra if extra else None,
        )

        if rows == 1:
            active_swept += 1
            log.info(
                "Startup sweep: active UoW %s → ready-for-steward (classification=%s)",
                uow_id, classification,
            )
        else:
            log.debug(
                "Startup sweep: race on active UoW %s — another component already "
                "advanced it (rows_affected=0)",
                uow_id,
            )

    # --- Population 2: ready-for-executor UoWs older than threshold ---
    for uow in rfe_uows:
        uow_id = uow.id
        proposed_at = uow.created_at  # proposed_at proxy: conservative lower bound

        try:
            proposed_dt = datetime.fromisoformat(
                proposed_at.replace("Z", "+00:00")
            )
            if proposed_dt.tzinfo is None:
                proposed_dt = proposed_dt.replace(tzinfo=timezone.utc)
            age_seconds = (now - proposed_dt).total_seconds()
        except (ValueError, TypeError, AttributeError):
            log.warning(
                "Startup sweep: UoW %s has unparseable created_at=%r — skipping",
                uow_id, proposed_at,
            )
            continue

        if age_seconds <= orphan_threshold_seconds:
            # Not old enough — leave it alone.
            continue

        if dry_run:
            log.info(
                "Startup sweep (DRY RUN): ready-for-executor UoW %s (age=%.0fs) "
                "would be classified as executor_orphan and transitioned to ready-for-steward",
                uow_id, age_seconds,
            )
            skipped_dry_run += 1
            continue

        rows = registry.record_startup_sweep_executor_orphan(
            uow_id=uow_id,
            proposed_at=proposed_at,
            age_seconds=age_seconds,
            threshold_seconds=orphan_threshold_seconds,
        )

        if rows == 1:
            executor_orphans_swept += 1
            log.info(
                "Startup sweep: executor_orphan UoW %s → ready-for-steward "
                "(age=%.0fs, threshold=%ds)",
                uow_id, age_seconds, orphan_threshold_seconds,
            )
        else:
            log.debug(
                "Startup sweep: race on ready-for-executor UoW %s — another component "
                "already advanced it (rows_affected=0)",
                uow_id,
            )

    # --- Population 3: diagnosing UoWs (Steward crash mid-diagnosis) ---
    for uow in diagnosing_uows:
        uow_id = uow.id

        if dry_run:
            log.info(
                "Startup sweep (DRY RUN): diagnosing UoW %s would be reset to "
                "ready-for-steward",
                uow_id,
            )
            skipped_dry_run += 1
            continue

        rows = registry.record_startup_sweep_diagnosing(uow_id=uow_id)

        if rows == 1:
            diagnosing_swept += 1
            log.info(
                "Startup sweep: diagnosing UoW %s → ready-for-steward "
                "(Steward crash recovery)",
                uow_id,
            )
        else:
            log.debug(
                "Startup sweep: race on diagnosing UoW %s — another component "
                "already advanced it (rows_affected=0)",
                uow_id,
            )

    total = active_swept + executor_orphans_swept + diagnosing_swept + skipped_dry_run
    if total > 0:
        log.info(
            "Startup sweep complete: %d active swept, %d executor_orphans swept, "
            "%d diagnosing swept, %d skipped (dry-run)",
            active_swept, executor_orphans_swept, diagnosing_swept, skipped_dry_run,
        )

    return StartupSweepResult(
        active_swept=active_swept,
        executor_orphans_swept=executor_orphans_swept,
        diagnosing_swept=diagnosing_swept,
        skipped_dry_run=skipped_dry_run,
    )


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
            "Startup sweep complete: active_swept=%d executor_orphans=%d "
            "diagnosing=%d skipped_dry_run=%d",
            sweep_result.active_swept,
            sweep_result.executor_orphans_swept,
            sweep_result.diagnosing_swept,
            sweep_result.skipped_dry_run,
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
