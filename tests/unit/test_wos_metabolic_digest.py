"""
Tests for scheduled-tasks/wos-metabolic-digest.py and the heartbeat contract
in dispatcher_handlers.py.

Tests are derived from the spec in the WOS audit doc (wos-audit-20260422.md)
and issue #849:

- classify_uow: pearl/heat/seed/shit classification heuristics
- aggregate_by_classification: all four keys always present
- compute_duration_minutes: handles valid, missing, and malformed timestamps
- format_digest: produces the required header and summary line
- jobs.json enabled gate
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
# Constants — from the audit doc and issue, NOT derived from implementation
# ---------------------------------------------------------------------------

OUTCOME_PEARL = "pearl"
OUTCOME_HEAT = "heat"
OUTCOME_SEED = "seed"
OUTCOME_SHIT = "shit"

DEFAULT_LOOKBACK_HOURS = 24   # daily digest window
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
    close_reason: str = "",
    output_ref: str = "",
    register: str = "operational",
    started_hours_ago: float = 2.0,
    closed_hours_ago: float = 1.0,
) -> dict:
    return {
        "id": uow_id,
        "status": status,
        "close_reason": close_reason,
        "output_ref": output_ref,
        "register": register,
        "steward_cycles": 1,
        "started_at": _hours_ago_iso(started_hours_ago),
        "closed_at": _hours_ago_iso(closed_hours_ago),
        "created_at": _hours_ago_iso(started_hours_ago + 1),
        "updated_at": _hours_ago_iso(closed_hours_ago),
        "summary": "test uow",
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
            token_usage INTEGER NULL
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
             started_at, closed_at, updated_at, created_at, steward_cycles)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uow["id"], uow["status"], uow.get("summary", "test uow"),
            uow.get("register", "operational"), uow.get("close_reason", ""),
            uow.get("output_ref", ""), uow.get("started_at"),
            uow.get("closed_at"), uow.get("updated_at"), uow["created_at"],
            uow.get("steward_cycles", 0),
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
# classify_uow — classification heuristics
# ---------------------------------------------------------------------------

class TestClassifyUow:
    def test_failed_status_is_shit(self):
        uow = _uow(status="failed", close_reason="execution error")
        assert md.classify_uow(uow) == OUTCOME_SHIT

    def test_expired_status_is_shit(self):
        uow = _uow(status="expired", close_reason="ttl exceeded")
        assert md.classify_uow(uow) == OUTCOME_SHIT

    def test_cancelled_status_is_shit(self):
        uow = _uow(status="cancelled", close_reason="user_closed")
        assert md.classify_uow(uow) == OUTCOME_SHIT

    def test_ttl_in_close_reason_is_shit(self):
        uow = _uow(status="done", close_reason="ttl_exceeded after 4h")
        assert md.classify_uow(uow) == OUTCOME_SHIT

    def test_hard_cap_in_close_reason_is_shit(self):
        uow = _uow(status="done", close_reason="hard_cap reached")
        assert md.classify_uow(uow) == OUTCOME_SHIT

    def test_user_closed_in_close_reason_is_shit(self):
        uow = _uow(status="done", close_reason="user_closed by admin")
        assert md.classify_uow(uow) == OUTCOME_SHIT

    def test_pr_in_close_reason_is_pearl(self):
        uow = _uow(status="done", close_reason="opened pr #123")
        assert md.classify_uow(uow) == OUTCOME_PEARL

    def test_implementation_in_close_reason_is_pearl(self):
        uow = _uow(status="done", close_reason="implementation complete")
        assert md.classify_uow(uow) == OUTCOME_PEARL

    def test_commit_in_close_reason_is_pearl(self):
        uow = _uow(status="done", close_reason="committed changes")
        assert md.classify_uow(uow) == OUTCOME_PEARL

    def test_issue_in_close_reason_is_seed(self):
        uow = _uow(status="done", close_reason="spawned issue #456")
        assert md.classify_uow(uow) == OUTCOME_SEED

    def test_seed_keyword_in_close_reason_is_seed(self):
        uow = _uow(status="done", close_reason="seeded follow-on work")
        assert md.classify_uow(uow) == OUTCOME_SEED

    def test_spawn_keyword_in_close_reason_is_seed(self):
        uow = _uow(status="done", close_reason="spawn new uow for subtask")
        assert md.classify_uow(uow) == OUTCOME_SEED

    def test_review_in_close_reason_is_heat(self):
        uow = _uow(status="done", close_reason="completed review of design doc")
        assert md.classify_uow(uow) == OUTCOME_HEAT

    def test_analysis_in_close_reason_is_heat(self):
        uow = _uow(status="done", close_reason="analysis complete")
        assert md.classify_uow(uow) == OUTCOME_HEAT

    def test_design_in_close_reason_is_heat(self):
        uow = _uow(status="done", close_reason="design specification written")
        assert md.classify_uow(uow) == OUTCOME_HEAT

    def test_done_with_no_signal_is_shit(self):
        # Done with empty close_reason and no output_ref: no verifiable output
        uow = _uow(status="done", close_reason="", output_ref="")
        assert md.classify_uow(uow) == OUTCOME_SHIT

    def test_source_issue_mention_without_creation_is_not_seed(self):
        # "issue analysis complete" mentions an issue being worked on, not a new issue
        # created. Bare "issue" substring must NOT classify as seed — this is the
        # false-positive the phrase-level matching is designed to prevent.
        uow = _uow(status="done", close_reason="issue analysis complete")
        assert md.classify_uow(uow) == OUTCOME_HEAT

    def test_pr_closing_source_issue_is_pearl_not_seed(self):
        # "issue referenced in pr" describes a PR that closes a source issue —
        # the UoW produced a PR (pearl), not a new seed issue.
        # Bare "issue" substring must NOT win over "pr" here.
        uow = _uow(status="done", close_reason="issue referenced in pr")
        assert md.classify_uow(uow) == OUTCOME_PEARL

    def test_creation_phrase_opened_issue_is_seed(self):
        # Explicit creation verb + "issue" → seed, regardless of other keywords
        uow = _uow(status="done", close_reason="opened issue #789 for follow-up")
        assert md.classify_uow(uow) == OUTCOME_SEED

    def test_creation_phrase_created_issue_is_seed(self):
        uow = _uow(status="done", close_reason="created issue #100 to track regressions")
        assert md.classify_uow(uow) == OUTCOME_SEED


# ---------------------------------------------------------------------------
# aggregate_by_classification
# ---------------------------------------------------------------------------

class TestAggregateByClassification:
    def test_all_four_keys_always_present(self):
        groups = md.aggregate_by_classification([])
        assert set(groups.keys()) == {OUTCOME_PEARL, OUTCOME_HEAT, OUTCOME_SEED, OUTCOME_SHIT}

    def test_all_keys_present_even_when_categories_empty(self):
        pairs = [(_uow(close_reason="opened pr #1"), OUTCOME_PEARL)]
        groups = md.aggregate_by_classification(pairs)
        assert OUTCOME_HEAT in groups
        assert OUTCOME_SEED in groups
        assert OUTCOME_SHIT in groups

    def test_uows_routed_to_correct_group(self):
        pearl = _uow(uow_id="p1", close_reason="opened pr #1")
        heat = _uow(uow_id="h1", close_reason="review complete")
        seed = _uow(uow_id="s1", close_reason="spawned issue")
        shit = _uow(uow_id="sh1", status="failed")

        pairs = [
            (pearl, OUTCOME_PEARL),
            (heat, OUTCOME_HEAT),
            (seed, OUTCOME_SEED),
            (shit, OUTCOME_SHIT),
        ]
        groups = md.aggregate_by_classification(pairs)

        assert len(groups[OUTCOME_PEARL]) == 1
        assert len(groups[OUTCOME_HEAT]) == 1
        assert len(groups[OUTCOME_SEED]) == 1
        assert len(groups[OUTCOME_SHIT]) == 1
        assert groups[OUTCOME_PEARL][0]["id"] == "p1"


# ---------------------------------------------------------------------------
# compute_duration_minutes
# ---------------------------------------------------------------------------

class TestComputeDurationMinutes:
    def test_basic_duration_computed_correctly(self):
        uow = {
            "started_at": "2026-04-22T09:00:00+00:00",
            "closed_at": "2026-04-22T09:30:00+00:00",
        }
        result = md.compute_duration_minutes(uow)
        assert result == 30.0

    def test_missing_started_at_returns_none(self):
        uow = {"started_at": None, "closed_at": "2026-04-22T09:30:00+00:00"}
        assert md.compute_duration_minutes(uow) is None

    def test_missing_closed_at_falls_back_to_updated_at(self):
        uow = {
            "started_at": "2026-04-22T09:00:00+00:00",
            "closed_at": None,
            "updated_at": "2026-04-22T09:20:00+00:00",
        }
        result = md.compute_duration_minutes(uow)
        assert result == 20.0

    def test_negative_duration_returns_none(self):
        # closed_at before started_at — malformed data
        uow = {
            "started_at": "2026-04-22T10:00:00+00:00",
            "closed_at": "2026-04-22T09:00:00+00:00",
        }
        assert md.compute_duration_minutes(uow) is None

    def test_malformed_timestamp_returns_none(self):
        uow = {"started_at": "not-a-date", "closed_at": "also-not-a-date"}
        assert md.compute_duration_minutes(uow) is None


# ---------------------------------------------------------------------------
# format_digest
# ---------------------------------------------------------------------------

class TestFormatDigest:
    def _empty_groups(self) -> dict:
        return md.aggregate_by_classification([])

    def test_header_line_present(self):
        text = md.format_digest(self._empty_groups(), [], 24, "2026-04-22T09:00:00+00:00")
        assert "WOS Daily Metabolic Report" in text

    def test_summary_line_contains_counts(self):
        pearl = _uow(uow_id="p1", close_reason="opened pr #1")
        shit = _uow(uow_id="sh1", status="failed")
        groups = md.aggregate_by_classification([
            (pearl, OUTCOME_PEARL),
            (shit, OUTCOME_SHIT),
        ])
        text = md.format_digest(groups, [], 24, "2026-04-22T09:00:00+00:00")
        assert "1 pearl" in text
        assert "1 shit" in text

    def test_zero_completed_shows_no_uows_message(self):
        text = md.format_digest(self._empty_groups(), [], 24, "2026-04-22T09:00:00+00:00")
        assert "No UoWs completed" in text

    def test_stalled_line_included_when_stalled_present(self):
        stalled = [{"id": "uow-stalled", "status": "active"}]
        text = md.format_digest(self._empty_groups(), stalled, 24, "2026-04-22T09:00:00+00:00")
        assert "Stalled" in text
        assert "uow-stalled" in text

    def test_stalled_line_absent_when_no_stalled(self):
        text = md.format_digest(self._empty_groups(), [], 24, "2026-04-22T09:00:00+00:00")
        assert "Stalled" not in text

    def test_lookback_hours_reflected_in_output(self):
        text = md.format_digest(self._empty_groups(), [], 48, "2026-04-22T09:00:00+00:00")
        assert "48h" in text

    def test_register_breakdown_present_when_uows_exist(self):
        pearl = _uow(uow_id="p1", close_reason="opened pr", register="operational")
        groups = md.aggregate_by_classification([(pearl, OUTCOME_PEARL)])
        text = md.format_digest(groups, [], 24, "2026-04-22T09:00:00+00:00")
        assert "Register breakdown" in text
        assert "operational" in text


# ---------------------------------------------------------------------------
# jobs.json enabled gate
# ---------------------------------------------------------------------------

class TestJobsJsonGate:
    def test_job_skips_when_disabled(self, tmp_path, monkeypatch):
        jobs_file = tmp_path / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": {"wos-metabolic-digest": {"enabled": False}}}))
        monkeypatch.setattr(md, "_jobs_file", lambda: jobs_file)
        assert md._is_job_enabled() is False

    def test_job_runs_when_enabled(self, tmp_path, monkeypatch):
        jobs_file = tmp_path / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": {"wos-metabolic-digest": {"enabled": True}}}))
        monkeypatch.setattr(md, "_jobs_file", lambda: jobs_file)
        assert md._is_job_enabled() is True

    def test_job_defaults_to_enabled_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(md, "_jobs_file", lambda: tmp_path / "nonexistent.json")
        assert md._is_job_enabled() is True

    def test_job_defaults_to_enabled_when_entry_missing(self, tmp_path, monkeypatch):
        jobs_file = tmp_path / "jobs.json"
        jobs_file.write_text(json.dumps({"jobs": {}}))
        monkeypatch.setattr(md, "_jobs_file", lambda: jobs_file)
        assert md._is_job_enabled() is True


# ---------------------------------------------------------------------------
# query_completed_uows — DB-backed query
# ---------------------------------------------------------------------------

class TestQueryCompletedUows:
    def test_uow_transitioned_to_done_within_window_is_returned(self, db_path):
        u = _uow(uow_id="uow-done", status="done", close_reason="pr merged")
        _insert_uow_row(db_path, u)
        _insert_audit_entry(db_path, "uow-done", "done", hours_ago=1)
        results = md.query_completed_uows(db_path, DEFAULT_LOOKBACK_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-done" in ids

    def test_uow_not_in_audit_window_is_excluded(self, db_path):
        # Transition happened 48h ago — outside the default 24h window
        u = _uow(uow_id="uow-old", status="done", close_reason="pr merged")
        _insert_uow_row(db_path, u)
        _insert_audit_entry(db_path, "uow-old", "done", hours_ago=48)
        results = md.query_completed_uows(db_path, DEFAULT_LOOKBACK_HOURS)
        ids = [r["id"] for r in results]
        assert "uow-old" not in ids

    def test_absent_db_returns_empty_list(self, tmp_path):
        results = md.query_completed_uows(tmp_path / "nonexistent.db", DEFAULT_LOOKBACK_HOURS)
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
        assert "60\u201390 seconds" in prompt

    def test_stop_on_zero_return_value_documented(self, prompt):
        # Agent must stop if write_heartbeat returns 0 (UoW re-queued)
        assert "0" in prompt and "stop" in prompt.lower()

    def test_instructions_still_present_after_heartbeat_section(self, prompt):
        # The heartbeat section must not replace the instructions
        assert "Do the thing." in prompt

    def test_result_contract_present(self, prompt):
        # The result contract (write_result / output_ref) must also be present
        assert "Result contract" in prompt or "result file" in prompt.lower()
