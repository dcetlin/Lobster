"""
Dispatcher command handlers for WOS.

These are pure functions: they take a UoW id (or status string) and a Registry
instance, and return a formatted string response suitable for sending back to
Telegram. No MCP tools, no network calls — those belong in the dispatcher.

The dispatcher calls these handlers when it recognizes:
  /approve <uow-id>        → handle_approve(uow_id, registry)
  /wos status [status]     → handle_wos_status(status, registry)
  /wos unblock             → handle_wos_unblock()
  decide retry <uow-id>    → handle_decide_retry(uow_id, registry)
  decide close <uow-id>    → handle_decide_close(uow_id, registry)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import Registry

from .registry import ApproveConfirmed, ApproveExpired, ApproveNotFound, ApproveSkipped


# ---------------------------------------------------------------------------
# Gate-cleared flag path — mirrors _GATE_CLEARED_FLAG in steward.py
# ---------------------------------------------------------------------------

_GATE_CLEARED_FLAG: Path = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
) / "data" / "wos-gate-cleared"


def handle_approve(uow_id: str, *, registry: "Registry") -> str:
    """
    Handle /approve <uow-id>.

    Returns a human-readable Telegram message describing the outcome.
    Uses match/case on the typed ApproveResult union — no string key checks.
    """
    result = registry.approve(uow_id)

    match result:
        case ApproveConfirmed():
            return (
                f"UoW `{uow_id}` confirmed.\n"
                f"Status: `proposed \u2192 pending`"
            )
        case ApproveNotFound():
            return (
                f"UoW `{uow_id}` not found. "
                "Run `/wos status proposed` to see current proposals."
            )
        case ApproveExpired():
            return (
                f"UoW `{uow_id}` has expired. "
                "Wait for the next sweep to re-propose, or run a manual sweep."
            )
        case ApproveSkipped(current_status=current_status):
            return f"UoW `{uow_id}` is already `{current_status}` — no action taken."


def handle_confirm(uow_id: str, *, registry: "Registry") -> str:
    """
    Handle /confirm <uow-id>.

    Alias for handle_approve — retains the /confirm command name for backwards
    compatibility while delegating to the renamed approve() method.
    """
    return handle_approve(uow_id, registry=registry)


def handle_decide_retry(uow_id: str, *, registry: "Registry") -> str:
    """
    Handle a decide_retry action for a UoW.

    Called when Dan selects "Retry" after the Steward surfaces a stuck UoW,
    or sends a message matching "decide retry <uow-id>".

    Resets steward_cycles to 0 and transitions blocked → ready-for-steward so
    the Steward re-diagnoses the UoW on its next heartbeat cycle.
    """
    rows = registry.decide_retry(uow_id)
    if rows == 1:
        return (
            f"UoW `{uow_id}` reset for retry.\n"
            f"Status: `blocked \u2192 ready-for-steward` (steward_cycles reset to 0)"
        )
    return (
        f"UoW `{uow_id}` could not be retried \u2014 it is not currently in `blocked` status.\n"
        f"Run `/wos status blocked` to see blocked UoWs."
    )


def handle_decide_close(uow_id: str, *, registry: "Registry") -> str:
    """
    Handle a decide_close action for a UoW.

    Called when Dan selects "Close" after the Steward surfaces a stuck UoW,
    or sends a message matching "decide close <uow-id>".

    Transitions blocked → failed with reason=user_closed.
    """
    rows = registry.decide_close(uow_id)
    if rows == 1:
        return (
            f"UoW `{uow_id}` closed.\n"
            f"Status: `blocked \u2192 failed` (reason: user_closed)"
        )
    return (
        f"UoW `{uow_id}` could not be closed \u2014 it is not currently in `blocked` status.\n"
        f"Run `/wos status blocked` to see blocked UoWs."
    )


def handle_wos_status(status: str | None, *, registry: "Registry") -> str:
    """
    Handle /wos status [status].

    When status is None, returns active + pending records (the useful default
    for "what's running and what's queued?").

    Format per record: <id> | <summary> | source: <source> | created: <date>
    """
    if status is None:
        active = registry.list(status="active")
        pending = registry.list(status="pending")
        records = active + pending
        header = "Active + pending UoWs:"
    else:
        records = registry.list(status=status)
        header = f"UoWs with status `{status}`:"

    if not records:
        return f"{header}\n\n(none)"

    lines = [header, ""]
    for r in records:
        summary = r.summary or "(no summary)"
        source = r.source or "unknown"
        created = r.created_at[:10]  # YYYY-MM-DD
        lines.append(f"`{r.id}` | {summary} | source: {source} | created: {created}")

    return "\n".join(lines)


def handle_wos_unblock() -> str:
    """
    Handle /wos unblock.

    Clears BOOTUP_CANDIDATE_GATE by creating the wos-gate-cleared file flag at
    ~/lobster-workspace/data/wos-gate-cleared.

    Once the flag exists, steward-heartbeat.py and executor-heartbeat.py will
    read it on their next invocation and process all UoWs — including those
    with the `bootup-candidate` label — without skipping.

    Idempotent: calling /wos unblock when already unblocked returns a notice
    rather than an error.

    Returns a human-readable Telegram message describing the outcome.
    """
    if _GATE_CLEARED_FLAG.exists():
        return (
            "BOOTUP_CANDIDATE_GATE is already cleared.\n"
            "All UoWs (including bootup-candidates) are being processed normally."
        )

    try:
        _GATE_CLEARED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _GATE_CLEARED_FLAG.touch()
    except OSError as exc:
        return (
            f"Failed to create gate-cleared flag: {exc}\n"
            f"Path: `{_GATE_CLEARED_FLAG}`"
        )

    return (
        "BOOTUP_CANDIDATE_GATE cleared.\n"
        "All 27 bootup-candidate UoWs (#271-#298) will be processed on the next "
        "steward-heartbeat cycle (within 3 minutes).\n"
        f"Flag: `{_GATE_CLEARED_FLAG}`"
    )
