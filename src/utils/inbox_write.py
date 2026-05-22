"""
Shared inbox I/O helpers for Lobster scheduled-task scripts.

Consolidates the copy-pasted write_inbox_message() / _inbox_dir() pattern
that previously appeared in six separate scripts (issue #781).

Public API
----------
write_inbox_message(job_name, chat_id, text, timestamp) -> str
    Write a subagent_result JSON to ~/messages/inbox/ using atomic
    tmp-then-rename.  Returns the message ID so callers can log it.
    Non-urgent messages sent outside the morning window are queued in
    pending-deliveries.jsonl instead of the inbox (circadian delivery).

write_crash_alert(job_name, exc, extra) -> None
    Write a scheduled_task_crash inbox message so the dispatcher alerts Dan.
    Best-effort; failures are logged but do not mask the original exception.

_inbox_dir() -> Path
_task_outputs_dir() -> Path
    Directory helpers; exposed for scripts that need the raw path (e.g. to
    construct task-output filenames with a consistent date prefix).
"""

from __future__ import annotations

import json
import logging
import os
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _inbox_dir() -> Path:
    """Return the inbox directory path, creating it if needed."""
    messages_base = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    inbox = Path(messages_base) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def _task_outputs_dir() -> Path:
    """Return the task-outputs directory path, creating it if needed."""
    messages_base = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    task_outputs = Path(messages_base) / "task-outputs"
    task_outputs.mkdir(parents=True, exist_ok=True)
    return task_outputs


def write_inbox_message(
    job_name: str,
    chat_id: int,
    text: str,
    timestamp: str,
) -> str:
    """
    Write a single subagent_result message to the Lobster inbox.

    Uses atomic tmp-then-rename so the dispatcher never reads a partial file.
    The dispatcher picks up the file and routes it via send_reply.

    Circadian routing: if the message is non-urgent and the current time is
    outside the morning window (06:00–10:00 Pacific), the message is queued
    in pending-deliveries.jsonl for batch morning delivery instead of being
    written to the inbox immediately.

    Parameters
    ----------
    job_name:
        Short identifier for the calling job (e.g. ``"daily-metrics"``).
        Used as the prefix of the generated message ID so log entries are
        traceable back to their origin.
    chat_id:
        Telegram chat ID to deliver the message to.
    text:
        Human-readable message body.
    timestamp:
        ISO-8601 timestamp string (e.g. ``datetime.now(timezone.utc).isoformat()``).

    Returns
    -------
    str
        The generated message ID (``"<job_name>_<uuid-hex>"``).
    """
    msg_id = f"{job_name}_{uuid.uuid4().hex}"
    source = os.environ.get("LOBSTER_DEFAULT_SOURCE", "telegram")
    msg = {
        "id": msg_id,
        "type": "subagent_result",
        "task_id": msg_id,
        "chat_id": chat_id,
        "source": source,
        "text": text,
        "status": "success",
        "sent_reply_to_user": False,
        "timestamp": timestamp,
    }

    # Circadian gate: defer non-urgent messages to the morning delivery window.
    try:
        from src.delivery.circadian import is_morning_window, is_non_urgent, queue_message  # noqa: PLC0415
        if is_non_urgent(msg) and not is_morning_window():
            queue_message(chat_id, text, source=job_name)
            return msg_id
    except Exception:
        pass  # circadian module unavailable — fall through to immediate delivery

    inbox = _inbox_dir()
    out_path = inbox / f"{msg_id}.json"
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    return msg_id


def write_crash_alert(job_name: str, exc: BaseException, extra: str = "") -> None:
    """Write a scheduled_task_crash inbox message so the dispatcher alerts Dan.

    Best-effort fire-and-forget. Failures are logged but do not mask the
    original exception — the caller must re-raise or sys.exit after this call.
    """
    admin_chat_id: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")

    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tb_str = "".join(tb_lines).strip()
    if len(tb_str) > 2000:
        tb_str = "...(truncated)...\n" + tb_str[-2000:]

    text = (
        f"[CRASH] {job_name} crashed with {type(exc).__name__}: {exc}\n\n"
        f"```\n{tb_str}\n```"
    )
    if extra:
        text += f"\n\n{extra}"

    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "type": "scheduled_task_crash",
        "chat_id": admin_chat_id,
        "job_name": job_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
    }

    try:
        inbox = _inbox_dir()
        tmp_path = inbox / f"{msg_id}.json.tmp"
        dest_path = inbox / f"{msg_id}.json"
        tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp_path.rename(dest_path)
        log.info("Crash alert written to inbox (%s)", msg_id)
    except Exception as write_exc:
        log.error("Failed to write crash alert to inbox: %s", write_exc)
