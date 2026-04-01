#!/usr/bin/env python3
"""
Startup Sweep — WOS Phase 2 crash recovery for orphaned UoWs.

Scans `active`, `ready-for-executor`, and `diagnosing` UoWs and surfaces
orphans back to the Steward via 'ready-for-steward' transitions with
'startup_sweep' audit entries.

Runs as Phase 1 of the steward-heartbeat.py cron script (every 3 minutes).
Can also be invoked standalone for testing:

    uv run ~/lobster/scheduled-tasks/startup-sweep.py [--dry-run]

Full implementation spec: #307.

Phase 2 dependency: requires schema migration to have been applied:
    uv run ~/lobster/scripts/migrate_add_steward_fields.py
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("startup-sweep")


# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_STATUS_ACTIVE = "active"
_STATUS_READY_FOR_EXECUTOR = "ready-for-executor"
_STATUS_DIAGNOSING = "diagnosing"
_STARTUP_SWEEP_ACTOR = "steward_startup"
_EXECUTOR_ORPHAN_THRESHOLD_SECONDS = 3600  # 1 hour: ready-for-executor age threshold

# Minimum age (seconds) for an `active` UoW before the startup sweep will
# classify it as a crash candidate.  The executor writes `output_ref` to the
# DB during the 6-step atomic claim BEFORE launching the `claude -p`
# subprocess, so the file is absent on disk while the subprocess is running.
# Without this guard the 3-minute heartbeat races the subprocess and
# misclassifies a live execution as `crashed_output_ref_missing`.
#
# 300 s (5 minutes) is a generous buffer above the 3-minute heartbeat cadence.
# True crashed UoWs are caught by TTL recovery (TTL_EXCEEDED_HOURS=4h in
# executor.py) if they slip past this window.
_ACTIVE_SWEEP_THRESHOLD_SECONDS = 300


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
    active_sweep_threshold_seconds: int = _ACTIVE_SWEEP_THRESHOLD_SECONDS,
    bootup_candidate_gate: bool | None = None,
    github_client: Callable[[int], dict[str, Any]] | None = None,
) -> StartupSweepResult:
    """
    Startup sweep — crash recovery for orphaned UoWs (#307).

    Runs on every heartbeat invocation (step 1 of 3, before Observation Loop
    and Steward main loop). Scans three populations:

    1. `active` UoWs: Executors that may have crashed mid-execution.
       Classified by output_ref state and surfaced to ready-for-steward.
       Only UoWs that have been `active` for longer than
       `active_sweep_threshold_seconds` (default: 300 s) are swept.
       Younger UoWs are skipped — the executor subprocess may still be
       running and has not yet written the output_ref file.

    2. `ready-for-executor` UoWs older than orphan_threshold_seconds:
       Executors that crashed before step 1 of the claim sequence.
       Classified as executor_orphan. Steward treats as clean first execution.

    3. `diagnosing` UoWs: Steward crashed mid-diagnosis.
       Reset to ready-for-steward for re-diagnosis on the next heartbeat.

    Each transition uses the optimistic-lock + audit pattern (Principle 1):
    - Audit entry written in same transaction as status UPDATE.
    - If rows_affected == 0: another process won the race — skip silently.
    - In dry_run mode: classify but do not write or transition.

    BOOTUP_CANDIDATE_GATE (#342): when True, UoWs whose GitHub issue carries
    the `bootup-candidate` label are skipped in all three populations,
    consistent with the gate applied in run_steward_cycle.

    active_sweep_threshold_seconds: minimum age (measured from updated_at,
        which is set atomically when status changes to `active`) before an
        `active` UoW is swept as a crash candidate. Default: 300 s. Pass 0
        to disable the guard (original behavior, not recommended in production).
    orphan_threshold_seconds: minimum age before a `ready-for-executor` UoW
        is classified as executor_orphan. Default: 3600 s.
    bootup_candidate_gate: override for BOOTUP_CANDIDATE_GATE module constant.
    github_client: callable(issue_number) → {labels, ...}. Defaults to the
        production gh CLI client from steward.py.

    Periodic 15-minute sweep (design doc pattern) is deferred to Phase 3.
    The 3-minute heartbeat cadence achieves finer coverage.

    Returns StartupSweepResult with counts for each population swept.
    """
    from src.orchestration.steward import BOOTUP_CANDIDATE_GATE, _default_github_client

    _gate = bootup_candidate_gate if bootup_candidate_gate is not None else BOOTUP_CANDIDATE_GATE
    _github = github_client or _default_github_client

    def _is_bootup_candidate_gated(uow) -> bool:
        """Return True if this UoW should be skipped due to BOOTUP_CANDIDATE_GATE."""
        if not _gate:
            return False
        source_issue_number = getattr(uow, "source_issue_number", None)
        if not source_issue_number:
            return False
        issue_info = _github(source_issue_number)
        labels = issue_info.get("labels", [])
        return "bootup-candidate" in labels

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

        if _is_bootup_candidate_gated(uow):
            log.debug(
                "Startup sweep: skipping bootup-candidate UoW %s (gate=True)",
                uow_id,
            )
            continue

        # Minimum-age guard: skip UoWs that transitioned to `active` too
        # recently.  The executor sets output_ref in the DB and commits the
        # status→active transition BEFORE launching the subprocess, so the
        # output_ref file is legitimately absent while the subprocess runs.
        # Without this guard the 3-minute heartbeat races the subprocess and
        # misclassifies a live execution as `crashed_output_ref_missing`.
        #
        # updated_at is set atomically when the status changes to `active`
        # (started_at is not exposed on the UoW dataclass) and is a reliable
        # proxy for "when did this UoW enter active state".
        if active_sweep_threshold_seconds > 0:
            age_anchor = getattr(uow, "updated_at", None)
            if age_anchor is not None:
                try:
                    anchor_dt = datetime.fromisoformat(
                        age_anchor.replace("Z", "+00:00")
                    )
                    if anchor_dt.tzinfo is None:
                        anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
                    active_age_seconds = (now - anchor_dt).total_seconds()
                    if active_age_seconds < active_sweep_threshold_seconds:
                        log.debug(
                            "Startup sweep: skipping active UoW %s — too young "
                            "(age=%.0fs < threshold=%ds); executor subprocess "
                            "may still be running",
                            uow_id, active_age_seconds, active_sweep_threshold_seconds,
                        )
                        continue
                except (ValueError, TypeError):
                    # Unparseable timestamp — fall through and sweep normally.
                    pass

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

        if _is_bootup_candidate_gated(uow):
            log.debug(
                "Startup sweep: skipping bootup-candidate UoW %s (gate=True)",
                uow_id,
            )
            continue

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

        if _is_bootup_candidate_gated(uow):
            log.debug(
                "Startup sweep: skipping bootup-candidate UoW %s (gate=True)",
                uow_id,
            )
            continue

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
