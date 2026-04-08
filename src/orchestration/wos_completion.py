"""
WOS execution completion helper (issue #669).

Provides maybe_complete_wos_uow — the deferred execution_complete transition
for the async inbox dispatch path.

Background: when the Executor writes a wos_execute message to the inbox (fire-
and-forget), it transitions the UoW to 'executing' rather than 'ready-for-
steward'. The execution_complete audit entry and the final executing →
ready-for-steward transition happen here, only after the subagent confirms
completion by calling write_result.

This module is imported by both inbox_server.py (production) and test code.
It has no dependency on inbox_server's heavy MCP server stack — only on the
orchestration.registry module.

Naming convention: task_id for WOS dispatches is "wos-{uow_id}", set by
route_wos_message in dispatcher_handlers.py.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("wos_completion")

#: Prefix used by route_wos_message to form the task_id for wos_execute dispatches.
WOS_TASK_ID_PREFIX = "wos-"

#: write_result status value that signals successful subagent completion.
WRITE_RESULT_SUCCESS_STATUS = "success"


def maybe_complete_wos_uow(task_id: str, status: str) -> None:
    """
    Transition a WOS UoW from 'executing' to 'ready-for-steward' when its
    subagent calls write_result with status='success'.

    This is the deferred execution_complete transition for the async inbox
    dispatch path (issue #669). The Executor transitions active → executing at
    dispatch time; this function fires the execution_complete audit entry and
    the executing → ready-for-steward transition only after the subagent
    confirms completion via write_result.

    Conditions required to fire:
    - task_id starts with WOS_TASK_ID_PREFIX ("wos-")
    - status == "success" (only successful completions advance the UoW)
    - UoW exists in the registry with status == "executing"

    A UoW not in 'executing' status is skipped silently — this handles the
    case where TTL recovery already failed the UoW, or where a duplicate
    write_result arrives after the first has already completed it.

    Errors are logged but never raised — write_result delivery must not be
    blocked by registry update failures.

    Args:
        task_id: The task_id string from the write_result call.
                 Expected format: "wos-{uow_id}".
        status:  The status from the write_result call ("success" or "error").
    """
    if not task_id.startswith(WOS_TASK_ID_PREFIX):
        return
    if status != WRITE_RESULT_SUCCESS_STATUS:
        # Only advance to ready-for-steward on success. Failed write_results
        # leave the UoW in 'executing' for TTL recovery to handle.
        return

    uow_id = task_id[len(WOS_TASK_ID_PREFIX):]
    try:
        from orchestration.registry import Registry, UoWStatus

        workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
        env_override = os.environ.get("REGISTRY_DB_PATH")
        db_path = Path(env_override) if env_override else workspace / "orchestration" / "registry.db"

        if not db_path.exists():
            log.debug(
                "maybe_complete_wos_uow: registry DB not found at %s — "
                "skipping (no WOS install or test env)",
                db_path,
            )
            return

        registry = Registry(db_path)
        uow = registry.get(uow_id)
        if uow is None:
            log.debug(
                "maybe_complete_wos_uow: UoW %r not found in registry — skipping",
                uow_id,
            )
            return

        if uow.status != UoWStatus.EXECUTING:
            log.debug(
                "maybe_complete_wos_uow: UoW %r is in status %r (expected 'executing') — "
                "skipping (already recovered or duplicate write_result)",
                uow_id,
                uow.status,
            )
            return

        output_ref = uow.output_ref or ""
        registry.complete_uow(uow_id, output_ref)
        log.info(
            "maybe_complete_wos_uow: UoW %r transitioned executing → ready-for-steward "
            "(execution_complete written on write_result confirmation)",
            uow_id,
        )
    except Exception as exc:
        log.warning(
            "maybe_complete_wos_uow: failed to complete UoW %r — %s: %s",
            uow_id,
            type(exc).__name__,
            exc,
        )
