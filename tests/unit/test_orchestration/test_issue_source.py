"""Unit tests for IssueSource Protocol, IssueSnapshot, SourceRef, and GitHubIssueSource.

All tests are pure — no subprocess calls, no network access. GitHubIssueSource
is tested by exercising _to_snapshot() directly with fixture dicts, keeping the
tests fast and substrate-independent.
"""
from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from src.orchestration.github_issue_source import GitHubIssueSource
from src.orchestration.issue_source import (
    IssueSnapshot,
    SourceRef,
    source_ref_from_str,
    source_ref_to_str,
)


# ---------------------------------------------------------------------------
# SourceRef round-trip
# ---------------------------------------------------------------------------

class TestSourceRefRoundTrip:
    def test_to_str_then_from_str_round_trips(self):
        ref = SourceRef(substrate="github", entity_type="issue", entity_id="42")
        assert source_ref_from_str(source_ref_to_str(ref)) == ref

    def test_to_str_produces_canonical_format(self):
        ref = SourceRef(substrate="github", entity_type="issue", entity_id="42")
        assert source_ref_to_str(ref) == "github:issue/42"

    def test_from_str_parses_canonical_format(self):
        ref = source_ref_from_str("github:issue/42")
        assert ref == SourceRef(substrate="github", entity_type="issue", entity_id="42")

    def test_from_str_preserves_substrate(self):
        ref = source_ref_from_str("github:issue/42")
        assert ref.substrate == "github"

    def test_from_str_preserves_entity_type(self):
        ref = source_ref_from_str("github:issue/42")
        assert ref.entity_type == "issue"

    def test_from_str_preserves_entity_id(self):
        ref = source_ref_from_str("github:issue/42")
        assert ref.entity_id == "42"

    def test_large_issue_number_round_trips(self):
        ref = SourceRef(substrate="github", entity_type="issue", entity_id="9999")
        assert source_ref_from_str(source_ref_to_str(ref)) == ref

    def test_entity_id_with_path_separator_preserved(self):
        """entity_id containing '/' should not be split further."""
        ref = source_ref_from_str("github:pr/10/comments")
        assert ref.entity_id == "10/comments"


# ---------------------------------------------------------------------------
# IssueSnapshot immutability
# ---------------------------------------------------------------------------

class TestIssueSnapshotImmutability:
    def _make_snapshot(self, **overrides) -> IssueSnapshot:
        defaults = dict(
            source_ref="github:issue/1",
            title="Test issue",
            state="open",
            labels=("bug",),
            body="body text",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
            url="https://github.com/owner/repo/issues/1",
        )
        defaults.update(overrides)
        return IssueSnapshot(**defaults)

    def test_snapshot_is_frozen(self):
        snapshot = self._make_snapshot()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            snapshot.title = "modified"  # type: ignore[misc]

    def test_snapshot_state_is_not_mutable(self):
        snapshot = self._make_snapshot()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            snapshot.state = "closed"  # type: ignore[misc]

    def test_labels_is_tuple(self):
        snapshot = self._make_snapshot(labels=("bug", "enhancement"))
        assert isinstance(snapshot.labels, tuple)

    def test_two_equal_snapshots_are_equal(self):
        a = self._make_snapshot()
        b = self._make_snapshot()
        assert a == b

    def test_snapshot_with_different_title_not_equal(self):
        a = self._make_snapshot(title="A")
        b = self._make_snapshot(title="B")
        assert a != b


# ---------------------------------------------------------------------------
# GitHubIssueSource._to_snapshot — fixture-driven, no subprocess
# ---------------------------------------------------------------------------

GH_ISSUE_FIXTURE = {
    "number": 42,
    "title": "Fix the thing",
    "state": "OPEN",
    "labels": [{"name": "bug"}, {"name": "good first issue"}],
    "body": "Here is the body.",
    "createdAt": "2026-03-01T10:00:00Z",
    "updatedAt": "2026-03-15T12:30:00Z",
    "url": "https://github.com/dcetlin/Lobster/issues/42",
}


class TestGitHubIssueSourceToSnapshot:
    def setup_method(self):
        self.source = GitHubIssueSource(repo="dcetlin/Lobster")

    def test_source_ref_is_canonical_string(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert snap.source_ref == "github:issue/42"

    def test_title_mapped_correctly(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert snap.title == "Fix the thing"

    def test_state_is_lowercased(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert snap.state == "open"

    def test_labels_extracted_from_name_field(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert snap.labels == ("bug", "good first issue")

    def test_body_preserved(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert snap.body == "Here is the body."

    def test_created_at_preserved(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert snap.created_at == "2026-03-01T10:00:00Z"

    def test_updated_at_preserved(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert snap.updated_at == "2026-03-15T12:30:00Z"

    def test_url_preserved(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert snap.url == "https://github.com/dcetlin/Lobster/issues/42"

    def test_none_body_coerced_to_empty_string(self):
        fixture = dict(GH_ISSUE_FIXTURE, body=None)
        snap = self.source._to_snapshot(fixture)
        assert snap.body == ""

    def test_missing_labels_defaults_to_empty_tuple(self):
        fixture = {k: v for k, v in GH_ISSUE_FIXTURE.items() if k != "labels"}
        snap = self.source._to_snapshot(fixture)
        assert snap.labels == ()

    def test_returns_issue_snapshot_type(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        assert isinstance(snap, IssueSnapshot)

    def test_result_is_immutable(self):
        snap = self.source._to_snapshot(GH_ISSUE_FIXTURE)
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            snap.title = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# GitHubIssueSource.scan — mock subprocess
# ---------------------------------------------------------------------------

class TestGitHubIssueSourceScan:
    def setup_method(self):
        self.source = GitHubIssueSource(repo="dcetlin/Lobster")

    def test_scan_yields_snapshots(self):
        import json

        mock_result = MagicMock()
        mock_result.stdout = json.dumps([GH_ISSUE_FIXTURE])

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            snapshots = list(self.source.scan())

        assert len(snapshots) == 1
        assert snapshots[0].source_ref == "github:issue/42"

    def test_scan_passes_correct_repo(self):
        import json

        mock_result = MagicMock()
        mock_result.stdout = json.dumps([])

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            list(self.source.scan())

        call_args = mock_run.call_args[0][0]
        assert "--repo" in call_args
        repo_idx = call_args.index("--repo")
        assert call_args[repo_idx + 1] == "dcetlin/Lobster"

    def test_scan_requests_open_state(self):
        import json

        mock_result = MagicMock()
        mock_result.stdout = json.dumps([])

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            list(self.source.scan())

        call_args = mock_run.call_args[0][0]
        assert "--state" in call_args
        state_idx = call_args.index("--state")
        assert call_args[state_idx + 1] == "open"

    def test_scan_empty_repo_yields_nothing(self):
        import json

        mock_result = MagicMock()
        mock_result.stdout = json.dumps([])

        with patch("subprocess.run", return_value=mock_result):
            snapshots = list(self.source.scan())

        assert snapshots == []


# ---------------------------------------------------------------------------
# GitHubIssueSource.get_issue — mock subprocess
# ---------------------------------------------------------------------------

class TestGitHubIssueSourceGetIssue:
    def setup_method(self):
        self.source = GitHubIssueSource(repo="dcetlin/Lobster")

    def test_get_issue_returns_snapshot_on_success(self):
        import json
        import subprocess

        mock_result = MagicMock()
        mock_result.stdout = json.dumps(GH_ISSUE_FIXTURE)

        with patch("subprocess.run", return_value=mock_result):
            snap = self.source.get_issue("github:issue/42")

        assert snap is not None
        assert snap.source_ref == "github:issue/42"

    def test_get_issue_returns_none_on_not_found(self):
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
            snap = self.source.get_issue("github:issue/999")

        assert snap is None

    def test_get_issue_passes_entity_id_as_positional(self):
        import json

        mock_result = MagicMock()
        mock_result.stdout = json.dumps(GH_ISSUE_FIXTURE)

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            self.source.get_issue("github:issue/42")

        call_args = mock_run.call_args[0][0]
        # entity_id "42" should appear after "view"
        view_idx = call_args.index("view")
        assert call_args[view_idx + 1] == "42"
