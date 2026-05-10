"""
Unit tests for wos_dashboard.py.

All Registry and audit_queries interactions are mocked — no real SQLite DB.
Tests cover:
- _active_uows: filters to active/ready-for-executor statuses, computes time_in_state
- _throughput_24h: delegates to execution_outcomes, maps key names
- _cycle_histogram_last_7d: groups by steward_cycles for completed UoWs
- _stalled_uows: filters by status + elapsed threshold
- _bootup_gate_status: calls is_bootup_candidate_gate_active()
- build_dashboard_data: assembles all sections into a single dict
- render_text: renders expected section headers and data
- render_text: empty states render '(none)' placeholders
- generate_drilldown_urls: generates URL map for given UoW IDs, skips on error
- render_html: produces valid HTML with UoW table and drilldown links when urls provided
- render_html: renders UoW IDs as plain text when no drilldown URLs provided
- render_html: displays issue title when provided
- render_html: displays category badge when provided
- main(): exits 0, text format default
- main(): --format json outputs valid JSON
- main(): --format html writes to canonical filename and outputs URL
- main(): --with-drilldowns flag calls generate_drilldown_urls
- _fetch_issue_metadata: returns {title, labels} dict for valid issue URL
- _fetch_issue_metadata: returns None when issue URL is missing or empty
- _fetch_issue_metadata: returns None on subprocess failure
- _fetch_issue_metadata: returns None on malformed JSON
- _enrich_uow_with_github_metadata: injects title and category when issue_url present
- _enrich_uow_with_github_metadata: passthrough with defaults when no issue_url
- _enrich_uow_with_github_metadata: graceful fallback when metadata fetch returns None
- _derive_category_from_labels: type:bug → "bug"
- _derive_category_from_labels: type:feat → "feature"
- _derive_category_from_labels: workstream:wos → "wos"
- _derive_category_from_labels: type: preferred over workstream: when both present
- _derive_category_from_labels: no matching labels → "general"
- _derive_category_from_labels: empty labels → "general"
- upload_html: writes to canonical filename, returns stable URL
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_uow(
    id: str = "uow_20260101_aaa",
    status: str = "active",
    steward_cycles: int = 0,
    updated_at: str | None = None,
) -> MagicMock:
    """Create a mock UoW value object."""
    uow = MagicMock()
    uow.id = id
    uow.status = status
    uow.steward_cycles = steward_cycles
    # Default updated_at: 10 minutes ago
    if updated_at is None:
        updated_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    uow.updated_at = updated_at
    return uow


def _make_registry(uows: list[MagicMock] | None = None) -> MagicMock:
    """Create a mock Registry whose .list() returns the given UoWs."""
    registry = MagicMock()
    uows_list = uows or []
    registry.list.return_value = uows_list
    # registry.get(id) maps by id
    def _get(uow_id: str):
        for u in uows_list:
            if u.id == uow_id:
                return u
        return None
    registry.get.side_effect = _get
    return registry


# ---------------------------------------------------------------------------
# _active_uows
# ---------------------------------------------------------------------------

class TestActiveUows:
    def test_returns_active_uows(self):
        from src.orchestration.wos_dashboard import _active_uows
        uow = _make_uow(id="uow_1", status="active", steward_cycles=2)
        registry = _make_registry([uow])
        result = _active_uows(registry)
        assert len(result) == 1
        assert result[0]["id"] == "uow_1"
        assert result[0]["status"] == "active"
        assert result[0]["steward_cycles"] == 2
        assert result[0]["time_in_state_seconds"] >= 0

    def test_returns_ready_for_executor(self):
        from src.orchestration.wos_dashboard import _active_uows
        uow = _make_uow(id="uow_2", status="ready-for-executor", steward_cycles=1)
        registry = _make_registry([uow])
        result = _active_uows(registry)
        assert len(result) == 1
        assert result[0]["status"] == "ready-for-executor"

    def test_excludes_other_statuses(self):
        from src.orchestration.wos_dashboard import _active_uows
        uows = [
            _make_uow(id="uow_a", status="done"),
            _make_uow(id="uow_b", status="ready-for-steward"),
            _make_uow(id="uow_c", status="proposed"),
        ]
        registry = _make_registry(uows)
        result = _active_uows(registry)
        assert result == []

    def test_time_in_state_computed(self):
        from src.orchestration.wos_dashboard import _active_uows
        # UoW updated 1 hour ago
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        uow = _make_uow(id="uow_3", status="active", updated_at=one_hour_ago)
        registry = _make_registry([uow])
        result = _active_uows(registry)
        assert result[0]["time_in_state_seconds"] >= 3590  # allow small margin

    def test_mixed_statuses_only_active_returned(self):
        from src.orchestration.wos_dashboard import _active_uows
        uows = [
            _make_uow(id="uow_active", status="active"),
            _make_uow(id="uow_rfe", status="ready-for-executor"),
            _make_uow(id="uow_done", status="done"),
        ]
        registry = _make_registry(uows)
        result = _active_uows(registry)
        returned_ids = {r["id"] for r in result}
        assert returned_ids == {"uow_active", "uow_rfe"}


# ---------------------------------------------------------------------------
# _throughput_24h
# ---------------------------------------------------------------------------

class TestThroughput24h:
    def test_delegates_to_execution_outcomes(self, tmp_path):
        from src.orchestration.wos_dashboard import _throughput_24h
        fake_outcomes = {"execution_complete": 5, "execution_failed": 2}
        with patch(
            "src.orchestration.audit_queries.execution_outcomes",
            return_value=fake_outcomes,
        ):
            result = _throughput_24h(tmp_path / "registry.db")
        assert result == {"completed": 5, "failed": 2}

    def test_missing_keys_default_to_zero(self, tmp_path):
        from src.orchestration.wos_dashboard import _throughput_24h
        with patch(
            "src.orchestration.audit_queries.execution_outcomes",
            return_value={},
        ):
            result = _throughput_24h(tmp_path / "registry.db")
        assert result == {"completed": 0, "failed": 0}

    def test_only_completed_key_present(self, tmp_path):
        from src.orchestration.wos_dashboard import _throughput_24h
        with patch(
            "src.orchestration.audit_queries.execution_outcomes",
            return_value={"execution_complete": 3},
        ):
            result = _throughput_24h(tmp_path / "registry.db")
        assert result["completed"] == 3
        assert result["failed"] == 0


# ---------------------------------------------------------------------------
# _cycle_histogram_last_7d
# ---------------------------------------------------------------------------

class TestCycleHistogram:
    def test_groups_by_steward_cycles(self, tmp_path):
        from src.orchestration.wos_dashboard import _cycle_histogram_last_7d

        uow_a = _make_uow(id="uow_a", steward_cycles=1)
        uow_b = _make_uow(id="uow_b", steward_cycles=2)
        uow_c = _make_uow(id="uow_c", steward_cycles=1)
        registry = _make_registry([uow_a, uow_b, uow_c])

        db_path = tmp_path / "registry.db"
        with patch(
            "src.orchestration.wos_dashboard._fetch_completed_uow_ids_since",
            return_value=["uow_a", "uow_b", "uow_c"],
        ):
            result = _cycle_histogram_last_7d(registry, db_path)

        assert result == {"cycles=1": 2, "cycles=2": 1}

    def test_empty_when_no_completions(self, tmp_path):
        from src.orchestration.wos_dashboard import _cycle_histogram_last_7d
        registry = _make_registry([])
        db_path = tmp_path / "registry.db"
        with patch(
            "src.orchestration.wos_dashboard._fetch_completed_uow_ids_since",
            return_value=[],
        ):
            result = _cycle_histogram_last_7d(registry, db_path)
        assert result == {}

    def test_sorted_by_cycle_count(self, tmp_path):
        from src.orchestration.wos_dashboard import _cycle_histogram_last_7d
        uow_a = _make_uow(id="uow_a", steward_cycles=3)
        uow_b = _make_uow(id="uow_b", steward_cycles=1)
        registry = _make_registry([uow_a, uow_b])
        db_path = tmp_path / "registry.db"
        with patch(
            "src.orchestration.wos_dashboard._fetch_completed_uow_ids_since",
            return_value=["uow_a", "uow_b"],
        ):
            result = _cycle_histogram_last_7d(registry, db_path)
        keys = list(result.keys())
        # Should be sorted ascending by cycle number
        assert keys == ["cycles=1", "cycles=3"]


# ---------------------------------------------------------------------------
# _stalled_uows
# ---------------------------------------------------------------------------

class TestStalledUows:
    def test_flags_ready_for_steward_over_threshold(self):
        from src.orchestration.wos_dashboard import _stalled_uows
        # Updated 45 minutes ago — should be flagged
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        uow = _make_uow(id="uow_stale", status="ready-for-steward", updated_at=stale_time)
        registry = _make_registry([uow])
        result = _stalled_uows(registry, stall_threshold_minutes=30)
        assert len(result) == 1
        assert result[0]["id"] == "uow_stale"
        assert result[0]["time_in_state_seconds"] >= 2700

    def test_does_not_flag_under_threshold(self):
        from src.orchestration.wos_dashboard import _stalled_uows
        # Updated 10 minutes ago — should NOT be flagged
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        uow = _make_uow(id="uow_fresh", status="ready-for-steward", updated_at=recent_time)
        registry = _make_registry([uow])
        result = _stalled_uows(registry, stall_threshold_minutes=30)
        assert result == []

    def test_flags_ready_for_executor_over_threshold(self):
        from src.orchestration.wos_dashboard import _stalled_uows
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        uow = _make_uow(id="uow_rfe_stale", status="ready-for-executor", updated_at=stale_time)
        registry = _make_registry([uow])
        result = _stalled_uows(registry, stall_threshold_minutes=30)
        assert len(result) == 1

    def test_ignores_active_status(self):
        from src.orchestration.wos_dashboard import _stalled_uows
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        uow = _make_uow(id="uow_active", status="active", updated_at=stale_time)
        registry = _make_registry([uow])
        result = _stalled_uows(registry, stall_threshold_minutes=30)
        assert result == []


# ---------------------------------------------------------------------------
# _bootup_gate_status
# ---------------------------------------------------------------------------

class TestBootupGateStatus:
    def test_gate_open_counts_ready_for_steward(self):
        from src.orchestration.wos_dashboard import _bootup_gate_status
        uow_rfs = _make_uow(id="uow_x", status="ready-for-steward")
        registry = _make_registry([uow_rfs])
        # list(status=...) should return UoWs in that status
        registry.list.side_effect = lambda status=None: (
            [uow_rfs] if status == "ready-for-steward" else []
        )
        with patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=True):
            result = _bootup_gate_status(registry)
        assert result["gate_open"] is True
        assert result["blocked_count"] == 1
        assert "OPEN" in result["description"]

    def test_gate_closed_reports_zero_blocked(self):
        from src.orchestration.wos_dashboard import _bootup_gate_status
        registry = _make_registry([])
        registry.list.side_effect = lambda status=None: []
        with patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False):
            result = _bootup_gate_status(registry)
        assert result["gate_open"] is False
        assert result["blocked_count"] == 0
        assert "CLOSED" in result["description"]


# ---------------------------------------------------------------------------
# build_dashboard_data
# ---------------------------------------------------------------------------

class TestBuildDashboardData:
    def test_contains_all_sections(self, tmp_path):
        from src.orchestration.wos_dashboard import build_dashboard_data
        registry = _make_registry([])
        db_path = tmp_path / "registry.db"

        with patch("src.orchestration.audit_queries.execution_outcomes", return_value={}), \
             patch("src.orchestration.wos_dashboard._fetch_completed_uow_ids_since", return_value=[]), \
             patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=True):
            data = build_dashboard_data(registry, db_path)

        assert "generated_at" in data
        assert "active_uows" in data
        assert "throughput_24h" in data
        assert "cycle_histogram_7d" in data
        assert "stalled_uows" in data
        assert "bootup_candidate_gate" in data

    def test_generated_at_is_iso_utc(self, tmp_path):
        from src.orchestration.wos_dashboard import build_dashboard_data
        registry = _make_registry([])
        db_path = tmp_path / "registry.db"

        with patch("src.orchestration.audit_queries.execution_outcomes", return_value={}), \
             patch("src.orchestration.wos_dashboard._fetch_completed_uow_ids_since", return_value=[]), \
             patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False):
            data = build_dashboard_data(registry, db_path)

        # Should parse without error
        ts = datetime.fromisoformat(data["generated_at"])
        assert ts.tzinfo is not None


# ---------------------------------------------------------------------------
# render_text
# ---------------------------------------------------------------------------

class TestRenderText:
    def _empty_data(self) -> dict:
        return {
            "generated_at": "2026-03-30T12:00:00+00:00",
            "active_uows": [],
            "throughput_24h": {"completed": 0, "failed": 0},
            "cycle_histogram_7d": {},
            "stalled_uows": [],
            "bootup_candidate_gate": {
                "gate_open": False,
                "blocked_count": 0,
                "description": "gate is CLOSED — all UoWs are processed normally",
            },
        }

    def test_has_all_sections(self):
        from src.orchestration.wos_dashboard import render_text
        text = render_text(self._empty_data())
        assert "[1] Active UoWs" in text
        assert "[2] Throughput" in text
        assert "[3] Steward-cycle distribution" in text
        assert "[4] Active stalls" in text
        assert "[5] BOOTUP_CANDIDATE_GATE" in text

    def test_empty_active_shows_none(self):
        from src.orchestration.wos_dashboard import render_text
        text = render_text(self._empty_data())
        assert "(none)" in text

    def test_active_uow_shown_in_text(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["active_uows"] = [{
            "id": "uow_20260101_abc",
            "status": "active",
            "steward_cycles": 3,
            "time_in_state_seconds": 120,
        }]
        text = render_text(data)
        assert "uow_20260101_abc" in text
        assert "cycles=3" in text

    def test_stall_shown_in_text(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["stalled_uows"] = [{
            "id": "uow_stalled_x",
            "status": "ready-for-steward",
            "time_in_state_seconds": 2700,
        }]
        text = render_text(data)
        assert "STALLED" in text
        assert "uow_stalled_x" in text

    def test_throughput_displayed(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["throughput_24h"] = {"completed": 7, "failed": 2}
        text = render_text(data)
        assert "completed: 7" in text
        assert "failed: 2" in text

    def test_histogram_displayed(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["cycle_histogram_7d"] = {"cycles=1": 3, "cycles=2": 5}
        text = render_text(data)
        assert "cycles=1: 3" in text
        assert "cycles=2: 5" in text

    def test_gate_open_displayed(self):
        from src.orchestration.wos_dashboard import render_text
        data = self._empty_data()
        data["bootup_candidate_gate"] = {
            "gate_open": True,
            "blocked_count": 4,
            "description": "gate is OPEN — bootup-candidate UoWs are skipped by the Steward",
        }
        text = render_text(data)
        assert "OPEN" in text
        assert "4" in text


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------

class TestMain:
    def _patch_all(self, tmp_path: Path):
        """Context manager stack: patch Registry + all data sources."""
        from contextlib import ExitStack
        stack = ExitStack()
        registry = _make_registry([])
        registry.list.return_value = []

        stack.enter_context(patch(
            "src.orchestration.registry.Registry",
            return_value=registry,
        ))
        stack.enter_context(patch(
            "src.orchestration.audit_queries.execution_outcomes",
            return_value={},
        ))
        stack.enter_context(patch(
            "src.orchestration.wos_dashboard._fetch_completed_uow_ids_since",
            return_value=[],
        ))
        stack.enter_context(patch(
            "src.orchestration.steward.BOOTUP_CANDIDATE_GATE",
            False,
        ))
        return stack

    def test_exits_zero_text(self, tmp_path, capsys):
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        with self._patch_all(tmp_path):
            rc = main(["--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "WOS Dashboard" in out

    def test_exits_zero_json(self, tmp_path, capsys):
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        with self._patch_all(tmp_path):
            rc = main(["--db", str(db), "--format", "json"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "generated_at" in parsed
        assert "active_uows" in parsed

    def test_default_format_is_text(self, tmp_path, capsys):
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        with self._patch_all(tmp_path):
            rc = main(["--db", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        # Text output has section headers, not JSON
        assert "[1]" in out

    def test_format_html_outputs_canonical_url(self, tmp_path, capsys):
        """--format html outputs the stable canonical URL (not raw HTML) to stdout."""
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        uploads_dir = tmp_path / "bisque-uploads"
        with self._patch_all(tmp_path), \
             patch("src.orchestration.wos_dashboard._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_dashboard._bisque_base_url", return_value="http://test:9101"):
            rc = main(["--db", str(db), "--format", "html"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "http://test:9101/files/wos-dashboard-active.html"

    def test_with_drilldowns_calls_generate_drilldown_urls(self, tmp_path, capsys):
        """--with-drilldowns causes generate_drilldown_urls to be called for each active UoW."""
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        uploads_dir = tmp_path / "bisque-uploads"
        with self._patch_all(tmp_path), \
             patch("src.orchestration.wos_dashboard.generate_drilldown_urls", return_value={}) as mock_gen, \
             patch("src.orchestration.wos_dashboard._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_dashboard._bisque_base_url", return_value="http://test:9101"):
            rc = main(["--db", str(db), "--format", "html", "--with-drilldowns"])
        assert rc == 0
        mock_gen.assert_called_once()

    def test_without_drilldowns_does_not_call_generate_drilldown_urls(self, tmp_path, capsys):
        """Without --with-drilldowns, generate_drilldown_urls is NOT called."""
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        uploads_dir = tmp_path / "bisque-uploads"
        with self._patch_all(tmp_path), \
             patch("src.orchestration.wos_dashboard.generate_drilldown_urls", return_value={}) as mock_gen, \
             patch("src.orchestration.wos_dashboard._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_dashboard._bisque_base_url", return_value="http://test:9101"):
            rc = main(["--db", str(db), "--format", "html"])
        assert rc == 0
        mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# generate_drilldown_urls
# ---------------------------------------------------------------------------

class TestGenerateDrilldownUrls:
    def test_returns_url_map_for_given_uow_ids(self):
        """generate_drilldown_urls returns {uow_id: url} for each id that succeeds."""
        from src.orchestration.wos_dashboard import generate_drilldown_urls

        def fake_generate(uow_id, db_path=None, ledger_path=None):
            return f"http://test:9101/files/{uow_id}.html"

        with patch(
            "src.orchestration.wos_uow_detail_gen.generate_and_upload",
            side_effect=fake_generate,
        ):
            result = generate_drilldown_urls(
                uow_ids=["uow_a", "uow_b"],
                db_path=None,
                ledger_path=None,
            )

        assert result == {
            "uow_a": "http://test:9101/files/uow_a.html",
            "uow_b": "http://test:9101/files/uow_b.html",
        }

    def test_skips_uow_on_error_without_raising(self):
        """If generate_and_upload raises for one UoW, that UoW is omitted; others succeed."""
        from src.orchestration.wos_dashboard import generate_drilldown_urls

        def fake_generate(uow_id, db_path=None, ledger_path=None):
            if uow_id == "uow_bad":
                raise ValueError("UoW not found")
            return f"http://test:9101/files/{uow_id}.html"

        with patch(
            "src.orchestration.wos_uow_detail_gen.generate_and_upload",
            side_effect=fake_generate,
        ):
            result = generate_drilldown_urls(
                uow_ids=["uow_good", "uow_bad"],
                db_path=None,
                ledger_path=None,
            )

        assert "uow_good" in result
        assert "uow_bad" not in result

    def test_empty_ids_returns_empty_dict(self):
        from src.orchestration.wos_dashboard import generate_drilldown_urls
        result = generate_drilldown_urls(uow_ids=[], db_path=None, ledger_path=None)
        assert result == {}


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------

class TestRenderHtml:
    def _base_data(self) -> dict:
        return {
            "generated_at": "2026-05-10T12:00:00+00:00",
            "active_uows": [
                {
                    "id": "uow_20260101_abc",
                    "status": "active",
                    "steward_cycles": 3,
                    "time_in_state_seconds": 600,
                }
            ],
            "throughput_24h": {"completed": 5, "failed": 1},
            "cycle_histogram_7d": {"cycles=1": 3, "cycles=2": 1},
            "stalled_uows": [],
            "bootup_candidate_gate": {
                "gate_open": False,
                "blocked_count": 0,
                "description": "gate is CLOSED — all UoWs are processed normally",
            },
        }

    def test_output_is_valid_html(self):
        from src.orchestration.wos_dashboard import render_html
        html = render_html(self._base_data(), drilldown_urls={})
        assert "<!DOCTYPE html>" in html
        assert "<html" in html
        assert "</html>" in html

    def test_uow_id_appears_in_html(self):
        from src.orchestration.wos_dashboard import render_html
        html = render_html(self._base_data(), drilldown_urls={})
        assert "uow_20260101_abc" in html

    def test_drilldown_link_rendered_when_url_provided(self):
        """UoW ID cell becomes a link when a drilldown URL is available."""
        from src.orchestration.wos_dashboard import render_html
        urls = {"uow_20260101_abc": "http://test:9101/files/abc.html"}
        html = render_html(self._base_data(), drilldown_urls=urls)
        assert 'href="http://test:9101/files/abc.html"' in html
        assert "uow_20260101_abc" in html

    def test_no_link_when_no_drilldown_url(self):
        """When drilldown_urls is empty, UoW ID appears as plain text (no href link for it)."""
        from src.orchestration.wos_dashboard import render_html
        html = render_html(self._base_data(), drilldown_urls={})
        # The uow id appears but there should be no drilldown href for it
        assert "uow_20260101_abc" in html
        assert "http://test:9101/files/" not in html

    def test_dashboard_title_in_html(self):
        from src.orchestration.wos_dashboard import render_html
        html = render_html(self._base_data(), drilldown_urls={})
        assert "WOS Dashboard" in html

    def test_throughput_numbers_in_html(self):
        from src.orchestration.wos_dashboard import render_html
        html = render_html(self._base_data(), drilldown_urls={})
        assert "5" in html   # completed count
        assert "1" in html   # failed count

    def test_stall_section_present(self):
        from src.orchestration.wos_dashboard import render_html
        data = self._base_data()
        data["stalled_uows"] = [{
            "id": "uow_stalled",
            "status": "ready-for-steward",
            "time_in_state_seconds": 3600,
        }]
        html = render_html(data, drilldown_urls={})
        assert "uow_stalled" in html

    def test_drilldown_links_for_stalled_uow(self):
        """Stalled UoWs also get drilldown links when URLs are available."""
        from src.orchestration.wos_dashboard import render_html
        data = self._base_data()
        data["stalled_uows"] = [{
            "id": "uow_stalled_x",
            "status": "ready-for-steward",
            "time_in_state_seconds": 3600,
        }]
        urls = {"uow_stalled_x": "http://test:9101/files/stalled.html"}
        html = render_html(data, drilldown_urls=urls)
        assert 'href="http://test:9101/files/stalled.html"' in html

    def test_issue_title_displayed_in_active_row(self):
        """When a UoW row has an issue_title, it appears prominently in the row HTML."""
        from src.orchestration.wos_dashboard import render_html
        data = self._base_data()
        data["active_uows"] = [{
            "id": "uow_20260101_abc",
            "status": "active",
            "steward_cycles": 3,
            "time_in_state_seconds": 600,
            "issue_title": "Fix flaky CI pipeline",
            "category": "bug",
        }]
        html = render_html(data, drilldown_urls={})
        assert "Fix flaky CI pipeline" in html

    def test_category_badge_displayed_in_active_row(self):
        """When a UoW row has a category, a badge appears in the row HTML."""
        from src.orchestration.wos_dashboard import render_html
        data = self._base_data()
        data["active_uows"] = [{
            "id": "uow_20260101_abc",
            "status": "active",
            "steward_cycles": 3,
            "time_in_state_seconds": 600,
            "issue_title": "Some feature",
            "category": "feature",
        }]
        html = render_html(data, drilldown_urls={})
        assert "feature" in html

    def test_missing_title_renders_empty_dash(self):
        """When issue_title is absent from a UoW row, render falls back gracefully."""
        from src.orchestration.wos_dashboard import render_html
        data = self._base_data()
        # base_data has a UoW without issue_title/category keys
        html = render_html(data, drilldown_urls={})
        # Should render without error and include the UoW id
        assert "uow_20260101_abc" in html


# ---------------------------------------------------------------------------
# _fetch_issue_metadata
# ---------------------------------------------------------------------------

class TestFetchIssueMetadata:
    _URL = "https://github.com/SiderealPress/lobster/issues/42"

    def test_returns_title_and_labels_for_valid_issue_url(self):
        """Returns {title, labels} dict when gh CLI returns both fields."""
        from src.orchestration.wos_dashboard import _fetch_issue_metadata
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = '{"title": "Fix flaky CI pipeline", "labels": [{"name": "type:bug"}]}'
        with patch("subprocess.run", return_value=fake_result):
            result = _fetch_issue_metadata(self._URL)
        assert result is not None
        assert result["title"] == "Fix flaky CI pipeline"
        assert result["labels"] == [{"name": "type:bug"}]

    def test_uses_single_combined_gh_call(self):
        """Verifies only one subprocess.run call is made (combined --json title,labels)."""
        from src.orchestration.wos_dashboard import _fetch_issue_metadata
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = '{"title": "My Issue", "labels": []}'
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            _fetch_issue_metadata(self._URL)
        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert "--json" in cmd
        # The json fields argument must contain both title and labels in a single call
        json_arg = cmd[cmd.index("--json") + 1]
        assert "title" in json_arg
        assert "labels" in json_arg

    def test_returns_none_when_issue_url_is_none(self):
        """Returns None when no issue URL is provided."""
        from src.orchestration.wos_dashboard import _fetch_issue_metadata
        assert _fetch_issue_metadata(None) is None

    def test_returns_none_when_issue_url_is_empty(self):
        """Returns None when issue URL is an empty string."""
        from src.orchestration.wos_dashboard import _fetch_issue_metadata
        assert _fetch_issue_metadata("") is None

    def test_returns_none_on_subprocess_failure(self):
        """Returns None when gh CLI returns non-zero exit code."""
        from src.orchestration.wos_dashboard import _fetch_issue_metadata
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        with patch("subprocess.run", return_value=fake_result):
            assert _fetch_issue_metadata(self._URL) is None

    def test_returns_none_on_subprocess_exception(self):
        """Returns None when subprocess.run raises an exception."""
        from src.orchestration.wos_dashboard import _fetch_issue_metadata
        with patch("subprocess.run", side_effect=Exception("gh not found")):
            assert _fetch_issue_metadata(self._URL) is None

    def test_returns_none_on_malformed_json(self):
        """Returns None when gh CLI returns unparseable JSON."""
        from src.orchestration.wos_dashboard import _fetch_issue_metadata
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "not valid json"
        with patch("subprocess.run", return_value=fake_result):
            assert _fetch_issue_metadata(self._URL) is None

    def test_empty_labels_normalised_to_list(self):
        """Returns labels as empty list when gh returns null or absent labels field."""
        from src.orchestration.wos_dashboard import _fetch_issue_metadata
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = '{"title": "My Issue"}'
        with patch("subprocess.run", return_value=fake_result):
            result = _fetch_issue_metadata(self._URL)
        assert result is not None
        assert result["labels"] == []


# ---------------------------------------------------------------------------
# _enrich_uow_with_github_metadata
# ---------------------------------------------------------------------------

class TestEnrichUowWithGithubMetadata:
    _URL = "https://github.com/SiderealPress/lobster/issues/42"

    def _uow_with_url(self) -> dict:
        return {
            "id": "uow_20260101_abc",
            "status": "active",
            "steward_cycles": 2,
            "time_in_state_seconds": 300,
            "issue_url": self._URL,
        }

    def _uow_without_url(self) -> dict:
        return {
            "id": "uow_20260101_xyz",
            "status": "active",
            "steward_cycles": 1,
            "time_in_state_seconds": 60,
            "issue_url": None,
        }

    def test_injects_title_and_category_when_issue_url_present(self):
        """When issue_url is present and metadata fetch succeeds, title and category are injected."""
        from src.orchestration.wos_dashboard import _enrich_uow_with_github_metadata
        metadata = {"title": "Fix flaky CI pipeline", "labels": [{"name": "type:bug"}]}
        with patch("src.orchestration.wos_dashboard._fetch_issue_metadata", return_value=metadata):
            result = _enrich_uow_with_github_metadata(self._uow_with_url())
        assert result["issue_title"] == "Fix flaky CI pipeline"
        assert result["category"] == "bug"
        # Original fields are preserved
        assert result["id"] == "uow_20260101_abc"
        assert result["steward_cycles"] == 2

    def test_passthrough_with_defaults_when_no_issue_url(self):
        """UoW without issue_url gets issue_title=None and category='general' without any gh call."""
        from src.orchestration.wos_dashboard import _enrich_uow_with_github_metadata
        with patch("src.orchestration.wos_dashboard._fetch_issue_metadata") as mock_fetch:
            result = _enrich_uow_with_github_metadata(self._uow_without_url())
        mock_fetch.assert_not_called()
        assert result["issue_title"] is None
        assert result["category"] == "general"
        assert result["id"] == "uow_20260101_xyz"

    def test_graceful_fallback_when_metadata_fetch_returns_none(self):
        """When _fetch_issue_metadata returns None, defaults are used and no exception raised."""
        from src.orchestration.wos_dashboard import _enrich_uow_with_github_metadata
        with patch("src.orchestration.wos_dashboard._fetch_issue_metadata", return_value=None):
            result = _enrich_uow_with_github_metadata(self._uow_with_url())
        assert result["issue_title"] is None
        assert result["category"] == "general"
        # Original fields are still preserved
        assert result["id"] == "uow_20260101_abc"

    def test_original_uow_dict_is_not_mutated(self):
        """_enrich_uow_with_github_metadata returns a new dict; the original is unchanged."""
        from src.orchestration.wos_dashboard import _enrich_uow_with_github_metadata
        original = self._uow_with_url()
        original_copy = dict(original)
        metadata = {"title": "A title", "labels": []}
        with patch("src.orchestration.wos_dashboard._fetch_issue_metadata", return_value=metadata):
            _enrich_uow_with_github_metadata(original)
        assert original == original_copy  # unchanged


# ---------------------------------------------------------------------------
# _derive_category_from_labels
# ---------------------------------------------------------------------------

class TestDeriveCategoryFromLabels:
    def test_type_bug_returns_bug(self):
        """type:bug label → category 'bug'."""
        from src.orchestration.wos_dashboard import _derive_category_from_labels
        labels = [{"name": "type:bug"}, {"name": "priority:high"}]
        assert _derive_category_from_labels(labels) == "bug"

    def test_type_feat_returns_feature(self):
        """type:feat label → category 'feature'."""
        from src.orchestration.wos_dashboard import _derive_category_from_labels
        labels = [{"name": "type:feat"}]
        assert _derive_category_from_labels(labels) == "feature"

    def test_type_label_preferred_over_workstream(self):
        """When both type: and workstream: labels present, type: wins."""
        from src.orchestration.wos_dashboard import _derive_category_from_labels
        labels = [{"name": "workstream:wos"}, {"name": "type:bug"}]
        assert _derive_category_from_labels(labels) == "bug"

    def test_workstream_label_used_when_no_type(self):
        """workstream:wos → category 'wos' when no type: label present."""
        from src.orchestration.wos_dashboard import _derive_category_from_labels
        labels = [{"name": "workstream:wos"}, {"name": "priority:high"}]
        assert _derive_category_from_labels(labels) == "wos"

    def test_no_matching_labels_returns_general(self):
        """No type: or workstream: labels → 'general'."""
        from src.orchestration.wos_dashboard import _derive_category_from_labels
        labels = [{"name": "priority:high"}, {"name": "status:blocked"}]
        assert _derive_category_from_labels(labels) == "general"

    def test_empty_labels_returns_general(self):
        """Empty labels list → 'general'."""
        from src.orchestration.wos_dashboard import _derive_category_from_labels
        assert _derive_category_from_labels([]) == "general"

    def test_none_labels_returns_general(self):
        """None labels → 'general'."""
        from src.orchestration.wos_dashboard import _derive_category_from_labels
        assert _derive_category_from_labels(None) == "general"

    def test_type_enhancement_returns_enhancement(self):
        """type:enhancement label → category 'enhancement' (value after colon)."""
        from src.orchestration.wos_dashboard import _derive_category_from_labels
        labels = [{"name": "type:enhancement"}]
        assert _derive_category_from_labels(labels) == "enhancement"


# ---------------------------------------------------------------------------
# upload_html
# ---------------------------------------------------------------------------

class TestUploadHtml:
    def test_writes_to_canonical_filename(self, tmp_path):
        """upload_html writes the HTML to wos-dashboard-active.html in bisque-uploads."""
        from src.orchestration.wos_dashboard import upload_html

        uploads_dir = tmp_path / "bisque-uploads"
        with patch("src.orchestration.wos_dashboard._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_dashboard._bisque_base_url", return_value="http://test:9101"):
            url = upload_html("<html>test</html>")

        assert url == "http://test:9101/files/wos-dashboard-active.html"
        dest = uploads_dir / "wos-dashboard-active.html"
        assert dest.exists()
        assert dest.read_text() == "<html>test</html>"

    def test_returns_stable_canonical_url(self, tmp_path):
        """Calling upload_html twice returns the same URL (stable filename)."""
        from src.orchestration.wos_dashboard import upload_html

        uploads_dir = tmp_path / "bisque-uploads"
        with patch("src.orchestration.wos_dashboard._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_dashboard._bisque_base_url", return_value="http://test:9101"):
            url1 = upload_html("<html>v1</html>")
            url2 = upload_html("<html>v2</html>")

        assert url1 == url2
        assert url1 == "http://test:9101/files/wos-dashboard-active.html"

    def test_creates_uploads_dir_if_missing(self, tmp_path):
        """upload_html creates the bisque-uploads directory when it doesn't exist."""
        from src.orchestration.wos_dashboard import upload_html

        uploads_dir = tmp_path / "nested" / "bisque-uploads"
        assert not uploads_dir.exists()
        with patch("src.orchestration.wos_dashboard._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_dashboard._bisque_base_url", return_value="http://test:9101"):
            upload_html("<html>test</html>")

        assert uploads_dir.exists()


# ---------------------------------------------------------------------------
# main() — html format writes canonical file and outputs URL
# ---------------------------------------------------------------------------

class TestMainHtmlCanonical:
    def _patch_all(self, tmp_path: Path):
        from contextlib import ExitStack
        stack = ExitStack()
        registry = _make_registry([])
        registry.list.return_value = []

        stack.enter_context(patch("src.orchestration.registry.Registry", return_value=registry))
        stack.enter_context(patch("src.orchestration.audit_queries.execution_outcomes", return_value={}))
        stack.enter_context(patch("src.orchestration.wos_dashboard._fetch_completed_uow_ids_since", return_value=[]))
        stack.enter_context(patch("src.orchestration.steward.BOOTUP_CANDIDATE_GATE", False))
        return stack

    def test_html_format_outputs_canonical_url(self, tmp_path, capsys):
        """--format html outputs the canonical URL to stdout, not raw HTML."""
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        uploads_dir = tmp_path / "bisque-uploads"
        with self._patch_all(tmp_path), \
             patch("src.orchestration.wos_dashboard._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_dashboard._bisque_base_url", return_value="http://test:9101"):
            rc = main(["--db", str(db), "--format", "html"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "http://test:9101/files/wos-dashboard-active.html"

    def test_html_format_writes_file_to_bisque_uploads(self, tmp_path):
        """--format html writes wos-dashboard-active.html to the bisque-uploads dir."""
        from src.orchestration.wos_dashboard import main
        db = tmp_path / "registry.db"
        uploads_dir = tmp_path / "bisque-uploads"
        with self._patch_all(tmp_path), \
             patch("src.orchestration.wos_dashboard._uploads_dir", return_value=uploads_dir), \
             patch("src.orchestration.wos_dashboard._bisque_base_url", return_value="http://test:9101"):
            main(["--db", str(db), "--format", "html"])
        dest = uploads_dir / "wos-dashboard-active.html"
        assert dest.exists()
        assert "<!DOCTYPE html>" in dest.read_text()


# ---------------------------------------------------------------------------
# _read_cc_budget_state
# ---------------------------------------------------------------------------

class TestReadCcBudgetState:
    def test_returns_dict_for_valid_state_file(self, tmp_path):
        """Returns parsed dict when state.json contains valid CC quota data."""
        from src.orchestration.wos_dashboard import _read_cc_budget_state
        state_file = tmp_path / "state.json"
        state_file.write_text(
            '{"five_hour_pct": 52.0, "seven_day_pct": 28.0, "fetched_at": "2026-05-10T18:42:00Z"}'
        )
        result = _read_cc_budget_state(str(state_file))
        assert result is not None
        assert result["five_hour_pct"] == 52.0
        assert result["seven_day_pct"] == 28.0
        assert result["fetched_at"] == "2026-05-10T18:42:00Z"

    def test_returns_none_when_file_missing(self, tmp_path):
        """Returns None when the state file does not exist."""
        from src.orchestration.wos_dashboard import _read_cc_budget_state
        result = _read_cc_budget_state(str(tmp_path / "nonexistent.json"))
        assert result is None

    def test_returns_none_on_malformed_json(self, tmp_path):
        """Returns None when the state file contains invalid JSON."""
        from src.orchestration.wos_dashboard import _read_cc_budget_state
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json")
        result = _read_cc_budget_state(str(state_file))
        assert result is None

    def test_returns_none_when_required_key_missing(self, tmp_path):
        """Returns None when the state file is missing required keys."""
        from src.orchestration.wos_dashboard import _read_cc_budget_state
        state_file = tmp_path / "state.json"
        state_file.write_text('{"five_hour_pct": 52.0}')  # missing seven_day_pct and fetched_at
        result = _read_cc_budget_state(str(state_file))
        assert result is None

    def test_reads_from_env_var_path_when_state_path_is_none(self, tmp_path, monkeypatch):
        """When state_path=None, reads from LOBSTER_CC_BUDGET_STATE env var."""
        from src.orchestration.wos_dashboard import _read_cc_budget_state
        state_file = tmp_path / "state.json"
        state_file.write_text(
            '{"five_hour_pct": 75.0, "seven_day_pct": 40.0, "fetched_at": "2026-05-10T10:00:00Z"}'
        )
        monkeypatch.setenv("LOBSTER_CC_BUDGET_STATE", str(state_file))
        result = _read_cc_budget_state(None)
        assert result is not None
        assert result["five_hour_pct"] == 75.0

    def test_uses_default_path_when_no_env_var_and_state_path_is_none(self, monkeypatch):
        """Falls back to ~/.claude/cc-budget/state.json when no env var and no explicit path."""
        from src.orchestration.wos_dashboard import _read_cc_budget_state
        monkeypatch.delenv("LOBSTER_CC_BUDGET_STATE", raising=False)
        # Default path does not exist in test environment — should return None, not raise.
        result = _read_cc_budget_state(None)
        assert result is None  # file absent at default location


# ---------------------------------------------------------------------------
# _format_cc_quota_widget
# ---------------------------------------------------------------------------

# Named constants matching spec requirements
CC_QUOTA_STALE_THRESHOLD_MINUTES = 60
CC_QUOTA_COLOR_GREEN_MAX = 70       # <70% is green
CC_QUOTA_COLOR_YELLOW_MAX = 90      # 70-89% is yellow/orange; >=90% is red


class TestFormatCcQuotaWidget:
    def _now(self) -> datetime:
        return datetime(2026, 5, 10, 18, 50, 0, tzinfo=timezone.utc)

    def _fresh_state(self) -> dict:
        """State fetched 8 minutes before _now()."""
        return {
            "five_hour_pct": 52.0,
            "seven_day_pct": 28.0,
            "fetched_at": "2026-05-10T18:42:00Z",
        }

    def _stale_state(self) -> dict:
        """State fetched 70 minutes before _now() — exceeds 60-min threshold."""
        return {
            "five_hour_pct": 30.0,
            "seven_day_pct": 15.0,
            "fetched_at": "2026-05-10T17:40:00Z",
        }

    def test_unavailable_widget_when_state_is_none(self):
        """None state → unavailable widget."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        html = _format_cc_quota_widget(None, self._now())
        assert "unavailable" in html.lower()

    def test_unavailable_widget_when_state_is_stale(self):
        """Stale state (>60 min) → unavailable widget."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        html = _format_cc_quota_widget(self._stale_state(), self._now())
        assert "unavailable" in html.lower()

    def test_fresh_state_shows_five_hour_percentage(self):
        """Fresh state shows 5h quota percentage."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        html = _format_cc_quota_widget(self._fresh_state(), self._now())
        assert "52" in html
        assert "5h" in html

    def test_fresh_state_shows_seven_day_percentage(self):
        """Fresh state shows 7d quota percentage."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        html = _format_cc_quota_widget(self._fresh_state(), self._now())
        assert "28" in html
        assert "7d" in html

    def test_fresh_state_shows_relative_data_age(self):
        """Fresh state shows data age as relative time ('as of Nm ago')."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        html = _format_cc_quota_widget(self._fresh_state(), self._now())
        # 8 minutes ago
        assert "8m" in html

    def test_green_color_below_threshold(self):
        """Percentages below 70% are colored green (low usage)."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        state = {"five_hour_pct": 40.0, "seven_day_pct": 20.0, "fetched_at": "2026-05-10T18:42:00Z"}
        html = _format_cc_quota_widget(state, self._now())
        # Green CSS color or class should appear
        assert "green" in html.lower() or "#" in html  # color indicator present

    def test_red_color_at_or_above_90_percent(self):
        """Percentages at or above 90% are colored red (high usage)."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        state = {"five_hour_pct": 92.0, "seven_day_pct": 95.0, "fetched_at": "2026-05-10T18:42:00Z"}
        html = _format_cc_quota_widget(state, self._now())
        assert "red" in html.lower() or "#c0392b" in html or "#f87171" in html

    def test_widget_is_html_string(self):
        """Returns an HTML string (contains angle brackets)."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        html = _format_cc_quota_widget(self._fresh_state(), self._now())
        assert "<" in html and ">" in html

    def test_cc_quota_label_present(self):
        """Widget contains a recognizable 'CC quota' or 'CC Quota' label."""
        from src.orchestration.wos_dashboard import _format_cc_quota_widget
        html = _format_cc_quota_widget(self._fresh_state(), self._now())
        assert "CC" in html


# ---------------------------------------------------------------------------
# render_html — CC quota widget injection
# ---------------------------------------------------------------------------

class TestRenderHtmlCcQuota:
    def _base_data(self, cc_quota: dict | None = None) -> dict:
        data = {
            "generated_at": "2026-05-10T18:50:00+00:00",
            "active_uows": [],
            "throughput_24h": {"completed": 3, "failed": 0},
            "cycle_histogram_7d": {},
            "stalled_uows": [],
            "bootup_candidate_gate": {
                "gate_open": False,
                "blocked_count": 0,
                "description": "gate is CLOSED — all UoWs are processed normally",
            },
        }
        if cc_quota is not None:
            data["cc_quota"] = cc_quota
        return data

    def test_cc_quota_section_present_when_data_provided(self):
        """When cc_quota key is present in data, the widget appears in the rendered HTML."""
        from src.orchestration.wos_dashboard import render_html
        cc_quota_html = "<span>CC quota: 5h: 52%</span>"
        data = self._base_data(cc_quota=cc_quota_html)
        html = render_html(data, drilldown_urls={})
        assert "52" in html
        assert "CC" in html

    def test_cc_quota_absent_when_key_missing(self):
        """When cc_quota key is absent, the widget area is omitted gracefully."""
        from src.orchestration.wos_dashboard import render_html
        data = self._base_data(cc_quota=None)  # no cc_quota key
        html = render_html(data, drilldown_urls={})
        # Should still be valid HTML without errors
        assert "<!DOCTYPE html>" in html

    def test_cc_quota_unavailable_string_renders(self):
        """When cc_quota contains 'unavailable', that text appears in the HTML."""
        from src.orchestration.wos_dashboard import render_html
        data = self._base_data(cc_quota="<span>CC quota: unavailable</span>")
        html = render_html(data, drilldown_urls={})
        assert "unavailable" in html


# ---------------------------------------------------------------------------
# build_dashboard_data — CC quota included
# ---------------------------------------------------------------------------

class TestBuildDashboardDataCcQuota:
    def test_cc_quota_key_present_in_data(self, tmp_path):
        """build_dashboard_data includes cc_quota key when called."""
        from src.orchestration.wos_dashboard import build_dashboard_data
        registry = _make_registry([])
        db_path = tmp_path / "registry.db"

        with patch("src.orchestration.audit_queries.execution_outcomes", return_value={}), \
             patch("src.orchestration.wos_dashboard._fetch_completed_uow_ids_since", return_value=[]), \
             patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.wos_dashboard._read_cc_budget_state", return_value=None):
            data = build_dashboard_data(registry, db_path)

        assert "cc_quota" in data

    def test_cc_quota_is_html_string_when_state_available(self, tmp_path):
        """When state is available, cc_quota value is an HTML string."""
        from src.orchestration.wos_dashboard import build_dashboard_data
        registry = _make_registry([])
        db_path = tmp_path / "registry.db"
        state = {"five_hour_pct": 52.0, "seven_day_pct": 28.0, "fetched_at": "2026-05-10T18:42:00Z"}

        with patch("src.orchestration.audit_queries.execution_outcomes", return_value={}), \
             patch("src.orchestration.wos_dashboard._fetch_completed_uow_ids_since", return_value=[]), \
             patch("src.orchestration.steward.is_bootup_candidate_gate_active", return_value=False), \
             patch("src.orchestration.wos_dashboard._read_cc_budget_state", return_value=state):
            data = build_dashboard_data(registry, db_path)

        assert isinstance(data["cc_quota"], str)
        assert "<" in data["cc_quota"]
