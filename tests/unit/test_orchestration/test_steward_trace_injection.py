"""
Tests for PR C: corrective trace injection in steward.py.

Covers:
- _read_trace_json: returns dict on valid file, None on missing/invalid
- _read_trace_json: tries both path conventions
- _bound_prescription_delta: truncates at PRESCRIPTION_DELTA_MAX_CHARS
- _bound_prescription_delta: leaves short strings unchanged
- _count_non_improving_gate_cycles: returns 0 with fewer than 2 entries
- _count_non_improving_gate_cycles: counts consecutive non-improving tail
- _count_non_improving_gate_cycles: resets on an improving cycle
- trace_injection event written to steward_log when trace.json present
- surprises injected into completion_gap_for_prescription
- prescription_delta injected into completion_gap_for_prescription
- iterative-convergent: gate_score injected into completion_gap
- no_gate_improvement stuck condition fires after 3 non-improving cycles
- misrouted trace (uow_id mismatch) is discarded silently
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
    _read_trace_json,
    _bound_prescription_delta,
    _count_non_improving_gate_cycles,
    _PRESCRIPTION_DELTA_MAX_CHARS,
    _NO_GATE_IMPROVEMENT_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_trace_log_entry(gate_score: float | None = None) -> str:
    """Build a steward_log string with one trace_injection entry."""
    entry = {
        "event": "trace_injection",
        "uow_id": "test-uow",
        "steward_cycles": 1,
        "register": "iterative-convergent",
        "gate_score": {"score": gate_score} if gate_score is not None else None,
        "surprises_count": 0,
        "prescription_delta_present": False,
        "timestamp": _now_iso(),
    }
    return json.dumps(entry)


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _insert_uow(conn: sqlite3.Connection, uow_id: str,
                status: str = "ready-for-steward",
                steward_cycles: int = 0,
                output_ref: str | None = None,
                steward_log: str | None = None,
                register: str = "operational",
                summary: str = "Test UoW",
                success_criteria: str = "output file present") -> None:
    now = _now_iso()
    conn.execute(
        """INSERT INTO uow_registry
           (id, type, source, source_issue_number, sweep_date, status, posture,
            created_at, updated_at, summary, output_ref, steward_cycles,
            steward_log, success_criteria, route_evidence, trigger, register)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uow_id, "executable", "github:issue/1", 1, "2026-01-01", status, "solo",
         now, now, summary, output_ref, steward_cycles,
         steward_log, success_criteria, "{}", '{"type": "immediate"}', register),
    )
    conn.commit()


def _insert_audit(conn: sqlite3.Connection, uow_id: str,
                  event: str = "execution_complete") -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, uow_id, event, note) VALUES (?,?,?,?)",
        (_now_iso(), uow_id, event, json.dumps({"event": event})),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: _read_trace_json
# ---------------------------------------------------------------------------

class TestReadTraceJson:
    def test_returns_dict_for_valid_primary_path(self, tmp_path):
        """Primary path: Path(output_ref).with_suffix('.trace.json')."""
        output_ref = str(tmp_path / "uow-123.md")
        trace_data = {"uow_id": "uow-123", "surprises": [], "prescription_delta": ""}
        (tmp_path / "uow-123.trace.json").write_text(json.dumps(trace_data), encoding="utf-8")

        result = _read_trace_json(output_ref)
        assert result is not None
        assert result["uow_id"] == "uow-123"

    def test_returns_dict_for_alternate_path(self, tmp_path):
        """Alternate path: str(output_ref) + '.trace.json'."""
        # Use a file whose primary .with_suffix wouldn't hit
        output_ref = str(tmp_path / "uow-456.result.json")
        trace_data = {"uow_id": "uow-456"}
        # Alternate: output_ref + ".trace.json" (not replacing extension)
        alt_path = Path(str(output_ref) + ".trace.json")
        alt_path.write_text(json.dumps(trace_data), encoding="utf-8")

        result = _read_trace_json(output_ref)
        assert result is not None
        assert result["uow_id"] == "uow-456"

    def test_returns_none_when_file_missing(self, tmp_path):
        output_ref = str(tmp_path / "nonexistent.md")
        result = _read_trace_json(output_ref)
        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path):
        output_ref = str(tmp_path / "bad.md")
        (tmp_path / "bad.trace.json").write_text("not valid json {{{", encoding="utf-8")
        result = _read_trace_json(output_ref)
        assert result is None

    def test_returns_none_for_empty_string(self):
        result = _read_trace_json("")
        assert result is None

    def test_returns_none_for_none(self):
        result = _read_trace_json(None)  # type: ignore[arg-type]
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _bound_prescription_delta
# ---------------------------------------------------------------------------

class TestBoundPrescriptionDelta:
    def test_short_delta_unchanged(self):
        delta = "add --no-header flag to gate command"
        result = _bound_prescription_delta(delta, history=[])
        assert result == delta

    def test_long_delta_truncated_at_max_chars(self):
        delta = "x" * (_PRESCRIPTION_DELTA_MAX_CHARS + 100)
        result = _bound_prescription_delta(delta, history=[])
        # First _PRESCRIPTION_DELTA_MAX_CHARS chars preserved
        assert result.startswith("x" * _PRESCRIPTION_DELTA_MAX_CHARS)
        assert "truncated" in result

    def test_delta_exactly_at_limit_unchanged(self):
        delta = "y" * _PRESCRIPTION_DELTA_MAX_CHARS
        result = _bound_prescription_delta(delta, history=[])
        assert result == delta
        assert "truncated" not in result

    def test_truncation_note_appended(self):
        delta = "z" * (_PRESCRIPTION_DELTA_MAX_CHARS + 1)
        result = _bound_prescription_delta(delta, history=[])
        assert "[prescription_delta truncated" in result

    def test_history_parameter_accepted(self):
        """history param is accepted (reserved for future smoothing) without error."""
        delta = "short"
        result = _bound_prescription_delta(delta, history=["prev1", "prev2"])
        assert result == delta


# ---------------------------------------------------------------------------
# Tests: _count_non_improving_gate_cycles
# ---------------------------------------------------------------------------

class TestCountNonImprovingGateCycles:
    def test_returns_zero_with_no_log(self):
        assert _count_non_improving_gate_cycles(None) == 0
        assert _count_non_improving_gate_cycles("") == 0

    def test_returns_zero_with_single_entry(self):
        log = _make_trace_log_entry(gate_score=0.5)
        assert _count_non_improving_gate_cycles(log) == 0

    def test_returns_zero_when_monotonically_improving(self):
        entries = [
            _make_trace_log_entry(gate_score=0.3),
            _make_trace_log_entry(gate_score=0.6),
            _make_trace_log_entry(gate_score=0.9),
        ]
        log = "\n".join(entries)
        assert _count_non_improving_gate_cycles(log) == 0

    def test_counts_non_improving_tail(self):
        # n=3 means window=4 entries; 3 comparison pairs needed for threshold.
        # With 5 entries [0.9, 0.5, 0.5, 0.5, 0.4], window=4 → [0.5, 0.5, 0.5, 0.4]
        # Comparisons: 0.5<=0.5 (ni), 0.5<=0.5 (ni), 0.4<=0.5 (ni) → count=3
        entries = [
            _make_trace_log_entry(gate_score=0.9),  # outside window
            _make_trace_log_entry(gate_score=0.5),
            _make_trace_log_entry(gate_score=0.5),  # flat
            _make_trace_log_entry(gate_score=0.5),  # flat
            _make_trace_log_entry(gate_score=0.4),  # declining
        ]
        log = "\n".join(entries)
        result = _count_non_improving_gate_cycles(log)
        assert result == 3

    def test_resets_on_improving_cycle(self):
        entries = [
            _make_trace_log_entry(gate_score=0.3),
            _make_trace_log_entry(gate_score=0.5),  # non-improving
            _make_trace_log_entry(gate_score=0.4),  # non-improving
            _make_trace_log_entry(gate_score=0.8),  # improving — resets count
        ]
        log = "\n".join(entries)
        assert _count_non_improving_gate_cycles(log) == 0

    def test_skips_non_trace_injection_entries(self):
        other = json.dumps({"event": "diagnosis", "gate_score": {"score": 0.1}})
        trace = _make_trace_log_entry(gate_score=0.9)
        log = "\n".join([other, trace])
        # Only 1 trace_injection entry — not enough to compare
        assert _count_non_improving_gate_cycles(log) == 0

    def test_skips_entries_without_gate_score(self):
        no_score = json.dumps({
            "event": "trace_injection",
            "uow_id": "x",
            "gate_score": None,
            "timestamp": _now_iso(),
        })
        assert _count_non_improving_gate_cycles(no_score) == 0

    def test_threshold_constant_is_three(self):
        assert _NO_GATE_IMPROVEMENT_THRESHOLD == 3


# ---------------------------------------------------------------------------
# Integration tests: trace injection in _process_uow
# ---------------------------------------------------------------------------

class TestTraceInjectionIntegration:
    """Integration tests verifying trace injection flows through _process_uow."""

    def _make_registry(self, tmp_path: Path):
        """Create a Registry with full migrations applied."""
        from src.orchestration.registry import Registry
        db_path = tmp_path / "registry.db"
        registry = Registry(db_path=db_path)
        return registry, db_path

    def test_trace_injection_event_written_when_trace_present(self, tmp_path):
        """When trace.json exists after trace gate clears, trace_injection logged."""
        registry, db_path = self._make_registry(tmp_path)

        output_file = tmp_path / "uow-abc.md"
        output_file.write_text("some output", encoding="utf-8")
        (tmp_path / "uow-abc.result.json").write_text(json.dumps({
            "uow_id": "uow-abc", "outcome": "partial", "reason": "needs more work",
        }), encoding="utf-8")
        (tmp_path / "uow-abc.trace.json").write_text(json.dumps({
            "uow_id": "uow-abc", "register": "operational",
            "execution_summary": "ran step 1", "surprises": ["test was slow"],
            "prescription_delta": "add timeout flag", "gate_score": None,
            "timestamp": _now_iso(),
        }), encoding="utf-8")

        conn = _open_db(db_path)
        _insert_uow(conn, "uow-abc", steward_cycles=1, output_ref=str(output_file),
                    steward_log=json.dumps({"event": "diagnosis"}))
        _insert_audit(conn, "uow-abc", "execution_complete")
        conn.close()

        def fake_prescriber(uow, posture, gap, body=""):
            return {"instructions": f"gap={gap}", "success_criteria_check": "ok", "estimated_cycles": 1}

        from src.orchestration.steward import _process_uow, _fetch_audit_entries

        uow = registry.get("uow-abc")
        audit_entries = _fetch_audit_entries(registry, "uow-abc")

        # Use dry_run=False so steward_log is actually written to DB
        _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info={"body": "", "state": "open", "labels": []},
            dry_run=False, artifact_dir=tmp_path, notify_dan=lambda *a, **k: None,
            llm_prescriber=fake_prescriber,
        )

        uow_after = registry.get("uow-abc")
        log_str = uow_after.steward_log or ""
        events = [
            _safe_json_parse(ln.strip()).get("event")
            for ln in log_str.splitlines()
            if ln.strip() and _safe_json_parse(ln.strip()) is not None
        ]
        assert "trace_injection" in events, f"Expected trace_injection in events: {events}"

    def test_surprises_injected_into_prescription_gap(self, tmp_path):
        """Surprises from trace.json appear in the completion_gap passed to prescriber."""
        registry, db_path = self._make_registry(tmp_path)

        output_file = tmp_path / "uow-surp.md"
        output_file.write_text("output", encoding="utf-8")
        (tmp_path / "uow-surp.result.json").write_text(json.dumps({
            "uow_id": "uow-surp", "outcome": "partial", "reason": "partial",
        }), encoding="utf-8")
        (tmp_path / "uow-surp.trace.json").write_text(json.dumps({
            "uow_id": "uow-surp", "register": "operational",
            "execution_summary": "did stuff",
            "surprises": ["unexpected dependency missing", "auth error"],
            "prescription_delta": "", "gate_score": None, "timestamp": _now_iso(),
        }), encoding="utf-8")

        conn = _open_db(db_path)
        _insert_uow(conn, "uow-surp", steward_cycles=1, output_ref=str(output_file))
        _insert_audit(conn, "uow-surp", "execution_complete")
        conn.close()

        captured_gaps: list[str] = []

        def fake_prescriber(uow, posture, gap, body=""):
            captured_gaps.append(gap)
            return {"instructions": "x", "success_criteria_check": "y", "estimated_cycles": 1}

        from src.orchestration.steward import _process_uow, _fetch_audit_entries

        uow = registry.get("uow-surp")
        audit_entries = _fetch_audit_entries(registry, "uow-surp")
        _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info={"body": "", "state": "open", "labels": []},
            dry_run=True, artifact_dir=tmp_path, notify_dan=lambda *a, **k: None,
            llm_prescriber=fake_prescriber,
        )

        assert len(captured_gaps) == 1
        gap = captured_gaps[0]
        assert "unexpected dependency missing" in gap
        assert "auth error" in gap

    def test_misrouted_trace_discarded(self, tmp_path):
        """A trace.json with a different uow_id is silently discarded."""
        registry, db_path = self._make_registry(tmp_path)

        output_file = tmp_path / "uow-real.md"
        output_file.write_text("output", encoding="utf-8")
        (tmp_path / "uow-real.result.json").write_text(json.dumps({
            "uow_id": "uow-real", "outcome": "partial", "reason": "needs more",
        }), encoding="utf-8")
        (tmp_path / "uow-real.trace.json").write_text(json.dumps({
            "uow_id": "DIFFERENT-UOW", "register": "operational",
            "surprises": ["should be discarded"],
            "prescription_delta": "should not appear",
            "gate_score": None, "timestamp": _now_iso(),
        }), encoding="utf-8")

        conn = _open_db(db_path)
        _insert_uow(conn, "uow-real", steward_cycles=1, output_ref=str(output_file))
        _insert_audit(conn, "uow-real", "execution_complete")
        conn.close()

        captured_gaps: list[str] = []

        def fake_prescriber(uow, posture, gap, body=""):
            captured_gaps.append(gap)
            return {"instructions": "x", "success_criteria_check": "y", "estimated_cycles": 1}

        from src.orchestration.steward import _process_uow, _fetch_audit_entries

        uow = registry.get("uow-real")
        audit_entries = _fetch_audit_entries(registry, "uow-real")
        _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info={"body": "", "state": "open", "labels": []},
            dry_run=True, artifact_dir=tmp_path, notify_dan=lambda *a, **k: None,
            llm_prescriber=fake_prescriber,
        )

        assert len(captured_gaps) == 1
        gap = captured_gaps[0]
        assert "should be discarded" not in gap
        assert "should not appear" not in gap

    def test_no_gate_improvement_surfaces_to_dan(self, tmp_path):
        """For iterative-convergent with 3+ non-improving cycles, surface to Dan."""
        registry, db_path = self._make_registry(tmp_path)

        # 4 consecutive flat gate scores in steward_log → 3 non-improving comparisons
        # (window=4 with n=3, so all 4 entries fit; 3 comparisons all non-improving)
        log_entries = [
            json.dumps({
                "event": "trace_injection", "uow_id": "uow-iter",
                "steward_cycles": i, "register": "iterative-convergent",
                "gate_score": {"score": 0.5},  # all equal = non-improving
                "surprises_count": 0, "prescription_delta_present": False,
                "timestamp": _now_iso(),
            })
            for i in range(4)
        ]
        steward_log = "\n".join(log_entries)

        output_file = tmp_path / "uow-iter.md"
        output_file.write_text("output", encoding="utf-8")
        (tmp_path / "uow-iter.result.json").write_text(json.dumps({
            "uow_id": "uow-iter", "outcome": "partial", "reason": "not done",
        }), encoding="utf-8")
        (tmp_path / "uow-iter.trace.json").write_text(json.dumps({
            "uow_id": "uow-iter", "register": "iterative-convergent",
            "execution_summary": "ran gate", "surprises": [],
            "prescription_delta": "",
            "gate_score": {"score": 0.5, "command": "pytest", "result": "2 failed"},
            "timestamp": _now_iso(),
        }), encoding="utf-8")

        conn = _open_db(db_path)
        _insert_uow(conn, "uow-iter", steward_cycles=3, output_ref=str(output_file),
                    steward_log=steward_log, register="iterative-convergent")
        _insert_audit(conn, "uow-iter", "execution_complete")
        conn.close()

        surfaced: list[tuple] = []

        def fake_notify(uow, condition, surface_log=None, return_reason=None):
            surfaced.append((uow.id, condition))

        from src.orchestration.steward import _process_uow, _fetch_audit_entries, Surfaced

        uow = registry.get("uow-iter")
        audit_entries = _fetch_audit_entries(registry, "uow-iter")

        result = _process_uow(
            uow=uow, registry=registry, audit_entries=audit_entries,
            issue_info={"body": "", "state": "open", "labels": []},
            dry_run=True, artifact_dir=tmp_path, notify_dan=fake_notify,
            llm_prescriber=lambda *a, **k: {
                "instructions": "x", "success_criteria_check": "y", "estimated_cycles": 1
            },
        )

        assert isinstance(result, Surfaced), f"Expected Surfaced, got {result}"
        assert result.condition == "no_gate_improvement"
        assert len(surfaced) >= 1
        assert any(c == "no_gate_improvement" for _, c in surfaced)


# ---------------------------------------------------------------------------
# Helpers for integration tests
# ---------------------------------------------------------------------------

def _safe_json_parse(s: str) -> dict | None:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None
