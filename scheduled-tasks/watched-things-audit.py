#!/usr/bin/env python3
"""
watched-things-audit.py — Daily self-audit of Lobster's own rows in the
shared monitoring registry.

Context: `/home/lobster/monitoring/watched-things.yaml` is a declarative
registry, jointly maintained by Lobster and glyph (dancetlin-infra), that
says what health checks *should* be running and who owns them. Nothing
enforces that the registry stays truthful as the underlying scripts change
— a `check_*()` function can be renamed/removed, a cron entry can be edited,
a cadence can drift, and the registry row would just silently go stale.

This script audits ONLY the rows Lobster owns (`owner: lobster`). It does
NOT audit `dancetlin-infra` rows — that's glyph's registry and glyph's own
equivalent validator is the right tool for it (see
~/dancetlin-infra/agent-chat/lobster-004.md).

For each Lobster-owned row it checks one of two things depending on which
script the row's `monitor:` field names:

  - `health-check-v3.sh` rows: the row's `id` is mapped (via a fixed alias
    table — the id doesn't textually match the function name in every case,
    e.g. `lobster:wrapper` -> `check_wrapper_process`) to a `check_<name>()`
    function, and we grep the live script to confirm that function still
    exists.
  - All other rows (`oom-monitor.py`, `daily-health-check.sh`,
    `agent-monitor.py`, `inbox-staleness-warn.sh`,
    `archive-log-retention.sh`, `processed-retention.sh`): we confirm the
    monitor script file still exists under $LOBSTER_DIR/scripts/, and — for
    rows whose `cadence` looks like a cron schedule — cross-check that
    cadence against the live crontab entry for that script (matching on the
    script's basename appearing in the crontab line).

Advisory only: this script never modifies the registry, never restarts
anything, and always exits 0 on a clean run (an unexpected internal error
exits 1 purely so cron logs record failure — see main()). On success and no
drift found it is silent (log-file only), matching the
"Silent on success, inbox-writes on failure" convention already used by
daily-health-check.sh. If drift IS found, it writes one batched inbox
message (source=system) so it surfaces to Lobster/Dan the same way other
Type B advisories do — it does not call any MCP tool directly (Type B
scripts run outside an MCP session).

Cron schedule (daily, well after the registry is likely to have settled for
the day):
    0 7 * * * cd ~/lobster && uv run scheduled-tasks/watched-things-audit.py >> ~/lobster-workspace/scheduled-jobs/logs/watched-things-audit.log 2>&1 # LOBSTER-WATCHED-THINGS-AUDIT

Type B dispatch: cron calls this script directly (no inbox message in, no
LLM round-trip). The jobs.json `enabled` gate is checked at the top of
main() so runtime enable/disable is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/watched-things-audit.py [--dry-run]

Related: ~/dancetlin-infra/agent-chat/lobster-004.md,
~/dancetlin-infra/agent-chat/glyph-004.md
"""

# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pyyaml",
# ]
# ///

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.jobs import is_job_enabled  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("watched-things-audit")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "watched-things-audit"

REGISTRY_PATH = Path("/home/lobster/monitoring/watched-things.yaml")

LOBSTER_DIR = _REPO_ROOT
HEALTH_CHECK_V3 = LOBSTER_DIR / "scripts" / "health-check-v3.sh"
SCRIPTS_DIR = LOBSTER_DIR / "scripts"

# Telegram chat_id for the batched advisory (only used if drift is found)
ADMIN_CHAT_ID: int = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

# Inbox dir — respects LOBSTER_MESSAGES env override like other scripts
_MESSAGES_BASE = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
INBOX_DIR = _MESSAGES_BASE / "inbox"

# Fixed id -> check_*() function alias table for health-check-v3.sh rows.
# Not all ids textually match their function 1:1 (e.g. "wrapper" is really
# check_wrapper_process()), so this is an explicit map rather than a
# heuristic — a wrong heuristic match would be worse than no match.
HEALTH_CHECK_V3_FUNCTION_MAP: dict[str, str] = {
    "lobster:wrapper": "check_wrapper_process",
    "lobster:services": "check_services",
    "lobster:tmux": "check_tmux",
    "lobster:claude_process": "check_claude_process",
    "lobster:inbox_drain": "check_inbox_drain",
    "lobster:outbox_drain": "check_outbox_drain",
    "lobster:dispatcher_heartbeat": "check_dispatcher_heartbeat",
    "lobster:memory": "check_memory",
    "lobster:disk": "check_disk",
    "lobster:auth_token": "check_auth_token",
    "lobster:dashboard_server": "check_dashboard_server",
    "lobster:messages_db": "check_messages_db",
    "lobster:memory_capability": "check_memory_capability",
    "lobster:cron_entries": "check_cron_entries",
    "lobster:usage_limit": "check_usage_limit",
    "lobster:session_age": "check_session_age",
}

CRON_FUNC_DEF_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\)\s*\{", re.MULTILINE)


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------


def load_registry(path: Path) -> list[dict[str, Any]]:
    """Load and return the `checks:` list from the registry YAML."""
    data = yaml.safe_load(path.read_text())
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError(f"{path}: no top-level 'checks:' list found")
    return checks


def lobster_rows(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter to rows Lobster owns — self-audit scope only."""
    return [c for c in checks if c.get("owner") == "lobster"]


# ---------------------------------------------------------------------------
# health-check-v3.sh function existence check
# ---------------------------------------------------------------------------


def health_check_v3_functions() -> set[str]:
    """Return the set of check_*() function names defined in health-check-v3.sh."""
    if not HEALTH_CHECK_V3.is_file():
        return set()
    text = HEALTH_CHECK_V3.read_text()
    return {m.group(1) for m in CRON_FUNC_DEF_RE.finditer(text) if m.group(1).startswith("check_")}


def audit_health_check_v3_row(row: dict[str, Any], live_functions: set[str]) -> str | None:
    """Return a drift description, or None if the row checks out."""
    row_id = row.get("id", "<no id>")
    expected_fn = HEALTH_CHECK_V3_FUNCTION_MAP.get(row_id)
    if expected_fn is None:
        return (
            f"{row_id}: no entry in HEALTH_CHECK_V3_FUNCTION_MAP — validator doesn't "
            f"know which check_*() function this row maps to (add it to the alias "
            f"table in watched-things-audit.py)"
        )
    if expected_fn not in live_functions:
        return (
            f"{row_id}: expected function {expected_fn}() not found in "
            f"health-check-v3.sh — row may be stale (function renamed/removed)"
        )
    return None


# ---------------------------------------------------------------------------
# Standalone-script rows (monitor != health-check-v3.sh)
# ---------------------------------------------------------------------------


def get_crontab_text() -> str:
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10, check=False
        )
        return result.stdout or ""
    except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
        log.warning("Could not read crontab: %s", exc)
        return ""


def find_crontab_line(crontab_text: str, script_basename: str) -> str | None:
    for line in crontab_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if script_basename in stripped:
            return stripped
    return None


def cron_schedule_from_line(line: str) -> str | None:
    """Extract the 5-field cron schedule from the start of a crontab line."""
    parts = line.split()
    if len(parts) < 5:
        return None
    return " ".join(parts[:5])


def audit_standalone_row(row: dict[str, Any], crontab_text: str) -> list[str]:
    """Return a list of drift descriptions (possibly empty) for a non-v3 row."""
    drift: list[str] = []
    row_id = row.get("id", "<no id>")
    monitor = row.get("monitor")
    cadence = row.get("cadence")

    if not monitor:
        drift.append(f"{row_id}: no 'monitor' field in registry row")
        return drift

    script_path = SCRIPTS_DIR / monitor
    if not script_path.is_file():
        drift.append(
            f"{row_id}: monitor script '{monitor}' not found at {script_path} "
            f"(registry may reference a moved/renamed/removed script)"
        )
        # Can't cross-check cadence without a live crontab line to find by name,
        # but we can still try — the script may exist elsewhere or the crontab
        # entry may be named differently. Fall through.

    line = find_crontab_line(crontab_text, monitor)
    if line is None:
        drift.append(f"{row_id}: no live crontab entry references '{monitor}'")
        return drift

    live_schedule = cron_schedule_from_line(line)
    if live_schedule is None or not cadence:
        return drift

    cadence = str(cadence)
    if " " in cadence:
        # Full 5-field cadence in the registry — compare exactly.
        if cadence.strip() != live_schedule.strip():
            drift.append(
                f"{row_id}: registry cadence '{cadence}' != live crontab schedule "
                f"'{live_schedule}'"
            )
    else:
        # Registry only records the minute field (e.g. "*/4") — compare that
        # field against the live schedule's minute field.
        live_minute = live_schedule.split()[0]
        if cadence.strip() != live_minute.strip():
            drift.append(
                f"{row_id}: registry cadence '{cadence}' != live crontab minute "
                f"field '{live_minute}' (full live schedule: '{live_schedule}')"
            )

    return drift


# ---------------------------------------------------------------------------
# Inbox write (advisory, only on drift — mirrors inbox-staleness-warn.sh)
# ---------------------------------------------------------------------------


def write_inbox_advisory(drift_lines: list[str], dry_run: bool = False) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    millis = int(time.time() * 1000)
    filename = INBOX_DIR / f"{millis}_watched_things_audit.json"

    body = "watched-things.yaml self-audit found drift in Lobster's own rows:\n\n" + "\n".join(
        f"- {d}" for d in drift_lines
    )

    message = {
        "type": "system",
        "source": "system",
        "chat_id": ADMIN_CHAT_ID,
        "text": body,
        "timestamp": timestamp,
    }

    if dry_run:
        log.info("[dry-run] Would write inbox advisory:\n%s", body)
        return

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    filename.write_text(json.dumps(message, indent=2))
    log.info("Wrote inbox advisory: %s", filename)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_audit(dry_run: bool = False) -> tuple[int, list[str]]:
    """Run the audit. Returns (rows_checked, drift_lines)."""
    if not REGISTRY_PATH.is_file():
        raise FileNotFoundError(f"Registry not found at {REGISTRY_PATH}")

    checks = load_registry(REGISTRY_PATH)
    rows = lobster_rows(checks)
    live_functions = health_check_v3_functions()
    crontab_text = get_crontab_text()

    drift: list[str] = []
    for row in rows:
        monitor = row.get("monitor")
        if monitor == "health-check-v3.sh":
            issue = audit_health_check_v3_row(row, live_functions)
            if issue:
                drift.append(issue)
        else:
            drift.extend(audit_standalone_row(row, crontab_text))

    return len(rows), drift


def main(dry_run: bool = False) -> int:
    if not is_job_enabled(JOB_NAME):
        log.info("%s is disabled in jobs.json — skipping", JOB_NAME)
        return 0

    try:
        rows_checked, drift = run_audit(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        log.error("Audit failed with an unexpected error: %s", exc, exc_info=True)
        return 1

    if not drift:
        log.info(
            "Clean — %d Lobster-owned row(s) in %s all check out against live scripts/crontab",
            rows_checked,
            REGISTRY_PATH,
        )
        return 0

    log.warning(
        "Drift found in %d of %d Lobster-owned row(s):",
        len(drift),
        rows_checked,
    )
    for line in drift:
        log.warning("  - %s", line)

    write_inbox_advisory(drift, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the audit and log what would happen, but don't write an inbox advisory",
    )
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
