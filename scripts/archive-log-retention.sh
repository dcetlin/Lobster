#!/bin/bash
# archive-log-retention.sh — 14-day retention policy for ~/lobster-workspace/logs/archive/
#
# logs/archive/ is populated daily by export-logs.py (LOBSTER-LOG-EXPORT cron,
# ~27M/day) into YYYY-MM-DD/ subdirectories, each holding a snapshot of
# audit.jsonl and observations.log. Nothing else consumes these snapshots, so
# a straightforward mtime-based prune is safe. Deletes files older than 14
# days, then removes any now-empty date directories.
set -euo pipefail

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
ARCHIVE_DIR="$WORKSPACE/logs/archive"
RETENTION_DAYS=14
LOG="$WORKSPACE/logs/archive-log-retention.log"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG" 2>&1; }

if [ ! -d "$ARCHIVE_DIR" ]; then
    log "archive-log-retention: skipped, $ARCHIVE_DIR does not exist"
    exit 0
fi

DELETED=0
BYTES=0
while IFS= read -r -d '' f; do
    SZ=$(stat -c %s "$f" 2>/dev/null || echo 0)
    if rm -f "$f"; then
        DELETED=$((DELETED + 1))
        BYTES=$((BYTES + SZ))
    fi
done < <(find "$ARCHIVE_DIR" -type f -mtime +"$RETENTION_DAYS" -print0)

# Remove now-empty date directories left behind by the prune above.
EMPTY_DIRS=0
while IFS= read -r -d '' d; do
    if rmdir "$d" 2>/dev/null; then
        EMPTY_DIRS=$((EMPTY_DIRS + 1))
    fi
done < <(find "$ARCHIVE_DIR" -mindepth 1 -maxdepth 1 -type d -empty -print0)

log "archive-log-retention: deleted_files=$DELETED bytes=$BYTES empty_dirs_removed=$EMPTY_DIRS retention_days=$RETENTION_DAYS"
