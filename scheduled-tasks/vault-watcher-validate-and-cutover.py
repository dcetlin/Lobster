#!/usr/bin/env python3
"""
vault-watcher-validate-and-cutover.py — One-shot 48h validation check.

Run once at 2026-05-13T03:00Z (48h after vault-watcher was deployed).
Checks vault-watcher.log for error-level entries in the past 48 hours.
If clean: sets todo-obsidian-sync enabled=false in jobs.json and sends
a Telegram notification.
If errors found: sends a Telegram notification listing the issues and
leaves todo-obsidian-sync enabled.

This script is invoked directly by a one-shot cron entry:
    0 3 13 5 * cd ~/lobster && uv run scheduled-tasks/vault-watcher-validate-and-cutover.py >> ~/lobster-workspace/scheduled-jobs/logs/vault-watcher-validate-and-cutover.log 2>&1 # LOBSTER-VAULT-WATCHER-CUTOVER

After it runs, remove the cron entry manually (or it will re-run on the same
day/hour in future years — unlikely to matter but safe to clean up).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("vault-watcher-cutover")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
JOBS_FILE = WORKSPACE / "scheduled-jobs" / "jobs.json"
VAULT_WATCHER_LOG = WORKSPACE / "scheduled-jobs" / "logs" / "vault-watcher.log"
LOBSTER_CHAT_ID = 8075091586

# ---------------------------------------------------------------------------
# Telegram notification via MCP HTTP (fallback: skip notification on error)
# ---------------------------------------------------------------------------

def _send_telegram(text: str) -> None:
    """Send a Telegram message via the lobster-inbox MCP server."""
    try:
        import urllib.request
        mcp_port = os.environ.get("LOBSTER_MCP_PORT", "8765")
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "send_reply",
                "arguments": {
                    "chat_id": LOBSTER_CHAT_ID,
                    "text": text,
                    "source": "telegram",
                },
            },
        }).encode()
        req = urllib.request.Request(
            f"http://localhost:{mcp_port}/mcp",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("Telegram notification sent, status=%s", resp.status)
    except Exception as exc:
        log.warning("Failed to send Telegram notification: %s", exc)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _check_vault_watcher_log_for_errors(since: datetime) -> list[str]:
    """
    Scan vault-watcher.log for ERROR or CRITICAL lines since `since`.

    Returns a list of matching lines (empty = clean).
    """
    if not VAULT_WATCHER_LOG.exists():
        log.warning("vault-watcher.log not found at %s", VAULT_WATCHER_LOG)
        return [f"[validation] vault-watcher.log not found at {VAULT_WATCHER_LOG}"]

    # Log lines have the format: 2026-05-11T03:08:00Z LEVEL [logger] message
    # We match the timestamp prefix and ERROR/CRITICAL level.
    ts_pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(ERROR|CRITICAL)\s+",
    )

    errors: list[str] = []
    try:
        with VAULT_WATCHER_LOG.open() as fh:
            for line in fh:
                m = ts_pattern.match(line)
                if not m:
                    continue
                try:
                    line_ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if line_ts >= since:
                    errors.append(line.rstrip())
    except OSError as exc:
        log.error("Could not read vault-watcher.log: %s", exc)
        return [f"[validation] Could not read log: {exc}"]

    return errors


def _disable_todo_obsidian_sync() -> bool:
    """
    Set todo-obsidian-sync enabled=false in jobs.json.
    Returns True on success.
    """
    try:
        with JOBS_FILE.open() as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read jobs.json: %s", exc)
        return False

    jobs = data.get("jobs", data)  # support both {"jobs": {...}} and flat dict
    if "todo-obsidian-sync" not in jobs:
        log.info("todo-obsidian-sync not found in jobs.json — already retired, skipping")
        return True

    jobs["todo-obsidian-sync"]["enabled"] = False

    # Preserve structure: if original data had "jobs" key, keep it
    if "jobs" in data:
        data["jobs"] = jobs
    else:
        data = jobs

    try:
        with JOBS_FILE.open("w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        log.info("todo-obsidian-sync disabled in jobs.json")
        return True
    except OSError as exc:
        log.error("Could not write jobs.json: %s", exc)
        return False


def main() -> None:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=48)
    log.info("vault-watcher 48h cutover check starting (checking since %s)", since.isoformat())

    errors = _check_vault_watcher_log_for_errors(since)

    if errors:
        error_summary = "\n".join(errors[:10])
        if len(errors) > 10:
            error_summary += f"\n... and {len(errors) - 10} more"
        msg = (
            f"Vault-watcher 48h validation: ERRORS FOUND ({len(errors)} error lines).\n"
            f"todo-obsidian-sync NOT disabled — manual review needed.\n\n"
            f"Sample errors:\n{error_summary}"
        )
        log.error("Validation failed: %d errors in vault-watcher.log", len(errors))
        _send_telegram(msg)
        sys.exit(1)
    else:
        log.info("No errors found in vault-watcher.log in past 48h — disabling todo-obsidian-sync")
        ok = _disable_todo_obsidian_sync()
        if ok:
            msg = (
                "Vault-watcher 48h validation: clean. "
                "todo-obsidian-sync has been disabled (replaced by vault-watcher)."
            )
        else:
            msg = (
                "Vault-watcher 48h validation: log clean, but failed to update jobs.json. "
                "Please disable todo-obsidian-sync manually."
            )
        _send_telegram(msg)
        log.info("Done: %s", msg)


if __name__ == "__main__":
    main()
