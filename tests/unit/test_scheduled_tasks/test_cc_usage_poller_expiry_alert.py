"""
Tests for cc-usage-poller.py — cookie-expiry alert feature.

Behavioral spec (from issue #1122 / cc-poller-expiry-alert task):
- A 401 or 403 HTTP response triggers an inbox message that delivers
  a Telegram alert to Dan.
- The alert is rate-limited: only one per calendar day (UTC). A second
  401 on the same day must NOT write another inbox file.
- Alert rate-limit resets on the next calendar day.
- The inbox message has the required fields: id, source, type, chat_id,
  timestamp, text.
- In dry_run mode no inbox file is written and no sentinel is created.
- The sentinel path respects the COOKIE_EXPIRY_SENTINEL_PREFIX constant.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    import requests.exceptions as _req_exc
    _RequestsHTTPError = _req_exc.HTTPError
except ImportError:  # pragma: no cover
    _RequestsHTTPError = Exception

# ---------------------------------------------------------------------------
# Load the module under test from its script path
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scheduled-tasks"
    / "cc-usage-poller.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("cc_usage_poller", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load_module()

# Constants imported from production module — importing rather than
# re-declaring prevents silent divergence when values change in the module.
ADMIN_CHAT_ID = cp.ADMIN_CHAT_ID
SENTINEL_PREFIX = cp.COOKIE_EXPIRY_SENTINEL_PREFIX


# ---------------------------------------------------------------------------
# Helper — build a fake HTTPError for a given HTTP status code
# ---------------------------------------------------------------------------

def _http_error(code: int) -> "_RequestsHTTPError":
    mock_response = MagicMock()
    mock_response.status_code = code
    return _RequestsHTTPError(response=mock_response)


# ---------------------------------------------------------------------------
# Tests — _cookie_expiry_alert_already_sent_today
# ---------------------------------------------------------------------------

class TestAlreadySentToday:
    def test_returns_false_when_no_sentinel_exists(self, tmp_path):
        """No sentinel file → not yet sent today."""
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        # Ensure the file does not exist (it shouldn't in tmp, but be explicit)
        sentinel = tmp_path / f"cc-usage-cookie-expired-alert-{today}"
        assert not sentinel.exists()

        with patch.object(Path, "exists", return_value=False):
            assert cp._cookie_expiry_alert_already_sent_today() is False

    def test_returns_true_when_sentinel_exists(self):
        """Sentinel file present → already sent today."""
        with patch.object(Path, "exists", return_value=True):
            assert cp._cookie_expiry_alert_already_sent_today() is True


# ---------------------------------------------------------------------------
# Tests — _write_cookie_expiry_alert
# ---------------------------------------------------------------------------

class TestWriteCookieExpiryAlert:
    def test_writes_inbox_message_with_required_fields(self, tmp_path):
        """On first call (no sentinel), inbox file is written with all required fields."""
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()

        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        sentinel_path = sentinel_dir / f"cc-usage-cookie-expired-alert-{today}"

        with (
            patch.object(cp, "_inbox_dir", return_value=inbox),
            patch.object(cp, "_cookie_expiry_alert_already_sent_today", return_value=False),
            patch.object(cp, "COOKIE_EXPIRY_SENTINEL_PREFIX", str(sentinel_dir) + "/cc-usage-cookie-expired-alert-"),
        ):
            cp._write_cookie_expiry_alert(http_code=401)

        # Exactly one JSON file written to inbox
        json_files = list(inbox.glob("*.json"))
        assert len(json_files) == 1

        msg = json.loads(json_files[0].read_text())
        assert msg["source"] == "system"
        assert msg["type"] == "message"
        assert msg["chat_id"] == ADMIN_CHAT_ID
        assert "id" in msg
        assert "timestamp" in msg
        assert "cookie expired" in msg["text"].lower() or "cc usage" in msg["text"].lower()

    def test_skips_when_already_sent_today(self, tmp_path):
        """Second 401 on same day must not write another inbox message."""
        inbox = tmp_path / "inbox"

        with (
            patch.object(cp, "_inbox_dir", return_value=inbox),
            patch.object(cp, "_cookie_expiry_alert_already_sent_today", return_value=True),
        ):
            cp._write_cookie_expiry_alert(http_code=401)

        assert not inbox.exists() or len(list(inbox.glob("*.json"))) == 0

    def test_touches_sentinel_after_writing_inbox_message(self, tmp_path):
        """After writing the inbox message, sentinel file is created."""
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        with (
            patch.object(cp, "_inbox_dir", return_value=inbox),
            patch.object(cp, "_cookie_expiry_alert_already_sent_today", return_value=False),
            patch.object(cp, "COOKIE_EXPIRY_SENTINEL_PREFIX", str(sentinel_dir) + "/cc-usage-cookie-expired-alert-"),
        ):
            cp._write_cookie_expiry_alert(http_code=403)

        sentinel = sentinel_dir / f"cc-usage-cookie-expired-alert-{today}"
        assert sentinel.exists()

    def test_dry_run_writes_nothing(self, tmp_path):
        """In dry_run mode: no inbox file, no sentinel."""
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()

        with (
            patch.object(cp, "_inbox_dir", return_value=inbox),
            patch.object(cp, "_cookie_expiry_alert_already_sent_today", return_value=False),
            patch.object(cp, "COOKIE_EXPIRY_SENTINEL_PREFIX", str(sentinel_dir) + "/cc-usage-cookie-expired-alert-"),
        ):
            cp._write_cookie_expiry_alert(http_code=401, dry_run=True)

        # Nothing written
        assert not inbox.exists() or len(list(inbox.glob("*.json"))) == 0
        assert len(list(sentinel_dir.iterdir())) == 0

    def test_inbox_file_written_atomically_via_tmp_rename(self, tmp_path):
        """Inbox write uses .json.tmp → .json rename (no partial file visible)."""
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()

        with (
            patch.object(cp, "_inbox_dir", return_value=inbox),
            patch.object(cp, "_cookie_expiry_alert_already_sent_today", return_value=False),
            patch.object(cp, "COOKIE_EXPIRY_SENTINEL_PREFIX", str(sentinel_dir) + "/cc-usage-cookie-expired-alert-"),
        ):
            cp._write_cookie_expiry_alert(http_code=401)

        # No .tmp files left behind
        tmp_files = list(inbox.glob("*.tmp"))
        assert tmp_files == []

    def test_403_triggers_alert_same_as_401(self, tmp_path):
        """Both 401 and 403 must trigger the alert (not just 401)."""
        inbox = tmp_path / "inbox"
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()

        with (
            patch.object(cp, "_inbox_dir", return_value=inbox),
            patch.object(cp, "_cookie_expiry_alert_already_sent_today", return_value=False),
            patch.object(cp, "COOKIE_EXPIRY_SENTINEL_PREFIX", str(sentinel_dir) + "/cc-usage-cookie-expired-alert-"),
        ):
            cp._write_cookie_expiry_alert(http_code=403)

        assert len(list(inbox.glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# Tests — main() integration: alert triggered on 401/403 HTTP response
# ---------------------------------------------------------------------------

class TestMainAlertOnAuthError:
    def _make_mock_cookie(self, tmp_path: Path) -> Path:
        cookie_file = tmp_path / "cc-usage-session-cookie"
        cookie_file.write_text("test-session-key\n")
        return cookie_file

    def test_main_calls_alert_on_401(self, tmp_path):
        """main() must call _write_cookie_expiry_alert when fetch returns 401."""
        cookie_file = self._make_mock_cookie(tmp_path)

        with (
            patch.object(cp, "COOKIE_CONFIG_PATH", cookie_file),
            patch.object(cp, "_is_job_enabled", return_value=True),
            patch.object(cp, "fetch_usage", side_effect=_http_error(401)),
            patch.object(cp, "_write_cookie_expiry_alert") as mock_alert,
        ):
            result = cp.main(dry_run=False)

        assert result == 0
        mock_alert.assert_called_once_with(401, dry_run=False)

    def test_main_calls_alert_on_403(self, tmp_path):
        """main() must call _write_cookie_expiry_alert when fetch returns 403."""
        cookie_file = self._make_mock_cookie(tmp_path)

        with (
            patch.object(cp, "COOKIE_CONFIG_PATH", cookie_file),
            patch.object(cp, "_is_job_enabled", return_value=True),
            patch.object(cp, "fetch_usage", side_effect=_http_error(403)),
            patch.object(cp, "_write_cookie_expiry_alert") as mock_alert,
        ):
            result = cp.main(dry_run=False)

        assert result == 0
        mock_alert.assert_called_once_with(403, dry_run=False)

    def test_main_does_not_call_alert_on_500(self, tmp_path):
        """main() must NOT call the alert for non-auth HTTP errors (e.g. 500)."""
        cookie_file = self._make_mock_cookie(tmp_path)

        with (
            patch.object(cp, "COOKIE_CONFIG_PATH", cookie_file),
            patch.object(cp, "_is_job_enabled", return_value=True),
            patch.object(cp, "fetch_usage", side_effect=_http_error(500)),
            patch.object(cp, "_write_cookie_expiry_alert") as mock_alert,
        ):
            result = cp.main(dry_run=False)

        assert result == 0
        mock_alert.assert_not_called()

    def test_main_does_not_call_alert_on_dry_run(self, tmp_path):
        """dry_run returns before the HTTP call — alert must not be called."""
        cookie_file = self._make_mock_cookie(tmp_path)

        with (
            patch.object(cp, "COOKIE_CONFIG_PATH", cookie_file),
            patch.object(cp, "_is_job_enabled", return_value=True),
            patch.object(cp, "_write_cookie_expiry_alert") as mock_alert,
        ):
            result = cp.main(dry_run=True)

        assert result == 0
        mock_alert.assert_not_called()
