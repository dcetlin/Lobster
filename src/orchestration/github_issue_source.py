"""Concrete IssueSource implementation for GitHub repos via gh CLI.

This is the only module in the codebase that may call subprocess to invoke gh.
GardenCaretaker receives GitHubIssueSource instances at construction time and
never calls gh directly.
"""
from __future__ import annotations

import json
import subprocess
from typing import Iterator

from .issue_source import IssueSnapshot, SourceRef, source_ref_from_str, source_ref_to_str


class GitHubIssueSource:
    """IssueSource for a single GitHub repo.

    Wraps all gh CLI calls and JSON parsing. Satisfies IssueSource (structural
    subtyping — no explicit base class needed).
    """

    def __init__(self, repo: str) -> None:
        self.repo = repo          # e.g. "dcetlin/Lobster"
        self.substrate = "github"

    def scan(self) -> Iterator[IssueSnapshot]:
        """Yield all open issues via gh CLI.

        Uses --limit 200 as a practical upper bound; increase if repos grow
        beyond that. Yields IssueSnapshot value objects — no gh types escape
        this module.
        """
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", self.repo,
                "--state", "open",
                "--json", "number,title,state,labels,body,createdAt,updatedAt,url",
                "--limit", "200",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        issues = json.loads(result.stdout)
        for issue in issues:
            yield self._to_snapshot(issue)

    def get_issue(self, source_ref: str) -> IssueSnapshot | None:
        """Fetch a single issue by source_ref.

        Returns None if the issue is not found (deleted, transferred, or the
        issue number does not exist in this repo). CalledProcessError from gh
        (non-zero exit) is mapped to None — it is not re-raised.
        """
        ref = source_ref_from_str(source_ref)
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "view", ref.entity_id,
                    "--repo", self.repo,
                    "--json", "number,title,state,labels,body,createdAt,updatedAt,url",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            issue = json.loads(result.stdout)
            return self._to_snapshot(issue)
        except subprocess.CalledProcessError:
            return None  # issue not found / deleted / transferred

    def _to_snapshot(self, issue: dict) -> IssueSnapshot:
        """Map a gh JSON issue dict to an IssueSnapshot value object.

        This is a pure function — its only input is the dict and self.substrate.
        All gh field names are confined to this method.
        """
        ref = SourceRef(
            substrate=self.substrate,
            entity_type="issue",
            entity_id=str(issue["number"]),
        )
        return IssueSnapshot(
            source_ref=source_ref_to_str(ref),
            title=issue["title"],
            state=issue["state"].lower(),
            labels=tuple(lbl["name"] for lbl in issue.get("labels", [])),
            body=issue.get("body") or "",
            created_at=issue["createdAt"],
            updated_at=issue["updatedAt"],
            url=issue["url"],
        )
