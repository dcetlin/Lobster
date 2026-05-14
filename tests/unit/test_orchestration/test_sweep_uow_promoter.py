"""
Unit tests for sweep_uow_promoter.py

Behaviors under test:
- promote_sweep_issue creates a proposed UoW when none exists
- promote_sweep_issue deduplicates: skips if a non-terminal UoW already exists
  for the same issue number (regardless of github_issue_url match)
- promote_sweep_issue sets source="sweep" on the created UoW
- promote_sweep_issue sets issue_url from the provided github_issue_url
- promote_sweep_issue uses priority="low" (sweep findings are background work)
- promote_sweep_issue returns PromoteResult.CREATED on new UoW
- promote_sweep_issue returns PromoteResult.SKIPPED_DEDUP when duplicate exists
- promote_sweep_issue re-proposes after a terminal UoW (done/failed/expired)
- job-enabled gate: returns PromoteResult.SKIPPED_JOB_DISABLED when disabled
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.orchestration.sweep_uow_promoter import SWEEP_SOURCE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path):
    from src.orchestration.registry import Registry
    return Registry(db_path)


@pytest.fixture
def jobs_json_enabled(tmp_path: Path) -> Path:
    """Write a jobs.json that has negentropic-sweep enabled."""
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps({
        "jobs": {
            "negentropic-sweep": {
                "enabled": True,
            }
        }
    }))
    return jobs_file


@pytest.fixture
def jobs_json_disabled(tmp_path: Path) -> Path:
    """Write a jobs.json that has negentropic-sweep disabled."""
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps({
        "jobs": {
            "negentropic-sweep": {
                "enabled": False,
            }
        }
    }))
    return jobs_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _do_promote(registry, jobs_json_path, issue_number=42, title="Fix entropy smell", issue_url="https://github.com/dcetlin/Lobster/issues/42"):
    from src.orchestration.sweep_uow_promoter import promote_sweep_issue
    return promote_sweep_issue(
        issue_number=issue_number,
        title=title,
        issue_url=issue_url,
        registry=registry,
        jobs_json_path=jobs_json_path,
    )


# ---------------------------------------------------------------------------
# Job-enabled gate
# ---------------------------------------------------------------------------

class TestJobEnabledGate:
    def test_returns_skipped_when_job_disabled(self, registry, jobs_json_disabled):
        from src.orchestration.sweep_uow_promoter import PromoteResult
        result = _do_promote(registry, jobs_json_disabled)
        assert result == PromoteResult.SKIPPED_JOB_DISABLED

    def test_proceeds_when_job_enabled(self, registry, jobs_json_enabled):
        from src.orchestration.sweep_uow_promoter import PromoteResult
        result = _do_promote(registry, jobs_json_enabled)
        assert result == PromoteResult.CREATED

    def test_proceeds_when_jobs_json_absent(self, registry, tmp_path):
        """Missing jobs.json defaults to enabled (matches _is_job_enabled behaviour)."""
        from src.orchestration.sweep_uow_promoter import PromoteResult
        nonexistent = tmp_path / "no-jobs.json"
        result = _do_promote(registry, nonexistent)
        assert result == PromoteResult.CREATED


# ---------------------------------------------------------------------------
# Creation and dedup
# ---------------------------------------------------------------------------

class TestCreation:
    def test_creates_proposed_uow_on_first_call(self, registry, jobs_json_enabled):
        from src.orchestration.sweep_uow_promoter import PromoteResult
        result = _do_promote(registry, jobs_json_enabled, issue_number=100, title="Smell detected")
        assert result == PromoteResult.CREATED

    def test_uow_has_source_sweep(self, registry, jobs_json_enabled, db_path):
        """Promoted UoW must have source='sweep' so it is distinguishable from cultivator UoWs."""
        import sqlite3
        _do_promote(registry, jobs_json_enabled, issue_number=101, title="Smell X")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source, status FROM uow_registry WHERE source_issue_number = 101"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["source"] == SWEEP_SOURCE
        assert row["status"] == "proposed"

    def test_uow_has_correct_issue_url(self, registry, jobs_json_enabled, db_path):
        """The issue_url column must carry the canonical GitHub URL supplied by the caller."""
        import sqlite3
        url = "https://github.com/dcetlin/Lobster/issues/202"
        _do_promote(registry, jobs_json_enabled, issue_number=202, title="URL smell", issue_url=url)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT issue_url FROM uow_registry WHERE source_issue_number = 202"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["issue_url"] == url

    def test_skipped_dedup_when_non_terminal_uow_exists(self, registry, jobs_json_enabled):
        """Second call for the same issue number returns SKIPPED_DEDUP — no duplicate UoW created."""
        from src.orchestration.sweep_uow_promoter import PromoteResult
        first = _do_promote(registry, jobs_json_enabled, issue_number=300, title="Smell Y")
        second = _do_promote(registry, jobs_json_enabled, issue_number=300, title="Smell Y (retry)")
        assert first == PromoteResult.CREATED
        assert second == PromoteResult.SKIPPED_DEDUP

    def test_reproposed_after_terminal_uow_on_new_sweep_date(self, registry, jobs_json_enabled, db_path):
        """After a UoW reaches a terminal state (done), a new proposed UoW can be created
        on a future sweep date.  Same-date re-proposal is intentionally blocked by the
        registry (one UoW per issue per sweep_date is the DB constraint)."""
        import sqlite3
        from datetime import date, timedelta
        from src.orchestration.sweep_uow_promoter import PromoteResult, promote_sweep_issue

        # Create via first day's sweep_date
        first_sweep_date = (date.today() - timedelta(days=1)).isoformat()

        first_result = registry.upsert(
            issue_number=400,
            title="Smell Z",
            sweep_date=first_sweep_date,
            success_criteria="Smell Z remediated.",
            issue_url="https://github.com/dcetlin/Lobster/issues/400",
            source_ref=SWEEP_SOURCE,
        )
        # Mark terminal
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET status = 'done' WHERE source_issue_number = 400"
        )
        conn.commit()
        conn.close()

        # Re-promote on today (a new sweep_date) — should succeed
        result = _do_promote(registry, jobs_json_enabled, issue_number=400, title="Smell Z revisit")
        assert result == PromoteResult.CREATED

    def test_uow_success_criteria_is_non_empty(self, registry, jobs_json_enabled, db_path):
        """registry.upsert() raises ValueError on empty success_criteria; promoter must supply one."""
        import sqlite3
        _do_promote(registry, jobs_json_enabled, issue_number=500, title="Sweep smell")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT success_criteria FROM uow_registry WHERE source_issue_number = 500"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["success_criteria"].strip() != ""
