"""
Tests for src/mcp/agent_channel.py — the extracted agent-channel (source=
"local-claude") protocol mechanics: envelope construction, the single-shot
reply-slot write, the ack write, and the agent_channel.* audit emits.

This module takes its dependencies (paths, the event emitter) as explicit
parameters, so it is testable directly without booting the MCP server or
patching inbox_server.py globals — see the module docstring for why.
"""

import json
import sys
import time
from pathlib import Path

import pytest

# Ensure src/mcp is on sys.path so `agent_channel` resolves the same way
# inbox_server.py imports it (sibling import), and src/ so `src.mcp.agent_channel`
# also works.
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from src.mcp import agent_channel


class _RecordingEmitter:
    """Fake emit_event that records every call for assertion."""

    def __init__(self):
        self.calls = []

    def __call__(self, event_type, payload, severity="info", chat_id=None):
        self.calls.append(
            {"event_type": event_type, "payload": payload, "severity": severity, "chat_id": chat_id}
        )


class _FakeClaimsDB:
    """Duck-typed stand-in for claims.AtomicClaimDB — write_progress only
    calls get_claim_status(), so that's all this fake needs to implement.
    Lets tests set up claim state directly rather than booting a real
    SQLite-backed AtomicClaimDB."""

    def __init__(self, statuses: dict | None = None):
        self._statuses = dict(statuses or {})

    def get_claim_status(self, message_id):
        return self._statuses.get(message_id)

    def set_status(self, message_id, status):
        self._statuses[message_id] = status


class TestSource:
    def test_source_is_local_claude(self):
        assert agent_channel.SOURCE == "local-claude"


class TestWriteReply:
    def test_first_writer_wins_and_creates_slot(self, tmp_path: Path):
        outcome = agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-1",
            text="the answer",
            in_reply_to="msg-1",
        )
        assert outcome.reply_slot_created is True
        assert outcome.request_id == "req-1"

        written = json.loads((tmp_path / "req-1.json").read_text())
        assert written["request_id"] == "req-1"
        assert written["text"] == "the answer"
        assert written["in_reply_to"] == "msg-1"
        assert "ts" in written

    def test_in_reply_to_defaults_to_request_id_when_absent(self, tmp_path: Path):
        agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-2",
            text="hi",
            in_reply_to=None,
        )
        written = json.loads((tmp_path / "req-2.json").read_text())
        assert written["in_reply_to"] == "req-2"

    def test_second_writer_loses_race_slot_untouched(self, tmp_path: Path):
        first = agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-3",
            text="first answer",
            in_reply_to=None,
        )
        assert first.reply_slot_created is True

        second = agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-3",
            text="second answer — should never land",
            in_reply_to=None,
        )
        assert second.reply_slot_created is False

        # Slot content is still the first writer's — never overwritten.
        written = json.loads((tmp_path / "req-3.json").read_text())
        assert written["text"] == "first answer"

    def test_reply_path_is_request_id_json_in_agent_replies_dir(self, tmp_path: Path):
        outcome = agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-4",
            text="x",
            in_reply_to=None,
        )
        assert outcome.reply_path == tmp_path / "req-4.json"

    def test_control_chars_round_trip_to_valid_single_line_json(self, tmp_path: Path):
        """Regression test for the reported agent-channel JSON-parse bug.

        bloom (external agent-channel collaborator) reported that replies
        broke its JSON parser with "unterminated string" / zsh "character
        not in range" errors. Investigation (2026-08-05) found the write
        path already escapes control characters correctly via
        ``json.dumps`` (spec-correct regardless of a leaked control byte in
        `text`) — but the file was pretty-printed (`indent=2`, multi-line),
        which silently breaks any external reader that isn't fully
        JSON-aware (e.g. one expecting one JSON object per line). This test
        locks in the fix: the file must be both (a) valid JSON that
        round-trips a `text` value containing raw control characters, and
        (b) written on a single line, so a naive line-oriented reader can
        never see a truncated/partial object.
        """
        nasty_text = "line one\tindented\nline two\rcarriage\x00null\x1bescape"
        agent_channel.write_reply(
            agent_replies_dir=tmp_path,
            request_id="req-nasty",
            text=nasty_text,
            in_reply_to=None,
        )
        raw = (tmp_path / "req-nasty.json").read_text()

        # Single line: no literal newline byte anywhere in the file content
        # (the escaped \n *inside* the JSON string is fine — this checks
        # there's no raw structural newline outside a string).
        assert "\n" not in raw

        written = json.loads(raw)
        assert written["text"] == nasty_text


class TestEmitReplyAudit:
    def test_won_slot_emits_info_severity(self):
        emitter = _RecordingEmitter()
        agent_channel.emit_reply_audit(
            request_id="req-1", reply_slot_created=True, text_len=42, emit_event=emitter
        )
        assert len(emitter.calls) == 1
        call = emitter.calls[0]
        assert call["event_type"] == "agent_channel.reply"
        assert call["severity"] == "info"
        assert call["payload"] == {
            "request_id": "req-1",
            "reply_slot_created": True,
            "text_len": 42,
        }

    def test_lost_race_emits_warn_severity(self):
        emitter = _RecordingEmitter()
        agent_channel.emit_reply_audit(
            request_id="req-1", reply_slot_created=False, text_len=10, emit_event=emitter
        )
        assert emitter.calls[0]["severity"] == "warn"
        assert emitter.calls[0]["payload"]["reply_slot_created"] is False


class TestFormatReplyResponse:
    def test_won_slot_response_includes_preview_and_mark_info(self):
        text = agent_channel.format_reply_response(
            request_id="req-1", text="hello world", reply_slot_created=True, mark_info=" | message m1 marked processed"
        )
        assert "req-1.json" in text
        assert "message m1 marked processed" in text
        assert "hello world" in text
        assert text.startswith("✅")

    def test_won_slot_response_truncates_long_text(self):
        long_text = "x" * 200
        text = agent_channel.format_reply_response(
            request_id="req-1", text=long_text, reply_slot_created=True, mark_info=""
        )
        assert "x" * 100 + "..." in text
        assert "x" * 101 not in text

    def test_lost_race_response_is_no_op_warning(self):
        text = agent_channel.format_reply_response(
            request_id="req-1", text="whatever", reply_slot_created=False, mark_info=""
        )
        assert text.startswith("⚠️ No-op")
        assert "req-1.json" in text
        assert "first writer wins" in text


class TestEmitRequestAudit:
    def test_emits_agent_channel_request_with_ids(self):
        emitter = _RecordingEmitter()
        agent_channel.emit_request_audit(
            request_id="req-1", message_id="msg-1", emit_event=emitter
        )
        assert len(emitter.calls) == 1
        call = emitter.calls[0]
        assert call["event_type"] == "agent_channel.request"
        assert call["payload"] == {"request_id": "req-1", "message_id": "msg-1"}
        assert call["severity"] == "info"  # default


class TestWriteAck:
    def test_success_writes_ack_file_distinct_from_answer_slot(self, tmp_path: Path):
        emitter = _RecordingEmitter()
        response = agent_channel.write_ack(
            agent_replies_dir=tmp_path,
            request_id="req-1",
            ack_text="working on it",
            message_id="msg-1",
            emit_event=emitter,
        )
        ack_file = tmp_path / "req-1.ack.json"
        answer_file = tmp_path / "req-1.json"
        assert ack_file.exists()
        assert not answer_file.exists()  # ack != answer — no code path to the answer slot

        ack_raw = ack_file.read_text()
        assert "\n" not in ack_raw  # compact single-line write — see write_reply's docstring

        written = json.loads(ack_raw)
        assert written == {
            "request_id": "req-1",
            "ack": True,
            "capabilities": list(agent_channel.CAPABILITIES),
            "phase": None,
            "pct": None,
            "text": "working on it",
            "ts": written["ts"],
        }

        assert "Claimed and acked (local-claude)" in response
        assert "req-1.ack.json" in response
        assert "working on it" in response

        assert len(emitter.calls) == 1
        assert emitter.calls[0]["event_type"] == "agent_channel.ack"
        assert emitter.calls[0]["payload"] == {
            "request_id": "req-1",
            "message_id": "msg-1",
            "text_len": len("working on it"),
        }
        assert emitter.calls[0]["severity"] == "info"

    def test_write_failure_returns_warning_and_escalates_to_error_severity(self, tmp_path: Path, monkeypatch):
        """Refinement 2 (agent-channel protocol v1.1): the claim must still
        succeed when the ack write fails (this function raises nothing —
        handle_claim_and_ack's move to processing/ already happened before
        write_ack was ever called), but the failure must be LOUD: escalated
        to severity="error" (tracked in the event bus's errors_last_1h
        metric) rather than the "warn" this used to emit silently."""
        emitter = _RecordingEmitter()

        def _boom(path, data, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(agent_channel, "atomic_write_json", _boom)

        response = agent_channel.write_ack(
            agent_replies_dir=tmp_path,
            request_id="req-2",
            ack_text="ack text",
            message_id="msg-2",
            emit_event=emitter,
        )
        # Claim-level outcome: still a "claimed, but ack failed" response,
        # never an exception — the claim itself is never rolled back.
        assert "Warning: message claimed but local-claude ack write failed" in response
        assert "msg-2" in response
        assert "remains in processing/" in response

        assert len(emitter.calls) == 1
        assert emitter.calls[0]["event_type"] == "agent_channel.ack"
        assert emitter.calls[0]["severity"] == "error"
        assert emitter.calls[0]["payload"]["error"] == "disk full"
        assert emitter.calls[0]["payload"]["request_id"] == "req-2"
        assert not (tmp_path / "req-2.ack.json").exists()

    def test_write_failure_logs_error_with_request_id(self, tmp_path: Path, monkeypatch, caplog):
        """The request_id must be greppable/correlatable in the log line
        itself, not only in the (separately-consumed) audit event payload."""
        import logging

        def _boom(path, data, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(agent_channel, "atomic_write_json", _boom)

        with caplog.at_level(logging.ERROR, logger=agent_channel.log.name):
            agent_channel.write_ack(
                agent_replies_dir=tmp_path,
                request_id="req-3",
                ack_text="ack text",
                message_id="msg-3",
                emit_event=_RecordingEmitter(),
            )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert "req-3" in error_records[0].message

    def test_capabilities_are_present_and_derived_from_get_capabilities(self, tmp_path: Path):
        """Refinement 1 (agent-channel protocol v1.1): the ack advertises
        what this server build actually supports, via the single source-of-
        truth get_capabilities() — not a literal hardcoded at the call site."""
        response = agent_channel.write_ack(
            agent_replies_dir=tmp_path,
            request_id="req-4",
            ack_text="working on it",
            message_id="msg-4",
            emit_event=_RecordingEmitter(),
        )
        written = json.loads((tmp_path / "req-4.ack.json").read_text())
        assert written["capabilities"] == agent_channel.get_capabilities()
        assert "write_progress" in written["capabilities"]
        assert "Claimed and acked (local-claude)" in response

    def test_get_capabilities_returns_a_fresh_list_each_call(self):
        """A caller mutating the returned list must never corrupt the
        module's own CAPABILITIES constant — get_capabilities() must copy,
        not hand out the tuple's own backing list/reference."""
        a = agent_channel.get_capabilities()
        a.append("not-a-real-capability")
        b = agent_channel.get_capabilities()
        assert "not-a-real-capability" not in b


class TestWriteProgress:
    """agent-channel protocol v1, §2 — write_progress: repeatable,
    claim-bound status writes for an OPEN exchange."""

    # -- Claim-bound authorization (§2.2) -----------------------------------

    def test_no_claim_row_is_refused(self, tmp_path: Path):
        emitter = _RecordingEmitter()
        claims_db = _FakeClaimsDB()  # no row at all for "req-1"

        outcome = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="working on it",
            emit_event=emitter,
        )

        assert outcome.accepted is False
        assert outcome.written is False
        assert outcome.reason == "unauthorized"
        assert not (tmp_path / "req-1.ack.json").exists()
        assert emitter.calls[0]["event_type"] == "agent_channel.progress"
        assert emitter.calls[0]["severity"] == "warn"
        assert emitter.calls[0]["payload"]["outcome"] == "unauthorized"

    def test_claim_already_processed_is_refused(self, tmp_path: Path):
        """A non-claimant (or a claimant whose exchange already closed)
        cannot resurrect it with a status write — this is the concrete case
        the adversarial review named: session_id collapses to 'dispatcher'
        across subagents, so authorization must be claim-row-bound, not
        session-identity-bound."""
        emitter = _RecordingEmitter()
        claims_db = _FakeClaimsDB({"req-1": "processed"})

        outcome = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="too late",
            emit_event=emitter,
        )

        assert outcome.accepted is False
        assert outcome.reason == "unauthorized"
        assert not (tmp_path / "req-1.ack.json").exists()

    def test_claim_failed_status_is_refused(self, tmp_path: Path):
        emitter = _RecordingEmitter()
        claims_db = _FakeClaimsDB({"req-1": "failed"})

        outcome = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="x",
            emit_event=emitter,
        )
        assert outcome.accepted is False
        assert outcome.reason == "unauthorized"

    def test_open_claim_is_authorized_and_writes(self, tmp_path: Path):
        emitter = _RecordingEmitter()
        claims_db = _FakeClaimsDB({"req-1": "processing"})

        outcome = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="3/5 tests passing",
            emit_event=emitter,
        )

        assert outcome.accepted is True
        assert outcome.written is True
        assert outcome.reason == "written"

        ack_file = tmp_path / "req-1.ack.json"
        assert ack_file.exists()
        written = json.loads(ack_file.read_text())
        assert written["request_id"] == "req-1"
        assert written["ack"] is True
        assert written["text"] == "3/5 tests passing"
        assert "ts" in written

        assert emitter.calls[-1]["event_type"] == "agent_channel.progress"
        assert emitter.calls[-1]["severity"] == "info"
        assert emitter.calls[-1]["payload"]["outcome"] == "written"

    # -- last-write-wins overwrite -------------------------------------------

    def test_second_open_write_overwrites_first_after_debounce_window(self, tmp_path: Path):
        """Current status, not a transcript: the second write clobbers the
        first entirely — no accumulating history."""
        claims_db = _FakeClaimsDB({"req-1": "processing"})
        emitter = _RecordingEmitter()

        agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="first status",
            emit_event=emitter,
        )
        ack_file = tmp_path / "req-1.ack.json"
        # Backdate the ack file's mtime past the debounce window so the
        # second write isn't itself debounced — isolates "does overwrite
        # replace content" from the separate debounce behavior below.
        old_ts = time.time() - (agent_channel.DEBOUNCE_INTERVAL_SECONDS + 1)
        import os
        os.utime(ack_file, (old_ts, old_ts))

        outcome = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="second status — replaces the first",
            emit_event=emitter,
        )
        assert outcome.written is True
        written = json.loads(ack_file.read_text())
        assert written["text"] == "second status — replaces the first"

    # -- Debounce (Open Dial 5) ------------------------------------------

    def test_rapid_second_call_within_window_is_debounced_not_error(self, tmp_path: Path):
        claims_db = _FakeClaimsDB({"req-1": "processing"})
        emitter = _RecordingEmitter()

        first = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="status A",
            emit_event=emitter,
        )
        assert first.written is True

        second = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="status B — should not land yet",
            emit_event=emitter,
        )

        # Debounced calls are still "accepted" (not an error to the caller)
        # — only the byte write itself is skipped.
        assert second.accepted is True
        assert second.written is False
        assert second.reason == "debounced"

        # File content is unchanged — still the first write.
        written = json.loads((tmp_path / "req-1.ack.json").read_text())
        assert written["text"] == "status A"

        assert emitter.calls[-1]["payload"]["outcome"] == "debounced"

    def test_rapid_calls_collapse_to_one_write(self, tmp_path: Path):
        """Ten calls inside the debounce window produce exactly one write."""
        claims_db = _FakeClaimsDB({"req-1": "processing"})
        emitter = _RecordingEmitter()

        outcomes = [
            agent_channel.write_progress(
                claims_db=claims_db,
                agent_replies_dir=tmp_path,
                request_id="req-1",
                status_text=f"status {i}",
                emit_event=emitter,
            )
            for i in range(10)
        ]

        assert sum(1 for o in outcomes if o.written) == 1
        assert all(o.accepted for o in outcomes)
        written = json.loads((tmp_path / "req-1.ack.json").read_text())
        assert written["text"] == "status 0"  # only the first call's content landed

    def test_write_after_debounce_window_elapses_lands(self, tmp_path: Path):
        claims_db = _FakeClaimsDB({"req-1": "processing"})
        emitter = _RecordingEmitter()

        agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="status A",
            emit_event=emitter,
        )
        ack_file = tmp_path / "req-1.ack.json"
        old_ts = time.time() - (agent_channel.DEBOUNCE_INTERVAL_SECONDS + 1)
        import os
        os.utime(ack_file, (old_ts, old_ts))

        outcome = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="status B — lands now",
            emit_event=emitter,
        )
        assert outcome.written is True
        written = json.loads(ack_file.read_text())
        assert written["text"] == "status B — lands now"

    # -- Message-after-complete guard (§2.5) ------------------------------

    def test_refuses_write_when_terminal_reply_already_exists(self, tmp_path: Path):
        claims_db = _FakeClaimsDB({"req-1": "processing"})
        emitter = _RecordingEmitter()
        # Terminal reply already landed (OPEN -> COMPLETE), but the claim
        # row hasn't necessarily flipped yet — COMPLETE is derived from the
        # reply file's existence, not from claim status (protocol v1 §3
        # state table).
        (tmp_path / "req-1.json").write_text('{"request_id": "req-1", "text": "done"}')

        outcome = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="still going?",
            emit_event=emitter,
        )

        assert outcome.accepted is True  # not an error — a sanctioned no-op
        assert outcome.written is False
        assert outcome.reason == "already_complete"
        assert not (tmp_path / "req-1.ack.json").exists()
        assert emitter.calls[-1]["payload"]["outcome"] == "already_complete"

    def test_message_after_complete_race_is_closed_against_concurrent_write_progress(
        self, tmp_path: Path, monkeypatch
    ):
        """The terminal-file check and the write happen inside ONE
        flock-guarded critical section — simulate a terminal reply landing
        *during* the check by having reply_path.exists() flip from False to
        True mid-call, and confirm no ack write follows once it's seen.

        Scope note: this only proves the guard is race-free with respect to
        write_progress's own check-then-write. It does NOT prove the race is
        closed against write_reply() (the real terminal writer), which holds
        no lock at all and is not coordinated with write_progress's flock —
        see the write_progress docstring's "IMPORTANT SCOPE LIMIT" note.
        That residual write_progress-vs-write_reply window is a documented,
        accepted limitation, not something this test (or the flock) closes.
        """
        claims_db = _FakeClaimsDB({"req-1": "processing"})
        emitter = _RecordingEmitter()

        reply_path = tmp_path / "req-1.json"
        real_exists = Path.exists

        def _flip_on_check(self):
            if self == reply_path:
                # Simulate the terminal file landing exactly when checked.
                reply_path.write_text('{"request_id": "req-1", "text": "raced in"}')
                return True
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", _flip_on_check)

        outcome = agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="never lands",
            emit_event=emitter,
        )
        assert outcome.written is False
        assert outcome.reason == "already_complete"
        assert not (tmp_path / "req-1.ack.json").exists()

    # -- Lock discipline ----------------------------------------------------

    def test_lock_is_released_after_call_completes(self, tmp_path: Path):
        """A stuck/held lock would hang every subsequent call — confirm the
        lock file can be re-acquired immediately after write_progress returns."""
        import fcntl

        claims_db = _FakeClaimsDB({"req-1": "processing"})
        emitter = _RecordingEmitter()

        agent_channel.write_progress(
            claims_db=claims_db,
            agent_replies_dir=tmp_path,
            request_id="req-1",
            status_text="status A",
            emit_event=emitter,
        )

        lock_path = tmp_path / ".req-1.progress.lock"
        assert lock_path.exists()
        with open(lock_path, "r+") as f:
            # Non-blocking acquire must succeed — if write_progress leaked
            # the lock, this raises BlockingIOError.
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class TestWritePointer:
    """agent-channel protocol v1, §3.2 — by-agent pointer mailbox."""

    def test_writes_zero_byte_pointer_file(self, tmp_path: Path):
        pointer_path = agent_channel.write_pointer(
            agent_replies_dir=tmp_path,
            agent_slug="bloom",
            request_id="req-1",
        )
        assert pointer_path == tmp_path / "by-agent" / "bloom" / "req-1"
        assert pointer_path.exists()
        assert pointer_path.stat().st_size == 0

    def test_creates_per_agent_subdirectory(self, tmp_path: Path):
        agent_channel.write_pointer(
            agent_replies_dir=tmp_path, agent_slug="glyph", request_id="req-9"
        )
        assert (tmp_path / "by-agent" / "glyph").is_dir()

    def test_idempotent_second_call_does_not_error(self, tmp_path: Path):
        agent_channel.write_pointer(
            agent_replies_dir=tmp_path, agent_slug="bloom", request_id="req-1"
        )
        # Calling again for the identical (agent, request_id) pair must not
        # raise — e.g. a retried send_reply for the same request_id.
        pointer_path = agent_channel.write_pointer(
            agent_replies_dir=tmp_path, agent_slug="bloom", request_id="req-1"
        )
        assert pointer_path.exists()

    def test_different_request_ids_get_distinct_pointer_files(self, tmp_path: Path):
        agent_channel.write_pointer(
            agent_replies_dir=tmp_path, agent_slug="bloom", request_id="req-1"
        )
        agent_channel.write_pointer(
            agent_replies_dir=tmp_path, agent_slug="bloom", request_id="req-2"
        )
        listing = sorted(p.name for p in (tmp_path / "by-agent" / "bloom").iterdir())
        assert listing == ["req-1", "req-2"]
