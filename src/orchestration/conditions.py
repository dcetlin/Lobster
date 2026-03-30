"""
UoW trigger condition evaluator — Phase 2 polling implementation.

evaluate_condition(uow) is called by the Registrar sweep for each `pending`
UoW to determine whether the UoW's trigger condition has been met.

Design constraints:
- Does not raise: all error paths return True or False with audit_log entries.
- GitHub API call is isolated behind the `github_client` callable parameter,
  making the function testable without monkeypatching.
- NULL trigger returns True (backward compat with pre-trigger UoWs).
- Malformed/unknown triggers return True + write condition_eval_error audit entry.
- GitHub API failures return False + write condition_eval_failed audit entry.
- registry_state with non-existent UoW returns False + condition_eval_error audit.
- Normal False conditions (issue open, state not matched) write no audit entry.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Type alias for the GitHub API client callable.
# Callable[[int], dict] — takes an issue number, returns {"status_code": int, "state": str|None}
# ---------------------------------------------------------------------------

GithubClient = Callable[[int], dict[str, Any]]


# ---------------------------------------------------------------------------
# Production GitHub client (uses gh CLI — no network library needed)
# ---------------------------------------------------------------------------

def _default_github_client(issue_number: int) -> dict[str, Any]:
    """
    Query GitHub issue state via the gh CLI.

    Returns {"status_code": 200, "state": "open"|"closed"} on success.
    Returns {"status_code": <N>, "state": None} on non-200 or subprocess error.
    """
    try:
        result = subprocess.run(
            [
                "gh", "issue", "view", str(issue_number),
                "--repo", "dcetlin/Lobster",
                "--json", "state",
                "--jq", ".state",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            state = result.stdout.strip().lower()
            return {"status_code": 200, "state": state}
        # gh CLI exits non-zero for 404 and other API errors.
        # Parse the stderr to extract a status code if available.
        stderr = result.stderr.lower()
        if "not found" in stderr or "404" in stderr:
            return {"status_code": 404, "state": None}
        if "forbidden" in stderr or "403" in stderr:
            return {"status_code": 403, "state": None}
        return {"status_code": 500, "state": None}
    except subprocess.TimeoutExpired:
        return {"status_code": 408, "state": None}
    except Exception:
        return {"status_code": 500, "state": None}


# ---------------------------------------------------------------------------
# Trigger handlers — pure functions mapping (trigger_dict, uow, registry,
# github_client) → bool. Each handler is responsible only for its trigger type.
# ---------------------------------------------------------------------------

def _eval_immediate(_trigger: dict, _uow: dict, **_kwargs) -> bool:
    """Immediate trigger always fires."""
    return True


def _eval_issue_closed(
    trigger: dict,
    uow: dict,
    registry: Any | None,
    github_client: GithubClient,
) -> bool:
    """
    Returns True when the specified GitHub issue is in 'closed' state.
    Returns False on API failure (defer, not advance).
    Writes condition_eval_failed audit entry on non-200 response.
    """
    issue_number = trigger.get("number")
    response = github_client(issue_number)
    status_code = response.get("status_code", 500)

    if status_code == 200:
        return response.get("state", "").lower() == "closed"

    # Non-200: cannot confirm condition — defer (return False) and log.
    if registry is not None:
        registry.append_audit_log(uow["id"], {
            "event": "condition_eval_failed",
            "error_code": status_code,
            "trigger_type": "issue_closed",
            "uow_id": uow["id"],
        })
    return False


def _eval_registry_state(
    trigger: dict,
    uow: dict,
    registry: Any | None,
) -> bool:
    """
    Returns True when the specified UoW is in the specified state.
    Returns False if the UoW does not exist or registry is unreadable.
    Writes condition_eval_error audit entry for non-existent UoW ID.
    """
    target_uow_id = trigger.get("uow_id")
    target_state = trigger.get("state")

    if registry is None:
        # No registry provided — cannot evaluate; return False (safe default).
        return False

    try:
        target = registry.get(target_uow_id)
    except Exception:
        # Unreadable registry — return False without crashing.
        return False

    if target.get("error") == "not found":
        registry.append_audit_log(uow["id"], {
            "event": "condition_eval_error",
            "note": f"referenced uow_id not found: {target_uow_id}",
            "uow_id": uow["id"],
        })
        return False

    return target.get("status") == target_state


# ---------------------------------------------------------------------------
# Named constants for trigger types
# ---------------------------------------------------------------------------

_TRIGGER_IMMEDIATE = "immediate"
_TRIGGER_ISSUE_CLOSED = "issue_closed"
_TRIGGER_REGISTRY_STATE = "registry_state"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def evaluate_condition(
    uow: dict[str, Any],
    *,
    registry: Any | None = None,
    github_client: GithubClient | None = None,
) -> bool:
    """
    Returns True if the UoW's trigger condition is met.

    uow: a dict from registry._row_to_dict — reads uow['trigger'], uow['source_issue_number'],
         uow['id']
    registry: optional Registry instance for registry_state lookups and audit_log writes.
              In production the sweep loop passes the registry instance. In isolated unit
              tests it can be None (disables audit writes and registry_state lookups).
    github_client: optional callable(issue_number) → {"status_code": int, "state": str|None}.
                   Defaults to _default_github_client (uses gh CLI). Override in tests.

    Returns True:  condition met — sweep should advance UoW to ready-for-steward.
    Returns False: condition not yet met — remain pending, no state change.
    Does not raise: all error paths return True or False, with optional audit_log entries.

    Error return semantics:
    - Malformed/unknown trigger → True (fail-open: do not silently block a UoW forever)
    - GitHub API failure → False (fail-safe: do not advance without confirmation)
    - registry_state with missing UoW → False (cannot confirm; log error)
    """
    if github_client is None:
        github_client = _default_github_client

    uow_id = uow.get("id", "unknown")
    trigger = uow.get("trigger")

    # NULL trigger: backward compat with pre-trigger UoWs — always fire.
    if trigger is None:
        return True

    # If trigger is still a raw string (not yet deserialized), attempt parse.
    if isinstance(trigger, str):
        try:
            trigger = json.loads(trigger)
        except (json.JSONDecodeError, ValueError):
            if registry is not None:
                registry.append_audit_log(uow_id, {
                    "event": "condition_eval_error",
                    "note": "trigger not valid JSON",
                    "uow_id": uow_id,
                })
            return True

    # Unexpected type (not dict, not str, not None).
    if not isinstance(trigger, dict):
        if registry is not None:
            registry.append_audit_log(uow_id, {
                "event": "condition_eval_error",
                "note": f"trigger not valid JSON (unexpected type: {type(trigger).__name__})",
                "uow_id": uow_id,
            })
        return True

    trigger_type = trigger.get("type")

    if trigger_type == _TRIGGER_IMMEDIATE:
        return _eval_immediate(trigger, uow)

    if trigger_type == _TRIGGER_ISSUE_CLOSED:
        return _eval_issue_closed(trigger, uow, registry=registry, github_client=github_client)

    if trigger_type == _TRIGGER_REGISTRY_STATE:
        return _eval_registry_state(trigger, uow, registry=registry)

    # Unknown trigger type — fail-open with audit entry.
    if registry is not None:
        registry.append_audit_log(uow_id, {
            "event": "condition_eval_error",
            "note": f"unknown trigger type: {trigger_type}",
            "uow_id": uow_id,
        })
    return True
