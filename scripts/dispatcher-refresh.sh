#!/usr/bin/env bash
# dispatcher-refresh.sh — Safe wrapper for a graceful dispatcher CLI refresh.
#
# USE THIS SCRIPT instead of manually running `kill -TERM $(cat
# ~/messages/config/dispatcher.pid)`. This is the operation documented as
# "Dispatcher CLI Restart" in CLAUDE.md: after a deploy that adds or changes
# MCP tools, the running dispatcher's tool-discovery cache needs a refresh —
# restarting the MCP server alone (restart-mcp.sh) does not do this, only
# bouncing the dispatcher CLI process itself does.
#
# Sending SIGTERM to the dispatcher PID fires the Stop hook cleanly, and
# `lobster-claude.service` (systemd) + `scripts/claude-persistent.sh`
# relaunch a brand-new `claude` session (never `--continue`), so bootup docs
# are re-read and the tool cache rebuilds. This is the same graceful pattern
# `scripts/health-check-v3.sh:check_session_age()` uses for the ~2h proactive
# session-age rotation.
#
# Do NOT use `tmux kill-session` (no respawn) or `systemctl restart
# lobster-claude` (hard-restart escalation) for a routine refresh — SIGTERM
# to the dispatcher PID is the correct, minimal action.
#
# Restart coordination (issue #1537): a manual dispatcher refresh and an
# automatic restart (health-check-v3.sh's do_restart() or
# check_session_age()) can otherwise fire concurrently and both act on the
# same dispatcher process. This script acquires the same shared,
# non-blocking restart-coordination lock used by restart-mcp.sh and
# health-check-v3.sh before sending SIGTERM, so it never races an in-flight
# automatic restart.
#
# Usage:
#   ~/lobster/scripts/dispatcher-refresh.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./restart-lock-lib.sh
source "${SCRIPT_DIR}/restart-lock-lib.sh"

DISPATCHER_PID_FILE="${LOBSTER_MESSAGES:-${HOME}/messages}/config/dispatcher.pid"

if [[ ! -f "${DISPATCHER_PID_FILE}" ]]; then
    echo "[dispatcher-refresh] No dispatcher.pid found at ${DISPATCHER_PID_FILE} — nothing to refresh." >&2
    exit 1
fi

PID="$(cat "${DISPATCHER_PID_FILE}")"
if [[ ! "${PID}" =~ ^[0-9]+$ ]]; then
    echo "[dispatcher-refresh] dispatcher.pid contains a non-numeric value ('${PID}') — aborting." >&2
    exit 1
fi

if ! kill -0 "${PID}" 2>/dev/null; then
    echo "[dispatcher-refresh] Dispatcher PID ${PID} is not alive — nothing to refresh." >&2
    exit 1
fi

# Restart coordination (issue #1537): refuse to send SIGTERM if another
# restart path (health-check-v3.sh's automatic restart, or a concurrently-run
# restart-mcp.sh) already holds the shared lock.
if ! acquire_restart_coordination_lock; then
    echo "[dispatcher-refresh] Another restart is already in progress (lock: ${RESTART_COORDINATION_LOCK_FILE}) — aborting to avoid a collision. Try again shortly." >&2
    exit 1
fi

echo "[dispatcher-refresh] Sending SIGTERM to dispatcher PID ${PID} for graceful rotation..."
kill -TERM "${PID}"
echo "[dispatcher-refresh] SIGTERM sent. claude-persistent.sh will relaunch a fresh session."
