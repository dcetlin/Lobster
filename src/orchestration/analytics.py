"""
analytics.py — Prescription quality and pipeline observability for WOS.

The steward_log field on each UoW is a newline-delimited JSON log of every
Steward decision point. Prescription events carry a ``prescription_path``
field that records whether the LLM or deterministic fallback path was used.
Trace injection events carry a ``gate_score`` dict with a convergence score.

This module exposes three public functions:

    prescription_quality_summary(registry_path?) -> dict
    convergence_metrics(registry_path?) -> dict
    diagnostic_accuracy(registry_path?) -> dict

And a CLI entry point (``main()``) that accepts subcommands:
    quality, convergence, diagnostic, all

Design notes:
- Pure function composition: each concern (connect, query, parse, aggregate)
  is a separate function. Public functions compose them.
- No writes. Read-only connection.
- Graceful on empty/missing DB: returns empty results with a data_gap note.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
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


# ---------------------------------------------------------------------------
# convergence_metrics — internal helpers
# ---------------------------------------------------------------------------

# Minimum tail window for stall detection: >= this many consecutive
# non-improving scores at the end of a trajectory qualifies as stalled.
_STALL_TAIL_LENGTH = 3

# A UoW is considered converged if its final gate score meets this threshold.
_CONVERGENCE_SCORE_THRESHOLD = 0.8


def _parse_gate_scores(steward_log_raw: str | None) -> list[float]:
    """
    Extract ordered gate_score values from trace_injection events in a
    newline-delimited steward_log string.

    Returns a list of floats in cycle order. Events without gate_score are
    skipped. Malformed JSON lines are silently skipped.
    """
    if not steward_log_raw:
        return []

    scores: list[float] = []
    for line in steward_log_raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get("event") != "trace_injection":
            continue
        gate_score = entry.get("gate_score")
        if isinstance(gate_score, dict):
            score = gate_score.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
        elif isinstance(gate_score, (int, float)):
            scores.append(float(gate_score))
    return scores


def _is_stalled(scores: list[float]) -> bool:
    """
    Return True if the tail of the score trajectory shows >= _STALL_TAIL_LENGTH
    consecutive non-improving scores.

    Non-improving means each score is <= the previous one. This is a local
    helper; do not import stall detection from the steward.
    """
    if len(scores) < _STALL_TAIL_LENGTH:
        return False
    tail = scores[-_STALL_TAIL_LENGTH:]
    return all(tail[i] <= tail[i - 1] for i in range(1, len(tail)))


def _build_convergence_record(row: dict) -> dict[str, Any]:
    """Build a per-UoW convergence record from a uow_registry row."""
    scores = _parse_gate_scores(row.get("steward_log"))
    status = row.get("status", "")

    converged = (
        (scores and scores[-1] >= _CONVERGENCE_SCORE_THRESHOLD)
        or status == "done"
    )

    # cycles_to_converge: index of first score crossing threshold, or None
    cycles_to_converge: int | None = None
    for i, s in enumerate(scores):
        if s >= _CONVERGENCE_SCORE_THRESHOLD:
            cycles_to_converge = i + 1  # 1-indexed cycle count
            break

    score_delta: float | None = (
        round(scores[-1] - scores[0], 4)
        if len(scores) >= 2
        else None
    )

    return {
        "id": row["id"],
        "summary": row.get("summary", ""),
        "status": status,
        "score_trajectory": scores,
        "converged": converged,
        "cycles_to_converge": cycles_to_converge,
        "score_delta": score_delta,
    }


def _compute_convergence_aggregate(per_uow: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate convergence metrics from per-UoW records."""
    tracked = [r for r in per_uow if r["score_trajectory"]]
    total_tracked = len(tracked)
    converged = [r for r in tracked if r["converged"]]
    convergence_rate = len(converged) / total_tracked if total_tracked > 0 else None

    cycles_list = [
        r["cycles_to_converge"]
        for r in converged
        if r["cycles_to_converge"] is not None
    ]
    avg_cycles_to_converge: float | None = (
        round(sum(cycles_list) / len(cycles_list), 2) if cycles_list else None
    )

    deltas = [r["score_delta"] for r in tracked if r["score_delta"] is not None]
    avg_score_delta: float | None = (
        round(sum(deltas) / len(deltas), 4) if deltas else None
    )

    stalled_count = sum(1 for r in tracked if _is_stalled(r["score_trajectory"]))

    return {
        "total_tracked": total_tracked,
        "convergence_rate": round(convergence_rate, 4) if convergence_rate is not None else None,
        "avg_cycles_to_converge": avg_cycles_to_converge,
        "avg_score_delta": avg_score_delta,
        "stalled_count": stalled_count,
    }


# ---------------------------------------------------------------------------
# diagnostic_accuracy — internal helpers
# ---------------------------------------------------------------------------

def _fetch_audit_diagnosis_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return all steward_diagnosis and steward_prescription audit entries."""
    rows = conn.execute(
        """
        SELECT id, ts, uow_id, event, from_status, to_status, agent, note
        FROM audit_log
        WHERE event IN ('steward_diagnosis', 'steward_prescription')
        ORDER BY ts ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_terminal_outcome_rows(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {uow_id: event} for the latest terminal event per UoW."""
    rows = conn.execute(
        """
        SELECT uow_id, event
        FROM audit_log
        WHERE id IN (
            SELECT MAX(id)
            FROM audit_log
            WHERE event IN ('execution_complete', 'execution_failed')
            GROUP BY uow_id
        )
        """
    ).fetchall()
    return {row["uow_id"]: row["event"] for row in rows}


def _build_diagnostic_per_uow(
    diagnosis_rows: list[dict],
    outcomes: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Build per-UoW diagnostic records by grouping diagnosis events and
    cross-referencing with terminal outcomes.
    """
    # Group by uow_id while preserving insertion order
    by_uow: dict[str, list[dict]] = {}
    for row in diagnosis_rows:
        by_uow.setdefault(row["uow_id"], []).append(row)

    result = []
    for uow_id, events in by_uow.items():
        outcome = outcomes.get(uow_id)
        result.append({
            "uow_id": uow_id,
            "diagnosis_count": len(events),
            "outcome": outcome,  # "execution_complete" | "execution_failed" | None
        })
    return result


def _compute_diagnostic_summary(per_uow: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate diagnostic accuracy across all UoWs."""
    total_diagnoses = sum(r["diagnosis_count"] for r in per_uow)
    followed_by_success = sum(
        1 for r in per_uow if r["outcome"] == "execution_complete"
    )
    followed_by_failure = sum(
        1 for r in per_uow if r["outcome"] == "execution_failed"
    )
    pending = sum(1 for r in per_uow if r["outcome"] is None)
    resolved = followed_by_success + followed_by_failure
    success_rate: float | None = (
        round(followed_by_success / resolved, 4) if resolved > 0 else None
    )
    return {
        "total_diagnoses": total_diagnoses,
        "followed_by_success": followed_by_success,
        "followed_by_failure": followed_by_failure,
        "pending": pending,
        "success_rate": success_rate,
    }


# ---------------------------------------------------------------------------
# Public API — convergence_metrics
# ---------------------------------------------------------------------------

def convergence_metrics(
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """
    Query uow_registry and return convergence metrics derived from
    trace_injection gate scores in each UoW's steward_log.

    Parameters
    ----------
    registry_path:
        Path to the registry SQLite DB. Defaults to the canonical path
        resolved from REGISTRY_DB_PATH or LOBSTER_WORKSPACE env vars.

    Returns
    -------
    dict with keys:
        per_uow   — list of per-UoW convergence records, each with:
                    id, summary, status, score_trajectory, converged,
                    cycles_to_converge, score_delta
        aggregate — cross-UoW summary with:
                    total_tracked, convergence_rate, avg_cycles_to_converge,
                    avg_score_delta, stalled_count
    """
    path = registry_path if registry_path is not None else _default_registry_path()

    if not path.exists():
        return {
            "per_uow": [],
            "aggregate": _compute_convergence_aggregate([]),
        }

    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return {
            "per_uow": [],
            "aggregate": _compute_convergence_aggregate([]),
        }

    try:
        rows = _fetch_uow_rows(conn)
    except sqlite3.OperationalError:
        return {
            "per_uow": [],
            "aggregate": _compute_convergence_aggregate([]),
        }
    finally:
        conn.close()

    per_uow = [_build_convergence_record(row) for row in rows]
    aggregate = _compute_convergence_aggregate(per_uow)
    return {"per_uow": per_uow, "aggregate": aggregate}


# ---------------------------------------------------------------------------
# Public API — diagnostic_accuracy
# ---------------------------------------------------------------------------

def diagnostic_accuracy(
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """
    Query audit_log for steward_diagnosis / steward_prescription events,
    cross-reference with terminal outcomes, and return diagnostic accuracy
    metrics.

    Parameters
    ----------
    registry_path:
        Path to the registry SQLite DB.

    Returns
    -------
    dict with keys:
        summary  — aggregate metrics:
                   total_diagnoses, followed_by_success, followed_by_failure,
                   pending, success_rate (float 0–1 or None)
        per_uow  — list of per-UoW records:
                   uow_id, diagnosis_count, outcome
    """
    path = registry_path if registry_path is not None else _default_registry_path()

    if not path.exists():
        return {
            "summary": _compute_diagnostic_summary([]),
            "per_uow": [],
        }

    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return {
            "summary": _compute_diagnostic_summary([]),
            "per_uow": [],
        }

    try:
        diagnosis_rows = _fetch_audit_diagnosis_rows(conn)
        outcomes = _fetch_terminal_outcome_rows(conn)
    except sqlite3.OperationalError:
        return {
            "summary": _compute_diagnostic_summary([]),
            "per_uow": [],
        }
    finally:
        conn.close()

    per_uow = _build_diagnostic_per_uow(diagnosis_rows, outcomes)
    summary = _compute_diagnostic_summary(per_uow)
    return {"summary": summary, "per_uow": per_uow}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    analytics_report CLI — print WOS analytics as JSON to stdout.

    Subcommands:
        quality      prescription_quality_summary()
        convergence  convergence_metrics()
        diagnostic   diagnostic_accuracy()
        all          all three metrics under their respective keys (default)

    Options:
        --db PATH    override the default registry DB path
    """
    parser = argparse.ArgumentParser(
        prog="analytics_report",
        description="WOS pipeline analytics report (JSON output)",
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=["quality", "convergence", "diagnostic", "all"],
        default="all",
        help="Which metrics to report (default: all)",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="Path to registry DB (overrides REGISTRY_DB_PATH and LOBSTER_WORKSPACE)",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None

    if args.subcommand == "quality":
        result = prescription_quality_summary(db_path)
    elif args.subcommand == "convergence":
        result = convergence_metrics(db_path)
    elif args.subcommand == "diagnostic":
        result = diagnostic_accuracy(db_path)
    else:  # "all"
        result = {
            "quality": prescription_quality_summary(db_path),
            "convergence": convergence_metrics(db_path),
            "diagnostic": diagnostic_accuracy(db_path),
        }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
