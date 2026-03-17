"""
Tests for write_result call deduplication.

Verifies that calling write_result twice with the same task_id is a no-op on
the second call — the inbox receives exactly one file regardless of how many
times the handler is invoked.

Root cause being defended against: CC 2.1.77 injects "Stop hook feedback:
[hook]: No stderr output" into subagent conversations even when the
SubagentStop hook returns {"suppressOutput": true}. Subagents that misread
this platform noise sometimes call write_result again, producing duplicate
inbox entries.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

# Ensure src/mcp is on sys.path so that `reliability` (a sibling module) can
# be resolved when inbox_server is imported via the `src.mcp.inbox_server`
# dotted path.  The root conftest adds `src/` but not `src/mcp/`, so we add
# the latter here; this is a no-op if the path is already present.
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

# Pre-load the module so that unittest.mock can resolve "src.mcp.inbox_server"
# as an attribute of the `src.mcp` package before patch.multiple opens.
import src.mcp.inbox_server  # noqa: F401


class TestWriteResultDedup:
    """Tests for write_result call deduplication (task_id-level idempotency)."""

    @pytest.fixture
    def dirs(self, temp_messages_dir: Path):
        """Return inbox and dedup directories inside a temp base."""
        inbox = temp_messages_dir / "inbox"
        dedup = temp_messages_dir / "write-result-dedup"
        task_replied = temp_messages_dir / "task-replied"
        sent_replies = temp_messages_dir / "sent-replies"
        for d in [inbox, dedup, task_replied, sent_replies]:
            d.mkdir(parents=True, exist_ok=True)
        return {"inbox": inbox, "dedup": dedup, "task_replied": task_replied, "sent_replies": sent_replies}

    def _run(self, args: dict, dirs: dict) -> list:
        """Run handle_write_result with patched directories and no async side effects."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=dirs["inbox"],
            WRITE_RESULT_DEDUP_DIR=dirs["dedup"],
            TASK_REPLIED_DIR=dirs["task_replied"],
            SENT_REPLIES_DIR=dirs["sent_replies"],
            _DEBUG_MODE=False,
            _DEBUG_RESOLVED=True,
            # Suppress the asyncio.create_task(_notify_wire_server()) call.
            # Must be an AsyncMock so create_task receives a coroutine.
            _notify_wire_server=AsyncMock(),
        ):
            # Stub out session_store.session_end so we don't need a real DB
            import src.mcp.inbox_server as srv
            original_session_store = srv._session_store

            class _FakeSessionStore:
                def session_end(self, **kwargs):
                    pass

            srv._session_store = _FakeSessionStore()
            try:
                from src.mcp.inbox_server import handle_write_result
                return asyncio.run(handle_write_result(args))
            finally:
                srv._session_store = original_session_store

    # ------------------------------------------------------------------
    # Happy path — first call succeeds
    # ------------------------------------------------------------------

    def test_first_call_writes_inbox_file(self, dirs):
        """First write_result call creates exactly one inbox file."""
        result = self._run(
            {"task_id": "task-abc", "chat_id": 12345, "text": "All done."},
            dirs,
        )

        assert "queued" in result[0].text.lower()
        files = list(dirs["inbox"].glob("*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["task_id"] == "task-abc"

    # ------------------------------------------------------------------
    # Dedup — second call is rejected
    # ------------------------------------------------------------------

    def test_second_call_same_task_id_is_rejected(self, dirs):
        """Calling write_result twice with the same task_id produces only one inbox file."""
        args = {"task_id": "task-dup", "chat_id": 12345, "text": "Result text."}

        first = self._run(args, dirs)
        second = self._run(args, dirs)

        # First call succeeds
        assert "queued" in first[0].text.lower()

        # Second call is rejected with an informative error
        assert "already recorded" in second[0].text.lower() or "duplicate" in second[0].text.lower()

        # Inbox contains exactly one file
        files = list(dirs["inbox"].glob("*.json"))
        assert len(files) == 1, f"Expected 1 inbox file, got {len(files)}"

    def test_second_call_response_mentions_platform_noise(self, dirs):
        """The rejection message explains that the CC Stop hook message is platform noise."""
        args = {"task_id": "task-noise", "chat_id": 12345, "text": "Done."}
        self._run(args, dirs)
        second = self._run(args, dirs)

        # The response should guide the model not to retry
        assert "No stderr output" in second[0].text or "platform noise" in second[0].text.lower()

    def test_19_duplicate_calls_produce_one_inbox_file(self, dirs):
        """Worst-case scenario: 19 duplicate write_result calls → exactly 1 inbox file."""
        args = {"task_id": "task-flood", "chat_id": 99999, "text": "Flooding..."}

        results = [self._run(args, dirs) for _ in range(19)]

        # First call accepted, rest rejected
        assert "queued" in results[0][0].text.lower()
        for r in results[1:]:
            assert "already recorded" in r[0].text.lower() or "duplicate" in r[0].text.lower()

        files = list(dirs["inbox"].glob("*.json"))
        assert len(files) == 1, f"Expected 1 inbox file, got {len(files)}"

    # ------------------------------------------------------------------
    # Different task_ids are independent
    # ------------------------------------------------------------------

    def test_different_task_ids_each_write_own_file(self, dirs):
        """Two distinct task_ids both succeed independently."""
        self._run({"task_id": "task-a", "chat_id": 1, "text": "Result A."}, dirs)
        self._run({"task_id": "task-b", "chat_id": 1, "text": "Result B."}, dirs)

        files = list(dirs["inbox"].glob("*.json"))
        assert len(files) == 2

        task_ids = {json.loads(f.read_text())["task_id"] for f in files}
        assert task_ids == {"task-a", "task-b"}

    # ------------------------------------------------------------------
    # Dedup expires after the window
    # ------------------------------------------------------------------

    def test_expired_dedup_marker_allows_retry(self, dirs):
        """After the dedup window expires, the same task_id can write_result again."""
        args = {"task_id": "task-expired", "chat_id": 1, "text": "Old result."}

        # First call
        first = self._run(args, dirs)
        assert "queued" in first[0].text.lower()

        # Manually back-date the dedup marker to force expiry
        import src.mcp.inbox_server as srv
        key = srv._write_result_dedup_key("task-expired")
        marker = dirs["dedup"] / key
        old_time = time.time() - srv._WRITE_RESULT_DEDUP_WINDOW_SECS - 1
        marker.write_text(str(old_time))

        # Second call should now succeed (marker expired)
        second = self._run({"task_id": "task-expired", "chat_id": 1, "text": "New result."}, dirs)
        assert "queued" in second[0].text.lower()

        # Two files now in the inbox
        files = list(dirs["inbox"].glob("*.json"))
        assert len(files) == 2

    # ------------------------------------------------------------------
    # Dedup marker persistence (helper functions unit tests)
    # ------------------------------------------------------------------

    def test_was_write_result_called_returns_false_before_any_call(self, dirs):
        """_was_write_result_called returns False when no marker exists."""
        with patch("src.mcp.inbox_server.WRITE_RESULT_DEDUP_DIR", dirs["dedup"]):
            from src.mcp.inbox_server import _was_write_result_called
            assert _was_write_result_called("task-fresh") is False

    def test_record_and_check_write_result_called(self, dirs):
        """_record_write_result + _was_write_result_called round-trip."""
        with patch("src.mcp.inbox_server.WRITE_RESULT_DEDUP_DIR", dirs["dedup"]):
            from src.mcp.inbox_server import _record_write_result, _was_write_result_called
            assert _was_write_result_called("task-round-trip") is False
            _record_write_result("task-round-trip")
            assert _was_write_result_called("task-round-trip") is True
