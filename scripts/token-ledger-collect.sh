#!/usr/bin/env bash
# token-ledger-collect.sh — Token ledger collector for flamegraph observability
#
# Called from a PostToolUse hook on Agent tool calls (fires after each subagent
# completes). Reads the current session JSONL, finds the most recent assistant
# message's usage fields, and appends a JSONL entry to token-ledger.jsonl.
#
# Idempotent: uses a last-processed pointer file to avoid double-counting.
# All errors exit 0 — this hook must never block the Agent tool call.
#
# Input: PostToolUse hook JSON on stdin
# Output: nothing (all results written to token-ledger.jsonl)
#
# JSONL entry schema:
#   {
#     "ts": 1712345678,
#     "source": "steward",
#     "task_id": "steward-heartbeat-abc",
#     "model": "claude-sonnet-4-6",
#     "input": 1234,
#     "output": 456,
#     "cache_read": 50000,
#     "cache_write": 200,
#     "session_id": "675cafb9..."
#   }

set -uo pipefail

WORKSPACE="${LOBSTER_WORKSPACE:-${HOME}/lobster-workspace}"
LEDGER_FILE="${WORKSPACE}/data/token-ledger.jsonl"
POINTER_FILE="${WORKSPACE}/data/token-ledger.pointer"
LOCK_FILE="${WORKSPACE}/data/token-ledger.lock"
INFLIGHT_FILE="${WORKSPACE}/data/inflight-work.jsonl"
SESSION_DIR="${HOME}/.claude/projects/-home-lobster-lobster-workspace"
LOG_FILE="${WORKSPACE}/logs/hook-failures.log"

# ---------------------------------------------------------------------------
# Failure logging — never to stdout, never exit non-zero
# ---------------------------------------------------------------------------
log_failure() {
  local msg="$1"
  local ts
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown")
  mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || true
  printf '[%s] token-ledger-collect: %s\n' "${ts}" "${msg}" >> "${LOG_FILE}" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Read stdin — the PostToolUse hook JSON
# ---------------------------------------------------------------------------
INPUT=$(cat)
if [[ -z "${INPUT// }" ]]; then
  exit 0
fi

# Only handle Agent tool calls
TOOL_NAME=$(printf '%s' "${INPUT}" | jq -r '.tool_name // empty' 2>/dev/null || true)
if [[ "${TOOL_NAME}" != "Agent" ]]; then
  exit 0
fi

SESSION_ID=$(printf '%s' "${INPUT}" | jq -r '.session_id // empty' 2>/dev/null || true)
if [[ -z "${SESSION_ID}" ]]; then
  log_failure "no session_id in hook input"
  exit 0
fi

# ---------------------------------------------------------------------------
# Find the most recent session JSONL (dynamic — session ID changes per session)
#
# ATTRIBUTION NOTE: This hook reads from the dispatcher's session JSONL, NOT
# from per-subagent JSONL files. Usage deltas therefore include the
# dispatcher's accumulated context cost (prompt tokens grow with each turn).
# Token counts in the ledger reflect dispatcher-session deltas tagged to the
# subagent being invoked — they are NOT isolated per-subagent costs. The
# flamegraph per-source numbers include parent-session context overhead and
# will overstate actual subagent spend.
# ---------------------------------------------------------------------------
JSONL_FILE=$(ls -t "${SESSION_DIR}"/*.jsonl 2>/dev/null | head -1 || true)
if [[ -z "${JSONL_FILE}" || ! -f "${JSONL_FILE}" ]]; then
  log_failure "no session JSONL found in ${SESSION_DIR}"
  exit 0
fi

# ---------------------------------------------------------------------------
# Acquire an exclusive lock before reading the pointer, collecting usage, and
# writing back. Without this lock, concurrent PostToolUse fires (e.g. when
# steward and executor heartbeats launch subagents simultaneously) can read
# the same LAST_OFFSET, extract the same delta, and double-count token spend.
# The lock covers the full read-pointer → read-delta → append-ledger →
# write-pointer sequence. It is released automatically when the script exits.
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "${LOCK_FILE}")" 2>/dev/null || true
exec 9>"${LOCK_FILE}"
if ! flock -w 10 9; then
  log_failure "could not acquire lock on ${LOCK_FILE} within 10s"
  exit 0
fi

# ---------------------------------------------------------------------------
# Load last-processed pointer (byte offset into the JSONL file)
# ---------------------------------------------------------------------------
LAST_OFFSET=0
if [[ -f "${POINTER_FILE}" ]]; then
  STORED=$(awk -F'\t' -v key="${JSONL_FILE}" '$1 == key {print $2}' "${POINTER_FILE}" 2>/dev/null || true)
  if [[ -n "${STORED}" && "${STORED}" =~ ^[0-9]+$ ]]; then
    LAST_OFFSET="${STORED}"
  fi
fi

# ---------------------------------------------------------------------------
# Find the most recent assistant message with usage fields AFTER last_offset
# Using a temp file to avoid subshell/SIGPIPE issues with heredoc + $()
# ---------------------------------------------------------------------------
TMPPY=$(mktemp /tmp/token-ledger-read.XXXXXX.py)
cat > "${TMPPY}" <<'PYEOF'
import sys
import json

jsonl_path = sys.argv[1]
last_offset = int(sys.argv[2])
out_path = sys.argv[3]

last_usage = None

try:
    with open(jsonl_path, 'rb') as f:
        f.seek(last_offset)
        while True:
            line = f.readline()
            if not line:
                break
            try:
                obj = json.loads(line.decode('utf-8', errors='replace'))
                msg = obj.get('message', {})
                if isinstance(msg, dict) and msg.get('role') == 'assistant':
                    usage = msg.get('usage')
                    if usage and isinstance(usage, dict):
                        last_usage = dict(usage)
                        last_usage['_new_offset'] = f.tell()
                        last_usage['_model'] = msg.get('model', '')
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
except Exception as e:
    sys.exit(1)

if last_usage:
    with open(out_path, 'w') as f:
        json.dump(last_usage, f)
    sys.exit(0)
else:
    sys.exit(2)
PYEOF

TMPOUT=$(mktemp /tmp/token-ledger-out.XXXXXX.json)
python3 "${TMPPY}" "${JSONL_FILE}" "${LAST_OFFSET}" "${TMPOUT}" 2>/dev/null
PY_EXIT=$?
rm -f "${TMPPY}"

if [[ ${PY_EXIT} -eq 2 ]]; then
  # No new assistant messages with usage — nothing to record
  rm -f "${TMPOUT}"
  exit 0
fi

if [[ ${PY_EXIT} -ne 0 || ! -f "${TMPOUT}" ]]; then
  log_failure "failed to extract usage from session JSONL ${JSONL_FILE}"
  rm -f "${TMPOUT}"
  exit 0
fi

USAGE_JSON=$(cat "${TMPOUT}" 2>/dev/null || true)
rm -f "${TMPOUT}"

if [[ -z "${USAGE_JSON}" ]]; then
  log_failure "empty usage JSON from session JSONL"
  exit 0
fi

# ---------------------------------------------------------------------------
# Extract token counts from usage JSON
# ---------------------------------------------------------------------------
INPUT_TOKENS=$(printf '%s' "${USAGE_JSON}" | jq -r '.input_tokens // 0' 2>/dev/null || echo "0")
OUTPUT_TOKENS=$(printf '%s' "${USAGE_JSON}" | jq -r '.output_tokens // 0' 2>/dev/null || echo "0")
CACHE_READ=$(printf '%s' "${USAGE_JSON}" | jq -r '.cache_read_input_tokens // 0' 2>/dev/null || echo "0")
CACHE_WRITE=$(printf '%s' "${USAGE_JSON}" | jq -r '.cache_creation_input_tokens // 0' 2>/dev/null || echo "0")
NEW_OFFSET=$(printf '%s' "${USAGE_JSON}" | jq -r '._new_offset // 0' 2>/dev/null || echo "0")
MODEL_VAL=$(printf '%s' "${USAGE_JSON}" | jq -r '._model // ""' 2>/dev/null || true)

# ---------------------------------------------------------------------------
# Determine source tag from tool_input prompt frontmatter (most reliable)
# ---------------------------------------------------------------------------
SOURCE_TAG="subagent:unknown"
TASK_ID_VAL="unknown"

# Extract task_id from the Agent prompt frontmatter via python
TMPPY2=$(mktemp /tmp/token-ledger-tid.XXXXXX.py)
cat > "${TMPPY2}" <<'PYEOF2'
import sys
import json
import re

input_json = sys.argv[1]

try:
    data = json.loads(input_json)
    prompt = data.get('tool_input', {}).get('prompt', '')
    # Try YAML frontmatter first
    m = re.search(r'^---\s*\n(.*?)^---', prompt, re.DOTALL | re.MULTILINE)
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, _, v = line.partition(':')
                if k.strip() == 'task_id':
                    print(v.strip())
                    sys.exit(0)
    # Fall back to legacy text
    m = re.search(r'task_id\s+is:\s*(\S+)', prompt, re.IGNORECASE)
    if m:
        print(m.group(1))
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PYEOF2

PROMPT_TASK_ID=$(python3 "${TMPPY2}" "${INPUT}" 2>/dev/null || true)
rm -f "${TMPPY2}"

if [[ -n "${PROMPT_TASK_ID}" ]]; then
  TASK_ID_VAL="${PROMPT_TASK_ID}"
fi

# Fall back to inflight-work.jsonl if no task_id from prompt
if [[ "${TASK_ID_VAL}" == "unknown" && -f "${INFLIGHT_FILE}" ]]; then
  LAST_INFLIGHT=$(tail -1 "${INFLIGHT_FILE}" 2>/dev/null || true)
  if [[ -n "${LAST_INFLIGHT}" ]]; then
    RAW_TASK_ID=$(printf '%s' "${LAST_INFLIGHT}" | jq -r '.task_id // empty' 2>/dev/null || true)
    RAW_TYPE=$(printf '%s' "${LAST_INFLIGHT}" | jq -r '.type // empty' 2>/dev/null || true)
    if [[ -n "${RAW_TASK_ID}" ]]; then
      TASK_ID_VAL="${RAW_TASK_ID}"
    fi
  fi
fi

# Derive source tag from task_id
case "${TASK_ID_VAL}" in
  steward-*)                 SOURCE_TAG="steward" ;;
  executor-*|uow-*)         SOURCE_TAG="executor" ;;
  ralph-*)                   SOURCE_TAG="ralph" ;;
  dispatcher-*)              SOURCE_TAG="dispatcher" ;;
  *)
    # Use first hyphen-delimited segment as the prefix
    PREFIX="${TASK_ID_VAL%%-*}"
    if [[ -n "${PREFIX}" && "${PREFIX}" != "${TASK_ID_VAL}" ]]; then
      SOURCE_TAG="subagent:${PREFIX}"
    elif [[ "${TASK_ID_VAL}" != "unknown" ]]; then
      SOURCE_TAG="subagent:${TASK_ID_VAL}"
    fi
    ;;
esac

# Also check inflight type field for well-known WOS types
if [[ "${SOURCE_TAG}" == "subagent:unknown" && -f "${INFLIGHT_FILE}" ]]; then
  LAST_TYPE=$(tail -1 "${INFLIGHT_FILE}" 2>/dev/null | jq -r '.type // empty' 2>/dev/null || true)
  case "${LAST_TYPE}" in
    wos-steward*) SOURCE_TAG="steward" ;;
    wos-uow*|wos-executor*) SOURCE_TAG="executor" ;;
    ralph*) SOURCE_TAG="ralph" ;;
    dispatcher*) SOURCE_TAG="dispatcher" ;;
  esac
fi

# ---------------------------------------------------------------------------
# Build and append JSONL entry
# ---------------------------------------------------------------------------
NOW_S=$(date +%s)

mkdir -p "$(dirname "${LEDGER_FILE}")" 2>/dev/null || true

ENTRY=$(jq -cn \
  --argjson ts "${NOW_S}" \
  --arg source "${SOURCE_TAG}" \
  --arg task_id "${TASK_ID_VAL}" \
  --arg model "${MODEL_VAL}" \
  --argjson input_tokens "${INPUT_TOKENS}" \
  --argjson output_tokens "${OUTPUT_TOKENS}" \
  --argjson cache_read "${CACHE_READ}" \
  --argjson cache_write "${CACHE_WRITE}" \
  --arg session_id "${SESSION_ID}" \
  '{
    "ts": $ts,
    "source": $source,
    "task_id": $task_id,
    "model": $model,
    "input": $input_tokens,
    "output": $output_tokens,
    "cache_read": $cache_read,
    "cache_write": $cache_write,
    "session_id": $session_id
  }' 2>/dev/null || true)

if [[ -z "${ENTRY}" ]]; then
  log_failure "failed to build ledger entry JSON"
  exit 0
fi

printf '%s\n' "${ENTRY}" >> "${LEDGER_FILE}" 2>/dev/null || {
  log_failure "failed to append to ${LEDGER_FILE}"
  exit 0
}

# ---------------------------------------------------------------------------
# Update the last-processed pointer (byte offset for next run)
# ---------------------------------------------------------------------------
if [[ "${NEW_OFFSET}" -gt 0 ]]; then
  POINTER_TMP="${POINTER_FILE}.$$.tmp"
  {
    if [[ -f "${POINTER_FILE}" ]]; then
      # Remove existing entry for this file, keeping others
      grep -vF "${JSONL_FILE}	" "${POINTER_FILE}" 2>/dev/null || true
    fi
    printf '%s\t%s\n' "${JSONL_FILE}" "${NEW_OFFSET}"
  } > "${POINTER_TMP}" 2>/dev/null && mv "${POINTER_TMP}" "${POINTER_FILE}" 2>/dev/null || {
    rm -f "${POINTER_TMP}" 2>/dev/null || true
    log_failure "failed to update pointer file"
  }
fi

exit 0
