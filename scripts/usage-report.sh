#!/usr/bin/env bash
# usage-report.sh — Unified Claude usage report entry point
#
# Wraps cc-usage-collect.sh (quota window state) and token-flamegraph.sh
# (token breakdown by source) into a single callable command.
#
# Usage:
#   usage-report.sh [--window 1h|24h|7d] [--format summary|flamegraph|full]
#
# Formats:
#   summary    — JSON to stdout with key quota and token fields (machine-readable)
#   flamegraph — Terminal flamegraph text (default, human-readable)
#   full       — Summary JSON followed by flamegraph text
#
# Exit codes: 0 on success, 1 on argument error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${HOME}/.claude/cc-budget/state.json"
WORKSPACE="${LOBSTER_WORKSPACE:-${HOME}/lobster-workspace}"
LEDGER_FILE="${WORKSPACE}/data/token-ledger.jsonl"
OUTCOME_LEDGER_FILE="${WORKSPACE}/data/outcome-ledger.jsonl"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
WINDOW="1h"
FORMAT="flamegraph"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --window)
      shift
      WINDOW="${1:-1h}"
      ;;
    --window=*)
      WINDOW="${1#--window=}"
      ;;
    --format)
      shift
      FORMAT="${1:-flamegraph}"
      ;;
    --format=*)
      FORMAT="${1#--format=}"
      ;;
    -h|--help)
      printf 'Usage: usage-report.sh [--window 1h|24h|7d] [--format summary|flamegraph|full]\n'
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 1
      ;;
  esac
  shift
done

# Validate
case "${WINDOW}" in
  1h|24h|7d) ;;
  *)
    printf 'Invalid --window: %s (must be 1h, 24h, or 7d)\n' "${WINDOW}" >&2
    exit 1
    ;;
esac

case "${FORMAT}" in
  summary|flamegraph|full) ;;
  *)
    printf 'Invalid --format: %s (must be summary, flamegraph, or full)\n' "${FORMAT}" >&2
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# summary mode: emit JSON with quota window pcts and aggregated token totals
# ---------------------------------------------------------------------------
emit_summary() {
  python3 - "${STATE_FILE}" "${LEDGER_FILE}" "${OUTCOME_LEDGER_FILE}" "${WINDOW}" <<'PYEOF'
import sys
import json
import os
from collections import defaultdict

state_path    = sys.argv[1]
ledger_path   = sys.argv[2]
outcome_path  = sys.argv[3]
window        = sys.argv[4]

import time
now_s = int(time.time())

WINDOW_SECS = {"1h": 3600, "24h": 86400, "7d": 604800}
since_s = now_s - WINDOW_SECS[window]

# --- Quota window pcts from state.json ---
window_5h_pct  = None
window_7d_pct  = None
resets_5h_at   = None
resets_7d_at   = None

if os.path.isfile(state_path):
    try:
        state = json.loads(open(state_path).read())
        fh = state.get("rate_limits", {}).get("five_hour") or {}
        sd = state.get("rate_limits", {}).get("seven_day") or {}
        window_5h_pct = fh.get("pct")
        window_7d_pct = sd.get("pct")
        resets_5h_at  = fh.get("resets_at")
        resets_7d_at  = sd.get("resets_at")
    except (json.JSONDecodeError, OSError):
        pass

# --- Token aggregation from ledger ---
total_input       = 0
total_output      = 0
total_cache_read  = 0
total_cache_write = 0
total_calls       = 0
source_counts     = defaultdict(int)

if os.path.isfile(ledger_path):
    try:
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
                total_input       += entry.get("input", 0)
                total_output      += entry.get("output", 0)
                total_cache_read  += entry.get("cache_read", 0)
                total_cache_write += entry.get("cache_write", 0)
                total_calls       += 1
                src = entry.get("source", "unknown")
                source_counts[src] += entry.get("input", 0)
    except OSError:
        pass

# --- Outcome distribution from outcome-ledger ---
outcome_dist = defaultdict(int)
if os.path.isfile(outcome_path):
    try:
        with open(outcome_path) as f:
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
                cat = entry.get("outcome_category", "")
                if cat:
                    outcome_dist[cat] += 1
    except OSError:
        pass

result = {
    "window": window,
    "as_of_ts": now_s,
    "quota": {
        "window_5h_pct":  window_5h_pct,
        "window_7d_pct":  window_7d_pct,
        "resets_5h_at":   resets_5h_at,
        "resets_7d_at":   resets_7d_at,
    },
    "tokens": {
        "total_calls":       total_calls,
        "input":             total_input,
        "output":            total_output,
        "cache_read":        total_cache_read,
        "cache_write":       total_cache_write,
        "top_source":        max(source_counts, key=source_counts.get) if source_counts else None,
    },
    "outcome_dist": dict(outcome_dist),
}

print(json.dumps(result, indent=2))
PYEOF
}

# ---------------------------------------------------------------------------
# flamegraph mode: delegate to token-flamegraph.sh
# ---------------------------------------------------------------------------
emit_flamegraph() {
  "${SCRIPT_DIR}/token-flamegraph.sh" --window "${WINDOW}"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${FORMAT}" in
  summary)
    emit_summary
    ;;
  flamegraph)
    emit_flamegraph
    ;;
  full)
    emit_summary
    printf '\n'
    emit_flamegraph
    ;;
esac
