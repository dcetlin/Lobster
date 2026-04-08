"""
wos_metrics_report.py — Consolidated WOS prescription pipeline metrics report.

Aggregates output from all analytics functions into a single terminal report
or JSON document. Complements wos_dashboard.py (which is interactive) with a
batch-friendly, CI-suitable snapshot.

Usage:
    uv run src/orchestration/wos_metrics_report.py [--format text|json] [--since YYYY-MM-DD]

Options:
    --format text|json   Output format. Default: text.
    --since YYYY-MM-DD   Only include data since this date. Default: 7 days ago.
    --db PATH            Override registry DB path.

Design notes:
- Pure function composition: build_report_data() assembles the dict; render
  functions format it. The script's main() only handles CLI concerns.
- No writes. All analytics functions are read-only.
- Prints to stdout only. No logging or side effects in library functions.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.orchestration.analytics import (
    complexity_appropriateness_summary,
    convergence_summary,
    diagnostic_accuracy_summary,
    execution_fidelity_summary,
    prescription_quality_summary,
)


# ---------------------------------------------------------------------------
# Default look-back window (days)
# ---------------------------------------------------------------------------

DEFAULT_LOOKBACK_DAYS = 7


# ---------------------------------------------------------------------------
# Pure: report assembly
# ---------------------------------------------------------------------------

def build_report_data(
    registry_path: Path | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """
    Assemble all metrics into a single report dict.

    Parameters
    ----------
    registry_path:
        Path to the registry SQLite DB. None = use default.
    since:
        ISO-8601 date string (YYYY-MM-DD). Informational only — analytics
        functions that accept a since parameter will use it; others aggregate
        over all data.

    Returns
    -------
    dict with keys:
        generated_at      — ISO timestamp of report generation
        since             — the since value used
        prescription_quality
        execution_fidelity
        diagnostic_accuracy
        convergence
        complexity
    """
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    if since is None:
        since = (datetime.now(tz=timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%d"
        )

    return {
        "generated_at": generated_at,
        "since": since,
        "prescription_quality": prescription_quality_summary(registry_path),
        "execution_fidelity": execution_fidelity_summary(registry_path),
        "diagnostic_accuracy": diagnostic_accuracy_summary(registry_path),
        "convergence": convergence_summary(registry_path),
        "complexity": complexity_appropriateness_summary(registry_path),
    }


# ---------------------------------------------------------------------------
# Pure: text rendering helpers
# ---------------------------------------------------------------------------

def _fmt_rate(value: float | None, label: str = "") -> str:
    """Format a 0-1 rate as a percentage string."""
    if value is None:
        return "n/a"
    pct = round(value * 100, 1)
    return f"{pct}%"


def _fmt_val(value: Any, precision: int = 2) -> str:
    """Format a numeric value or return 'n/a' for None."""
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return str(round(value, precision))
    return str(value)


def render_text(report: dict[str, Any]) -> str:
    """
    Render report dict as a concise terminal text report.

    Each analytics section gets a header and key metrics. Designed for
    terminal consumption — no decorative borders, no ANSI colours.
    """
    lines: list[str] = []

    lines.append("WOS Prescription Pipeline Metrics")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append(f"Since:     {report['since']}")
    lines.append("")

    # --- Prescription Quality ---
    lines.append("== Prescription Quality ==")
    pq = report.get("prescription_quality", {})
    agg = pq.get("aggregate", {})
    lines.append(f"  Total UoWs:        {_fmt_val(agg.get('total_uows'))}")
    lines.append(f"  UoWs with data:    {_fmt_val(agg.get('uows_with_data'))}")
    lines.append(f"  Avg cycles (done): {_fmt_val(agg.get('avg_cycles_to_done'))}")
    lines.append(f"  LLM path:          {_fmt_val(agg.get('pct_llm'))}%")
    lines.append(f"  Fallback path:     {_fmt_val(agg.get('pct_fallback'))}%")
    if pq.get("data_gap"):
        lines.append(f"  Note: {pq['data_gap']}")
    lines.append("")

    # --- Execution Fidelity ---
    lines.append("== Execution Fidelity ==")
    ef = report.get("execution_fidelity", {})
    agg = ef.get("aggregate", {})
    lines.append(f"  Total executions:  {_fmt_val(agg.get('total_executions'))}")
    lines.append(f"  Success rate:      {_fmt_rate(agg.get('success_rate'))}")
    lines.append(f"  Failure rate:      {_fmt_rate(agg.get('failure_rate'))}")
    lines.append(f"  Re-diagnosis rate: {_fmt_rate(agg.get('re_diagnosis_rate'))}")
    lines.append("")

    # --- Diagnostic Accuracy (first-attempt) ---
    lines.append("== Diagnostic Accuracy (First Attempt) ==")
    da = report.get("diagnostic_accuracy", {})
    agg = da.get("aggregate", {})
    lines.append(f"  Total diagnosed:          {_fmt_val(agg.get('total_diagnosed'))}")
    lines.append(f"  First-attempt successes:  {_fmt_val(agg.get('successful_first_attempt_count'))}")
    lines.append(f"  First-attempt success %:  {_fmt_rate(agg.get('first_attempt_success_rate'))}")
    lines.append("")

    # --- Convergence ---
    lines.append("== Convergence (Completed UoWs) ==")
    cv = report.get("convergence", {})
    agg = cv.get("aggregate", {})
    lines.append(f"  Avg cycles to done:  {_fmt_val(agg.get('avg_cycles_to_done'))}")
    lines.append(f"  Median cycles:       {_fmt_val(agg.get('median_cycles'))}")
    lines.append(f"  P90 cycles:          {_fmt_val(agg.get('p90_cycles'))}")
    lines.append(f"  Max cycles:          {_fmt_val(agg.get('max_cycles'))}")
    lines.append(f"  Avg wall-clock hrs:  {_fmt_val(agg.get('avg_wall_clock_hours'))}")
    outliers = agg.get("outlier_uow_ids", [])
    if outliers:
        lines.append(f"  Outliers (>2x med):  {', '.join(str(o) for o in outliers[:5])}")
        if len(outliers) > 5:
            lines.append(f"                       ... and {len(outliers) - 5} more")
    else:
        lines.append("  Outliers (>2x med):  none")
    lines.append("")

    # --- Complexity ---
    lines.append("== Complexity by Register ==")
    cx = report.get("complexity", {})
    by_reg = cx.get("aggregate", {}).get("by_register", {})
    if not by_reg:
        lines.append("  No data.")
    else:
        for reg, stats in sorted(by_reg.items()):
            lines.append(f"  [{reg}]")
            lines.append(f"    Count:            {_fmt_val(stats.get('count'))}")
            lines.append(f"    Avg cycles:       {_fmt_val(stats.get('avg_cycles'))}")
            lines.append(f"    LLM path:         {_fmt_val(stats.get('pct_llm'))}%")
            lines.append(f"    Fallback path:    {_fmt_val(stats.get('pct_fallback'))}%")
            over = stats.get("over_complex_count", 0)
            if over:
                lines.append(f"    Over-complex:     {over} flagged")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    wos_metrics_report CLI — print WOS pipeline metrics to stdout.

    Options:
        --format text|json   Output format (default: text)
        --since YYYY-MM-DD   Look-back start date (default: 7 days ago)
        --db PATH            Override registry DB path
    """
    parser = argparse.ArgumentParser(
        prog="wos_metrics_report",
        description="Consolidated WOS pipeline metrics report",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default=None,
        help="Include data since this date (default: 7 days ago)",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="Path to registry DB (overrides REGISTRY_DB_PATH env var)",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None

    report = build_report_data(registry_path=db_path, since=args.since)

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))


if __name__ == "__main__":
    main()
