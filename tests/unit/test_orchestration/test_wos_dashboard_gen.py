"""
Unit tests for wos_dashboard_gen.py.

Covers the pure-function and near-pure layers of the new dashboard generator:
- _build_cc_data: all-time totals, daily chart, per-UoW grouping, missing ledger
- generate_html: template placeholders replaced, output is valid HTML
- _enrich_uow_tokens: token fields attached to matching UoW entries
- parse_wos_dashboard_command: command detection (True/False)
- SONNET_4_6_* constants: named module-level values

All SQLite-dependent functions (_build_registry_data, generate_and_upload) are
excluded from this module — they require a live DB or bisque relay and belong in
integration tests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Pricing constants
# ---------------------------------------------------------------------------

class TestPricingConstants:
    def test_input_constant_value(self):
        from src.orchestration.wos_dashboard_gen import SONNET_4_6_INPUT_PER_MTK
        assert SONNET_4_6_INPUT_PER_MTK == 3.0

    def test_output_constant_value(self):
        from src.orchestration.wos_dashboard_gen import SONNET_4_6_OUTPUT_PER_MTK
        assert SONNET_4_6_OUTPUT_PER_MTK == 15.0

    def test_cache_read_constant_value(self):
        from src.orchestration.wos_dashboard_gen import SONNET_4_6_CACHE_READ_PER_MTK
        assert SONNET_4_6_CACHE_READ_PER_MTK == 0.30


# ---------------------------------------------------------------------------
# _build_cc_data
# ---------------------------------------------------------------------------

class TestBuildCcData:
    def test_missing_ledger_returns_stale(self, tmp_path):
        from src.orchestration.wos_dashboard_gen import _build_cc_data
        result = _build_cc_data(tmp_path / "nonexistent.jsonl")
        assert result["stale"] is True
        assert result["all_time"]["calls"] == 0
        assert result["all_time"]["est_cost_usd"] == 0

    def test_empty_ledger_returns_not_stale(self, tmp_path):
        from src.orchestration.wos_dashboard_gen import _build_cc_data
        ledger = tmp_path / "token-ledger.jsonl"
        ledger.write_text("")
        result = _build_cc_data(ledger)
        assert result["stale"] is False
        assert result["all_time"]["calls"] == 0

    def test_all_time_totals_sum_correctly(self, tmp_path):
        from src.orchestration.wos_dashboard_gen import (
            _build_cc_data,
            SONNET_4_6_INPUT_PER_MTK,
            SONNET_4_6_OUTPUT_PER_MTK,
            SONNET_4_6_CACHE_READ_PER_MTK,
        )
        ledger = tmp_path / "token-ledger.jsonl"
        now_ts = time.time()
        entries = [
            {"ts": now_ts, "input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000, "cache_write": 0},
            {"ts": now_ts, "input": 2_000_000, "output": 500_000, "cache_read": 0, "cache_write": 0},
        ]
        ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _build_cc_data(ledger)

        assert result["all_time"]["calls"] == 2
        assert result["all_time"]["input"] == 3_000_000
        assert result["all_time"]["output"] == 1_500_000
        assert result["all_time"]["cache_read"] == 1_000_000

        # Verify cost is computed via constants, not inline literals
        expected_cost = (
            3_000_000 * SONNET_4_6_INPUT_PER_MTK
            + 1_500_000 * SONNET_4_6_OUTPUT_PER_MTK
        ) / 1_000_000 + 1_000_000 * SONNET_4_6_CACHE_READ_PER_MTK / 1_000_000
        assert abs(result["all_time"]["est_cost_usd"] - round(expected_cost, 2)) < 0.01

    def test_uow_token_grouping_by_task_id(self, tmp_path):
        from src.orchestration.wos_dashboard_gen import _build_cc_data
        ledger = tmp_path / "token-ledger.jsonl"
        now_ts = time.time()
        entries = [
            {"ts": now_ts, "task_id": "wos-uow_20260101_aabbcc", "input": 100, "output": 200, "cache_read": 0, "cache_write": 0},
            {"ts": now_ts, "task_id": "wos-uow_20260101_aabbcc", "input": 50, "output": 80, "cache_read": 0, "cache_write": 0},
            {"ts": now_ts, "task_id": "fix-uow_20260101_ddeeff", "input": 10, "output": 20, "cache_read": 0, "cache_write": 0},
        ]
        ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _build_cc_data(ledger)

        uow_tokens = result.get("uow_tokens", {})
        assert "uow_20260101_aabbcc" in uow_tokens
        tok = uow_tokens["uow_20260101_aabbcc"]
        assert tok["input"] == 150
        assert tok["output"] == 280
        assert tok["calls"] == 2

    def test_uow_top20_populated(self, tmp_path):
        from src.orchestration.wos_dashboard_gen import _build_cc_data
        ledger = tmp_path / "token-ledger.jsonl"
        now_ts = time.time()
        entries = [
            {"ts": now_ts, "task_id": f"uow_20260101_{'a' * 6}{str(i).zfill(2)}", "input": 100, "output": 1000 + i, "cache_read": 0, "cache_write": 0}
            for i in range(5)
        ]
        ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _build_cc_data(ledger)
        assert len(result["uow_top20_by_output"]) == 5
        # Should be sorted by output descending
        outputs = [u["output"] for u in result["uow_top20_by_output"]]
        assert outputs == sorted(outputs, reverse=True)

    def test_malformed_lines_skipped(self, tmp_path):
        from src.orchestration.wos_dashboard_gen import _build_cc_data
        ledger = tmp_path / "token-ledger.jsonl"
        now_ts = time.time()
        ledger.write_text(
            json.dumps({"ts": now_ts, "input": 100, "output": 200, "cache_read": 0, "cache_write": 0}) + "\n"
            + "NOT VALID JSON\n"
            + json.dumps({"ts": now_ts, "input": 50, "output": 75, "cache_read": 0, "cache_write": 0}) + "\n"
        )
        result = _build_cc_data(ledger)
        assert result["all_time"]["calls"] == 2  # malformed line skipped
        assert result["all_time"]["input"] == 150


# ---------------------------------------------------------------------------
# generate_html
# ---------------------------------------------------------------------------

class TestGenerateHtml:
    def _minimal_d(self) -> dict:
        return {
            "generated_at": "2026-05-23T00:00:00+00:00",
            "status_counts": [],
            "outcome_counts": [],
            "total_uows": 0,
            "done_cnt": 0,
            "active_cnt": 0,
            "total_audit": 0,
            "total_traces": 0,
            "dispatch_count": 0,
            "exec_fail_count": 0,
            "date_range": {"min_dt": None, "max_dt": None},
            "weekly": [],
            "all_uows": [],
            "audit_by_uow": {},
            "traces_by_uow": {},
        }

    def _minimal_cc(self) -> dict:
        return {
            "all_time": {"calls": 0, "output": 0, "cache_read": 0, "input": 0, "est_cost_usd": 0},
            "daily_chart": [],
            "model_breakdown": [],
            "uow_top20_by_output": [],
            "stale": False,
        }

    def test_returns_string(self):
        from src.orchestration.wos_dashboard_gen import generate_html
        html = generate_html(self._minimal_d(), self._minimal_cc())
        assert isinstance(html, str)

    def test_d_data_placeholder_replaced(self):
        from src.orchestration.wos_dashboard_gen import generate_html
        html = generate_html(self._minimal_d(), self._minimal_cc())
        assert "{D_DATA}" not in html

    def test_cc_data_placeholder_replaced(self):
        from src.orchestration.wos_dashboard_gen import generate_html
        html = generate_html(self._minimal_d(), self._minimal_cc())
        assert "{CC_DATA}" not in html

    def test_d_data_content_embedded(self):
        from src.orchestration.wos_dashboard_gen import generate_html
        d = self._minimal_d()
        d["total_uows"] = 42
        html = generate_html(d, self._minimal_cc())
        assert '"total_uows":42' in html or '"total_uows": 42' in html

    def test_cc_data_content_embedded(self):
        from src.orchestration.wos_dashboard_gen import generate_html
        cc = self._minimal_cc()
        cc["all_time"]["calls"] = 77
        html = generate_html(self._minimal_d(), cc)
        assert '"calls":77' in html or '"calls": 77' in html

    def test_html_doctype_present(self):
        from src.orchestration.wos_dashboard_gen import generate_html
        html = generate_html(self._minimal_d(), self._minimal_cc())
        assert html.strip().startswith("<!DOCTYPE html>")

    def test_uow_tokens_key_stripped_from_cc_embed(self):
        """uow_tokens is internal-only and must not appear in the embedded JSON."""
        from src.orchestration.wos_dashboard_gen import generate_html
        cc = self._minimal_cc()
        cc["uow_tokens"] = {"uow_20260101_abc": {"output": 999}}
        html = generate_html(self._minimal_d(), cc)
        # The uow_tokens dict should not be in the embedded CC object
        # (it's filtered out before embedding)
        embedded_cc_start = html.index("const CC=") + len("const CC=")
        # Find end of CC object — it's followed by ";"
        # We can verify by parsing the embedded JSON
        cc_json_start = html.index("const CC=") + len("const CC=")
        # The HTML uses {CC_DATA} replacement, so find the closing semicolon
        # Simple check: uow_tokens with its internal values should not appear
        assert '"uow_tokens"' not in html[cc_json_start:cc_json_start + 2000]


# ---------------------------------------------------------------------------
# _enrich_uow_tokens
# ---------------------------------------------------------------------------

class TestEnrichUowTokens:
    def test_attaches_token_fields_to_matching_uow(self):
        from src.orchestration.wos_dashboard_gen import _enrich_uow_tokens
        d_data = {
            "all_uows": [
                {"id": "uow_20260101_aabbcc", "lo": None, "li": None, "lcr": None, "lcw": None, "lc": None, "ec": None},
            ]
        }
        cc_data = {
            "uow_tokens": {
                "uow_20260101_aabbcc": {
                    "output": 500, "input": 200, "cache_read": 100, "cache_write": 50, "calls": 3, "est_cost": 0.01,
                }
            }
        }
        _enrich_uow_tokens(d_data, cc_data)
        uow = d_data["all_uows"][0]
        assert uow["lo"] == 500
        assert uow["li"] == 200
        assert uow["lcr"] == 100
        assert uow["lcw"] == 50
        assert uow["lc"] == 3
        assert uow["ec"] is not None  # cost string like "$0.0xxx"

    def test_unmatched_uow_fields_remain_none(self):
        from src.orchestration.wos_dashboard_gen import _enrich_uow_tokens
        d_data = {
            "all_uows": [
                {"id": "uow_20260101_xxxxxx", "lo": None, "li": None, "lcr": None, "lcw": None, "lc": None, "ec": None},
            ]
        }
        cc_data = {
            "uow_tokens": {
                "uow_20260101_aabbcc": {"output": 100, "input": 50, "cache_read": 0, "cache_write": 0, "calls": 1, "est_cost": 0},
            }
        }
        _enrich_uow_tokens(d_data, cc_data)
        uow = d_data["all_uows"][0]
        assert uow["lo"] is None
        assert uow["ec"] is None

    def test_empty_uow_tokens_is_noop(self):
        from src.orchestration.wos_dashboard_gen import _enrich_uow_tokens
        uow = {"id": "uow_20260101_aabbcc", "lo": None, "li": None, "lcr": None, "lcw": None, "lc": None, "ec": None}
        d_data = {"all_uows": [uow]}
        cc_data = {"uow_tokens": {}}
        _enrich_uow_tokens(d_data, cc_data)
        assert uow["lo"] is None

    def test_ec_uses_named_pricing_constants(self):
        """Cost string must be consistent with the module-level pricing constants."""
        from src.orchestration.wos_dashboard_gen import (
            _enrich_uow_tokens,
            SONNET_4_6_INPUT_PER_MTK,
            SONNET_4_6_OUTPUT_PER_MTK,
            SONNET_4_6_CACHE_READ_PER_MTK,
        )
        inp, out, cr = 1_000_000, 1_000_000, 1_000_000
        d_data = {
            "all_uows": [
                {"id": "uow_20260101_zzzzzz", "lo": None, "li": None, "lcr": None, "lcw": None, "lc": None, "ec": None},
            ]
        }
        cc_data = {
            "uow_tokens": {
                "uow_20260101_zzzzzz": {
                    "output": out, "input": inp, "cache_read": cr, "cache_write": 0, "calls": 1, "est_cost": 0,
                }
            }
        }
        _enrich_uow_tokens(d_data, cc_data)
        ec_str = d_data["all_uows"][0]["ec"]
        # Strip leading "$" and parse
        ec_val = float(ec_str.lstrip("$"))
        expected = round(
            (inp * SONNET_4_6_INPUT_PER_MTK + out * SONNET_4_6_OUTPUT_PER_MTK) / 1_000_000
            + cr * SONNET_4_6_CACHE_READ_PER_MTK / 1_000_000,
            4,
        )
        assert abs(ec_val - expected) < 1e-6


# ---------------------------------------------------------------------------
# parse_wos_dashboard_command
# ---------------------------------------------------------------------------

class TestParseWosDashboardCommand:
    def test_matches_bare_command(self):
        from src.orchestration.dispatcher_handlers import parse_wos_dashboard_command
        assert parse_wos_dashboard_command("wos dashboard") is True

    def test_matches_slash_prefix(self):
        from src.orchestration.dispatcher_handlers import parse_wos_dashboard_command
        assert parse_wos_dashboard_command("/wos dashboard") is True

    def test_matches_case_insensitive(self):
        from src.orchestration.dispatcher_handlers import parse_wos_dashboard_command
        assert parse_wos_dashboard_command("WOS DASHBOARD") is True
        assert parse_wos_dashboard_command("Wos Dashboard") is True

    def test_matches_with_surrounding_whitespace(self):
        from src.orchestration.dispatcher_handlers import parse_wos_dashboard_command
        assert parse_wos_dashboard_command("  wos dashboard  ") is True

    def test_does_not_match_wos_status(self):
        from src.orchestration.dispatcher_handlers import parse_wos_dashboard_command
        assert parse_wos_dashboard_command("wos status") is False

    def test_does_not_match_wos_start(self):
        from src.orchestration.dispatcher_handlers import parse_wos_dashboard_command
        assert parse_wos_dashboard_command("wos start") is False

    def test_does_not_match_partial(self):
        from src.orchestration.dispatcher_handlers import parse_wos_dashboard_command
        assert parse_wos_dashboard_command("wos dashboard extra") is False

    def test_empty_string(self):
        from src.orchestration.dispatcher_handlers import parse_wos_dashboard_command
        assert parse_wos_dashboard_command("") is False
