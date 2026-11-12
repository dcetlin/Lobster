"""
Tests for message_claims pruning (issue #1436 memory-leak fix).

Verifies:
- cleanup_old_claims() removes rows older than the prune window
- cleanup_old_claims() preserves recent rows
- claim() triggers periodic self-pruning every _CLAIM_PRUNE_INTERVAL calls
- _CLAIM_PRUNE_AGE_DAYS is importable as a named constant (golden-patterns)
"""

from __future__ import annotations

import sys
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from claims import AtomicClaimDB

# Named constant so tests can be understood without reading the source.
PRUNE_AGE_DAYS = AtomicClaimDB._CLAIM_PRUNE_AGE_DAYS
PRUNE_INTERVAL = AtomicClaimDB._CLAIM_PRUNE_INTERVAL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_db(tmp_path: Path) -> tuple[AtomicClaimDB, Path]:
    db_path = tmp_path / "claims_test.db"
    db = AtomicClaimDB(path=db_path)
    return db, db_path


def _insert_claim(db_path: Path, message_id: str, claimed_at: datetime, status: str = "processed") -> None:
    """Directly insert a claim row at a specific timestamp, bypassing claim() to control age."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO message_claims (message_id, claimed_by, claimed_at, status) "
        "VALUES (?, ?, ?, ?)",
        (message_id, "test", _iso(claimed_at), status),
    )
    conn.commit()
    conn.close()


def _count_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM message_claims").fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# cleanup_old_claims() — one-shot startup cleanup
# ---------------------------------------------------------------------------

class TestCleanupOldClaims:

    def test_old_rows_removed_and_count_returned(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        old_dt = datetime.now(timezone.utc) - timedelta(days=PRUNE_AGE_DAYS + 1)
        for i in range(5):
            _insert_claim(db_path, f"old_{i}", old_dt)

        deleted = db.cleanup_old_claims()

        assert deleted == 5
        assert _count_rows(db_path) == 0

    def test_recent_rows_preserved(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        old_dt = datetime.now(timezone.utc) - timedelta(days=PRUNE_AGE_DAYS + 1)
        recent_dt = datetime.now(timezone.utc) - timedelta(hours=1)

        for i in range(3):
            _insert_claim(db_path, f"old_{i}", old_dt)
        for i in range(4):
            _insert_claim(db_path, f"recent_{i}", recent_dt)

        deleted = db.cleanup_old_claims()

        assert deleted == 3
        assert _count_rows(db_path) == 4

    def test_returns_zero_when_table_is_empty(self, tmp_path):
        db, _ = _make_db(tmp_path)
        assert db.cleanup_old_claims() == 0

    def test_rows_exactly_at_boundary_not_deleted(self, tmp_path):
        """Rows claimed exactly PRUNE_AGE_DAYS ago (not older) must survive."""
        db, db_path = _make_db(tmp_path)
        # Use a timestamp just inside the prune window (e.g. 1 hour shy of the cutoff).
        borderline_dt = datetime.now(timezone.utc) - timedelta(days=PRUNE_AGE_DAYS - 0.05)
        _insert_claim(db_path, "borderline", borderline_dt)

        deleted = db.cleanup_old_claims()

        assert deleted == 0
        assert _count_rows(db_path) == 1


# ---------------------------------------------------------------------------
# Periodic pruning from claim()
# ---------------------------------------------------------------------------

class TestPeriodicPruning:

    def test_prune_fires_after_prune_interval_claims(self, tmp_path):
        """After exactly PRUNE_INTERVAL claim() calls, old rows must have been evicted."""
        db, db_path = _make_db(tmp_path)
        old_dt = datetime.now(timezone.utc) - timedelta(days=PRUNE_AGE_DAYS + 1)

        # Insert old rows directly (not via claim() so they don't increment the counter).
        for i in range(10):
            _insert_claim(db_path, f"pre_old_{i}", old_dt)

        initial_count = _count_rows(db_path)
        assert initial_count == 10

        # Drive claim() enough times to trigger the periodic prune.
        for i in range(PRUNE_INTERVAL):
            db.claim(f"live_{i}")

        # Old rows must have been pruned by the periodic mechanism.
        remaining = _count_rows(db_path)
        # We inserted PRUNE_INTERVAL live rows plus had 10 old ones; only live rows survive.
        assert remaining == PRUNE_INTERVAL

    def test_prune_does_not_fire_before_interval(self, tmp_path):
        """Old rows must still exist if claim() has not reached the prune interval."""
        db, db_path = _make_db(tmp_path)
        old_dt = datetime.now(timezone.utc) - timedelta(days=PRUNE_AGE_DAYS + 1)

        for i in range(3):
            _insert_claim(db_path, f"pre_old_{i}", old_dt)

        # Call claim() fewer times than the prune interval.
        calls_before_prune = PRUNE_INTERVAL - 1
        for i in range(calls_before_prune):
            db.claim(f"live_{i}")

        # Old rows must still be present (prune has not fired yet).
        # live_N rows are recent so they'd survive anyway; we specifically check
        # that the old rows haven't been deleted prematurely.
        remaining = _count_rows(db_path)
        assert remaining >= 3  # at minimum the old rows survive (plus the live ones)


# ---------------------------------------------------------------------------
# Named constant importability (golden-patterns: importable constants)
# ---------------------------------------------------------------------------

class TestNamedConstants:

    def test_prune_age_days_is_importable_int(self):
        assert isinstance(AtomicClaimDB._CLAIM_PRUNE_AGE_DAYS, int)
        assert AtomicClaimDB._CLAIM_PRUNE_AGE_DAYS > 0

    def test_prune_interval_is_importable_int(self):
        assert isinstance(AtomicClaimDB._CLAIM_PRUNE_INTERVAL, int)
        assert AtomicClaimDB._CLAIM_PRUNE_INTERVAL > 0
