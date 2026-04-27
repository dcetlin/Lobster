"""
Tests for outcome_telemetry (issue #990).

Covers:
- token_usage column written to uow_registry when complete_uow is called with a value
- token_usage remains NULL when complete_uow is called without a value
- COALESCE semantics: existing non-NULL token_usage is not overwritten by NULL
- wall_clock_seconds computed at query time from completed_at - started_at
- maybe_complete_wos_uow forwards token_usage to complete_uow
- metabolic digest: aggregate_token_usage returns sum for UoWs with data, None when absent
- metabolic digest: compute_wall_clock_stats returns correct avg and count
- metabolic digest: format_digest includes token and wall-clock lines when data present

Named constants mirror the spec so test failures are self-documenting.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — make src importable
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.wos_completion import (
    WOS_TASK_ID_PREFIX,
    WRITE_RESULT_SUCCESS_STATUS,
    maybe_complete_wos_uow,
)
from orchestration.registry import Registry, UoWStatus, UpsertInserted

# ---------------------------------------------------------------------------
# Load metabolic digest module (script, not a package)
# ---------------------------------------------------------------------------

_DIGEST_SCRIPT_PATH = _REPO_ROOT / "scheduled-tasks" / "wos-metabolic-digest.py"


def _load_digest_module():
    spec = importlib.util.spec_from_file_location("wos_metabolic_digest", _DIGEST_SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Named constants matching the spec
# ---------------------------------------------------------------------------

# Threshold below which token_usage is rejected as invalid (non-positive)
TOKEN_USAGE_MIN_VALID = 1

# Representative valid token counts from the spec context
SMALL_TOKEN_COUNT = 1_234
LARGE_TOKEN_COUNT = 98_765
COMBINED_TOKEN_COUNT = SMALL_TOKEN_COUNT + LARGE_TOKEN_COUNT  # 100_000 - 1

# Wall-clock thresholds used in the digest display
WALL_CLOCK_60S = 60
WALL_CLOCK_120S = 120
WALL_CLOCK_AVG_MIN = round((WALL_CLOCK_60S + WALL_CLOCK_120S) / 2 / 60, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_executing_uow(registry: Registry, tmp_path: Path) -> tuple[str, str]:
    """
    Seed a UoW and advance it to 'executing'. Returns (uow_id, output_ref).
    """
    result = registry.upsert(
        issue_number=9902,
        title="Telemetry test UoW",
        success_criteria="token_usage and wall_clock_seconds are recorded",
    )
    assert isinstance(result, UpsertInserted)
    uow_id = result.id

    registry.approve(uow_id)

    output_ref = str(tmp_path / f"{uow_id}.json")
    registry.set_status_direct(uow_id, "active")

    # Write output_ref directly — bypasses Executor internals for test isolation
    conn = sqlite3.connect(str(registry.db_path))
    conn.execute(
        "UPDATE uow_registry SET output_ref = ? WHERE id = ?",
        (output_ref, uow_id),
    )
    conn.commit()
    conn.close()

    registry.transition_to_executing(uow_id, "mock-executor-001")
    return uow_id, output_ref


def _fetch_uow_row(db_path: Path, uow_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    conn.close()
    assert row is not None, f"UoW {uow_id!r} not found in registry"
    return dict(row)


# ---------------------------------------------------------------------------
# Registry: complete_uow with token_usage
# ---------------------------------------------------------------------------

class TestCompleteUowTokenUsage:
    """Token usage is written to the registry when complete_uow provides it."""

    def test_token_usage_stored_when_provided(self, tmp_path: Path) -> None:
        """
        complete_uow(token_usage=N) writes N to the token_usage column.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, output_ref = _seed_executing_uow(registry, tmp_path)

        registry.complete_uow(uow_id, output_ref, token_usage=SMALL_TOKEN_COUNT)

        row = _fetch_uow_row(db_path, uow_id)
        assert row["token_usage"] == SMALL_TOKEN_COUNT, (
            f"Expected token_usage={SMALL_TOKEN_COUNT}, got {row['token_usage']}"
        )

    def test_token_usage_null_when_not_provided(self, tmp_path: Path) -> None:
        """
        complete_uow() without token_usage leaves the column NULL.

        NULL is the correct state for UoWs whose subagent did not report usage —
        it distinguishes 'not reported' from '0 tokens'.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, output_ref = _seed_executing_uow(registry, tmp_path)

        registry.complete_uow(uow_id, output_ref)

        row = _fetch_uow_row(db_path, uow_id)
        assert row["token_usage"] is None, (
            f"Expected token_usage=NULL when not provided, got {row['token_usage']}"
        )

    def test_existing_token_usage_not_overwritten_by_null(self, tmp_path: Path) -> None:
        """
        COALESCE semantics: a pre-existing non-NULL token_usage is not overwritten
        when complete_uow is called without token_usage.

        This covers the edge case where token_usage was written by a prior call
        (e.g. partial update) and a subsequent complete_uow with no usage data
        should not erase the existing value.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, output_ref = _seed_executing_uow(registry, tmp_path)

        # Pre-set token_usage directly
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET token_usage = ? WHERE id = ?",
            (LARGE_TOKEN_COUNT, uow_id),
        )
        conn.commit()
        conn.close()

        # complete_uow without token_usage — COALESCE should preserve existing value
        registry.complete_uow(uow_id, output_ref, token_usage=None)

        row = _fetch_uow_row(db_path, uow_id)
        assert row["token_usage"] == LARGE_TOKEN_COUNT, (
            f"COALESCE failed: expected {LARGE_TOKEN_COUNT}, got {row['token_usage']}"
        )

    def test_token_usage_in_audit_note(self, tmp_path: Path) -> None:
        """
        When token_usage is provided, it appears in the execution_complete audit note.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, output_ref = _seed_executing_uow(registry, tmp_path)

        registry.complete_uow(uow_id, output_ref, token_usage=SMALL_TOKEN_COUNT)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        audit_row = conn.execute(
            "SELECT note FROM audit_log WHERE uow_id = ? AND event = 'execution_complete'",
            (uow_id,),
        ).fetchone()
        conn.close()

        assert audit_row is not None, "execution_complete audit entry must exist"
        note = json.loads(audit_row["note"])
        assert note.get("token_usage") == SMALL_TOKEN_COUNT, (
            f"Expected token_usage in audit note, got: {note}"
        )

    def test_token_usage_absent_from_audit_note_when_not_provided(self, tmp_path: Path) -> None:
        """
        When token_usage is not provided, the key is absent from the audit note
        (not present as null — absence is the signal 'not reported').
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, output_ref = _seed_executing_uow(registry, tmp_path)

        registry.complete_uow(uow_id, output_ref)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        audit_row = conn.execute(
            "SELECT note FROM audit_log WHERE uow_id = ? AND event = 'execution_complete'",
            (uow_id,),
        ).fetchone()
        conn.close()

        assert audit_row is not None
        note = json.loads(audit_row["note"])
        assert "token_usage" not in note, (
            f"token_usage key should be absent when not provided, got: {note}"
        )

    def test_completed_at_written_on_complete_uow(self, tmp_path: Path) -> None:
        """
        complete_uow sets completed_at, which enables wall_clock_seconds computation.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, output_ref = _seed_executing_uow(registry, tmp_path)

        registry.complete_uow(uow_id, output_ref)

        row = _fetch_uow_row(db_path, uow_id)
        assert row["completed_at"] is not None, (
            "completed_at must be set by complete_uow to enable wall_clock_seconds"
        )


# ---------------------------------------------------------------------------
# Registry: wall_clock_seconds computed from timestamps
# ---------------------------------------------------------------------------

class TestWallClockSeconds:
    """wall_clock_seconds is computed at query time from started_at/completed_at."""

    def test_wall_clock_computed_from_timestamps(self, tmp_path: Path) -> None:
        """
        A UoW with both started_at and completed_at yields a non-NULL
        wall_clock_seconds when queried via the julianday expression.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, output_ref = _seed_executing_uow(registry, tmp_path)

        # Set started_at explicitly so we can compute expected delta
        started = datetime.now(timezone.utc) - timedelta(seconds=WALL_CLOCK_60S)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET started_at = ? WHERE id = ?",
            (started.isoformat(), uow_id),
        )
        conn.commit()
        conn.close()

        registry.complete_uow(uow_id, output_ref)

        # Query using the same expression as the metabolic digest
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT CASE
                WHEN completed_at IS NOT NULL AND started_at IS NOT NULL
                THEN CAST(
                    (julianday(completed_at) - julianday(started_at)) * 86400.0
                    AS INTEGER
                )
                ELSE NULL
            END AS wall_clock_seconds
            FROM uow_registry WHERE id = ?
            """,
            (uow_id,),
        ).fetchone()
        conn.close()

        assert row["wall_clock_seconds"] is not None, (
            "wall_clock_seconds should be non-NULL when both timestamps present"
        )
        # Allow ±2s tolerance for test execution time
        assert abs(row["wall_clock_seconds"] - WALL_CLOCK_60S) <= 2, (
            f"Expected wall_clock ~{WALL_CLOCK_60S}s, got {row['wall_clock_seconds']}s"
        )

    def test_wall_clock_null_when_started_at_missing(self, tmp_path: Path) -> None:
        """
        A UoW without started_at yields NULL wall_clock_seconds.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, output_ref = _seed_executing_uow(registry, tmp_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET started_at = NULL WHERE id = ?",
            (uow_id,),
        )
        conn.commit()
        conn.close()

        registry.complete_uow(uow_id, output_ref)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT CASE
                WHEN completed_at IS NOT NULL AND started_at IS NOT NULL
                THEN CAST(
                    (julianday(completed_at) - julianday(started_at)) * 86400.0
                    AS INTEGER
                )
                ELSE NULL
            END AS wall_clock_seconds
            FROM uow_registry WHERE id = ?
            """,
            (uow_id,),
        ).fetchone()
        conn.close()

        assert row["wall_clock_seconds"] is None, (
            "wall_clock_seconds must be NULL when started_at is missing"
        )


# ---------------------------------------------------------------------------
# wos_completion: maybe_complete_wos_uow forwards token_usage
# ---------------------------------------------------------------------------

class TestMaybeCompleteWosUowTokenUsage:
    """maybe_complete_wos_uow propagates token_usage to complete_uow."""

    def test_token_usage_forwarded_to_registry(self, tmp_path: Path) -> None:
        """
        maybe_complete_wos_uow(token_usage=N) results in token_usage=N in the registry.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, _ = _seed_executing_uow(registry, tmp_path)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with (
            patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}),
            patch("orchestration.wos_completion._write_steward_trigger"),
            patch("orchestration.wos_completion._backpropagate_result_to_output_file"),
            patch("orchestration.wos_completion._enrich_result_file"),
            patch("orchestration.wos_completion._post_closeout_comment_if_github"),
        ):
            maybe_complete_wos_uow(
                task_id,
                WRITE_RESULT_SUCCESS_STATUS,
                result_text="WOS test complete",
                token_usage=SMALL_TOKEN_COUNT,
            )

        row = _fetch_uow_row(db_path, uow_id)
        assert row["token_usage"] == SMALL_TOKEN_COUNT, (
            f"Expected token_usage={SMALL_TOKEN_COUNT} after maybe_complete, got {row['token_usage']}"
        )

    def test_token_usage_null_when_not_passed(self, tmp_path: Path) -> None:
        """
        maybe_complete_wos_uow without token_usage leaves the registry column NULL.
        """
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path)
        uow_id, _ = _seed_executing_uow(registry, tmp_path)
        task_id = f"{WOS_TASK_ID_PREFIX}{uow_id}"

        with (
            patch.dict(os.environ, {"REGISTRY_DB_PATH": str(db_path)}),
            patch("orchestration.wos_completion._write_steward_trigger"),
            patch("orchestration.wos_completion._backpropagate_result_to_output_file"),
            patch("orchestration.wos_completion._enrich_result_file"),
            patch("orchestration.wos_completion._post_closeout_comment_if_github"),
        ):
            maybe_complete_wos_uow(
                task_id,
                WRITE_RESULT_SUCCESS_STATUS,
                result_text="WOS test complete",
            )

        row = _fetch_uow_row(db_path, uow_id)
        assert row["token_usage"] is None, (
            f"Expected token_usage=NULL when not provided, got {row['token_usage']}"
        )


# ---------------------------------------------------------------------------
# Metabolic digest: aggregate_token_usage
# ---------------------------------------------------------------------------

class TestAggregateTokenUsage:
    """aggregate_token_usage correctly sums token counts across UoW dicts."""

    def setup_method(self):
        self.mod = _load_digest_module()

    def test_returns_sum_of_reporting_uows(self) -> None:
        """
        UoWs that reported token_usage are summed; the result equals COMBINED_TOKEN_COUNT.
        """
        uows = [
            {"token_usage": SMALL_TOKEN_COUNT, "id": "a"},
            {"token_usage": LARGE_TOKEN_COUNT, "id": "b"},
        ]
        result = self.mod.aggregate_token_usage(uows)
        assert result == COMBINED_TOKEN_COUNT, (
            f"Expected {COMBINED_TOKEN_COUNT}, got {result}"
        )

    def test_returns_none_when_all_null(self) -> None:
        """
        None is returned when all UoWs have NULL token_usage (no data available).
        """
        uows = [{"token_usage": None, "id": "a"}, {"id": "b"}]
        result = self.mod.aggregate_token_usage(uows)
        assert result is None, "Expected None when no UoWs reported token_usage"

    def test_partial_data_returns_partial_sum(self) -> None:
        """
        Only UoWs with non-NULL token_usage contribute to the sum.
        Partial data is better than no data — the sum excludes NULLs.
        """
        uows = [
            {"token_usage": SMALL_TOKEN_COUNT, "id": "a"},
            {"token_usage": None, "id": "b"},
        ]
        result = self.mod.aggregate_token_usage(uows)
        assert result == SMALL_TOKEN_COUNT, (
            f"Partial sum should equal {SMALL_TOKEN_COUNT}, got {result}"
        )

    def test_empty_list_returns_none(self) -> None:
        """An empty UoW list yields None (no data)."""
        result = self.mod.aggregate_token_usage([])
        assert result is None


# ---------------------------------------------------------------------------
# Metabolic digest: compute_wall_clock_stats
# ---------------------------------------------------------------------------

class TestComputeWallClockStats:
    """compute_wall_clock_stats returns correct count, total, and avg."""

    def setup_method(self):
        self.mod = _load_digest_module()

    def test_stats_from_two_uows(self) -> None:
        """
        Two UoWs with wall_clock_seconds yield the correct avg and count.
        """
        uows = [
            {"wall_clock_seconds": WALL_CLOCK_60S, "id": "a"},
            {"wall_clock_seconds": WALL_CLOCK_120S, "id": "b"},
        ]
        stats = self.mod.compute_wall_clock_stats(uows)
        assert stats["count"] == 2
        assert stats["total_seconds"] == WALL_CLOCK_60S + WALL_CLOCK_120S
        expected_avg = round((WALL_CLOCK_60S + WALL_CLOCK_120S) / 2)
        assert stats["avg_seconds"] == expected_avg

    def test_null_wall_clock_excluded(self) -> None:
        """UoWs with NULL wall_clock_seconds are excluded from stats."""
        uows = [
            {"wall_clock_seconds": WALL_CLOCK_60S, "id": "a"},
            {"wall_clock_seconds": None, "id": "b"},
        ]
        stats = self.mod.compute_wall_clock_stats(uows)
        assert stats["count"] == 1
        assert stats["avg_seconds"] == WALL_CLOCK_60S

    def test_all_null_returns_none_avg(self) -> None:
        """All-NULL input yields zero count and None avg."""
        uows = [{"wall_clock_seconds": None, "id": "a"}]
        stats = self.mod.compute_wall_clock_stats(uows)
        assert stats["count"] == 0
        assert stats["avg_seconds"] is None
        assert stats["total_seconds"] is None

    def test_empty_list_returns_zero_count(self) -> None:
        """Empty input yields zero count and None values."""
        stats = self.mod.compute_wall_clock_stats([])
        assert stats["count"] == 0
        assert stats["avg_seconds"] is None


# ---------------------------------------------------------------------------
# Metabolic digest: format_digest includes telemetry
# ---------------------------------------------------------------------------

class TestFormatDigestTelemetry:
    """format_digest includes token and wall-clock lines when data is present."""

    def setup_method(self):
        self.mod = _load_digest_module()

    def _make_groups(self, token_usage=None, wall_clock_seconds=None) -> dict:
        """Construct a groups dict with one pearl UoW carrying the given telemetry."""
        uow = {
            "id": "test-uow-1",
            "status": "done",
            "summary": "test",
            "register": "operational",
            "close_reason": "pr opened",
            "output_ref": "",
            "started_at": None,
            "closed_at": None,
            "updated_at": None,
            "created_at": None,
            "steward_cycles": 0,
            "token_usage": token_usage,
            "wall_clock_seconds": wall_clock_seconds,
        }
        return {
            "pearl": [uow],
            "heat": [],
            "seed": [],
            "shit": [],
        }

    def test_token_line_present_when_data_available(self) -> None:
        """
        format_digest includes a 'Tokens:' line when token_usage is available.
        """
        groups = self._make_groups(token_usage=SMALL_TOKEN_COUNT)
        result = self.mod.format_digest(groups, [], 24, "2026-04-27T00:00:00Z")
        assert "Tokens:" in result, f"Expected 'Tokens:' line in digest:\n{result}"
        assert f"{SMALL_TOKEN_COUNT:,}" in result

    def test_token_line_absent_when_no_data(self) -> None:
        """
        format_digest omits the 'Tokens:' line when no UoW reported token_usage.
        """
        groups = self._make_groups(token_usage=None)
        result = self.mod.format_digest(groups, [], 24, "2026-04-27T00:00:00Z")
        assert "Tokens:" not in result, f"'Tokens:' line should be absent:\n{result}"

    def test_wall_clock_line_present_when_data_available(self) -> None:
        """
        format_digest includes a 'Wall-clock avg:' line when wall_clock_seconds is available.
        """
        groups = self._make_groups(wall_clock_seconds=WALL_CLOCK_60S)
        result = self.mod.format_digest(groups, [], 24, "2026-04-27T00:00:00Z")
        assert "Wall-clock avg:" in result, f"Expected 'Wall-clock avg:' line:\n{result}"

    def test_wall_clock_line_absent_when_no_data(self) -> None:
        """
        format_digest omits wall-clock line when no UoW has wall_clock_seconds.
        """
        groups = self._make_groups(wall_clock_seconds=None)
        result = self.mod.format_digest(groups, [], 24, "2026-04-27T00:00:00Z")
        assert "Wall-clock avg:" not in result, f"'Wall-clock avg:' should be absent:\n{result}"

    def test_both_lines_present_when_full_data(self) -> None:
        """
        format_digest includes both telemetry lines when both values are available.
        """
        groups = self._make_groups(
            token_usage=SMALL_TOKEN_COUNT,
            wall_clock_seconds=WALL_CLOCK_60S,
        )
        result = self.mod.format_digest(groups, [], 24, "2026-04-27T00:00:00Z")
        assert "Tokens:" in result
        assert "Wall-clock avg:" in result

    def test_no_uow_window_omits_telemetry(self) -> None:
        """
        format_digest with zero UoWs omits telemetry lines (no data to show).
        """
        groups = {"pearl": [], "heat": [], "seed": [], "shit": []}
        result = self.mod.format_digest(groups, [], 24, "2026-04-27T00:00:00Z")
        assert "Tokens:" not in result
        assert "Wall-clock avg:" not in result
        assert "No UoWs completed" in result
