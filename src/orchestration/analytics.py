"""
analytics.py — Prescription quality and pipeline observability for WOS.

The steward_log field on each UoW is a newline-delimited JSON log of every
Steward decision point. Prescription events carry a ``prescription_path``
field that records whether the LLM or deterministic fallback path was used.
Trace injection events carry a ``gate_score`` dict with a convergence score.

This module exposes seven public functions:

    prescription_quality_summary(registry_path?) -> dict
    convergence_metrics(registry_path?) -> dict
    diagnostic_accuracy(registry_path?) -> dict
    execution_fidelity_summary(registry_path?) -> dict
    diagnostic_accuracy_summary(registry_path?) -> dict
    convergence_summary(registry_path?) -> dict
    complexity_appropriateness_summary(registry_path?) -> dict

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
import statistics
import sys
from datetime import datetime, timezone
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
# execution_fidelity_summary — internal helpers
# ---------------------------------------------------------------------------

# Named constants derived from the spec
_EXECUTION_COMPLETE_EVENT = "execution_complete"
_EXECUTION_FAILED_EVENT = "execution_failed"
_EXECUTOR_DISPATCH_EVENT = "executor_dispatch"
_STEWARD_DIAGNOSIS_EVENT = "steward_diagnosis"
_REENTRY_PRESCRIPTION_EVENT = "reentry_prescription"

# Operational register UoWs with more cycles than this are flagged as
# potentially over-complex in complexity_appropriateness_summary.
_OPERATIONAL_COMPLEXITY_THRESHOLD = 3


def _fetch_execution_audit_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return all execution-related audit events: complete, failed, dispatch."""
    rows = conn.execute(
        """
        SELECT id, ts, uow_id, event, from_status, to_status, agent, note
        FROM audit_log
        WHERE event IN (?, ?, ?)
        ORDER BY id ASC
        """,
        (
            _EXECUTION_COMPLETE_EVENT,
            _EXECUTION_FAILED_EVENT,
            _EXECUTOR_DISPATCH_EVENT,
        ),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_rediagnosis_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return steward_diagnosis and reentry_prescription events."""
    rows = conn.execute(
        """
        SELECT id, ts, uow_id, event
        FROM audit_log
        WHERE event IN (?, ?)
        ORDER BY id ASC
        """,
        (_STEWARD_DIAGNOSIS_EVENT, _REENTRY_PRESCRIPTION_EVENT),
    ).fetchall()
    return [dict(r) for r in rows]


def _build_fidelity_per_uow(
    exec_rows: list[dict],
    rediag_rows: list[dict],
) -> list[dict[str, Any]]:
    """
    Build per-UoW execution fidelity records.

    For each UoW that has execution events: count attempts, determine final
    outcome, and detect whether re-diagnosis occurred after a failure.
    """
    # Group execution events by uow_id
    by_uow: dict[str, list[dict]] = {}
    for row in exec_rows:
        by_uow.setdefault(row["uow_id"], []).append(row)

    # Build set of uow_ids that had rediagnosis (any steward_diagnosis or
    # reentry_prescription event after an execution_failed)
    failed_uow_ids: set[str] = set()
    for row in exec_rows:
        if row["event"] == _EXECUTION_FAILED_EVENT:
            failed_uow_ids.add(row["uow_id"])

    rediag_uow_ids: set[str] = set()
    for row in rediag_rows:
        if row["uow_id"] in failed_uow_ids:
            rediag_uow_ids.add(row["uow_id"])

    result = []
    for uow_id, events in by_uow.items():
        dispatch_events = [e for e in events if e["event"] == _EXECUTOR_DISPATCH_EVENT]
        fail_events = [e for e in events if e["event"] == _EXECUTION_FAILED_EVENT]
        complete_events = [e for e in events if e["event"] == _EXECUTION_COMPLETE_EVENT]

        # Final outcome is the last terminal event
        terminal = [e for e in events if e["event"] in (_EXECUTION_COMPLETE_EVENT, _EXECUTION_FAILED_EVENT)]
        final_outcome: str | None = terminal[-1]["event"] if terminal else None

        result.append({
            "uow_id": uow_id,
            "execution_attempts": len(dispatch_events),
            "failure_count": len(fail_events),
            "success_count": len(complete_events),
            "final_outcome": final_outcome,
            "re_diagnosis_occurred": uow_id in rediag_uow_ids,
        })
    return result


def _compute_fidelity_aggregate(per_uow: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate execution fidelity rates across UoWs."""
    total = len(per_uow)
    if total == 0:
        return {
            "total_executions": 0,
            "success_rate": None,
            "failure_rate": None,
            "re_diagnosis_rate": None,
        }
    resolved = [r for r in per_uow if r["final_outcome"] is not None]
    successes = [r for r in resolved if r["final_outcome"] == _EXECUTION_COMPLETE_EVENT]
    failures = [r for r in resolved if r["final_outcome"] == _EXECUTION_FAILED_EVENT]
    rediagnosed = [r for r in per_uow if r["re_diagnosis_occurred"]]
    total_resolved = len(resolved)
    return {
        "total_executions": total,
        "success_rate": round(len(successes) / total_resolved, 4) if total_resolved > 0 else None,
        "failure_rate": round(len(failures) / total_resolved, 4) if total_resolved > 0 else None,
        "re_diagnosis_rate": round(len(rediagnosed) / total, 4) if total > 0 else None,
    }


def execution_fidelity_summary(
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """
    Measure how often executors succeed vs. fail vs. require re-diagnosis.

    Queries audit_log for execution_complete, execution_failed, and
    executor_dispatch events. Per-UoW counts are combined into aggregate
    success_rate, failure_rate, and re_diagnosis_rate.

    Parameters
    ----------
    registry_path:
        Path to the registry SQLite DB. Defaults to the canonical path
        resolved from REGISTRY_DB_PATH or LOBSTER_WORKSPACE env vars.

    Returns
    -------
    dict with keys:
        per_uow   — list of per-UoW fidelity records
        aggregate — {total_executions, success_rate, failure_rate, re_diagnosis_rate}
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    if not path.exists():
        return {"per_uow": [], "aggregate": _compute_fidelity_aggregate([])}
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return {"per_uow": [], "aggregate": _compute_fidelity_aggregate([])}
    try:
        exec_rows = _fetch_execution_audit_rows(conn)
        rediag_rows = _fetch_rediagnosis_rows(conn)
    except sqlite3.OperationalError:
        return {"per_uow": [], "aggregate": _compute_fidelity_aggregate([])}
    finally:
        conn.close()

    per_uow = _build_fidelity_per_uow(exec_rows, rediag_rows)
    aggregate = _compute_fidelity_aggregate(per_uow)
    return {"per_uow": per_uow, "aggregate": aggregate}


# ---------------------------------------------------------------------------
# diagnostic_accuracy_summary — internal helpers
# ---------------------------------------------------------------------------

def _fetch_prescription_audit_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return all prescription and reentry_prescription audit events."""
    rows = conn.execute(
        """
        SELECT id, ts, uow_id, event
        FROM audit_log
        WHERE event IN ('prescription', 'reentry_prescription')
        ORDER BY id ASC
        """,
    ).fetchall()
    return [dict(r) for r in rows]


def _build_first_attempt_success(
    prescription_rows: list[dict],
    exec_rows: list[dict],
) -> list[dict[str, Any]]:
    """
    Determine first-execution-attempt outcome per UoW.

    A first attempt is successful if execution_complete follows the first
    prescription event without an intervening execution_failed.
    """
    # Build ordered event id -> event for each uow
    by_uow_prescriptions: dict[str, list[dict]] = {}
    for row in prescription_rows:
        by_uow_prescriptions.setdefault(row["uow_id"], []).append(row)

    by_uow_exec: dict[str, list[dict]] = {}
    for row in exec_rows:
        if row["event"] in (_EXECUTION_COMPLETE_EVENT, _EXECUTION_FAILED_EVENT):
            by_uow_exec.setdefault(row["uow_id"], []).append(row)

    result = []
    for uow_id, presc_events in by_uow_prescriptions.items():
        first_presc_id = presc_events[0]["id"]
        exec_events = by_uow_exec.get(uow_id, [])
        # Events after the first prescription, in order
        post_presc = [e for e in exec_events if e["id"] > first_presc_id]
        if not post_presc:
            # No execution events yet — pending
            result.append({"uow_id": uow_id, "first_attempt_success": None})
            continue
        first_exec = post_presc[0]
        # First attempt is a success if no failure precedes the first complete
        first_failure = next(
            (e for e in post_presc if e["event"] == _EXECUTION_FAILED_EVENT), None
        )
        first_complete = next(
            (e for e in post_presc if e["event"] == _EXECUTION_COMPLETE_EVENT), None
        )
        if first_complete and (first_failure is None or first_complete["id"] < first_failure["id"]):
            success = True
        else:
            success = False
        result.append({"uow_id": uow_id, "first_attempt_success": success})
    return result


def _compute_first_attempt_aggregate(per_uow: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate first-attempt success rate."""
    total_diagnosed = len(per_uow)
    resolved = [r for r in per_uow if r["first_attempt_success"] is not None]
    successes = [r for r in resolved if r["first_attempt_success"] is True]
    return {
        "total_diagnosed": total_diagnosed,
        "successful_first_attempt_count": len(successes),
        "first_attempt_success_rate": (
            round(len(successes) / len(resolved), 4) if resolved else None
        ),
    }


def diagnostic_accuracy_summary(
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """
    Measure how often steward prescriptions lead to successful closure on
    the first execution attempt.

    A first attempt is successful when execution_complete follows the first
    prescription/reentry_prescription event without an intervening
    execution_failed.

    Parameters
    ----------
    registry_path:
        Path to the registry SQLite DB.

    Returns
    -------
    dict with keys:
        per_uow   — list of {uow_id, first_attempt_success: bool | None}
        aggregate — {total_diagnosed, successful_first_attempt_count,
                     first_attempt_success_rate}
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    if not path.exists():
        return {"per_uow": [], "aggregate": _compute_first_attempt_aggregate([])}
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return {"per_uow": [], "aggregate": _compute_first_attempt_aggregate([])}
    try:
        presc_rows = _fetch_prescription_audit_rows(conn)
        exec_rows = _fetch_execution_audit_rows(conn)
    except sqlite3.OperationalError:
        return {"per_uow": [], "aggregate": _compute_first_attempt_aggregate([])}
    finally:
        conn.close()

    per_uow = _build_first_attempt_success(presc_rows, exec_rows)
    aggregate = _compute_first_attempt_aggregate(per_uow)
    return {"per_uow": per_uow, "aggregate": aggregate}


# ---------------------------------------------------------------------------
# convergence_summary — internal helpers
# ---------------------------------------------------------------------------

def _fetch_completed_uow_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return completed UoWs with timing and cycle fields."""
    rows = conn.execute(
        """
        SELECT id, steward_cycles, created_at, completed_at
        FROM uow_registry
        WHERE status = 'done'
          AND steward_cycles IS NOT NULL
        ORDER BY id ASC
        """,
    ).fetchall()
    return [dict(r) for r in rows]


def _wall_clock_hours(created_at: str | None, completed_at: str | None) -> float | None:
    """Return wall-clock hours between two ISO timestamps, or None if either is missing."""
    if not created_at or not completed_at:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        # Strip timezone suffix for fromisoformat compatibility
        def _parse(ts: str) -> datetime:
            ts = ts.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                # Fallback: strip tz and treat as UTC
                return datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)
        start = _parse(created_at)
        end = _parse(completed_at)
        delta = (end - start).total_seconds()
        return round(delta / 3600, 4)
    except (ValueError, TypeError):
        return None


def _compute_convergence_summary_aggregate(
    per_uow: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate cycle and wall-clock duration stats for completed UoWs."""
    cycles = [r["steward_cycles"] for r in per_uow if r["steward_cycles"] is not None]
    hours = [r["wall_clock_hours"] for r in per_uow if r["wall_clock_hours"] is not None]

    if not cycles:
        return {
            "avg_cycles_to_done": None,
            "median_cycles": None,
            "p90_cycles": None,
            "max_cycles": None,
            "avg_wall_clock_hours": None,
            "outlier_uow_ids": [],
        }

    avg_cycles = round(sum(cycles) / len(cycles), 4)
    median_c = statistics.median(cycles)
    sorted_cycles = sorted(cycles)
    p90_index = max(0, int(len(sorted_cycles) * 0.9) - 1)
    p90_c = sorted_cycles[p90_index]
    max_c = max(cycles)
    avg_hours = round(sum(hours) / len(hours), 4) if hours else None

    # Outliers: UoWs with steward_cycles > 2 × median
    outlier_threshold = 2 * median_c
    outlier_ids = [
        r["uow_id"]
        for r in per_uow
        if r["steward_cycles"] is not None and r["steward_cycles"] > outlier_threshold
    ]

    return {
        "avg_cycles_to_done": avg_cycles,
        "median_cycles": median_c,
        "p90_cycles": p90_c,
        "max_cycles": max_c,
        "avg_wall_clock_hours": avg_hours,
        "outlier_uow_ids": outlier_ids,
    }


def convergence_summary(
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """
    Measure cycles to UoW closure and identify outliers among completed UoWs.

    Only considers UoWs with status='done'. Outliers are UoWs whose
    steward_cycles exceed 2× the median cycle count.

    Parameters
    ----------
    registry_path:
        Path to the registry SQLite DB.

    Returns
    -------
    dict with keys:
        per_uow   — list of {uow_id, steward_cycles, wall_clock_hours}
        aggregate — {avg_cycles_to_done, median_cycles, p90_cycles, max_cycles,
                     avg_wall_clock_hours, outlier_uow_ids}
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    if not path.exists():
        return {"per_uow": [], "aggregate": _compute_convergence_summary_aggregate([])}
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return {"per_uow": [], "aggregate": _compute_convergence_summary_aggregate([])}
    try:
        rows = _fetch_completed_uow_rows(conn)
    except sqlite3.OperationalError:
        return {"per_uow": [], "aggregate": _compute_convergence_summary_aggregate([])}
    finally:
        conn.close()

    per_uow = [
        {
            "uow_id": r["id"],
            "steward_cycles": r["steward_cycles"],
            "wall_clock_hours": _wall_clock_hours(r["created_at"], r["completed_at"]),
        }
        for r in rows
    ]
    aggregate = _compute_convergence_summary_aggregate(per_uow)
    return {"per_uow": per_uow, "aggregate": aggregate}


# ---------------------------------------------------------------------------
# complexity_appropriateness_summary — internal helpers
# ---------------------------------------------------------------------------

def _fetch_uow_complexity_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return UoW rows with register, type, steward_cycles, and steward_log."""
    rows = conn.execute(
        """
        SELECT id, register, type, steward_cycles, steward_log
        FROM uow_registry
        ORDER BY id ASC
        """,
    ).fetchall()
    return [dict(r) for r in rows]


def _classify_prescription_path(steward_log_raw: str | None) -> str:
    """
    Return 'llm', 'fallback', or 'unknown' based on the dominant prescription
    path in the steward_log.

    'llm' wins if any prescription event used the LLM path.
    'fallback' if all prescription events used fallback.
    'unknown' if no prescription events exist.
    """
    paths = _parse_prescription_paths(steward_log_raw)
    if not paths:
        return "unknown"
    if "llm" in paths:
        return "llm"
    return "fallback"


def _build_complexity_per_uow(rows: list[dict]) -> list[dict[str, Any]]:
    """Build per-UoW complexity records from uow_registry rows."""
    result = []
    for row in rows:
        register = row.get("register") or "operational"
        uow_type = row.get("type") or "executable"
        cycles = row.get("steward_cycles") or 0
        path = _classify_prescription_path(row.get("steward_log"))
        over_complex = (
            register == "operational" and cycles > _OPERATIONAL_COMPLEXITY_THRESHOLD
        )
        result.append({
            "uow_id": row["id"],
            "register": register,
            "type": uow_type,
            "steward_cycles": cycles,
            "prescription_path": path,
            "over_complex_flag": over_complex,
        })
    return result


def _compute_complexity_aggregate(
    per_uow: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Compute per-register breakdown of avg_cycles and prescription path
    distribution.

    Returns:
        by_register: {register_value: {avg_cycles, pct_llm, pct_fallback,
                                       count, over_complex_count}}
    """
    by_register: dict[str, list[dict]] = {}
    for r in per_uow:
        by_register.setdefault(r["register"], []).append(r)

    breakdown: dict[str, dict[str, Any]] = {}
    for reg, uows in by_register.items():
        count = len(uows)
        cycles_vals = [u["steward_cycles"] for u in uows if u["steward_cycles"] is not None]
        avg_c = round(sum(cycles_vals) / len(cycles_vals), 4) if cycles_vals else None
        llm_count = sum(1 for u in uows if u["prescription_path"] == "llm")
        fallback_count = sum(1 for u in uows if u["prescription_path"] == "fallback")
        over_complex = sum(1 for u in uows if u["over_complex_flag"])
        pct_llm = round(100 * llm_count / count, 1) if count > 0 else None
        pct_fallback = round(100 * fallback_count / count, 1) if count > 0 else None
        breakdown[reg] = {
            "count": count,
            "avg_cycles": avg_c,
            "pct_llm": pct_llm,
            "pct_fallback": pct_fallback,
            "over_complex_count": over_complex,
        }

    return {"by_register": breakdown}


def complexity_appropriateness_summary(
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """
    Compare prescribed workflow complexity to UoW type and register.

    Flags 'operational' register UoWs with more than
    _OPERATIONAL_COMPLEXITY_THRESHOLD steward cycles as potentially
    over-complex.

    Parameters
    ----------
    registry_path:
        Path to the registry SQLite DB.

    Returns
    -------
    dict with keys:
        per_uow   — list of {uow_id, register, type, steward_cycles,
                             prescription_path, over_complex_flag}
        aggregate — {by_register: {register: {count, avg_cycles, pct_llm,
                                              pct_fallback, over_complex_count}}}
    """
    path = registry_path if registry_path is not None else _default_registry_path()
    if not path.exists():
        return {"per_uow": [], "aggregate": _compute_complexity_aggregate([])}
    try:
        conn = _connect_ro(path)
    except sqlite3.OperationalError:
        return {"per_uow": [], "aggregate": _compute_complexity_aggregate([])}
    try:
        rows = _fetch_uow_complexity_rows(conn)
    except sqlite3.OperationalError:
        return {"per_uow": [], "aggregate": _compute_complexity_aggregate([])}
    finally:
        conn.close()

    per_uow = _build_complexity_per_uow(rows)
    aggregate = _compute_complexity_aggregate(per_uow)
    return {"per_uow": per_uow, "aggregate": aggregate}


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
