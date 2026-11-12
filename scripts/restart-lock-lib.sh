#!/usr/bin/env bash
# restart-lock-lib.sh — Shared restart-coordination lock (issue #1537).
#
# Problem: a manual restart (operator-run `restart-mcp.sh` or
# `dispatcher-refresh.sh`) and an automatic restart (health-check-v3.sh's
# do_restart() or check_session_age()) can fire concurrently. Both paths
# disrupt the same dispatcher session — one restarts the MCP server the
# dispatcher talks to, the others restart or SIGTERM the dispatcher process
# itself — so overlapping execution can leave the system in an inconsistent
# state (e.g. two respawn attempts racing, or a restart landing mid-restart).
# A failure-injection test surfaced this collision; see issue #1537.
#
# This lock is advisory and non-blocking: it does not queue. Whichever path
# acquires the lock first proceeds; the other path skips its restart action
# for this invocation and lets the caller re-evaluate (health-check-v3.sh
# re-runs every 4 minutes via cron; a manual script can simply be re-run).
# This mirrors health-check-v3.sh's own acquire_lock() pattern (used to
# prevent concurrent health-check runs), extended here to cover coordination
# *across* the different restart-triggering scripts.
#
# For the lock to actually provide mutual exclusion, every restart-triggering
# path must contend for the SAME lock file:
#   - scripts/restart-mcp.sh                          (manual MCP restart)
#   - scripts/dispatcher-refresh.sh                    (manual dispatcher SIGTERM refresh)
#   - scripts/health-check-v3.sh do_restart()          (automatic full lobster-claude restart)
#   - scripts/health-check-v3.sh check_session_age()   (automatic proactive SIGTERM refresh)
#
# health-check-v3.sh does not source this file (it is a large, mostly
# self-contained script whose functions are unit-tested by extracting single
# function bodies via `sed`). Instead it implements the identical flock
# sequence inline against the same RESTART_COORDINATION_LOCK_FILE path
# convention, so the two implementations still contend on one real file.
#
# Usage (from a sourcing script):
#   source "$(dirname "${BASH_SOURCE[0]}")/restart-lock-lib.sh"
#   if ! acquire_restart_coordination_lock; then
#       echo "Another restart is already in progress — aborting." >&2
#       exit 1
#   fi
#   # ... perform the restart ...
#   release_restart_coordination_lock   # optional — also released on process exit

RESTART_COORDINATION_LOCK_FILE="${LOBSTER_RESTART_LOCK:-${LOBSTER_MESSAGES:-$HOME/messages}/config/restart-coordination.lock}"
RESTART_COORDINATION_LOCK_FD=201

# Attempt to acquire the shared restart-coordination lock without blocking.
# Returns 0 if acquired, 1 if another process already holds it.
acquire_restart_coordination_lock() {
    mkdir -p "$(dirname "$RESTART_COORDINATION_LOCK_FILE")" 2>/dev/null
    eval "exec ${RESTART_COORDINATION_LOCK_FD}>\"\$RESTART_COORDINATION_LOCK_FILE\"" || return 1
    if ! flock -n "$RESTART_COORDINATION_LOCK_FD"; then
        return 1
    fi
    return 0
}

# Release the lock explicitly. Not required for correctness (the lock is
# released automatically when the holding process exits and its file
# descriptors close), but useful for long-running callers that want to free
# the lock before they themselves finish.
release_restart_coordination_lock() {
    eval "exec ${RESTART_COORDINATION_LOCK_FD}>&-" 2>/dev/null || true
}
