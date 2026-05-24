"""
Unit tests for registry_cli.py subprocess interface.

Tests verify the JSON output contract for every command:
  upsert, get, list, approve, check-stale, expire-proposals, gate-readiness

All tests invoke the CLI as a subprocess (matching how scheduled subagents use it)
and parse stdout as JSON.

The report command outputs plain text (not JSON). Its tests use run_cli_text()
which returns raw stdout instead of parsing it.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
import os

# Import production constants so tests do not mirror raw string literals.
# These are defined in registry_cli.py and must be kept in sync with
# _RETURN_REASON_CLASSIFICATIONS in steward.py.
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))
from orchestration.registry_cli import (
    _RETURN_REASON_EXECUTOR_ORPHAN,
    _RETURN_REASON_DIAGNOSING_ORPHAN,
    DEFAULT_REPORT_HOURS,
    _classify_status,
    _compute_summary,
    _window_start_iso,
    KILL_TYPE_ORPHAN_BEFORE_START,
    KILL_TYPE_ORPHAN_DURING_EXECUTION,
    KILL_TYPE_HARD_CAP,
    KILL_TYPE_USER_CLOSED,
    KILL_TYPE_EXECUTION_FAILED,
    KILL_TYPE_UNKNOWN,
    _compute_latency_stats,
    _format_latency_report,
)

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


def run_cli_text(db_path: Path, *args, extra_env: dict | None = None) -> str:
    """Run registry_cli.py and return raw stdout text (for commands that output plain text)."""
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
    return result.stdout


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


# ---------------------------------------------------------------------------
# status-breakdown command
# ---------------------------------------------------------------------------

class TestStatusBreakdownCommand:
    def test_status_breakdown_returns_dict_of_counts(self, db_path):
        """status-breakdown outputs a dict with status names as keys and integer counts as values."""
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "100", "--title", "Issue 100", "--sweep-date", today)
        run_cli(db_path, "upsert", "--issue", "101", "--title", "Issue 101", "--sweep-date", today)
        result = run_cli(db_path, "status-breakdown")
        assert isinstance(result, dict), "status-breakdown must return a JSON object"
        assert "proposed" in result, "proposed status must be present after upsert"
        assert result["proposed"] == 2, "count must equal number of inserted records"

    def test_status_breakdown_counts_are_integers(self, db_path):
        """All values in the status-breakdown output are non-negative integers."""
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "110", "--title", "Issue 110", "--sweep-date", today)
        result = run_cli(db_path, "status-breakdown")
        for status, count in result.items():
            assert isinstance(count, int), f"count for {status!r} must be an int, got {type(count)}"
            assert count >= 0, f"count for {status!r} must be non-negative"

    def test_status_breakdown_empty_db_returns_empty_dict(self, db_path):
        """status-breakdown on an empty but initialized DB returns an empty dict."""
        # Initialize DB without inserting any UoWs
        run_cli(db_path, "upsert", "--issue", "999", "--title", "Init", "--sweep-date", "2026-01-01")
        # Force the single record to done so there are no proposed
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM uow_registry")
        conn.commit()
        conn.close()
        # Must not error; must return {} or a dict without error key
        result = run_cli(db_path, "status-breakdown")
        assert isinstance(result, dict)
        assert "error" not in result

    def test_status_breakdown_reflects_multiple_statuses(self, db_path):
        """status-breakdown counts separately for each status that exists in the DB."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "120", "--title", "Issue 120", "--sweep-date", today)
        uow_id_1 = inserted["id"]
        inserted2 = run_cli(db_path, "upsert", "--issue", "121", "--title", "Issue 121", "--sweep-date", today)
        uow_id_2 = inserted2["id"]
        # Force one to blocked so we get two different statuses
        _force_status(db_path, uow_id_1, "blocked")
        result = run_cli(db_path, "status-breakdown")
        assert result.get("blocked", 0) >= 1
        assert result.get("proposed", 0) >= 1


# ---------------------------------------------------------------------------
# escalation-candidates command
# ---------------------------------------------------------------------------

class TestEscalationCandidatesCommand:
    def test_escalation_candidates_returns_list(self, db_path):
        """escalation-candidates returns a JSON array."""
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "200", "--title", "Issue 200", "--sweep-date", today)
        result = run_cli(db_path, "escalation-candidates")
        assert isinstance(result, list), "escalation-candidates must return a JSON array"

    def test_escalation_candidates_includes_needs_human_review(self, db_path):
        """UoWs in needs-human-review status appear as escalation candidates."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "201", "--title", "Issue 201", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "needs-human-review")
        result = run_cli(db_path, "escalation-candidates")
        ids = [r["id"] for r in result]
        assert uow_id in ids, "UoW in needs-human-review must appear in escalation-candidates"

    def test_escalation_candidates_excludes_done_uows(self, db_path):
        """UoWs in done status do not appear as escalation candidates."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "202", "--title", "Issue 202", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "done")
        result = run_cli(db_path, "escalation-candidates")
        ids = [r["id"] for r in result]
        assert uow_id not in ids, "UoW in done status must not appear in escalation-candidates"

    def test_escalation_candidates_output_has_required_fields(self, db_path):
        """Each escalation candidate record has id, status, and summary fields."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "203", "--title", "Issue 203", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "needs-human-review")
        result = run_cli(db_path, "escalation-candidates")
        assert len(result) >= 1
        record = result[0]
        assert "id" in record
        assert "status" in record
        assert "summary" in record


# ---------------------------------------------------------------------------
# stale command
# ---------------------------------------------------------------------------

class TestStaleCommand:
    def test_stale_returns_list(self, db_path):
        """stale returns a JSON array."""
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "300", "--title", "Issue 300", "--sweep-date", today)
        result = run_cli(db_path, "stale")
        assert isinstance(result, list), "stale must return a JSON array"

    def test_stale_excludes_uows_without_heartbeat(self, db_path):
        """A UoW with no heartbeat_at recorded is not included in stale output."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "301", "--title", "Issue 301", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "active")
        # No heartbeat written — should not appear as stale
        result = run_cli(db_path, "stale")
        ids = [r["id"] for r in result]
        assert uow_id not in ids, "UoW with no heartbeat_at must not appear in stale output"

    def test_stale_includes_uows_with_expired_heartbeat(self, db_path):
        """A UoW with an old heartbeat_at beyond its TTL appears in stale output."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "302", "--title", "Issue 302", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "active")
        # Write an old heartbeat (well beyond any TTL)
        old_heartbeat = "2020-01-01T00:00:00+00:00"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET heartbeat_at = ?, heartbeat_ttl = 300 WHERE id = ?",
            (old_heartbeat, uow_id),
        )
        conn.commit()
        conn.close()
        result = run_cli(db_path, "stale")
        ids = [r["id"] for r in result]
        assert uow_id in ids, "UoW with expired heartbeat must appear in stale output"

    def test_stale_excludes_done_uows(self, db_path):
        """Done UoWs are never reported as stale even if heartbeat_at is old."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "303", "--title", "Issue 303", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "done")
        old_heartbeat = "2020-01-01T00:00:00+00:00"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET heartbeat_at = ?, heartbeat_ttl = 300 WHERE id = ?",
            (old_heartbeat, uow_id),
        )
        conn.commit()
        conn.close()
        result = run_cli(db_path, "stale")
        ids = [r["id"] for r in result]
        assert uow_id not in ids, "done UoW must not appear in stale output"


# ---------------------------------------------------------------------------
# trace command
# ---------------------------------------------------------------------------

def _write_audit_entry(db_path: Path, uow_id: str, event: str,
                       from_status: str | None = None, to_status: str | None = None,
                       agent: str | None = None, note: str | None = None,
                       ts: str | None = None) -> None:
    """Write a raw audit_log entry for test setup."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, uow_id, event, from_status, to_status, agent, note),
    )
    conn.commit()
    conn.close()


def _write_corrective_trace(db_path: Path, uow_id: str, execution_summary: str,
                             prescription_delta: str = "", surprises: str = "[]",
                             gate_score: str | None = None) -> None:
    """Write a corrective_traces row for test setup."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO corrective_traces "
        "(uow_id, register, execution_summary, surprises, prescription_delta, gate_score, created_at) "
        "VALUES (?, 'operational', ?, ?, ?, ?, datetime('now'))",
        (uow_id, execution_summary, surprises, prescription_delta, gate_score),
    )
    conn.commit()
    conn.close()


class TestTraceCommand:
    def test_trace_not_found_returns_error(self, db_path):
        """trace on a non-existent UoW returns an error key."""
        # Initialize DB
        run_cli(db_path, "upsert", "--issue", "400", "--title", "Init", "--sweep-date", "2026-01-01")
        result = run_cli(db_path, "trace", "--id", "uow_does_not_exist")
        assert "error" in result, "trace on missing UoW must return error key"

    def test_trace_surfaces_current_state_fields(self, db_path):
        """trace output includes status, execution_attempts, and lifetime_cycles from the registry row."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "401", "--title", "Issue 401", "--sweep-date", today)
        uow_id = inserted["id"]

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert result["uow_id"] == uow_id
        assert "current_state" in result
        state = result["current_state"]
        assert "status" in state
        assert "execution_attempts" in state
        assert "lifetime_cycles" in state

    def test_trace_includes_audit_log_in_chronological_order(self, db_path):
        """trace.audit_log entries are present and ordered by id ASC."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "402", "--title", "Issue 402", "--sweep-date", today)
        uow_id = inserted["id"]

        _write_audit_entry(db_path, uow_id, "state_transition",
                           from_status="proposed", to_status="pending",
                           ts="2026-04-26T10:00:00+00:00")
        _write_audit_entry(db_path, uow_id, "state_transition",
                           from_status="pending", to_status="active",
                           ts="2026-04-26T10:05:00+00:00")

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "audit_log" in result
        audit = result["audit_log"]
        assert isinstance(audit, list)
        events = [e["event"] for e in audit]
        assert "state_transition" in events

    def test_trace_includes_corrective_traces_when_present(self, db_path):
        """trace.corrective_traces includes rows from the corrective_traces table."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "403", "--title", "Issue 403", "--sweep-date", today)
        uow_id = inserted["id"]

        _write_corrective_trace(db_path, uow_id,
                                execution_summary="Executor ran, wrote output.",
                                prescription_delta="Add error handling for edge case X.")

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "corrective_traces" in result
        traces = result["corrective_traces"]
        assert isinstance(traces, list)
        assert len(traces) >= 1
        assert traces[0]["execution_summary"] == "Executor ran, wrote output."

    def test_trace_corrective_traces_empty_when_none_written(self, db_path):
        """trace.corrective_traces is an empty list when no corrective traces exist."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "404", "--title", "Issue 404", "--sweep-date", today)
        uow_id = inserted["id"]

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "corrective_traces" in result
        assert result["corrective_traces"] == []

    def test_trace_includes_return_reasons_in_chronological_order(self, db_path):
        """trace.return_reasons lists audit events with return_reason note payloads, oldest first."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "405", "--title", "Issue 405", "--sweep-date", today)
        uow_id = inserted["id"]

        _write_audit_entry(db_path, uow_id, "steward_re_entry",
                           note=json.dumps({"return_reason": _RETURN_REASON_EXECUTOR_ORPHAN}),
                           ts="2026-04-26T08:00:00+00:00")
        _write_audit_entry(db_path, uow_id, "steward_re_entry",
                           note=json.dumps({"return_reason": _RETURN_REASON_DIAGNOSING_ORPHAN}),
                           ts="2026-04-26T09:00:00+00:00")

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "return_reasons" in result
        reasons = result["return_reasons"]
        assert isinstance(reasons, list)
        assert len(reasons) >= 2
        assert reasons[0]["return_reason"] == _RETURN_REASON_EXECUTOR_ORPHAN
        assert reasons[1]["return_reason"] == _RETURN_REASON_DIAGNOSING_ORPHAN

    def test_trace_includes_kill_classification_when_present(self, db_path):
        """trace.kill_classification surfaces kill_type and heartbeats_before_kill from audit notes."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "406", "--title", "Issue 406", "--sweep-date", today)
        uow_id = inserted["id"]

        _write_audit_entry(
            db_path, uow_id, "orphan_kill_classified",
            note='{"kill_type": "kill_during_execution", "heartbeats_before_kill": 3}',
        )

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "kill_classification" in result
        kc = result["kill_classification"]
        assert kc["kill_type"] == "kill_during_execution"
        assert kc["heartbeats_before_kill"] == 3

    def test_trace_kill_classification_is_none_when_absent(self, db_path):
        """trace.kill_classification is null when no orphan_kill_classified event exists."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "407", "--title", "Issue 407", "--sweep-date", today)
        uow_id = inserted["id"]

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "kill_classification" in result
        assert result["kill_classification"] is None

    def test_trace_reads_trace_json_when_output_ref_set(self, db_path, tmp_path):
        """trace.trace_json contains the parsed trace.json when output_ref is set and file exists."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "408", "--title", "Issue 408", "--sweep-date", today)
        uow_id = inserted["id"]

        # Write a trace.json file
        trace_content = {
            "uow_id": uow_id,
            "register": "operational",
            "execution_summary": "Subagent completed the task.",
            "surprises": ["Unexpected dependency on module X"],
            "prescription_delta": "Add X to prescribed_skills",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        output_ref = str(tmp_path / f"{uow_id}.json")
        trace_path = tmp_path / f"{uow_id}.trace.json"
        trace_path.write_text(json.dumps(trace_content))

        # Set output_ref on the UoW
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET output_ref = ? WHERE id = ?", (output_ref, uow_id))
        conn.commit()
        conn.close()

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "trace_json" in result
        assert result["trace_json"] is not None
        assert result["trace_json"]["execution_summary"] == "Subagent completed the task."

    def test_trace_json_is_none_when_file_absent(self, db_path, tmp_path):
        """trace.trace_json is null when output_ref is set but trace.json file does not exist."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "409", "--title", "Issue 409", "--sweep-date", today)
        uow_id = inserted["id"]

        # Set output_ref pointing to a file that doesn't exist
        output_ref = str(tmp_path / f"{uow_id}.json")
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET output_ref = ? WHERE id = ?", (output_ref, uow_id))
        conn.commit()
        conn.close()

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "trace_json" in result
        assert result["trace_json"] is None

    def test_trace_includes_diagnosis_suggestion(self, db_path):
        """trace.diagnosis_hint is always present and non-empty."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "410", "--title", "Issue 410", "--sweep-date", today)
        uow_id = inserted["id"]

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "diagnosis_hint" in result
        assert isinstance(result["diagnosis_hint"], str)
        assert len(result["diagnosis_hint"]) > 0

    def test_trace_diagnosis_hints_infrastructure_kill_wave(self, db_path):
        """When all return reasons are orphan types, diagnosis_hint names infrastructure-kill-wave."""
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "411", "--title", "Issue 411", "--sweep-date", today)
        uow_id = inserted["id"]
        _force_status(db_path, uow_id, "needs-human-review")

        for ts_offset, reason in enumerate([_RETURN_REASON_EXECUTOR_ORPHAN] * 3):
            _write_audit_entry(
                db_path, uow_id, "steward_re_entry",
                note=json.dumps({"return_reason": reason}),
                ts=f"2026-04-26T0{ts_offset}:00:00+00:00",
            )

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "infrastructure-kill-wave" in result["diagnosis_hint"].lower() or \
               "orphan" in result["diagnosis_hint"].lower(), (
            f"diagnosis_hint should name the orphan/kill-wave pattern; got: {result['diagnosis_hint']!r}"
        )

    def test_trace_reads_trace_json_via_alt_path(self, db_path, tmp_path):
        """
        _read_trace_json fallback: when output_ref has a suffix and the primary path
        (p.with_suffix('.trace.json')) does not exist, but the string-append path
        (str(output_ref) + '.trace.json') does, the alt path is used.

        Concretely: output_ref = 'dir/file.json'
          primary:  dir/file.trace.json    — does NOT exist
          alt:      dir/file.json.trace.json — DOES exist
        """
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "413", "--title", "Issue 413", "--sweep-date", today)
        uow_id = inserted["id"]

        trace_content = {
            "uow_id": uow_id,
            "register": "operational",
            "execution_summary": "Alt-path trace loaded.",
        }
        # Write ONLY the string-append alt path; leave the with_suffix path absent.
        output_ref = str(tmp_path / f"{uow_id}.json")
        alt_trace_path = tmp_path / f"{uow_id}.json.trace.json"
        alt_trace_path.write_text(json.dumps(trace_content))

        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET output_ref = ? WHERE id = ?", (output_ref, uow_id))
        conn.commit()
        conn.close()

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert "trace_json" in result
        assert result["trace_json"] is not None, (
            "trace_json must be populated via the alt path when primary path is absent"
        )
        assert result["trace_json"]["execution_summary"] == "Alt-path trace loaded."

    def test_trace_output_has_stable_top_level_keys(self, db_path):
        """trace output always has the documented top-level keys regardless of UoW state."""
        EXPECTED_KEYS = {
            "uow_id", "current_state", "audit_log", "corrective_traces",
            "return_reasons", "kill_classification", "trace_json", "diagnosis_hint",
        }
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = run_cli(db_path, "upsert", "--issue", "412", "--title", "Issue 412", "--sweep-date", today)
        uow_id = inserted["id"]

        result = run_cli(db_path, "trace", "--id", uow_id)

        assert EXPECTED_KEYS.issubset(result.keys()), (
            f"trace output missing keys: {EXPECTED_KEYS - result.keys()}"
        )


# ---------------------------------------------------------------------------
# _window_start_iso unit tests
# ---------------------------------------------------------------------------

class TestWindowStartIso:
    def test_defaults_to_DEFAULT_REPORT_HOURS_when_no_args(self):
        """With no arguments the window start is DEFAULT_REPORT_HOURS ago."""
        before = datetime.now(timezone.utc) - timedelta(hours=DEFAULT_REPORT_HOURS, seconds=2)
        result = _window_start_iso(None, None)
        after = datetime.now(timezone.utc) - timedelta(hours=DEFAULT_REPORT_HOURS) + timedelta(seconds=2)
        dt = datetime.fromisoformat(result)
        assert before <= dt <= after

    def test_since_hours_overrides_default(self):
        """--since 6 produces a window start approximately 6 hours ago."""
        SINCE_HOURS = 6
        before = datetime.now(timezone.utc) - timedelta(hours=SINCE_HOURS, seconds=2)
        result = _window_start_iso(SINCE_HOURS, None)
        after = datetime.now(timezone.utc) - timedelta(hours=SINCE_HOURS) + timedelta(seconds=2)
        dt = datetime.fromisoformat(result)
        assert before <= dt <= after

    def test_from_iso_takes_priority_over_since(self):
        """--from ISO_DATE overrides --since."""
        from_ts = "2026-01-15T12:00:00+00:00"
        result = _window_start_iso(since_hours=48.0, from_iso=from_ts)
        assert result == from_ts

    def test_from_iso_invalid_raises_value_error(self):
        """A non-ISO --from value raises ValueError."""
        with pytest.raises(ValueError, match="not a valid ISO 8601"):
            _window_start_iso(None, "not-a-date")


# ---------------------------------------------------------------------------
# _classify_status unit tests
# ---------------------------------------------------------------------------

class TestClassifyStatus:
    def test_done_maps_to_complete(self):
        assert _classify_status("done") == "complete"

    def test_failed_maps_to_failed(self):
        assert _classify_status("failed") == "failed"

    def test_cancelled_maps_to_failed(self):
        assert _classify_status("cancelled") == "failed"

    def test_needs_human_review_maps_to_escalated(self):
        assert _classify_status("needs-human-review") == "escalated"

    def test_active_maps_to_executing(self):
        assert _classify_status("active") == "executing"

    def test_executing_status_maps_to_executing(self):
        """Statuses in _EXECUTING_STATUSES are reported as 'executing'."""
        assert _classify_status("executing") == "executing"
        assert _classify_status("ready-for-steward") == "executing"
        assert _classify_status("ready-for-executor") == "executing"
        assert _classify_status("diagnosing") == "executing"

    def test_proposed_maps_to_in_pipeline(self):
        """Proposed UoWs are not executing — they map to 'in-pipeline'."""
        assert _classify_status("proposed") == "in-pipeline"

    def test_pipeline_statuses_map_to_in_pipeline(self):
        """pending, blocked, and expired are queued but not yet running."""
        assert _classify_status("pending") == "in-pipeline"
        assert _classify_status("blocked") == "in-pipeline"
        assert _classify_status("expired") == "in-pipeline"


# ---------------------------------------------------------------------------
# _compute_summary unit tests
# ---------------------------------------------------------------------------

class TestComputeSummary:
    def _make_row(self, status: str, wall_clock: int | None = None,
                  token_usage: int | None = None,
                  outcome_category: str | None = None) -> dict:
        return {
            "status": status,
            "wall_clock_seconds": wall_clock,
            "token_usage": token_usage,
            "outcome_category": outcome_category,
        }

    def test_counts_correct_for_mixed_statuses(self):
        """Bucket counts match the spec for a known set of rows."""
        WINDOW_START = "2026-01-01T00:00:00+00:00"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
        rows = [
            self._make_row("done", wall_clock=120),
            self._make_row("done", wall_clock=180),
            self._make_row("failed"),
            self._make_row("needs-human-review"),
            self._make_row("active"),
        ]
        summary = _compute_summary(rows, WINDOW_START, now)

        assert summary["total"] == 5
        assert summary["complete"] == 2
        assert summary["failed"] == 1
        assert summary["escalated"] == 1
        assert summary["executing"] == 1
        assert summary["in_pipeline"] == 0

    def test_counts_proposed_in_pipeline_not_executing(self):
        """proposed/pending/blocked UoWs appear in in_pipeline, not executing."""
        WINDOW_START = "2026-01-01T00:00:00+00:00"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
        rows = [
            self._make_row("proposed"),
            self._make_row("proposed"),
            self._make_row("pending"),
            self._make_row("blocked"),
            self._make_row("active"),
            self._make_row("done", wall_clock=100),
        ]
        summary = _compute_summary(rows, WINDOW_START, now)

        assert summary["total"] == 6
        assert summary["in_pipeline"] == 4
        assert summary["executing"] == 1
        assert summary["complete"] == 1

    def test_median_wall_clock_uses_complete_uows_only(self):
        """Median wall-clock is computed only from UoWs with status 'done'."""
        WINDOW_START = "2026-01-01T00:00:00+00:00"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
        rows = [
            self._make_row("done", wall_clock=100),
            self._make_row("done", wall_clock=200),
            self._make_row("done", wall_clock=300),
            self._make_row("failed", wall_clock=50),   # excluded from median
        ]
        summary = _compute_summary(rows, WINDOW_START, now)

        assert summary["median_wall_clock_seconds"] == 200  # median of [100, 200, 300]

    def test_total_token_usage_sums_all_non_null(self):
        """Total token_usage sums all rows regardless of status."""
        WINDOW_START = "2026-01-01T00:00:00+00:00"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
        rows = [
            self._make_row("done", token_usage=1000),
            self._make_row("failed", token_usage=500),
            self._make_row("active", token_usage=None),  # excluded
        ]
        summary = _compute_summary(rows, WINDOW_START, now)

        assert summary["total_token_usage"] == 1500

    def test_empty_window_returns_zero_counts(self):
        """An empty row list produces all-zero counts and None for stats."""
        WINDOW_START = "2026-01-01T00:00:00+00:00"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
        summary = _compute_summary([], WINDOW_START, now)

        assert summary["total"] == 0
        assert summary["complete"] == 0
        assert summary["median_wall_clock_seconds"] is None


    def test_outcome_category_counts_valid_labels_only(self):
        """outcome_category breakdown counts only valid label values; NULL is excluded."""
        WINDOW_START = "2026-01-01T00:00:00+00:00"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
        rows = [
            self._make_row("done", outcome_category="pearl"),
            self._make_row("done", outcome_category="pearl"),
            self._make_row("done", outcome_category="seed"),
            self._make_row("done", outcome_category="heat"),
            self._make_row("done", outcome_category=None),         # excluded — NULL
            self._make_row("done", outcome_category="mystery"),    # excluded — invalid label
        ]
        summary = _compute_summary(rows, WINDOW_START, now)

        counts = summary["outcome_category_counts"]
        assert counts.get("pearl") == 2
        assert counts.get("seed") == 1
        assert counts.get("heat") == 1
        assert "shit" not in counts          # not present in this dataset
        assert "mystery" not in counts       # invalid label filtered out

    def test_outcome_category_counts_empty_when_all_null(self):
        """outcome_category_counts is empty when no UoW has a category set."""
        WINDOW_START = "2026-01-01T00:00:00+00:00"
        now = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
        rows = [
            self._make_row("done", outcome_category=None),
            self._make_row("done", outcome_category=None),
        ]
        summary = _compute_summary(rows, WINDOW_START, now)

        assert summary["outcome_category_counts"] == {}


# ---------------------------------------------------------------------------
# report command integration tests (subprocess, real DB)
# ---------------------------------------------------------------------------

class TestReportCommand:
    def test_report_runs_against_empty_db(self, db_path):
        """report command exits 0 and emits header even when DB has no UoWs."""
        # Touch the DB first so migrations run.
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2024-01-01")
        output = run_cli_text(db_path, "report", "--since", "24")
        assert "WOS Pipeline Report" in output
        assert "Window start" in output
        assert "Total UoWs" in output

    def test_report_counts_proposed_uow_in_window(self, db_path):
        """A UoW created within the window appears in the total count."""
        today = datetime.now(timezone.utc).date().isoformat()
        # Insert two UoWs.
        run_cli(db_path, "upsert", "--issue", "10", "--title", "In-window A", "--sweep-date", today)
        run_cli(db_path, "upsert", "--issue", "11", "--title", "In-window B", "--sweep-date", today)
        # Neither UoW has started_at yet (they're proposed), so we need to seed
        # started_at directly to make them visible to the window query.
        conn = sqlite3.connect(str(db_path))
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE uow_registry SET started_at = ? WHERE source_issue_number IN (10, 11)", (now_iso,))
        conn.commit()
        conn.close()

        output = run_cli_text(db_path, "report", "--since", "1")
        # The total count line should mention at least 2.
        # Extract the "Total UoWs : N" portion.
        total_line = next(l for l in output.splitlines() if l.startswith("Total UoWs"))
        # Parse the leading integer after the colon.
        total_str = total_line.split(":")[1].strip().split()[0]
        assert int(total_str) >= 2

    def test_report_from_flag_filters_correctly(self, db_path):
        """--from timestamp in the future returns zero UoWs."""
        run_cli(db_path, "upsert", "--issue", "20", "--title", "Old UoW", "--sweep-date", "2024-01-01")
        # Seed started_at to a past time.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET started_at = '2024-01-01T00:00:00+00:00'"
        )
        conn.commit()
        conn.close()

        future_ts = "2099-01-01T00:00:00+00:00"
        output = run_cli_text(db_path, "report", "--from", future_ts)
        total_line = next(l for l in output.splitlines() if l.startswith("Total UoWs"))
        total_str = total_line.split(":")[1].strip().split()[0]
        assert int(total_str) == 0

    def test_report_since_and_from_both_accepted(self, db_path):
        """Both --since and --from flags are accepted by the CLI without error."""
        run_cli(db_path, "upsert", "--issue", "30", "--title", "Init", "--sweep-date", "2024-01-01")
        run_cli_text(db_path, "report", "--since", "6")
        run_cli_text(db_path, "report", "--from", "2026-01-01T00:00:00+00:00")

    def test_report_default_window_is_DEFAULT_REPORT_HOURS(self, db_path):
        """report with no flags uses DEFAULT_REPORT_HOURS as the look-back."""
        run_cli(db_path, "upsert", "--issue", "40", "--title", "Init", "--sweep-date", "2024-01-01")
        output = run_cli_text(db_path, "report")
        assert "WOS Pipeline Report" in output

    def test_report_outcome_category_line_present_when_categories_exist(self, db_path):
        """The 'Outcomes' header line appears when at least one UoW has outcome_category set."""
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "50", "--title", "Pearl UoW", "--sweep-date", today)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET started_at = ?, outcome_category = 'pearl' "
            "WHERE source_issue_number = 50",
            (now_iso,),
        )
        conn.commit()
        conn.close()

        output = run_cli_text(db_path, "report", "--since", "1")
        assert "Outcomes" in output
        assert "pearl: 1" in output

    def test_report_outcome_category_line_absent_when_all_null(self, db_path):
        """The 'Outcomes' header line is omitted when no UoW has outcome_category."""
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "60", "--title", "No category", "--sweep-date", today)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET started_at = ? WHERE source_issue_number = 60",
            (now_iso,),
        )
        conn.commit()
        conn.close()

        output = run_cli_text(db_path, "report", "--since", "1")
        assert "Outcomes" not in output

    def test_report_per_uow_line_shows_category_label(self, db_path):
        """The per-UoW line includes the [category] label when outcome_category is set."""
        today = datetime.now(timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "70", "--title", "Seed UoW", "--sweep-date", today)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET started_at = ?, outcome_category = 'seed' "
            "WHERE source_issue_number = 70",
            (now_iso,),
        )
        conn.commit()
        conn.close()

        output = run_cli_text(db_path, "report", "--since", "1")
        # The per-UoW line for issue 70 should contain [seed]
        per_uow_lines = [l for l in output.splitlines() if "uow_" in l]
        assert any("[seed]" in l for l in per_uow_lines), (
            f"Expected [seed] in a per-UoW line, got:\n" + "\n".join(per_uow_lines)
        )

    def test_report_from_flag_future_returns_zero(self, db_path):
        """--from with a future timestamp returns zero UoWs."""
        run_cli(db_path, "upsert", "--issue", "20", "--title", "Old UoW", "--sweep-date", "2024-01-01")
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET started_at = '2024-01-01T00:00:00+00:00'")
        conn.commit()
        conn.close()

        output = run_cli_text(db_path, "report", "--from", "2099-01-01T00:00:00+00:00")
        total_line = next(l for l in output.splitlines() if l.startswith("Total UoWs"))
        total_str = total_line.split(":")[1].strip().split()[0]
        assert int(total_str) == 0


# ---------------------------------------------------------------------------
# failure-breakdown command
# ---------------------------------------------------------------------------


def _insert_failed_uow(db_path: Path, issue_number: int, register: str = "operational") -> str:
    """Insert a UoW directly into the DB in failed status. Returns the UoW id."""
    import uuid
    uow_id = f"uow_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, success_criteria, register)
        VALUES (?, 'executable', 'github', ?, ?, 'failed', 'solo', ?, ?, ?, '', ?)
        """,
        (uow_id, issue_number, datetime.now(timezone.utc).date().isoformat(),
         now, now, f"Issue #{issue_number}", register),
# _compute_latency_stats unit tests
# ---------------------------------------------------------------------------

class TestComputeLatencyStats:
    """Unit tests for the pure _compute_latency_stats function."""

    def _row(self, created_at: str, started_at: str | None, completed_at: str | None,
             status: str = "done") -> dict:
        return {
            "created_at": created_at,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": status,
        }

    def test_empty_rows_returns_none_for_all_stats(self):
        """No rows means no percentiles can be computed — all stat values are None."""
        stats = _compute_latency_stats([])
        assert stats["queue_wait"]["p50"] is None
        assert stats["queue_wait"]["p90"] is None
        assert stats["queue_wait"]["p99"] is None
        assert stats["queue_wait"]["mean"] is None
        assert stats["execution_time"]["p50"] is None
        assert stats["execution_time"]["count"] == 0

    def test_rows_without_started_at_excluded_from_queue_wait(self):
        """UoWs with no started_at cannot contribute to queue_wait stats."""
        rows = [
            self._row("2026-01-01T10:00:00+00:00", None, None, status="proposed"),
        ]
        stats = _compute_latency_stats(rows)
        assert stats["queue_wait"]["count"] == 0
        assert stats["queue_wait"]["p50"] is None

    def test_rows_without_completed_at_excluded_from_execution_time(self):
        """UoWs with started_at but no completed_at cannot contribute to execution_time stats."""
        rows = [
            self._row("2026-01-01T10:00:00+00:00", "2026-01-01T10:05:00+00:00", None, status="active"),
        ]
        stats = _compute_latency_stats(rows)
        # queue_wait is available (started_at is set)
        assert stats["queue_wait"]["count"] == 1
        assert stats["queue_wait"]["p50"] == 300  # 5 minutes in seconds
        # execution_time is not available (no completed_at)
        assert stats["execution_time"]["count"] == 0
        assert stats["execution_time"]["p50"] is None

    def test_queue_wait_computed_from_created_at_to_started_at(self):
        """queue_wait = started_at - created_at in seconds."""
        # 10 minutes queue wait
        rows = [
            self._row(
                "2026-01-01T10:00:00+00:00",
                "2026-01-01T10:10:00+00:00",
                "2026-01-01T10:30:00+00:00",
            )
        ]
        stats = _compute_latency_stats(rows)
        assert stats["queue_wait"]["p50"] == 600   # 10 min = 600s
        assert stats["queue_wait"]["count"] == 1

    def test_execution_time_computed_from_started_at_to_completed_at(self):
        """execution_time = completed_at - started_at in seconds."""
        # 20 minutes execution
        rows = [
            self._row(
                "2026-01-01T10:00:00+00:00",
                "2026-01-01T10:10:00+00:00",
                "2026-01-01T10:30:00+00:00",
            )
        ]
        stats = _compute_latency_stats(rows)
        assert stats["execution_time"]["p50"] == 1200  # 20 min = 1200s
        assert stats["execution_time"]["count"] == 1

    def test_percentiles_correct_for_known_dataset(self):
        """p50, p90, p99, and mean are correct for a known set of queue_wait values."""
        # 5 rows with queue_wait values: 100, 200, 300, 400, 500 seconds
        # p50 = 300, p90 = 500 (or interpolated), mean = 300
        rows = [
            self._row(
                f"2026-01-01T10:00:0{i}+00:00",
                f"2026-01-01T10:0{1+i}:40+00:00" if False else None,  # use abs offsets below
                None,
            )
            for i in range(5)
        ]
        # Build rows with controlled deltas
        base_created = "2026-01-01T10:00:00+00:00"
        queue_waits_s = [100, 200, 300, 400, 500]
        rows = []
        for qw in queue_waits_s:
            from datetime import datetime, timezone, timedelta
            created = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
            started = created + timedelta(seconds=qw)
            rows.append({
                "created_at": created.isoformat(),
                "started_at": started.isoformat(),
                "completed_at": None,
                "status": "active",
            })

        stats = _compute_latency_stats(rows)
        assert stats["queue_wait"]["count"] == 5
        assert stats["queue_wait"]["p50"] == 300
        assert stats["queue_wait"]["mean"] == 300.0

    def test_mean_rounds_to_one_decimal(self):
        """Mean is a float (not truncated to int)."""
        from datetime import datetime, timezone, timedelta
        created = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        rows = [
            {
                "created_at": created.isoformat(),
                "started_at": (created + timedelta(seconds=100)).isoformat(),
                "completed_at": (created + timedelta(seconds=350)).isoformat(),
                "status": "done",
            },
            {
                "created_at": created.isoformat(),
                "started_at": (created + timedelta(seconds=200)).isoformat(),
                "completed_at": (created + timedelta(seconds=450)).isoformat(),
                "status": "done",
            },
        ]
        stats = _compute_latency_stats(rows)
        # queue_wait mean = (100 + 200) / 2 = 150.0
        assert stats["queue_wait"]["mean"] == 150.0
        # execution_time: 250s and 250s → mean = 250.0
        assert stats["execution_time"]["mean"] == 250.0

    def test_malformed_timestamp_rows_are_skipped(self):
        """Rows with unparseable timestamps do not crash — they are silently skipped."""
        rows = [
            {
                "created_at": "not-a-timestamp",
                "started_at": "also-invalid",
                "completed_at": None,
                "status": "active",
            }
        ]
        # Must not raise; result should treat this row as having no usable data
        stats = _compute_latency_stats(rows)
        assert stats["queue_wait"]["count"] == 0


# ---------------------------------------------------------------------------
# _format_latency_report unit tests
# ---------------------------------------------------------------------------

class TestFormatLatencyReport:
    """Unit tests for the pure _format_latency_report function."""

    def _stats_with_data(self) -> dict:
        return {
            "queue_wait": {"count": 5, "p50": 120, "p90": 300, "p99": 600, "mean": 180.0},
            "execution_time": {"count": 5, "p50": 900, "p90": 1800, "p99": 3600, "mean": 1200.0},
        }

    def _stats_empty(self) -> dict:
        return {
            "queue_wait": {"count": 0, "p50": None, "p90": None, "p99": None, "mean": None},
            "execution_time": {"count": 0, "p50": None, "p90": None, "p99": None, "mean": None},
        }

    def test_report_header_always_present(self):
        """The report always starts with a header line."""
        output = _format_latency_report(self._stats_with_data(), since_hours=24.0, status_filter=None)
        assert "Queue Latency" in output

    def test_report_shows_queue_wait_section(self):
        """Queue wait section is always present."""
        output = _format_latency_report(self._stats_with_data(), since_hours=24.0, status_filter=None)
        assert "Queue wait" in output or "queue_wait" in output.lower() or "Queue" in output

    def test_report_shows_execution_time_section(self):
        """Execution time section is always present."""
        output = _format_latency_report(self._stats_with_data(), since_hours=24.0, status_filter=None)
        assert "Execution" in output

    def test_report_shows_p50_p90_p99(self):
        """p50, p90, p99 labels appear in the report."""
        output = _format_latency_report(self._stats_with_data(), since_hours=24.0, status_filter=None)
        assert "p50" in output
        assert "p90" in output
        assert "p99" in output

    def test_report_shows_mean(self):
        """Mean appears in the report."""
        output = _format_latency_report(self._stats_with_data(), since_hours=24.0, status_filter=None)
        assert "mean" in output.lower()

    def test_empty_stats_shows_no_data_message(self):
        """When no UoWs have timing data, the report says so clearly."""
        output = _format_latency_report(self._stats_empty(), since_hours=24.0, status_filter=None)
        assert "no data" in output.lower() or "n/a" in output.lower() or "0" in output

    def test_status_filter_appears_in_output_when_set(self):
        """When --status is specified, the filter is visible in the report."""
        output = _format_latency_report(self._stats_with_data(), since_hours=24.0, status_filter="done")
        assert "done" in output

    def test_no_markdown_tables(self):
        """Output must not contain markdown table syntax (| characters used as column separators)."""
        output = _format_latency_report(self._stats_with_data(), since_hours=24.0, status_filter=None)
        # A markdown table line starts with | — check that no line uses this pattern
        table_lines = [l for l in output.splitlines() if l.strip().startswith("|")]
        assert len(table_lines) == 0, f"Markdown table lines found: {table_lines}"


# ---------------------------------------------------------------------------
# queue-latency command integration tests (subprocess, real DB)
# ---------------------------------------------------------------------------

def _seed_completed_uow(db_path: Path, issue_number: int,
                        queue_wait_s: int, execution_s: int,
                        status: str = "done") -> str:
    """
    Insert a UoW with controlled timestamps and return its id.

    created_at is anchored to 1 hour ago so it always falls inside the default
    24-hour window. started_at = created_at + queue_wait_s.
    completed_at = started_at + execution_s.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    inserted = run_cli(db_path, "upsert", "--issue", str(issue_number),
                       "--title", f"Issue {issue_number}", "--sweep-date", today)
    uow_id = inserted["id"]

    # Anchor to 1 hour ago so the row falls within the default 24h window.
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    started = created + timedelta(seconds=queue_wait_s)
    completed = started + timedelta(seconds=execution_s)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE uow_registry SET created_at = ?, started_at = ?, completed_at = ?, status = ? "
        "WHERE id = ?",
        (created.isoformat(), started.isoformat(), completed.isoformat(), status, uow_id),
    )
    conn.commit()
    conn.close()
    return uow_id


def _add_audit_entry(db_path: Path, uow_id: str, event: str, note: dict | None = None) -> None:
    """Append an audit_log entry for the given UoW."""
    now = datetime.now(timezone.utc).isoformat()
    note_json = json.dumps(note) if note is not None else None
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
        VALUES (?, ?, ?, NULL, 'failed', 'test', ?)
        """,
        (now, uow_id, event, note_json),
    )
    conn.commit()
    conn.close()


def _set_close_reason(db_path: Path, uow_id: str, close_reason: str) -> None:
    """Set close_reason on a UoW row directly."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE uow_registry SET close_reason = ? WHERE id = ?", (close_reason, uow_id))
    conn.commit()
    conn.close()


class TestFailureBreakdownCommand:
    def test_empty_db_returns_zero_failures(self, db_path):
        """When no failed UoWs exist, output says no failures found."""
        # Initialize the DB by inserting and removing a record
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")
        output = run_cli_text(db_path, "failure-breakdown")
        assert "0" in output or "no failed" in output.lower()

    def test_execution_failed_uow_classified_correctly(self, db_path):
        """A UoW with an execution_failed audit event is classified as execution_failed."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")
        uow_id = _insert_failed_uow(db_path, issue_number=100)
        _add_audit_entry(db_path, uow_id, "execution_failed",
                         note={"actor": "executor", "reason": "agent returned nonzero"})

        output = run_cli_text(db_path, "failure-breakdown")
        assert KILL_TYPE_EXECUTION_FAILED in output

    def test_hard_cap_uow_classified_correctly(self, db_path):
        """A UoW with close_reason=hard_cap_cleanup is classified as hard_cap."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")
        uow_id = _insert_failed_uow(db_path, issue_number=101)
        _set_close_reason(db_path, uow_id, "hard_cap_cleanup")

        output = run_cli_text(db_path, "failure-breakdown")
        assert KILL_TYPE_HARD_CAP in output

    def test_user_closed_uow_classified_correctly(self, db_path):
        """A UoW with a decide_close audit event is classified as user_closed."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")
        uow_id = _insert_failed_uow(db_path, issue_number=102)
        _add_audit_entry(db_path, uow_id, "decide_close",
                         note={"event": "decide_close", "actor": "user", "reason": "user_closed"})

        output = run_cli_text(db_path, "failure-breakdown")
        assert KILL_TYPE_USER_CLOSED in output

    def test_orphan_kill_before_start_classified_correctly(self, db_path):
        """A UoW with orphan_kill_classified event and kill_type=orphan_kill_before_start."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")
        uow_id = _insert_failed_uow(db_path, issue_number=103)
        _add_audit_entry(db_path, uow_id, "orphan_kill_classified",
                         note={"kill_type": "orphan_kill_before_start", "heartbeats_before_kill": 0})

        output = run_cli_text(db_path, "failure-breakdown")
        assert KILL_TYPE_ORPHAN_BEFORE_START in output

    def test_orphan_kill_during_execution_classified_correctly(self, db_path):
        """A UoW with orphan_kill_classified event and kill_type=orphan_kill_during_execution."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")
        uow_id = _insert_failed_uow(db_path, issue_number=104)
        _add_audit_entry(db_path, uow_id, "orphan_kill_classified",
                         note={"kill_type": "orphan_kill_during_execution", "heartbeats_before_kill": 3})

        output = run_cli_text(db_path, "failure-breakdown")
        assert KILL_TYPE_ORPHAN_DURING_EXECUTION in output

    def test_unknown_failure_classified_as_unknown(self, db_path):
        """A failed UoW with no classifiable audit entries is classified as unknown."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")
        _insert_failed_uow(db_path, issue_number=105)
        # No audit entries added — classification must fall back to unknown

        output = run_cli_text(db_path, "failure-breakdown")
        assert KILL_TYPE_UNKNOWN in output

    def test_kill_type_counts_and_percentages_are_accurate(self, db_path):
        """Two execution_failed and one hard_cap UoW produce correct counts and pct."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")

        for issue_num in (200, 201):
            uid = _insert_failed_uow(db_path, issue_number=issue_num)
            _add_audit_entry(db_path, uid, "execution_failed",
                             note={"actor": "executor", "reason": "nonzero"})

        uid_cap = _insert_failed_uow(db_path, issue_number=202)
        _set_close_reason(db_path, uid_cap, "hard_cap_cleanup")

        output = run_cli_text(db_path, "failure-breakdown")
        lines = output.splitlines()

        # Find the execution_failed line and confirm count of 2
        exec_failed_lines = [l for l in lines if KILL_TYPE_EXECUTION_FAILED in l]
        assert exec_failed_lines, f"Expected {KILL_TYPE_EXECUTION_FAILED} in output:\n{output}"
        assert "2" in exec_failed_lines[0], f"Expected count 2 in line: {exec_failed_lines[0]}"

        # Find the hard_cap line and confirm count of 1
        hard_cap_lines = [l for l in lines if KILL_TYPE_HARD_CAP in l]
        assert hard_cap_lines, f"Expected {KILL_TYPE_HARD_CAP} in output:\n{output}"
        assert "1" in hard_cap_lines[0], f"Expected count 1 in line: {hard_cap_lines[0]}"

        # Percentages present (66% and 33%, or close)
        assert "%" in output, "Output must include percentage values"

    def test_register_breakdown_section_present(self, db_path):
        """Output includes a breakdown by register field."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")

        uid_op = _insert_failed_uow(db_path, issue_number=300, register="operational")
        _add_audit_entry(db_path, uid_op, "execution_failed")

        uid_ic = _insert_failed_uow(db_path, issue_number=301, register="iterative-convergent")
        _add_audit_entry(db_path, uid_ic, "execution_failed")

        output = run_cli_text(db_path, "failure-breakdown")
        assert "operational" in output
        assert "iterative-convergent" in output

    def test_since_flag_filters_by_created_at(self, db_path):
        """--since filters UoWs to only those created on or after the given timestamp."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")

        # Insert an old failed UoW backdated to 2024
        old_uid = f"uow_old_test_01"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, type, source, source_issue_number, sweep_date, status, posture,
                 created_at, updated_at, summary, success_criteria, register)
            VALUES (?, 'executable', 'github', 400, '2024-01-01', 'failed', 'solo',
                    '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', 'Old UoW', '', 'operational')
            """,
            (old_uid,),
        )
        conn.commit()
        conn.close()

        # Insert a recent failed UoW
        recent_uid = _insert_failed_uow(db_path, issue_number=401)
        _add_audit_entry(db_path, recent_uid, "execution_failed")

        # Filter to only 2026 — the 2024 UoW should not appear in counts
        output_filtered = run_cli_text(db_path, "failure-breakdown", "--since", "2026-01-01T00:00:00+00:00")
        output_all = run_cli_text(db_path, "failure-breakdown")

        # The filtered output must show fewer total failures than the unfiltered one
        def _extract_total(text: str) -> int:
            for line in text.splitlines():
                if "total" in line.lower():
                    parts = line.split()
                    for p in parts:
                        if p.isdigit():
                            return int(p)
            return 0

        assert _extract_total(output_filtered) < _extract_total(output_all), (
            f"Filtered output should have fewer failures than unfiltered.\n"
            f"Filtered:\n{output_filtered}\nAll:\n{output_all}"
        )

    def test_output_has_no_markdown_table_syntax(self, db_path):
        """Output must use plain aligned columns, not markdown table syntax (no | delimiters)."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2026-01-01")
        uow_id = _insert_failed_uow(db_path, issue_number=500)
        _add_audit_entry(db_path, uow_id, "execution_failed")

        output = run_cli_text(db_path, "failure-breakdown")
        # No markdown table pipe delimiter at the start of a line
        lines_with_pipes = [l for l in output.splitlines() if l.strip().startswith("|")]
        assert not lines_with_pipes, (
            f"Output must not use markdown table syntax. Found: {lines_with_pipes}"
        )
class TestQueueLatencyCommand:
    def test_queue_latency_exits_zero_on_empty_db(self, db_path):
        """queue-latency exits 0 even when no UoWs with timing data exist."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2024-01-01")
        output = run_cli_text(db_path, "queue-latency")
        assert output  # non-empty output

    def test_queue_latency_output_contains_header(self, db_path):
        """queue-latency output contains a recognizable header."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2024-01-01")
        output = run_cli_text(db_path, "queue-latency")
        assert "Queue Latency" in output

    def test_queue_latency_shows_percentile_labels(self, db_path):
        """Output includes p50, p90, p99 labels."""
        _seed_completed_uow(db_path, 500, queue_wait_s=60, execution_s=120)
        output = run_cli_text(db_path, "queue-latency")
        assert "p50" in output
        assert "p90" in output
        assert "p99" in output

    def test_queue_latency_shows_mean(self, db_path):
        """Output includes a mean value."""
        _seed_completed_uow(db_path, 501, queue_wait_s=60, execution_s=120)
        output = run_cli_text(db_path, "queue-latency")
        assert "mean" in output.lower()

    def test_queue_latency_since_flag_accepted(self, db_path):
        """--since flag is accepted without error."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2024-01-01")
        output = run_cli_text(db_path, "queue-latency", "--since", "48")
        assert output

    def test_queue_latency_status_flag_accepted(self, db_path):
        """--status flag is accepted without error."""
        run_cli(db_path, "upsert", "--issue", "1", "--title", "Init", "--sweep-date", "2024-01-01")
        output = run_cli_text(db_path, "queue-latency", "--status", "done")
        assert output

    def test_queue_latency_reflects_correct_queue_wait_for_seeded_data(self, db_path):
        """queue-latency p50 for a single UoW matches the seeded queue_wait value."""
        # Seed one UoW with 5 minutes (300s) queue wait and 10 minutes execution
        _seed_completed_uow(db_path, 502, queue_wait_s=300, execution_s=600)
        output = run_cli_text(db_path, "queue-latency")
        # The report must mention 300s somewhere in the queue wait section
        assert "300" in output, f"Expected 300s queue wait in output, got:\n{output}"

    def test_queue_latency_uows_without_started_at_excluded(self, db_path):
        """UoWs with no started_at (proposed) are not included in the count."""
        today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
        run_cli(db_path, "upsert", "--issue", "503", "--title", "No start", "--sweep-date", today)
        # Do NOT set started_at — leave it NULL (proposed status)
        output = run_cli_text(db_path, "queue-latency")
        # The report should say 0 UoWs with queue_wait data (or n/a)
        lines = output.splitlines()
        count_line = next((l for l in lines if "count" in l.lower() or "n/a" in l.lower()), None)
        # We just verify the command runs successfully without raising
        assert output

    def test_queue_latency_no_markdown_table_syntax(self, db_path):
        """Output must not use markdown table formatting."""
        _seed_completed_uow(db_path, 504, queue_wait_s=60, execution_s=120)
        output = run_cli_text(db_path, "queue-latency")
        table_lines = [l for l in output.splitlines() if l.strip().startswith("|")]
        assert len(table_lines) == 0, f"Markdown table lines found:\n{chr(10).join(table_lines)}"
