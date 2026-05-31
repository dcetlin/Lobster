"""
WOS Event-Native Nervous System — typed event emission (Stage 2).

Provides three pure emit functions that write typed inbox messages and record
events in the event_log DB table. The dispatcher's WOS_MESSAGE_TYPE_DISPATCH
table routes these message types to the corresponding handlers.

**Event types emitted:**

  wos_issue_created       — GitHub issue with label wos:uow was created
  wos_uow_completed       — UoW transitioned to done/failed state
  wos_capacity_available  — Executor has free slots (running < max_parallel)

**Design principles:**

- All emit functions are pure side-effectful functions: they write to disk
  (inbox/ and event_log) and return the event_id on success.
- Deduplication is enforced at the DB layer via the UNIQUE(event_type, dedup_key)
  index. Callers can safely call emit_* on every poll cycle without flooding
  the inbox — duplicate events are silently skipped.
- Non-fatal pattern: inbox write failures are logged and swallowed so that
  event emission never blocks the caller's critical path.
- event_log writes use atomic tmp-then-rename for the inbox file; the DB write
  uses a short-timeout connection consistent with Registry._connect().

**Consuming events:**

  After the dispatcher processes a typed event message, it should call
  mark_event_consumed(event_id, consumer_task_id) to record the consumed_at
  timestamp. This is advisory — unconsumed events do not block future emission.

References:
  spec: ~/lobster-workspace/workstreams/wos/spec/wos-evolution-spec.md §3-II
  issue: https://github.com/dcetlin/Lobster/issues/1351
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path helpers — read from env at call time so tests can override via
# patch.dict(os.environ, {...}) without re-importing the module.
# ---------------------------------------------------------------------------

_INBOX_DIR_DEFAULT: str = "~/messages/inbox"
_ADMIN_CHAT_ID_DEFAULT: str = "8075091586"


def _inbox_dir() -> Path:
    """Return the inbox directory path, creating it if absent."""
    base = os.environ.get("LOBSTER_INBOX_DIR", _INBOX_DIR_DEFAULT)
    path = Path(os.path.expanduser(base))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _admin_chat_id() -> str:
    """Return the admin chat_id from the environment."""
    return os.environ.get("LOBSTER_ADMIN_CHAT_ID", _ADMIN_CHAT_ID_DEFAULT)


def _registry_db_path() -> Path:
    """Return the registry.db path from the environment."""
    if env_path := os.environ.get("REGISTRY_DB_PATH"):
        return Path(env_path)
    workspace = Path(
        os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    )
    return workspace / "orchestration" / "registry.db"


# ---------------------------------------------------------------------------
# event_log DB helpers
# ---------------------------------------------------------------------------

def _db_connect(db_path: Path) -> sqlite3.Connection:
    """Open a short-timeout WAL connection to registry.db."""
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _record_event(
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    dedup_key: str | None,
    db_path: Path | None = None,
) -> bool:
    """
    Insert a row into event_log; return True on success, False on duplicate.

    Uses INSERT OR IGNORE so callers can safely call this on every poll cycle
    without raising on duplicate dedup_key values. Returns False when the
    insert was a no-op (duplicate), True when a new row was written.

    Non-fatal: DB errors are logged and return False so inbox emission can
    proceed independently of whether the DB write succeeded.
    """
    path = db_path or _registry_db_path()
    if not path.exists():
        log.debug("_record_event: registry DB not found at %s — skipping", path)
        return False
    try:
        conn = _db_connect(path)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO event_log
                (event_id, event_type, payload, emitted_at, dedup_key)
            VALUES
                (?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                json.dumps(payload),
                datetime.now(timezone.utc).isoformat(),
                dedup_key,
            ),
        )
        conn.commit()
        conn.close()
        return cursor.rowcount > 0
    except Exception as exc:
        log.warning("_record_event: DB write failed for %s/%s: %s", event_type, dedup_key, exc)
        return False


def mark_event_consumed(
    event_id: str,
    consumer_task_id: str | None = None,
    *,
    db_path: Path | None = None,
) -> None:
    """
    Mark an event as consumed by recording consumed_at and consumer_task_id.

    Called by the dispatcher after it finishes processing a typed WOS event.
    Non-fatal — failures are logged and swallowed.
    """
    path = db_path or _registry_db_path()
    if not path.exists():
        return
    try:
        conn = _db_connect(path)
        conn.execute(
            """
            UPDATE event_log
               SET consumed_at = ?, consumer_task_id = ?
             WHERE event_id = ? AND consumed_at IS NULL
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                consumer_task_id,
                event_id,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("mark_event_consumed: failed for event %s: %s", event_id, exc)


# ---------------------------------------------------------------------------
# Inbox message writer
# ---------------------------------------------------------------------------

def _write_inbox_message(msg: dict[str, Any]) -> None:
    """
    Atomically write a typed WOS event message to the inbox.

    Uses tmp-then-rename so the dispatcher never reads a partial file.
    Non-fatal: failures are logged and re-raised so callers can decide.
    """
    msg_id: str = msg["id"]
    inbox = _inbox_dir()
    dest = inbox / f"{msg_id}.json"
    tmp = inbox / f"{msg_id}.json.tmp"
    try:
        tmp.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp.rename(dest)
        log.info("_write_inbox_message: wrote %s → %s", msg["type"], dest.name)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public emit functions
# ---------------------------------------------------------------------------

def emit_issue_created(
    *,
    issue_number: int,
    issue_url: str,
    title: str,
    labels: list[str],
    triggered_at: str | None = None,
    db_path: Path | None = None,
) -> str | None:
    """
    Emit a ``wos_issue_created`` typed inbox event.

    Called by the delta poller when it detects a new GitHub issue with the
    ``wos:uow`` label that has not yet been seen (i.e. not in event_log).

    Deduplication key: ``str(issue_number)`` — one emission per issue.
    A second call for the same issue_number is a no-op (returns None).

    Args:
        issue_number: GitHub issue number.
        issue_url: Full GitHub issue URL.
        title: Issue title.
        labels: List of label names on the issue.
        triggered_at: ISO-8601 timestamp of when the issue was detected.
                      Defaults to now.
        db_path: Override registry DB path (tests).

    Returns:
        event_id (str) on first emission, None if the event is a duplicate.
    """
    dedup_key = str(issue_number)
    triggered_at = triggered_at or datetime.now(timezone.utc).isoformat()

    event_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "issue_number": issue_number,
        "issue_url": issue_url,
        "title": title,
        "labels": labels,
        "triggered_at": triggered_at,
    }

    inserted = _record_event(
        event_id=event_id,
        event_type="wos_issue_created",
        payload=payload,
        dedup_key=dedup_key,
        db_path=db_path,
    )

    if not inserted:
        log.debug("emit_issue_created: duplicate issue #%d — skipping inbox write", issue_number)
        return None

    msg: dict[str, Any] = {
        "id": event_id,
        "source": "system",
        "type": "wos_issue_created",
        "chat_id": _admin_chat_id(),
        "event_id": event_id,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "title": title,
        "labels": labels,
        "triggered_at": triggered_at,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        _write_inbox_message(msg)
    except Exception as exc:
        log.warning(
            "emit_issue_created: inbox write failed for issue #%d: %s",
            issue_number, exc,
        )
        return None

    log.info("emit_issue_created: emitted event %s for issue #%d", event_id, issue_number)
    return event_id


def emit_uow_completed(
    *,
    uow_id: str,
    outcome: str,
    register: str,
    output_ref: str | None = None,
    triggered_at: str | None = None,
    db_path: Path | None = None,
) -> str | None:
    """
    Emit a ``wos_uow_completed`` typed inbox event.

    Called when a UoW transitions to done or failed state. Signals downstream
    components (capacity tracker, germinator, etc.) that a slot has freed.

    Deduplication key: ``uow_id`` — one emission per UoW terminal transition.

    Args:
        uow_id: The UoW identifier.
        outcome: Terminal outcome — ``"done"`` or ``"failed"``.
        register: Register the UoW was assigned to.
        output_ref: Path to the UoW output artifact (optional).
        triggered_at: ISO-8601 timestamp of the transition. Defaults to now.
        db_path: Override registry DB path (tests).

    Returns:
        event_id (str) on first emission, None if duplicate.
    """
    dedup_key = uow_id
    triggered_at = triggered_at or datetime.now(timezone.utc).isoformat()

    event_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "uow_id": uow_id,
        "outcome": outcome,
        "register": register,
        "output_ref": output_ref,
        "triggered_at": triggered_at,
    }

    inserted = _record_event(
        event_id=event_id,
        event_type="wos_uow_completed",
        payload=payload,
        dedup_key=dedup_key,
        db_path=db_path,
    )

    if not inserted:
        log.debug("emit_uow_completed: duplicate UoW %r — skipping inbox write", uow_id)
        return None

    msg: dict[str, Any] = {
        "id": event_id,
        "source": "system",
        "type": "wos_uow_completed",
        "chat_id": _admin_chat_id(),
        "event_id": event_id,
        "uow_id": uow_id,
        "outcome": outcome,
        "register": register,
        "output_ref": output_ref,
        "triggered_at": triggered_at,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        _write_inbox_message(msg)
    except Exception as exc:
        log.warning(
            "emit_uow_completed: inbox write failed for UoW %r: %s",
            uow_id, exc,
        )
        return None

    log.info("emit_uow_completed: emitted event %s for UoW %r (outcome=%s)", event_id, uow_id, outcome)
    return event_id


def emit_capacity_available(
    *,
    freed_uow_id: str,
    freed_at: str | None = None,
    current_active_count: int,
    max_parallel: int,
    db_path: Path | None = None,
) -> str | None:
    """
    Emit a ``wos_capacity_available`` typed inbox event.

    Called when executor capacity becomes available (active UoWs < max_parallel).
    Signals the germinator that it can promote pending UoWs to ready-for-executor.

    Deduplication key: ``freed_uow_id`` — one capacity event per UoW that freed
    a slot. This prevents duplicate capacity signals when the same UoW finishes
    and the poller runs multiple times before the dispatcher processes the event.

    Args:
        freed_uow_id: The UoW whose completion freed the slot.
        freed_at: ISO-8601 timestamp when capacity became available. Defaults to now.
        current_active_count: Number of active UoWs after the slot was freed.
        max_parallel: Maximum parallel UoWs allowed by wos-config.json.
        db_path: Override registry DB path (tests).

    Returns:
        event_id (str) on first emission, None if duplicate.
    """
    dedup_key = freed_uow_id
    freed_at = freed_at or datetime.now(timezone.utc).isoformat()

    event_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "freed_uow_id": freed_uow_id,
        "freed_at": freed_at,
        "current_active_count": current_active_count,
        "max_parallel": max_parallel,
    }

    inserted = _record_event(
        event_id=event_id,
        event_type="wos_capacity_available",
        payload=payload,
        dedup_key=dedup_key,
        db_path=db_path,
    )

    if not inserted:
        log.debug(
            "emit_capacity_available: duplicate freed_uow_id %r — skipping inbox write",
            freed_uow_id,
        )
        return None

    msg: dict[str, Any] = {
        "id": event_id,
        "source": "system",
        "type": "wos_capacity_available",
        "chat_id": _admin_chat_id(),
        "event_id": event_id,
        "freed_uow_id": freed_uow_id,
        "freed_at": freed_at,
        "current_active_count": current_active_count,
        "max_parallel": max_parallel,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        _write_inbox_message(msg)
    except Exception as exc:
        log.warning(
            "emit_capacity_available: inbox write failed for freed_uow_id %r: %s",
            freed_uow_id, exc,
        )
        return None

    log.info(
        "emit_capacity_available: emitted event %s (freed=%r active=%d/%d)",
        event_id, freed_uow_id, current_active_count, max_parallel,
    )
    return event_id
