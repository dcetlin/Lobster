"""
Unit tests for OAuth token refresh in steward._build_claude_env().

Issue #775 — extend PR #798's expiry detection to also attempt a refresh
when the token is within the warning window or already expired.

Covers:
- refresh triggered when token is within warning window (near expiry)
- refresh triggered when token is already expired
- refresh skipped when token is fresh (beyond warning threshold)
- refresh skipped on fast path (CLAUDE_CODE_OAUTH_TOKEN already in env)
- successful refresh writes new accessToken + expiresAt to credentials.json
- successful refresh updates CLAUDE_CODE_OAUTH_TOKEN in returned env
- refresh failure (network error) logs error, does not raise, returns old token
- refresh failure (HTTP 4xx) logs error, does not raise, returns old token
- refresh failure (invalid JSON response) logs error, does not raise, returns old token
- millisecond timestamp in expiresAt is detected and handled correctly
- _refresh_oauth_token pure: does not modify its inputs
"""

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Named constants (mirror what the implementation should define)
# ---------------------------------------------------------------------------

# A timestamp far enough in the future to be "ms" — ~year 2026 in ms vs seconds
_MILLIS_THRESHOLD = 1e11  # values > this are assumed to be milliseconds

# Warning window from spec
_WARN_HOURS = 2

# Anthropic's token endpoint (discovered from CLI binary)
_ANTHROPIC_TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_steward():
    import importlib
    import src.orchestration.steward as steward_mod
    return steward_mod


def _unix_ts_from_now(hours: float) -> float:
    """Return a Unix timestamp (seconds) N hours from now."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp()


def _unix_ts_ms_from_now(hours: float) -> int:
    """Return a Unix timestamp in milliseconds N hours from now."""
    return int(_unix_ts_from_now(hours) * 1000)


def _make_credentials(
    access_token: str = "tok-access-old",
    refresh_token: str = "tok-refresh",
    expires_at=None,
) -> str:
    """Build a minimal credentials.json payload."""
    oauth: dict = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
    }
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    return json.dumps({"claudeAiOauth": oauth})


def _make_token_refresh_response(
    access_token: str = "tok-access-new",
    expires_in_seconds: int = 28800,  # 8 hours
) -> bytes:
    """Fake Anthropic token endpoint response body."""
    return json.dumps({
        "access_token": access_token,
        "expires_in": expires_in_seconds,
        "token_type": "Bearer",
    }).encode()


# ---------------------------------------------------------------------------
# Tests: _refresh_oauth_token — pure network call, writes credentials back
# ---------------------------------------------------------------------------

class TestRefreshOAuthToken:
    """Tests for the _refresh_oauth_token function.

    Verifies the function:
    1. Calls the Anthropic token endpoint with correct payload.
    2. Writes the new access_token and updated expiresAt back to disk.
    3. Returns the new access token string on success.
    4. Does NOT raise on network failure — logs and returns None.
    5. Does NOT raise on HTTP error — logs and returns None.
    6. Does NOT raise on malformed response — logs and returns None.
    """

    def test_calls_anthropic_endpoint_with_refresh_grant(self, tmp_path):
        """Refresh call uses grant_type=refresh_token and sends the refresh token."""
        steward = _import_steward()

        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(refresh_token="my-refresh-tok"))

        captured_requests = []

        def mock_urlopen(req, timeout=None):
            captured_requests.append(req)
            resp = MagicMock()
            resp.read.return_value = _make_token_refresh_response()
            resp.__enter__ = lambda s: resp
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", mock_urlopen):
            steward._refresh_oauth_token("my-refresh-tok", creds_file)

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req.full_url == _ANTHROPIC_TOKEN_ENDPOINT

        # Payload must contain grant_type and refresh_token
        body = json.loads(req.data.decode())
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "my-refresh-tok"

    def test_writes_new_access_token_to_credentials_file(self, tmp_path):
        """On success, credentials.json is updated with the new access token."""
        steward = _import_steward()

        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(
            access_token="tok-old",
            refresh_token="tok-refresh",
            expires_at=_unix_ts_from_now(-1),  # expired 1 hour ago
        ))

        def mock_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = _make_token_refresh_response(access_token="tok-new")
            resp.__enter__ = lambda s: resp
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", mock_urlopen):
            steward._refresh_oauth_token("tok-refresh", creds_file)

        written = json.loads(creds_file.read_text())
        assert written["claudeAiOauth"]["accessToken"] == "tok-new"

    def test_returns_new_access_token_on_success(self, tmp_path):
        """Return value is the new access token string."""
        steward = _import_steward()

        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(refresh_token="tok-refresh"))

        def mock_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = _make_token_refresh_response(access_token="tok-fresh")
            resp.__enter__ = lambda s: resp
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", mock_urlopen):
            result = steward._refresh_oauth_token("tok-refresh", creds_file)

        assert result == "tok-fresh"

    def test_writes_updated_expiry_to_credentials_file(self, tmp_path):
        """On success, credentials.json expiresAt is updated to reflect new expiry."""
        steward = _import_steward()

        creds_file = tmp_path / ".credentials.json"
        old_expiry = _unix_ts_from_now(-1)  # expired
        creds_file.write_text(_make_credentials(
            expires_at=old_expiry,
            refresh_token="tok-refresh",
        ))

        def mock_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = _make_token_refresh_response(expires_in_seconds=3600)
            resp.__enter__ = lambda s: resp
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        before = datetime.now(timezone.utc)
        with patch("urllib.request.urlopen", mock_urlopen):
            steward._refresh_oauth_token("tok-refresh", creds_file)
        after = datetime.now(timezone.utc)

        written = json.loads(creds_file.read_text())
        new_expiry_raw = written["claudeAiOauth"]["expiresAt"]
        # Accept either seconds or milliseconds — just verify it's in the future
        if new_expiry_raw > _MILLIS_THRESHOLD:
            new_expiry = datetime.fromtimestamp(new_expiry_raw / 1000, tz=timezone.utc)
        else:
            new_expiry = datetime.fromtimestamp(new_expiry_raw, tz=timezone.utc)
        assert new_expiry > before, "New expiresAt must be in the future"

    def test_network_error_returns_none_and_logs_error(self, tmp_path, caplog):
        """When urlopen raises URLError, return None and log an error — do not raise."""
        steward = _import_steward()

        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(refresh_token="tok-refresh"))

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with caplog.at_level("ERROR", logger="src.orchestration.steward"):
                result = steward._refresh_oauth_token("tok-refresh", creds_file)

        assert result is None
        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, "Expected ERROR log on network failure"

    def test_http_401_returns_none_and_logs_error(self, tmp_path, caplog):
        """When the server responds with 401, return None and log an error."""
        steward = _import_steward()

        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(refresh_token="tok-refresh"))

        http_err = urllib.error.HTTPError(
            url=_ANTHROPIC_TOKEN_ENDPOINT,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            with caplog.at_level("ERROR", logger="src.orchestration.steward"):
                result = steward._refresh_oauth_token("tok-refresh", creds_file)

        assert result is None
        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, "Expected ERROR log on HTTP 401"

    def test_malformed_response_returns_none_and_logs_error(self, tmp_path, caplog):
        """When the response body is not valid JSON, return None and log an error."""
        steward = _import_steward()

        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(refresh_token="tok-refresh"))

        def mock_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = b"not json {{{"
            resp.__enter__ = lambda s: resp
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", mock_urlopen):
            with caplog.at_level("ERROR", logger="src.orchestration.steward"):
                result = steward._refresh_oauth_token("tok-refresh", creds_file)

        assert result is None
        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, "Expected ERROR log on malformed response"

    def test_preserves_other_credential_fields_on_write(self, tmp_path):
        """When writing the updated token, other fields in credentials.json are preserved."""
        steward = _import_steward()

        original = {
            "claudeAiOauth": {
                "accessToken": "tok-old",
                "refreshToken": "tok-refresh",
                "expiresAt": _unix_ts_from_now(-1),
                "scopes": ["user:inference"],
                "subscriptionType": "max",
                "rateLimitTier": "standard_plus_1",
            }
        }
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(json.dumps(original))

        def mock_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = _make_token_refresh_response(access_token="tok-new")
            resp.__enter__ = lambda s: resp
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", mock_urlopen):
            steward._refresh_oauth_token("tok-refresh", creds_file)

        written = json.loads(creds_file.read_text())
        oauth = written["claudeAiOauth"]
        assert oauth["refreshToken"] == "tok-refresh"
        assert oauth["scopes"] == ["user:inference"]
        assert oauth["subscriptionType"] == "max"
        assert oauth["rateLimitTier"] == "standard_plus_1"


# ---------------------------------------------------------------------------
# Tests: _build_claude_env — refresh integration
# ---------------------------------------------------------------------------

class TestBuildClaudeEnvRefresh:
    """Integration tests verifying _build_claude_env triggers refresh correctly."""

    def test_refresh_triggered_when_token_near_expiry(self, tmp_path, monkeypatch):
        """When token is within the warning window, _refresh_oauth_token is called."""
        steward = _import_steward()

        # Token expires in 1 hour — within the 2h warning window
        expires_at_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(
            access_token="tok-old",
            refresh_token="tok-refresh",
            expires_at=expires_at_iso,
        ))

        monkeypatch.setattr(steward, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        refresh_calls = []

        def mock_refresh(refresh_token, credentials_path):
            refresh_calls.append((refresh_token, credentials_path))
            return "tok-new"

        monkeypatch.setattr(steward, "_refresh_oauth_token", mock_refresh)

        env = steward._build_claude_env()

        assert len(refresh_calls) == 1, "Expected _refresh_oauth_token to be called once"
        assert refresh_calls[0][0] == "tok-refresh"

    def test_refresh_triggered_when_token_expired(self, tmp_path, monkeypatch):
        """When token is already expired, _refresh_oauth_token is called."""
        steward = _import_steward()

        expires_at_iso = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(
            access_token="tok-old",
            refresh_token="tok-refresh",
            expires_at=expires_at_iso,
        ))

        monkeypatch.setattr(steward, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        refresh_calls = []

        def mock_refresh(refresh_token, credentials_path):
            refresh_calls.append(refresh_token)
            return "tok-renewed"

        monkeypatch.setattr(steward, "_refresh_oauth_token", mock_refresh)

        env = steward._build_claude_env()

        assert len(refresh_calls) == 1, "Expected refresh when token expired"

    def test_refresh_skipped_when_token_is_fresh(self, tmp_path, monkeypatch):
        """When token expires more than 2 hours from now, no refresh is attempted."""
        steward = _import_steward()

        # Token expires in 10 hours — well outside the warning window
        expires_at_iso = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(
            access_token="tok-ok",
            refresh_token="tok-refresh",
            expires_at=expires_at_iso,
        ))

        monkeypatch.setattr(steward, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        refresh_calls = []

        def mock_refresh(refresh_token, credentials_path):
            refresh_calls.append(refresh_token)
            return "tok-new"

        monkeypatch.setattr(steward, "_refresh_oauth_token", mock_refresh)

        env = steward._build_claude_env()

        assert len(refresh_calls) == 0, "Expected NO refresh for fresh token"
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-ok"

    def test_refresh_skipped_on_fast_path(self, monkeypatch):
        """When CLAUDE_CODE_OAUTH_TOKEN is already set in env, no refresh is attempted."""
        steward = _import_steward()

        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-from-env")

        refresh_calls = []

        def mock_refresh(refresh_token, credentials_path):
            refresh_calls.append(refresh_token)
            return "tok-new"

        monkeypatch.setattr(steward, "_refresh_oauth_token", mock_refresh)

        env = steward._build_claude_env()

        assert len(refresh_calls) == 0, "Expected NO refresh on fast path"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-from-env"

    def test_refresh_success_updates_env_token(self, tmp_path, monkeypatch):
        """When refresh succeeds, the new token is used in the returned env dict."""
        steward = _import_steward()

        expires_at_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(
            access_token="tok-stale",
            refresh_token="tok-refresh",
            expires_at=expires_at_iso,
        ))

        monkeypatch.setattr(steward, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        monkeypatch.setattr(
            steward,
            "_refresh_oauth_token",
            lambda rt, cp: "tok-fresh",
        )

        env = steward._build_claude_env()

        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-fresh"

    def test_refresh_failure_falls_back_to_old_token(self, tmp_path, monkeypatch, caplog):
        """When refresh fails (returns None), the old token is still used — no crash."""
        steward = _import_steward()

        expires_at_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(
            access_token="tok-expired-but-available",
            refresh_token="tok-refresh",
            expires_at=expires_at_iso,
        ))

        monkeypatch.setattr(steward, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        # Simulate refresh failure — returns None
        monkeypatch.setattr(steward, "_refresh_oauth_token", lambda rt, cp: None)

        with caplog.at_level("ERROR", logger="src.orchestration.steward"):
            env = steward._build_claude_env()

        # Must not raise; should still contain the old token
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-expired-but-available"

    def test_no_refresh_when_no_refresh_token_in_credentials(self, tmp_path, monkeypatch, caplog):
        """When credentials.json has no refreshToken, log a warning but do not crash."""
        steward = _import_steward()

        expires_at_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        creds_no_refresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "tok-ok",
                "expiresAt": expires_at_iso,
                # No refreshToken field
            }
        })
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(creds_no_refresh)

        monkeypatch.setattr(steward, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        refresh_calls = []

        def mock_refresh(refresh_token, credentials_path):
            refresh_calls.append(refresh_token)
            return "tok-new"

        monkeypatch.setattr(steward, "_refresh_oauth_token", mock_refresh)

        with caplog.at_level("WARNING", logger="src.orchestration.steward"):
            env = steward._build_claude_env()

        # Should not call refresh if there is no refresh token
        assert len(refresh_calls) == 0, "Must not call refresh without a refresh token"
        # Old token still used
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-ok"


# ---------------------------------------------------------------------------
# Tests: millisecond timestamp handling
# ---------------------------------------------------------------------------

class TestMillisecondTimestampHandling:
    """Verify that expiresAt values in milliseconds are correctly detected."""

    def test_ms_timestamp_near_expiry_triggers_refresh(self, tmp_path, monkeypatch):
        """expiresAt in milliseconds that is near-expiry triggers refresh."""
        steward = _import_steward()

        # 1 hour from now in ms
        expires_at_ms = _unix_ts_ms_from_now(1)
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(
            access_token="tok-old",
            refresh_token="tok-refresh",
            expires_at=expires_at_ms,
        ))

        monkeypatch.setattr(steward, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        refresh_calls = []

        def mock_refresh(rt, cp):
            refresh_calls.append(rt)
            return "tok-new"

        monkeypatch.setattr(steward, "_refresh_oauth_token", mock_refresh)

        env = steward._build_claude_env()

        assert len(refresh_calls) == 1, "Expected refresh for ms-format near-expiry timestamp"

    def test_ms_timestamp_fresh_does_not_trigger_refresh(self, tmp_path, monkeypatch):
        """expiresAt in milliseconds that is well in the future skips refresh."""
        steward = _import_steward()

        # 10 hours from now in ms
        expires_at_ms = _unix_ts_ms_from_now(10)
        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(_make_credentials(
            access_token="tok-ok",
            refresh_token="tok-refresh",
            expires_at=expires_at_ms,
        ))

        monkeypatch.setattr(steward, "_CREDENTIALS_PATH", creds_file)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        refresh_calls = []

        def mock_refresh(rt, cp):
            refresh_calls.append(rt)
            return "tok-new"

        monkeypatch.setattr(steward, "_refresh_oauth_token", mock_refresh)

        env = steward._build_claude_env()

        assert len(refresh_calls) == 0, "No refresh for ms-format fresh token"
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-ok"
