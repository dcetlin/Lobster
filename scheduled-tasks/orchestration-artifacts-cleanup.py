#!/usr/bin/env python3
"""Cleanup old orchestration artifacts beyond the retention cap.

Policy: keep all files newer than KEEP_DAYS (14), and always keep the
KEEP_MIN_COUNT (500) most recent files regardless of age. Delete anything
older than DELETE_AFTER_DAYS (30) that falls outside the 500-most-recent window.

Cron schedule (daily at 3:15 AM, offset from nightly-consolidation):
    15 3 * * * cd ~/lobster && uv run scheduled-tasks/orchestration-artifacts-cleanup.py >> ~/lobster-workspace/scheduled-jobs/logs/orchestration-artifacts-cleanup.log 2>&1 # LOBSTER-ORCHESTRATION-ARTIFACTS-CLEANUP

WOS-UoW: uow_20260518_f729fe
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.jobs import is_job_enabled  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

ARTIFACTS_DIR = Path.home() / "lobster-workspace/orchestration/artifacts"
KEEP_DAYS = 14
KEEP_MIN_COUNT = 500
DELETE_AFTER_DAYS = 30


def cleanup_dir(directory: Path) -> tuple[int, int]:
    now = datetime.now()
    cutoff_hard = now - timedelta(days=DELETE_AFTER_DAYS)
    cutoff_soft = now - timedelta(days=KEEP_DAYS)

    # Sort newest-first; skip subdirectories
    files = sorted(
        [f for f in directory.iterdir() if f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    kept, deleted = 0, 0
    for i, f in enumerate(files):
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if i < KEEP_MIN_COUNT or mtime >= cutoff_soft:
            kept += 1
        elif mtime < cutoff_hard:
            f.unlink()
            deleted += 1
        else:
            # Between soft and hard cutoff, outside top-500 — keep
            kept += 1

    return kept, deleted


def main() -> None:
    if not is_job_enabled("orchestration-artifacts-cleanup"):
        log.info("Job disabled, skipping.")
        sys.exit(0)

    if not ARTIFACTS_DIR.exists():
        log.warning("Directory not found: %s — skipping.", ARTIFACTS_DIR)
        sys.exit(0)

    before = sum(1 for f in ARTIFACTS_DIR.iterdir() if f.is_file())
    kept, deleted = cleanup_dir(ARTIFACTS_DIR)
    after = sum(1 for f in ARTIFACTS_DIR.iterdir() if f.is_file())

    log.info(
        "orchestration/artifacts/: before=%d kept=%d deleted=%d after=%d",
        before,
        kept,
        deleted,
        after,
    )


if __name__ == "__main__":
    main()
