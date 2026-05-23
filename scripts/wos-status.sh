#!/usr/bin/env bash
# wos-status.sh — Print active (non-terminal) UoW summary as JSON.
#
# Usage:
#   scripts/wos-status.sh [DB_PATH]
#
# Output: JSON array of non-terminal UoWs with id, posture, route_reason,
#         status, and hooks_applied. Reads from local SQLite DB only —
#         no gh CLI or GitHub API calls.
#
# The dispatcher uses this to answer "what's running?" without GitHub.

set -euo pipefail

DB_PATH="${1:-${REGISTRY_DB_PATH:-${HOME}/lobster-workspace/data/registry.db}}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

exec uv run --project "${REPO_ROOT}" python3 "${REPO_ROOT}/scripts/wos_active_summary.py" "${DB_PATH}"
