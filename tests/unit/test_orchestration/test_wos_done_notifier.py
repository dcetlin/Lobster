"""
Unit tests for wos_completion_notifier.py — per-cycle ping for WOS Done/Failed transitions.

Spec: docs/wos/wos-completion-report-spec.md §Per-Cycle Ping

Behavior under test:

Format selection:
- Short-form used when primary_outcome is 'pearl' AND execution_attempts <= 1
- Rich-form used when primary_outcome is not 'pearl' (any non-pearl outcome)
- Rich-form used when execution_attempts > 1 (>1 attempt, regardless of outcome)
- Failed-form used for failed UoWs

Message content:
- Short-form contains uow_title and primary_outcome on first line
- Short-form second line contains steward_cycles, token_usage, seeds_surfaced_count
- Rich-form contains Outcome, Topology (with cycles + attempts), Tokens, Seeds, Rationale
- Failed-form contains Topology, Tokens (or "unknown"), Failure summary

Inbox write:
- _write_wos_done_message writes a JSON file to ~/messages/inbox/
- File has type='wos_done', source='system', correct chat_id
- Non-fatal: inbox write failure does not raise (logged and swallowed)

Dispatcher handler:
- handle_wos_done is registered in WOS_MESSAGE_TYPE_DISPATCH
- handle_wos_done returns action='send_reply' with pre-formatted text
- wos_done dispatched before the spawn-gate (fast-path like wos_pr_sweep_result)

Steward log extraction:
- _extract_completion_rationale extracts assessment from steward_closure event
- Returns empty string when steward_log is None or has no steward_closure event

Seeds interim count:
- _count_seeds_from_artifacts counts artifacts where category='seed'
- Returns 0 when artifacts is None or empty

Named constants used in assertions:
- SHORT_FORM_TRIGGER_OUTCOME: 'pearl' — the only outcome that qualifies for short-form
- SHORT_FORM_MAX_EXECUTION_ATTEMPTS: 1 — threshold above which rich-form fires
- WOS_DONE_MESSAGE_TYPE: 'wos_done' — the inbox message type
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.wos_completion_notifier import (
    SHORT_FORM_TRIGGER_OUTCOME,
    SHORT_FORM_MAX_EXECUTION_ATTEMPTS,
    WOS_DONE_MESSAGE_TYPE,
    _build_short_form,
    _build_rich_form,
    _build_failed_form,
    _select_format_and_build,
    _extract_completion_rationale,
    _count_seeds_from_artifacts,
    _write_wos_done_message,
)
from orchestration.dispatcher_handlers import (
    handle_wos_done,
    route_wos_message,
    WOS_MESSAGE_TYPE_DISPATCH,
)


# ---------------------------------------------------------------------------
# Constants mirroring the spec
# ---------------------------------------------------------------------------

_SHORT_FORM_OUTCOME = SHORT_FORM_TRIGGER_OUTCOME          # 'pearl'
_MAX_SHORT_FORM_ATTEMPTS = SHORT_FORM_MAX_EXECUTION_ATTEMPTS  # 1

# Sample UoW data for tests
_SAMPLE_UOW_TITLE = "Refactor WOS escalation path"
_SAMPLE_PRIMARY_OUTCOME = "pearl"
_SAMPLE_STEWARD_CYCLES = 2
_SAMPLE_TOKEN_USAGE = 15000
_SAMPLE_SEEDS_COUNT = 0
_SAMPLE_EXECUTION_ATTEMPTS = 1
_SAMPLE_COMPLETION_RATIONALE = "PR merged and tests green"
_SAMPLE_FAILURE_SUMMARY = "Executor orphan after 3 attempts"
_SAMPLE_UOW_ID = "uow_20260509_abc123"
_SAMPLE_GATE_FIRED = "none"


# ---------------------------------------------------------------------------
# Short-form format tests
# ---------------------------------------------------------------------------

class TestBuildShortForm:
    """_build_short_form produces the correct two-line Telegram message."""

    def test_first_line_contains_done_prefix_and_title(self):
        text = _build_short_form(
            uow_title=_SAMPLE_UOW_TITLE,
            primary_outcome=_SAMPLE_PRIMARY_OUTCOME,
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            token_usage=_SAMPLE_TOKEN_USAGE,
            seeds_surfaced_count=_SAMPLE_SEEDS_COUNT,
        )
        first_line = text.splitlines()[0]
        assert "UoW done:" in first_line
        assert _SAMPLE_UOW_TITLE in first_line

    def test_first_line_contains_primary_outcome_in_brackets(self):
        text = _build_short_form(
            uow_title=_SAMPLE_UOW_TITLE,
            primary_outcome=_SAMPLE_PRIMARY_OUTCOME,
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            token_usage=_SAMPLE_TOKEN_USAGE,
            seeds_surfaced_count=_SAMPLE_SEEDS_COUNT,
        )
        first_line = text.splitlines()[0]
        assert f"[{_SAMPLE_PRIMARY_OUTCOME}]" in first_line

    def test_second_line_contains_steward_cycles(self):
        text = _build_short_form(
            uow_title=_SAMPLE_UOW_TITLE,
            primary_outcome=_SAMPLE_PRIMARY_OUTCOME,
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            token_usage=_SAMPLE_TOKEN_USAGE,
            seeds_surfaced_count=_SAMPLE_SEEDS_COUNT,
        )
        second_line = text.splitlines()[1]
        assert str(_SAMPLE_STEWARD_CYCLES) in second_line
        assert "cycle" in second_line

    def test_second_line_contains_token_usage(self):
        text = _build_short_form(
            uow_title=_SAMPLE_UOW_TITLE,
            primary_outcome=_SAMPLE_PRIMARY_OUTCOME,
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            token_usage=_SAMPLE_TOKEN_USAGE,
            seeds_surfaced_count=_SAMPLE_SEEDS_COUNT,
        )
        second_line = text.splitlines()[1]
        assert str(_SAMPLE_TOKEN_USAGE) in second_line
        assert "token" in second_line

    def test_second_line_contains_seeds_surfaced(self):
        text = _build_short_form(
            uow_title=_SAMPLE_UOW_TITLE,
            primary_outcome=_SAMPLE_PRIMARY_OUTCOME,
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            token_usage=_SAMPLE_TOKEN_USAGE,
            seeds_surfaced_count=3,
        )
        second_line = text.splitlines()[1]
        assert "3" in second_line
        assert "seed" in second_line

    def test_token_usage_none_shows_unknown(self):
        text = _build_short_form(
            uow_title=_SAMPLE_UOW_TITLE,
            primary_outcome=_SAMPLE_PRIMARY_OUTCOME,
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            token_usage=None,
            seeds_surfaced_count=_SAMPLE_SEEDS_COUNT,
        )
        assert "unknown" in text.lower() or "?" in text


# ---------------------------------------------------------------------------
# Rich-form format tests
# ---------------------------------------------------------------------------

class TestBuildRichForm:
    """_build_rich_form produces the correct multi-field Telegram message."""

    def _make_rich(self, **overrides):
        defaults = dict(
            uow_title=_SAMPLE_UOW_TITLE,
            primary_outcome="seed",
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            execution_attempts=_SAMPLE_EXECUTION_ATTEMPTS,
            token_usage=_SAMPLE_TOKEN_USAGE,
            seeds_surfaced_count=_SAMPLE_SEEDS_COUNT,
            gate_fired=_SAMPLE_GATE_FIRED,
            completion_rationale=_SAMPLE_COMPLETION_RATIONALE,
        )
        defaults.update(overrides)
        return _build_rich_form(**defaults)

    def test_first_line_is_done_prefix_with_title(self):
        text = self._make_rich()
        assert text.startswith("UoW done:")
        assert _SAMPLE_UOW_TITLE in text.splitlines()[0]

    def test_outcome_field_present(self):
        text = self._make_rich(primary_outcome="seed")
        assert "Outcome" in text
        assert "seed" in text

    def test_topology_field_contains_cycles_and_attempts(self):
        text = self._make_rich(steward_cycles=3, execution_attempts=2)
        assert "Topology" in text
        assert "3" in text
        assert "2" in text

    def test_tokens_field_present(self):
        text = self._make_rich(token_usage=12345)
        assert "Token" in text
        assert "12345" in text

    def test_seeds_field_present(self):
        text = self._make_rich(seeds_surfaced_count=2)
        assert "Seed" in text
        assert "2" in text

    def test_rationale_field_present_when_nonempty(self):
        text = self._make_rich(completion_rationale="Tests passed and PR merged")
        assert "Rationale" in text
        assert "Tests passed" in text

    def test_rationale_field_absent_when_empty(self):
        text = self._make_rich(completion_rationale="")
        assert "Rationale" not in text

    def test_token_usage_none_shows_unknown(self):
        text = self._make_rich(token_usage=None)
        assert "unknown" in text.lower() or "?" in text


# ---------------------------------------------------------------------------
# Failed-form format tests
# ---------------------------------------------------------------------------

class TestBuildFailedForm:
    """_build_failed_form produces the correct failed-UoW Telegram message."""

    def _make_failed(self, **overrides):
        defaults = dict(
            uow_title=_SAMPLE_UOW_TITLE,
            gate_fired=_SAMPLE_GATE_FIRED,
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            execution_attempts=_SAMPLE_EXECUTION_ATTEMPTS,
            token_usage=_SAMPLE_TOKEN_USAGE,
            failure_summary=_SAMPLE_FAILURE_SUMMARY,
        )
        defaults.update(overrides)
        return _build_failed_form(**defaults)

    def test_first_line_is_failed_prefix(self):
        text = self._make_failed()
        assert text.startswith("UoW failed:")
        assert _SAMPLE_UOW_TITLE in text.splitlines()[0]

    def test_topology_field_present(self):
        text = self._make_failed(gate_fired="spiral", steward_cycles=5, execution_attempts=3)
        assert "Topology" in text
        assert "5" in text
        assert "3" in text

    def test_tokens_field_shows_unknown_when_none(self):
        text = self._make_failed(token_usage=None)
        assert "unknown" in text.lower()

    def test_failure_field_contains_summary(self):
        text = self._make_failed(failure_summary="Retry cap exceeded")
        assert "Failure" in text
        assert "Retry cap exceeded" in text


# ---------------------------------------------------------------------------
# Format selection tests
# ---------------------------------------------------------------------------

class TestSelectFormatAndBuild:
    """_select_format_and_build chooses the right format based on spec rules."""

    def _make_surface(self, **overrides):
        defaults = dict(
            uow_title=_SAMPLE_UOW_TITLE,
            primary_outcome=_SHORT_FORM_OUTCOME,
            steward_cycles=_SAMPLE_STEWARD_CYCLES,
            execution_attempts=1,
            token_usage=_SAMPLE_TOKEN_USAGE,
            seeds_surfaced_count=0,
            gate_fired="none",
            completion_rationale="",
            failure_summary=None,
            failed=False,
        )
        defaults.update(overrides)
        return _select_format_and_build(**defaults)

    def test_pearl_with_one_attempt_produces_short_form(self):
        text = self._make_surface(primary_outcome="pearl", execution_attempts=1)
        # Short-form has exactly 2 lines; rich-form has more
        lines = [l for l in text.splitlines() if l.strip()]
        assert len(lines) == 2

    def test_non_pearl_outcome_produces_rich_form(self):
        text = self._make_surface(primary_outcome="seed", execution_attempts=1)
        assert "Outcome" in text

    def test_pearl_with_two_attempts_produces_rich_form(self):
        text = self._make_surface(primary_outcome="pearl", execution_attempts=2)
        assert "Outcome" in text

    def test_heat_outcome_produces_rich_form(self):
        text = self._make_surface(primary_outcome="heat", execution_attempts=1)
        assert "Outcome" in text

    def test_shit_outcome_produces_rich_form(self):
        text = self._make_surface(primary_outcome="shit", execution_attempts=1)
        assert "Outcome" in text

    def test_failed_uow_produces_failed_form(self):
        text = self._make_surface(failed=True, failure_summary="Timeout")
        assert text.startswith("UoW failed:")

    def test_failed_form_not_triggered_for_done_uow(self):
        text = self._make_surface(failed=False, primary_outcome="pearl", execution_attempts=1)
        assert not text.startswith("UoW failed:")


# ---------------------------------------------------------------------------
# Steward log extraction tests
# ---------------------------------------------------------------------------

class TestExtractCompletionRationale:
    """_extract_completion_rationale reads assessment from steward_closure event."""

    def test_returns_empty_string_when_log_is_none(self):
        assert _extract_completion_rationale(None) == ""

    def test_returns_empty_string_when_log_is_empty(self):
        assert _extract_completion_rationale("") == ""

    def test_extracts_assessment_from_steward_closure_event(self):
        entry = {"event": "steward_closure", "assessment": "All criteria met", "timestamp": "2026-05-09T00:00:00Z"}
        log = json.dumps(entry)
        assert _extract_completion_rationale(log) == "All criteria met"

    def test_returns_last_steward_closure_when_multiple_present(self):
        entry1 = {"event": "steward_closure", "assessment": "First closure", "timestamp": "2026-05-09T00:00:00Z"}
        entry2 = {"event": "steward_closure", "assessment": "Second closure", "timestamp": "2026-05-09T01:00:00Z"}
        log = json.dumps(entry1) + "\n" + json.dumps(entry2)
        assert _extract_completion_rationale(log) == "Second closure"

    def test_returns_empty_when_no_steward_closure_event(self):
        entry = {"event": "prescription", "completion_assessment": "nothing", "timestamp": "2026-05-09T00:00:00Z"}
        log = json.dumps(entry)
        assert _extract_completion_rationale(log) == ""

    def test_ignores_malformed_json_lines(self):
        good_entry = {"event": "steward_closure", "assessment": "done", "timestamp": "x"}
        log = "not valid json\n" + json.dumps(good_entry) + "\n{also bad"
        assert _extract_completion_rationale(log) == "done"

    def test_missing_assessment_key_returns_empty_string(self):
        entry = {"event": "steward_closure", "timestamp": "2026-05-09T00:00:00Z"}
        log = json.dumps(entry)
        assert _extract_completion_rationale(log) == ""


# ---------------------------------------------------------------------------
# Seeds count from artifacts tests
# ---------------------------------------------------------------------------

class TestCountSeedsFromArtifacts:
    """_count_seeds_from_artifacts counts artifacts with category='seed'."""

    def test_returns_zero_when_artifacts_is_none(self):
        assert _count_seeds_from_artifacts(None) == 0

    def test_returns_zero_when_artifacts_is_empty(self):
        assert _count_seeds_from_artifacts([]) == 0

    def test_counts_only_seed_category(self):
        artifacts = [
            {"type": "issue", "ref": "dcetlin/Lobster#42", "category": "seed"},
            {"type": "pr", "ref": "dcetlin/Lobster#43", "category": "pearl"},
            {"type": "issue", "ref": "dcetlin/Lobster#44", "category": "seed"},
        ]
        assert _count_seeds_from_artifacts(artifacts) == 2

    def test_returns_zero_when_no_seeds(self):
        artifacts = [
            {"type": "pr", "ref": "dcetlin/Lobster#1", "category": "pearl"},
        ]
        assert _count_seeds_from_artifacts(artifacts) == 0

    def test_handles_missing_category_key(self):
        artifacts = [
            {"type": "issue", "ref": "dcetlin/Lobster#1"},  # no category key
        ]
        assert _count_seeds_from_artifacts(artifacts) == 0


# ---------------------------------------------------------------------------
# Inbox write tests
# ---------------------------------------------------------------------------

class TestWriteWosDoneMessage:
    """_write_wos_done_message writes a wos_done inbox JSON file."""

    def test_writes_json_file_to_inbox_dir(self, tmp_path):
        with patch.dict(os.environ, {"LOBSTER_INBOX_DIR": str(tmp_path)}):
            _write_wos_done_message(
                uow_id=_SAMPLE_UOW_ID,
                text="UoW done: test [pearl]\n1 cycle · 100 tokens · 0 seeds surfaced",
                chat_id="8075091586",
            )
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1

    def test_written_file_has_correct_type(self, tmp_path):
        with patch.dict(os.environ, {"LOBSTER_INBOX_DIR": str(tmp_path)}):
            _write_wos_done_message(
                uow_id=_SAMPLE_UOW_ID,
                text="test text",
                chat_id="8075091586",
            )
        files = list(tmp_path.glob("*.json"))
        payload = json.loads(files[0].read_text())
        assert payload["type"] == WOS_DONE_MESSAGE_TYPE

    def test_written_file_has_correct_source(self, tmp_path):
        with patch.dict(os.environ, {"LOBSTER_INBOX_DIR": str(tmp_path)}):
            _write_wos_done_message(
                uow_id=_SAMPLE_UOW_ID,
                text="test text",
                chat_id="8075091586",
            )
        files = list(tmp_path.glob("*.json"))
        payload = json.loads(files[0].read_text())
        assert payload["source"] == "system"

    def test_written_file_contains_uow_id(self, tmp_path):
        with patch.dict(os.environ, {"LOBSTER_INBOX_DIR": str(tmp_path)}):
            _write_wos_done_message(
                uow_id=_SAMPLE_UOW_ID,
                text="test text",
                chat_id="8075091586",
            )
        files = list(tmp_path.glob("*.json"))
        payload = json.loads(files[0].read_text())
        assert payload.get("uow_id") == _SAMPLE_UOW_ID

    def test_write_failure_does_not_raise(self, tmp_path):
        """Non-fatal: inbox write failure must not propagate to caller."""
        # Use a non-existent path where mkdir can't help — read-only parent simulation
        bad_dir = "/dev/null/nonexistent_inbox_path"
        with patch.dict(os.environ, {"LOBSTER_INBOX_DIR": bad_dir}):
            # Should not raise
            _write_wos_done_message(
                uow_id=_SAMPLE_UOW_ID,
                text="test text",
                chat_id="8075091586",
            )

    def test_uses_env_var_for_chat_id_when_not_supplied(self, tmp_path):
        """Falls back to LOBSTER_ADMIN_CHAT_ID env var."""
        with patch.dict(os.environ, {
            "LOBSTER_INBOX_DIR": str(tmp_path),
            "LOBSTER_ADMIN_CHAT_ID": "9999999999",
        }):
            _write_wos_done_message(
                uow_id=_SAMPLE_UOW_ID,
                text="test text",
            )
        files = list(tmp_path.glob("*.json"))
        payload = json.loads(files[0].read_text())
        assert payload["chat_id"] == "9999999999"


# ---------------------------------------------------------------------------
# Dispatcher handler tests
# ---------------------------------------------------------------------------

class TestHandleWosDone:
    """handle_wos_done returns action='send_reply' with the pre-formatted text."""

    def _make_msg(self, **overrides):
        defaults = {
            "type": WOS_DONE_MESSAGE_TYPE,
            "source": "system",
            "chat_id": "8075091586",
            "uow_id": _SAMPLE_UOW_ID,
            "text": "UoW done: test [pearl]\n2 cycle(s) · 15000 tokens · 0 seeds surfaced",
        }
        defaults.update(overrides)
        return defaults

    def test_returns_send_reply_action(self):
        msg = self._make_msg()
        result = handle_wos_done(msg)
        assert result["action"] == "send_reply"

    def test_returns_text_from_message(self):
        expected_text = "UoW done: my uow [pearl]\n2 cycle(s) · 100 tokens · 0 seeds surfaced"
        msg = self._make_msg(text=expected_text)
        result = handle_wos_done(msg)
        assert result["text"] == expected_text

    def test_returns_chat_id_from_message(self):
        msg = self._make_msg(chat_id="12345678")
        result = handle_wos_done(msg)
        assert result["chat_id"] == "12345678"

    def test_falls_back_to_env_chat_id_when_missing(self):
        msg = self._make_msg()
        del msg["chat_id"]
        with patch.dict(os.environ, {"LOBSTER_ADMIN_CHAT_ID": "7777777777"}):
            result = handle_wos_done(msg)
        assert result["chat_id"] == "7777777777"

    def test_returns_message_type_wos_done(self):
        msg = self._make_msg()
        result = handle_wos_done(msg)
        assert result["message_type"] == WOS_DONE_MESSAGE_TYPE

    def test_missing_text_uses_fallback(self):
        msg = self._make_msg()
        del msg["text"]
        result = handle_wos_done(msg)
        assert isinstance(result["text"], str)
        assert len(result["text"]) > 0


class TestWosDoneInDispatchTable:
    """wos_done is registered in WOS_MESSAGE_TYPE_DISPATCH and routed correctly."""

    def test_wos_done_in_dispatch_table(self):
        assert WOS_DONE_MESSAGE_TYPE in WOS_MESSAGE_TYPE_DISPATCH

    def test_route_wos_message_routes_wos_done(self):
        msg = {
            "type": WOS_DONE_MESSAGE_TYPE,
            "source": "system",
            "chat_id": "8075091586",
            "uow_id": _SAMPLE_UOW_ID,
            "text": "UoW done: test [pearl]\n1 cycle(s) · 100 tokens · 0 seeds surfaced",
        }
        result = route_wos_message(msg)
        assert result["action"] == "send_reply"
        assert result["message_type"] == WOS_DONE_MESSAGE_TYPE
