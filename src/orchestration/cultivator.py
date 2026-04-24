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


# Section headings that indicate acceptance/success criteria in issue bodies.
# Checked in order; first match wins.
_SUCCESS_CRITERIA_HEADINGS = (
    "## Acceptance Criteria",
    "## acceptance criteria",
    "## Success Criteria",
    "## success criteria",
    "## Definition of Done",
    "## definition of done",
)


def _extract_success_criteria(body: str) -> str:
    """
    Extract success criteria from a GitHub issue body.

    Looks for a section headed by one of _SUCCESS_CRITERIA_HEADINGS and returns
    its content (everything up to the next ## heading or end of body). Falls back
    to the first paragraph of the body if no criteria section is found.

    Pure function — no I/O, no mutation.
    """
    if not body:
        return ""

    for heading in _SUCCESS_CRITERIA_HEADINGS:
        idx = body.find(heading)
        if idx == -1:
            continue
        # Advance past the heading line
        section_start = body.find("\n", idx)
        if section_start == -1:
            continue
        section_start += 1
        # Find the next ## heading (or end of string)
        next_heading = body.find("\n##", section_start)
        if next_heading == -1:
            section = body[section_start:]
        else:
            section = body[section_start:next_heading]
        criteria = section.strip()
        if criteria:
            return criteria

    # Fallback: use the first non-empty paragraph of the body.
    # This gives the executor something concrete even on issues with no
    # formal criteria section.
    for paragraph in body.split("\n\n"):
        paragraph = paragraph.strip()
        if paragraph and not paragraph.startswith("#"):
            # Truncate long paragraphs to keep the field readable.
            return paragraph[:500] if len(paragraph) > 500 else paragraph

    return ""


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

def promote_to_wos(
    candidates: list[ClassifiedIssue],
    dry_run: bool = False,
) -> tuple[list[PromotionResult], int]:
    """
    Promote all candidate issues into the WOS registry.

    Returns (promotion_results, already_registered_count).
    Registry.upsert handles idempotency — issues already in WOS as non-terminal
    UoWs return UpsertSkipped.

    At germination, the Germinator classifies register for each issue. Register is
    written to the UoW at INSERT time and is immutable thereafter. The Steward
    surfaces any register mismatch to Dan on diagnosis — it does not reclassify.

    Naming note: this function is part of the *GitHub Issue Cultivator* scheduled
    job. The Germinator (germinator.py) is a separate component called here at
    germination time. See docs/WOS-INDEX.md for the component glossary.

    Note: source is written by Registry as "github:issue/{number}" which traces
    back to dcetlin/Lobster since that is the only repo cultivator operates on.
    """
    # Import here so the module can be imported without WOS available
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from src.orchestration.registry import Registry, UpsertInserted, UpsertSkipped
    from src.orchestration.germinator import classify_register
    from src.orchestration.wos_throttle import ConsumptionRateMonitor, PrescriptionThrottleGate

    from src.orchestration.paths import REGISTRY_DB as _REGISTRY_DB
    registry = Registry(_REGISTRY_DB)

    # Throttle gate: suppress UoW writes when the backlog is critical.
    # Fail-open: if the monitor cannot read the registry, suppression does not fire.
    _monitor = ConsumptionRateMonitor()
    _gate = PrescriptionThrottleGate(_monitor)
    if _gate.should_suppress_prescription():
        _status = _gate.gate_status()
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "[THROTTLED] Prescription suppressed — backlog critical: %s", _status["reason"]
        )
        return [], 0

    results: list[PromotionResult] = []
    already_registered = 0

    for classified in candidates:
        issue = classified.issue
        success_criteria = _extract_success_criteria(issue.body)

        # Germinator: classify register at germination time.
        # Register is immutable after this point.
        reg_classification = classify_register(
            title=issue.title,
            body=issue.body,
            success_criteria=success_criteria,
        )
        register = reg_classification.register

        if dry_run:
            print(
                f"  [dry-run] would promote #{issue.number}: {issue.title[:60]} "
                f"(priority={classified.priority}, register={register}, "
                f"gate={reg_classification.gate_matched}, "
                f"confidence={reg_classification.confidence})"
            )
            continue

        result = registry.upsert(
            issue_number=issue.number,
            title=issue.title,
            success_criteria=success_criteria,
            register=register,
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
