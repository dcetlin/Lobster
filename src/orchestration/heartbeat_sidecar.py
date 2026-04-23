"""
Heartbeat sidecar — structural enforcement of heartbeat writes for WOS UoWs.

Guarantees that heartbeat_at is updated for all in-flight UoWs regardless of
whether the executing subagent calls write_heartbeat() itself. This is the
structural enforcement path described in issue #849.

Design: polling approach (Option B from the issue spec). Called at the top of
every executor-heartbeat.py invocation, before the dispatch cycle. Since
executor-heartbeat.py is driven by cron every 3 minutes (offset 90s from the
steward heartbeat), heartbeats are written every ~3 minutes for all active and
executing UoWs — well within the default heartbeat_ttl of 300s.

This eliminates agent-side compliance as a reliability requirement: the
observation loop (steward-heartbeat.py Phase 2b) can now detect true stalls
rather than false stalls caused by agents that forget to call write_heartbeat().

Canonical named constant:
    SIDECAR_WRITES_PER_CYCLE = 1  — one heartbeat write per UoW per cron tick

Interaction with the observation loop:
    - steward-heartbeat.py Phase 2b calls registry.get_stale_heartbeat_uows()
      which returns UoWs where (now - heartbeat_at) > heartbeat_ttl + buffer.
    - executor-heartbeat.py calls write_heartbeats_for_active_uows() which
      calls registry.write_heartbeat() for every active/executing UoW.
    - As long as cron fires executor-heartbeat within heartbeat_ttl (default 300s),
      the observation loop will not see false stalls.

Side-effect audit:
    - write_heartbeat() uses an optimistic lock on status IN ('active', 'executing').
      If the UoW has been recovered or transitioned, write_heartbeat is a no-op.
    - No floods: one write per UoW per cron tick (bounded by number of active UoWs).
    - No unscoped writes: writes are limited to the heartbeat_at column.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("heartbeat_sidecar")

# Number of heartbeat writes per UoW per cron tick.
# Named constant so tests can assert behavior without magic literals.
SIDECAR_WRITES_PER_CYCLE: int = 1


@dataclass(frozen=True, slots=True)
class HeartbeatSidecarResult:
    """Pure value returned by write_heartbeats_for_active_uows."""
    checked: int       # UoWs in active/executing that were candidates
    written: int       # Successful heartbeat writes (rowcount == 1)
    skipped: int       # write_heartbeat returned 0 (race — already transitioned)
    errors: int        # Exceptions caught (write proceeded for remaining UoWs)


def write_heartbeats_for_active_uows(registry: object) -> HeartbeatSidecarResult:
    """
    Write heartbeats for all UoWs in 'active' or 'executing' status.

    Iterates the in-flight UoW list from the registry and calls
    registry.write_heartbeat(uow_id) for each. Returns a HeartbeatSidecarResult
    summarising the outcome.

    This is a pure side-effect function: it calls write_heartbeat() on the
    registry for each in-flight UoW. No mutation of local state occurs; all
    effects are isolated to registry.write_heartbeat().

    Errors on individual UoWs are caught and logged — the function continues
    for remaining UoWs. The errors count in the result lets callers decide
    whether to alert.

    Args:
        registry: A Registry instance with write_heartbeat(uow_id) and
            list(status=...) public methods. Typed as `object` so this module
            does not import Registry directly — avoids circular imports when
            loaded early in executor-heartbeat.py before the registry path is
            set up.

    Returns:
        HeartbeatSidecarResult with checked, written, skipped, errors counts.
    """
    candidates = _collect_in_flight_uows(registry)
    written = 0
    skipped = 0
    errors = 0

    for uow in candidates:
        uow_id = uow.id
        try:
            rowcount = registry.write_heartbeat(uow_id)
            if rowcount == 1:
                written += 1
                log.debug(
                    "Heartbeat sidecar: wrote heartbeat for UoW %s (status=%s)",
                    uow_id, uow.status,
                )
            else:
                # rowcount == 0: UoW was already transitioned (race) — no-op.
                skipped += 1
                log.debug(
                    "Heartbeat sidecar: write_heartbeat no-op for UoW %s "
                    "(already transitioned, rowcount=0)",
                    uow_id,
                )
        except Exception as e:
            errors += 1
            log.warning(
                "Heartbeat sidecar: failed to write heartbeat for UoW %s — %s",
                uow_id, e,
            )

    result = HeartbeatSidecarResult(
        checked=len(candidates),
        written=written,
        skipped=skipped,
        errors=errors,
    )

    if result.checked > 0:
        log.info(
            "Heartbeat sidecar: checked=%d written=%d skipped=%d errors=%d",
            result.checked, result.written, result.skipped, result.errors,
        )
    else:
        log.debug("Heartbeat sidecar: no in-flight UoWs to write heartbeats for")

    return result


def _collect_in_flight_uows(registry: object) -> list:
    """
    Return all UoWs in 'active' or 'executing' status.

    Pure read: no side effects. Uses registry.list(status=...) for each status
    and merges the results. Returns a flat list of UoW objects.
    """
    active = _safe_list(registry, "active")
    executing = _safe_list(registry, "executing")
    return active + executing


def _safe_list(registry: object, status: str) -> list:
    """
    Call registry.list(status=status), returning [] on error.

    Isolates the error boundary so a failure to query one status does not
    prevent the other from being checked.
    """
    try:
        return registry.list(status=status)  # type: ignore[attr-defined]
    except Exception as e:
        log.warning(
            "Heartbeat sidecar: failed to list UoWs with status=%r — %s",
            status, e,
        )
        return []
