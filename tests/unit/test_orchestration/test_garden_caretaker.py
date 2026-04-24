"""Unit tests for GardenCaretaker — scan() and tend() with full reconciliation.

Test strategy:
- In-memory SQLite via real Registry (real migrations applied)
- IssueSource stubbed with unittest.mock — no subprocess, no gh CLI
- All tests are isolated (tmp_path fixture for DB)
- Reconciliation table covered cell-by-cell
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirror pattern from test_registry.py
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from src.orchestration.registry import Registry, UoWStatus
from src.orchestration.issue_source import IssueSnapshot
from src.orchestration.garden_caretaker import (
    GardenCaretaker,
    EXECUTING_STATES,
    _qualifies,
    _reconcile,
    _DEFAULT_CONFIG,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(days_ago: int = 0) -> str:
    """Return ISO 8601 timestamp offset by days_ago from now (UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


def _snapshot(
    source_ref: str = "github:issue/1",
    title: str = "Test issue",
    state: str = "open",
    labels: tuple[str, ...] = (),
    body: str = "Some body text",
    created_at: str | None = None,
    url: str = "https://github.com/example/repo/issues/1",
    updated_at: str | None = None,
) -> IssueSnapshot:
    return IssueSnapshot(
        source_ref=source_ref,
        title=title,
        state=state,
        labels=labels,
        body=body,
        created_at=created_at or _iso(0),
        updated_at=updated_at or _iso(0),
        url=url,
    )


def _make_source(
    scan_issues: list[IssueSnapshot] | None = None,
    issue_map: dict[str, IssueSnapshot | None] | None = None,
) -> MagicMock:
    """Build a mock IssueSource.

    scan() yields from scan_issues.
    get_issue(source_ref) looks up issue_map (returns None if not found).
    """
    source = MagicMock()
    source.scan.return_value = iter(scan_issues or [])
    if issue_map is not None:
        source.get_issue.side_effect = lambda ref: issue_map.get(ref)
    else:
        source.get_issue.return_value = None
    return source


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


@pytest.fixture
def caretaker(registry: Registry) -> GardenCaretaker:
    source = _make_source()
    return GardenCaretaker(source=source, registry=registry, config={})


# ---------------------------------------------------------------------------
# Pure function tests — _qualifies
# ---------------------------------------------------------------------------

class TestQualifiesPredicate:
    def test_qualifying_label_qualifies(self) -> None:
        snap = _snapshot(labels=("bug",), body="body", created_at=_iso(0))
        assert _qualifies(snap, _DEFAULT_CONFIG) is True

    def test_high_priority_label_qualifies(self) -> None:
        snap = _snapshot(labels=("high-priority",), body="body")
        assert _qualifies(snap, _DEFAULT_CONFIG) is True

    def test_ready_to_execute_label_qualifies(self) -> None:
        snap = _snapshot(labels=("ready-to-execute",), body="body")
        assert _qualifies(snap, _DEFAULT_CONFIG) is True

    def test_old_issue_without_qualifying_label_qualifies(self) -> None:
        snap = _snapshot(body="body", created_at=_iso(5))
        assert _qualifies(snap, _DEFAULT_CONFIG) is True

    def test_new_issue_without_qualifying_label_does_not_qualify(self) -> None:
        snap = _snapshot(body="body", created_at=_iso(0))
        assert _qualifies(snap, _DEFAULT_CONFIG) is False

    def test_blocking_label_prevents_qualification(self) -> None:
        snap = _snapshot(labels=("bug", "blocked"), body="body", created_at=_iso(5))
        assert _qualifies(snap, _DEFAULT_CONFIG) is False

    def test_missing_body_prevents_qualification(self) -> None:
        snap = _snapshot(labels=("bug",), body="")
        assert _qualifies(snap, _DEFAULT_CONFIG) is False

    def test_whitespace_only_body_prevents_qualification(self) -> None:
        snap = _snapshot(labels=("bug",), body="   \n  ")
        assert _qualifies(snap, _DEFAULT_CONFIG) is False


# ---------------------------------------------------------------------------
# Pure function tests — _reconcile
# ---------------------------------------------------------------------------

class TestReconcileDecisionTable:
    """Cell-by-cell coverage of the reconciliation decision table."""

    # source=open rows
    def test_open_proposed(self) -> None:
        assert _reconcile(UoWStatus.PROPOSED, _snapshot(state="open")) == "no_op"

    def test_open_pending(self) -> None:
        assert _reconcile(UoWStatus.PENDING, _snapshot(state="open")) == "no_op"

    def test_open_active(self) -> None:
        assert _reconcile(UoWStatus.ACTIVE, _snapshot(state="open")) == "no_op"

    def test_open_done(self) -> None:
        assert _reconcile(UoWStatus.DONE, _snapshot(state="open")) == "no_op"

    def test_open_expired_triggers_reactivate(self) -> None:
        # "reopened" — source open, UoW terminal → reactivate
        assert _reconcile(UoWStatus.EXPIRED, _snapshot(state="open")) == "reactivate"

    def test_open_failed_triggers_reactivate(self) -> None:
        assert _reconcile(UoWStatus.FAILED, _snapshot(state="open")) == "reactivate"

    # source=closed rows
    def test_closed_proposed_archives(self) -> None:
        assert _reconcile(UoWStatus.PROPOSED, _snapshot(state="closed")) == "archive"

    def test_closed_pending_archives(self) -> None:
        assert _reconcile(UoWStatus.PENDING, _snapshot(state="closed")) == "archive"

    # EXECUTING_STATES (active, ready-for-executor): no-op even when source closes
    # (issue #676: caretaker must not interrupt in-flight execution)
    def test_closed_active_is_noop_not_surface(self) -> None:
        """Source closure must not surface or archive a UoW that is actively executing."""
        assert _reconcile(UoWStatus.ACTIVE, _snapshot(state="closed")) == "no_op"

    def test_closed_ready_for_executor_is_noop_not_surface(self) -> None:
        """Source closure must not surface a UoW queued for execution."""
        assert _reconcile(UoWStatus.READY_FOR_EXECUTOR, _snapshot(state="closed")) == "no_op"

    def test_closed_ready_for_steward_surfaces(self) -> None:
        assert _reconcile(UoWStatus.READY_FOR_STEWARD, _snapshot(state="closed")) == "surface"

    def test_closed_done_noop(self) -> None:
        assert _reconcile(UoWStatus.DONE, _snapshot(state="closed")) == "no_op"

    def test_closed_expired_noop(self) -> None:
        assert _reconcile(UoWStatus.EXPIRED, _snapshot(state="closed")) == "no_op"

    # source=None (deleted/not_found) rows
    def test_deleted_proposed_archives(self) -> None:
        assert _reconcile(UoWStatus.PROPOSED, None) == "archive"

    def test_deleted_pending_archives(self) -> None:
        assert _reconcile(UoWStatus.PENDING, None) == "archive"

    def test_deleted_active_is_noop_not_surface(self) -> None:
        """Source deletion must not surface a UoW that is actively executing (issue #676)."""
        assert _reconcile(UoWStatus.ACTIVE, None) == "no_op"

    def test_deleted_ready_for_executor_is_noop(self) -> None:
        """Source deletion must not archive/surface a UoW queued for execution."""
        assert _reconcile(UoWStatus.READY_FOR_EXECUTOR, None) == "no_op"

    def test_deleted_done_noop(self) -> None:
        assert _reconcile(UoWStatus.DONE, None) == "no_op"

    def test_deleted_expired_archives(self) -> None:
        assert _reconcile(UoWStatus.EXPIRED, None) == "archive"

    def test_deleted_failed_archives(self) -> None:
        assert _reconcile(UoWStatus.FAILED, None) == "archive"

    # unknown/error state
    def test_unknown_state_warns(self) -> None:
        assert _reconcile(UoWStatus.PROPOSED, _snapshot(state="unknown")) == "warn"

    def test_error_state_warns(self) -> None:
        assert _reconcile(UoWStatus.ACTIVE, _snapshot(state="error")) == "warn"

    # EXECUTING_STATES set membership — all members must be no_op on close/delete
    def test_executing_states_are_all_noop_on_closed(self) -> None:
        """Every status in EXECUTING_STATES must yield no_op when source closes (issue #676)."""
        for status in EXECUTING_STATES:
            result = _reconcile(status, _snapshot(state="closed"))
            assert result == "no_op", (
                f"Expected no_op for EXECUTING_STATES status {status!r} on source close, "
                f"got {result!r}. Add it to EXECUTING_STATES or fix _reconcile_closed."
            )

    def test_executing_states_are_all_noop_on_deleted(self) -> None:
        """Every status in EXECUTING_STATES must yield no_op when source is deleted (issue #676)."""
        for status in EXECUTING_STATES:
            result = _reconcile(status, None)
            assert result == "no_op", (
                f"Expected no_op for EXECUTING_STATES status {status!r} on source deletion, "
                f"got {result!r}. Add it to EXECUTING_STATES or fix _reconcile_deleted."
            )


# ---------------------------------------------------------------------------
# scan() integration tests
# ---------------------------------------------------------------------------

class TestScan:
    def test_scan_seeds_new_uow(self, registry: Registry) -> None:
        snap = _snapshot(source_ref="github:issue/10")
        source = _make_source(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 1
        proposed = registry.query(status="proposed")
        assert len(proposed) == 1
        assert proposed[0].source_issue_number == 10

    def test_scan_skips_issue_already_in_registry(self, registry: Registry) -> None:
        # Pre-seed the registry so the issue already has a non-terminal UoW
        registry.upsert(issue_number=10, title="Existing", success_criteria="Test completion.")

        snap = _snapshot(source_ref="github:issue/10")
        source = _make_source(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 0
        # Only one UoW should exist
        assert len(registry.query(status="proposed")) == 1

    def test_scan_skips_meta_labelled_issues(self, registry: Registry) -> None:
        snap = _snapshot(source_ref="github:issue/99", labels=("wos-phase-2",))
        source = _make_source(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 0
        assert registry.query(status="proposed") == []

    def test_scan_qualifies_uow_when_criteria_met(self, registry: Registry) -> None:
        # Issue has qualifying label → should go directly to ready-for-steward
        snap = _snapshot(
            source_ref="github:issue/5",
            labels=("bug",),
            body="Non-empty body",
        )
        source = _make_source(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 1
        assert result["qualified"] == 1
        # Should be in ready-for-steward, not proposed
        assert registry.query(status="proposed") == []
        ready = registry.query(status="ready-for-steward")
        assert len(ready) == 1

    def test_scan_does_not_qualify_uow_when_criteria_not_met(self, registry: Registry) -> None:
        snap = _snapshot(
            source_ref="github:issue/7",
            labels=(),
            body="Some body",
            created_at=_iso(0),  # brand new — doesn't qualify by age
        )
        source = _make_source(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 1
        assert result["qualified"] == 0
        assert len(registry.query(status="proposed")) == 1

    def test_scan_seeds_multiple_issues(self, registry: Registry) -> None:
        snaps = [
            _snapshot(source_ref="github:issue/1"),
            _snapshot(source_ref="github:issue/2"),
            _snapshot(source_ref="github:issue/3"),
        ]
        source = _make_source(scan_issues=snaps)
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 3

    def test_scan_handles_empty_source(self, registry: Registry) -> None:
        source = _make_source(scan_issues=[])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result == {"seeded": 0, "qualified": 0}


# ---------------------------------------------------------------------------
# tend() integration tests
# ---------------------------------------------------------------------------

class TestTend:
    def test_tend_noop_when_source_open(self, registry: Registry) -> None:
        # Seed a proposed UoW
        upsert = registry.upsert(issue_number=42, title="Open issue", success_criteria="Test completion.")
        assert upsert

        snap = _snapshot(source_ref="github:issue/42", state="open")
        source = _make_source(issue_map={"github:issue/42": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["no_change"] == 1
        assert result["archived"] == 0
        # UoW should still be proposed
        proposed = registry.query(status="proposed")
        assert len(proposed) == 1

    def test_tend_archives_proposed_uow_when_source_closed(self, registry: Registry) -> None:
        registry.upsert(issue_number=10, title="Will close", success_criteria="Test completion.")

        snap = _snapshot(source_ref="github:issue/10", state="closed")
        source = _make_source(issue_map={"github:issue/10": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["archived"] == 1
        assert result["surfaced_to_steward"] == 0
        # Proposed UoW should be gone
        assert registry.query(status="proposed") == []
        # Should be in expired
        expired = registry.query(status="expired")
        assert len(expired) == 1

    def test_tend_archives_pending_uow_when_source_closed(self, registry: Registry) -> None:
        """pending is no longer a resting state — approve() lands on ready-for-steward.
        A UoW that has been approved (ready-for-steward) and whose source closes is
        surfaced to the Steward (not archived) because in-flight work is involved.
        """
        upsert_result = registry.upsert(issue_number=11, title="Pending issue", success_criteria="Test completion.")
        # approve() now lands on ready-for-steward, not pending
        registry.approve(upsert_result.id)
        uow = registry.get(upsert_result.id)
        assert uow.status.value == "ready-for-steward"

        snap = _snapshot(source_ref="github:issue/11", state="closed")
        source = _make_source(issue_map={"github:issue/11": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        # ready-for-steward + source closed → surface (not archive), per reconciliation table.
        # The UoW remains in ready-for-steward (the surface action writes an audit entry and
        # keeps the status; the Steward will close it on its next cycle).
        assert result["surfaced_to_steward"] == 1
        assert result["archived"] == 0
        uow_after = registry.get(upsert_result.id)
        assert uow_after.status.value == "ready-for-steward"

    def test_tend_archives_pending_uow_set_directly_when_source_closed(self, registry: Registry) -> None:
        """Legacy: a UoW manually set to pending (pre-auto-advance) is archived when source closes."""
        upsert_result = registry.upsert(issue_number=111, title="Legacy pending issue", success_criteria="Test completion.")
        # Bypass approve() to simulate a legacy pending UoW
        registry.set_status_direct(upsert_result.id, "pending")

        snap = _snapshot(source_ref="github:issue/111", state="closed")
        source = _make_source(issue_map={"github:issue/111": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["archived"] == 1
        assert registry.query(status="pending") == []

    def test_tend_noop_when_source_closed_and_active(
        self, registry: Registry
    ) -> None:
        """UoW in 'active' status must not be disturbed when source closes (issue #676).

        Closing the source while a subagent is actively executing must not interrupt
        in-flight work. The UoW must remain in 'active' and be counted as no_change.
        """
        upsert_result = registry.upsert(issue_number=20, title="Active issue", success_criteria="Test completion.")
        # Manually set to active (bypassing normal flow for test setup)
        registry.set_status_direct(upsert_result.id, "active")

        snap = _snapshot(source_ref="github:issue/20", state="closed")
        source = _make_source(issue_map={"github:issue/20": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        # active + source closed → no_op (EXECUTING_STATES protection)
        assert result["no_change"] == 1
        assert result["surfaced_to_steward"] == 0
        assert result["archived"] == 0
        # UoW must remain in 'active' — caretaker must not touch it
        active = registry.query(status="active")
        assert len(active) == 1
        assert active[0].id == upsert_result.id

    def test_tend_noop_when_source_deleted_and_active(
        self, registry: Registry
    ) -> None:
        """UoW in 'active' status must not be disturbed when source is deleted (issue #676).

        Deleting the source issue must not interrupt an actively executing UoW.
        The UoW must remain in 'active' — counted as no_change.
        """
        upsert_result = registry.upsert(issue_number=30, title="Active deleted", success_criteria="Test completion.")
        registry.set_status_direct(upsert_result.id, "active")

        # source.get_issue returns None → deleted
        source = _make_source(issue_map={"github:issue/30": None})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        # active + source deleted → no_op (EXECUTING_STATES protection)
        assert result["no_change"] == 1
        assert result["surfaced_to_steward"] == 0
        active = registry.query(status="active")
        assert len(active) == 1
        assert active[0].id == upsert_result.id

    def test_tend_reactivates_archived_uow_when_source_reopens(
        self, registry: Registry
    ) -> None:
        upsert_result = registry.upsert(issue_number=50, title="Was expired", success_criteria="Test completion.")
        # Simulate archived (expired) UoW
        registry.set_status_direct(upsert_result.id, "expired")

        # Source is now open → "reopened"
        snap = _snapshot(source_ref="github:issue/50", state="open")
        source = _make_source(issue_map={"github:issue/50": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["reactivated"] == 1
        # Should be back in proposed
        proposed = registry.query(status="proposed")
        assert len(proposed) == 1
        assert proposed[0].id == upsert_result.id

    def test_tend_noop_for_done_uow_with_closed_source(self, registry: Registry) -> None:
        upsert_result = registry.upsert(issue_number=60, title="Done issue", success_criteria="Test completion.")
        registry.set_status_direct(upsert_result.id, "done")

        # tend() does not fetch done UoWs — they are excluded from _fetch_active_uows
        snap = _snapshot(source_ref="github:issue/60", state="closed")
        source = _make_source(issue_map={"github:issue/60": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        # Done UoW should not be touched — get_issue should not be called
        source.get_issue.assert_not_called()
        assert registry.query(status="done")[0].id == upsert_result.id

    def test_tend_handles_source_error_gracefully(self, registry: Registry) -> None:
        registry.upsert(issue_number=70, title="Error case", success_criteria="Test completion.")

        source = MagicMock()
        source.get_issue.side_effect = Exception("network error")
        caretaker = GardenCaretaker(source=source, registry=registry)

        # Should not raise — error is logged and counted as no_change
        result = caretaker.tend()

        assert result["no_change"] == 1
        assert result["archived"] == 0

    def test_tend_audit_log_written_on_archive(self, registry: Registry) -> None:
        import sqlite3
        upsert_result = registry.upsert(issue_number=80, title="Audit check", success_criteria="Test completion.")

        snap = _snapshot(source_ref="github:issue/80", state="closed")
        source = _make_source(issue_map={"github:issue/80": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.tend()

        conn = sqlite3.connect(str(registry.db_path))
        conn.row_factory = sqlite3.Row
        entries = conn.execute(
            "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id",
            (upsert_result.id,),
        ).fetchall()
        conn.close()

        events = [r["event"] for r in entries]
        assert "archived_by_caretaker" in events

    def test_tend_audit_log_written_on_surface(self, registry: Registry) -> None:
        """Audit log records surface event for diagnosing UoW whose source closes."""
        import sqlite3
        upsert_result = registry.upsert(issue_number=90, title="Surface audit", success_criteria="Test completion.")
        # Use 'diagnosing' — still in _SURFACE_ON_CLOSE_STATES (not EXECUTING_STATES).
        # 'active' is now protected (EXECUTING_STATES) and yields no_op on close.
        registry.set_status_direct(upsert_result.id, "diagnosing")

        snap = _snapshot(source_ref="github:issue/90", state="closed")
        source = _make_source(issue_map={"github:issue/90": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.tend()

        conn = sqlite3.connect(str(registry.db_path))
        conn.row_factory = sqlite3.Row
        entries = conn.execute(
            "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id",
            (upsert_result.id,),
        ).fetchall()
        conn.close()

        events = [r["event"] for r in entries]
        assert "surfaced_to_steward" in events

    def test_tend_archives_proposed_uow_when_source_deleted(
        self, registry: Registry
    ) -> None:
        registry.upsert(issue_number=100, title="Deleted source", success_criteria="Test completion.")

        source = _make_source(issue_map={"github:issue/100": None})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["archived"] == 1
        assert registry.query(status="expired")


# ---------------------------------------------------------------------------
# run() integration test
# ---------------------------------------------------------------------------

class TestRun:
    def test_run_returns_merged_summary(self, registry: Registry) -> None:
        snap = _snapshot(source_ref="github:issue/200", state="open")
        source = _make_source(
            scan_issues=[snap],
            issue_map={"github:issue/200": snap},
        )
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.run()

        assert "seeded" in result
        assert "qualified" in result
        assert "requalified" in result
        assert "archived" in result
        assert "surfaced_to_steward" in result
        assert "reactivated" in result
        assert "no_change" in result


# ---------------------------------------------------------------------------
# requalify_proposed() integration tests
# ---------------------------------------------------------------------------

class TestRequalifyProposed:
    """Proposed UoWs that now pass qualification criteria are auto-advanced.

    This covers the gap where UoWs reset to 'proposed' (e.g. after source
    reopens) would stay stuck there indefinitely under the old weekly-scan model.
    """

    def test_proposed_uow_with_qualifying_label_is_auto_advanced(
        self, registry: Registry
    ) -> None:
        """A proposed UoW whose source now carries a qualifying label advances to ready-for-steward."""
        upsert_result = registry.upsert(
            issue_number=300, title="Now has bug label", success_criteria="Fix the bug."
        )
        snap = _snapshot(
            source_ref="github:issue/300",
            labels=("bug",),
            body="Non-empty body",
            state="open",
        )
        source = _make_source(issue_map={"github:issue/300": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.requalify_proposed()

        assert result["requalified"] == 1
        assert registry.query(status="proposed") == []
        ready = registry.query(status="ready-for-steward")
        assert len(ready) == 1
        assert ready[0].id == upsert_result.id

    def test_proposed_uow_aged_without_label_is_auto_advanced(
        self, registry: Registry
    ) -> None:
        """Age-based promotion: a UoW open ≥3 days with no blocking labels advances without a qualifying label.

        This is the fallback path that unblocks UoWs accumulating without label intervention
        (issue #907). Matches scan-time qualification behavior where age alone is sufficient.
        Blocking labels still suppress promotion at any age.
        """
        upsert_result = registry.upsert(
            issue_number=301, title="Old issue", success_criteria="Do something."
        )
        snap = _snapshot(
            source_ref="github:issue/301",
            labels=(),
            body="Some body text",
            state="open",
            created_at=_iso(5),  # 5 days old — past the 3-day threshold, no blocking label
        )
        source = _make_source(issue_map={"github:issue/301": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.requalify_proposed()

        # Age-based promotion: UoW advanced without any qualifying label
        assert result["requalified"] == 1
        assert registry.query(status="proposed") == []
        ready = registry.query(status="ready-for-steward")
        assert len(ready) == 1
        assert ready[0].id == upsert_result.id

    def test_proposed_uow_with_blocking_label_is_not_advanced(
        self, registry: Registry
    ) -> None:
        """A proposed UoW with a blocking label must not be auto-advanced regardless of age."""
        registry.upsert(
            issue_number=302, title="Blocked issue", success_criteria="Waiting."
        )
        snap = _snapshot(
            source_ref="github:issue/302",
            labels=("needs-design",),  # blocking label
            body="Some body text",
            state="open",
            created_at=_iso(10),  # old enough to qualify by age, but blocked
        )
        source = _make_source(issue_map={"github:issue/302": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.requalify_proposed()

        assert result["requalified"] == 0
        assert len(registry.query(status="proposed")) == 1

    def test_proposed_uow_too_new_without_qualifying_label_is_not_advanced(
        self, registry: Registry
    ) -> None:
        """A brand-new proposed UoW without qualifying labels remains proposed."""
        registry.upsert(
            issue_number=303, title="New issue", success_criteria="TBD."
        )
        snap = _snapshot(
            source_ref="github:issue/303",
            labels=(),
            body="Some body text",
            state="open",
            created_at=_iso(0),  # just created — does not meet age threshold
        )
        source = _make_source(issue_map={"github:issue/303": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.requalify_proposed()

        assert result["requalified"] == 0
        assert len(registry.query(status="proposed")) == 1

    def test_proposed_uow_without_source_ref_is_skipped(
        self, registry: Registry, db_path: Path
    ) -> None:
        """A proposed UoW with no source_ref (empty string) cannot be requalified and is skipped.

        The 'source' column is always set by upsert() (derived from issue_number), so this
        guard catches legacy rows or direct DB writes where source was not set. We simulate
        that by writing source='' directly via sqlite3.
        """
        import sqlite3
        upsert_result = registry.upsert(
            issue_number=304, title="No source ref", success_criteria="Abstract task."
        )
        # Blank out the source column to simulate a legacy row
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET source = '' WHERE id = ?", (upsert_result.id,))
        conn.commit()
        conn.close()

        # Reload registry so it sees the blanked source
        from src.orchestration.registry import Registry as _R
        registry2 = _R(db_path)

        source = _make_source(issue_map={})
        caretaker = GardenCaretaker(source=source, registry=registry2)

        result = caretaker.requalify_proposed()

        assert result["requalified"] == 0
        source.get_issue.assert_not_called()

    def test_proposed_uow_with_deleted_source_is_skipped(
        self, registry: Registry
    ) -> None:
        """A proposed UoW whose source returns None is skipped — tend() handles the archive."""
        registry.upsert(
            issue_number=305, title="Deleted issue", success_criteria="Gone."
        )
        # source.get_issue returns None → deleted
        source = _make_source(issue_map={"github:issue/305": None})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.requalify_proposed()

        assert result["requalified"] == 0
        # Still proposed — tend() will archive it on next pass
        assert len(registry.query(status="proposed")) == 1

    def test_proposed_uow_source_error_is_skipped_gracefully(
        self, registry: Registry
    ) -> None:
        """A source fetch error does not raise — the UoW is skipped and remains proposed."""
        registry.upsert(
            issue_number=306, title="Network error case", success_criteria="Unreachable."
        )
        source = MagicMock()
        source.scan.return_value = iter([])
        source.get_issue.side_effect = Exception("network timeout")
        caretaker = GardenCaretaker(source=source, registry=registry)

        # Should not raise
        result = caretaker.requalify_proposed()

        assert result["requalified"] == 0
        assert len(registry.query(status="proposed")) == 1

    def test_requalify_writes_auto_qualified_audit_event(
        self, registry: Registry
    ) -> None:
        """Audit log records 'auto_qualified' event to distinguish from seed-time qualification."""
        import sqlite3
        upsert_result = registry.upsert(
            issue_number=307, title="Audit event check", success_criteria="Verify audit."
        )
        snap = _snapshot(
            source_ref="github:issue/307",
            labels=("bug",),
            body="Non-empty",
            state="open",
        )
        source = _make_source(issue_map={"github:issue/307": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.requalify_proposed()

        conn = sqlite3.connect(str(registry.db_path))
        conn.row_factory = sqlite3.Row
        entries = conn.execute(
            "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id",
            (upsert_result.id,),
        ).fetchall()
        conn.close()

        events = [r["event"] for r in entries]
        assert "auto_qualified" in events

    def test_requalify_does_not_advance_non_proposed_uows(
        self, registry: Registry
    ) -> None:
        """requalify_proposed() only operates on proposed UoWs — other states are untouched."""
        upsert_result = registry.upsert(
            issue_number=308, title="Already ready", success_criteria="Done already."
        )
        registry.set_status_direct(upsert_result.id, "ready-for-steward")

        snap = _snapshot(
            source_ref="github:issue/308",
            labels=("bug",),
            body="Non-empty",
            state="open",
        )
        source = _make_source(issue_map={"github:issue/308": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.requalify_proposed()

        assert result["requalified"] == 0
        # UoW remains in ready-for-steward — requalify_proposed should not touch it
        ready = registry.query(status="ready-for-steward")
        assert len(ready) == 1
        # get_issue should never be called (query only fetches proposed)
        source.get_issue.assert_not_called()

    def test_run_includes_requalified_count_in_summary(
        self, registry: Registry
    ) -> None:
        """run() merges requalify_proposed() output — 'requalified' key is present and correct."""
        upsert_result = registry.upsert(
            issue_number=309, title="Run integration", success_criteria="Check run output."
        )
        snap = _snapshot(
            source_ref="github:issue/309",
            labels=("bug",),
            body="Non-empty",
            state="open",
        )
        source = _make_source(
            scan_issues=[],  # no new seeding
            issue_map={"github:issue/309": snap},
        )
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.run()

        assert result["requalified"] == 1
        assert registry.query(status="proposed") == []
        assert len(registry.query(status="ready-for-steward")) == 1
