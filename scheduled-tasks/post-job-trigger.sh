#!/bin/bash
# Lobster Cron-to-Dispatcher Trigger
#
# Writes a scheduled_job_trigger inbox message so the dispatcher spawns a
# subagent for the job. Does NOT invoke Claude directly.
#
# Usage: post-job-trigger.sh <job-name>
#
# This replaces run-job.sh as the cron entry point.  The dispatcher reads the
# trigger message and spawns a lobster-generalist subagent with the task file
# content prepended by YAML frontmatter.  See the scheduled_job_trigger handler
# in .claude/sys.dispatcher.bootup.md for the full dispatcher-side protocol.

set -e

JOB_NAME="$1"

if [ -z "$JOB_NAME" ]; then
    echo "Usage: $0 <job-name>"
    exit 1
fi

# Load env files so tokens and config (including LOBSTER_ADMIN_CHAT_ID) are
# available when running from cron.  Mirror the approach used in dispatch-job.sh.
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
for _env_file in "$CONFIG_DIR/config.env" "$CONFIG_DIR/global.env"; do
    if [ -f "$_env_file" ]; then
        # shellcheck source=/dev/null
        set -a
        source "$_env_file"
        set +a
    fi
done
unset _env_file

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
JOBS_FILE="${SCHEDULED_JOBS_FILE:-$WORKSPACE/scheduled-jobs/jobs.json}"
LOG_DIR="$WORKSPACE/scheduled-jobs/logs"
INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
TIMESTAMP=$(date -Iseconds)

# Ensure directories exist
mkdir -p "$LOG_DIR" "$INBOX_DIR"

LOG_FILE="$LOG_DIR/${JOB_NAME}.log"

# --- Check enabled flag in jobs.json ---
if [ -f "$JOBS_FILE" ]; then
    if command -v jq &>/dev/null; then
        ENABLED=$(jq -r --arg name "$JOB_NAME" '.jobs[$name].enabled // true' "$JOBS_FILE" 2>/dev/null || echo "true")
    else
        ENABLED=$(uv run - "$JOBS_FILE" "$JOB_NAME" << 'PYEOF'
import json, sys
jobs_file = sys.argv[1]
job_name  = sys.argv[2]
try:
    with open(jobs_file) as f:
        data = json.load(f)
    job = data.get('jobs', {}).get(job_name, {})
    print(str(job.get('enabled', True)).lower())
except Exception:
    print('true')
PYEOF
        2>/dev/null)
    fi
    if [ "$ENABLED" = "false" ]; then
        # Job disabled — exit silently (no log noise)
        exit 0
    fi
fi

# --- Write scheduled_job_trigger to inbox ---
EPOCH=$(date +%s)
FILENAME="scheduled_job_${JOB_NAME}_${EPOCH}.json"

CHAT_ID="${LOBSTER_ADMIN_CHAT_ID:-0}"

uv run - \
    "${INBOX_DIR}/${FILENAME}" \
    "${JOB_NAME}" \
    "${TIMESTAMP}" \
    "${CHAT_ID}" \
    << 'PYEOF'
import json, os, sys

out_path  = sys.argv[1]
job_name  = sys.argv[2]
timestamp = sys.argv[3]
chat_id   = sys.argv[4]

msg = {
    "type": "scheduled_job_trigger",
    "job_name": job_name,
    "timestamp": timestamp,
    "chat_id": chat_id,
    "source": "cron",
}

tmp_path = out_path + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(msg, f, ensure_ascii=False, indent=2)
    f.flush()

os.replace(tmp_path, out_path)
print(f"trigger written: {out_path}")
PYEOF

echo "[$TIMESTAMP] trigger written" >> "$LOG_FILE" 2>&1 || true
