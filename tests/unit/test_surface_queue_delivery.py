"""
Tests for scheduled-tasks/surface-queue-delivery.py

Focuses on the apply_updates function and the source_id-less item bug fix.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Load the module from its script path (not a package)
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent
    / "scheduled-tasks"
    / "surface-queue-delivery.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("surface_queue_delivery", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sqd = _load_module()


# ---------------------------------------------------------------------------
# apply_updates: items with source_id (existing behaviour)
# ---------------------------------------------------------------------------

class TestApplyUpdatesWithSourceId:
    def _item(self, source_id: str, delivered: bool = False) -> dict:
        return {
            "source_id": source_id,
            "queued_at": "2026-03-25T12:00:00Z",
            "observation": "test",
            "delivered": delivered,
        }

    def test_marks_delivered_by_source_id(self):
        item = self._item("abc-123")
        result = sqd.apply_updates([item], delivered=[item], archived=[], timestamp="T1")
        assert result[0]["delivered"] is True
        assert result[0]["delivered_at"] == "T1"

    def test_marks_archived_by_source_id(self):
        item = self._item("abc-123")
        result = sqd.apply_updates([item], delivered=[], archived=[item], timestamp="T1")
        assert result[0].get("archived") is True
        assert result[0].get("archived_at") == "T1"

    def test_unmatched_item_unchanged(self):
        item = self._item("abc-123")
        other = self._item("xyz-999")
        result = sqd.apply_updates([item], delivered=[other], archived=[], timestamp="T1")
        assert result[0]["delivered"] is False
        assert result[0].get("delivered_at") is None

    def test_pure_does_not_mutate_original(self):
        item = self._item("abc-123")
        sqd.apply_updates([item], delivered=[item], archived=[], timestamp="T1")
        # Original dict must be untouched
        assert item["delivered"] is False
        assert "delivered_at" not in item


# ---------------------------------------------------------------------------
# apply_updates: items WITHOUT source_id (the bug fix)
# ---------------------------------------------------------------------------

class TestApplyUpdatesWithoutSourceId:
    def _item_no_sid(self, queued_at: str = "2026-03-24T00:00:00Z") -> dict:
        return {
            "queued_at": queued_at,
            "observation": "no source_id item",
            "delivered": False,
        }

    def test_marks_delivered_without_source_id(self):
        """Items without source_id must be marked delivered via object identity."""
        item = self._item_no_sid()
        result = sqd.apply_updates([item], delivered=[item], archived=[], timestamp="T2")
        assert result[0]["delivered"] is True
        assert result[0]["delivered_at"] == "T2"

    def test_marks_archived_without_source_id(self):
        item = self._item_no_sid()
        result = sqd.apply_updates([item], delivered=[], archived=[item], timestamp="T2")
        assert result[0].get("archived") is True

    def test_unselected_item_without_source_id_unchanged(self):
        """A different item without source_id must not be accidentally marked delivered."""
        item_a = self._item_no_sid("2026-03-24T00:00:00Z")
        item_b = self._item_no_sid("2026-03-24T01:00:00Z")
        result = sqd.apply_updates([item_a, item_b], delivered=[item_a], archived=[], timestamp="T2")
        assert result[0]["delivered"] is True
        assert result[1]["delivered"] is False

    def test_mixed_with_and_without_source_id(self):
        """Batch with some source_id, some not — all must be marked correctly."""
        with_sid = {"source_id": "abc", "queued_at": "2026-03-25T00:00:00Z", "observation": "x", "delivered": False}
        without_sid = {"queued_at": "2026-03-24T00:00:00Z", "observation": "y", "delivered": False}
        items = [with_sid, without_sid]
        result = sqd.apply_updates(
            items,
            delivered=[with_sid, without_sid],
            archived=[],
            timestamp="T3",
        )
        assert result[0]["delivered"] is True
        assert result[1]["delivered"] is True

    def test_null_source_id_treated_as_missing(self):
        """source_id: null should use identity fallback, not string matching."""
        item = {"source_id": None, "queued_at": "2026-03-24T00:00:00Z", "observation": "z", "delivered": False}
        result = sqd.apply_updates([item], delivered=[item], archived=[], timestamp="T4")
        assert result[0]["delivered"] is True


# ---------------------------------------------------------------------------
# select_items: integration with the delivery pipeline
# ---------------------------------------------------------------------------

class TestSelectItems:
    from datetime import datetime, timezone

    def test_selects_top_3_by_priority(self):
        from datetime import datetime, timezone
        now = datetime(2026, 3, 27, tzinfo=timezone.utc)
        items = [
            {"source_id": f"id-{i}", "queued_at": "2026-03-25T00:00:00Z",
             "observation": f"obs {i}", "delivered": False,
             "source_file": "meta/premise-review.md",
             "surface_reason": "Questioned verdict"}
            for i in range(5)
        ]
        to_deliver, to_archive = sqd.select_items(items, now)
        assert len(to_deliver) == 3
        assert len(to_archive) == 0

    def test_already_delivered_excluded(self):
        from datetime import datetime, timezone
        now = datetime(2026, 3, 27, tzinfo=timezone.utc)
        items = [
            {"source_id": "id-1", "queued_at": "2026-03-25T00:00:00Z",
             "observation": "obs", "delivered": True, "source_file": "meta/premise-review.md",
             "surface_reason": ""},
        ]
        to_deliver, to_archive = sqd.select_items(items, now)
        assert len(to_deliver) == 0


# ---------------------------------------------------------------------------
# format_digest: single digest message from multiple items
# ---------------------------------------------------------------------------

class TestFormatDigest:
    def _item(self, source_file: str, observation: str, surface_reason: str = "",
              queued_at: str = "2026-03-25T12:00:00Z") -> dict:
        return {
            "source_file": source_file,
            "observation": observation,
            "surface_reason": surface_reason,
            "queued_at": queued_at,
        }

    def test_empty_list_returns_empty_string(self):
        assert sqd.format_digest([]) == ""

    def test_single_item_contains_header_and_observation(self):
        item = self._item("meta/premise-review.md", "Core premise is off")
        result = sqd.format_digest([item])
        assert "Reflective Surface Digest" in result
        assert "Premise Review" in result
        assert "Core premise is off" in result

    def test_groups_by_source_type(self):
        items = [
            self._item("meta/premise-review.md", "Premise obs"),
            self._item("meta/hygiene-review.md", "Hygiene obs"),
        ]
        result = sqd.format_digest(items)
        assert "Premise Review" in result
        assert "Hygiene Review" in result
        assert "Premise obs" in result
        assert "Hygiene obs" in result

    def test_premise_review_before_hygiene_review(self):
        items = [
            self._item("meta/hygiene-review.md", "Hygiene obs"),
            self._item("meta/premise-review.md", "Premise obs"),
        ]
        result = sqd.format_digest(items)
        premise_pos = result.index("Premise Review")
        hygiene_pos = result.index("Hygiene Review")
        assert premise_pos < hygiene_pos

    def test_surface_reason_included_when_present(self):
        item = self._item("meta/hygiene-review.md", "The queue grew", surface_reason="Misaligned priority")
        result = sqd.format_digest([item])
        assert "Why surfaced: Misaligned priority" in result

    def test_surface_reason_absent_when_empty(self):
        item = self._item("meta/hygiene-review.md", "Routine check", surface_reason="")
        result = sqd.format_digest([item])
        assert "Why surfaced" not in result

    def test_queued_date_formatted(self):
        item = self._item("meta/premise-review.md", "obs", queued_at="2026-01-15T08:00:00Z")
        result = sqd.format_digest([item])
        assert "2026-01-15" in result

    def test_result_is_single_string(self):
        items = [
            self._item("meta/premise-review.md", "obs 1"),
            self._item("meta/premise-review.md", "obs 2"),
        ]
        result = sqd.format_digest(items)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_multiple_items_same_group(self):
        items = [
            self._item("meta/premise-review.md", "first obs"),
            self._item("meta/premise-review.md", "second obs"),
        ]
        result = sqd.format_digest(items)
        # Group header should appear once
        assert result.count("Premise Review") == 1
        assert "first obs" in result
        assert "second obs" in result
