"""
Thread registry — issue #19, Phase 1: deterministic thread_id stamping.

Groups messages into conversational threads using only existing,
deterministic structural signals — in the fixed precedence order defined by
the issue #19 design comment (Tier 0):

  1. explicit reply    (reply_to.reply_to_message_id / Telegram
                         reply_to_message_id, or the older
                         `thread_root_message_id` field lobster_bot.py
                         already computes for the same purpose)
  2. platform thread    (Slack `thread_ts`)
  3. shared task_id     (a subagent dispatch and its write_result/
                         write_progress calls)

No chat+recency fallback (Tier 1) and no semantic matching (Tier 2) — those
are Phases 2-3 and are deliberately not implemented here. This module is
stamp-only: nothing reads or branches on `thread_id` yet.

Follows the additive-stamping precedent from issue #1536
(message_types.MessageRelationship / lobster_meta.classify_relationship):
a new field is written by the callers of this module; existing readers are
untouched.

Storage
-------
One JSON file per thread in ``~/lobster-workspace/data/threads/<thread_id>.json``
(mirrors the ``meta-threads/*.json`` convention already used by
``scripts/meta_threads.py``), plus a single ``index.json`` in the same
directory mapping structural signal keys (e.g.
``"reply_to:<chat_id>:<telegram_message_id>"``) to `thread_id`, so a parent
lookup is O(1) instead of a scan over every thread file.

This module depends only on stdlib + src.utils.fs.atomic_write_json, so it
can be imported and tested in isolation (same dependency discipline as
message_types.py and lobster_meta.py).
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from src.utils.fs import atomic_write_json  # noqa: E402

log = logging.getLogger("lobster-mcp")

DEFAULT_THREADS_DIR = Path.home() / "lobster-workspace" / "data" / "threads"
_INDEX_FILENAME = "index.json"


# ---------------------------------------------------------------------------
# Pure signal-key derivation (no I/O)
# ---------------------------------------------------------------------------


def _reply_target_id(msg: dict[str, Any]) -> Any | None:
    """Return the message ID this message is an explicit reply to, if any.

    Checks, in order: the structured ``reply_to`` block (as written by
    inbox_write producers and rendered by wait_for_messages),
    ``reply_to_message_id`` at the top level, and lobster_bot.py's
    ``thread_root_message_id`` (computed the same way — the Telegram ID of
    the message being replied to — for group-chat engagement tracking).
    """
    reply_to = msg.get("reply_to") or {}
    if isinstance(reply_to, dict):
        reply_id = reply_to.get("reply_to_message_id") or reply_to.get("message_id")
        if reply_id is not None:
            return reply_id
    top_level = msg.get("reply_to_message_id")
    if top_level is not None:
        return top_level
    return msg.get("thread_root_message_id")


def parent_lookup_keys(msg: dict[str, Any]) -> list[str]:
    """Return signal keys, precedence order first, for finding *msg*'s parent thread.

    Only structural signals already present on the message are considered.
    An empty list means no Tier-0 signal is available — the caller mints a
    new thread_id.
    """
    chat_id = msg.get("chat_id")
    keys: list[str] = []

    reply_id = _reply_target_id(msg)
    if reply_id is not None:
        keys.append(f"reply_to:{chat_id}:{reply_id}")

    thread_ts = msg.get("thread_ts")
    if thread_ts:
        keys.append(f"thread_ts:{chat_id}:{thread_ts}")

    task_id = msg.get("task_id")
    if task_id:
        keys.append(f"task_id:{task_id}")

    return keys


def self_registration_keys(msg: dict[str, Any]) -> list[str]:
    """Return signal keys a future reply could use to find *msg* as its parent.

    Registered regardless of whether *msg* itself resolved a parent — every
    stamped message becomes a potential parent for the next hop in its
    chain.
    """
    chat_id = msg.get("chat_id")
    keys: list[str] = []

    tg_id = msg.get("telegram_message_id")
    if tg_id is not None:
        keys.append(f"reply_to:{chat_id}:{tg_id}")

    thread_ts = msg.get("thread_ts")
    if thread_ts:
        keys.append(f"thread_ts:{chat_id}:{thread_ts}")

    task_id = msg.get("task_id")
    if task_id:
        keys.append(f"task_id:{task_id}")

    return keys


# ---------------------------------------------------------------------------
# Stateful registry (JSON-backed)
# ---------------------------------------------------------------------------


class ThreadRegistry:
    """Lightweight JSON-backed registry mapping structural signals to thread_id.

    Not process-safe against true concurrent writers (read-modify-write on
    index.json is not locked) — a race between two simultaneous claims that
    both resolve the same brand-new parent key could, in the rare worst
    case, mint two thread_ids for what should be one thread. This mirrors
    the Phase-1 design note ("ship, then do a data-quality pass") and is
    consistent with the additive/no-reader-yet safety bar: at worst a
    thread splits into two, never a crash or data loss, and no existing
    behavior changes either way. index.json itself is always written via
    atomic_write_json (temp file + rename), so a race can lose an update,
    never corrupt the file.
    """

    def __init__(self, threads_dir: Path | None = None) -> None:
        self.threads_dir = threads_dir or DEFAULT_THREADS_DIR
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.threads_dir / _INDEX_FILENAME

    def _load_index(self) -> dict[str, str]:
        try:
            return json.loads(self.index_path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(f"thread_registry: index.json unreadable, treating as empty ({exc})")
            return {}

    def _save_index(self, index: dict[str, str]) -> None:
        atomic_write_json(self.index_path, index)

    def lookup(self, keys: list[str]) -> str | None:
        """Return the first thread_id matched by *keys*, or None."""
        if not keys:
            return None
        index = self._load_index()
        for key in keys:
            thread_id = index.get(key)
            if thread_id:
                return thread_id
        return None

    def stamp(self, msg: dict[str, Any], message_id: str) -> str:
        """Derive, register, and return the thread_id for *msg*.

        Idempotent: calling this more than once for the same *message_id*
        (e.g. after stale-processing recovery re-claims it) resolves to the
        same thread_id and does not fork the thread or duplicate its member
        entry. This idempotency check is keyed on message_id specifically
        (a dedicated ``msgid:<message_id>`` index entry) rather than on
        *msg*'s structural signal keys — those (e.g. task_id) can be shared
        with other, unrelated messages, and reusing them for the
        idempotency check would let a sibling's registration incorrectly
        short-circuit this message's own precedence-ordered parent lookup.

        A message seen for the first time resolves its thread_id via the
        parent lookup (explicit reply -> thread_ts -> task_id, first match
        wins) and, failing that, mints a new thread_id.
        """
        msgid_key = f"msgid:{message_id}"
        thread_id = self.lookup([msgid_key])
        signals_used: list[str] = []

        if thread_id is None:
            parent_keys = parent_lookup_keys(msg)
            thread_id = self.lookup(parent_keys)
            if thread_id is not None:
                # Record which tier actually matched (first key that hit).
                index = self._load_index()
                for key in parent_keys:
                    if index.get(key) == thread_id:
                        signals_used.append(key.split(":", 1)[0])
                        break
            else:
                thread_id = str(uuid.uuid4())
            own_keys = self_registration_keys(msg)
            self._register_keys([msgid_key, *own_keys, *parent_keys], thread_id)

        self._append_member(thread_id, message_id, msg, signals_used)
        return thread_id

    def _register_keys(self, keys: list[str], thread_id: str) -> None:
        if not keys:
            return
        index = self._load_index()
        changed = False
        for key in keys:
            if index.get(key) != thread_id:
                index[key] = thread_id
                changed = True
        if changed:
            self._save_index(index)

    def _append_member(
        self,
        thread_id: str,
        message_id: str,
        msg: dict[str, Any],
        signals_used: list[str],
    ) -> None:
        path = self.threads_dir / f"{thread_id}.json"
        now = datetime.now(timezone.utc).isoformat()
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {
                "id": thread_id,
                "chat_id": msg.get("chat_id"),
                "source": msg.get("source"),
                "created_at": now,
                "member_message_ids": [],
                "signals_used": [],
            }
        data["last_active_at"] = now
        if message_id not in data["member_message_ids"]:
            data["member_message_ids"].append(message_id)
        for signal in signals_used:
            if signal not in data["signals_used"]:
                data["signals_used"].append(signal)
        atomic_write_json(path, data)
