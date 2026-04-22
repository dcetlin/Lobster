"""
Dispatcher command handlers for WOS.

These are pure functions: they take a UoW id (or status string) and a Registry
instance, and return a formatted string response suitable for sending back to
Telegram. No MCP tools, no network calls — those belong in the dispatcher.

The dispatcher calls these handlers when it recognizes:
  /approve <uow-id>                    → handle_approve(uow_id, registry)
  /decide <uow-id> <proceed|abandon|retry> → handle_decide(uow_id, action, registry)
  /wos status [status]                 → handle_wos_status(status, registry)
  /wos unblock                         → handle_wos_unblock()
  /wos start                           → handle_wos_start()
  /wos stop                            → handle_wos_stop()
  decide retry <uow-id>                → handle_decide_retry(uow_id, registry)
  decide close <uow-id>                → handle_decide_close(uow_id, registry)
  type: "wos_execute"                  → handle_wos_execute(uow_id, instructions, output_ref)

## Compaction-resilient dispatch

WOS_MESSAGE_TYPE_DISPATCH maps inbox message types to handler descriptors.
The dispatcher calls route_wos_message(msg) to dispatch type-routed messages
instead of relying on prose instructions that can be lost under context compaction.
Import and call this table unconditionally — Python imports survive compaction.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .registry import Registry

from .registry import ApproveConfirmed, ApproveExpired, ApproveNotFound, ApproveSkipped
from .paths import LOBSTER_WORKSPACE as _LOBSTER_WORKSPACE, WOS_CONFIG as _WOS_CONFIG_PATH_FROM_PATHS


# ---------------------------------------------------------------------------
# Gate-cleared flag path — mirrors _GATE_CLEARED_FLAG in steward.py
# ---------------------------------------------------------------------------

_GATE_CLEARED_FLAG: Path = _LOBSTER_WORKSPACE / "data" / "wos-gate-cleared"


# ---------------------------------------------------------------------------
# WOS execution config — runtime start/stop for executor dispatch
# ---------------------------------------------------------------------------

_WOS_CONFIG_PATH: Path = _WOS_CONFIG_PATH_FROM_PATHS

_DEFAULT_WOS_CONFIG: dict = {
    "execution_enabled": False,
    "prescription_model": "opus",  # Default to opus; can be overridden by env var or user config
}


def read_wos_config() -> dict:
    """Read wos-config.json from disk and return its contents as a dict.

    Returns _DEFAULT_WOS_CONFIG if the file does not exist or cannot be parsed.
    Reads from disk on every call so that runtime changes take effect immediately
    on the next executor-heartbeat cycle without requiring a restart.
    """
    try:
        with _WOS_CONFIG_PATH.open() as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_WOS_CONFIG)


def is_execution_enabled() -> bool:
    """Return True if WOS execution is enabled in wos-config.json.

    Reads from disk on every call — cron processes get a fresh value on each
    invocation. Default is False (safe) when the file is absent or unreadable.
    """
    return bool(read_wos_config().get("execution_enabled", False))


def _write_wos_config(config: dict) -> None:
    """Write config dict to wos-config.json atomically (write-then-rename)."""
    _WOS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _WOS_CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as fh:
        json.dump(config, fh)
    tmp.rename(_WOS_CONFIG_PATH)


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
                f"Status: `proposed \u2192 ready-for-steward` (via pending)"
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


def handle_decide_retry(uow_id: str, *, registry: "Registry", force: bool = False) -> str:
    """
    Handle a decide_retry action for a UoW.

    Called when Dan selects "Retry" after the Steward surfaces a stuck UoW,
    or sends a message matching "decide retry <uow-id>".

    Resets steward_cycles to 0 and transitions blocked → ready-for-steward so
    the Steward re-diagnoses the UoW on its next heartbeat cycle.

    Hard-cap commitment gate: if the UoW was cleaned up by the hard-cap arc
    (close_reason == "hard_cap_cleanup"), a bare retry is rejected. Pass
    force=True to override after manual operator review.
    """
    rows = registry.decide_retry(uow_id, force=force)
    if rows == 1:
        return (
            f"UoW `{uow_id}` reset for retry.\n"
            f"Status: `blocked \u2192 ready-for-steward` (steward_cycles reset to 0)"
            + (" — hard-cap force override applied" if force else "")
        )
    if rows == registry.DECIDE_RETRY_BLOCKED_BY_HARD_CAP:
        return (
            f"UoW `{uow_id}` cannot be retried \u2014 the hard-cap cleanup arc has run.\n"
            f"This is a commitment gate: the UoW exhausted its lifetime cycle budget. "
            f"To override, use `/decide {uow_id} retry force` (requires explicit operator intent)."
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


_VALID_DECIDE_ACTIONS = frozenset({"proceed", "abandon", "retry", "defer"})


def handle_decide_defer(uow_id: str, note: str = "", *, registry: "Registry") -> str:
    """
    Handle a decide_defer action for a UoW.

    Called when Dan sends `/decide <uow-id> defer [note]` to explicitly
    acknowledge a blocked UoW without yet choosing to retry or close it.
    The UoW remains in `blocked` status; a dated audit entry records the
    deferral decision and any operator note for future context.

    No status transition occurs — the UoW stays blocked until a subsequent
    decide-proceed, decide-retry, or decide-close.
    """
    rows = registry.decide_defer(uow_id, note=note)
    if rows == 1:
        note_suffix = f"\nNote recorded: {note}" if note else ""
        return (
            f"UoW `{uow_id}` deferred.\n"
            f"Status: `blocked` (unchanged) \u2014 audit entry written."
            + note_suffix
        )
    return (
        f"UoW `{uow_id}` could not be deferred \u2014 it is not currently in `blocked` status.\n"
        f"Run `/wos status blocked` to see blocked UoWs."
    )


def handle_decide(uow_id: str, action: str, *, registry: "Registry") -> str:
    """
    Handle /decide <uow-id> <proceed|abandon|retry[force]|defer[note]>.

    Provides a single unified command for resolving blocked UoWs from Telegram.
    Action semantics:
      proceed          — unblock and re-queue to ready-for-steward (preserves steward_cycles)
      retry            — reset steward_cycles to 0 and re-queue to ready-for-steward (full retry)
      retry force      — override the hard-cap commitment gate (explicit operator intent required)
      abandon          — close the UoW as user-requested failure (blocked → failed)
      defer [note]     — leave in blocked, write a dated audit entry with optional note

    All actions operate only on UoWs in `blocked` status — optimistic lock
    prevents accidental double-writes if the UoW has already been advanced.

    Returns a human-readable Telegram message describing the outcome.
    """
    # Support "retry force" as a two-word action token.
    # Support "defer <note>" where any trailing text after "defer" is the note.
    action_normalized = action.lower().strip()
    force_retry = False
    defer_note = ""

    if action_normalized in ("retry force", "force retry"):
        action_normalized = "retry"
        force_retry = True
    elif action_normalized.startswith("defer "):
        # "defer waiting on external review" → action=defer, note="waiting on external review"
        defer_note = action.strip()[len("defer "):].strip()
        action_normalized = "defer"

    if action_normalized not in _VALID_DECIDE_ACTIONS:
        valid = ", ".join(sorted(_VALID_DECIDE_ACTIONS))
        return (
            f"Unknown action `{action}`.\n"
            f"Valid actions: {valid}\n"
            f"Usage: `/decide {uow_id} <{valid}>`"
        )

    match action_normalized:
        case "proceed":
            rows = registry.decide_proceed(uow_id)
            if rows == 1:
                return (
                    f"UoW `{uow_id}` unblocked.\n"
                    f"Status: `blocked \u2192 ready-for-steward` (steward_cycles preserved)"
                )
            return (
                f"UoW `{uow_id}` could not be unblocked \u2014 it is not currently in `blocked` status.\n"
                f"Run `/wos status blocked` to see blocked UoWs."
            )
        case "retry":
            return handle_decide_retry(uow_id, registry=registry, force=force_retry)
        case "abandon":
            return handle_decide_close(uow_id, registry=registry)
        case "defer":
            return handle_decide_defer(uow_id, defer_note, registry=registry)
        case _:
            # Unreachable — guarded by frozenset check above — but satisfies mypy exhaustiveness
            return f"Unhandled action `{action}`."


def handle_wos_status(status: str | None, *, registry: "Registry") -> str:
    """
    Handle /wos status [status].

    When status is None, returns active + ready-for-steward + pending records
    (the useful default for "what's running and what's queued?"). Pending is
    included for backward compatibility with any UoWs that were written before
    the auto-advance change; in normal operation pending is never a resting state.

    Format per record: <id> | <summary> | source: <source> | created: <date>
    """
    if status is None:
        active = registry.list(status="active")
        ready_for_steward = registry.list(status="ready-for-steward")
        pending = registry.list(status="pending")
        records = active + ready_for_steward + pending
        header = "Active + queued UoWs:"
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


def handle_wos_execute(uow_id: str, instructions: str, output_ref: str) -> str:
    """
    Build the Task prompt for a wos_execute inbox message.

    Called by the dispatcher when it receives a message with type="wos_execute".
    Returns the prompt string to pass to the background functional-engineer subagent
    via the Task tool. The dispatcher is responsible for the actual Task spawn and
    the mark_processing / mark_processed bookkeeping — this function is pure.

    The dispatched subagent must write a result file at output_ref with the schema:
        {
            "uow_id": "<uow_id>",
            "outcome": "complete" | "partial" | "failed" | "blocked",
            "success": true | false,       # true iff outcome == "complete"
            "reason": "<optional explanation>"   # required when success is false
        }
    Outcome semantics:
        "complete"  — all prescribed steps finished without error
        "partial"   — some steps completed; subagent stopped intentionally before finishing
        "failed"    — execution could not proceed; reason explains what went wrong
        "blocked"   — an external dependency prevents progress; reason names the blocker

    Dispatch is fire-and-forget: the Executor does not block waiting for the subagent.
    The Steward detects completion on its next heartbeat cycle by reading output_ref.
    If the subagent fails to write the result file before timeout_at, the Observation
    Loop detects the stall and surfaces it to the user.

    Args:
        uow_id:       The Unit of Work identifier (used as task_id and in the result file).
        instructions: The prescribed instructions from the WorkflowArtifact — what the
                      subagent must do to execute this UoW.
        output_ref:   Absolute path where the subagent must write its result file.
                      This must be the result file path (`{uow_id}.result.json`), NOT the
                      artifact path (`{uow_id}.json`). The Executor computes it as:
                      `_result_json_path(_output_ref_path(uow_id))` before dispatch.
                      Conventionally: ~/lobster-workspace/orchestration/outputs/{uow_id}.result.json
                      This is the path the Steward reads on its next heartbeat to detect completion.

    Returns:
        A prompt string for the functional-engineer subagent Task call.
    """
    return (
        f"---\n"
        f"task_id: wos-{uow_id}\n"
        f"chat_id: 0\n"
        f"source: system\n"
        f"---\n\n"
        f"You are executing a Work Order System (WOS) unit of work on behalf of the Steward.\n"
        f"UoW ID: {uow_id}\n\n"
        f"## Instructions\n\n"
        f"{instructions}\n\n"
        f"## Result contract (REQUIRED)\n\n"
        f"After completing the instructions (or on any error that prevents completion),\n"
        f"write the result file to: {output_ref}\n\n"
        f"The file must be valid JSON matching one of these shapes:\n"
        f'  {{"uow_id": "{uow_id}", "outcome": "complete", "success": true}}\n'
        f'  {{"uow_id": "{uow_id}", "outcome": "failed", "success": false, "reason": "<why>"}}\n'
        f'  {{"uow_id": "{uow_id}", "outcome": "partial", "success": false, "reason": "<what was done and what was not>"}}\n'
        f'  {{"uow_id": "{uow_id}", "outcome": "blocked", "success": false, "reason": "<what is blocking and why>"}}\n\n'
        f"Outcome values: \"complete\" | \"partial\" | \"failed\" | \"blocked\"\n"
        f"\"success\" must be true if and only if outcome == \"complete\".\n\n"
        f"Steps to write the file:\n"
        f"  1. mkdir -p {'/'.join(output_ref.split('/')[:-1])}\n"
        f"  2. Write JSON to {output_ref}.tmp, then rename to {output_ref}\n\n"
        f"After writing the result file:\n"
        f'  write_result(task_id="wos-{uow_id}", chat_id=0, source="system",\n'
        f'               text="WOS UoW {uow_id}: outcome=<outcome>")\n\n'
        f"Minimum viable output: {output_ref} with uow_id, outcome, and success fields.\n"
        f"Boundary: do not modify executor.py, registry.py, or any WOS source files.\n"
    )


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


def handle_wos_start() -> str:
    """
    Handle /wos start (or "wos start").

    Sets execution_enabled: true in wos-config.json so that executor-heartbeat
    dispatches UoWs on its next cycle (within ~90 seconds).

    Idempotent: calling /wos start when already started returns a notice.

    Returns a human-readable Telegram message describing the outcome.
    """
    config = read_wos_config()
    if config.get("execution_enabled"):
        return (
            "WOS execution is already enabled.\n"
            "executor-heartbeat is dispatching UoWs normally."
        )

    try:
        _write_wos_config({**config, "execution_enabled": True})
    except OSError as exc:
        return (
            f"Failed to write wos-config.json: {exc}\n"
            f"Path: `{_WOS_CONFIG_PATH}`"
        )

    return (
        "WOS execution enabled.\n"
        "executor-heartbeat will dispatch ready-for-executor UoWs on its next cycle "
        "(within ~90 seconds).\n"
        f"Config: `{_WOS_CONFIG_PATH}`"
    )


def handle_wos_stop() -> str:
    """
    Handle /wos stop (or "wos stop").

    Sets execution_enabled: false in wos-config.json so that executor-heartbeat
    skips dispatch on its next cycle. UoWs already active are not affected —
    TTL recovery will handle any that stall.

    Idempotent: calling /wos stop when already stopped returns a notice.

    Returns a human-readable Telegram message describing the outcome.
    """
    config = read_wos_config()
    if not config.get("execution_enabled"):
        return (
            "WOS execution is already disabled.\n"
            "executor-heartbeat is skipping dispatch."
        )

    try:
        _write_wos_config({**config, "execution_enabled": False})
    except OSError as exc:
        return (
            f"Failed to write wos-config.json: {exc}\n"
            f"Path: `{_WOS_CONFIG_PATH}`"
        )

    return (
        "WOS execution disabled.\n"
        "executor-heartbeat will skip dispatch on its next cycle (within ~90 seconds).\n"
        "UoWs already active will continue running; TTL recovery handles any that stall.\n"
        f"Config: `{_WOS_CONFIG_PATH}`"
    )


# ---------------------------------------------------------------------------
# Compaction-resilient message-type dispatch table
#
# Maps inbox message `type` values to handler descriptors.  The dispatcher
# calls route_wos_message(msg) instead of embedding routing logic in prose
# instructions — prose can be lost under context compaction, Python imports
# cannot.
#
# Dispatcher integration (add to main loop):
#
#     from src.orchestration.dispatcher_handlers import route_wos_message
#
#     if msg.get("type") in WOS_MESSAGE_TYPE_DISPATCH:
#         result = route_wos_message(msg)
#         # result["action"] tells the dispatcher what to do next
#         # See route_wos_message docstring for the result schema.
# ---------------------------------------------------------------------------

WOS_MESSAGE_TYPE_DISPATCH: dict[str, str] = {
    # message type → handler name (used as a stable, compaction-safe key)
    "wos_execute": "handle_wos_execute",
}


def route_wos_message(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Route an inbox message whose `type` is listed in WOS_MESSAGE_TYPE_DISPATCH.

    This is the compaction-resilient entry point for WOS message routing.  The
    dispatcher should call this function rather than conditionally re-reading
    prose documentation that may not survive context compaction.

    For ``type: "wos_execute"`` the function extracts the required fields and
    builds the subagent prompt via ``handle_wos_execute``.  The dispatcher is
    still responsible for spawning the subagent Task and for all
    mark_processing / mark_processed bookkeeping — this function is pure.

    Args:
        msg: The raw inbox message dict as returned by ``wait_for_messages``.
             Must contain ``type`` and the type-specific payload fields.

    Returns:
        A dict with the following keys:

        ``action`` (str):
            What the dispatcher must do.  Currently always ``"spawn_subagent"``
            for ``wos_execute`` messages.

        ``task_id`` (str):
            The ``task_id`` to pass to the Task tool (e.g. ``"wos-<uow_id>"``).

        ``prompt`` (str):
            The prompt string to pass to the background subagent Task call.

        ``message_type`` (str):
            Echo of ``msg["type"]`` — lets callers confirm which branch fired.

    Raises:
        KeyError: if a required field is missing from ``msg``.
        ValueError: if ``msg["type"]`` is not in ``WOS_MESSAGE_TYPE_DISPATCH``.

    Example dispatcher integration::

        from src.orchestration.dispatcher_handlers import (
            route_wos_message,
            WOS_MESSAGE_TYPE_DISPATCH,
        )

        msg_type = msg.get("type", "")
        if msg_type in WOS_MESSAGE_TYPE_DISPATCH:
            routing = route_wos_message(msg)
            # routing["action"] == "spawn_subagent"
            # spawn Task(routing["prompt"], run_in_background=True,
            #             task_id=routing["task_id"])
            mark_processed(message_id)
    """
    msg_type: str = msg.get("type", "")

    if msg_type not in WOS_MESSAGE_TYPE_DISPATCH:
        raise ValueError(
            f"route_wos_message: unrecognised message type {msg_type!r}. "
            f"Known types: {sorted(WOS_MESSAGE_TYPE_DISPATCH)}"
        )

    if msg_type == "wos_execute":
        uow_id: str = msg["uow_id"]
        instructions: str = msg["instructions"]
        # output_ref may be supplied by the Executor, or derived from uow_id
        output_ref: str = msg.get(
            "output_ref",
            str(
                Path.home()
                / "lobster-workspace"
                / "orchestration"
                / "outputs"
                / f"{uow_id}.result.json"
            ),
        )
        prompt = handle_wos_execute(uow_id, instructions, output_ref)
        return {
            "action": "spawn_subagent",
            "task_id": f"wos-{uow_id}",
            "prompt": prompt,
            "message_type": msg_type,
        }

    # Unreachable given the guard above, but satisfies exhaustiveness checkers
    raise ValueError(f"route_wos_message: no branch for type {msg_type!r}")


# ---------------------------------------------------------------------------
# Vision Object callback handler — vision_accept / vision_decline
# ---------------------------------------------------------------------------


def handle_vision_callback(callback_data: str, chat_id: int = 6036) -> str | None:
    """
    Handle Telegram inline keyboard callbacks for the Vision Object inlet.

    Parses ``callback_data`` for ``vision_accept:<field_path>:<hash>`` and
    ``vision_decline:<field_path>:<hash>`` prefixes and routes to the accept or
    decline handler in ``src.harvest.vision_inlet``.

    Returns a reply string if this is a vision callback, or ``None`` if the
    callback_data does not match a vision prefix (so the caller can route other
    callbacks normally).

    Dispatcher integration — add to the callback handling branch::

        from src.orchestration.dispatcher_handlers import handle_vision_callback

        if msg.get("type") == "callback":
            callback_data = msg.get("callback_data", "")
            reply = handle_vision_callback(callback_data, chat_id=msg.get("chat_id", 6036))
            if reply is not None:
                send_reply(chat_id=msg["chat_id"], text=reply, message_id=msg["id"])
                return  # mark processed and continue main loop
    """
    if not (callback_data.startswith("vision_accept:") or callback_data.startswith("vision_decline:")):
        return None

    try:
        from src.harvest.vision_inlet import handle_vision_callback as _vi_callback  # type: ignore[import]
    except ImportError:
        return "vision_inlet module unavailable — cannot process vision callback."

    return _vi_callback(callback_data, chat_id=chat_id)
