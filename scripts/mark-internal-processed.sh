#!/bin/bash
INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
PROCESSED_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/processed"

mkdir -p "$PROCESSED_DIR"

count=0
find "$INBOX_DIR" -name '*.json' -type f -print0 2>/dev/null | while IFS= read -r -d '' f; do
    type=$(jq -r '.type // "unknown"' "$f" 2>/dev/null)
    
    if [[ "$type" == "subagent_result" ]] || [[ "$type" == "subagent_notification" ]] || [[ "$type" == "subagent_error" ]]; then
        basename=$(basename "$f")
        mv "$f" "$PROCESSED_DIR/$basename" 2>/dev/null && count=$((count + 1))
    fi
done

