"""
Tests for dispatcher command handlers for /confirm and /wos status.

These test the pure handler functions in isolation — no Telegram or MCP required.
The handlers receive parsed command arguments and a Registry instance, and return
a formatted string response.
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest

from src.orchestration.dispatcher_handlers import handle_confirm, handle_wos_status


@pytest.fixture
def registry(tmp_path: Path):
    from src.orchestration.registry import Registry
    return Registry(tmp_path / "registry.db")


@pytest.fixture
def uow_id(registry) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    result = registry.upsert(issue_number=200, title="Test issue for dispatcher", sweep_date=today)
    return result["id"]


class TestHandleConfirm:
    def test_success_message_contains_status_transition(self, registry, uow_id):
        response = handle_confirm(uow_id, registry=registry)
        assert "pending" in response.lower()
        assert uow_id in response

    def test_not_found_message(self, registry):
        response = handle_confirm("nonexistent-id", registry=registry)
        assert "not found" in response.lower()
        assert "/wos status proposed" in response

    def test_already_pending_message(self, registry, uow_id):
        registry.confirm(uow_id)
        response = handle_confirm(uow_id, registry=registry)
        # Should mention current status, not raise
        assert "pending" in response.lower()

    def test_expired_message(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=201, title="Expiring issue", sweep_date=today)
        registry.set_status_direct(result["id"], "expired")
        response = handle_confirm(result["id"], registry=registry)
        assert "expired" in response.lower()


class TestHandleWosStatus:
    def test_returns_active_records(self, registry, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        r1 = registry.upsert(issue_number=210, title="Running issue", sweep_date=today)
        registry.set_status_direct(r1["id"], "active")
        response = handle_wos_status("active", registry=registry)
        assert r1["id"] in response

    def test_returns_empty_message_when_no_records(self, registry):
        response = handle_wos_status("active", registry=registry)
        assert "no" in response.lower() or "empty" in response.lower() or "0" in response

    def test_formats_each_record_with_required_fields(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        r = registry.upsert(issue_number=220, title="Status test issue", sweep_date=today)
        response = handle_wos_status("proposed", registry=registry)
        # Each line should contain: id, summary, source, created date
        assert r["id"] in response
        assert "Status test issue" in response

    def test_defaults_to_active_and_pending(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        r1 = registry.upsert(issue_number=230, title="Active issue", sweep_date=today)
        registry.set_status_direct(r1["id"], "active")
        r2 = registry.upsert(issue_number=231, title="Pending issue", sweep_date=today)
        registry.confirm(r2["id"])
        # No status arg → returns active + pending
        response = handle_wos_status(None, registry=registry)
        assert r1["id"] in response
        assert r2["id"] in response
