#!/bin/bash
#===============================================================================
# Periodic Self-Check (REMOVED)
#
# This script has been retired. The periodic self-check inbox injection was
# generating ~20 no-op messages per hour that the dispatcher silently discarded.
# Subagent completion is now handled entirely by the reconciler, which delivers
# structured subagent_result / subagent_notification messages directly.
#
# This stub exits immediately so any stale cron entries are harmless.
# Migration 21 in upgrade.sh removes the cron entry on existing installs.
#===============================================================================

exit 0
