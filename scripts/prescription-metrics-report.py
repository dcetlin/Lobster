#!/usr/bin/env python3
"""
prescription-metrics-report.py — WOS prescription pipeline success metrics.

Reads from wos-metrics.db (prescription_metrics table) and prints a structured
report covering completeness, fidelity, convergence, complexity, and trends.

Usage:
    uv run scripts/prescription-metrics-report.py [--json] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _default_db_path() -> Path:
    workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    return Path(workspace) / "data" / "wos-metrics.db"


def _connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fetch_rows(conn: sqlite3.Connection, event_type: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM prescription_metrics WHERE event_type = ? ORDER BY ts ASC",
        (event_type,),
    ).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        try:
            r["payload"] = json.loads(r["payload"])
        except (json.JSONDecodeError, KeyError):
            r["payload"] = {}
        result.append(r)
    return result


# ---------------------------------------------------------------------------
# Metric computations (pure functions on row lists)
# ---------------------------------------------------------------------------

def compute_prescription_completeness(prescriptions: list[dict]) -> dict:
    if not prescriptions:
        return {"total": 0, "complete": 0, "rate": None, "note": "no data"}

    complete = sum(
        1 for r in prescriptions
        if r["payload"].get("has_minimum_viable_output")
        and r["payload"].get("has_boundary")
        and r["payload"].get("has_success_criteria_check")
    )
    total = len(prescriptions)
    return {
        "total": total,
        "complete": complete,
        "rate": round(complete / total, 4) if total else None,
        "missing_mvo": sum(1 for r in prescriptions if not r["payload"].get("has_minimum_viable_output")),
        "missing_boundary": sum(1 for r in prescriptions if not r["payload"].get("has_boundary")),
        "missing_success_criteria": sum(1 for r in prescriptions if not r["payload"].get("has_success_criteria_check")),
    }


def compute_execution_fidelity(completions: list[dict]) -> dict:
    if not completions:
        return {"total": 0, "followed": 0, "rate": None, "note": "no data"}

    followed = sum(1 for r in completions if r["payload"].get("followed_prescription"))
    deviated = sum(1 for r in completions if r["payload"].get("deviated"))
    total = len(completions)
    return {
        "total": total,
        "followed": followed,
        "deviated": deviated,
        "rate": round(followed / total, 4) if total else None,
    }


def compute_convergence(closures: list[dict]) -> dict:
    if not closures:
        return {"total": 0, "by_outcome": {}, "avg_cycles": None, "note": "no data"}

    by_outcome: dict[str, list[int]] = defaultdict(list)
    for r in closures:
        outcome = r["payload"].get("closure_outcome", "unknown")
        cycles = r["payload"].get("total_cycles", 0)
        by_outcome[outcome].append(cycles)

    all_cycles = [c for lst in by_outcome.values() for c in lst]
    avg = round(sum(all_cycles) / len(all_cycles), 2) if all_cycles else None

    return {
        "total": len(closures),
        "avg_cycles": avg,
        "by_outcome": {
            k: {"count": len(v), "avg_cycles": round(sum(v) / len(v), 2) if v else None}
            for k, v in by_outcome.items()
        },
    }


def compute_top_multicycle(closures: list[dict], n: int = 10) -> list[dict]:
    rows = sorted(closures, key=lambda r: r["payload"].get("total_cycles", 0), reverse=True)
    return [
        {"uow_id": r["uow_id"], "total_cycles": r["payload"].get("total_cycles", 0),
         "closure_outcome": r["payload"].get("closure_outcome", "unknown")}
        for r in rows[:n]
    ]


_WORD_BUCKETS = [(0, 200), (200, 500), (500, 1000), (1000, float("inf"))]
_BUCKET_LABELS = ["<200", "200-500", "500-1000", "1000+"]


def compute_complexity_distribution(prescriptions: list[dict]) -> dict:
    word_dist: Counter = Counter()
    step_dist: Counter = Counter()

    for r in prescriptions:
        wc = r["payload"].get("word_count", 0)
        sc = r["payload"].get("step_count", 0)
        for (lo, hi), label in zip(_WORD_BUCKETS, _BUCKET_LABELS):
            if lo <= wc < hi:
                word_dist[label] += 1
                break
        step_dist[str(sc)] += 1

    return {
        "word_count_buckets": dict(word_dist),
        "step_count_distribution": dict(step_dist),
    }


def compute_trend(prescriptions: list[dict], days: int = 30) -> dict:
    if not prescriptions:
        return {"note": "no data"}

    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    recent = [r for r in prescriptions if r["ts"] >= cutoff]
    by_day: Counter = Counter()
    for r in recent:
        day = r["ts"][:10]
        by_day[day] += 1

    return {
        "days_requested": days,
        "days_with_data": len(by_day),
        "total_in_window": len(recent),
        "by_day": dict(sorted(by_day.items())),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(db_path: Path) -> dict:
    conn = _connect(db_path)
    if conn is None:
        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "db_path": str(db_path),
            "error": "wos-metrics.db not found — instrumentation may not have run yet",
            "prescription_completeness": {"note": "no data"},
            "execution_fidelity": {"note": "no data"},
            "convergence": {"note": "no data"},
            "top_multicycle_uows": [],
            "complexity_distribution": {"note": "no data"},
            "trend_30d": {"note": "no data"},
        }

    try:
        prescriptions = _fetch_rows(conn, "prescription_generated")
        completions = _fetch_rows(conn, "execution_completed")
        closures = _fetch_rows(conn, "uow_closed")
    finally:
        conn.close()

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "db_path": str(db_path),
        "row_counts": {
            "prescription_generated": len(prescriptions),
            "execution_completed": len(completions),
            "uow_closed": len(closures),
        },
        "prescription_completeness": compute_prescription_completeness(prescriptions),
        "execution_fidelity": compute_execution_fidelity(completions),
        "convergence": compute_convergence(closures),
        "top_multicycle_uows": compute_top_multicycle(closures),
        "complexity_distribution": compute_complexity_distribution(prescriptions),
        "trend_30d": compute_trend(prescriptions),
    }


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _pct(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.1f}%"


def render_text(report: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("WOS Prescription Pipeline — Success Metrics Report")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append(f"DB: {report.get('db_path', 'unknown')}")
    lines.append("=" * 64)

    if "error" in report:
        lines.append(f"\nERROR: {report['error']}")
        return "\n".join(lines)

    rc = report.get("row_counts", {})
    lines.append(f"\nData: {rc.get('prescription_generated', 0)} prescriptions, "
                 f"{rc.get('execution_completed', 0)} completions, "
                 f"{rc.get('uow_closed', 0)} closures")

    # 1. Prescription completeness
    lines.append("\n── 1. Prescription Completeness ──────────────────────────")
    pc = report["prescription_completeness"]
    if "note" in pc and pc.get("total", 0) == 0:
        lines.append("  No prescription data yet.")
    else:
        lines.append(f"  Completeness rate:     {_pct(pc.get('rate'))} "
                     f"({pc.get('complete', 0)}/{pc.get('total', 0)})")
        lines.append(f"  Missing MVO:           {pc.get('missing_mvo', 0)}")
        lines.append(f"  Missing Boundary:      {pc.get('missing_boundary', 0)}")
        lines.append(f"  Missing success crit.: {pc.get('missing_success_criteria', 0)}")

    # 2. Execution fidelity
    lines.append("\n── 2. Execution Fidelity Rate ────────────────────────────")
    ef = report["execution_fidelity"]
    if "note" in ef and ef.get("total", 0) == 0:
        lines.append("  No execution completion data yet.")
    else:
        lines.append(f"  Fidelity rate:         {_pct(ef.get('rate'))} "
                     f"({ef.get('followed', 0)}/{ef.get('total', 0)})")
        lines.append(f"  Deviations recorded:   {ef.get('deviated', 0)}")

    # 3. Convergence
    lines.append("\n── 3. Average Cycles to UoW Closure ─────────────────────")
    cv = report["convergence"]
    if "note" in cv and cv.get("total", 0) == 0:
        lines.append("  No closure data yet.")
    else:
        lines.append(f"  Overall avg cycles:    {cv.get('avg_cycles', 'n/a')}")
        for outcome, stats in cv.get("by_outcome", {}).items():
            lines.append(f"  {outcome:20s}  {stats['count']} UoWs, "
                         f"avg {stats.get('avg_cycles', 'n/a')} cycles")

    # 4. Top 10 multi-cycle UoWs
    lines.append("\n── 4. Top Multi-Cycle UoWs ───────────────────────────────")
    top = report["top_multicycle_uows"]
    if not top:
        lines.append("  No multi-cycle data yet.")
    else:
        for i, row in enumerate(top[:10], 1):
            lines.append(f"  {i:2d}. {row['uow_id']}  "
                         f"cycles={row['total_cycles']}  outcome={row['closure_outcome']}")

    # 5. Complexity distribution
    lines.append("\n── 5. Prescription Complexity Distribution ───────────────")
    cd = report["complexity_distribution"]
    if "note" in cd:
        lines.append("  No prescription data yet.")
    else:
        lines.append("  Word count buckets:")
        for label in _BUCKET_LABELS:
            count = cd.get("word_count_buckets", {}).get(label, 0)
            lines.append(f"    {label:12s}  {count}")
        lines.append("  Step count distribution:")
        for k, v in sorted(cd.get("step_count_distribution", {}).items(), key=lambda x: int(x[0])):
            lines.append(f"    {k:4s} sections  {v}")

    # 6. Trend
    lines.append("\n── 6. Prescription Trend (last 30 days) ──────────────────")
    tr = report["trend_30d"]
    if "note" in tr:
        lines.append("  No data.")
    else:
        lines.append(f"  Total prescriptions in window: {tr.get('total_in_window', 0)}")
        lines.append(f"  Days with data: {tr.get('days_with_data', 0)}")
        for day, count in list(tr.get("by_day", {}).items())[-14:]:
            bar = "█" * min(count, 40)
            lines.append(f"  {day}  {bar} {count}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="WOS prescription pipeline metrics report")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--db", type=Path, default=None, help="Override wos-metrics.db path")
    args = parser.parse_args()

    db_path = args.db or _default_db_path()
    report = build_report(db_path)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))


if __name__ == "__main__":
    main()
