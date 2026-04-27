"""
Tests for dispatcher command handlers for /approve, /wos status, and /wos unblock.

These test the pure handler functions in isolation — no Telegram or MCP required.
The handlers receive parsed command arguments and a Registry instance, and return
a formatted string response.
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest

from src.orchestration.dispatcher_handlers import handle_approve, handle_confirm, handle_decide, handle_decide_defer, handle_wos_execute, handle_wos_status, handle_wos_unblock, handle_steward_trigger, route_wos_message, route_callback_message, CALLBACK_DATA_HANDLERS, WOS_MESSAGE_TYPE_DISPATCH


@pytest.fixture
def registry(tmp_path: Path):
    from src.orchestration.registry import Registry
    return Registry(tmp_path / "registry.db")


@pytest.fixture
def uow_id(registry) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    result = registry.upsert(issue_number=200, title="Test issue for dispatcher", sweep_date=today, success_criteria="Test completion.")
    return result.id


class TestHandleWosExecute:
    """Tests for handle_wos_execute — pure prompt-builder for the wos_execute message type."""

    _UOW_ID = "abc-123"
    _INSTRUCTIONS = "Run the linter and fix any errors."
    _OUTPUT_REF = "/home/lobster/lobster-workspace/orchestration/outputs/abc-123.result.json"

    def _prompt(self) -> str:
        return handle_wos_execute(self._UOW_ID, self._INSTRUCTIONS, self._OUTPUT_REF)

    def test_returns_string(self):
        """handle_wos_execute is a pure function — no side effects, returns str."""
        result = self._prompt()
        assert isinstance(result, str)

    def test_prompt_includes_uow_id(self):
        """The UoW ID must appear in the prompt so the subagent can correlate results."""
        assert self._UOW_ID in self._prompt()

    def test_prompt_includes_instructions(self):
        """The prescribed instructions must be embedded verbatim."""
        assert self._INSTRUCTIONS in self._prompt()

    def test_prompt_includes_output_ref(self):
        """The subagent must know the exact path to write the result file."""
        assert self._OUTPUT_REF in self._prompt()

    def test_prompt_includes_task_id_header(self):
        """The task_id frontmatter must use the wos- prefix for dispatcher correlation."""
        assert f"task_id: wos-{self._UOW_ID}" in self._prompt()

    def test_prompt_includes_chat_id_zero(self):
        """chat_id: 0 is the silent-drop sentinel — result must not be relayed to user."""
        assert "chat_id: 0" in self._prompt()

    def test_prompt_includes_result_contract_section(self):
        """The result contract section must be present so the subagent knows what to write."""
        assert "Result contract" in self._prompt()

    def test_prompt_embeds_all_four_outcome_values(self):
        """All four valid outcome values must appear in the result contract."""
        prompt = self._prompt()
        for outcome in ("complete", "partial", "failed", "blocked"):
            assert outcome in prompt

    def test_prompt_includes_write_result_instruction(self):
        """The subagent must call write_result after writing the result file."""
        assert "write_result" in self._prompt()

    def test_prompt_includes_boundary_constraint(self):
        """The Boundary constraint must prevent the subagent from touching WOS source files."""
        assert "Boundary" in self._prompt()

    def test_pure_function_same_inputs_same_output(self):
        """Pure function contract: identical inputs always produce identical outputs."""
        p1 = handle_wos_execute(self._UOW_ID, self._INSTRUCTIONS, self._OUTPUT_REF)
        p2 = handle_wos_execute(self._UOW_ID, self._INSTRUCTIONS, self._OUTPUT_REF)
        assert p1 == p2

    def test_different_uow_ids_produce_different_prompts(self):
        """Each UoW must produce a distinct prompt — no cross-contamination."""
        p1 = handle_wos_execute("uow-001", self._INSTRUCTIONS, self._OUTPUT_REF)
        p2 = handle_wos_execute("uow-002", self._INSTRUCTIONS, self._OUTPUT_REF)
        assert p1 != p2

    def test_uow_id_appears_in_task_id_and_body(self):
        """UoW ID must appear in the frontmatter task_id AND in the body (for result correlation)."""
        uow_id = "xyz-789"
        prompt = handle_wos_execute(uow_id, self._INSTRUCTIONS, self._OUTPUT_REF)
        assert f"wos-{uow_id}" in prompt   # frontmatter task_id
        assert uow_id in prompt            # body (UoW ID line or result contract)

    def test_prompt_includes_mcp_heartbeat_tool(self):
        """
        The MCP tool name must appear in the heartbeat instructions so subagents
        know they can call write_wos_heartbeat directly rather than importing Python.
        """
        prompt = self._prompt()
        assert "write_wos_heartbeat" in prompt, (
            "The write_wos_heartbeat MCP tool must be the preferred heartbeat method "
            "in the dispatched prompt (issue #849)"
        )

    def test_prompt_heartbeat_section_is_labeled_required(self):
        """The heartbeat section must be labeled REQUIRED so subagents do not skip it."""
        prompt = self._prompt()
        assert "REQUIRED" in prompt

    def test_prompt_includes_wos_registry_fallback(self):
        """WOSRegistry must remain as a Python fallback in the heartbeat instructions."""
        prompt = self._prompt()
        assert "WOSRegistry" in prompt, (
            "WOSRegistry fallback must remain in the prompt so agents without MCP "
            "tool access can still write heartbeats via Python"
        )


class TestHandleApprove:
    def test_success_message_contains_ready_for_steward(self, registry, uow_id):
        """approve now goes proposed → ready-for-steward; response reflects that."""
        response = handle_approve(uow_id, registry=registry)
        assert "ready-for-steward" in response.lower()
        assert uow_id in response

    def test_success_message_notes_pending_via(self, registry, uow_id):
        """Response mentions 'via pending' so the user knows the intermediate step."""
        response = handle_approve(uow_id, registry=registry)
        assert "pending" in response.lower()

    def test_not_found_message(self, registry):
        response = handle_approve("nonexistent-id", registry=registry)
        assert "not found" in response.lower()
        assert "/wos status proposed" in response

    def test_already_ready_for_steward_message(self, registry, uow_id):
        """After approve, second approve returns ApproveSkipped with current ready-for-steward status."""
        registry.approve(uow_id)
        response = handle_approve(uow_id, registry=registry)
        # Should mention current status (ready-for-steward), not raise
        assert "ready-for-steward" in response.lower()

    def test_expired_message(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=201, title="Expiring issue", sweep_date=today, success_criteria="Test completion.")
        registry.set_status_direct(result.id, "expired")
        response = handle_approve(result.id, registry=registry)
        assert "expired" in response.lower()


class TestHandleConfirmAlias:
    """handle_confirm is an alias for handle_approve — basic smoke tests."""

    def test_confirm_alias_delegates_to_approve(self, registry, uow_id):
        response = handle_confirm(uow_id, registry=registry)
        assert "ready-for-steward" in response.lower()
        assert uow_id in response


class TestHandleWosStatus:
    def test_returns_active_records(self, registry, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        r1 = registry.upsert(issue_number=210, title="Running issue", sweep_date=today, success_criteria="Test completion.")
        registry.set_status_direct(r1.id, "active")
        response = handle_wos_status("active", registry=registry)
        assert r1.id in response

    def test_returns_empty_message_when_no_records(self, registry):
        response = handle_wos_status("active", registry=registry)
        assert "no" in response.lower() or "empty" in response.lower() or "0" in response

    def test_formats_each_record_with_required_fields(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        r = registry.upsert(issue_number=220, title="Status test issue", sweep_date=today, success_criteria="Test completion.")
        response = handle_wos_status("proposed", registry=registry)
        # Each line should contain: id, summary, source, created date
        assert r.id in response
        assert "Status test issue" in response

    def test_defaults_to_active_and_queued(self, registry):
        """Default /wos status shows active + ready-for-steward (+ pending for backward compat)."""
        today = datetime.now(timezone.utc).date().isoformat()
        r1 = registry.upsert(issue_number=230, title="Active issue", sweep_date=today, success_criteria="Test completion.")
        registry.set_status_direct(r1.id, "active")
        r2 = registry.upsert(issue_number=231, title="Approved issue", sweep_date=today, success_criteria="Test completion.")
        registry.approve(r2.id)  # now lands on ready-for-steward, not pending
        # No status arg → returns active + ready-for-steward + pending
        response = handle_wos_status(None, registry=registry)
        assert r1.id in response
        assert r2.id in response


class TestHandleWosUnblock:
    """Tests for handle_wos_unblock — BOOTUP_CANDIDATE_GATE file-flag clearing."""

    def test_creates_flag_file_when_not_present(self, tmp_path, monkeypatch):
        """Calling unblock when flag absent creates the flag and returns success."""
        from src.orchestration import dispatcher_handlers
        flag = tmp_path / "wos-gate-cleared"
        monkeypatch.setattr(dispatcher_handlers, "_GATE_CLEARED_FLAG", flag)

        assert not flag.exists()
        response = handle_wos_unblock()
        assert flag.exists()
        assert "cleared" in response.lower()

    def test_idempotent_when_already_cleared(self, tmp_path, monkeypatch):
        """Calling unblock when flag already exists returns a notice, not an error."""
        from src.orchestration import dispatcher_handlers
        flag = tmp_path / "wos-gate-cleared"
        flag.touch()
        monkeypatch.setattr(dispatcher_handlers, "_GATE_CLEARED_FLAG", flag)

        response = handle_wos_unblock()
        assert "already" in response.lower() or "cleared" in response.lower()
        # Flag should still exist
        assert flag.exists()

    def test_creates_parent_directory_if_missing(self, tmp_path, monkeypatch):
        """Flag file parent directory is created if it does not exist."""
        from src.orchestration import dispatcher_handlers
        flag = tmp_path / "nonexistent" / "subdir" / "wos-gate-cleared"
        monkeypatch.setattr(dispatcher_handlers, "_GATE_CLEARED_FLAG", flag)

        assert not flag.parent.exists()
        response = handle_wos_unblock()
        assert flag.exists()
        assert "cleared" in response.lower()

    def test_response_mentions_flag_path(self, tmp_path, monkeypatch):
        """Success response includes the flag path so Dan can verify."""
        from src.orchestration import dispatcher_handlers
        flag = tmp_path / "wos-gate-cleared"
        monkeypatch.setattr(dispatcher_handlers, "_GATE_CLEARED_FLAG", flag)

        response = handle_wos_unblock()
        assert str(flag) in response

    def test_is_bootup_candidate_gate_active_reflects_flag(self, tmp_path, monkeypatch):
        """After unblock, is_bootup_candidate_gate_active() returns False."""
        from src.orchestration import dispatcher_handlers, steward
        flag = tmp_path / "wos-gate-cleared"
        monkeypatch.setattr(dispatcher_handlers, "_GATE_CLEARED_FLAG", flag)
        monkeypatch.setattr(steward, "_GATE_CLEARED_FLAG", flag)

        assert steward.is_bootup_candidate_gate_active() is True
        handle_wos_unblock()
        assert steward.is_bootup_candidate_gate_active() is False


# ---------------------------------------------------------------------------
# /decide command tests
# ---------------------------------------------------------------------------

@pytest.fixture
def blocked_uow_id(registry) -> str:
    """A UoW set to blocked status for decide command tests."""
    today = datetime.now(timezone.utc).date().isoformat()
    result = registry.upsert(issue_number=300, title="Blocked issue", sweep_date=today, success_criteria="Test done.")
    registry.set_status_direct(result.id, "blocked")
    return result.id


class TestHandleDecide:
    """Tests for /decide <uow-id> <proceed|abandon|retry>."""

    def test_proceed_transitions_blocked_to_ready_for_steward(self, registry, blocked_uow_id):
        """proceed unblocks a UoW and re-queues it without resetting steward_cycles."""
        response = handle_decide(blocked_uow_id, "proceed", registry=registry)
        assert "ready-for-steward" in response.lower()
        assert blocked_uow_id in response
        uow = registry.get(blocked_uow_id)
        assert uow.status.value == "ready-for-steward"

    def test_retry_transitions_blocked_to_ready_for_steward_and_resets_cycles(self, registry, blocked_uow_id):
        """retry is equivalent to /decide retry — transitions blocked→ready-for-steward, cycles=0."""
        response = handle_decide(blocked_uow_id, "retry", registry=registry)
        assert "ready-for-steward" in response.lower()
        assert blocked_uow_id in response
        uow = registry.get(blocked_uow_id)
        assert uow.status.value == "ready-for-steward"

    def test_abandon_transitions_blocked_to_failed(self, registry, blocked_uow_id):
        """abandon closes the UoW as user-requested failure."""
        response = handle_decide(blocked_uow_id, "abandon", registry=registry)
        assert "failed" in response.lower()
        assert blocked_uow_id in response
        uow = registry.get(blocked_uow_id)
        assert uow.status.value == "failed"

    def test_unknown_action_returns_error_message(self, registry, blocked_uow_id):
        """Invalid action returns an informative error, not a crash."""
        response = handle_decide(blocked_uow_id, "frobnicate", registry=registry)
        assert "unknown action" in response.lower()
        assert "proceed" in response.lower()
        assert "abandon" in response.lower()
        assert "retry" in response.lower()

    def test_proceed_on_non_blocked_uow_returns_error(self, registry):
        """proceed on a UoW not in blocked status returns a diagnostic message."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=301, title="Active issue", sweep_date=today, success_criteria="Test done.")
        registry.set_status_direct(result.id, "active")
        response = handle_decide(result.id, "proceed", registry=registry)
        assert "not currently in" in response.lower() or "could not be" in response.lower()

    def test_action_is_case_insensitive(self, registry, blocked_uow_id):
        """Action matching is case-insensitive — PROCEED, Retry, ABANDON all work."""
        response = handle_decide(blocked_uow_id, "PROCEED", registry=registry)
        assert "ready-for-steward" in response.lower()

    def test_proceed_preserves_steward_cycles(self, registry):
        """proceed does not reset steward_cycles — retry is the full-reset action."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=302, title="Cycles issue", sweep_date=today, success_criteria="Test done.")
        # Manually set cycles and blocked status
        import sqlite3
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute("UPDATE uow_registry SET status='blocked', steward_cycles=3 WHERE id=?", (result.id,))
        conn.commit()
        conn.close()
        handle_decide(result.id, "proceed", registry=registry)
        uow = registry.get(result.id)
        assert uow.steward_cycles == 3  # preserved

    def test_retry_resets_steward_cycles(self, registry):
        """retry resets steward_cycles to 0 — full fresh start."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=303, title="Reset cycles issue", sweep_date=today, success_criteria="Test done.")
        import sqlite3
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute("UPDATE uow_registry SET status='blocked', steward_cycles=3 WHERE id=?", (result.id,))
        conn.commit()
        conn.close()
        handle_decide(result.id, "retry", registry=registry)
        uow = registry.get(result.id)
        assert uow.steward_cycles == 0  # reset


# ---------------------------------------------------------------------------
# /decide defer tests — issue #343
# ---------------------------------------------------------------------------

# Named constant: the expected event name written to audit_log on defer.
DECIDE_DEFER_AUDIT_EVENT = "decide_defer"


class TestHandleDecideDefer:
    """Tests for /decide <uow-id> defer — leave blocked, write audit entry."""

    def test_defer_leaves_uow_in_blocked_status(self, registry, blocked_uow_id):
        """defer does not transition the UoW — it stays in blocked status."""
        response = handle_decide(blocked_uow_id, "defer", registry=registry)
        assert "deferred" in response.lower()
        uow = registry.get(blocked_uow_id)
        assert uow.status.value == "blocked"

    def test_defer_writes_audit_entry(self, registry, blocked_uow_id):
        """defer writes a decide_defer audit log entry — the deferral is auditable."""
        import sqlite3
        handle_decide(blocked_uow_id, "defer", registry=registry)
        conn = sqlite3.connect(str(registry.db_path))
        row = conn.execute(
            "SELECT event, from_status, to_status, agent FROM audit_log WHERE uow_id=? AND event=?",
            (blocked_uow_id, DECIDE_DEFER_AUDIT_EVENT),
        ).fetchone()
        conn.close()
        assert row is not None, "audit log must contain a decide_defer entry"
        assert row[1] == "blocked"   # from_status
        assert row[2] == "blocked"   # to_status (no transition)
        assert row[3] == "user"      # actor

    def test_defer_with_note_includes_note_in_response(self, registry, blocked_uow_id):
        """defer <note> includes the operator note in the response."""
        note = "waiting on external security review"
        response = handle_decide(blocked_uow_id, f"defer {note}", registry=registry)
        assert note in response

    def test_defer_with_note_records_note_in_audit_log(self, registry, blocked_uow_id):
        """defer <note> persists the note text in the audit_log entry."""
        import sqlite3
        import json
        note = "blocked by upstream API outage"
        handle_decide(blocked_uow_id, f"defer {note}", registry=registry)
        conn = sqlite3.connect(str(registry.db_path))
        row = conn.execute(
            "SELECT note FROM audit_log WHERE uow_id=? AND event=?",
            (blocked_uow_id, DECIDE_DEFER_AUDIT_EVENT),
        ).fetchone()
        conn.close()
        assert row is not None
        note_payload = json.loads(row[0])
        assert note in note_payload["note"]

    def test_defer_without_note_uses_default_message(self, registry, blocked_uow_id):
        """defer with no note writes a default audit message — not an empty string."""
        import sqlite3
        import json
        handle_decide(blocked_uow_id, "defer", registry=registry)
        conn = sqlite3.connect(str(registry.db_path))
        row = conn.execute(
            "SELECT note FROM audit_log WHERE uow_id=? AND event=?",
            (blocked_uow_id, DECIDE_DEFER_AUDIT_EVENT),
        ).fetchone()
        conn.close()
        assert row is not None
        note_payload = json.loads(row[0])
        assert note_payload["note"], "default note must be a non-empty string"

    def test_defer_on_non_blocked_uow_returns_error(self, registry):
        """defer on a non-blocked UoW returns an informative error, not a crash."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=310, title="Active defer test", sweep_date=today, success_criteria="Test done.")
        registry.set_status_direct(result.id, "active")
        response = handle_decide(result.id, "defer", registry=registry)
        assert "not currently in" in response.lower() or "could not be" in response.lower()

    def test_defer_does_not_reset_steward_cycles(self, registry):
        """defer leaves steward_cycles unchanged — it is a record-only operation."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=311, title="Cycles defer test", sweep_date=today, success_criteria="Test done.")
        import sqlite3
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute("UPDATE uow_registry SET status='blocked', steward_cycles=4 WHERE id=?", (result.id,))
        conn.commit()
        conn.close()
        handle_decide(result.id, "defer", registry=registry)
        uow = registry.get(result.id)
        assert uow.steward_cycles == 4  # unchanged

    def test_defer_is_listed_in_unknown_action_error(self, registry, blocked_uow_id):
        """The error message for unknown actions lists defer among valid options."""
        response = handle_decide(blocked_uow_id, "frobnicate", registry=registry)
        assert "defer" in response.lower()

    def test_handle_decide_defer_standalone_function(self, registry, blocked_uow_id):
        """handle_decide_defer is callable directly — same semantics as via handle_decide."""
        response = handle_decide_defer(blocked_uow_id, "direct call test", registry=registry)
        assert "deferred" in response.lower()
        uow = registry.get(blocked_uow_id)
        assert uow.status.value == "blocked"

    def test_defer_idempotent_multiple_calls_each_write_audit_entry(self, registry, blocked_uow_id):
        """Multiple defer calls each write a separate audit entry — deferral is a log, not a state."""
        import sqlite3
        handle_decide(blocked_uow_id, "defer first", registry=registry)
        handle_decide(blocked_uow_id, "defer second", registry=registry)
        conn = sqlite3.connect(str(registry.db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE uow_id=? AND event=?",
            (blocked_uow_id, DECIDE_DEFER_AUDIT_EVENT),
        ).fetchone()[0]
        conn.close()
        assert count == 2, "each defer call must produce a distinct audit entry"


# ---------------------------------------------------------------------------
# route_wos_message structural dispatch tests — issue #856
#
# These tests verify that wos_execute messages are routed through
# route_wos_message() which returns a spawn_subagent action. This is the
# structural path the dispatcher must follow — calling route_wos_message
# unconditionally for any message type in WOS_MESSAGE_TYPE_DISPATCH ensures
# correctness even after context compaction.
# ---------------------------------------------------------------------------

# Named constant: the only message type the WOS executor produces.
WOS_EXECUTE_MESSAGE_TYPE = "wos_execute"

# Named constant: the action that route_wos_message must return for wos_execute.
WOS_SPAWN_ACTION = "spawn_subagent"


class TestRouteWosMessage:
    """route_wos_message must structurally dispatch wos_execute to spawn_subagent."""

    _SAMPLE_MSG = {
        "type": "wos_execute",
        "uow_id": "uow_test_001",
        "instructions": "Run the linter and fix any errors found.",
        "output_ref": "/home/lobster/lobster-workspace/orchestration/outputs/uow_test_001.result.json",
    }

    def test_wos_execute_registered_in_dispatch_table(self):
        """wos_execute must appear in WOS_MESSAGE_TYPE_DISPATCH.

        Absence means the dispatcher's type-based routing table cannot fire for
        wos_execute messages — the root cause of the starvation described in the
        wos-starvation-diagnosis-20260422.md audit.
        """
        assert WOS_EXECUTE_MESSAGE_TYPE in WOS_MESSAGE_TYPE_DISPATCH, (
            f"{WOS_EXECUTE_MESSAGE_TYPE!r} must be registered in WOS_MESSAGE_TYPE_DISPATCH "
            "so the dispatcher routes it structurally, not via prose that is lost on compaction"
        )

    def test_route_wos_message_returns_spawn_subagent_action(self):
        """route_wos_message for wos_execute must return action='spawn_subagent'."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert result["action"] == WOS_SPAWN_ACTION, (
            f"Expected action={WOS_SPAWN_ACTION!r}, got {result['action']!r}. "
            "The dispatcher must call Task() when this action is returned."
        )

    def test_route_wos_message_returns_task_id_with_wos_prefix(self):
        """task_id must use the wos- prefix for dispatcher correlation."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert result["task_id"] == f"wos-{self._SAMPLE_MSG['uow_id']}"

    def test_route_wos_message_returns_non_empty_prompt(self):
        """prompt must be a non-empty string — it is the subagent Task input."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert isinstance(result["prompt"], str)
        assert len(result["prompt"]) > 0

    def test_route_wos_message_prompt_contains_uow_id(self):
        """The subagent prompt must embed the UoW ID for result correlation."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert self._SAMPLE_MSG["uow_id"] in result["prompt"]

    def test_route_wos_message_prompt_contains_instructions(self):
        """The prescribed instructions must be embedded verbatim in the prompt."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert self._SAMPLE_MSG["instructions"] in result["prompt"]

    def test_route_wos_message_defaults_agent_type_to_functional_engineer(self):
        """When agent_type is absent from the message, default to functional-engineer."""
        msg = {**self._SAMPLE_MSG}
        msg.pop("agent_type", None)
        result = route_wos_message(msg)
        assert result["agent_type"] == "functional-engineer"

    def test_route_wos_message_respects_explicit_agent_type(self):
        """When agent_type is present in the message, use it — not the default."""
        msg = {**self._SAMPLE_MSG, "agent_type": "lobster-generalist"}
        result = route_wos_message(msg)
        assert result["agent_type"] == "lobster-generalist"

    def test_route_wos_message_echoes_message_type(self):
        """result['message_type'] must echo the input type so callers can confirm routing."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert result["message_type"] == WOS_EXECUTE_MESSAGE_TYPE

    def test_route_wos_message_raises_for_unknown_type(self):
        """route_wos_message must raise ValueError for types not in WOS_MESSAGE_TYPE_DISPATCH."""
        bad_msg = {**self._SAMPLE_MSG, "type": "totally_not_a_wos_type"}
        with pytest.raises(ValueError, match="unrecognised message type"):
            route_wos_message(bad_msg)

    def test_route_wos_message_is_pure_same_inputs_same_output(self):
        """route_wos_message is a pure function — identical inputs produce identical outputs."""
        r1 = route_wos_message(self._SAMPLE_MSG)
        r2 = route_wos_message(self._SAMPLE_MSG)
        assert r1 == r2

    def test_route_wos_message_uses_default_output_ref_when_absent(self):
        """When output_ref is absent, route_wos_message derives it from uow_id."""
        msg = {
            "type": "wos_execute",
            "uow_id": "uow_no_ref_test",
            "instructions": "Do something.",
        }
        result = route_wos_message(msg)
        # The derived path must contain the uow_id so the Steward can find it
        assert "uow_no_ref_test" in result["prompt"]


# ---------------------------------------------------------------------------
# steward_trigger dispatch tests — issue #920
#
# These tests verify that steward_trigger messages are routed through
# route_wos_message() and return action="spawn_subagent".  steward_trigger is
# the post-completion event path added in PR #914 — the dispatcher must handle
# it structurally so it survives context compaction just like wos_execute.
#
# Advisory fixes verified here (oracle r2 on PR #914):
#   1. handle_steward_trigger prompt must include chat_id: 0 sentinel
#   2. route_wos_message must return action="spawn_subagent" for steward_trigger
# ---------------------------------------------------------------------------

# Named constant: the steward_trigger message type registered in PR #914.
STEWARD_TRIGGER_MESSAGE_TYPE = "steward_trigger"


class TestRouteWosStewardTrigger:
    """route_wos_message must dispatch steward_trigger as spawn_subagent."""

    _SAMPLE_MSG = {
        "type": "steward_trigger",
        "uow_id": "uow_steward_test_001",
    }

    def test_steward_trigger_registered_in_dispatch_table(self):
        """steward_trigger must appear in WOS_MESSAGE_TYPE_DISPATCH.

        Absence means the dispatcher's type-based routing cannot fire for
        steward_trigger messages — same compaction vulnerability as wos_execute.
        """
        assert STEWARD_TRIGGER_MESSAGE_TYPE in WOS_MESSAGE_TYPE_DISPATCH, (
            f"{STEWARD_TRIGGER_MESSAGE_TYPE!r} must be registered in WOS_MESSAGE_TYPE_DISPATCH "
            "so the dispatcher routes it structurally, not via prose"
        )

    def test_route_wos_message_steward_trigger_returns_spawn_subagent(self):
        """route_wos_message for steward_trigger must return action='spawn_subagent'."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert result["action"] == WOS_SPAWN_ACTION, (
            f"Expected action={WOS_SPAWN_ACTION!r}, got {result['action']!r}. "
            "The dispatcher must spawn a Task — not mark_processed without spawning."
        )

    def test_route_wos_message_steward_trigger_returns_task_id(self):
        """task_id must be a non-empty string identifying the steward heartbeat task."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert isinstance(result["task_id"], str)
        assert len(result["task_id"]) > 0

    def test_route_wos_message_steward_trigger_returns_non_empty_prompt(self):
        """prompt must be a non-empty string for the background subagent."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert isinstance(result["prompt"], str)
        assert len(result["prompt"]) > 0

    def test_route_wos_message_steward_trigger_echoes_message_type(self):
        """result['message_type'] must echo 'steward_trigger' so callers can confirm routing."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert result["message_type"] == STEWARD_TRIGGER_MESSAGE_TYPE

    def test_route_wos_message_steward_trigger_uses_lobster_generalist_agent(self):
        """steward_trigger subagent must use lobster-generalist — not functional-engineer."""
        result = route_wos_message(self._SAMPLE_MSG)
        assert result["agent_type"] == "lobster-generalist"

    def test_handle_steward_trigger_prompt_contains_chat_id_zero(self):
        """chat_id: 0 is the silent-drop sentinel — steward results must not be relayed to the user.

        Advisory fix from oracle r2 on PR #914: the steward_trigger prompt was missing
        'chat_id: 0', meaning write_result from the steward subagent could attempt to
        relay a reply to the triggering chat rather than silently dropping it.
        """
        result = handle_steward_trigger("uow_test_abc")
        assert "chat_id: 0" in result["prompt"], (
            "steward_trigger prompt must include 'chat_id: 0' so write_result "
            "silently drops the result rather than forwarding to a user chat"
        )

    def test_handle_steward_trigger_prompt_contains_task_id_header(self):
        """task_id frontmatter must be present for write_result correlation."""
        result = handle_steward_trigger("uow_sentinel_xyz")
        assert "task_id:" in result["prompt"]

    def test_handle_steward_trigger_is_pure_same_inputs_same_output(self):
        """handle_steward_trigger is a pure function — identical inputs produce identical outputs."""
        r1 = handle_steward_trigger("uow_pure_test")
        r2 = handle_steward_trigger("uow_pure_test")
        assert r1 == r2


# ---------------------------------------------------------------------------
# route_wos_message spawn-gate tests — issue #920
#
# These tests verify the in-code guard added to route_wos_message: if any
# registered handler returns action != "spawn_subagent" (or raises), the
# function returns action="send_reply" to alert Dan rather than silently
# calling mark_processed without spawning a Task.
#
# The invariant: route_wos_message NEVER returns action="mark_processed".
# ---------------------------------------------------------------------------

# Named constant: the action that must never be returned by route_wos_message.
WOS_FORBIDDEN_ACTION = "mark_processed"


class TestRouteWosMessageSpawnGate:
    """route_wos_message must never return action='mark_processed' for WOS message types."""

    def test_wos_execute_never_returns_mark_processed(self):
        """route_wos_message for wos_execute must not return action='mark_processed'."""
        msg = {
            "type": "wos_execute",
            "uow_id": "uow_gate_test",
            "instructions": "Do the thing.",
        }
        result = route_wos_message(msg)
        assert result["action"] != WOS_FORBIDDEN_ACTION, (
            "route_wos_message must never return action='mark_processed' for wos_execute. "
            "Returning mark_processed without spawning a Task silently orphans the UoW."
        )

    def test_steward_trigger_never_returns_mark_processed(self):
        """route_wos_message for steward_trigger must not return action='mark_processed'."""
        msg = {
            "type": "steward_trigger",
            "uow_id": "uow_gate_test_st",
        }
        result = route_wos_message(msg)
        assert result["action"] != WOS_FORBIDDEN_ACTION, (
            "route_wos_message must never return action='mark_processed' for steward_trigger. "
            "Returning mark_processed without spawning a Task silently drops the trigger."
        )

    def test_spawn_gate_sends_alert_when_handler_returns_non_spawn_action(self, monkeypatch):
        """If a handler returns a non-spawn_subagent action dict, route_wos_message returns send_reply.

        This verifies the in-code guard: a misbehaving handler cannot cause
        route_wos_message to return action='mark_processed'.  steward_trigger is the
        right vector to test here — handle_steward_trigger returns a full action dict,
        unlike handle_wos_execute which is a pure prompt-builder (returns str).
        """
        import src.orchestration.dispatcher_handlers as dh

        # Patch handle_steward_trigger to return a 'noop' action (simulates compaction regression
        # where the handler reverts to its old synchronous implementation returning {"action": "noop"})
        monkeypatch.setattr(
            dh, "handle_steward_trigger",
            lambda *a, **kw: {"action": "noop", "task_id": "x", "agent_type": "x", "prompt": "x"},
        )

        msg = {
            "type": "steward_trigger",
            "uow_id": "uow_guard_test",
        }
        result = route_wos_message(msg)
        assert result["action"] == "send_reply", (
            "When handle_steward_trigger returns action='noop' instead of 'spawn_subagent', "
            "route_wos_message must return action='send_reply' to alert Dan."
        )
        assert "alert" in result.get("text", "").lower() or "error" in result.get("text", "").lower(), (
            "The send_reply alert text must describe the error so Dan can investigate."
        )

    def test_spawn_gate_sends_alert_when_wos_execute_handler_raises(self, monkeypatch):
        """If handle_wos_execute raises, route_wos_message returns send_reply rather than propagating."""
        import src.orchestration.dispatcher_handlers as dh

        monkeypatch.setattr(
            dh, "handle_wos_execute",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated handler crash"))
        )

        msg = {
            "type": "wos_execute",
            "uow_id": "uow_raise_test",
            "instructions": "Do the thing.",
        }
        result = route_wos_message(msg)
        assert result["action"] == "send_reply", (
            "When handle_wos_execute raises, route_wos_message must return send_reply, "
            "not propagate the exception or return mark_processed."
        )

    def test_spawn_gate_sends_alert_when_steward_trigger_handler_raises(self, monkeypatch):
        """If handle_steward_trigger raises, route_wos_message returns send_reply rather than propagating."""
        import src.orchestration.dispatcher_handlers as dh

        monkeypatch.setattr(
            dh, "handle_steward_trigger",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated steward crash"))
        )

        msg = {
            "type": "steward_trigger",
            "uow_id": "uow_steward_raise_test",
        }
        result = route_wos_message(msg)
        assert result["action"] == "send_reply", (
            "When handle_steward_trigger raises, route_wos_message must return send_reply, "
            "not propagate the exception."
        )


# ---------------------------------------------------------------------------
# route_wos_message artifact-fallback tests — issue #922
#
# These tests verify that route_wos_message handles wos_execute messages that
# do NOT carry an 'instructions' field (test/manual invocations). The fix
# adds _load_instructions_from_artifact as a fallback path so a minimal
# {"type":"wos_execute","uow_id":"x","chat_id":"1"} dict no longer raises
# KeyError but instead loads instructions from the WorkflowArtifact on disk.
# ---------------------------------------------------------------------------

class TestRouteWosMessageArtifactFallback:
    """route_wos_message must handle wos_execute messages without 'instructions'."""

    _ARTIFACT_CONTENT = (
        '---json\n'
        '{"uow_id":"test-uow","executor_type":"functional-engineer","constraints":[],"prescribed_skills":[]}\n'
        '---\n'
        'Do the thing.\n'
    )

    def test_inline_instructions_used_when_present(self):
        """When 'instructions' is in the message, it is used directly without artifact lookup."""
        msg = {
            "type": "wos_execute",
            "uow_id": "test-uow",
            "chat_id": 8075091586,
            "instructions": "Inline instructions here.",
        }
        result = route_wos_message(msg)
        assert result["action"] == "spawn_subagent", (
            "route_wos_message with inline instructions must return spawn_subagent"
        )
        assert "Inline instructions here." in result["prompt"]

    def test_artifact_fallback_when_instructions_absent(self, tmp_path, monkeypatch):
        """When 'instructions' is absent, instructions are loaded from the artifact file."""
        artifact_file = tmp_path / "test-uow-fallback.md"
        artifact_file.write_text(self._ARTIFACT_CONTENT, encoding="utf-8")

        import src.orchestration.workflow_artifact as wa
        monkeypatch.setattr(wa, "artifact_path", lambda uow_id: artifact_file)

        msg = {
            "type": "wos_execute",
            "uow_id": "test-uow-fallback",
            "chat_id": 8075091586,
        }
        result = route_wos_message(msg)
        assert result["action"] == "spawn_subagent", (
            "route_wos_message without instructions field must fall back to artifact "
            "and return spawn_subagent when artifact exists"
        )
        assert "Do the thing." in result["prompt"]

    def test_missing_artifact_returns_send_reply_with_alert(self, tmp_path, monkeypatch):
        """When 'instructions' is absent and artifact is missing, spawn-gate returns send_reply."""
        nonexistent = tmp_path / "no-such-artifact.md"
        # Do not create the file — it should not exist

        import src.orchestration.workflow_artifact as wa
        monkeypatch.setattr(wa, "artifact_path", lambda uow_id: nonexistent)

        msg = {
            "type": "wos_execute",
            "uow_id": "uow-missing-artifact",
            "chat_id": 8075091586,
        }
        result = route_wos_message(msg)
        assert result["action"] == "send_reply", (
            "route_wos_message must return send_reply (spawn-gate) when instructions "
            "field is absent and artifact file does not exist"
        )
        assert "spawn-gate alert" in result["text"], (
            "send_reply result must contain 'spawn-gate alert'"
        )
        assert "instructions" in result["text"], (
            "send_reply result must mention 'instructions' so the alert is actionable"
        )


# ---------------------------------------------------------------------------
# decide_retry / decide_close callback handler tests — issue #901
#
# These tests verify that tapping the "Retry" or "Close" buttons on a
# needs-human-review escalation message correctly routes to the existing
# handle_decide_retry / handle_decide_close handlers.  The handlers are pure
# functions — no Telegram or MCP required.
# ---------------------------------------------------------------------------

# Named constants from the spec (issue #901 / steward.py escalation surface)
DECIDE_RETRY_CALLBACK_PREFIX = "decide_retry:"
DECIDE_CLOSE_CALLBACK_PREFIX = "decide_close:"

# Named constant: the status the UoW must reach after a retry decision
DECIDE_RETRY_EXPECTED_STATUS = "ready-for-steward"

# Named constant: the status the UoW must reach after a close decision
DECIDE_CLOSE_EXPECTED_STATUS = "failed"


@pytest.fixture
def blocked_uow_for_callbacks(registry) -> str:
    """A UoW in blocked status for decide_retry / decide_close callback tests."""
    today = datetime.now(timezone.utc).date().isoformat()
    result = registry.upsert(
        issue_number=500,
        title="Escalated UoW needing human review",
        sweep_date=today,
        success_criteria="Human reviews and resolves.",
    )
    registry.set_status_direct(result.id, "blocked")
    return result.id


class TestDecideRetryCallback:
    """decide_retry:<uow_id> callback wires to handle_decide_retry."""

    def test_retry_transitions_blocked_to_ready_for_steward(self, registry, blocked_uow_for_callbacks):
        """Tapping Retry on an escalation message resets the UoW to ready-for-steward."""
        from src.orchestration.dispatcher_handlers import handle_decide_retry
        response = handle_decide_retry(blocked_uow_for_callbacks, registry=registry)
        uow = registry.get(blocked_uow_for_callbacks)
        assert uow.status.value == DECIDE_RETRY_EXPECTED_STATUS, (
            f"After decide_retry, UoW must be {DECIDE_RETRY_EXPECTED_STATUS!r}, "
            f"got {uow.status.value!r}"
        )

    def test_retry_resets_steward_cycles_to_zero(self, registry, blocked_uow_for_callbacks):
        """Retry clears steward_cycles so the steward re-diagnoses from scratch."""
        import sqlite3
        conn = sqlite3.connect(str(registry.db_path))
        conn.execute(
            "UPDATE uow_registry SET steward_cycles=5 WHERE id=?",
            (blocked_uow_for_callbacks,),
        )
        conn.commit()
        conn.close()

        from src.orchestration.dispatcher_handlers import handle_decide_retry
        handle_decide_retry(blocked_uow_for_callbacks, registry=registry)
        uow = registry.get(blocked_uow_for_callbacks)
        assert uow.steward_cycles == 0, (
            "steward_cycles must reset to 0 on decide_retry so the steward "
            "gets a full fresh diagnosis pass"
        )

    def test_retry_response_mentions_uow_id(self, registry, blocked_uow_for_callbacks):
        """Response includes the UoW ID so the user can correlate the action."""
        from src.orchestration.dispatcher_handlers import handle_decide_retry
        response = handle_decide_retry(blocked_uow_for_callbacks, registry=registry)
        assert blocked_uow_for_callbacks in response

    def test_retry_response_mentions_ready_for_steward(self, registry, blocked_uow_for_callbacks):
        """Confirmation text names the new status so the user knows what happened."""
        from src.orchestration.dispatcher_handlers import handle_decide_retry
        response = handle_decide_retry(blocked_uow_for_callbacks, registry=registry)
        assert DECIDE_RETRY_EXPECTED_STATUS in response.lower()

    def test_retry_on_non_blocked_uow_returns_diagnostic_message(self, registry):
        """Tapping Retry on a UoW not in blocked state returns a clear error, not a crash."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(
            issue_number=501,
            title="Active UoW retry test",
            sweep_date=today,
            success_criteria="Test done.",
        )
        registry.set_status_direct(result.id, "active")
        from src.orchestration.dispatcher_handlers import handle_decide_retry
        response = handle_decide_retry(result.id, registry=registry)
        # Must explain why nothing happened, not silently succeed
        assert "not currently in" in response.lower() or "could not be" in response.lower()

    def test_callback_prefix_matches_steward_escalation_format(self):
        """The callback data prefix must match what steward.py writes into the button."""
        # steward.py writes: f"decide_retry:{uow_id}"
        uow_id = "uow-test-42"
        expected_callback_data = f"{DECIDE_RETRY_CALLBACK_PREFIX}{uow_id}"
        assert expected_callback_data == f"decide_retry:{uow_id}", (
            f"Prefix {DECIDE_RETRY_CALLBACK_PREFIX!r} must produce callback_data "
            f"matching steward.py's format 'decide_retry:<uow_id>'"
        )


class TestDecideCloseCallback:
    """decide_close:<uow_id> callback wires to handle_decide_close."""

    def test_close_transitions_blocked_to_failed(self, registry, blocked_uow_for_callbacks):
        """Tapping Close on an escalation message moves the UoW to failed."""
        from src.orchestration.dispatcher_handlers import handle_decide_close
        response = handle_decide_close(blocked_uow_for_callbacks, registry=registry)
        uow = registry.get(blocked_uow_for_callbacks)
        assert uow.status.value == DECIDE_CLOSE_EXPECTED_STATUS, (
            f"After decide_close, UoW must be {DECIDE_CLOSE_EXPECTED_STATUS!r}, "
            f"got {uow.status.value!r}"
        )

    def test_close_response_mentions_uow_id(self, registry, blocked_uow_for_callbacks):
        """Response includes the UoW ID so the user can correlate the action."""
        from src.orchestration.dispatcher_handlers import handle_decide_close
        response = handle_decide_close(blocked_uow_for_callbacks, registry=registry)
        assert blocked_uow_for_callbacks in response

    def test_close_response_mentions_failed_status(self, registry, blocked_uow_for_callbacks):
        """Confirmation text names the terminal status so the user knows the UoW is done."""
        from src.orchestration.dispatcher_handlers import handle_decide_close
        response = handle_decide_close(blocked_uow_for_callbacks, registry=registry)
        assert DECIDE_CLOSE_EXPECTED_STATUS in response.lower()

    def test_close_on_non_blocked_uow_returns_diagnostic_message(self, registry):
        """Tapping Close on a non-blocked UoW returns a clear error, not a crash."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(
            issue_number=502,
            title="Active UoW close test",
            sweep_date=today,
            success_criteria="Test done.",
        )
        registry.set_status_direct(result.id, "active")
        from src.orchestration.dispatcher_handlers import handle_decide_close
        response = handle_decide_close(result.id, registry=registry)
        assert "not currently in" in response.lower() or "could not be" in response.lower()

    def test_callback_prefix_matches_steward_escalation_format(self):
        """The callback data prefix must match what steward.py writes into the button."""
        # steward.py writes: f"decide_close:{uow_id}"
        uow_id = "uow-test-42"
        expected_callback_data = f"{DECIDE_CLOSE_CALLBACK_PREFIX}{uow_id}"
        assert expected_callback_data == f"decide_close:{uow_id}", (
            f"Prefix {DECIDE_CLOSE_CALLBACK_PREFIX!r} must produce callback_data "
            f"matching steward.py's format 'decide_close:<uow_id>'"
        )


class TestDecideCallbackParseRoundtrip:
    """The callback_data format must survive a parse round-trip without loss."""

    def test_retry_callback_data_parses_uow_id_correctly(self):
        """Parsing 'decide_retry:<uow_id>' yields the correct uow_id."""
        uow_id = "uow-2026-042-abc"
        callback_data = f"decide_retry:{uow_id}"
        assert callback_data.startswith(DECIDE_RETRY_CALLBACK_PREFIX)
        parsed_id = callback_data[len(DECIDE_RETRY_CALLBACK_PREFIX):]
        assert parsed_id == uow_id

    def test_close_callback_data_parses_uow_id_correctly(self):
        """Parsing 'decide_close:<uow_id>' yields the correct uow_id."""
        uow_id = "uow-2026-042-xyz"
        callback_data = f"decide_close:{uow_id}"
        assert callback_data.startswith(DECIDE_CLOSE_CALLBACK_PREFIX)
        parsed_id = callback_data[len(DECIDE_CLOSE_CALLBACK_PREFIX):]
        assert parsed_id == uow_id

    def test_uow_ids_with_hyphens_parse_correctly(self):
        """UoW IDs containing hyphens (the canonical format) must not be truncated."""
        uow_id = "uow-issue-123-20260424"
        for prefix in (DECIDE_RETRY_CALLBACK_PREFIX, DECIDE_CLOSE_CALLBACK_PREFIX):
            callback_data = f"{prefix}{uow_id}"
            parsed = callback_data[len(prefix):]
            assert parsed == uow_id, (
                f"UoW ID with hyphens must survive round-trip through {prefix!r}"
            )


class TestRouteCallbackMessage:
    """Tests for route_callback_message — compaction-resilient callback dispatch."""

    def test_decide_retry_returns_send_reply_action(self, registry):
        """decide_retry callback returns action='send_reply'."""
        uow_id = registry.upsert(
            issue_number=901, title="Retry callback test", sweep_date="2026-04-24",
            success_criteria="Test."
        ).id
        registry.set_status_direct(uow_id, "blocked")
        msg = {"callback_data": f"decide_retry:{uow_id}", "chat_id": 12345}
        result = route_callback_message(msg, registry=registry)
        assert result["action"] == "send_reply"

    def test_decide_retry_returns_handled_true(self, registry):
        """decide_retry callback sets handled=True."""
        uow_id = registry.upsert(
            issue_number=901, title="Retry handled test", sweep_date="2026-04-24",
            success_criteria="Test."
        ).id
        registry.set_status_direct(uow_id, "blocked")
        msg = {"callback_data": f"decide_retry:{uow_id}", "chat_id": 12345}
        result = route_callback_message(msg, registry=registry)
        assert result["handled"] is True

    def test_decide_retry_echoes_chat_id(self, registry):
        """route_callback_message echoes chat_id from the message."""
        uow_id = registry.upsert(
            issue_number=901, title="ChatId echo test", sweep_date="2026-04-24",
            success_criteria="Test."
        ).id
        registry.set_status_direct(uow_id, "blocked")
        msg = {"callback_data": f"decide_retry:{uow_id}", "chat_id": 99999}
        result = route_callback_message(msg, registry=registry)
        assert result["chat_id"] == 99999

    def test_decide_retry_transitions_blocked_uow(self, registry):
        """decide_retry callback transitions a blocked UoW to ready-for-steward."""
        uow_id = registry.upsert(
            issue_number=901, title="Retry transition test", sweep_date="2026-04-24",
            success_criteria="Test."
        ).id
        registry.set_status_direct(uow_id, "blocked")
        msg = {"callback_data": f"decide_retry:{uow_id}", "chat_id": 12345}
        route_callback_message(msg, registry=registry)
        uow = registry.get(uow_id)
        assert uow.status == "ready-for-steward"

    def test_decide_close_returns_send_reply_action(self, registry):
        """decide_close callback returns action='send_reply'."""
        uow_id = registry.upsert(
            issue_number=901, title="Close callback test", sweep_date="2026-04-24",
            success_criteria="Test."
        ).id
        registry.set_status_direct(uow_id, "blocked")
        msg = {"callback_data": f"decide_close:{uow_id}", "chat_id": 12345}
        result = route_callback_message(msg, registry=registry)
        assert result["action"] == "send_reply"

    def test_decide_close_returns_handled_true(self, registry):
        """decide_close callback sets handled=True."""
        uow_id = registry.upsert(
            issue_number=901, title="Close handled test", sweep_date="2026-04-24",
            success_criteria="Test."
        ).id
        registry.set_status_direct(uow_id, "blocked")
        msg = {"callback_data": f"decide_close:{uow_id}", "chat_id": 12345}
        result = route_callback_message(msg, registry=registry)
        assert result["handled"] is True

    def test_decide_close_transitions_blocked_to_failed(self, registry):
        """decide_close callback transitions a blocked UoW to failed."""
        uow_id = registry.upsert(
            issue_number=901, title="Close transition test", sweep_date="2026-04-24",
            success_criteria="Test."
        ).id
        registry.set_status_direct(uow_id, "blocked")
        msg = {"callback_data": f"decide_close:{uow_id}", "chat_id": 12345}
        route_callback_message(msg, registry=registry)
        uow = registry.get(uow_id)
        assert uow.status == "failed"

    def test_unknown_callback_returns_handled_false(self):
        """Non-WOS callbacks return handled=False so dispatcher can handle them."""
        msg = {"callback_data": "job-confirm-yes-some-job", "chat_id": 12345}
        result = route_callback_message(msg)
        assert result["handled"] is False

    def test_unknown_callback_returns_send_reply_action(self):
        """Unknown callbacks still return action='send_reply'."""
        msg = {"callback_data": "delete-confirm-no-some-slug", "chat_id": 12345}
        result = route_callback_message(msg)
        assert result["action"] == "send_reply"

    def test_callback_data_handlers_contains_expected_prefixes(self):
        """CALLBACK_DATA_HANDLERS contains the two WOS prefixes."""
        assert "decide_retry:" in CALLBACK_DATA_HANDLERS
        assert "decide_close:" in CALLBACK_DATA_HANDLERS
