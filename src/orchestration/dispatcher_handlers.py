"""
Dispatcher command handlers for WOS.

These are pure functions: they take a UoW id (or status string) and a Registry
instance, and return a formatted string response suitable for sending back to
Telegram. No MCP tools, no network calls — those belong in the dispatcher.

The dispatcher calls these handlers when it recognizes:
  /approve <uow-id>                    → handle_approve(uow_id, registry)
  /decide <uow-id> <proceed|abandon|retry|owner <decision>> → handle_decide(uow_id, action, registry)
  /wos status [status]                 → handle_wos_status(status, registry)
  /wos uow <uow-id>                    → handle_wos_uow(uow_id, registry) → dispatcher spawns subagent
  /wos unblock                         → handle_wos_unblock()
  /wos start                           → handle_wos_start()
  /wos stop                            → handle_wos_stop()
  /wos dashboard (or "wos dashboard")  → handle_wos_dashboard()
  wos abort <uow-id>                   → handle_wos_abort(uow_id, registry)
  decide retry <uow-id>                → handle_decide_retry(uow_id, registry)
  decide close <uow-id>                → handle_decide_close(uow_id, registry)
  type: "wos_execute"                  → handle_wos_execute(uow_id, instructions, output_ref)
  type: "wos_owner_required"           → handle_wos_owner_required(msg)
  type: "callback" (decide_retry/close)→ route_callback_message(msg)

## Compaction-resilient dispatch

WOS_MESSAGE_TYPE_DISPATCH maps inbox message types to handler descriptors.
The dispatcher calls route_wos_message(msg) to dispatch type-routed messages
instead of relying on prose instructions that can be lost under context compaction.
Import and call this table unconditionally — Python imports survive compaction.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .registry import Registry

from .registry import ApproveConfirmed, ApproveExpired, ApproveNotFound, ApproveSkipped
from .paths import LOBSTER_WORKSPACE as _LOBSTER_WORKSPACE, WOS_CONFIG as _WOS_CONFIG_PATH_FROM_PATHS, WOS_GATE_CLEARED_FLAG as _GATE_CLEARED_FLAG, JOBS_JSON as _JOBS_JSON_PATH
from .steward import ReturnReasonClassification, MAX_RETRIES as _STEWARD_MAX_RETRIES, _HARD_CAP_CYCLES
from .wos_issue_lifecycle import (
    HUMAN_GATE_LABELS as _HUMAN_GATE_LABELS,
    bulk_swap_executing_to_paused as _bulk_swap_executing_to_paused,
    bulk_swap_paused_to_executing as _bulk_swap_paused_to_executing,
)
from src.utils.timezone import format_iso_for_user as _format_iso_for_user, get_owner_tz_name as _get_owner_tz_name


# ---------------------------------------------------------------------------
# Control event type constants
# ---------------------------------------------------------------------------

class ControlEventType(StrEnum):
    """Named constants for dispatcher control event types written to control_events."""

    WOS_START = "wos_start"
    WOS_STOP = "wos_stop"
    WOS_ABORT = "wos_abort"


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
    # annotations on concurrent candidates.
    # Raised from 2 to 5 (2026-06-03): original value was conservative
    # with no Attunement evidence at higher scale; matches
    # MAX_CONCURRENT_PRESCRIPTIONS=5 (PR #1391). Additional throttles
    # (ScalingGovernor, CC quota gate, context pressure threshold) remain.
    "max_parallel": 5,
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


# Canonical pause_reason value written to wos-config.json when the operator
# explicitly runs `wos stop`. Steward-heartbeat imports this constant to
# identify intentional pauses and suppress false-positive starvation alerts.
_PAUSE_REASON_USER_COMMAND: str = "user_command"


# ---------------------------------------------------------------------------
# WOS-core job list — canonical gate list for wos start / wos stop
# ---------------------------------------------------------------------------
#
# These are the jobs that constitute the WOS pipeline. All 14 are toggled
# atomically when the operator runs `wos start` or `wos stop`. The source of
# truth for which jobs are WOS-core is the `wos_core: true` field in
# jobs.json (instance config). This constant mirrors that list so that the
# toggle function can be tested independently of a live jobs.json, and so
# that missing entries are detectable at toggle time.
#
# wos-health-monitor is listed here but may not be present in jobs.json on
# all instances (it was previously managed via systemd only). The toggle
# function skips entries not found in jobs.json and surfaces them in its
# return value.
#
_WOS_CORE_JOBS: frozenset[str] = frozenset({
    "executor-heartbeat",
    "steward-heartbeat",
    "issue-sweeper",
    "uow-reflection",
    "pattern-candidate-sweep",
    "github-issue-cultivator",
    "proposals-authorship",
    "wos-overnight-loop",
    "wos-hourly-observation",
    "wos-queue-monitor",
    "wos-health-check",
    "wos-metabolic-digest",
    "wos-pr-sweeper",
    "wos-health-monitor",
})

# ---------------------------------------------------------------------------
# Systemd timer management for WOS-core jobs
# ---------------------------------------------------------------------------
#
# A subset of WOS-core jobs are dispatched via systemd timers (not cron).
# These timers call dispatch-job.sh, which reads the jobs.json `enabled` flag
# as a second gate. When the timer is disabled, the job never fires regardless
# of the jobs.json state — so wos start/stop must manage both layers.
#
# Jobs in this set have unit files at /etc/systemd/system/lobster-{job}.timer
# and are managed only when the file exists AND contains the LOBSTER-MANAGED
# comment (safety guard against touching unrelated timers).
#
_SYSTEMD_UNIT_DIR: Path = Path("/etc/systemd/system")

_WOS_CORE_TIMER_JOBS: frozenset[str] = frozenset({
    "issue-sweeper",
    "github-issue-cultivator",
    "pattern-candidate-sweep",
    "uow-reflection",
    "wos-overnight-loop",
    "wos-hourly-observation",
    "wos-health-monitor",
})

_LOBSTER_MANAGED_MARKER = "# LOBSTER-MANAGED"


def _toggle_systemd_timers(enabled: bool) -> list[str]:
    """Enable or disable systemd timers for WOS-core jobs that use them.

    For each job in ``_WOS_CORE_TIMER_JOBS``:
    - Checks whether ``/etc/systemd/system/lobster-{job}.timer`` exists.
    - Reads the unit file and verifies it contains ``# LOBSTER-MANAGED`` (safety
      guard: we never touch timers we did not install).
    - Calls ``sudo systemctl enable --now`` or ``sudo systemctl disable --now``
      depending on ``enabled``.
    - Failures (non-zero exit, subprocess error, permission error) are logged
      and skipped — this function never raises.

    Returns a list of timer names that were successfully toggled.
    """
    action = "enable" if enabled else "disable"
    toggled: list[str] = []

    for job_name in sorted(_WOS_CORE_TIMER_JOBS):
        timer_name = f"lobster-{job_name}.timer"
        unit_path = _SYSTEMD_UNIT_DIR / timer_name

        if not unit_path.exists():
            continue

        try:
            unit_text = unit_path.read_text()
        except OSError:
            _log.debug("_toggle_systemd_timers: cannot read %s — skipping", unit_path)
            continue

        if _LOBSTER_MANAGED_MARKER not in unit_text:
            _log.debug(
                "_toggle_systemd_timers: %s lacks LOBSTER-MANAGED marker — skipping",
                timer_name,
            )
            continue

        try:
            proc = subprocess.run(
                ["sudo", "systemctl", f"{action}", "--now", timer_name],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 0:
                toggled.append(timer_name)
                _log.info("_toggle_systemd_timers: %s %sd successfully", timer_name, action)
            else:
                _log.warning(
                    "_toggle_systemd_timers: systemctl %s %s returned %d: %s",
                    action,
                    timer_name,
                    proc.returncode,
                    proc.stderr.decode(errors="replace").strip(),
                )
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning(
                "_toggle_systemd_timers: could not %s %s: %s",
                action,
                timer_name,
                exc,
            )

    return toggled


def _read_jobs_json() -> dict:
    """Read jobs.json and return its contents. Returns empty jobs dict on error."""
    try:
        with _JOBS_JSON_PATH.open() as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"jobs": {}}


def _write_jobs_json(data: dict) -> None:
    """Write jobs.json atomically (write-then-rename)."""
    tmp = _JOBS_JSON_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2)
    tmp.rename(_JOBS_JSON_PATH)


def get_disabled_wos_core_jobs() -> list[str]:
    """Return a sorted list of wos_core job names that are currently disabled.

    Reads jobs.json and returns the names of all jobs where both:
    - ``wos_core`` is truthy
    - ``enabled`` is falsy (False, missing, or None)

    Used by handle_wos_start to detect partially-enabled state: when
    execution_enabled=True in wos-config.json but one or more wos_core jobs
    are still disabled (e.g. due to a manual edit that bypassed toggle_wos_core_jobs).

    Returns an empty list when all wos_core jobs are enabled or when jobs.json
    is absent/unreadable.
    """
    jobs = _read_jobs_json().get("jobs", {})
    return sorted(
        name
        for name, entry in jobs.items()
        if entry.get("wos_core") and not entry.get("enabled", False)
    )


def toggle_wos_core_jobs(enabled: bool, pause_reason: str | None = None) -> dict:
    """Enable or disable all WOS-core jobs atomically.

    Reads jobs.json, sets the `enabled` field on every job where
    `wos_core == True`, and writes both jobs.json and wos-config.json
    atomically. The `execution_enabled` flag in wos-config.json is also
    updated to match.

    Jobs listed in _WOS_CORE_JOBS but absent from jobs.json are silently
    skipped and reported in the `not_found` list of the return value.

    Args:
        enabled: True to enable WOS execution; False to disable.
        pause_reason: Optional reason string written to wos-config.json when
            disabling (``enabled=False``). Pass ``"user_command"`` when the
            pause was explicitly requested by the user (via ``/wos stop``).
            When ``enabled=True``, ``pause_reason`` is removed from the config
            regardless of this argument. When ``enabled=False`` and
            ``pause_reason`` is None, the field is left unchanged if already
            present (preserves any prior reason written by an earlier call).

    Returns a summary dict:
        {
            "toggled": [list of job names that were updated],
            "not_found": [list of job names absent from jobs.json],
            "new_state": "enabled" | "disabled",
        }

    This function is pure with respect to side effects: all I/O is
    isolated to two atomic file writes at the end, after all mutations
    are computed in memory.
    """
    jobs_data = _read_jobs_json()
    jobs = jobs_data.get("jobs", {})

    toggled: list[str] = []
    not_found: list[str] = []

    # Compute the updated jobs dict without mutating the original.
    updated_jobs = {
        name: ({**entry, "enabled": enabled} if entry.get("wos_core") else entry)
        for name, entry in jobs.items()
    }

    # Identify which WOS-core jobs were toggled vs. not found.
    for job_name in sorted(_WOS_CORE_JOBS):
        if job_name in jobs and jobs[job_name].get("wos_core"):
            toggled.append(job_name)
        else:
            not_found.append(job_name)

    # Write both files atomically. If either write fails, raise — the caller
    # (handle_wos_start / handle_wos_stop) catches OSError and reports it.
    _write_jobs_json({**jobs_data, "jobs": updated_jobs})
    config = read_wos_config()
    new_config = {**config, "execution_enabled": enabled}
    if enabled:
        # Starting: clear pause_reason so monitoring knows execution is intentionally on.
        new_config.pop("pause_reason", None)
    elif pause_reason is not None:
        # Stopping with an explicit reason: record it so monitors can suppress
        # starvation alerts when the pause is intentional (user_command).
        new_config["pause_reason"] = pause_reason
    # else: stopping without a reason — leave any existing pause_reason unchanged.
    _write_wos_config(new_config)

    return {
        "toggled": toggled,
        "not_found": not_found,
        "new_state": "enabled" if enabled else "disabled",
    }


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
    or sends a message matching "decide close <uow-id>" / "wos abort <uow-id>".

    Transitions blocked → failed with reason=user_closed. Writes a 'wos_abort'
    control event to the registry log when the close succeeds.
    """
    rows = registry.decide_close(uow_id)
    if rows == 1:
        registry.log_control_event(ControlEventType.WOS_ABORT, {"uow_id": uow_id, "result": "closed"})
        return (
            f"UoW `{uow_id}` closed.\n"
            f"Status: `blocked \u2192 failed` (reason: user_closed)"
        )
    return (
        f"UoW `{uow_id}` could not be closed \u2014 it is not currently in `blocked` status.\n"
        f"Run `/wos status blocked` to see blocked UoWs."
    )


_VALID_DECIDE_ACTIONS = frozenset({"proceed", "abandon", "retry", "defer", "owner"})


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


def handle_owner_decide(uow_id: str, decision_note: str, *, registry: "Registry") -> str:
    """
    Handle `/decide <uow-id> owner <decision>` — provide an owner decision to a paused UoW.

    Called when Dan provides a decision for a UoW that was paused in
    `awaiting-owner` status (i.e., a subagent wrote outcome=owner_decision_required).

    The decision text is recorded in the UoW's steward_log and the UoW is
    transitioned back to ready-for-steward so the Steward can read the decision
    and prescribe the next execution step accordingly.

    Returns a human-readable Telegram message describing the outcome.
    """
    if not decision_note.strip():
        return (
            f"Decision text is required for owner action.\n"
            f"Usage: `/decide {uow_id} owner <your decision text>`"
        )
    rows = registry.owner_decide(uow_id, decision_note.strip())
    if rows == 1:
        return (
            f"UoW `{uow_id}` re-queued with owner decision.\n"
            f"Status: `awaiting-owner → ready-for-steward`\n"
            f"Decision recorded: {decision_note.strip()}"
        )
    return (
        f"UoW `{uow_id}` could not be re-queued — it is not currently in `awaiting-owner` status.\n"
        f"Run `/wos status awaiting-owner` to see UoWs awaiting a decision."
    )


def handle_decide(uow_id: str, action: str, *, registry: "Registry") -> str:
    """
    Handle /decide <uow-id> <proceed|abandon|retry[force]|defer[note]|owner <decision>>.

    Provides a single unified command for resolving blocked UoWs from Telegram.
    Action semantics:
      proceed              — unblock and re-queue to ready-for-steward (preserves steward_cycles)
      retry                — reset steward_cycles to 0 and re-queue to ready-for-steward (full retry)
      retry force          — override the hard-cap commitment gate (explicit operator intent required)
      abandon          — close the UoW as user-requested failure (blocked → failed)
      defer [note]     — leave in blocked, write a dated audit entry with optional note
      owner <decision> — record owner decision and re-queue awaiting-owner → ready-for-steward

    Most actions operate only on UoWs in `blocked` status. The `owner` action
    operates only on UoWs in `awaiting-owner` status. Optimistic lock prevents
    accidental double-writes if the UoW has already been advanced.

    Returns a human-readable Telegram message describing the outcome.
    """
    # Support "retry force" as a two-word action token.
    # Support "defer <note>" where any trailing text after "defer" is the note.
    # Support "owner <decision>" where trailing text is the owner decision note.
    action_normalized = action.lower().strip()
    force_retry = False
    defer_note = ""
    owner_decision = ""

    if action_normalized in ("retry force", "force retry"):
        action_normalized = "retry"
        force_retry = True
    elif action_normalized.startswith("defer "):
        # "defer waiting on external review" → action=defer, note="waiting on external review"
        defer_note = action.strip()[len("defer "):].strip()
        action_normalized = "defer"
    elif action_normalized.startswith("owner "):
        # "owner proceed with option A" → action=owner, decision="proceed with option A"
        owner_decision = action.strip()[len("owner "):].strip()
        action_normalized = "owner"

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
        case "owner":
            return handle_owner_decide(uow_id, owner_decision, registry=registry)
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


def handle_wos_dashboard() -> str:
    """
    Generate a fresh WOS HTML dashboard from live registry data and upload to Bisque.

    Calls wos_dashboard_gen.generate_and_upload() which:
      1. Queries the registry DB for all UoW data (status counts, audit trail, etc.)
      2. Reads the token ledger for Claude Code usage stats
      3. Renders a self-contained HTML file with embedded JSON
      4. Writes it to ~/messages/bisque-uploads/ with a UUID filename
      5. Returns the public Bisque URL

    Returns a formatted reply string with the URL.
    """
    from src.orchestration.wos_dashboard_gen import generate_and_upload

    url = generate_and_upload()
    return f"WOS Dashboard ready: {url}"


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
        f"uow_id: {uow_id}\n"
        f"chat_id: 0\n"
        f"source: system\n"
        f"---\n\n"
        f"You are executing a Work Order System (WOS) unit of work on behalf of the Steward.\n"
        f"UoW ID: {uow_id}\n\n"
        f"## Heartbeat contract (REQUIRED — structural, not advisory)\n\n"
        f"FIRST ACTION BEFORE ANY OTHER WORK: write a startup heartbeat immediately.\n"
        f"This proves the agent started. A UoW with no agent-originated heartbeat within\n"
        f"5 minutes of dispatch is classified as orphan_kill_before_start by the\n"
        f"Observation Loop, regardless of the sidecar heartbeat_at value.\n\n"
        f"You must write a heartbeat at most every 90 seconds throughout execution.\n"
        f"90 seconds is a hard maximum — not a suggestion. If no natural checkpoint\n"
        f"falls within 90 seconds, write a heartbeat unconditionally.\n\n"
        f"Required checkpoints (write at each, in order):\n"
        f"  1. startup   — IMMEDIATELY after receiving this prompt, before any other work\n"
        f"  2. post-read — after reading/understanding the issue or task\n"
        f"  3. post-impl — after completing the implementation\n"
        f"  4. pre-result — immediately before writing the result file\n\n"
        f"Preferred: use the MCP tool (no Python imports needed):\n"
        f"  mcp__lobster-inbox__write_wos_heartbeat(uow_id='{uow_id}', token_usage=<cumulative_tokens>)\n\n"
        f"token_usage is REQUIRED (not optional). Pass your running cumulative total of\n"
        f"input_tokens + output_tokens from all Claude API responses received so far.\n"
        f"Track this across all API calls in your session and pass the updated total at each\n"
        f"heartbeat. The Observation Loop uses token_usage to distinguish agent-originated\n"
        f"heartbeats (token_usage IS NOT NULL) from sidecar-only writes (token_usage IS NULL).\n"
        f"A heartbeat without token_usage does not prove the agent is alive.\n\n"
        f"Fallback: call the registry directly via Bash:\n"
        f"  import sys; sys.path.insert(0, '/home/lobster/lobster')\n"
        f"  from src.orchestration.registry import WOSRegistry\n"
        f"  WOSRegistry().write_heartbeat('{uow_id}', token_usage=<cumulative_tokens>)\n\n"
        f"The heartbeat call returns rowcount: 1 on success, 0 if the UoW status has changed.\n"
        f"A return value of 0 (or {{\"rowcount\": 0}} from the MCP tool) means the Steward\n"
        f"has already re-queued this UoW — stop execution immediately and call write_result\n"
        f"with outcome=failed.\n\n"
        f"## PR-close guard (REQUIRED)\n\n"
        f"Before closing any PR via `gh pr close` or any equivalent operation:\n\n"
        f"1. Fetch the PR's labels:\n"
        f"   ```bash\n"
        f"   gh pr view <PR_NUMBER> --repo <REPO> --json labels --jq '[.labels[].name]'\n"
        f"   ```\n\n"
        f"2. If the output contains any of these labels — "
        f"{', '.join(f'**`{lbl}`**' for lbl in sorted(_HUMAN_GATE_LABELS))} — "
        f"**do not close the PR**. Instead:\n"
        f"   - Skip the close operation entirely.\n"
        f"   - Include in your result file's `reason` field: "
        f"\"PR #<N> skipped: carries human-gate label <LABEL> — human review required\"\n"
        f"   - Set outcome to `blocked` only if this is the sole blocker; otherwise "
        f"continue with remaining instructions and report the skipped PR in the reason.\n\n"
        f"3. If the `gh pr view` call fails for any reason, treat the PR as gated and skip.\n\n"
        f"This guard applies to every PR close operation in the instructions below, "
        f"regardless of how the instruction is phrased.\n\n"
        f"## PR-state live-check guard (REQUIRED)\n\n"
        f"When your instructions require you to determine whether a PR is merged,\n"
        f"unblocked, or in any specific state, you MUST verify by calling:\n\n"
        f"```bash\n"
        f"gh pr view <PR_NUMBER> --repo <OWNER/REPO> --json state,mergedAt\n"
        f"```\n\n"
        f"Then check that `state == \"MERGED\"` in the JSON output.\n\n"
        f"You MUST NOT infer PR state from:\n"
        f"  - Prior relay messages or Telegram notification text\n"
        f"  - Registry state, UoW records, or memory\n"
        f"  - Oracle verdict files or any cached state\n\n"
        f"If `gh pr view` fails or returns non-MERGED state, treat the PR as NOT merged.\n"
        f"This applies to every PR state check in your instructions, regardless of phrasing.\n\n"
        f"## Instructions\n\n"
        f"{instructions}\n\n"
        f"## Result contract (REQUIRED)\n\n"
        f"After completing the instructions (or on any error that prevents completion),\n"
        f"write the result file to: {output_ref}\n\n"
        f"The file must be valid JSON matching one of these shapes:\n"
        f'  {{"uow_id": "{uow_id}", "outcome": "complete", "success": true}}\n'
        f'  {{"uow_id": "{uow_id}", "outcome": "failed", "success": false, "reason": "<why>"}}\n'
        f'  {{"uow_id": "{uow_id}", "outcome": "partial", "success": false, "reason": "<what was done and what was not>"}}\n'
        f'  {{"uow_id": "{uow_id}", "outcome": "blocked", "success": false, "reason": "<what is blocking and why>"}}\n'
        f'  {{"uow_id": "{uow_id}", "outcome": "owner_decision_required", "success": false, "reason": "<what decision is needed and why only the owner can resolve it>"}}\n\n'
        f"Outcome values: \"complete\" | \"partial\" | \"failed\" | \"blocked\" | \"owner_decision_required\"\n"
        f"\"success\" must be true if and only if outcome == \"complete\".\n\n"
        f"Use \"owner_decision_required\" only when you have reached a genuine decision point\n"
        f"that only the owner (Dan) can resolve and you cannot proceed without the answer.\n"
        f"Do not use it for transient errors or blockers that a retry might resolve — use\n"
        f"\"blocked\" or \"failed\" for those instead.\n\n"
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


def handle_wos_start(*, registry: "Registry | None" = None) -> str:
    """
    Handle /wos start (or "wos start").

    Enables all WOS-core jobs atomically: sets `enabled: true` on every job
    tagged `wos_core: true` in jobs.json, and sets `execution_enabled: true`
    in wos-config.json so that executor-heartbeat dispatches UoWs on its next
    cycle (within ~90 seconds).

    Partial-recovery path: if execution_enabled is already True but some wos_core
    jobs are disabled (e.g. due to a manual jobs.json edit that bypassed the toggle),
    re-enables those jobs and reports the recovered names rather than silently
    declaring "already running". This prevents a class of stall where a partial
    enable leaves the steward (or other core jobs) disabled while the system
    believes the pipeline is running.

    After enabling jobs, bulk-swaps wos:paused → wos:executing on all GitHub
    issues whose UoWs are currently in executing status, restoring label state
    to accurately reflect active execution.

    Idempotent: calling /wos start when already fully started (all wos_core jobs
    enabled) returns a notice without calling toggle_wos_core_jobs.

    Args:
        registry: Optional Registry instance. When omitted (the default), a new
            Registry() is created for the control event write. Callers may pass
            an existing instance to avoid opening a second connection (useful in
            tests that need to inspect the written rows).

    Returns a human-readable Telegram message describing the outcome.
    """
    from .registry import Registry as _Registry  # local import — keeps module importable without DB

    config = read_wos_config()
    if config.get("execution_enabled"):
        # Detect partial-enable: execution is on but some wos_core jobs are still disabled.
        disabled_core_jobs = get_disabled_wos_core_jobs()
        if not disabled_core_jobs:
            # Fully started — nothing to do.
            return (
                "WOS pipeline is already running.\n"
                "executor-heartbeat is dispatching UoWs normally."
            )

        # Partial recovery: re-enable the disabled wos_core jobs via toggle.
        try:
            result = toggle_wos_core_jobs(enabled=True)
        except OSError as exc:
            return (
                f"Failed to re-enable disabled WOS-core jobs: {exc}\n"
                f"Config: `{_WOS_CONFIG_PATH}`"
            )

        timer_toggled = _toggle_systemd_timers(True)

        lines = [
            f"WOS partial recovery: {len(disabled_core_jobs)} wos_core job(s) were "
            f"disabled while execution_enabled=true — re-enabled: "
            f"{', '.join(disabled_core_jobs)}.",
            "executor-heartbeat will dispatch ready-for-executor UoWs on its next "
            "cycle (within ~90 seconds).",
        ]
        if timer_toggled:
            lines.append(
                f"Systemd timers re-enabled ({len(timer_toggled)}): "
                f"{', '.join(sorted(timer_toggled))}"
            )
        if result["not_found"]:
            lines.append(
                f"Note: {len(result['not_found'])} WOS-core job(s) not in jobs.json "
                f"(may be systemd-only): {', '.join(sorted(result['not_found']))}"
            )

        # Restore wos:paused → wos:executing on all currently-executing issues.
        reg = registry if registry is not None else _Registry()
        executing_uows = reg.list(status="executing")
        issue_numbers = [
            uow.source_issue_number
            for uow in executing_uows
            if uow.source_issue_number is not None
        ]
        wos_repo = os.environ.get("LOBSTER_WOS_REPO", "dcetlin/Lobster")
        label_success, label_failure = _bulk_swap_paused_to_executing(issue_numbers, repo=wos_repo)
        lines.append(
            f"Labels restored: {label_success} wos:paused → wos:executing "
            f"(failures: {label_failure})"
        )

        reg.log_control_event(
            ControlEventType.WOS_START,
            {
                "partial_recovery": True,
                "recovered": sorted(disabled_core_jobs),
                "toggled": sorted(result["toggled"]),
                "not_found": sorted(result["not_found"]),
                "timers_toggled": sorted(timer_toggled),
                "label_swap": {"success": label_success, "failed": label_failure},
            },
        )

        return "\n".join(lines)

    try:
        result = toggle_wos_core_jobs(enabled=True)
    except OSError as exc:
        return (
            f"Failed to enable WOS-core jobs: {exc}\n"
            f"Config: `{_WOS_CONFIG_PATH}`"
        )

    timer_toggled = _toggle_systemd_timers(True)

    toggled_count = len(result["toggled"])
    lines = [
        f"WOS pipeline started. {toggled_count} WOS-core jobs enabled.",
        "executor-heartbeat will dispatch ready-for-executor UoWs on its next "
        "cycle (within ~90 seconds).",
    ]
    if timer_toggled:
        lines.append(
            f"Systemd timers re-enabled ({len(timer_toggled)}): "
            f"{', '.join(sorted(timer_toggled))}"
        )
    if result["not_found"]:
        lines.append(
            f"Note: {len(result['not_found'])} WOS-core job(s) not in jobs.json "
            f"(may be systemd-only): {', '.join(sorted(result['not_found']))}"
        )

    # Restore wos:paused → wos:executing on all currently-executing issues.
    reg = registry if registry is not None else _Registry()
    executing_uows = reg.list(status="executing")
    issue_numbers = [
        uow.source_issue_number
        for uow in executing_uows
        if uow.source_issue_number is not None
    ]
    wos_repo = os.environ.get("LOBSTER_WOS_REPO", "dcetlin/Lobster")
    label_success, label_failure = _bulk_swap_paused_to_executing(issue_numbers, repo=wos_repo)
    lines.append(
        f"Labels restored: {label_success} wos:paused → wos:executing "
        f"(failures: {label_failure})"
    )

    reg.log_control_event(
        ControlEventType.WOS_START,
        {
            "toggled": sorted(result["toggled"]),
            "not_found": sorted(result["not_found"]),
            "timers_toggled": sorted(timer_toggled),
            "label_swap": {"success": label_success, "failed": label_failure},
        },
    )

    return "\n".join(lines)


def handle_wos_stop(*, registry: "Registry | None" = None) -> str:
    """
    Handle /wos stop (or "wos stop").

    Pauses all WOS-core jobs atomically: sets `enabled: false` on every job
    tagged `wos_core: true` in jobs.json, and sets `execution_enabled: false`
    in wos-config.json so that executor-heartbeat skips dispatch on its next
    cycle. UoWs already active are not affected — TTL recovery will handle
    any that stall.

    After disabling jobs, bulk-swaps wos:executing → wos:paused on all GitHub
    issues whose UoWs are currently in executing status, so label state stays
    aligned with execution reality during the pause.

    Idempotent: calling /wos stop when already stopped returns a notice.

    Args:
        registry: Optional Registry instance. When omitted (the default), a new
            Registry() is created for the control event write. Callers may pass
            an existing instance to avoid opening a second connection (useful in
            tests that need to inspect the written rows).

    Returns a human-readable Telegram message describing the outcome.
    """
    from .registry import Registry as _Registry  # local import — keeps module importable without DB

    config = read_wos_config()
    if not config.get("execution_enabled"):
        return (
            "WOS pipeline is already paused.\n"
            "executor-heartbeat is skipping dispatch."
        )

    try:
        result = toggle_wos_core_jobs(enabled=False, pause_reason=_PAUSE_REASON_USER_COMMAND)
    except OSError as exc:
        return (
            f"Failed to disable WOS-core jobs: {exc}\n"
            f"Config: `{_WOS_CONFIG_PATH}`"
        )

    timer_toggled = _toggle_systemd_timers(False)

    toggled_count = len(result["toggled"])
    lines = [
        f"WOS pipeline paused. {toggled_count} WOS-core jobs disabled.",
        "UoWs already active will continue running; TTL recovery handles any that stall.",
    ]
    if timer_toggled:
        lines.append(
            f"Systemd timers disabled ({len(timer_toggled)}): "
            f"{', '.join(sorted(timer_toggled))}"
        )
    if result["not_found"]:
        lines.append(
            f"Note: {len(result['not_found'])} WOS-core job(s) not in jobs.json "
            f"(may be systemd-only): {', '.join(sorted(result['not_found']))}"
        )

    # Bulk-swap wos:executing → wos:paused on all currently-executing issues.
    reg = registry if registry is not None else _Registry()
    executing_uows = reg.list(status="executing")
    issue_numbers = [
        uow.source_issue_number
        for uow in executing_uows
        if uow.source_issue_number is not None
    ]
    wos_repo = os.environ.get("LOBSTER_WOS_REPO", "dcetlin/Lobster")
    label_success, label_failure = _bulk_swap_executing_to_paused(issue_numbers, repo=wos_repo)
    lines.append(
        f"Labels swapped: {label_success} wos:executing → wos:paused "
        f"(failures: {label_failure})"
    )

    reg.log_control_event(
        ControlEventType.WOS_STOP,
        {
            "toggled": sorted(result["toggled"]),
            "not_found": sorted(result["not_found"]),
            "timers_toggled": sorted(timer_toggled),
            "label_swap": {"success": label_success, "failed": label_failure},
        },
    )

    return "\n".join(lines)


def handle_wos_abort(uow_id: str, *, registry: "Registry") -> str:
    """
    Handle 'wos abort <uow_id>'.

    Sends SIGTERM to the process group of the subprocess dispatched for the
    given UoW. This kills the running worker explicitly without waiting for
    TTL recovery (which takes up to 24 hours).

    The kill targets the entire process group (os.killpg) so that any child
    processes spawned by the subagent (e.g. claude -p spawning shell commands)
    are also terminated.

    Flow:
      1. Look up executor_pid via registry.get_executor_pid(uow_id).
      2. If None (no running process): report not found — nothing to kill.
      3. If found: call registry.kill_executor(uow_id) which sends SIGTERM
         and clears executor_pid on success or ProcessLookupError.

    Returns a human-readable Telegram message describing the outcome.

    Note: The registry transition from executing/active to failed/ready-for-steward
    is NOT performed here — the killed subprocess will fail to write its result
    file, and TTL/heartbeat recovery will detect the missing result and re-queue
    the UoW on the next observation cycle. This preserves the existing recovery
    path and avoids a conflicting state transition race.

    Args:
        uow_id: The UoW identifier to abort.
        registry: The Registry instance to query and update.
    """
    pid = registry.get_executor_pid(uow_id)
    if pid is None:
        return (
            f"UoW `{uow_id}`: no running process found (executor_pid is not set).\n"
            "The UoW may not have been dispatched via subprocess, or it already completed.\n"
            "Run `/wos status active` or `/wos status executing` to check current state."
        )

    killed = registry.kill_executor(uow_id)
    if killed:
        return (
            f"UoW `{uow_id}` aborted: sent SIGTERM to process group of PID {pid}.\n"
            "The subprocess and its children have been signaled for termination.\n"
            "TTL/heartbeat recovery will re-queue the UoW on the next observation cycle."
        )

    # kill_executor returned False — two distinct cases depending on whether the PID was retained.
    # PermissionError: process still running but unowned → PID retained.
    # ProcessLookupError: process already gone → PID cleared.
    retained_pid = registry.get_executor_pid(uow_id)
    if retained_pid is not None:
        # PermissionError path: process alive but we cannot signal it.
        return (
            f"UoW `{uow_id}`: cannot kill PID {pid} — permission denied.\n"
            "The process may still be running (owned by a different user).\n"
            "executor_pid has been retained for future abort attempts."
        )
    else:
        # ProcessLookupError path: process already exited, stale PID cleared.
        return (
            f"UoW `{uow_id}`: process PID {pid} was already gone (ProcessLookupError).\n"
            "The subprocess may have exited before the abort command reached it.\n"
            "executor_pid has been cleared. The UoW will be re-queued by heartbeat recovery."
        )


def handle_wos_uow(uow_id: str, *, registry: "Registry") -> str:
    """
    Handle '/wos uow <uow_id>' — validate the UoW and return a sentinel or error string.

    This function validates the UoW exists (or could exist via suffix match) and
    returns the sentinel string "found" so the dispatcher knows it can proceed to
    spawn the wos-uow-detail subagent.

    The dispatcher is responsible for:
      1. Reading the task file.
      2. Spawning a lobster-generalist subagent with the task file content, injecting
         `uow_id` into the prompt header.
      3. Sending an ack reply ("Looking up UoW...").

    Returns:
        The sentinel string "found" if the UoW exists, or an inline not-found message
        string if no matching UoW was found. When the UoW is not found, the dispatcher
        should send the returned string directly to the user without spawning a subagent.

    Note: Full detail formatting (timestamps, cycle counts, etc.) happens inside the
    subagent using the task file. This function only performs a lightweight existence
    check so the dispatcher can give an immediate "not found" response instead of
    spawning a subagent that immediately fails.
    """
    # Quick existence check — if the exact ID is not found, try suffix match.
    uow = registry.get(uow_id)
    if uow is not None:
        return "found"

    # Try suffix match (user may have typed the trailing hex only, e.g. "abc123").
    import sqlite3
    conn = registry._connect()
    try:
        rows = conn.execute(
            "SELECT id FROM uow_registry WHERE id LIKE ? ORDER BY created_at DESC",
            (f"%{uow_id}",),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) == 0:
        return f"UoW `{uow_id}` not found in registry."
    if len(rows) > 1:
        ids = ", ".join(f"`{r['id']}`" for r in rows[:5])
        suffix = f" (and {len(rows) - 5} more)" if len(rows) > 5 else ""
        return f"Ambiguous ID — {len(rows)} UoWs end with `{uow_id}`: {ids}{suffix}"
    # Exactly one suffix match.
    return "found"




def handle_wos_action(payload_b64: str, *, chat_id: int, registry: "Registry") -> str:
    """Parse a base64url action payload from a Telegram deep-link callback and route it.

    Payload format: base64url(JSON) where JSON is {"a": action, "u": uow_id}.
    Valid actions: "retry", "escalate", "mark_resolved", "close_wont_fix".

    Returns a human-readable result string for send_reply, or an error message if
    the payload is malformed or the UoW is not found.

    Auth: only executes if the caller's chat_id matches TELEGRAM_ADMIN_CHAT_ID env var.
    If the env var is unset, all WOS actions are denied (fail-secure default).
    Set TELEGRAM_ADMIN_CHAT_ID in config.env to authorize your chat ID; upgrade.sh
    Migration 122 populates this automatically from LOBSTER_ADMIN_CHAT_ID.
    """
    import base64

    admin_chat_id_str = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if not admin_chat_id_str:
        return (
            "WOS actions are disabled: TELEGRAM_ADMIN_CHAT_ID is not set. "
            "Run upgrade.sh (Migration 122) or set TELEGRAM_ADMIN_CHAT_ID in config.env."
        )
    if str(chat_id) != admin_chat_id_str:
        return "Unauthorized: WOS actions are restricted to the admin user."

    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        payload = json.loads(decoded)
        action = payload["a"]
        uow_id = payload["u"]
    except Exception as exc:
        return f"Could not parse WOS action payload: {exc}"

    if action not in ("retry", "escalate", "mark_resolved", "close_wont_fix"):
        return f"Unknown action: {action!r}"

    uow = registry.get(uow_id)
    if uow is None:
        return f"UoW not found: {uow_id}"

    from src.orchestration.registry import UoWStatus

    if action == "retry":
        registry.set_status_direct(uow_id, str(UoWStatus.READY_FOR_STEWARD))
        registry.append_audit_log(uow_id, {"event": "dashboard_action", "action": "retry", "note": "retry requested via dashboard action"})
        return f"UoW `{uow_id}` queued for retry (→ ready-for-steward)."

    if action == "escalate":
        registry.set_status_direct(uow_id, str(UoWStatus.NEEDS_HUMAN_REVIEW))
        registry.append_audit_log(uow_id, {"event": "dashboard_action", "action": "escalate", "note": "escalated via dashboard action"})
        return f"UoW `{uow_id}` escalated (→ needs-human-review)."

    if action == "mark_resolved":
        registry.set_status_direct(uow_id, str(UoWStatus.DONE))
        registry.append_audit_log(uow_id, {"event": "dashboard_action", "action": "mark_resolved", "note": "marked resolved via dashboard action"})
        return f"UoW `{uow_id}` marked resolved (→ done)."

    if action == "close_wont_fix":
        registry.set_status_direct(uow_id, str(UoWStatus.CANCELLED))
        registry.append_audit_log(uow_id, {"event": "dashboard_action", "action": "close_wont_fix", "note": "closed won't fix via dashboard action"})
        return f"UoW `{uow_id}` closed as won't fix (→ cancelled)."

    return f"Unhandled action: {action!r}"


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
    # Per-cycle completion ping: written by wos_completion_notifier.py when a UoW
    # transitions to Done or Failed in _process_uow(). Fast-path — dispatched before the
    # spawn-gate; returns action="send_reply" to deliver the pre-formatted ping to Dan.
    # Non-fatal write: steward.py swallows inbox write errors so the Done/Failed
    # registry transition is never blocked.
    "wos_done": "handle_wos_done",
    # Owner escalation: written by steward._write_owner_required_message when a subagent
    # writes outcome=owner_decision_required in its result file. Fast-path — dispatched
    # before the spawn-gate; returns action="send_reply" to notify Dan directly.
    # No subagent spawn required.
    "wos_owner_required": "handle_wos_owner_required",
    # Reconciler kill handler (issue #1253 secondary fix): written by inbox_server.py
    # when the reconciler detects a dead agent and marks it failed. When task_id has
    # the 'wos-uow_' prefix, the handler transitions the UoW directly from
    # executing → ready-for-steward, eliminating the 24h claimed_until expiry
    # cycle that previously bypassed the steward's orphan retry logic.
    "agent_failed": "handle_agent_failed",
    # Stage 2 Event-Native Nervous System (issue #1351): three typed event message types
    # emitted by wos-event-poller.py (30s Type B cron). All three are fast-path handlers
    # (dispatched before the spawn-gate) that return action="mark_processed" — they update
    # event_log.consumed_at and log the event; no subagent spawn is required.
    #
    # wos_issue_created: emitted by delta poller when a GitHub issue with wos:uow label
    # is detected. Handler logs the event and marks it consumed. Germination is still
    # handled by the existing issue-sweeper job; this event is observability + future hook.
    "wos_issue_created": "handle_wos_issue_created",
    # wos_uow_completed: emitted by delta poller when a UoW transitions to done/failed.
    # Handler marks the event consumed and logs it. Downstream capacity signaling is
    # carried by wos_capacity_available (separate event per slot freed).
    "wos_uow_completed": "handle_wos_uow_completed",
    # wos_capacity_available: emitted by delta poller when executor has free slots
    # (running < max_parallel). Handler marks the event consumed and logs it.
    # Future use: signal the germinator to promote pending UoWs faster.
    "wos_capacity_available": "handle_wos_capacity_available",
    # Async prescription request (Path 1 migration): written by steward._process_uow when
    # the heartbeat offloads the blocking claude -p call to a background subagent.
    # The UoW is in 'prescribing' state while waiting. The prescription subagent runs
    # the LLM call, writes the WorkflowArtifact, and transitions to ready-for-executor.
    # Always returns action="spawn_subagent".
    "wos_prescribe": "handle_wos_prescribe",
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
# wos_prescribe handler — async prescription dispatch (Path 1 migration)
#
# Called when the dispatcher receives a message with type="wos_prescribe".
# The message was written by steward._process_uow when the heartbeat offloaded
# the blocking LLM call to avoid tying up the 3-minute cron.
#
# Design: the prescription subagent IS an LLM — it generates the prescription
# directly from the embedded context rather than spawning a nested `claude -p`
# subprocess. The previous approach (running wos-prescription-agent.py which
# calls `claude -p`) failed systematically: the Bash tool default timeout (2 min)
# killed the Python script before `claude -p` (600s budget) could complete,
# causing ~41 consecutive failures per UoW with startup_sweep resetting each at 660s.
#
# The prescription subagent:
# 1. Generates the prescription text directly using its own LLM reasoning
# 2. Passes it to wos-write-artifact.py (pure Python, <5s, no LLM call)
# 3. The helper writes the WorkflowArtifact and transitions prescribing → ready-for-executor
# 4. Calls write_result to signal completion
#
# Always returns action="spawn_subagent".
# ---------------------------------------------------------------------------

def handle_wos_prescribe(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a wos_prescribe inbox message by spawning a prescription subagent.

    Called by route_wos_message when the dispatcher receives a message with
    type="wos_prescribe". This is the async prescription dispatch path
    (Path 1 migration): the steward heartbeat wrote this message after
    offloading the blocking LLM call to avoid tying up the 3-minute cron.

    The prescription subagent generates the prescription directly (as an LLM
    it already is), then calls wos-write-artifact.py to write the artifact and
    transition the UoW. No nested `claude -p` subprocess is involved.

    Args:
        msg: The wos_prescribe inbox message dict. Required fields:
            uow_id, uow_summary, reentry_posture, completion_gap, issue_body,
            cycles, new_cycles, selected_executor_type, prescribed_skills,
            vision_orientation, dan_register, steward_log, now_iso.

    Returns:
        A dict with action="spawn_subagent" containing task_id, agent_type,
        and prompt for the dispatcher to pass to the background Task call.
    """
    uow_id: str = msg["uow_id"]
    task_id = f"wos-prescribe-{uow_id[:8]}"

    # Extract prescription context from the payload.
    uow_summary: str = msg.get("uow_summary", "")
    uow_type: str = msg.get("uow_type", "")
    success_criteria: str = msg.get("success_criteria", "")
    reentry_posture: str = msg.get("reentry_posture", "first_execution")
    completion_gap: str = msg.get("completion_gap", "")
    issue_body: str = msg.get("issue_body", "")
    cycles: int = msg.get("cycles", 0)
    new_cycles: int = msg.get("new_cycles", 1)
    selected_executor_type: str = msg.get("selected_executor_type", "functional-engineer")
    prescribed_skills: list = msg.get("prescribed_skills", [])
    vision_orientation: str = msg.get("vision_orientation", "")
    dan_register: str = msg.get("dan_register", "")
    steward_log: str = msg.get("steward_log", "")
    uow_source: str = msg.get("uow_source", "telegram")

    # Build prior prescription history from steward_log for context.
    import json as _json_mod
    prior_prescriptions_lines: list[str] = []
    if steward_log:
        for line in steward_log.strip().splitlines():
            if not line.strip():
                continue
            try:
                entry = _json_mod.loads(line)
                if not isinstance(entry, dict):
                    continue
                event = entry.get("event", "")
                if event in ("prescription", "reentry_prescription"):
                    assessment = entry.get("completion_assessment", "")
                    cycle = entry.get("steward_cycles", "?")
                    if assessment:
                        prior_prescriptions_lines.append(
                            f"  - Cycle {cycle}: {assessment}"
                        )
            except (_json_mod.JSONDecodeError, KeyError):
                pass

    # Truncate issue_body if very large to keep the prompt manageable.
    issue_body_excerpt = issue_body.strip()
    if len(issue_body_excerpt) > 2000:
        issue_body_excerpt = issue_body_excerpt[:2000] + "\n[...truncated]"

    # Build the UoW context block (mirrors _llm_prescribe logic in steward.py).
    context_parts = [
        f"UoW ID: {uow_id}",
        f"Summary: {uow_summary}",
    ]
    if uow_type:
        context_parts.append(f"Type: {uow_type}")
    if success_criteria:
        context_parts.append(f"Success criteria: {success_criteria}")
    elif issue_body_excerpt:
        context_parts.append(f"Issue body:\n{issue_body_excerpt}")
    context_parts.append(f"Execution cycle: {cycles} (0 = first pass)")
    context_parts.append(f"Executor posture: {reentry_posture}")
    context_parts.append(f"Completion gap identified: {completion_gap}")
    if prior_prescriptions_lines:
        context_parts.append(
            "Prior prescription history:\n" + "\n".join(prior_prescriptions_lines)
        )
    uow_context = "\n".join(context_parts)

    orientation_block = (
        "\n## Dan's current orientation\n\n" + dan_register + "\n"
        if dan_register
        else ""
    )

    vision_block = (
        "\n## Vision orientation\n\n" + vision_orientation + "\n"
        if vision_orientation
        else ""
    )

    # Dispatch conventions embedded in the prescription so the Executor knows
    # how to structure its own work.
    dispatch_conventions = (
        "## Lobster Subagent Dispatch Conventions\n\n"
        "### Prompt YAML Frontmatter (required at top of every prompt)\n"
        "---\n"
        "task_id: <short-slug>\n"
        "chat_id: <user's chat_id>\n"
        "source: " + uow_source + "\n"
        "---\n\n"
        "### Agent type selection\n"
        "- GitHub issue implementation, feature work, bug fix: functional-engineer\n"
        "- Lobster system ops, infra, deploy: lobster-ops\n"
        "- General background tasks: lobster-generalist\n\n"
        "### Required prompt structure\n"
        "Every prompt must include:\n"
        "  Minimum viable output: <one concrete deliverable>\n"
        "  Boundary: do not <X>\n\n"
        "### Output delivery\n"
        "1. send_reply(chat_id=<id>, text='<result>', task_id='<slug>')\n"
        "2. write_result(task_id='<slug>', sent_reply_to_user=True)\n"
        "For internal tasks (no user reply): write_result only with sent_reply_to_user=False\n"
    )

    # Path to the write-artifact helper (pure Python, <5s, no LLM call).
    # Resolves from this file's location: src/orchestration/ -> ../../scheduled-tasks/
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '..')
    )
    write_artifact_script = os.path.join(
        repo_root, 'scheduled-tasks', 'wos-write-artifact.py'
    )
    prescribed_skills_json_arg = _json_mod.dumps(prescribed_skills)

    # Build the artifact-write command. Prescription text is piped via a temp file
    # to avoid heredoc/quoting issues with multiline content.
    write_cmd = (
        f"cd {repo_root} && uv run {write_artifact_script}"
        f" --uow-id {uow_id}"
        f" --new-cycles {new_cycles}"
        f" --executor-type {selected_executor_type}"
        f" --prescribed-skills {repr(prescribed_skills_json_arg)}"
        " < /tmp/wos_prescription_text.txt"
    )

    reset_cmd = (
        f"cd {repo_root} && uv run python -c "
        f"'import sys; sys.path.insert(0, \"{repo_root}/src\"); "
        f"from orchestration.registry import Registry, UoWStatus; "
        f"Registry().transition(\"{uow_id}\", UoWStatus.READY_FOR_STEWARD, UoWStatus.PRESCRIBING); "
        f"print(\"reset to ready-for-steward\")'"
    )

    prompt = (
        f"---\n"
        f"task_id: {task_id}\n"
        f"chat_id: 0\n"
        f"source: system\n"
        f"---\n\n"
        "You are prescribing work instructions for a Lobster subagent that will execute "
        "a Unit of Work (UoW) in a software development pipeline. "
        "Your prescription must be concrete, actionable, and directly executable. "
        "Avoid vague language. Use the success_criteria as your north star for what 'done' means. "
        "The Executor is a capable autonomous coding agent — write instructions at that level.\n\n"
        "HARD CONSTRAINT: SiderealPress/Lobster is the upstream read-only repo. "
        "Never generate a prescription that targets SiderealPress/Lobster for any write operation — "
        "this includes PR comments, issue comments, PR updates, pushes, or any other mutation. "
        "Write targets must always be dcetlin/lobster or another non-upstream repo.\n\n"
        "## Unit of Work\n\n"
        f"{uow_context}\n"
        f"{orientation_block}"
        f"{vision_block}"
        f"\n{dispatch_conventions}\n"
        "## Step 1: Generate the prescription\n\n"
        "Write a precise prescription for this UoW in front-matter + prose format. "
        "Output ONLY the prescription — no preamble, no explanation outside this structure:\n\n"
        "---\n"
        f"executor_type: {selected_executor_type}\n"
        "estimated_cycles: <integer 1-3>\n"
        "success_criteria_check: <one or two sentences describing how to verify completion>\n"
        "---\n\n"
        "<complete, actionable instructions for the Executor — include specific steps, "
        "what to produce, where to write output, and constraints; embed YAML frontmatter, "
        "Minimum viable output, Boundary, and agent_type lines as described above>\n\n"
        "## Step 2: Write the WorkflowArtifact and transition the UoW\n\n"
        "After generating the prescription above, write it to a temp file and run the "
        "write-artifact helper (pure Python, ~2s, no LLM call):\n\n"
        "```bash\n"
        "# Write your prescription output to a temp file\n"
        "cat > /tmp/wos_prescription_text.txt << 'PRESC_EOF'\n"
        "<paste your complete prescription output here — the entire ---...--- block plus instructions>\n"
        "PRESC_EOF\n\n"
        f"# Run the write-artifact helper\n"
        f"{write_cmd}\n"
        "```\n\n"
        "If the helper fails:\n"
        f"```bash\n{reset_cmd}\n```\n\n"
        f"Then call write_result(task_id=\"{task_id}\", chat_id=0, "
        f"text=\"Prescription complete for {uow_id}\", sent_reply_to_user=False).\n\n"
        f"Minimum viable output: UoW {uow_id} transitioned to ready-for-executor "
        "(or reset to ready-for-steward on failure).\n"
        "Boundary: do not modify the UoW's steward_agenda, steward_log, or "
        "steward_cycles fields directly — those are managed by the steward heartbeat."
    )

    return {
        "action": "spawn_subagent",
        "task_id": task_id,
        "agent_type": "lobster-generalist",
        "prompt": prompt,
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


def parse_council_command(text: str) -> str | None:
    """
    Parse a ``council: <topic>`` Telegram command and return the topic.

    The dispatcher calls this to detect the Agent Council invocation pattern.
    Matches ``council: <topic>`` (case-insensitive, leading/trailing whitespace
    ignored). Returns the topic string if the command matches; ``None`` otherwise.

    The council deliberation is a three-role sequential process (Researcher →
    Synthesizer → Canon-Keeper) implemented in the council-deliberation task
    definition. This parser is a pure predicate — no side effects.

    Args:
        text: The raw Telegram message text.

    Returns:
        The topic string if the command matches; ``None`` otherwise.

    Examples::

        parse_council_command("council: stiffness-toughness tradeoff")
        # → "stiffness-toughness tradeoff"

        parse_council_command("Council: What does ergonomics say about API friction?")
        # → "What does ergonomics say about API friction?"

        parse_council_command("wos status")
        # → None
    """
    import re as _re

    stripped = text.strip()
    m = _re.match(r'^council:\s+(.+)', stripped, _re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def parse_wos_abort_command(text: str) -> str | None:
    """
    Parse a ``wos abort <uow_id>`` Telegram command and return the UoW ID.

    Matches ``wos abort <uow_id>`` (case-insensitive, leading/trailing whitespace
    ignored). Returns the uow_id token if the command matches; None otherwise.

    The dispatcher calls this alongside other 'wos ...' command parsers before
    routing the command to handle_wos_abort().

    Args:
        text: The raw Telegram message text.

    Returns:
        The ``uow_id`` token if the command matches; ``None`` otherwise.

    Examples::

        parse_wos_abort_command("wos abort uow_20260426_abc123")
        # → "uow_20260426_abc123"

        parse_wos_abort_command("WOS ABORT uow_20260426_abc123")
        # → "uow_20260426_abc123"

        parse_wos_abort_command("wos start")
        # → None
    """
    stripped = text.strip()
    lower = stripped.lower()
    if lower.startswith("wos abort "):
        remainder = stripped[len("wos abort "):].strip()
        tokens = remainder.split()
        if tokens:
            return tokens[0]
    return None


def parse_wos_dashboard_command(text: str) -> bool:
    """
    Return True if the text matches the ``wos dashboard`` command.

    Matches ``wos dashboard`` or ``/wos dashboard`` (case-insensitive, leading/trailing
    whitespace ignored). Returns True if the command matches; False otherwise.

    The dispatcher calls this to detect the "wos dashboard" text command before
    routing to handle_wos_dashboard().

    Args:
        text: The raw Telegram message text.

    Returns:
        True if the text is the ``wos dashboard`` command; False otherwise.

    Examples::

        parse_wos_dashboard_command("wos dashboard")
        # → True

        parse_wos_dashboard_command("/wos dashboard")
        # → True

        parse_wos_dashboard_command("WOS DASHBOARD")
        # → True

        parse_wos_dashboard_command("wos status")
        # → False
    """
    stripped = text.strip()
    lower = stripped.lower()
    return lower in ("wos dashboard", "/wos dashboard")


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


def handle_wos_done(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_done`` inbox message — per-cycle ping for a UoW Done/Failed transition.

    Called by route_wos_message when the dispatcher receives a message written by
    wos_completion_notifier.py at the end of the Done() or fail_uow() branch.

    This handler is a fast-path: it returns action="send_reply" so the dispatcher
    delivers the pre-formatted Telegram ping to Dan directly, without spawning a
    subagent. It is dispatched before the spawn-gate (which applies only to execution
    message types that must always spawn a subagent).

    The ping text is pre-formatted by wos_completion_notifier._select_format_and_build
    in one of three variants:
    - Short-form: pearl outcome with ≤1 execution attempt (two-line terse format)
    - Rich-form: non-pearl outcome or >1 execution attempt (labelled multi-field format)
    - Failed-form: UoW that failed (failed prefix, topology, tokens, failure summary)

    After assembling the ping text, this handler generates a per-UoW HTML drilldown
    page via wos_uow_detail_gen.generate_and_upload and appends the public URL to the
    message so Dan can tap through to token breakdown, audit trail, and corrective
    traces. The HTML generation step is non-fatal: if the registry is absent (e.g. test
    env), the UoW is not found, or the upload fails, the original text is sent unchanged.

    Args:
        msg: The raw wos_done inbox message dict.  Expected fields:
            - ``text`` (str): Pre-formatted ping text from wos_completion_notifier.
            - ``chat_id`` (str): Admin chat_id to deliver the ping to.
            - ``uow_id`` (str): The UoW ID — used to generate the HTML detail URL.

    Returns:
        A dict with action="send_reply" and the ping text (with HTML detail URL when
        generation succeeds).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    _default_chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")
    text: str = msg.get("text", "WOS UoW completion notification (no detail available)")
    chat_id: str = str(msg.get("chat_id", _default_chat_id))
    uow_id: str = msg.get("uow_id", "")

    # Append the HTML drilldown URL when the UoW id is known and the detail page
    # can be generated successfully.  Non-fatal: failures are logged and the
    # original text is sent unchanged so the completion notification always fires.
    if uow_id:
        try:
            from .wos_uow_detail_gen import generate_and_upload
            detail_url = generate_and_upload(uow_id=uow_id)
            text = f"{text}\n{detail_url}"
        except Exception as exc:
            _log.debug(
                "handle_wos_done: could not generate HTML detail for UoW %r — %s: %s",
                uow_id, type(exc).__name__, exc,
            )

    return {
        "action": "send_reply",
        "text": text,
        "chat_id": chat_id,
        "message_type": "wos_done",
    }


def handle_wos_owner_required(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_owner_required`` inbox message — owner escalation from a subagent.

    Called by route_wos_message when the dispatcher receives a message written by
    steward._write_owner_required_message when a subagent writes
    outcome=owner_decision_required in its result file.

    This handler is a fast-path: it returns action="send_reply" so the dispatcher
    delivers the pre-formatted owner decision request to Dan directly, without
    spawning a subagent.

    The UoW has already been transitioned to 'awaiting-owner' status by the steward
    before this message was written to the inbox. Dan's reply in the primary thread
    constitutes the decision; the dispatcher can then re-queue the UoW to
    ready-for-steward with the decision as a note.

    Args:
        msg: The raw wos_owner_required inbox message dict. Expected fields:
            - ``text`` (str): Pre-formatted notification text from the steward.
            - ``chat_id`` (str|int): Admin chat_id to deliver the notification to.
            - ``uow_id`` (str): The UoW ID — carried for dispatcher reference.
            - ``uow_title`` (str): UoW summary — carried for dispatcher reference.

    Returns:
        A dict with action="send_reply" and the notification text.
    """
    _default_chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")
    text: str = msg.get("text", "WOS UoW awaiting your decision (no detail available)")
    chat_id: str = str(msg.get("chat_id", _default_chat_id))

    return {
        "action": "send_reply",
        "text": text,
        "chat_id": chat_id,
        "message_type": "wos_owner_required",
    }


def handle_agent_failed(
    msg: dict[str, Any],
    registry: "Registry | None" = None,
) -> dict[str, Any]:
    """
    Handle an agent_failed inbox message by transitioning the WOS UoW to ready-for-steward.

    Called by route_wos_message when the dispatcher receives a message with
    type="agent_failed". The reconciler writes these messages when it detects a
    dead agent session.

    If the task_id has the 'wos-uow_' prefix, the UoW is immediately transitioned
    from executing → ready-for-steward via registry.record_agent_failed_kill().
    This eliminates the 24h claimed_until expiry window that previously caused dead
    WOS agents to cycle through claim_expired → ready-for-executor indefinitely,
    bypassing the steward's orphan retry budget.

    For non-WOS task_ids (ghost-mark-failed-* from agent-monitor, or system agents),
    the message is acknowledged with no state change.

    Returns action="mark_processed" in all cases — no subagent spawn, no user reply.

    Args:
        msg: The raw agent_failed inbox message dict.
        registry: Optional Registry instance. If omitted, a default Registry() is
            created. Pass an explicit instance in tests.

    Returns:
        {"action": "mark_processed", "message_type": "agent_failed"}
    """
    from .registry import Registry as _Registry

    task_id: str = msg.get("task_id", "") or ""
    agent_id: str = msg.get("agent_id", "") or ""

    _WOS_UOW_PREFIX = "wos-uow_"
    if task_id.startswith(_WOS_UOW_PREFIX):
        uow_id = task_id[len("wos-"):]  # strips "wos-" → "uow_20260519_3ab0bd"
        reg = registry if registry is not None else _Registry()
        try:
            rows = reg.record_agent_failed_kill(uow_id, agent_task_id=task_id)
            if rows == 1:
                _log.info(
                    "handle_agent_failed: transitioned WOS UoW %s → ready-for-steward "
                    "(task_id=%s, agent_id=%s)",
                    uow_id, task_id, agent_id,
                )
            else:
                _log.info(
                    "handle_agent_failed: UoW %s already transitioned (rows=0, race) "
                    "(task_id=%s)",
                    uow_id, task_id,
                )
        except Exception as exc:
            _log.error(
                "handle_agent_failed: record_agent_failed_kill raised %s: %s "
                "(uow_id=%s, task_id=%s) — message will be marked processed anyway",
                type(exc).__name__, exc, uow_id, task_id,
            )
    else:
        _log.debug(
            "handle_agent_failed: non-WOS task_id %r (agent_id=%s) — no UoW transition",
            task_id, agent_id,
        )

    return {"action": "mark_processed", "message_type": "agent_failed"}


# ---------------------------------------------------------------------------
# Stage 2 Event-Native Nervous System handlers (issue #1351)
#
# Three typed event message types emitted by wos-event-poller.py (30s Type B
# cron). All three are fast-path handlers: they mark the event consumed in
# event_log and return action="mark_processed". No subagent spawn required.
#
# route_wos_message dispatches these before the spawn-gate because they
# legitimately return action="mark_processed" rather than "spawn_subagent".
# ---------------------------------------------------------------------------


def handle_wos_issue_created(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_issue_created`` typed inbox event (Stage 2, issue #1351).

    Emitted by wos-event-poller.py when a new GitHub issue with label ``wos:uow``
    is detected via the 30s delta poller. Records the event in event_log as
    consumed and logs it for observability.

    Germination is still handled by the existing issue-sweeper job; this handler
    is primarily an observability hook and a foundation for future Stage 2 routing.

    Pure function — no blocking I/O beyond the event_log update.

    Args:
        msg: The raw inbox message dict. Expected fields:
            ``event_id`` (str): UUID of the event_log row.
            ``issue_number`` (int): GitHub issue number.
            ``issue_url`` (str): Full GitHub issue URL.
            ``title`` (str): Issue title.
            ``labels`` (list[str]): Label names on the issue.
            ``triggered_at`` (str): ISO-8601 timestamp of detection.

    Returns:
        ``{"action": "mark_processed", "message_type": "wos_issue_created"}``
    """
    event_id: str = msg.get("event_id", "")
    issue_number: int = msg.get("issue_number", 0)
    issue_url: str = msg.get("issue_url", "")
    title: str = msg.get("title", "")

    _log.info(
        "handle_wos_issue_created: issue #%d %r (%s) event_id=%s",
        issue_number, title, issue_url, event_id,
    )

    if event_id:
        try:
            from .wos_events import mark_event_consumed as _mark_consumed  # noqa: PLC0415
            _mark_consumed(event_id, consumer_task_id="dispatcher-wos_issue_created")
        except Exception as exc:
            _log.warning(
                "handle_wos_issue_created: mark_event_consumed failed for %s: %s",
                event_id, exc,
            )

    return {"action": "mark_processed", "message_type": "wos_issue_created"}


def handle_wos_uow_completed(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_uow_completed`` typed inbox event (Stage 2, issue #1351).

    Emitted by wos-event-poller.py when it detects a UoW that has transitioned
    to ``done`` or ``failed`` state since the last poll. Records the event as
    consumed in event_log.

    Downstream capacity signaling is carried by ``wos_capacity_available``
    (a separate event emitted alongside this one when a slot is freed).

    Pure function — no blocking I/O beyond the event_log update.

    Args:
        msg: The raw inbox message dict. Expected fields:
            ``event_id`` (str): UUID of the event_log row.
            ``uow_id`` (str): The completed UoW identifier.
            ``outcome`` (str): Terminal outcome — ``"done"`` or ``"failed"``.
            ``register`` (str): Register the UoW was assigned to.
            ``output_ref`` (str | None): Path to the output artifact.
            ``triggered_at`` (str): ISO-8601 timestamp of the transition.

    Returns:
        ``{"action": "mark_processed", "message_type": "wos_uow_completed"}``
    """
    event_id: str = msg.get("event_id", "")
    uow_id: str = msg.get("uow_id", "")
    outcome: str = msg.get("outcome", "")

    _log.info(
        "handle_wos_uow_completed: UoW %r outcome=%s event_id=%s",
        uow_id, outcome, event_id,
    )

    if event_id:
        try:
            from .wos_events import mark_event_consumed as _mark_consumed  # noqa: PLC0415
            _mark_consumed(event_id, consumer_task_id="dispatcher-wos_uow_completed")
        except Exception as exc:
            _log.warning(
                "handle_wos_uow_completed: mark_event_consumed failed for %s: %s",
                event_id, exc,
            )

    return {"action": "mark_processed", "message_type": "wos_uow_completed"}


def handle_wos_capacity_available(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a ``wos_capacity_available`` typed inbox event (Stage 2, issue #1351).

    Emitted by wos-event-poller.py when the executor has free slots
    (running < max_parallel). Signals that the germinator can promote pending
    UoWs to ready-for-executor state without violating the parallel cap.

    Records the event as consumed in event_log and logs capacity state for
    observability. Future use: trigger germination without waiting for the
    next steward-heartbeat cron tick.

    Pure function — no blocking I/O beyond the event_log update.

    Args:
        msg: The raw inbox message dict. Expected fields:
            ``event_id`` (str): UUID of the event_log row.
            ``freed_uow_id`` (str): UoW whose completion freed the slot.
            ``freed_at`` (str): ISO-8601 timestamp when the slot was freed.
            ``current_active_count`` (int): Active UoWs after the slot freed.
            ``max_parallel`` (int): Maximum allowed concurrent UoWs.

    Returns:
        ``{"action": "mark_processed", "message_type": "wos_capacity_available"}``
    """
    event_id: str = msg.get("event_id", "")
    freed_uow_id: str = msg.get("freed_uow_id", "")
    current_active_count: int = msg.get("current_active_count", -1)
    max_parallel: int = msg.get("max_parallel", -1)

    _log.info(
        "handle_wos_capacity_available: freed_uow_id=%r active=%d/%d event_id=%s",
        freed_uow_id, current_active_count, max_parallel, event_id,
    )

    if event_id:
        try:
            from .wos_events import mark_event_consumed as _mark_consumed  # noqa: PLC0415
            _mark_consumed(event_id, consumer_task_id="dispatcher-wos_capacity_available")
        except Exception as exc:
            _log.warning(
                "handle_wos_capacity_available: mark_event_consumed failed for %s: %s",
                event_id, exc,
            )

    return {"action": "mark_processed", "message_type": "wos_capacity_available"}


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
    # wos_done fast-path: per-cycle completion ping written by wos_completion_notifier.py
    # at the end of the Done() or fail_uow() branch in steward._process_uow().
    # Always returns action="send_reply" — no subagent spawn required.
    # ---------------------------------------------------------------------------
    if msg_type == "wos_done":
        try:
            done_result = handle_wos_done(msg)
            done_result["message_type"] = msg_type
            return done_result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_wos_done raised %s: %s — "
                "returning send_reply alert",
                type(exc).__name__, exc,
            )
            return {
                "action": "send_reply",
                "text": (
                    f"WOS completion ping handler raised an error "
                    f"({type(exc).__name__}: {exc}). "
                    "Completion ping was NOT delivered. Check logs."
                ),
                "message_type": msg_type,
            }

    # ---------------------------------------------------------------------------
    # wos_owner_required fast-path: written by steward._write_owner_required_message
    # when a subagent writes outcome=owner_decision_required. The UoW is already
    # transitioned to awaiting-owner by the steward before this message is written.
    # Always returns action="send_reply" — no subagent spawn required.
    # ---------------------------------------------------------------------------
    if msg_type == "wos_owner_required":
        try:
            owner_result = handle_wos_owner_required(msg)
            owner_result["message_type"] = msg_type
            return owner_result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_wos_owner_required raised %s: %s — "
                "returning send_reply alert",
                type(exc).__name__, exc,
            )
            return {
                "action": "send_reply",
                "text": (
                    f"WOS owner-required handler raised an error "
                    f"({type(exc).__name__}: {exc}). "
                    "Owner notification was NOT delivered. Check logs."
                ),
                "message_type": msg_type,
            }

    # ---------------------------------------------------------------------------
    # agent_failed fast-path: dispatched before the spawn-gate because this handler
    # legitimately returns action="mark_processed" (not spawn_subagent).
    # Transitions WOS UoWs from executing → ready-for-steward immediately on
    # reconciler kill, bypassing the 24h claimed_until expiry cycle.
    # ---------------------------------------------------------------------------
    if msg_type == "agent_failed":
        try:
            result = handle_agent_failed(msg)
            result["message_type"] = msg_type
            return result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_agent_failed raised %s: %s — "
                "marking processed without state transition",
                type(exc).__name__, exc,
            )
            return {"action": "mark_processed", "message_type": msg_type}

    # ---------------------------------------------------------------------------
    # Stage 2 Event-Native fast-paths (issue #1351): all three event types return
    # action="mark_processed" — they record event_log consumption and log the event.
    # Dispatched before the spawn-gate because they legitimately do not spawn subagents.
    # ---------------------------------------------------------------------------
    if msg_type == "wos_issue_created":
        try:
            result = handle_wos_issue_created(msg)
            result["message_type"] = msg_type
            return result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_wos_issue_created raised %s: %s — "
                "marking processed",
                type(exc).__name__, exc,
            )
            return {"action": "mark_processed", "message_type": msg_type}

    if msg_type == "wos_uow_completed":
        try:
            result = handle_wos_uow_completed(msg)
            result["message_type"] = msg_type
            return result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_wos_uow_completed raised %s: %s — "
                "marking processed",
                type(exc).__name__, exc,
            )
            return {"action": "mark_processed", "message_type": msg_type}

    if msg_type == "wos_capacity_available":
        try:
            result = handle_wos_capacity_available(msg)
            result["message_type"] = msg_type
            return result
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "route_wos_message: handle_wos_capacity_available raised %s: %s — "
                "marking processed",
                type(exc).__name__, exc,
            )
            return {"action": "mark_processed", "message_type": msg_type}

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

        elif msg_type == "wos_prescribe":
            result = handle_wos_prescribe(msg)
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
    "vision_accept:",
    "vision_decline:",
    "routing_pref_confirm:",
    "routing_pref_reject:",
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
    - ``vision_accept:<field_path>:<hash>`` — accept a vision proposal
    - ``vision_decline:<field_path>:<hash>`` — decline a vision proposal
    - ``routing_pref_confirm:<payload_b64>`` — confirm a routing preference proposal
    - ``routing_pref_reject:<payload_b64>`` — reject a routing preference proposal

    All other callback_data values fall through to ``handled=False``, which lets
    the dispatcher handle other callback types (job-confirm, delete-confirm, etc.)
    via its existing prose routing logic.

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
            ``True`` if callback_data matched a known pattern;
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

    if data.startswith("vision_accept:") or data.startswith("vision_decline:"):
        effective_chat_id = int(chat_id) if chat_id is not None else int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
        text = handle_vision_callback(data, chat_id=effective_chat_id)
        if text is None:
            text = f"Unknown vision callback: {data}"
        return {"action": "send_reply", "text": text, "chat_id": chat_id, "handled": True}

    if data.startswith("routing_pref_confirm:") or data.startswith("routing_pref_reject:"):
        effective_chat_id = int(chat_id) if chat_id is not None else int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
        text = handle_routing_pref_callback(data, chat_id=effective_chat_id)
        return {"action": "send_reply", "text": text, "chat_id": chat_id, "handled": True}

    # Not a known callback — signal the dispatcher to use its own handling
    return {"action": "send_reply", "text": f"Unknown callback: {data}", "chat_id": chat_id, "handled": False}


# ---------------------------------------------------------------------------
# Vision Object callback handler — vision_accept / vision_decline
# ---------------------------------------------------------------------------


def handle_vision_callback(
    callback_data: str,
    chat_id: int = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")),
) -> str | None:
    """
    Handle Telegram inline keyboard callbacks for the Vision Object inlet.

    Parses ``callback_data`` for ``vision_accept:<field_path>:<hash>`` and
    ``vision_decline:<field_path>:<hash>`` prefixes and routes to the accept or
    decline handler in ``src.harvest.vision_inlet``.

    Returns a reply string if this is a vision callback, or ``None`` if the
    callback_data does not match a vision prefix (so the caller can route other
    callbacks normally).

    Dispatcher integration — call via ``route_callback_message`` which wires this
    automatically for type="callback" messages::

        from src.orchestration.dispatcher_handlers import route_callback_message

        if msg.get("type") == "callback":
            result = route_callback_message(msg)
            if result["handled"]:
                send_reply(chat_id=result["chat_id"], text=result["text"],
                           message_id=message_id)
    """
    if not (callback_data.startswith("vision_accept:") or callback_data.startswith("vision_decline:")):
        return None

    try:
        from src.harvest.vision_inlet import handle_vision_callback as _vi_callback  # type: ignore[import]
    except ImportError:
        return "vision_inlet module unavailable — cannot process vision callback."

    return _vi_callback(callback_data, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Routing preference callback handler — routing_pref_confirm / routing_pref_reject
# ---------------------------------------------------------------------------

# Module-level routing-preferences cache.  Populated on first load and refreshed
# every _ROUTING_PREFS_RELOAD_INTERVAL messages (or after a routing_pref_confirm).
_cached_routing_prefs: list[dict] = []
_routing_prefs_message_counter: int = 0
_ROUTING_PREFS_RELOAD_INTERVAL: int = 10

_ROUTING_PREFS_PATH = Path.home() / "lobster-user-config" / "routing-preferences.yaml"
_PENDING_PROPOSALS_PATH = Path.home() / "lobster-workspace" / "data" / "pending-routing-proposals.json"


def reload_routing_preferences() -> list[dict]:
    """Load (or reload) routing preferences from disk. Updates module-level cache."""
    global _cached_routing_prefs
    try:
        from src.routing.preferences import load_routing_preferences
        _cached_routing_prefs = load_routing_preferences(_ROUTING_PREFS_PATH)
    except Exception as exc:
        _log.warning("routing_prefs: failed to load: %s", exc)
        _cached_routing_prefs = []
    return _cached_routing_prefs


def maybe_annotate_routing_hint(message: dict) -> dict:
    """Check cached routing preferences against message; annotate with routing_hint if matched.

    Reloads preferences from disk every _ROUTING_PREFS_RELOAD_INTERVAL calls so
    newly confirmed preferences take effect without a dispatcher restart.
    Returns the (possibly annotated) message dict — caller owns the returned value.
    """
    global _routing_prefs_message_counter
    _routing_prefs_message_counter += 1
    if _routing_prefs_message_counter % _ROUTING_PREFS_RELOAD_INTERVAL == 1:
        reload_routing_preferences()

    if not _cached_routing_prefs:
        return message

    try:
        from src.routing.preferences import match_routing_preference
        rule = match_routing_preference(message, _cached_routing_prefs)
    except Exception as exc:
        _log.warning("routing_prefs: match failed: %s", exc)
        return message

    if rule:
        _log.info("routing_pref_match: rule=%s condition=%s", rule.get("id"), rule.get("condition"))
        message = dict(message)
        message["routing_hint"] = rule.get("route_to")
    return message


def handle_routing_pref_callback(callback_data: str, chat_id: int) -> str:
    """Handle ``routing_pref_confirm:<payload_b64>`` and ``routing_pref_reject:<payload_b64>``.

    On confirm: decodes the base64 JSON payload, derives a stable rule id,
                appends the rule to ``~/lobster-user-config/routing-preferences.yaml``,
                records the accepted proposal in ``pending-routing-proposals.json``,
                and refreshes the in-memory preferences cache immediately.
    On reject:  records the rejected proposal and returns a dismissal message.

    The payload format matches what morning-briefing.md specifies::

        {
          "condition": "<observation text, trimmed to 80 chars>",
          "agent_hint": "<agent name>",
          "source_event_ids": ["<id>", ...],
          "proposed_at": "<ISO timestamp>"
        }

    Returns a reply string suitable for sending directly to the user.
    """
    import base64
    import json as _json
    import uuid as _uuid
    from datetime import datetime, timezone
    import yaml

    is_accept = callback_data.startswith("routing_pref_confirm:")
    prefix = "routing_pref_confirm:" if is_accept else "routing_pref_reject:"
    payload_b64 = callback_data[len(prefix):]

    try:
        payload = _json.loads(base64.b64decode(payload_b64).decode())
    except Exception as exc:
        return f"routing_pref: could not decode payload — {exc}"

    condition = payload.get("condition", "unknown")[:80]
    agent_hint = payload.get("agent_hint", "general-purpose")

    # Update pending proposals file — keyed by first 16 chars of b64 for stability.
    proposals: dict = {}
    if _PENDING_PROPOSALS_PATH.exists():
        try:
            proposals = _json.loads(_PENDING_PROPOSALS_PATH.read_text())
        except Exception:
            proposals = {}

    proposal_id = payload_b64[:16]
    decision_status = "accepted" if is_accept else "rejected"
    if proposal_id in proposals:
        proposals[proposal_id]["status"] = decision_status
    else:
        proposals[proposal_id] = {**payload, "status": decision_status}

    try:
        _PENDING_PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PENDING_PROPOSALS_PATH.write_text(_json.dumps(proposals, indent=2))
    except Exception as exc:
        _log.warning("routing_pref: could not write proposals file: %s", exc)

    if not is_accept:
        return f"Skipped. Routing suggestion for '{condition}' discarded."

    # Append rule to routing-preferences.yaml
    try:
        raw = yaml.safe_load(_ROUTING_PREFS_PATH.read_text()) if _ROUTING_PREFS_PATH.exists() else {}
    except Exception:
        raw = {}

    raw = raw or {}
    rules: list = raw.get("rules") or raw.get("preferences") or []

    now_iso = datetime.now(timezone.utc).isoformat()
    rule_id = f"rule_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{_uuid.uuid4().hex[:6]}"

    new_rule = {
        "id": rule_id,
        "condition": condition,
        "route_to": agent_hint,
        "confidence": float(payload.get("confidence", 0.0)),
        "observation_count": 1,
        "confirmed_by": "dan",
        "confirmed_at": now_iso,
        "active": True,
    }
    rules.append(new_rule)

    raw["rules"] = rules
    raw.pop("preferences", None)  # normalise to "rules" key
    raw["last_updated"] = now_iso

    try:
        _ROUTING_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ROUTING_PREFS_PATH.write_text(
            "# Routing preferences — managed by Lobster HITL loop\n"
            "# Each entry is a rule confirmed by Dan via Telegram\n"
            "# managed_by: pattern-obs-routing-loop\n"
            + yaml.dump(raw, default_flow_style=False, allow_unicode=True)
        )
    except Exception as exc:
        return f"routing_pref: rule decoded but could not write YAML — {exc}"

    # Refresh in-memory cache immediately so new rule takes effect in this session.
    reload_routing_preferences()

    return f"Routing preference saved: '{condition}' -> '{agent_hint}'"


# ---------------------------------------------------------------------------
# Help text — update when new commands are added
# ---------------------------------------------------------------------------

COMMAND_HELP: str = """Lobster command index

System status:
  /status             — running agents, WOS state, CC usage snapshot
  /quota              — CC quota windows and reset times (5h and 7d)
  status / health     — usage %, WOS state, active agents (prose command)
  usage               — Claude quota windows and reset times (prose command)
  usage full          — full usage report (spawns subagent)
  agents              — list active subagent sessions
  inbox               — queue depth and processing state

LOS (action items):
  /todos              — show open action items with Done/Snooze buttons
  /todo add <text>    — add a new action item
  /todo done <text>   — mark an item done by partial text or ID
  /todo snooze <text> [days] — snooze an item (default: 3 days)

WOS control:
  /wos                — active UoW count, pipeline status breakdown, Bisque link
  wos start  — enable WOS pipeline (all 14 WOS-core jobs + execution_enabled)
  wos stop   — pause WOS pipeline (all 14 WOS-core jobs + execution_enabled)
  wos status [status] — show active + queued UoWs
  wos uow <uow-id>    — show detail for a specific UoW
  wos unblock         — clear BOOTUP_CANDIDATE_GATE flag
  wos abort <uow-id>  — send SIGTERM to running subprocess for a UoW

Decision:
  /approve <uow-id>   — approve a proposed UoW
  /decide <uow-id> <action> — resolve a blocked UoW
    actions: proceed, retry, retry force, abandon, defer [note], owner <decision>
    owner action: re-queues an awaiting-owner UoW with the given decision note

Config (user bootup files):
  /config list                  — list all user config files with line counts
  /config read <filename>       — show file contents (chunked if long)
  /config search <query>        — search for text across all user config files
  /config append <filename> <text> — append text to a user config file

Skills:
  /shop               — list available skills
  /shop install <name> — install and activate a skill
  /skill activate/deactivate <name> — toggle a skill

Review:
  /re-review <PR URL or number> — re-run oracle review on a PR

Restart:
  restart mcp         — restart MCP server (auto-reconnects)
  restart dispatcher  — instructions to restart dispatcher process

Debug:
  debug on / debug off — toggle debug flag file

Help:
  /help / help        — this index
"""


def handle_help() -> str:
    """Handle 'help' / '/help' command — return command index."""
    return COMMAND_HELP


# ---------------------------------------------------------------------------
# CC quota state — path and stale threshold
# ---------------------------------------------------------------------------

# Default path for the cc-budget state file written by cc-usage-poller.py.
# Overridable via LOBSTER_CC_BUDGET_STATE env var or the state_path argument.
_CC_BUDGET_STATE_PATH: Path = Path.home() / ".claude" / "cc-budget" / "state.json"

# Data older than this many hours is treated as unavailable (poller may be down).
QUOTA_STALE_THRESHOLD_HOURS: int = 2


def read_quota_state(state_path: Path | None = None) -> dict | None:
    """Read the CC budget state written by cc-usage-poller.

    Pure read: no side effects beyond file I/O. Returns the parsed dict, or None
    when the file is absent, unreadable, malformed, or missing ``rate_limits``.

    Path resolution order:
    1. ``state_path`` argument (if provided)
    2. ``LOBSTER_CC_BUDGET_STATE`` env var
    3. ``~/.claude/cc-budget/state.json`` (default)
    """
    resolved: Path
    if state_path is not None:
        resolved = state_path
    else:
        env_override = os.environ.get("LOBSTER_CC_BUDGET_STATE")
        resolved = Path(env_override) if env_override else _CC_BUDGET_STATE_PATH

    try:
        text = resolved.read_text(encoding="utf-8")
        data = json.loads(text)
        # Accept state that has either:
        # - ``rate_limits`` (written by cc-usage-poller / cc-usage-collect.sh — v2 schema)
        # - ``token_usage`` (written by local-session-parser — cookie-free fallback)
        # Require at least one to ensure we have meaningful data, not a bare empty dict.
        if "rate_limits" not in data and "token_usage" not in data:
            return None
        return data
    except Exception:
        return None


def _is_quota_state_stale(state: dict) -> bool:
    """Return True if the state's last_updated timestamp exceeds QUOTA_STALE_THRESHOLD_HOURS.

    Falls back to False (fresh) when last_updated is absent or unparseable so that
    partial state data is still surfaced rather than silently suppressed.
    """
    from datetime import datetime as _datetime, timezone as _timezone, timedelta as _timedelta

    last_updated = state.get("last_updated")
    if not last_updated:
        return False  # no timestamp — assume fresh rather than suppress
    try:
        ts_str = last_updated.replace("Z", "+00:00")
        ts = _datetime.fromisoformat(ts_str)
        age = _datetime.now(_timezone.utc) - ts
        return age > _timedelta(hours=QUOTA_STALE_THRESHOLD_HOURS)
    except Exception:
        return False


def _format_token_usage_fallback(token_usage: dict) -> str:
    """Format a CC usage string from local-session-parser token counts.

    Called when rate_limits percentage data is unavailable (poller cookie
    expired) but local token counts from the session-file parser are present.

    Format:
        CC usage (local): today 269M tokens | week 1.2B tokens | 5h 42M tokens
        [cookie expired — no % available]

    Pure function: no side effects. All inputs are arguments.
    """
    def _fmt_tokens(n: int) -> str:
        """Format a raw token count as a human-readable abbreviated string."""
        if n >= 1_000_000_000:
            return f"{n / 1_000_000_000:.1f}B"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.0f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}K"
        return str(n)

    today = _fmt_tokens(token_usage.get("tokens_today", 0))
    week = _fmt_tokens(token_usage.get("tokens_this_week", 0))
    five_h = _fmt_tokens(token_usage.get("five_hour_tokens", 0))
    return (
        f"CC usage (local): today {today} | week {week} | 5h {five_h} tokens\n"
        f"[cookie expired — quota % unavailable]"
    )


def format_quota_message(state: dict | None) -> str:
    """Format a CC usage string from the cc-budget state dict.

    Data source priority:
    1. ``rate_limits`` with non-None pct values (poller or statusLine hook) — full % format.
    2. ``token_usage`` from local-session-parser — token counts format when % unavailable.
    3. "unavailable" string — when neither source has fresh data.

    Returns the unavailable message when:
    - ``state`` is None (file missing/unreadable)
    - ``state`` is stale (older than QUOTA_STALE_THRESHOLD_HOURS) AND has no token_usage
    - ``rate_limits`` pct values are None and ``token_usage`` is absent or stale

    Format when poller data is available:
        CC usage: 5h 42% | 7d 15%. Resets 5h: May 15 4:10 PM ET / 7d: May 22 11:00 AM ET.

    Format when only local token counts are available (cookie expired):
        CC usage (local): today 269M | week 1.2B | 5h 42M tokens
        [cookie expired — quota % unavailable]

    Pure function: no side effects. All inputs are arguments.
    """
    _UNAVAILABLE = "CC usage data unavailable — poller may not have run yet."

    if state is None:
        return _UNAVAILABLE

    # Try rate_limits (poller / hook data) first — requires non-None pct values.
    try:
        rl = state.get("rate_limits", {})
        five_pct = rl.get("five_hour", {}).get("pct")
        seven_pct = rl.get("seven_day", {}).get("pct")
        five_resets_at = rl.get("five_hour", {}).get("resets_at")
        seven_resets_at = rl.get("seven_day", {}).get("resets_at")
        has_pct = five_pct is not None and seven_pct is not None
    except (KeyError, TypeError, AttributeError):
        has_pct = False

    if has_pct:
        # Staleness check only applies to the percentage-based path.
        if _is_quota_state_stale(state):
            has_pct = False  # fall through to token_usage check below
        else:
            def _fmt_reset(iso: str | None) -> str:
                """Format an ISO reset timestamp in the owner's configured timezone."""
                if not iso:
                    return "unknown"
                try:
                    return _format_iso_for_user(iso, fmt="%b %-d %-I:%M %p %Z")
                except Exception:
                    return iso[:16]  # fallback: truncated ISO

            five_reset_str = _fmt_reset(five_resets_at)
            seven_reset_str = _fmt_reset(seven_resets_at)
            return (
                f"CC usage: 5h {five_pct:.0f}% | 7d {seven_pct:.0f}%. "
                f"Resets — 5h: {five_reset_str} / 7d: {seven_reset_str}."
            )

    # Fallback: local token counts from session-file parser.
    # These use their own last_updated timestamp inside token_usage, independent
    # of the top-level state last_updated (which may be stale from the poller).
    token_usage = state.get("token_usage")
    if token_usage:
        token_last_updated = token_usage.get("last_updated")
        token_fresh = False
        if token_last_updated:
            try:
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                ts = _dt.fromisoformat(token_last_updated.replace("Z", "+00:00"))
                age = _dt.now(_tz.utc) - ts
                token_fresh = age <= _td(hours=QUOTA_STALE_THRESHOLD_HOURS)
            except Exception:
                token_fresh = True  # unparseable → assume fresh

        if token_fresh:
            return _format_token_usage_fallback(token_usage)

    return _UNAVAILABLE


def format_status_message(
    active_sessions: list[dict],
    wos_config: dict,
    status_counts: dict,
    quota_state: dict | None,
) -> str:
    """Format a system status snapshot for Telegram display.

    Assembles three lines from independently-sourced data:
    - WOS execution state and queue depth from wos_config + status_counts
    - Active agent count and IDs from active_sessions
    - CC usage percentage from quota_state (or unavailable)

    Pure function: all inputs are arguments; no file reads or MCP calls.

    Example output:
        ◉ WOS: enabled | 179 ready-for-steward | 1 executing
        ◉ Agents: 2 running (task-a, task-b)
        ◉ CC usage: 5h 42% | 7d 15%. Resets — 5h: May 15 4:10 PM ET / 7d: May 22 11:00 AM ET.
    """
    execution_enabled = bool(wos_config.get("execution_enabled", False))
    wos_label = "enabled" if execution_enabled else "stopped"

    # Build queue-depth string from status_counts
    queue_parts: list[str] = []
    for status, count in sorted(status_counts.items()):
        if count > 0:
            queue_parts.append(f"{count} {status}")
    queue_str = " | ".join(queue_parts) if queue_parts else "0 UoWs"

    wos_line = f"◉ WOS: {wos_label} | {queue_str}"

    # Active agents line
    agent_count = len(active_sessions)
    if agent_count == 0:
        agents_line = "◉ Agents: 0 running"
    else:
        agent_ids = [
            s.get("task_id") or s.get("id") or "?"
            for s in active_sessions
        ]
        agents_line = f"◉ Agents: {agent_count} running ({', '.join(agent_ids)})"

    # CC usage line
    quota_line = "◉ " + format_quota_message(quota_state)

    return "\n".join([wos_line, agents_line, quota_line])


# ---------------------------------------------------------------------------
# Inline dispatcher command handlers (Phase 1 + 2)
#
# These handlers implement snag-reachable commands that execute directly on
# the dispatcher main thread without spawning a subagent.  Each function is
# pure with respect to MCP calls — any MCP data (active_sessions, inbox msgs)
# must be gathered by the dispatcher before calling these functions.
# ---------------------------------------------------------------------------

# Path to the debug-enabled flag file.  Touch to enable; unlink to disable.
_DEBUG_FLAG_PATH: Path = Path.home() / "lobster-workspace" / "data" / "debug-enabled"


def handle_usage() -> str:
    """Handle prose 'usage' command — inline CC quota read from state.json.

    Pure file read: reads cc-budget/state.json via read_quota_state() and
    formats the result using format_quota_message().  Adds session cost when
    available.  Returns the unavailable message when the file is absent or stale.
    """
    state = read_quota_state()
    quota_msg = format_quota_message(state)
    if state:
        cost = state.get("session_cost_usd")
        if cost is not None:
            quota_msg += f"\nSession cost: ${cost:.2f}"
    return quota_msg


def handle_status(active_sessions: list[dict]) -> str:
    """Handle prose 'status' / 'health' command — inline system snapshot.

    Reads wos-config.json and cc-budget/state.json directly (fast file reads).
    active_sessions must be gathered by the dispatcher via get_active_sessions()
    before calling this function.

    Returns a 3-line status string covering WOS state, agent count, and CC usage.
    """
    wos_config = read_wos_config()
    quota_state = read_quota_state()

    execution_enabled = bool(wos_config.get("execution_enabled", False))
    wos_label = "enabled" if execution_enabled else "stopped"

    agent_count = len(active_sessions)
    quota_msg = format_quota_message(quota_state)

    return "\n".join([
        f"WOS: {wos_label}",
        f"Active agents: {agent_count}",
        quota_msg,
    ])


def handle_agents(active_sessions: list[dict]) -> str:
    """Handle prose 'agents' command — format active session list.

    active_sessions must be gathered by the dispatcher via get_active_sessions()
    before calling this function.
    """
    if not active_sessions:
        return "No active agents."
    lines = [f"Active agents ({len(active_sessions)}):"]
    for s in active_sessions:
        agent_id = s.get("task_id") or s.get("agent_id") or s.get("id") or "?"
        desc = s.get("description", "")
        lines.append(f"  • {agent_id}: {desc}")
    return "\n".join(lines)


def handle_inbox(msgs: list[dict], total_count: int) -> str:
    """Handle prose 'inbox' command — format queue depth and recent messages.

    msgs and total_count must be gathered by the dispatcher via check_inbox()
    and get_stats() before calling this function.
    """
    lines = [f"Inbox: {total_count} pending"]
    for m in (msgs or [])[:5]:
        preview = (m.get("text") or "")[:60].replace("\n", " ")
        if preview:
            lines.append(f"  • {preview}")
    return "\n".join(lines)


def handle_debug(on: bool) -> str:
    """Handle 'debug on' / 'debug off' — toggle the debug-enabled flag file.

    Touches ~/lobster-workspace/data/debug-enabled to enable debug mode;
    unlinks it to disable.  Returns a confirmation string.
    """
    try:
        if on:
            _DEBUG_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _DEBUG_FLAG_PATH.touch()
            return f"Debug mode enabled. Flag: `{_DEBUG_FLAG_PATH}`"
        else:
            if _DEBUG_FLAG_PATH.exists():
                _DEBUG_FLAG_PATH.unlink()
            return "Debug mode disabled. Flag file removed."
    except OSError as exc:
        return f"Debug toggle failed: {exc}"


def handle_restart_mcp() -> str:
    """Handle 'restart mcp' — return the inline ACK message.

    The dispatcher sends this text as an immediate reply, then spawns a subagent
    to run ~/lobster/scripts/restart-mcp.sh --no-wait.  The subagent performs
    the actual restart; the dispatcher reconnects automatically.

    Returns the ACK text to send before the subagent is spawned.
    """
    return (
        "MCP restart initiated. The service will restart in ~5 seconds. "
        "Reconnection is automatic — you may see a brief gap in responsiveness."
    )


def handle_restart_dispatcher() -> str:
    """Handle 'restart dispatcher' — return manual restart instructions.

    The Claude Code process cannot restart itself.  This function returns
    the instructions Dan must follow to restart the dispatcher manually.
    """
    return (
        "The dispatcher (Claude Code process) cannot restart itself.\n\n"
        "To restart:\n"
        "1. Open a new terminal on the Lobster host\n"
        "2. Run: ~/lobster/scripts/claude-persistent.sh\n"
        "3. The new session will pick up from the inbox queue automatically."
    )


def handle_usage_full() -> str:
    """Handle 'usage full' — return the spawning acknowledgement.

    This command is NOT snag-reachable by design: it requires a subagent.
    Returns the ack text to send before spawning the usage-report subagent.
    The dispatcher is responsible for the actual Task spawn with the appropriate
    prompt (run usage-report.sh --format full, or fall back to state.json).
    """
    return "Spawning usage report agent..."


# ---------------------------------------------------------------------------
# /config — user bootup file access from Telegram (issue #1018)
# ---------------------------------------------------------------------------

# Allowlist of user config files accessible via /config commands.
# System files in .claude/ are not included — those are protected.
_USER_CONFIG_DIR: Path = Path.home() / "lobster-user-config" / "agents"
_USER_CONFIG_FILENAMES: tuple[str, ...] = (
    "user.base.bootup.md",
    "user.base.context.md",
    "user.dispatcher.bootup.md",
    "user.subagent.bootup.md",
    "system-audit.context.md",
    "user.development.md",
    "user.epistemic.md",
)

# Telegram message size limit (chars). Content beyond this is chunked.
_TELEGRAM_CHAR_LIMIT: int = 4000


def _config_file_path(filename: str) -> Path | None:
    """Return the resolved path for a user config file, or None if not allowed."""
    # Strip leading path components — accept bare filename or agents/filename
    name = Path(filename).name
    if name not in _USER_CONFIG_FILENAMES:
        return None
    p = _USER_CONFIG_DIR / name
    return p if p.exists() else None


def handle_config_list() -> str:
    """Return a formatted list of user config files with line counts."""
    lines: list[str] = ["User config files in ~/lobster-user-config/agents/:", ""]
    found = False
    for name in _USER_CONFIG_FILENAMES:
        p = _USER_CONFIG_DIR / name
        if p.exists():
            try:
                line_count = len(p.read_text(encoding="utf-8").splitlines())
            except OSError:
                line_count = 0
            lines.append(f"  {name} ({line_count} lines)")
            found = True
    if not found:
        lines.append("  (no user config files found)")
    return "\n".join(lines)


def handle_config_read(filename: str) -> tuple[str, bool]:
    """Read a user config file and return (text, needs_chunking).

    Returns (error_message, False) if the file is not found or not allowed.
    Returns (content, True) if content exceeds _TELEGRAM_CHAR_LIMIT.
    Returns (content, False) otherwise.
    """
    p = _config_file_path(filename)
    if p is None:
        name = Path(filename).name
        if name not in _USER_CONFIG_FILENAMES:
            return (
                f"Not allowed: '{name}' is not in the user config allowlist.\n"
                "Use /config list to see available files.",
                False,
            )
        return (f"File not found: '{name}' (may not exist yet).", False)

    try:
        content = p.read_text(encoding="utf-8")
    except OSError as exc:
        return (f"Could not read '{p.name}': {exc}", False)

    needs_chunking = len(content) > _TELEGRAM_CHAR_LIMIT
    return (content, needs_chunking)


def handle_config_search(query: str) -> str:
    """Search for a term across all user config files.

    Returns matching lines with filename and line number, formatted for Telegram.
    """
    if not query or not query.strip():
        return "Usage: /config search <query>"

    query = query.strip()
    results: list[str] = []
    matched_files = 0

    for name in _USER_CONFIG_FILENAMES:
        p = _USER_CONFIG_DIR / name
        if not p.exists():
            continue
        try:
            file_results: list[str] = []
            for lineno, line in enumerate(
                p.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if query.lower() in line.lower():
                    # Truncate long lines for Telegram readability
                    display = line.rstrip()
                    if len(display) > 120:
                        display = display[:117] + "..."
                    file_results.append(f"  L{lineno}: {display}")
            if file_results:
                results.append(f"{name}:")
                results.extend(file_results)
                matched_files += 1
        except OSError:
            continue

    if not results:
        return f"No matches for '{query}' in user config files."

    header = f"Search results for '{query}' ({matched_files} file(s)):"
    body = "\n".join(results)
    full = f"{header}\n\n{body}"

    # Truncate if over limit, with a note
    if len(full) > _TELEGRAM_CHAR_LIMIT:
        truncated = full[: _TELEGRAM_CHAR_LIMIT - 60]
        full = truncated + f"\n\n... (truncated, {len(full)} chars total)"

    return full


def handle_config_append(filename: str, text: str) -> str:
    """Append text to a user config file.

    Returns a confirmation string or an error message.
    """
    if not text or not text.strip():
        return "Usage: /config append <filename> <text>"

    p = _config_file_path(filename)
    if p is None:
        name = Path(filename).name
        if name not in _USER_CONFIG_FILENAMES:
            return (
                f"Not allowed: '{name}' is not in the user config allowlist.\n"
                "Use /config list to see available files."
            )
        # File doesn't exist yet — create it
        p = _USER_CONFIG_DIR / name

    try:
        _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            # Ensure we start on a new line
            f.write(f"\n{text.strip()}\n")
        # Return a confirmation with the last 200 chars of the file for verification
        content = p.read_text(encoding="utf-8")
        tail = content[-200:].strip()
        return f"Appended to {p.name}.\n\nTail:\n{tail}"
    except OSError as exc:
        return f"Could not write to '{p.name}': {exc}"


# ---------------------------------------------------------------------------
# WOS PR coordinator routing (issue uow_20260516_71b777)
#
# Called from the dispatcher's ENGINEER → REVIEWER routing block when a
# completed subagent result contains a GitHub PR URL.  Routes WOS-originated
# PRs (task_id starts with "wos-") to the wos-pr-coordinator agent, which
# owns the full oracle→fix→merge loop internally.  Non-WOS PRs fall through
# to the existing review agent path unchanged.
#
# Dispatcher integration (add to ENGINEER → REVIEWER routing, before the
# existing Task(subagent_type="review", ...) call):
#
#     from src.orchestration.dispatcher_handlers import route_wos_pr_result
#
#     pr_url_match = re.search(r"https://github\.com/.*/pull/\d+", msg["text"])
#     if pr_url_match:
#         routing = route_wos_pr_result(
#             pr_url=pr_url_match.group(0),
#             task_id=msg.get("task_id"),
#             chat_id=msg["chat_id"],
#             result_text=msg["text"],
#         )
#         if routing["action"] == "spawn_subagent":
#             Task(subagent_type=routing["agent_type"],
#                  run_in_background=True,
#                  prompt=routing["prompt"])
#             mark_processed(message_id)
#             continue
#         # else: fallthrough — let existing review agent path handle it
# ---------------------------------------------------------------------------


def route_wos_pr_result(
    pr_url: str,
    task_id: str | None,
    chat_id: int | str,
    result_text: str,
) -> dict[str, Any]:
    """Route a subagent result containing a GitHub PR URL.

    If ``task_id`` starts with ``"wos-"``, builds a coordinator Task prompt
    that owns the full oracle→fix→merge loop for the PR internally.  Returns
    ``action="spawn_subagent"`` so the dispatcher can spawn the coordinator
    without any further logic.

    If ``task_id`` does NOT start with ``"wos-"`` (or is None), returns
    ``action="fallthrough"`` so the dispatcher falls through to the existing
    review agent path unchanged.

    Pure function: no side effects, no I/O.

    Args:
        pr_url:      Full GitHub PR URL extracted from the subagent result text.
        task_id:     task_id from the subagent result message (may be None).
        chat_id:     Admin chat_id for Dan notifications (passed through to coordinator).
        result_text: Full result text from the subagent (used as task_context).

    Returns:
        ``{"action": "spawn_subagent", "task_id": ..., "prompt": ..., "agent_type": ...}``
        when routing to coordinator, or ``{"action": "fallthrough"}`` otherwise.
    """
    import re as _re

    if not (task_id and task_id.startswith("wos-")):
        return {"action": "fallthrough"}

    # Extract pr_number and repo from the PR URL.
    # Expected format: https://github.com/{owner}/{repo}/pull/{number}
    parts = pr_url.rstrip("/").split("/")
    try:
        pr_number = int(parts[-1])
        repo = f"{parts[-4]}/{parts[-3]}"
    except (IndexError, ValueError):
        _log.warning(
            "route_wos_pr_result: could not parse PR URL %r — falling through",
            pr_url,
        )
        return {"action": "fallthrough"}

    coordinator_task_id = f"wos-pr-coord-{pr_number}"

    prompt = (
        f"---\n"
        f"task_id: {coordinator_task_id}\n"
        f"chat_id: {chat_id}\n"
        f"source: wos/coordinator\n"
        f"---\n\n"
        f"You are the WOS PR pipeline coordinator for PR #{pr_number}.\n\n"
        f"pr_url: {pr_url}\n"
        f"pr_number: {pr_number}\n"
        f"repo: {repo}\n"
        f"task_id: {coordinator_task_id}\n"
        f"chat_id: {chat_id}\n"
        f"task_context: {result_text[:500]}\n\n"
        f"Follow the wos-pr-coordinator agent definition at "
        f".claude/agents/wos-pr-coordinator.md exactly.\n\n"
        f"Minimum viable output: Single write_result call reporting PR merged or escalated.\n"
        f"Boundary: do not send intermediate oracle/fix status to the dispatcher inbox."
    )

    return {
        "action": "spawn_subagent",
        "task_id": coordinator_task_id,
        "prompt": prompt,
        "agent_type": "lobster-generalist",
    }
