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
from typing import Protocol, Sequence

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.paths import REGISTRY_DB
from src.orchestration.steward import is_bootup_candidate_gate_active, run_steward_cycle
from src.orchestration.github_sync import run_post_completion_sync

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
HIGH_PRESCRIPTION_THRESHOLD = 10  # Alert when prescriptions per cycle exceeds this (#618)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _observations_log_path() -> Path:
    """Return the observations.log path from env or default workspace."""
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "logs" / "observations.log"


def _append_observation(message: str) -> None:
    """Append a plain-text warning line to observations.log (side effect isolated here)."""
    obs_log = _observations_log_path()
    obs_log.parent.mkdir(parents=True, exist_ok=True)
    with obs_log.open("a") as fh:
        fh.write(message + "\n")


def _task_outputs_dir() -> Path:
    """Return the task-outputs directory path."""
    messages_base = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_base / "task-outputs"


def _write_task_output(output: str, status: str, timestamp: str) -> None:
    """
    Write a task output record directly to the task-outputs directory.
    Mirrors the format expected by check_task_outputs.
    """
    task_outputs = _task_outputs_dir()
    task_outputs.mkdir(parents=True, exist_ok=True)
    date_prefix = timestamp[:19].replace(":", "").replace("-", "").replace("T", "-")
    filename = f"{date_prefix}-steward-heartbeat.json"
    record = {
        "job_name": "steward-heartbeat",
        "timestamp": timestamp,
        "status": status,
        "output": output,
    }
    out_path = task_outputs / filename
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


# ---------------------------------------------------------------------------
# Stall reason constants — canonical strings recognized by the Steward's
# re-entry classification table (#303).
# ---------------------------------------------------------------------------

class _StallReason(StrEnum):
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    STARTED_AT_NULL = "started_at_null"


# ---------------------------------------------------------------------------
# Named constants — heartbeat staleness detection
# ---------------------------------------------------------------------------

# Grace period added to heartbeat_ttl before declaring a stall.
# Absorbs minor scheduling jitter between the agent's heartbeat write and
# the observation loop's check interval. Must be < steward heartbeat period (3 min).
HEARTBEAT_STALL_BUFFER_SECONDS: int = 30


# ---------------------------------------------------------------------------
# Named result type for the observation loop
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ObservationResult:
    """Pure result value returned by run_observation_loop."""
    checked: int
    stalled: int
    skipped_dry_run: int


@dataclass(frozen=True, slots=True)
class HeartbeatStallResult:
    """Pure result value returned by recover_stale_heartbeat_uows."""
    checked: int
    recovered: int
    skipped_dry_run: int


# ---------------------------------------------------------------------------
# Clock protocol — injectable for tests
# ---------------------------------------------------------------------------

class _Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _warn_if_legacy_registry_exists() -> None:
    """Log a warning if the deprecated legacy registry path exists and is non-empty."""
    workspace = Path(os.environ.get(
        "LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"
    ))
    legacy_path = workspace / "data" / "wos-registry.db"
    if legacy_path.exists() and legacy_path.stat().st_size > 0:
        log.warning(
            "Legacy registry DB at %s exists and is non-empty (%d bytes). "
            "Canonical path is %s. Investigate and remove the legacy file.",
            legacy_path,
            legacy_path.stat().st_size,
            workspace / "orchestration" / "registry.db",
        )


# ---------------------------------------------------------------------------
# Agent cleanup — prevent backlog accumulation (Phase 1)
# ---------------------------------------------------------------------------

STALE_AGENT_THRESHOLD_SECONDS: int = 7200  # 2 hours


def run_stale_agent_cleanup(dry_run: bool = False) -> dict:
    """
    Unregister agents older than STALE_AGENT_THRESHOLD_SECONDS with no recent
    output activity.

    Scans the agent_sessions table for running agents whose spawned_at timestamp
    is older than the threshold and whose output files (if specified) haven't been
    updated recently. These agents are presumed dead or stuck and are unregistered
    to prevent indefinite backlog accumulation.

    In dry_run mode: queries agents but does NOT unregister any.

    Returns a dict with keys: evaluated, cleaned, skipped, running_total.
    """
    from src.agents.session_store import _get_connection, _DEFAULT_DB_PATH

    try:
        conn = _get_connection(_DEFAULT_DB_PATH)
        now_iso = _now_iso()
        now_timestamp = datetime.now(timezone.utc).timestamp()

        # Get total running agent count for metrics
        total_running = conn.execute(
            "SELECT COUNT(*) as count FROM agent_sessions WHERE status = 'running'"
        ).fetchone()
        running_total = total_running["count"] if total_running else 0

        # Query all running agents with spawned_at older than threshold
        cutoff_seconds = now_timestamp - STALE_AGENT_THRESHOLD_SECONDS
        cutoff_iso = datetime.fromtimestamp(cutoff_seconds, timezone.utc).isoformat()

        rows = conn.execute(
            """
            SELECT id, spawned_at, output_file
            FROM agent_sessions
            WHERE status = 'running'
              AND spawned_at < ?
            ORDER BY spawned_at ASC
            """,
            (cutoff_iso,),
        ).fetchall()

        evaluated = len(rows)
        cleaned = 0
        skipped_dry_run = 0

        if dry_run:
            if evaluated > 0:
                log.info(
                    "Stale agent cleanup (DRY RUN): %d running agents older than %.0f hours "
                    "would be unregistered (total running=%d)",
                    evaluated,
                    STALE_AGENT_THRESHOLD_SECONDS / 3600.0,
                    running_total,
                )
            else:
                log.debug("Stale agent cleanup (DRY RUN): no stale agents found (total running=%d)", running_total)
            return {"evaluated": evaluated, "cleaned": 0, "skipped": evaluated, "running_total": running_total}

        # Process each stale agent
        from src.agents.session_store import session_end

        for row in rows:
            agent_id = row["id"]
            spawned_at_str = row["spawned_at"]
            output_file = row["output_file"]

            # Check if output_file exists and has recent activity
            is_stale = True
            if output_file:
                try:
                    mtime = Path(output_file).stat().st_mtime
                    mtime_age_seconds = now_timestamp - mtime
                    if mtime_age_seconds < STALE_AGENT_THRESHOLD_SECONDS:
                        # File was updated recently — agent may still be working
                        is_stale = False
                except (OSError, FileNotFoundError):
                    # File doesn't exist or can't be stat'd — treat as stale
                    is_stale = True

            if not is_stale:
                log.debug(
                    "Stale agent cleanup: skipping agent %s "
                    "(output file recently updated)",
                    agent_id,
                )
                skipped_dry_run += 1
                continue

            # Unregister the stale agent
            try:
                session_end(
                    id_or_task_id=agent_id,
                    status="dead",
                    result_summary="Agent unregistered after inactivity threshold exceeded",
                )
                cleaned += 1
                log.info(
                    "Stale agent cleanup: unregistered agent %s "
                    "(spawned_at=%s, no recent output activity)",
                    agent_id,
                    spawned_at_str,
                )
            except Exception as e:
                log.warning(
                    "Stale agent cleanup: failed to unregister agent %s — %s",
                    agent_id, e,
                )

        return {
            "evaluated": evaluated,
            "cleaned": cleaned,
            "skipped": skipped_dry_run,
            "running_total": running_total,
        }

    except Exception as e:
        log.warning("Stale agent cleanup: query failed — %s", e)
        return {"evaluated": 0, "cleaned": 0, "skipped": 0, "running_total": 0}


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
# Heartbeat stall recovery (migration 0009) — separate from timeout_at loop
# ---------------------------------------------------------------------------

def recover_stale_heartbeat_uows(
    registry,
    dry_run: bool = False,
    buffer_seconds: int = HEARTBEAT_STALL_BUFFER_SECONDS,
) -> HeartbeatStallResult:
    """
    Scan for UoWs whose heartbeat has gone stale and re-queue them.

    Called in the observation phase (Phase 2) alongside run_observation_loop.
    This handles UoWs that have written at least one heartbeat — the signal-driven
    path. UoWs with heartbeat_at=NULL continue to use the timeout_at-based path
    in run_observation_loop.

    Stall condition:
    - status IN ('active', 'executing')
    - heartbeat_at IS NOT NULL
    - (now - heartbeat_at) > heartbeat_ttl + buffer_seconds

    Recovery action: transition to 'ready-for-steward' via record_heartbeat_stall,
    which writes a stall_detected audit entry with stall_type='heartbeat_stall'.
    The Steward's re-entry classifier distinguishes this from 'stall_detected'
    (timeout-based) via the audit entry's stall_type field.

    In dry_run mode: detects stale UoWs but does NOT write audit entries or
    transition status. Returns recovered count in skipped_dry_run.

    Returns HeartbeatStallResult(checked, recovered, skipped_dry_run).
    """
    try:
        stale_uows = registry.get_stale_heartbeat_uows(buffer_seconds=buffer_seconds)
    except Exception as e:
        log.warning("Heartbeat stall recovery: failed to query stale UoWs — %s", e)
        return HeartbeatStallResult(checked=0, recovered=0, skipped_dry_run=0)

    recovered = 0
    skipped_dry_run = 0

    for uow in stale_uows:
        uow_id = uow.id
        heartbeat_at = uow.heartbeat_at
        heartbeat_ttl = uow.heartbeat_ttl

        # Compute silence duration for logging.
        silence_seconds: float = 0.0
        try:
            if heartbeat_at:
                silence_seconds = (_utc_now() - _parse_iso(heartbeat_at)).total_seconds()
        except (ValueError, TypeError):
            pass

        if dry_run:
            log.info(
                "Heartbeat stall (DRY RUN): UoW %s would be re-queued "
                "(heartbeat_at=%s, heartbeat_ttl=%ds, silence=%.0fs)",
                uow_id, heartbeat_at, heartbeat_ttl, silence_seconds,
            )
            skipped_dry_run += 1
            continue

        try:
            rows = registry.record_heartbeat_stall(
                uow_id=uow_id,
                heartbeat_at=heartbeat_at,
                heartbeat_ttl=heartbeat_ttl,
                silence_seconds=silence_seconds,
            )
        except Exception as e:
            log.warning(
                "Heartbeat stall: failed to record stall for UoW %s — %s",
                uow_id, e,
            )
            continue

        if rows == 1:
            recovered += 1
            log.info(
                "Heartbeat stall: UoW %s re-queued to ready-for-steward "
                "(stall_type=heartbeat_stall, silence=%.0fs, ttl=%ds)",
                uow_id, silence_seconds, heartbeat_ttl,
            )
        else:
            log.debug(
                "Heartbeat stall: race on UoW %s — already advanced by another "
                "component (rows_affected=0)",
                uow_id,
            )

    return HeartbeatStallResult(
        checked=len(stale_uows),
        recovered=recovered,
        skipped_dry_run=skipped_dry_run,
    )


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

    _warn_if_legacy_registry_exists()

    gate_active = is_bootup_candidate_gate_active()
    log.info("BOOTUP_CANDIDATE_GATE = %s", gate_active)

    from src.orchestration.registry import Registry

    db_path = REGISTRY_DB
    if not db_path.exists():
        log.error("Registry DB not found at %s — run install/migrate first", db_path)
        return 1

    registry = Registry(db_path)

    # Phase 0: Stale agent cleanup
    log.info("--- Phase 0: Stale agent cleanup ---")
    cleanup_result = {"evaluated": 0, "cleaned": 0, "skipped": 0, "running_total": 0}
    try:
        cleanup_result = run_stale_agent_cleanup(dry_run=dry_run)
        log.info(
            "Stale agent cleanup complete: evaluated=%d cleaned=%d skipped=%d (running_agents=%d)",
            cleanup_result["evaluated"],
            cleanup_result["cleaned"],
            cleanup_result["skipped"],
            cleanup_result["running_total"],
        )
    except Exception:
        log.exception("Stale agent cleanup failed — continuing to startup sweep")

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

    # Phase 2b: Heartbeat stall recovery — signal-driven path (migration 0009).
    # Checks UoWs with non-NULL heartbeat_at for silence exceeding heartbeat_ttl.
    # Complements the timeout_at-based path above: that path handles UoWs without
    # heartbeat_at; this path handles UoWs that have written at least one heartbeat.
    log.info("--- Phase 2b: Heartbeat stall recovery ---")
    try:
        hb_result = recover_stale_heartbeat_uows(registry, dry_run=dry_run)
        log.info(
            "Heartbeat stall recovery complete: %d checked, %d recovered, "
            "%d skipped (dry-run)",
            hb_result.checked, hb_result.recovered, hb_result.skipped_dry_run,
        )
    except Exception:
        log.exception("Heartbeat stall recovery failed — continuing to Steward main loop")

    # execution_enabled gate — mirrors the executor-heartbeat pattern.
    # Phases 0–2 (stale agent cleanup, startup sweep, observation loop) always
    # run because they are cheap and ensure state consistency even when WOS is
    # paused. Phase 3 (LLM prescription) and Phase 4 (GitHub sync) are skipped
    # when execution_enabled=false to prevent LLM cost drain.
    from src.orchestration.dispatcher_handlers import is_execution_enabled

    # Alert condition 2: queue depth when execution is disabled (#618).
    # Check before skipping Phase 3 so the alert fires even when WOS is paused.
    execution_enabled = is_execution_enabled()
    if not execution_enabled:
        try:
            eligible_uows = registry.list(status="ready-for-steward")
            eligible_count = len(eligible_uows)
            if eligible_count > 0:
                msg = (
                    f"steward: {eligible_count} UoWs eligible for prescription "
                    f"but execution_enabled=false — queue will not drain"
                )
                log.warning("%s", msg)
                _append_observation(msg)
        except Exception:
            log.exception("Queue depth check (execution disabled) failed — continuing")

        log.info(
            "Steward heartbeat: skipping LLM prescription "
            "(wos-config.json execution_enabled=false). "
            "Use 'wos start' to enable."
        )
        log.info("Steward heartbeat complete")
        return 0

    # Phase 3: Steward main loop
    log.info("--- Phase 3: Steward main loop ---")
    prescriptions_this_cycle = 0
    try:
        result = run_steward_cycle(
            registry=registry,
            dry_run=dry_run,
            bootup_candidate_gate=gate_active,
        )
        prescriptions_this_cycle = result.prescribed
        log.info(
            "Steward cycle complete: evaluated=%d prescribed=%d done=%d "
            "surfaced=%d skipped=%d race_skipped=%d",
            result.evaluated,
            result.prescribed,
            result.done,
            result.surfaced,
            result.skipped,
            result.race_skipped,
        )
    except RuntimeError as e:
        # Schema migration not applied — hard exit with clear message
        log.error("%s", e)
        return 1
    except Exception:
        log.exception("Steward main loop failed")
        return 1

    # Alert condition 1: high prescription count per cycle (#618).
    if prescriptions_this_cycle > HIGH_PRESCRIPTION_THRESHOLD:
        msg = (
            f"steward: high prescription count ({prescriptions_this_cycle}) "
            f"this cycle — possible queue buildup"
        )
        log.warning("%s", msg)
        _append_observation(msg)

    # Phase 4: Post-completion GitHub sync
    log.info("--- Phase 4: Post-completion GitHub sync ---")
    try:
        sync_result = run_post_completion_sync(registry, dry_run=dry_run)
        log.info(
            "GitHub sync complete: synced=%d skipped_no_url=%d failed=%d",
            sync_result.synced,
            sync_result.skipped_no_url,
            sync_result.failed,
        )
        if sync_result.errors:
            for err in sync_result.errors:
                log.warning("GitHub sync error: %s", err)
    except Exception:
        log.exception("Post-completion GitHub sync failed — continuing")

    # Write task output with cycle metrics (#618).
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    task_output_summary = (
        f"steward cycle complete: prescriptions_this_cycle={prescriptions_this_cycle}"
    )
    try:
        _write_task_output(task_output_summary, "success", timestamp)
    except Exception:
        log.exception("Failed to write task output — continuing")

    log.info("Steward heartbeat complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
