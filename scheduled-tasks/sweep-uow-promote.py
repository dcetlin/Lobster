#!/usr/bin/env python3
"""
Sweep → WOS UoW Promotion CLI

Called by the negentropic sweep agent after filing each new GitHub issue.
Creates a proposed WOS UoW sourced from the sweep-filed issue.

Usage:
    uv run ~/lobster/scheduled-tasks/sweep-uow-promote.py \\
        --issue-number 1234 \\
        --title "Fix entropy smell in executor" \\
        --issue-url "https://github.com/dcetlin/Lobster/issues/1234"

Exit codes:
    0 — UoW created or skipped (dedup or job-disabled); sweep should continue.
    1 — Unexpected error; logged to stderr.

The sweep agent should treat exit code 0 as success regardless of which
PromoteResult variant was returned.  The script prints a one-line status
to stdout so the sweep agent can record it in its sweep file.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running as a script (not just via importlib)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

log = logging.getLogger("sweep-uow-promote-cli")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Create a proposed WOS UoW for a sweep-filed GitHub issue.",
    )
    p.add_argument(
        "--issue-number",
        type=int,
        required=True,
        help="GitHub issue number (integer).",
    )
    p.add_argument(
        "--title",
        required=True,
        help="Issue title — used as UoW summary.",
    )
    p.add_argument(
        "--issue-url",
        required=True,
        help="Canonical GitHub issue URL, e.g. https://github.com/dcetlin/Lobster/issues/42",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        from src.orchestration.registry import Registry
        from src.orchestration.sweep_uow_promoter import promote_sweep_issue, PromoteResult

        registry = Registry()

        result = promote_sweep_issue(
            issue_number=args.issue_number,
            title=args.title,
            issue_url=args.issue_url,
            registry=registry,
        )

        status_line = {
            PromoteResult.CREATED: f"wos-uow: created proposed UoW for issue #{args.issue_number}",
            PromoteResult.SKIPPED_DEDUP: f"wos-uow: skipped issue #{args.issue_number} (UoW already exists)",
            PromoteResult.SKIPPED_JOB_DISABLED: f"wos-uow: skipped issue #{args.issue_number} (negentropic-sweep disabled)",
        }[result]

        print(status_line)
        return 0

    except Exception as exc:
        log.exception("sweep-uow-promote failed for issue #%s: %s", args.issue_number, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
