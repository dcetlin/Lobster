"""
Tests for WOS registry bugs #857 and #858.

Bug #857: Registry.__init__ default DB path — Registry() with no args must resolve
to the canonical REGISTRY_DB path from paths.py, not a wrong or missing location.

Bug #858: Startup sweep must recover 'executing' UoWs — UoWs stuck in 'executing'
status (subagent dispatched but write_result never received) must be transitioned
to 'ready-for-steward' by the startup sweep's new Population 4 loop.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _stub_ooda_if_missing() -> None:
    """
    Insert a minimal stub for src.ooda into sys.modules if the real module is absent.

    startup_sweep.py imports from src.orchestration.steward, which imports
    src.ooda at module level. src.ooda is not present in the CI/test environment
    (it is a vendor package that is not committed to the repo). Without this stub,
    any test that transitively loads steward.py fails with ModuleNotFoundError.

    The stub only needs to satisfy the attribute imports in steward.py:
      from src.ooda.fast_thorough_selector import select_path, cite_basis
    """
    if "src.ooda" in sys.modules:
        return

    import types

    # Create stub package src.ooda
    ooda_pkg = types.ModuleType("src.ooda")
    sys.modules["src.ooda"] = ooda_pkg

    # Create stub sub-module src.ooda.fast_thorough_selector
    selector_mod = types.ModuleType("src.ooda.fast_thorough_selector")
    selector_mod.select_path = lambda context: "fast"   # type: ignore[attr-defined]
    selector_mod.cite_basis = lambda context: ""         # type: ignore[attr-defined]
    sys.modules["src.ooda.fast_thorough_selector"] = selector_mod
    ooda_pkg.fast_thorough_selector = selector_mod       # type: ignore[attr-defined]


def _load_startup_sweep():
    """Load startup_sweep.py via importlib."""
    _stub_ooda_if_missing()
    sweep_path = _REPO_ROOT / "scheduled-tasks" / "startup_sweep.py"
    spec = importlib.util.spec_from_file_location("startup_sweep", sweep_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["startup_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod

from orchestration.migrate import run_migrations
from orchestration.registry import Registry, UpsertInserted, ApproveConfirmed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_registry(tmp_path):
    """A fresh Registry instance backed by a temp DB with migrations applied."""
    db_path = tmp_path / "test_registry.db"
    registry = Registry(db_path=db_path)
    return registry


def _seed_uow(registry: Registry, db_path: Path) -> str:
    """Insert a proposed UoW and approve it to ready-for-steward. Returns uow_id."""
    result = registry.upsert(
        issue_number=9001,
        title="Test UoW for orphan recovery",
        success_criteria="The test passes.",
    )
    assert isinstance(result, UpsertInserted)
    uow_id = result.id

    approve_result = registry.approve(uow_id)
    assert isinstance(approve_result, ApproveConfirmed)
    return uow_id


# ===========================================================================
# Bug #857: Registry default path
# ===========================================================================

class TestRegistryDefaultPath:
    """Registry() with no db_path must use the canonical path from paths.py."""

    def test_registry_accepts_no_args_without_raising(self, tmp_path, monkeypatch):
        """
        Registry() with no arguments must not raise TypeError.
        Previously __init__ required an explicit db_path with no default.
        """
        # Point REGISTRY_DB to a tmp path so we don't touch the live DB.
        db_path = tmp_path / "default_path_test.db"
        monkeypatch.setenv("REGISTRY_DB_PATH", str(db_path))

        # Force paths module to re-read env (it's a module-level constant,
        # so we reload via explicit construction — the env override is honored
        # because the local import in __init__ reads the env at call time).
        registry = Registry()  # must not raise TypeError
        assert registry.db_path == db_path

    def test_registry_explicit_path_still_works(self, tmp_path):
        """Explicit db_path must override the default — backward compatibility."""
        explicit_path = tmp_path / "explicit.db"
        registry = Registry(db_path=explicit_path)
        assert registry.db_path == explicit_path

    def test_registry_explicit_path_none_uses_env(self, tmp_path, monkeypatch):
        """Passing db_path=None explicitly must fall through to the canonical path."""
        db_path = tmp_path / "none_test.db"
        monkeypatch.setenv("REGISTRY_DB_PATH", str(db_path))
        registry = Registry(db_path=None)
        assert registry.db_path == db_path


# ===========================================================================
# Bug #858: Startup sweep must recover 'executing' UoWs
# ===========================================================================

class TestRecordStartupSweepExecuting:
    """record_startup_sweep_executing transitions executing → ready-for-steward."""

    def test_transitions_executing_to_ready_for_steward(self, tmp_registry, tmp_path):
        """
        A UoW in 'executing' status must be transitioned to 'ready-for-steward'
        by record_startup_sweep_executing. Returns 1 on success.
        """
        uow_id = _seed_uow(tmp_registry, tmp_registry.db_path)

        # Manually set status to 'executing' (as the Executor does after inbox dispatch)
        tmp_registry.set_status_direct(uow_id, "executing")
        uow = tmp_registry.get(uow_id)
        assert uow.status.value == "executing"

        rows = tmp_registry.record_startup_sweep_executing(
            uow_id=uow_id,
            started_at=uow.started_at,
            age_seconds=700.0,
            threshold_seconds=600,
        )
        assert rows == 1

        recovered = tmp_registry.get(uow_id)
        assert recovered.status.value == "ready-for-steward"

    def test_writes_audit_entry_with_executing_orphan_classification(self, tmp_registry, tmp_path):
        """
        The audit log must record a startup_sweep event with
        classification='executing_orphan' so the steward can identify the
        recovery path.
        """
        import sqlite3
        uow_id = _seed_uow(tmp_registry, tmp_registry.db_path)
        tmp_registry.set_status_direct(uow_id, "executing")

        tmp_registry.record_startup_sweep_executing(
            uow_id=uow_id,
            started_at=None,
            age_seconds=800.0,
            threshold_seconds=600,
        )

        conn = sqlite3.connect(str(tmp_registry.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE uow_id = ? AND event = 'startup_sweep'",
                (uow_id,),
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        note = json.loads(rows[0]["note"])
        assert note["classification"] == "executing_orphan"
        assert note["prior_status"] == "executing"
        assert note["actor"] == "steward"

    def test_idempotent_when_already_advanced(self, tmp_registry):
        """
        If another process has already transitioned the UoW away from 'executing',
        record_startup_sweep_executing must return 0 (no-op, no double audit entry).
        """
        uow_id = _seed_uow(tmp_registry, tmp_registry.db_path)
        tmp_registry.set_status_direct(uow_id, "executing")

        # First sweep: transitions to ready-for-steward
        rows1 = tmp_registry.record_startup_sweep_executing(
            uow_id=uow_id,
            started_at=None,
            age_seconds=700.0,
            threshold_seconds=600,
        )
        assert rows1 == 1

        # Second sweep: UoW is no longer in 'executing' — must be a no-op
        rows2 = tmp_registry.record_startup_sweep_executing(
            uow_id=uow_id,
            started_at=None,
            age_seconds=700.0,
            threshold_seconds=600,
        )
        assert rows2 == 0


class TestStartupSweepExecutingPopulation:
    """
    run_startup_sweep must recover 'executing' UoWs older than the threshold.
    """

    def test_executing_orphan_recovered_when_above_threshold(self, tmp_registry, tmp_path):
        """
        An 'executing' UoW with started_at older than executing_orphan_threshold_seconds
        must be transitioned to 'ready-for-steward' by run_startup_sweep.
        """
        mod = _load_startup_sweep()
        run_startup_sweep = mod.run_startup_sweep
        StartupSweepResult = mod.StartupSweepResult

        uow_id = _seed_uow(tmp_registry, tmp_registry.db_path)
        tmp_registry.set_status_direct(uow_id, "executing")

        # Backdate started_at to simulate an orphan older than the threshold
        import sqlite3
        stale_started_at = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        conn = sqlite3.connect(str(tmp_registry.db_path))
        try:
            conn.execute(
                "UPDATE uow_registry SET started_at = ?, updated_at = ? WHERE id = ?",
                (stale_started_at, stale_started_at, uow_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Use a short threshold (600s) so our 700s-old UoW qualifies
        result = run_startup_sweep(
            tmp_registry,
            dry_run=False,
            executing_orphan_threshold_seconds=600,
            bootup_candidate_gate=False,
            github_client=lambda n: type("IssueInfo", (), {"labels": []})(),
        )

        assert isinstance(result, StartupSweepResult)
        assert result.executing_swept == 1

        recovered = tmp_registry.get(uow_id)
        assert recovered.status.value == "ready-for-steward"

    def test_executing_uow_below_threshold_not_recovered(self, tmp_registry, tmp_path):
        """
        An 'executing' UoW with started_at within the threshold must NOT be
        transitioned — it may still be actively executing.
        """
        mod = _load_startup_sweep()
        run_startup_sweep = mod.run_startup_sweep

        uow_id = _seed_uow(tmp_registry, tmp_registry.db_path)
        tmp_registry.set_status_direct(uow_id, "executing")

        # started_at = 5 seconds ago — well within the 600s threshold
        import sqlite3
        recent_started_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        conn = sqlite3.connect(str(tmp_registry.db_path))
        try:
            conn.execute(
                "UPDATE uow_registry SET started_at = ?, updated_at = ? WHERE id = ?",
                (recent_started_at, recent_started_at, uow_id),
            )
            conn.commit()
        finally:
            conn.close()

        result = run_startup_sweep(
            tmp_registry,
            dry_run=False,
            executing_orphan_threshold_seconds=600,
            bootup_candidate_gate=False,
            github_client=lambda n: type("IssueInfo", (), {"labels": []})(),
        )

        # Must NOT have been swept
        assert result.executing_swept == 0
        still_executing = tmp_registry.get(uow_id)
        assert still_executing.status.value == "executing"

    def test_dry_run_does_not_transition(self, tmp_registry, tmp_path):
        """
        In dry_run mode, the executing UoW must not be transitioned — only
        the skipped_dry_run counter must be incremented.
        """
        mod = _load_startup_sweep()
        run_startup_sweep = mod.run_startup_sweep

        uow_id = _seed_uow(tmp_registry, tmp_registry.db_path)
        tmp_registry.set_status_direct(uow_id, "executing")

        import sqlite3
        stale_started_at = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        conn = sqlite3.connect(str(tmp_registry.db_path))
        try:
            conn.execute(
                "UPDATE uow_registry SET started_at = ?, updated_at = ? WHERE id = ?",
                (stale_started_at, stale_started_at, uow_id),
            )
            conn.commit()
        finally:
            conn.close()

        result = run_startup_sweep(
            tmp_registry,
            dry_run=True,
            executing_orphan_threshold_seconds=600,
            bootup_candidate_gate=False,
            github_client=lambda n: type("IssueInfo", (), {"labels": []})(),
        )

        assert result.executing_swept == 0
        assert result.skipped_dry_run >= 1  # at least our UoW was counted

        # Status must be unchanged
        still_executing = tmp_registry.get(uow_id)
        assert still_executing.status.value == "executing"
