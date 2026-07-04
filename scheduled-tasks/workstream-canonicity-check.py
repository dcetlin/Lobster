#!/usr/bin/env python3
"""
Workstream canonicity check.

Type B cron-direct script — deterministic, no LLM round-trip. Flags
workstream directories that violate the canonicity rules defined in
~/lobster-workspace/assessments/workstreams-canonicity-model-20260704.md
(rule 2: README.md + log.md required; rule 3: must be listed in INDEX.md).

This check is intentionally bounded: it FLAGS candidates only. It never
deletes, moves, or edits a workstream directory. Reclassification and
archival are separate, deliberate actions (see the Long-Running Dispatch
Preamble's auto-archive-on-completion step in CLAUDE.md for the one case
where a directory move is automated, and even that is gated on an explicit
terminal marker written by the owning agent).

Job: workstream-canonicity-check (Type B)
Suggested schedule: daily, e.g. `17 3 * * *` (not yet wired into cron/jobs.json —
this script is runnable standalone via `uv run` until cron wiring is added).

Usage:
    uv run ~/lobster/scheduled-tasks/workstream-canonicity-check.py
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LOBSTER_HOME = Path(os.environ.get("LOBSTER_HOME", Path.home() / "lobster"))
sys.path.insert(0, str(LOBSTER_HOME))

from src.utils.jobs import is_job_enabled  # noqa: E402 — path insert must come first

JOB_NAME = "workstream-canonicity-check"

WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
WORKSTREAMS_ROOT = WORKSPACE / "workstreams"
INDEX_PATH = WORKSTREAMS_ROOT / "INDEX.md"
HYGIENE_DIR = WORKSPACE / "hygiene"

# Directories under workstreams/ that are not themselves workstreams and
# should never be flagged.
NON_WORKSTREAM_ENTRIES = {"archive", "HOWTO.md", "INDEX.md"}


@dataclass(frozen=True)
class WorkstreamFlag:
    name: str
    missing_readme: bool
    missing_log: bool
    not_in_index: bool

    @property
    def is_clean(self) -> bool:
        return not (self.missing_readme or self.missing_log or self.not_in_index)


# ---------------------------------------------------------------------------
# Pure functions — no I/O side effects beyond the read calls passed in
# ---------------------------------------------------------------------------


def list_workstream_dirs(workstreams_root: Path) -> list[Path]:
    """Return canonical-candidate directories under workstreams_root.

    Excludes non-workstream entries (archive/, INDEX.md, HOWTO.md) and
    anything that isn't a directory.
    """
    if not workstreams_root.exists():
        return []
    return sorted(
        p
        for p in workstreams_root.iterdir()
        if p.is_dir() and p.name not in NON_WORKSTREAM_ENTRIES
    )


def parse_index_names(index_text: str) -> set[str]:
    """Extract workstream names from INDEX.md's markdown table.

    Matches lines of the form `| <name> | ... |` where <name> is the first
    cell, skipping header/separator rows (e.g. `| Workstream | ... |` and
    `|---|---|`).
    """
    names: set[str] = set()
    for line in index_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        first_cell = cells[0]
        if first_cell.lower() == "workstream":
            continue  # header row
        if re.fullmatch(r"-+", first_cell):
            continue  # separator row
        names.add(first_cell)
    return names


def check_workstream(dir_path: Path, index_names: set[str]) -> WorkstreamFlag:
    """Evaluate one workstream directory against the canonicity rules."""
    return WorkstreamFlag(
        name=dir_path.name,
        missing_readme=not (dir_path / "README.md").exists(),
        missing_log=not (dir_path / "log.md").exists(),
        not_in_index=dir_path.name not in index_names,
    )


def evaluate_all(dirs: list[Path], index_names: set[str]) -> list[WorkstreamFlag]:
    return [check_workstream(d, index_names) for d in dirs]


def flagged_only(results: list[WorkstreamFlag]) -> list[WorkstreamFlag]:
    return [r for r in results if not r.is_clean]


def render_report(all_results: list[WorkstreamFlag], flagged: list[WorkstreamFlag], today: str) -> str:
    lines = [
        f"# Workstream Canonicity Check — {today}",
        "",
        f"Scanned {len(all_results)} workstream director{'y' if len(all_results) == 1 else 'ies'}, "
        f"flagged {len(flagged)}.",
        "",
        "This is a flag-only report per the canonicity model "
        "(assessments/workstreams-canonicity-model-20260704.md). "
        "No directories were moved, edited, or deleted.",
        "",
    ]
    if not flagged:
        lines.append("No violations found.")
        return "\n".join(lines) + "\n"

    lines.append("## Flagged")
    lines.append("")
    lines.append("| Workstream | Missing README.md | Missing log.md | Absent from INDEX.md |")
    lines.append("|---|---|---|---|")
    for r in flagged:
        lines.append(
            f"| {r.name} | {'Y' if r.missing_readme else ''} "
            f"| {'Y' if r.missing_log else ''} | {'Y' if r.not_in_index else ''} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# I/O boundary
# ---------------------------------------------------------------------------


def main() -> int:
    if not is_job_enabled(JOB_NAME):
        print(f"[{JOB_NAME}] Job disabled — skipping")
        return 0

    dirs = list_workstream_dirs(WORKSTREAMS_ROOT)
    index_text = INDEX_PATH.read_text() if INDEX_PATH.exists() else ""
    index_names = parse_index_names(index_text)

    results = evaluate_all(dirs, index_names)
    flagged = flagged_only(results)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = render_report(results, flagged, today)

    HYGIENE_DIR.mkdir(parents=True, exist_ok=True)
    report_path = HYGIENE_DIR / f"{today}-workstream-canonicity-check.md"
    report_path.write_text(report)

    print(f"[{JOB_NAME}] Scanned {len(results)}, flagged {len(flagged)}. Report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
