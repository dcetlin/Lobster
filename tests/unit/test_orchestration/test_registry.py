"""
Unit tests for the UoW Registry — registry.py

Tests cover:
- Schema initialization (tables created on first open)
- Upsert: insert new proposed record
- Upsert: dedup logic — skip if non-terminal record already exists for same issue
- Upsert: re-propose after terminal (done/failed/expired)
- Upsert: UNIQUE conflict with conditional update (same issue+sweep_date)
- Approve: proposed → pending transition
- Approve: idempotent on already-pending
- Approve: error on non-existent id
- Approve: error on expired
- List: filter by status (returns list[UoW])
- Get: by id (returns UoW | None)
- Check-stale: returns list[UoW] for active UoWs whose source issue is closed
- Expire-proposals: ages out proposed records > 14 days
- Audit log: every state change writes an audit entry in the same transaction
- WAL mode is enabled
- All writes use BEGIN IMMEDIATE
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

# Path to the registry module under src/orchestration/
REPO_ROOT = Path(__file__).parent.parent.parent.parent
REGISTRY_MODULE = REPO_ROOT / "src" / "orchestration" / "registry.py"


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path):
    """Returns an initialized Registry instance pointed at a temp db."""
    from src.orchestration.registry import Registry
    return Registry(db_path)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestSchemaInit:
    def test_tables_created_on_init(self, registry, db_path):
        conn = _open_db(db_path)
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "uow_registry" in tables
        assert "audit_log" in tables
        conn.close()

    def test_wal_mode_enabled(self, registry, db_path):
        conn = _open_db(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_unique_constraint_exists(self, registry, db_path):
        conn = _open_db(db_path)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='uow_registry'"
        ).fetchall()
        index_names = [r["name"] for r in indexes]
        # SQLite auto-names UNIQUE constraints, so we check any unique index
        # by probing the actual uniqueness constraint
        conn.close()
        # Duplicate upsert with same issue+sweep_date should not create second row
        today = datetime.now(timezone.utc).date().isoformat()
        uow_a = registry.upsert(issue_number=999, title="Test A", sweep_date=today, success_criteria="Test completion.")
        uow_b = registry.upsert(issue_number=999, title="Test A again", sweep_date=today, success_criteria="Test completion.")
        conn2 = _open_db(db_path)
        count = conn2.execute(
            "SELECT COUNT(*) as c FROM uow_registry WHERE source_issue_number = 999"
        ).fetchone()["c"]
        conn2.close()
        assert count == 1, "UNIQUE(source_issue_number, sweep_date) constraint should prevent second row"


# ---------------------------------------------------------------------------
# Upsert tests
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_insert_new_proposed_record(self, registry, db_path):
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=1, title="First issue", sweep_date=today, success_criteria="Test completion.")
        assert isinstance(result, UpsertInserted)
        assert result.id.startswith("uow_")

        conn = _open_db(db_path)
        row = conn.execute("SELECT * FROM uow_registry WHERE id = ?", (result.id,)).fetchone()
        assert row is not None
        assert row["status"] == "proposed"
        assert row["source_issue_number"] == 1
        assert row["posture"] == "solo"
        assert row["route_reason"] == "phase1-default: no classifier"
        conn.close()

    def test_upsert_stores_success_criteria(self, registry, db_path):
        """success_criteria passed to upsert() must be persisted in the INSERT row."""
        from src.orchestration.registry import UpsertInserted
        issue_body = (
            "## Summary\nFix the widget.\n\n"
            "## Acceptance Criteria\n"
            "- Widget renders correctly\n"
            "- No regression in tests\n\n"
            "## Notes\nSee attached mockup."
        )
        from src.orchestration.cultivator import _extract_success_criteria
        criteria = _extract_success_criteria(issue_body)

        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(
            issue_number=9999,
            title="Widget fix",
            sweep_date=today,
            success_criteria=criteria,
        )
        assert isinstance(result, UpsertInserted)

        conn = _open_db(db_path)
        row = conn.execute(
            "SELECT success_criteria FROM uow_registry WHERE id = ?", (result.id,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["success_criteria"], "success_criteria must be non-empty after upsert with issue body"
        assert "Widget renders correctly" in row["success_criteria"]

    def test_audit_entry_on_insert(self, registry, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=2, title="Issue with audit", sweep_date=today, success_criteria="Test completion.")
        conn = _open_db(db_path)
        audit = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ?", (result.id,)
        ).fetchall()
        assert len(audit) >= 1
        assert audit[0]["event"] == "created"
        conn.close()

    def test_skip_if_proposed_already_exists_same_issue(self, registry):
        from src.orchestration.registry import UpsertInserted, UpsertSkipped
        today = datetime.now(timezone.utc).date().isoformat()
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        # First sweep: insert proposed
        first = registry.upsert(issue_number=10, title="Issue 10", sweep_date=yesterday, success_criteria="Test completion.")
        assert isinstance(first, UpsertInserted)
        # Second sweep (different date): should skip since non-terminal exists
        second = registry.upsert(issue_number=10, title="Issue 10", sweep_date=today, success_criteria="Test completion.")
        assert isinstance(second, UpsertSkipped)
        assert "proposed" in second.reason

    def test_skip_if_pending_already_exists(self, registry, db_path):
        from src.orchestration.registry import UpsertSkipped
        today = datetime.now(timezone.utc).date().isoformat()
        first = registry.upsert(issue_number=11, title="Issue 11", sweep_date=today, success_criteria="Test completion.")
        # Manually set to pending
        conn = _open_db(db_path)
        conn.execute("UPDATE uow_registry SET status='pending' WHERE id=?", (first.id,))
        conn.commit()
        conn.close()
        # Next sweep should skip
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        second = registry.upsert(issue_number=11, title="Issue 11", sweep_date=tomorrow, success_criteria="Test completion.")
        assert isinstance(second, UpsertSkipped)

    def test_skip_if_active_already_exists(self, registry, db_path):
        from src.orchestration.registry import UpsertSkipped
        today = datetime.now(timezone.utc).date().isoformat()
        first = registry.upsert(issue_number=12, title="Issue 12", sweep_date=today, success_criteria="Test completion.")
        conn = _open_db(db_path)
        conn.execute("UPDATE uow_registry SET status='active' WHERE id=?", (first.id,))
        conn.commit()
        conn.close()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        second = registry.upsert(issue_number=12, title="Issue 12", sweep_date=tomorrow, success_criteria="Test completion.")
        assert isinstance(second, UpsertSkipped)

    def test_reinsert_after_done(self, registry):
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        first = registry.upsert(issue_number=20, title="Issue 20", sweep_date=today, success_criteria="Test completion.")
        registry.set_status_direct(first.id, "done")
        # New sweep should create a fresh proposed record
        second = registry.upsert(issue_number=20, title="Issue 20", sweep_date=tomorrow, success_criteria="Test completion.")
        assert isinstance(second, UpsertInserted)

    def test_reinsert_after_failed(self, registry):
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        first = registry.upsert(issue_number=21, title="Issue 21", sweep_date=today, success_criteria="Test completion.")
        registry.set_status_direct(first.id, "failed")
        second = registry.upsert(issue_number=21, title="Issue 21", sweep_date=tomorrow, success_criteria="Test completion.")
        assert isinstance(second, UpsertInserted)

    def test_reinsert_after_expired(self, registry):
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        first = registry.upsert(issue_number=22, title="Issue 22", sweep_date=today, success_criteria="Test completion.")
        registry.set_status_direct(first.id, "expired")
        second = registry.upsert(issue_number=22, title="Issue 22", sweep_date=tomorrow, success_criteria="Test completion.")
        assert isinstance(second, UpsertInserted)

    def test_unique_conflict_does_not_overwrite_non_proposed(self, registry, db_path):
        """Same issue_number + sweep_date conflict must not overwrite pending/active."""
        today = datetime.now(timezone.utc).date().isoformat()
        first = registry.upsert(issue_number=30, title="Issue 30", sweep_date=today, success_criteria="Test completion.")
        # Transition to pending
        conn = _open_db(db_path)
        conn.execute("UPDATE uow_registry SET status='pending' WHERE id=?", (first.id,))
        conn.commit()
        conn.close()
        # Same sweep_date conflict — should leave status as pending
        registry.upsert(issue_number=30, title="Issue 30 updated", sweep_date=today, success_criteria="Test completion.")
        conn2 = _open_db(db_path)
        row = conn2.execute("SELECT status FROM uow_registry WHERE id=?", (first.id,)).fetchone()
        conn2.close()
        assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# Issue #488: success_criteria enforcement and issue_url population
# ---------------------------------------------------------------------------

class TestSuccessCriteriaEnforcement:
    """upsert() must reject empty success_criteria (issue #488)."""

    def test_empty_string_raises_value_error(self, registry):
        """Calling upsert() with success_criteria='' raises ValueError."""
        today = datetime.now(timezone.utc).date().isoformat()
        with pytest.raises(ValueError, match="success_criteria must not be empty"):
            registry.upsert(
                issue_number=8001,
                title="Missing criteria",
                sweep_date=today,
                success_criteria="",
            )

    def test_whitespace_only_raises_value_error(self, registry):
        """Calling upsert() with success_criteria='   ' raises ValueError."""
        today = datetime.now(timezone.utc).date().isoformat()
        with pytest.raises(ValueError, match="success_criteria must not be empty"):
            registry.upsert(
                issue_number=8002,
                title="Whitespace criteria",
                sweep_date=today,
                success_criteria="   ",
            )

    def test_non_empty_criteria_succeeds(self, registry, db_path):
        """upsert() with a non-empty success_criteria stores it correctly."""
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        criteria = "Migration applied, tests green, PR merged."
        result = registry.upsert(
            issue_number=8003,
            title="Valid criteria",
            sweep_date=today,
            success_criteria=criteria,
        )
        assert isinstance(result, UpsertInserted)
        conn = _open_db(db_path)
        row = conn.execute(
            "SELECT success_criteria FROM uow_registry WHERE id = ?", (result.id,)
        ).fetchone()
        conn.close()
        assert row["success_criteria"] == criteria


class TestIssueUrlPopulation:
    """issue_url is stored at proposal time (issue #488)."""

    def test_explicit_issue_url_stored(self, registry, db_path):
        """When issue_url is passed explicitly, it is persisted."""
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        url = "https://github.com/owner/repo/issues/42"
        result = registry.upsert(
            issue_number=42,
            title="Issue with explicit URL",
            sweep_date=today,
            success_criteria="Widget works.",
            issue_url=url,
        )
        assert isinstance(result, UpsertInserted)
        conn = _open_db(db_path)
        row = conn.execute(
            "SELECT issue_url FROM uow_registry WHERE id = ?", (result.id,)
        ).fetchone()
        conn.close()
        assert row["issue_url"] == url

    def test_issue_url_derived_from_source_repo(self, registry, db_path):
        """When source_repo is passed and issue_url is None, URL is derived."""
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(
            issue_number=99,
            title="Issue with derived URL",
            sweep_date=today,
            success_criteria="Done.",
            source_repo="myorg/myrepo",
        )
        assert isinstance(result, UpsertInserted)
        conn = _open_db(db_path)
        row = conn.execute(
            "SELECT issue_url FROM uow_registry WHERE id = ?", (result.id,)
        ).fetchone()
        conn.close()
        assert row["issue_url"] == "https://github.com/myorg/myrepo/issues/99"

    def test_no_source_repo_no_issue_url_stored_null(self, registry, db_path):
        """When neither source_repo nor issue_url is provided, issue_url is NULL."""
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(
            issue_number=100,
            title="No URL",
            sweep_date=today,
            success_criteria="Done.",
        )
        assert isinstance(result, UpsertInserted)
        conn = _open_db(db_path)
        row = conn.execute(
            "SELECT issue_url FROM uow_registry WHERE id = ?", (result.id,)
        ).fetchone()
        conn.close()
        assert row["issue_url"] is None

    def test_uow_value_object_carries_issue_url(self, registry):
        """registry.get() returns a UoW with issue_url populated."""
        from src.orchestration.registry import UpsertInserted
        today = datetime.now(timezone.utc).date().isoformat()
        url = "https://github.com/dcetlin/Lobster/issues/488"
        result = registry.upsert(
            issue_number=488,
            title="Schema gaps",
            sweep_date=today,
            success_criteria="Migration applied.",
            issue_url=url,
        )
        assert isinstance(result, UpsertInserted)
        uow = registry.get(result.id)
        assert uow is not None
        assert uow.issue_url == url


class TestRepoFromIssueUrl:
    """_repo_from_issue_url pure function (issue #488)."""

    def test_extracts_owner_repo_from_valid_url(self):
        from src.orchestration.steward import _repo_from_issue_url
        assert _repo_from_issue_url(
            "https://github.com/dcetlin/Lobster/issues/42"
        ) == "dcetlin/Lobster"

    def test_returns_none_for_none_input(self):
        from src.orchestration.steward import _repo_from_issue_url
        assert _repo_from_issue_url(None) is None

    def test_returns_none_for_non_github_url(self):
        from src.orchestration.steward import _repo_from_issue_url
        assert _repo_from_issue_url("https://gitlab.com/org/repo/issues/1") is None

    def test_returns_none_for_empty_string(self):
        from src.orchestration.steward import _repo_from_issue_url
        assert _repo_from_issue_url("") is None


# ---------------------------------------------------------------------------
# Approve tests (replaces legacy "Confirm" tests)
# ---------------------------------------------------------------------------

class TestApprove:
    def test_approve_transitions_proposed_to_ready_for_steward(self, registry):
        """approve() now skips pending as a resting state — goes straight to ready-for-steward."""
        from src.orchestration.registry import ApproveConfirmed
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=50, title="Issue 50", sweep_date=today, success_criteria="Test completion.")
        uow_id = result.id
        approve_result = registry.approve(uow_id)
        assert isinstance(approve_result, ApproveConfirmed)
        assert approve_result.id == uow_id
        uow = registry.get(uow_id)
        assert uow.status.value == "ready-for-steward"

    def test_approve_writes_audit_entries_for_both_transitions(self, registry, db_path):
        """approve() writes two audit entries: proposed→pending and pending→ready-for-steward."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=51, title="Issue 51", sweep_date=today, success_criteria="Test completion.")
        uow_id = result.id
        registry.approve(uow_id)
        conn = _open_db(db_path)
        pending_entry = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? AND event = 'status_change' AND to_status = 'pending'",
            (uow_id,)
        ).fetchone()
        ready_entry = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? AND event = 'status_change' AND to_status = 'ready-for-steward'",
            (uow_id,)
        ).fetchone()
        conn.close()
        assert pending_entry is not None, "audit entry for proposed→pending must exist"
        assert ready_entry is not None, "audit entry for pending→ready-for-steward must exist"

    def test_approve_idempotent_on_already_ready_for_steward(self, registry):
        """After approve, second approve returns ApproveSkipped with current_status=ready-for-steward."""
        from src.orchestration.registry import ApproveSkipped
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=52, title="Issue 52", sweep_date=today, success_criteria="Test completion.")
        uow_id = result.id
        registry.approve(uow_id)
        # Second approve returns ApproveSkipped, not an error
        approve_result = registry.approve(uow_id)
        assert isinstance(approve_result, ApproveSkipped)
        assert approve_result.current_status == "ready-for-steward"
        assert "already" in approve_result.reason.lower()

    def test_approve_returns_not_found_on_nonexistent(self, registry):
        from src.orchestration.registry import ApproveNotFound
        result = registry.approve("nonexistent-id")
        assert isinstance(result, ApproveNotFound)

    def test_approve_returns_expired_on_expired(self, registry):
        from src.orchestration.registry import ApproveExpired
        today = datetime.now(timezone.utc).date().isoformat()
        result = registry.upsert(issue_number=53, title="Issue 53", sweep_date=today, success_criteria="Test completion.")
        uow_id = result.id
        registry.set_status_direct(uow_id, "expired")
        approve_result = registry.approve(uow_id)
        assert isinstance(approve_result, ApproveExpired)


# ---------------------------------------------------------------------------
# List tests
# ---------------------------------------------------------------------------

class TestList:
    def test_list_by_status(self, registry):
        """approve() lands on ready-for-steward, not pending (pending is no longer a resting state)."""
        from src.orchestration.registry import UoW
        today = datetime.now(timezone.utc).date().isoformat()
        r1 = registry.upsert(issue_number=60, title="Issue 60", sweep_date=today, success_criteria="Test completion.")
        r2 = registry.upsert(issue_number=61, title="Issue 61", sweep_date=today, success_criteria="Test completion.")
        r3 = registry.upsert(issue_number=62, title="Issue 62", sweep_date=today, success_criteria="Test completion.")
        registry.approve(r2.id)
        proposed = registry.list(status="proposed")
        ready_for_steward = registry.list(status="ready-for-steward")
        pending = registry.list(status="pending")
        assert len(proposed) == 2
        assert len(ready_for_steward) == 1
        assert len(pending) == 0  # pending is never a resting state after approve()
        assert all(isinstance(u, UoW) for u in proposed)
        assert ready_for_steward[0].id == r2.id

    def test_list_returns_all_when_no_filter(self, registry):
        today = datetime.now(timezone.utc).date().isoformat()
        registry.upsert(issue_number=70, title="Issue 70", sweep_date=today, success_criteria="Test completion.")
        registry.upsert(issue_number=71, title="Issue 71", sweep_date=today, success_criteria="Test completion.")
        all_records = registry.list()
        assert len(all_records) >= 2

    def test_list_returns_empty_list_for_nonexistent_status(self, registry):
        result = registry.list(status="active")
        assert result == []


# ---------------------------------------------------------------------------
# Get tests
# ---------------------------------------------------------------------------

class TestGet:
    def test_get_existing_record(self, registry):
        from src.orchestration.registry import UoW
        today = datetime.now(timezone.utc).date().isoformat()
        inserted = registry.upsert(issue_number=80, title="Issue 80", sweep_date=today, success_criteria="Test completion.")
        got = registry.get(inserted.id)
        assert isinstance(got, UoW)
        assert got.id == inserted.id
        assert got.source_issue_number == 80

    def test_get_nonexistent_returns_none(self, registry):
        result = registry.get("does-not-exist")
        assert result is None


# ---------------------------------------------------------------------------
# Expire proposals tests
# ---------------------------------------------------------------------------

class TestExpireProposals:
    def test_expires_old_proposed_records(self, registry, db_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=15)).date().isoformat()
        recent_date = datetime.now(timezone.utc).date().isoformat()
        old = registry.upsert(issue_number=90, title="Old issue", sweep_date=old_date, success_criteria="Test completion.")
        recent = registry.upsert(issue_number=91, title="Recent issue", sweep_date=recent_date, success_criteria="Test completion.")
        # Manually backdate the old record's created_at
        conn = _open_db(db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        conn.execute("UPDATE uow_registry SET created_at = ? WHERE id = ?", (old_ts, old.id))
        conn.commit()
        conn.close()
        result = registry.expire_proposals()
        assert result["expired_count"] >= 1
        assert old.id in result["ids"]
        assert recent.id not in result["ids"]

    def test_does_not_expire_non_proposed(self, registry, db_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=15)).date().isoformat()
        r = registry.upsert(issue_number=92, title="Active old", sweep_date=old_date, success_criteria="Test completion.")
        # Backdate and set to active
        conn = _open_db(db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        conn.execute(
            "UPDATE uow_registry SET created_at = ?, status = 'active' WHERE id = ?",
            (old_ts, r.id)
        )
        conn.commit()
        conn.close()
        result = registry.expire_proposals()
        assert r.id not in result["ids"]

    def test_expire_writes_audit_entries(self, registry, db_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=15)).date().isoformat()
        r = registry.upsert(issue_number=93, title="Expiring", sweep_date=old_date, success_criteria="Test completion.")
        conn = _open_db(db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        conn.execute("UPDATE uow_registry SET created_at = ? WHERE id = ?", (old_ts, r.id))
        conn.commit()
        conn.close()
        registry.expire_proposals()
        conn2 = _open_db(db_path)
        audit = conn2.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? AND event = 'expired'", (r.id,)
        ).fetchone()
        conn2.close()
        assert audit is not None


# ---------------------------------------------------------------------------
# Audit log integrity
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_audit_log_is_append_only(self, registry, db_path):
        """Approve entries cannot be deleted (test that entries accumulate)."""
        today = datetime.now(timezone.utc).date().isoformat()
        r = registry.upsert(issue_number=100, title="Issue 100", sweep_date=today, success_criteria="Test completion.")
        registry.approve(r.id)
        conn = _open_db(db_path)
        count = conn.execute("SELECT COUNT(*) as c FROM audit_log WHERE uow_id = ?", (r.id,)).fetchone()["c"]
        conn.close()
        assert count >= 2  # at minimum: created + status_change

    def test_all_fields_present_in_audit_entry(self, registry, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        r = registry.upsert(issue_number=101, title="Audit fields test", sweep_date=today, success_criteria="Test completion.")
        conn = _open_db(db_path)
        entry = conn.execute("SELECT * FROM audit_log WHERE uow_id = ?", (r.id,)).fetchone()
        conn.close()
        assert entry["ts"] is not None
        assert entry["uow_id"] == r.id
        assert entry["event"] == "created"


# ---------------------------------------------------------------------------
# Check-stale tests (unit — mock the gh subprocess call)
# ---------------------------------------------------------------------------

class TestCheckStale:
    def test_check_stale_returns_empty_when_no_active(self, registry):
        result = registry.check_stale(issue_checker=lambda n: False)
        assert result == []

    def test_check_stale_returns_uow_when_issue_closed(self, registry, db_path):
        from src.orchestration.registry import UoW
        today = datetime.now(timezone.utc).date().isoformat()
        r = registry.upsert(issue_number=110, title="Issue 110", sweep_date=today, success_criteria="Test completion.")
        conn = _open_db(db_path)
        conn.execute("UPDATE uow_registry SET status='active' WHERE id=?", (r.id,))
        conn.commit()
        conn.close()
        # issue_checker returns True means "issue is closed"
        result = registry.check_stale(issue_checker=lambda n: n == 110)
        assert len(result) == 1
        assert isinstance(result[0], UoW)
        assert result[0].id == r.id

    def test_check_stale_excludes_open_issues(self, registry, db_path):
        today = datetime.now(timezone.utc).date().isoformat()
        r = registry.upsert(issue_number=111, title="Issue 111", sweep_date=today, success_criteria="Test completion.")
        conn = _open_db(db_path)
        conn.execute("UPDATE uow_registry SET status='active' WHERE id=?", (r.id,))
        conn.commit()
        conn.close()
        # issue is NOT closed
        result = registry.check_stale(issue_checker=lambda n: False)
        assert result == []


# ---------------------------------------------------------------------------
# Gate readiness metric (now via registry_health())
# ---------------------------------------------------------------------------

class TestRegistryHealth:
    def test_gate_met_regardless_of_days_running(self, registry):
        from src.orchestration.registry import GateStatus
        # Phase 1 declared complete — 14-day calendar gate removed.
        # gate_met is True even with a fresh (0-day-old) registry.
        readiness = registry.registry_health()
        assert isinstance(readiness, GateStatus)
        assert readiness.gate_met is True

    def test_registry_health_fields_present(self, registry):
        from src.orchestration.registry import GateStatus
        readiness = registry.registry_health()
        assert isinstance(readiness, GateStatus)
        assert hasattr(readiness, "gate_met")
        assert hasattr(readiness, "days_running")
        assert hasattr(readiness, "approval_rate")
        assert hasattr(readiness, "reason")

    def test_registry_health_no_records(self, registry):
        from src.orchestration.registry import GateStatus
        # Empty registry should still report gate_met True (phase 1 complete).
        readiness = registry.registry_health()
        assert isinstance(readiness, GateStatus)
        assert readiness.gate_met is True
        assert readiness.days_running == 0


# ---------------------------------------------------------------------------
# Item 16: Registry.complete_uow and Registry.fail_uow public API
# ---------------------------------------------------------------------------

class TestRegistryCompleteUow:
    def _make_active_uow(self, registry, db_path: Path) -> str:
        """Helper: create a UoW in 'active' status for transition tests."""
        from src.orchestration.registry import UpsertInserted
        result = registry.upsert(issue_number=9001, title="Test active UoW", success_criteria="Test completion.")
        assert isinstance(result, UpsertInserted)
        uow_id = result.id
        registry.approve(uow_id)
        # Manually advance to 'active' (bypassing executor claim sequence)
        registry.set_status_direct(uow_id, "active")
        return uow_id

    def test_complete_uow_transitions_to_ready_for_steward(self, registry, db_path):
        uow_id = self._make_active_uow(registry, db_path)
        registry.complete_uow(uow_id, "/tmp/output.json")
        uow = registry.get(uow_id)
        assert uow is not None
        assert str(uow.status) == "ready-for-steward"

    def test_complete_uow_writes_audit_entry(self, registry, db_path):
        uow_id = self._make_active_uow(registry, db_path)
        registry.complete_uow(uow_id, "/tmp/output.json")
        conn = _open_db(db_path)
        row = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? AND event = 'execution_complete'",
            (uow_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["from_status"] == "active"
        assert row["to_status"] == "ready-for-steward"
        assert row["agent"] == "executor"

    def test_fail_uow_transitions_to_failed(self, registry, db_path):
        uow_id = self._make_active_uow(registry, db_path)
        registry.fail_uow(uow_id, "test failure reason")
        uow = registry.get(uow_id)
        assert uow is not None
        assert str(uow.status) == "failed"

    def test_fail_uow_writes_audit_entry(self, registry, db_path):
        uow_id = self._make_active_uow(registry, db_path)
        registry.fail_uow(uow_id, "disk full")
        conn = _open_db(db_path)
        row = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? AND event = 'execution_failed'",
            (uow_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["from_status"] == "active"
        assert row["to_status"] == "failed"
        note_data = json.loads(row["note"])
        assert note_data["reason"] == "disk full"


# ---------------------------------------------------------------------------
# Item 18: NoteAccessor
# ---------------------------------------------------------------------------

class TestNoteAccessor:
    def _make_uow_id(self, registry) -> str:
        from src.orchestration.registry import UpsertInserted
        result = registry.upsert(issue_number=5555, title="NoteAccessor test UoW", success_criteria="Test completion.")
        assert isinstance(result, UpsertInserted)
        return result.id

    def test_get_returns_none_for_missing_key(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        assert notes.get(uow_id, "nonexistent") is None

    def test_get_returns_none_for_missing_uow(self, registry):
        from src.orchestration.registry import NoteAccessor
        notes = NoteAccessor(registry)
        assert notes.get("uow_does_not_exist", "key") is None

    def test_set_and_get_roundtrip(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        notes.set(uow_id, "deploy_tag", "v1.2.3")
        assert notes.get(uow_id, "deploy_tag") == "v1.2.3"

    def test_set_overwrites_existing_key(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        notes.set(uow_id, "status", "pending")
        notes.set(uow_id, "status", "done")
        assert notes.get(uow_id, "status") == "done"

    def test_set_does_not_affect_other_keys(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        notes.set(uow_id, "key_a", "value_a")
        notes.set(uow_id, "key_b", "value_b")
        assert notes.get(uow_id, "key_a") == "value_a"
        assert notes.get(uow_id, "key_b") == "value_b"

    def test_set_rejects_dotted_key(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        with pytest.raises(ValueError, match="nested path"):
            notes.set(uow_id, "parent.child", "value")

    def test_get_rejects_dotted_key(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        with pytest.raises(ValueError, match="nested path"):
            notes.get(uow_id, "a.b")

    def test_append_log_creates_list(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        notes.append_log(uow_id, "step 1 complete")
        log = notes.get(uow_id, "log")
        assert log == ["step 1 complete"]

    def test_append_log_accumulates_entries(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        notes.append_log(uow_id, "entry one")
        notes.append_log(uow_id, "entry two")
        notes.append_log(uow_id, "entry three")
        log = notes.get(uow_id, "log")
        assert log == ["entry one", "entry two", "entry three"]

    def test_set_stores_non_string_values(self, registry):
        from src.orchestration.registry import NoteAccessor
        uow_id = self._make_uow_id(registry)
        notes = NoteAccessor(registry)
        notes.set(uow_id, "count", 42)
        notes.set(uow_id, "flag", True)
        notes.set(uow_id, "data", {"nested": "dict"})
        assert notes.get(uow_id, "count") == 42
        assert notes.get(uow_id, "flag") is True
        assert notes.get(uow_id, "data") == {"nested": "dict"}

    def test_set_on_missing_uow_is_noop(self, registry):
        from src.orchestration.registry import NoteAccessor
        notes = NoteAccessor(registry)
        # Should not raise — silently no-ops when UoW doesn't exist
        notes.set("nonexistent_id", "key", "value")
