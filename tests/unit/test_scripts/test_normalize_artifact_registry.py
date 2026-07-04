"""
Unit tests for the pure normalize_registry() function in
scripts/normalize-artifact-registry.py.

Migration 135 (scripts/upgrade.sh) calls this script on every upgrade run,
not just once — it must be idempotent (a no-op, reporting unchanged, once a
registry is already in canonical shape) rather than existence-gated. This
replaces the original PR #1463 migration, whose `if [ ! -f ... ]` guard was a
permanent no-op on any machine where a registry already existed under a
different schema (oracle verdict on pr-1463.md, finding #2).

Canonical shape (Implementation A, live):
    {
      "_meta": {
        "version", "created", "last_reconciled", "invariant",
        "classifications": {...}, "staleness_thresholds": {...}
      },
      "artifacts": [ {id, artifact_class, path, owner, state,
                       last_activity, convergence_target, expiry, notes} ]
    }

Named after behaviors, not mechanisms.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).parents[3] / "scripts" / "normalize-artifact-registry.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "normalize_artifact_registry", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


normalize_mod = _load_module()
normalize_registry = normalize_mod.normalize_registry
from src.utils.artifact_registry import (  # noqa: E402
    DEFAULT_CLASSIFICATIONS,
    DEFAULT_STALENESS_THRESHOLDS,
)


def test_already_canonical_registry_is_left_unchanged():
    data = {
        "_meta": {
            "version": "1",
            "created": "2026-07-04",
            "last_reconciled": "2026-07-04",
            "invariant": "Every artifact converges...",
            "classifications": DEFAULT_CLASSIFICATIONS,
            "staleness_thresholds": DEFAULT_STALENESS_THRESHOLDS,
        },
        "artifacts": [
            {
                "id": "workstreams/foo",
                "artifact_class": "workstream",
                "path": "~/lobster-workspace/workstreams/foo/",
                "owner": "Dan",
                "state": "active_wip",
                "last_activity": "2026-07-01",
                "convergence_target": "ship it",
                "expiry": "2026-08-01",
                "notes": "",
            }
        ],
    }
    result, changed = normalize_registry(data)
    assert changed is False
    assert result == data


def test_missing_staleness_thresholds_are_added():
    data = {
        "_meta": {
            "version": "1",
            "created": "2026-07-04",
            "last_reconciled": "2026-07-04",
            "invariant": "inv",
            "classifications": DEFAULT_CLASSIFICATIONS,
        },
        "artifacts": [],
    }
    result, changed = normalize_registry(data)
    assert changed is True
    assert result["_meta"]["staleness_thresholds"] == DEFAULT_STALENESS_THRESHOLDS


def test_flat_b_schema_is_wrapped_into_meta():
    """B's schema (PR #1463) used top-level _schema_version/_state_law/etc.
    instead of A's nested _meta wrapper. Normalize must fold these into
    _meta and drop the flat keys, without touching `artifacts`."""
    data = {
        "_schema_version": "1.0",
        "_description": "Global artifact registry",
        "_state_law": {"seed": "...", "cadence": "...", "active_wip": "...", "orphan": "..."},
        "_last_reconciled": "2026-07-04",
        "_reconciled_by": "initial-population",
        "artifacts": [{"id": "workstreams/bar", "state": "seed"}],
    }
    result, changed = normalize_registry(data)
    assert changed is True
    assert "_meta" in result
    assert result["_meta"]["last_reconciled"] == "2026-07-04"
    assert result["_meta"]["classifications"] == data["_state_law"]
    assert result["_meta"]["staleness_thresholds"] == DEFAULT_STALENESS_THRESHOLDS
    assert "_schema_version" not in result
    assert "_state_law" not in result
    assert "_last_reconciled" not in result
    assert "_reconciled_by" not in result
    # artifacts data is preserved untouched aside from the added `path` key
    assert result["artifacts"][0]["id"] == "workstreams/bar"
    assert result["artifacts"][0]["state"] == "seed"


def test_entries_missing_path_field_get_path_none():
    data = {
        "_meta": {
            "version": "1",
            "created": "2026-07-04",
            "last_reconciled": "2026-07-04",
            "invariant": "inv",
            "classifications": DEFAULT_CLASSIFICATIONS,
            "staleness_thresholds": DEFAULT_STALENESS_THRESHOLDS,
        },
        "artifacts": [
            {"id": "workstreams/foo", "state": "seed"},
            {"id": "workstreams/bar", "state": "seed", "path": "already/set"},
        ],
    }
    result, changed = normalize_registry(data)
    assert changed is True
    assert result["artifacts"][0]["path"] is None
    assert result["artifacts"][1]["path"] == "already/set"


def test_artifacts_array_is_never_overwritten_wholesale():
    """Existing artifact data (state, notes, expiry, etc.) must survive
    normalization untouched — only missing fields are backfilled."""
    data = {
        "_schema_version": "1.0",
        "_state_law": DEFAULT_CLASSIFICATIONS,
        "artifacts": [
            {
                "id": "workstreams/foo",
                "state": "orphan",
                "notes": "auto-transitioned orphan 2026-06-01",
                "owner": "unowned",
            }
        ],
    }
    result, changed = normalize_registry(data)
    assert changed is True
    entry = result["artifacts"][0]
    assert entry["state"] == "orphan"
    assert entry["notes"] == "auto-transitioned orphan 2026-06-01"
    assert entry["owner"] == "unowned"
    assert entry["path"] is None  # backfilled, not data loss


def test_empty_registry_seed_is_canonical_shape():
    result = normalize_mod.empty_registry(created="2026-07-04")
    assert result["_meta"]["staleness_thresholds"] == DEFAULT_STALENESS_THRESHOLDS
    assert result["_meta"]["classifications"] == DEFAULT_CLASSIFICATIONS
    assert result["artifacts"] == []
