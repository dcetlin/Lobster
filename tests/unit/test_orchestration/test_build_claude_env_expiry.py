"""
Unit tests for _build_claude_env() expiry awareness.

Covers:
- test_build_claude_env_warns_near_expiry: token expiring in 1 hour → WARNING logged
- test_build_claude_env_errors_on_expired: token expired 1 hour ago → ERROR logged
- test_build_claude_env_no_expiry_field: credentials without expiresAt → no warning/error
- test_build_claude_env_unix_timestamp_near_expiry: expiresAt as Unix int → WARNING logged
- test_build_claude_env_malformed_expiry: unparseable expiresAt → only DEBUG, no warning/error
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import (
    _TOKEN_EXPIRY_WARN_SECONDS,
    _build_claude_env,
    _check_token_expiry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_credentials(expires_at=None, access_token="tok-abc") -> str:
    """Build a minimal credentials.json payload."""
    oauth: dict = {"accessToken": access_token}
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    return json.dumps({"claudeAiOauth": oauth})


# ---------------------------------------------------------------------------
# Tests for _check_token_expiry (pure unit — no filesystem)
# ---------------------------------------------------------------------------

class TestCheckTokenExpiry:
    def test_warns_near_expiry_iso_string(self, caplog):
        """Token expiring in 1 hour (well within 2-hour threshold) → WARNING."""
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        with caplog.at_level("WARNING", logger="src.orchestration.steward"):
            _check_token_expiry(soon.isoformat())
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "Expected a WARNING log for near-expiry token"
        assert "expires in" in warnings[0].message

    def test_errors_on_expired_iso_string(self, caplog):
        """Token expired 1 hour ago → ERROR."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with caplog.at_level("ERROR", logger="src.orchestration.steward"):
            _check_token_expiry(past.isoformat())
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, "Expected an ERROR log for expired token"
        assert "expired" in errors[0].message

    def test_no_warning_for_fresh_token(self, caplog):
        """Token expiring in 10 hours (beyond 2-hour threshold) → no warning/error."""
        future = datetime.now(timezone.utc) + timedelta(hours=10)
        with caplog.at_level("DEBUG", logger="src.orchestration.steward"):
            _check_token_expiry(future.isoformat())
        bad = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
        assert not bad, f"Unexpected warning/error for fresh token: {bad}"

    def test_warns_near_expiry_unix_timestamp(self, caplog):
        """expiresAt as a Unix integer (1 hour from now) → WARNING."""
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        unix_ts = soon.timestamp()
        with caplog.at_level("WARNING", logger="src.orchestration.steward"):
            _check_token_expiry(unix_ts)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "Expected a WARNING log for near-expiry Unix timestamp"

    def test_no_log_for_none(self, caplog):
        """None expiresAt → only DEBUG, no warning/error."""
        with caplog.at_level("DEBUG", logger="src.orchestration.steward"):
            _check_token_expiry(None)
        bad = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
        assert not bad, f"Unexpected warning/error for None expiresAt: {bad}"

    def test_debug_for_malformed_expiry(self, caplog):
        """Unparseable expiresAt → only DEBUG, no warning/error."""
        with caplog.at_level("DEBUG", logger="src.orchestration.steward"):
            _check_token_expiry("not-a-date")
        bad = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
        assert not bad, f"Unexpected warning/error for malformed expiresAt: {bad}"


# ---------------------------------------------------------------------------
# Tests for _build_claude_env() (filesystem-level — mock credentials.json)
# ---------------------------------------------------------------------------

class TestBuildClaudeEnv:
    def test_build_claude_env_warns_near_expiry(self, caplog, tmp_path, monkeypatch):
        """Mock credentials with expiresAt 1 hour from now → WARNING logged."""
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(expires_at=soon.isoformat()))

        import src.orchestration.steward as steward_mod
        monkeypatch.setattr(steward_mod, "_CREDENTIALS_PATH", creds_file)
        # Ensure CLAUDE_CODE_OAUTH_TOKEN is not in env so we hit the slow path.
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        with caplog.at_level("WARNING", logger="src.orchestration.steward"):
            _build_claude_env()

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "Expected WARNING for near-expiry token in _build_claude_env"
        assert "expires in" in warnings[0].message

    def test_build_claude_env_errors_on_expired(self, caplog, tmp_path, monkeypatch):
        """Mock credentials with expiresAt 1 hour ago → ERROR logged."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(expires_at=past.isoformat()))

        import src.orchestration.steward as steward_mod
        monkeypatch.setattr(steward_mod, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        with caplog.at_level("ERROR", logger="src.orchestration.steward"):
            _build_claude_env()

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, "Expected ERROR for expired token in _build_claude_env"
        assert "expired" in errors[0].message

    def test_build_claude_env_no_expiry_field(self, caplog, tmp_path, monkeypatch):
        """credentials.json without expiresAt → no warning or error logged."""
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials())  # no expires_at

        import src.orchestration.steward as steward_mod
        monkeypatch.setattr(steward_mod, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        with caplog.at_level("DEBUG", logger="src.orchestration.steward"):
            _build_claude_env()

        bad = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
        assert not bad, f"Unexpected warning/error when expiresAt absent: {bad}"

    def test_build_claude_env_fast_path_skips_expiry_check(self, caplog, monkeypatch):
        """When CLAUDE_CODE_OAUTH_TOKEN already set in env, credentials.json is not read."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "already-set")

        with caplog.at_level("DEBUG", logger="src.orchestration.steward"):
            env = _build_claude_env()

        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "already-set"
        # No expiry-related log should appear since we never read credentials.json.
        bad = [r for r in caplog.records if "expir" in r.message.lower() and r.levelname in ("WARNING", "ERROR")]
        assert not bad, f"Unexpected expiry log on fast path: {bad}"
