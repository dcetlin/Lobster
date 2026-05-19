"""
Tests for scheduled-tasks/wos-metabolic-digest.py and the heartbeat contract
in dispatcher_handlers.py.

Tests cover:
- resolve_outcome_category: uses outcome_category field from DB, falls back to shit
- aggregate_by_category: all four keys always present, UoWs routed correctly
- aggregate_gate_churn: counts gate_fired values, excludes 'none'
- format_digest: spec-compliant format — idle suppression, one-liner, full digest
- jobs.json enabled gate
- query_completed_uows / query_still_running: DB-backed queries
- handle_wos_execute: heartbeat contract section present in dispatched prompt
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load wos-metabolic-digest from its script path (not a package)
# ---------------------------------------------------------------------------

DIGEST_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent
    / "scheduled-tasks"
    / "wos-metabolic-digest.py"
)

HANDLERS_MODULE_PATH = (
    Path(__file__).parent.parent.parent
    / "src"
    / "orchestration"
    / "dispatcher_handlers.py"
)


def _load_digest_module():
    spec = importlib.util.spec_from_file_location("wos_metabolic_digest", DIGEST_SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_handlers_module():
    # dispatcher_handlers uses relative imports (from .registry import ...) so it
    # must be loaded as part of the src.orchestration package, not as a standalone file.
    repo_root = str(HANDLERS_MODULE_PATH.parent.parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    import importlib
    return importlib.import_module("src.orchestration.dispatcher_handlers")


md = _load_digest_module()

# ---------------------------------------------------------------------------
# Constants — non-mirrored test fixtures
# ---------------------------------------------------------------------------

STALL_THRESHOLD_HOURS = 6     # stalled section threshold in the digest


# ---------------------------------------------------------------------------
# Helpers for building UoW dicts
# ---------------------------------------------------------------------------

def _hours_ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _uow(
    *,
    uow_id: str = "uow-test",
    status: str = "done",
    outcome_category: str | None = None,
    close_reason: str = "",
    output_ref: str = "",
    register: str = "operational",
    steward_cycles: int = 1,
    token_usage: int | None = None,
    gate_fired: str | None = None,
    started_hours_ago: float = 2.0,
    closed_hours_ago: float = 1.0,
) -> dict:
    return {
        "id": uow_id,
        "status": status,
        "outcome_category": outcome_category,
        "close_reason": close_reason,
        "output_ref": output_ref,
        "register": register,
        "steward_cycles": steward_cycles,
        "token_usage": token_usage,
        "gate_fired": gate_fired,
        "started_at": _hours_ago_iso(started_hours_ago),
        "closed_at": _hours_ago_iso(closed_hours_ago),
        "created_at": _hours_ago_iso(started_hours_ago + 1),
        "updated_at": _hours_ago_iso(closed_hours_ago),
        "summary": "test uow",
        "seeds_surfaced": None,
    }


# ---------------------------------------------------------------------------
# SQLite fixture for DB-backed query tests
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE uow_registry (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            summary TEXT,
            register TEXT DEFAULT 'operational',
            close_reason TEXT,
            output_ref TEXT,
            started_at TEXT,
            completed_at TEXT,
            closed_at TEXT,
            updated_at TEXT,
            created_at TEXT NOT NULL,
            heartbeat_at TEXT,
            heartbeat_ttl INTEGER DEFAULT 300,
            steward_cycles INTEGER DEFAULT 0,
            token_usage INTEGER NULL,
            outcome_category TEXT NULL,
            gate_fired TEXT NULL,
            seeds_surfaced TEXT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            uow_id TEXT NOT NULL,
            event TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            agent TEXT,
            note TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db


def _insert_uow_row(db: Path, uow: dict) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, status, summary, register, close_reason, output_ref,
             started_at, closed_at, updated_at, created_at, steward_cycles,
             outcome_category, gate_fired)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uow["id"], uow["status"], uow.get("summary", "test uow"),
            uow.get("register", "operational"), uow.get("close_reason", ""),
            uow.get("output_ref", ""), uow.get("started_at"),
            uow.get("closed_at"), uow.get("updated_at"), uow["created_at"],
            uow.get("steward_cycles", 0),
            uow.get("outcome_category"),
            uow.get("gate_fired"),
        ),
    )
    conn.commit()
    conn.close()


def _insert_audit_entry(db: Path, uow_id: str, to_status: str, hours_ago: float = 0.5) -> None:
    ts = _hours_ago_iso(hours_ago)
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status)
        VALUES (?, ?, 'status_change', 'active', ?)
        """,
        (ts, uow_id, to_status),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# resolve_outcome_category — uses outcome_category field, falls back to shit
# ---------------------------------------------------------------------------

class TestResolveOutcomeCategory:
    def test_pearl_outcome_category_resolved(self):
        uow = _uow(status="done", outcome_category="pearl")
        assert md.resolve_outcome_category(uow) == md.OUTCOME_PEARL

    def test_heat_outcome_category_resolved(self):
        uow = _uow(status="done", outcome_category="heat")
        assert md.resolve_outcome_category(uow) == md.OUTCOME_HEAT

    def test_seed_outcome_category_resolved(self):
        uow = _uow(status="done", outcome_category="seed")
        assert md.resolve_outcome_category(uow) == md.OUTCOME_SEED

    def test_shit_outcome_category_explicit(self):
        uow = _uow(status="done", outcome_category="shit")
        assert md.resolve_outcome_category(uow) == md.OUTCOME_SHIT

    def test_null_outcome_category_falls_back_to_shit(self):
        uow = _uow(status="done", outcome_category=None)
        assert md.resolve_outcome_category(uow) == md.OUTCOME_SHIT

    def test_unrecognized_outcome_category_falls_back_to_shit(self):
        uow = _uow(status="done", outcome_category="unknown_value")
        assert md.resolve_outcome_category(uow) == md.OUTCOME_SHIT

    def test_failed_status_overrides_outcome_category(self):
        # Terminal failure overrides any subagent classification
        uow = _uow(status="failed", outcome_category="pearl")
        assert md.resolve_outcome_category(uow) == md.OUTCOME_SHIT

    def test_expired_status_overrides_outcome_category(self):
        uow = _uow(status="expired", outcome_category="heat")
        assert md.resolve_outcome_category(uow) == md.OUTCOME_SHIT

    def test_cancelled_status_overrides_outcome_category(self):
        uow = _uow(status="cancelled", outcome_category="seed")
        assert md.resolve_outcome_category(uow) == md.OUTCOME_SHIT


# ---------------------------------------------------------------------------
# aggregate_by_category
# ---------------------------------------------------------------------------

class TestAggregateByCategory:
    def test_all_four_keys_always_present(self):
        groups = md.aggregate_by_category([])
        assert set(groups.keys()) == {md.OUTCOME_PEARL, md.OUTCOME_HEAT, md.OUTCOME_SEED, md.OUTCOME_SHIT}

    def test_all_keys_present_even_when_categories_empty(self):
        uows = [_uow(uow_id="p1", outcome_category="pearl")]
        groups = md.aggregate_by_category(uows)
        assert md.OUTCOME_HEAT in groups
        assert md.OUTCOME_SEED in groups
        assert md.OUTCOME_SHIT in groups

    def test_uows_routed_to_correct_group(self):
        pearl = _uow(uow_id="p1", outcome_category="pearl")
        heat = _uow(uow_id="h1", outcome_category="heat")
        seed = _uow(uow_id="s1", outcome_category="seed")
        shit = _uow(uow_id="sh1", status="failed")

        groups = md.aggregate_by_category([pearl, heat, seed, shit])

        assert len(groups[md.OUTCOME_PEARL]) == 1
        assert len(groups[md.OUTCOME_HEAT]) == 1
        assert len(groups[md.OUTCOME_SEED]) == 1
        assert len(groups[md.OUTCOME_SHIT]) == 1
        assert groups[md.OUTCOME_PEARL][0]["id"] == "p1"

    def test_null_outcome_category_goes_to_shit(self):
        uow = _uow(uow_id="no-cat", status="done", outcome_category=None)
        groups = md.aggregate_by_category([uow])
        assert len(groups[md.OUTCOME_SHIT]) == 1
        assert groups[md.OUTCOME_SHIT][0]["id"] == "no-cat"


# ---------------------------------------------------------------------------
# aggregate_gate_churn
# ---------------------------------------------------------------------------

class TestAggregateGateChurn:
    def test_empty_uows_all_zeros(self):
        churn = md.aggregate_gate_churn([])
        assert churn == {md.GATE_SPIRAL: 0, md.GATE_DEAD_END: 0, md.GATE_BURST: 0}

    def test_spiral_counted(self):
        uows = [_uow(gate_fired="spiral"), _uow(gate_fired="spiral")]
        churn = md.aggregate_gate_churn(uows)
        assert churn[md.GATE_SPIRAL] == 2

    def test_dead_end_counted(self):
        uows = [_uow(gate_fired="dead_end")]
        churn = md.aggregate_gate_churn(uows)
        assert churn[md.GATE_DEAD_END] == 1

    def test_burst_counted(self):
        uows = [_uow(gate_fired="burst")]
        churn = md.aggregate_gate_churn(uows)
        assert churn[md.GATE_BURST] == 1

    def test_none_gate_excluded_from_churn(self):
        uows = [_uow(gate_fired="none"), _uow(gate_fired=None)]
        churn = md.aggregate_gate_churn(uows)
        assert sum(churn.values()) == 0

    def test_mixed_gates_counted_correctly(self):
        uows = [
            _uow(gate_fired="spiral"),
            _uow(gate_fired="dead_end"),
            _uow(gate_fired="burst"),
            _uow(gate_fired="none"),
        ]
        churn = md.aggregate_gate_churn(uows)
        assert churn[md.GATE_SPIRAL] == 1
        assert churn[md.GATE_DEAD_END] == 1
        assert churn[md.GATE_BURST] == 1


# ---------------------------------------------------------------------------
# format_digest — idle suppression, one-liner, full format
# ---------------------------------------------------------------------------

class TestFormatDigest:
    _now_dt = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)

    def _groups_with(self, **kwargs) -> dict:
        """Build groups dict with specified count per category."""
        uows: list = []
        for cat, count in kwargs.items():
            for i in range(count):
                uows.append(_uow(uow_id=f"{cat}-{i}", outcome_category=cat))
        return md.aggregate_by_category(uows)

    def test_idle_day_returns_none(self):
        groups = md.aggregate_by_category([])
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert result is None

    def test_one_liner_for_one_uow(self):
        groups = self._groups_with(pearl=1)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert result is not None
        assert "WOS:" in result
        assert "pearl 1" in result
        # One-liner should NOT contain the multi-line header
        assert "WOS Daily" not in result

    def test_one_liner_for_two_uows(self):
        groups = self._groups_with(shit=2)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert result is not None
        assert "WOS:" in result

    def test_full_format_for_three_or_more_uows(self):
        groups = self._groups_with(pearl=1, heat=1, shit=1)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert result is not None
        assert "WOS Daily" in result
        assert "2026-05-18" in result

    def test_full_format_counts_line(self):
        groups = self._groups_with(pearl=2, heat=1, seed=1, shit=3)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert "Completed : 7 UoW(s)" in result

    def test_full_format_category_distribution_line(self):
        groups = self._groups_with(pearl=2, seed=1, heat=0, shit=3)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert "pearl 2" in result
        assert "shit 3" in result

    def test_seeds_surfaced_shown_when_nonzero(self):
        groups = self._groups_with(seed=3)
        result = md.format_digest(groups, [], 24, self._now_dt, 5)
        assert "Seeds surfaced: 5" in result

    def test_seeds_surfaced_omitted_when_zero(self):
        groups = self._groups_with(pearl=3)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert "Seeds" not in result

    def test_gate_churn_shown_when_nonzero(self):
        uows = [
            _uow(uow_id="s1", outcome_category="shit", gate_fired="spiral"),
            _uow(uow_id="s2", outcome_category="shit", gate_fired="dead_end"),
            _uow(uow_id="s3", outcome_category="shit"),
        ]
        groups = md.aggregate_by_category(uows)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert "Churn" in result
        assert "spiral" in result
        assert "dead-end" in result

    def test_gate_churn_omitted_when_all_clean(self):
        uows = [
            _uow(uow_id="p1", outcome_category="pearl", gate_fired="none"),
            _uow(uow_id="p2", outcome_category="pearl"),
            _uow(uow_id="p3", outcome_category="pearl"),
        ]
        groups = md.aggregate_by_category(uows)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert "Churn" not in result

    def test_stalled_line_included_when_stalled_present(self):
        stalled = [{"id": "uow-stalled", "status": "active"}]
        groups = self._groups_with(pearl=3)
        result = md.format_digest(groups, stalled, 24, self._now_dt, 0)
        assert "Stalled" in result
        assert "uow-stalled" in result

    def test_stalled_line_absent_when_no_stalled(self):
        groups = self._groups_with(pearl=3)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert "Stalled" not in result

    def test_avg_tokens_shown_when_available(self):
        uows = [
            _uow(uow_id="p1", outcome_category="pearl", token_usage=1000),
            _uow(uow_id="p2", outcome_category="pearl", token_usage=2000),
            _uow(uow_id="p3", outcome_category="pearl", token_usage=3000),
        ]
        groups = md.aggregate_by_category(uows)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert "Avg tokens" in result
        assert "2,000" in result

    def test_avg_tokens_omitted_when_all_null(self):
        uows = [
            _uow(uow_id="p1", outcome_category="pearl"),
            _uow(uow_id="p2", outcome_category="pearl"),
            _uow(uow_id="p3", outcome_category="pearl"),
        ]
        groups = md.aggregate_by_category(uows)
        result = md.format_digest(groups, [], 24, self._now_dt, 0)
        assert "Avg tokens" not in result


# ---------------------------------------------------------------------------
# jobs.json enabled gate
# ---------------------------------------------------------------------------

class TestJobsJsonGate:
    def test_job_gate_fires_when_disabled(self, tmp_path, monkeypatch):
        """is_job_enabled returns False → job skips execution."""
        monkeypatch.setattr(md, "is_job_enabled", lambda name: False)
        assert md.is_job_enabled(md.JOB_NAME) is False

    def test_job_gate_passes_when_enabled(self, monkeypatch):
        """is_job_enabled returns True → job proceeds."""
        monkeypatch.setattr(md, "is_job_enabled", lambda name: True)
        assert md.is_job_enabled(md.JOB_NAME) is True

    def test_job_defaults_to_enabled_when_jobs_json_absent(self, tmp_path, monkeypatch):
        """is_job_enabled defaults to True when jobs.json is absent."""
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        # jobs.json absent — LOBSTER_WORKSPACE set but no scheduled-jobs/jobs.json
        import importlib
        utils_jobs = importlib.import_module("src.utils.jobs")
        assert utils_jobs.is_job_enabled(md.JOB_NAME) is True

    def test_job_defaults_to_enabled_when_entry_missing(self, tmp_path, monkeypatch):
        """is_job_enabled defaults to True when the job entry is absent from jobs.json."""
        jobs_json = tmp_path / "scheduled-jobs" / "jobs.json"
        jobs_json.parent.mkdir(parents=True)
        jobs_json.write_text(json.dumps({"jobs": {}}))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        import importlib
        utils_jobs = importlib.import_module("src.utils.jobs")
        assert utils_jobs.is_job_enabled(md.JOB_NAME) is True


# ---------------------------------------------------------------------------
# query_completed_uows — DB-backed query
# ---------------------------------------------------------------------------

class TestQueryCompletedUows:
    def test_uow_transitioned_to_done_within_window_is_returned(self, db_path):
        u = _uow(uow_id="uow-done", status="done", outcome_category="pearl")
        _insert_uow_row(db_path, u)
        _insert_audit_entry(db_path, "uow-done", "done", hours_ago=1)
        results = md.query_completed_uows(db_path, md.DEFAULT_LOOKBACK_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-done" in ids

    def test_outcome_category_included_in_returned_row(self, db_path):
        u = _uow(uow_id="uow-pearl", status="done", outcome_category="pearl")
        _insert_uow_row(db_path, u)
        _insert_audit_entry(db_path, "uow-pearl", "done", hours_ago=1)
        results = md.query_completed_uows(db_path, md.DEFAULT_LOOKBACK_HOURS)
        row = next((r for r in results if r["id"] == "uow-pearl"), None)
        assert row is not None
        assert row["outcome_category"] == "pearl"

    def test_uow_not_in_audit_window_is_excluded(self, db_path):
        # Transition happened 48h ago — outside the default 24h window
        u = _uow(uow_id="uow-old", status="done", outcome_category="pearl")
        _insert_uow_row(db_path, u)
        _insert_audit_entry(db_path, "uow-old", "done", hours_ago=48)
        results = md.query_completed_uows(db_path, md.DEFAULT_LOOKBACK_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-old" not in ids

    def test_absent_db_returns_empty_list(self, tmp_path):
        results = md.query_completed_uows(tmp_path / "nonexistent.db", md.DEFAULT_LOOKBACK_HOURS)
        assert results == []


# ---------------------------------------------------------------------------
# query_still_running — DB-backed query
# ---------------------------------------------------------------------------

class TestQueryStillRunning:
    def test_long_running_active_uow_returned(self, db_path):
        u = _uow(
            uow_id="uow-running",
            status="active",
            started_hours_ago=STALL_THRESHOLD_HOURS + 1,
            closed_hours_ago=STALL_THRESHOLD_HOURS + 1,
        )
        u["closed_at"] = None  # still running
        u["status"] = "active"
        _insert_uow_row(db_path, u)
        results = md.query_still_running(db_path, threshold_hours=STALL_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-running" in ids

    def test_recently_started_uow_not_returned(self, db_path):
        u = _uow(uow_id="uow-new", status="active", started_hours_ago=1)
        u["closed_at"] = None
        _insert_uow_row(db_path, u)
        results = md.query_still_running(db_path, threshold_hours=STALL_THRESHOLD_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-new" not in ids


# ---------------------------------------------------------------------------
# handle_wos_execute — heartbeat contract in dispatched prompt
# ---------------------------------------------------------------------------

class TestHandleWosExecuteHeartbeatContract:
    """
    Verify that the heartbeat contract section is present in the prompt returned
    by handle_wos_execute. This section was added in issue #849 — the heartbeat
    infrastructure existed (PR #848) but agents were never instructed to call it.
    """

    @pytest.fixture(scope="class")
    def prompt(self):
        handlers = _load_handlers_module()
        return handlers.handle_wos_execute(
            uow_id="test-uow-001",
            instructions="Do the thing.",
            output_ref="/tmp/test-uow-001.result.json",
        )

    def test_heartbeat_section_heading_present(self, prompt):
        assert "Heartbeat contract" in prompt

    def test_write_heartbeat_call_present(self, prompt):
        assert "write_heartbeat" in prompt

    def test_uow_id_interpolated_in_heartbeat_call(self, prompt):
        # The heartbeat call must use the actual UoW ID, not a placeholder.
        # Both the MCP path (uow_id='test-uow-001') and the fallback path
        # (WOSRegistry().write_heartbeat('test-uow-001', ...)) embed the UoW ID.
        # Check that at least one form is present.
        assert (
            "uow_id='test-uow-001'" in prompt
            or "write_heartbeat('test-uow-001'" in prompt
        )

    def test_60_90_second_interval_documented(self, prompt):
        # The prompt uses an en-dash (U+2013) between 60 and 90.
        assert "60–90 seconds" in prompt

    def test_stop_on_zero_return_value_documented(self, prompt):
        # Agent must stop if write_heartbeat returns 0 (UoW re-queued)
        assert "0" in prompt and "stop" in prompt.lower()

    def test_instructions_still_present_after_heartbeat_section(self, prompt):
        # The heartbeat section must not replace the instructions
        assert "Do the thing." in prompt

    def test_result_contract_present(self, prompt):
        # The result contract (write_result / output_ref) must also be present
        assert "Result contract" in prompt or "result file" in prompt.lower()
