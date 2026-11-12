#!/bin/bash
#===============================================================================
# arm-detached-watchdog.sh — launch a self-restart/cutover watchdog OUTSIDE
# the calling service's systemd cgroup, so it survives that service restarting.
#
# WHY THIS EXISTS (postbounce #10)
#   A one-shot watchdog armed with bare `setsid`/`nohup`/`disown` escapes the
#   POSIX session and process group, but does NOT escape the systemd CGROUP.
#   `systemctl restart <unit>` sweeps the unit's entire control group by
#   default (KillMode=control-group, the systemd default) — including any
#   detached-but-still-in-cgroup child, regardless of setsid. A watchdog
#   armed to survive exactly that restart can be killed BY the restart it
#   exists to observe.
#
#   Observed 2026-08-05: a cutover watchdog (mcp-cutover-watchdog.sh) armed
#   via bare setsid died ~98s into its 210s observation window during a
#   `systemctl restart lobster-claude.service` bounce. It got lucky — the
#   cutover was healthy, so the missing auto-revert didn't matter — but if
#   the cutover had failed, there would have been no safety net. See
#   docs/mcp-architecture.md ("Detached Watchdogs" section) for the full
#   incident writeup.
#
# THE FIX
#   Launch the watchdog as its own transient systemd service unit via
#   `systemd-run`, not as a detached child of the calling shell. A
#   `systemd-run --unit=<name> --collect -- <command>` invocation:
#     - runs the command under /system.slice/<name>.service — a cgroup that
#       is a SIBLING of the service being restarted, not a descendant of it.
#       `systemctl restart lobster-claude.service`'s KillMode=control-group
#       sweep only touches lobster-claude.service's own cgroup tree.
#     - detaches immediately: `systemd-run` returns as soon as the transient
#       unit is accepted, stdout/stderr of the launched command go to the
#       journal (`journalctl -u <name>`), and the command keeps running with
#       no controlling terminal — no setsid/nohup/disown needed.
#     - `--collect` garbage-collects the transient unit definition once the
#       command exits, so repeated arms don't accumulate dead unit files.
#   Requires passwordless sudo for the `lobster` user (already configured on
#   this host) because the transient unit is registered with the SYSTEM
#   manager (not a user session — this process may be launched from within
#   a service with no XDG_RUNTIME_DIR / D-Bus user session available).
#
# USAGE
#   source scripts/lib/arm-detached-watchdog.sh
#   arm_detached_watchdog <unit-name> <command> [args...]
#
#   <unit-name> must be a valid systemd unit name fragment (alphanumeric,
#   '-', '_', '.' — no spaces). Pick something unique per arm (e.g. include
#   a timestamp) if the same watchdog script might be armed more than once
#   without waiting for the previous run to finish and be collected.
#
# EXAMPLE
#   source "$(dirname "$0")/lib/arm-detached-watchdog.sh"
#   arm_detached_watchdog "mcp-cutover-watchdog-$(date -u +%s)" \
#       /home/lobster/lobster-workspace/scripts/mcp-cutover-watchdog.sh
#
# VERIFYING A WATCHDOG SURVIVED A RESTART
#   systemctl status <unit-name>.service   # still "active (running)" after the bounce
#   journalctl -u <unit-name>.service      # watchdog's own log output
#===============================================================================

# arm_detached_watchdog UNIT_NAME CMD [ARGS...]
#
# Launches CMD (with ARGS) as a transient, detached systemd service named
# UNIT_NAME.service, outside the caller's own service cgroup. Returns
# non-zero (and prints an error to stderr) if systemd-run itself fails to
# accept the unit — this only reports launch failure, not the watchdog's
# eventual exit status (it is detached; check its own log for that).
arm_detached_watchdog() {
    local unit_name="$1"
    shift
    if [ -z "$unit_name" ] || [ "$#" -eq 0 ]; then
        echo "[ERROR] arm_detached_watchdog: usage: arm_detached_watchdog <unit-name> <command> [args...]" >&2
        return 1
    fi

    if ! sudo -n systemd-run \
            --unit="$unit_name" \
            --collect \
            --description="Detached watchdog: $unit_name" \
            -- "$@"; then
        echo "[ERROR] arm_detached_watchdog: systemd-run failed to launch '$unit_name' (command: $*)" >&2
        return 1
    fi

    echo "[arm-detached-watchdog] Armed '$unit_name.service' (systemd.slice sibling, survives restarts of the calling service). Logs: journalctl -u ${unit_name}.service"
}
