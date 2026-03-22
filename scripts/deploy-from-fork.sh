#!/bin/bash
#===============================================================================
# deploy-from-fork.sh
#
# Pulls latest commits from dcetlin/Lobster main into the live ~/lobster/
# installation. This is the "deploy" step: once a PR is merged to the fork's
# main branch, this script propagates those changes to the running system.
#
# Run by the Lobster scheduled job "deploy-from-fork" every 15 minutes.
#
# Behavior:
#   1. Fetch origin/main (dcetlin/Lobster)
#   2. If HEAD is already up to date, exit silently
#   3. Attempt git pull --ff-only (fast-forward only — never a merge commit)
#   4. Success → log commit hash, exit 0
#   5. Non-fast-forward (diverged history) → log warning, do not force, exit 1
#
# Safety:
#   - Lock file prevents concurrent runs
#   - Fast-forward only: never rewrites history or creates merge commits
#   - Does not restart services (operator's responsibility or future enhancement)
#
# Usage:
#   ./deploy-from-fork.sh [--dry-run]
#
# Exit codes:
#   0 - Success (up to date or cleanly pulled)
#   1 - Pull failed or non-fast-forward (needs investigation)
#   2 - Lock conflict (another deploy is running)
#===============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
LOG_FILE="$WORKSPACE_DIR/logs/deploy-from-fork.log"
LOCK_FILE="/tmp/lobster-deploy-from-fork.lock"
DRY_RUN=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            sed -n '2,22p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    local level="$1"
    shift
    local msg="$*"
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] [$level] $msg" | tee -a "$LOG_FILE"
}

die() {
    log ERROR "$1"
    cleanup_lock
    exit "${2:-1}"
}

# ---------------------------------------------------------------------------
# Lock management
# ---------------------------------------------------------------------------

acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        local pid
        pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            log WARN "Another deploy is running (PID: $pid) — skipping"
            exit 2
        fi
        log WARN "Stale lock file found, removing"
        rm -f "$LOCK_FILE"
    fi
    echo $$ > "$LOCK_FILE"
}

cleanup_lock() {
    rm -f "$LOCK_FILE"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    # Verify repo exists
    if [ ! -d "$LOBSTER_DIR/.git" ]; then
        die "Lobster repo not found at $LOBSTER_DIR"
    fi

    acquire_lock
    trap cleanup_lock EXIT

    # Step 1: Fetch origin/main to get latest refs without applying changes yet
    log INFO "Fetching origin/main (dcetlin/Lobster)..."
    if $DRY_RUN; then
        log INFO "[dry-run] Would: git fetch origin main"
    else
        if ! git -C "$LOBSTER_DIR" fetch origin main --quiet 2>&1 | tee -a "$LOG_FILE"; then
            die "Failed to fetch from origin"
        fi
    fi

    # Step 2: Check if there's anything to pull
    local behind
    if $DRY_RUN; then
        log INFO "[dry-run] Would check if origin/main is ahead of HEAD"
        log INFO "[dry-run] Skipping actual pull"
        log INFO "=== Deploy check complete (dry-run) ==="
        exit 0
    fi

    behind=$(git -C "$LOBSTER_DIR" rev-list HEAD..origin/main --count 2>/dev/null || echo "0")

    if [ "$behind" = "0" ]; then
        # Silent exit — no new commits, nothing to report
        exit 0
    fi

    local prev_sha
    prev_sha=$(git -C "$LOBSTER_DIR" rev-parse --short HEAD)
    log INFO "origin/main is $behind commit(s) ahead of HEAD ($prev_sha) — pulling"

    # Step 3: Fast-forward pull only
    # --ff-only ensures we never create a merge commit or rewrite history.
    # If the local branch has diverged (e.g., a manual commit on the server),
    # this will fail with a clear error instead of silently creating a mess.
    if git -C "$LOBSTER_DIR" pull --ff-only origin main 2>&1 | tee -a "$LOG_FILE"; then
        local new_sha
        new_sha=$(git -C "$LOBSTER_DIR" rev-parse --short HEAD)
        log INFO "Deploy successful: $prev_sha -> $new_sha ($behind new commit(s))"
        log INFO "=== Deploy complete ==="
        exit 0
    else
        die "Pull failed — history may have diverged. Manual intervention needed. Check $LOG_FILE" 1
    fi
}

main "$@"
