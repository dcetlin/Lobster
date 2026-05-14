"""
Unit tests for wos_uow_detail_gen.py.

Tests cover behavior, not implementation detail:
- _fetch_uow_data: returns structured dict with all UoW fields, None for missing UoW
- _fetch_audit_trail: returns full (uncapped) list of audit events for a UoW
- _fetch_corrective_traces: returns traces list, empty when table absent
- _fetch_heartbeat_log: returns heartbeat list, empty when table absent
- _fetch_token_data: extracts per-UoW token totals from ledger entries
- _compute_elapsed: computes wall-clock duration from timestamps
- _estimate_cost: computes USD cost from token counts using Sonnet 4.6 pricing
- generate_html: produces valid HTML containing UoW id in output
- generate_and_upload: writes file to uploads dir, returns URL with uow id context
- CLI: --uow-id required, exits 1 on missing UoW, exits 0 on success
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.orchestration.wos_uow_detail_gen import (
    SONNET_4_6_CACHE_READ_PER_MTK,
    SONNET_4_6_INPUT_PER_MTK,
    SONNET_4_6_OUTPUT_PER_MTK,
)


# ---------------------------------------------------------------------------
# Fixtures — in-memory SQLite DB with sample data
# ---------------------------------------------------------------------------

def _build_db() -> tuple[sqlite3.Connection, str]:
    """Create an in-memory SQLite DB with uow_registry, audit_log, corrective_traces,
    and uow_heartbeat_log populated with a single sample UoW."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE uow_registry (
            id TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            source_issue_number INTEGER,
            issue_url TEXT,
            outcome_category TEXT,
            steward_cycles INTEGER NOT NULL DEFAULT 0,
            lifetime_cycles INTEGER NOT NULL DEFAULT 0,
            execution_attempts INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            token_usage INTEGER,
            posture TEXT NOT NULL DEFAULT 'solo',
            register TEXT NOT NULL DEFAULT 'operational',
            close_reason TEXT,
            prescription_confidence REAL,
            success_criteria TEXT NOT NULL DEFAULT '',
            prescribed_skills TEXT,
            type TEXT NOT NULL DEFAULT 'executable',
            source TEXT NOT NULL DEFAULT 'github',
            gate_fired TEXT
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            uow_id TEXT NOT NULL,
            event TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            agent TEXT,
            note TEXT
        );

        CREATE TABLE corrective_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uow_id TEXT NOT NULL,
            register TEXT NOT NULL,
            execution_summary TEXT,
            surprises TEXT DEFAULT '[]',
            prescription_delta TEXT,
            gate_score REAL,
            summary TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE uow_heartbeat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uow_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            token_usage INTEGER
        );
    """)

    # Insert a sample UoW
    conn.execute("""
        INSERT INTO uow_registry (
            id, summary, status, created_at, updated_at, started_at, completed_at,
            source_issue_number, issue_url, outcome_category,
            steward_cycles, lifetime_cycles, execution_attempts, retry_count,
            token_usage, posture, register, close_reason, prescription_confidence,
            success_criteria, type, source
        ) VALUES (
            'uow_20260501_abc123',
            'feat: example feature implementation',
            'done',
            '2026-05-01T10:00:00+00:00',
            '2026-05-01T11:30:00+00:00',
            '2026-05-01T11:00:00+00:00',
            '2026-05-01T11:30:00+00:00',
            999,
            'https://github.com/dcetlin/Lobster/issues/999',
            'pearl',
            2,
            4,
            1,
            0,
            50000,
            'solo',
            'operational',
            'Completed successfully',
            0.9,
            'PR merged and tests pass',
            'executable',
            'github'
        )
    """)

    # Insert audit events
    conn.executemany(
        "INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, note) VALUES (?,?,?,?,?,?)",
        [
            ('2026-05-01T10:00:00+00:00', 'uow_20260501_abc123', 'created', None, 'proposed', None),
            ('2026-05-01T10:05:00+00:00', 'uow_20260501_abc123', 'status_change', 'proposed', 'ready-for-steward', None),
            ('2026-05-01T11:00:00+00:00', 'uow_20260501_abc123', 'executor_dispatch', 'ready-for-executor', 'executing', None),
            ('2026-05-01T11:30:00+00:00', 'uow_20260501_abc123', 'execution_complete', 'executing', 'ready-for-steward', None),
            ('2026-05-01T11:35:00+00:00', 'uow_20260501_abc123', 'steward_closure', None, None, None),
        ]
    )

    # Insert corrective trace
    conn.execute("""
        INSERT INTO corrective_traces (uow_id, register, execution_summary, surprises, created_at)
        VALUES ('uow_20260501_abc123', 'operational', 'Executor completed without surprises', '[]', '2026-05-01T11:00:00+00:00')
    """)

    # Insert heartbeat entries
    conn.executemany(
        "INSERT INTO uow_heartbeat_log (uow_id, recorded_at, token_usage) VALUES (?,?,?)",
        [
            ('uow_20260501_abc123', '2026-05-01T11:10:00+00:00', 15000),
            ('uow_20260501_abc123', '2026-05-01T11:20:00+00:00', 32000),
            ('uow_20260501_abc123', '2026-05-01T11:30:00+00:00', 50000),
        ]
    )

    conn.commit()
    return conn, 'uow_20260501_abc123'


def _build_ledger_entries(uow_id: str) -> list[dict]:
    """Build sample token ledger entries for a UoW."""
    return [
        {
            "ts": 1746097260,
            "task_id": f"wos-{uow_id}",
            "input": 10000,
            "output": 20000,
            "cache_read": 80000,
            "cache_write": 5000,
            "model": "claude-sonnet-4-6",
        },
        {
            "ts": 1746097320,
            "task_id": f"wos-{uow_id}",
            "input": 5000,
            "output": 15000,
            "cache_read": 40000,
            "cache_write": 2000,
            "model": "claude-sonnet-4-6",
        },
        # Unrelated entry — should not be included in UoW totals
        {
            "ts": 1746097320,
            "task_id": "wos-uow_20260401_other",
            "input": 1000,
            "output": 500,
            "cache_read": 10000,
            "cache_write": 1000,
            "model": "claude-sonnet-4-6",
        },
    ]


# ---------------------------------------------------------------------------
# _fetch_uow_data
# ---------------------------------------------------------------------------

class TestFetchUoWData:
    def test_returns_dict_with_all_fields_for_existing_uow(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_uow_data
        conn, uow_id = _build_db()
        result = _fetch_uow_data(conn, uow_id)
        assert result is not None
        assert result["id"] == uow_id
        assert result["summary"] == "feat: example feature implementation"
        assert result["status"] == "done"
        assert result["outcome_category"] == "pearl"
        assert result["steward_cycles"] == 2
        assert result["token_usage"] == 50000
        assert result["issue_url"] == "https://github.com/dcetlin/Lobster/issues/999"
        conn.close()

    def test_returns_none_for_missing_uow(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_uow_data
        conn, _ = _build_db()
        result = _fetch_uow_data(conn, "uow_does_not_exist")
        assert result is None
        conn.close()


# ---------------------------------------------------------------------------
# _fetch_audit_trail — full uncapped trail
# ---------------------------------------------------------------------------

AUDIT_TRAIL_LENGTH = 5  # matches events inserted in _build_db()


class TestFetchAuditTrail:
    def test_returns_all_events_uncapped(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_audit_trail
        conn, uow_id = _build_db()
        events = _fetch_audit_trail(conn, uow_id)
        assert len(events) == AUDIT_TRAIL_LENGTH
        conn.close()

    def test_events_ordered_ascending_by_timestamp(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_audit_trail
        conn, uow_id = _build_db()
        events = _fetch_audit_trail(conn, uow_id)
        timestamps = [e["ts"] for e in events]
        assert timestamps == sorted(timestamps)
        conn.close()

    def test_each_event_has_required_keys(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_audit_trail
        conn, uow_id = _build_db()
        events = _fetch_audit_trail(conn, uow_id)
        for event in events:
            assert "ts" in event
            assert "event" in event
        conn.close()

    def test_returns_empty_for_unknown_uow(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_audit_trail
        conn, _ = _build_db()
        events = _fetch_audit_trail(conn, "no_such_uow")
        assert events == []
        conn.close()


# ---------------------------------------------------------------------------
# _fetch_corrective_traces
# ---------------------------------------------------------------------------

class TestFetchCorrectiveTraces:
    def test_returns_traces_for_uow(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_corrective_traces
        conn, uow_id = _build_db()
        traces = _fetch_corrective_traces(conn, uow_id)
        assert len(traces) == 1
        assert traces[0]["execution_summary"] == "Executor completed without surprises"
        conn.close()

    def test_returns_empty_when_no_traces(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_corrective_traces
        conn, _ = _build_db()
        traces = _fetch_corrective_traces(conn, "no_traces_uow")
        assert traces == []
        conn.close()

    def test_returns_empty_when_table_absent(self):
        """Gracefully handles DBs that predate corrective_traces migration."""
        from src.orchestration.wos_uow_detail_gen import _fetch_corrective_traces
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE uow_registry (id TEXT PRIMARY KEY, summary TEXT NOT NULL)")
        conn.commit()
        traces = _fetch_corrective_traces(conn, "uow_20260501_abc123")
        assert traces == []
        conn.close()


# ---------------------------------------------------------------------------
# _fetch_heartbeat_log
# ---------------------------------------------------------------------------

HEARTBEAT_COUNT = 3  # matches entries inserted in _build_db()


class TestFetchHeartbeatLog:
    def test_returns_heartbeats_for_uow(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_heartbeat_log
        conn, uow_id = _build_db()
        beats = _fetch_heartbeat_log(conn, uow_id)
        assert len(beats) == HEARTBEAT_COUNT
        conn.close()

    def test_heartbeats_have_recorded_at_and_token_usage(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_heartbeat_log
        conn, uow_id = _build_db()
        beats = _fetch_heartbeat_log(conn, uow_id)
        for b in beats:
            assert "recorded_at" in b
            assert "token_usage" in b
        conn.close()

    def test_returns_empty_when_table_absent(self):
        """Gracefully handles DBs that predate heartbeat migration."""
        from src.orchestration.wos_uow_detail_gen import _fetch_heartbeat_log
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE uow_registry (id TEXT PRIMARY KEY, summary TEXT NOT NULL)")
        conn.commit()
        beats = _fetch_heartbeat_log(conn, "uow_20260501_abc123")
        assert beats == []
        conn.close()


# ---------------------------------------------------------------------------
# _fetch_token_data
# ---------------------------------------------------------------------------

class TestFetchTokenData:
    def test_aggregates_tokens_for_matching_uow_id(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_token_data
        uow_id = "uow_20260501_abc123"
        entries = _build_ledger_entries(uow_id)
        result = _fetch_token_data(entries, uow_id)
        assert result is not None
        # Two matching entries: input 10000+5000=15000, output 20000+15000=35000
        assert result["input"] == 15000
        assert result["output"] == 35000
        assert result["cache_read"] == 120000
        assert result["cache_write"] == 7000
        assert result["calls"] == 2

    def test_excludes_unrelated_entries(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_token_data
        uow_id = "uow_20260501_abc123"
        entries = _build_ledger_entries(uow_id)
        result = _fetch_token_data(entries, uow_id)
        # The "other" UoW's 1000 input tokens must not appear here
        assert result["input"] == 15000

    def test_returns_none_for_uow_not_in_ledger(self):
        from src.orchestration.wos_uow_detail_gen import _fetch_token_data
        uow_id = "uow_20260501_abc123"
        result = _fetch_token_data([], uow_id)
        assert result is None

    def test_matches_task_id_containing_uow_id(self):
        """task_id can be 'wos-uow_YYYYMMDD_xxxxxx' or just the uow_id."""
        from src.orchestration.wos_uow_detail_gen import _fetch_token_data
        uow_id = "uow_20260501_abc123"
        entries = [
            {"ts": 1000, "task_id": uow_id, "input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
            {"ts": 1001, "task_id": f"wos-{uow_id}", "input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
            {"ts": 1002, "task_id": f"wos-executor-{uow_id}", "input": 1, "output": 2, "cache_read": 0, "cache_write": 0},
        ]
        result = _fetch_token_data(entries, uow_id)
        assert result is not None
        assert result["calls"] == 3


# ---------------------------------------------------------------------------
# _compute_elapsed
# ---------------------------------------------------------------------------

class TestComputeElapsed:
    def test_returns_seconds_between_start_and_end(self):
        from src.orchestration.wos_uow_detail_gen import _compute_elapsed
        result = _compute_elapsed(
            "2026-05-01T11:00:00+00:00",
            "2026-05-01T11:30:00+00:00",
        )
        assert result == 1800  # 30 minutes in seconds

    def test_returns_none_when_either_timestamp_is_none(self):
        from src.orchestration.wos_uow_detail_gen import _compute_elapsed
        assert _compute_elapsed(None, "2026-05-01T11:30:00+00:00") is None
        assert _compute_elapsed("2026-05-01T11:00:00+00:00", None) is None
        assert _compute_elapsed(None, None) is None

    def test_returns_none_for_invalid_timestamps(self):
        from src.orchestration.wos_uow_detail_gen import _compute_elapsed
        assert _compute_elapsed("not-a-date", "2026-05-01T11:30:00+00:00") is None


# ---------------------------------------------------------------------------
# _estimate_cost — Sonnet 4.6 pricing
# ---------------------------------------------------------------------------

class TestEstimateCost:
    def test_zero_tokens_yield_zero_cost(self):
        from src.orchestration.wos_uow_detail_gen import _estimate_cost
        assert _estimate_cost(0, 0, 0) == 0.0

    def test_input_priced_at_three_dollars_per_million(self):
        from src.orchestration.wos_uow_detail_gen import _estimate_cost
        cost = _estimate_cost(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=0)
        assert abs(cost - SONNET_4_6_INPUT_PER_MTK) < 0.0001

    def test_output_priced_at_fifteen_dollars_per_million(self):
        from src.orchestration.wos_uow_detail_gen import _estimate_cost
        cost = _estimate_cost(input_tokens=0, output_tokens=1_000_000, cache_read_tokens=0)
        assert abs(cost - SONNET_4_6_OUTPUT_PER_MTK) < 0.0001

    def test_cache_read_priced_at_thirty_cents_per_million(self):
        from src.orchestration.wos_uow_detail_gen import _estimate_cost
        cost = _estimate_cost(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)
        assert abs(cost - SONNET_4_6_CACHE_READ_PER_MTK) < 0.0001

    def test_combined_cost_sums_all_three_sources(self):
        from src.orchestration.wos_uow_detail_gen import _estimate_cost
        # 100K input: $0.30, 100K output: $1.50, 1M cache_read: $0.30
        cost = _estimate_cost(
            input_tokens=100_000,
            output_tokens=100_000,
            cache_read_tokens=1_000_000,
        )
        expected = (100_000 * 3 + 100_000 * 15) / 1_000_000 + 1_000_000 * 0.30 / 1_000_000
        assert abs(cost - expected) < 0.0001


# ---------------------------------------------------------------------------
# _render_markdown — pure markdown-to-HTML conversion
# ---------------------------------------------------------------------------

class TestRenderMarkdown:
    def test_empty_string_returns_empty(self):
        from src.orchestration.wos_uow_detail_gen import _render_markdown
        assert _render_markdown("") == ""

    def test_whitespace_only_returns_empty(self):
        from src.orchestration.wos_uow_detail_gen import _render_markdown
        assert _render_markdown("   \n  ") == ""

    def test_plain_text_becomes_paragraph(self):
        from src.orchestration.wos_uow_detail_gen import _render_markdown
        result = _render_markdown("PR merged and tests pass")
        assert "<p>" in result
        assert "PR merged and tests pass" in result

    def test_bullet_list_becomes_ul_li(self):
        from src.orchestration.wos_uow_detail_gen import _render_markdown
        result = _render_markdown("- item one\n- item two")
        assert "<ul>" in result
        assert "<li>" in result
        assert "item one" in result
        assert "item two" in result

    def test_bold_text_becomes_strong(self):
        from src.orchestration.wos_uow_detail_gen import _render_markdown
        result = _render_markdown("**important**")
        assert "<strong>" in result
        assert "important" in result

    def test_heading_becomes_h_tag(self):
        from src.orchestration.wos_uow_detail_gen import _render_markdown
        result = _render_markdown("## Section heading")
        assert "<h2>" in result
        assert "Section heading" in result


# ---------------------------------------------------------------------------
# generate_html
# ---------------------------------------------------------------------------

class TestGenerateHtml:
    def test_output_is_valid_html_containing_uow_id(self):
        from src.orchestration.wos_uow_detail_gen import generate_html
        uow_data = {
            "id": "uow_20260501_abc123",
            "summary": "Test feature",
            "status": "done",
            "created_at": "2026-05-01T10:00:00+00:00",
            "updated_at": "2026-05-01T11:30:00+00:00",
            "started_at": "2026-05-01T11:00:00+00:00",
            "completed_at": "2026-05-01T11:30:00+00:00",
            "source_issue_number": 999,
            "issue_url": "https://github.com/dcetlin/Lobster/issues/999",
            "outcome_category": "pearl",
            "steward_cycles": 2,
            "lifetime_cycles": 4,
            "execution_attempts": 1,
            "retry_count": 0,
            "token_usage": 50000,
            "posture": "solo",
            "register": "operational",
            "close_reason": "Completed successfully",
            "prescription_confidence": 0.9,
            "success_criteria": "PR merged and tests pass",
            "gate_fired": None,
        }
        audit_trail = [
            {"ts": "2026-05-01T10:00:00+00:00", "event": "created", "from_status": None, "to_status": "proposed", "note": None, "agent": None},
        ]
        traces = [
            {"execution_summary": "Clean run", "surprises": "[]", "created_at": "2026-05-01T11:00:00+00:00", "gate_score": None, "summary": ""},
        ]
        heartbeats = [
            {"recorded_at": "2026-05-01T11:10:00+00:00", "token_usage": 15000},
        ]
        token_data = {"input": 15000, "output": 35000, "cache_read": 120000, "cache_write": 7000, "calls": 2}

        html = generate_html(uow_data, audit_trail, traces, heartbeats, token_data)

        assert "<!DOCTYPE html>" in html
        assert "uow_20260501_abc123" in html

    def test_html_contains_token_section_when_data_present(self):
        from src.orchestration.wos_uow_detail_gen import generate_html
        uow_data = {
            "id": "uow_20260501_abc123",
            "summary": "Test",
            "status": "done",
            "created_at": "2026-05-01T10:00:00+00:00",
            "updated_at": "2026-05-01T11:30:00+00:00",
            "started_at": None,
            "completed_at": None,
            "source_issue_number": None,
            "issue_url": None,
            "outcome_category": None,
            "steward_cycles": 0,
            "lifetime_cycles": 0,
            "execution_attempts": 0,
            "retry_count": 0,
            "token_usage": None,
            "posture": "solo",
            "register": "operational",
            "close_reason": None,
            "prescription_confidence": None,
            "success_criteria": "",
            "gate_fired": None,
        }
        token_data = {"input": 10000, "output": 20000, "cache_read": 50000, "cache_write": 3000, "calls": 2}
        html = generate_html(uow_data, [], [], [], token_data)
        # Token section header must appear
        assert "Token" in html

    def test_html_handles_none_token_data_gracefully(self):
        from src.orchestration.wos_uow_detail_gen import generate_html
        uow_data = {
            "id": "uow_20260501_abc123",
            "summary": "Test",
            "status": "proposed",
            "created_at": "2026-05-01T10:00:00+00:00",
            "updated_at": "2026-05-01T10:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "source_issue_number": None,
            "issue_url": None,
            "outcome_category": None,
            "steward_cycles": 0,
            "lifetime_cycles": 0,
            "execution_attempts": 0,
            "retry_count": 0,
            "token_usage": None,
            "posture": "solo",
            "register": "operational",
            "close_reason": None,
            "prescription_confidence": None,
            "success_criteria": "",
            "gate_fired": None,
        }
        # Should not raise
        html = generate_html(uow_data, [], [], [], None)
        assert "<!DOCTYPE html>" in html

    def test_success_criteria_markdown_rendered_as_html(self):
        """Markdown in success_criteria must appear as HTML tags, not raw text."""
        from src.orchestration.wos_uow_detail_gen import generate_html
        uow_data = {
            "id": "uow_20260501_md1",
            "summary": "Test",
            "status": "done",
            "created_at": "2026-05-01T10:00:00+00:00",
            "updated_at": "2026-05-01T11:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "source_issue_number": None,
            "issue_url": None,
            "outcome_category": None,
            "steward_cycles": 0,
            "lifetime_cycles": 0,
            "execution_attempts": 0,
            "retry_count": 0,
            "token_usage": None,
            "posture": "solo",
            "register": "operational",
            "close_reason": None,
            "prescription_confidence": None,
            "success_criteria": "- All tests pass\n- PR merged to main",
            "gate_fired": None,
        }
        html = generate_html(uow_data, [], [], [], None)
        # The rendered HTML must contain list tags, not raw markdown hyphens as text
        assert "<ul>" in html
        assert "<li>" in html
        assert "All tests pass" in html

    def test_close_reason_markdown_rendered_as_html(self):
        """Markdown in close_reason must appear as HTML tags, not raw text."""
        from src.orchestration.wos_uow_detail_gen import generate_html
        uow_data = {
            "id": "uow_20260501_md2",
            "summary": "Test",
            "status": "closed",
            "created_at": "2026-05-01T10:00:00+00:00",
            "updated_at": "2026-05-01T11:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "source_issue_number": None,
            "issue_url": None,
            "outcome_category": None,
            "steward_cycles": 0,
            "lifetime_cycles": 0,
            "execution_attempts": 0,
            "retry_count": 0,
            "token_usage": None,
            "posture": "solo",
            "register": "operational",
            "close_reason": "**Blocked**: dependency not resolved",
            "prescription_confidence": None,
            "success_criteria": "",
            "gate_fired": None,
        }
        html = generate_html(uow_data, [], [], [], None)
        # Bold markdown must produce <strong>, not raw ** characters
        assert "<strong>" in html
        assert "Blocked" in html

    def test_empty_success_criteria_produces_empty_html_field(self):
        """When success_criteria is empty, success_criteria_html in the payload must be empty.

        The template conditionally renders the section only when success_criteria_html is
        non-empty (evaluated at runtime in JS). This test verifies that the server-side
        payload correctly produces an empty string for empty input, which prevents the
        section from appearing in the browser.
        """
        from src.orchestration.wos_uow_detail_gen import generate_html
        import json as _json
        uow_data = {
            "id": "uow_20260501_md3",
            "summary": "Test",
            "status": "done",
            "created_at": "2026-05-01T10:00:00+00:00",
            "updated_at": "2026-05-01T11:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "source_issue_number": None,
            "issue_url": None,
            "outcome_category": None,
            "steward_cycles": 0,
            "lifetime_cycles": 0,
            "execution_attempts": 0,
            "retry_count": 0,
            "token_usage": None,
            "posture": "solo",
            "register": "operational",
            "close_reason": None,
            "prescription_confidence": None,
            "success_criteria": "",
            "gate_fired": None,
        }
        html = generate_html(uow_data, [], [], [], None)
        # Extract the embedded JSON payload from the HTML
        # The payload is embedded as: const D = {...};
        import re as _re
        match = _re.search(r'const D = (\{.*?\});', html, _re.DOTALL)
        assert match is not None, "Could not find D JSON payload in HTML"
        payload = _json.loads(match.group(1))
        # Empty success_criteria must produce empty rendered HTML — JS will evaluate this as falsy
        assert payload["success_criteria_html"] == ""


# ---------------------------------------------------------------------------
# generate_and_upload
# ---------------------------------------------------------------------------

class TestGenerateAndUpload:
    def test_writes_file_and_returns_url(self, tmp_path: Path):
        from src.orchestration.wos_uow_detail_gen import generate_and_upload

        # Write a minimal DB to a temp file
        db_path = tmp_path / "registry.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Use the rich in-memory DB data but write it to disk
        base_conn, uow_id = _build_db()
        # Dump schema + data to the file DB
        for line in base_conn.iterdump():
            try:
                conn.execute(line)
            except Exception:
                pass
        conn.commit()
        base_conn.close()

        # Write a minimal ledger
        ledger_path = tmp_path / "token-ledger.jsonl"
        entries = _build_ledger_entries(uow_id)
        with ledger_path.open("w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        uploads_dir = tmp_path / "bisque-uploads"

        with patch("src.orchestration.wos_uow_detail_gen._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_uow_detail_gen._bisque_base_url", return_value="http://test:9101"):
            url = generate_and_upload(uow_id=uow_id, db_path=db_path, ledger_path=ledger_path)

        # File must exist
        html_files = list(uploads_dir.glob("*.html"))
        assert len(html_files) == 1

        # URL must reference the file
        filename = html_files[0].name
        assert url == f"http://test:9101/files/{filename}"

        # File must contain uow id
        content = html_files[0].read_text()
        assert uow_id in content

    def test_raises_value_error_for_missing_uow(self, tmp_path: Path):
        from src.orchestration.wos_uow_detail_gen import generate_and_upload

        # Build a full-schema DB (from _build_db) so column checks pass, but
        # request a UoW that doesn't exist — the function should raise ValueError.
        db_path = tmp_path / "registry.db"
        base_conn, _ = _build_db()
        dest_conn = sqlite3.connect(str(db_path))
        for line in base_conn.iterdump():
            try:
                dest_conn.execute(line)
            except Exception:
                pass
        dest_conn.commit()
        base_conn.close()
        dest_conn.close()

        ledger_path = tmp_path / "ledger.jsonl"
        ledger_path.write_text("")

        with pytest.raises(ValueError, match="not found"):
            generate_and_upload(
                uow_id="uow_does_not_exist",
                db_path=db_path,
                ledger_path=ledger_path,
            )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

class TestCli:
    def test_exits_1_when_no_uow_id_given(self, tmp_path: Path, capsys):
        """--uow-id is required; missing it should exit non-zero."""
        import sys
        from src.orchestration.wos_uow_detail_gen import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--db", str(tmp_path / "registry.db")])
        assert exc_info.value.code != 0

    def test_exits_1_when_uow_not_found(self, tmp_path: Path):
        from src.orchestration.wos_uow_detail_gen import main

        # Build a full-schema DB so column checks pass, but request a nonexistent UoW.
        db_path = tmp_path / "registry.db"
        base_conn, _ = _build_db()
        dest_conn = sqlite3.connect(str(db_path))
        for line in base_conn.iterdump():
            try:
                dest_conn.execute(line)
            except Exception:
                pass
        dest_conn.commit()
        base_conn.close()
        dest_conn.close()

        # main() returns an exit code (int) when called directly.
        result = main(["--uow-id", "uow_does_not_exist", "--db", str(db_path)])
        assert result == 1

    def test_exits_0_on_success(self, tmp_path: Path, capsys):
        import sys
        from src.orchestration.wos_uow_detail_gen import main

        db_path = tmp_path / "registry.db"
        conn = sqlite3.connect(str(db_path))
        base_conn, uow_id = _build_db()
        for line in base_conn.iterdump():
            try:
                conn.execute(line)
            except Exception:
                pass
        conn.commit()
        base_conn.close()
        conn.close()

        ledger_path = tmp_path / "ledger.jsonl"
        ledger_path.write_text("")

        uploads_dir = tmp_path / "bisque-uploads"

        with patch("src.orchestration.wos_uow_detail_gen._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_uow_detail_gen._bisque_base_url", return_value="http://test:9101"):
            result = main(["--uow-id", uow_id, "--db", str(db_path), "--ledger", str(ledger_path)])

        assert result == 0
