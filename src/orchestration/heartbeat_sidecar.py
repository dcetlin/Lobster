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

Liveness gate (issue #1253):
    The sidecar checks the Claude Code agent output file mtime before writing a
    heartbeat. For each UoW, the session record is looked up in agent_sessions.db
    by task_id='wos-{uow_id}'. If the output file exists but has not been updated
    within AGENT_OUTPUT_STALE_SECONDS, the agent is dead — the sidecar skips the
    heartbeat write, allowing heartbeat_at to grow stale so the startup_sweep and
    observation loop can detect the orphan and route it to ready-for-steward.

    Conservative default: only skip when the file *exists* but is stale. If the
    output_file is absent, empty, or the session record is not found, the sidecar
    continues writing heartbeats (fail-open — avoids blocking live agents that
    registered before the output_file column was added).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("heartbeat_sidecar")

# Number of heartbeat writes per UoW per cron tick.
# Named constant so tests can assert behavior without magic literals.
SIDECAR_WRITES_PER_CYCLE: int = 1

# Seconds of output-file inactivity before the sidecar treats an agent as dead.
# 10 minutes (600s) is 2× the executor-heartbeat cron interval (3 min) and
# well inside the reconciler's default dead threshold (90 min). Chosen to be
# long enough to ignore slow tool calls while short enough to stop masking
# kills within the same cron cycle as the reconciler's agent_failed notice.
# Override via LOBSTER_SIDECAR_STALE_SECONDS environment variable.
_DEFAULT_AGENT_OUTPUT_STALE_SECONDS: int = 600

def _agent_output_stale_seconds() -> int:
    """Return the configured stale threshold, defaulting to 600s."""
    raw = os.environ.get("LOBSTER_SIDECAR_STALE_SECONDS", "")
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_AGENT_OUTPUT_STALE_SECONDS
    except (ValueError, TypeError):
        return _DEFAULT_AGENT_OUTPUT_STALE_SECONDS

# Default path for agent_sessions.db — mirrors session_store._DEFAULT_DB_PATH.
# Resolved at call time so the env var override works in tests.
def _default_sessions_db_path() -> Path:
    messages_dir = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_dir / "config" / "agent_sessions.db"


@dataclass(frozen=True, slots=True)
class HeartbeatSidecarResult:
    """Pure value returned by write_heartbeats_for_active_uows."""
    checked: int            # UoWs in active/executing that were candidates
    written: int            # Successful heartbeat writes (rowcount == 1)
    skipped: int            # write_heartbeat returned 0 (race — already transitioned)
    errors: int             # Exceptions caught (write proceeded for remaining UoWs)
    dead_agent_skipped: int = 0  # UoWs skipped because agent output file is stale (issue #1253)


def write_heartbeats_for_active_uows(
    registry: object,
    sessions_db_path: "Path | None" = None,
) -> HeartbeatSidecarResult:
    """
    Write heartbeats for all UoWs in 'active' or 'executing' status.

    Iterates the in-flight UoW list from the registry and calls
    registry.write_heartbeat(uow_id) for each. Returns a HeartbeatSidecarResult
    summarising the outcome.

    This is a pure side-effect function: it calls write_heartbeat() on the
    registry for each in-flight UoW. No mutation of local state occurs; all
    effects are isolated to registry.write_heartbeat().

    Liveness gate (issue #1253): before writing a heartbeat, the sidecar checks
    whether the UoW's Claude Code agent output file has been updated recently. If
    the file exists but is older than LOBSTER_SIDECAR_STALE_SECONDS (default 600s),
    the agent is considered dead and the heartbeat write is skipped — leaving
    heartbeat_at stale so the startup_sweep and observation loop can detect the
    orphan. If the output_file is absent or unknown, the write proceeds (fail-open).

    Errors on individual UoWs are caught and logged — the function continues
    for remaining UoWs. The errors count in the result lets callers decide
    whether to alert.

    Args:
        registry: A Registry instance with write_heartbeat(uow_id) and
            list(status=...) public methods. Typed as `object` so this module
            does not import Registry directly — avoids circular imports when
            loaded early in executor-heartbeat.py before the registry path is
            set up.
        sessions_db_path: Path to agent_sessions.db. Defaults to the standard
            location derived from LOBSTER_MESSAGES env var. Pass an explicit
            path in tests to redirect to a tmpdir.

    Returns:
        HeartbeatSidecarResult with checked, written, skipped, errors, and
        dead_agent_skipped counts.
    """
    stale_seconds = _agent_output_stale_seconds()
    resolved_sessions_db = sessions_db_path or _default_sessions_db_path()
    candidates = _collect_in_flight_uows(registry)
    written = 0
    skipped = 0
    errors = 0
    dead_agent_skipped = 0

    now = datetime.now(timezone.utc)

    for uow in candidates:
        uow_id = uow.id

        # Liveness gate (issue #1253): skip heartbeat if the agent output file
        # exists but is stale — the agent is dead and should not be masked.
        if _is_agent_output_stale(uow_id, now, stale_seconds, resolved_sessions_db):
            dead_agent_skipped += 1
            log.info(
                "Heartbeat sidecar: skipping UoW %s — agent output file stale "
                "(no update in >%ds); heartbeat_at will grow stale for orphan detection",
                uow_id, stale_seconds,
            )
            continue

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
        dead_agent_skipped=dead_agent_skipped,
    )

    if result.checked > 0:
        log.info(
            "Heartbeat sidecar: checked=%d written=%d skipped=%d "
            "dead_agent_skipped=%d errors=%d",
            result.checked, result.written, result.skipped,
            result.dead_agent_skipped, result.errors,
        )
    else:
        log.debug("Heartbeat sidecar: no in-flight UoWs to write heartbeats for")

    return result


def _get_session_output_file(uow_id: str, sessions_db_path: "Path") -> str | None:
    """
    Return the output_file path for the WOS UoW agent session, or None.

    Queries agent_sessions.db for the session with task_id='wos-{uow_id}'.
    Returns the output_file column value, or None when:
    - No session row is found (agent not yet registered, or pre-output_file schema).
    - The output_file column is absent (pre-migration schema).
    - Any DB access error.

    Pure read: no side effects.
    """
    task_id = f"wos-{uow_id}"
    db_path_str = str(sessions_db_path)
    try:
        conn = sqlite3.connect(db_path_str, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT output_file FROM agent_sessions
                WHERE task_id = ?
                ORDER BY spawned_at DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return row["output_file"] or None
        finally:
            conn.close()
    except Exception as exc:
        log.debug(
            "Heartbeat sidecar: could not look up output_file for UoW %s — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
        return None


def _is_agent_output_stale(
    uow_id: str,
    now: datetime,
    stale_threshold_seconds: int,
    sessions_db_path: "Path",
) -> bool:
    """
    Return True if the UoW's agent output file exists but has not been updated
    within stale_threshold_seconds.

    This is the liveness gate for issue #1253: a dead agent stops writing to its
    output file immediately on kill, so mtime staleness is a reliable signal.

    Conservative (fail-open) by design:
    - Returns False (do NOT skip) when output_file is absent, empty, or unknown.
    - Returns False when the sessions DB cannot be read.
    - Returns False when the file does not exist on disk (agent may not have started
      yet, or output path not yet created — do not block).
    - Returns True only when the file EXISTS on disk AND mtime < now - threshold.

    Args:
        uow_id: The UoW ID (e.g., 'uow_20260519_3ab0bd').
        now: Current UTC datetime.
        stale_threshold_seconds: Seconds of inactivity before treating as dead.
        sessions_db_path: Path to agent_sessions.db.

    Returns:
        True if the agent output file is stale (agent is dead).
        False if unknown, file missing, or file is recent enough.
    """
    output_file = _get_session_output_file(uow_id, sessions_db_path)
    if not output_file:
        # No output_file registered — cannot determine liveness; fail-open.
        return False

    try:
        real_path = Path(output_file).resolve()
        if not real_path.exists():
            # File not yet created (agent may be starting) — fail-open.
            return False
        mtime = real_path.stat().st_mtime
        age_seconds = now.timestamp() - mtime
        if age_seconds > stale_threshold_seconds:
            log.debug(
                "Heartbeat sidecar: UoW %s output file stale "
                "(path=%s, age=%.0fs, threshold=%ds)",
                uow_id, output_file, age_seconds, stale_threshold_seconds,
            )
            return True
        return False
    except OSError as exc:
        log.debug(
            "Heartbeat sidecar: could not stat output file for UoW %s — %s: %s "
            "(fail-open: treating as live)",
            uow_id, type(exc).__name__, exc,
        )
        return False


def _collect_in_flight_uows(registry: object) -> list:
    """
    Return UoWs in 'active' or 'executing' status whose claim has not yet expired.

    Pure read: no side effects. Uses registry.list(status=...) for each status
    and merges the results. Filters out UoWs whose claimed_until is in the past —
    those have a dead agent by definition (the claim window closed without the
    agent completing or renewing), so refreshing their heartbeat_at would mask
    stall detection in the steward's observation loop.

    Note: the agent output-file liveness gate (issue #1253) is applied after this
    function returns, in write_heartbeats_for_active_uows. UoWs with a live
    claimed_until but a stale output file are filtered there, not here.

    Returns a flat list of UoW objects eligible for heartbeat refresh.
    """
    now = datetime.now(timezone.utc)
    active = _safe_list(registry, "active")
    executing = _safe_list(registry, "executing")
    candidates = []
    for uow in active + executing:
        if _claim_expired(uow, now):
            log.debug(
                "Heartbeat sidecar: skipping UoW %s — claimed_until %s is in the past",
                uow.id, getattr(uow, "claimed_until", None),
            )
            continue
        candidates.append(uow)
    return candidates


def _claim_expired(uow: object, now: datetime) -> bool:
    """
    Return True if the UoW's claimed_until deadline has already passed.

    A UoW with no claimed_until (NULL) is not considered expired — it either
    has not been claimed yet or was claimed before the visibility-timeout schema
    migration. Such UoWs are included in the heartbeat candidates.

    Pure function: reads only from the uow object and the provided now timestamp.
    No database access, no side effects.
    """
    claimed_until = getattr(uow, "claimed_until", None)
    if not claimed_until:
        return False
    try:
        deadline = datetime.fromisoformat(claimed_until.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline < now
    except (ValueError, AttributeError):
        return False


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
