"""
analytics.py — Prescription quality tracking for the WOS steward pipeline.

The steward_log field on each UoW is a newline-delimited JSON log of every
Steward decision point. Prescription events carry a ``prescription_path``
field that records whether the LLM or deterministic fallback path was used.

This module exposes a single public function:

    prescription_quality_summary(registry_path?) -> dict

It queries uow_registry, parses each steward_log, and returns:

  {
    "per_uow": [
      {
        "id":            str,          # UoW UUID
        "summary":       str,          # issue title / summary
        "status":        str,          # final/current status
        "steward_cycles": int,         # total steward cycles from DB column
        "prescription_paths": list[str],  # ordered "llm"/"fallback" per cycle
        "llm_count":     int,
        "fallback_count": int,
      },
      ...
    ],
    "aggregate": {
      "total_uows":         int,
      "uows_with_data":     int,   # UoWs that have >=1 prescription event
      "avg_cycles_to_done": float | None,
      "pct_llm":            float | None,   # 0–100
      "pct_fallback":       float | None,   # 0–100
      "total_prescriptions": int,
      "llm_prescriptions":   int,
      "fallback_prescriptions": int,
    },
    "data_gap": str | None,  # human-readable note if data is too sparse
  }

Design notes:
- Pure function composition: each concern (connect, query, parse, aggregate)
  is a separate function. prescription_quality_summary() composes them.
- No writes. Read-only connection.
- Graceful on empty/missing DB: returns empty results with a data_gap note.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _default_registry_path() -> Path:
    """Return the canonical registry DB path, matching audit_queries.py."""
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    return Path(workspace) / "orchestration" / "registry.db"


def _connect_ro(registry_path: Path) -> sqlite3.Connection:
    """Open a read-only WAL connection, matching audit_queries.py pattern."""
    conn = sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _fetch_uow_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return all UoW rows with only the fields needed for quality analysis."""
    rows = conn.execute(
        """
        SELECT id, summary, status, steward_cycles, steward_log
        FROM uow_registry
        ORDER BY id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _parse_prescription_paths(steward_log_raw: str | None) -> list[str]:
    """
    Extract ordered prescription_path values from a newline-delimited
    steward_log string.

    Returns a list of "llm" or "fallback" strings in cycle order.
    Events with no ``prescription_path`` key are skipped.
    """
    if not steward_log_raw:
        return []

    paths: list[str] = []
    for line in steward_log_raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        # Only prescription events carry prescription_path
        if entry.get("event") in ("prescription", "reentry_prescription"):
            path = entry.get("prescription_path")
            if path in ("llm", "fallback"):
                paths.append(path)
    return paths


def _build_per_uow_record(row: dict) -> dict[str, Any]:
    """Combine DB row fields with parsed steward_log data into a per-UoW dict."""
    paths = _parse_prescription_paths(row.get("steward_log"))
    llm_count = paths.count("llm")
    fallback_count = paths.count("fallback")
    return {
        "id": row["id"],
        "summary": row.get("summary", ""),
        "status": row.get("status", ""),
        "steward_cycles": row.get("steward_cycles", 0),
        "prescription_paths": paths,
        "llm_count": llm_count,
        "fallback_count": fallback_count,
    }


def _compute_aggregate(per_uow: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Compute aggregate quality metrics from per-UoW records.

    avg_cycles_to_done: mean steward_cycles across UoWs with status='done'.
    pct_llm / pct_fallback: share of LLM vs fallback across all prescriptions.
    """
    total_uows = len(per_uow)
    uows_with_data = sum(1 for r in per_uow if r["prescription_paths"])

    done_cycles = [
        r["steward_cycles"]
        for r in per_uow
        if r["status"] == "done" and r["steward_cycles"] is not None
    ]
    avg_cycles_to_done: float | None = (
        sum(done_cycles) / len(done_cycles) if done_cycles else None
    )

    total_prescriptions = sum(r["llm_count"] + r["fallback_count"] for r in per_uow)
    llm_prescriptions = sum(r["llm_count"] for r in per_uow)
    fallback_prescriptions = sum(r["fallback_count"] for r in per_uow)

    pct_llm: float | None = None
    pct_fallback: float | None = None
    if total_prescriptions > 0:
        pct_llm = round(100 * llm_prescriptions / total_prescriptions, 1)
        pct_fallback = round(100 * fallback_prescriptions / total_prescriptions, 1)

    return {
        "total_uows": total_uows,
        "uows_with_data": uows_with_data,
        "avg_cycles_to_done": avg_cycles_to_done,
        "pct_llm": pct_llm,
        "pct_fallback": pct_fallback,
        "total_prescriptions": total_prescriptions,
        "llm_prescriptions": llm_prescriptions,
        "fallback_prescriptions": fallback_prescriptions,
    }


def _data_gap_note(aggregate: dict[str, Any]) -> str | None:
    """
    Return a human-readable explanation if the data is too sparse to be
    meaningful, or None if the data is sufficient.
    """
    if aggregate["total_uows"] == 0:
        return (
            "No UoWs found in registry. Run the steward at least once "
            "to populate prescription data."
        )
    if aggregate["uows_with_data"] == 0:
        return (
            f"{aggregate['total_uows']} UoW(s) exist but none have steward "
            "prescription events yet. The steward writes prescription_path "
            "on each diagnosis/prescription cycle; data will appear after "
            "the first steward heartbeat processes a UoW."
        )
    if aggregate["total_prescriptions"] < 3:
        return (
            f"Only {aggregate['total_prescriptions']} prescription event(s) "
            "recorded across all UoWs. Metrics will be more meaningful once "
            "more cycles have completed."
        )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def prescription_quality_summary(
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """
    Query uow_registry and return prescription quality metrics.

    Parameters
    ----------
    registry_path:
        Path to the registry SQLite DB. Defaults to the canonical path
        resolved from REGISTRY_DB_PATH or LOBSTER_WORKSPACE env vars.

    Returns
    -------
    dict with keys:
        per_uow   — list of per-UoW quality records
        aggregate — cross-UoW summary metrics
        data_gap  — human-readable note when data is sparse, else None
    """
    path = registry_path if registry_path is not None else _default_registry_path()

    if not path.exists():
        empty_aggregate = _compute_aggregate([])
        return {
            "per_uow": [],
            "aggregate": empty_aggregate,
            "data_gap": (
                f"Registry DB not found at {path}. "
                "Run the WOS steward at least once to create the database."
            ),
        }

    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError as exc:
        return {
            "per_uow": [],
            "aggregate": _compute_aggregate([]),
            "data_gap": f"Could not open registry DB: {exc}",
        }

    try:
        rows = _fetch_uow_rows(conn)
    except sqlite3.OperationalError as exc:
        conn.close()
        return {
            "per_uow": [],
            "aggregate": _compute_aggregate([]),
            "data_gap": f"Could not query uow_registry: {exc}",
        }
    finally:
        conn.close()

    per_uow = [_build_per_uow_record(row) for row in rows]
    aggregate = _compute_aggregate(per_uow)
    data_gap = _data_gap_note(aggregate)

    return {
        "per_uow": per_uow,
        "aggregate": aggregate,
        "data_gap": data_gap,
    }
