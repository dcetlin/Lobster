"""
Integration tests for WOS pipeline wiring.

These tests verify component interconnections that unit tests cannot catch.
Each test runs against an in-memory Registry (SQLite tmp_path) with minimal
UoW fixtures. No network calls, no production DB.

Root cause context
------------------
Bug #2: GardenCaretaker replaced issue-sweeper.py but the evaluate_condition
wiring was silently dropped for pending UoWs. Unit tests for each component
in isolation cannot catch this class of bug — only an end-to-end test that
exercises the full _check_pending_triggers() path can catch it.

Tests
-----
- test_garden_caretaker_promotes_immediate_trigger
    A pending UoW with trigger_type=immediate transitions to ready-for-steward
    when GardenCaretaker.run() is called. This is the primary wiring regression
    guard for Bug #2.

- test_garden_caretaker_holds_issue_closed_trigger
    A pending UoW with trigger_type=issue_closed pointing to an open issue stays
    in pending. GardenCaretaker must not promote it.

- test_evaluate_condition_is_called_for_pending_uow
    A mock injected in place of evaluate_condition verifies the wiring path —
    that GardenCaretaker._check_pending_triggers() actually calls the function
    for each pending UoW.

- test_steward_picks_up_ready_for_steward_uow
    A ready-for-steward UoW is processed by run_steward_cycle(). The steward
    transitions it out of ready-for-steward (to diagnosing or beyond), proving
    the steward→executor handoff entry point is wired.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module path setup — same pattern as existing integration tests
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.registry import Registry, UoWStatus, UpsertInserted
from orchestration.issue_source import IssueSnapshot
from orchestration.garden_caretaker import GardenCaretaker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_days_ago(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Minimal IssueSource stub — satisfies the IssueSource Protocol without MagicMock
# ---------------------------------------------------------------------------

class _EmptyIssueSource:
    """IssueSource that returns no issues from scan() and raises on get_issue().

    Used when the test only exercises _check_pending_triggers() and does not
    need scan/tend to do any work.
    """

    def scan(self) -> Iterator[IssueSnapshot]:
        return iter([])

    def get_issue(self, source_ref: str) -> IssueSnapshot | None:
        raise RuntimeError(f"_EmptyIssueSource.get_issue called unexpectedly for {source_ref!r}")


# ---------------------------------------------------------------------------
# Fixture: Registry backed by tmp_path SQLite DB (never touches production DB)
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(db: Path) -> Registry:
    """Registry on a fully-migrated in-memory (tmp_path) SQLite DB."""
    return Registry(db)


# ---------------------------------------------------------------------------
# Helper: seed a pending UoW with an explicit trigger JSON
# ---------------------------------------------------------------------------

def _seed_pending_uow(
    registry: Registry,
    issue_number: int,
    title: str,
    trigger: dict,
) -> str:
    """Create a UoW in 'pending' status with a specific trigger.

    Steps:
    1. upsert → proposed (trigger field defaults to {"type": "immediate"})
    2. Overwrite trigger with the desired value via direct SQL
    3. set_status_direct → pending

    Returns the uow_id.
    """
    result = registry.upsert(
        issue_number=issue_number,
        title=title,
        success_criteria=f"Test completion for issue #{issue_number}.",
    )
    assert isinstance(result, UpsertInserted), f"Expected UpsertInserted, got {result!r}"
    uow_id = result.id

    # Write the desired trigger directly — upsert always sets {"type": "immediate"}
    conn = sqlite3.connect(str(registry.db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "UPDATE uow_registry SET trigger = ? WHERE id = ?",
        (json.dumps(trigger), uow_id),
    )
    conn.commit()
    conn.close()

    # Transition to pending (the status GardenCaretaker._check_pending_triggers() queries)
    registry.set_status_direct(uow_id, "pending")

    return uow_id


# ---------------------------------------------------------------------------
# Test 1: GardenCaretaker promotes a pending UoW with trigger_type=immediate
# ---------------------------------------------------------------------------

def test_garden_caretaker_promotes_immediate_trigger(registry: Registry) -> None:
    """Pending UoW with trigger_type=immediate must transition to ready-for-steward.

    This is the primary regression guard for Bug #2: GardenCaretaker replaced
    issue-sweeper.py, and the evaluate_condition wiring for pending UoWs was
    silently dropped. A passing scan does nothing here — the promotion must
    come from _check_pending_triggers() calling evaluate_condition.
    """
    uow_id = _seed_pending_uow(
        registry,
        issue_number=7001,
        title="Immediate trigger integration test",
        trigger={"type": "immediate"},
    )

    # Verify precondition
    uow_before = registry.get(uow_id)
    assert uow_before is not None
    assert uow_before.status == UoWStatus.PENDING

    caretaker = GardenCaretaker(source=_EmptyIssueSource(), registry=registry)
    result = caretaker.run()

    uow_after = registry.get(uow_id)
    assert uow_after is not None, "UoW disappeared after GardenCaretaker.run()"
    assert uow_after.status == UoWStatus.READY_FOR_STEWARD, (
        f"Expected ready-for-steward after immediate trigger fired, got {uow_after.status!r}. "
        f"GardenCaretaker result: {result}"
    )
    assert result.get("triggers_fired", 0) == 1, (
        f"Expected triggers_fired=1, got {result.get('triggers_fired')}"
    )


# ---------------------------------------------------------------------------
# Test 2: GardenCaretaker holds a pending UoW with trigger_type=issue_closed
#         when the referenced issue is still open
# ---------------------------------------------------------------------------

def test_garden_caretaker_holds_issue_closed_trigger(registry: Registry) -> None:
    """Pending UoW with trigger_type=issue_closed stays pending when issue is open.

    evaluate_condition must consult the github_client (here: a stub returning
    state=open) and return False, so _check_pending_triggers() leaves the UoW
    in pending status.
    """
    uow_id = _seed_pending_uow(
        registry,
        issue_number=7002,
        title="Issue-closed trigger integration test",
        trigger={"type": "issue_closed", "number": 999},
    )

    # Stub github_client: issue 999 is still open
    def _stub_github_open(issue_number: int) -> dict:
        return {"status_code": 200, "state": "open"}

    with patch(
        "orchestration.garden_caretaker.evaluate_condition",
        wraps=lambda uow, registry=None, github_client=None: (
            # Call through with our open-issue stub replacing the default client
            __import__("orchestration.conditions", fromlist=["evaluate_condition"])
            .evaluate_condition(uow, registry=registry, github_client=_stub_github_open)
        ),
    ):
        caretaker = GardenCaretaker(source=_EmptyIssueSource(), registry=registry)
        result = caretaker.run()

    uow_after = registry.get(uow_id)
    assert uow_after is not None
    assert uow_after.status == UoWStatus.PENDING, (
        f"Expected UoW to stay pending (issue still open), got {uow_after.status!r}. "
        f"GardenCaretaker result: {result}"
    )
    assert result.get("triggers_fired", 0) == 0, (
        f"Expected triggers_fired=0 (issue open), got {result.get('triggers_fired')}"
    )


# ---------------------------------------------------------------------------
# Test 3: evaluate_condition is called for each pending UoW
# ---------------------------------------------------------------------------

def test_evaluate_condition_is_called_for_pending_uow(registry: Registry) -> None:
    """GardenCaretaker._check_pending_triggers() must call evaluate_condition for each pending UoW.

    This test is the direct wiring guard: it verifies that the function is
    actually invoked, not just that the state changes. A refactor that
    accidentally removes the call would fail here even if the logic is otherwise
    intact.
    """
    uow_id_a = _seed_pending_uow(
        registry,
        issue_number=7003,
        title="Pending UoW A — call count check",
        trigger={"type": "immediate"},
    )
    uow_id_b = _seed_pending_uow(
        registry,
        issue_number=7004,
        title="Pending UoW B — call count check",
        trigger={"type": "immediate"},
    )

    call_args: list[str] = []

    def _mock_evaluate(uow, *, registry=None, github_client=None) -> bool:
        call_args.append(uow.id)
        return False  # Return False so no state changes — we only care about the call

    with patch("orchestration.garden_caretaker.evaluate_condition", side_effect=_mock_evaluate):
        caretaker = GardenCaretaker(source=_EmptyIssueSource(), registry=registry)
        caretaker.run()

    assert uow_id_a in call_args, (
        f"evaluate_condition was not called for pending UoW {uow_id_a!r}. "
        f"Called with: {call_args}"
    )
    assert uow_id_b in call_args, (
        f"evaluate_condition was not called for pending UoW {uow_id_b!r}. "
        f"Called with: {call_args}"
    )
    assert len(call_args) == 2, (
        f"Expected evaluate_condition called exactly 2 times, got {len(call_args)}: {call_args}"
    )


# ---------------------------------------------------------------------------
# Test 4: Steward picks up a ready-for-steward UoW (handoff entry point)
# ---------------------------------------------------------------------------

def test_steward_picks_up_ready_for_steward_uow(registry: Registry, tmp_path: Path) -> None:
    """run_steward_cycle() must process a ready-for-steward UoW.

    The steward claims a ready-for-steward UoW by transitioning it to
    diagnosing (optimistic lock), then continues through the cycle. This test
    verifies the steward→executor handoff entry point is wired: a UoW that
    enters ready-for-steward is no longer in that state after the cycle.

    Uses dry_run=False with a stubbed github_client (no labels/closed state)
    and a null llm_prescriber to avoid subprocess calls.
    """
    from orchestration.registry import UpsertInserted, ApproveConfirmed

    result = registry.upsert(
        issue_number=7005,
        title="Steward handoff integration test",
        success_criteria="Integration test output written.",
    )
    assert isinstance(result, UpsertInserted)
    uow_id = result.id

    # Advance to ready-for-steward (approve now goes proposed→ready-for-steward atomically)
    approve = registry.approve(uow_id)
    assert isinstance(approve, ApproveConfirmed), f"Approve failed: {approve!r}"

    uow_before = registry.get(uow_id)
    assert uow_before is not None
    assert uow_before.status == UoWStatus.READY_FOR_STEWARD

    # Stub github_client: issue is open, no blocking labels
    def _stub_github_client(issue_number: int) -> dict:
        return {
            "status_code": 200,
            "state": "open",
            "labels": [],
            "body": "Integration test UoW.",
            "title": "Steward handoff integration test",
        }

    # null notify_dan — tests must not send Telegram messages
    def _null_notify(*_args, **_kwargs) -> None:
        pass

    from src.orchestration.steward import run_steward_cycle

    cycle_result = run_steward_cycle(
        registry=registry,
        dry_run=False,
        github_client=_stub_github_client,
        artifact_dir=tmp_path / "artifacts",
        notify_dan=_null_notify,
        notify_dan_early_warning=_null_notify,
        bootup_candidate_gate=False,
        llm_prescriber=None,  # bypass LLM subprocess
    )

    uow_after = registry.get(uow_id)
    assert uow_after is not None
    assert uow_after.status != UoWStatus.READY_FOR_STEWARD, (
        f"Steward did not claim UoW {uow_id!r} — still ready-for-steward after cycle. "
        f"Cycle result: {cycle_result}"
    )
    assert cycle_result.evaluated >= 1, (
        f"Steward cycle reported 0 UoWs evaluated. Result: {cycle_result}"
    )


# ---------------------------------------------------------------------------
# Helper shared by steward trace tests
# ---------------------------------------------------------------------------

def _seed_ready_for_steward_uow(
    registry: Registry,
    issue_number: int,
    title: str,
    success_criteria: str = "Test completion.",
) -> str:
    """Create a UoW and advance it to ready-for-steward. Returns uow_id."""
    from orchestration.registry import UpsertInserted, ApproveConfirmed

    result = registry.upsert(
        issue_number=issue_number,
        title=title,
        success_criteria=success_criteria,
    )
    assert isinstance(result, UpsertInserted)
    uow_id = result.id
    approve = registry.approve(uow_id)
    assert isinstance(approve, ApproveConfirmed), f"Approve failed: {approve!r}"
    return uow_id


def _null_notify(*_args, **_kwargs) -> None:
    """No-op notification stub — tests must not send Telegram messages."""
    pass


def _stub_github_open(issue_number: int) -> dict:
    """Stub GitHub client: open issue, no labels."""
    return {
        "status_code": 200,
        "state": "open",
        "labels": [],
        "body": "Test UoW.",
        "title": "Test",
    }


# ---------------------------------------------------------------------------
# Test 5: Steward writes a trace entry on first execution
# ---------------------------------------------------------------------------

def test_steward_trace_entry_written_on_first_execution(registry: Registry, tmp_path: Path) -> None:
    """run_steward_cycle() must append a trace entry to steward_agenda on first contact.

    The trace entry must have cycle=0, posture="first_execution",
    success_criteria_checked as a list, anomalies==[], and a non-empty
    posture_rationale.
    """
    from src.orchestration.steward import run_steward_cycle

    uow_id = _seed_ready_for_steward_uow(
        registry,
        issue_number=8001,
        title="Trace entry first execution test",
    )

    run_steward_cycle(
        registry=registry,
        dry_run=False,
        github_client=_stub_github_open,
        artifact_dir=tmp_path / "artifacts",
        notify_dan=_null_notify,
        notify_dan_early_warning=_null_notify,
        bootup_candidate_gate=False,
        llm_prescriber=None,
    )

    uow_after = registry.get(uow_id)
    assert uow_after is not None
    assert uow_after.steward_agenda is not None, "steward_agenda is None after cycle"

    agenda = json.loads(uow_after.steward_agenda)
    assert isinstance(agenda, list), f"steward_agenda is not a list: {agenda!r}"

    # Find the trace entry (has "cycle" key)
    trace_entries = [e for e in agenda if "cycle" in e]
    assert len(trace_entries) >= 1, (
        f"No trace entries found in agenda. Full agenda: {agenda}"
    )

    entry = trace_entries[0]
    assert entry["cycle"] == 0, f"Expected cycle=0, got {entry['cycle']!r}"
    assert entry["posture"] == "first_execution", (
        f"Expected posture='first_execution', got {entry['posture']!r}"
    )
    assert isinstance(entry["success_criteria_checked"], list), (
        f"success_criteria_checked is not a list: {entry['success_criteria_checked']!r}"
    )
    assert entry["anomalies"] == [], (
        f"Expected anomalies=[], got {entry['anomalies']!r}"
    )
    assert isinstance(entry.get("posture_rationale"), str) and entry["posture_rationale"], (
        f"posture_rationale is empty or missing: {entry.get('posture_rationale')!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: Steward trace entries accumulate across cycles
# ---------------------------------------------------------------------------

def test_steward_trace_entry_accumulates_per_cycle(registry: Registry, tmp_path: Path) -> None:
    """steward_agenda must accumulate one trace entry per steward cycle.

    After the first cycle the agenda has 1 trace entry; after a second cycle
    (simulated by advancing UoW back to ready-for-steward) it has 2.
    """
    from src.orchestration.steward import run_steward_cycle

    uow_id = _seed_ready_for_steward_uow(
        registry,
        issue_number=8002,
        title="Trace accumulation test",
    )

    # Cycle 1
    run_steward_cycle(
        registry=registry,
        dry_run=False,
        github_client=_stub_github_open,
        artifact_dir=tmp_path / "artifacts",
        notify_dan=_null_notify,
        notify_dan_early_warning=_null_notify,
        bootup_candidate_gate=False,
        llm_prescriber=None,
    )

    uow_after_1 = registry.get(uow_id)
    assert uow_after_1 is not None
    agenda_1 = json.loads(uow_after_1.steward_agenda or "[]")
    trace_entries_1 = [e for e in agenda_1 if "cycle" in e]
    assert len(trace_entries_1) == 1, (
        f"Expected 1 trace entry after cycle 1, got {len(trace_entries_1)}: {agenda_1}"
    )

    # Advance UoW back to ready-for-steward to simulate executor return
    # (set status directly so we can run a second Steward pass)
    registry.set_status_direct(uow_id, "ready-for-steward")

    # Cycle 2
    run_steward_cycle(
        registry=registry,
        dry_run=False,
        github_client=_stub_github_open,
        artifact_dir=tmp_path / "artifacts",
        notify_dan=_null_notify,
        notify_dan_early_warning=_null_notify,
        bootup_candidate_gate=False,
        llm_prescriber=None,
    )

    uow_after_2 = registry.get(uow_id)
    assert uow_after_2 is not None
    agenda_2 = json.loads(uow_after_2.steward_agenda or "[]")
    trace_entries_2 = [e for e in agenda_2 if "cycle" in e]
    assert len(trace_entries_2) == 2, (
        f"Expected 2 trace entries after cycle 2, got {len(trace_entries_2)}: {agenda_2}"
    )


# ---------------------------------------------------------------------------
# Test 7: Trace entry shows completion_check passed on a done UoW
# ---------------------------------------------------------------------------

def test_steward_trace_completion_check_passes_on_done(registry: Registry, tmp_path: Path) -> None:
    """When the executor signals completion, the trace entry must show passed=True.

    Simulated by writing a result file with outcome=complete and advancing the
    UoW through an execution cycle before the steward re-enters.
    """
    import uuid
    from src.orchestration.steward import run_steward_cycle
    from orchestration.registry import UpsertInserted, ApproveConfirmed

    # Seed and advance UoW
    result = registry.upsert(
        issue_number=8003,
        title="Completion check trace test",
        success_criteria="Output must report outcome=complete.",
    )
    assert isinstance(result, UpsertInserted)
    uow_id = result.id
    approve = registry.approve(uow_id)
    assert isinstance(approve, ApproveConfirmed)

    # Cycle 1: first_execution — steward dispatches executor
    run_steward_cycle(
        registry=registry,
        dry_run=False,
        github_client=_stub_github_open,
        artifact_dir=tmp_path / "artifacts",
        notify_dan=_null_notify,
        notify_dan_early_warning=_null_notify,
        bootup_candidate_gate=False,
        llm_prescriber=None,
    )

    # Simulate executor completing: set output_ref + result file + audit entry
    output_ref = str(tmp_path / f"{uow_id}.output.md")
    Path(output_ref).write_text("Execution output — complete.", encoding="utf-8")

    result_file = Path(output_ref + ".result.json")
    result_file.write_text(
        json.dumps({"uow_id": uow_id, "outcome": "complete", "reason": "all steps done"}),
        encoding="utf-8",
    )

    # Write output_ref to the UoW record and write an execution_complete audit entry
    conn = sqlite3.connect(str(registry.db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "UPDATE uow_registry SET output_ref = ?, status = 'ready-for-steward' WHERE id = ?",
        (output_ref, uow_id),
    )
    conn.commit()
    conn.close()
    registry.append_audit_log(uow_id, {
        "event": "execution_complete",
        "actor": "executor",
        "uow_id": uow_id,
        "timestamp": _now_iso(),
    })

    # Cycle 2: steward re-enters and should find execution_complete posture
    run_steward_cycle(
        registry=registry,
        dry_run=False,
        github_client=_stub_github_open,
        artifact_dir=tmp_path / "artifacts",
        notify_dan=_null_notify,
        notify_dan_early_warning=_null_notify,
        bootup_candidate_gate=False,
        llm_prescriber=None,
    )

    uow_after = registry.get(uow_id)
    assert uow_after is not None
    agenda = json.loads(uow_after.steward_agenda or "[]")
    trace_entries = [e for e in agenda if "cycle" in e]
    assert len(trace_entries) >= 2, (
        f"Expected at least 2 trace entries, got {len(trace_entries)}: {agenda}"
    )

    # The cycle 1 trace entry (index depends on initial agenda nodes before traces)
    # Find the trace entry with cycle==1 (second pass)
    cycle_1_entry = next((e for e in trace_entries if e["cycle"] == 1), None)
    assert cycle_1_entry is not None, (
        f"No trace entry with cycle=1 found. Trace entries: {trace_entries}"
    )
    checks = cycle_1_entry.get("success_criteria_checked", [])
    assert len(checks) >= 1, (
        f"success_criteria_checked is empty in cycle 1 trace entry: {cycle_1_entry}"
    )
    assert checks[0]["passed"] is True, (
        f"Expected passed=True for execution_complete posture, got {checks[0]!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: Trace entry captures anomaly on stuck condition
# ---------------------------------------------------------------------------

def test_steward_trace_anomaly_captured_on_stuck(registry: Registry, tmp_path: Path) -> None:
    """When a stuck condition fires, the trace entry anomalies list must be non-empty.

    Simulated by forcing the hard-cap condition (steward_cycles >= _HARD_CAP_CYCLES)
    via direct SQL — the Steward will diagnose hard_cap as stuck_condition and
    write a trace entry with anomalies=[hard_cap].
    """
    from src.orchestration.steward import run_steward_cycle, _HARD_CAP_CYCLES

    uow_id = _seed_ready_for_steward_uow(
        registry,
        issue_number=8004,
        title="Anomaly trace capture test",
    )

    # Directly set steward_cycles to _HARD_CAP_CYCLES so the Steward
    # immediately hits the hard_cap stuck condition on next diagnosis.
    conn = sqlite3.connect(str(registry.db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "UPDATE uow_registry SET steward_cycles = ? WHERE id = ?",
        (_HARD_CAP_CYCLES, uow_id),
    )
    conn.commit()
    conn.close()

    captured_notifications: list[tuple] = []

    def _capturing_notify(uow, condition, **_kwargs) -> None:
        captured_notifications.append((uow.id, condition))

    run_steward_cycle(
        registry=registry,
        dry_run=False,
        github_client=_stub_github_open,
        artifact_dir=tmp_path / "artifacts",
        notify_dan=_capturing_notify,
        notify_dan_early_warning=_null_notify,
        bootup_candidate_gate=False,
        llm_prescriber=None,
    )

    uow_after = registry.get(uow_id)
    assert uow_after is not None
    agenda = json.loads(uow_after.steward_agenda or "[]")
    trace_entries = [e for e in agenda if "cycle" in e]
    assert len(trace_entries) >= 1, (
        f"No trace entries found in agenda after stuck cycle: {agenda}"
    )

    # Find the trace entry for this cycle (cycle == _HARD_CAP_CYCLES)
    stuck_entry = next((e for e in trace_entries if e["cycle"] == _HARD_CAP_CYCLES), None)
    assert stuck_entry is not None, (
        f"No trace entry with cycle={_HARD_CAP_CYCLES} found. Entries: {trace_entries}"
    )
    assert len(stuck_entry.get("anomalies", [])) > 0, (
        f"Expected non-empty anomalies for hard_cap stuck condition, got: {stuck_entry!r}"
    )
    # Also verify the notification was fired (confirms the stuck branch was hit)
    assert len(captured_notifications) >= 1, "Stuck condition did not fire a Dan notification"
