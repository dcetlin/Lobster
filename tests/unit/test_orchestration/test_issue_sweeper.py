"""
Integration tests for the issue-sweeper.py Registrar sweep loop.

Coverage per acceptance criteria:
- Sweep with one pending UoW whose trigger is immediate → transitions to ready-for-steward,
  audit_log contains trigger_fired
- Sweep with issue_closed trigger where issue is NOT closed → UoW stays pending, no audit entry
- Two consecutive sweep runs on same UoW that advanced on first run — second run does not
  call evaluate_condition for this UoW (it is no longer pending)
- Optimistic lock: transition returns 0 rows → no audit entry written
"""

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SWEEPER_PATH = REPO_ROOT / "scheduled-tasks" / "issue-sweeper.py"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path):
    from src.orchestration.registry import Registry
    return Registry(db_path)


def _audit_entries(db_path: Path, uow_id: str | None = None) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if uow_id:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id",
            (uow_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _run_sweep(registry, github_client=None) -> dict[str, Any]:
    """Import and run the sweep function directly."""
    # Dynamically import the sweeper module
    import importlib.util
    spec = importlib.util.spec_from_file_location("issue_sweeper", SWEEPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    kwargs = {"registry": registry}
    if github_client is not None:
        kwargs["github_client"] = github_client
    return module.run_sweep(**kwargs)


# ---------------------------------------------------------------------------
# Immediate trigger: pending → ready-for-steward
# ---------------------------------------------------------------------------

class TestImmediateTriggerSweep:
    def test_pending_immediate_advances_to_ready_for_steward(self, registry, db_path):
        # Insert and confirm a UoW (proposed → pending)
        result = registry.upsert(issue_number=10, title="test UoW")
        uow_id = result["id"]
        registry.confirm(uow_id)

        # Verify it's pending with immediate trigger
        uow = registry.get(uow_id)
        assert uow["status"] == "pending"

        _run_sweep(registry)

        uow_after = registry.get(uow_id)
        assert uow_after["status"] == "ready-for-steward"

    def test_trigger_fired_audit_entry_written(self, registry, db_path):
        result = registry.upsert(issue_number=11, title="audit test")
        uow_id = result["id"]
        registry.confirm(uow_id)

        _run_sweep(registry)

        entries = _audit_entries(db_path, uow_id)
        trigger_fired = [e for e in entries if e["event"] == "trigger_fired"]
        assert len(trigger_fired) == 1

        note = json.loads(trigger_fired[0]["note"])
        assert note["actor"] == "registrar"
        assert note["uow_id"] == uow_id
        assert "trigger" in note
        assert "timestamp" in note

    def test_proposed_uow_not_swept(self, registry, db_path):
        """Proposed UoWs must not be passed to evaluate_condition."""
        result = registry.upsert(issue_number=12, title="proposed only")
        uow_id = result["id"]
        # Do NOT confirm — stays at "proposed"

        _run_sweep(registry)

        uow_after = registry.get(uow_id)
        assert uow_after["status"] == "proposed"

    def test_ready_for_steward_uow_not_swept(self, registry, db_path):
        """UoWs already at ready-for-steward must not be swept again."""
        result = registry.upsert(issue_number=13, title="already advanced")
        uow_id = result["id"]
        registry.confirm(uow_id)
        registry.set_status_direct(uow_id, "ready-for-steward")

        eval_call_count = {"n": 0}
        original_import = None

        # Patch evaluate_condition to count calls
        import src.orchestration.conditions as cond_module
        original_eval = cond_module.evaluate_condition

        def counting_eval(uow, **kwargs):
            eval_call_count["n"] += 1
            return original_eval(uow, **kwargs)

        cond_module.evaluate_condition = counting_eval
        try:
            _run_sweep(registry)
        finally:
            cond_module.evaluate_condition = original_eval

        assert eval_call_count["n"] == 0


# ---------------------------------------------------------------------------
# issue_closed trigger: issue not closed → stays pending, no audit
# ---------------------------------------------------------------------------

class TestIssueClosedTriggerSweep:
    def test_issue_not_closed_stays_pending_no_audit(self, registry, db_path):
        result = registry.upsert(issue_number=50, title="awaiting issue close")
        uow_id = result["id"]
        registry.confirm(uow_id)

        # Override trigger to issue_closed
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET trigger = ? WHERE id = ?",
            (json.dumps({"type": "issue_closed", "number": 50}), uow_id),
        )
        conn.commit()
        conn.close()

        def mock_github_client(issue_number: int) -> dict:
            return {"status_code": 200, "state": "open"}

        _run_sweep(registry, github_client=mock_github_client)

        uow_after = registry.get(uow_id)
        assert uow_after["status"] == "pending"

        # No trigger_fired audit entry
        entries = _audit_entries(db_path, uow_id)
        trigger_fired = [e for e in entries if e["event"] == "trigger_fired"]
        assert trigger_fired == []


# ---------------------------------------------------------------------------
# Two consecutive sweeps — second sweep skips already-advanced UoW
# ---------------------------------------------------------------------------

class TestConsecutiveSweeps:
    def test_second_sweep_skips_advanced_uow(self, registry, db_path):
        """After first sweep advances a UoW, second sweep must not evaluate it."""
        result = registry.upsert(issue_number=60, title="two-sweep test")
        uow_id = result["id"]
        registry.confirm(uow_id)

        eval_calls = []
        import src.orchestration.conditions as cond_module
        original_eval = cond_module.evaluate_condition

        def recording_eval(uow, **kwargs):
            eval_calls.append(uow["id"])
            return original_eval(uow, **kwargs)

        cond_module.evaluate_condition = recording_eval
        try:
            _run_sweep(registry)  # first sweep — advances to ready-for-steward
            first_count = len(eval_calls)
            assert first_count == 1  # was evaluated once

            _run_sweep(registry)  # second sweep — UoW is now ready-for-steward, not pending
            second_count = len(eval_calls)
            assert second_count == 1  # NOT called again
        finally:
            cond_module.evaluate_condition = original_eval

        uow_after = registry.get(uow_id)
        assert uow_after["status"] == "ready-for-steward"


# ---------------------------------------------------------------------------
# Optimistic lock: transition returns 0 rows → no audit entry
# ---------------------------------------------------------------------------

class TestOptimisticLock:
    def test_zero_rows_transition_writes_no_trigger_fired(self, registry, db_path):
        """
        Simulate a race: another process advances the UoW between evaluate_condition
        and transition. When transition returns 0, no trigger_fired entry should be written.
        """
        result = registry.upsert(issue_number=70, title="race condition test")
        uow_id = result["id"]
        registry.confirm(uow_id)

        # Patch transition to return 0 (simulate another sweep winning the race)
        original_transition = registry.transition

        def zero_transition(tid, to_status, where_status):
            # Advance the UoW externally, then call original which finds 0 rows
            registry.set_status_direct(tid, "ready-for-steward")
            return original_transition(tid, to_status=to_status, where_status=where_status)

        registry.transition = zero_transition
        try:
            _run_sweep(registry)
        finally:
            registry.transition = original_transition

        entries = _audit_entries(db_path, uow_id)
        trigger_fired = [e for e in entries if e["event"] == "trigger_fired"]
        assert trigger_fired == []
