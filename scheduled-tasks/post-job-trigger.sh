#!/bin/bash
# Lobster Scheduled Job Trigger Writer
# Called by cron instead of run-job.sh. Writes a trigger message to the
# dispatcher inbox so the dispatcher spawns the job subagent — avoiding the
# competing claude -p session problem (issue #138).
#
# Usage: post-job-trigger.sh <job-name>

set -e

JOB_NAME="$1"

if [ -z "$JOB_NAME" ]; then
    echo "Usage: $0 <job-name>"
    exit 1
fi

# Load env files so LOBSTER_ADMIN_CHAT_ID and other vars are available
# when running from cron (which does not inherit systemd EnvironmentFile entries).
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

INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
mkdir -p "$INBOX_DIR"

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
FILENAME="$INBOX_DIR/trigger-${JOB_NAME}-$(date +%Y%m%d%H%M%S)-$$.json"

# Write trigger message. The dispatcher reads this and spawns the job subagent.
cat > "$FILENAME" << EOF
{
  "type": "scheduled_job_trigger",
  "job_name": "$JOB_NAME",
  "timestamp": "$TIMESTAMP",
  "source": "cron",
  "chat_id": ${LOBSTER_ADMIN_CHAT_ID:-0}
}
EOF

echo "[$TIMESTAMP] Trigger written for job: $JOB_NAME -> $FILENAME"
