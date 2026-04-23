"""
tests/test_vision_inlet.py

Smoke tests for the Vision Object inlet mechanism.

Tests:
  1. A proposal targeting current_focus.this_week.primary passes validation.
  2. A proposal targeting vision.long_term_direction (ineligible field) is rejected.
  3. A proposal with identical value to the current field is rejected as no-op.
  4. The accept handler correctly writes the new value to a temp copy of vision.yaml.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixture: temp vision.yaml copy for tests that write to it
# ---------------------------------------------------------------------------

VISION_YAML_PATH = Path.home() / "lobster-user-config" / "vision.yaml"


@pytest.fixture
def tmp_vision_yaml(tmp_path: Path) -> Path:
    """Copy the real vision.yaml to a temp file for write tests."""
    src = VISION_YAML_PATH
    if not src.exists():
        pytest.skip("vision.yaml not found at expected path — skipping write tests.")
    dest = tmp_path / "vision.yaml"
    shutil.copy(src, dest)
    return dest


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch):
    """
    Redirect vision_inlet module's data paths to tmp_path so tests
    do not write to production JSONL files.
    """
    import src.harvest.vision_inlet as vi

    monkeypatch.setattr(vi, "PENDING_JSON", tmp_path / "vision-proposals-pending.json")
    monkeypatch.setattr(vi, "ACCEPTED_JSONL", tmp_path / "vision-proposals-accepted.jsonl")
    monkeypatch.setattr(vi, "DISCARD_JSONL", tmp_path / "vision-proposals-discard.jsonl")
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_vision_yaml() -> dict:
    """Load the real vision.yaml to get current field values."""
    return yaml.safe_load(VISION_YAML_PATH.read_text())


def _current_primary() -> str:
    """Return the current value of current_focus.this_week.primary."""
    data = _load_vision_yaml()
    return str(data.get("current_focus", {}).get("this_week", {}).get("primary", ""))


# ---------------------------------------------------------------------------
# Test 1: proposal targeting current_focus.this_week.primary passes validation
# ---------------------------------------------------------------------------


def test_valid_proposal_current_focus(tmp_data_dir):
    """A proposal targeting current_focus.this_week.primary is accepted."""
    from src.harvest.vision_inlet import validate_vision_proposals

    if not VISION_YAML_PATH.exists():
        pytest.skip("vision.yaml not found.")

    # Use a value different from the current one to avoid no-op rejection
    current = _current_primary()
    proposed = "Test proposal value that is different from current"
    if proposed == current:
        proposed = proposed + " (modified for test)"

    proposals = [
        {
            "field_path": "current_focus.this_week.primary",
            "proposed_value": proposed,
            "basis": "Smoke test proposal.",
            "source_session": "2026-04-22-test",
        }
    ]

    valid, rejected = validate_vision_proposals(proposals, vision_yaml_path=VISION_YAML_PATH)

    assert len(valid) == 1, f"Expected 1 valid proposal, got {len(valid)}. Rejected: {rejected}"
    assert len(rejected) == 0, f"Expected 0 rejected, got {rejected}"
    assert valid[0]["field_path"] == "current_focus.this_week.primary"


# ---------------------------------------------------------------------------
# Test 2: proposal targeting ineligible field is rejected
# ---------------------------------------------------------------------------


def test_ineligible_field_is_rejected(tmp_data_dir):
    """A proposal targeting vision.long_term_direction (ineligible) is rejected."""
    from src.harvest.vision_inlet import validate_vision_proposals

    if not VISION_YAML_PATH.exists():
        pytest.skip("vision.yaml not found.")

    proposals = [
        {
            "field_path": "vision.long_term_direction",
            "proposed_value": "Some new direction",
            "basis": "Test basis.",
            "source_session": "2026-04-22-test",
        }
    ]

    valid, rejected = validate_vision_proposals(proposals, vision_yaml_path=VISION_YAML_PATH)

    assert len(valid) == 0, f"Expected 0 valid proposals, got {len(valid)}"
    assert len(rejected) == 1, f"Expected 1 rejected proposal, got {len(rejected)}"
    assert "eligible" in rejected[0].get("rejection_reason", "").lower(), (
        f"Expected 'eligible' in rejection_reason, got: {rejected[0]}"
    )

    # Verify it was written to discard log
    discard_log = tmp_data_dir / "vision-proposals-discard.jsonl"
    assert discard_log.exists(), "Discard log should have been written."
    entries = [json.loads(line) for line in discard_log.read_text().strip().splitlines()]
    assert len(entries) == 1
    assert entries[0]["field_path"] == "vision.long_term_direction"


# ---------------------------------------------------------------------------
# Test 3: proposal with identical value is rejected as no-op
# ---------------------------------------------------------------------------


def test_noop_proposal_is_rejected(tmp_data_dir):
    """A proposal with identical value to current field is rejected as no-op."""
    from src.harvest.vision_inlet import validate_vision_proposals

    if not VISION_YAML_PATH.exists():
        pytest.skip("vision.yaml not found.")

    current = _current_primary()
    if not current:
        pytest.skip("current_focus.this_week.primary is empty — cannot test no-op.")

    proposals = [
        {
            "field_path": "current_focus.this_week.primary",
            "proposed_value": current,
            "basis": "Testing no-op detection.",
            "source_session": "2026-04-22-test",
        }
    ]

    valid, rejected = validate_vision_proposals(proposals, vision_yaml_path=VISION_YAML_PATH)

    assert len(valid) == 0, f"Expected 0 valid (no-op), got {len(valid)}"
    assert len(rejected) == 1, f"Expected 1 rejected, got {len(rejected)}"
    assert "no-op" in rejected[0].get("rejection_reason", "").lower(), (
        f"Expected 'no-op' in rejection_reason, got: {rejected[0]}"
    )


# ---------------------------------------------------------------------------
# Test 4: accept handler writes new value to temp copy of vision.yaml
# ---------------------------------------------------------------------------


def test_accept_handler_writes_vision_yaml(tmp_vision_yaml, tmp_data_dir):
    """The accept handler correctly writes the new value to a temp copy of vision.yaml."""
    from src.harvest.vision_inlet import (
        handle_vision_accept,
        proposal_hash,
        _save_pending,
    )

    field_path = "current_focus.this_week.primary"
    proposed_value = "Acceptance test value — written by test_vision_inlet"
    phash = proposal_hash(field_path, proposed_value)

    # Pre-populate pending with the proposal
    proposal = {
        "field_path": field_path,
        "proposed_value": proposed_value,
        "basis": "Acceptance test.",
        "source_session": "2026-04-22-test",
        "sent_at": "2026-04-22T00:00:00+00:00",
    }

    import src.harvest.vision_inlet as vi

    # Temporarily redirect PENDING_JSON to tmp_data_dir
    original_pending = vi.PENDING_JSON
    try:
        _save_pending({phash: proposal})

        result = handle_vision_accept(
            field_path=field_path,
            phash=phash,
            chat_id=6036,
            vision_yaml_path=tmp_vision_yaml,
        )

        # Check reply
        assert "vision.yaml updated" in result, f"Unexpected result: {result}"
        assert field_path in result, f"field_path not in result: {result}"

        # Check vision.yaml was written
        written_data = yaml.safe_load(tmp_vision_yaml.read_text())
        path_parts = field_path.split(".")
        current = written_data
        for part in path_parts:
            current = current[part]
        assert current == proposed_value, (
            f"Expected '{proposed_value}', got '{current}'"
        )

        # Check pending is empty after accept
        pending_data = json.loads(vi.PENDING_JSON.read_text()) if vi.PENDING_JSON.exists() else {}
        assert phash not in pending_data, "Hash should be removed from pending after accept."

        # Check accepted log has the entry
        accepted_log = tmp_data_dir / "vision-proposals-accepted.jsonl"
        assert accepted_log.exists(), "Accepted log should exist."
        entries = [json.loads(line) for line in accepted_log.read_text().strip().splitlines()]
        assert len(entries) == 1
        assert entries[0]["field_path"] == field_path

    finally:
        vi.PENDING_JSON = original_pending


# ---------------------------------------------------------------------------
# Test 5: decline handler removes from pending and writes to discard log
# ---------------------------------------------------------------------------


def test_decline_handler_discards_proposal(tmp_data_dir):
    """The decline handler removes the proposal from pending and records in discard log."""
    from src.harvest.vision_inlet import (
        handle_vision_decline,
        proposal_hash,
        _save_pending,
    )
    import src.harvest.vision_inlet as vi

    field_path = "current_focus.this_week.secondary"
    proposed_value = "Test decline value"
    phash = proposal_hash(field_path, proposed_value)

    proposal = {
        "field_path": field_path,
        "proposed_value": proposed_value,
        "basis": "Decline test.",
        "source_session": "2026-04-22-test",
        "sent_at": "2026-04-22T00:00:00+00:00",
    }

    _save_pending({phash: proposal})

    result = handle_vision_decline(field_path=field_path, phash=phash, chat_id=6036)

    assert "Declined" in result, f"Expected 'Declined' in result: {result}"

    # Check pending is empty
    pending_data = json.loads(vi.PENDING_JSON.read_text()) if vi.PENDING_JSON.exists() else {}
    assert phash not in pending_data, "Hash should be removed from pending after decline."

    # Check discard log
    discard_log = tmp_data_dir / "vision-proposals-discard.jsonl"
    assert discard_log.exists(), "Discard log should exist after decline."
    entries = [json.loads(line) for line in discard_log.read_text().strip().splitlines()]
    assert len(entries) == 1
    assert entries[0]["rejection_reason"] == "user_declined"
