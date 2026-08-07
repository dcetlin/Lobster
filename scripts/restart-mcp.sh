#!/usr/bin/env bash
# restart-mcp.sh — Safe wrapper for restarting the lobster MCP systemd service.
#
# USE THIS SCRIPT instead of calling `sudo systemctl restart <unit>` directly.
# Direct restarts invalidate the active MCP session immediately, leaving the
# dispatcher blocked in wait_for_messages with a "Session not found" error and
# no recovery guidance.
#
# Unit name auto-detection: different hosts install this service under
# different names (some use `lobster-mcp-local`, others `lobster-mcp`). This
# script detects which unit is actually installed rather than hardcoding one:
#   - If `lobster-mcp-local.service` is a known unit, use it.
#   - Otherwise fall back to `lobster-mcp.service`.
#
# NOTE: on hosts where the dispatcher's MCP tools are served via a stdio
# server spawned directly by the `claude` process (registered in
# ~/.claude.json under mcpServers, not via systemd), restarting this systemd
# unit does NOT reconnect that session. Check `ps`/`~/.claude.json` if a
# restart doesn't appear to take effect.
#
# This script:
#   1. Acquires the shared restart-coordination lock (restart-lock-lib.sh)
#   2. Writes an mcp-restart warning message to ~/messages/inbox/
#   3. Waits 2 seconds for the dispatcher to process it
#   4. Runs `sudo systemctl restart <detected-unit>`
#
# The inbox message tells the dispatcher the restart is intentional and that
# it should re-orient after reconnecting.  Combined with Fix 1
# (session-lost-reminder written on server startup), the dispatcher has two
# chances to see recovery guidance.
#
# Restart coordination (issue #1537): this script and health-check-v3.sh's
# automatic restart paths (do_restart(), check_session_age()) and
# dispatcher-refresh.sh all contend for one shared, non-blocking lock so a
# manual restart never collides with an automatic one. If the lock is already
# held, this script aborts immediately rather than racing the other restart.
#
# Usage:
#   ~/lobster/scripts/restart-mcp.sh
#   ~/lobster/scripts/restart-mcp.sh --no-wait   (skip 2s delay, for scripted use)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./restart-lock-lib.sh
source "${SCRIPT_DIR}/restart-lock-lib.sh"

INBOX_DIR="${LOBSTER_MESSAGES:-${HOME}/messages}/inbox"
REASON="${1:-manual restart}"
NO_WAIT=false
if [[ "${1:-}" == "--no-wait" ]]; then
    NO_WAIT=true
fi

# Restart coordination (issue #1537): refuse to restart if another restart
# path (health-check-v3.sh's automatic restart, or a concurrently-run
# dispatcher-refresh.sh) already holds the shared lock. Restarting into an
# in-flight restart risks two respawn attempts racing each other.
if ! acquire_restart_coordination_lock; then
    echo "[restart-mcp] Another restart is already in progress (lock: ${RESTART_COORDINATION_LOCK_FILE}) — aborting to avoid a collision. Try again shortly." >&2
    exit 1
fi

# Auto-detect the installed unit name: prefer lobster-mcp-local if it exists
# as a known unit file, otherwise fall back to lobster-mcp.
if systemctl list-unit-files --no-legend "lobster-mcp-local.service" 2>/dev/null | grep -q "lobster-mcp-local.service"; then
    MCP_UNIT="lobster-mcp-local"
else
    MCP_UNIT="lobster-mcp"
fi

# Write the warning message to the inbox
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
MSG_ID="mcp-restart-$(date -u +%s)"
MSG_FILE="${INBOX_DIR}/${MSG_ID}.json"

mkdir -p "${INBOX_DIR}"

cat > "${MSG_FILE}.tmp" <<EOF
{
  "id": "${MSG_ID}",
  "source": "system",
  "type": "session_reconnect",
  "chat_id": 0,
  "task_origin": "internal",
  "text": "MCP RESTART INCOMING — The ${MCP_UNIT} service is about to restart. Your MCP session will be invalidated. This is a lightweight reconnect, not a context compaction — situational awareness was NOT lost, so do NOT spawn compact-catchup. Re-orient after reconnecting: read sys.dispatcher.bootup.md and resume the main loop.",
  "timestamp": "${TIMESTAMP}"
}
EOF
mv "${MSG_FILE}.tmp" "${MSG_FILE}"

echo "[restart-mcp] Wrote restart warning to inbox: ${MSG_ID}"

if [[ "${NO_WAIT}" == "false" ]]; then
    echo "[restart-mcp] Waiting 2s for dispatcher to see the message..."
    sleep 2
fi

echo "[restart-mcp] Restarting ${MCP_UNIT}..."
sudo systemctl restart "${MCP_UNIT}"
echo "[restart-mcp] Service restarted."
