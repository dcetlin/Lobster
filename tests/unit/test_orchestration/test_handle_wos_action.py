"""
Tests for handle_wos_action — the /start deep-link callback handler.

Covers:
- Successful action dispatch: encodes a valid payload, calls handle_wos_action,
  asserts both the return string and the registry state transition.
- Auth guard: denies when TELEGRAM_ADMIN_CHAT_ID is unset (fail-secure default).
- Auth guard: denies wrong chat_id when env var is set.
- Auth guard: permits correct chat_id when env var is set.
- Malformed payload: returns a descriptive error without raising.
- Unknown action: returns a descriptive error without writing to registry.
- Missing UoW: returns a descriptive error without raising.
"""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import MagicMock, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_payload(action: str, uow_id: str) -> str:
    """Encode an action payload identically to tg_deep_link (without the URL prefix)."""
    payload_json = json.dumps({"a": action, "u": uow_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload_json.encode()).rstrip(b"=").decode()


def _make_registry(uow_id: str | None = "uow_20260101_abc123") -> MagicMock:
    """Return a mock Registry with a single UoW (or None if uow_id is None)."""
    registry = MagicMock()
    if uow_id is not None:
        uow = MagicMock()
        uow.id = uow_id
        uow.status = "active"
        registry.get.return_value = uow
    else:
        registry.get.return_value = None
    return registry


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

class TestHandleWosActionAuth:
    UOW_ID = "uow_20260101_abc123"
    CHAT_ID = 12345678

    def test_denies_all_when_env_var_unset(self, monkeypatch):
        """When TELEGRAM_ADMIN_CHAT_ID is unset, all actions are denied (fail-secure)."""
        from src.orchestration.dispatcher_handlers import handle_wos_action

        monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
        payload = _encode_payload("retry", self.UOW_ID)
        registry = _make_registry(self.UOW_ID)

        result = handle_wos_action(payload, chat_id=self.CHAT_ID, registry=registry)

        assert "disabled" in result.lower() or "not set" in result.lower()
        registry.set_status_direct.assert_not_called()

    def test_denies_wrong_chat_id(self, monkeypatch):
        """When TELEGRAM_ADMIN_CHAT_ID is set, wrong chat_id is rejected."""
        from src.orchestration.dispatcher_handlers import handle_wos_action

        monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "99999999")
        payload = _encode_payload("retry", self.UOW_ID)
        registry = _make_registry(self.UOW_ID)

        result = handle_wos_action(payload, chat_id=self.CHAT_ID, registry=registry)

        assert "unauthorized" in result.lower()
        registry.set_status_direct.assert_not_called()

    def test_permits_correct_chat_id(self, monkeypatch):
        """When TELEGRAM_ADMIN_CHAT_ID matches the caller, the action proceeds."""
        from src.orchestration.dispatcher_handlers import handle_wos_action

        monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", str(self.CHAT_ID))
        payload = _encode_payload("retry", self.UOW_ID)
        registry = _make_registry(self.UOW_ID)

        result = handle_wos_action(payload, chat_id=self.CHAT_ID, registry=registry)

        # Should not be an auth error
        assert "unauthorized" not in result.lower()
        assert "disabled" not in result.lower()
        registry.set_status_direct.assert_called_once()


# ---------------------------------------------------------------------------
# Action dispatch — registry state transitions
# ---------------------------------------------------------------------------

class TestHandleWosActionDispatch:
    UOW_ID = "uow_20260101_abc123"
    CHAT_ID = 42000000

    @pytest.fixture(autouse=True)
    def set_admin_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", str(self.CHAT_ID))

    def _call(self, action: str, uow_id: str = UOW_ID) -> tuple[str, MagicMock]:
        from src.orchestration.dispatcher_handlers import handle_wos_action

        payload = _encode_payload(action, uow_id)
        registry = _make_registry(uow_id)
        result = handle_wos_action(payload, chat_id=self.CHAT_ID, registry=registry)
        return result, registry

    def test_retry_transitions_to_ready_for_steward(self):
        """retry action: sets status to ready-for-steward and appends audit log."""
        from src.orchestration.registry import UoWStatus

        result, registry = self._call("retry")

        assert "retry" in result.lower()
        registry.set_status_direct.assert_called_once_with(
            self.UOW_ID, str(UoWStatus.READY_FOR_STEWARD)
        )
        registry.append_audit_log.assert_called_once()

    def test_escalate_transitions_to_needs_human_review(self):
        """escalate action: sets status to needs-human-review and appends audit log."""
        from src.orchestration.registry import UoWStatus

        result, registry = self._call("escalate")

        assert "escalat" in result.lower()
        registry.set_status_direct.assert_called_once_with(
            self.UOW_ID, str(UoWStatus.NEEDS_HUMAN_REVIEW)
        )
        registry.append_audit_log.assert_called_once()

    def test_mark_resolved_transitions_to_done(self):
        """mark_resolved action: sets status to done and appends audit log."""
        from src.orchestration.registry import UoWStatus

        result, registry = self._call("mark_resolved")

        assert "resolved" in result.lower() or "done" in result.lower()
        registry.set_status_direct.assert_called_once_with(
            self.UOW_ID, str(UoWStatus.DONE)
        )
        registry.append_audit_log.assert_called_once()

    def test_close_wont_fix_transitions_to_cancelled(self):
        """close_wont_fix action: sets status to cancelled and appends audit log."""
        from src.orchestration.registry import UoWStatus

        result, registry = self._call("close_wont_fix")

        assert "cancel" in result.lower() or "fix" in result.lower()
        registry.set_status_direct.assert_called_once_with(
            self.UOW_ID, str(UoWStatus.CANCELLED)
        )
        registry.append_audit_log.assert_called_once()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestHandleWosActionErrors:
    CHAT_ID = 42000000

    @pytest.fixture(autouse=True)
    def set_admin_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", str(self.CHAT_ID))

    def test_malformed_payload_returns_error_without_raising(self):
        """A non-base64 payload returns a descriptive error string, not an exception."""
        from src.orchestration.dispatcher_handlers import handle_wos_action

        registry = _make_registry()
        result = handle_wos_action("not-valid-base64!!!", chat_id=self.CHAT_ID, registry=registry)

        assert "could not parse" in result.lower() or "error" in result.lower()
        registry.set_status_direct.assert_not_called()

    def test_unknown_action_returns_error_without_registry_write(self):
        """An unknown action name returns an error; the registry is not written to."""
        from src.orchestration.dispatcher_handlers import handle_wos_action

        payload = _encode_payload("delete_all", "uow_20260101_abc123")
        registry = _make_registry("uow_20260101_abc123")

        result = handle_wos_action(payload, chat_id=self.CHAT_ID, registry=registry)

        assert "unknown" in result.lower()
        registry.set_status_direct.assert_not_called()

    def test_missing_uow_returns_error_without_raising(self):
        """When the UoW does not exist, returns a descriptive error without raising."""
        from src.orchestration.dispatcher_handlers import handle_wos_action

        payload = _encode_payload("retry", "uow_20260101_nonexistent")
        registry = _make_registry(uow_id=None)  # registry.get returns None

        result = handle_wos_action(payload, chat_id=self.CHAT_ID, registry=registry)

        assert "not found" in result.lower()
        registry.set_status_direct.assert_not_called()
