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
  type: "callback" (decide_retry/close)→ route_callback_message(msg)

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
from .paths import LOBSTER_WORKSPACE as _LOBSTER_WORKSPACE, WOS_CONFIG as _WOS_CONFIG_PATH_FROM_PATHS, WOS_GATE_CLEARED_FLAG as _GATE_CLEARED_FLAG
from .steward import ReturnReasonClassification, MAX_RETRIES as _STEWARD_MAX_RETRIES, _HARD_CAP_CYCLES


# ---------------------------------------------------------------------------
# WOS execution config — runtime start/stop for executor dispatch
# ---------------------------------------------------------------------------

_WOS_CONFIG_PATH: Path = _WOS_CONFIG_PATH_FROM_PATHS

_DEFAULT_WOS_CONFIG: dict = {
    "execution_enabled": False,
    "prescription_model": "opus",  # Default to opus; can be overridden by env var or user config
    # max_parallel: maximum number of UoWs that may execute concurrently.
    # The steward shard-stream gate enforces this cap before dispatching
    # a new UoW to ready-for-executor. Requires non-overlapping file_scope
    # annotations on concurrent candidates. Default 2 (conservative).
    "max_parallel": 2,
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

    The subagent is also required to write periodic heartbeats to the registry so the
    Observation Loop can detect stalls before the 4h TTL (issue #849). Heartbeats must
    be written every 60-90 seconds — before reading the issue, after implementation,
    before PR creation. The heartbeat is a single registry call, documented below.

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
        f"## Heartbeat contract (REQUIRED)\n\n"
        f"You must write a liveness heartbeat every 60–90 seconds throughout execution.\n"
        f"The heartbeat proves you are alive — without it, the Observation Loop will detect\n"
        f"a stall and re-queue this UoW for re-execution.\n\n"
        f"Call write_heartbeat at these checkpoints (at minimum):\n"
        f"  - Before starting work (immediately after receiving this prompt)\n"
        f"  - After reading/understanding the issue or task\n"
        f"  - After completing the implementation\n"
        f"  - Before opening a PR or writing the result file\n\n"
        f"Preferred: use the MCP tool (no Python imports needed):\n"
        f"  mcp__lobster-inbox__write_wos_heartbeat(uow_id='{uow_id}', token_usage=<cumulative_tokens>)\n\n"
        f"token_usage in write_wos_heartbeat: pass your running cumulative total of\n"
        f"input_tokens + output_tokens from all Claude API responses received so far.\n"
        f"Track this across all API calls in your session and pass the updated total at each\n"
        f"heartbeat. The steward uses consecutive token deltas to detect stuck agents.\n"
        f"Omit token_usage only if you are not tracking tokens at all.\n\n"
        f"Fallback: call the registry directly via Bash:\n"
        f"  import sys; sys.path.insert(0, '/home/lobster/lobster')\n"
        f"  from src.orchestration.registry import WOSRegistry\n"
        f"  WOSRegistry().write_heartbeat('{uow_id}', token_usage=<cumulative_tokens>)\n\n"
        f"The heartbeat call returns rowcount: 1 on success, 0 if the UoW status has changed.\n"
        f"A return value of 0 (or {{\"rowcount\": 0}} from the MCP tool) means the Steward\n"
        f"has already re-queued this UoW — stop execution immediately and call write_result\n"
        f"with outcome=failed.\n\n"
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
        f'               text="WOS UoW {uow_id}: outcome=<outcome>",\n'
        f'               token_usage=<total_input_plus_output_tokens>)\n\n'
        f"token_usage: accumulate usage.input_tokens + usage.output_tokens from every Claude API\n"
        f"response across all turns and report the total. This enables per-UoW cost telemetry.\n"
        f"Omit token_usage if you did not track it.\n\n"
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
    # Post-completion steward trigger (issue #912): written by wos_completion.py
    # after executing → ready-for-steward transition. Dispatcher calls
    # handle_steward_trigger() which returns a spawn_subagent action, running
    # the steward heartbeat as a background subagent (7-second rule compliant),
    # bypassing the 0–3 minute cron wait.
    "steward_trigger": "handle_steward_trigger",
    # Dispatcher escalation handler (issue #969): written by the Steward when a UoW
    # exhausts its retry cap. The dispatcher routes wos_escalate through a 4-branch
    # decision tree before deciding whether to auto-retry or surface to Dan.
    # Unlike wos_execute/steward_trigger, this handler legitimately returns either
    # action="spawn_subagent" (auto-retry branches) or action="send_reply" (surface-to-Dan
    # branches) — it is exempt from the spawn-gate that applies to execution message types.
    "wos_escalate": "handle_wos_escalate",
    # Batch escalation handler (T1-A): written by the Steward when >= 3 UoWs escalate
    # in one steward cycle (consolidated kill wave) or when _write_wos_escalate_message
    # raises an OSError (write-failure fallback).  Like wos_escalate, this handler is
    # exempt from the spawn-gate — it legitimately returns action="spawn_subagent" for
    # all-orphan auto-retry and action="send_reply" for surface-to-Dan branches.
    "wos_surface": "handle_wos_surface",
    # Manual-trigger forensics handler: written by the dispatcher when Dan types
    # "diagnose <uow_id>". Spawns a diagnostic subagent that runs registry_cli trace
    # and returns a structured forensic report. Always returns action="spawn_subagent";
    # runs inside the spawn-gate block where the enforcement check is always satisfied.
    "wos_diagnose": "handle_wos_diagnose",
    # PR sweep result handler: written by wos-pr-sweeper.py (Type C cron script) when
    # stale open PRs or merged PRs with non-done UoWs are detected. Fast-path — dispatched
    # before the spawn-gate because this handler legitimately returns action="send_reply"
    # (surface PR attention items to Dan). No subagent spawn required.
    "wos_pr_sweep_result": "handle_wos_pr_sweep_result",
}


def handle_steward_trigger(uow_id: str) -> dict[str, Any]:
    """
    Handle a steward_trigger inbox message by spawning a background subagent.

    Called by route_wos_message when the dispatcher receives a message with
    type="steward_trigger". This is the post-completion event-driven path
    (issue #912): rather than waiting up to 3 minutes for the next cron tick,
    the dispatcher spawns a background subagent to run the steward heartbeat
    immediately after a UoW completes.

    Returns a spawn_subagent action so the dispatcher runs the steward heartbeat
    as a background subagent, consistent with how wos_execute messages are handled
    and compliant with the 7-second rule (no synchronous subprocess blocking).
    The 3-minute cron remains the recovery fallback if the subagent fails.

    Returns:
        A dict with action="spawn_subagent" containing the task_id, agent_type,
        and prompt for the dispatcher to pass to the background Task call.

    Args:
        uow_id: The UoW whose completion triggered this message.
    """
    steward_script = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '..', 'scheduled-tasks', 'steward-heartbeat.py')
    )
    task_id = f"steward-trigger-{uow_id[:8]}"
    return {
        "action": "spawn_subagent",
        "task_id": task_id,
        "agent_type": "lobster-generalist",
        "prompt": (
            f"---\n"
            f"task_id: {task_id}\n"
            f"chat_id: 0\n"
            f"source: system\n"
            f"---\n\n"
            f"Run the steward heartbeat to process newly completed UoW {uow_id}:\n\n"
            f"```bash\n"
            f"cd ~/lobster-workspace && uv run python {steward_script}\n"
            f"```\n\n"
            f"Then call write_result with the steward output.\n\n"
            f"Minimum viable output: steward heartbeat completed.\n"
            f"Boundary: do not modify any UoW status directly."
        ),
    }


# ---------------------------------------------------------------------------
# wos_escalate handler — dispatcher escalation decision tree (issue #969)
#
# Called when a UoW exhausts its retry cap and the Steward writes a wos_escalate
# inbox message instead of notifying Dan directly.  The handler classifies the
# failure and routes it: auto-retry for infrastructure kills, surface to Dan for
# genuine execution failures or human-judgment UoWs.
#
# This handler is exempt from the spawn-gate that governs wos_execute and
# steward_trigger — it legitimately returns action="send_reply" for the
# surface-to-Dan branches and action="spawn_subagent" for the auto-retry branches.
# route_wos_message handles this by dispatching wos_escalate outside the spawn-gate.
# ---------------------------------------------------------------------------

# Execution attempts threshold at which the handler surfaces to Dan regardless
# of return_reason_classification.  3 confirmed execution attempts means the
# prescription itself may be broken — auto-retrying without diagnosis loops forever.
_ESCALATE_SURFACE_EXECUTION_THRESHOLD: int = 3

# Registers that bypass auto-retry and surface to Dan immediately.
# The structured executor was never the right tool for these UoW types.
_ESCALATE_HUMAN_JUDGMENT_REGISTERS: frozenset[str] = frozenset({
    "human-judgment",
    "philosophical",
})

# return_reason_classification values that indicate an infrastructure kill
# (session killed before or during execution — no execution outcome produced).
_ESCALATE_ORPHAN_CLASSIFICATIONS: frozenset[str] = frozenset({
    ReturnReasonClassification.ORPHAN,
})


# ---------------------------------------------------------------------------
# wos_surface handler — batch escalation dispatcher (T1-A)
#
# Called when the Steward writes a wos_surface message.  This happens in two cases:
#   1. Consolidated kill wave (>= ESCALATION_CONSOLIDATION_THRESHOLD UoWs escalate in
#      one steward cycle) — condition="retry_cap_consolidated", carries uow_ids list.
#   2. Write-failure fallback (_send_escalation_notification) — condition="retry_cap",
#      carries singular uow_id.
#
# Like wos_escalate, this handler is exempt from the spawn-gate — it legitimately
# returns either action="spawn_subagent" (all-orphan auto-retry) or action="send_reply"
# (surface-to-Dan branches).  route_wos_message dispatches wos_surface outside the
# spawn-gate, parallel to the wos_escalate fast-path.
# ---------------------------------------------------------------------------

# return_reason strings (raw, not classifications) that identify infrastructure kill events
# eligible for auto-retry.  These are the same strings stored in metadata.causes by
# _send_consolidated_escalation_notification — they come from EscalationRecord.return_reason,
# which is the raw return_reason string, not the classification.
_SURFACE_ORPHAN_RETURN_REASONS: frozenset[str] = frozenset({
    "executor_orphan",
    "executing_orphan",
    "diagnosing_orphan",
    "orphan_kill_before_start",
    "orphan_kill_during_execution",
})


def handle_wos_escalate(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_escalate`` inbox message via the 4-branch dispatcher decision tree.

    Called by route_wos_message when the dispatcher receives a message with
    type="wos_escalate".  The Steward writes this message when a UoW exhausts
    its execution retry cap (MAX_RETRIES on execution_attempts, not retry_count),
    inserting a programmatic triage layer before the human-judgment escalation path.

    Pure function — no side effects, no I/O.  All branches return a dict describing
    what the dispatcher must do next.

    Decision tree (checked in order; first matching branch wins):

    **Branch 4 — Human-judgment register** (checked first — register overrides all):
        If ``register`` is "human-judgment" or "philosophical", surface to Dan
        immediately.  The structured executor was never the right tool; retrying
        would waste cycles.
        → Returns ``action="send_reply"`` with structured context for Dan.

    **Branch 3 — Execution cap exhausted** (3+ confirmed execution_attempts):
        If ``execution_attempts >= _ESCALATE_SURFACE_EXECUTION_THRESHOLD``, the
        prescription has been attempted multiple times and failed.  Auto-retrying
        would loop without diagnosis.
        → Returns ``action="send_reply"`` with structured context for Dan.

    **Branch 1 — Pure infrastructure failure** (execution_attempts == 0, orphan):
        If ``execution_attempts == 0`` and ``return_reason_classification`` is
        "orphan", the UoW was never executed — the session was killed before the
        subagent established working state.  The original prescription is intact.
        → Returns ``action="spawn_subagent"`` to run steward heartbeat (auto-retry).

    **Branch 2 — Mid-execution kill** (execution_attempts > 0, orphan):
        If ``execution_attempts > 0`` and ``return_reason_classification`` is
        "orphan", the subagent was killed mid-execution.  Partial work may exist.
        → Returns ``action="spawn_subagent"`` to run steward heartbeat (retry).

    **Default — surface to Dan** (unclassified failures):
        Any failure not matched by the above branches is surfaced to Dan.
        → Returns ``action="send_reply"`` with structured context.

    Args:
        msg: The raw wos_escalate inbox message dict.  Expected fields:
            - ``uow_id`` (str): The Unit of Work identifier.
            - ``uow_title`` (str, optional): Human-readable UoW title.
            - ``register`` (str, optional): UoW register ("operational", "human-judgment",
              "philosophical", "iterative-convergent"). Default "operational".
            - ``failure_history`` (dict): Failure context from the Steward.
              Key sub-fields:
                - ``execution_attempts`` (int): Confirmed execution attempts.
                - ``return_reason_classification`` (str): Classification of the last
                  return reason ("orphan", "error", "abnormal", etc.).
                - ``kill_type`` (str, optional): Heartbeat-derived kill classification
                  ("orphan_kill_before_start", "orphan_kill_during_execution").
                - ``heartbeats_before_kill`` (int, optional): Heartbeats written before
                  the subagent was killed.  0 means killed before execution began.
            - ``posture`` (str, optional): Trace-diagnosed reentry posture.

    Returns:
        A dict with ``action`` and branch-specific fields:

        For ``action="spawn_subagent"`` (auto-retry branches 1 and 2):
            - ``task_id`` (str): Task identifier for the steward heartbeat subagent.
            - ``agent_type`` (str): Always "lobster-generalist".
            - ``prompt`` (str): Subagent prompt to run the steward heartbeat.
            - ``message_type`` (str): Echo of "wos_escalate".

        For ``action="send_reply"`` (surface-to-Dan branches 3 and 4):
            - ``text`` (str): Telegram notification text with structured context.
            - ``chat_id`` (str | int): Admin chat ID (from LOBSTER_ADMIN_CHAT_ID env var).
            - ``message_type`` (str): Echo of "wos_escalate".
    """
    uow_id: str = msg.get("uow_id", "unknown")
    uow_title: str = msg.get("uow_title", "")
    register: str = msg.get("register", "operational")
    failure_history: dict[str, Any] = msg.get("failure_history", {})
    posture: str = msg.get("posture", "")

    execution_attempts: int = int(failure_history.get("execution_attempts", 0))
    return_reason_classification: str = failure_history.get("return_reason_classification", "")
    kill_type: str = failure_history.get("kill_type", "")
    heartbeats_before_kill: int = int(failure_history.get("heartbeats_before_kill", 0))

    _msg_type = "wos_escalate"

    # Branch 4 — Human-judgment register: surface immediately, no retry.
    # Checked first — register classification overrides all other branches.
    if register in _ESCALATE_HUMAN_JUDGMENT_REGISTERS:
        text = (
            f"WOS escalation: UoW `{uow_id}` is in `{register}` register — "
            f"surfaces for human judgment rather than executor retry.\n\n"
            f"Title: {uow_title}\n"
            f"Register: {register}\n"
            f"Execution attempts: {execution_attempts}\n"
            f"Posture: {posture}\n\n"
            f"The structured executor cannot resolve this UoW. "
            f"Please review and either `/decide {uow_id} proceed` or `/decide {uow_id} abandon`."
        )
        return {
            "action": "send_reply",
            "text": text,
            "chat_id": os.environ.get("LOBSTER_ADMIN_CHAT_ID", "0"),
            "message_type": _msg_type,
        }

    # Branch 3 — Execution cap exhausted: surface to Dan.
    # execution_attempts >= threshold means the prescription was tried multiple times.
    if execution_attempts >= _ESCALATE_SURFACE_EXECUTION_THRESHOLD:
        text = (
            f"WOS escalation: UoW `{uow_id}` exhausted execution attempts.\n\n"
            f"Title: {uow_title}\n"
            f"Execution attempts: {execution_attempts} (threshold: {_ESCALATE_SURFACE_EXECUTION_THRESHOLD})\n"
            f"Return reason classification: {return_reason_classification}\n"
            f"Kill type: {kill_type or 'n/a'}\n"
            f"Posture: {posture}\n\n"
            f"The executor ran {execution_attempts} times without completing. "
            f"Please review the prescription and either:\n"
            f"  `/decide {uow_id} retry` — reset and re-queue with fresh prescription\n"
            f"  `/decide {uow_id} abandon` — close as failed"
        )
        return {
            "action": "send_reply",
            "text": text,
            "chat_id": os.environ.get("LOBSTER_ADMIN_CHAT_ID", "0"),
            "message_type": _msg_type,
        }

    # Branch 1 — Pure infrastructure failure: auto-retry via steward heartbeat.
    # execution_attempts == 0 AND orphan classification means the UoW was never executed.
    if execution_attempts == 0 and return_reason_classification in _ESCALATE_ORPHAN_CLASSIFICATIONS:
        task_id = f"escalate-retry-{uow_id[:12]}"
        steward_script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..', 'scheduled-tasks', 'steward-heartbeat.py')
        )
        prompt = (
            f"---\n"
            f"task_id: {task_id}\n"
            f"chat_id: 0\n"
            f"source: system\n"
            f"---\n\n"
            f"WOS escalation auto-retry: UoW `{uow_id}` was killed before execution began "
            f"(kill_type={kill_type!r}, execution_attempts=0). "
            f"The prescription is intact — running steward heartbeat to re-queue.\n\n"
            f"```bash\n"
            f"cd ~/lobster-workspace && uv run python {steward_script}\n"
            f"```\n\n"
            f"Then call write_result with the steward output.\n\n"
            f"Minimum viable output: steward heartbeat completed for UoW {uow_id}.\n"
            f"Boundary: do not modify any UoW status directly."
        )
        return {
            "action": "spawn_subagent",
            "task_id": task_id,
            "agent_type": "lobster-generalist",
            "prompt": prompt,
            "message_type": _msg_type,
        }

    # Branch 2 — Mid-execution kill: retry via steward heartbeat.
    # execution_attempts > 0 AND orphan classification means the subagent was killed
    # while working. Partial output may exist; retry is still warranted.
    if return_reason_classification in _ESCALATE_ORPHAN_CLASSIFICATIONS:
        task_id = f"escalate-midexec-{uow_id[:12]}"
        steward_script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..', 'scheduled-tasks', 'steward-heartbeat.py')
        )
        prompt = (
            f"---\n"
            f"task_id: {task_id}\n"
            f"chat_id: 0\n"
            f"source: system\n"
            f"---\n\n"
            f"WOS escalation mid-execution retry: UoW `{uow_id}` was killed during execution "
            f"(kill_type={kill_type!r}, heartbeats_before_kill={heartbeats_before_kill}, "
            f"execution_attempts={execution_attempts}). "
            f"Partial output may exist — running steward heartbeat to re-queue with resume context.\n\n"
            f"```bash\n"
            f"cd ~/lobster-workspace && uv run python {steward_script}\n"
            f"```\n\n"
            f"Then call write_result with the steward output.\n\n"
            f"Minimum viable output: steward heartbeat completed for UoW {uow_id}.\n"
            f"Boundary: do not modify any UoW status directly."
        )
        return {
            "action": "spawn_subagent",
            "task_id": task_id,
            "agent_type": "lobster-generalist",
            "prompt": prompt,
            "message_type": _msg_type,
        }

    # Default — unclassified failure: surface to Dan.
    text = (
        f"WOS escalation: UoW `{uow_id}` requires review (unclassified failure).\n\n"
        f"Title: {uow_title}\n"
        f"Execution attempts: {execution_attempts}\n"
        f"Return reason classification: {return_reason_classification or 'unknown'}\n"
        f"Kill type: {kill_type or 'n/a'}\n"
        f"Posture: {posture or 'unknown'}\n\n"
        f"Please review and either:\n"
        f"  `/decide {uow_id} retry` — reset and re-queue\n"
        f"  `/decide {uow_id} abandon` — close as failed"
    )
    return {
        "action": "send_reply",
        "text": text,
        "chat_id": os.environ.get("LOBSTER_ADMIN_CHAT_ID", "0"),
        "message_type": _msg_type,
    }


def handle_wos_surface(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_surface`` inbox message via the batch dispatcher decision tree (T1-A).

    Called by route_wos_message when the dispatcher receives a message with
    type="wos_surface".  The Steward writes this message in two situations:

    1. **Consolidated kill wave** (condition="retry_cap_consolidated"): written by
       ``_send_consolidated_escalation_notification`` when >= 3 UoWs escalate in one
       steward cycle.  Carries ``metadata.uow_ids`` (list) and ``metadata.causes`` (list
       of raw return_reason strings).

    2. **Write-failure fallback** (condition="retry_cap"): written by
       ``_send_escalation_notification`` when ``_write_wos_escalate_message`` raises an
       OSError.  Carries ``metadata.uow_id`` (singular) and no causes list.

    Decision tree (checked in order; first matching branch wins):

    **Branch: Pipeline paused** (execution_enabled=False):
        All UoWs surface to Dan regardless of return_reason.  Auto-retrying into a
        stopped pipeline is never safe.
        → Returns ``action="send_reply"`` with pipeline-paused note and UoW list.

    **Branch: All causes are orphan return_reasons** (infrastructure kill wave):
        Every cause in ``metadata.causes`` is in ``_SURFACE_ORPHAN_RETURN_REASONS``.
        The batch is a single infrastructure event — all UoWs can be safely auto-retried.
        Spawns one steward heartbeat subagent (steward re-queues all UoWs on its next cycle).
        Sends Dan a brief summary notification (one message, no action required).
        → Returns ``action="spawn_subagent"`` targeting the steward heartbeat.

    **Branch: Mixed causes** (some orphan, some non-orphan):
        Auto-retry eligible UoWs are identified by cross-referencing their position in
        ``uow_ids`` against ``causes``.  Non-orphan UoWs surface to Dan individually.
        → Returns ``action="send_reply"`` with the non-orphan UoW IDs and a note that
        orphan UoWs were auto-retried.

    **Default: All causes are non-orphan or causes list is absent**:
        Surface all UoWs to Dan with structured context.  No auto-retry.
        → Returns ``action="send_reply"`` with all UoW IDs.

    This handler is exempt from the spawn-gate (see route_wos_message) because it
    legitimately returns either action for different branches.

    Args:
        msg: The raw wos_surface inbox message dict.  Expected fields (in metadata):
            - ``type`` (str): "wos_surface"
            - ``condition`` (str): "retry_cap_consolidated" | "retry_cap" | StuckCondition
            - ``uow_ids`` (list[str], optional): Affected UoW IDs (retry_cap_consolidated)
            - ``uow_id`` (str, optional): Single affected UoW (retry_cap fallback)
            - ``causes`` (list[str], optional): Raw return_reason strings per UoW
            - ``escalation_count`` (int, optional): Number of UoWs in the batch

    Returns:
        A dict with ``action`` and branch-specific fields — same schema as
        ``handle_wos_escalate``.

        For ``action="spawn_subagent"`` (all-orphan auto-retry):
            - ``task_id`` (str): Batch retry task identifier.
            - ``agent_type`` (str): "lobster-generalist".
            - ``prompt`` (str): Subagent prompt to run the steward heartbeat.
            - ``message_type`` (str): "wos_surface".

        For ``action="send_reply"`` (surface-to-Dan branches):
            - ``text`` (str): Telegram notification text with structured context.
            - ``chat_id`` (str | int): Admin chat ID.
            - ``message_type`` (str): "wos_surface".
    """
    metadata: dict[str, Any] = msg.get("metadata", {})
    condition: str = metadata.get("condition", "")

    # Extract UoW IDs — support both retry_cap_consolidated (list) and retry_cap (singular)
    uow_ids: list[str] = metadata.get("uow_ids") or []
    if not uow_ids:
        singular = metadata.get("uow_id")
        if singular:
            uow_ids = [singular]

    causes: list[str] = metadata.get("causes") or []
    _msg_type = "wos_surface"
    chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "0")

    # Branch: Pipeline paused — surface all regardless of return_reasons.
    # Spawning a steward heartbeat into a stopped pipeline re-queues work
    # that will never execute, building up stale ready-for-executor entries.
    if not is_execution_enabled():
        uow_list = "\n".join(f"  - `{uid}`" for uid in uow_ids) if uow_ids else "  (none listed)"
        text = (
            f"WOS kill wave ({condition}): {len(uow_ids)} UoW(s) surfaced — "
            f"pipeline is paused (execution_enabled=false).\n\n"
            f"Auto-retry was not attempted because executor dispatch is disabled.\n\n"
            f"Affected UoWs:\n{uow_list}\n\n"
            f"Use `/wos start` to resume the pipeline, then `/decide <uow_id> retry` "
            f"for each UoW, or run `registry_cli decide-retry --id <uow_id>` for each."
        )
        return {
            "action": "send_reply",
            "text": text,
            "chat_id": chat_id,
            "message_type": _msg_type,
        }

    # Partition UoWs into orphan-eligible (auto-retry) and non-orphan (surface to Dan).
    # causes[i] is the return_reason for uow_ids[i] when both lists are present and
    # aligned.  If causes is shorter than uow_ids, treat the excess UoWs as non-orphan
    # (conservative: surface rather than blindly retry without evidence).
    orphan_uow_ids: list[str] = []
    non_orphan_uow_ids: list[str] = []

    if causes and uow_ids:
        for i, uid in enumerate(uow_ids):
            reason = causes[i] if i < len(causes) else "unknown"
            if reason in _SURFACE_ORPHAN_RETURN_REASONS:
                orphan_uow_ids.append(uid)
            else:
                non_orphan_uow_ids.append(uid)
    else:
        # No causes list — fallback path (condition="retry_cap") or malformed message.
        # Surface all to Dan conservatively.
        non_orphan_uow_ids = list(uow_ids)

    # Branch: All causes are orphan return_reasons — auto-retry all via steward heartbeat.
    # The batch is a single infrastructure kill event; no execution budget was consumed.
    if orphan_uow_ids and not non_orphan_uow_ids:
        steward_script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..', 'scheduled-tasks', 'steward-heartbeat.py')
        )
        uow_id_list_str = ", ".join(orphan_uow_ids)
        task_id = f"surface-batch-retry-{len(orphan_uow_ids)}uow"
        prompt = (
            f"---\n"
            f"task_id: {task_id}\n"
            f"chat_id: 0\n"
            f"source: system\n"
            f"---\n\n"
            f"WOS kill-wave batch auto-retry ({condition}): "
            f"{len(orphan_uow_ids)} UoW(s) were killed before or during execution "
            f"(all causes are orphan return_reasons — no execution budget consumed).\n\n"
            f"Affected UoW IDs: {uow_id_list_str}\n\n"
            f"Run the steward heartbeat to re-queue all affected UoWs:\n\n"
            f"```bash\n"
            f"cd ~/lobster-workspace && uv run python {steward_script}\n"
            f"```\n\n"
            f"Then call write_result with the steward output.\n\n"
            f"Minimum viable output: steward heartbeat completed for batch kill wave.\n"
            f"Boundary: do not modify any UoW status directly."
        )
        return {
            "action": "spawn_subagent",
            "task_id": task_id,
            "agent_type": "lobster-generalist",
            "prompt": prompt,
            "message_type": _msg_type,
        }

    # Branch: Mixed causes — surface non-orphans to Dan; list orphans for Dan to retry.
    # Branch: All non-orphan causes — surface all to Dan (non_orphan_uow_ids == uow_ids).
    #
    # Note: this handler cannot spawn a subagent AND send a reply in the same result —
    # the dispatch architecture returns one action per message.  Orphan UoWs in the mixed
    # case are identified and listed for Dan to retry, but no steward heartbeat is spawned
    # here.  Dan can use `/decide <uow_id> retry` for each orphan UoW listed.
    uow_list = "\n".join(f"  - `{uid}`" for uid in non_orphan_uow_ids)
    orphan_note = ""
    if orphan_uow_ids:
        orphan_ids_str = "\n".join(f"  - `{uid}` → `/decide {uid} retry`" for uid in orphan_uow_ids)
        orphan_note = (
            f"\nOrphan UoW(s) eligible for auto-retry (infrastructure kills, "
            f"no execution budget consumed) — use `/decide retry` for each:\n"
            f"{orphan_ids_str}\n"
        )

    text = (
        f"WOS kill wave ({condition}): {len(non_orphan_uow_ids)} UoW(s) require review.\n"
        f"{orphan_note}\n"
        f"UoWs needing your decision:\n{uow_list}\n\n"
        f"For each UoW:\n"
        f"  `/decide <uow_id> retry` — reset and re-queue\n"
        f"  `/decide <uow_id> abandon` — close as failed"
    )
    return {
        "action": "send_reply",
        "text": text,
        "chat_id": chat_id,
        "message_type": _msg_type,
    }


# ---------------------------------------------------------------------------
# wos_diagnose handler — manual-trigger forensics subagent
#
# Called when the dispatcher receives a message with type="wos_diagnose".
# Written by the dispatcher when Dan types "diagnose <uow_id>" in Telegram.
# Spawns a diagnostic subagent that runs registry_cli trace and returns a
# structured forensic report.
#
# Unlike wos_escalate (which has send_reply branches and runs before the
# spawn-gate), wos_diagnose always returns action="spawn_subagent" and runs
# inside the spawn-gate block. The gate enforcement check is always satisfied
# for this handler, making the gate redundant but not harmful.
#
# UoW ID resolution is intentionally isolated through _resolve_uow_id().
# Today that function is a direct pass-through; a future PR (short-ID
# support) will add lookup logic there without changing this handler.
# ---------------------------------------------------------------------------

def _resolve_uow_id(uow_id: str) -> str:
    """
    Resolve a UoW ID from a raw identifier supplied by the user.

    Today this is a direct pass-through: full IDs like ``uow_20260426_abc123``
    are returned unchanged.

    A future PR will add short-ID support here — resolving a serial number or
    semantic slug alias to the canonical UoW ID via a registry lookup — without
    requiring any changes to ``handle_wos_diagnose``.

    Args:
        uow_id: The raw UoW identifier as parsed from the user's command.

    Returns:
        The canonical UoW ID to pass to registry_cli.
    """
    return uow_id


def handle_wos_diagnose(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_diagnose`` inbox message by spawning a diagnostic subagent.

    Called by ``route_wos_message`` when the dispatcher receives a message with
    ``type="wos_diagnose"``.  Written by the dispatcher when Dan types
    ``diagnose <uow_id>`` in Telegram.

    The spawned subagent runs ``registry_cli trace`` against the UoW, applies
    the five-pattern diagnosis algorithm, and returns a structured forensic
    report. If the diagnosis confidence is high and the pattern is a pure
    infrastructure kill, the subagent calls ``registry_cli decide-retry``
    autonomously before reporting; otherwise it surfaces the report to Dan.

    UoW ID resolution goes through ``_resolve_uow_id()``.  Today that is a
    direct pass-through for full IDs; a future PR adds short-ID lookup there.

    Args:
        msg: The raw ``wos_diagnose`` inbox message dict.  Expected fields:

            - ``uow_id`` (str): The Unit of Work identifier.
            - ``escalation_id`` (str, optional): Correlation ID from the
              originating ``wos_escalate`` message; ``""`` for manual triggers.
            - ``escalation_trigger`` (str, optional): ``"manual"`` for
              Telegram-triggered diagnoses; escalation trigger string otherwise.
            - ``failure_history`` (dict, optional): Pre-computed failure context
              from the Steward; ``{}`` for manual triggers.

    Returns:
        A dict with ``action="spawn_subagent"`` and the following fields:

        ``task_id`` (str):
            Task identifier for the diagnostic subagent.

        ``agent_type`` (str):
            Always ``"lobster-generalist"``.

        ``prompt`` (str):
            Subagent prompt implementing the diagnosis algorithm.

        ``message_type`` (str):
            Always ``"wos_diagnose"``.
    """
    raw_uow_id: str = msg.get("uow_id", "unknown")
    uow_id: str = _resolve_uow_id(raw_uow_id)
    escalation_id: str = msg.get("escalation_id", "")
    escalation_trigger: str = msg.get("escalation_trigger", "manual")
    failure_history: dict[str, Any] = msg.get("failure_history", {})

    task_id = f"wos-diagnose-{uow_id[:12]}"
    registry_cli_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "registry_cli.py")
    )
    failure_history_json = json.dumps(failure_history, indent=2)

    prompt = (
        f"---\n"
        f"task_id: {task_id}\n"
        f"chat_id: 0\n"
        f"source: system\n"
        f"---\n\n"
        f"Your task_id is: {task_id}\n\n"
        f"You are a WOS self-diagnosing subagent. Your only job is to diagnose one UoW "
        f"and decide whether to reset it, retire it, or surface it to Dan.\n\n"
        f"## Task\n\n"
        f"UoW ID: {uow_id}\n"
        f"Escalation trigger: {escalation_trigger}\n"
        f"Escalation ID: {escalation_id or '(manual trigger)'}\n\n"
        f"Pre-computed failure history from escalation message:\n"
        f"```json\n{failure_history_json}\n```\n\n"
        f"## Steps\n\n"
        f"1. Run: uv run {registry_cli_path} trace --id {uow_id}\n"
        f"   Read the output. Focus on: diagnosis_hint, return_reasons, "
        f"execution_attempts, kill_classification.\n\n"
        f"2. Apply the diagnosis algorithm:\n\n"
        f"   ORPHAN_REASONS = {{'executor_orphan', 'executing_orphan', 'diagnosing_orphan', "
        f"'orphan_kill_before_start', 'orphan_kill_during_execution'}}\n"
        f"   MAX_RETRIES = {_STEWARD_MAX_RETRIES}\n"
        f"   HARD_CAP = {_HARD_CAP_CYCLES}\n\n"
        f"   - If ALL return_reasons are in ORPHAN_REASONS and execution_attempts == 0:\n"
        f"     posture = reset, pattern = 'infrastructure-kill-wave'\n"
        f"   - If ALL return_reasons are in ORPHAN_REASONS and "
        f"kill_type == 'orphan_kill_before_start':\n"
        f"     posture = reset, pattern = 'kill-before-start'\n"
        f"   - If ALL return_reasons are in ORPHAN_REASONS and "
        f"kill_type == 'orphan_kill_during_execution':\n"
        f"     posture = reset, pattern = 'kill-during-execution'\n"
        f"   - If execution_attempts >= MAX_RETRIES:\n"
        f"     posture = surface-to-human, pattern = 'genuine-retry-cap'\n"
        f"     Run: uv run {registry_cli_path} get --id {uow_id}\n"
        f"     (to get steward_log for Dan's context)\n"
        f"   - If lifetime_cycles >= HARD_CAP:\n"
        f"     posture = surface-to-human, pattern = 'hard-cap'\n"
        f"   - If steward_cycles >= 3 and execution_attempts == 0 and "
        f"no orphan return_reasons:\n"
        f"     posture = surface-to-human, pattern = 'dead-prescription-loop'\n"
        f"   - Otherwise:\n"
        f"     posture = surface-to-human, pattern = 'unrecognised'\n\n"
        f"3. Before any reset: check ~/lobster-workspace/data/wos-config.json.\n"
        f"   If execution_enabled is false, change posture to surface-to-human "
        f"regardless of pattern.\n"
        f"   Rationale: 'execution disabled system-wide, auto-reset deferred.'\n\n"
        f"4. IMPORTANT: `registry_cli decide-retry` only accepts UoWs in 'blocked' or\n"
        f"   'ready-for-steward' status. If the UoW is in 'needs-human-review' status,\n"
        f"   you cannot call decide-retry — surface to Dan with that note included.\n\n"
        f"5. If posture == reset AND status is 'blocked' or 'ready-for-steward':\n"
        f"   Run: uv run {registry_cli_path} decide-retry --id {uow_id}\n"
        f"   Confirm success.\n\n"
        f"6. Call write_result with:\n"
        f"   task_id: {task_id}\n"
        f"   chat_id: 0\n"
        f"   text: structured diagnosis (see format below)\n"
        f"   sent_reply_to_user: False\n\n"
        f"## Output format for write_result text\n\n"
        f"Always write a JSON object:\n"
        f"{{\n"
        f'  "event": "diagnosis_complete",\n'
        f'  "uow_id": "{uow_id}",\n'
        f'  "escalation_id": "{escalation_id}",\n'
        f'  "escalation_trigger": "{escalation_trigger}",\n'
        f'  "pattern_matched": "<pattern>",\n'
        f'  "confidence": "<high|medium|low>",\n'
        f'  "posture": "<reset|surface-to-human>",\n'
        f'  "action_taken": "<registry_cli decide-retry | null>",\n'
        f'  "rationale": "<one sentence>",\n'
        f'  "execution_attempts_at_diagnosis": <int>,\n'
        f'  "lifetime_cycles_at_diagnosis": <int>,\n'
        f'  "surface_message": "<only if posture=surface-to-human: one paragraph for Dan>",\n'
        f'  "timestamp": "<iso8601>"\n'
        f"}}\n\n"
        f"## Constraints\n\n"
        f"- Maximum 3 shell commands total "
        f"(trace + optionally get + optionally decide-retry).\n"
        f"- Do not call decide-retry if execution_enabled is false in wos-config.json.\n"
        f"- Do not call decide-retry if UoW status is 'needs-human-review' — "
        f"surface to Dan instead with a note that status must be 'blocked' first.\n"
        f"- Do not call decide-close. Retirement requires human confirmation always.\n"
        f"- Do not send a Telegram message directly. write_result only.\n"
        f"- Do not loop over multiple UoWs. You handle exactly one: {uow_id}.\n\n"
        f"Minimum viable output: write_result called with the diagnosis JSON.\n"
        f"Boundary: do not open PRs, do not modify code, do not send Telegram messages, "
        f"do not touch steward.py.\n"
    )

    return {
        "action": "spawn_subagent",
        "task_id": task_id,
        "agent_type": "lobster-generalist",
        "prompt": prompt,
        "message_type": "wos_diagnose",
    }


def parse_diagnose_command(text: str) -> str | None:
    """
    Parse a ``diagnose <uow_id>`` Telegram command and return the UoW ID.

    The dispatcher calls this when processing direct user messages.  If the
    message matches ``diagnose <uow_id>`` (case-insensitive, leading/trailing
    whitespace ignored), the UoW ID token is returned.  Otherwise, ``None``
    is returned and the dispatcher continues its normal routing.

    The parsed ``uow_id`` is a raw token — full resolution (including
    short-ID lookup) happens inside ``_resolve_uow_id()`` at dispatch time.

    Args:
        text: The raw Telegram message text.

    Returns:
        The ``uow_id`` token if the command matches; ``None`` otherwise.

    Examples::

        parse_diagnose_command("diagnose uow_20260426_abc123")
        # → "uow_20260426_abc123"

        parse_diagnose_command("DIAGNOSE uow_20260426_abc123")
        # → "uow_20260426_abc123"

        parse_diagnose_command("wos status")
        # → None
    """
    stripped = text.strip()
    lower = stripped.lower()
    if lower.startswith("diagnose "):
        remainder = stripped[len("diagnose "):].strip()
        tokens = remainder.split()
        if tokens:
            return tokens[0]
    return None


def _load_instructions_from_artifact(uow_id: str) -> str:
    """
    Load prescribed instructions from the WorkflowArtifact file for uow_id.

    Called by route_wos_message when the wos_execute inbox message does not
    embed an 'instructions' field (test/manual invocations).
    Raises ValueError with a descriptive message if the artifact is missing
    or malformed — this is caught by the spawn-gate and surfaced as a send_reply
    alert rather than a raw KeyError.
    """
    from .workflow_artifact import artifact_path, from_frontmatter
    path = artifact_path(uow_id)
    if not path.exists():
        raise ValueError(
            f"wos_execute message has no 'instructions' field and artifact file "
            f"not found at {path} for uow_id={uow_id!r}"
        )
    text = path.read_text(encoding="utf-8")
    artifact = from_frontmatter(text)
    return artifact["instructions"]


def handle_wos_pr_sweep_result(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_pr_sweep_result`` inbox message from the PR sweeper cron script.

    Called by route_wos_message when the dispatcher receives a message written by
    wos-pr-sweeper.py.  The sweeper produces these messages when stale open PRs
    (open >7 days) or merged PRs with non-done UoWs are detected.

    This handler is a fast-path: it returns action="send_reply" so the dispatcher
    surfaces the sweep results directly to Dan without spawning a subagent.  It is
    dispatched before the spawn-gate (which only applies to execution message types
    that must always spawn a subagent).

    Pure function — no side effects, no I/O.

    Args:
        msg: The raw wos_pr_sweep_result inbox message dict.  Expected fields:
            - ``text`` (str): Pre-formatted notification text from the sweeper.
            - ``chat_id`` (int): Admin chat ID to deliver the message to.
            - ``data`` (dict, optional): Structured counts (stale_open_count, etc.).

    Returns:
        A dict with action="send_reply" and the notification text.
    """
    text: str = msg.get("text", "WOS PR sweep results (no detail available)")
    chat_id: int = int(msg.get("chat_id", os.environ.get("LOBSTER_ADMIN_CHAT_ID", "0")))
    return {
        "action": "send_reply",
        "text": text,
        "chat_id": chat_id,
        "message_type": "wos_pr_sweep_result",
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
            What the dispatcher must do.  ``"spawn_subagent"`` for both
            ``wos_execute`` and ``steward_trigger`` messages.

        ``task_id`` (str):
            The ``task_id`` to pass to the Task tool (e.g. ``"wos-<uow_id>"``).

        ``prompt`` (str):
            The prompt string to pass to the background subagent Task call.

        ``agent_type`` (str):
            The subagent_type to pass to the Task tool (e.g. ``"functional-engineer"``,
            ``"lobster-generalist"``, ``"lobster-meta"``). Taken from ``msg["agent_type"]``
            if present; defaults to ``"functional-engineer"`` for backward compatibility
            with messages written before issue #842.

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

    # ---------------------------------------------------------------------------
    # wos_escalate fast-path: dispatched before the spawn-gate because this handler
    # legitimately returns either action="spawn_subagent" (auto-retry branches) or
    # action="send_reply" (surface-to-Dan branches).  The spawn-gate applies only to
    # execution message types (wos_execute, steward_trigger, wos_diagnose) that must
    # always spawn.
    # ---------------------------------------------------------------------------
    if msg_type == "wos_escalate":
        try:
            escalate_result = handle_wos_escalate(msg)
            escalate_result["message_type"] = msg_type
            return escalate_result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_wos_escalate raised %s: %s — "
                "returning send_reply alert",
                type(exc).__name__, exc,
            )
            return {
                "action": "send_reply",
                "text": (
                    f"WOS escalation handler raised an error "
                    f"({type(exc).__name__}: {exc}). "
                    f"UoW escalation was NOT processed. "
                    "Check logs and re-queue manually if needed."
                ),
                "message_type": msg_type,
            }

    # ---------------------------------------------------------------------------
    # wos_surface fast-path: dispatched before the spawn-gate for the same reason as
    # wos_escalate — this handler legitimately returns either action="spawn_subagent"
    # (all-orphan auto-retry) or action="send_reply" (surface-to-Dan branches).
    # ---------------------------------------------------------------------------
    if msg_type == "wos_surface":
        try:
            surface_result = handle_wos_surface(msg)
            surface_result["message_type"] = msg_type
            return surface_result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_wos_surface raised %s: %s — "
                "returning send_reply alert",
                type(exc).__name__, exc,
            )
            return {
                "action": "send_reply",
                "text": (
                    f"WOS surface handler raised an error "
                    f"({type(exc).__name__}: {exc}). "
                    f"Kill wave was NOT processed. "
                    "Check logs and re-queue manually if needed."
                ),
                "message_type": msg_type,
            }

    # ---------------------------------------------------------------------------
    # wos_pr_sweep_result fast-path: dispatched before the spawn-gate.  The PR sweeper
    # cron script writes these messages when stale open PRs or merged PRs with non-done
    # UoWs are found.  This handler always returns action="send_reply" — no subagent
    # spawn is needed, just surface the pre-formatted text to Dan.
    # ---------------------------------------------------------------------------
    if msg_type == "wos_pr_sweep_result":
        try:
            sweep_result = handle_wos_pr_sweep_result(msg)
            sweep_result["message_type"] = msg_type
            return sweep_result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_wos_pr_sweep_result raised %s: %s — "
                "returning send_reply alert",
                type(exc).__name__, exc,
            )
            return {
                "action": "send_reply",
                "text": (
                    f"WOS PR sweep handler raised an error "
                    f"({type(exc).__name__}: {exc}). "
                    "PR sweep results were NOT delivered. Check logs."
                ),
                "message_type": msg_type,
            }

    # ---------------------------------------------------------------------------
    # Spawn-gate (issue #920): all WOS message types MUST produce action="spawn_subagent".
    # If a handler returns any other action or raises, return action="send_reply" to
    # alert the user rather than silently calling mark_processed without spawning a Task.
    # Returning action="mark_processed" here is the root cause of executor orphan incidents.
    # ---------------------------------------------------------------------------
    try:
        if msg_type == "wos_execute":
            uow_id: str = msg["uow_id"]
            instructions: str = msg.get("instructions") or _load_instructions_from_artifact(uow_id)
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
            # agent_type identifies which subagent_type to spawn (issue #842).
            # Executor embeds this in the message based on the UoW register.
            # Default: functional-engineer for backward compatibility with messages
            # written before this field was added.
            agent_type: str = msg.get("agent_type", "functional-engineer")
            prompt = handle_wos_execute(uow_id, instructions, output_ref)
            result: dict[str, Any] = {
                "action": "spawn_subagent",
                "task_id": f"wos-{uow_id}",
                "prompt": prompt,
                "agent_type": agent_type,
                "message_type": msg_type,
            }

        elif msg_type == "steward_trigger":
            trigger_uow_id: str = msg.get("uow_id", "unknown")
            result = handle_steward_trigger(trigger_uow_id)
            result["message_type"] = msg_type

        elif msg_type == "wos_diagnose":
            result = handle_wos_diagnose(msg)
            result["message_type"] = msg_type

        else:
            # Unreachable given the guard above, but satisfies exhaustiveness checkers
            raise ValueError(f"route_wos_message: no branch for type {msg_type!r}")

    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "route_wos_message: handler for type %r raised %s: %s — "
            "returning send_reply alert to prevent mark_processed without spawn",
            msg_type, type(exc).__name__, exc,
        )
        return {
            "action": "send_reply",
            "text": (
                f"WOS spawn-gate alert: handler for message type {msg_type!r} raised an error "
                f"({type(exc).__name__}: {exc}). The UoW was NOT dispatched. "
                "Check the executor logs and re-queue manually if needed."
            ),
            "message_type": msg_type,
        }

    # Spawn-gate enforcement: the result must carry action="spawn_subagent".
    # Any other value (e.g. "noop", "mark_processed") is a gate violation —
    # return a send_reply alert instead so Dan can investigate.
    if result.get("action") != "spawn_subagent":
        import logging as _logging
        _logging.getLogger(__name__).error(
            "route_wos_message: handler for type %r returned unexpected action %r — "
            "expected 'spawn_subagent'. Returning send_reply alert.",
            msg_type, result.get("action"),
        )
        return {
            "action": "send_reply",
            "text": (
                f"WOS spawn-gate alert: handler for message type {msg_type!r} returned "
                f"action={result.get('action')!r} instead of 'spawn_subagent'. "
                "The UoW was NOT dispatched. Check the handler and re-queue if needed."
            ),
            "message_type": msg_type,
        }

    return result


# ---------------------------------------------------------------------------
# Compaction-resilient callback dispatch
#
# The dispatcher calls route_callback_message(msg) for type="callback" messages
# instead of relying on prose instructions that may not survive context compaction.
#
# Dispatcher integration (add to main loop):
#
#     from src.orchestration.dispatcher_handlers import route_callback_message
#
#     if msg.get("type") == "callback":
#         result = route_callback_message(msg)
#         # result["action"] tells the dispatcher what to do:
#         #   "send_reply" → send result["text"] to result["chat_id"]
#         # mark_processed(message_id) after sending the reply
# ---------------------------------------------------------------------------

#: Callback data prefixes that this router handles.
CALLBACK_DATA_HANDLERS: frozenset[str] = frozenset({
    "decide_retry:",
    "decide_close:",
})


def route_callback_message(msg: dict[str, Any], *, registry: "Registry | None" = None) -> dict[str, Any]:
    """
    Route a ``type: "callback"`` inbox message from an inline keyboard button press.

    This is the compaction-resilient entry point for callback routing.  The
    dispatcher should import and call this function rather than relying on prose
    boot instructions that can be lost under context compaction.

    Currently handles:
    - ``decide_retry:<uow_id>`` — retry a blocked UoW (calls ``handle_decide_retry``)
    - ``decide_close:<uow_id>`` — close a blocked UoW (calls ``handle_decide_close``)

    All other callback_data values fall through to an "unknown callback" reply,
    which lets the dispatcher handle other callback types (job-confirm, delete-confirm,
    etc.) via its existing prose routing logic.

    Args:
        msg: The raw inbox message dict.  Must contain ``callback_data`` and
             ``chat_id``.
        registry: Optional Registry instance.  If omitted, a default Registry()
             is constructed (uses the production DB path).  Pass an explicit
             instance in tests to use a temp DB.

    Returns:
        A dict with:

        ``action`` (str):
            Always ``"send_reply"`` — the dispatcher must call send_reply.

        ``text`` (str):
            The reply text to send to the user.

        ``chat_id`` (int | str):
            The chat to reply to (echoed from ``msg["chat_id"]``).

        ``handled`` (bool):
            ``True`` if callback_data matched a known WOS pattern;
            ``False`` if the dispatcher should fall through to its own
            handling (e.g. job-confirm-yes, delete-confirm-yes).

    Example dispatcher integration::

        from src.orchestration.dispatcher_handlers import route_callback_message

        if msg.get("type") == "callback":
            result = route_callback_message(msg)
            if result["handled"]:
                send_reply(chat_id=result["chat_id"], text=result["text"],
                           message_id=message_id)
            else:
                # fall through to prose-based job-confirm / delete-confirm logic
                ...
    """
    from .registry import Registry  # local import to keep module importable without DB

    data: str = msg.get("callback_data", "")
    chat_id = msg.get("chat_id")

    if data.startswith("decide_retry:"):
        uow_id = data[len("decide_retry:"):]
        reg = registry if registry is not None else Registry()
        text = handle_decide_retry(uow_id, registry=reg)
        return {"action": "send_reply", "text": text, "chat_id": chat_id, "handled": True}

    if data.startswith("decide_close:"):
        uow_id = data[len("decide_close:"):]
        reg = registry if registry is not None else Registry()
        text = handle_decide_close(uow_id, registry=reg)
        return {"action": "send_reply", "text": text, "chat_id": chat_id, "handled": True}

    # Not a WOS callback — signal the dispatcher to use its own handling
    return {"action": "send_reply", "text": f"Unknown callback: {data}", "chat_id": chat_id, "handled": False}
