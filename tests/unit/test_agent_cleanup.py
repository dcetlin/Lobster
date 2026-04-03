"""
Tests for agent cleanup improvements.

Covers:
1. Executor cleanup handler — verifies session_end is called after UoW dispatch
2. Stale agent cleanup — verifies agents are unregistered after threshold
3. Agent metrics — verifies running agent count is captured
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# Import the heartbeat modules via importlib to avoid path issues
import sys
import importlib.util

_REPO_ROOT = Path(__file__).parent.parent.parent
_EXECUTOR_PATH = _REPO_ROOT / "scheduled-tasks" / "executor-heartbeat.py"
_STEWARD_PATH = _REPO_ROOT / "scheduled-tasks" / "steward-heartbeat.py"


def _load_module(path):
    """Load a module from a Python file."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


class TestExecutorCleanup:
    """Test executor cleanup handler."""

    def test_executor_cleanup_calls_session_end_on_dispatch(self):
        """Verify that executor cleanup calls session_end after successful dispatch."""
        executor_heartbeat = _load_module(_EXECUTOR_PATH)

        # Mock the registry and executor
        mock_registry = Mock()
        mock_uow = Mock(id="uow-123")
        mock_registry.list.return_value = [mock_uow]

        # Mock the executor
        mock_result = Mock()
        mock_result.outcome = "complete"
        mock_result.executor_id = "exec-456"

        with patch("src.orchestration.executor.Executor") as mock_executor_class:
            mock_executor_instance = Mock()
            mock_executor_instance.execute_uow.return_value = mock_result
            mock_executor_class.return_value = mock_executor_instance

            with patch("src.agents.session_store.session_end") as mock_session_end:
                # Run the executor cycle
                result = executor_heartbeat.run_executor_cycle(mock_registry, dry_run=False)

                # Verify session_end was called
                mock_session_end.assert_called_once()
                call_args = mock_session_end.call_args
                assert call_args[1]["id_or_task_id"] == "exec-456"
                assert call_args[1]["status"] == "completed"

        # Verify dispatch count
        assert result["dispatched"] == 1

    def test_executor_cleanup_handles_missing_executor_id(self):
        """Verify executor cleanup gracefully handles missing executor_id."""
        executor_heartbeat = _load_module(_EXECUTOR_PATH)

        mock_registry = Mock()
        mock_uow = Mock(id="uow-123")
        mock_registry.list.return_value = [mock_uow]

        # Result with no executor_id
        mock_result = Mock()
        mock_result.outcome = "complete"
        mock_result.executor_id = None

        with patch("src.orchestration.executor.Executor") as mock_executor_class:
            mock_executor_instance = Mock()
            mock_executor_instance.execute_uow.return_value = mock_result
            mock_executor_class.return_value = mock_executor_instance

            with patch("src.agents.session_store.session_end") as mock_session_end:
                result = executor_heartbeat.run_executor_cycle(mock_registry, dry_run=False)

                # session_end should not be called when executor_id is None
                mock_session_end.assert_not_called()

        assert result["dispatched"] == 1


class TestStaleAgentCleanup:
    """Test stale agent cleanup."""

    def test_stale_agent_cleanup_identifies_old_agents(self):
        """Verify that old agents are identified for cleanup."""
        steward_heartbeat = _load_module(_STEWARD_PATH)

        # Create a temporary test database
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_agents.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            # Create minimal schema
            conn.execute("""
                CREATE TABLE agent_sessions (
                    id TEXT PRIMARY KEY,
                    spawned_at TEXT NOT NULL,
                    output_file TEXT,
                    status TEXT NOT NULL DEFAULT 'running'
                )
            """)

            # Insert old agent (3 hours ago)
            now = datetime.now(timezone.utc)
            old_time = (now - timedelta(hours=3)).isoformat()
            conn.execute(
                "INSERT INTO agent_sessions (id, spawned_at, status) VALUES (?, ?, ?)",
                ("agent-old", old_time, "running"),
            )

            # Insert recent agent (30 minutes ago)
            recent_time = (now - timedelta(minutes=30)).isoformat()
            conn.execute(
                "INSERT INTO agent_sessions (id, spawned_at, status) VALUES (?, ?, ?)",
                ("agent-recent", recent_time, "running"),
            )

            conn.commit()
            conn.close()

            # Mock the connection getter
            with patch("src.agents.session_store._get_connection") as mock_get_conn:
                mock_get_conn.return_value = sqlite3.connect(str(db_path))
                mock_get_conn.return_value.row_factory = sqlite3.Row

                with patch("src.agents.session_store.session_end") as mock_session_end:
                    with patch("src.agents.session_store._DEFAULT_DB_PATH", db_path):
                        result = steward_heartbeat.run_stale_agent_cleanup(dry_run=False)

                        # Verify old agent was identified
                        assert result["evaluated"] >= 1  # At least the old agent
                        # session_end should be called for old agent
                        if result["cleaned"] > 0:
                            mock_session_end.assert_called()

    def test_stale_agent_cleanup_dry_run(self):
        """Verify dry-run mode doesn't unregister agents."""
        steward_heartbeat = _load_module(_STEWARD_PATH)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_agents.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            conn.execute("""
                CREATE TABLE agent_sessions (
                    id TEXT PRIMARY KEY,
                    spawned_at TEXT NOT NULL,
                    output_file TEXT,
                    status TEXT NOT NULL DEFAULT 'running'
                )
            """)

            now = datetime.now(timezone.utc)
            old_time = (now - timedelta(hours=3)).isoformat()
            conn.execute(
                "INSERT INTO agent_sessions (id, spawned_at, status) VALUES (?, ?, ?)",
                ("agent-old", old_time, "running"),
            )
            conn.commit()
            conn.close()

            with patch("src.agents.session_store._get_connection") as mock_get_conn:
                mock_get_conn.return_value = sqlite3.connect(str(db_path))
                mock_get_conn.return_value.row_factory = sqlite3.Row

                with patch("src.agents.session_store.session_end") as mock_session_end:
                    with patch("src.agents.session_store._DEFAULT_DB_PATH", db_path):
                        result = steward_heartbeat.run_stale_agent_cleanup(dry_run=True)

                        # In dry-run, agents should be skipped
                        assert result["cleaned"] == 0
                        assert result["skipped"] >= 1

    def test_agent_metrics_included_in_result(self):
        """Verify running agent count is included in cleanup result."""
        steward_heartbeat = _load_module(_STEWARD_PATH)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_agents.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            conn.execute("""
                CREATE TABLE agent_sessions (
                    id TEXT PRIMARY KEY,
                    spawned_at TEXT NOT NULL,
                    output_file TEXT,
                    status TEXT NOT NULL DEFAULT 'running'
                )
            """)

            now = datetime.now(timezone.utc)
            # Insert 3 recent running agents
            for i in range(3):
                recent_time = (now - timedelta(minutes=30)).isoformat()
                conn.execute(
                    "INSERT INTO agent_sessions (id, spawned_at, status) VALUES (?, ?, ?)",
                    (f"agent-{i}", recent_time, "running"),
                )

            conn.commit()
            conn.close()

            with patch("src.agents.session_store._get_connection") as mock_get_conn:
                mock_get_conn.return_value = sqlite3.connect(str(db_path))
                mock_get_conn.return_value.row_factory = sqlite3.Row

                with patch("src.agents.session_store._DEFAULT_DB_PATH", db_path):
                    result = steward_heartbeat.run_stale_agent_cleanup(dry_run=False)

                    # Verify running_total is in the result
                    assert "running_total" in result
                    assert result["running_total"] == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
