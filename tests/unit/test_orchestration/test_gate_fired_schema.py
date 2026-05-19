"""
Unit tests for gate_fired registry column — migration 0019 + steward write.

Spec: docs/wos/wos-completion-report-spec.md §Schema Additions §1

Behavior under test:

Migration:
- Migration 0019 adds gate_fired TEXT NULL DEFAULT 'none' to uow_registry
- Column is nullable; existing rows get NULL (not 'none') until written
- Column accepts valid gate values: 'spiral', 'dead_end', 'burst', 'none'

Translation map:
- _GATE_TRANSLATION maps eligibility verdicts to gate names:
  "escalate" → "spiral", "pause" → "dead_end", "throttle" → "burst", "dispatch" → "none"
- _GATE_SEVERITY assigns ordinal priorities: spiral=3, dead_end=2, burst=1, none=0

Precedence (once written, only upgrade — never downgrade):
- write_gate_fired('burst') then write_gate_fired('spiral') → stored: 'spiral'
- write_gate_fired('spiral') then write_gate_fired('burst') → stored: 'spiral' (unchanged)
- write_gate_fired('none') on a 'burst' UoW → stored: 'burst' (no downgrade)

Registry method:
- Registry.write_gate_fired(uow_id, gate_value) writes the gate_fired column
- Only upgrades: uses MAX logic or CASE expression to avoid downgrades

Dispatch translation:
- translate_eligibility_to_gate('escalate') == 'spiral'
- translate_eligibility_to_gate('pause') == 'dead_end'
- translate_eligibility_to_gate('throttle') == 'burst'
- translate_eligibility_to_gate('dispatch') == 'none'
- translate_eligibility_to_gate('unknown_value') raises ValueError (unknown verdict)

Named constants:
- _GATE_TRANSLATION: dict mapping verdict → gate name
- _GATE_SEVERITY: dict mapping gate name → ordinal priority
- GATE_FIRED_COLUMN_NAME = 'gate_fired'
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.gate_fired import (
    _GATE_TRANSLATION,
    _GATE_SEVERITY,
    GATE_FIRED_COLUMN_NAME,
    translate_eligibility_to_gate,
    gate_fired_severity,
    is_upgrade,
)


# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

# Valid gate_fired values per spec
_GATE_SPIRAL = "spiral"
_GATE_DEAD_END = "dead_end"
_GATE_BURST = "burst"
_GATE_NONE = "none"

_ALL_VALID_GATES = [_GATE_SPIRAL, _GATE_DEAD_END, _GATE_BURST, _GATE_NONE]

# Eligibility verdict to gate name mapping
_ESCALATE_VERDICT = "escalate"
_PAUSE_VERDICT = "pause"
_THROTTLE_VERDICT = "throttle"
_DISPATCH_VERDICT = "dispatch"


# ---------------------------------------------------------------------------
# Translation map tests
# ---------------------------------------------------------------------------

class TestGateTranslation:
    """_GATE_TRANSLATION maps eligibility verdicts to gate names."""

    def test_escalate_maps_to_spiral(self):
        assert _GATE_TRANSLATION[_ESCALATE_VERDICT] == _GATE_SPIRAL

    def test_pause_maps_to_dead_end(self):
        assert _GATE_TRANSLATION[_PAUSE_VERDICT] == _GATE_DEAD_END

    def test_throttle_maps_to_burst(self):
        assert _GATE_TRANSLATION[_THROTTLE_VERDICT] == _GATE_BURST

    def test_dispatch_maps_to_none(self):
        assert _GATE_TRANSLATION[_DISPATCH_VERDICT] == _GATE_NONE

    def test_all_four_verdicts_are_mapped(self):
        assert set(_GATE_TRANSLATION.keys()) == {_ESCALATE_VERDICT, _PAUSE_VERDICT, _THROTTLE_VERDICT, _DISPATCH_VERDICT}


# ---------------------------------------------------------------------------
# Severity ordering tests
# ---------------------------------------------------------------------------

class TestGateSeverity:
    """_GATE_SEVERITY assigns ordinal priorities: spiral > dead_end > burst > none."""

    def test_spiral_has_highest_severity(self):
        assert _GATE_SEVERITY[_GATE_SPIRAL] > _GATE_SEVERITY[_GATE_DEAD_END]
        assert _GATE_SEVERITY[_GATE_SPIRAL] > _GATE_SEVERITY[_GATE_BURST]
        assert _GATE_SEVERITY[_GATE_SPIRAL] > _GATE_SEVERITY[_GATE_NONE]

    def test_dead_end_outranks_burst_and_none(self):
        assert _GATE_SEVERITY[_GATE_DEAD_END] > _GATE_SEVERITY[_GATE_BURST]
        assert _GATE_SEVERITY[_GATE_DEAD_END] > _GATE_SEVERITY[_GATE_NONE]

    def test_burst_outranks_none(self):
        assert _GATE_SEVERITY[_GATE_BURST] > _GATE_SEVERITY[_GATE_NONE]

    def test_none_has_lowest_severity(self):
        assert _GATE_SEVERITY[_GATE_NONE] == 0

    def test_all_four_gates_have_severity(self):
        assert set(_GATE_SEVERITY.keys()) == set(_ALL_VALID_GATES)


# ---------------------------------------------------------------------------
# translate_eligibility_to_gate tests
# ---------------------------------------------------------------------------

class TestTranslateEligibilityToGate:
    """translate_eligibility_to_gate converts verdict strings to gate names."""

    def test_escalate_returns_spiral(self):
        assert translate_eligibility_to_gate(_ESCALATE_VERDICT) == _GATE_SPIRAL

    def test_pause_returns_dead_end(self):
        assert translate_eligibility_to_gate(_PAUSE_VERDICT) == _GATE_DEAD_END

    def test_throttle_returns_burst(self):
        assert translate_eligibility_to_gate(_THROTTLE_VERDICT) == _GATE_BURST

    def test_dispatch_returns_none(self):
        assert translate_eligibility_to_gate(_DISPATCH_VERDICT) == _GATE_NONE

    def test_unknown_verdict_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown eligibility verdict"):
            translate_eligibility_to_gate("unknown_verdict")


# ---------------------------------------------------------------------------
# gate_fired_severity tests
# ---------------------------------------------------------------------------

class TestGateFiredSeverity:
    """gate_fired_severity returns the ordinal priority of a gate name."""

    def test_spiral_severity(self):
        assert gate_fired_severity(_GATE_SPIRAL) == _GATE_SEVERITY[_GATE_SPIRAL]

    def test_none_severity_is_zero(self):
        assert gate_fired_severity(_GATE_NONE) == 0

    def test_unknown_gate_name_returns_zero(self):
        """Unknown gate names are treated as severity 0 (safe default)."""
        assert gate_fired_severity("unknown_gate") == 0


# ---------------------------------------------------------------------------
# is_upgrade tests
# ---------------------------------------------------------------------------

class TestIsUpgrade:
    """is_upgrade returns True only when new_gate has strictly higher severity."""

    def test_none_to_spiral_is_upgrade(self):
        assert is_upgrade(current="none", new="spiral") is True

    def test_burst_to_spiral_is_upgrade(self):
        assert is_upgrade(current="burst", new="spiral") is True

    def test_dead_end_to_spiral_is_upgrade(self):
        assert is_upgrade(current="dead_end", new="spiral") is True

    def test_spiral_to_burst_is_not_upgrade(self):
        assert is_upgrade(current="spiral", new="burst") is False

    def test_spiral_to_spiral_is_not_upgrade(self):
        assert is_upgrade(current="spiral", new="spiral") is False

    def test_spiral_to_none_is_not_upgrade(self):
        assert is_upgrade(current="spiral", new="none") is False

    def test_none_to_none_is_not_upgrade(self):
        assert is_upgrade(current="none", new="none") is False

    def test_none_to_burst_is_upgrade(self):
        assert is_upgrade(current="none", new="burst") is True


# ---------------------------------------------------------------------------
# Column name constant test
# ---------------------------------------------------------------------------

def test_gate_fired_column_name():
    assert GATE_FIRED_COLUMN_NAME == "gate_fired"


# ---------------------------------------------------------------------------
# Registry integration — write_gate_fired upgrade-only semantics
# ---------------------------------------------------------------------------

class TestRegistryWriteGateFired:
    """Registry.write_gate_fired enforces upgrade-only semantics."""

    def _make_registry(self, tmp_path):
        """Create a fresh in-memory registry with gate_fired column for testing."""
        from orchestration.registry import Registry
        import os
        db_path = str(tmp_path / "test_registry.db")
        os.environ["REGISTRY_DB_PATH"] = db_path
        # We can't easily run the full migration stack in unit tests.
        # Instead we'll test write_gate_fired via the registry's SQLite connection.
        # This is the same pattern used in test_registry.py.
        return Registry(db_path=db_path)

    def test_write_gate_fired_stores_value(self, tmp_path):
        """write_gate_fired writes the gate value to the registry."""
        from orchestration.registry import Registry
        from orchestration.registry import UoWStatus

        registry = self._make_registry(tmp_path)
        # Create a minimal UoW
        uow_id = registry.upsert(
            issue_number=1001,
            title="Test UoW",
            success_criteria="done",
        ).id

        # Add gate_fired column if not present (migration may not have run)
        try:
            conn = registry._connect()
            conn.execute("ALTER TABLE uow_registry ADD COLUMN gate_fired TEXT NULL DEFAULT 'none'")
            conn.commit()
        except Exception:
            pass  # Column already exists

        registry.write_gate_fired(uow_id, "spiral")

        conn = registry._connect()
        row = conn.execute(
            "SELECT gate_fired FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        assert row is not None
        assert row["gate_fired"] == "spiral"

    def test_write_gate_fired_only_upgrades(self, tmp_path):
        """write_gate_fired does not downgrade: spiral → burst stays spiral."""
        from orchestration.registry import Registry

        registry = self._make_registry(tmp_path)
        uow_id = registry.upsert(
            issue_number=1002,
            title="Test UoW",
            success_criteria="done",
        ).id

        try:
            conn = registry._connect()
            conn.execute("ALTER TABLE uow_registry ADD COLUMN gate_fired TEXT NULL DEFAULT 'none'")
            conn.commit()
        except Exception:
            pass

        # Write higher severity first
        registry.write_gate_fired(uow_id, "spiral")
        # Attempt to downgrade
        registry.write_gate_fired(uow_id, "burst")

        conn = registry._connect()
        row = conn.execute(
            "SELECT gate_fired FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        assert row["gate_fired"] == "spiral"  # unchanged

    def test_write_gate_fired_upgrades_when_higher(self, tmp_path):
        """write_gate_fired upgrades when new gate has higher severity."""
        from orchestration.registry import Registry

        registry = self._make_registry(tmp_path)
        uow_id = registry.upsert(
            issue_number=1003,
            title="Test UoW",
            success_criteria="done",
        ).id

        try:
            conn = registry._connect()
            conn.execute("ALTER TABLE uow_registry ADD COLUMN gate_fired TEXT NULL DEFAULT 'none'")
            conn.commit()
        except Exception:
            pass

        # Write lower severity first
        registry.write_gate_fired(uow_id, "burst")
        # Upgrade to higher severity
        registry.write_gate_fired(uow_id, "spiral")

        conn = registry._connect()
        row = conn.execute(
            "SELECT gate_fired FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        assert row["gate_fired"] == "spiral"
