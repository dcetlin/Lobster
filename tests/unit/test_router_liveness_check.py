"""Unit tests for router-liveness-check.py

Tests are derived from issue #1563: the router liveness check must use
getWebhookInfo pending_update_count as the discriminator, not log silence.

Verdict logic:
  - pending_update_count == 0  -> HEALTHY
  - pending_update_count > 0   -> UNHEALTHY
  - API call fails             -> UNKNOWN (not actionable)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# router-liveness-check.py has a hyphenated filename so we use importlib.
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "router-liveness-check.py"
_spec = importlib.util.spec_from_file_location("router_liveness_check", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
rlc = importlib.util.module_from_spec(_spec)
sys.modules["router_liveness_check"] = rlc
_spec.loader.exec_module(rlc)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# evaluate_verdict — pure function tests
# ---------------------------------------------------------------------------


class TestEvaluateVerdict:
    def test_pending_count_zero_is_healthy(self) -> None:
        result = rlc.evaluate_verdict(pending_update_count=0)
        assert result.verdict == rlc.Verdict.HEALTHY
        assert result.pending_update_count == 0

    def test_pending_count_positive_is_unhealthy(self) -> None:
        result = rlc.evaluate_verdict(pending_update_count=5)
        assert result.verdict == rlc.Verdict.UNHEALTHY
        assert result.pending_update_count == 5

    def test_pending_count_large_is_unhealthy(self) -> None:
        result = rlc.evaluate_verdict(pending_update_count=1000)
        assert result.verdict == rlc.Verdict.UNHEALTHY

    def test_none_pending_count_is_unknown(self) -> None:
        result = rlc.evaluate_verdict(pending_update_count=None)
        assert result.verdict == rlc.Verdict.UNKNOWN

    def test_error_string_is_unknown(self) -> None:
        result = rlc.evaluate_verdict(pending_update_count=None, error="HTTP 502: Bad Gateway")
        assert result.verdict == rlc.Verdict.UNKNOWN
        assert "502" in result.detail

    def test_error_overrides_valid_count(self) -> None:
        """If an error is reported alongside a count, error takes precedence."""
        result = rlc.evaluate_verdict(pending_update_count=0, error="Parse error")
        assert result.verdict == rlc.Verdict.UNKNOWN

    def test_healthy_detail_mentions_pending_count(self) -> None:
        result = rlc.evaluate_verdict(pending_update_count=0)
        assert "pending_update_count=0" in result.detail

    def test_unhealthy_detail_mentions_count(self) -> None:
        result = rlc.evaluate_verdict(pending_update_count=42)
        assert "42" in result.detail


# ---------------------------------------------------------------------------
# result_to_dict — serialization
# ---------------------------------------------------------------------------


class TestResultToDict:
    def test_serializes_healthy(self) -> None:
        result = rlc.LivenessResult(
            verdict=rlc.Verdict.HEALTHY,
            pending_update_count=0,
            detail="all good",
        )
        d = rlc.result_to_dict(result)
        assert d["verdict"] == "HEALTHY"
        assert d["pending_update_count"] == 0

    def test_serializes_unknown_with_none_count(self) -> None:
        result = rlc.LivenessResult(
            verdict=rlc.Verdict.UNKNOWN,
            pending_update_count=None,
            detail="no token",
        )
        d = rlc.result_to_dict(result)
        assert d["verdict"] == "UNKNOWN"
        assert d["pending_update_count"] is None

    def test_output_is_json_serializable(self) -> None:
        result = rlc.LivenessResult(
            verdict=rlc.Verdict.UNHEALTHY,
            pending_update_count=10,
            detail="accumulating",
        )
        # Should not raise
        json.dumps(rlc.result_to_dict(result))


# ---------------------------------------------------------------------------
# load_bot_token — config loading
# ---------------------------------------------------------------------------


class TestLoadBotToken:
    def test_reads_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")
        assert rlc.load_bot_token() == "test-token-123"

    def test_reads_from_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = tmp_path / "config.env"
        config.write_text('TELEGRAM_BOT_TOKEN=file-token-456\n')
        assert rlc.load_bot_token(str(config)) == "file-token-456"

    def test_env_takes_precedence_over_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
        config = tmp_path / "config.env"
        config.write_text('TELEGRAM_BOT_TOKEN=file-token\n')
        assert rlc.load_bot_token(str(config)) == "env-token"

    def test_returns_none_when_no_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = tmp_path / "config.env"
        config.write_text('OTHER_VAR=something\n')
        assert rlc.load_bot_token(str(config)) is None

    def test_returns_none_for_missing_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert rlc.load_bot_token("/nonexistent/path/config.env") is None

    def test_strips_quotes_from_config_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = tmp_path / "config.env"
        config.write_text('TELEGRAM_BOT_TOKEN="quoted-token"\n')
        assert rlc.load_bot_token(str(config)) == "quoted-token"


# ---------------------------------------------------------------------------
# check_liveness — integration (mocked HTTP)
# ---------------------------------------------------------------------------


class TestCheckLiveness:
    def test_healthy_when_pending_zero(self) -> None:
        with patch.object(rlc, "fetch_webhook_info", return_value=(0, None)):
            result = rlc.check_liveness("fake-token")
        assert result.verdict == rlc.Verdict.HEALTHY

    def test_unhealthy_when_pending_positive(self) -> None:
        with patch.object(rlc, "fetch_webhook_info", return_value=(15, None)):
            result = rlc.check_liveness("fake-token")
        assert result.verdict == rlc.Verdict.UNHEALTHY

    def test_unknown_when_api_fails(self) -> None:
        with patch.object(rlc, "fetch_webhook_info", return_value=(None, "Connection refused")):
            result = rlc.check_liveness("fake-token")
        assert result.verdict == rlc.Verdict.UNKNOWN


# ---------------------------------------------------------------------------
# Verdict StrEnum
# ---------------------------------------------------------------------------


class TestVerdictEnum:
    def test_verdict_values_are_uppercase(self) -> None:
        assert rlc.Verdict.HEALTHY == "HEALTHY"
        assert rlc.Verdict.UNHEALTHY == "UNHEALTHY"
        assert rlc.Verdict.UNKNOWN == "UNKNOWN"

    def test_verdict_is_strenum(self) -> None:
        """Verify Verdict uses StrEnum for safe string comparison."""
        assert isinstance(rlc.Verdict.HEALTHY, str)
