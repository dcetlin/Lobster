#!/usr/bin/env python3
"""
vault-watcher.py — Vault change detection and debounce timer.

Detection and timing ONLY. This script does not sync, render, or commit.
Its sole job: poll the remote git HEAD, manage debounce state, and invoke
vault-processor.py when the debounce threshold has passed.

Type B (cron-direct) job. Two cron entries achieve 30-second polling:

Cron entries (two required — cron cannot fire sub-minute natively):
    * * * * * cd ~/lobster && uv run scheduled-tasks/vault-watcher.py >> ~/lobster-workspace/scheduled-jobs/logs/vault-watcher.log 2>&1 # LOBSTER-VAULT-WATCHER
    * * * * * sleep 30 && cd ~/lobster && uv run scheduled-tasks/vault-watcher.py >> ~/lobster-workspace/scheduled-jobs/logs/vault-watcher.log 2>&1 # LOBSTER-VAULT-WATCHER-HALF

jobs.json entries:
    {
        "vault-watcher": {
            "name": "vault-watcher",
            "type": "B",
            "dispatch": "cron-direct",
            "schedule": "every 30s (two cron entries)",
            "task_file": null,
            "enabled": true
        },
        "vault-watcher-half": {
            "name": "vault-watcher-half",
            "type": "B",
            "dispatch": "cron-direct",
            "schedule": "every 30s offset (second cron entry)",
            "task_file": null,
            "enabled": true
        }
    }

Run standalone (for debugging):
    uv run ~/lobster/scheduled-tasks/vault-watcher.py
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_TASKS_DIR = Path(__file__).parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(_TASKS_DIR))

from obsidian_sync_core import acquire_lock_or_skip, release_lock  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("vault-watcher")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "vault-watcher"
JOB_NAME_HALF = "vault-watcher-half"

# Default paths (overridable via config)
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_USER_CONFIG = Path(os.environ.get("LOBSTER_USER_CONFIG", Path.home() / "lobster-user-config"))

CONFIG_PATH = _USER_CONFIG / "data" / "vault-watch-config.json"
STATE_PATH = _USER_CONFIG / "data" / "vault-watch-state.json"
LOCK_PATH = Path("/tmp/vault-processor.lock")

# Config defaults
DEFAULT_DEBOUNCE_SECONDS = 60
DEFAULT_MAX_DEBOUNCE_SECONDS = 300
DEFAULT_VAULT_PATH = _WORKSPACE / "obsidian-vault"
DEFAULT_WATCHED_FILES = ["✅ ACTIVE TODOS.md"]
DEFAULT_ANNOTATION_SCOPE = "all"

# Debounce validation bounds
MIN_DEBOUNCE_SECONDS = 10
MAX_DEBOUNCE_SECONDS_CAP = 3600

# ---------------------------------------------------------------------------
# Jobs.json enabled gate (Type B compliance)
# ---------------------------------------------------------------------------


def _get_workspace() -> Path:
    return Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))


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
# Config loading
# ---------------------------------------------------------------------------


def _load_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load and validate vault-watch-config.json.

    On first run (absent), writes defaults and continues.
    On malformed JSON, logs error and raises (hard stop — avoid silent misconfiguration).
    """
    if not config_path.exists():
        log.info("Config file not found at %s — creating defaults", config_path)
        defaults = _default_config()
        _write_json_atomic(config_path, defaults)
        return defaults

    try:
        with config_path.open() as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as e:
        log.error("Config file malformed JSON at %s: %s", config_path, e)
        raise

    return _validate_config(raw, config_path)


def _default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "vault_path": str(DEFAULT_VAULT_PATH),
        "debounce_seconds": DEFAULT_DEBOUNCE_SECONDS,
        "max_debounce_seconds": DEFAULT_MAX_DEBOUNCE_SECONDS,
        "watched_files": DEFAULT_WATCHED_FILES,
        "annotation_scope": DEFAULT_ANNOTATION_SCOPE,
        "lobster_chat_id": None,
        "webhook_mode": False,
    }


def _validate_config(raw: dict, config_path: Path) -> dict[str, Any]:
    """Validate and coerce config values. Returns validated dict."""
    config = dict(raw)  # shallow copy — unknown keys pass through (forward compat)

    # vault_path
    vault_path_str = config.get("vault_path", str(DEFAULT_VAULT_PATH))
    if not isinstance(vault_path_str, str):
        log.warning("vault_path must be a string — using default")
        vault_path_str = str(DEFAULT_VAULT_PATH)
    config["vault_path"] = vault_path_str

    # debounce_seconds
    debounce = config.get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS)
    if not isinstance(debounce, int) or debounce <= 0:
        log.warning("debounce_seconds invalid (%r) — using default %d", debounce, DEFAULT_DEBOUNCE_SECONDS)
        debounce = DEFAULT_DEBOUNCE_SECONDS
    debounce = max(MIN_DEBOUNCE_SECONDS, min(debounce, MAX_DEBOUNCE_SECONDS_CAP))
    config["debounce_seconds"] = debounce

    # max_debounce_seconds
    max_debounce = config.get("max_debounce_seconds", DEFAULT_MAX_DEBOUNCE_SECONDS)
    if not isinstance(max_debounce, int) or max_debounce <= 0:
        log.warning("max_debounce_seconds invalid (%r) — using debounce_seconds * 5", max_debounce)
        max_debounce = debounce * 5
    if max_debounce < debounce:
        log.warning("max_debounce_seconds (%d) < debounce_seconds (%d) — setting to debounce * 5", max_debounce, debounce)
        max_debounce = debounce * 5
    config["max_debounce_seconds"] = max_debounce

    # watched_files
    watched = config.get("watched_files", DEFAULT_WATCHED_FILES)
    if not isinstance(watched, list):
        log.warning("watched_files must be a list — using default")
        watched = DEFAULT_WATCHED_FILES
    else:
        watched = [f for f in watched if isinstance(f, str) or (log.warning("Skipping non-string watched_file: %r", f) or False)]
    config["watched_files"] = watched

    # annotation_scope
    scope = config.get("annotation_scope", DEFAULT_ANNOTATION_SCOPE)
    if scope not in ("all", "watched_only"):
        log.warning("annotation_scope must be 'all' or 'watched_only' — using 'all'")
        scope = "all"
    config["annotation_scope"] = scope

    # lobster_chat_id
    chat_id = config.get("lobster_chat_id")
    if chat_id is not None and (not isinstance(chat_id, int) or chat_id <= 0):
        log.error("lobster_chat_id must be a positive integer — got %r", chat_id)
        raise ValueError(f"lobster_chat_id must be a positive integer, got {chat_id!r}")
    config["lobster_chat_id"] = chat_id

    return config


# ---------------------------------------------------------------------------
# State file management
# ---------------------------------------------------------------------------


def _load_state(state_path: Path = STATE_PATH) -> dict[str, Any]:
    """Load vault-watch-state.json. Initializes fresh state if absent or malformed."""
    fresh: dict[str, Any] = {
        "last_known_head": None,
        "last_push_at": None,
        "last_processed_head": None,
        "first_push_at_in_burst": None,
    }
    if not state_path.exists():
        log.info("State file not found — initializing fresh state")
        _write_json_atomic(state_path, fresh)
        return fresh

    try:
        with state_path.open() as fh:
            state = json.load(fh)
        # Ensure all required keys are present
        for k, v in fresh.items():
            state.setdefault(k, v)
        return state
    except (json.JSONDecodeError, Exception) as e:
        log.warning("State file malformed (%s) — reinitializing", e)
        _write_json_atomic(state_path, fresh)
        return fresh


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically using rename to avoid partial writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Git operations (watcher only — detection, no mutation)
# ---------------------------------------------------------------------------


def _get_remote_head(vault_path: Path, timeout: int = 30) -> Optional[str]:
    """Fetch remote and return the current HEAD SHA, or None on failure."""
    # Determine the default branch
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=str(vault_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if fetch.returncode != 0:
            log.warning("git fetch failed: %s", fetch.stderr.strip())
            return None
    except subprocess.TimeoutExpired:
        log.warning("git fetch timed out after %ds", timeout)
        return None

    # Get the remote HEAD SHA
    rev = subprocess.run(
        ["git", "rev-parse", "origin/HEAD"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if rev.returncode != 0:
        # Try FETCH_HEAD as fallback
        rev = subprocess.run(
            ["git", "rev-parse", "FETCH_HEAD"],
            cwd=str(vault_path),
            capture_output=True,
            text=True,
        )
        if rev.returncode != 0:
            log.warning("git rev-parse failed: %s", rev.stderr.strip())
            return None

    sha = rev.stdout.strip()
    if not sha:
        log.warning("git rev-parse returned empty SHA")
        return None
    return sha


# ---------------------------------------------------------------------------
# Processor invocation
# ---------------------------------------------------------------------------


def _invoke_processor(config: dict, lock_path: Path = LOCK_PATH) -> None:
    """Invoke vault-processor.py as a subprocess.

    The processor inherits the lock file descriptor (the OS keeps it open
    across fork/exec). vault-processor.py re-acquires the same lock when
    invoked directly — this is fine because the watcher will have released
    it before the processor starts (the lock is released in the finally block
    of main() after invoking the processor synchronously).

    We invoke the processor synchronously (blocking) so that vault-watcher.py
    holds the lock for the duration of the processor run and the next watcher
    tick finds the lock held and skips.
    """
    processor_script = Path(__file__).parent / "vault-processor.py"
    config_path = str(CONFIG_PATH)

    cmd = [sys.executable, str(processor_script), "--config", config_path]
    log.info("Invoking vault-processor.py")
    result = subprocess.run(
        cmd,
        capture_output=False,  # let stdout/stderr flow to the cron log
        text=True,
    )
    if result.returncode != 0:
        log.warning("vault-processor.py exited with code %d", result.returncode)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    # Gate 1: jobs.json enabled check (Type B compliance — before any I/O)
    if not _is_job_enabled(JOB_NAME) and not _is_job_enabled(JOB_NAME_HALF):
        log.info("Job '%s' is disabled in jobs.json — exiting", JOB_NAME)
        return

    # Gate 2: process mutex (non-blocking — skip if processor is already running)
    lock_fd = acquire_lock_or_skip(LOCK_PATH)
    if lock_fd is None:
        log.info("skipping: processor already running")
        return

    try:
        config = _load_config()

        if not config.get("enabled", True):
            log.info("Vault watcher is disabled in config (enabled=false) — exiting")
            return

        vault_path = Path(config["vault_path"]).expanduser()

        # Validate vault path
        if not vault_path.exists():
            log.warning("Vault path does not exist: %s — skipping this cycle", vault_path)
            return
        if not (vault_path / ".git").exists():
            log.error("Vault path is not a git repo (no .git): %s", vault_path)
            return

        state = _load_state()
        now = time.time()

        if config.get("webhook_mode", False):
            # Webhook mode: skip git fetch — last_push_at was written by webhook receiver
            remote_head = state.get("last_known_head")
            # When webhook updates last_push_at without changing last_known_head,
            # we still need to detect the push. Use last_push_at > last_processed
            # logic only (no head comparison needed).
            # For simplicity: treat any update to last_push_at after last_processed_head
            # was written as "changed".
            changed = (
                state.get("last_push_at") is not None
                and state.get("last_known_head") != state.get("last_processed_head")
            )
        else:
            # Polling mode: fetch remote HEAD and compare
            remote_head = _get_remote_head(vault_path)
            if remote_head is None:
                return  # Network failure already logged

            changed = (remote_head != state["last_known_head"])

            if changed:
                state["last_known_head"] = remote_head
                state["last_push_at"] = now
                if state.get("first_push_at_in_burst") is None:
                    state["first_push_at_in_burst"] = now
                _write_json_atomic(STATE_PATH, state)
                log.info("New remote HEAD detected: %s (debounce timer reset)", remote_head[:12])
            elif state["last_known_head"] == state.get("last_processed_head"):
                # Already caught up — reset burst tracker if needed
                if state.get("first_push_at_in_burst") is not None:
                    state["first_push_at_in_burst"] = None
                    _write_json_atomic(STATE_PATH, state)
                return

        # Check debounce conditions
        debounce_seconds = config.get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS)
        max_debounce_seconds = config.get("max_debounce_seconds", DEFAULT_MAX_DEBOUNCE_SECONDS)
        last_push_at = state.get("last_push_at")
        first_push_at_in_burst = state.get("first_push_at_in_burst")
        last_processed_head = state.get("last_processed_head")
        last_known_head = state.get("last_known_head")

        # Guard: nothing to process if already caught up
        if last_known_head is not None and last_known_head == last_processed_head:
            return

        debounce_expired = (
            last_push_at is not None
            and (now - last_push_at) >= debounce_seconds
            and last_processed_head != last_known_head
        )
        max_debounce_exceeded = (
            first_push_at_in_burst is not None
            and (now - first_push_at_in_burst) >= max_debounce_seconds
            and last_processed_head != last_known_head
        )

        if debounce_expired or max_debounce_exceeded:
            reason = "debounce expired" if debounce_expired else "max_debounce exceeded"
            log.info("Firing processor (%s) — head=%s", reason, str(last_known_head or "")[:12])
            _invoke_processor(config)
            # After processor runs, update state
            state["last_processed_head"] = last_known_head
            state["first_push_at_in_burst"] = None
            _write_json_atomic(STATE_PATH, state)
            log.info("Processor complete — last_processed_head updated to %s", str(last_known_head or "")[:12])
        else:
            if last_push_at is not None:
                wait = debounce_seconds - (now - last_push_at)
                log.debug("Debounce not yet expired — %.0fs remaining", max(0, wait))

    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
