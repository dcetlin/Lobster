#!/bin/bash
set -o pipefail

MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
INBOX_DIR="$MESSAGES_DIR/inbox"
ARCHIVE_DIR="$MESSAGES_DIR/archive"
CLEANUP_LOG="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/inbox-cleanup.log"

# TIGHTENED retention policies (internal messages accumulating faster than expected)
SUBAGENT_RESULT_TTL=0  # DELETE IMMEDIATELY - internal routing only          # 1 hour (was 24h) - accumulating too fast
SUBAGENT_NOTIF_TTL=0    # DELETE IMMEDIATELY - internal routing only           # 1 hour (was 24h) - accumulating too fast
SESSION_LOST_TTL=10800            # 3 hours (was 6h)
SCHEDULED_JOB_TTL=21600           # 6 hours (was 24h)
CONSOLIDATION_TTL=21600           # 6 hours (was 24h)
INTERNAL_TTL=21600                # 6 hours (was 24h)
UNPARSEABLE_TTL=86400             # 24 hours (unchanged)
UUID_MESSAGE_TTL=7200             # 2 hours - new UUIDs from philosophy-harvest etc

DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --verbose) VERBOSE=true; shift ;;
        *) shift ;;
    esac
done

mkdir -p "$ARCHIVE_DIR"

log_cleanup() {
    local level="$1"
    shift
    local timestamp=$(date '+%Y-%m-%dT%H:%M:%SZ')
    echo "[$timestamp] [$level] $@" | tee -a "$CLEANUP_LOG"
}

get_message_info() {
    local f="$1"
    local now="$2"
    local type=$(jq -r '.type // "unknown"' "$f" 2>/dev/null)
    local source=$(jq -r '.source // "unknown"' "$f" 2>/dev/null)
    local file_time=$(stat -c %Y "$f" 2>/dev/null)
    local age=$((now - file_time))
    echo "$type||$source||$age"
}

should_delete() {
    local basename_f="$1"
    local type="$2"
    local age="$3"
    
    # Keep user messages forever
    case "$type" in
        message|photo|image|voice|audio|callback|text|document|video|animation)
            return 1
            ;;
    esac
    
    # TTL-based cleanup for internal messages
    if [[ "$type" == "subagent_result" ]]; then
        [[ $age -gt $SUBAGENT_RESULT_TTL ]] && return 0 || return 1
    elif [[ "$type" == "subagent_notification" ]]; then
        [[ $age -gt $SUBAGENT_NOTIF_TTL ]] && return 0 || return 1
    elif [[ "$basename_f" == session-lost* ]]; then
        [[ $age -gt $SESSION_LOST_TTL ]] && return 0 || return 1
    elif [[ "$basename_f" == *scheduled* ]]; then
        [[ $age -gt $SCHEDULED_JOB_TTL ]] && return 0 || return 1
    elif [[ "$basename_f" == consolidation* ]]; then
        [[ $age -gt $CONSOLIDATION_TTL ]] && return 0 || return 1
    elif [[ "$type" == "subagent_error" ]] || [[ "$type" == "system" ]] || [[ "$type" == "compact" ]]; then
        [[ $age -gt $INTERNAL_TTL ]] && return 0 || return 1
    elif [[ "$basename_f" =~ ^[a-f0-9]{8}-[a-f0-9]{4}- ]]; then
        # UUID-based messages (philosophy-harvest, etc.)
        [[ $age -gt $UUID_MESSAGE_TTL ]] && return 0 || return 1
    elif [[ "$type" == "unknown" ]]; then
        [[ $age -gt $UNPARSEABLE_TTL ]] && return 0 || return 1
    fi
    
    return 1
}

main() {
    # First mark internal messages as processed
    ~/lobster/scripts/mark-internal-processed.sh 2>/dev/null || true
    local now=$(date +%s)
    local deleted_count=0
    local total_scanned=0
    
    while IFS= read -r -d '' f; do
        local basename_f=$(basename "$f")
        total_scanned=$((total_scanned + 1))
        
        local info=$(get_message_info "$f" "$now")
        local type source age
        IFS='||' read -r type source age <<< "$info"
        
        if should_delete "$basename_f" "$type" "$age"; then
            if [[ "$DRY_RUN" != true ]]; then
                mv "$f" "$ARCHIVE_DIR/" 2>/dev/null
                [[ $? -eq 0 ]] && deleted_count=$((deleted_count + 1))
            fi
        fi
    done < <(find "$INBOX_DIR" -maxdepth 1 -name '*.json' -print0 2>/dev/null)
    
    local inbox_count=$(find "$INBOX_DIR" -maxdepth 1 -name '*.json' | wc -l)
    
    if [[ $inbox_count -gt 200 ]]; then
        log_cleanup "ERROR" "Inbox CRITICAL: $inbox_count messages (>200 threshold)"
    elif [[ $inbox_count -gt 100 ]]; then
        log_cleanup "WARN" "Inbox elevated: $inbox_count messages (>100 threshold)"
    fi
    
    if [[ "$DRY_RUN" != true ]]; then
        log_cleanup "INFO" "Cleanup: scanned $total_scanned, archived $deleted_count, inbox now at $inbox_count"
    fi
}

main
