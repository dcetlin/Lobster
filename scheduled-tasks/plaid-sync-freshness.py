#!/usr/bin/env python3
"""
Plaid sync freshness poller for the ledger app.

Queries ledger_db for max(last_synced) across plaid_items. If the newest
sync is older than the staleness threshold, alerts Dan — a stale Plaid sync
usually means the sync job inside ledger_api silently stopped working.

Cron schedule (hourly):
    0 * * * * cd ~/lobster && uv run scheduled-tasks/plaid-sync-freshness.py >> ~/lobster-workspace/scheduled-jobs/logs/plaid-sync-freshness.log 2>&1 # LOBSTER-PLAID-SYNC-FRESHNESS

Type B dispatch: cron calls this script directly. The jobs.json enabled gate
is checked at the top of main().

Alerting is transition-based: alert when crossing into "stale", send a
recovery notice when crossing back to "fresh", stay silent for unchanged
state so this doesn't nag hourly while a known issue is being fixed.

Run standalone:
    uv run ~/lobster/scheduled-tasks/plaid-sync-freshness.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
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
log = logging.getLogger("plaid-sync-freshness")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JOB_NAME = "plaid-sync-freshness"
CONTAINER = "ledger_db"
DB_NAME = "ledger"
DB_USER = "postgres"
STALE_THRESHOLD_HOURS = 7
STATE_FILE = Path.home() / "lobster-workspace" / "data" / "plaid-sync-freshness-state.json"
ADMIN_CHAT_ID = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_max_last_synced(raw_output: str) -> datetime | None:
    """Parse the raw psql -t -A output of `select max(last_synced) ...`.

    Returns None if there are no plaid_items rows (NULL) or output is empty.
    """
    value = raw_output.strip()
    if not value:
        return None
    # psql prints timestamptz like: 2026-11-01 12:34:56.789012+00
    normalized = value.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        log.warning("Could not parse timestamp from psql output: %r", value)
        return None


def age_hours(last_synced: datetime, now: datetime) -> float:
    if last_synced.tzinfo is None:
        last_synced = last_synced.replace(tzinfo=timezone.utc)
    return (now - last_synced).total_seconds() / 3600.0


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {"is_stale": False}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"is_stale": False}


def save_state(state_file: Path, is_stale: bool, last_synced_iso: str | None) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "is_stale": is_stale,
        "last_synced": last_synced_iso,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(state_file)


# ---------------------------------------------------------------------------
# Side-effecting boundary functions
# ---------------------------------------------------------------------------


def query_max_last_synced(container: str, db_user: str, db_name: str) -> tuple[str | None, str | None]:
    """Returns (raw_stdout, error). Uses -t -A for a bare scalar value."""
    cmd = [
        "docker", "exec", container,
        "psql", "-U", db_user, "-d", db_name,
        "-t", "-A", "-c", "select max(last_synced) from plaid_items",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None, "psql query timed out after 30s"
    except OSError as exc:
        return None, f"OS error: {exc}"
    if result.returncode != 0:
        return None, f"psql exit {result.returncode}: {result.stderr.strip()}"
    return result.stdout, None


def alert(text: str) -> None:
    write_inbox_message(JOB_NAME, ADMIN_CHAT_ID, text, datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(dry_run: bool) -> int:
    now = datetime.now(timezone.utc)
    raw, err = query_max_last_synced(CONTAINER, DB_USER, DB_NAME)
    if err:
        log.error("Query failed: %s", err)
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    last_synced = parse_max_last_synced(raw)
    if last_synced is None:
        msg = "plaid_items has no rows (or last_synced is NULL for all) — nothing to assess"
        log.info(msg)
        print(msg)
        return 0

    hours_old = age_hours(last_synced, now)
    is_stale = hours_old > STALE_THRESHOLD_HOURS

    summary = f"Newest Plaid sync: {last_synced.isoformat()} ({hours_old:.1f}h ago). Threshold: {STALE_THRESHOLD_HOURS}h. Stale={is_stale}"
    log.info(summary)
    print(summary)

    if dry_run:
        return 1 if is_stale else 0

    prev_state = load_state(STATE_FILE)
    was_stale = prev_state.get("is_stale", False)

    if is_stale and not was_stale:
        alert(
            f"[ALERT] Plaid sync is stale: newest sync was {hours_old:.1f}h ago "
            f"(threshold {STALE_THRESHOLD_HOURS}h), at {last_synced.isoformat()}. "
            f"The ledger_api sync job may have stopped working."
        )
        log.info("Alert sent: transitioned to stale")
    elif was_stale and not is_stale:
        alert(
            f"[RECOVERY] Plaid sync is fresh again: newest sync {hours_old:.1f}h ago "
            f"({last_synced.isoformat()})."
        )
        log.info("Alert sent: recovered from stale")

    save_state(STATE_FILE, is_stale, last_synced.isoformat())
    return 1 if is_stale else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't write state or alert")
    args = parser.parse_args()

    if not is_job_enabled(JOB_NAME):
        log.info("Job disabled in jobs.json — skipping")
        return 0

    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
