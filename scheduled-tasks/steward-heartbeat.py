#!/usr/bin/env python3
"""
Steward Heartbeat — WOS Phase 2 cron-driven Steward agent.

Runs every 3 minutes. On each invocation executes three functions in order:

1. Startup sweep — crash recovery. Scans `active`, `ready-for-executor`, and
   `diagnosing` UoWs and surfaces orphans back to the Steward via
   'ready-for-steward' transitions with 'startup_sweep' audit entries.
   Implementation lives in startup-sweep.py. Full spec: #307.

2. Observation Loop — detect stalled `active` UoWs by checking `timeout_at`.
   Surfaces stalled UoWs back to the Steward via the 'ready-for-steward'
   transition with a 'stall_detected' audit entry. Full implementation is here.

3. Steward main loop — diagnose and prescribe for all `ready-for-steward` UoWs.
   This is the primary Phase 2 deliverable.

Note: A separate 15-minute periodic sweep process (as mentioned in the design
doc) is deferred to Phase 3. The startup sweep running on every 3-minute
heartbeat invocation achieves finer coverage.

Cron schedule (every 3 minutes):
    */3 * * * * cd ~/lobster && uv run scheduled-tasks/steward-heartbeat.py >> ~/lobster-workspace/scheduled-jobs/logs/steward-heartbeat.log 2>&1

Type B dispatch: cron calls this script directly (no inbox/ message, no dispatcher
involvement). The jobs.json enabled gate is checked at the top of main() so that
runtime enable/disable is respected without touching cron.

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

from src.orchestration.steward import is_bootup_candidate_gate_active, run_steward_cycle

# ---------------------------------------------------------------------------
# Startup sweep — imported from startup-sweep.py (Phase 1 concern)
# Re-exported here so tests that load this module via importlib can still
# access run_startup_sweep, StartupSweepResult, and _classify_active_uow
# by name without change.
# ---------------------------------------------------------------------------

import importlib.util as _ilu

_SWEEP_PATH = Path(__file__).parent / "startup-sweep.py"
_sweep_spec = _ilu.spec_from_file_location("startup_sweep", _SWEEP_PATH)
_sweep_mod = _ilu.module_from_spec(_sweep_spec)
sys.modules["startup_sweep"] = _sweep_mod
_sweep_spec.loader.exec_module(_sweep_mod)

run_startup_sweep = _sweep_mod.run_startup_sweep
StartupSweepResult = _sweep_mod.StartupSweepResult
_classify_active_uow = _sweep_mod._classify_active_uow

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
# Named constants
# ---------------------------------------------------------------------------

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
    # Primary: orchestration/registry.db (used by registry_cli.py and the Executor)
    # Fallback: REGISTRY_DB_PATH env override (used in tests and alternate installs)
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    return workspace / "orchestration" / "registry.db"


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
        # started_at is not on the UoW dataclass — access it via the public
        # registry.get_started_at() method when needed for the fallback path.
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
            # Fallback: fetch started_at via the public Registry API.
            started_at = registry.get_started_at(uow_id)

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

    # jobs.json enabled gate — respect runtime enable/disable toggled via
    # the dispatcher commands or direct jobs.json edits.
    if not _is_job_enabled("steward-heartbeat"):
        log.info("Steward heartbeat: skipped (disabled in jobs.json)")
        return 0

    gate_active = is_bootup_candidate_gate_active()
    log.info("BOOTUP_CANDIDATE_GATE = %s", gate_active)

    from src.orchestration.registry import Registry

    db_path = _default_db_path()
    if not db_path.exists():
        log.error("Registry DB not found at %s — run install/migrate first", db_path)
        return 1

    registry = Registry(db_path)

    # Phase 1: Startup sweep
    log.info("--- Phase 1: Startup sweep ---")
    try:
        sweep_result = run_startup_sweep(registry, dry_run=dry_run, bootup_candidate_gate=gate_active)
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
            bootup_candidate_gate=gate_active,
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
