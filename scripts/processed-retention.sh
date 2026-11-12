#!/bin/bash
# processed-retention.sh — 30-day retention policy for ~/messages/processed/ and ~/messages/failed/
# Moves files older than 30 days to archive dirs for stage-2 deletion.
# processed/ added by hygiene-execute-20260704 (2026-07-04).
# failed/ added by kill-ralph-loop (2026-07-04).
set -euo pipefail

MESSAGES_BASE="${LOBSTER_MESSAGES:-$HOME/messages}"
PROCESSED_DIR="$MESSAGES_BASE/processed"
PROCESSED_ARCHIVE_DIR="$MESSAGES_BASE/processed-archive"
FAILED_DIR="$MESSAGES_BASE/failed"
FAILED_ARCHIVE_DIR="$MESSAGES_BASE/failed-archive"
LOG="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/processed-retention.log"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG" 2>&1; }

mkdir -p "$PROCESSED_ARCHIVE_DIR" "$FAILED_ARCHIVE_DIR"

CUTOFF=$(date -d '30 days ago' +%s 2>/dev/null || date -v-30d +%s 2>/dev/null)

# --- processed/ retention ---
MOVED=0
ERRORS=0
for f in "$PROCESSED_DIR"/*.json; do
    [ -e "$f" ] || continue
    FILE_TIME=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)
    if [ "$FILE_TIME" -lt "$CUTOFF" ]; then
        mv "$f" "$PROCESSED_ARCHIVE_DIR/" 2>/dev/null && MOVED=$((MOVED + 1)) || ERRORS=$((ERRORS + 1))
    fi
done
log "processed-retention: moved=$MOVED errors=$ERRORS cutoff=$(date -d @$CUTOFF -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -r $CUTOFF -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"

# --- failed/ retention ---
FAILED_MOVED=0
FAILED_ERRORS=0
for f in "$FAILED_DIR"/*.json; do
    [ -e "$f" ] || continue
    FILE_TIME=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)
    if [ "$FILE_TIME" -lt "$CUTOFF" ]; then
        mv "$f" "$FAILED_ARCHIVE_DIR/" 2>/dev/null && FAILED_MOVED=$((FAILED_MOVED + 1)) || FAILED_ERRORS=$((FAILED_ERRORS + 1))
    fi
done
log "failed-retention: moved=$FAILED_MOVED errors=$FAILED_ERRORS cutoff=$(date -d @$CUTOFF -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -r $CUTOFF -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"
