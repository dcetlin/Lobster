"""
Integration tests: executor_orphan detection via startup sweep.

Scenario:
- UoW seeded, advanced to ready-for-executor, created_at pushed back > 1 hour.
- run_startup_sweep() called.
- Assert UoW transitions to 'ready-for-steward' with audit entry
  classification='executor_orphan'.

Tests use a SQLite DB (via tmp_path) with all real migrations applied.
No network calls, no subprocess spawning.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.migrate import run_migrations
from orchestration.registry import Registry, UpsertInserted, ApproveConfirmed
from orchestration.steward import IssueInfo

_SCHEDULED_TASKS = str(_REPO_ROOT / "scheduled-tasks")
if _SCHEDULED_TASKS not in sys.path:
    sys.path.insert(0, _SCHEDULED_TASKS)

from startup_sweep import run_startup_sweep, StartupSweepResult  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(hours: float = 0, seconds: float = 0) -> str:
    """Return an ISO timestamp N hours/seconds in the past."""
    delta = timedelta(hours=hours, seconds=seconds)
    return (datetime.now(timezone.utc) - delta).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ttl_recovery_test.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    """Fully-migrated Registry on a fresh DB."""
    run_migrations(db_path)
    return Registry(db_path)


@pytest.fixture
def conn(db_path: Path, registry: Registry) -> sqlite3.Connection:
    """Open raw connection for direct SQL assertions. Closed after test."""
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_ready_for_executor(registry: Registry) -> str:
    """
    Seed a UoW and advance it to 'ready-for-executor' status.

    Returns the uow_id.
    """
    result = registry.upsert(
        issue_number=9902,
        title="TTL test: executor_orphan",
        sweep_date="2026-03-31",
        success_criteria="Test completion.",
    )
    assert isinstance(result, UpsertInserted), f"seed failed: {result}"
    uow_id = result.id

    registry.approve(uow_id)
    registry.set_status_direct(uow_id, "ready-for-steward")
    registry.set_status_direct(uow_id, "ready-for-executor")

    return uow_id


# ---------------------------------------------------------------------------
# Test 1: executor_orphan — startup sweep detects stale ready-for-executor UoW
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_startup_sweep_detects_executor_orphan(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    UoWs stuck in 'ready-for-executor' for > 1 hour are surfaced as
    executor_orphan by run_startup_sweep(), transitioning to 'ready-for-steward'.

    Proof: backdate created_at to 2 hours ago, run startup sweep with a
    no-op github_client, assert status transitions and audit classification.
    """
    uow_id = _seed_ready_for_executor(registry)

    # Backdate both created_at and updated_at to be well past the 1-hour orphan threshold.
    # The startup sweep uses updated_at as the age anchor (added to fix Sprint 2 regression
    # where created_at always exceeded threshold). Both must be backdated so the sweep
    # treats the UoW as stale.
    old_timestamp = _ago_iso(hours=2)
    conn.execute(
        "UPDATE uow_registry SET created_at = ?, updated_at = ? WHERE id = ?",
        (old_timestamp, old_timestamp, uow_id),
    )
    conn.commit()

    # Verify precondition
    row = conn.execute(
        "SELECT status, created_at, updated_at FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()
    assert row["status"] == "ready-for-executor"
    assert row["created_at"] == old_timestamp
    assert row["updated_at"] == old_timestamp

    # Run startup sweep with no-op github_client (no labels → not bootup-candidate-gated)
    def _noop_github(issue_number: int) -> IssueInfo:
        return IssueInfo(status_code=200, state="open", labels=[], body="", title="")

    result = run_startup_sweep(
        registry=registry,
        dry_run=False,
        orphan_threshold_seconds=3600,
        bootup_candidate_gate=False,
        github_client=_noop_github,
    )

    # Assert startup sweep counted 1 executor_orphan swept
    assert result.executor_orphans_swept == 1, (
        f"Expected executor_orphans_swept=1, got {result.executor_orphans_swept}"
    )

    # Assert UoW status is now 'ready-for-steward'
    row = conn.execute(
        "SELECT status FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()
    assert row["status"] == "ready-for-steward", (
        f"Expected status='ready-for-steward', got {row['status']!r}"
    )

    # Assert audit trail contains executor_orphan classification
    audit_rows = conn.execute(
        "SELECT event, from_status, to_status, note FROM audit_log WHERE uow_id = ?",
        (uow_id,),
    ).fetchall()
    orphan_entries = [
        r for r in audit_rows
        if r["event"] == "startup_sweep"
        and r["from_status"] == "ready-for-executor"
        and r["to_status"] == "ready-for-steward"
    ]
    assert orphan_entries, (
        f"No startup_sweep audit entry found. All audit entries: "
        f"{[dict(r) for r in audit_rows]}"
    )

    note = json.loads(orphan_entries[0]["note"])
    assert note.get("classification") == "executor_orphan", (
        f"Expected classification='executor_orphan' in audit note, got: {note!r}"
    )


@pytest.mark.integration
def test_startup_sweep_ignores_fresh_ready_for_executor_uow(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    UoWs recently put into 'ready-for-executor' are NOT swept as executor_orphan.

    Proof: created_at is 'now' (under the 1-hour threshold), run sweep, assert
    UoW stays 'ready-for-executor' and sweep count is 0.
    """
    uow_id = _seed_ready_for_executor(registry)

    # created_at is very recent — under the orphan threshold

    def _noop_github(issue_number: int) -> IssueInfo:
        return IssueInfo(status_code=200, state="open", labels=[], body="", title="")

    result = run_startup_sweep(
        registry=registry,
        dry_run=False,
        orphan_threshold_seconds=3600,
        bootup_candidate_gate=False,
        github_client=_noop_github,
    )

    assert result.executor_orphans_swept == 0, (
        f"Expected executor_orphans_swept=0, got {result.executor_orphans_swept}"
    )

    row = conn.execute(
        "SELECT status FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()
    assert row["status"] == "ready-for-executor", (
        f"Expected status='ready-for-executor' (unchanged), got {row['status']!r}"
    )
