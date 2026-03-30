"""
Dispatcher command handlers for WOS.

These are pure functions: they take a UoW id (or status string) and a Registry
instance, and return a formatted string response suitable for sending back to
Telegram. No MCP tools, no network calls — those belong in the dispatcher.

The dispatcher calls these handlers when it recognizes:
  /approve <uow-id>        → handle_approve(uow_id, registry)
  /wos status [status]     → handle_wos_status(status, registry)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import Registry

from .registry import ApproveConfirmed, ApproveExpired, ApproveNotFound, ApproveSkipped


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
