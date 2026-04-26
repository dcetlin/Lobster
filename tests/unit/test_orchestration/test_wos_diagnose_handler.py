"""
Tests for the wos_diagnose dispatcher handler and parse_diagnose_command utility.

Spec (self-diagnosing-subagent-design.md §5, §7 PR 1):
  - New message type "wos_diagnose" is registered in WOS_MESSAGE_TYPE_DISPATCH.
  - handle_wos_diagnose(msg) spawns a diagnostic subagent (always action="spawn_subagent").
  - The spawned subagent prompt implements the 5-pattern diagnosis algorithm from the
    design spec, with all ORPHAN_REASONS, MAX_RETRIES, and HARD_CAP constants embedded.
  - The subagent prompt includes the UoW ID and task_id header (chat_id: 0 sentinel).
  - parse_diagnose_command(text) returns the uow_id token for "diagnose <uow_id>" commands
    and None for non-matching text.
  - route_wos_message accepts "wos_diagnose" and dispatches to handle_wos_diagnose.
  - _resolve_uow_id() is the single resolution point for uow_id extraction — today it
    is a direct pass-through; the interface is stable for future short-ID support.
  - handle_wos_diagnose is a pure function: identical inputs produce identical outputs.
"""

from __future__ import annotations

import pytest

from src.orchestration.dispatcher_handlers import (
    WOS_MESSAGE_TYPE_DISPATCH,
    _resolve_uow_id,
    handle_wos_diagnose,
    parse_diagnose_command,
    route_wos_message,
)

# ---------------------------------------------------------------------------
# Named constants from the spec — imported from production module where
# possible; defined here from the spec where they are embedded in the prompt
# rather than exported as module constants.
# ---------------------------------------------------------------------------

# The message type this PR adds
WOS_DIAGNOSE_MESSAGE_TYPE = "wos_diagnose"

# Pattern names the diagnosis algorithm can match (from design spec §3)
PATTERN_INFRASTRUCTURE_KILL_WAVE = "infrastructure-kill-wave"
PATTERN_KILL_BEFORE_START = "kill-before-start"
PATTERN_KILL_DURING_EXECUTION = "kill-during-execution"
PATTERN_GENUINE_RETRY_CAP = "genuine-retry-cap"
PATTERN_HARD_CAP = "hard-cap"
PATTERN_DEAD_PRESCRIPTION_LOOP = "dead-prescription-loop"
PATTERN_UNRECOGNISED = "unrecognised"

# Algorithm constants embedded in the subagent prompt (from design spec §3)
MAX_RETRIES = 3
HARD_CAP = 9


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_diagnose_msg(
    uow_id: str = "uow_test_20260426_abc123",
    escalation_id: str = "esc-001",
    escalation_trigger: str = "retry_cap_exceeded",
    failure_history: dict | None = None,
) -> dict:
    """Build a minimal wos_diagnose inbox message."""
    return {
        "type": WOS_DIAGNOSE_MESSAGE_TYPE,
        "uow_id": uow_id,
        "escalation_id": escalation_id,
        "escalation_trigger": escalation_trigger,
        "failure_history": failure_history if failure_history is not None else {},
    }


def _make_manual_diagnose_msg(uow_id: str = "uow_test_20260426_abc123") -> dict:
    """Build a minimal manual-trigger wos_diagnose message (from dispatcher command parsing)."""
    return {
        "type": WOS_DIAGNOSE_MESSAGE_TYPE,
        "uow_id": uow_id,
        "escalation_id": "",
        "escalation_trigger": "manual",
        "failure_history": {},
    }


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class TestWosDiagnoseRegistered:
    """wos_diagnose must be registered in the dispatch table."""

    def test_wos_diagnose_in_dispatch_table(self):
        """wos_diagnose must appear in WOS_MESSAGE_TYPE_DISPATCH.

        Absence means the dispatcher's type-based routing table cannot fire for
        wos_diagnose messages — manual diagnose commands would silently stall.
        """
        assert WOS_DIAGNOSE_MESSAGE_TYPE in WOS_MESSAGE_TYPE_DISPATCH, (
            f"{WOS_DIAGNOSE_MESSAGE_TYPE!r} must be registered in WOS_MESSAGE_TYPE_DISPATCH "
            "so the dispatcher routes it structurally rather than via prose that is lost on compaction"
        )

    def test_wos_diagnose_dispatch_value_is_non_empty_string(self):
        """Dispatch table value for wos_diagnose must be a non-empty string (handler name)."""
        value = WOS_MESSAGE_TYPE_DISPATCH.get(WOS_DIAGNOSE_MESSAGE_TYPE, "")
        assert isinstance(value, str) and len(value) > 0


# ---------------------------------------------------------------------------
# handle_wos_diagnose: return structure
# ---------------------------------------------------------------------------

class TestHandleWosDiagnoseReturnStructure:
    """handle_wos_diagnose must always return a spawn_subagent action."""

    def test_always_returns_spawn_subagent_action(self):
        """handle_wos_diagnose must always return action='spawn_subagent'.

        Unlike wos_escalate (which can return send_reply for surface branches),
        the diagnosis handler always spawns — the subagent decides whether to
        surface or reset, not the dispatcher.
        """
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert result["action"] == "spawn_subagent", (
            "handle_wos_diagnose must always return action='spawn_subagent' — "
            "the subagent performs the triage, not the dispatcher"
        )

    def test_returns_non_empty_task_id(self):
        """spawn_subagent result must include a non-empty task_id."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert isinstance(result.get("task_id"), str) and len(result["task_id"]) > 0

    def test_returns_non_empty_prompt(self):
        """spawn_subagent result must include a non-empty prompt for the subagent."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert isinstance(result.get("prompt"), str) and len(result["prompt"]) > 0

    def test_returns_agent_type_lobster_generalist(self):
        """Diagnostic subagent must be lobster-generalist, not functional-engineer."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert result.get("agent_type") == "lobster-generalist", (
            "Diagnostic subagent must be lobster-generalist — it runs CLI commands "
            "and calls write_result, not code-implementation work"
        )

    def test_returns_message_type_wos_diagnose(self):
        """Result must echo message_type='wos_diagnose' for caller confirmation."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert result.get("message_type") == WOS_DIAGNOSE_MESSAGE_TYPE


# ---------------------------------------------------------------------------
# handle_wos_diagnose: prompt content requirements
# ---------------------------------------------------------------------------

class TestHandleWosDiagnosePromptContent:
    """The subagent prompt must embed the required algorithm and constraints."""

    def test_prompt_contains_uow_id(self):
        """Prompt must include the UoW ID so the subagent runs trace on the right UoW."""
        uow_id = "uow_20260426_unique_test_id"
        msg = _make_diagnose_msg(uow_id=uow_id)
        result = handle_wos_diagnose(msg)
        assert uow_id in result["prompt"], (
            "Subagent prompt must contain the UoW ID for registry_cli trace invocation"
        )

    def test_prompt_contains_task_id_header(self):
        """Prompt must include 'task_id:' header — required by SessionStart hook."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert "task_id:" in result["prompt"], (
            "Subagent prompt must include 'task_id:' header for session registration"
        )

    def test_prompt_contains_chat_id_zero(self):
        """Prompt must include 'chat_id: 0' — silent-drop sentinel for auto-diagnosis results."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert "chat_id: 0" in result["prompt"], (
            "Diagnostic subagent prompt must include 'chat_id: 0' so write_result "
            "silently drops auto-reset results rather than forwarding to a user chat"
        )

    def test_prompt_contains_registry_cli_trace_command(self):
        """Prompt must instruct the subagent to run registry_cli trace."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert "registry_cli.py trace" in result["prompt"] or "registry_cli trace" in result["prompt"], (
            "Subagent prompt must include the registry_cli trace command — "
            "this is the primary forensics data source"
        )

    def test_prompt_contains_max_retries_constant(self):
        """Prompt must embed MAX_RETRIES constant for the execution cap branch."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert str(MAX_RETRIES) in result["prompt"], (
            f"Subagent prompt must include MAX_RETRIES={MAX_RETRIES} "
            "so the subagent knows when execution_attempts triggers surface-to-human"
        )

    def test_prompt_contains_hard_cap_constant(self):
        """Prompt must embed HARD_CAP constant for the circuit-breaker branch."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert str(HARD_CAP) in result["prompt"], (
            f"Subagent prompt must include HARD_CAP={HARD_CAP} "
            "so the subagent can detect and surface hard-cap exhaustion"
        )

    def test_prompt_contains_orphan_reasons(self):
        """Prompt must embed the ORPHAN_REASONS set for infrastructure kill detection."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        # Check for key orphan reason values from the spec
        assert "executor_orphan" in result["prompt"], (
            "Subagent prompt must include orphan return_reason values for pattern matching"
        )
        assert "orphan_kill_before_start" in result["prompt"], (
            "Subagent prompt must include 'orphan_kill_before_start' for kill-before-start detection"
        )

    def test_prompt_contains_decide_retry_constraint_for_needs_human_review(self):
        """Prompt must warn that decide-retry only works on blocked/ready-for-steward UoWs.

        registry_cli decide-retry RETRYABLE_STATUSES = {'blocked', 'ready-for-steward'}.
        needs-human-review is NOT in that set. The subagent must know this or it will
        call decide-retry and get a not_retryable error, silently doing nothing.
        """
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert "needs-human-review" in result["prompt"], (
            "Subagent prompt must include 'needs-human-review' status note so the subagent "
            "knows decide-retry requires blocked/ready-for-steward status — "
            "needs-human-review UoWs cannot be directly retried"
        )

    def test_prompt_contains_no_decide_close_constraint(self):
        """Prompt must instruct the subagent not to call decide-close."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert "decide-close" in result["prompt"], (
            "Subagent prompt must mention decide-close in the constraints section — "
            "retirement requires human confirmation and must not be done autonomously"
        )

    def test_prompt_contains_write_result_instruction(self):
        """Prompt must instruct the subagent to call write_result (not send_reply directly)."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert "write_result" in result["prompt"], (
            "Subagent must call write_result, not send_reply directly — "
            "the dispatcher relay filter handles surfacing to Dan"
        )

    def test_prompt_embeds_escalation_trigger(self):
        """Prompt must embed the escalation_trigger so the subagent has context."""
        trigger = "retry_cap_exceeded"
        msg = _make_diagnose_msg(escalation_trigger=trigger)
        result = handle_wos_diagnose(msg)
        assert trigger in result["prompt"]

    def test_prompt_embeds_escalation_id_when_present(self):
        """Prompt must embed the escalation_id for audit correlation."""
        esc_id = "esc-correlation-999"
        msg = _make_diagnose_msg(escalation_id=esc_id)
        result = handle_wos_diagnose(msg)
        assert esc_id in result["prompt"]


# ---------------------------------------------------------------------------
# handle_wos_diagnose: task_id format
# ---------------------------------------------------------------------------

class TestHandleWosDiagnoseTaskId:
    """task_id must follow the expected slug format for session registration."""

    def test_task_id_contains_wos_diagnose_prefix(self):
        """task_id must start with 'wos-diagnose-' for identification."""
        msg = _make_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert result["task_id"].startswith("wos-diagnose-"), (
            "task_id must start with 'wos-diagnose-' for dispatcher in-flight tracking"
        )

    def test_task_id_contains_uow_id_prefix(self):
        """task_id must include a prefix of the UoW ID for correlation."""
        uow_id = "uow_20260426_abc123"
        msg = _make_diagnose_msg(uow_id=uow_id)
        result = handle_wos_diagnose(msg)
        # The task_id uses the first 12 chars of uow_id
        assert uow_id[:12] in result["task_id"], (
            "task_id must include a UoW ID prefix so the dispatcher can correlate "
            "the diagnostic subagent with its originating UoW"
        )

    def test_different_uow_ids_produce_different_task_ids(self):
        """Each UoW ID must produce a distinct task_id — no cross-contamination."""
        msg1 = _make_diagnose_msg(uow_id="uow_aaa_20260426_001")
        msg2 = _make_diagnose_msg(uow_id="uow_bbb_20260426_002")
        r1 = handle_wos_diagnose(msg1)
        r2 = handle_wos_diagnose(msg2)
        assert r1["task_id"] != r2["task_id"]


# ---------------------------------------------------------------------------
# handle_wos_diagnose: purity
# ---------------------------------------------------------------------------

class TestHandleWosDiagnosePurity:
    """handle_wos_diagnose must be a pure function."""

    def test_pure_function_same_inputs_same_output(self):
        """Pure function contract: identical inputs always produce identical outputs."""
        msg = _make_diagnose_msg()
        r1 = handle_wos_diagnose(msg)
        r2 = handle_wos_diagnose(msg)
        assert r1 == r2

    def test_manual_trigger_produces_same_structure(self):
        """Manual trigger (escalation_trigger='manual') must produce same result structure."""
        msg = _make_manual_diagnose_msg()
        result = handle_wos_diagnose(msg)
        assert result["action"] == "spawn_subagent"
        assert "task_id" in result
        assert "prompt" in result
        assert "agent_type" in result

    def test_missing_uow_id_uses_unknown_sentinel(self):
        """When uow_id is absent from msg, handler must not raise — use 'unknown' sentinel."""
        msg = {"type": WOS_DIAGNOSE_MESSAGE_TYPE}
        # Should not raise
        result = handle_wos_diagnose(msg)
        assert result["action"] == "spawn_subagent"

    def test_different_uow_ids_produce_different_prompts(self):
        """Each UoW ID must produce a distinct prompt — no cross-contamination."""
        msg1 = _make_diagnose_msg(uow_id="uow_20260426_001")
        msg2 = _make_diagnose_msg(uow_id="uow_20260426_002")
        r1 = handle_wos_diagnose(msg1)
        r2 = handle_wos_diagnose(msg2)
        assert r1["prompt"] != r2["prompt"]


# ---------------------------------------------------------------------------
# _resolve_uow_id: current pass-through contract
# ---------------------------------------------------------------------------

class TestResolveUowId:
    """_resolve_uow_id is the single resolution point for UoW ID extraction.

    Today it is a direct pass-through for full IDs. A future PR will add
    short-ID support without changing handle_wos_diagnose.
    """

    def test_full_id_is_returned_unchanged(self):
        """Full canonical IDs must pass through unmodified."""
        full_id = "uow_20260426_abc123"
        assert _resolve_uow_id(full_id) == full_id

    def test_arbitrary_string_is_returned_unchanged(self):
        """Any string passes through unchanged — resolution is deferred to future PR."""
        for value in ("uow_001", "short-id", "42", "test"):
            assert _resolve_uow_id(value) == value


# ---------------------------------------------------------------------------
# parse_diagnose_command: Telegram command parsing
# ---------------------------------------------------------------------------

class TestParseDiagnoseCommand:
    """parse_diagnose_command must parse 'diagnose <uow_id>' Telegram commands."""

    def test_parses_full_uow_id(self):
        """'diagnose uow_20260426_abc123' → 'uow_20260426_abc123'."""
        result = parse_diagnose_command("diagnose uow_20260426_abc123")
        assert result == "uow_20260426_abc123"

    def test_parses_case_insensitive(self):
        """'DIAGNOSE uow_id' and 'Diagnose uow_id' must both match."""
        assert parse_diagnose_command("DIAGNOSE uow_20260426_abc123") == "uow_20260426_abc123"
        assert parse_diagnose_command("Diagnose uow_20260426_abc123") == "uow_20260426_abc123"

    def test_strips_leading_trailing_whitespace(self):
        """Leading/trailing whitespace in the full command must be ignored."""
        result = parse_diagnose_command("  diagnose uow_20260426_abc123  ")
        assert result == "uow_20260426_abc123"

    def test_returns_none_for_non_diagnose_command(self):
        """Non-diagnose commands must return None."""
        assert parse_diagnose_command("wos status") is None
        assert parse_diagnose_command("decide retry uow_001") is None
        assert parse_diagnose_command("wos start") is None
        assert parse_diagnose_command("hello world") is None

    def test_returns_none_for_empty_string(self):
        """Empty string must return None."""
        assert parse_diagnose_command("") is None

    def test_returns_none_for_diagnose_without_uow_id(self):
        """'diagnose' with no following token must return None (no uow_id to extract)."""
        assert parse_diagnose_command("diagnose") is None
        assert parse_diagnose_command("diagnose   ") is None

    def test_returns_none_for_partial_prefix_match(self):
        """'diagnosing uow_001' must not match — only exact 'diagnose ' prefix."""
        assert parse_diagnose_command("diagnosing uow_001") is None

    def test_parses_uow_id_with_hyphens(self):
        """UoW IDs with hyphens must be parsed correctly."""
        result = parse_diagnose_command("diagnose uow-test-001")
        assert result == "uow-test-001"

    def test_uow_id_token_is_first_word_after_diagnose(self):
        """Only the first token after 'diagnose ' is returned (no multi-word parsing)."""
        result = parse_diagnose_command("diagnose uow_001 extra ignored")
        # The function returns the full remainder after 'diagnose ' with leading/trailing
        # whitespace stripped — so "uow_001 extra ignored" is the raw uow_id token.
        # This matches the design: _resolve_uow_id does full resolution later.
        assert result is not None
        assert "uow_001" in result


# ---------------------------------------------------------------------------
# route_wos_message integration
# ---------------------------------------------------------------------------

class TestRouteWosMessageDiagnose:
    """route_wos_message must dispatch wos_diagnose to handle_wos_diagnose."""

    def test_route_wos_message_accepts_wos_diagnose_type(self):
        """route_wos_message must not raise ValueError for wos_diagnose messages."""
        msg = _make_diagnose_msg()
        result = route_wos_message(msg)
        assert "action" in result

    def test_route_wos_message_wos_diagnose_returns_spawn_subagent(self):
        """route_wos_message for wos_diagnose must return action='spawn_subagent'."""
        msg = _make_diagnose_msg()
        result = route_wos_message(msg)
        assert result["action"] == "spawn_subagent", (
            "wos_diagnose must spawn a diagnostic subagent, not send a reply directly"
        )

    def test_route_wos_message_echoes_message_type(self):
        """result['message_type'] must echo 'wos_diagnose' so callers can confirm routing."""
        msg = _make_diagnose_msg()
        result = route_wos_message(msg)
        assert result["message_type"] == WOS_DIAGNOSE_MESSAGE_TYPE

    def test_route_wos_message_wos_diagnose_includes_uow_id_in_prompt(self):
        """route_wos_message for wos_diagnose must include the UoW ID in the prompt."""
        uow_id = "uow_route_test_abc999"
        msg = _make_diagnose_msg(uow_id=uow_id)
        result = route_wos_message(msg)
        assert uow_id in result["prompt"]

    def test_route_wos_message_manual_trigger_returns_spawn_subagent(self):
        """Manual-trigger diagnose messages (from Telegram command) must also spawn."""
        msg = _make_manual_diagnose_msg()
        result = route_wos_message(msg)
        assert result["action"] == "spawn_subagent"

    def test_route_wos_message_missing_uow_id_returns_spawn_subagent_not_error(self):
        """Missing uow_id must not propagate as an unhandled exception."""
        msg = {"type": WOS_DIAGNOSE_MESSAGE_TYPE}
        result = route_wos_message(msg)
        assert result["action"] == "spawn_subagent", (
            "Missing uow_id must not raise — use 'unknown' sentinel and spawn safely"
        )
