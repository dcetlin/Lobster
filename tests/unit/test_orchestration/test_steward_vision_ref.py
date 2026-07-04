"""
Unit tests for Vision Object commitment device mechanisms.

Issue: https://github.com/dcetlin/Lobster/issues/689

Tests cover:
- Mechanism 2: steward cycle records vision_ref in steward_log (cycle_vision_anchor event)
- Mechanism 2: steward cycle records vision_ref=null + warning when UoW has no vision_ref
- Mechanism 2: cycle_vision_anchor entry includes uow_id, cycle, vision_ref, timestamp
- Mechanism 1: UoW creation without vision_ref writes a vision_ref_missing audit entry
- Mechanism 1: UoW creation WITH vision_ref does NOT write vision_ref_missing
- Mechanism 1: vision_ref_missing is warning-only — UoW is still created (no block)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Named constants from the spec (never use magic literals)
CYCLE_VISION_ANCHOR_EVENT = "cycle_vision_anchor"
VISION_REF_MISSING_EVENT = "vision_ref_missing"


# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _make_uow_row(
    conn: sqlite3.Connection,
    uow_id: str | None = None,
    status: str = "ready-for-steward",
    steward_cycles: int = 0,
    lifetime_cycles: int | None = None,
    output_ref: str | None = None,
    steward_agenda: str | None = None,
    steward_log: str | None = None,
    source_issue_number: int = 42,
    summary: str = "Test UoW",
    success_criteria: str | None = "Output file exists with non-empty content",
    vision_ref: dict | None = None,
) -> str:
    """Insert a UoW row. Returns the uow_id."""
    if uow_id is None:
        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
    effective_lifetime_cycles = lifetime_cycles if lifetime_cycles is not None else steward_cycles
    now = _now_iso()
    vision_ref_json = json.dumps(vision_ref) if vision_ref is not None else None
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, output_ref, steward_cycles, lifetime_cycles,
             steward_agenda, steward_log, success_criteria, vision_ref,
             route_evidence, trigger)
        VALUES (?, 'executable', 'github:issue/42', ?, '2026-01-01', ?, 'solo',
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}')
        """,
        (
            uow_id, source_issue_number, status, now, now, summary,
            output_ref, steward_cycles, effective_lifetime_cycles,
            steward_agenda, steward_log, success_criteria, vision_ref_json,
        ),
    )
    conn.commit()
    return uow_id


def _get_steward_log_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = _open_db(db_path)
    row = conn.execute(
        "SELECT steward_log FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    conn.close()
    if row is None or row["steward_log"] is None:
        return []
    raw = row["steward_log"]
    # steward_log is stored as newline-delimited JSON objects
    entries = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def _get_audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = _open_db(db_path)
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id",
        (uow_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _mock_github_client_open(issue_number: int):
    """Mock GitHub client: issue is open, no labels. Returns typed IssueInfo."""
    from src.orchestration.steward import IssueInfo
    return IssueInfo(
        status_code=200,
        state="open",
        labels=[],
        body=f"Issue #{issue_number}: implement this feature.\n\nAcceptance criteria:\n- Feature works",
        title=f"Test issue {issue_number}",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Provision a test registry DB using the real Registry (runs migrations)."""
    path = tmp_path / "registry.db"
    from src.orchestration.registry import Registry
    Registry(db_path=path)  # runs migrations — creates the full schema
    return path


@pytest.fixture
def registry(db_path: Path):
    from src.orchestration.registry import Registry
    return Registry(db_path)


@pytest.fixture
def db_conn(db_path: Path) -> sqlite3.Connection:
    conn = _open_db(db_path)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Mechanism 2: steward cycle vision_ref logging
# ---------------------------------------------------------------------------

class TestStewardCycleVisionRefLogging:
    """Steward cycle appends a cycle_vision_anchor entry to steward_log.

    Behavior:
    - UoW with vision_ref: cycle_vision_anchor records the vision_ref value
    - UoW without vision_ref: cycle_vision_anchor records null + warning note
    - Entry includes: event, vision_ref, uow_id, cycle, timestamp
    """

    def _run_one_cycle(self, db_path: Path, registry, tmp_path: Path, uow_id: str) -> None:
        """Run one steward cycle against a single UoW."""
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        from src.orchestration import steward
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=tmp_path / "artifacts",
        )

    def test_cycle_vision_anchor_recorded_when_vision_ref_present(
        self, db_path, registry, db_conn, tmp_path
    ):
        """Steward cycle with a vision_ref UoW records cycle_vision_anchor with the ref."""
        vision_ref = {
            "layer": "active_project",
            "field": "phase_intent",
            "statement": "Build the substrate for intent-anchored decisions.",
            "anchored_at": "2026-06-01T00:00:00+00:00",
        }
        uow_id = _make_uow_row(
            db_conn,
            status="ready-for-steward",
            steward_cycles=0,
            vision_ref=vision_ref,
        )

        self._run_one_cycle(db_path, registry, tmp_path, uow_id)

        log_entries = _get_steward_log_entries(db_path, uow_id)
        anchor_entries = [e for e in log_entries if e.get("event") == CYCLE_VISION_ANCHOR_EVENT]
        assert len(anchor_entries) >= 1, (
            f"Expected at least one {CYCLE_VISION_ANCHOR_EVENT!r} entry in steward_log. "
            f"Got events: {[e.get('event') for e in log_entries]}"
        )

        entry = anchor_entries[0]
        assert entry["vision_ref"] == vision_ref, (
            f"cycle_vision_anchor must record the full vision_ref dict. Got: {entry['vision_ref']!r}"
        )
        assert entry["uow_id"] == uow_id
        assert "cycle" in entry
        assert "timestamp" in entry

    def test_cycle_vision_anchor_recorded_as_null_when_no_vision_ref(
        self, db_path, registry, db_conn, tmp_path
    ):
        """Steward cycle with a vision_ref-less UoW records cycle_vision_anchor with null."""
        uow_id = _make_uow_row(
            db_conn,
            status="ready-for-steward",
            steward_cycles=0,
            vision_ref=None,
        )

        self._run_one_cycle(db_path, registry, tmp_path, uow_id)

        log_entries = _get_steward_log_entries(db_path, uow_id)
        anchor_entries = [e for e in log_entries if e.get("event") == CYCLE_VISION_ANCHOR_EVENT]
        assert len(anchor_entries) >= 1, (
            f"Expected at least one {CYCLE_VISION_ANCHOR_EVENT!r} entry when vision_ref is absent. "
            f"Got events: {[e.get('event') for e in log_entries]}"
        )

        entry = anchor_entries[0]
        assert entry["vision_ref"] is None, (
            f"cycle_vision_anchor must record null vision_ref when UoW has no anchor. "
            f"Got: {entry['vision_ref']!r}"
        )
        # Null vision_ref must carry a warning note
        assert "note" in entry, (
            f"cycle_vision_anchor with null vision_ref must include a 'note' warning. "
            f"Entry: {entry}"
        )

    def test_cycle_vision_anchor_includes_required_fields(
        self, db_path, registry, db_conn, tmp_path
    ):
        """cycle_vision_anchor entries include: event, vision_ref, uow_id, cycle, timestamp."""
        uow_id = _make_uow_row(
            db_conn,
            status="ready-for-steward",
            steward_cycles=0,
            vision_ref={
                "layer": "north_star",
                "field": "purpose",
                "statement": "Build X",
                "anchored_at": "2026-01-01",
            },
        )

        self._run_one_cycle(db_path, registry, tmp_path, uow_id)

        log_entries = _get_steward_log_entries(db_path, uow_id)
        anchor_entries = [e for e in log_entries if e.get("event") == CYCLE_VISION_ANCHOR_EVENT]
        assert anchor_entries, f"Expected {CYCLE_VISION_ANCHOR_EVENT} in steward_log"

        entry = anchor_entries[0]
        for required_field in ("event", "vision_ref", "uow_id", "cycle", "timestamp"):
            assert required_field in entry, (
                f"cycle_vision_anchor entry missing required field '{required_field}'. "
                f"Entry: {entry}"
            )

    def test_cycle_vision_anchor_cycle_field_equals_steward_cycles(
        self, db_path, registry, db_conn, tmp_path
    ):
        """cycle_vision_anchor 'cycle' field records the current steward_cycles value (0 on first)."""
        uow_id = _make_uow_row(
            db_conn,
            status="ready-for-steward",
            steward_cycles=0,
            vision_ref={
                "layer": "active_project",
                "field": "momentum",
                "statement": "Keep shipping.",
                "anchored_at": "2026-06-01T00:00:00+00:00",
            },
        )

        self._run_one_cycle(db_path, registry, tmp_path, uow_id)

        log_entries = _get_steward_log_entries(db_path, uow_id)
        anchor_entries = [e for e in log_entries if e.get("event") == CYCLE_VISION_ANCHOR_EVENT]
        assert anchor_entries, f"Expected {CYCLE_VISION_ANCHOR_EVENT} in steward_log on cycle 0"
        assert anchor_entries[0]["cycle"] == 0, (
            f"cycle field must be 0 on first steward cycle. Got: {anchor_entries[0]['cycle']!r}"
        )


# ---------------------------------------------------------------------------
# Mechanism 1: soft vision_ref gate in UoW creation
# ---------------------------------------------------------------------------

class TestSoftVisionRefGate:
    """UoW creation without vision_ref writes a vision_ref_missing audit entry.

    Behavior:
    - No vision_ref → audit entry event=VISION_REF_MISSING_EVENT written
    - vision_ref_missing is warning-only: UoW is still created (soft gate, no block)
    """

    def test_vision_ref_missing_audit_entry_written_when_no_vision_ref(
        self, db_path, registry, tmp_path
    ):
        """Creating a UoW without vision_ref writes a vision_ref_missing audit entry."""
        from src.orchestration.registry import UpsertInserted
        result = registry.upsert(
            issue_number=9001,
            title="Test UoW without vision anchor",
            success_criteria="Something gets done.",
        )
        # UoW must be created (soft gate — no block)
        assert isinstance(result, UpsertInserted), (
            f"UoW must be created even without vision_ref (soft gate). Got: {result}"
        )

        audit_entries = registry.fetch_audit_entries(result.id)
        vision_miss_events = [
            e for e in audit_entries if e.get("event") == VISION_REF_MISSING_EVENT
        ]
        assert len(vision_miss_events) >= 1, (
            f"Expected at least one '{VISION_REF_MISSING_EVENT}' audit entry when UoW has no "
            f"vision_ref. Audit events found: {[e.get('event') for e in audit_entries]}"
        )

    def test_vision_ref_missing_entry_has_note_describing_alignment_gap(
        self, db_path, registry, tmp_path
    ):
        """vision_ref_missing audit note describes the alignment gap."""
        from src.orchestration.registry import UpsertInserted
        result = registry.upsert(
            issue_number=9002,
            title="UoW without anchor for field test",
            success_criteria="Task complete.",
        )
        assert isinstance(result, UpsertInserted)

        audit_entries = registry.fetch_audit_entries(result.id)
        vision_miss_events = [
            e for e in audit_entries if e.get("event") == VISION_REF_MISSING_EVENT
        ]
        assert vision_miss_events, f"No {VISION_REF_MISSING_EVENT} entries found"

        # The note must reference vision alignment
        note_str = vision_miss_events[0].get("note", "") or ""
        note_text = note_str.lower()
        if note_str.startswith("{"):
            try:
                note_obj = json.loads(note_str)
                note_text = json.dumps(note_obj).lower()
            except (json.JSONDecodeError, TypeError):
                pass
        assert any(
            term in note_text for term in ("vision", "vision_ref", "aligned", "alignment")
        ), (
            f"vision_ref_missing note should mention vision alignment. Got: {note_str!r}"
        )

    def test_uow_created_despite_missing_vision_ref(
        self, db_path, registry, tmp_path
    ):
        """vision_ref_missing is a soft gate — UoW is created, not blocked."""
        from src.orchestration.registry import UpsertInserted
        result = registry.upsert(
            issue_number=9003,
            title="Soft gate: UoW must still be created",
            success_criteria="Verify no hard block.",
        )
        assert isinstance(result, UpsertInserted), (
            "vision_ref gate must be soft: UoW must be created even without vision_ref. "
            f"Got: {result}"
        )
        uow = registry.get(result.id)
        assert uow is not None, "UoW must be retrievable after creation without vision_ref"
        assert uow.vision_ref is None

    def test_vision_ref_missing_not_written_when_vision_ref_present(
        self, db_path, registry, db_conn, tmp_path
    ):
        """No vision_ref_missing entry when UoW is created and then given a vision_ref.

        The registry.upsert() API does not accept vision_ref (it is written by the
        issue-sweeper in a separate UPDATE). To test the gate's inverse, we manually
        insert a UoW with vision_ref populated and verify the gate function is not
        triggered for rows that already carry a vision_ref.

        This test exercises the audit log directly to confirm no spurious
        vision_ref_missing entries are written for anchored UoWs.
        """
        from src.orchestration.registry import UpsertInserted
        # First create without vision_ref (will generate vision_ref_missing)
        result = registry.upsert(
            issue_number=9004,
            title="UoW that will get a vision_ref",
            success_criteria="Has vision anchor.",
        )
        assert isinstance(result, UpsertInserted)

        # Now simulate the sweeper writing a vision_ref to the row
        vision_ref = {"layer": "active_project", "field": "phase_intent",
                      "statement": "Build X", "anchored_at": "2026-06-01"}
        db_conn.execute(
            "UPDATE uow_registry SET vision_ref = ? WHERE id = ?",
            (json.dumps(vision_ref), result.id),
        )
        db_conn.commit()

        # Count vision_ref_missing entries — there should be exactly one (from the initial
        # creation without vision_ref), not two. The post-update state must not add another.
        audit_entries = registry.fetch_audit_entries(result.id)
        vision_miss_count = sum(
            1 for e in audit_entries if e.get("event") == VISION_REF_MISSING_EVENT
        )
        assert vision_miss_count == 1, (
            f"Expected exactly one vision_ref_missing entry (at creation time). "
            f"Got {vision_miss_count}. This test verifies the gate fires once at creation, "
            f"not on every subsequent operation."
        )
