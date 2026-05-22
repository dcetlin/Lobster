"""
Integration tests: WOS visibility-timeout (claimed_until) model.

Tests cover reset_expired_claims() and the claimed_until lifecycle:
- Dispatch sets claimed_until
- complete_uow clears claimed_until
- Expired claim resets UoW to ready-for-executor
- Unexpired claim is not reset
- Multiple expired claims all reset without blocking dispatch slots

Tests use a SQLite DB (via tmp_path) with all real migrations applied.
No network calls, no subprocess spawning.
"""

from __future__ import annotations

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
from orchestration.registry import Registry, UoWStatus, UpsertInserted


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "visibility_timeout_test.db"


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


_ISSUE_COUNTER = 9990


def _seed_ready_for_executor(registry: Registry, issue_offset: int = 0) -> str:
    """
    Seed a UoW and advance it to 'ready-for-executor' status.

    Returns the uow_id.
    """
    global _ISSUE_COUNTER
    _ISSUE_COUNTER += 1
    issue_number = _ISSUE_COUNTER + issue_offset

    result = registry.upsert(
        issue_number=issue_number,
        title=f"Visibility timeout test: issue {issue_number}",
        sweep_date="2026-05-20",
        success_criteria="Test completion.",
    )
    assert isinstance(result, UpsertInserted), f"seed failed: {result}"
    uow_id = result.id

    registry.approve(uow_id)
    registry.set_status_direct(uow_id, "ready-for-steward")
    registry.set_status_direct(uow_id, "ready-for-executor")

    return uow_id


# ---------------------------------------------------------------------------
# Test A — Dispatch sets claimed_until
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_dispatch_sets_claimed_until(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    transition_to_executing() must set claimed_until to a future timestamp
    and transition status to 'executing'.
    """
    uow_id = _seed_ready_for_executor(registry)

    registry.transition_to_executing(uow_id, "test-executor-id")

    row = conn.execute(
        "SELECT status, claimed_until FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()

    assert row["status"] == "executing", (
        f"Expected status='executing', got {row['status']!r}"
    )
    assert row["claimed_until"] is not None, (
        "Expected claimed_until IS NOT NULL after dispatch"
    )

    # claimed_until must be in the future
    claimed_until_dt = datetime.fromisoformat(row["claimed_until"])
    # Ensure timezone-aware comparison
    if claimed_until_dt.tzinfo is None:
        claimed_until_dt = claimed_until_dt.replace(tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    assert claimed_until_dt > now_utc, (
        f"Expected claimed_until ({claimed_until_dt}) to be in the future (now={now_utc})"
    )


# ---------------------------------------------------------------------------
# Test B — complete_uow clears claimed_until
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_complete_uow_clears_claimed_until(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    complete_uow() must clear claimed_until (set to NULL) and transition
    status to 'ready-for-steward'.
    """
    uow_id = _seed_ready_for_executor(registry)

    registry.transition_to_executing(uow_id, "test-executor-id")

    # Verify claimed_until was set
    row = conn.execute(
        "SELECT claimed_until FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    assert row["claimed_until"] is not None, "Precondition: claimed_until should be set"

    # Complete the UoW
    registry.complete_uow(uow_id, "/tmp/test_output.json")

    row = conn.execute(
        "SELECT status, claimed_until FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()

    assert row["status"] == "ready-for-steward", (
        f"Expected status='ready-for-steward', got {row['status']!r}"
    )
    assert row["claimed_until"] is None, (
        f"Expected claimed_until IS NULL after complete_uow, got {row['claimed_until']!r}"
    )


# ---------------------------------------------------------------------------
# Test C — Expired claim resets UoW to ready-for-executor
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_expired_claim_resets_to_ready_for_executor(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    When claimed_until has expired, reset_expired_claims() must:
    - Return the uow_id in its result list
    - Set claimed_until to NULL
    - Set status back to 'ready-for-executor'
    """
    uow_id = _seed_ready_for_executor(registry)

    registry.transition_to_executing(uow_id, "test-executor-id")

    # Backdate claimed_until to 1 hour in the past
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute(
        "UPDATE uow_registry SET claimed_until = ? WHERE id = ?",
        (past, uow_id),
    )
    conn.commit()

    # Reset expired claims
    reset_ids = registry.reset_expired_claims()

    assert uow_id in reset_ids, (
        f"Expected uow_id {uow_id!r} in reset_ids, got {reset_ids!r}"
    )

    row = conn.execute(
        "SELECT status, claimed_until FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()

    assert row["status"] == "ready-for-executor", (
        f"Expected status='ready-for-executor', got {row['status']!r}"
    )
    assert row["claimed_until"] is None, (
        f"Expected claimed_until IS NULL after reset, got {row['claimed_until']!r}"
    )


# ---------------------------------------------------------------------------
# Test D — Unexpired claim is not reset
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_unexpired_claim_is_not_reset(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    When claimed_until has NOT expired, reset_expired_claims() must:
    - Return an empty list (this UoW is not reset)
    - Leave status as 'executing'
    """
    uow_id = _seed_ready_for_executor(registry)

    registry.transition_to_executing(uow_id, "test-executor-id")

    # claimed_until is in the future (set by transition_to_executing) — do NOT backdate

    reset_ids = registry.reset_expired_claims()

    assert uow_id not in reset_ids, (
        f"Did not expect uow_id {uow_id!r} in reset_ids (claim not expired), got {reset_ids!r}"
    )

    row = conn.execute(
        "SELECT status FROM uow_registry WHERE id = ?",
        (uow_id,),
    ).fetchone()

    assert row["status"] == "executing", (
        f"Expected status='executing' (unchanged), got {row['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test E — Multiple expired claims all reset without blocking dispatch slots
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_multiple_expired_claims_all_reset(
    registry: Registry,
    conn: sqlite3.Connection,
) -> None:
    """
    When 5 UoWs all have expired claimed_until values, reset_expired_claims()
    must reset all 5 back to 'ready-for-executor' and leave no UoWs in 'executing'.
    """
    uow_ids = []
    for i in range(5):
        uow_id = _seed_ready_for_executor(registry, issue_offset=i * 100)
        registry.transition_to_executing(uow_id, f"test-executor-id-{i}")
        uow_ids.append(uow_id)

    # Backdate all 5 claimed_until values to the past
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    for uow_id in uow_ids:
        conn.execute(
            "UPDATE uow_registry SET claimed_until = ? WHERE id = ?",
            (past, uow_id),
        )
    conn.commit()

    reset_ids = registry.reset_expired_claims()

    # All 5 UoWs must be in the reset list
    for uow_id in uow_ids:
        assert uow_id in reset_ids, (
            f"Expected uow_id {uow_id!r} in reset_ids, got {reset_ids!r}"
        )

    # No UoWs should remain in 'executing'
    executing_uows = registry.list(status=UoWStatus.EXECUTING)
    executing_ids = [u.id for u in executing_uows if u.id in uow_ids]
    assert executing_ids == [], (
        f"Expected no UoWs in 'executing' after reset, still executing: {executing_ids!r}"
    )

    # All 5 should now be 'ready-for-executor'
    for uow_id in uow_ids:
        row = conn.execute(
            "SELECT status, claimed_until FROM uow_registry WHERE id = ?",
            (uow_id,),
        ).fetchone()
        assert row["status"] == "ready-for-executor", (
            f"UoW {uow_id!r}: expected 'ready-for-executor', got {row['status']!r}"
        )
        assert row["claimed_until"] is None, (
            f"UoW {uow_id!r}: expected claimed_until IS NULL, got {row['claimed_until']!r}"
        )
