#!/bin/bash
#===============================================================================
# cron-manage.sh - Safe crontab entry management
#
# Usage:
#   cron-manage.sh add    "# MARKER-COMMENT" "*/3 * * * * /path/to/script.sh"
#   cron-manage.sh remove "# MARKER-COMMENT"
#
# Subcommands:
#   add    - Add a cron entry idempotently. If an existing entry with the same
#            marker is present, it is replaced. Never clobbers unrelated entries.
#   remove - Remove all cron entries containing the given marker.
#
# Safety guarantee:
#   Both subcommands use the (crontab -l | grep -v MARKER; ...) | crontab -
#   pattern to avoid overwriting the entire crontab. Raw `echo | crontab -`
#   usage is prohibited — always use this script instead.
#
# Prerequisites:
#   The calling user must be in the crontab group (or the system must allow
#   crontab writes via the setgid bit on /usr/bin/crontab). If you see a
#   "mkstemp: Permission denied" error, run upgrade.sh which applies Migration 46
#   to add the lobster user to the crontab group, or run manually:
#     sudo usermod -aG crontab $USER
#   The change takes effect on the next login (or via: newgrp crontab).
#===============================================================================

set -euo pipefail

# Write a new crontab from stdin, capturing stderr to provide an actionable
# error message when crontab writes fail due to group permissions.
crontab_write() {
    local tmp_err
    tmp_err=$(mktemp)
    if ! crontab - 2>"$tmp_err"; then
        local err_text
        err_text=$(cat "$tmp_err")
        rm -f "$tmp_err"
        if echo "$err_text" | grep -qi "permission denied\|mkstemp\|cannot create"; then
            echo "Error: crontab write failed — $err_text" >&2
            echo "" >&2
            echo "Fix: the lobster user needs to be in the crontab group." >&2
            echo "  sudo usermod -aG crontab \$USER" >&2
            echo "  newgrp crontab  (or log out and back in)" >&2
            echo "" >&2
            echo "Alternatively, run upgrade.sh which applies Migration 46 automatically." >&2
        else
            echo "$err_text" >&2
        fi
        return 1
    fi
    rm -f "$tmp_err"
}

usage() {
    cat >&2 <<EOF
Usage:
  $(basename "$0") add    "<marker>" "<full cron entry including marker>"
  $(basename "$0") remove "<marker>"

Examples:
  $(basename "$0") add "# LOBSTER-SELF-CHECK" "*/3 * * * * /home/lobster/lobster/scripts/periodic-self-check.sh # LOBSTER-SELF-CHECK"
  $(basename "$0") remove "# LOBSTER-SELF-CHECK"
EOF
    exit 1
}

if [[ $# -lt 2 ]]; then
    usage
fi

SUBCOMMAND="$1"
MARKER="$2"

case "$SUBCOMMAND" in
    add)
        if [[ $# -lt 3 ]]; then
            echo "Error: 'add' requires a marker and a full cron entry." >&2
            usage
        fi
        ENTRY="$3"
        # Remove any existing entry with the same marker, then append the new one.
        # The `|| true` guards against grep returning exit code 1 when no lines match.
        (crontab -l 2>/dev/null | grep -vF "$MARKER" || true; echo "$ENTRY") | crontab_write
        echo "Cron entry added: $ENTRY"
        ;;

    remove)
        # Strip all lines containing the marker. If none match, crontab is unchanged.
        (crontab -l 2>/dev/null | grep -vF "$MARKER" || true) | crontab_write
        echo "Cron entry removed (marker: $MARKER)"
        ;;

    *)
        echo "Error: unknown subcommand '$SUBCOMMAND'" >&2
        usage
        ;;
esac
