# Work Orchestration System
# Registry, CLI, and dispatcher handler integration

from .issue_source import IssueSnapshot, IssueSource, SourceRef, source_ref_from_str, source_ref_to_str
from .github_issue_source import GitHubIssueSource

__all__ = [
    "IssueSnapshot",
    "IssueSource",
    "SourceRef",
    "source_ref_from_str",
    "source_ref_to_str",
    "GitHubIssueSource",
]
