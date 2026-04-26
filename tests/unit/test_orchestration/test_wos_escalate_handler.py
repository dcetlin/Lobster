"""
Tests for the wos_escalate dispatcher handler.

Spec (dispatcher-escalation-design-20260426.md §2):
  - New message type "wos_escalate" is registered in WOS_MESSAGE_TYPE_DISPATCH.
  - handle_wos_escalate(msg) implements a 4-branch decision tree:
    1. Pure infrastructure failure (execution_attempts == 0, orphan return_reason):
       → auto-retry: returns action="spawn_subagent" targeting the steward heartbeat
    2. Mid-execution kill (execution_attempts > 0, orphan return_reason):
       → retry with extended TTL: returns action="spawn_subagent"
    3. ≥3 execution_attempts (genuine execution failure):
       → surface to Dan: returns action="send_reply" with structured context
    4. human-judgment or philosophical register:
       → immediate surface to Dan: returns action="send_reply"
  - route_wos_message accepts "wos_escalate" and dispatches to handle_wos_escalate.
  - The spawn-gate does NOT apply to wos_escalate — it legitimately returns send_reply
    for the "surface to Dan" branches.
  - handle_wos_escalate is a pure function: identical inputs produce identical outputs.
"""

from __future__ import annotations

import pytest
from src.orchestration.dispatcher_handlers import (
    handle_wos_escalate,
    route_wos_message,
    WOS_MESSAGE_TYPE_DISPATCH,
)

# ---------------------------------------------------------------------------
# Named constants from the spec
# ---------------------------------------------------------------------------

# The message type this PR adds
WOS_ESCALATE_MESSAGE_TYPE = "wos_escalate"

# Execution_attempts threshold at which the handler surfaces to Dan
SURFACE_TO_DAN_EXECUTION_THRESHOLD = 3

# Registers that bypass auto-retry and surface directly to Dan
HUMAN_JUDGMENT_REGISTERS = ("human-judgment", "philosophical")

# Orphan return_reason values — infrastructure kills, no execution occurred
ORPHAN_RETURN_REASONS = ("executor_orphan", "executing_orphan", "diagnosing_orphan",
                         "orphan_kill_before_start", "orphan_kill_during_execution")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_escalate_msg(
    uow_id: str = "uow-test-001",
    execution_attempts: int = 0,
    return_reason_classification: str = "orphan",
    kill_type: str = "orphan_kill_before_start",
    heartbeats_before_kill: int = 0,
    posture: str = "orphan",
    register: str = "operational",
    uow_title: str = "Test UoW",
) -> dict:
    """Build a minimal wos_escalate inbox message."""
    return {
        "type": WOS_ESCALATE_MESSAGE_TYPE,
        "uow_id": uow_id,
        "uow_title": uow_title,
        "failure_history": {
            "execution_attempts": execution_attempts,
            "return_reason_classification": return_reason_classification,
            "kill_type": kill_type,
            "heartbeats_before_kill": heartbeats_before_kill,
        },
        "posture": posture,
        "register": register,
    }


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class TestWosEscalateRegistered:
    """wos_escalate must be registered in the dispatch table."""

    def test_wos_escalate_in_dispatch_table(self):
        """wos_escalate must appear in WOS_MESSAGE_TYPE_DISPATCH.

        Absence means the dispatcher's type-based routing table cannot fire for
        wos_escalate messages — the escalation path would silently stall.
        """
        assert WOS_ESCALATE_MESSAGE_TYPE in WOS_MESSAGE_TYPE_DISPATCH, (
            f"{WOS_ESCALATE_MESSAGE_TYPE!r} must be registered in WOS_MESSAGE_TYPE_DISPATCH "
            "so the dispatcher routes it structurally rather than via prose that is lost on compaction"
        )


# ---------------------------------------------------------------------------
# Branch 1: pure infrastructure failure → auto-retry
# ---------------------------------------------------------------------------

class TestPureInfrastructureFailureAutoRetry:
    """
    Branch 1: execution_attempts == 0 AND orphan return_reason_classification.

    The UoW was never actually executed — the session was killed before execution
    began. Auto-retry is safe because the original prescription is still valid.
    The dispatcher spawns a steward heartbeat subagent to re-queue the UoW.
    """

    def test_pure_infra_kill_returns_spawn_subagent(self):
        """Pure infrastructure kill with 0 execution_attempts → spawn_subagent action."""
        msg = _make_escalate_msg(
            execution_attempts=0,
            return_reason_classification="orphan",
            kill_type="orphan_kill_before_start",
            heartbeats_before_kill=0,
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "spawn_subagent", (
            "Pure infrastructure kill (0 execution_attempts, orphan classification) "
            "must auto-retry by spawning a steward subagent, not surface to Dan"
        )

    def test_pure_infra_kill_includes_task_id(self):
        """spawn_subagent result must include a non-empty task_id."""
        msg = _make_escalate_msg(execution_attempts=0, return_reason_classification="orphan")
        result = handle_wos_escalate(msg)
        assert isinstance(result.get("task_id"), str)
        assert len(result["task_id"]) > 0

    def test_pure_infra_kill_includes_non_empty_prompt(self):
        """spawn_subagent result must include a non-empty prompt for the subagent."""
        msg = _make_escalate_msg(execution_attempts=0, return_reason_classification="orphan")
        result = handle_wos_escalate(msg)
        assert isinstance(result.get("prompt"), str)
        assert len(result["prompt"]) > 0

    def test_pure_infra_kill_prompt_contains_uow_id(self):
        """The subagent prompt must mention the UoW ID for correlation."""
        uow_id = "uow-infra-test-001"
        msg = _make_escalate_msg(uow_id=uow_id, execution_attempts=0, return_reason_classification="orphan")
        result = handle_wos_escalate(msg)
        assert uow_id in result["prompt"]

    def test_pure_infra_kill_prompt_contains_chat_id_zero(self):
        """chat_id: 0 is the silent-drop sentinel — steward results must not be relayed to user."""
        msg = _make_escalate_msg(execution_attempts=0, return_reason_classification="orphan")
        result = handle_wos_escalate(msg)
        assert "chat_id: 0" in result["prompt"], (
            "Steward subagent prompt must include 'chat_id: 0' so write_result "
            "silently drops the result rather than forwarding to a user chat"
        )

    def test_zero_execution_attempts_with_kill_during_execution_is_still_infra_kill(self):
        """kill_during_execution with 0 execution_attempts is still treated as pure infra kill.

        This covers the edge case where the heartbeat was written but the subagent
        was killed before any actual work completed.
        """
        msg = _make_escalate_msg(
            execution_attempts=0,
            return_reason_classification="orphan",
            kill_type="orphan_kill_during_execution",
            heartbeats_before_kill=2,
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "spawn_subagent"


# ---------------------------------------------------------------------------
# Branch 2: mid-execution kill → retry with extended TTL
# ---------------------------------------------------------------------------

class TestMidExecutionKillRetry:
    """
    Branch 2: execution_attempts > 0 AND orphan return_reason_classification.

    The subagent was killed mid-execution — some work may have been done.
    The dispatcher retries but the prompt instructs the subagent to check
    for partial output and resume if possible.
    """

    def test_mid_execution_kill_returns_spawn_subagent(self):
        """Mid-execution kill (orphan, >0 execution_attempts) → spawn_subagent action."""
        msg = _make_escalate_msg(
            execution_attempts=1,
            return_reason_classification="orphan",
            kill_type="orphan_kill_during_execution",
            heartbeats_before_kill=3,
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "spawn_subagent", (
            "Mid-execution kill (1 execution_attempt, orphan classification) "
            "must retry by spawning a subagent, not surface to Dan"
        )

    def test_mid_execution_kill_includes_uow_id_in_prompt(self):
        """The retry prompt must include the UoW ID."""
        uow_id = "uow-mid-exec-001"
        msg = _make_escalate_msg(
            uow_id=uow_id,
            execution_attempts=1,
            return_reason_classification="orphan",
        )
        result = handle_wos_escalate(msg)
        assert uow_id in result["prompt"]

    def test_mid_execution_kill_exactly_two_attempts_still_retries(self):
        """2 execution_attempts with orphan classification still triggers retry, not surface-to-Dan."""
        msg = _make_escalate_msg(
            execution_attempts=2,
            return_reason_classification="orphan",
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "spawn_subagent", (
            f"With 2 execution_attempts (< {SURFACE_TO_DAN_EXECUTION_THRESHOLD}), "
            "orphan classification must still retry, not surface to Dan"
        )


# ---------------------------------------------------------------------------
# Branch 3: ≥3 execution_attempts → surface to Dan
# ---------------------------------------------------------------------------

class TestGenuineExecutionFailureSurfaceToDan:
    """
    Branch 3: execution_attempts >= SURFACE_TO_DAN_EXECUTION_THRESHOLD.

    The agent ran 3+ times and failed each time. This is a genuine execution
    failure, not an infrastructure kill. Surface to Dan with structured context.
    """

    def test_three_execution_attempts_surfaces_to_dan(self):
        """3 execution_attempts → surface to Dan (send_reply action)."""
        msg = _make_escalate_msg(
            execution_attempts=SURFACE_TO_DAN_EXECUTION_THRESHOLD,
            return_reason_classification="error",
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "send_reply", (
            f"With {SURFACE_TO_DAN_EXECUTION_THRESHOLD} execution_attempts, "
            "the handler must surface to Dan rather than auto-retry"
        )

    def test_four_execution_attempts_surfaces_to_dan(self):
        """4+ execution_attempts also surfaces to Dan."""
        msg = _make_escalate_msg(
            execution_attempts=SURFACE_TO_DAN_EXECUTION_THRESHOLD + 1,
            return_reason_classification="error",
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "send_reply"

    def test_surface_to_dan_includes_uow_id_in_reply_text(self):
        """Dan notification must include the UoW ID so he can act on it."""
        uow_id = "uow-genuine-fail-001"
        msg = _make_escalate_msg(
            uow_id=uow_id,
            execution_attempts=SURFACE_TO_DAN_EXECUTION_THRESHOLD,
        )
        result = handle_wos_escalate(msg)
        assert uow_id in result.get("text", ""), (
            "Dan notification text must include the UoW ID for correlation"
        )

    def test_surface_to_dan_includes_execution_attempts_in_reply_text(self):
        """Dan notification must include the execution_attempts count for context."""
        msg = _make_escalate_msg(
            execution_attempts=SURFACE_TO_DAN_EXECUTION_THRESHOLD,
        )
        result = handle_wos_escalate(msg)
        text = result.get("text", "")
        assert str(SURFACE_TO_DAN_EXECUTION_THRESHOLD) in text or "execution" in text.lower(), (
            "Dan notification must include execution attempt count so he knows the failure history"
        )

    def test_surface_to_dan_reply_includes_chat_id(self):
        """send_reply result must include a chat_id so the dispatcher knows where to send it."""
        msg = _make_escalate_msg(execution_attempts=SURFACE_TO_DAN_EXECUTION_THRESHOLD)
        result = handle_wos_escalate(msg)
        assert "chat_id" in result, (
            "send_reply result must include chat_id — without it the dispatcher "
            "cannot route the notification to Dan"
        )

    def test_three_execution_attempts_orphan_still_surfaces_to_dan(self):
        """3 execution_attempts with orphan classification → surface to Dan.

        execution_attempts is the hard gate. Even if return_reason_classification
        is 'orphan', 3+ confirmed execution attempts means the prescription itself
        may be broken — auto-retry without diagnosis would loop forever.
        """
        msg = _make_escalate_msg(
            execution_attempts=SURFACE_TO_DAN_EXECUTION_THRESHOLD,
            return_reason_classification="orphan",
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "send_reply", (
            f"3+ execution_attempts must surface to Dan regardless of return_reason_classification "
            "— the execution budget is exhausted"
        )


# ---------------------------------------------------------------------------
# Branch 4: human-judgment or philosophical register → surface to Dan
# ---------------------------------------------------------------------------

class TestHumanJudgmentRegisterSurfaceToDan:
    """
    Branch 4: register in ('human-judgment', 'philosophical').

    These UoWs require human judgment — the structured executor was never the
    right tool. Surface immediately to Dan without attempting auto-retry.
    """

    @pytest.mark.parametrize("register", HUMAN_JUDGMENT_REGISTERS)
    def test_human_judgment_register_surfaces_to_dan(self, register):
        """human-judgment and philosophical registers surface immediately to Dan."""
        msg = _make_escalate_msg(
            execution_attempts=0,
            return_reason_classification="orphan",
            register=register,
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "send_reply", (
            f"UoW with register={register!r} must surface to Dan immediately — "
            "the executor was never the right tool for this work"
        )

    @pytest.mark.parametrize("register", HUMAN_JUDGMENT_REGISTERS)
    def test_human_judgment_register_includes_register_in_text(self, register):
        """Dan notification for human-judgment register must mention the register type."""
        uow_id = "uow-human-judgment-001"
        msg = _make_escalate_msg(
            uow_id=uow_id,
            execution_attempts=0,
            register=register,
        )
        result = handle_wos_escalate(msg)
        text = result.get("text", "")
        assert register in text or "judgment" in text.lower() or "philosophical" in text.lower(), (
            f"Dan notification for register={register!r} must explain why it was surfaced"
        )

    def test_human_judgment_with_zero_attempts_still_surfaces(self):
        """Even 0 execution_attempts: human-judgment register bypasses auto-retry."""
        msg = _make_escalate_msg(
            execution_attempts=0,
            return_reason_classification="orphan",
            register="human-judgment",
        )
        result = handle_wos_escalate(msg)
        assert result["action"] == "send_reply", (
            "human-judgment register must surface to Dan even with 0 execution_attempts — "
            "register check takes precedence over the infrastructure-kill auto-retry path"
        )


# ---------------------------------------------------------------------------
# Purity and structure tests
# ---------------------------------------------------------------------------

class TestHandleWosEscalatePurity:
    """handle_wos_escalate must be a pure function."""

    def test_pure_function_same_inputs_same_output_infra_kill(self):
        """Pure function contract: identical inputs always produce identical outputs (infra branch)."""
        msg = _make_escalate_msg(execution_attempts=0, return_reason_classification="orphan")
        r1 = handle_wos_escalate(msg)
        r2 = handle_wos_escalate(msg)
        assert r1 == r2

    def test_pure_function_same_inputs_same_output_surface(self):
        """Pure function contract: identical inputs always produce identical outputs (surface branch)."""
        msg = _make_escalate_msg(execution_attempts=SURFACE_TO_DAN_EXECUTION_THRESHOLD)
        r1 = handle_wos_escalate(msg)
        r2 = handle_wos_escalate(msg)
        assert r1 == r2

    def test_different_uow_ids_produce_different_prompts(self):
        """Each UoW ID must produce a distinct prompt — no cross-contamination."""
        msg1 = _make_escalate_msg(uow_id="uow-001", execution_attempts=0, return_reason_classification="orphan")
        msg2 = _make_escalate_msg(uow_id="uow-002", execution_attempts=0, return_reason_classification="orphan")
        r1 = handle_wos_escalate(msg1)
        r2 = handle_wos_escalate(msg2)
        assert r1["prompt"] != r2["prompt"]

    def test_result_always_contains_message_type(self):
        """All branches must include message_type='wos_escalate' in the result."""
        for execution_attempts in (0, SURFACE_TO_DAN_EXECUTION_THRESHOLD):
            msg = _make_escalate_msg(execution_attempts=execution_attempts)
            result = handle_wos_escalate(msg)
            assert result.get("message_type") == WOS_ESCALATE_MESSAGE_TYPE, (
                f"All branches must echo message_type={WOS_ESCALATE_MESSAGE_TYPE!r} "
                "so callers can confirm which handler fired"
            )


# ---------------------------------------------------------------------------
# route_wos_message integration
# ---------------------------------------------------------------------------

class TestRouteWosMessageEscalate:
    """route_wos_message must dispatch wos_escalate to handle_wos_escalate."""

    def test_route_wos_message_accepts_wos_escalate_type(self):
        """route_wos_message must not raise ValueError for wos_escalate messages."""
        msg = _make_escalate_msg(execution_attempts=0, return_reason_classification="orphan")
        # Should not raise
        result = route_wos_message(msg)
        assert "action" in result

    def test_route_wos_message_infra_kill_returns_spawn_subagent(self):
        """route_wos_message for wos_escalate pure infra kill returns action='spawn_subagent'."""
        msg = _make_escalate_msg(execution_attempts=0, return_reason_classification="orphan")
        result = route_wos_message(msg)
        assert result["action"] == "spawn_subagent"

    def test_route_wos_message_genuine_failure_returns_send_reply(self):
        """route_wos_message for wos_escalate genuine failure returns action='send_reply'."""
        msg = _make_escalate_msg(execution_attempts=SURFACE_TO_DAN_EXECUTION_THRESHOLD)
        result = route_wos_message(msg)
        assert result["action"] == "send_reply"

    def test_route_wos_message_echoes_message_type(self):
        """result['message_type'] must echo 'wos_escalate' so callers can confirm routing."""
        msg = _make_escalate_msg()
        result = route_wos_message(msg)
        assert result["message_type"] == WOS_ESCALATE_MESSAGE_TYPE

    def test_route_wos_message_missing_uow_id_returns_send_reply_alert(self):
        """If uow_id is missing from the message, route_wos_message returns send_reply alert."""
        msg = {
            "type": WOS_ESCALATE_MESSAGE_TYPE,
            "failure_history": {"execution_attempts": 0, "return_reason_classification": "orphan"},
        }
        result = route_wos_message(msg)
        # Should not raise; should return a send_reply alert
        assert result["action"] in ("send_reply", "spawn_subagent"), (
            "Missing uow_id must not propagate as an unhandled exception"
        )
