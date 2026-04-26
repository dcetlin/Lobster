"""
Tests for the Steward's orphan trace classification (Issue #964).

When a UoW is orphaned (executor_orphan / executing_orphan / diagnosing_orphan),
the Steward should read trace.json and classify the kill type so the prescriber
has evidence rather than diagnosing blind.

Three kill classes:
- kill_before_start: agent dispatched, session ended before any execution work
- kill_during_execution: agent was killed mid-work (has surprises/prescription_delta)
- completed_without_output: trace says complete but result.json / write_result absent

Coverage:
- _classify_orphan_from_trace: all three classification branches
- _classify_orphan_from_trace: absent trace falls back to kill_before_start
- _classify_orphan_from_trace: result.json present → completed_without_output
- _enrich_orphan_completion_rationale: returns bare rationale when no trace
- _enrich_orphan_completion_rationale: enriches rationale with classification
- _enrich_orphan_completion_rationale: includes execution_summary when present
- _enrich_orphan_completion_rationale: includes surprises when present
- _diagnose_uow: executor_orphan rationale includes trace data when trace exists
- _diagnose_uow: executing_orphan rationale includes trace data when trace exists
- _diagnose_uow: non-orphan postures are not affected
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from orchestration.steward import (
    _classify_orphan_from_trace,
    _enrich_orphan_completion_rationale,
    _read_trace_json,
)


# ---------------------------------------------------------------------------
# Named constants (spec-derived)
# ---------------------------------------------------------------------------

KILL_BEFORE_START = "kill_before_start"
KILL_DURING_EXECUTION = "kill_during_execution"
COMPLETED_WITHOUT_OUTPUT = "completed_without_output"


# ---------------------------------------------------------------------------
# _classify_orphan_from_trace unit tests
# ---------------------------------------------------------------------------

class TestClassifyOrphanFromTrace:
    """Pure function — all inputs via args, no DB or file system side effects
    (except the output_ref path check for result.json)."""

    def test_absent_trace_returns_kill_before_start(self, tmp_path: Path) -> None:
        """When trace.json is absent, default to kill_before_start."""
        output_ref = str(tmp_path / "uow_absent.json")
        result = _classify_orphan_from_trace(trace_data=None, output_ref=output_ref)
        assert result == KILL_BEFORE_START

    def test_dispatch_only_summary_empty_surprises_is_kill_before_start(self, tmp_path: Path) -> None:
        """Dispatch-only execution_summary + empty surprises → kill_before_start."""
        output_ref = str(tmp_path / "uow_dispatch.json")
        trace = {
            "uow_id": "uow_dispatch",
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc123, subprocess exit 0.",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        result = _classify_orphan_from_trace(trace_data=trace, output_ref=output_ref)
        assert result == KILL_BEFORE_START

    def test_nonempty_surprises_is_kill_during_execution(self, tmp_path: Path) -> None:
        """Non-empty surprises list → kill_during_execution."""
        output_ref = str(tmp_path / "uow_surprises.json")
        trace = {
            "uow_id": "uow_surprises",
            "register": "operational",
            "execution_summary": "Executor dispatched subagent xyz, session ended mid-work.",
            "surprises": ["ran out of context", "incomplete test run"],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        result = _classify_orphan_from_trace(trace_data=trace, output_ref=output_ref)
        assert result == KILL_DURING_EXECUTION

    def test_nonempty_prescription_delta_is_kill_during_execution(self, tmp_path: Path) -> None:
        """Non-empty prescription_delta → kill_during_execution even if surprises empty."""
        output_ref = str(tmp_path / "uow_delta.json")
        trace = {
            "uow_id": "uow_delta",
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc.",
            "surprises": [],
            "prescription_delta": "gate command needs --no-header flag",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        result = _classify_orphan_from_trace(trace_data=trace, output_ref=output_ref)
        assert result == KILL_DURING_EXECUTION

    def test_result_json_present_is_completed_without_output(self, tmp_path: Path) -> None:
        """result.json exists → completed_without_output (agent ran, write_result never called)."""
        output_ref = str(tmp_path / "uow_result.json")
        # Write a result.json file to simulate a completed executor
        result_path = Path(output_ref).with_suffix(".result.json")
        result_path.write_text(json.dumps({
            "uow_id": "uow_result",
            "outcome": "complete",
            "success": True,
        }), encoding="utf-8")

        trace = {
            "uow_id": "uow_result",
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc.",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        result = _classify_orphan_from_trace(trace_data=trace, output_ref=output_ref)
        assert result == COMPLETED_WITHOUT_OUTPUT

    def test_result_json_present_overrides_surprises(self, tmp_path: Path) -> None:
        """result.json existence takes priority over surprises (agent completed its work)."""
        output_ref = str(tmp_path / "uow_result_surprises.json")
        result_path = Path(output_ref).with_suffix(".result.json")
        result_path.write_text(json.dumps({"uow_id": "uow_result_surprises", "outcome": "complete", "success": True}))

        trace = {
            "uow_id": "uow_result_surprises",
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc.",
            "surprises": ["some surprise"],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        result = _classify_orphan_from_trace(trace_data=trace, output_ref=output_ref)
        assert result == COMPLETED_WITHOUT_OUTPUT

    def test_empty_execution_summary_no_surprises_is_kill_before_start(self, tmp_path: Path) -> None:
        """Empty execution_summary with no other signals → kill_before_start."""
        output_ref = str(tmp_path / "uow_empty.json")
        trace = {
            "uow_id": "uow_empty",
            "register": "operational",
            "execution_summary": "",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        result = _classify_orphan_from_trace(trace_data=trace, output_ref=output_ref)
        assert result == KILL_BEFORE_START

    def test_output_ref_none_falls_back_to_kill_before_start(self) -> None:
        """output_ref=None means no result.json check possible → kill_before_start."""
        trace = {
            "uow_id": "uow_none_ref",
            "execution_summary": "Executor dispatched subagent abc.",
            "surprises": [],
            "prescription_delta": "",
        }
        result = _classify_orphan_from_trace(trace_data=trace, output_ref=None)
        assert result == KILL_BEFORE_START


# ---------------------------------------------------------------------------
# _enrich_orphan_completion_rationale unit tests
# ---------------------------------------------------------------------------

class TestEnrichOrphanCompletionRationale:
    """Enrichment function reads trace.json and returns enriched rationale string."""

    def test_no_trace_returns_bare_rationale(self, tmp_path: Path) -> None:
        """When trace.json is absent, returns the base rationale unchanged (no suffix)."""
        output_ref = str(tmp_path / "uow_notrace.json")
        # No trace.json file — just the output_ref without a trace file
        base = "re-entry posture is 'executor_orphan' — not a normal completion"
        result = _enrich_orphan_completion_rationale(
            base_rationale=base,
            output_ref=output_ref,
            uow_id="uow_notrace",
        )
        assert result == base

    def test_trace_present_appends_classification(self, tmp_path: Path) -> None:
        """When trace.json exists, result includes orphan_classification."""
        output_ref = str(tmp_path / "uow_classified.json")
        trace_path = Path(output_ref).with_suffix(".trace.json")
        trace = {
            "uow_id": "uow_classified",
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc123, subprocess exit 0.",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        trace_path.write_text(json.dumps(trace))

        result = _enrich_orphan_completion_rationale(
            base_rationale="base orphan rationale",
            output_ref=output_ref,
            uow_id="uow_classified",
        )
        assert "kill_before_start" in result

    def test_trace_with_surprises_appears_in_rationale(self, tmp_path: Path) -> None:
        """Surprises from trace are included in the enriched rationale."""
        output_ref = str(tmp_path / "uow_surprises_enrich.json")
        trace_path = Path(output_ref).with_suffix(".trace.json")
        trace = {
            "uow_id": "uow_surprises_enrich",
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc.",
            "surprises": ["ran out of context window"],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        trace_path.write_text(json.dumps(trace))

        result = _enrich_orphan_completion_rationale(
            base_rationale="base",
            output_ref=output_ref,
            uow_id="uow_surprises_enrich",
        )
        assert "kill_during_execution" in result
        assert "ran out of context window" in result

    def test_trace_execution_summary_appears_in_rationale(self, tmp_path: Path) -> None:
        """execution_summary from trace is included in the enriched rationale."""
        output_ref = str(tmp_path / "uow_summary_enrich.json")
        trace_path = Path(output_ref).with_suffix(".trace.json")
        exec_summary = "Executor dispatched subagent xyz-9f3, subprocess exit 0."
        trace = {
            "uow_id": "uow_summary_enrich",
            "register": "operational",
            "execution_summary": exec_summary,
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        trace_path.write_text(json.dumps(trace))

        result = _enrich_orphan_completion_rationale(
            base_rationale="base",
            output_ref=output_ref,
            uow_id="uow_summary_enrich",
        )
        # execution_summary should appear in the enriched string
        assert exec_summary in result

    def test_completed_without_output_classification_appears(self, tmp_path: Path) -> None:
        """result.json present → completed_without_output label appears in rationale."""
        output_ref = str(tmp_path / "uow_cwout.json")
        trace_path = Path(output_ref).with_suffix(".trace.json")
        result_path = Path(output_ref).with_suffix(".result.json")
        result_path.write_text(json.dumps({"uow_id": "uow_cwout", "outcome": "complete", "success": True}))
        trace = {
            "uow_id": "uow_cwout",
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc.",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        trace_path.write_text(json.dumps(trace))

        result = _enrich_orphan_completion_rationale(
            base_rationale="base",
            output_ref=output_ref,
            uow_id="uow_cwout",
        )
        assert "completed_without_output" in result

    def test_mismatched_uow_id_trace_returns_bare_rationale(self, tmp_path: Path) -> None:
        """When trace.json has a different uow_id, returns bare rationale (security guard)."""
        output_ref = str(tmp_path / "uow_mismatch.json")
        trace_path = Path(output_ref).with_suffix(".trace.json")
        trace = {
            "uow_id": "uow_OTHER",  # mismatch
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc.",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        trace_path.write_text(json.dumps(trace))

        base = "base orphan rationale"
        result = _enrich_orphan_completion_rationale(
            base_rationale=base,
            output_ref=output_ref,
            uow_id="uow_mismatch",  # doesn't match trace
        )
        assert result == base


# ---------------------------------------------------------------------------
# _diagnose_uow integration — orphan postures get enriched completion_rationale
# ---------------------------------------------------------------------------

class TestDiagnoseUowOrphanEnrichment:
    """_diagnose_uow enriches completion_rationale with trace data for orphan postures."""

    def _make_registry(self, db_path: Path):
        from orchestration.registry import Registry
        return Registry(db_path)

    def _insert_uow(
        self,
        db_path: Path,
        uow_id: str,
        status: str = "ready-for-steward",
        output_ref: str | None = None,
        return_reason: str = "executor_orphan",
        register: str = "operational",
    ) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                """
                INSERT INTO uow_registry (
                    id, type, source, status, posture, created_at, updated_at,
                    summary, success_criteria, output_ref, register
                ) VALUES (?, 'executable', 'test', ?, 'solo', ?, ?, 'Test UoW', 'done', ?, ?)
                """,
                (uow_id, status, now, now, output_ref, register),
            )
            if return_reason and output_ref is not None:
                conn.execute(
                    """
                    INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                    VALUES (?, ?, 'startup_sweep', 'active', 'failed', 'steward', ?)
                    """,
                    (
                        now,
                        uow_id,
                        json.dumps({
                            "classification": return_reason,
                            "output_ref": output_ref,
                        }),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def test_executor_orphan_with_trace_has_enriched_rationale(
        self, tmp_path: Path
    ) -> None:
        """executor_orphan posture: completion_rationale contains kill_before_start."""
        from orchestration.steward import _diagnose_uow
        from orchestration.registry import Registry

        db_path = tmp_path / "test.db"
        registry = Registry(db_path)

        uow_id = "uow_orphan_trace_001"
        output_ref = str(tmp_path / f"{uow_id}.json")

        # Write trace.json alongside output_ref
        trace_path = Path(output_ref).with_suffix(".trace.json")
        trace = {
            "uow_id": uow_id,
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc123, subprocess exit 0.",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        trace_path.write_text(json.dumps(trace))

        self._insert_uow(
            db_path, uow_id,
            output_ref=output_ref,
            return_reason="executor_orphan",
        )

        uow = registry.get(uow_id)
        assert uow is not None

        # Build audit entries that produce executor_orphan posture
        audit_entries = [
            {
                "event": "startup_sweep",
                "note": json.dumps({"classification": "executor_orphan", "output_ref": output_ref}),
            }
        ]

        diagnosis = _diagnose_uow(uow, audit_entries, issue_info=None)

        assert "kill_before_start" in diagnosis.completion_rationale

    def test_executing_orphan_with_surprises_trace_has_kill_during_execution(
        self, tmp_path: Path
    ) -> None:
        """executing_orphan posture: surprises in trace → kill_during_execution in rationale."""
        from orchestration.steward import _diagnose_uow
        from orchestration.registry import Registry

        db_path = tmp_path / "test2.db"
        registry = Registry(db_path)

        uow_id = "uow_orphan_trace_002"
        output_ref = str(tmp_path / f"{uow_id}.json")

        trace_path = Path(output_ref).with_suffix(".trace.json")
        trace = {
            "uow_id": uow_id,
            "register": "operational",
            "execution_summary": "Executor dispatched subagent xyz, session ended.",
            "surprises": ["context compaction occurred mid-work"],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        trace_path.write_text(json.dumps(trace))

        self._insert_uow(
            db_path, uow_id,
            output_ref=output_ref,
            return_reason="executing_orphan",
        )

        uow = registry.get(uow_id)
        assert uow is not None

        audit_entries = [
            {
                "event": "startup_sweep",
                "note": json.dumps({"classification": "executing_orphan", "output_ref": output_ref}),
            }
        ]

        diagnosis = _diagnose_uow(uow, audit_entries, issue_info=None)

        assert "kill_during_execution" in diagnosis.completion_rationale
        assert "context compaction occurred mid-work" in diagnosis.completion_rationale

    def test_diagnosing_orphan_with_trace_has_enriched_rationale(
        self, tmp_path: Path
    ) -> None:
        """diagnosing_orphan posture: trace data enriches completion_rationale."""
        from orchestration.steward import _diagnose_uow
        from orchestration.registry import Registry

        db_path = tmp_path / "test3.db"
        registry = Registry(db_path)

        uow_id = "uow_orphan_trace_003"
        output_ref = str(tmp_path / f"{uow_id}.json")

        trace_path = Path(output_ref).with_suffix(".trace.json")
        trace = {
            "uow_id": uow_id,
            "register": "operational",
            "execution_summary": "Executor dispatched subagent abc123, subprocess exit 0.",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": "2026-04-26T10:00:00+00:00",
        }
        trace_path.write_text(json.dumps(trace))

        self._insert_uow(
            db_path, uow_id,
            output_ref=output_ref,
            return_reason="diagnosing_orphan",
        )

        uow = registry.get(uow_id)
        assert uow is not None

        audit_entries = [
            {
                "event": "startup_sweep",
                "note": json.dumps({"classification": "diagnosing_orphan", "output_ref": output_ref}),
            }
        ]

        diagnosis = _diagnose_uow(uow, audit_entries, issue_info=None)

        # Should have trace data in the rationale
        assert "kill_before_start" in diagnosis.completion_rationale

    def test_non_orphan_posture_unaffected_by_trace_enrichment(
        self, tmp_path: Path
    ) -> None:
        """Normal execution_complete posture: trace enrichment does not apply."""
        from orchestration.steward import _diagnose_uow
        from orchestration.registry import Registry

        db_path = tmp_path / "test4.db"
        registry = Registry(db_path)

        uow_id = "uow_non_orphan_001"
        output_ref = str(tmp_path / f"{uow_id}.json")

        # Write result.json and output to simulate a completed execution
        result_path = Path(output_ref).with_suffix(".result.json")
        result_path.write_text(json.dumps({
            "uow_id": uow_id,
            "outcome": "complete",
            "success": True,
        }))
        Path(output_ref).write_text("execution output here", encoding="utf-8")

        self._insert_uow(
            db_path, uow_id,
            output_ref=output_ref,
            return_reason="execution_complete",
        )

        uow = registry.get(uow_id)
        assert uow is not None

        audit_entries = [
            {"event": "execution_complete", "note": json.dumps({"uow_id": uow_id})},
        ]

        diagnosis = _diagnose_uow(uow, audit_entries, issue_info=None)

        # Normal completion path — orphan enrichment terms should NOT appear
        assert "kill_before_start" not in diagnosis.completion_rationale
        assert "kill_during_execution" not in diagnosis.completion_rationale
        assert "completed_without_output" not in diagnosis.completion_rationale

    def test_orphan_with_no_trace_returns_unenriched_rationale(
        self, tmp_path: Path
    ) -> None:
        """executor_orphan with no trace.json: rationale is the bare posture string."""
        from orchestration.steward import _diagnose_uow
        from orchestration.registry import Registry

        db_path = tmp_path / "test5.db"
        registry = Registry(db_path)

        uow_id = "uow_orphan_notrace_001"
        # No output_ref, no trace.json
        self._insert_uow(
            db_path, uow_id,
            output_ref=None,
            return_reason="executor_orphan",
        )

        uow = registry.get(uow_id)
        assert uow is not None

        audit_entries = [
            {
                "event": "startup_sweep",
                "note": json.dumps({"classification": "executor_orphan"}),
            }
        ]

        diagnosis = _diagnose_uow(uow, audit_entries, issue_info=None)

        # No trace → no enrichment, but rationale exists and mentions the posture
        assert "kill_before_start" not in diagnosis.completion_rationale
        assert "kill_during_execution" not in diagnosis.completion_rationale
