#!/usr/bin/env bash
# token-flamegraph.sh — Token flamegraph summary for Lobster
#
# Reads ~/lobster-workspace/data/token-ledger.jsonl and outputs a text
# flamegraph showing token spend by source over a configurable time window.
#
# Usage:
#   token-flamegraph.sh [--window 1h|24h|7d]
#
# Default window: 1h
#
# Output (stdout):
#   Token flamegraph — last 1h (2026-04-06 14:00–15:00 ET)
#   steward    ████████████████████  45,234 in / 8,102 out  [62%]
#   executor   ██████████           22,100 in / 4,210 out  [30%]
#   dispatcher ██                    4,500 in / 890 out    [6%]
#   ralph      ░                     1,200 in / 210 out    [2%]
#   ──────────────────────────────────────────────────────────
#   TOTAL      73,034 in / 13,412 out | cache_read: 2.1M

set -euo pipefail

WORKSPACE="${LOBSTER_WORKSPACE:-${HOME}/lobster-workspace}"
LEDGER_FILE="${WORKSPACE}/data/token-ledger.jsonl"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
WINDOW="1h"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --window)
      shift
      WINDOW="${1:-1h}"
      ;;
    --window=*)
      WINDOW="${1#--window=}"
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 1
      ;;
  esac
  shift
done

# Validate window
case "${WINDOW}" in
  1h|24h|7d) ;;
  *)
    printf 'Invalid window: %s (must be 1h, 24h, or 7d)\n' "${WINDOW}" >&2
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# Handle missing/empty ledger
# ---------------------------------------------------------------------------
if [[ ! -f "${LEDGER_FILE}" || ! -s "${LEDGER_FILE}" ]]; then
  NOW_S=$(date +%s)
  NOW_ET=$(python3 -c "
from datetime import datetime, timezone, timedelta
import sys
ts = int(sys.argv[1])
et_offset = timedelta(hours=-4)  # EDT; adjust to -5 for EST if needed
dt = datetime.fromtimestamp(ts, tz=timezone(et_offset))
print(dt.strftime('%Y-%m-%d %H:%M ET'))
" "${NOW_S}" 2>/dev/null || date -u +"%Y-%m-%d %H:%M UTC")
  printf 'Token flamegraph — last %s (as of %s)\n' "${WINDOW}" "${NOW_ET}"
  printf 'No token data collected yet. The ledger is empty.\n'
  printf 'Entries are collected by the PostToolUse hook on Agent tool calls.\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# Compute time range
# ---------------------------------------------------------------------------
NOW_S=$(date +%s)
case "${WINDOW}" in
  1h)  WINDOW_SECS=3600 ;;
  24h) WINDOW_SECS=86400 ;;
  7d)  WINDOW_SECS=604800 ;;
esac
SINCE_S=$(( NOW_S - WINDOW_SECS ))

# ---------------------------------------------------------------------------
# Aggregate by source using Python (shell + jq aggregation is too error-prone
# for multi-key groupby with formatting)
# ---------------------------------------------------------------------------
python3 - "${LEDGER_FILE}" "${SINCE_S}" "${NOW_S}" "${WINDOW}" <<'PYEOF'
import sys
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

ledger_path = sys.argv[1]
since_s = int(sys.argv[2])
now_s = int(sys.argv[3])
window = sys.argv[4]

# Eastern Time offset (EDT = UTC-4; EST = UTC-5)
# Use UTC-4 as default; production system is always on EDT/EST
ET_OFFSET = timedelta(hours=-4)
ET = timezone(ET_OFFSET)

def fmt_ts(ts):
    dt = datetime.fromtimestamp(ts, tz=ET)
    return dt.strftime('%Y-%m-%d %H:%M ET')

def fmt_num(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)

def fmt_num_comma(n):
    return f"{n:,}"

# Aggregation buckets: source -> {input, output, cache_read, cache_write, count}
buckets = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "count": 0})

with open(ledger_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = entry.get("ts", 0)
        if ts < since_s or ts > now_s:
            continue

        source = entry.get("source", "unknown")
        buckets[source]["input"] += entry.get("input", 0)
        buckets[source]["output"] += entry.get("output", 0)
        buckets[source]["cache_read"] += entry.get("cache_read", 0)
        buckets[source]["cache_write"] += entry.get("cache_write", 0)
        buckets[source]["count"] += 1

# Totals
total_input = sum(b["input"] for b in buckets.values())
total_output = sum(b["output"] for b in buckets.values())
total_cache_read = sum(b["cache_read"] for b in buckets.values())
total_cache_write = sum(b["cache_write"] for b in buckets.values())
total_count = sum(b["count"] for b in buckets.values())

# Sort by total input tokens descending
sorted_sources = sorted(buckets.items(), key=lambda x: x[1]["input"], reverse=True)

# Build flamegraph bar (max 20 chars wide)
BAR_WIDTH = 20
FULL_BLOCK = "█"
LIGHT_BLOCK = "░"

def make_bar(pct):
    if pct <= 0:
        return LIGHT_BLOCK
    filled = round(BAR_WIDTH * pct / 100)
    filled = max(1, min(filled, BAR_WIDTH))
    return FULL_BLOCK * filled

# Header
since_dt = fmt_ts(since_s)
now_dt = fmt_ts(now_s)
print(f"Token flamegraph — last {window} ({since_dt} – {now_dt})")
print()

if not sorted_sources:
    print("  (no data in this window)")
else:
    # Calculate column widths
    max_source_len = max(len(s) for s, _ in sorted_sources)
    max_source_len = max(max_source_len, 8)  # minimum width

    for source, b in sorted_sources:
        pct = (b["input"] / total_input * 100) if total_input > 0 else 0
        bar = make_bar(pct)
        in_s = fmt_num_comma(b["input"])
        out_s = fmt_num_comma(b["output"])
        print(f"  {source:<{max_source_len}}  {bar:<{BAR_WIDTH}}  {in_s:>10} in / {out_s} out  [{pct:.0f}%]")

print()
print("  " + "─" * 70)

# Totals line
total_in_s = fmt_num_comma(total_input)
total_out_s = fmt_num_comma(total_output)
cache_read_s = fmt_num(total_cache_read)
cache_write_s = fmt_num(total_cache_write)
calls_s = f"{total_count} call{'s' if total_count != 1 else ''}"
print(f"  {'TOTAL':<{max_source_len if sorted_sources else 8}}  {'':<{BAR_WIDTH}}  {total_in_s:>10} in / {total_out_s} out | cache_read: {cache_read_s} | cache_write: {cache_write_s} | {calls_s}")
PYEOF

exit 0
