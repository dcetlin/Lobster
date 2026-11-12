#!/bin/bash
# cleanup-hook-fires.sh — Remove orphaned /tmp/lobster-hook-fires-* files
# older than 24 hours. These are counter files written by
# hooks/require-write-result.py to track SubagentStop retries; they become
# orphaned when the parent agent session ends without cleanup.
#
# Context: upstream SiderealPress/lobster#2175 (ghost subagent recovery spam).
# This does not fix the root cause (phantom SubagentStop fires from the CC
# harness) but prevents orphaned counter files from accumulating.

set -euo pipefail

find /tmp -maxdepth 1 -name "lobster-hook-fires-*" -type f -mmin +1440 -delete 2>/dev/null || true

count=$(find /tmp -maxdepth 1 -name "lobster-hook-fires-*" -type f 2>/dev/null | wc -l)
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cleanup-hook-fires: $count file(s) remaining"
