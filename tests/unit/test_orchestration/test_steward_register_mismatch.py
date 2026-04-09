"""
Tests for PR D: register-mismatch gate in steward.py.

Covers:
- _check_register_executor_compatibility: compatible pairs return (True, "")
- _check_register_executor_compatibility: incompatible pairs return (False, reason)
- Key mismatch: philosophical→functional-engineer returns (False, reason)
- Key mismatch: human-judgment→lobster-ops returns (False, reason)
- Compatible: operational→functional-engineer returns (True, "")
- Compatible: iterative-convergent→lobster-ops returns (True, "")
- Compatible: philosophical→frontier-writer returns (True, "")
- Compatible: human-judgment→design-review returns (True, "")
- Unknown register: treated as compatible (conservative pass-through)
- Gate fires in prescribe branch: philosophical UoW → Surfaced(register_mismatch)
- Gate blocks artifact write: mismatch → no workflow artifact written
- Gate observability: mismatch_observation logged to audit_log
- No mismatch for operational UoWs: prescribes normally
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import (
    _check_register_executor_compatibility,
    IssueInfo,
    LLMPrescription,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _insert_uow(conn: sqlite3.Connection, uow_id: str,
                status: str = "ready-for-steward",
                register: str = "operational",
                summary: str = "Test UoW",
                success_criteria: str = "output file present",
                steward_cycles: int = 0) -> None:
    now = _now_iso()
    conn.execute(
        """INSERT INTO uow_registry
           (id, type, source, source_issue_number, sweep_date, status, posture,
            created_at, updated_at, summary, steward_cycles,
            success_criteria, route_evidence, trigger, register)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uow_id, "executable", "github:issue/1", 1, "2026-01-01", status, "solo",
         now, now, summary, steward_cycles,
         success_criteria, "{}", '{"type": "immediate"}', register),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Unit tests: _check_register_executor_compatibility
# ---------------------------------------------------------------------------

class TestCheckRegisterExecutorCompatibility:
    """Pure function — register + executor_type → (is_compatible, reason)."""

    # Compatible pairs

    def test_operational_functional_engineer(self):
        ok, reason = _check_register_executor_compatibility("operational", "functional-engineer")
        assert ok is True
        assert reason == ""

    def test_operational_lobster_ops(self):
        ok, reason = _check_register_executor_compatibility("operational", "lobster-ops")
        assert ok is True

    def test_operational_general(self):
        ok, reason = _check_register_executor_compatibility("operational", "general")
        assert ok is True

    def test_iterative_convergent_functional_engineer(self):
        ok, reason = _check_register_executor_compatibility("iterative-convergent", "functional-engineer")
        assert ok is True

    def test_iterative_convergent_lobster_ops(self):
        ok, reason = _check_register_executor_compatibility("iterative-convergent", "lobster-ops")
        assert ok is True

    def test_philosophical_frontier_writer(self):
        ok, reason = _check_register_executor_compatibility("philosophical", "frontier-writer")
        assert ok is True

    def test_human_judgment_design_review(self):
        ok, reason = _check_register_executor_compatibility("human-judgment", "design-review")
        assert ok is True

    # Incompatible pairs

    def test_philosophical_functional_engineer_is_incompatible(self):
        """Key spec case: philosophical→functional-engineer must be blocked."""
        ok, reason = _check_register_executor_compatibility("philosophical", "functional-engineer")
        assert ok is False
        assert "philosophical" in reason
        assert "functional-engineer" in reason

    def test_philosophical_lobster_ops_is_incompatible(self):
        ok, reason = _check_register_executor_compatibility("philosophical", "lobster-ops")
        assert ok is False

    def test_human_judgment_functional_engineer_is_incompatible(self):
        ok, reason = _check_register_executor_compatibility("human-judgment", "functional-engineer")
        assert ok is False

    def test_human_judgment_lobster_ops_is_incompatible(self):
        ok, reason = _check_register_executor_compatibility("human-judgment", "lobster-ops")
        assert ok is False

    def test_iterative_convergent_general_is_incompatible(self):
        """general is not compatible with iterative-convergent per spec."""
        ok, reason = _check_register_executor_compatibility("iterative-convergent", "general")
        assert ok is False

    def test_philosophical_register_lobster_ops_mismatch(self):
        """philosophical register is incompatible with lobster-ops executor."""
        ok, reason = _check_register_executor_compatibility("philosophical", "lobster-ops")
        assert ok is False

    # Direction string

    def test_mismatch_reason_contains_direction(self):
        ok, reason = _check_register_executor_compatibility("philosophical", "functional-engineer")
        assert ok is False
        assert "\u2192" in reason or "->" in reason or "philosophical" in reason and "functional-engineer" in reason

    # Unknown register

    def test_unknown_register_is_compatible(self):
        """Unknown registers pass through without blocking."""
        ok, reason = _check_register_executor_compatibility("experimental-v4", "functional-engineer")
        assert ok is True


# ---------------------------------------------------------------------------
# Integration tests: gate fires in _process_uow
# ---------------------------------------------------------------------------

class TestRegisterMismatchGateIntegration:
    """Integration tests verifying the gate fires correctly in _process_uow."""

    def _make_registry(self, tmp_path: Path):
        from src.orchestration.registry import Registry
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path=db_path)
        return registry, db_path

    def test_philosophical_uow_triggers_mismatch_surface(self, tmp_path):
        """Philosophical UoW with functional-engineer keyword → register_mismatch surface."""
        registry, db_path = self._make_registry(tmp_path)

        # UoW with philosophical register and a summary that would route to functional-engineer
        # (has 'implement' keyword → _select_executor_type → functional-engineer)
        conn = _open_db(db_path)
        _insert_uow(conn, "uow-phil", register="philosophical",
                    summary="implement philosophical exploration of consciousness",
                    steward_cycles=0)
        conn.close()

        surfaced: list[tuple] = []

        def fake_notify(uow, condition, surface_log=None, return_reason=None):
            surfaced.append((uow.id, condition))

        from src.orchestration.steward import _process_uow, _fetch_audit_entries, Surfaced

        uow = registry.get("uow-phil")
        audit_entries = _fetch_audit_entries(registry, "uow-phil")

        result = _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info=IssueInfo(status_code=1, state="open", labels=[], body="", title=""),
            dry_run=True, artifact_dir=tmp_path, notify_dan=fake_notify,
            llm_prescriber=lambda *a, **k: LLMPrescription(instructions="x", success_criteria_check="y", estimated_cycles=1),
        )

        assert isinstance(result, Surfaced), f"Expected Surfaced, got {result}"
        assert result.condition == "register_mismatch"
        assert len(surfaced) == 1
        assert surfaced[0] == ("uow-phil", "register_mismatch")

    def test_mismatch_gate_blocks_artifact_write(self, tmp_path):
        """When register_mismatch fires, no workflow artifact is written to disk."""
        registry, db_path = self._make_registry(tmp_path)
        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        conn = _open_db(db_path)
        _insert_uow(conn, "uow-block", register="philosophical",
                    summary="implement philosophical work",
                    steward_cycles=0)
        conn.close()

        from src.orchestration.steward import _process_uow, _fetch_audit_entries, Surfaced

        uow = registry.get("uow-block")
        audit_entries = _fetch_audit_entries(registry, "uow-block")

        result = _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info=IssueInfo(status_code=1, state="open", labels=[], body="", title=""),
            dry_run=True, artifact_dir=artifact_dir, notify_dan=lambda *a, **k: None,
            llm_prescriber=lambda *a, **k: LLMPrescription(instructions="x", success_criteria_check="y", estimated_cycles=1),
        )

        assert isinstance(result, Surfaced)
        # No artifact file should exist in artifact_dir
        artifact_files = list(artifact_dir.glob("*.json"))
        assert len(artifact_files) == 0, f"Unexpected artifact files: {artifact_files}"

    def test_mismatch_observation_logged_to_steward_log(self, tmp_path):
        """register_mismatch event appears in steward_log after gate fires."""
        registry, db_path = self._make_registry(tmp_path)

        conn = _open_db(db_path)
        _insert_uow(conn, "uow-obs", register="philosophical",
                    summary="implement philosophical analysis",
                    steward_cycles=0)
        conn.close()

        from src.orchestration.steward import _process_uow, _fetch_audit_entries

        uow = registry.get("uow-obs")
        audit_entries = _fetch_audit_entries(registry, "uow-obs")

        _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info=IssueInfo(status_code=1, state="open", labels=[], body="", title=""),
            dry_run=False, artifact_dir=tmp_path, notify_dan=lambda *a, **k: None,
            llm_prescriber=lambda *a, **k: LLMPrescription(instructions="x", success_criteria_check="y", estimated_cycles=1),
        )

        uow_after = registry.get("uow-obs")
        log_str = uow_after.steward_log or ""
        events = []
        for line in log_str.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
                events.append(entry.get("event"))
            except json.JSONDecodeError:
                pass

        assert "register_mismatch" in events, f"Expected register_mismatch in events: {events}"

    def test_operational_uow_prescribes_normally(self, tmp_path):
        """Operational UoW with functional-engineer executor — no mismatch, prescribes normally."""
        registry, db_path = self._make_registry(tmp_path)

        conn = _open_db(db_path)
        _insert_uow(conn, "uow-ops", register="operational",
                    summary="implement feature for operational task",
                    steward_cycles=0)
        conn.close()

        from src.orchestration.steward import _process_uow, _fetch_audit_entries, Prescribed

        uow = registry.get("uow-ops")
        audit_entries = _fetch_audit_entries(registry, "uow-ops")

        result = _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info=IssueInfo(status_code=1, state="open", labels=[], body="", title=""),
            dry_run=True, artifact_dir=tmp_path, notify_dan=lambda *a, **k: None,
            llm_prescriber=lambda *a, **k: LLMPrescription(instructions="x", success_criteria_check="y", estimated_cycles=1),
        )

        assert isinstance(result, Prescribed), f"Expected Prescribed, got {result}"

    def test_mismatch_observation_in_audit_log(self, tmp_path):
        """register_mismatch_observation entry written to audit_log on mismatch."""
        registry, db_path = self._make_registry(tmp_path)

        conn = _open_db(db_path)
        _insert_uow(conn, "uow-audit", register="philosophical",
                    summary="implement philosophical deep work",
                    steward_cycles=0)
        conn.close()

        from src.orchestration.steward import _process_uow, _fetch_audit_entries

        uow = registry.get("uow-audit")
        audit_entries = _fetch_audit_entries(registry, "uow-audit")

        _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info=IssueInfo(status_code=1, state="open", labels=[], body="", title=""),
            dry_run=False, artifact_dir=tmp_path, notify_dan=lambda *a, **k: None,
            llm_prescriber=lambda *a, **k: LLMPrescription(instructions="x", success_criteria_check="y", estimated_cycles=1),
        )

        # Read audit log directly
        audit_conn = _open_db(db_path)
        rows = audit_conn.execute(
            "SELECT event, note FROM audit_log WHERE uow_id = ?", ("uow-audit",)
        ).fetchall()
        audit_conn.close()

        events = [r["event"] for r in rows]
        assert "register_mismatch_observation" in events, f"Expected register_mismatch_observation in: {events}"

        # Verify observation schema — note column is a JSON string
        obs_rows = [r for r in rows if r["event"] == "register_mismatch_observation"]
        assert len(obs_rows) >= 1
        raw_note = obs_rows[0]["note"]
        note = json.loads(raw_note) if isinstance(raw_note, str) else raw_note
        # note is the JSON payload in the audit_log note column
        assert note.get("event") == "register_mismatch_observation"
        assert note.get("uow_id") == "uow-audit"
        assert "register" in note
        assert "executor_type_attempted" in note
        assert "direction" in note
        assert "steward_cycles" in note
        assert "timestamp" in note
