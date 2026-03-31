"""
Integration tests: TTL recovery and executor_orphan detection.

Two scenarios:

1. TTL recovery (active UoW stalled too long):
   - UoW seeded, claimed by Executor (status → active), started_at pushed
     back > TTL_EXCEEDED_HOURS.
   - recover_ttl_exceeded_uows() called.
   - Assert UoW transitions to 'failed' with return_reason containing
     'ttl_exceeded'.

2. executor_orphan (startup sweep):
   - UoW seeded, advanced to ready-for-executor, created_at pushed back
     > 1 hour.
   - run_startup_sweep() called.
   - Assert UoW transitions to 'ready-for-steward' with audit entry
     classification='executor_orphan'.

Both tests use a SQLite DB (via tmp_path) with all real migrations applied.
No network calls, no subprocess spawning.
"""

from __future__ import annotations

import importlib.util
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
from orchestration.executor import (
    TTL_EXCEEDED_HOURS,
    recover_ttl_exceeded_uows,
)

# startup-sweep.py has a hyphen in the filename — not directly importable as a module.
# Use importlib to load it by file path and register it in sys.modules.
_STARTUP_SWEEP_PATH = _REPO_ROOT / "scheduled-tasks" / "startup-sweep.py"
_spec = importlib.util.spec_from_file_location("startup_sweep", _STARTUP_SWEEP_PATH)
_startup_sweep_mod = importlib.util.module_from_spec(_spec)
sys.modules["startup_sweep"] = _startup_sweep_mod
_spec.loader.exec_module(_startup_sweep_mod)  # type: ignore[union-attr]

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


def _seed_and_activate(registry: Registry, conn: sqlite3.Connection) -> str:
    """
    Seed a UoW and advance it to 'active' status (simulating a claimed execution).

    Returns the uow_id.

    The 6-step Executor claim sequence sets status='active' and writes started_at.
    We simulate this directly in SQL so tests remain self-contained and do not
    depend on a live WorkflowArtifact or dispatch subprocess.
    """
    result = registry.upsert(
        issue_number=9901,
        title="TTL test: stalled execution",
        sweep_date="2026-03-31",
    )
    assert isinstance(result, UpsertInserted), f"seed failed: {result}"
    uow_id = result.id

    # proposed → pending
    confirm = registry.approve(uow_id)
    assert isinstance(confirm, ApproveConfirmed), f"approve failed: {confirm}"

    # pending → ready-for-steward (trigger evaluator stub)
    registry.set_status_direct(uow_id, "ready-for-steward")

    # ready-for-steward → ready-for-executor (steward prescription stub)
    registry.set_status_direct(uow_id, "ready-for-executor")

    # ready-for-executor → active (executor claim stub)
    now = _now_iso()
    conn.execute(
        """
        UPDATE uow_registry
        SET status = 'active',
            started_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (now, now, uow_id),
    )
    conn.execute(
        """
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
        VALUES (?, ?, 'claimed', 'ready-for-executor', 'active', 'executor', ?)
        """,
        (now, uow_id, json.dumps({"actor": "executor_stub", "started_at": now})),
    )
    conn.commit()

    return uow_id


def _seed_ready_for_executor(registry: Registry) -> str:
    """
    Seed a UoW and advance it to 'ready-for-executor' status.

    Returns the uow_id.
    """
    result = registry.upsert(
        issue_number=9902,
        title="TTL test: executor_orphan",
        sweep_date="2026-03-31",
    )
    assert isinstance(result, UpsertInserted), f"seed failed: {result}"
    uow_id = result.id

    registry.approve(uow_id)
    registry.set_status_direct(uow_id, "ready-for-steward")
    registry.set_status_direct(uow_id, "ready-for-executor")

    return uow_id


# ---------------------------------------------------------------------------
# Test 1: TTL recovery — active UoW stalled > TTL_EXCEEDED_HOURS
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ttl_recovery_transitions_stalled_active_uow_to_failed(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    UoWs that stay in 'active' for longer than TTL_EXCEEDED_HOURS are
    transitioned to 'failed' by recover_ttl_exceeded_uows().

    Proof: set started_at to (now - TTL_EXCEEDED_HOURS - 1 minute), run
    recovery, assert status='failed' with an audit entry noting ttl_exceeded.
    """
    uow_id = _seed_and_activate(registry, conn)

    # Backdate started_at to be beyond the TTL threshold
    stale_started_at = _ago_iso(hours=TTL_EXCEEDED_HOURS, seconds=60)
    conn.execute(
        "UPDATE uow_registry SET started_at = ?, updated_at = ? WHERE id = ?",
        (stale_started_at, _now_iso(), uow_id),
    )
    conn.commit()

    # Verify precondition: UoW is 'active' with stale started_at
    row = conn.execute(
        "SELECT status, started_at FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()
    assert row["status"] == "active"
    assert row["started_at"] == stale_started_at

    # Run TTL recovery
    recovered = recover_ttl_exceeded_uows(registry)

    # Assert the UoW was recovered
    assert uow_id in recovered, (
        f"Expected {uow_id!r} in recovered list {recovered!r}"
    )

    # Assert status is now 'failed'
    row = conn.execute(
        "SELECT status FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()
    assert row["status"] == "failed", (
        f"Expected status='failed', got {row['status']!r}"
    )

    # Assert audit trail contains a ttl_exceeded entry
    audit_rows = conn.execute(
        "SELECT event, from_status, to_status, note FROM audit_log WHERE uow_id = ?",
        (uow_id,),
    ).fetchall()
    ttl_entries = [
        r for r in audit_rows
        if r["event"] == "execution_failed"
        and r["from_status"] == "active"
        and r["to_status"] == "failed"
    ]
    assert ttl_entries, (
        f"No execution_failed audit entry found. All audit entries: "
        f"{[dict(r) for r in audit_rows]}"
    )

    note = json.loads(ttl_entries[0]["note"])
    assert "ttl_exceeded" in note.get("reason", ""), (
        f"Expected 'ttl_exceeded' in audit note reason, got: {note!r}"
    )


@pytest.mark.integration
def test_ttl_recovery_ignores_fresh_active_uow(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    UoWs in 'active' with a recent started_at are NOT affected by TTL recovery.

    Proof: set started_at to 1 minute ago (well below TTL_EXCEEDED_HOURS),
    run recovery, assert UoW stays 'active'.
    """
    uow_id = _seed_and_activate(registry, conn)

    # started_at is already 'now' from _seed_and_activate — stays fresh
    recovered = recover_ttl_exceeded_uows(registry)

    assert uow_id not in recovered, (
        f"Expected fresh UoW {uow_id!r} NOT to be in recovered list"
    )

    row = conn.execute(
        "SELECT status FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()
    assert row["status"] == "active", (
        f"Expected status='active' (unchanged), got {row['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: executor_orphan — startup sweep detects stale ready-for-executor UoW
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

    # Backdate created_at to be well past the 1-hour orphan threshold
    old_created_at = _ago_iso(hours=2)
    conn.execute(
        "UPDATE uow_registry SET created_at = ?, updated_at = ? WHERE id = ?",
        (old_created_at, _now_iso(), uow_id),
    )
    conn.commit()

    # Verify precondition
    row = conn.execute(
        "SELECT status, created_at FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()
    assert row["status"] == "ready-for-executor"
    assert row["created_at"] == old_created_at

    # Run startup sweep with no-op github_client (no labels → not bootup-candidate-gated)
    def _noop_github(issue_number: int) -> dict:
        return {"labels": [], "state": "open", "status_code": 200, "body": "", "title": ""}

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

    def _noop_github(issue_number: int) -> dict:
        return {"labels": [], "state": "open", "status_code": 200, "body": "", "title": ""}

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
