"""
Centralized path constants for WOS and orchestration modules.

All key paths are derived from the LOBSTER_WORKSPACE environment variable,
with a sensible fallback to ~/lobster-workspace. Import from this module
rather than re-deriving paths inline — inline derivations have caused multiple
bugs when the path logic drifted between files (#4, #5, #6).

Usage:
    from src.orchestration.paths import REGISTRY_DB, WOS_CONFIG, SURFACE_QUEUE

Environment variables honored:
    LOBSTER_WORKSPACE  — workspace root (default: ~/lobster-workspace)
    LOBSTER_REPO       — repo root (default: ~/lobster)
    REGISTRY_DB_PATH   — override path for registry.db (default: <workspace>/orchestration/registry.db)
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Root anchors
# ---------------------------------------------------------------------------

LOBSTER_WORKSPACE = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
)

LOBSTER_REPO = Path(
    os.environ.get("LOBSTER_REPO", os.environ.get("LOBSTER_INSTALL_DIR", str(Path.home() / "lobster")))
)

# ---------------------------------------------------------------------------
# WOS / orchestration
# ---------------------------------------------------------------------------

REGISTRY_DB = Path(os.environ["REGISTRY_DB_PATH"]) if os.environ.get("REGISTRY_DB_PATH") else LOBSTER_WORKSPACE / "orchestration" / "registry.db"
WOS_CONFIG = LOBSTER_WORKSPACE / "data" / "wos-config.json"

# ---------------------------------------------------------------------------
# Meta / reflective surface queue
# ---------------------------------------------------------------------------

META_DIR = LOBSTER_WORKSPACE / "meta"
SURFACE_QUEUE = META_DIR / "reflective-surface-queue.json"

# Oracle files live in the repo, not the workspace
ORACLE_DECISIONS = LOBSTER_REPO / "oracle" / "decisions.md"
ORACLE_LEARNINGS = LOBSTER_REPO / "oracle" / "learnings.md"

# ---------------------------------------------------------------------------
# Hygiene / auto-router queue
# ---------------------------------------------------------------------------

# Canonical queue directory — reflective-surface-queue.json lives here.
# Historical note: an earlier bug used "hygiene/meta/" (which never existed);
# the canonical location has always been META_DIR (i.e. <workspace>/meta/).
HYGIENE_QUEUE_DIR = META_DIR

# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

SCHEDULED_JOBS_DIR = LOBSTER_WORKSPACE / "scheduled-jobs"
JOBS_JSON = SCHEDULED_JOBS_DIR / "jobs.json"
