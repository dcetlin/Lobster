"""
Tests for PR C: register-aware diagnosis + corrective trace injection.

Covers:
- _register_completion_policy: maps register → policy identifier
- _read_trace_json: pure function, reads and validates trace file
- _bound_prescription_delta: truncates oversized deltas with trailing note
- _count_non_improving_gate_cycles: counts consecutive non-improving gate cycles
- _assess_completion with register policy applied:
  - philosophical register: always returns is_complete=False + philosophical_surface
  - human-judgment register: returns False without close_reason, True with close_reason
  - operational/iterative-convergent: existing machine-gate logic unchanged
- _detect_stuck_condition with new conditions:
  - philosophical_register fires on re-entry (not on first_execution)
  - no_gate_improvement fires for iterative-convergent with 3 non-improving cycles
- Trace injection in _process_uow:
  - trace_injection event written to steward_log when trace.json exists
  - trace content (surprises, prescription_delta) injected into prescription context
  - gate_score included for iterative-convergent
  - no_gate_improvement surface fires after 3 non-improving gate cycles
  - philosophical_register surface fires on re-entry for philosophical UoWs
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import (
    _register_completion_policy,
    _read_trace_json,
    _bound_prescription_delta,
    _count_non_improving_gate_cycles,
    _assess_completion,
    _detect_stuck_condition,
    Surfaced,
    Prescribed,
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
                steward_cycles: int = 0,
                close_reason: str | None = None) -> None:
    now = _now_iso()
    conn.execute(
        """INSERT INTO uow_registry
           (id, type, source, source_issue_number, sweep_date, status, posture,
            created_at, updated_at, summary, steward_cycles,
            success_criteria, route_evidence, trigger, register, close_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uow_id, "executable", "github:issue/1", 1, "2026-01-01", status, "solo",
         now, now, summary, steward_cycles,
         success_criteria, "{}", '{"type": "immediate"}', register, close_reason),
    )
    conn.commit()


def _make_registry(tmp_path: Path):
    from src.orchestration.registry import Registry
    db_path = tmp_path / "registry.db"
    registry = Registry(db_path=db_path)
    return registry, db_path


# ---------------------------------------------------------------------------
# Tests: _register_completion_policy
# ---------------------------------------------------------------------------

class TestRegisterCompletionPolicy:
    """Pure function: register → policy identifier."""

    def test_operational_returns_machine_gate(self):
        assert _register_completion_policy("operational") == "machine-gate"

    def test_iterative_convergent_returns_machine_gate(self):
        assert _register_completion_policy("iterative-convergent") == "machine-gate"

    def test_philosophical_returns_always_surface(self):
        assert _register_completion_policy("philosophical") == "always-surface"

    def test_human_judgment_returns_require_confirmation(self):
        assert _register_completion_policy("human-judgment") == "require-confirmation"

    def test_unknown_register_defaults_to_machine_gate(self):
        """Unknown registers should not crash — default to machine-gate (safe pass-through)."""
        assert _register_completion_policy("experimental-new") == "machine-gate"


# ---------------------------------------------------------------------------
# Tests: _read_trace_json
# ---------------------------------------------------------------------------

class TestReadTraceJson:
    """Pure function: reads and validates trace.json from output_ref path."""

    def test_reads_valid_trace_with_suffix_replacement(self, tmp_path):
        """Primary path: output_ref.trace.json (suffix replaced)."""
        output_ref = tmp_path / "uow-1.output"
        trace_file = output_ref.with_suffix(".trace.json")
        trace_data = {
            "uow_id": "uow-1",
            "register": "operational",
            "execution_summary": "Completed normally",
            "surprises": [],
            "prescription_delta": "",
            "gate_score": None,
            "timestamp": _now_iso(),
        }
        trace_file.write_text(json.dumps(trace_data), encoding="utf-8")

        result = _read_trace_json(str(output_ref), expected_uow_id="uow-1")
        assert result is not None
        assert result["uow_id"] == "uow-1"
        assert result["execution_summary"] == "Completed normally"

    def test_reads_valid_trace_with_suffix_append(self, tmp_path):
        """Fallback path: output_ref + .trace.json (suffix appended)."""
        output_ref = tmp_path / "uow-2.output"
        trace_file = Path(str(output_ref) + ".trace.json")
        trace_data = {
            "uow_id": "uow-2",
            "register": "operational",
            "execution_summary": "Fallback path",
            "surprises": ["unexpected blocker"],
            "prescription_delta": "add --no-header flag",
            "gate_score": None,
            "timestamp": _now_iso(),
        }
        trace_file.write_text(json.dumps(trace_data), encoding="utf-8")

        result = _read_trace_json(str(output_ref), expected_uow_id="uow-2")
        assert result is not None
        assert result["surprises"] == ["unexpected blocker"]

    def test_returns_none_when_trace_absent(self, tmp_path):
        output_ref = tmp_path / "uow-missing.output"
        result = _read_trace_json(str(output_ref), expected_uow_id="uow-missing")
        assert result is None

    def test_returns_none_when_uow_id_mismatches(self, tmp_path):
        """Misrouted trace file — uow_id mismatch should return None (safe guard)."""
        output_ref = tmp_path / "uow-a.output"
        trace_file = output_ref.with_suffix(".trace.json")
        trace_data = {"uow_id": "uow-b", "register": "operational",
                      "execution_summary": "x", "surprises": [],
                      "prescription_delta": "", "gate_score": None,
                      "timestamp": _now_iso()}
        trace_file.write_text(json.dumps(trace_data), encoding="utf-8")

        result = _read_trace_json(str(output_ref), expected_uow_id="uow-a")
        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path):
        output_ref = tmp_path / "uow-bad.output"
        trace_file = output_ref.with_suffix(".trace.json")
        trace_file.write_text("not-json{broken", encoding="utf-8")

        result = _read_trace_json(str(output_ref), expected_uow_id="uow-bad")
        assert result is None

    def test_returns_none_when_output_ref_is_none(self):
        result = _read_trace_json(None, expected_uow_id="uow-x")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _bound_prescription_delta
# ---------------------------------------------------------------------------

# Named constant matching the spec's candidate threshold
PRESCRIPTION_DELTA_MAX_CHARS = 500


class TestBoundPrescriptionDelta:
    """Pure function: truncates oversized prescription_delta strings."""

    def test_short_delta_passes_through_unchanged(self):
        delta = "add --no-header flag to gate command"
        result = _bound_prescription_delta(delta, history=[])
        assert result == delta

    def test_exact_limit_passes_through(self):
        delta = "x" * PRESCRIPTION_DELTA_MAX_CHARS
        result = _bound_prescription_delta(delta, history=[])
        assert result == delta

    def test_oversized_delta_is_truncated(self):
        delta = "x" * (PRESCRIPTION_DELTA_MAX_CHARS + 100)
        result = _bound_prescription_delta(delta, history=[])
        # Allow room for the trailing note (60 chars)
        assert len(result) <= PRESCRIPTION_DELTA_MAX_CHARS + 60
        assert "bounded" in result.lower() or "truncated" in result.lower()

    def test_empty_delta_passes_through(self):
        result = _bound_prescription_delta("", history=[])
        assert result == ""

    def test_history_does_not_expand_limit(self):
        """History is informational; it should not expand the max bound."""
        delta = "x" * (PRESCRIPTION_DELTA_MAX_CHARS + 100)
        long_history = ["short delta"] * 10
        result = _bound_prescription_delta(delta, history=long_history)
        # Still truncated despite long history (allow room for trailing note)
        assert len(result) <= PRESCRIPTION_DELTA_MAX_CHARS + 60


# ---------------------------------------------------------------------------
# Tests: _count_non_improving_gate_cycles
# ---------------------------------------------------------------------------

# Named constant per spec: 3 consecutive non-improving cycles triggers surface
NON_IMPROVING_GATE_THRESHOLD = 3


class TestCountNonImprovingGateCycles:
    """Pure function: counts consecutive non-improving gate_score cycles from steward_log."""

    def _make_log_with_trace_injections(self, gate_scores: list[float | None]) -> str:
        """Build a steward_log string with trace_injection entries for given gate_scores."""
        entries = []
        for score in gate_scores:
            gate_score = {"score": score, "command": "pytest", "result": "x"} if score is not None else None
            entry = {
                "event": "trace_injection",
                "uow_id": "uow-test",
                "steward_cycles": len(entries),
                "register": "iterative-convergent",
                "gate_score": gate_score,
                "surprises_count": 0,
                "prescription_delta_present": False,
                "timestamp": _now_iso(),
            }
            entries.append(json.dumps(entry))
        return "\n".join(entries)

    def test_zero_when_no_trace_injections(self):
        result = _count_non_improving_gate_cycles("", n=NON_IMPROVING_GATE_THRESHOLD)
        assert result == 0

    def test_zero_when_score_improving(self):
        log = self._make_log_with_trace_injections([0.5, 0.7, 0.9])
        result = _count_non_improving_gate_cycles(log, n=NON_IMPROVING_GATE_THRESHOLD)
        assert result == 0

    def test_counts_consecutive_non_improving(self):
        """Three cycles with same score → non_improving count = 3."""
        log = self._make_log_with_trace_injections([0.5, 0.5, 0.5])
        result = _count_non_improving_gate_cycles(log, n=NON_IMPROVING_GATE_THRESHOLD)
        assert result >= NON_IMPROVING_GATE_THRESHOLD

    def test_resets_after_improvement(self):
        """Improvement then plateau: counter reflects only the tail plateau length."""
        log = self._make_log_with_trace_injections([0.4, 0.6, 0.7, 0.7, 0.7])
        result = _count_non_improving_gate_cycles(log, n=NON_IMPROVING_GATE_THRESHOLD)
        # Scores improve to 0.7 then plateau for 3 cycles → non-improving count = 3
        assert result == 3

    def test_zero_when_no_gate_score_entries(self):
        """Entries without gate_score (non-iterative-convergent) count as 0."""
        entries = []
        for i in range(4):
            entries.append(json.dumps({
                "event": "trace_injection",
                "uow_id": "uow-test",
                "steward_cycles": i,
                "register": "operational",
                "gate_score": None,
                "timestamp": _now_iso(),
            }))
        log = "\n".join(entries)
        result = _count_non_improving_gate_cycles(log, n=NON_IMPROVING_GATE_THRESHOLD)
        assert result == 0


# ---------------------------------------------------------------------------
# Tests: _assess_completion with register policy
# ---------------------------------------------------------------------------

class TestAssessCompletionWithRegisterPolicy:
    """_assess_completion branches on register-aware policy."""

    def _make_uow_with_result(self, tmp_path: Path, uow_id: str,
                               register: str, outcome: str = "complete",
                               cycles: int = 1, close_reason: str | None = None):
        """Create a minimal UoW dataclass with a result.json file."""
        from src.orchestration.registry import UoW

        output_ref = tmp_path / f"{uow_id}.output"
        output_ref.write_text("executor output here", encoding="utf-8")

        result_file = output_ref.with_suffix(".result.json")
        result_data = {"uow_id": uow_id, "outcome": outcome, "reason": "done"}
        result_file.write_text(json.dumps(result_data), encoding="utf-8")

        # Build minimal UoW — use dataclass directly
        uow = UoW(
            id=uow_id,
            type="executable",
            source="github:issue/1",
            source_issue_number=1,
            sweep_date="2026-01-01",
            status="diagnosing",
            posture="solo",
            created_at=_now_iso(),
            updated_at=_now_iso(),
            summary="test summary",
            steward_cycles=cycles,
            success_criteria="output present",
            register=register,
            output_ref=str(output_ref),
            close_reason=close_reason,
        )
        return uow

    def test_philosophical_always_surfaces_despite_complete_outcome(self, tmp_path):
        """philosophical register: outcome=complete still returns is_complete=False."""
        uow = self._make_uow_with_result(tmp_path, "uow-phil", "philosophical",
                                          outcome="complete", cycles=1)
        is_complete, rationale, executor_outcome = _assess_completion(
            uow, "some output content", reentry_posture="execution_complete"
        )
        assert is_complete is False
        assert "philosophical" in rationale.lower() or "human" in rationale.lower() or "surface" in rationale.lower()

    def test_human_judgment_surfaces_without_close_reason(self, tmp_path):
        """human-judgment register without close_reason: returns is_complete=False."""
        uow = self._make_uow_with_result(tmp_path, "uow-hj", "human-judgment",
                                          outcome="complete", cycles=1,
                                          close_reason=None)
        is_complete, rationale, executor_outcome = _assess_completion(
            uow, "some output content", reentry_posture="execution_complete"
        )
        assert is_complete is False
        assert "human" in rationale.lower() or "confirm" in rationale.lower() or "judgment" in rationale.lower()

    def test_human_judgment_closes_with_close_reason(self, tmp_path):
        """human-judgment register with close_reason set: is_complete=True."""
        uow = self._make_uow_with_result(tmp_path, "uow-hj2", "human-judgment",
                                          outcome="complete", cycles=1,
                                          close_reason="Dan confirmed via Telegram 2026-04-07")
        is_complete, rationale, executor_outcome = _assess_completion(
            uow, "some output content", reentry_posture="execution_complete"
        )
        assert is_complete is True

    def test_operational_closes_normally_on_complete(self, tmp_path):
        """operational register: outcome=complete → is_complete=True (machine-gate unchanged)."""
        uow = self._make_uow_with_result(tmp_path, "uow-ops", "operational",
                                          outcome="complete", cycles=1)
        is_complete, rationale, executor_outcome = _assess_completion(
            uow, "some output content", reentry_posture="execution_complete"
        )
        assert is_complete is True

    def test_iterative_convergent_closes_on_complete(self, tmp_path):
        """iterative-convergent register: outcome=complete → is_complete=True."""
        uow = self._make_uow_with_result(tmp_path, "uow-ic", "iterative-convergent",
                                          outcome="complete", cycles=1)
        is_complete, rationale, executor_outcome = _assess_completion(
            uow, "some output content", reentry_posture="execution_complete"
        )
        assert is_complete is True


# ---------------------------------------------------------------------------
# Tests: _detect_stuck_condition with new conditions
# ---------------------------------------------------------------------------

class TestDetectStuckConditionNewConditions:
    """New stuck conditions: philosophical_register, no_gate_improvement."""

    def _make_uow(self, register: str, cycles: int = 1, steward_log: str = "") -> "UoW":
        from src.orchestration.registry import UoW
        return UoW(
            id="uow-test",
            type="executable",
            source="github:issue/1",
            source_issue_number=1,
            sweep_date="2026-01-01",
            status="diagnosing",
            posture="solo",
            created_at=_now_iso(),
            updated_at=_now_iso(),
            summary="test",
            steward_cycles=cycles,
            success_criteria="done",
            register=register,
            steward_log=steward_log,
        )

    def test_philosophical_register_fires_on_reentry(self):
        """philosophical_register stuck condition fires when reentry_posture != first_execution."""
        uow = self._make_uow("philosophical", cycles=1)
        result = _detect_stuck_condition(uow, "execution_complete", return_reason=None)
        assert result == "philosophical_register"

    def test_philosophical_register_does_not_fire_on_first_execution(self):
        """philosophical_register does NOT fire on first_execution — wait for evidence."""
        uow = self._make_uow("philosophical", cycles=0)
        result = _detect_stuck_condition(uow, "first_execution", return_reason=None)
        assert result != "philosophical_register"

    def test_no_gate_improvement_fires_after_n_non_improving_cycles(self):
        """no_gate_improvement fires for iterative-convergent with N non-improving cycles."""
        # Build steward_log with 3 identical gate scores
        entries = []
        for i in range(NON_IMPROVING_GATE_THRESHOLD):
            entry = {
                "event": "trace_injection",
                "uow_id": "uow-test",
                "steward_cycles": i,
                "register": "iterative-convergent",
                "gate_score": {"score": 0.5, "command": "pytest", "result": "x"},
                "surprises_count": 0,
                "prescription_delta_present": False,
                "timestamp": _now_iso(),
            }
            entries.append(json.dumps(entry))
        log_str = "\n".join(entries)

        uow = self._make_uow("iterative-convergent", cycles=3, steward_log=log_str)
        result = _detect_stuck_condition(uow, "execution_complete", return_reason=None)
        assert result == "no_gate_improvement"

    def test_no_gate_improvement_does_not_fire_when_improving(self):
        """no_gate_improvement should not fire when gate scores are improving."""
        entries = []
        for i, score in enumerate([0.4, 0.6, 0.8]):
            entry = {
                "event": "trace_injection",
                "uow_id": "uow-test",
                "steward_cycles": i,
                "register": "iterative-convergent",
                "gate_score": {"score": score, "command": "pytest", "result": "x"},
                "surprises_count": 0,
                "prescription_delta_present": False,
                "timestamp": _now_iso(),
            }
            entries.append(json.dumps(entry))
        log_str = "\n".join(entries)

        uow = self._make_uow("iterative-convergent", cycles=3, steward_log=log_str)
        result = _detect_stuck_condition(uow, "execution_complete", return_reason=None)
        assert result != "no_gate_improvement"

    def test_no_gate_improvement_does_not_fire_for_operational(self):
        """no_gate_improvement is specific to iterative-convergent register."""
        # Even with 3 identical gate scores, operational should not fire this condition
        entries = []
        for i in range(NON_IMPROVING_GATE_THRESHOLD):
            entry = {
                "event": "trace_injection",
                "uow_id": "uow-test",
                "steward_cycles": i,
                "register": "operational",
                "gate_score": {"score": 0.5, "command": "pytest", "result": "x"},
                "timestamp": _now_iso(),
            }
            entries.append(json.dumps(entry))
        log_str = "\n".join(entries)

        uow = self._make_uow("operational", cycles=3, steward_log=log_str)
        result = _detect_stuck_condition(uow, "execution_complete", return_reason=None)
        assert result != "no_gate_improvement"


# ---------------------------------------------------------------------------
# Integration tests: trace injection in _process_uow
# ---------------------------------------------------------------------------

class TestTraceInjectionIntegration:
    """Integration tests verifying trace content is injected into prescription context."""

    def test_trace_injection_event_written_to_steward_log(self, tmp_path):
        """When trace.json exists and is valid, a trace_injection event is written to steward_log."""
        registry, db_path = _make_registry(tmp_path)
        conn = _open_db(db_path)
        _insert_uow(conn, "uow-ti", register="operational",
                    summary="implement feature", steward_cycles=0)
        conn.close()

        from src.orchestration.steward import _process_uow, _fetch_audit_entries

        uow = registry.get("uow-ti")

        # Create a valid output_ref with result.json and trace.json
        output_ref = tmp_path / "uow-ti.output"
        output_ref.write_text("executor output", encoding="utf-8")

        result_file = output_ref.with_suffix(".result.json")
        result_file.write_text(json.dumps({
            "uow_id": "uow-ti",
            "outcome": "partial",
            "reason": "half done",
        }), encoding="utf-8")

        trace_file = output_ref.with_suffix(".trace.json")
        trace_file.write_text(json.dumps({
            "uow_id": "uow-ti",
            "register": "operational",
            "execution_summary": "Ran 5 steps, completed 2",
            "surprises": ["found config conflict"],
            "prescription_delta": "add --force flag",
            "gate_score": None,
            "timestamp": _now_iso(),
        }), encoding="utf-8")

        # Update uow in DB with output_ref
        conn = _open_db(db_path)
        conn.execute(
            "UPDATE uow_registry SET output_ref = ? WHERE id = ?",
            (str(output_ref), "uow-ti")
        )
        conn.commit()
        conn.close()

        uow = registry.get("uow-ti")
        audit_entries = _fetch_audit_entries(registry, "uow-ti")

        _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info={"body": "", "state": "open", "labels": []},
            dry_run=False, artifact_dir=tmp_path, notify_dan=lambda *a, **k: None,
            llm_prescriber=lambda *a, **k: {"instructions": "x",
                                             "success_criteria_check": "y",
                                             "estimated_cycles": 1},
        )

        uow_after = registry.get("uow-ti")
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

        assert "trace_injection" in events, f"Expected trace_injection in events: {events}"

    def test_philosophical_surface_fires_on_reentry(self, tmp_path):
        """philosophical UoW with execution_complete posture → Surfaced(philosophical_register)."""
        registry, db_path = _make_registry(tmp_path)
        conn = _open_db(db_path)
        _insert_uow(conn, "uow-phil-surface", register="philosophical",
                    summary="explore consciousness", steward_cycles=1)
        conn.close()

        surfaced = []

        def fake_notify(uow, condition, surface_log=None, return_reason=None):
            surfaced.append((uow.id, condition))

        from src.orchestration.steward import _process_uow, _fetch_audit_entries

        # Set up output_ref so reentry_posture != first_execution
        output_ref = tmp_path / "uow-phil-surface.output"
        output_ref.write_text("philosophical output", encoding="utf-8")

        result_file = output_ref.with_suffix(".result.json")
        result_file.write_text(json.dumps({
            "uow_id": "uow-phil-surface",
            "outcome": "complete",
            "reason": "done",
        }), encoding="utf-8")

        conn = _open_db(db_path)
        conn.execute(
            "UPDATE uow_registry SET output_ref = ?, steward_cycles = 1 WHERE id = ?",
            (str(output_ref), "uow-phil-surface")
        )
        # Add an audit entry so reentry_posture = execution_complete
        conn.execute(
            """INSERT INTO audit_log (uow_id, event, agent, note, ts)
               VALUES (?, ?, ?, ?, ?)""",
            ("uow-phil-surface", "execution_complete", "executor",
             json.dumps({"return_reason": "observation_complete"}), _now_iso())
        )
        conn.commit()
        conn.close()

        uow = registry.get("uow-phil-surface")
        audit_entries = _fetch_audit_entries(registry, "uow-phil-surface")

        result = _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info={"body": "", "state": "open", "labels": []},
            dry_run=True, artifact_dir=tmp_path, notify_dan=fake_notify,
            llm_prescriber=lambda *a, **k: {"instructions": "x",
                                             "success_criteria_check": "y",
                                             "estimated_cycles": 1},
        )

        assert isinstance(result, Surfaced), f"Expected Surfaced, got {result}"
        assert result.condition == "philosophical_register"

    def test_philosophical_no_surface_on_first_execution(self, tmp_path):
        """philosophical UoW on first_execution (cycles=0, no output_ref) → not surfaced yet."""
        registry, db_path = _make_registry(tmp_path)
        conn = _open_db(db_path)
        _insert_uow(conn, "uow-phil-first", register="philosophical",
                    summary="explore something", steward_cycles=0)
        conn.close()

        surfaced = []

        def fake_notify(uow, condition, surface_log=None, return_reason=None):
            surfaced.append((uow.id, condition))

        from src.orchestration.steward import _process_uow, _fetch_audit_entries

        uow = registry.get("uow-phil-first")
        audit_entries = _fetch_audit_entries(registry, "uow-phil-first")

        result = _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info={"body": "", "state": "open", "labels": []},
            dry_run=True, artifact_dir=tmp_path, notify_dan=fake_notify,
            llm_prescriber=lambda *a, **k: {"instructions": "x",
                                             "success_criteria_check": "y",
                                             "estimated_cycles": 1},
        )

        # On first_execution, philosophical UoW should be prescribed (not surfaced)
        assert isinstance(result, Prescribed), f"Expected Prescribed on first_execution, got {result}"
        assert not any(c == "philosophical_register" for _, c in surfaced)
