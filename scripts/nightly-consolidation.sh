#!/bin/bash
# Nightly Consolidation - Cron wrapper script
#
# Injects a consolidation task message into the inbox for the running
# Claude session to process. Claude handles the actual synthesis using
# its MCP memory tools (memory_recent, mark_consolidated, etc.).
#
# No direct API calls are made here. Everything goes through Claude Code.
#
# Crontab entry:
#   0 3 * * * $HOME/lobster/scripts/nightly-consolidation.sh
#
# Dedup guard: if a consolidation message is already pending in the inbox,
# this script exits without writing a duplicate.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOBSTER_DIR="$(dirname "$SCRIPT_DIR")"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
INBOX="$MESSAGES_DIR/inbox"
TIMESTAMP=$(date +%s%3N)

# Ensure inbox directory exists
mkdir -p "$INBOX"

# Dedup guard: skip if a consolidation message is already pending
if ls "$INBOX"/*_consolidation.json 2>/dev/null | grep -q .; then
    echo "Consolidation message already pending in inbox. Skipping."
    exit 0
fi

# Inject a consolidation message for the running Claude session
cat > "$INBOX/${TIMESTAMP}_consolidation.json" << EOF
{
  "id": "${TIMESTAMP}_consolidation",
  "source": "internal",
  "chat_id": 0,
  "type": "consolidation",
  "text": "NIGHTLY CONSOLIDATION: Review today's events using memory_recent(hours=24) and update canonical memory files. Steps:\n1. Call memory_recent(hours=24) to get all events from the past day\n2. Synthesize key themes, decisions, and action items\n3. Update memory/canonical/daily-digest.md with the synthesis\n4. Update memory/canonical/priorities.md if priorities changed\n5. Update relevant project files in memory/canonical/projects/\n6. Update people files if new relationship info emerged\n7. Mark all reviewed events as consolidated using mark_consolidated\n8. Update memory/canonical/handoff.md with current state\n\nCOHERENCE CHECK (required — include in daily-digest.md under 'Structural Coherence'):\nA. Compare active tasks (list_tasks) to stated priorities (priorities.md). Are they telling the same story? Name any task that exists but has no corresponding priority, or any priority that has no active task.\nB. Compare recent observations to canonical memory (handoff.md, project files). Has anything drifted? Name conflicts explicitly — do not smooth them over.\nC. Check active meta-threads (memory/meta-threads/): for each thread, has the open question been resolved by recent activity without the thread being updated? Flag stale threads by name.\nD. Compensating machinery: is there any workaround, patch, or TODO that exists because something is structurally misaligned upstream? Name it: 'This workaround exists because X is misaligned.'\nIf none of A–D surface anything: write 'Coherence check: no conflicts detected.' The section must always be present.",
  "timestamp": "$(date -Iseconds)"
}
EOF

echo "Consolidation message injected at $(date -Iseconds)"
