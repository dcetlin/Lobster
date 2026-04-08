"""
Unit tests for registry_cli.py subprocess interface.

Tests verify the JSON output contract for every command:
  upsert, get, list, approve, check-stale, expire-proposals, gate-readiness

All tests invoke the CLI as a subprocess (matching how scheduled subagents use it)
and parse stdout as JSON.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
import os

REPO_ROOT = Path(__file__).parent.parent.parent.parent
CLI_PATH = REPO_ROOT / "src" / "orchestration" / "registry_cli.py"


def run_cli(db_path: Path, *args, extra_env: dict | None = None) -> dict | list:
    """Run registry_cli.py with the given args and return parsed JSON output."""
    env = os.environ.copy()
    env["REGISTRY_DB_PATH"] = str(db_path)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(CLI_PATH)] + list(args),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"CLI exited with {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


# ---------------------------------------------------------------------------
# upsert command
# ---------------------------------------------------------------------------

class TestUpsertCommand:
    def test_upsert_inserts_new_record(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        result = run_cli(db_path, "upsert", "--issue", "1", "--title", "My Issue", "--sweep-date", today)
        assert result["action"] == "inserted"
        assert "id" in result
        assert result["id"].startswith("uow_")

    def test_upsert_skips_existing_non_terminal(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "5", "--title", "Issue 5", "--sweep-date", yesterday)
        result = run_cli(db_path, "upsert", "--issue", "5", "--title", "Issue 5", "--sweep-date", today)
        assert result["action"] == "skipped"
        assert "reason" in result

    def test_upsert_output_has_required_fields(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        result = run_cli(db_path, "upsert", "--issue", "2", "--title", "Issue 2", "--sweep-date", today)
        assert "id" in result
        assert "action" in result

    def test_upsert_with_issue_body_populates_success_criteria(self, db_path):
        """--issue-body containing ## Acceptance Criteria must produce non-empty success_criteria."""
        today = datetime.now(timezone.utc).date().isoformat()
        issue_body = (
            "## Summary\nFix the thing.\n\n"
            "## Acceptance Criteria\n"
            "- It works\n"
            "- Tests pass\n\n"
            "## Notes\nSee PR for details."
        )
        inserted = run_cli(
            db_path,
            "upsert", "--issue", "3", "--title", "CLI criteria test",
            "--sweep-date", today,
            "--issue-body", issue_body,
        )
        assert inserted["action"] == "inserted"
        uow_id = inserted["id"]

        # Retrieve the record and assert success_criteria is populated
        record = run_cli(db_path, "get", "--id", uow_id)
        assert record["success_criteria"], "success_criteria must be non-empty when --issue-body is provided"
        assert "It works" in record["success_criteria"]


# ---------------------------------------------------------------------------
# get command
# ---------------------------------------------------------------------------

class TestGetCommand:
    def test_get_existing_record(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "10", "--title", "Issue 10", "--sweep-date", today)
        uow_id = inserted["id"]
        got = run_cli(db_path, "get", "--id", uow_id)
        assert got["id"] == uow_id
        assert got["source_issue_number"] == 10

    def test_get_nonexistent_returns_error(self, db_path):
        # Initialize the DB first
        run_cli(db_path, "upsert", "--issue", "11", "--title", "Init DB", "--sweep-date", "2026-01-01")
        # Now check non-existent
        result = run_cli(db_path, "get", "--id", "does-not-exist")
        assert "error" in result


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------

class TestListCommand:
    def test_list_by_status(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "20", "--title", "Issue 20", "--sweep-date", today)
        run_cli(db_path, "upsert", "--issue", "21", "--title", "Issue 21", "--sweep-date", today)
        result = run_cli(db_path, "list", "--status", "proposed")
        assert isinstance(result, list)
        assert len(result) == 2

    def test_list_empty_returns_empty_array(self, db_path):
        # Initialize DB, then list active (none exist)
        run_cli(db_path, "upsert", "--issue", "99", "--title", "Init", "--sweep-date", "2026-01-01")
        result = run_cli(db_path, "list", "--status", "active")
        assert result == []

    def test_list_without_status_returns_all(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "30", "--title", "Issue 30", "--sweep-date", today)
        run_cli(db_path, "upsert", "--issue", "31", "--title", "Issue 31", "--sweep-date", today)
        result = run_cli(db_path, "list")
        assert isinstance(result, list)
        assert len(result) >= 2

    def test_list_records_have_typed_fields(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "32", "--title", "Issue 32", "--sweep-date", today)
        result = run_cli(db_path, "list", "--status", "proposed")
        assert len(result) >= 1
        record = result[0]
        # UoW fields must be present in JSON output
        assert "id" in record
        assert "status" in record
        assert "summary" in record
        assert "source" in record


# ---------------------------------------------------------------------------
# approve command
# ---------------------------------------------------------------------------

class TestApproveCommand:
    def test_approve_proposed_record(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "40", "--title", "Issue 40", "--sweep-date", today)
        uow_id = inserted["id"]
        result = run_cli(db_path, "approve", "--id", uow_id)
        assert result["status"] == "pending"
        assert result["previous_status"] == "proposed"

    def test_approve_idempotent_on_pending(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "41", "--title", "Issue 41", "--sweep-date", today)
        uow_id = inserted["id"]
        run_cli(db_path, "approve", "--id", uow_id)
        result = run_cli(db_path, "approve", "--id", uow_id)
        assert result["status"] == "pending"
        assert result["action"] == "noop"

    def test_approve_not_found_returns_error(self, db_path):
        # Need to init the DB first
        run_cli(db_path, "upsert", "--issue", "42", "--title", "Init", "--sweep-date", "2026-01-01")
        result = run_cli(db_path, "approve", "--id", "nonexistent")
        assert "error" in result


# ---------------------------------------------------------------------------
# check-stale command
# ---------------------------------------------------------------------------

class TestCheckStaleCommand:
    def test_check_stale_returns_array(self, db_path):
        # Use a fake issue checker that says all issues are open (not stale)
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "50", "--title", "Issue 50", "--sweep-date", today)
        # No active records, so result should be empty array
        result = run_cli(db_path, "check-stale")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# expire-proposals command
# ---------------------------------------------------------------------------

class TestExpireProposalsCommand:
    def test_expire_proposals_returns_count(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "60", "--title", "Issue 60", "--sweep-date", today)
        result = run_cli(db_path, "expire-proposals")
        assert "expired_count" in result
        assert "ids" in result
        assert isinstance(result["ids"], list)

    def test_expire_proposals_recent_records_not_expired(self, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "61", "--title", "Issue 61", "--sweep-date", today)
        result = run_cli(db_path, "expire-proposals")
        assert result["expired_count"] == 0


# ---------------------------------------------------------------------------
# gate-readiness command
# ---------------------------------------------------------------------------

class TestGateReadinessCommand:
    def test_gate_readiness_returns_gate_met(self, db_path):
        run_cli(db_path, "upsert", "--issue", "70", "--title", "Issue 70", "--sweep-date", "2026-01-01")
        result = run_cli(db_path, "gate-readiness")
        assert "gate_met" in result
        assert result["gate_met"] is True
        assert result["phase"] == "wos_active"
        assert "days_running" in result
        assert "proposed_to_confirmed_ratio_7d" in result
        assert "reason" in result


# ---------------------------------------------------------------------------
# decide-retry command
# ---------------------------------------------------------------------------

def _force_status(db_path: Path, uow_id: str, status: str) -> None:
    """Directly set a UoW's status in the DB, bypassing Registry transitions."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE uow_registry SET status = ? WHERE id = ?",
        (status, uow_id),
    )
    conn.commit()
    conn.close()


class TestDecideRetryCommand:
    def test_retry_from_blocked_resets_to_ready_for_steward(self, db_path):
        """A UoW in blocked status is retried successfully."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "80", "--title", "Issue 80", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "blocked")

        result = run_cli(db_path, "decide-retry", "--id", uow_id)

        assert result["status"] == "ok"
        assert result["id"] == uow_id
        # Confirm DB status was updated
        record = run_cli(db_path, "get", "--id", uow_id)
        assert record["status"] == "ready-for-steward"
        assert record["steward_cycles"] == 0

    def test_retry_from_ready_for_steward_resets_cycles(self, db_path):
        """
        A UoW stuck in ready-for-steward (false-complete via issue #669) can be
        retried — decide-retry must not silently no-op when status is not blocked.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "81", "--title", "Issue 81", "--sweep-date", today)
        uow_id = inserted["id"]
        # Force into ready-for-steward with non-zero steward_cycles to simulate false-complete
        _force_status(db_path, uow_id, "ready-for-steward")
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET steward_cycles = 3 WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        result = run_cli(db_path, "decide-retry", "--id", uow_id)

        assert result["status"] == "ok"
        assert result["id"] == uow_id
        record = run_cli(db_path, "get", "--id", uow_id)
        assert record["status"] == "ready-for-steward"
        assert record["steward_cycles"] == 0

    def test_retry_from_non_retryable_status_returns_not_retryable(self, db_path):
        """A UoW in active status (not retryable) returns a clear error, not silent no-op."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "82", "--title", "Issue 82", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "active")

        result = run_cli(db_path, "decide-retry", "--id", uow_id)

        assert result["status"] == "not_retryable"
        assert result["id"] == uow_id
        assert "retryable" in result["message"].lower()
        # Status unchanged
        record = run_cli(db_path, "get", "--id", uow_id)
        assert record["status"] == "active"

    def test_retry_from_done_status_returns_not_retryable(self, db_path):
        """A done UoW cannot be retried — done is terminal and intentional."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "83", "--title", "Issue 83", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "done")

        result = run_cli(db_path, "decide-retry", "--id", uow_id)

        assert result["status"] == "not_retryable"

    def test_retry_audit_log_records_actual_from_status(self, db_path):
        """Audit log must record the actual source status, not a hardcoded 'blocked'."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "84", "--title", "Issue 84", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "ready-for-steward")

        run_cli(db_path, "decide-retry", "--id", uow_id)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT from_status FROM audit_log WHERE uow_id = ? AND event = 'decide_retry' ORDER BY ts DESC LIMIT 1",
            (uow_id,),
        ).fetchone()
        conn.close()
        assert row is not None, "audit_log must have a decide_retry entry"
        assert row["from_status"] == "ready-for-steward", (
            "audit log from_status must reflect actual source status, not hardcoded 'blocked'"
        )
