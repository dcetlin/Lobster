"""
Tests for dispatcher command handlers for /approve, /wos status, and /wos unblock.

These test the pure handler functions in isolation — no Telegram or MCP required.
The handlers receive parsed command arguments and a Registry instance, and return
a formatted string response.
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest

from src.orchestration.dispatcher_handlers import handle_approve, handle_confirm, handle_decide, handle_wos_execute, handle_wos_status, handle_wos_unblock


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
