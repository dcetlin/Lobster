"""
WOS completion notification layer — per-cycle ping for Done/Failed transitions.

Spec: docs/wos/wos-completion-report-spec.md §Per-Cycle Ping

Provides:
- Pure builder functions for short/rich/failed Telegram message formats
- _select_format_and_build — format selector (pure, no I/O)
- _extract_completion_rationale — steward_log parser (pure)
- _count_seeds_from_artifacts — seed counter from artifacts list (pure)
- _write_wos_done_message — inbox writer (side-effecting, non-fatal)

Insertion point:
- Called by steward.py at the end of the Done() branch in _process_uow()
- Called by steward.py at the end of fail_uow() for failed UoWs

Design principles:
- Pure builders at the core; side effects isolated to _write_wos_done_message
- Non-fatal: inbox write failure must not block the Done/Failed registry transition
- Degrades gracefully when gate_fired / seeds_surfaced fields are absent (not yet in schema)
- Reads LOBSTER_ADMIN_CHAT_ID and LOBSTER_INBOX_DIR at call time (not module load time)
  so tests can override via patch.dict(os.environ) without re-importing the module
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("wos_completion_notifier")

# ---------------------------------------------------------------------------
# Named constants — anchored to spec values
# ---------------------------------------------------------------------------

#: The only primary_outcome that qualifies for short-form (spec: "pearl outcomes").
SHORT_FORM_TRIGGER_OUTCOME: str = "pearl"

#: Maximum execution_attempts for short-form eligibility.
#: At ">1 execution attempt" the spec requires rich-form.
SHORT_FORM_MAX_EXECUTION_ATTEMPTS: int = 1

#: Inbox message type for per-cycle completion ping.
WOS_DONE_MESSAGE_TYPE: str = "wos_done"

#: Default admin chat_id (read from env at call time; module-level default only).
_ADMIN_CHAT_ID_DEFAULT: str = "8075091586"

#: Default inbox directory (read from env at call time; overridable in tests).
_INBOX_DIR_DEFAULT: str = "~/messages/inbox"


# ---------------------------------------------------------------------------
# Pure builder functions
# ---------------------------------------------------------------------------

def _build_short_form(
    uow_title: str,
    primary_outcome: str,
    steward_cycles: int,
    token_usage: int | None,
    seeds_surfaced_count: int,
) -> str:
    """
    Build the short-form per-cycle ping for pearl outcomes with ≤1 execution attempt.

    Spec format:
        UoW done: <uow_title> [<primary_outcome>]
        <steward_cycles> cycle(s) · <token_usage> tokens · <seeds_surfaced_count> seeds surfaced

    Pure function — no I/O.

    Args:
        uow_title: UoW summary string from the registry.
        primary_outcome: Metabolic taxonomy label (pearl/heat/seed/shit).
        steward_cycles: Number of steward cycles for this UoW.
        token_usage: Total input+output tokens, or None if not recorded.
        seeds_surfaced_count: Number of artifacts with category='seed'.

    Returns:
        Two-line formatted Telegram message string.
    """
    token_str = str(token_usage) if token_usage is not None else "unknown"
    line1 = f"UoW done: {uow_title} [{primary_outcome}]"
    line2 = f"{steward_cycles} cycle(s) · {token_str} tokens · {seeds_surfaced_count} seeds surfaced"
    return f"{line1}\n{line2}"


def _build_rich_form(
    uow_title: str,
    primary_outcome: str,
    steward_cycles: int,
    execution_attempts: int,
    token_usage: int | None,
    seeds_surfaced_count: int,
    gate_fired: str,
    completion_rationale: str,
) -> str:
    """
    Build the rich-form per-cycle ping for non-pearl outcomes or >1 execution attempt.

    Spec format:
        UoW done: <uow_title>
        Outcome : <primary_outcome>
        Topology: <gate_fired description> (<steward_cycles> cycles, <execution_attempts> attempt(s))
        Tokens  : <token_usage>
        Seeds   : <seeds_surfaced_count> new item(s) surfaced
        Rationale: <completion_rationale>

    The Rationale line is omitted when completion_rationale is empty.

    Pure function — no I/O.

    Args:
        uow_title: UoW summary string.
        primary_outcome: Metabolic taxonomy label.
        steward_cycles: Number of steward cycles.
        execution_attempts: Number of confirmed execution dispatches.
        token_usage: Total tokens or None.
        seeds_surfaced_count: Seed artifact count.
        gate_fired: Topology gate label (spiral/dead_end/burst/none), or 'none' when absent.
        completion_rationale: Assessment from steward_closure event, or empty string.

    Returns:
        Multi-line formatted Telegram message string.
    """
    token_str = str(token_usage) if token_usage is not None else "unknown"
    lines = [
        f"UoW done: {uow_title}",
        f"Outcome : {primary_outcome}",
        f"Topology: {gate_fired} ({steward_cycles} cycles, {execution_attempts} attempt(s))",
        f"Tokens  : {token_str}",
        f"Seeds   : {seeds_surfaced_count} new item(s) surfaced",
    ]
    if completion_rationale:
        lines.append(f"Rationale: {completion_rationale}")
    return "\n".join(lines)


def _build_failed_form(
    uow_title: str,
    gate_fired: str,
    steward_cycles: int,
    execution_attempts: int,
    token_usage: int | None,
    failure_summary: str,
) -> str:
    """
    Build the failed-UoW per-cycle ping.

    Spec format:
        UoW failed: <uow_title>
        Topology: <gate_fired> gate (<steward_cycles> cycles, <execution_attempts> attempts)
        Tokens  : <token_usage or "unknown">
        Failure : <failure_summary>

    Pure function — no I/O.

    Args:
        uow_title: UoW summary string.
        gate_fired: Topology gate label or 'none'.
        steward_cycles: Number of steward cycles.
        execution_attempts: Number of confirmed execution dispatches.
        token_usage: Total tokens or None.
        failure_summary: Close reason or audit log excerpt summarising the failure.

    Returns:
        Multi-line formatted Telegram message string.
    """
    token_str = str(token_usage) if token_usage is not None else "unknown"
    lines = [
        f"UoW failed: {uow_title}",
        f"Topology: {gate_fired} gate ({steward_cycles} cycles, {execution_attempts} attempts)",
        f"Tokens  : {token_str}",
        f"Failure : {failure_summary}",
    ]
    return "\n".join(lines)


def _select_format_and_build(
    uow_title: str,
    primary_outcome: str,
    steward_cycles: int,
    execution_attempts: int,
    token_usage: int | None,
    seeds_surfaced_count: int,
    gate_fired: str,
    completion_rationale: str,
    failure_summary: str | None,
    failed: bool,
) -> str:
    """
    Select the correct message format and build the Telegram notification text.

    Format selection (spec §Per-Cycle Ping):
    - Failed form: when failed=True
    - Short form: when primary_outcome == SHORT_FORM_TRIGGER_OUTCOME
                  AND execution_attempts <= SHORT_FORM_MAX_EXECUTION_ATTEMPTS
    - Rich form: all other done cases (non-pearl, or >1 attempt)

    Pure function — no I/O.

    Args:
        uow_title: UoW summary string.
        primary_outcome: Metabolic taxonomy label ('pearl'/'heat'/'seed'/'shit').
        steward_cycles: Steward cycle count.
        execution_attempts: Confirmed execution dispatches.
        token_usage: Total tokens or None.
        seeds_surfaced_count: Number of seed artifacts.
        gate_fired: Topology gate label.
        completion_rationale: Assessment from steward_closure event.
        failure_summary: Failure description (required when failed=True).
        failed: True if this is a failed UoW ping (not a done ping).

    Returns:
        Formatted Telegram message string.
    """
    if failed:
        return _build_failed_form(
            uow_title=uow_title,
            gate_fired=gate_fired,
            steward_cycles=steward_cycles,
            execution_attempts=execution_attempts,
            token_usage=token_usage,
            failure_summary=failure_summary or "",
        )

    use_short_form = (
        primary_outcome == SHORT_FORM_TRIGGER_OUTCOME
        and execution_attempts <= SHORT_FORM_MAX_EXECUTION_ATTEMPTS
    )

    if use_short_form:
        return _build_short_form(
            uow_title=uow_title,
            primary_outcome=primary_outcome,
            steward_cycles=steward_cycles,
            token_usage=token_usage,
            seeds_surfaced_count=seeds_surfaced_count,
        )

    return _build_rich_form(
        uow_title=uow_title,
        primary_outcome=primary_outcome,
        steward_cycles=steward_cycles,
        execution_attempts=execution_attempts,
        token_usage=token_usage,
        seeds_surfaced_count=seeds_surfaced_count,
        gate_fired=gate_fired,
        completion_rationale=completion_rationale,
    )


# ---------------------------------------------------------------------------
# Steward log extraction — pure
# ---------------------------------------------------------------------------

def _extract_completion_rationale(steward_log: str | None) -> str:
    """
    Extract the assessment field from the last steward_closure event in steward_log.

    The steward_log is a newline-delimited sequence of JSON objects.  The
    steward_closure event (written at Done() time) carries an 'assessment'
    field containing the Steward's completion rationale.

    Pure function — no I/O.

    Args:
        steward_log: Raw steward_log string from the UoW registry, or None.

    Returns:
        The assessment string from the last steward_closure event, or '' if
        none is found or the field is absent.
    """
    if not steward_log:
        return ""

    last_assessment: str = ""
    for line in steward_log.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("event") == "steward_closure":
            last_assessment = entry.get("assessment") or ""

    return last_assessment


# ---------------------------------------------------------------------------
# Seeds interim count from artifacts — pure
# ---------------------------------------------------------------------------

def _count_seeds_from_artifacts(artifacts: list[dict] | None) -> int:
    """
    Count artifacts with category='seed' as an interim seeds_surfaced_count.

    Per spec §Schema Additions §2: until the structured seeds_surfaced field is
    implemented (Addition 2), seeds_surfaced_count is approximated by counting
    artifacts where category='seed'.  This over-counts auto-extracted issue refs
    but is acceptable as an interim measure.

    Pure function — no I/O.

    Args:
        artifacts: List of typed ref dicts from the registry artifacts field, or None.

    Returns:
        Count of artifacts with category == 'seed'.  0 when artifacts is None or empty.
    """
    if not artifacts:
        return 0
    return sum(1 for a in artifacts if isinstance(a, dict) and a.get("category") == "seed")


# ---------------------------------------------------------------------------
# Inbox writer — side-effecting, non-fatal
# ---------------------------------------------------------------------------

def _write_wos_done_message(
    uow_id: str,
    text: str,
    chat_id: str | None = None,
) -> None:
    """
    Write a wos_done inbox message so the dispatcher can deliver it to Dan.

    Called by steward.py at the end of the Done() branch and fail_uow() path.
    Non-fatal: any write failure is logged and swallowed so that the registry
    Done/Failed transition is never blocked by inbox write errors.

    Message schema:
        {
            "id":       "<uuid>",
            "source":   "system",
            "type":     "wos_done",
            "chat_id":  "<admin_chat_id>",
            "uow_id":   "<uow_id>",
            "text":     "<pre-formatted Telegram message>",
            "timestamp": "<ISO-8601 UTC>"
        }

    Uses atomic write (tmp → rename) to avoid the dispatcher reading a partial file.

    Args:
        uow_id: The UoW ID that just completed or failed.
        text: Pre-formatted Telegram notification text from _select_format_and_build.
        chat_id: Admin chat_id to deliver to.  Defaults to LOBSTER_ADMIN_CHAT_ID env var,
                 then falls back to _ADMIN_CHAT_ID_DEFAULT.
    """
    try:
        resolved_chat_id = (
            chat_id
            or os.environ.get("LOBSTER_ADMIN_CHAT_ID", _ADMIN_CHAT_ID_DEFAULT)
        )
        inbox_dir_str = os.environ.get("LOBSTER_INBOX_DIR", _INBOX_DIR_DEFAULT)
        inbox_dir = Path(os.path.expanduser(inbox_dir_str))
        inbox_dir.mkdir(parents=True, exist_ok=True)

        msg_id = str(uuid.uuid4())
        msg: dict[str, Any] = {
            "id": msg_id,
            "source": "system",
            "type": WOS_DONE_MESSAGE_TYPE,
            "chat_id": resolved_chat_id,
            "uow_id": uow_id,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        tmp_path = inbox_dir / f"{msg_id}.json.tmp"
        dest_path = inbox_dir / f"{msg_id}.json"
        try:
            tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
            tmp_path.rename(dest_path)
            log.info(
                "_write_wos_done_message: wrote %s for UoW %r → %s",
                WOS_DONE_MESSAGE_TYPE, uow_id, dest_path,
            )
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    except Exception as exc:
        log.warning(
            "_write_wos_done_message: failed to write %s for UoW %r — %s: %s",
            WOS_DONE_MESSAGE_TYPE, uow_id, type(exc).__name__, exc,
        )


# ---------------------------------------------------------------------------
# Public API — called by steward.py
# ---------------------------------------------------------------------------

def notify_uow_done(
    uow_id: str,
    uow_title: str,
    primary_outcome: str | None,
    steward_cycles: int,
    execution_attempts: int,
    token_usage: int | None,
    artifacts: list[dict] | None,
    steward_log: str | None,
    gate_fired: str = "none",
    chat_id: str | None = None,
) -> None:
    """
    Compose and write the per-cycle ping for a successful (Done) UoW transition.

    Orchestrates format selection, field extraction, and inbox delivery.
    Non-fatal at every step: failures are logged and swallowed so the
    Done() registry transition is never blocked.

    Insertion point: end of the Done() branch in _process_uow(), after all
    registry writes and _append_cycle_trace().

    Args:
        uow_id: The WOS unit-of-work ID.
        uow_title: UoW summary from the registry.
        primary_outcome: Metabolic taxonomy label; None falls back to 'seed'.
        steward_cycles: Steward cycle count.
        execution_attempts: Confirmed execution dispatch count.
        token_usage: Total tokens or None.
        artifacts: Registry artifacts list or None (for seed count approximation).
        steward_log: Raw steward_log JSON string or None.
        gate_fired: Topology gate label (populated after migration 0019).
        chat_id: Admin chat_id; defaults to LOBSTER_ADMIN_CHAT_ID env var.
    """
    try:
        resolved_outcome = primary_outcome or "seed"
        seeds_count = _count_seeds_from_artifacts(artifacts)
        rationale = _extract_completion_rationale(steward_log)

        text = _select_format_and_build(
            uow_title=uow_title,
            primary_outcome=resolved_outcome,
            steward_cycles=steward_cycles,
            execution_attempts=execution_attempts,
            token_usage=token_usage,
            seeds_surfaced_count=seeds_count,
            gate_fired=gate_fired,
            completion_rationale=rationale,
            failure_summary=None,
            failed=False,
        )
        _write_wos_done_message(uow_id=uow_id, text=text, chat_id=chat_id)
    except Exception as exc:
        log.warning(
            "notify_uow_done: failed for UoW %r — %s: %s",
            uow_id, type(exc).__name__, exc,
        )


def notify_uow_failed(
    uow_id: str,
    uow_title: str,
    gate_fired: str = "none",
    steward_cycles: int = 0,
    execution_attempts: int = 0,
    token_usage: int | None = None,
    failure_summary: str = "",
    chat_id: str | None = None,
) -> None:
    """
    Compose and write the per-cycle ping for a failed UoW transition.

    Orchestrates failed-form format and inbox delivery.  Non-fatal.

    Insertion point: end of the fail_uow() / orphan path in _process_uow().

    Args:
        uow_id: The WOS unit-of-work ID.
        uow_title: UoW summary from the registry.
        gate_fired: Topology gate label (populated after migration 0019).
        steward_cycles: Steward cycle count.
        execution_attempts: Confirmed execution dispatch count.
        token_usage: Total tokens or None.
        failure_summary: Close reason or audit log excerpt.
        chat_id: Admin chat_id; defaults to LOBSTER_ADMIN_CHAT_ID env var.
    """
    try:
        text = _select_format_and_build(
            uow_title=uow_title,
            primary_outcome="",
            steward_cycles=steward_cycles,
            execution_attempts=execution_attempts,
            token_usage=token_usage,
            seeds_surfaced_count=0,
            gate_fired=gate_fired,
            completion_rationale="",
            failure_summary=failure_summary,
            failed=True,
        )
        _write_wos_done_message(uow_id=uow_id, text=text, chat_id=chat_id)
    except Exception as exc:
        log.warning(
            "notify_uow_failed: failed for UoW %r — %s: %s",
            uow_id, type(exc).__name__, exc,
        )
