"""
Unit tests for generate_v2_prescription() and related helpers in steward.py.

WOS-UoW: uow_20260504_1cf8cb
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import (
    Diagnosis,
    IssueInfo,
    PrescriptionV2,
    _build_v2_prescription_deterministic,
    generate_v2_prescription,
)


# ---------------------------------------------------------------------------
# DB helpers (mirror test_steward.py patterns)
# ---------------------------------------------------------------------------

def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the Phase 1 + Phase 2 base schema (mirrors test_steward.py _apply_phase2_schema).

    Columns added by migrations (register, lifetime_cycles, execution_attempts, etc.)
    are intentionally omitted — Registry._run_migrations() adds them at fixture time.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS uow_registry (
            id                  TEXT    PRIMARY KEY,
            type                TEXT    NOT NULL DEFAULT 'executable',
            source              TEXT    NOT NULL,
            source_issue_number INTEGER,
            sweep_date          TEXT,
            status              TEXT    NOT NULL DEFAULT 'proposed',
            posture             TEXT    NOT NULL DEFAULT 'solo',
            agent               TEXT,
            children            TEXT    DEFAULT '[]',
            parent              TEXT,
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL,
            started_at          TEXT,
            completed_at        TEXT,
            summary             TEXT    NOT NULL,
            output_ref          TEXT,
            hooks_applied       TEXT    DEFAULT '[]',
            route_reason        TEXT,
            route_evidence      TEXT    DEFAULT '{}',
            trigger             TEXT    DEFAULT '{"type": "immediate"}',
            vision_ref          TEXT    DEFAULT NULL,
            workflow_artifact   TEXT    NULL,
            success_criteria    TEXT    NULL,
            prescribed_skills   TEXT    NULL,
            steward_cycles      INTEGER NOT NULL DEFAULT 0,
            timeout_at          TEXT    NULL,
            estimated_runtime   INTEGER NULL,
            steward_agenda      TEXT    NULL,
            steward_log         TEXT    NULL,
            UNIQUE(source_issue_number, sweep_date)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            uow_id      TEXT    NOT NULL,
            event       TEXT    NOT NULL,
            from_status TEXT,
            to_status   TEXT,
            agent       TEXT,
            note        TEXT
        );
        CREATE VIEW IF NOT EXISTS executor_uow_view AS
        SELECT id, type, source, source_issue_number, sweep_date,
               status, posture, agent, children, parent,
               created_at, updated_at, started_at, completed_at,
               summary, output_ref, hooks_applied,
               route_reason, route_evidence, trigger, vision_ref,
               workflow_artifact, success_criteria, prescribed_skills,
               steward_cycles, timeout_at, estimated_runtime
        FROM uow_registry;
    """)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_uow_row(
    conn: sqlite3.Connection,
    uow_id: str | None = None,
    status: str = "ready-for-steward",
    steward_cycles: int = 0,
    summary: str = "Test UoW",
    success_criteria: str | None = "Output file exists with non-empty content",
    vision_ref: dict | None = None,
    register: str = "operational",
    source: str = "github:issue/574",
    source_issue_number: int | None = None,
    sweep_date: str | None = None,
) -> str:
    if uow_id is None:
        uow_id = f"uow_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
    now = _now_iso()
    vision_ref_json = json.dumps(vision_ref) if vision_ref else None
    # Insert base fields — migration-added columns (register, lifetime_cycles,
    # execution_attempts) are set separately via UPDATE after migration runs.
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, steward_cycles,
             success_criteria, route_evidence, trigger, vision_ref)
        VALUES (?, 'executable', ?, ?, ?, ?, 'solo',
                ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}', ?)
        """,
        (uow_id, source, source_issue_number, sweep_date, status, now, now, summary,
         steward_cycles, success_criteria, vision_ref_json),
    )
    conn.commit()
    return uow_id


def _set_uow_register(db_path: Path, uow_id: str, register: str) -> None:
    """Set register column after migrations have been applied."""
    conn = _open_db(db_path)
    conn.execute("UPDATE uow_registry SET register = ? WHERE id = ?", (register, uow_id))
    conn.commit()
    conn.close()


def _get_audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = _open_db(db_path)
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id",
        (uow_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_first_execution_diagnosis() -> Diagnosis:
    return Diagnosis(
        reentry_posture="first_execution",
        return_reason=None,
        return_reason_classification="none",
        output_content="",
        output_valid=False,
        is_complete=False,
        completion_rationale="",
        stuck_condition=None,
        executor_outcome=None,
        success_criteria_missing=False,
    )


def _make_partial_diagnosis(completion_rationale: str = "Partial work completed") -> Diagnosis:
    return Diagnosis(
        reentry_posture="execution_partial",
        return_reason="partial_output",
        return_reason_classification="partial",
        output_content="some output",
        output_valid=True,
        is_complete=False,
        completion_rationale=completion_rationale,
        stuck_condition=None,
        executor_outcome="partial",
        success_criteria_missing=False,
    )


def _make_failed_diagnosis(completion_rationale: str = "Execution failed") -> Diagnosis:
    return Diagnosis(
        reentry_posture="execution_failed",
        return_reason="execution_error",
        return_reason_classification="error",
        output_content="",
        output_valid=False,
        is_complete=False,
        completion_rationale=completion_rationale,
        stuck_condition=None,
        executor_outcome="failed",
        success_criteria_missing=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "registry.db"
    conn = _open_db(path)
    _apply_schema(conn)
    conn.close()
    return path


@pytest.fixture
def registry(db_path: Path):
    from src.orchestration.registry import Registry
    return Registry(db_path)


def _make_uow(
    db_path: Path,
    uow_id: str | None = None,
    steward_cycles: int = 0,
    summary: str = "Test UoW",
    success_criteria: str | None = "Output file exists with non-empty content",
    vision_ref: dict | None = None,
    register: str = "operational",
):
    conn = _open_db(db_path)
    uid = _make_uow_row(
        conn,
        uow_id=uow_id,
        steward_cycles=steward_cycles,
        summary=summary,
        success_criteria=success_criteria,
        vision_ref=vision_ref,
        register=register,
    )
    conn.close()
    from src.orchestration.registry import Registry
    r = Registry(db_path)
    return r.get(uid)


# ---------------------------------------------------------------------------
# Monkeypatch helper: stub _llm_prescribe_v2 to use deterministic fallback
# ---------------------------------------------------------------------------

def _stub_llm_prescribe_v2(
    uow, diagnosis_section, executor_posture, selected_executor_type,
    issue_body, vision_orientation, dan_register, prescribed_skills, cycles, now_iso,
):
    """Return a deterministic v2 prescription without calling Claude."""
    from src.orchestration.steward import _build_v2_prescription_deterministic
    return _build_v2_prescription_deterministic(
        uow=uow,
        diagnosis_section=diagnosis_section,
        executor_posture=executor_posture,
        selected_executor_type=selected_executor_type,
        prescribed_skills=prescribed_skills,
        cycles=cycles,
        now_iso=now_iso,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_v2_prescription_first_execution_investigation(db_path, registry):
    """generate_v2_prescription returns valid schema for first_execution operational UoW."""
    uow = _make_uow(db_path, steward_cycles=0, register="operational")
    diagnosis = _make_first_execution_diagnosis()

    with patch("src.orchestration.steward._llm_prescribe_v2", side_effect=_stub_llm_prescribe_v2):
        p = generate_v2_prescription(
            uow=uow,
            diagnosis=diagnosis,
            issue_info=None,
            cycles=0,
            registry=registry,
            dry_run=True,
        )

    assert isinstance(p, PrescriptionV2)
    assert p.audit_metadata["executor_posture"] == "first_execution"
    assert p.audit_metadata["cycle"] == 0
    assert p.audit_metadata["schema_version"] == "1.0.0"
    assert p.diagnosis["completion_gap"] == ""
    assert p.diagnosis["prior_cycle_count"] == 0
    assert p.diagnosis["reentry_posture"] == "first_execution"
    assert p.prescription["instructions"]
    assert p.workflow["agent_type"] in {
        "functional-engineer", "lobster-ops", "lobster-generalist",
        "lobster-meta", "frontier-writer", "design-review",
    }


def test_v2_prescription_reentry_continuation(db_path, registry):
    """generate_v2_prescription sets executor_posture=continuation on second cycle."""
    uow = _make_uow(db_path, steward_cycles=1, register="operational")
    diagnosis = _make_partial_diagnosis("Tests are passing but PR is not open yet")

    with patch("src.orchestration.steward._llm_prescribe_v2", side_effect=_stub_llm_prescribe_v2):
        p = generate_v2_prescription(
            uow=uow,
            diagnosis=diagnosis,
            issue_info=None,
            cycles=1,
            registry=registry,
            dry_run=True,
        )

    assert p.audit_metadata["executor_posture"] == "continuation"
    assert p.audit_metadata["cycle"] == 1
    assert p.diagnosis["prior_cycle_count"] == 1
    assert p.diagnosis["completion_gap"] != ""


def test_v2_prescription_remediation_posture_on_failure(db_path, registry):
    """generate_v2_prescription sets executor_posture=remediation on execution_failed."""
    uow = _make_uow(db_path, steward_cycles=1, register="operational")
    diagnosis = _make_failed_diagnosis("Tests failed with import error")

    with patch("src.orchestration.steward._llm_prescribe_v2", side_effect=_stub_llm_prescribe_v2):
        p = generate_v2_prescription(
            uow=uow,
            diagnosis=diagnosis,
            issue_info=None,
            cycles=1,
            registry=registry,
            dry_run=True,
        )

    assert p.audit_metadata["executor_posture"] == "remediation"
    assert p.diagnosis.get("corrective_intent") is True


def test_v2_prescription_vision_context_in_dan_context(db_path, registry, monkeypatch):
    """generate_v2_prescription uses vision route result when vision_ref present."""
    from src.orchestration.vision_routing import VisionRouteResult

    vision_ref = {
        "field": "test_field",
        "layer": "operational",
        "statement": "test",
        "anchored_at": "2026-01-01",
    }
    uow = _make_uow(db_path, steward_cycles=0, vision_ref=vision_ref)
    diagnosis = _make_first_execution_diagnosis()

    fake_vision_result = VisionRouteResult(
        route_reason="vision-anchored: test vision route",
        anchored=True,
        fallback_logged=False,
        vision_layer="operational",
        vision_field="test_field",
        stale=False,
    )

    monkeypatch.setattr(
        "src.orchestration.steward.resolve_vision_route",
        lambda uow_, **kwargs: fake_vision_result,
    )

    captured_vision: list[str] = []

    def _capturing_llm_v2(uow, diagnosis_section, executor_posture, selected_executor_type,
                          issue_body, vision_orientation, dan_register, prescribed_skills,
                          cycles, now_iso):
        captured_vision.append(vision_orientation)
        return _stub_llm_prescribe_v2(
            uow, diagnosis_section, executor_posture, selected_executor_type,
            issue_body, vision_orientation, dan_register, prescribed_skills, cycles, now_iso,
        )

    with patch("src.orchestration.steward._llm_prescribe_v2", side_effect=_capturing_llm_v2):
        generate_v2_prescription(
            uow=uow,
            diagnosis=diagnosis,
            issue_info=None,
            cycles=0,
            registry=registry,
            dry_run=True,
        )

    assert captured_vision, "vision_orientation was never passed to _llm_prescribe_v2"
    assert "vision-anchored" in captured_vision[0]


def test_v2_prescription_audit_trail_written(db_path, registry):
    """generate_v2_prescription writes prescription_v2 event to audit log when dry_run=False."""
    uow = _make_uow(db_path, steward_cycles=0)
    diagnosis = _make_first_execution_diagnosis()

    with patch("src.orchestration.steward._llm_prescribe_v2", side_effect=_stub_llm_prescribe_v2):
        generate_v2_prescription(
            uow=uow,
            diagnosis=diagnosis,
            issue_info=None,
            cycles=0,
            registry=registry,
            dry_run=False,
        )

    entries = _get_audit_entries(db_path, uow.id)
    events = [e["event"] for e in entries]
    assert "prescription_v2" in events


def test_v2_deterministic_fallback_produces_valid_structure(db_path):
    """_build_v2_prescription_deterministic returns all 7 required sections."""
    uow = _make_uow(db_path, steward_cycles=0, summary="Implement feature X")
    diagnosis_section = {
        "signal": "UoW entered ready-for-steward.",
        "reentry_posture": "first_execution",
        "completion_gap": "",
        "executor_outcome": None,
        "prior_cycle_count": 0,
    }

    p = _build_v2_prescription_deterministic(
        uow=uow,
        diagnosis_section=diagnosis_section,
        executor_posture="first_execution",
        selected_executor_type="lobster-generalist",
        prescribed_skills=[],
        cycles=0,
        now_iso=_now_iso(),
    )

    assert isinstance(p, PrescriptionV2)
    # All 7 sections present
    for section in ("diagnosis", "prescription", "workflow", "constraints",
                    "success_criteria", "dan_context", "audit_metadata"):
        assert hasattr(p, section), f"missing section: {section}"

    assert p.prescription["minimum_viable_output"]
    assert p.audit_metadata["schema_version"] == "1.0.0"
    assert p.audit_metadata["executor_posture"] == "first_execution"
    assert p.workflow["agent_type"] == "lobster-generalist"
    assert p.constraints["boundary"]
    assert p.success_criteria["check"]


def test_v2_prescription_cycle_equals_prior_cycle_count(db_path, registry):
    """audit_metadata.cycle must equal diagnosis.prior_cycle_count."""
    for cycles in (0, 1, 3):
        uow = _make_uow(db_path, steward_cycles=cycles)
        if cycles == 0:
            diagnosis = _make_first_execution_diagnosis()
        else:
            diagnosis = _make_partial_diagnosis()

        with patch("src.orchestration.steward._llm_prescribe_v2", side_effect=_stub_llm_prescribe_v2):
            p = generate_v2_prescription(
                uow=uow,
                diagnosis=diagnosis,
                issue_info=None,
                cycles=cycles,
                registry=registry,
                dry_run=True,
            )

        assert p.audit_metadata["cycle"] == p.diagnosis["prior_cycle_count"], (
            f"cycle mismatch at cycles={cycles}: "
            f"audit={p.audit_metadata['cycle']} != diagnosis={p.diagnosis['prior_cycle_count']}"
        )


def test_v2_prescription_with_issue_info(db_path, registry):
    """generate_v2_prescription passes issue body to _llm_prescribe_v2 when issue_info present."""
    uow = _make_uow(db_path, steward_cycles=0, success_criteria=None)
    diagnosis = _make_first_execution_diagnosis()
    issue_info = IssueInfo(
        status_code=200,
        state="open",
        labels=[],
        body="Implement the --dry-run flag",
        title="Add --dry-run flag",
    )

    captured_issue_bodies: list[str] = []

    def _capturing_llm_v2(uow, diagnosis_section, executor_posture, selected_executor_type,
                          issue_body, vision_orientation, dan_register, prescribed_skills,
                          cycles, now_iso):
        captured_issue_bodies.append(issue_body)
        return _stub_llm_prescribe_v2(
            uow, diagnosis_section, executor_posture, selected_executor_type,
            issue_body, vision_orientation, dan_register, prescribed_skills, cycles, now_iso,
        )

    with patch("src.orchestration.steward._llm_prescribe_v2", side_effect=_capturing_llm_v2):
        generate_v2_prescription(
            uow=uow,
            diagnosis=diagnosis,
            issue_info=issue_info,
            cycles=0,
            registry=registry,
            dry_run=True,
        )

    assert captured_issue_bodies
    assert "dry-run" in captured_issue_bodies[0]
