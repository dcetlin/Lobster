#!/usr/bin/env python3
"""
Kernel-patch monitor for CVE-2026-98432 (Linux IPv4 TCP UAF, CVSS 9.8).

Context: this VPS runs Ubuntu 24.04.5 / kernel 6.8.0-110-generic. The fix
lands in linux-image-generic >= 6.8.0-149; as of 2026-11-01 apt only offers
6.8.0-147 (unpatched). Dan approved install-instantly / reboot-on-GO:

  - Lobster owns this watch + auto-install the moment the fix ships.
  - atlas owns the immediate stopgap (nft rule + net.mptcp.enabled=0) and
    owns executing the eventual reboot (with ~5 min notice to Lobster).
  - Installing a kernel package does NOT reboot the box, so auto-installing
    is safe without a blind unattended reboot — Dan still gates the reboot
    itself via an explicit GO.

Detection condition: a linux-image-generic kernel >= 6.8.0-149 is either
already installed (`dpkg -l`) or available as the apt candidate
(`apt-cache policy linux-image-generic`) after `apt-get update`.

On first detection: `apt-get install -y` the fixed kernel (install only,
never reboot), then notify Dan via Telegram. Idempotency is state-file
based (see STATE_FILE) so the install + GO-request notification fires
exactly once; while reboot is still pending, at most one reminder/24h goes
out. Once the running kernel (`uname -r`) is itself >= 6.8.0-149, a single
"resolved" notice fires and the monitor goes permanently quiet.

Known limitation (by design, not a bug): there is no proactive-push path
to atlas from this script. The local-claude agent channel
(docs/reference/agent-channel.md) is reply-only, keyed to a request_id
atlas itself generates when it initiates contact — Lobster cannot write an
unsolicited message into atlas's inbox. So the "notify atlas" half of the
original ask is satisfied as: (a) the Dan-facing Telegram message explicitly
says the status is for atlas too and asks Dan to relay or wait for atlas's
next check-in, and (b) /home/lobster/monitoring/watched-things.yaml (the
registry atlas itself pointed to during CVE coordination) carries a
declarative row for this watch so atlas can see it's live and who owns it.
Live status itself stays in STATE_FILE, per that registry's own convention.

Cron schedule (hourly, offset from :00 to avoid clustering):
    7 * * * * cd ~/lobster && uv run scheduled-tasks/kernel-patch-monitor.py >> ~/lobster-workspace/scheduled-jobs/logs/kernel-patch-monitor.log 2>&1 # LOBSTER-KERNEL-PATCH-MONITOR

Type B dispatch: cron calls this script directly (no inbox message in, no
LLM round-trip) — the apt/dpkg check, install, and notification text are
all fully deterministic, and nothing about "notify atlas" gains real
push capability from LLM judgment either (see limitation above), so a
Type A job would add an LLM round-trip for no behavioral benefit. The
jobs.json `enabled` gate is checked at the top of main() so runtime
enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/kernel-patch-monitor.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.inbox_write import write_inbox_message  # noqa: E402
from src.utils.jobs import is_job_enabled  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("kernel-patch-monitor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JOB_NAME = "kernel-patch-monitor"
CVE_ID = "CVE-2026-98432"
TARGET_VERSION = "6.8.0-149"  # dpkg-comparable threshold; fix is >= this
APT_PACKAGE = "linux-image-generic"
INSTALL_PACKAGES = ["linux-image-generic", "linux-headers-generic"]
STATE_FILE = Path.home() / "lobster-workspace" / "data" / "kernel-patch-monitor-state.json"
ADMIN_CHAT_ID = 8075091586
REMINDER_INTERVAL = timedelta(hours=24)

_KERNEL_VERSION_RE = re.compile(r"^linux-image-(\d[\w.]*-\d+)-generic$")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def is_fixed(version: str, target: str = TARGET_VERSION) -> bool:
    """True if `version` is dpkg-comparable >= target."""
    if not version:
        return False
    result = subprocess.run(
        ["dpkg", "--compare-versions", version, "ge", target],
        capture_output=True,
    )
    return result.returncode == 0


def parse_candidate_version(policy_output: str) -> str | None:
    for line in policy_output.splitlines():
        line = line.strip()
        if line.startswith("Candidate:"):
            value = line.split(":", 1)[1].strip()
            if value == "(none)":
                return None
            return value
    return None


def parse_installed_kernel_versions(dpkg_list_output: str) -> list[str]:
    """Parse `dpkg-query -W -f` style lines: '<status> <package> <version>'.

    Only returns versions for packages actually in the "installed" state
    (dpkg status field 'ii', i.e. install ok installed) — not removed-but-
    config-remaining ('rc') entries, which litter this box's dpkg history.
    """
    versions = []
    for line in dpkg_list_output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        status, package, version = parts
        if status.strip() != "install ok installed":
            continue
        m = _KERNEL_VERSION_RE.match(package.strip())
        if m:
            versions.append(version.strip())
    return versions


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_file)


# ---------------------------------------------------------------------------
# Side-effecting boundary functions
# ---------------------------------------------------------------------------


def apt_get_update() -> tuple[bool, str]:
    result = subprocess.run(
        ["sudo", "apt-get", "update", "-qq"],
        capture_output=True, text=True, timeout=180,
    )
    ok = result.returncode == 0
    return ok, (result.stderr.strip() or result.stdout.strip())


def get_candidate_version(package: str) -> str | None:
    result = subprocess.run(
        ["apt-cache", "policy", package],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        log.warning("apt-cache policy %s failed: %s", package, result.stderr.strip())
        return None
    return parse_candidate_version(result.stdout)


def get_installed_kernel_versions() -> list[str]:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}\t${Package}\t${Version}\n",
         "linux-image-*-generic"],
        capture_output=True, text=True, timeout=30,
    )
    # Non-zero exit here just means "no packages matched the glob" on some
    # dpkg versions — treat as empty rather than an error.
    return parse_installed_kernel_versions(result.stdout)


def get_running_kernel() -> str:
    result = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=10)
    raw = result.stdout.strip()
    # "6.8.0-110-generic" -> "6.8.0-110" (strip the flavour suffix so it's
    # dpkg-comparable against TARGET_VERSION the same way package versions are).
    return re.sub(r"-generic$", "", raw)


def apt_get_install(packages: list[str]) -> tuple[bool, str]:
    result = subprocess.run(
        ["sudo", "apt-get", "install", "-y", *packages],
        capture_output=True, text=True, timeout=600,
    )
    ok = result.returncode == 0
    output = (result.stdout + "\n" + result.stderr).strip()
    return ok, output


def alert(text: str) -> None:
    write_inbox_message(JOB_NAME, ADMIN_CHAT_ID, text, datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(dry_run: bool) -> int:
    now = datetime.now(timezone.utc)
    state = load_state(STATE_FILE)

    # Monitor goes permanently quiet once resolved (reboot has happened).
    if state.get("resolved_at"):
        log.info("Already resolved at %s — nothing to do", state["resolved_at"])
        return 0

    ok, err = apt_get_update()
    if not ok:
        log.error("apt-get update failed: %s", err)
        print(f"ERROR: apt-get update failed: {err}", file=sys.stderr)
        return 2

    candidate = get_candidate_version(APT_PACKAGE)
    installed_versions = get_installed_kernel_versions()
    candidate_fixed = bool(candidate) and is_fixed(candidate)
    already_installed_fixed_versions = [v for v in installed_versions if is_fixed(v)]
    fixed_available = candidate_fixed or bool(already_installed_fixed_versions)

    summary = (
        f"{CVE_ID} kernel watch: candidate={candidate!r} "
        f"(fixed={candidate_fixed}), installed_kernels={installed_versions} "
        f"(fixed={already_installed_fixed_versions}), "
        f"fixed_kernel_installed_state={state.get('fixed_kernel_installed', False)}"
    )
    log.info(summary)
    print(summary)

    if dry_run:
        return 0

    if not fixed_available:
        state["last_checked_at"] = now.isoformat()
        save_state(STATE_FILE, state)
        log.info("Fixed kernel not yet available — nothing to do")
        return 0

    # ---- Fixed kernel is available (in apt, or already installed) ----

    if not state.get("fixed_kernel_installed"):
        installed_version = None
        if already_installed_fixed_versions:
            # Fixed kernel already present (e.g. unattended-upgrades beat us
            # to it) — nothing to apt-get install, just pick up the version.
            installed_version = max(already_installed_fixed_versions)
            log.info("Fixed kernel already installed: %s", installed_version)
        else:
            log.info("Installing fixed kernel via: apt-get install -y %s", INSTALL_PACKAGES)
            install_ok, install_output = apt_get_install(INSTALL_PACKAGES)
            if not install_ok:
                log.error("apt-get install failed: %s", install_output)
                alert(
                    f"[{CVE_ID}] Fixed kernel ({candidate}) is available but "
                    f"`apt-get install -y {' '.join(INSTALL_PACKAGES)}` FAILED. "
                    f"Will retry next hour. Error:\n{install_output[-1500:]}"
                )
                state["last_checked_at"] = now.isoformat()
                save_state(STATE_FILE, state)
                return 2
            installed_versions = get_installed_kernel_versions()
            fixed_now = [v for v in installed_versions if is_fixed(v)]
            installed_version = max(fixed_now) if fixed_now else candidate
            log.info("Install succeeded: %s", installed_version)

        state["fixed_kernel_installed"] = True
        state["installed_version"] = installed_version
        state["installed_at"] = now.isoformat()
        state["go_notified_at"] = now.isoformat()
        state["last_reminder_at"] = now.isoformat()
        state["last_checked_at"] = now.isoformat()
        save_state(STATE_FILE, state)

        alert(
            f"[{CVE_ID}] Fixed kernel {installed_version} installed — reboot "
            f"required to activate (install only, no auto-reboot per your "
            f"install-instantly/reboot-on-GO policy).\n\n"
            f"Reply GO to reboot (atlas owns execution, ~5 min notice to "
            f"Lobster) or HOLD.\n\n"
            f"Note: this is also the status atlas needs, but Lobster has no "
            f"proactive-push path to atlas (the local-claude agent channel "
            f"is reply-only, keyed to a request atlas itself initiates) — "
            f"please relay, or atlas will see this on its next check-in. "
            f"Registered as a declarative row in "
            f"/home/lobster/monitoring/watched-things.yaml in the meantime."
        )
        log.info("GO-request notification sent (first detection)")
        return 0

    # ---- Already installed + notified in a prior run — check for reboot ----

    running = get_running_kernel()
    if is_fixed(running):
        state["resolved_at"] = now.isoformat()
        state["last_checked_at"] = now.isoformat()
        save_state(STATE_FILE, state)
        alert(
            f"[{CVE_ID}] Resolved — running kernel is now {running} (fixed). "
            f"Reboot happened and the fix is active. This monitor is now "
            f"quiet permanently."
        )
        log.info("Resolved: running kernel %s is fixed", running)
        return 0

    last_reminder_at = state.get("last_reminder_at")
    due_for_reminder = True
    if last_reminder_at:
        try:
            last_dt = datetime.fromisoformat(last_reminder_at)
            due_for_reminder = (now - last_dt) >= REMINDER_INTERVAL
        except ValueError:
            due_for_reminder = True

    if due_for_reminder:
        alert(
            f"[{CVE_ID}] Still awaiting reboot GO — fixed kernel "
            f"{state.get('installed_version')} has been installed since "
            f"{state.get('installed_at')}, but running kernel is still "
            f"{running}. Reply GO to reboot (atlas owns execution) or HOLD."
        )
        state["last_reminder_at"] = now.isoformat()
        log.info("Daily reminder sent (still awaiting reboot)")

    state["last_checked_at"] = now.isoformat()
    save_state(STATE_FILE, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't install/notify/write state")
    args = parser.parse_args()

    if not args.dry_run and not is_job_enabled(JOB_NAME):
        log.info("Job disabled in jobs.json — skipping")
        return 0

    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
