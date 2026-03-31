"""IssueSource Protocol — substrate-agnostic abstraction for issue tracking sources.

Defines the normalized value objects and Protocol that isolate all source-system
knowledge from GardenCaretaker. Any class implementing scan() and get_issue()
satisfies the Protocol (structural subtyping — no base class required).
"""
from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass(frozen=True)
class IssueSnapshot:
    """Value object — a point-in-time view of an issue from any source.

    Carries no GitHub-specific fields. The source_ref string encodes substrate
    identity (e.g. "github:issue/42") without leaking substrate knowledge into
    GardenCaretaker.
    """

    source_ref: str           # canonical string: "github:issue/42"
    title: str
    state: str                # "open" | "closed" | "deleted"
    labels: tuple[str, ...]
    body: str
    created_at: str           # ISO 8601
    updated_at: str           # ISO 8601
    url: str


SourceRef = namedtuple("SourceRef", ["substrate", "entity_type", "entity_id"])
"""Structured representation of a source identifier.

Example:
    SourceRef(substrate='github', entity_type='issue', entity_id='42')
"""


def source_ref_to_str(ref: SourceRef) -> str:
    """Serialize a SourceRef to its canonical string form.

    Example:
        source_ref_to_str(SourceRef('github', 'issue', '42')) == 'github:issue/42'
    """
    return f"{ref.substrate}:{ref.entity_type}/{ref.entity_id}"


def source_ref_from_str(s: str) -> SourceRef:
    """Parse a canonical source_ref string into a SourceRef namedtuple.

    Example:
        source_ref_from_str('github:issue/42')
        # → SourceRef(substrate='github', entity_type='issue', entity_id='42')

    Raises:
        ValueError: if the string does not conform to '<substrate>:<type>/<id>'
    """
    substrate, rest = s.split(":", 1)
    entity_type, entity_id = rest.split("/", 1)
    return SourceRef(substrate=substrate, entity_type=entity_type, entity_id=entity_id)


class IssueSource(Protocol):
    """Structural subtyping — any class implementing scan() and get_issue() qualifies.

    GardenCaretaker depends only on this Protocol. Concrete implementations
    (GitHubIssueSource, or any future in-memory stub) are injected at construction
    time and never imported by the caretaker directly.
    """

    def scan(self) -> Iterator[IssueSnapshot]:
        """Yield all currently open issues from this source."""
        ...

    def get_issue(self, source_ref: str) -> IssueSnapshot | None:
        """Fetch a single issue by source_ref.

        Returns None if the issue is not found (deleted, transferred, or the
        source_ref does not exist in this source). Never raises for missing issues.
        """
        ...
