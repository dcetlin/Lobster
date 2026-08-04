"""
src/utils/fs.py — Filesystem utility functions for Lobster.

Canonical implementations of atomic file operations used across the codebase.
All functions are pure in the sense that they have no hidden dependencies —
their only side effects are the filesystem operations described in their names.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Atomically write JSON data to a file.

    Uses write-to-temp-then-rename pattern. On POSIX, rename() within the
    same filesystem is atomic, so readers never see a partial file.

    Args:
        path: Target file path.
        data: JSON-serializable data.
        indent: JSON indentation level.

    Raises:
        OSError: If the write or rename fails.
        TypeError: If data is not JSON-serializable.
    """
    # Serialize first (fail fast if not serializable)
    content = json.dumps(data, indent=indent)

    # Write to temp file in same directory (same filesystem = atomic rename)
    dir_path = str(path.parent)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # Force to disk before rename
        os.rename(tmp_path, str(path))
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_create_json(path: Path, data: Any, indent: int = 2) -> bool:
    """Atomically create a JSON file only if it does not already exist.

    ``atomic_write_json`` makes each individual write atomic (temp file +
    fsync + rename) but the rename always succeeds and always overwrites —
    it gives no way to tell whether a destination already existed, so two
    concurrent callers racing for the same path both "win" and the second
    one silently clobbers the first. This function closes that gap: it
    writes the content to a temp file, then links (not renames) the temp
    file onto the destination. Hard-link creation is atomic on POSIX and
    fails with ``FileExistsError`` if the destination is already present,
    so at most one caller's content ever lands at ``path`` — the first
    writer wins, and every later caller can tell it lost the race.

    Intended for single-shot slots that must be immutable once written
    (e.g. the agent channel's ``agent-replies/<request_id>.json`` — see
    the agent-channel protocol spec, principle 1: "Single-shot per-request
    reply slot... nothing may overwrite it with a different answer").

    Args:
        path: Target file path. Must be on the same filesystem as a temp
            directory alongside it (uses ``path.parent`` for the temp file,
            same as ``atomic_write_json``).
        data: JSON-serializable data.
        indent: JSON indentation level.

    Returns:
        True if this call created ``path``. False if ``path`` already
        existed — in which case it was left completely untouched.

    Raises:
        OSError: If the write or link fails for a reason other than the
            destination already existing.
        TypeError: If data is not JSON-serializable.
    """
    content = json.dumps(data, indent=indent)

    dir_path = str(path.parent)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp_path, str(path))
            return True
        except FileExistsError:
            return False
    finally:
        # The temp file is always disposable: on success it's a spare hard
        # link to the same inode as `path` (unlinking it doesn't touch
        # `path`); on FileExistsError it never got linked at all.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def safe_move(src: Path, dest: Path) -> bool:
    """Safely move a file, ensuring source exists before moving.

    Returns True if moved, False if source was already gone (idempotent).
    Raises OSError on other failures.
    """
    try:
        src.rename(dest)
        return True
    except FileNotFoundError:
        # Source already moved (concurrent processing) — idempotent
        return False
