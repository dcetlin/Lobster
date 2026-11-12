"""
Tests for Message State Machine and Retry Logic

Tests mark_processing, mark_processed (updated), mark_failed,
stale recovery, and retry recovery.
"""

import json
import os
import time
import pytest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone
from reliability import ValidationError


class TestMarkProcessing:
    """Tests for mark_processing tool."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        return inbox, processing

    def test_moves_file_to_processing(self, setup_dirs, message_generator):
        """Test that message file is moved from inbox to processing."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processing

            result = asyncio.run(handle_mark_processing({"message_id": msg_id}))

            assert "claimed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (processing / f"{msg_id}.json").exists()

    def test_not_found_returns_error(self, setup_dirs):
        """Test that non-existent message returns error."""
        inbox, processing = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processing

            result = asyncio.run(
                handle_mark_processing({"message_id": "nonexistent_id"})
            )

            assert "not found" in result[0].text.lower()

    def test_requires_message_id(self, setup_dirs):
        """Test that message_id is required — raises ValidationError when absent."""
        inbox, processing = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processing

            with pytest.raises(ValidationError):
                asyncio.run(handle_mark_processing({}))

    def test_refuses_local_claude_source(self, setup_dirs, message_generator):
        """mark_processing must refuse source='local-claude' messages (issue #1531):
        claim_and_ack is the sole claim path for the agent channel. The message
        must be left untouched in inbox/ — no partial claim, no filesystem move."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message(source="local-claude")
        msg["request_id"] = msg["id"]
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processing

            result = asyncio.run(handle_mark_processing({"message_id": msg_id}))

            assert "not supported" in result[0].text.lower()
            assert "claim_and_ack" in result[0].text
            # Left untouched in inbox/ — no partial claim, no move.
            assert (inbox / f"{msg_id}.json").exists()
            assert not (processing / f"{msg_id}.json").exists()

    def test_refuses_local_claude_source_case_insensitive(self, setup_dirs, message_generator):
        """The deprecation gate must catch a mixed-case source field (e.g.
        'Local-Claude') exactly like claim_and_ack's own fail-closed source
        checks do (issue #1532 review fix: align case handling between the two
        gates so a case mismatch can't slip a local-claude message through
        mark_processing, which has no ack step for the agent channel)."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message(source="Local-Claude")
        msg["request_id"] = msg["id"]
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processing

            result = asyncio.run(handle_mark_processing({"message_id": msg_id}))

            assert "not supported" in result[0].text.lower()
            assert "claim_and_ack" in result[0].text
            # Left untouched in inbox/ — no partial claim, no move.
            assert (inbox / f"{msg_id}.json").exists()
            assert not (processing / f"{msg_id}.json").exists()

    def test_still_claims_non_local_claude_sources(self, setup_dirs, message_generator):
        """Regression guard: the source-branch gate must not widen to other
        sources — mark_processing remains fully supported for human channels."""
        inbox, processing = setup_dirs

        for source in ("telegram", "slack", "sms"):
            msg = message_generator.generate_text_message(source=source)
            msg_id = msg["id"]
            (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

            with patch.multiple(
                "src.mcp.inbox_server",
                INBOX_DIR=inbox,
                PROCESSING_DIR=processing,
            ):
                import asyncio
                from src.mcp.inbox_server import handle_mark_processing

                result = asyncio.run(handle_mark_processing({"message_id": msg_id}))

                assert "claimed" in result[0].text.lower()
                assert not (inbox / f"{msg_id}.json").exists()
                assert (processing / f"{msg_id}.json").exists()


class TestMarkProcessedUpdated:
    """Tests for updated mark_processed that checks processing/ first."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        processed = temp_messages_dir / "processed"
        return inbox, processing, processed

    def test_finds_in_processing_first(self, setup_dirs, message_generator):
        """Test that mark_processed checks processing/ before inbox/."""
        inbox, processing, processed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        # Put the message in processing/ (not inbox/)
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "processed" in result[0].text.lower()
            assert not (processing / f"{msg_id}.json").exists()
            assert (processed / f"{msg_id}.json").exists()

    def test_falls_back_to_inbox(self, setup_dirs, message_generator):
        """Test that mark_processed falls back to inbox/ if not in processing/."""
        inbox, processing, processed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(handle_mark_processed({"message_id": msg_id}))

            assert "processed" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (processed / f"{msg_id}.json").exists()

    def test_not_found_returns_error(self, setup_dirs):
        """Test that non-existent message returns error."""
        inbox, processing, processed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            PROCESSED_DIR=processed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_processed

            result = asyncio.run(
                handle_mark_processed({"message_id": "nonexistent_id"})
            )

            assert "not found" in result[0].text.lower()


class TestMarkFailed:
    """Tests for mark_failed tool."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        failed = temp_messages_dir / "failed"
        return inbox, processing, failed

    def test_moves_to_failed_with_retry_metadata(self, setup_dirs, message_generator):
        """Test that message is moved to failed/ with retry metadata."""
        inbox, processing, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "test error",
            }))

            assert "retry" in result[0].text.lower()
            assert not (processing / f"{msg_id}.json").exists()
            assert (failed / f"{msg_id}.json").exists()

            # Check retry metadata
            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            assert failed_msg["_retry_count"] == 1
            assert failed_msg["_last_error"] == "test error"
            assert "_retry_at" in failed_msg
            assert "_last_failed_at" in failed_msg

    def test_increments_retry_count(self, setup_dirs, message_generator):
        """Test that retry count is incremented on subsequent failures."""
        inbox, processing, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 1
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "another error",
            }))

            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            assert failed_msg["_retry_count"] == 2

    def test_permanent_failure_after_max_retries(self, setup_dirs, message_generator):
        """Test that message is permanently failed after max retries."""
        inbox, processing, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 3  # Already at max
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "final error",
                "max_retries": 3,
            }))

            assert "permanently failed" in result[0].text.lower()
            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            assert failed_msg["_permanently_failed"] is True

    def test_exponential_backoff(self, setup_dirs, message_generator):
        """Test that backoff increases exponentially."""
        inbox, processing, failed = setup_dirs

        # First failure: backoff should be 60s
        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "err",
            }))

            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            now = datetime.now(timezone.utc).timestamp()
            # First retry: 60s backoff
            assert abs(failed_msg["_retry_at"] - (now + 60)) < 5

        # Second failure: backoff should be 120s
        (failed / f"{msg_id}.json").rename(processing / f"{msg_id}.json")

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "err",
            }))

            failed_msg = json.loads((failed / f"{msg_id}.json").read_text())
            now = datetime.now(timezone.utc).timestamp()
            # Second retry: 120s backoff
            assert abs(failed_msg["_retry_at"] - (now + 120)) < 5

    def test_requires_message_id(self, setup_dirs):
        """Test that message_id is required — raises ValidationError when absent."""
        inbox, processing, failed = setup_dirs

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            with pytest.raises(ValidationError):
                asyncio.run(handle_mark_failed({}))

    def test_finds_in_inbox_fallback(self, setup_dirs, message_generator):
        """Test that mark_failed can find messages in inbox/ too."""
        inbox, processing, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_mark_failed

            result = asyncio.run(handle_mark_failed({
                "message_id": msg_id,
                "error": "err",
            }))

            assert "retry" in result[0].text.lower()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (failed / f"{msg_id}.json").exists()


class TestStaleRecovery:
    """Tests for stale processing recovery."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        return inbox, processing

    def test_recovers_stale_messages(self, setup_dirs, message_generator):
        """Test that old messages in processing/ are moved back to inbox/."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        msg_file = processing / f"{msg_id}.json"
        msg_file.write_text(json.dumps(msg))

        # Set mtime to 10 minutes ago
        old_time = time.time() - 600
        os.utime(msg_file, (old_time, old_time))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            assert not (processing / f"{msg_id}.json").exists()
            assert (inbox / f"{msg_id}.json").exists()

    def test_leaves_recent_messages(self, setup_dirs, message_generator):
        """Test that recent messages in processing/ are left alone."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message()
        msg_id = msg["id"]
        (processing / f"{msg_id}.json").write_text(json.dumps(msg))
        # File was just created, so mtime is now

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            # Should still be in processing
            assert (processing / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()

    def test_local_claude_survives_generic_text_timeout(self, setup_dirs, message_generator):
        """A source='local-claude' message past the generic 90s text timeout, but
        still under the agent-channel's own budget, must NOT be recovered — recovering
        it would let a second execution path claim the same request_id while the
        first is still working (protocol spec: no racing writers to one reply slot).
        """
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message(source="local-claude")
        msg["request_id"] = msg["id"]
        msg_id = msg["id"]
        msg_file = processing / f"{msg_id}.json"
        msg_file.write_text(json.dumps(msg))

        # 5 minutes old: past the generic 90s text timeout, under the 600s
        # local-claude timeout.
        old_time = time.time() - 300
        os.utime(msg_file, (old_time, old_time))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            assert (processing / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()

    def test_local_claude_recovered_past_its_own_timeout(self, setup_dirs, message_generator):
        """A source='local-claude' message DOES get recovered once it exceeds the
        agent channel's own (longer) stale-processing budget."""
        inbox, processing = setup_dirs

        msg = message_generator.generate_text_message(source="local-claude")
        msg["request_id"] = msg["id"]
        msg_id = msg["id"]
        msg_file = processing / f"{msg_id}.json"
        msg_file.write_text(json.dumps(msg))

        old_time = time.time() - 700  # past the 600s local-claude timeout
        os.utime(msg_file, (old_time, old_time))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            assert not (processing / f"{msg_id}.json").exists()
            assert (inbox / f"{msg_id}.json").exists()


class TestStaleRecoveryExhaustion:
    """Tests for issue #1535: the agent-channel reaper must close (not abandon)
    an OPEN exchange once its own stale-recovery loop is exhausted.

    Distinct from #1525 (write_progress abandonment sliding timer) — this is
    the reaper's own bounded-retry behavior in _recover_stale_processing().
    """

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        failed = temp_messages_dir / "failed"
        agent_replies = temp_messages_dir / "agent-replies"
        agent_replies.mkdir(parents=True, exist_ok=True)
        return inbox, processing, failed, agent_replies

    def _stale_local_claude_message(self, processing, message_generator, *, recovery_count=0, age_seconds=700):
        """Write a source='local-claude' message into processing/, stale by age_seconds
        and already carrying `recovery_count` prior stale-recovery attempts."""
        msg = message_generator.generate_text_message(source="local-claude")
        msg["request_id"] = msg["id"]
        if recovery_count:
            msg["_stale_recovery_count"] = recovery_count
        msg_id = msg["id"]
        msg_file = processing / f"{msg_id}.json"
        msg_file.write_text(json.dumps(msg))
        old_time = time.time() - age_seconds
        os.utime(msg_file, (old_time, old_time))
        return msg, msg_file

    def test_recovery_count_increments_and_message_still_requeued_below_bound(
        self, setup_dirs, message_generator
    ):
        """Below the bound, a stale local-claude message is still recovered to
        inbox/ (existing behavior) but now carries an incremented
        `_stale_recovery_count` so the reaper can eventually detect exhaustion."""
        inbox, processing, failed, agent_replies = setup_dirs
        msg, msg_file = self._stale_local_claude_message(processing, message_generator, recovery_count=0)
        msg_id = msg["id"]

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
            AGENT_REPLIES_DIR=agent_replies,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            assert not (processing / f"{msg_id}.json").exists()
            recovered = json.loads((inbox / f"{msg_id}.json").read_text())
            assert recovered["_stale_recovery_count"] == 1
            # Exchange must still be OPEN — no terminal reply written yet.
            assert not (agent_replies / f"{msg_id}.json").exists()

    def test_finalizes_and_writes_terminal_reply_after_max_attempts(self, setup_dirs, message_generator):
        """Once a local-claude message has already been recovered
        _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS times, the next stale pass must
        close the exchange with a terminal reply instead of requeuing it again."""
        inbox, processing, failed, agent_replies = setup_dirs
        from src.mcp.inbox_server import _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS

        msg, msg_file = self._stale_local_claude_message(
            processing, message_generator, recovery_count=_LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS
        )
        msg_id = msg["id"]
        request_id = msg["request_id"]

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
            AGENT_REPLIES_DIR=agent_replies,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            # No longer bounces back to inbox/ — the loop is closed.
            assert not (processing / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()

            # Terminal reply closes the exchange for the requester.
            reply_path = agent_replies / f"{request_id}.json"
            assert reply_path.exists()
            reply = json.loads(reply_path.read_text())
            assert reply["error"] is True
            assert reply["error_type"] == "stale_recovery_exhausted"
            assert reply["request_id"] == request_id

    def test_finalized_message_archived_to_failed_dir(self, setup_dirs, message_generator):
        """The exhausted message itself is archived to failed/ (permanently failed),
        mirroring handle_mark_failed's own retry-count-bounded permanent-failure path."""
        inbox, processing, failed, agent_replies = setup_dirs
        from src.mcp.inbox_server import _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS

        msg, msg_file = self._stale_local_claude_message(
            processing, message_generator, recovery_count=_LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS
        )
        msg_id = msg["id"]

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
            AGENT_REPLIES_DIR=agent_replies,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            archived_path = failed / f"{msg_id}.json"
            assert archived_path.exists()
            archived = json.loads(archived_path.read_text())
            assert archived["_permanently_failed"] is True
            assert archived["_stale_recovery_exhausted"] is True

    def test_non_local_claude_source_unaffected_by_bound(self, setup_dirs, message_generator):
        """The bound and its finalization path are scoped to source='local-claude'
        only — a non-agent-channel message stays on the pre-existing unbounded
        recover-to-inbox behavior."""
        inbox, processing, failed, agent_replies = setup_dirs

        msg = message_generator.generate_text_message(source="telegram")
        msg_id = msg["id"]
        msg_file = processing / f"{msg_id}.json"
        msg_file.write_text(json.dumps(msg))
        old_time = time.time() - 600
        os.utime(msg_file, (old_time, old_time))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
            AGENT_REPLIES_DIR=agent_replies,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            assert not (processing / f"{msg_id}.json").exists()
            assert (inbox / f"{msg_id}.json").exists()
            assert not (failed / f"{msg_id}.json").exists()
            # No _stale_recovery_count bookkeeping for non-agent-channel sources.
            assert "_stale_recovery_count" not in json.loads((inbox / f"{msg_id}.json").read_text())

    def test_pre_restart_path_also_bounded_for_local_claude(self, setup_dirs, message_generator):
        """Path 1 (pre-restart recovery) shares the same bound as path 2 (timeout)
        — a message can't dodge exhaustion by always qualifying for the
        pre-restart branch instead of the timeout branch."""
        inbox, processing, failed, agent_replies = setup_dirs
        from src.mcp.inbox_server import _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS

        msg = message_generator.generate_text_message(source="local-claude")
        msg["request_id"] = msg["id"]
        msg["_stale_recovery_count"] = _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS
        # _processing_started_at before "server start" triggers path 1.
        msg["_processing_started_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        msg_id = msg["id"]
        request_id = msg["request_id"]
        msg_file = processing / f"{msg_id}.json"
        msg_file.write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
            AGENT_REPLIES_DIR=agent_replies,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            assert not (processing / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()
            assert (agent_replies / f"{request_id}.json").exists()
            assert (failed / f"{msg_id}.json").exists()

    def test_fresh_claim_not_finalized_despite_exhausted_count(self, setup_dirs, message_generator):
        """Regression test for issue #1543 (fast-follow to #1541/#1535).

        A message can legitimately reach `_stale_recovery_count >=
        _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS` through past stale cycles
        and then be freshly reclaimed via `claim_and_ack`, which resets
        `_processing_started_at` on every fresh claim but never resets
        `_stale_recovery_count`. Gating finalization on the count alone
        (without checking whether the *current* claim is stale) would close
        out and archive a message while its 4th, fully live worker is still
        running — dropping that worker's eventual successful reply. The
        reaper must leave a fresh, non-stale claim alone regardless of its
        historical count.
        """
        inbox, processing, failed, agent_replies = setup_dirs
        from src.mcp.inbox_server import _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS

        msg = message_generator.generate_text_message(source="local-claude")
        msg["request_id"] = msg["id"]
        msg["_stale_recovery_count"] = _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS
        # Simulates claim_and_ack's fresh claim: _processing_started_at reset
        # to "now" (well within the 600s local-claude timeout), while
        # _stale_recovery_count is carried over from prior stale cycles.
        msg["_processing_started_at"] = datetime.now(timezone.utc).isoformat()
        msg_id = msg["id"]
        request_id = msg["request_id"]
        msg_file = processing / f"{msg_id}.json"
        msg_file.write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            FAILED_DIR=failed,
            AGENT_REPLIES_DIR=agent_replies,
        ):
            from src.mcp.inbox_server import _recover_stale_processing

            _recover_stale_processing()

            # Fresh, live claim — must be left alone, not finalized or requeued.
            assert (processing / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()
            assert not (failed / f"{msg_id}.json").exists()
            assert not (agent_replies / f"{request_id}.json").exists()

            # On-disk message must be unmutated — no premature failure markers.
            untouched = json.loads((processing / f"{msg_id}.json").read_text())
            assert untouched.get("_permanently_failed") is not True
            assert untouched.get("_stale_recovery_exhausted") is not True
            assert untouched["_stale_recovery_count"] == _LOCAL_CLAUDE_STALE_RECOVERY_MAX_ATTEMPTS


class TestRetryRecovery:
    """Tests for retry recovery from failed/."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        failed = temp_messages_dir / "failed"
        return inbox, failed

    def test_recovers_retryable_messages_past_retry_at(self, setup_dirs, message_generator):
        """Test that messages past their retry_at time are moved to inbox/."""
        inbox, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 1
        msg["_retry_at"] = time.time() - 10  # 10 seconds ago
        msg_id = msg["id"]
        (failed / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            FAILED_DIR=failed,
        ):
            from src.mcp.inbox_server import _recover_retryable_messages

            _recover_retryable_messages()

            assert not (failed / f"{msg_id}.json").exists()
            assert (inbox / f"{msg_id}.json").exists()

    def test_leaves_messages_before_retry_at(self, setup_dirs, message_generator):
        """Test that messages before retry_at stay in failed/."""
        inbox, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_retry_count"] = 1
        msg["_retry_at"] = time.time() + 3600  # 1 hour from now
        msg_id = msg["id"]
        (failed / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            FAILED_DIR=failed,
        ):
            from src.mcp.inbox_server import _recover_retryable_messages

            _recover_retryable_messages()

            assert (failed / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()

    def test_permanently_failed_messages_stay(self, setup_dirs, message_generator):
        """Test that permanently failed messages stay in failed/."""
        inbox, failed = setup_dirs

        msg = message_generator.generate_text_message()
        msg["_permanently_failed"] = True
        msg["_retry_count"] = 4
        msg["_retry_at"] = time.time() - 3600  # Long past
        msg_id = msg["id"]
        (failed / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            FAILED_DIR=failed,
        ):
            from src.mcp.inbox_server import _recover_retryable_messages

            _recover_retryable_messages()

            assert (failed / f"{msg_id}.json").exists()
            assert not (inbox / f"{msg_id}.json").exists()

