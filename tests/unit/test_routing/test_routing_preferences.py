"""
Unit tests for src/routing/preferences.py — load_routing_preferences and match_routing_preference.

Behavior coverage:
- load_routing_preferences returns [] when file is absent
- load_routing_preferences returns [] when YAML is malformed
- load_routing_preferences returns only active rules (active: true)
- load_routing_preferences handles both "rules" and "preferences" YAML keys
- match_routing_preference returns None when rules list is empty
- match_routing_preference matches on message type keyword in condition
- match_routing_preference matches on time-range in condition when in range
- match_routing_preference skips time-range when outside range
- match_routing_preference returns None when no rule matches
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Ensure repo root is on path so src.routing is importable
_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_prefs(tmp_path: Path, rules: list[dict]) -> Path:
    """Write a minimal routing-preferences.yaml and return its path."""
    prefs_file = tmp_path / "routing-preferences.yaml"
    prefs_file.write_text(yaml.dump({"version": 1, "rules": rules}))
    return prefs_file


# ---------------------------------------------------------------------------
# load_routing_preferences
# ---------------------------------------------------------------------------

class TestLoadRoutingPreferences:
    def test_returns_empty_list_when_file_absent(self, tmp_path):
        from src.routing.preferences import load_routing_preferences
        result = load_routing_preferences(tmp_path / "nonexistent.yaml")
        assert result == []

    def test_returns_empty_list_on_malformed_yaml(self, tmp_path):
        from src.routing.preferences import load_routing_preferences
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{{not valid yaml::")
        result = load_routing_preferences(bad_file)
        assert result == []

    def test_returns_only_active_rules(self, tmp_path):
        from src.routing.preferences import load_routing_preferences
        rules = [
            {"id": "r1", "condition": "brain dump", "route_to": "brain-dumps", "active": True},
            {"id": "r2", "condition": "code review", "route_to": "functional-engineer", "active": False},
        ]
        prefs = _write_prefs(tmp_path, rules)
        result = load_routing_preferences(prefs)
        assert len(result) == 1
        assert result[0]["id"] == "r1"

    def test_treats_missing_active_flag_as_true(self, tmp_path):
        from src.routing.preferences import load_routing_preferences
        rules = [{"id": "r1", "condition": "voice note", "route_to": "brain-dumps"}]
        prefs = _write_prefs(tmp_path, rules)
        result = load_routing_preferences(prefs)
        assert len(result) == 1

    def test_accepts_preferences_key_as_alias_for_rules(self, tmp_path):
        from src.routing.preferences import load_routing_preferences
        prefs_file = tmp_path / "prefs.yaml"
        prefs_file.write_text(yaml.dump({
            "version": 1,
            "preferences": [
                {"id": "r1", "condition": "brain dump", "route_to": "brain-dumps", "active": True},
            ],
        }))
        result = load_routing_preferences(prefs_file)
        assert len(result) == 1
        assert result[0]["route_to"] == "brain-dumps"

    def test_returns_empty_list_when_rules_is_empty_array(self, tmp_path):
        from src.routing.preferences import load_routing_preferences
        prefs = _write_prefs(tmp_path, [])
        result = load_routing_preferences(prefs)
        assert result == []


# ---------------------------------------------------------------------------
# match_routing_preference
# ---------------------------------------------------------------------------

class TestMatchRoutingPreference:
    def test_returns_none_when_rules_empty(self):
        from src.routing.preferences import match_routing_preference
        assert match_routing_preference({"type": "brain_dump"}, []) is None

    def test_matches_brain_dump_type_in_condition(self):
        from src.routing.preferences import match_routing_preference
        rules = [{"id": "r1", "condition": "brain dump messages", "route_to": "brain-dumps", "active": True}]
        msg = {"type": "brain_dump", "source": "telegram"}
        result = match_routing_preference(msg, rules)
        assert result is not None
        assert result["id"] == "r1"

    def test_returns_none_when_no_condition_matches(self):
        from src.routing.preferences import match_routing_preference
        rules = [{"id": "r1", "condition": "voice note messages", "route_to": "brain-dumps", "active": True}]
        msg = {"type": "code_review", "source": "telegram"}
        result = match_routing_preference(msg, rules)
        assert result is None

    def test_matches_first_matching_rule_in_order(self):
        from src.routing.preferences import match_routing_preference
        rules = [
            {"id": "r1", "condition": "brain dump", "route_to": "brain-dumps", "active": True},
            {"id": "r2", "condition": "brain dump", "route_to": "voice-note-agent", "active": True},
        ]
        msg = {"type": "brain_dump", "source": "telegram"}
        result = match_routing_preference(msg, rules)
        assert result["id"] == "r1"

    def test_time_range_in_condition_matches_when_in_range(self, monkeypatch):
        """Condition with '06:00-09:00' should match when current hour is in that range."""
        from datetime import datetime, timezone
        import src.routing.preferences as prefs_module

        # Monkeypatch datetime.now to return 07:30 UTC
        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 5, 25, 7, 30, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(prefs_module, "datetime", FakeDatetime)

        from src.routing.preferences import match_routing_preference
        rules = [{"id": "r1", "condition": "brain dump messages 06:00-09:00", "route_to": "brain-dumps", "active": True}]
        msg = {"type": "brain_dump", "source": "telegram"}
        result = match_routing_preference(msg, rules)
        assert result is not None
        assert result["id"] == "r1"

    def test_time_range_in_condition_does_not_match_outside_range(self, monkeypatch):
        """Condition with '06:00-09:00' should not match when current hour is outside that range."""
        from datetime import datetime, timezone
        import src.routing.preferences as prefs_module

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(prefs_module, "datetime", FakeDatetime)

        from src.routing.preferences import match_routing_preference
        rules = [{"id": "r1", "condition": "brain dump messages 06:00-09:00", "route_to": "brain-dumps", "active": True}]
        msg = {"type": "brain_dump", "source": "telegram"}
        result = match_routing_preference(msg, rules)
        assert result is None
