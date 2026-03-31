"""
GitHub Issue Cultivator — promotes qualifying GitHub issues into the WOS registry as UoWs.

Scoring heuristic (MVP, no LLM calls):
  +2  issue has label "bug" or "urgent"
  +1  issue has label "enhancement" or "feat"
  +1  issue has been open > 7 days
  +1  issue is assigned to someone

Skip conditions (applied before scoring):
  - Issue has label "wos-phase-2" (already tracked in WOS)
  - Issue title or body contains "wos-phase-2" label sentinel

Issues scoring >= 2 are promoted to WOS as proposed UoWs.

Usage:
    uv run src/orchestration/cultivator.py [--dry-run] [--limit N] [--threshold N]
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
class ScoredIssue:
    issue: GitHubIssue
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PromotionResult:
    issue_number: int
    title: str
    uow_id: str
    was_new: bool


@dataclass(frozen=True)
class SweepSummary:
    scanned: int
    skipped_wos_tracked: int
    below_threshold: int
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


def fetch_open_issues(repo: str, limit: int = 200) -> list[GitHubIssue]:
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
# Scoring — pure functions, no side effects
# ---------------------------------------------------------------------------

_SKIP_LABELS = frozenset({"wos-phase-2"})
_BUG_LABELS = frozenset({"bug", "urgent"})
_ENHANCEMENT_LABELS = frozenset({"enhancement", "feat"})
_DAYS_OPEN_THRESHOLD = 7
_DEFAULT_SCORE_THRESHOLD = 2


def _should_skip(issue: GitHubIssue) -> str | None:
    """Return a skip reason string if this issue should be excluded, else None."""
    if issue.label_names & _SKIP_LABELS:
        return f"label: {issue.label_names & _SKIP_LABELS}"
    return None


def _compute_score(issue: GitHubIssue, now: datetime) -> ScoredIssue:
    """Score an issue on 4 signals. Pure function — no I/O."""
    reasons: list[str] = []
    score = 0

    if issue.label_names & _BUG_LABELS:
        score += 2
        matched = issue.label_names & _BUG_LABELS
        reasons.append(f"label {matched} +2")

    if issue.label_names & _ENHANCEMENT_LABELS:
        score += 1
        matched = issue.label_names & _ENHANCEMENT_LABELS
        reasons.append(f"label {matched} +1")

    days_open = (now - issue.created_at).days
    if days_open > _DAYS_OPEN_THRESHOLD:
        score += 1
        reasons.append(f"open {days_open} days +1")

    if issue.assignee_count > 0:
        score += 1
        reasons.append(f"assigned ({issue.assignee_count}) +1")

    return ScoredIssue(issue=issue, score=score, reasons=tuple(reasons))


def score_issues(issues: list[GitHubIssue]) -> tuple[list[ScoredIssue], int]:
    """
    Score all issues. Returns (scored_issues, skipped_count).
    Skipped issues (wos-phase-2 label) are excluded from the returned list.
    """
    now = datetime.now(timezone.utc)
    scored = []
    skipped = 0
    for issue in issues:
        skip_reason = _should_skip(issue)
        if skip_reason:
            skipped += 1
            continue
        scored.append(_compute_score(issue, now))
    return scored, skipped


def filter_qualifying(
    scored: list[ScoredIssue], threshold: int = _DEFAULT_SCORE_THRESHOLD
) -> tuple[list[ScoredIssue], int]:
    """Partition into (qualifying, below_threshold_count)."""
    qualifying = [s for s in scored if s.score >= threshold]
    below = len(scored) - len(qualifying)
    return qualifying, below


# ---------------------------------------------------------------------------
# WOS promotion — side effects isolated here
# ---------------------------------------------------------------------------

def _build_db_path() -> Path:
    """Resolve the WOS database path."""
    return Path.home() / "lobster-workspace" / "data" / "wos.db"


def promote_to_wos(
    qualifying: list[ScoredIssue],
    dry_run: bool = False,
) -> tuple[list[PromotionResult], int]:
    """
    Promote qualifying issues into the WOS registry.

    Returns (promotion_results, already_registered_count).
    The Registry.upsert method handles dedup — if an issue is already
    a non-terminal UoW, it returns UpsertSkipped.
    """
    # Import here so the module can be imported without WOS available
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from src.orchestration.registry import Registry, UpsertInserted, UpsertSkipped

    db_path = _build_db_path()
    registry = Registry(db_path)

    results: list[PromotionResult] = []
    already_registered = 0

    for scored in qualifying:
        issue = scored.issue

        if dry_run:
            print(
                f"  [dry-run] would promote #{issue.number}: {issue.title[:60]} "
                f"(score={scored.score}, reasons={list(scored.reasons)})"
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
        f"  Skipped (wos):     {summary.skipped_wos_tracked}",
        f"  Below threshold:   {summary.below_threshold}",
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
    limit: int = 200,
    threshold: int = _DEFAULT_SCORE_THRESHOLD,
    dry_run: bool = False,
) -> SweepSummary:
    """
    Main sweep function. Composes the pure pipeline stages:
    fetch → score → filter → promote.
    Returns a SweepSummary value object.
    """
    issues = fetch_open_issues(repo=repo, limit=limit)
    scored, skipped_wos = score_issues(issues)
    qualifying, below_threshold = filter_qualifying(scored, threshold=threshold)

    if dry_run:
        print(f"Fetched {len(issues)} open issues, {len(qualifying)} qualify (score>={threshold}):")

    promotion_results, already_registered = promote_to_wos(qualifying, dry_run=dry_run)

    return SweepSummary(
        scanned=len(issues),
        skipped_wos_tracked=skipped_wos,
        below_threshold=below_threshold,
        promoted=len(promotion_results),
        already_registered=already_registered,
        promotion_results=tuple(promotion_results),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Issue Cultivator for WOS")
    parser.add_argument("--repo", default="dcetlin/Lobster", help="GitHub repo (owner/name)")
    parser.add_argument("--limit", type=int, default=200, help="Max issues to fetch")
    parser.add_argument("--threshold", type=int, default=2, help="Minimum score to promote")
    parser.add_argument("--dry-run", action="store_true", help="Score but do not write to WOS")
    args = parser.parse_args()

    summary = run_sweep(
        repo=args.repo,
        limit=args.limit,
        threshold=args.threshold,
        dry_run=args.dry_run,
    )
    print(_render_summary(summary, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
