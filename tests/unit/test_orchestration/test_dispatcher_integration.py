"""
Tests for dispatcher command handlers for /approve, /wos status, and /wos unblock.

These test the pure handler functions in isolation — no Telegram or MCP required.
The handlers receive parsed command arguments and a Registry instance, and return
a formatted string response.
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest

from src.orchestration.dispatcher_handlers import handle_approve, handle_confirm, handle_wos_status, handle_wos_unblock


@pytest.fixture
def registry(tmp_path: Path):
    from src.orchestration.registry import Registry
    return Registry(tmp_path / "registry.db")


@pytest.fixture
def uow_id(registry) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    result = registry.upsert(issue_number=200, title="Test issue for dispatcher", sweep_date=today)
    return result.id


class TestHandleApprove:
    def test_success_message_contains_status_transition(self, registry, uow_id):
        response = handle_approve(uow_id, registry=registry)
        assert "pending" in response.lower()
        assert uow_id in response

    def test_not_found_message(self, registry):
        response = handle_approve("nonexistent-id", registry=registry)
        assert "not found" in response.lower()
        assert "/wos status proposed" in response

    def test_already_pending_message(self, registry, uow_id):
        registry.approve(uow_id)
        response = handle_approve(uow_id, registry=registry)
        # Should mention current status, not raise
        assert "pending" in response.lower()

    def test_expired_message(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=201, title="Expiring issue", sweep_date=today)
        registry.set_status_direct(result.id, "expired")
        response = handle_approve(result.id, registry=registry)
        assert "expired" in response.lower()


class TestHandleConfirmAlias:
    """handle_confirm is an alias for handle_approve — basic smoke tests."""

    def test_confirm_alias_delegates_to_approve(self, registry, uow_id):
        response = handle_confirm(uow_id, registry=registry)
        assert "pending" in response.lower()
        assert uow_id in response


class TestHandleWosStatus:
    def test_returns_active_records(self, registry, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        r1 = registry.upsert(issue_number=210, title="Running issue", sweep_date=today)
        registry.set_status_direct(r1.id, "active")
        response = handle_wos_status("active", registry=registry)
        assert r1.id in response

    def test_returns_empty_message_when_no_records(self, registry):
        response = handle_wos_status("active", registry=registry)
        assert "no" in response.lower() or "empty" in response.lower() or "0" in response

    def test_formats_each_record_with_required_fields(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        r = registry.upsert(issue_number=220, title="Status test issue", sweep_date=today)
        response = handle_wos_status("proposed", registry=registry)
        # Each line should contain: id, summary, source, created date
        assert r.id in response
        assert "Status test issue" in response

    def test_defaults_to_active_and_pending(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        r1 = registry.upsert(issue_number=230, title="Active issue", sweep_date=today)
        registry.set_status_direct(r1.id, "active")
        r2 = registry.upsert(issue_number=231, title="Pending issue", sweep_date=today)
        registry.approve(r2.id)
        # No status arg → returns active + pending
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
