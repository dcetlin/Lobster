"""
Tests for the thread registry (issue #19, phase 1 — deterministic thread_id
stamping).

All tests are pure unit tests against thread_registry.py — no MCP, Telegram,
or network calls. Mirrors the dependency-light, standalone-importable
discipline of message_types.py / lobster_meta.py (issue #1536 precedent).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# thread_registry.py lives in src/mcp/ alongside inbox_server.py.
# Add that directory to sys.path so we can import it directly.
_MCP_DIR = str(Path(__file__).resolve().parents[3] / "src" / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

from thread_registry import (  # noqa: E402
    ThreadRegistry,
    parent_lookup_keys,
    self_registration_keys,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(**overrides) -> dict:
    base = {
        "id": "m1",
        "type": "text",
        "source": "telegram",
        "chat_id": 12345,
        "text": "hello",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# parent_lookup_keys — pure precedence logic
# ---------------------------------------------------------------------------


class TestParentLookupKeys:
    def test_no_signals_returns_empty(self) -> None:
        assert parent_lookup_keys(_msg()) == []

    def test_reply_to_block_produces_key(self) -> None:
        msg = _msg(reply_to={"reply_to_message_id": 999})
        assert parent_lookup_keys(msg) == ["reply_to:12345:999"]

    def test_top_level_reply_to_message_id_produces_key(self) -> None:
        msg = _msg(reply_to_message_id=999)
        assert parent_lookup_keys(msg) == ["reply_to:12345:999"]

    def test_thread_root_message_id_fallback(self) -> None:
        # lobster_bot.py's group-engagement field, same semantics as reply_to.
        msg = _msg(thread_root_message_id=888)
        assert parent_lookup_keys(msg) == ["reply_to:12345:888"]

    def test_thread_ts_produces_key(self) -> None:
        msg = _msg(source="slack", thread_ts="1700000000.000100")
        assert parent_lookup_keys(msg) == ["thread_ts:12345:1700000000.000100"]

    def test_task_id_produces_key(self) -> None:
        msg = _msg(task_id="task-abc")
        assert parent_lookup_keys(msg) == ["task_id:task-abc"]

    def test_precedence_order_reply_before_thread_ts_before_task_id(self) -> None:
        msg = _msg(
            reply_to={"reply_to_message_id": 999},
            thread_ts="1700000000.000100",
            task_id="task-abc",
        )
        assert parent_lookup_keys(msg) == [
            "reply_to:12345:999",
            "thread_ts:12345:1700000000.000100",
            "task_id:task-abc",
        ]

    def test_chat_id_namespaces_reply_key(self) -> None:
        # Same Telegram message_id in two different chats must not collide.
        msg_a = _msg(chat_id=1, reply_to_message_id=42)
        msg_b = _msg(chat_id=2, reply_to_message_id=42)
        assert parent_lookup_keys(msg_a) != parent_lookup_keys(msg_b)


class TestSelfRegistrationKeys:
    def test_registers_own_telegram_message_id(self) -> None:
        msg = _msg(telegram_message_id=555)
        assert self_registration_keys(msg) == ["reply_to:12345:555"]

    def test_registers_own_thread_ts_and_task_id(self) -> None:
        msg = _msg(thread_ts="ts-1", task_id="task-1")
        assert self_registration_keys(msg) == ["thread_ts:12345:ts-1", "task_id:task-1"]

    def test_no_identifiers_returns_empty(self) -> None:
        assert self_registration_keys(_msg()) == []


# ---------------------------------------------------------------------------
# ThreadRegistry — stateful stamping behavior
# ---------------------------------------------------------------------------


class TestThreadRegistryStamp:
    def test_first_message_mints_new_thread_id(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        thread_id = registry.stamp(_msg(id="m1"), "m1")
        assert thread_id
        # A thread file was written for it.
        assert (tmp_path / f"{thread_id}.json").exists()

    def test_two_unrelated_messages_get_different_thread_ids(self, tmp_path: Path) -> None:
        # No chat+recency fallback (Tier 1) in phase 1 — same chat_id, no
        # shared structural signal, must NOT be merged.
        registry = ThreadRegistry(threads_dir=tmp_path)
        t1 = registry.stamp(_msg(id="m1"), "m1")
        t2 = registry.stamp(_msg(id="m2"), "m2")
        assert t1 != t2

    def test_explicit_reply_inherits_parent_thread_id(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        parent = _msg(id="m1", telegram_message_id=100)
        t1 = registry.stamp(parent, "m1")

        child = _msg(id="m2", reply_to={"reply_to_message_id": 100})
        t2 = registry.stamp(child, "m2")

        assert t2 == t1

    def test_slack_thread_ts_inherits_parent_thread_id(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        parent = _msg(id="m1", source="slack", thread_ts="ts-1")
        t1 = registry.stamp(parent, "m1")

        child = _msg(id="m2", source="slack", thread_ts="ts-1")
        t2 = registry.stamp(child, "m2")

        assert t2 == t1

    def test_shared_task_id_inherits_parent_thread_id(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        first = _msg(id="m1", type="subagent_result", task_id="task-xyz")
        t1 = registry.stamp(first, "m1")

        second = _msg(id="m2", type="subagent_result", task_id="task-xyz")
        t2 = registry.stamp(second, "m2")

        assert t2 == t1

    def test_reply_to_wins_over_task_id_when_both_present(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        reply_parent = _msg(id="m1", telegram_message_id=100)
        t_reply = registry.stamp(reply_parent, "m1")

        task_parent = _msg(id="m2", task_id="task-xyz")
        t_task = registry.stamp(task_parent, "m2")
        assert t_task != t_reply  # sanity: distinct threads so far

        child = _msg(
            id="m3",
            reply_to={"reply_to_message_id": 100},
            task_id="task-xyz",
        )
        t_child = registry.stamp(child, "m3")

        assert t_child == t_reply
        assert t_child != t_task

    def test_stamping_same_message_twice_does_not_fork_thread(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        msg = _msg(id="m1", telegram_message_id=100)
        t1 = registry.stamp(msg, "m1")
        t2 = registry.stamp(msg, "m1")
        assert t1 == t2

        data = json.loads((tmp_path / f"{t1}.json").read_text())
        assert data["member_message_ids"].count("m1") == 1

    def test_thread_file_records_members_and_signals(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        parent = _msg(id="m1", telegram_message_id=100)
        thread_id = registry.stamp(parent, "m1")

        child = _msg(id="m2", reply_to={"reply_to_message_id": 100})
        registry.stamp(child, "m2")

        data = json.loads((tmp_path / f"{thread_id}.json").read_text())
        assert data["id"] == thread_id
        assert data["member_message_ids"] == ["m1", "m2"]
        assert "reply_to" in data["signals_used"]

    def test_registry_state_persists_across_instances(self, tmp_path: Path) -> None:
        registry_a = ThreadRegistry(threads_dir=tmp_path)
        parent = _msg(id="m1", telegram_message_id=100)
        t1 = registry_a.stamp(parent, "m1")

        registry_b = ThreadRegistry(threads_dir=tmp_path)
        child = _msg(id="m2", reply_to={"reply_to_message_id": 100})
        t2 = registry_b.stamp(child, "m2")

        assert t1 == t2

    def test_lookup_returns_none_for_empty_keys(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        assert registry.lookup([]) is None

    def test_lookup_returns_none_for_unknown_keys(self, tmp_path: Path) -> None:
        registry = ThreadRegistry(threads_dir=tmp_path)
        assert registry.lookup(["reply_to:1:2"]) is None

    def test_creates_threads_dir_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "nested" / "threads"
        registry = ThreadRegistry(threads_dir=nested)
        assert nested.is_dir()
        thread_id = registry.stamp(_msg(id="m1"), "m1")
        assert thread_id
