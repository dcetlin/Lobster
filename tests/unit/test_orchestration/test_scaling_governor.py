"""
Tests for src/orchestration/scaling_governor.py.

Coverage:
1. No terminal UoWs → attunement_scale=0, cap fires, allowed_n = proposed_n // 5.
2. 8 terminal UoWs, 7 done (87.5% success) → attunement_scale=8, no cap when proposed_n <= 8.
3. 8 clean UoWs but proposed_n=50 → cap fires, allowed_n=10.
4. Override flag (scaling_governor_override: true) → capped=False even with 0 Attunement.
5. DB read failure → cap fires safely (no exception propagated).
6. proposed_n=1 → allowed_n=1 (minimum 1 enforced, never 0).
7. _compute_decision is importable and testable without a live DB.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.orchestration.scaling_governor import (
    GovernorDecision,
    ScalingGovernor,
    _compute_decision,
)


# ---------------------------------------------------------------------------
# Helpers — create a minimal registry DB with terminal UoW rows
# ---------------------------------------------------------------------------

_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS uow_registry (
    id          TEXT    PRIMARY KEY,
    status      TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
"""


def _make_db(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """
    Create a minimal registry DB at tmp_path/registry.db with the given rows.

    rows: list of (id, status) tuples — updated_at is set to a fixed timestamp.
    """
    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_REGISTRY_SCHEMA)
    for i, (row_id, status) in enumerate(rows):
        conn.execute(
            "INSERT INTO uow_registry (id, status, updated_at) VALUES (?, ?, ?)",
            (row_id, status, f"2026-04-23T10:{i:02d}:00Z"),
        )
    conn.commit()
    conn.close()
    return db_path


def _make_wos_config(tmp_path: Path, override: bool) -> Path:
    """Write a wos-config.json with scaling_governor_override set."""
    cfg_path = tmp_path / "wos-config.json"
    cfg_path.write_text(json.dumps({"scaling_governor_override": override}))
    return cfg_path


# ---------------------------------------------------------------------------
# _compute_decision — pure function tests (no DB needed)
# ---------------------------------------------------------------------------

class TestComputeDecision:
    def test_no_cap_when_attunement_meets_proposed(self) -> None:
        d = _compute_decision(proposed_n=8, attunement_scale=8)
        assert d.capped is False
        assert d.allowed_n == 8
        assert d.cap_reason is None

    def test_no_cap_when_attunement_exceeds_proposed(self) -> None:
        d = _compute_decision(proposed_n=4, attunement_scale=8)
        assert d.capped is False
        assert d.allowed_n == 4

    def test_cap_fires_when_attunement_below_proposed(self) -> None:
        d = _compute_decision(proposed_n=50, attunement_scale=8)
        assert d.capped is True
        assert d.allowed_n == 10  # 50 // 5

    def test_cap_reason_is_human_readable(self) -> None:
        d = _compute_decision(proposed_n=50, attunement_scale=8)
        assert "50" in d.cap_reason
        assert "8" in d.cap_reason
        assert "10" in d.cap_reason

    def test_minimum_allowed_n_is_1(self) -> None:
        d = _compute_decision(proposed_n=1, attunement_scale=0)
        assert d.allowed_n == 1

    def test_proposed_n_4_attunement_0_cap_fires(self) -> None:
        d = _compute_decision(proposed_n=4, attunement_scale=0)
        assert d.capped is True
        assert d.allowed_n == 1  # 4 // 5 = 0, floored to 1

    def test_fields_are_frozen(self) -> None:
        d = _compute_decision(proposed_n=8, attunement_scale=8)
        with pytest.raises(Exception):
            d.capped = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 1 — No terminal UoWs: attunement_scale=0, cap fires
# ---------------------------------------------------------------------------

class TestNoTerminalUoWs:
    def test_cap_fires_with_empty_db(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, [])
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=10)
        assert decision.attunement_scale == 0
        assert decision.capped is True
        assert decision.allowed_n == 2  # 10 // 5

    def test_allowed_n_minimum_1_with_proposed_1(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, [])
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=1)
        assert decision.allowed_n == 1


# ---------------------------------------------------------------------------
# Test 2 — 8 terminal UoWs, 7 done (87.5%): attunement_scale=8, no cap <= 8
# ---------------------------------------------------------------------------

class TestEightUoWsSevenDone:
    def _make_rows(self) -> list[tuple[str, str]]:
        """7 done + 1 failed = 8 rows, 87.5% success rate."""
        rows = [(f"uow-{i:03d}", "done") for i in range(7)]
        rows.append(("uow-007", "failed"))
        return rows

    def test_attunement_scale_is_8(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, self._make_rows())
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=8)
        assert decision.attunement_scale == 8

    def test_no_cap_when_proposed_le_8(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, self._make_rows())
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=8)
        assert decision.capped is False
        assert decision.allowed_n == 8

    def test_cap_fires_when_proposed_is_4(self, tmp_path: Path) -> None:
        # With proposed_n=4, only windows w=1,2,4 are checked (powers of 2 up to 4).
        # Most recent rows (DESC): failed, done, done, done, done, done, done, done.
        # w=1: 0/1=0% fails. w=2: 1/2=50% fails. w=4: 3/4=75% fails.
        # → attunement_scale=0 for proposed_n=4, cap fires.
        db_path = _make_db(tmp_path, self._make_rows())
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=4)
        # The governor only checks windows up to proposed_n, and windows <= 4 fail.
        assert decision.capped is True


# ---------------------------------------------------------------------------
# Test 3 — 8 clean UoWs, proposed_n=50: cap fires, allowed_n=10
# ---------------------------------------------------------------------------

class TestEightCleanUoWsWithLargeProposed:
    def _make_rows(self) -> list[tuple[str, str]]:
        return [(f"uow-{i:03d}", "done") for i in range(8)]

    def test_cap_fires_for_proposed_50(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, self._make_rows())
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=50)
        assert decision.capped is True
        assert decision.allowed_n == 10

    def test_attunement_scale_is_8(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, self._make_rows())
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=50)
        assert decision.attunement_scale == 8


# ---------------------------------------------------------------------------
# Test 4 — Override flag: capped=False even with 0 Attunement evidence
# ---------------------------------------------------------------------------

class TestOverrideFlag:
    def test_override_bypasses_cap(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, [])  # empty DB → attunement=0 normally
        cfg_path = _make_wos_config(tmp_path, override=True)
        governor = ScalingGovernor(db_path, wos_config_path=cfg_path)
        decision = governor.check(proposed_n=100)
        assert decision.capped is False
        assert decision.allowed_n == 100

    def test_override_still_reports_attunement_scale(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, [])
        cfg_path = _make_wos_config(tmp_path, override=True)
        governor = ScalingGovernor(db_path, wos_config_path=cfg_path)
        decision = governor.check(proposed_n=10)
        assert decision.attunement_scale == 0

    def test_no_override_applies_cap(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, [])
        cfg_path = _make_wos_config(tmp_path, override=False)
        governor = ScalingGovernor(db_path, wos_config_path=cfg_path)
        decision = governor.check(proposed_n=10)
        assert decision.capped is True


# ---------------------------------------------------------------------------
# Test 5 — DB read failure: cap fires safely, no exception propagated
# ---------------------------------------------------------------------------

class TestDbReadFailure:
    def test_nonexistent_db_does_not_raise(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent.db"
        cfg_path = _make_wos_config(tmp_path, override=False)
        governor = ScalingGovernor(db_path, wos_config_path=cfg_path)
        decision = governor.check(proposed_n=10)
        # Should not raise; should return a safe capped decision.
        assert isinstance(decision, GovernorDecision)
        assert decision.capped is True

    def test_corrupt_db_does_not_raise(self, tmp_path: Path) -> None:
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"not a sqlite database")
        cfg_path = _make_wos_config(tmp_path, override=False)
        governor = ScalingGovernor(db_path, wos_config_path=cfg_path)
        decision = governor.check(proposed_n=10)
        assert isinstance(decision, GovernorDecision)
        # attunement_scale=0 when DB fails
        assert decision.attunement_scale == 0


# ---------------------------------------------------------------------------
# Test 6 — proposed_n=1: allowed_n=1, never 0
# ---------------------------------------------------------------------------

class TestProposedNOfOne:
    def test_allowed_n_is_1_with_no_history(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path, [])
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=1)
        assert decision.allowed_n == 1

    def test_allowed_n_is_1_even_when_cap_applies(self, tmp_path: Path) -> None:
        # Cap: 1 // 5 = 0, floored to 1.
        db_path = _make_db(tmp_path, [])
        governor = ScalingGovernor(db_path)
        decision = governor.check(proposed_n=1)
        assert decision.allowed_n >= 1


# ---------------------------------------------------------------------------
# GovernorDecision is a frozen dataclass importable without a live DB
# ---------------------------------------------------------------------------

class TestGovernorDecisionImportable:
    def test_can_construct_without_db(self) -> None:
        d = GovernorDecision(
            proposed_n=10,
            allowed_n=2,
            capped=True,
            cap_reason="test",
            attunement_scale=0,
        )
        assert d.proposed_n == 10
        assert d.allowed_n == 2
        assert d.capped is True
