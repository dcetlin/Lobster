#!/usr/bin/env python3
"""
Unauthenticated-route probe.

Generic cron-direct job: for each configured public Caddy hostname, GET an
allowlist of API paths with NO credentials attached, and alert if any path
that is supposed to require auth (expect_401) instead returns a 2xx with a
body. This catches the class of bug where an API route accidentally skips
its auth middleware and is reachable by anyone on the internet.

The host -> allowlist map lives in a small JSON config (NOT hardcoded in this
script) so adding hosts/paths later is a config change, not a code change.
Default config path: ~/lobster-workspace/data/unauth-route-probe-config.json
(seeded for this instance by scripts/user-update.sh, since the host list is
instance-specific). Override with UNAUTH_PROBE_CONFIG env var.

Config schema:
{
  "hosts": {
    "<hostname>": {
      "expect_401": ["/api/path", ...],   // probed; 2xx-with-body => violation
      "expect_200": ["/", ...]            // documented only, NOT probed/alerted
    }
  }
}

Alerting is transition-based (like dancetlin-infra/health-check.sh): a state
file holds the current violation set. New violations alert immediately; an
all-clear after a prior violation sends a recovery notice; an unchanged
violation set stays silent (no repeat spam every 15 minutes).

Cron schedule (every 15 minutes):
    */15 * * * * cd ~/lobster && uv run scheduled-tasks/unauth-route-probe.py >> ~/lobster-workspace/scheduled-jobs/logs/unauth-route-probe.log 2>&1 # LOBSTER-UNAUTH-ROUTE-PROBE

Type B dispatch: cron calls this script directly. The jobs.json enabled gate
is checked at the top of main().

Run standalone:
    uv run ~/lobster/scheduled-tasks/unauth-route-probe.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

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
log = logging.getLogger("unauth-route-probe")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JOB_NAME = "unauth-route-probe"
DEFAULT_CONFIG_PATH = Path.home() / "lobster-workspace" / "data" / "unauth-route-probe-config.json"
STATE_FILE = Path.home() / "lobster-workspace" / "data" / "unauth-route-probe-state.json"
ADMIN_CHAT_ID = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
REQUEST_TIMEOUT_S = 10


@dataclass(frozen=True)
class Violation:
    host: str
    path: str
    status_code: int
    body_len: int

    @property
    def key(self) -> str:
        return f"{self.host}{self.path}"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"No probe config at {config_path}")
    return json.loads(config_path.read_text()).get("hosts", {})


def classify_response(host: str, path: str, status_code: int, body: bytes) -> Violation | None:
    """Pure classification: a 2xx response with a non-empty body on an
    expect_401 path is a violation. Anything else (401/403/404/5xx/empty
    2xx/network error already filtered out) is fine."""
    if 200 <= status_code < 300 and len(body) > 0:
        return Violation(host=host, path=path, status_code=status_code, body_len=len(body))
    return None


def load_state(state_file: Path) -> set[str]:
    if not state_file.exists():
        return set()
    try:
        return set(json.loads(state_file.read_text()).get("violation_keys", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_state(state_file: Path, violation_keys: set[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "violation_keys": sorted(violation_keys),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(state_file)


def format_alert(new_violations: list[Violation], cleared: list[str]) -> str:
    lines = []
    if new_violations:
        lines.append("[SECURITY ALERT] Unauthenticated route probe found exposed endpoint(s):")
        for v in new_violations:
            lines.append(f"  - https://{v.host}{v.path} -> HTTP {v.status_code}, body {v.body_len} bytes (NO credentials sent)")
        lines.append("")
        lines.append("These paths are expected to require auth (401) but returned a 2xx body to an anonymous request. Investigate immediately.")
    if cleared:
        lines.append("[RECOVERY] Previously-alerted unauth-route violation(s) are now clear:")
        for k in cleared:
            lines.append(f"  - {k}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Side-effecting boundary functions
# ---------------------------------------------------------------------------


def probe_path(host: str, path: str, timeout: int = REQUEST_TIMEOUT_S) -> tuple[int | None, bytes, str | None]:
    """GET https://{host}{path} with no credentials. Returns (status_code, body, error)."""
    url = f"https://{host}{path}"
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "lobster-unauth-route-probe/1.0"})
        return resp.status_code, resp.content, None
    except requests.RequestException as exc:
        return None, b"", str(exc)


def alert(text: str) -> None:
    write_inbox_message(JOB_NAME, ADMIN_CHAT_ID, text, datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(config_path: Path, state_file: Path, dry_run: bool, verbose: bool) -> int:
    hosts_config = load_config(config_path)
    all_violations: list[Violation] = []
    network_errors: list[str] = []

    for host, allowlist in hosts_config.items():
        for path in allowlist.get("expect_401", []):
            status_code, body, err = probe_path(host, path)
            if err is not None:
                network_errors.append(f"{host}{path}: {err}")
                if verbose:
                    log.warning("Network error probing %s%s: %s", host, path, err)
                continue
            if verbose:
                log.info("%s%s -> %s (%d bytes)", host, path, status_code, len(body))
            v = classify_response(host, path, status_code, body)
            if v:
                all_violations.append(v)

    current_keys = {v.key for v in all_violations}
    previous_keys = load_state(state_file)

    new_violations = [v for v in all_violations if v.key not in previous_keys]
    cleared = sorted(previous_keys - current_keys)

    if dry_run:
        print(f"DRY RUN: {len(all_violations)} violation(s) found, {len(network_errors)} network error(s)")
        for v in all_violations:
            print(f"  VIOLATION: {v.host}{v.path} -> {v.status_code} ({v.body_len} bytes)")
        for e in network_errors:
            print(f"  NETWORK ERROR: {e}")
        return 1 if all_violations else 0

    if new_violations or cleared:
        alert_text = format_alert(new_violations, cleared)
        alert(alert_text)
        log.info("Alert sent: %d new violation(s), %d cleared", len(new_violations), len(cleared))

    save_state(state_file, current_keys)

    if network_errors:
        log.warning("%d network error(s) during probe (not alerted — treated as transient): %s", len(network_errors), network_errors)

    summary = f"Probed {sum(len(v.get('expect_401', [])) for v in hosts_config.values())} path(s) across {len(hosts_config)} host(s): {len(all_violations)} violation(s), {len(network_errors)} network error(s)"
    log.info(summary)
    print(summary)

    return 1 if all_violations else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Probe and report but don't write state or alert")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not is_job_enabled(JOB_NAME):
        log.info("Job disabled in jobs.json — skipping")
        return 0

    try:
        return run(args.config, args.state_file, args.dry_run, args.verbose)
    except FileNotFoundError as exc:
        log.error(str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
