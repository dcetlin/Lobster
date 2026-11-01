#!/usr/bin/env python3
"""
Tier 1 intrusion-detection alarms for the VPS.

Monitors five state-change events and alerts Dan when changes are detected.
Alerts are advisory only — no blocking, no access denial. Each alert should
mean "a human should look."

The five state changes:
1. Accepted SSH login from any IP other than TRUSTED_SSH_IP (configurable).
   Alert on first detection, never on repeated logins from the same unauthorized IP.
   Do NOT allowlist external scanners (e.g., 139.19.117.197).

2. New listening TCP/UDP socket compared with a baseline.
   Covers the host and each docker container network namespace.
   First run writes the baseline silently; subsequent runs alert only on changes.

3. Changes to system identity files: authorized_keys (all users), /etc/passwd,
   /etc/group, /etc/shadow (hash only, not contents), /etc/sudoers, /etc/sudoers.d/*.
   Uses SHA-256 content hashes to track changes.

4. Kernel oops, BUG, general protection fault, or KASAN lines in journal/dmesg
   since the last check. Alert if any such lines appear.

5. The `inet cve_2026_98432` nft table drop counter going above 0 or increasing.
   Alert if counter > 0 or has increased since last check.

Type B dispatch (cron-direct):
    Checks are deterministic state-comparisons, not LLM inference tasks.
    Type A would add round-trip latency for no gain in judgment.
    Cron invokes this script directly; jobs.json enabled gate is checked at startup.

Cron schedule (every 5 minutes):
    */5 * * * * cd ~/lobster && uv run scheduled-tasks/intrusion-alarms.py >> ~/lobster-workspace/scheduled-jobs/logs/intrusion-alarms.log 2>&1 # LOBSTER-INTRUSION-ALARMS

State directory: ~/lobster-workspace/data/intrusion-alarms/
    - baselines/ — baseline files for sockets and identity-file hashes
    - state.json — last-seen values for dmesg and nft counter

Run standalone:
    uv run ~/lobster/scheduled-tasks/intrusion-alarms.py [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
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
log = logging.getLogger("intrusion-alarms")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JOB_NAME = "intrusion-alarms"
ADMIN_CHAT_ID = 8075091586
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# SSH: trusted IP (configurable).
# Note: May be overridden via INTRUSION_ALARM_TRUSTED_SSH_IP env var.
TRUSTED_SSH_IP = os.environ.get("INTRUSION_ALARM_TRUSTED_SSH_IP", "107.208.10.157")

# Data paths
STATE_DIR = Path.home() / "lobster-workspace" / "data" / "intrusion-alarms"
STATE_FILE = STATE_DIR / "state.json"
BASELINES_DIR = STATE_DIR / "baselines"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


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


def sha256_file(path: Path, use_sudo: bool = False) -> str | None:
    """Return SHA-256 hash of file contents, or None if file doesn't exist.

    If use_sudo=True, attempts to read via sudo if normal file access fails.
    This is needed for root-owned files like /root/.ssh/authorized_keys and
    /etc/sudoers.d/* files.
    """
    if not path.exists():
        return None

    content_bytes = None
    try:
        # Try reading as regular user first
        with open(path, "rb") as f:
            content_bytes = f.read()
    except (OSError, IOError) as e:
        if not use_sudo:
            return None
        # Fall back to sudo if permission denied
        try:
            result = subprocess.run(
                ["sudo", "cat", str(path)],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                content_bytes = result.stdout
            else:
                log.debug("sudo cat %s failed: %s", path, result.stderr.decode(errors='ignore').strip())
                return None
        except (OSError, subprocess.TimeoutExpired) as sudo_e:
            log.debug("Failed to read %s with sudo: %s", path, sudo_e)
            return None

    if content_bytes is None:
        return None

    try:
        h = hashlib.sha256()
        h.update(content_bytes)
        return h.hexdigest()
    except Exception:
        return None


def sha256_shadow_hash(path: Path) -> str | None:
    """Extract and hash only the /etc/shadow hashes, not plaintext passwords.

    /etc/shadow lines are: login:hash:lastchanged:mindays:maxdays:warndays:inactivedays:expiredate:reserved
    We hash only the hash field (field 1, 0-indexed) to detect password changes
    without leaking password hashes in our state files.

    Uses sudo to read /etc/shadow if normal file access fails.
    """
    if not path.exists():
        return None

    content = None
    try:
        # Try reading as regular user first
        with open(path, "r") as f:
            content = f.read()
    except (OSError, IOError):
        # Fall back to sudo if permission denied
        try:
            result = subprocess.run(
                ["sudo", "cat", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                content = result.stdout
            else:
                log.debug("sudo cat %s failed: %s", path, result.stderr.strip())
                return None
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("Failed to read %s with sudo: %s", path, e)
            return None

    if not content:
        return None

    try:
        h = hashlib.sha256()
        for line in content.splitlines():
            parts = line.rstrip("\n").split(":")
            if len(parts) >= 2:
                # Include login + hash (field 0 + field 1)
                h.update(f"{parts[0]}:{parts[1]}\n".encode())
        return h.hexdigest()
    except Exception:
        return None


def send_alert_via_telegram(text: str) -> bool:
    """Send alert directly to Telegram via Bot API.

    Returns True if successful, False otherwise.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not configured; cannot send alert directly")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                log.info("Alert sent directly via Telegram API (message_id=%s)", result.get("result", {}).get("message_id"))
                return True
            else:
                log.warning("Telegram API returned error: %s", result.get("description", "unknown"))
                return False
    except urllib.error.URLError as e:
        log.error("Failed to send alert via Telegram API: %s", e)
        return False
    except (json.JSONDecodeError, OSError, TimeoutError) as e:
        log.error("Error communicating with Telegram API: %s", e)
        return False


def alert(text: str) -> str:
    """Send an intrusion alert to Dan via Telegram.

    Attempts direct Bot API send first (immediate, not dispatcher-dependent).
    Falls back to write_inbox_message if direct send fails.
    Prepends "🚨" emoji for visual urgency.
    """
    urgent_text = f"🚨 {text}"

    # Try direct Telegram API send first
    if send_alert_via_telegram(urgent_text):
        # Direct send succeeded; optionally write a non-urgent inbox note
        # so dispatcher has context without re-sending.
        # For now, skip inbox note to avoid clutter on successful sends.
        return "direct-send"

    # Fall back to dispatcher queue
    log.warning("Direct Telegram send failed, falling back to inbox queue")
    try:
        msg_id = write_inbox_message(
            JOB_NAME, ADMIN_CHAT_ID,
            f"ALERT: {text}",  # Keep ALERT: for dispatcher queue marking
            datetime.now(timezone.utc).isoformat()
        )
        return msg_id
    except Exception as e:
        log.error("Failed to queue alert via inbox: %s", e)
        return "failed"


# ---------------------------------------------------------------------------
# Check 1: Unauthorized SSH logins
# ---------------------------------------------------------------------------


def check_unauthorized_ssh_logins() -> str | None:
    """Alert on any accepted SSH login from IP != TRUSTED_SSH_IP.

    Reads SSH service journal via journalctl for "Accepted" lines, extracts remote IP.
    Tracks seen IPs in state; alerts only on first detection of a new IP.
    """
    state = load_state(STATE_FILE)
    seen_ips = set(state.get("ssh_unauthorized_ips_seen", []))

    unauthorized_ips = set()
    try:
        # Use journalctl -u ssh to get SSH service logs (more reliable than auth.log)
        result = subprocess.run(
            ["sudo", "journalctl", "-u", "ssh", "-n", "10000", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.warning("journalctl -u ssh failed: %s", result.stderr.strip())
            return None

        for line in result.stdout.splitlines():
            # Match: "Accepted publickey for user from 107.208.10.1 port 12345 ssh2"
            m = re.search(
                r"Accepted \w+ for .+ from ([0-9a-f.:]+) port \d+",
                line,
                re.IGNORECASE,
            )
            if m:
                ip = m.group(1)
                if ip != TRUSTED_SSH_IP:
                    unauthorized_ips.add(ip)
    except subprocess.TimeoutExpired:
        log.warning("journalctl -u ssh timeout")
        return None
    except (OSError, IOError) as e:
        log.error("Failed to read SSH journal: %s", e)
        return None

    # Detect new unauthorized IPs
    new_ips = unauthorized_ips - seen_ips
    if new_ips:
        state["ssh_unauthorized_ips_seen"] = list(seen_ips | unauthorized_ips)
        save_state(STATE_FILE, state)
        return alert(
            f"[Intrusion Alert] Accepted SSH login(s) from unauthorized IP(s):\n\n"
            f"{', '.join(sorted(new_ips))}\n\n"
            f"Trusted IP: {TRUSTED_SSH_IP}\n"
            f"All seen unauthorized IPs: {', '.join(sorted(unauthorized_ips)) if unauthorized_ips else '(none yet)'}"
        )

    return None


# ---------------------------------------------------------------------------
# Check 2: New listening sockets
# ---------------------------------------------------------------------------


def get_listening_sockets(container_name: str | None = None) -> set[str]:
    """Get set of listening TCP/UDP sockets for the host or a docker container.

    Format: 'protocol:port' (e.g., 'tcp:22', 'udp:53').
    container_name=None means the host namespace.

    For containers, uses nsenter to enter the container's network namespace
    instead of docker exec, as netstat may not be installed in containers.
    Uses `ss` (socket statistics) which is more likely to be available.
    """
    sockets = set()
    try:
        if container_name:
            # Get container PID via docker inspect
            inspect_result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Pid}}", container_name],
                capture_output=True, text=True, timeout=5,
            )
            if inspect_result.returncode != 0:
                log.warning("Failed to get PID for container %s", container_name)
                return set()

            container_pid = inspect_result.stdout.strip()
            if not container_pid or container_pid == "0":
                log.warning("Container %s is not running or has invalid PID", container_name)
                return set()

            # Use nsenter to enter the container's network namespace and run ss
            cmd = ["sudo", "nsenter", "-t", container_pid, "-n", "ss", "-tulnH"]
        else:
            # Host namespace: use ss directly
            cmd = ["ss", "-tulnH"]

        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.warning("ss command failed for %s: %s", container_name or "host", result.stderr.strip())
            return set()

        # ss -tulnH output format: "Netid  State  Recv-Q Send-Q Local Address:Port  Peer Address:Port"
        # Lines with LISTEN state: "tcp    LISTEN  0      128    0.0.0.0:22         0.0.0.0:*"
        for line in result.stdout.splitlines():
            if "LISTEN" not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            proto = parts[0].lower()  # tcp, udp, tcp6, udp6
            if not proto.startswith(("tcp", "udp")):
                continue
            # Extract port from "address:port" (typically the 5th field in ss output)
            local_addr = parts[4]
            if ":" in local_addr:
                addr, port = local_addr.rsplit(":", 1)
                # Skip Docker embedded DNS resolver (127.0.0.11) — port changes on every container restart
                if addr == "127.0.0.11":
                    continue
                sockets.add(f"{proto.replace('6', '')}:{port}")  # normalize tcp6 -> tcp
    except subprocess.TimeoutExpired:
        log.warning("ss/nsenter timeout for %s", container_name or "host")
    except (OSError, IOError) as e:
        log.error("Failed to get sockets for %s: %s", container_name or "host", e)

    return sockets


def check_new_listening_sockets(dry_run: bool = False) -> str | None:
    """Alert on new listening sockets vs baseline.

    First run writes baseline silently. Subsequent runs compare.
    """
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    baseline_file = BASELINES_DIR / "listening_sockets.json"

    # Get current listening sockets for host and each docker container
    host_sockets = get_listening_sockets(None)
    container_sockets = {}

    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for container_name in result.stdout.strip().splitlines():
                sockets = get_listening_sockets(container_name)
                # Only record if we got sockets (skip failed containers silently)
                if sockets:
                    container_sockets[container_name] = sockets
    except (subprocess.TimeoutExpired, OSError):
        log.warning("Failed to enumerate docker containers")

    current_baseline = {
        "host": sorted(list(host_sockets)),
        "containers": {c: sorted(list(s)) for c, s in container_sockets.items()},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not baseline_file.exists():
        # First run: write baseline silently
        if not dry_run:
            baseline_file.write_text(json.dumps(current_baseline, indent=2))
        log.info("Socket baseline created (first run)")
        return None

    # Subsequent run: compare
    try:
        prev_baseline = json.loads(baseline_file.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("Failed to read baseline; recreating")
        if not dry_run:
            baseline_file.write_text(json.dumps(current_baseline, indent=2))
        return None

    prev_host = set(prev_baseline.get("host", []))
    prev_containers = prev_baseline.get("containers", {})

    new_host_sockets = host_sockets - prev_host
    all_changes = {}

    if new_host_sockets:
        all_changes["host"] = sorted(list(new_host_sockets))

    for container, sockets in container_sockets.items():
        prev_sockets = set(prev_containers.get(container, []))
        new_sockets = sockets - prev_sockets
        if new_sockets:
            all_changes[f"container:{container}"] = sorted(list(new_sockets))

    if all_changes:
        if not dry_run:
            baseline_file.write_text(json.dumps(current_baseline, indent=2))
        changes_str = "\n".join(
            f"  {ns}: {', '.join(sockets)}"
            for ns, sockets in all_changes.items()
        )
        return alert(
            f"[Intrusion Alert] New listening sockets detected:\n\n"
            f"{changes_str}"
        )

    return None


# ---------------------------------------------------------------------------
# Check 3: Changes to identity files
# ---------------------------------------------------------------------------


def check_identity_file_changes(dry_run: bool = False) -> str | None:
    """Alert on changes to system identity files.

    Files monitored:
    - /etc/passwd
    - /etc/group
    - /etc/shadow (hash only, not plaintext)
    - /etc/sudoers
    - /etc/sudoers.d/* (all files, read with sudo if needed)
    - ~/.ssh/authorized_keys (all users' home dirs, including root with sudo)

    Gracefully skips files that cannot be read (permission denied).
    """
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    baseline_file = BASELINES_DIR / "identity_files.json"

    # Map of filepath -> (hash_function, use_sudo_flag)
    # Most files don't need sudo, but some do
    files_to_check = {
        "/etc/passwd": (sha256_file, False),
        "/etc/group": (sha256_file, False),
        "/etc/shadow": (sha256_shadow_hash, False),  # sha256_shadow_hash handles sudo internally
        "/etc/sudoers": (sha256_file, False),  # Usually readable without sudo; fall back if needed
    }

    # Add /etc/sudoers.d/* files (typically need sudo)
    try:
        sudoers_d = Path("/etc/sudoers.d")
        if sudoers_d.exists():
            for f in sudoers_d.glob("*"):
                if f.is_file():
                    files_to_check[str(f)] = (sha256_file, True)  # Use sudo for sudoers.d files
    except (OSError, IOError):
        pass

    # Add ~/.ssh/authorized_keys for all users with home dirs
    try:
        home_dir = Path("/home")
        if home_dir.exists():
            for home in home_dir.iterdir():
                if home.is_dir():
                    ak = home / ".ssh" / "authorized_keys"
                    if ak.exists():
                        files_to_check[str(ak)] = (sha256_file, False)  # Regular users' keys usually readable
    except (OSError, IOError):
        pass

    # Also check root's authorized_keys (requires sudo)
    files_to_check["/root/.ssh/authorized_keys"] = (sha256_file, True)

    # Compute current hashes, skipping files we can't read
    current_hashes = {}
    for filepath, (hash_fn, use_sudo) in files_to_check.items():
        try:
            if hash_fn == sha256_shadow_hash:
                # sha256_shadow_hash has its own sudo handling
                h = hash_fn(Path(filepath))
            elif use_sudo:
                # Pass use_sudo flag to sha256_file
                h = hash_fn(Path(filepath), use_sudo=use_sudo)
            else:
                h = hash_fn(Path(filepath))
            if h:
                current_hashes[filepath] = h
        except (OSError, IOError) as e:
            log.debug("Skipped unreadable file %s: %s", filepath, e)
            continue

    if not baseline_file.exists():
        # First run: write baseline silently
        if not dry_run:
            baseline_file.write_text(json.dumps(current_hashes, indent=2))
        log.info("Identity file baseline created (first run)")
        return None

    # Subsequent run: compare
    try:
        prev_hashes = json.loads(baseline_file.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("Failed to read baseline; recreating")
        if not dry_run:
            baseline_file.write_text(json.dumps(current_hashes, indent=2))
        return None

    changes = {}
    for filepath, h in current_hashes.items():
        prev_h = prev_hashes.get(filepath)
        if prev_h != h:
            if prev_h is None:
                changes[filepath] = f"NEW: {h[:16]}..."
            else:
                changes[filepath] = f"was {prev_h[:16]}... now {h[:16]}..."

    # Also detect deleted files (were in baseline, now gone)
    deleted = set(prev_hashes.keys()) - set(current_hashes.keys())
    if deleted:
        for f in sorted(deleted):
            changes[f] = "DELETED"

    if changes:
        if not dry_run:
            baseline_file.write_text(json.dumps(current_hashes, indent=2))
        changes_str = "\n".join(
            f"  {filepath}: {change}"
            for filepath, change in sorted(changes.items())
        )
        return alert(
            f"[Intrusion Alert] Changes detected to system identity files:\n\n"
            f"{changes_str}"
        )

    return None


# ---------------------------------------------------------------------------
# Check 4: Kernel oops/bugs in dmesg
# ---------------------------------------------------------------------------


def check_kernel_oops() -> str | None:
    """Alert if new kernel oops/BUG/GPF/KASAN lines appear in dmesg since last check."""
    state = load_state(STATE_FILE)

    # Read dmesg via sudo (dmesg requires root on some systems)
    try:
        result = subprocess.run(
            ["sudo", "dmesg"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log.debug("dmesg command failed (may require sudo): %s", result.stderr.strip()[:100])
            return None
        dmesg_lines = result.stdout.splitlines()
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("Failed to read dmesg: %s", e)
        return None

    # Look for oops/bug/gpf/kasan patterns
    oops_patterns = [
        r"kernel panic",
        r"BUG:",
        r"Oops:",
        r"general protection fault",
        r"\[.*KASAN.*\]",
    ]
    oops_re = re.compile("|".join(oops_patterns), re.IGNORECASE)

    oops_lines = [line for line in dmesg_lines if oops_re.search(line)]

    if oops_lines:
        state["last_checked_dmesg_at"] = datetime.now(timezone.utc).isoformat()
        save_state(STATE_FILE, state)
        oops_str = "\n".join(oops_lines[-10:])  # Last 10 lines to avoid spam
        return alert(
            f"[Intrusion Alert] Kernel oops/bug detected in dmesg:\n\n"
            f"```\n{oops_str}\n```"
        )

    state["last_checked_dmesg_at"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, state)
    return None


# ---------------------------------------------------------------------------
# Check 5: NFT CVE counter
# ---------------------------------------------------------------------------


def get_nft_cve_counter() -> int | None:
    """Get the drop counter for the 'inet cve_2026_98432' nft table.

    Runs: nft list table inet cve_2026_98432
    Parses for "drop" counter and extracts the count.
    Returns None if table doesn't exist or counter unavailable.
    """
    try:
        result = subprocess.run(
            ["sudo", "nft", "list", "table", "inet", "cve_2026_98432"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            # Table doesn't exist or nft access denied — not an alert
            log.debug("nft table cve_2026_98432 not found or access denied")
            return None

        # Look for "counter ... packets 123" pattern
        # nft output example: "counter packets 42 bytes 5120"
        for line in result.stdout.splitlines():
            if "drop" in line.lower() or "counter" in line.lower():
                # Try to extract the number after "packets"
                m = re.search(r"packets (\d+)", line)
                if m:
                    return int(m.group(1))
    except (subprocess.TimeoutExpired, OSError) as e:
        log.error("Failed to query nft counter: %s", e)

    return None


def check_nft_counter() -> str | None:
    """Alert if nft cve_2026_98432 drop counter is > 0 or has increased."""
    state = load_state(STATE_FILE)
    prev_counter = state.get("nft_cve_counter", 0)

    current_counter = get_nft_cve_counter()
    if current_counter is None:
        # Table doesn't exist yet (atlas will create it as part of stopgap)
        return None

    if current_counter > 0 or current_counter > prev_counter:
        state["nft_cve_counter"] = current_counter
        save_state(STATE_FILE, state)
        return alert(
            f"[Intrusion Alert] NFT cve_2026_98432 drop counter active:\n\n"
            f"Previous counter: {prev_counter}\n"
            f"Current counter: {current_counter}\n\n"
            f"This indicates CVE-2026-98432 exploitation attempt(s) blocked by the stopgap nft rule."
        )

    state["nft_cve_counter"] = current_counter
    save_state(STATE_FILE, state)
    return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(dry_run: bool = False) -> int:
    """Run all five checks. Return 0 on success, 2 on error."""
    log.info("Starting intrusion-alarm checks")

    alerts = []

    # Check 1: SSH
    try:
        msg_id = check_unauthorized_ssh_logins()
        if msg_id:
            alerts.append(f"SSH: {msg_id}")
    except Exception as e:
        log.error("SSH check failed: %s", e)
        alerts.append(f"SSH check error: {e}")

    # Check 2: Sockets
    try:
        msg_id = check_new_listening_sockets(dry_run=dry_run)
        if msg_id:
            alerts.append(f"Sockets: {msg_id}")
    except Exception as e:
        log.error("Sockets check failed: %s", e)
        alerts.append(f"Sockets check error: {e}")

    # Check 3: Identity files
    try:
        msg_id = check_identity_file_changes(dry_run=dry_run)
        if msg_id:
            alerts.append(f"Identity: {msg_id}")
    except Exception as e:
        log.error("Identity file check failed: %s", e)
        alerts.append(f"Identity check error: {e}")

    # Check 4: Kernel oops
    try:
        msg_id = check_kernel_oops()
        if msg_id:
            alerts.append(f"Kernel: {msg_id}")
    except Exception as e:
        log.error("Kernel check failed: %s", e)
        alerts.append(f"Kernel check error: {e}")

    # Check 5: NFT counter
    try:
        msg_id = check_nft_counter()
        if msg_id:
            alerts.append(f"NFT: {msg_id}")
    except Exception as e:
        log.error("NFT check failed: %s", e)
        alerts.append(f"NFT check error: {e}")

    if alerts:
        log.info("Alerts sent: %s", ", ".join(alerts))

    log.info("Intrusion-alarm checks completed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't write baselines/state")
    parser.add_argument("--test-alert", action="store_true", help="Send a synthetic TEST alert via direct Bot API (bypass dry-run)")
    args = parser.parse_args()

    if args.test_alert:
        # Send a test alert to verify direct Bot API delivery path
        test_text = (
            "[TEST — direct path] Synthetic intrusion alert to verify immediate delivery.\n\n"
            "If received now, direct Telegram Bot API send is working."
        )
        result = alert(test_text)
        if result == "direct-send":
            log.info("Test alert delivered via direct Telegram Bot API")
            return 0
        else:
            log.warning("Test alert fell back to inbox queue or failed: %s", result)
            return 0

    if not args.dry_run and not is_job_enabled(JOB_NAME):
        log.info("Job disabled in jobs.json — skipping")
        return 0

    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
