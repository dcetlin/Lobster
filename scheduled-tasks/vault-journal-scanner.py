#!/usr/bin/env python3
"""
vault-journal-scanner.py — Extract unchecked todo items from Obsidian journal files.

Runs hourly. Scans all vault journal files (*.md, excluding ACTIVE TODOS.md and
files in .obsidian/) for unchecked checkbox items (``- [ ] ...``). For each file
not yet seen, persists extracted items to self_action_items.db via the LOS
extractor API.

Items are extracted without LLM involvement — pure regex scan. The decision to
use Type B (cron-direct) rather than Type A (LLM subagent) is deliberate:

- Extraction is deterministic: a ``- [ ] <text>`` line is unambiguously a todo.
- The job runs hourly. Spinning up an LLM session every hour for structural
  checkbox extraction adds latency and cost with no quality benefit.
- "Did I write '- [ ] call Sarah' in my journal?" is not a reasoning task.

Dedup is handled by src.los.db.find_duplicate, which computes a normalized
content hash and skips items already in the DB under any source.

Type B dispatch pattern:
    0 * * * * cd ~/lobster && uv run scheduled-tasks/vault-journal-scanner.py >> ~/lobster-workspace/scheduled-jobs/logs/vault-journal-scanner.log 2>&1 # LOBSTER-VAULT-JOURNAL-SCANNER

jobs.json entry:
    {
        "name": "vault-journal-scanner",
        "type": "B",
        "dispatch": "cron-direct",
        "schedule": "0 * * * *",
        "schedule_human": "Hourly",
        "task_file": null,
        "enabled": true
    }

Run standalone (for testing):
    uv run ~/lobster/scheduled-tasks/vault-journal-scanner.py [--dry-run] [--vault PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.los.db import connect  # noqa: E402
from src.los.extractor import extract_action_items  # noqa: E402
from src.utils.jobs import is_job_enabled  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("vault-journal-scanner")

# ---------------------------------------------------------------------------
# Constants — named after spec requirements (never magic literals)
# ---------------------------------------------------------------------------

JOB_NAME = "vault-journal-scanner"

# Files and directories to exclude when scanning the vault
SCAN_EXCLUDES: frozenset[str] = frozenset({".git", ".obsidian", ".trash"})

# The managed todo file — exclude so we don't double-extract its items
ACTIVE_TODOS_FILENAME = "✅ ACTIVE TODOS.md"

# State file records which journal files have been fully scanned.
# Lives outside the repo so it persists across upgrades.
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_USER_CONFIG = Path(os.environ.get("LOBSTER_USER_CONFIG", Path.home() / "lobster-user-config"))

STATE_FILE_PATH = _WORKSPACE / "data" / "vault-journal-scanner-state.json"
DB_PATH_DEFAULT = _USER_CONFIG / "data" / "self_action_items.db"

# Checkbox pattern: "- [ ] <text>" (leading whitespace allowed for nested items)
UNCHECKED_CHECKBOX_RE = re.compile(r"^[ \t]*- \[ \]\s+(.+)$", re.MULTILINE)

# Priority assigned to all journal-extracted items (spec: medium, can be edited)
JOURNAL_ITEM_DEFAULT_PRIORITY = 5


# ---------------------------------------------------------------------------
# State management — pure load/save, no mutation
# ---------------------------------------------------------------------------


def load_scanner_state(state_path: Path) -> dict[str, str]:
    """Load the scanner state from disk.

    Returns a dict mapping file path (str) → ISO timestamp of last scan.
    Returns {} on any error so the scanner degrades gracefully.
    """
    if not state_path.exists():
        return {}
    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
        return {}
    except Exception as exc:
        log.warning("Could not load scanner state from %s: %s", state_path, exc)
        return {}


def save_scanner_state(state_path: Path, state: dict[str, str]) -> None:
    """Persist the scanner state to disk (atomic write via temp file).

    Side effect: writes to state_path. Isolated at the boundary.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(state_path)
    except Exception as exc:
        log.error("Could not save scanner state to %s: %s", state_path, exc)


# ---------------------------------------------------------------------------
# Pure extraction helpers
# ---------------------------------------------------------------------------


def extract_checkboxes(content: str) -> list[str]:
    """Extract all unchecked checkbox item texts from markdown content.

    Pure function — no I/O.

    Matches: ``- [ ] <text>`` (with optional leading whitespace for nesting).
    Returns a list of stripped text strings (empty list if none found).
    """
    return [m.group(1).strip() for m in UNCHECKED_CHECKBOX_RE.finditer(content)
            if m.group(1).strip()]


def _file_key(vault_path: Path, file_path: Path) -> str:
    """Return a stable string key for a vault file (vault-relative path as str)."""
    try:
        return str(file_path.relative_to(vault_path))
    except ValueError:
        return str(file_path)


def _source_name(vault_path: Path, file_path: Path) -> str:
    """Compute the source name for DB entries from a journal file path.

    Format: ``journal:<vault-relative-stem>`` — consistent with existing
    manual extractions (e.g. ``journal-121`` for entry 121).
    """
    try:
        rel = file_path.relative_to(vault_path)
    except ValueError:
        rel = file_path
    # Use the stem (filename without .md) as the source slug
    stem = rel.stem if rel.stem else str(rel)
    # Collapse spaces and special chars to hyphens for readability
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return f"journal:{slug}"


# ---------------------------------------------------------------------------
# Vault scanning
# ---------------------------------------------------------------------------


def collect_journal_files(vault_path: Path) -> list[Path]:
    """Return all markdown files in the vault that are candidate journal files.

    Exclusions (pure — no side effects):
    - Files inside excluded directories (.git, .obsidian, .trash)
    - The ACTIVE TODOS.md file (managed separately by vault-processor)

    Returns sorted list of absolute Paths.
    """
    result: list[Path] = []
    for md_file in vault_path.rglob("*.md"):
        # Skip excluded directories anywhere in the path
        parts = set(md_file.relative_to(vault_path).parts)
        if parts & SCAN_EXCLUDES:
            continue
        # Skip the managed todo file
        if md_file.name == ACTIVE_TODOS_FILENAME:
            continue
        result.append(md_file)
    return sorted(result)


def _is_file_changed(file_path: Path, last_scanned_iso: Optional[str]) -> bool:
    """Return True if the file has been modified since last_scanned_iso.

    Compares file mtime to the recorded scan timestamp. Files with no recorded
    scan timestamp are always treated as new/changed.
    """
    if last_scanned_iso is None:
        return True
    try:
        mtime = file_path.stat().st_mtime
        mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        last_scan_dt = datetime.fromisoformat(last_scanned_iso.replace("Z", "+00:00"))
        return mtime_dt > last_scan_dt
    except Exception:
        return True  # Default: treat as new on any error


# ---------------------------------------------------------------------------
# Main scan logic
# ---------------------------------------------------------------------------


def scan_vault(
    vault_path: Path,
    db_path: Path,
    state: dict[str, str],
    dry_run: bool = False,
) -> tuple[int, int, dict[str, str]]:
    """Scan vault journal files and persist new todo items.

    Args:
        vault_path: Root of the Obsidian vault.
        db_path: Path to self_action_items.db.
        state: Current scanner state (file_key → last_scanned_iso).
        dry_run: If True, log what would be extracted but don't write.

    Returns:
        (files_scanned, items_extracted, updated_state)

    Side effects (when not dry_run):
        - Writes to self_action_items.db via extract_action_items
        - Returns updated state dict (caller is responsible for saving)
    """
    journal_files = collect_journal_files(vault_path)
    log.info("Found %d markdown files in vault (after exclusions)", len(journal_files))

    now_iso = datetime.now(timezone.utc).isoformat()
    files_scanned = 0
    items_extracted = 0
    updated_state = dict(state)  # immutable input — build new state

    conn = connect(db_path) if not dry_run else None
    try:
        for file_path in journal_files:
            key = _file_key(vault_path, file_path)
            last_scanned = state.get(key)

            if not _is_file_changed(file_path, last_scanned):
                log.debug("Skipping unchanged file: %s", key)
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                log.warning("Could not read %s: %s", key, exc)
                continue

            checkboxes = extract_checkboxes(content)
            if not checkboxes:
                # No unchecked items — still mark as scanned to avoid re-reading
                updated_state[key] = now_iso
                continue

            source = _source_name(vault_path, file_path)
            log.info("  %s: found %d unchecked item(s)", key, len(checkboxes))

            if dry_run:
                for text in checkboxes:
                    log.info("    [dry-run] would extract: %r (source=%s)", text[:80], source)
                items_extracted += len(checkboxes)
            else:
                # Build items in the format extract_action_items expects
                items = [{"text": text, "priority": JOURNAL_ITEM_DEFAULT_PRIORITY}
                         for text in checkboxes]
                saved = extract_action_items(
                    conn=conn,
                    items=items,
                    source=source,
                    source_message_id=key,  # vault-relative path as message ID
                )
                new_count = len(saved)
                items_extracted += new_count
                log.info("    Persisted %d item(s) from %s (dedup skipped duplicates)",
                         new_count, key)

            updated_state[key] = now_iso
            files_scanned += 1

    finally:
        if conn is not None:
            conn.close()

    return files_scanned, items_extracted, updated_state


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_vault_path() -> Optional[Path]:
    """Resolve vault path from vault-watch-config.json.

    Returns None if config is missing or vault_path is not set.
    """
    config_path = _USER_CONFIG / "data" / "vault-watch-config.json"
    if not config_path.exists():
        log.warning("vault-watch-config.json not found at %s", config_path)
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        raw = data.get("vault_path", "")
        if not raw:
            log.warning("vault_path not set in vault-watch-config.json")
            return None
        return Path(raw).expanduser()
    except Exception as exc:
        log.error("Could not read vault-watch-config.json: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Vault journal scanner — extract todo items")
    parser.add_argument("--dry-run", action="store_true", help="Log what would be extracted without writing to DB")
    parser.add_argument("--vault", help="Override vault path (default: from vault-watch-config.json)")
    parser.add_argument("--db", default=str(DB_PATH_DEFAULT), help="Override DB path")
    parser.add_argument("--state", default=str(STATE_FILE_PATH), help="Override state file path")
    args = parser.parse_args()

    if not is_job_enabled(JOB_NAME):
        log.info("Job %s is disabled in jobs.json — exiting.", JOB_NAME)
        return

    # Resolve vault path
    vault_path: Optional[Path] = Path(args.vault).expanduser() if args.vault else _load_vault_path()
    if vault_path is None:
        log.error("Cannot determine vault path — exiting. Set vault_path in vault-watch-config.json or pass --vault.")
        sys.exit(1)

    if not vault_path.exists():
        log.error("Vault path does not exist: %s — exiting.", vault_path)
        sys.exit(1)

    db_path = Path(args.db)
    state_path = Path(args.state)

    log.info("Starting vault journal scan (vault=%s, dry_run=%s)", vault_path, args.dry_run)

    state = load_scanner_state(state_path)
    log.info("Loaded state: %d previously scanned files", len(state))

    files_scanned, items_extracted, updated_state = scan_vault(
        vault_path=vault_path,
        db_path=db_path,
        state=state,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        save_scanner_state(state_path, updated_state)

    log.info(
        "Scan complete: %d file(s) scanned, %d item(s) extracted%s.",
        files_scanned,
        items_extracted,
        " (dry-run)" if args.dry_run else "",
    )


if __name__ == "__main__":
    main()
