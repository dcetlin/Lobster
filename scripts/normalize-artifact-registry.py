#!/usr/bin/env python3
"""
normalize-artifact-registry.py — schema-normalizing migration for
data/artifact-registry.json.

Replaces the original PR #1463 Migration 135, whose `if [ ! -f ... ]` guard
made it a permanent no-op on any machine where a registry already existed
under a different schema (oracle verdict pr-1463.md, blocking finding #2).
This script runs on every upgrade, checks the registry's actual shape, and
normalizes it in place — a true migration, not an existence check.

Canonical shape (Implementation A, live — see CANON.md):
    {
      "_meta": {
        "version", "created", "last_reconciled", "invariant",
        "classifications": {seed, cadence, active_wip, orphan},
        "staleness_thresholds": {workstream_active_wip_days,
                                 repo_active_wip_days, cadence_missed_cycles}
      },
      "artifacts": [ {id, artifact_class, path, owner, state,
                       last_activity, convergence_target, expiry, notes} ]
    }

Normalization performed (idempotent — safe to run every upgrade):
  1. If the file is absent, seed a minimal empty-but-canonically-shaped
     registry (no populated template — a fresh install should not inherit
     another workspace's workstream/repo names).
  2. If present but using B's flat schema (top-level `_schema_version` /
     `_state_law` / `_last_reconciled` / `_reconciled_by` instead of a
     `_meta` wrapper), fold those into `_meta` and drop the flat keys.
     `artifacts` data is never overwritten wholesale.
  3. If `_meta.staleness_thresholds` or `_meta.classifications` is missing,
     add the CANON.md-documented defaults (60/60/2 days; four-state law).
  4. If any artifact entry is missing the `path` field, add `path: null`
     (backfill only — never touches existing field values).

Usage:
    uv run scripts/normalize-artifact-registry.py [path-to-registry.json]

Exit code 0 always (idempotent no-op is success, not failure). Prints
"CHANGED" or "UNCHANGED" to stdout so callers (scripts/upgrade.sh) can decide
whether to count this as an applied migration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.artifact_registry import (  # noqa: E402
    DEFAULT_CLASSIFICATIONS,
    DEFAULT_INVARIANT,
    DEFAULT_STALENESS_THRESHOLDS,
)

# Flat top-level keys from PR #1463's schema (Implementation B) that get
# folded into `_meta` and removed.
_B_SCHEMA_KEYS = ("_schema_version", "_description", "_state_law", "_last_reconciled", "_reconciled_by")

_ARTIFACT_FIELD_DEFAULTS = {
    "path": None,
}


def empty_registry(created: str) -> dict:
    """A minimal, canonically-shaped registry for fresh installs."""
    return {
        "_meta": {
            "version": "1",
            "created": created,
            "last_reconciled": created,
            "invariant": DEFAULT_INVARIANT,
            "classifications": dict(DEFAULT_CLASSIFICATIONS),
            "staleness_thresholds": dict(DEFAULT_STALENESS_THRESHOLDS),
        },
        "artifacts": [],
    }


def normalize_registry(data: dict) -> tuple[dict, bool]:
    """Return (normalized_data, changed). Never mutates `data` in place.

    `artifacts` entries are backfilled field-by-field, never replaced —
    existing state/notes/expiry/etc. values are preserved exactly.
    """
    changed = False
    result = dict(data)

    # Step 1: fold B's flat schema into a _meta wrapper, if present.
    if "_meta" not in result and any(k in result for k in _B_SCHEMA_KEYS):
        meta = {
            "version": "1",
            "created": result.get("_last_reconciled") or "",
            "last_reconciled": result.get("_last_reconciled") or "",
            "invariant": result.get("_description") or DEFAULT_INVARIANT,
            "classifications": result.get("_state_law") or dict(DEFAULT_CLASSIFICATIONS),
            "staleness_thresholds": dict(DEFAULT_STALENESS_THRESHOLDS),
        }
        result["_meta"] = meta
        for key in _B_SCHEMA_KEYS:
            result.pop(key, None)
        changed = True

    # Step 2: ensure _meta exists at all (absent-file case handled by caller
    # via empty_registry(); this covers a malformed/partial file).
    if "_meta" not in result:
        result["_meta"] = {
            "version": "1",
            "created": "",
            "last_reconciled": "",
            "invariant": DEFAULT_INVARIANT,
            "classifications": dict(DEFAULT_CLASSIFICATIONS),
            "staleness_thresholds": dict(DEFAULT_STALENESS_THRESHOLDS),
        }
        changed = True
    else:
        meta = dict(result["_meta"])
        if "staleness_thresholds" not in meta:
            meta["staleness_thresholds"] = dict(DEFAULT_STALENESS_THRESHOLDS)
            changed = True
        if "classifications" not in meta:
            meta["classifications"] = dict(DEFAULT_CLASSIFICATIONS)
            changed = True
        result["_meta"] = meta

    # Step 3: backfill missing per-entry fields (currently: `path`).
    artifacts = result.get("artifacts", [])
    normalized_artifacts = []
    for entry in artifacts:
        new_entry = dict(entry)
        for field, default in _ARTIFACT_FIELD_DEFAULTS.items():
            if field not in new_entry:
                new_entry[field] = default
                changed = True
        normalized_artifacts.append(new_entry)
    result["artifacts"] = normalized_artifacts

    return result, changed


def main(argv: list[str]) -> int:
    import os
    from datetime import datetime, timezone

    default_path = Path(
        os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")
    ) / "data" / "artifact-registry.json"
    registry_path = Path(argv[1]) if len(argv) > 1 else default_path

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not registry_path.exists():
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps(empty_registry(today), indent=2))
        print(f"CHANGED: seeded new empty registry at {registry_path}")
        return 0

    data = json.loads(registry_path.read_text())
    normalized, changed = normalize_registry(data)

    if not changed:
        print(f"UNCHANGED: {registry_path} already canonical")
        return 0

    normalized["_meta"]["last_reconciled"] = today
    registry_path.write_text(json.dumps(normalized, indent=2))
    print(f"CHANGED: normalized {registry_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
