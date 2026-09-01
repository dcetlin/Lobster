"""
PR Classifier — classify open pull requests for automated review routing.

Callable standalone for manual classification.

Usage:
    from src.agents.pr_classifier import classify, PRReviewType

    result = classify(pr_number=1234, repo="dcetlin/Lobster")
    print(result.review_type, result.prescribed_skills, result.rationale)
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger("pr_classifier")


@dataclass(frozen=True)
class PRReviewType:
    review_type: str
    prescribed_skills: list[str]
    rationale: str


def classify(pr_number: int, repo: str = "dcetlin/Lobster") -> PRReviewType:
    """
    Classify a PR for automated review routing.

    Routing order (first match wins):
    1. Labels contain 'documentation' or title starts with 'docs:' → docs_quality
    2. Labels contain 'bug' or title starts with 'fix:' → bug_regression
    3. Files include .claude/skills/ or src/skills/ → skill_compliance
    4. Default → oracle_only

    On failure (GitHub API unavailable, malformed response), defaults to
    oracle_only with an error note in rationale.

    Returns PRReviewType with review_type, prescribed_skills, and rationale.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view", str(pr_number),
                "--repo", repo,
                "--json", "labels,title,files",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        log.warning("pr_classifier: gh pr view failed for %s#%d — %s", repo, pr_number, exc)
        return PRReviewType(
            review_type="oracle_only",
            prescribed_skills=["lobster-oracle"],
            rationale=f"github fetch failed ({type(exc).__name__}) — defaulting to oracle_only",
        )

    labels: list[str] = [lbl.get("name", "") for lbl in data.get("labels", [])]
    title: str = data.get("title", "")
    files: list[str] = [f.get("path", "") for f in data.get("files", [])]

    # Rule 1: docs label or docs: title prefix
    if "documentation" in labels or title.startswith("docs:"):
        return PRReviewType(
            review_type="docs_quality",
            prescribed_skills=["lobster-generalist"],
            rationale="documentation label or docs: title prefix",
        )

    # Rule 2: bug label or fix: title prefix
    if "bug" in labels or title.startswith("fix:"):
        return PRReviewType(
            review_type="bug_regression",
            prescribed_skills=["lobster-oracle"],
            rationale="bug label or fix: title prefix",
        )

    # Rule 3: skill files
    if any(
        f.startswith(".claude/skills/") or f.startswith("src/skills/")
        for f in files
    ):
        return PRReviewType(
            review_type="skill_compliance",
            prescribed_skills=["lobster-generalist"],
            rationale="touches .claude/skills/ or src/skills/",
        )

    # Default
    return PRReviewType(
        review_type="oracle_only",
        prescribed_skills=["lobster-oracle"],
        rationale="default path — no specific routing signals matched",
    )
