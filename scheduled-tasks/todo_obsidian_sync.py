#!/usr/bin/env python3
"""
Obsidian <-> DB Bidirectional Sync

Reads ACTIVE TODOS.md from the obsidian vault and syncs human edits back
into the canonical self_action_items.db, then regenerates the file from DB
and commits it.

SOLE WRITER OF ACTIVE TODOS.md
-------------------------------
This script is the exclusive writer of ACTIVE TODOS.md in the Obsidian vault.
No other script or job (including any future LOS nightly sweep) should call
render_active_todos() or write to this file directly.

Rationale: a race condition arises if any other job regenerates ACTIVE TODOS.md
independently. The sequence that causes data loss:
  1. Dan checks off items in Obsidian on his Mac.
  2. Obsidian-git pushes the vault to the VPS.
  3. A second writer (e.g. LOS nightly at 3am) reads the DB and regenerates
     ACTIVE TODOS.md before this sync job runs — overwriting Dan's checkmarks.
  4. This sync job runs, finds no checkmarks in the file, and never marks those
     items done in the DB.

Keeping this script as the sole writer removes the race: checkmarks are always
read before the file is overwritten, in a single atomic pass.

# ACTIVE TODOS.md is written exclusively by todo_obsidian_sync.py to prevent
# the race condition where a second writer overwrites unsynced Obsidian checkmarks
# before this script can persist them to the DB.

Flow:
  1. git pull the obsidian vault (pull latest edits from Mac)
  2. Parse ACTIVE TODOS.md:
       - [x] items  → mark as done in DB (if currently open)
       - [ ] items not in DB → insert (source = 'obsidian:ACTIVE TODOS.md')
       - [ ] items in DB with mismatched priority band → update priority
  3. Regenerate ACTIVE TODOS.md from DB
  4. git commit + push the vault

All DB writes are idempotent — re-running produces the same state.

Type B (cron-direct) job. See jobs.json entry below.

jobs.json entry:
    {
        "name": "todo-obsidian-sync",
        "type": "B",
        "dispatch": "cron-direct",
        "schedule": "*/30 * * * *",
        "task_file": null,
        "enabled": true
    }

Cron entry:
    */30 * * * * cd ~/lobster && uv run scheduled-tasks/todo_obsidian_sync.py >> ~/lobster-workspace/scheduled-jobs/logs/todo-obsidian-sync.log 2>&1 # LOBSTER-TODO-OBSIDIAN-SYNC

Run standalone (for testing):
    uv run ~/lobster/scheduled-tasks/todo_obsidian_sync.py [--dry-run] [--vault PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TASKS_DIR = Path(__file__).parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(_TASKS_DIR))

# ---------------------------------------------------------------------------
# Shared module imports (obsidian_sync_core.py)
# ---------------------------------------------------------------------------
# All sync/render/git/parsing logic lives in obsidian_sync_core.py.
# This file retains: main(), the jobs.json gate, and CLI argument handling.
# Behavior is unchanged from the original implementation.

from obsidian_sync_core import (  # noqa: E402
    ACTIVE_TODOS_FILENAME,
    OBSIDIAN_SOURCE,
    PRIORITY_URGENT_MAX,
    PRIORITY_ACTIVE_MAX,
    ParsedItem,
    ParsedTodos,
    SyncResult,
    acquire_lock_or_skip,
    git_commit_and_push,
    git_pull,
    parse_active_todos,
    release_lock,
    render_active_todos,
    sync_obsidian_to_db,
)
from src.los.db import connect  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("todo-obsidian-sync")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "todo-obsidian-sync"

_VAULT_DEFAULT = Path.home() / "lobster-workspace" / "obsidian-vault"
_DB_DEFAULT = Path.home() / "lobster-user-config" / "data" / "self_action_items.db"
_LOCK_PATH = Path("/tmp/vault-processor.lock")


def _get_workspace() -> Path:
    """Return the workspace path, reading LOBSTER_WORKSPACE at call time (not import time).

    Deferred to function call so tests can override via monkeypatch.setenv.
    """
    return Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))


# ---------------------------------------------------------------------------
# Jobs.json enabled gate (Type B compliance — must gate before any DB work)
# ---------------------------------------------------------------------------


def _is_job_enabled(job_name: str) -> bool:
    """Return True if the job is enabled in jobs.json."""
    try:
        jobs_file = _get_workspace() / "scheduled-jobs" / "jobs.json"
        with jobs_file.open() as fh:
            data = json.load(fh)
        entry = data.get("jobs", {}).get(job_name, {})
        return bool(entry.get("enabled", True))
    except Exception:
        return True  # Safe default: enabled when unreadable


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Obsidian <-> DB bidirectional sync")
    parser.add_argument("--dry-run", action="store_true", help="Parse and sync to DB but skip vault write and commit")
    parser.add_argument("--vault", default=str(_VAULT_DEFAULT), help="Path to obsidian vault")
    parser.add_argument("--db", default=str(_DB_DEFAULT), help="Path to self_action_items.db")
    args = parser.parse_args()

    if not _is_job_enabled(JOB_NAME):
        log.info("Job '%s' is disabled in jobs.json — exiting", JOB_NAME)
        return

    # Acquire process lock to prevent races with vault-processor.py during the
    # 48-hour validation period when both jobs may be enabled simultaneously.
    # (See design doc Section 11, Q1: "Sole-writer invariant during transition")
    lock_fd = acquire_lock_or_skip(_LOCK_PATH)
    if lock_fd is None:
        log.info("skipping: vault-processor already running (lock held)")
        return

    try:
        vault_path = Path(args.vault)
        db_path = Path(args.db)
        todos_path = vault_path / ACTIVE_TODOS_FILENAME

        # Step 1: git pull vault (must succeed before we write anything)
        pull_ok = True
        if vault_path.exists() and (vault_path / ".git").exists():
            pull_ok = git_pull(vault_path)
        else:
            log.warning("Vault directory not found or not a git repo: %s", vault_path)

        # Step 2: Parse ACTIVE TODOS.md
        if not todos_path.exists():
            log.info("ACTIVE TODOS.md not found at %s — nothing to parse", todos_path)
            content = ""
        else:
            content = todos_path.read_text(encoding="utf-8")
            log.info("Read %d bytes from %s", len(content), todos_path)

        # Step 3: Sync edits to DB
        conn = connect(db_path)
        try:
            if content:
                sync_result = sync_obsidian_to_db(conn, content)
                log.info("Sync complete: %s", sync_result)
            else:
                log.info("Empty content — skipping sync pass")
                sync_result = SyncResult()

            # Step 4: Regenerate ACTIVE TODOS.md from DB
            # Pass last_synced=None to preserve legacy footer format
            new_content = render_active_todos(conn, last_synced=None)

            if args.dry_run:
                log.info("[dry-run] Would write %d chars to %s", len(new_content), todos_path)
                log.info("[dry-run] Preview (first 500 chars):\n%s", new_content[:500])
                return

            # Write the regenerated file
            todos_path.parent.mkdir(parents=True, exist_ok=True)
            todos_path.write_text(new_content, encoding="utf-8")
            log.info("Wrote regenerated ACTIVE TODOS.md (%d chars)", len(new_content))

            # Step 5: Commit and push (only when pull succeeded — avoids pushing
            # on top of a failed rebase and creating a diverged history)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if vault_path.exists() and (vault_path / ".git").exists():
                if not pull_ok:
                    log.warning("Skipping commit/push because git pull failed")
                else:
                    commit_message = f"todos: sync ACTIVE TODOS.md [{timestamp}]"
                    committed = git_commit_and_push(vault_path, [todos_path], commit_message)
                    if committed:
                        log.info("Vault updated and committed")
                    else:
                        log.info("No vault changes to commit")

        finally:
            conn.close()

        log.info(
            "todo-obsidian-sync complete: done=%d inserted=%d priority_changed=%d",
            sync_result.done_count,
            sync_result.inserted_count,
            sync_result.priority_changed_count,
        )

    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
