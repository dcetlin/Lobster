#!/usr/bin/env python3
"""
vault-processor.py — Vault execution and mutation.

Execution and mutation ONLY. This script does not poll, debounce, or manage
state files. Its sole job: pull the vault, run guards, dispatch @lobster
annotations, sync DB, render ACTIVE TODOS.md, commit, and push.

Invoked by vault-watcher.py when the debounce threshold fires.
Also invokable manually for debugging (without simulating a push event):
    uv run ~/lobster/scheduled-tasks/vault-processor.py [--config PATH]

Process lockfile: /tmp/vault-processor.lock
  Vault-watcher.py holds the lock across the full processor run (invoked
  synchronously). When invoked directly (manual/debug), this script acquires
  the lock itself via acquire_lock_or_skip().

jobs.json entry:
  vault-processor is NOT a cron entry — invoked only by vault-watcher.py.
  No jobs.json entry required (vault-watcher.py is the Type B cron job).
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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import from shared module (same directory)
_TASKS_DIR = Path(__file__).parent
if str(_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(_TASKS_DIR))

from obsidian_sync_core import (  # noqa: E402
    ACTIVE_TODOS_FILENAME,
    acquire_lock_or_skip,
    git_commit_and_push,
    git_pull,
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
log = logging.getLogger("vault-processor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_USER_CONFIG = Path(os.environ.get("LOBSTER_USER_CONFIG", Path.home() / "lobster-user-config"))

CONFIG_PATH_DEFAULT = _USER_CONFIG / "data" / "vault-watch-config.json"
DB_PATH_DEFAULT = _USER_CONFIG / "data" / "self_action_items.db"
INBOX_DIR = Path(os.environ.get("LOBSTER_INBOX_DIR", Path.home() / "messages" / "inbox"))
LOCK_PATH = Path("/tmp/vault-processor.lock")

# DISABLE PROCESSING guard
DISABLE_PROCESSING_LINES_TO_CHECK = 10  # Only scan first 10 lines
DISABLE_PROCESSING_TEXT = "DISABLE PROCESSING"
DISABLE_PROCESSING_UNCHECKED_RE = re.compile(
    r"^- \[ \]\s+🔒\s+DISABLE PROCESSING\s*$", re.IGNORECASE
)
DISABLE_PROCESSING_CHECKED_RE = re.compile(
    r"^- \[[xX]\]\s+🔒\s+DISABLE PROCESSING\s*$", re.IGNORECASE
)

# Conflict markers
CONFLICT_MARKER = "<<<<<<< HEAD"
CONFLICT_CHECK_LINES = 50

# @lobster annotation
LOBSTER_ANNOTATION_RE = re.compile(r"(?i)@lobster\s+(.+?)(?:\s*<!--dispatched_at:[^>]*-->)?\s*$")
DISPATCHED_AT_RE = re.compile(r"\s*<!--\s*dispatched_at:[^>]*-->")

# Files to exclude from annotation scan
ANNOTATION_SCAN_EXCLUDES = {".git", ".obsidian"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class LobsterAnnotation:
    """A parsed @lobster annotation from a vault file."""
    command_text: str
    line_number: int       # 0-indexed
    original_line: str
    file_path: Path        # absolute path


# ---------------------------------------------------------------------------
# DISABLE PROCESSING guard
# ---------------------------------------------------------------------------


def check_disable_processing_guard(content: str) -> tuple[bool, bool]:
    """Check ACTIVE TODOS.md for the DISABLE PROCESSING guard line.

    Returns (guard_found: bool, is_disabled: bool).
      - guard_found=False: guard line absent (State 3 — alert Dan, skip, do NOT modify file)
      - guard_found=True, is_disabled=False: guard present and unchecked (State 1 — proceed)
      - guard_found=True, is_disabled=True: guard present and checked (State 2 — skip + alert)

    Only reads the first DISABLE_PROCESSING_LINES_TO_CHECK lines.
    """
    lines = content.splitlines()[:DISABLE_PROCESSING_LINES_TO_CHECK]
    for line in lines:
        if DISABLE_PROCESSING_CHECKED_RE.match(line):
            return True, True   # State 2: disabled
        if DISABLE_PROCESSING_UNCHECKED_RE.match(line):
            return True, False  # State 1: enabled
        # Also match if it contains the text but without exact checkbox format
        if DISABLE_PROCESSING_TEXT in line.upper():
            # Determine checked state
            is_checked = bool(re.search(r"\[[xX]\]", line))
            return True, is_checked
    return False, False  # State 3: guard absent


# ---------------------------------------------------------------------------
# Conflict marker check
# ---------------------------------------------------------------------------


def has_conflict_markers(content: str) -> bool:
    """Return True if ACTIVE TODOS.md contains git conflict markers in the first N lines."""
    lines = content.splitlines()[:CONFLICT_CHECK_LINES]
    return any(CONFLICT_MARKER in line for line in lines)


# ---------------------------------------------------------------------------
# @lobster annotation parser
# ---------------------------------------------------------------------------


def parse_lobster_annotations(content: str, file_path: Path) -> list[LobsterAnnotation]:
    """Parse @lobster annotations from file content.

    Pure function — no I/O or side effects.

    Returns list of LobsterAnnotation ordered by line number.
    Only processes annotations without an existing dispatched_at marker.
    A single line can have at most one @lobster annotation processed;
    if a line has already been marked with dispatched_at, it is skipped.
    """
    results = []
    for line_no, line in enumerate(content.splitlines()):
        # Skip if line has a dispatched_at marker (already processed)
        if "dispatched_at:" in line:
            continue

        match = re.search(r"(?i)@lobster\s+(.+?)$", line.strip())
        if match:
            command_text = match.group(1).strip()
            if command_text:
                results.append(LobsterAnnotation(
                    command_text=command_text,
                    line_number=line_no,
                    original_line=line,
                    file_path=file_path,
                ))
    return results


# ---------------------------------------------------------------------------
# Annotation dispatch
# ---------------------------------------------------------------------------


def _annotation_message_id(command_text: str, file_path: Path, original_line: str) -> str:
    """Compute a deterministic content-hash message_id for deduplication."""
    content = f"{command_text}|{file_path}|{original_line.strip()}"
    sha = hashlib.sha256(content.encode()).hexdigest()[:16]
    return f"vault-annotation-{sha}"


def _dispatch_annotation(
    annotation: LobsterAnnotation,
    vault_path: Path,
    chat_id: int,
    inbox_dir: Path = INBOX_DIR,
) -> bool:
    """Write annotation to inbox as a user_message JSON.

    Returns True on success, False on failure.
    On failure, the annotation is left in the file (will retry next cycle).
    """
    vault_relative = annotation.file_path.relative_to(vault_path)
    message_id = _annotation_message_id(
        annotation.command_text, vault_relative, annotation.original_line
    )
    timestamp = datetime.now(timezone.utc).isoformat()
    file_slug = re.sub(r"[^a-z0-9]+", "-", str(vault_relative).lower())[:40]
    filename = (
        f"vault-lobster-annotation-{int(time.time() * 1000)}"
        f"-{file_slug}-{annotation.line_number}.json"
    )

    payload = {
        "type": "user_message",
        "source": "telegram",
        "chat_id": chat_id,
        "message_id": message_id,
        "text": annotation.command_text,
        "timestamp": timestamp,
        "metadata": {
            "origin": "vault_watcher",
            "vault_file": str(vault_relative),
            "line_number": annotation.line_number,
            "original_line": annotation.original_line.strip(),
        },
    }

    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        (inbox_dir / filename).write_text(json.dumps(payload, indent=2))
        log.info(
            "Dispatched @lobster annotation: %r (message_id=%s)",
            annotation.command_text[:60],
            message_id,
        )
        return True
    except OSError as e:
        log.error(
            "Failed to write inbox JSON for annotation %r: %s",
            annotation.command_text[:60],
            e,
        )
        return False


def _mark_annotation_dispatched(line: str) -> str:
    """Append a dispatched_at HTML comment to the annotation line (before cleanup)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return line.rstrip() + f" <!-- dispatched_at: {ts} -->"


def _remove_annotation_from_line(line: str) -> Optional[str]:
    """Remove the @lobster annotation from a line.

    If the annotation is the entire line (only whitespace + @lobster ...), return None
    (caller should delete the line entirely).
    If it's a trailing suffix, strip the @lobster part and return the cleaned line.
    """
    stripped = line.strip()
    # Check if annotation is the entire line
    if re.match(r"^@lobster\s+.+$", stripped, re.IGNORECASE):
        return None  # Delete the whole line

    # Trailing annotation: strip from @lobster onward (including dispatched_at)
    cleaned = re.sub(r"\s*@lobster\s+.+?(?:\s*<!--\s*dispatched_at:[^>]*-->)?\s*$", "", line, flags=re.IGNORECASE)
    return cleaned if cleaned.strip() else None


def process_annotations_in_file(
    file_path: Path,
    vault_path: Path,
    chat_id: int,
    inbox_dir: Path = INBOX_DIR,
) -> tuple[str, list[Path]]:
    """Process all @lobster annotations in a file.

    For each annotation:
    1. Mark inline with dispatched_at comment
    2. Write inbox JSON (dedup gate)
    3. Remove annotation from line

    Returns (modified_content, [file_path_if_changed]).
    On dispatch failure for an annotation, that annotation is left in the file.
    """
    content = file_path.read_text(encoding="utf-8")
    annotations = parse_lobster_annotations(content, file_path)

    if not annotations:
        return content, []

    lines = content.splitlines(keepends=True)
    changed = False

    for annotation in annotations:
        ln = annotation.line_number
        if ln >= len(lines):
            continue

        # Step 1: Mark as dispatched_at (inline marker before write)
        marked_line = _mark_annotation_dispatched(lines[ln].rstrip("\n"))
        lines[ln] = marked_line + ("\n" if lines[ln].endswith("\n") else "")

        # Step 2: Write inbox JSON
        success = _dispatch_annotation(annotation, vault_path, chat_id, inbox_dir)
        if not success:
            # Revert the dispatched_at marker (annotation stays for retry)
            lines[ln] = annotation.original_line
            continue

        # Step 3: Remove annotation from line (cleanup)
        cleaned = _remove_annotation_from_line(annotation.original_line)
        if cleaned is None:
            # Delete the whole line
            lines[ln] = ""
        else:
            lines[ln] = cleaned + ("\n" if annotation.original_line.endswith("\n") else "")

        changed = True

    if not changed:
        return content, []

    new_content = "".join(line for line in lines if line != "")
    file_path.write_text(new_content, encoding="utf-8")
    return new_content, [file_path]


# ---------------------------------------------------------------------------
# Annotation scope scanning
# ---------------------------------------------------------------------------


def _collect_files_to_scan(vault_path: Path, annotation_scope: str, watched_files: list[str]) -> list[Path]:
    """Return list of .md files to scan for @lobster annotations."""
    if annotation_scope == "watched_only":
        return [vault_path / f for f in watched_files if (vault_path / f).exists()]

    # "all": scan entire vault tree, excluding .git and .obsidian
    result = []
    for md_file in vault_path.rglob("*.md"):
        parts = set(md_file.relative_to(vault_path).parts)
        if parts & ANNOTATION_SCAN_EXCLUDES:
            continue
        result.append(md_file)
    return sorted(result)


# ---------------------------------------------------------------------------
# Telegram alert helper
# ---------------------------------------------------------------------------


def _send_telegram_alert(chat_id: int, text: str) -> None:
    """Write a Telegram alert to the inbox for the dispatcher to deliver.

    Uses the same inbox pattern as annotation dispatch.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    message_id = f"vault-alert-{int(time.time() * 1000)}"
    filename = f"vault-processor-alert-{int(time.time() * 1000)}.json"

    payload = {
        "type": "user_message",
        "source": "telegram",
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "timestamp": timestamp,
        "metadata": {
            "origin": "vault_processor_alert",
        },
    }

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (INBOX_DIR / filename).write_text(json.dumps(payload, indent=2))
    except OSError as e:
        log.error("Failed to write Telegram alert to inbox: %s", e)


# ---------------------------------------------------------------------------
# Config loading (for direct invocation)
# ---------------------------------------------------------------------------


def _load_config(config_path: Path) -> dict[str, Any]:
    """Load vault-watch-config.json (minimal validation for processor use)."""
    if not config_path.exists():
        log.warning("Config file not found at %s — using empty config", config_path)
        return {}
    try:
        with config_path.open() as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        log.error("Config file malformed JSON: %s", e)
        raise


# ---------------------------------------------------------------------------
# Main processor logic
# ---------------------------------------------------------------------------


def run_processor(config: dict, db_path: Path = DB_PATH_DEFAULT) -> bool:
    """Execute the full vault-processor pipeline.

    Steps (strict order — each step assumes previous succeeded):
    1. git pull --rebase --autostash
    2. Read ACTIVE TODOS.md
    3. Check conflict markers (first 50 lines) — alert + skip if found
    4. Check DISABLE PROCESSING guard — skip (with alert) if disabled/missing
    5. Scan watched files for @lobster annotations — mark, dispatch, remove
    6. git add annotation-cleared files (before sync)
    7. sync_obsidian_to_db()
    8. render_active_todos() with current timestamp
    9. git add ACTIVE TODOS.md
    10. git commit + push
    11. Log completion

    Returns True on success, False if processing was skipped (guard active, etc.)
    """
    vault_path = Path(config.get("vault_path", "")).expanduser()
    chat_id: Optional[int] = config.get("lobster_chat_id")
    watched_files: list[str] = config.get("watched_files", ["✅ ACTIVE TODOS.md"])
    annotation_scope: str = config.get("annotation_scope", "all")

    if not vault_path.exists():
        log.error("Vault path does not exist: %s", vault_path)
        return False

    todos_path = vault_path / ACTIVE_TODOS_FILENAME

    # Step 1: git pull --rebase --autostash
    log.info("Step 1: git pull")
    pull_ok = git_pull(vault_path)
    if not pull_ok:
        log.error("git pull failed — aborting processor this cycle")
        return False

    # Step 2: Read ACTIVE TODOS.md
    log.info("Step 2: reading ACTIVE TODOS.md")
    if not todos_path.exists():
        log.warning("ACTIVE TODOS.md not found — proceeding with annotation scan only")
        todos_content = ""
    else:
        try:
            todos_content = todos_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            log.error("UnicodeDecodeError reading ACTIVE TODOS.md: %s", e)
            return False

    # Step 3: Conflict markers pre-check
    log.info("Step 3: conflict markers check")
    if todos_content and has_conflict_markers(todos_content):
        log.error("Git conflict markers detected in ACTIVE TODOS.md — processing paused")
        if chat_id:
            _send_telegram_alert(
                chat_id,
                "vault-watcher: git conflict markers detected in ACTIVE TODOS.md — "
                "processing paused. Resolve the conflict manually to resume.",
            )
        return False  # do NOT advance last_processed_head

    # Step 4: DISABLE PROCESSING guard
    log.info("Step 4: DISABLE PROCESSING guard check")
    if todos_content:
        guard_found, is_disabled = check_disable_processing_guard(todos_content)
        if not guard_found:
            # State 3: guard missing
            log.warning("DISABLE PROCESSING guard not found — processing skipped this cycle. Alert sent to Dan.")
            if chat_id:
                _send_telegram_alert(
                    chat_id,
                    "vault-watcher: DISABLE PROCESSING guard not found in ACTIVE TODOS.md — "
                    "processing paused. Restore the guard line (- [ ] 🔒 DISABLE PROCESSING) "
                    "within the first 10 lines to resume.",
                )
            return True  # Advance last_processed_head to avoid infinite retry

        if is_disabled:
            # State 2: guard checked
            log.info("DISABLE PROCESSING guard is active — skipping processor this cycle")
            if chat_id:
                _send_telegram_alert(
                    chat_id,
                    "DISABLE PROCESSING is active in ACTIVE TODOS.md — skipping this sync cycle. "
                    "Uncheck the guard line to resume.",
                )
            return True  # Advance last_processed_head

    # State 1: guard present and unchecked — proceed normally

    # Step 5: Scan for @lobster annotations + dispatch
    log.info("Step 5: @lobster annotation scan")
    files_to_scan = _collect_files_to_scan(vault_path, annotation_scope, watched_files)
    staging_files: list[Path] = []

    for scan_file in files_to_scan:
        if not scan_file.exists():
            continue
        try:
            _content, modified = process_annotations_in_file(
                scan_file, vault_path, chat_id or 0, INBOX_DIR
            )
            staging_files.extend(modified)
        except Exception as e:
            log.error("Error processing annotations in %s: %s", scan_file, e)

    # Step 6: git add annotation-cleared files
    if staging_files:
        log.info("Step 6: git add %d annotation-cleared files", len(staging_files))
        for f in staging_files:
            subprocess.run(
                ["git", "add", str(f)],
                cwd=str(vault_path),
                capture_output=True,
                text=True,
            )
        # Re-read todos_content in case it was modified by annotation cleanup
        if todos_path.exists():
            todos_content = todos_path.read_text(encoding="utf-8")
    else:
        log.info("Step 6: no annotation files to stage")

    # Step 7: sync_obsidian_to_db()
    log.info("Step 7: sync_obsidian_to_db")
    conn = connect(db_path)
    try:
        if todos_content:
            sync_result = sync_obsidian_to_db(conn, todos_content)
            log.info("Sync complete: %s", sync_result)
        else:
            log.info("No ACTIVE TODOS.md content — skipping sync")
            sync_result = None

        # Step 8: render_active_todos() with current timestamp
        log.info("Step 8: render_active_todos")
        from zoneinfo import ZoneInfo
        pst = ZoneInfo("America/Los_Angeles")
        last_synced = datetime.now(pst).strftime("%Y-%m-%d %H:%M PST")
        done_reset_hour_pst: int = config.get("done_reset_hour_pst", 5)
        new_todos_content = render_active_todos(
            conn,
            last_synced=last_synced,
            done_reset_hour_pst=done_reset_hour_pst,
        )

        if not new_todos_content:
            log.error("render_active_todos returned empty content — skipping write")
            return False

    finally:
        conn.close()

    # Write the regenerated file
    todos_path.parent.mkdir(parents=True, exist_ok=True)
    todos_path.write_text(new_todos_content, encoding="utf-8")
    log.info("Wrote regenerated ACTIVE TODOS.md (%d chars)", len(new_todos_content))

    # Step 9: git add ACTIVE TODOS.md
    log.info("Step 9: git add ACTIVE TODOS.md")
    subprocess.run(
        ["git", "add", str(todos_path)],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )

    # Step 10: git commit + push
    log.info("Step 10: git commit + push")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit_message = f"vault-watcher: sync [{timestamp}]"

    # Stage all modified files (annotation cleanups + todos render)
    all_staged = list({todos_path} | set(staging_files))
    committed = git_commit_and_push(vault_path, all_staged, commit_message)

    if committed:
        log.info("Vault updated and committed: %s", commit_message)
    else:
        log.info("No vault changes to commit (idempotent run)")

    log.info("vault-processor complete")
    return True


# ---------------------------------------------------------------------------
# Main entry point (for direct/manual invocation)
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Vault processor — execution and mutation")
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH_DEFAULT),
        help="Path to vault-watch-config.json",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH_DEFAULT),
        help="Path to self_action_items.db",
    )
    parser.add_argument(
        "--skip-lock",
        action="store_true",
        default=False,
        help=(
            "Skip acquiring the process mutex lock. Use ONLY when invoked by vault-watcher.py, "
            "which holds the lock on behalf of this process. Passing this flag when the caller "
            "does not hold the lock removes the mutual-exclusion guarantee."
        ),
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    db_path = Path(args.db)

    config = _load_config(config_path)

    if args.skip_lock:
        # vault-watcher.py holds /tmp/vault-processor.lock for the duration of this run.
        # subprocess.run(close_fds=True) does not inherit the parent fd, so re-acquiring
        # the lock here would always fail with BlockingIOError. The watcher already holds
        # the lock, so mutual exclusion is guaranteed — skip the acquire.
        log.debug("--skip-lock set: skipping lock acquisition (vault-watcher.py holds the lock)")
        run_processor(config, db_path)
    else:
        # When invoked directly (not via vault-watcher.py), acquire the lock ourselves
        lock_fd = acquire_lock_or_skip(LOCK_PATH)
        if lock_fd is None:
            log.info("skipping: processor already running (lock held)")
            return

        try:
            run_processor(config, db_path)
        finally:
            release_lock(lock_fd)


if __name__ == "__main__":
    main()
