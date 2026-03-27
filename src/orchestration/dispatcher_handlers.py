"""
Dispatcher command handlers for WOS Phase 1.

These are pure functions: they take a UoW id (or status string) and a Registry
instance, and return a formatted string response suitable for sending back to
Telegram. No MCP tools, no network calls — those belong in the dispatcher.

The dispatcher calls these handlers when it recognizes:
  /confirm <uow-id>        → handle_confirm(uow_id, registry)
  /wos status [status]     → handle_wos_status(status, registry)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import Registry


def handle_confirm(uow_id: str, *, registry: "Registry") -> str:
    """
    Handle /confirm <uow-id>.

    Returns a human-readable Telegram message describing the outcome.
    Error cases follow the spec from the design doc §6 Phase 1 implementation notes.
    """
    result = registry.confirm(uow_id)

    if "error" in result:
        error = result["error"]
        if error == "not found":
            return (
                f"UoW `{uow_id}` not found. "
                "Run `/wos status proposed` to see current proposals."
            )
        if error == "expired":
            return (
                f"UoW `{uow_id}` has expired. "
                "Wait for the next sweep to re-propose, or run a manual sweep."
            )
        return f"Error confirming `{uow_id}`: {result.get('message', error)}"

    if result.get("action") == "noop":
        current = result["status"]
        return f"UoW `{uow_id}` is already `{current}` — no action taken."

    return (
        f"UoW `{uow_id}` confirmed.\n"
        f"Status: `proposed \u2192 pending`"
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
        uow_id = r["id"]
        summary = r.get("summary", "(no summary)")
        source = r.get("source", "unknown")
        created = (r.get("created_at") or "")[:10]  # YYYY-MM-DD
        lines.append(f"`{uow_id}` | {summary} | source: {source} | created: {created}")

    return "\n".join(lines)
