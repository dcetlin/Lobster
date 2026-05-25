"""
Unit tests for routing_pref_confirm / routing_pref_reject callback handlers in
dispatcher_handlers.py.

Behavior coverage:
- route_callback_message routes routing_pref_confirm: to handle_routing_pref_callback
- route_callback_message routes routing_pref_reject: to handle_routing_pref_callback
- routing_pref_confirm: writes a new rule to routing-preferences.yaml
- routing_pref_reject: records rejection without modifying routing-preferences.yaml
- handle_routing_pref_callback returns an error string for a malformed payload
- confirmed rule is appended (not replaced) when rules list already has entries
- confirmed rule uses "rules" key, not "preferences" (normalisation)
- CALLBACK_DATA_HANDLERS frozenset includes the new prefixes
- maybe_annotate_routing_hint annotates message with routing_hint when rule matches
- maybe_annotate_routing_hint returns original message unmodified when no rule matches
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_payload(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _make_confirm_callback(payload: dict) -> str:
    return f"routing_pref_confirm:{_encode_payload(payload)}"


def _make_reject_callback(payload: dict) -> str:
    return f"routing_pref_reject:{_encode_payload(payload)}"


_SAMPLE_PAYLOAD = {
    "condition": "brain-dump messages arriving between 06:00-09:00",
    "agent_hint": "brain-dumps",
    "source_event_ids": ["ev_001"],
    "proposed_at": "2026-05-25T07:00:00+00:00",
    "confidence": 0.85,
}


# ---------------------------------------------------------------------------
# CALLBACK_DATA_HANDLERS frozenset
# ---------------------------------------------------------------------------

class TestCallbackDataHandlersFrozenset:
    def test_routing_pref_confirm_prefix_registered(self):
        from src.orchestration.dispatcher_handlers import CALLBACK_DATA_HANDLERS
        assert "routing_pref_confirm:" in CALLBACK_DATA_HANDLERS

    def test_routing_pref_reject_prefix_registered(self):
        from src.orchestration.dispatcher_handlers import CALLBACK_DATA_HANDLERS
        assert "routing_pref_reject:" in CALLBACK_DATA_HANDLERS


# ---------------------------------------------------------------------------
# route_callback_message routing
# ---------------------------------------------------------------------------

class TestRouteCallbackMessageRouting:
    def test_confirm_callback_is_handled(self, tmp_path):
        """route_callback_message marks routing_pref_confirm: as handled=True."""
        from src.orchestration.dispatcher_handlers import route_callback_message

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
        ):
            msg = {
                "type": "callback",
                "callback_data": _make_confirm_callback(_SAMPLE_PAYLOAD),
                "chat_id": 12345,
            }
            result = route_callback_message(msg)

        assert result["handled"] is True
        assert result["action"] == "send_reply"

    def test_reject_callback_is_handled(self, tmp_path):
        """route_callback_message marks routing_pref_reject: as handled=True."""
        from src.orchestration.dispatcher_handlers import route_callback_message

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
        ):
            msg = {
                "type": "callback",
                "callback_data": _make_reject_callback(_SAMPLE_PAYLOAD),
                "chat_id": 12345,
            }
            result = route_callback_message(msg)

        assert result["handled"] is True

    def test_unrelated_callback_not_handled(self):
        """Unrelated callback data still falls through (handled=False)."""
        from src.orchestration.dispatcher_handlers import route_callback_message
        msg = {
            "type": "callback",
            "callback_data": "job-confirm-yes:abc123",
            "chat_id": 12345,
        }
        result = route_callback_message(msg)
        assert result["handled"] is False


# ---------------------------------------------------------------------------
# handle_routing_pref_callback — confirm path
# ---------------------------------------------------------------------------

class TestHandleRoutingPrefCallbackConfirm:
    def test_confirm_writes_new_rule_to_yaml(self, tmp_path):
        """Confirming a proposal appends a new rule to routing-preferences.yaml."""
        from src.orchestration.dispatcher_handlers import handle_routing_pref_callback

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
            patch("src.orchestration.dispatcher_handlers.reload_routing_preferences"),
        ):
            reply = handle_routing_pref_callback(
                _make_confirm_callback(_SAMPLE_PAYLOAD),
                chat_id=12345,
            )

        assert "saved" in reply.lower() or "routing preference" in reply.lower()
        assert prefs_path.exists()
        content = yaml.safe_load(prefs_path.read_text())
        assert "rules" in content
        assert len(content["rules"]) == 1
        rule = content["rules"][0]
        assert rule["condition"] == _SAMPLE_PAYLOAD["condition"]
        assert rule["route_to"] == _SAMPLE_PAYLOAD["agent_hint"]
        assert rule["active"] is True
        assert rule["confirmed_by"] == "dan"

    def test_confirm_uses_rules_key_not_preferences(self, tmp_path):
        """The output YAML must use 'rules', not 'preferences', as the list key."""
        from src.orchestration.dispatcher_handlers import handle_routing_pref_callback

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
            patch("src.orchestration.dispatcher_handlers.reload_routing_preferences"),
        ):
            handle_routing_pref_callback(
                _make_confirm_callback(_SAMPLE_PAYLOAD),
                chat_id=12345,
            )

        raw = yaml.safe_load(prefs_path.read_text())
        assert "preferences" not in raw
        assert "rules" in raw

    def test_confirm_appends_to_existing_rules(self, tmp_path):
        """Confirming a second proposal appends, not replaces, the existing rule."""
        from src.orchestration.dispatcher_handlers import handle_routing_pref_callback

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"
        # Seed file with one existing rule
        prefs_path.write_text(yaml.dump({
            "version": 1,
            "rules": [{"id": "r_old", "condition": "other condition", "route_to": "general-purpose", "active": True}],
        }))

        second_payload = {**_SAMPLE_PAYLOAD, "condition": "voice note at night", "agent_hint": "brain-dumps"}
        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
            patch("src.orchestration.dispatcher_handlers.reload_routing_preferences"),
        ):
            handle_routing_pref_callback(
                _make_confirm_callback(second_payload),
                chat_id=12345,
            )

        raw = yaml.safe_load(prefs_path.read_text())
        assert len(raw["rules"]) == 2
        rule_ids = [r["id"] for r in raw["rules"]]
        assert "r_old" in rule_ids

    def test_confirm_records_proposal_as_accepted(self, tmp_path):
        """Confirming a proposal records 'accepted' status in pending-routing-proposals.json."""
        from src.orchestration.dispatcher_handlers import handle_routing_pref_callback

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
            patch("src.orchestration.dispatcher_handlers.reload_routing_preferences"),
        ):
            handle_routing_pref_callback(
                _make_confirm_callback(_SAMPLE_PAYLOAD),
                chat_id=12345,
            )

        assert proposals_path.exists()
        proposals = json.loads(proposals_path.read_text())
        statuses = [v["status"] for v in proposals.values()]
        assert "accepted" in statuses

    def test_confirm_triggers_cache_reload(self, tmp_path):
        """Confirming a proposal calls reload_routing_preferences to update the cache."""
        from src.orchestration.dispatcher_handlers import handle_routing_pref_callback

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
            patch("src.orchestration.dispatcher_handlers.reload_routing_preferences") as mock_reload,
        ):
            handle_routing_pref_callback(
                _make_confirm_callback(_SAMPLE_PAYLOAD),
                chat_id=12345,
            )

        mock_reload.assert_called_once()


# ---------------------------------------------------------------------------
# handle_routing_pref_callback — reject path
# ---------------------------------------------------------------------------

class TestHandleRoutingPrefCallbackReject:
    def test_reject_does_not_write_yaml(self, tmp_path):
        """Rejecting a proposal leaves routing-preferences.yaml untouched."""
        from src.orchestration.dispatcher_handlers import handle_routing_pref_callback

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
        ):
            reply = handle_routing_pref_callback(
                _make_reject_callback(_SAMPLE_PAYLOAD),
                chat_id=12345,
            )

        assert not prefs_path.exists()
        assert "skipped" in reply.lower() or "discard" in reply.lower()

    def test_reject_records_proposal_as_rejected(self, tmp_path):
        """Rejecting a proposal records 'rejected' status in pending-routing-proposals.json."""
        from src.orchestration.dispatcher_handlers import handle_routing_pref_callback

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
        ):
            handle_routing_pref_callback(
                _make_reject_callback(_SAMPLE_PAYLOAD),
                chat_id=12345,
            )

        assert proposals_path.exists()
        proposals = json.loads(proposals_path.read_text())
        statuses = [v["status"] for v in proposals.values()]
        assert "rejected" in statuses


# ---------------------------------------------------------------------------
# handle_routing_pref_callback — error handling
# ---------------------------------------------------------------------------

class TestHandleRoutingPrefCallbackErrors:
    def test_malformed_base64_payload_returns_error_string(self, tmp_path):
        """A callback with garbage base64 returns a user-friendly error string."""
        from src.orchestration.dispatcher_handlers import handle_routing_pref_callback

        prefs_path = tmp_path / "routing-preferences.yaml"
        proposals_path = tmp_path / "pending-routing-proposals.json"

        with (
            patch("src.orchestration.dispatcher_handlers._ROUTING_PREFS_PATH", prefs_path),
            patch("src.orchestration.dispatcher_handlers._PENDING_PROPOSALS_PATH", proposals_path),
        ):
            reply = handle_routing_pref_callback(
                "routing_pref_confirm:!!!not-valid-base64!!!",
                chat_id=12345,
            )

        assert "routing_pref" in reply.lower() or "could not" in reply.lower() or "decode" in reply.lower()
        # Must NOT have written anything
        assert not prefs_path.exists()


# ---------------------------------------------------------------------------
# maybe_annotate_routing_hint
# ---------------------------------------------------------------------------

class TestMaybeAnnotateRoutingHint:
    def test_annotates_message_when_rule_matches(self, tmp_path):
        """When a rule matches, message gains a routing_hint key."""
        from src.orchestration import dispatcher_handlers as dh
        import src.orchestration.dispatcher_handlers as dh_mod

        rules = [{"id": "r1", "condition": "brain dump", "route_to": "brain-dumps", "active": True}]
        dh_mod._cached_routing_prefs = rules
        dh_mod._routing_prefs_message_counter = 9  # next call won't reload

        msg = {"type": "brain_dump", "source": "telegram"}
        result = dh.maybe_annotate_routing_hint(msg)

        assert result.get("routing_hint") == "brain-dumps"
        # Original dict must be immutable — the returned dict is a new copy
        assert "routing_hint" not in msg

    def test_returns_original_when_no_rule_matches(self):
        """When no rule matches, the message is returned without routing_hint."""
        from src.orchestration import dispatcher_handlers as dh
        import src.orchestration.dispatcher_handlers as dh_mod

        rules = [{"id": "r1", "condition": "brain dump", "route_to": "brain-dumps", "active": True}]
        dh_mod._cached_routing_prefs = rules
        dh_mod._routing_prefs_message_counter = 9

        msg = {"type": "code_review", "source": "telegram"}
        result = dh.maybe_annotate_routing_hint(msg)

        assert "routing_hint" not in result

    def test_returns_original_when_cache_empty(self):
        """When the cache is empty, the message is returned unchanged."""
        from src.orchestration import dispatcher_handlers as dh
        import src.orchestration.dispatcher_handlers as dh_mod

        dh_mod._cached_routing_prefs = []
        dh_mod._routing_prefs_message_counter = 9

        msg = {"type": "brain_dump", "source": "telegram"}
        result = dh.maybe_annotate_routing_hint(msg)

        assert result is msg  # unchanged object, not a new copy
