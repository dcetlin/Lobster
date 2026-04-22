"""
vision_inlet.py

Vision Object inlet mechanism: validates vision_object_proposals from action_seeds,
dispatches binary Telegram confirms for eligible field changes, and handles
accept/decline callbacks to write vision.yaml.

Schema for vision_object_proposals in action_seeds YAML:

    vision_object_proposals:
      - field_path: "current_focus.this_week.primary"
        proposed_value: "string value"
        basis: "One sentence explaining why this change is warranted."
        source_session: "2026-04-22-1400"  # ISO datetime slug

Eligible field paths (all others are rejected):
  - current_focus.*                  (any depth under current_focus)
  - core.inviolable_constraints      (list append only — never replace)
  - active_project.phase_intent      (scalar replace)

Storage:
  - Pending proposals: ~/lobster-workspace/data/vision-proposals-pending.json
  - Accepted: ~/lobster-workspace/data/vision-proposals-accepted.jsonl
  - Discarded: ~/lobster-workspace/data/vision-proposals-discard.jsonl
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LOBSTER_WORKSPACE = Path.home() / "lobster-workspace"
LOBSTER_USER_CONFIG = Path.home() / "lobster-user-config"

VISION_YAML_PATH = LOBSTER_USER_CONFIG / "vision.yaml"
PENDING_JSON = LOBSTER_WORKSPACE / "data" / "vision-proposals-pending.json"
ACCEPTED_JSONL = LOBSTER_WORKSPACE / "data" / "vision-proposals-accepted.jsonl"
DISCARD_JSONL = LOBSTER_WORKSPACE / "data" / "vision-proposals-discard.jsonl"

# ---------------------------------------------------------------------------
# Eligible field path rules
# ---------------------------------------------------------------------------

# Fields under current_focus.* match via prefix
_CURRENT_FOCUS_PREFIX = "current_focus."

# These fields are eligible as exact matches (non-prefix)
_ELIGIBLE_EXACT = frozenset(
    {
        "core.inviolable_constraints",
        "active_project.phase_intent",
    }
)


def is_eligible_field_path(field_path: str) -> bool:
    """Return True if field_path is in the eligible set."""
    if field_path.startswith(_CURRENT_FOCUS_PREFIX):
        return True
    return field_path in _ELIGIBLE_EXACT


# ---------------------------------------------------------------------------
# Proposal hash
# ---------------------------------------------------------------------------


def proposal_hash(field_path: str, proposed_value: str) -> str:
    """Return an 8-char SHA256 hex digest of field_path + proposed_value."""
    raw = (field_path + proposed_value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


# ---------------------------------------------------------------------------
# YAML navigation helpers
# ---------------------------------------------------------------------------


def _navigate_yaml(data: Any, path_parts: list[str]) -> tuple[Any, bool]:
    """
    Navigate a nested YAML structure using a list of key parts.

    Returns (value, found) where found is True if every key in path_parts exists.
    """
    current = data
    for part in path_parts:
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True


def _set_yaml_value(data: Any, path_parts: list[str], value: Any) -> None:
    """
    Set a value at a dot-notation path in a nested dict structure.

    Raises KeyError if the path does not exist (must exist before writing).
    Raises ValueError if trying to set a non-list field on core.inviolable_constraints.
    """
    current = data
    for part in path_parts[:-1]:
        current = current[part]
    current[path_parts[-1]] = value


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_vision_proposals(
    proposals: list[dict],
    vision_yaml_path: Path = VISION_YAML_PATH,
) -> tuple[list[dict], list[dict]]:
    """
    Validate a list of vision_object_proposal dicts against vision.yaml.

    For each proposal:
      1. Checks field_path is in the eligible set — rejects with reason if not.
      2. Navigates the YAML to confirm the field exists — rejects if path is invalid.
         Exception: core.inviolable_constraints may be a list — existence check passes
         if the key resolves to a list.
      3. Compares proposed_value to the current field value (string equality) —
         rejects as "no-op" if identical.

    Returns (valid_proposals, rejected_proposals).
    Rejected proposals are also appended to DISCARD_JSONL immediately.
    """
    # Load vision.yaml
    try:
        vision_data = yaml.safe_load(vision_yaml_path.read_text())
    except (OSError, yaml.YAMLError) as e:
        # Cannot validate — reject all with reason
        rejected = []
        for p in proposals:
            _record_discard(dict(p), rejection_reason=f"vision.yaml load error: {e}")
            rejected.append({**p, "rejection_reason": f"vision.yaml load error: {e}"})
        return [], rejected

    valid: list[dict] = []
    rejected: list[dict] = []

    for p in proposals:
        field_path = (p.get("field_path") or "").strip()
        proposed_value = str(p.get("proposed_value", "")).strip()

        # 1. Eligible field check
        if not is_eligible_field_path(field_path):
            reason = (
                f"field_path '{field_path}' is not in the eligible set "
                f"(current_focus.*, core.inviolable_constraints, active_project.phase_intent)"
            )
            _record_discard(p, rejection_reason=reason)
            rejected.append({**p, "rejection_reason": reason})
            continue

        # 2. Field exists check
        path_parts = field_path.split(".")
        current_value, found = _navigate_yaml(vision_data, path_parts)
        if not found:
            reason = f"field_path '{field_path}' does not exist in vision.yaml"
            _record_discard(p, rejection_reason=reason)
            rejected.append({**p, "rejection_reason": reason})
            continue

        # 3. No-op check — skip for list fields (core.inviolable_constraints)
        if field_path != "core.inviolable_constraints":
            if str(current_value).strip() == proposed_value:
                reason = (
                    f"no-op: proposed_value is identical to current value for '{field_path}'"
                )
                _record_discard(p, rejection_reason=reason)
                rejected.append({**p, "rejection_reason": reason})
                continue

        valid.append(p)

    return valid, rejected


# ---------------------------------------------------------------------------
# Pending proposal storage
# ---------------------------------------------------------------------------


def _load_pending() -> dict:
    """Load pending proposals dict from disk; return empty dict if missing."""
    PENDING_JSON.parent.mkdir(parents=True, exist_ok=True)
    if not PENDING_JSON.exists():
        return {}
    try:
        return json.loads(PENDING_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_pending(pending: dict) -> None:
    """Atomically write pending proposals dict to disk."""
    PENDING_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pending, indent=2))
    tmp.rename(PENDING_JSON)


def _record_discard(proposal: dict, rejection_reason: str) -> None:
    """Append a rejected proposal to the discard JSONL log."""
    DISCARD_JSONL.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "field_path": proposal.get("field_path", ""),
        "proposed_value": str(proposal.get("proposed_value", "")),
        "basis": proposal.get("basis", ""),
        "rejection_reason": rejection_reason,
    }
    with DISCARD_JSONL.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _record_accepted(proposal: dict, hash_: str) -> None:
    """Append an accepted proposal to the accepted JSONL log."""
    ACCEPTED_JSONL.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hash": hash_,
        **proposal,
    }
    with ACCEPTED_JSONL.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Telegram dispatch helpers
# ---------------------------------------------------------------------------


def _call_mcp_send_reply(chat_id: int, text: str, buttons: list) -> bool:
    """
    Send a Telegram message with inline keyboard via the lobster-mcp CLI.
    Falls back gracefully if lobster-mcp is unavailable.
    Returns True on success.
    """
    params = {
        "chat_id": chat_id,
        "text": text,
        "buttons": buttons,
        "source": "telegram",
    }
    try:
        result = subprocess.run(
            ["lobster-mcp", "send_reply", json.dumps(params)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        print(f"  send_reply failed: {result.stderr.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"  lobster-mcp unavailable: {e}")
    return False


def dispatch_vision_proposals(
    valid_proposals: list[dict],
    chat_id: int = 6036,
    vision_yaml_path: Path = VISION_YAML_PATH,
) -> list[str]:
    """
    For each valid proposal, send a binary Telegram confirm message and store
    in the pending proposals file.

    Returns list of hashes dispatched.
    """
    pending = _load_pending()
    dispatched_hashes = []

    for proposal in valid_proposals:
        field_path = proposal["field_path"]
        proposed_value = str(proposal.get("proposed_value", ""))
        basis = proposal.get("basis", "")
        source_session = proposal.get("source_session", "")

        phash = proposal_hash(field_path, proposed_value)

        # Skip if already pending (idempotent)
        if phash in pending:
            continue

        # Store in pending before sending (so callback handler can find it)
        pending_entry = {
            **proposal,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        pending[phash] = pending_entry
        _save_pending(pending)

        text = (
            f"Proposed: update {field_path} to \"{proposed_value}\"\n\n"
            f"Basis: {basis}\n\n"
            f"Source: {source_session}"
        )

        buttons = [
            [
                {"text": "Accept", "callback_data": f"vision_accept:{field_path}:{phash}"},
                {"text": "Decline", "callback_data": f"vision_decline:{field_path}:{phash}"},
            ]
        ]

        sent = _call_mcp_send_reply(chat_id, text, buttons)
        if sent:
            dispatched_hashes.append(phash)
            print(f"  Dispatched vision proposal for {field_path} (hash={phash})")
        else:
            print(f"  Failed to dispatch proposal for {field_path} — remains in pending")

    return dispatched_hashes


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------


def handle_vision_accept(
    field_path: str,
    phash: str,
    chat_id: int = 6036,
    vision_yaml_path: Path = VISION_YAML_PATH,
) -> str:
    """
    Handle vision_accept:<field_path>:<hash> callback.

    1. Looks up proposal in pending.
    2. Loads vision.yaml.
    3. Navigates to field_path and writes the value.
       - For core.inviolable_constraints: appends to list.
       - All other eligible fields: replace scalar value.
    4. Writes vision.yaml back.
    5. Removes from pending, appends to accepted log.
    6. Returns a reply string.
    """
    pending = _load_pending()
    proposal = pending.get(phash)
    if proposal is None:
        return f"Proposal {phash} not found in pending — may have already been processed."

    proposed_value = str(proposal.get("proposed_value", ""))

    # Load vision.yaml
    try:
        vision_text = vision_yaml_path.read_text()
        vision_data = yaml.safe_load(vision_text)
    except (OSError, yaml.YAMLError) as e:
        return f"Error loading vision.yaml: {e}"

    path_parts = field_path.split(".")
    current_value, found = _navigate_yaml(vision_data, path_parts)
    if not found:
        return f"Field path '{field_path}' no longer exists in vision.yaml — cannot write."

    # Apply the change
    if field_path == "core.inviolable_constraints":
        # Append to the list
        if not isinstance(current_value, list):
            return f"core.inviolable_constraints is not a list — cannot append."
        # Generate a new constraint id
        existing_ids = [
            int(re.sub(r"\D", "", c.get("id", "0")))
            for c in current_value
            if isinstance(c, dict)
        ]
        next_id = max(existing_ids, default=0) + 1
        new_constraint = {
            "id": f"constraint-{next_id}",
            "statement": proposed_value,
            "rationale": proposal.get("basis", ""),
        }
        current_value.append(new_constraint)
        _set_yaml_value(vision_data, path_parts, current_value)
    else:
        _set_yaml_value(vision_data, path_parts, proposed_value)

    # Write vision.yaml back using PyYAML with block style
    try:
        vision_yaml_path.write_text(
            yaml.dump(vision_data, default_flow_style=False, allow_unicode=True)
        )
    except OSError as e:
        return f"Error writing vision.yaml: {e}"

    # Remove from pending
    del pending[phash]
    _save_pending(pending)

    # Append to accepted log
    _record_accepted(proposal, phash)

    return f"vision.yaml updated: {field_path} \u2192 {proposed_value}"


def handle_vision_decline(
    field_path: str,
    phash: str,
    chat_id: int = 6036,
) -> str:
    """
    Handle vision_decline:<field_path>:<hash> callback.

    1. Looks up proposal in pending.
    2. Removes from pending.
    3. Appends to discard log with rejection_reason=user_declined.
    4. Returns a reply string.
    """
    pending = _load_pending()
    proposal = pending.get(phash)
    if proposal is None:
        return f"Proposal {phash} not found in pending — may have already been processed."

    # Remove from pending
    del pending[phash]
    _save_pending(pending)

    # Record to discard log
    _record_discard(proposal, rejection_reason="user_declined")

    return "Declined. Proposal discarded."


def handle_vision_callback(callback_data: str, chat_id: int = 6036) -> str | None:
    """
    Top-level dispatcher for vision_accept: and vision_decline: callbacks.

    Parses callback_data and routes to the appropriate handler.
    Returns a reply string if this is a vision callback, or None if not.

    This function should be called from the dispatcher's callback handling code
    when processing inbox messages with type="callback".

    Usage in dispatcher:
        from src.harvest.vision_inlet import handle_vision_callback
        if msg.get("type") == "callback":
            reply = handle_vision_callback(msg.get("callback_data", ""), chat_id)
            if reply is not None:
                # send reply and mark processed
    """
    if callback_data.startswith("vision_accept:"):
        # vision_accept:<field_path>:<hash>
        # field_path may contain dots; hash is always last 8-char segment
        parts = callback_data[len("vision_accept:"):].rsplit(":", 1)
        if len(parts) != 2:
            return "Malformed vision_accept callback_data."
        field_path, phash = parts
        return handle_vision_accept(field_path, phash, chat_id=chat_id)

    if callback_data.startswith("vision_decline:"):
        # vision_decline:<field_path>:<hash>
        parts = callback_data[len("vision_decline:"):].rsplit(":", 1)
        if len(parts) != 2:
            return "Malformed vision_decline callback_data."
        field_path, phash = parts
        return handle_vision_decline(field_path, phash, chat_id=chat_id)

    return None
