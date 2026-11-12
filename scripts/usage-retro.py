#!/usr/bin/env python3
"""
usage-retro.py — Retroactive usage analysis from token-ledger.jsonl

Reads the token ledger and outputs:
  - Daily totals with cache_read (dominant cost driver)
  - Top 10 sessions/sources by output token count
  - Per-source breakdown (best-effort from task_id prefixes)

Usage:
  uv run scripts/usage-retro.py [--days N] [--format telegram|text]

Options:
  --days N        Only show last N days (default: all)
  --format        Output format: 'telegram' (markdown) or 'text' (default: text)
"""

import sys
import json
import os
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
WORKSPACE = os.environ.get("LOBSTER_WORKSPACE", os.path.expanduser("~/lobster-workspace"))
LEDGER_PATH = os.path.join(WORKSPACE, "data", "token-ledger.jsonl")

# Cost multipliers relative to opus (opus = 1.0x reference).
# Haiku is ~10x cheaper than opus; sonnet ~2.5x cheaper.
MODEL_COST_MULTIPLIERS = {
    "opus": 1.0,
    "sonnet": 0.4,
    "haiku": 0.1,
}
MODEL_UNKNOWN_MULTIPLIER = 0.4  # assume sonnet when model field absent or unrecognized


def model_family(model_str: str) -> str:
    """Normalize a full model ID to its family name for cost lookup."""
    m = model_str.lower() if model_str else ""
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"  # sonnet is default; covers blank/unknown


def fmt_num(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}k"
    return str(n)


def fmt_ts_et(ts):
    dt = datetime.fromtimestamp(ts, tz=ET)
    return dt.strftime("%Y-%m-%d %H:%M ET")


def load_records(days=None):
    records = []
    if not os.path.exists(LEDGER_PATH):
        return records

    cutoff = None
    if days is not None:
        now = datetime.now(tz=ET)
        cutoff_dt = now - timedelta(days=days)
        cutoff = int(cutoff_dt.timestamp())

    with open(LEDGER_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if cutoff and rec.get("ts", 0) < cutoff:
                    continue
                records.append(rec)
            except json.JSONDecodeError:
                continue

    return records


def daily_breakdown(records):
    daily = defaultdict(lambda: {
        "input": 0, "output": 0,
        "cache_read": 0, "cache_write": 0, "count": 0
    })
    for r in records:
        day = datetime.fromtimestamp(r["ts"], tz=ET).strftime("%Y-%m-%d")
        daily[day]["input"] += r.get("input", 0)
        daily[day]["output"] += r.get("output", 0)
        daily[day]["cache_read"] += r.get("cache_read", 0)
        daily[day]["cache_write"] += r.get("cache_write", 0)
        daily[day]["count"] += 1
    return daily


def model_breakdown(records):
    """Group records by model family, computing weighted cost units for attribution."""
    models = defaultdict(lambda: {
        "input": 0, "output": 0,
        "cache_read": 0, "cache_write": 0, "count": 0,
        "cost_units": 0.0,
    })
    for r in records:
        family = model_family(r.get("model", ""))
        multiplier = MODEL_COST_MULTIPLIERS.get(family, MODEL_UNKNOWN_MULTIPLIER)
        total_tokens = (
            r.get("input", 0) +
            r.get("output", 0) +
            r.get("cache_read", 0) +
            r.get("cache_write", 0)
        )
        models[family]["input"] += r.get("input", 0)
        models[family]["output"] += r.get("output", 0)
        models[family]["cache_read"] += r.get("cache_read", 0)
        models[family]["cache_write"] += r.get("cache_write", 0)
        models[family]["count"] += 1
        models[family]["cost_units"] += total_tokens * multiplier
    return models


def source_breakdown(records):
    sources = defaultdict(lambda: {
        "input": 0, "output": 0,
        "cache_read": 0, "cache_write": 0, "count": 0
    })
    for r in records:
        src = r.get("source", "unknown")
        sources[src]["input"] += r.get("input", 0)
        sources[src]["output"] += r.get("output", 0)
        sources[src]["cache_read"] += r.get("cache_read", 0)
        sources[src]["cache_write"] += r.get("cache_write", 0)
        sources[src]["count"] += 1
    return sources


def main():
    parser = argparse.ArgumentParser(description="Usage retro from token ledger")
    parser.add_argument("--days", type=int, default=None, help="Show last N days only")
    parser.add_argument("--format", choices=["telegram", "text"], default="text")
    args = parser.parse_args()

    records = load_records(days=args.days)

    if not records:
        print("No records found in ledger.")
        if not os.path.exists(LEDGER_PATH):
            print(f"Ledger file missing: {LEDGER_PATH}")
        return

    min_ts = min(r["ts"] for r in records)
    max_ts = max(r["ts"] for r in records)

    daily = daily_breakdown(records)
    sources = source_breakdown(records)
    models = model_breakdown(records)

    # Sort sources by cache_read (dominant cost driver)
    sorted_sources = sorted(sources.items(), key=lambda x: x[1]["cache_read"], reverse=True)
    # Sort models by cost_units descending
    sorted_models = sorted(models.items(), key=lambda x: x[1]["cost_units"], reverse=True)
    total_cost_units = sum(m["cost_units"] for m in models.values()) or 1.0

    total_calls = sum(d["count"] for d in daily.values())
    total_cache_read = sum(d["cache_read"] for d in daily.values())
    total_cache_write = sum(d["cache_write"] for d in daily.values())
    total_output = sum(d["output"] for d in daily.values())

    if args.format == "telegram":
        lines = []
        title = "Token Usage Retro"
        if args.days:
            title += f" — last {args.days} days"
        lines.append(f"*{title}*")
        lines.append(f"_{fmt_ts_et(min_ts)} → {fmt_ts_et(max_ts)}_")
        lines.append(f"{len(records)} calls total")
        lines.append("")
        lines.append("*Daily Breakdown*")

        for day in sorted(daily.keys()):
            d = daily[day]
            cr = fmt_num(d["cache_read"])
            out = fmt_num(d["output"])
            lines.append(
                f"  `{day}` — {d['count']} calls | cache\\_read: {cr} | out: {out}"
            )

        lines.append("")
        lines.append("*Top Sources by cache\\_read* (dominant cost driver)")
        for src, b in sorted_sources[:10]:
            cr = fmt_num(b["cache_read"])
            out = fmt_num(b["output"])
            cnt = b["count"]
            lines.append(f"  `{src}` — {cnt} calls | cache\\_read: {cr} | out: {out}")

        lines.append("")
        lines.append("*Model Breakdown* (cost\\-weighted, opus=1.0x ref, sonnet=0.4x, haiku=0.1x)")
        for family, m in sorted_models:
            pct = m["cost_units"] / total_cost_units * 100
            cr = fmt_num(m["cache_read"])
            cnt = m["count"]
            lines.append(
                f"  `{family}` — {cnt} calls | cache\\_read: {cr} | cost share: {pct:.0f}%"
            )

        lines.append("")
        lines.append(
            f"*Totals* — cache\\_read: {fmt_num(total_cache_read)} | "
            f"cache\\_write: {fmt_num(total_cache_write)} | output: {fmt_num(total_output)}"
        )
        lines.append(
            "_Note: input token counts are session deltas (include context overhead); "
            "cache\\_read is the dominant quota cost driver._"
        )
        print("\n".join(lines))

    else:
        # Plain text
        title = "Token Usage Retro"
        if args.days:
            title += f" — last {args.days} days"
        print(f"\n{title}")
        print(f"Range: {fmt_ts_et(min_ts)} → {fmt_ts_et(max_ts)}")
        print(f"Total: {len(records)} calls\n")

        print("Daily Breakdown:")
        print(f"  {'Date':<12} {'Calls':>5} {'cache_read':>12} {'cache_write':>12} {'output':>10}")
        print("  " + "-" * 58)
        for day in sorted(daily.keys()):
            d = daily[day]
            print(
                f"  {day:<12} {d['count']:>5} "
                f"{fmt_num(d['cache_read']):>12} "
                f"{fmt_num(d['cache_write']):>12} "
                f"{fmt_num(d['output']):>10}"
            )

        print("\nTop Sources by cache_read (dominant cost driver):")
        print(f"  {'Source':<25} {'Calls':>5} {'cache_read':>12} {'output':>10}")
        print("  " + "-" * 55)
        for src, b in sorted_sources[:10]:
            print(
                f"  {src:<25} {b['count']:>5} "
                f"{fmt_num(b['cache_read']):>12} "
                f"{fmt_num(b['output']):>10}"
            )

        print("\nModel Breakdown (cost-weighted; opus=1.0x ref, sonnet=0.4x, haiku=0.1x):")
        print(f"  {'Model':<12} {'Calls':>5} {'cache_read':>12} {'cost share':>12}")
        print("  " + "-" * 44)
        for family, m in sorted_models:
            pct = m["cost_units"] / total_cost_units * 100
            print(
                f"  {family:<12} {m['count']:>5} "
                f"{fmt_num(m['cache_read']):>12} "
                f"{pct:>11.0f}%"
            )

        print(
            f"\nTotals: cache_read={fmt_num(total_cache_read)} | "
            f"cache_write={fmt_num(total_cache_write)} | output={fmt_num(total_output)} | "
            f"{total_calls} calls"
        )
        print(
            "\nNote: input token counts are session deltas (include context overhead);\n"
            "cache_read is the dominant quota cost driver."
        )


if __name__ == "__main__":
    main()
