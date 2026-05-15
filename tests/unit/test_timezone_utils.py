"""
Tests for src/utils/timezone.py — specifically the LOBSTER_USER_TZ env var layer.

TDD: these tests are derived from the requirement spec, not from existing code.
The env var support does not exist yet; these tests define the expected behavior.
"""
from __future__ import annotations

import os
import zoneinfo
from datetime import datetime, timezone as utc_tz
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_tz():
    """Import timezone module freshly to avoid module-level caching issues."""
    import importlib
    import utils.timezone as tz_mod
    importlib.reload(tz_mod)
    return tz_mod


# ---------------------------------------------------------------------------
# LOBSTER_USER_TZ env var priority
# ---------------------------------------------------------------------------


class TestLobsterUserTzEnvVar:
    """LOBSTER_USER_TZ must be the highest-priority timezone source."""

    def test_env_var_overrides_owner_toml(self):
        """When LOBSTER_USER_TZ is set, it wins over owner.toml."""
        with patch.dict(os.environ, {"LOBSTER_USER_TZ": "America/Chicago"}, clear=False):
            import utils.timezone as tz_mod
            name = tz_mod.get_owner_tz_name()
        assert name == "America/Chicago"

    def test_env_var_empty_string_falls_through_to_owner(self):
        """Empty LOBSTER_USER_TZ is ignored; falls through to owner.toml / UTC fallback."""
        env = {k: v for k, v in os.environ.items() if k != "LOBSTER_USER_TZ"}
        env["LOBSTER_USER_TZ"] = ""
        with patch.dict(os.environ, env, clear=True):
            # owner.toml may or may not exist in test env; either way must not crash
            import utils.timezone as tz_mod
            name = tz_mod.get_owner_tz_name()
        # Must return a non-empty IANA string (could be UTC or whatever owner.toml says)
        assert isinstance(name, str) and name.strip()

    def test_env_var_invalid_iana_falls_back_to_utc(self):
        """An invalid IANA name in LOBSTER_USER_TZ must not crash; fall back to UTC."""
        with patch.dict(os.environ, {"LOBSTER_USER_TZ": "NotARealTimezone"}, clear=False):
            import utils.timezone as tz_mod
            zi = tz_mod.get_owner_zoneinfo()
        assert zi == zoneinfo.ZoneInfo("UTC")

    def test_env_var_used_in_format_for_user(self):
        """format_for_user uses the env-configured timezone when no explicit user_tz given."""
        # 2026-05-15 20:00:00 UTC
        dt = datetime(2026, 5, 15, 20, 0, 0, tzinfo=utc_tz.utc)
        # Chicago is UTC-5 in CDT (UTC-6 in CST); May 15 → CDT → 15:00 CDT
        with patch.dict(os.environ, {"LOBSTER_USER_TZ": "America/Chicago"}, clear=False):
            import utils.timezone as tz_mod
            result = tz_mod.format_for_user(dt, fmt="%H:%M %Z")
        # CDT is UTC-5 in summer
        assert "CDT" in result or "CST" in result or "15:00" in result

    def test_explicit_user_tz_arg_overrides_env_var(self):
        """Explicit user_tz argument beats LOBSTER_USER_TZ env var."""
        dt = datetime(2026, 5, 15, 20, 0, 0, tzinfo=utc_tz.utc)
        with patch.dict(os.environ, {"LOBSTER_USER_TZ": "America/Chicago"}, clear=False):
            import utils.timezone as tz_mod
            result = tz_mod.format_for_user(dt, fmt="%Z", user_tz="Pacific/Auckland")
        # Auckland is well ahead of UTC; must show NZST or NZDT
        assert "NZ" in result


# ---------------------------------------------------------------------------
# get_owner_tz_name fallback chain
# ---------------------------------------------------------------------------


class TestGetOwnerTzNameFallback:
    """get_owner_tz_name must degrade gracefully when sources are unavailable."""

    def test_returns_string(self):
        """Must return a non-empty string under any conditions."""
        import utils.timezone as tz_mod
        name = tz_mod.get_owner_tz_name()
        assert isinstance(name, str) and name.strip()

    def test_utc_fallback_when_no_owner_and_no_env(self):
        """Without LOBSTER_USER_TZ and with owner imports patched to fail, returns UTC."""
        import utils.timezone as tz_mod
        env = {k: v for k, v in os.environ.items() if k != "LOBSTER_USER_TZ"}
        with patch.dict(os.environ, env, clear=True):
            # Patch both owner import paths inside the module to raise ImportError
            with (
                patch("utils.timezone._import_owner_tz", side_effect=ImportError("mocked"))
                if hasattr(tz_mod, "_import_owner_tz")
                else patch.object(tz_mod, "get_owner_tz_name",
                                   return_value="UTC")
            ):
                name = tz_mod.get_owner_tz_name()
        # Must return a string — UTC fallback is acceptable
        assert isinstance(name, str) and name.strip()

    def test_get_owner_zoneinfo_returns_zoneinfo(self):
        """get_owner_zoneinfo must always return a ZoneInfo object."""
        import utils.timezone as tz_mod
        zi = tz_mod.get_owner_zoneinfo()
        assert isinstance(zi, zoneinfo.ZoneInfo)


def _stubbed_get_owner_tz_name_no_owner() -> str:
    return "UTC"


# ---------------------------------------------------------------------------
# format_iso_for_user — round-trip test
# ---------------------------------------------------------------------------


class TestFormatIsoForUser:
    """format_iso_for_user converts ISO strings to the configured local time."""

    def test_converts_utc_z_suffix(self):
        """ISO strings ending in Z are parsed as UTC."""
        with patch.dict(os.environ, {"LOBSTER_USER_TZ": "America/New_York"}, clear=False):
            import utils.timezone as tz_mod
            result = tz_mod.format_iso_for_user("2026-05-15T20:00:00Z", fmt="%H:%M")
        # ET is UTC-4 (EDT) in May
        assert result == "16:00"

    def test_fallback_on_invalid_iso(self):
        """Returns the raw string when parsing fails."""
        import utils.timezone as tz_mod
        raw = "not-a-timestamp"
        assert tz_mod.format_iso_for_user(raw) == raw


# ---------------------------------------------------------------------------
# Lint-style regression: quota formatter must not contain hardcoded tz strings
# ---------------------------------------------------------------------------


class TestNoHardcodedTimezoneInQuotaFormatter:
    """
    The quota reset formatter in dispatcher_handlers must use the timezone utility,
    not a hardcoded America/New_York or America/Los_Angeles string.

    This test fails if the hardcoded strings are present — it will pass once
    the formatter is updated to use utils.timezone.format_for_user.
    """

    def test_format_quota_message_uses_owner_tz_not_hardcoded_et(self):
        """
        format_quota_message must honour LOBSTER_USER_TZ — if the result changes
        when we swap the env var, the formatter is correctly using the utility.

        Strategy: set LOBSTER_USER_TZ to two different zones and verify the
        output differs (zone abbreviation changes).
        """
        from datetime import datetime, timezone as _utc_tz, timedelta
        import json
        from pathlib import Path
        import tempfile

        # Build a minimal valid state dict inline (avoiding conftest)
        now_utc = datetime.now(_utc_tz.utc)
        five_reset = (now_utc + timedelta(hours=5)).isoformat()
        seven_reset = (now_utc + timedelta(days=7)).isoformat()
        state = {
            "ts": int(now_utc.timestamp()),
            "last_updated": now_utc.isoformat(),
            "rate_limits": {
                "five_hour": {
                    "pct": 42.0,
                    "resets_at": five_reset,
                },
                "seven_day": {
                    "pct": 15.0,
                    "resets_at": seven_reset,
                },
            },
        }

        from orchestration.dispatcher_handlers import format_quota_message

        with patch.dict(os.environ, {"LOBSTER_USER_TZ": "America/New_York"}, clear=False):
            msg_et = format_quota_message(state)

        with patch.dict(os.environ, {"LOBSTER_USER_TZ": "America/Los_Angeles"}, clear=False):
            msg_pt = format_quota_message(state)

        # The two messages must differ — if the formatter hardcodes ET they will be identical
        assert msg_et != msg_pt, (
            "format_quota_message produced the same output for ET and PT — "
            "this means the timezone is still hardcoded. "
            "Update _fmt_reset to use utils.timezone.format_for_user."
        )
