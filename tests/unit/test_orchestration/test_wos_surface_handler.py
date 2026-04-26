"""
Tests for the wos_surface dispatcher handler (T1-A).

Spec (tiered-pr-roadmap-20260426.md §T1-A):
  - New message type "wos_surface" is registered in WOS_MESSAGE_TYPE_DISPATCH.
  - handle_wos_surface(msg) implements a batch decision tree:
    1. execution_enabled=False → surface all UoWs to Dan with pipeline-paused note
    2. All causes are orphan return_reasons → auto-retry all; send Dan a summary
    3. Mixed causes (some orphan, some non-orphan) → auto-retry orphans; surface non-orphans
    4. All causes are non-orphan (human-judgment or genuine failures) → surface all to Dan
  - route_wos_message accepts "wos_surface" and dispatches to handle_wos_surface.
  - handle_wos_surface is a pure function (except for is_execution_enabled, which is
    tested with monkeypatching).

wos_surface message shapes:
  - condition="retry_cap_consolidated" (from _send_consolidated_escalation_notification):
      metadata.uow_ids: list of UoW IDs
      metadata.causes: list of return_reason strings
  - condition="retry_cap" (from _send_escalation_notification fallback):
      metadata.uow_id: single UoW ID (singular)
      metadata.causes: absent or empty
  - condition=<StuckCondition> (from _default_notify_dan):
      metadata.uow_id: single UoW ID
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from src.orchestration.dispatcher_handlers import (
    handle_wos_surface,
    route_wos_message,
    WOS_MESSAGE_TYPE_DISPATCH,
    _ESCALATE_HUMAN_JUDGMENT_REGISTERS,
    _SURFACE_ORPHAN_RETURN_REASONS,
)

# ---------------------------------------------------------------------------
# Named constants from the spec — imported from production module
# ---------------------------------------------------------------------------

WOS_SURFACE_MESSAGE_TYPE = "wos_surface"

# Human-judgment registers that bypass auto-retry
HUMAN_JUDGMENT_REGISTERS = tuple(_ESCALATE_HUMAN_JUDGMENT_REGISTERS)

# Return reasons that are auto-retry eligible (orphan kill events)
ORPHAN_RETURN_REASONS = tuple(_SURFACE_ORPHAN_RETURN_REASONS)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_surface_msg(
    condition: str = "retry_cap_consolidated",
    uow_ids: list[str] | None = None,
    causes: list[str] | None = None,
    uow_id: str | None = None,
) -> dict:
    """Build a minimal wos_surface inbox message."""
    if uow_ids is None:
        uow_ids = ["uow-test-001", "uow-test-002", "uow-test-003"]
    if causes is None:
        causes = ["executor_orphan", "executor_orphan", "executor_orphan"]
    msg: dict = {
        "type": WOS_SURFACE_MESSAGE_TYPE,
        "metadata": {
            "type": WOS_SURFACE_MESSAGE_TYPE,
            "condition": condition,
            "uow_ids": uow_ids,
            "causes": causes,
        },
    }
    if uow_id is not None:
        msg["metadata"]["uow_id"] = uow_id
    return msg


def _make_surface_msg_retry_cap(uow_id: str = "uow-fallback-001") -> dict:
    """Build a wos_surface message from the _send_escalation_notification fallback path.

    This has condition='retry_cap' and a singular uow_id, not uow_ids list.
    """
    return {
        "type": WOS_SURFACE_MESSAGE_TYPE,
        "metadata": {
            "type": WOS_SURFACE_MESSAGE_TYPE,
            "condition": "retry_cap",
            "uow_id": uow_id,
        },
    }


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestWosSurfaceRegistered:
    """wos_surface must be registered in the dispatch table."""

    def test_wos_surface_in_dispatch_table(self):
        """wos_surface must appear in WOS_MESSAGE_TYPE_DISPATCH.

        Absence means the dispatcher cannot route wos_surface messages — kill waves
        produce dangling unprocessed messages and require manual /decide for each UoW.
        """
        assert WOS_SURFACE_MESSAGE_TYPE in WOS_MESSAGE_TYPE_DISPATCH, (
            f"{WOS_SURFACE_MESSAGE_TYPE!r} must be registered in WOS_MESSAGE_TYPE_DISPATCH "
            "so the dispatcher routes it structurally rather than via prose that is lost on compaction"
        )


# ---------------------------------------------------------------------------
# Branch: execution_enabled=False → surface all UoWs with pipeline-paused note
# ---------------------------------------------------------------------------


class TestPipelinePausedSurfacesAll:
    """When execution_enabled=False, all UoWs surface to Dan with a paused note."""

    def test_pipeline_paused_returns_send_reply(self):
        """Pipeline paused → action='send_reply' (surface to Dan)."""
        msg = _make_surface_msg(causes=["executor_orphan", "executor_orphan", "executor_orphan"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=False
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "send_reply", (
            "When execution_enabled=False, all UoWs must surface to Dan — "
            "auto-retry would queue work into a stopped pipeline"
        )

    def test_pipeline_paused_text_mentions_paused(self):
        """Dan notification must explain the pipeline is paused."""
        msg = _make_surface_msg()
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=False
        ):
            result = handle_wos_surface(msg)
        text = result.get("text", "").lower()
        assert "pause" in text or "disabled" in text or "stopped" in text, (
            "Dan notification must tell him the pipeline is paused so he knows "
            "why auto-retry was not attempted"
        )

    def test_pipeline_paused_text_includes_uow_ids(self):
        """Dan notification must list affected UoW IDs."""
        uow_ids = ["uow-paused-001", "uow-paused-002"]
        msg = _make_surface_msg(uow_ids=uow_ids, causes=["executor_orphan", "executor_orphan"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=False
        ):
            result = handle_wos_surface(msg)
        text = result.get("text", "")
        for uow_id in uow_ids:
            assert uow_id in text, (
                f"UoW ID {uow_id!r} must appear in the Dan notification so he can act on each"
            )

    def test_pipeline_paused_overrides_all_orphan_auto_retry(self):
        """Pipeline paused check takes priority — even all-orphan causes must surface."""
        msg = _make_surface_msg(
            causes=["executor_orphan", "executing_orphan", "executor_orphan"]
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=False
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "send_reply", (
            "Pipeline pause overrides auto-retry eligibility — "
            "must not spawn a subagent into a stopped pipeline"
        )


# ---------------------------------------------------------------------------
# Branch: all causes are orphan return_reasons → auto-retry all
# ---------------------------------------------------------------------------


class TestAllOrphansAutoRetryAll:
    """When all causes are orphan return_reasons and pipeline is running, auto-retry all."""

    def test_all_orphans_returns_spawn_subagent(self):
        """All orphan causes → action='spawn_subagent' (auto-retry)."""
        msg = _make_surface_msg(
            causes=["executor_orphan", "executing_orphan", "executor_orphan"],
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "spawn_subagent", (
            "Kill wave with all-orphan causes is a single infrastructure event — "
            "auto-retrying all is safe and correct"
        )

    def test_all_orphans_batch_includes_all_uow_ids_in_prompt(self):
        """The batch retry prompt must reference all affected UoW IDs."""
        uow_ids = ["uow-batch-001", "uow-batch-002", "uow-batch-003"]
        msg = _make_surface_msg(
            uow_ids=uow_ids,
            causes=["executor_orphan", "executing_orphan", "diagnosing_orphan"],
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        prompt = result.get("prompt", "")
        for uow_id in uow_ids:
            assert uow_id in prompt, (
                f"Batch retry prompt must include UoW ID {uow_id!r} "
                "so the steward heartbeat processes all affected UoWs"
            )

    def test_all_orphans_prompt_contains_chat_id_zero(self):
        """Batch retry subagent must use chat_id: 0 to silence the result relay."""
        msg = _make_surface_msg(causes=["executor_orphan", "executor_orphan", "executor_orphan"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert "chat_id: 0" in result.get("prompt", ""), (
            "Steward subagent prompt must include 'chat_id: 0' so write_result "
            "silently drops the result rather than forwarding to a user chat"
        )

    def test_all_orphans_includes_task_id(self):
        """spawn_subagent result must include a non-empty task_id."""
        msg = _make_surface_msg(causes=["executor_orphan", "executor_orphan", "executor_orphan"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert isinstance(result.get("task_id"), str)
        assert len(result["task_id"]) > 0

    def test_all_orphans_single_uow_still_auto_retries(self):
        """Even a single-UoW wos_surface with orphan cause auto-retries."""
        msg = _make_surface_msg(
            uow_ids=["uow-single-001"],
            causes=["executor_orphan"],
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "spawn_subagent"

    @pytest.mark.parametrize("reason", ORPHAN_RETURN_REASONS)
    def test_each_orphan_reason_is_auto_retry_eligible(self, reason):
        """Each individual orphan return_reason triggers auto-retry for a single-UoW batch."""
        msg = _make_surface_msg(uow_ids=["uow-orphan-001"], causes=[reason])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "spawn_subagent", (
            f"return_reason={reason!r} is an orphan reason — must auto-retry"
        )


# ---------------------------------------------------------------------------
# Branch: mixed causes → auto-retry orphans, surface non-orphans
# ---------------------------------------------------------------------------


class TestMixedCausesPartialRetry:
    """Mixed causes: orphan UoWs auto-retry; non-orphan UoWs surface to Dan."""

    def test_mixed_causes_returns_send_reply_for_non_orphans(self):
        """Mixed causes: non-orphan UoWs must be surfaced to Dan."""
        msg = _make_surface_msg(
            uow_ids=["uow-mix-001", "uow-mix-002"],
            causes=["executor_orphan", "execution_failed"],
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "send_reply", (
            "Mixed causes must surface the non-orphan UoWs to Dan — "
            "auto-retry alone is not sufficient"
        )

    def test_mixed_causes_text_includes_non_orphan_uow_id(self):
        """Dan notification must include the non-orphan UoW ID that needs review."""
        msg = _make_surface_msg(
            uow_ids=["uow-needs-review-001", "uow-auto-retry-001"],
            causes=["execution_failed", "executor_orphan"],
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        text = result.get("text", "")
        assert "uow-needs-review-001" in text, (
            "Dan notification must include the non-orphan UoW ID "
            "so he knows which UoW requires his decision"
        )

    def test_mixed_causes_text_mentions_auto_retried_orphans(self):
        """Dan notification must acknowledge that orphan UoWs were auto-retried."""
        msg = _make_surface_msg(
            uow_ids=["uow-review-001", "uow-retried-001"],
            causes=["execution_failed", "executor_orphan"],
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        text = result.get("text", "").lower()
        assert "retry" in text or "auto" in text or "uow-retried-001" in text.lower(), (
            "Dan notification must mention that orphan UoWs were auto-retried "
            "so he understands the full picture of the kill wave"
        )

    def test_all_non_orphan_causes_surfaces_all(self):
        """All non-orphan causes: all UoWs surface to Dan — no auto-retry at all."""
        msg = _make_surface_msg(
            uow_ids=["uow-fail-001", "uow-fail-002"],
            causes=["execution_failed", "execution_failed"],
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "send_reply", (
            "All non-orphan causes must surface to Dan — auto-retry is not safe "
            "when the prescription itself may be broken"
        )

    def test_all_non_orphan_text_includes_all_uow_ids(self):
        """Dan notification for all-non-orphan must include all UoW IDs."""
        uow_ids = ["uow-fail-001", "uow-fail-002", "uow-fail-003"]
        msg = _make_surface_msg(
            uow_ids=uow_ids,
            causes=["execution_failed", "crashed_no_output", "execution_failed"],
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        text = result.get("text", "")
        for uow_id in uow_ids:
            assert uow_id in text, (
                f"Dan notification must include UoW ID {uow_id!r} "
                "so he can act on each one"
            )


# ---------------------------------------------------------------------------
# Fallback path: condition="retry_cap" with singular uow_id
# ---------------------------------------------------------------------------


class TestRetryCapFallbackPath:
    """condition='retry_cap' messages come from the _send_escalation_notification fallback.

    They carry a singular uow_id (not uow_ids list) and no causes list.
    The handler must not raise on these messages.
    """

    def test_retry_cap_with_singular_uow_id_does_not_raise(self):
        """condition='retry_cap' with singular uow_id must be handled without raising."""
        msg = _make_surface_msg_retry_cap(uow_id="uow-fallback-001")
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert "action" in result, (
            "retry_cap messages must produce a valid action, not raise an exception"
        )

    def test_retry_cap_fallback_surfaces_to_dan(self):
        """condition='retry_cap' has no causes list — surface to Dan as fallback."""
        msg = _make_surface_msg_retry_cap(uow_id="uow-fallback-002")
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "send_reply", (
            "Fallback retry_cap messages have no cause classification — "
            "surface to Dan rather than auto-retrying blindly"
        )

    def test_retry_cap_fallback_text_includes_uow_id(self):
        """Dan notification must include the UoW ID from the fallback path."""
        uow_id = "uow-fallback-003"
        msg = _make_surface_msg_retry_cap(uow_id=uow_id)
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert uow_id in result.get("text", ""), (
            f"Dan notification must include {uow_id!r} from the fallback path"
        )


# ---------------------------------------------------------------------------
# Purity and structure tests
# ---------------------------------------------------------------------------


class TestHandleWosSurfacePurity:
    """handle_wos_surface must return consistent structured results."""

    def test_pure_function_same_inputs_same_output(self):
        """Identical inputs (same execution_enabled state) produce identical outputs."""
        msg = _make_surface_msg(causes=["executor_orphan", "executor_orphan", "executor_orphan"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            r1 = handle_wos_surface(msg)
            r2 = handle_wos_surface(msg)
        assert r1 == r2

    def test_result_always_contains_action(self):
        """All branches must include an 'action' key in the result."""
        for pipeline_enabled in (True, False):
            msg = _make_surface_msg()
            with patch(
                "src.orchestration.dispatcher_handlers.is_execution_enabled",
                return_value=pipeline_enabled,
            ):
                result = handle_wos_surface(msg)
            assert "action" in result, (
                f"handle_wos_surface must always return a dict with 'action' key "
                f"(pipeline_enabled={pipeline_enabled})"
            )

    def test_send_reply_result_includes_chat_id(self):
        """send_reply branches must include chat_id so the dispatcher knows where to send."""
        msg = _make_surface_msg(
            causes=["execution_failed", "execution_failed", "execution_failed"]
        )
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert result["action"] == "send_reply"
        assert "chat_id" in result, (
            "send_reply result must include chat_id — without it the dispatcher "
            "cannot route the notification to Dan"
        )

    def test_result_contains_message_type(self):
        """All branches must include message_type='wos_surface' in the result."""
        for pipeline_enabled in (True, False):
            msg = _make_surface_msg()
            with patch(
                "src.orchestration.dispatcher_handlers.is_execution_enabled",
                return_value=pipeline_enabled,
            ):
                result = handle_wos_surface(msg)
            assert result.get("message_type") == WOS_SURFACE_MESSAGE_TYPE, (
                f"All branches must echo message_type={WOS_SURFACE_MESSAGE_TYPE!r} "
                "so callers can confirm which handler fired"
            )

    def test_empty_uow_ids_does_not_raise(self):
        """handle_wos_surface with empty uow_ids must not raise."""
        msg = _make_surface_msg(uow_ids=[], causes=[])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = handle_wos_surface(msg)
        assert "action" in result


# ---------------------------------------------------------------------------
# route_wos_message integration
# ---------------------------------------------------------------------------


class TestRouteWosMessageSurface:
    """route_wos_message must dispatch wos_surface to handle_wos_surface."""

    def test_route_wos_message_accepts_wos_surface_type(self):
        """route_wos_message must not raise ValueError for wos_surface messages."""
        msg = _make_surface_msg(causes=["executor_orphan", "executor_orphan", "executor_orphan"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = route_wos_message(msg)
        assert "action" in result

    def test_route_wos_message_all_orphans_returns_spawn_subagent(self):
        """route_wos_message for wos_surface all-orphan kill wave returns spawn_subagent."""
        msg = _make_surface_msg(causes=["executor_orphan", "executor_orphan", "executor_orphan"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = route_wos_message(msg)
        assert result["action"] == "spawn_subagent"

    def test_route_wos_message_genuine_failure_returns_send_reply(self):
        """route_wos_message for wos_surface genuine failure returns send_reply."""
        msg = _make_surface_msg(causes=["execution_failed", "execution_failed", "execution_failed"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = route_wos_message(msg)
        assert result["action"] == "send_reply"

    def test_route_wos_message_echoes_message_type(self):
        """result['message_type'] must echo 'wos_surface' so callers can confirm routing."""
        msg = _make_surface_msg()
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=True
        ):
            result = route_wos_message(msg)
        assert result["message_type"] == WOS_SURFACE_MESSAGE_TYPE

    def test_route_wos_message_pipeline_paused_returns_send_reply(self):
        """route_wos_message for wos_surface with pipeline paused returns send_reply."""
        msg = _make_surface_msg(causes=["executor_orphan", "executor_orphan", "executor_orphan"])
        with patch(
            "src.orchestration.dispatcher_handlers.is_execution_enabled", return_value=False
        ):
            result = route_wos_message(msg)
        assert result["action"] == "send_reply"
