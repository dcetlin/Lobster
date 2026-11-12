#!/usr/bin/env bash
# cc-usage-collect.sh — Claude Code usage collector
#
# Called by Claude Code as the statusLine command on every message exchange.
# Reads JSON from stdin, extracts rate_limits fields, and accumulates state
# to ~/.claude/cc-budget/state.json.
#
# Outputs a short status line to stdout for display in Claude Code's status bar.
# Never writes to stderr — Claude Code treats that as a hook error.
#
# JSON schema received from Claude Code (statusLine):
#   {
#     "session_id": "...",
#     "rate_limits": {
#       "five_hour": { "used_percentage": 24.5, "resets_at": 1712345678 },
#       "seven_day": { "used_percentage": 31.2, "resets_at": 1712987654 }
#     },
#     "cost": { "total_cost_usd": 0.42 }
#   }

set -euo pipefail

STATE_DIR="${HOME}/.claude/cc-budget"
STATE_FILE="${STATE_DIR}/state.json"
NOW_S=$(date +%s)

# Ensure state directory exists
mkdir -p "${STATE_DIR}"

# Read stdin into variable; exit silently if empty
INPUT=$(cat)
if [[ -z "${INPUT// }" ]]; then
  exit 0
fi

# --- Parse input fields with jq (graceful: null on missing keys) ---
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
PCT_5H=$(printf '%s' "$INPUT" | jq -r '.rate_limits.five_hour.used_percentage // empty' 2>/dev/null || true)
RESETS_5H=$(printf '%s' "$INPUT" | jq -r '.rate_limits.five_hour.resets_at // empty' 2>/dev/null || true)
PCT_7D=$(printf '%s' "$INPUT" | jq -r '.rate_limits.seven_day.used_percentage // empty' 2>/dev/null || true)
RESETS_7D=$(printf '%s' "$INPUT" | jq -r '.rate_limits.seven_day.resets_at // empty' 2>/dev/null || true)
COST_USD=$(printf '%s' "$INPUT" | jq -r '.cost.total_cost_usd // empty' 2>/dev/null || true)

# --- Read existing state or initialize fresh ---
if [[ -f "${STATE_FILE}" ]]; then
  CURRENT=$(cat "${STATE_FILE}")
  # Validate it's JSON; reset if corrupt
  if ! printf '%s' "$CURRENT" | jq '.' >/dev/null 2>&1; then
    CURRENT='{}'
  fi
else
  CURRENT='{}'
fi

# --- Build updated five_hour block ---
if [[ -n "${PCT_5H}" && -n "${RESETS_5H}" ]]; then
  FIVE_HOUR_JSON=$(jq -n \
    --argjson pct "${PCT_5H}" \
    --argjson resets_at "${RESETS_5H}" \
    '{"pct": $pct, "resets_at": $resets_at}')
else
  FIVE_HOUR_JSON="null"
fi

# --- Build updated seven_day block ---
if [[ -n "${PCT_7D}" && -n "${RESETS_7D}" ]]; then
  SEVEN_DAY_JSON=$(jq -n \
    --argjson pct "${PCT_7D}" \
    --argjson resets_at "${RESETS_7D}" \
    '{"pct": $pct, "resets_at": $resets_at}')
else
  SEVEN_DAY_JSON="null"
fi

# --- Merge into state using jq ---
COST_ARG="${COST_USD:-null}"
SESSION_ARG="${SESSION_ID:-}"

NEW_STATE=$(printf '%s' "$CURRENT" | jq \
  --argjson five_hour "${FIVE_HOUR_JSON}" \
  --argjson seven_day "${SEVEN_DAY_JSON}" \
  --argjson cost "${COST_ARG}" \
  --argjson ts "${NOW_S}" \
  --arg session_id "${SESSION_ARG}" \
  '
  # Initialize required keys if absent
  .v = 1 |
  .ts = $ts |
  .rate_limits.five_hour = $five_hour |
  .rate_limits.seven_day = $seven_day |
  (if $cost != null then .session_cost_usd = $cost else . end) |
  (if ($session_id != "") then
    .snapshots = (.snapshots // {}) |
    .snapshots[$session_id] = {
      "five_hour_pct": (if $five_hour != null then $five_hour.pct else null end),
      "seven_day_pct": (if $seven_day != null then $seven_day.pct else null end),
      "session_cost_usd": $cost,
      "ts": ($ts * 1000)
    }
  else . end) |
  # Prune snapshots older than 48 hours (ts stored in ms)
  .snapshots = (
    (.snapshots // {}) | to_entries |
    map(select(.value.ts > (($ts - 172800) * 1000))) |
    from_entries
  )
  ')

# --- Write atomically via temp file ---
TMP_FILE="${STATE_DIR}/state.$$.tmp"
printf '%s\n' "$(printf '%s' "$NEW_STATE" | jq '.')" > "${TMP_FILE}"
mv "${TMP_FILE}" "${STATE_FILE}"

# --- Emit status line to stdout ---
# Format: "5h: 24% | 7d: 31%" or "5h: --" if no data
if [[ -n "${PCT_5H}" ]]; then
  PCT_5H_INT=$(printf '%.0f' "${PCT_5H}" 2>/dev/null || echo "?")
  STATUS="5h:${PCT_5H_INT}%"

  if [[ -n "${RESETS_5H}" ]]; then
    SECS_LEFT=$(( RESETS_5H - NOW_S ))
    if (( SECS_LEFT > 0 )); then
      HOURS_LEFT=$(( SECS_LEFT / 3600 ))
      MINS_LEFT=$(( (SECS_LEFT % 3600) / 60 ))
      STATUS="${STATUS} ➞${HOURS_LEFT}h${MINS_LEFT}m"
    fi
  fi

  if [[ -n "${PCT_7D}" ]]; then
    PCT_7D_INT=$(printf '%.0f' "${PCT_7D}" 2>/dev/null || echo "?")
    STATUS="${STATUS} | 7d:${PCT_7D_INT}%"
  fi
elif [[ -n "${COST_USD}" ]]; then
  STATUS="\$$(printf '%.2f' "${COST_USD}")"
else
  STATUS="usage:--"
fi

printf '%s\n' "${STATUS}"
