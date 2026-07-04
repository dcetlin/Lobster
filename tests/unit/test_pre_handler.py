"""
Unit tests for src.bot.pre_handler — Tier 1 deterministic slash command handlers.

WOS-UoW: uow_20260515_75d522
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# handle_wos_quick
# ---------------------------------------------------------------------------

def test_handle_wos_quick_execution_disabled(tmp_path):
    """When execution_enabled=False, output should contain ✗."""
    fake_config = {"execution_enabled": False, "max_parallel": 2}

    # Minimal in-memory registry DB with uow table
    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE uow (status TEXT)")
    conn.execute("INSERT INTO uow VALUES ('active')")
    conn.execute("INSERT INTO uow VALUES ('pending')")
    conn.commit()
    conn.close()

    with (
        patch("src.orchestration.dispatcher_handlers.read_wos_config", return_value=fake_config),
        patch("src.orchestration.paths.REGISTRY_DB", db_path),
    ):
        from src.bot.pre_handler import handle_wos_quick
        result = handle_wos_quick(12345)

    assert "✗" in result
    assert "execution" in result.lower()


def test_handle_wos_quick_execution_enabled(tmp_path):
    """When execution_enabled=True, output should contain ✓."""
    fake_config = {"execution_enabled": True, "max_parallel": 2}

    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE uow (status TEXT)")
    conn.execute("INSERT INTO uow VALUES ('executing')")
    conn.execute("INSERT INTO uow VALUES ('pending')")
    conn.commit()
    conn.close()

    with (
        patch("src.orchestration.dispatcher_handlers.read_wos_config", return_value=fake_config),
        patch("src.orchestration.paths.REGISTRY_DB", db_path),
    ):
        from src.bot.pre_handler import handle_wos_quick
        result = handle_wos_quick(12345)

    assert "✓" in result
    assert "Dashboard:" in result


def test_handle_wos_quick_db_failure_graceful(tmp_path):
    """DB failure should not raise — status summary is still returned."""
    fake_config = {"execution_enabled": True}
    nonexistent = tmp_path / "doesnotexist.db"

    with (
        patch("src.orchestration.dispatcher_handlers.read_wos_config", return_value=fake_config),
        patch("src.orchestration.paths.REGISTRY_DB", nonexistent),
    ):
        from src.bot.pre_handler import handle_wos_quick
        result = handle_wos_quick(12345)

    assert "WOS:" in result


# ---------------------------------------------------------------------------
# handle_todos_quick
# ---------------------------------------------------------------------------

def test_handle_todos_quick_empty(tmp_path):
    """When no open todos, output should say 'No open todos.'"""
    db_path = tmp_path / "los.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE action_items (
            id INTEGER PRIMARY KEY,
            text TEXT,
            status TEXT,
            priority INTEGER DEFAULT 50,
            mention_count INTEGER DEFAULT 0,
            extracted_at TEXT,
            snoozed_until TEXT,
            dismissed_at TEXT
        )"""
    )
    conn.commit()
    conn.close()

    with patch("src.los.db.DEFAULT_DB_PATH", db_path):
        from src.bot.pre_handler import handle_todos_quick
        result = handle_todos_quick(12345)

    assert result == "No open todos."


def test_handle_todos_quick_with_items(tmp_path):
    """When todos exist, they should appear as a numbered list."""
    db_path = tmp_path / "los.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE action_items (
            id INTEGER PRIMARY KEY,
            text TEXT,
            status TEXT,
            priority INTEGER DEFAULT 50,
            mention_count INTEGER DEFAULT 0,
            extracted_at TEXT DEFAULT '2026-01-01',
            snoozed_until TEXT,
            dismissed_at TEXT,
            source TEXT,
            source_ref TEXT,
            created_at TEXT DEFAULT '2026-01-01'
        )"""
    )
    conn.execute("INSERT INTO action_items (id, text, status) VALUES (1, 'Buy groceries', 'open')")
    conn.execute("INSERT INTO action_items (id, text, status) VALUES (2, 'Fix the bug', 'open')")
    conn.commit()
    conn.close()

    with patch("src.los.db.DEFAULT_DB_PATH", db_path):
        from src.bot.pre_handler import handle_todos_quick
        result = handle_todos_quick(12345)

    assert "1." in result
    assert "Buy groceries" in result
    assert "2." in result
    assert "Fix the bug" in result


# ---------------------------------------------------------------------------
# handle_quota_quick
# ---------------------------------------------------------------------------

def test_handle_quota_quick_file_absent(tmp_path):
    """When state.json is absent, return the unavailable message."""
    nonexistent = tmp_path / "nonexistent.json"

    import src.bot.pre_handler as ph
    orig_path_home = Path.home

    with patch("src.bot.pre_handler.Path") as mock_path_cls:
        mock_path_cls.home.return_value = Path(tmp_path)
        # Make chained / operator return a path to nonexistent file
        mock_path_cls.return_value = nonexistent
        # Use real Path for home resolution
        mock_path_cls.home.return_value.__truediv__ = lambda self, p: tmp_path / p

        from src.bot.pre_handler import handle_quota_quick
        # Simpler: just test that a missing file returns the right message
        result = handle_quota_quick.__wrapped__(12345) if hasattr(handle_quota_quick, "__wrapped__") else None

    # Direct test: create a real temp state.json path and see what happens
    # when the path doesn't exist
    import src.bot.pre_handler as ph_module
    with patch.object(Path, "exists", return_value=False):
        from src.bot.pre_handler import handle_quota_quick as hq
        result = hq(12345)

    assert "unavailable" in result.lower()


def test_handle_quota_quick_with_data(tmp_path):
    """When state.json has quota fields, they should appear in the output."""
    state = {
        "five_hour_usage_pct": 45.0,
        "five_hour_tokens_used": 12345,
        "weekly_usage_pct": 30.0,
        "weekly_tokens_used": 50000,
        "weekly_token_limit": 200000,
        "weekly_reset_at": "2026-05-20T00:00:00Z",
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    with patch("src.bot.pre_handler.Path") as mock_cls:
        # Path.home() / ".claude" / "cc-budget" / "state.json"
        mock_path = MagicMock()
        mock_cls.home.return_value = mock_path
        mock_path.__truediv__.return_value = mock_path
        mock_path.exists.return_value = True
        mock_path.open.return_value = state_file.open()

        from src.bot.pre_handler import handle_quota_quick
        result = handle_quota_quick(12345)

    assert "5h:" in result or "Quota" in result


# ---------------------------------------------------------------------------
# handle_jobs_quick
# ---------------------------------------------------------------------------

def test_handle_jobs_quick_no_file(tmp_path):
    """When jobs.json is absent, return the not-configured message."""
    with patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(tmp_path)}):
        (tmp_path / "scheduled-jobs").mkdir()
        from src.bot.pre_handler import handle_jobs_quick
        result = handle_jobs_quick(12345)

    assert "not configured" in result.lower() or "No jobs" in result


def test_handle_jobs_quick_with_jobs(tmp_path):
    """When jobs.json has jobs, list them."""
    jobs_dir = tmp_path / "scheduled-jobs"
    jobs_dir.mkdir()
    jobs_data = {
        "jobs": {
            "cc-usage-poller": {"enabled": True, "last_run": "2026-05-19T10:00:00Z"},
            "wos-health-check": {"enabled": False},
        }
    }
    (jobs_dir / "jobs.json").write_text(json.dumps(jobs_data))

    with patch.dict("os.environ", {"LOBSTER_WORKSPACE": str(tmp_path)}):
        from src.bot.pre_handler import handle_jobs_quick
        result = handle_jobs_quick(12345)

    assert "cc-usage-poller" in result
    assert "wos-health-check" in result
    assert "✓" in result
    assert "✗" in result


# ---------------------------------------------------------------------------
# handle_status_quick
# ---------------------------------------------------------------------------

def test_handle_status_quick_no_sessions():
    """When no active sessions, return 'No active sessions found.'"""
    with patch("src.agents.session_store.get_active_sessions", return_value=[]) as mock_gas:
        # Since pre_handler imports get_active_sessions inside the function,
        # patch at the source module
        with patch("src.bot.pre_handler.get_active_sessions", return_value=[]):
            from src.bot.pre_handler import handle_status_quick
            result = handle_status_quick(12345)

    assert result == "No active sessions found."


def test_handle_status_quick_with_sessions():
    """When sessions exist, return a summary with counts."""
    fake_sessions = [
        {
            "chat_id": "12345",
            "agent_type": "functional-engineer",
            "description": "Fix the bug",
            "elapsed_seconds": 120,
        },
        {
            "chat_id": "0",
            "agent_type": "dispatcher",
            "description": "startup-catchup",
            "elapsed_seconds": 60,
        },
    ]
    with patch("src.agents.session_store.get_active_sessions", return_value=fake_sessions):
        with patch("src.bot.pre_handler.get_active_sessions", return_value=fake_sessions):
            from src.bot.pre_handler import handle_status_quick
            result = handle_status_quick(12345)

    assert "1 user" in result
    assert "1 system" in result


# ---------------------------------------------------------------------------
# callbacks.py — confirm-restart and cancel-restart
# ---------------------------------------------------------------------------

def test_restart_callback_mpc_blocked():
    """confirm-restart-mcp should be handled=True with warning text."""
    from src.los.callbacks import route_los_callback

    msg = {"callback_data": "confirm-restart-mcp", "chat_id": 12345}
    result = route_los_callback(msg)

    assert result["handled"] is True
    assert "shell" in result["text"].lower() or "external" in result["text"].lower()
    assert "mcp" in result["text"].lower()


def test_restart_callback_all_blocked():
    """confirm-restart-all should be handled=True with warning text."""
    from src.los.callbacks import route_los_callback

    msg = {"callback_data": "confirm-restart-all", "chat_id": 12345}
    result = route_los_callback(msg)

    assert result["handled"] is True
    assert "shell" in result["text"].lower() or "external" in result["text"].lower()


def test_restart_callback_dispatcher_passes_through():
    """confirm-restart-dispatcher should pass through to inbox (handled=False)."""
    from src.los.callbacks import route_los_callback

    msg = {"callback_data": "confirm-restart-dispatcher", "chat_id": 12345}
    result = route_los_callback(msg)

    assert result["handled"] is False


def test_cancel_restart():
    """cancel-restart should be handled=True with cancellation text."""
    from src.los.callbacks import route_los_callback

    msg = {"callback_data": "cancel-restart", "chat_id": 12345}
    result = route_los_callback(msg)

    assert result["handled"] is True
    assert "cancel" in result["text"].lower()


# ---------------------------------------------------------------------------
# route_tier1_command
# ---------------------------------------------------------------------------

def test_route_tier1_command_unknown_returns_none():
    """Unknown commands should return None (fall back to dispatcher)."""
    from src.bot.pre_handler import route_tier1_command
    assert route_tier1_command("unknown_command", 12345) is None


def test_route_tier1_command_known_dispatches():
    """Known commands should dispatch to the correct handler."""
    with patch("src.bot.pre_handler.handle_wos_quick", return_value="WOS: OK") as mock_h:
        from src.bot.pre_handler import route_tier1_command
        result = route_tier1_command("wos", 12345)

    assert result == "WOS: OK"
    mock_h.assert_called_once_with(12345)
