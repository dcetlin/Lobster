#!/bin/bash
#===============================================================================
# sync-upstream.sh
#
# Syncs dcetlin/Lobster fork with upstream SiderealPress/lobster.
#
# Behavior:
#   1. Fetch upstream/main
#   2. Attempt git merge
#   3. Clean merge → push to origin, log silently
#   4. Conflicts → abort merge, write an inbox message for Dan to review
#
# Usage:
#   ./sync-upstream.sh [--chat-id <id>] [--dry-run]
#
# Options:
#   --chat-id <id>  Telegram chat_id for conflict alerts (default: 8075091586)
#   --dry-run       Show what would happen without making any changes
#
# Exit codes:
#   0 - Success (clean merge or already up to date)
#   1 - Conflict alert sent (merge aborted, human judgment needed)
#   2 - General error
#===============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
LOG_FILE="$WORKSPACE_DIR/logs/upstream-sync.log"
CHAT_ID="8075091586"
DRY_RUN=false
LOCK_FILE="/tmp/lobster-upstream-sync.lock"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --chat-id)
            CHAT_ID="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
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
    exit "${2:-2}"
}

# ---------------------------------------------------------------------------
# Lock management
# ---------------------------------------------------------------------------

acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        local pid
        pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            die "Another sync is already running (PID: $pid)" 2
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
# Safety check: abort any in-progress merge before starting
# ---------------------------------------------------------------------------

abort_stale_merge() {
    if [ -f "$LOBSTER_DIR/.git/MERGE_HEAD" ]; then
        log WARN "Found stale merge in progress — aborting before proceeding"
        if ! $DRY_RUN; then
            git -C "$LOBSTER_DIR" merge --abort 2>/dev/null || true
        fi
    fi
}

# ---------------------------------------------------------------------------
# Conflict analysis helpers (pure functions over git output)
# ---------------------------------------------------------------------------

# Extract conflicting file paths from git status output
conflicting_files() {
    git -C "$LOBSTER_DIR" diff --name-only --diff-filter=U 2>/dev/null
}

# Summarize upstream change for a single file (what did upstream do?)
upstream_diff_summary() {
    local file="$1"
    git -C "$LOBSTER_DIR" log --oneline upstream/main..MERGE_HEAD -- "$file" 2>/dev/null | head -5 \
        || echo "(no upstream commits found for $file)"
}

# Summarize our local change for a single file
local_diff_summary() {
    local file="$1"
    git -C "$LOBSTER_DIR" log --oneline "$(git -C "$LOBSTER_DIR" merge-base HEAD upstream/main)"..HEAD -- "$file" 2>/dev/null | head -5 \
        || echo "(no local commits found for $file)"
}

# Produce a conflict summary block for a single file
file_conflict_block() {
    local file="$1"
    local upstream_log
    local local_log
    upstream_log=$(git -C "$LOBSTER_DIR" log --oneline "$(git -C "$LOBSTER_DIR" merge-base HEAD MERGE_HEAD)"..MERGE_HEAD -- "$file" 2>/dev/null | head -5 || echo "(none)")
    local_log=$(git -C "$LOBSTER_DIR" log --oneline "$(git -C "$LOBSTER_DIR" merge-base HEAD MERGE_HEAD)"..HEAD -- "$file" 2>/dev/null | head -5 || echo "(none)")

    printf "\n  File: %s\n  Upstream commits:\n" "$file"
    echo "$upstream_log" | sed 's/^/    /'
    printf "  Our commits:\n"
    echo "$local_log" | sed 's/^/    /'
}

# Build the full conflict report as a plain string
build_conflict_report() {
    local conflicting="$1"
    local file_count
    file_count=$(echo "$conflicting" | wc -l | tr -d ' ')

    local report
    report="Upstream sync conflict: $file_count file(s) cannot be auto-merged and need your review.\n\n"
    report+="Conflicting files:\n"

    while IFS= read -r file; do
        report+=$(file_conflict_block "$file")
        report+="\n"
    done <<< "$conflicting"

    report+="\nUpstream branch: upstream/main (SiderealPress/lobster)\n"
    report+="Our branch: main (dcetlin/Lobster)\n"
    report+="The merge has been aborted. No changes were pushed.\n"
    report+="Please resolve conflicts manually or advise how to proceed."

    printf '%s' "$report"
}

# ---------------------------------------------------------------------------
# Inbox message writer
#
# Writes a message to ~/messages/inbox/ in the format the dispatcher picks up.
# This is a side-effecting function isolated at the system boundary.
# ---------------------------------------------------------------------------

write_inbox_alert() {
    local text="$1"
    local ts_ms
    ts_ms=$(date '+%s%3N')
    local msg_id="${ts_ms}_upstream_sync_conflict"
    local inbox_file="$MESSAGES_DIR/inbox/${msg_id}.json"

    mkdir -p "$MESSAGES_DIR/inbox"

    local timestamp
    timestamp=$(date --iso-8601=seconds)

    # Compose the JSON message using printf to avoid heredoc quoting issues
    printf '{\n  "id": "%s",\n  "source": "internal",\n  "chat_id": %s,\n  "type": "upstream_sync_conflict",\n  "text": %s,\n  "timestamp": "%s"\n}\n' \
        "$msg_id" \
        "$CHAT_ID" \
        "$(printf '%s' "$text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
        "$timestamp" \
        > "$inbox_file"

    log INFO "Conflict alert written to $inbox_file"
}

# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

main() {
    log INFO "=== Upstream sync started (dry_run=$DRY_RUN) ==="

    # Verify the repo exists
    if [ ! -d "$LOBSTER_DIR/.git" ]; then
        die "Lobster repo not found at $LOBSTER_DIR"
    fi

    acquire_lock
    trap cleanup_lock EXIT

    abort_stale_merge

    # Step 1: Fetch upstream
    log INFO "Fetching upstream (SiderealPress/lobster)..."
    if $DRY_RUN; then
        log INFO "[dry-run] Would: git fetch upstream"
    else
        if ! git -C "$LOBSTER_DIR" fetch upstream 2>&1 | tee -a "$LOG_FILE"; then
            die "Failed to fetch from upstream"
        fi
    fi

    # Step 2: Check if there's anything to merge
    if $DRY_RUN; then
        log INFO "[dry-run] Would check if upstream/main is ahead of HEAD"
    else
        local behind
        behind=$(git -C "$LOBSTER_DIR" rev-list HEAD..upstream/main --count 2>/dev/null || echo "0")
        if [ "$behind" = "0" ]; then
            log INFO "Already up to date with upstream/main — nothing to do"
            exit 0
        fi
        log INFO "upstream/main is $behind commit(s) ahead — merging"
    fi

    # Step 3: Attempt merge
    log INFO "Attempting merge of upstream/main..."
    local merge_exit=0
    if $DRY_RUN; then
        log INFO "[dry-run] Would: git merge upstream/main --no-edit"
    else
        git -C "$LOBSTER_DIR" merge upstream/main --no-edit 2>&1 | tee -a "$LOG_FILE" || merge_exit=$?
    fi

    if [ "$merge_exit" -eq 0 ]; then
        # Clean merge
        log INFO "Merge succeeded cleanly"

        if $DRY_RUN; then
            log INFO "[dry-run] Would: git push origin main"
        else
            log INFO "Pushing to origin (dcetlin/Lobster)..."
            if git -C "$LOBSTER_DIR" push origin main 2>&1 | tee -a "$LOG_FILE"; then
                local new_sha
                new_sha=$(git -C "$LOBSTER_DIR" rev-parse --short HEAD)
                log INFO "Push successful — HEAD is now $new_sha"
            else
                die "Push to origin failed after clean merge"
            fi
        fi

        log INFO "=== Upstream sync complete (clean merge) ==="
        exit 0
    fi

    # Merge had conflicts
    log WARN "Merge encountered conflicts"

    local conflicting
    conflicting=$(conflicting_files)

    if [ -z "$conflicting" ]; then
        # Unexpected: merge failed but no unresolved files — abort and bail
        git -C "$LOBSTER_DIR" merge --abort 2>/dev/null || true
        die "Merge failed with no identifiable conflicting files — check $LOG_FILE"
    fi

    log WARN "Conflicting files: $(echo "$conflicting" | tr '\n' ' ')"

    # Build conflict report before aborting (we need MERGE_HEAD for git log)
    local report
    report=$(build_conflict_report "$conflicting")

    # Abort the merge
    log INFO "Aborting merge — no changes will be pushed"
    if $DRY_RUN; then
        log INFO "[dry-run] Would: git merge --abort"
        log INFO "[dry-run] Would write conflict alert to inbox with this content:"
        echo "$report"
    else
        git -C "$LOBSTER_DIR" merge --abort 2>&1 | tee -a "$LOG_FILE"
        write_inbox_alert "$report"
    fi

    log WARN "=== Upstream sync ended with unresolvable conflicts — alert sent ==="
    exit 1
}

main "$@"
