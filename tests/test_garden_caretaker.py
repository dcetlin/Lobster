"""Integration tests for GardenCaretaker — end-to-end scan/tend flows.

Test strategy
-------------
- Real Registry backed by a tmp_path SQLite DB with all migrations applied.
- IssueSource implemented as a concrete stub class (not MagicMock) to satisfy
  the Protocol structurally and make test intent explicit.
- Each test exercises a full GardenCaretaker.scan() or .tend() call and
  asserts on observable registry state (status, audit log entries, UoW fields).
- No GitHub API calls, no subprocess, no network.

Coverage
--------
- Happy path: new issue discovered → UoW proposed in registry
- Issue with qualifying label → UoW promoted to ready-for-steward at scan time
- Issue already in registry (non-terminal) → no duplicate created
- Issue closed → UoW archived (proposed/pending) or surfaced (in-flight)
- Issue state unchanged (open + proposed) → no action
- Source returns empty → no crash, zero seeded
- Source tracking: source_ref set on UoW created by upsert, audit entries carry
  correct source_ref, source_last_seen_at, and source_state annotations
- Malformed source_ref → scan skips gracefully
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Module path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.registry import Registry, UoWStatus, UpsertInserted
from orchestration.issue_source import IssueSnapshot
from orchestration.garden_caretaker import GardenCaretaker, _DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Concrete IssueSource stub — satisfies Protocol without MagicMock
# ---------------------------------------------------------------------------

class StubIssueSource:
    """In-memory IssueSource stub.

    Implements the IssueSource Protocol concretely so test intent is explicit
    and traceable. scan() yields from the provided list; get_issue() looks up
    the provided map (returns None if the ref is absent).
    """

    def __init__(
        self,
        scan_issues: list[IssueSnapshot] | None = None,
        issue_map: dict[str, IssueSnapshot | None] | None = None,
    ) -> None:
        self._scan_issues: list[IssueSnapshot] = scan_issues or []
        self._issue_map: dict[str, IssueSnapshot | None] = issue_map or {}

    def scan(self) -> Iterator[IssueSnapshot]:
        yield from self._scan_issues

    def get_issue(self, source_ref: str) -> IssueSnapshot | None:
        return self._issue_map.get(source_ref)


# ---------------------------------------------------------------------------
# Snapshot factory — minimizes boilerplate while keeping tests readable
# ---------------------------------------------------------------------------

def _iso(days_ago: int = 0) -> str:
    """ISO 8601 timestamp offset by days_ago from now (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _snap(
    issue_number: int = 1,
    state: str = "open",
    labels: tuple[str, ...] = (),
    body: str = "Issue body text.",
    created_at: str | None = None,
) -> IssueSnapshot:
    source_ref = f"github:issue/{issue_number}"
    return IssueSnapshot(
        source_ref=source_ref,
        title=f"Issue #{issue_number}",
        state=state,
        labels=labels,
        body=body,
        created_at=created_at or _iso(0),
        updated_at=_iso(0),
        url=f"https://github.com/example/repo/issues/{issue_number}",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "garden_test.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


def _audit_events(registry: Registry, uow_id: str) -> list[str]:
    """Return all audit event names for a UoW, in insertion order."""
    conn = sqlite3.connect(str(registry.db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id",
            (uow_id,),
        ).fetchall()
        return [r["event"] for r in rows]
    finally:
        conn.close()


# ===========================================================================
# scan() — happy path
# ===========================================================================

class TestScanHappyPath:
    def test_new_issue_is_proposed_in_registry(self, registry: Registry) -> None:
        """A new issue discovered by scan() must produce a proposed UoW."""
        source = StubIssueSource(scan_issues=[_snap(issue_number=10)])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 1
        proposed = registry.query(status="proposed")
        assert len(proposed) == 1
        assert proposed[0].source_issue_number == 10

    def test_uow_source_field_encodes_issue_ref(self, registry: Registry) -> None:
        """The UoW.source field must match the canonical github:issue/<n> format."""
        source = StubIssueSource(scan_issues=[_snap(issue_number=42)])
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.scan()

        uows = registry.query(status="proposed")
        assert uows[0].source == "github:issue/42"

    def test_qualifying_label_promotes_uow_to_ready_for_steward(
        self, registry: Registry
    ) -> None:
        """An issue with a qualifying label must bypass proposed → ready-for-steward."""
        snap = _snap(issue_number=5, labels=("bug",), body="Non-empty body.")
        source = StubIssueSource(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 1
        assert result["qualified"] == 1
        assert registry.query(status="proposed") == []
        ready = registry.query(status="ready-for-steward")
        assert len(ready) == 1
        assert ready[0].source_issue_number == 5

    def test_qualified_uow_audit_log_contains_qualified_event(
        self, registry: Registry
    ) -> None:
        """The audit log must record a 'qualified' event when a UoW is promoted."""
        snap = _snap(issue_number=7, labels=("high-priority",), body="Body.")
        source = StubIssueSource(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.scan()

        uow = registry.query(status="ready-for-steward")[0]
        events = _audit_events(registry, uow.id)
        assert "qualified" in events

    def test_multiple_issues_all_seeded(self, registry: Registry) -> None:
        """scan() must propose a UoW for each new issue returned by the source."""
        snaps = [_snap(issue_number=n) for n in (1, 2, 3)]
        source = StubIssueSource(scan_issues=snaps)
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 3
        assert len(registry.query(status="proposed")) == 3


# ===========================================================================
# scan() — duplicate / already-in-registry
# ===========================================================================

class TestScanDuplicatePrevention:
    def test_issue_already_proposed_is_not_re_seeded(
        self, registry: Registry
    ) -> None:
        """A non-terminal UoW for an issue must block re-seeding on the next scan."""
        registry.upsert(issue_number=10, title="Existing", success_criteria="Test completion.")

        source = StubIssueSource(scan_issues=[_snap(issue_number=10)])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 0
        # Exactly one UoW must exist — no phantom duplicate
        assert len(registry.query(status="proposed")) == 1

    def test_meta_labelled_issues_are_filtered_at_scan(
        self, registry: Registry
    ) -> None:
        """Issues carrying a meta label must be ignored entirely by scan()."""
        snap = _snap(issue_number=99, labels=("wos-phase-2",))
        source = StubIssueSource(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result["seeded"] == 0
        assert registry.query(status="proposed") == []

    def test_malformed_source_ref_is_skipped_without_crash(
        self, registry: Registry
    ) -> None:
        """scan() must skip issues whose source_ref cannot be parsed, not raise."""
        bad_snap = IssueSnapshot(
            source_ref="not-a-valid-ref",
            title="Bad ref",
            state="open",
            labels=(),
            body="body",
            created_at=_iso(0),
            updated_at=_iso(0),
            url="https://example.com",
        )
        source = StubIssueSource(scan_issues=[bad_snap])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()  # must not raise

        assert result["seeded"] == 0


# ===========================================================================
# scan() — empty source
# ===========================================================================

class TestScanEmptySource:
    def test_empty_source_returns_zero_seeded(self, registry: Registry) -> None:
        """scan() must handle an empty source without error."""
        source = StubIssueSource(scan_issues=[])
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.scan()

        assert result == {"seeded": 0, "qualified": 0}
        assert registry.query(status="proposed") == []


# ===========================================================================
# tend() — issue state unchanged (open source, proposed UoW)
# ===========================================================================

class TestTendNoChange:
    def test_open_source_proposed_uow_is_no_op(self, registry: Registry) -> None:
        """An open issue paired with a proposed UoW must produce no state change."""
        registry.upsert(issue_number=42, title="Open and proposed", success_criteria="Test completion.")
        snap = _snap(issue_number=42, state="open")
        source = StubIssueSource(issue_map={"github:issue/42": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["no_change"] == 1
        assert result["archived"] == 0
        assert result["surfaced_to_steward"] == 0
        # UoW must remain proposed
        assert len(registry.query(status="proposed")) == 1

    def test_open_source_pending_uow_is_no_op(self, registry: Registry) -> None:
        """An open issue paired with a pending UoW must produce no state change."""
        result = registry.upsert(issue_number=43, title="Pending open", success_criteria="Test completion.")
        registry.approve(result.id)

        snap = _snap(issue_number=43, state="open")
        source = StubIssueSource(issue_map={"github:issue/43": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        tend_result = caretaker.tend()

        assert tend_result["no_change"] == 1
        assert len(registry.query(status="pending")) == 1


# ===========================================================================
# tend() — issue closed → archive (proposed/pending)
# ===========================================================================

class TestTendArchiveOnClose:
    def test_closed_source_archives_proposed_uow(self, registry: Registry) -> None:
        """A closed issue with a proposed UoW must be archived (→ expired)."""
        registry.upsert(issue_number=10, title="Will close", success_criteria="Test completion.")
        snap = _snap(issue_number=10, state="closed")
        source = StubIssueSource(issue_map={"github:issue/10": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["archived"] == 1
        assert result["surfaced_to_steward"] == 0
        assert registry.query(status="proposed") == []
        assert len(registry.query(status="expired")) == 1

    def test_closed_source_archives_pending_uow(self, registry: Registry) -> None:
        """A closed issue with a pending UoW must also be archived."""
        upsert = registry.upsert(issue_number=11, title="Pending close", success_criteria="Test completion.")
        registry.approve(upsert.id)

        snap = _snap(issue_number=11, state="closed")
        source = StubIssueSource(issue_map={"github:issue/11": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["archived"] == 1
        assert registry.query(status="pending") == []
        assert len(registry.query(status="expired")) == 1

    def test_archive_writes_audit_entry(self, registry: Registry) -> None:
        """tend() archiving a UoW must write an 'archived_by_caretaker' audit entry."""
        upsert = registry.upsert(issue_number=80, title="Audit on archive", success_criteria="Test completion.")
        snap = _snap(issue_number=80, state="closed")
        source = StubIssueSource(issue_map={"github:issue/80": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.tend()

        events = _audit_events(registry, upsert.id)
        assert "archived_by_caretaker" in events


# ===========================================================================
# tend() — issue closed → surface (in-flight UoW)
# ===========================================================================

class TestTendSurfaceOnClose:
    def test_closed_source_surfaces_active_uow(self, registry: Registry) -> None:
        """A closed issue with an active UoW must be surfaced to Steward."""
        upsert = registry.upsert(issue_number=20, title="Active close", success_criteria="Test completion.")
        registry.set_status_direct(upsert.id, "active")

        snap = _snap(issue_number=20, state="closed")
        source = StubIssueSource(issue_map={"github:issue/20": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["surfaced_to_steward"] == 1
        assert result["archived"] == 0
        assert registry.query(status="expired") == []
        ready = registry.query(status="ready-for-steward")
        assert len(ready) == 1

    def test_deleted_source_surfaces_active_uow(self, registry: Registry) -> None:
        """A deleted issue (get_issue returns None) with an active UoW must be surfaced."""
        upsert = registry.upsert(issue_number=30, title="Active deleted", success_criteria="Test completion.")
        registry.set_status_direct(upsert.id, "active")

        # Return None from get_issue → source deleted/not found
        source = StubIssueSource(issue_map={"github:issue/30": None})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["surfaced_to_steward"] == 1
        ready = registry.query(status="ready-for-steward")
        assert len(ready) == 1

    def test_surface_writes_audit_entry(self, registry: Registry) -> None:
        """tend() surfacing a UoW must write a 'surfaced_to_steward' audit entry."""
        upsert = registry.upsert(issue_number=90, title="Surface audit", success_criteria="Test completion.")
        registry.set_status_direct(upsert.id, "active")

        snap = _snap(issue_number=90, state="closed")
        source = StubIssueSource(issue_map={"github:issue/90": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.tend()

        events = _audit_events(registry, upsert.id)
        assert "surfaced_to_steward" in events


# ===========================================================================
# tend() — source reopened → reactivate terminal UoW
# ===========================================================================

class TestTendReactivate:
    def test_expired_uow_reactivated_when_source_reopens(
        self, registry: Registry
    ) -> None:
        """An open source issue paired with an expired UoW must be reactivated."""
        upsert = registry.upsert(issue_number=50, title="Was expired", success_criteria="Test completion.")
        registry.set_status_direct(upsert.id, "expired")

        snap = _snap(issue_number=50, state="open")
        source = StubIssueSource(issue_map={"github:issue/50": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["reactivated"] == 1
        proposed = registry.query(status="proposed")
        assert len(proposed) == 1
        assert proposed[0].id == upsert.id

    def test_failed_uow_reactivated_when_source_reopens(
        self, registry: Registry
    ) -> None:
        """An open source issue paired with a failed UoW must also be reactivated."""
        upsert = registry.upsert(issue_number=51, title="Was failed", success_criteria="Test completion.")
        registry.set_status_direct(upsert.id, "failed")

        snap = _snap(issue_number=51, state="open")
        source = StubIssueSource(issue_map={"github:issue/51": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        assert result["reactivated"] == 1
        assert len(registry.query(status="proposed")) == 1


# ===========================================================================
# tend() — done UoW is never touched
# ===========================================================================

class TestTendDoneUoWIsExcluded:
    def test_done_uow_excluded_from_tend(self, registry: Registry) -> None:
        """Done UoWs are excluded from tend() entirely — get_issue must not be called."""
        upsert = registry.upsert(issue_number=60, title="Done issue", success_criteria="Test completion.")
        registry.set_status_direct(upsert.id, "done")

        # The stub source has no entry for this ref — get_issue would return None
        # (deleted semantics). If tend() incorrectly includes done UoWs it would
        # attempt an archive, making this test fail.
        source = StubIssueSource(issue_map={})
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.tend()

        # No archived, no surfaced — done UoW is untouched
        assert result["archived"] == 0
        assert result["surfaced_to_steward"] == 0
        done = registry.query(status="done")
        assert len(done) == 1
        assert done[0].id == upsert.id


# ===========================================================================
# Source tracking field assertions
# ===========================================================================

class TestSourceTracking:
    def test_scan_uow_source_field_set_from_source_ref(
        self, registry: Registry
    ) -> None:
        """The UoW.source field written at scan time must match source_ref."""
        snap = _snap(issue_number=100)
        source = StubIssueSource(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.scan()

        uow = registry.query(status="proposed")[0]
        # The registry derives source from issue_number at upsert time.
        assert uow.source == "github:issue/100"

    def test_audit_log_archive_entry_contains_source_ref(
        self, registry: Registry
    ) -> None:
        """The archived_by_caretaker audit note must include the source_ref."""
        upsert = registry.upsert(issue_number=101, title="Source ref audit", success_criteria="Test completion.")
        snap = _snap(issue_number=101, state="closed")
        source = StubIssueSource(issue_map={"github:issue/101": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.tend()

        conn = sqlite3.connect(str(registry.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT event, note FROM audit_log WHERE uow_id = ? ORDER BY id",
                (upsert.id,),
            ).fetchall()
        finally:
            conn.close()

        archive_notes = [
            r["note"] for r in rows if r["event"] == "archived_by_caretaker"
        ]
        assert archive_notes, "No archived_by_caretaker entry found"
        import json
        entry = json.loads(archive_notes[0])
        assert entry.get("source_ref") == "github:issue/101"

    def test_audit_log_surface_entry_contains_source_ref(
        self, registry: Registry
    ) -> None:
        """The surfaced_to_steward audit note must include the source_ref."""
        upsert = registry.upsert(issue_number=102, title="Surface source ref", success_criteria="Test completion.")
        registry.set_status_direct(upsert.id, "active")

        snap = _snap(issue_number=102, state="closed")
        source = StubIssueSource(issue_map={"github:issue/102": snap})
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.tend()

        conn = sqlite3.connect(str(registry.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT event, note FROM audit_log WHERE uow_id = ? ORDER BY id",
                (upsert.id,),
            ).fetchall()
        finally:
            conn.close()

        surface_notes = [
            r["note"] for r in rows if r["event"] == "surfaced_to_steward"
        ]
        assert surface_notes, "No surfaced_to_steward entry found"
        import json
        entry = json.loads(surface_notes[0])
        assert entry.get("source_ref") == "github:issue/102"

    def test_qualified_audit_entry_contains_source_ref(
        self, registry: Registry
    ) -> None:
        """The 'qualified' audit note from scan() must include the source_ref."""
        snap = _snap(issue_number=103, labels=("bug",), body="Body text.")
        source = StubIssueSource(scan_issues=[snap])
        caretaker = GardenCaretaker(source=source, registry=registry)
        caretaker.scan()

        uow = registry.query(status="ready-for-steward")[0]
        conn = sqlite3.connect(str(registry.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT event, note FROM audit_log WHERE uow_id = ? ORDER BY id",
                (uow.id,),
            ).fetchall()
        finally:
            conn.close()

        qualified_notes = [r["note"] for r in rows if r["event"] == "qualified"]
        assert qualified_notes, "No 'qualified' audit entry found"
        import json
        entry = json.loads(qualified_notes[0])
        assert entry.get("source_ref") == "github:issue/103"


# ===========================================================================
# run() — merged summary
# ===========================================================================

class TestRun:
    def test_run_returns_all_expected_keys(self, registry: Registry) -> None:
        """run() must return a merged dict with keys from both scan() and tend()."""
        snap = _snap(issue_number=200, state="open")
        source = StubIssueSource(
            scan_issues=[snap],
            issue_map={"github:issue/200": snap},
        )
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.run()

        expected_keys = {
            "seeded", "qualified",
            "archived", "surfaced_to_steward", "reactivated", "no_change",
        }
        assert expected_keys <= result.keys()

    def test_run_scan_then_tend_on_same_issue(self, registry: Registry) -> None:
        """run() must scan then tend: a newly seeded open issue results in no_change from tend."""
        snap = _snap(issue_number=201, state="open")
        source = StubIssueSource(
            scan_issues=[snap],
            issue_map={"github:issue/201": snap},
        )
        caretaker = GardenCaretaker(source=source, registry=registry)

        result = caretaker.run()

        assert result["seeded"] == 1
        assert result["no_change"] == 1
        assert result["archived"] == 0
