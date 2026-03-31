"""
GitHub Issue Cultivator — promotes ALL open GitHub issues into the WOS registry as UoWs.

Design: no scoring threshold. Every open issue is a candidate unless it matches a
skip condition. The cultivator is a pure pipeline: fetch → classify → promote.

Skip conditions (applied before promotion):
  - Issue already in WOS as a non-terminal UoW (idempotency — handled by Registry.upsert)
  - Issue has label "wos-phase-2" (meta-tracking issues, not work items)
  - Issue has label "tracking" (meta-tracking issues, not work items)

Priority classification (informational; stored in summary but not yet persisted to WOS schema):
  - bug or urgent label → high
  - enhancement or feat label → low
  - anything else → medium

Usage:
    uv run src/orchestration/cultivator.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    label_names: frozenset[str]
    assignee_count: int
    created_at: datetime
    url: str


@dataclass(frozen=True)
class ClassifiedIssue:
    issue: GitHubIssue
    priority: str  # "high" | "medium" | "low"


@dataclass(frozen=True)
class PromotionResult:
    issue_number: int
    title: str
    uow_id: str
    was_new: bool


@dataclass(frozen=True)
class SweepSummary:
    scanned: int
    skipped_meta: int
    promoted: int
    already_registered: int
    promotion_results: tuple[PromotionResult, ...]


# ---------------------------------------------------------------------------
# GitHub data fetching — pure transformation from raw JSON
# ---------------------------------------------------------------------------

def _parse_issue(raw: dict) -> GitHubIssue:
    """Convert raw gh CLI JSON dict into a typed GitHubIssue."""
    created_at = datetime.fromisoformat(raw["createdAt"].replace("Z", "+00:00"))
    label_names = frozenset(label["name"] for label in raw.get("labels", []))
    assignee_count = len(raw.get("assignees", []))
    number = raw["number"]
    url = f"https://github.com/dcetlin/Lobster/issues/{number}"
    return GitHubIssue(
        number=number,
        title=raw["title"],
        body=raw.get("body") or "",
        label_names=label_names,
        assignee_count=assignee_count,
        created_at=created_at,
        url=url,
    )


def fetch_open_issues(repo: str, limit: int = 300) -> list[GitHubIssue]:
    """Fetch open issues from GitHub via gh CLI. Returns list of GitHubIssue."""
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--limit", str(limit),
            "--json", "number,title,labels,body,assignees,createdAt",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    raw_issues: list[dict] = json.loads(result.stdout)
    return [_parse_issue(raw) for raw in raw_issues]


# ---------------------------------------------------------------------------
# Classification — pure functions, no side effects
# ---------------------------------------------------------------------------

_SKIP_LABELS = frozenset({"wos-phase-2", "tracking"})
_HIGH_PRIORITY_LABELS = frozenset({"bug", "urgent"})
_LOW_PRIORITY_LABELS = frozenset({"enhancement", "feat"})


def _should_skip(issue: GitHubIssue) -> str | None:
    """Return a skip reason string if this issue should be excluded, else None."""
    if issue.label_names & _SKIP_LABELS:
        return f"label: {issue.label_names & _SKIP_LABELS}"
    return None


def _classify_priority(issue: GitHubIssue) -> str:
    """Map issue labels to a priority tier. Pure function — no I/O."""
    if issue.label_names & _HIGH_PRIORITY_LABELS:
        return "high"
    if issue.label_names & _LOW_PRIORITY_LABELS:
        return "low"
    return "medium"


def classify_issues(issues: list[GitHubIssue]) -> tuple[list[ClassifiedIssue], int]:
    """
    Classify all issues. Returns (classified_issues, skipped_count).
    Meta-tracking issues (wos-phase-2, tracking labels) are excluded.
    """
    classified = []
    skipped = 0
    for issue in issues:
        skip_reason = _should_skip(issue)
        if skip_reason:
            skipped += 1
            continue
        classified.append(ClassifiedIssue(issue=issue, priority=_classify_priority(issue)))
    return classified, skipped


# ---------------------------------------------------------------------------
# WOS promotion — side effects isolated here
# ---------------------------------------------------------------------------

def _build_db_path() -> Path:
    """Resolve the WOS database path."""
    return Path.home() / "lobster-workspace" / "data" / "wos.db"


def promote_to_wos(
    candidates: list[ClassifiedIssue],
    dry_run: bool = False,
) -> tuple[list[PromotionResult], int]:
    """
    Promote all candidate issues into the WOS registry.

    Returns (promotion_results, already_registered_count).
    Registry.upsert handles idempotency — issues already in WOS as non-terminal
    UoWs return UpsertSkipped.

    Note: source is written by Registry as "github:issue/{number}" which traces
    back to dcetlin/Lobster since that is the only repo cultivator operates on.
    """
    # Import here so the module can be imported without WOS available
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from src.orchestration.registry import Registry, UpsertInserted, UpsertSkipped

    db_path = _build_db_path()
    registry = Registry(db_path)

    results: list[PromotionResult] = []
    already_registered = 0

    for classified in candidates:
        issue = classified.issue

        if dry_run:
            print(
                f"  [dry-run] would promote #{issue.number}: {issue.title[:60]} "
                f"(priority={classified.priority})"
            )
            continue

        result = registry.upsert(
            issue_number=issue.number,
            title=issue.title,
        )

        if isinstance(result, UpsertInserted):
            results.append(
                PromotionResult(
                    issue_number=issue.number,
                    title=issue.title,
                    uow_id=result.id,
                    was_new=True,
                )
            )
        elif isinstance(result, UpsertSkipped):
            already_registered += 1

    return results, already_registered


# ---------------------------------------------------------------------------
# Summary rendering — pure formatting functions
# ---------------------------------------------------------------------------

def _render_summary(summary: SweepSummary, dry_run: bool) -> str:
    """Render a human-readable sweep summary."""
    lines = [
        f"GitHub Issue Cultivator — {'DRY RUN ' if dry_run else ''}Sweep Complete",
        f"  Scanned:           {summary.scanned}",
        f"  Skipped (meta):    {summary.skipped_meta}",
        f"  Already in WOS:    {summary.already_registered}",
        f"  Promoted:          {summary.promoted}",
    ]
    if summary.promotion_results:
        lines.append("  Promoted issues:")
        for pr in summary.promotion_results:
            lines.append(f"    #{pr.issue_number} → {pr.uow_id}: {pr.title[:60]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run_sweep(
    repo: str = "dcetlin/Lobster",
    limit: int = 300,
    dry_run: bool = False,
) -> SweepSummary:
    """
    Main sweep function. Composes the pure pipeline stages:
    fetch → classify → promote.
    Returns a SweepSummary value object.
    """
    issues = fetch_open_issues(repo=repo, limit=limit)
    candidates, skipped_meta = classify_issues(issues)

    if dry_run:
        print(f"Fetched {len(issues)} open issues, {len(candidates)} eligible (all non-meta):")

    promotion_results, already_registered = promote_to_wos(candidates, dry_run=dry_run)

    return SweepSummary(
        scanned=len(issues),
        skipped_meta=skipped_meta,
        promoted=len(promotion_results),
        already_registered=already_registered,
        promotion_results=tuple(promotion_results),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Issue Cultivator for WOS")
    parser.add_argument("--repo", default="dcetlin/Lobster", help="GitHub repo (owner/name)")
    parser.add_argument("--limit", type=int, default=300, help="Max issues to fetch")
    parser.add_argument("--dry-run", action="store_true", help="Classify but do not write to WOS")
    args = parser.parse_args()

    summary = run_sweep(
        repo=args.repo,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(_render_summary(summary, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
