"""
src/utils/staleness_gate.py — Reusable staleness gate for file-scanning scorers.

Schedule-driven file-scanning jobs (e.g. philosophy-discovery-scorer) should
call ``file_changed(file_path, job_name)`` at the top of their run, before
any LLM/inference call. If the file has not changed since the last scan,
the job should short-circuit immediately:

    from src.utils.staleness_gate import file_changed, record_scan

    if not file_changed(file_path, job_name="my-scorer"):
        write_result(... outcome_category="heat", text="<file> unchanged — skipped")
        return

    # ... run scoring ...

    record_scan(file_path, job_name="my-scorer")

Design decisions:
- Content hash (SHA-256 of file bytes) is used rather than mtime. mtime is
  fragile: it changes whenever a file is touched or re-written with identical
  content (e.g. git checkout, rsync, backup restore). Content hash detects
  actual changes reliably regardless of how the file was written.
- Records are persisted in ``~/lobster-workspace/data/staleness-records.json``
  keyed by ``{job_name}::{file_path}``. This small JSON store is created on
  first use and tolerates a missing or corrupt file (treats as "no record" →
  proceed).
- The store is written atomically (write-to-tmp, rename) via the existing
  ``src/utils/fs.atomic_write_json`` helper.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _default_record_path() -> Path:
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "staleness-records.json"


def _record_key(file_path: str | Path, job_name: str) -> str:
    """Composite key uniquely identifying a (job, file) pair in the store."""
    return f"{job_name}::{Path(file_path).resolve()}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_hash(file_path: str | Path) -> Optional[str]:
    """Return the SHA-256 hex digest of the file's bytes, or None on read error."""
    try:
        data = Path(file_path).read_bytes()
        return hashlib.sha256(data).hexdigest()
    except (OSError, PermissionError):
        return None


def _load_store(record_path: Path) -> dict:
    """
    Load the staleness record store from disk.

    Returns an empty dict when the file is absent, unreadable, or malformed.
    Callers treat an empty dict as "no prior records" — all files are considered
    changed and proceed through scoring.
    """
    try:
        return json.loads(record_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_store(store: dict, record_path: Path) -> None:
    """Atomically write the staleness record store to disk."""
    # Import here to avoid circular imports at module load time.
    from src.utils.fs import atomic_write_json
    record_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(record_path, store)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def file_changed(
    file_path: str | Path,
    job_name: str,
    record_path: Optional[Path] = None,
) -> bool:
    """
    Return True if the file has changed since the last recorded scan, False if unchanged.

    Callers should proceed with scoring only when this returns True. When it
    returns False, the job should short-circuit to a "heat" outcome:

        if not file_changed(path, job_name="my-scorer"):
            write_result(... outcome_category="heat", text="unchanged — skipped")
            return

    Behaviour at the edges:
    - File absent or unreadable → returns True (treat as changed; let the scorer
      handle the missing file error itself).
    - Store absent or corrupt → returns True (safe default: no prior record means
      proceed).
    - No prior record for this (job, file) pair → returns True.

    Args:
        file_path:   Absolute or relative path to the file being scanned.
        job_name:    The job's canonical name (matches jobs.json / write_result).
        record_path: Override path for the staleness store (used in tests).
    """
    rp = record_path or _default_record_path()
    store = _load_store(rp)
    key = _record_key(file_path, job_name)
    prior_hash = store.get(key)

    if prior_hash is None:
        # No prior record — treat as changed so the first scan always runs.
        return True

    current_hash = _compute_hash(file_path)
    if current_hash is None:
        # Unreadable file — treat as changed; let the scorer report the error.
        return True

    return current_hash != prior_hash


def record_scan(
    file_path: str | Path,
    job_name: str,
    record_path: Optional[Path] = None,
) -> None:
    """
    Record the current content hash of the file as the baseline for future comparisons.

    Call this after a successful scan/score run, not before. Writing the record
    before the scan completes would cause the next run to skip a file that was
    changed between scan start and record write.

    If the file is unreadable or the store cannot be written, the error is
    swallowed silently — a missing record is always safe (it causes the next run
    to treat the file as changed and proceed, which is the correct conservative
    fallback).

    Args:
        file_path:   Absolute or relative path to the file that was scanned.
        job_name:    The job's canonical name.
        record_path: Override path for the staleness store (used in tests).
    """
    rp = record_path or _default_record_path()
    current_hash = _compute_hash(file_path)
    if current_hash is None:
        # If we can't hash it, don't record anything — next run will retry.
        return

    store = _load_store(rp)
    key = _record_key(file_path, job_name)
    store[key] = current_hash
    try:
        _save_store(store, rp)
    except OSError:
        # Non-fatal: failure to persist the record means the next run re-scores.
        pass
