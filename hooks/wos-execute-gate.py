#!/usr/bin/env python3
"""PostToolUse hook: enforces the WOS Execute Gate structurally.

Fires after every `mcp__lobster-inbox__mark_processed` tool call. Checks
whether the processed message was a `wos_execute` type that bypassed
`mark_processing`. If so, logs a gate violation — the WOS Execute Gate was
missed.

## Gate logic

The WOS Execute Gate requires that `mark_processing` is called before
`mark_processed` for `wos_execute` messages. This is structurally verifiable
because `mark_processing` stamps a `_processing_started_at` field into the
message JSON at claim time. At PostToolUse time the message is already in
`processed/` — we read it there and check for that field.

Detection decision table:

  | _processing_started_at present? | type == wos_execute? | Gate result |
  |----------------------------------|----------------------|-------------|
  | yes                              | yes                  | PASS        |
  | yes                              | no                   | PASS        |
  | no                               | yes                  | MISS        |
  | no                               | no                   | PASS        |

## Failure policy

Gate misses are logged (never block — blocking could cause stuck messages).
A gate miss writes to the log file and writes a subagent_observation JSON file
directly to the inbox so the dispatcher is informed without requiring an MCP
session handshake.

On any error (malformed input, missing file, I/O failure) the hook appends a
timestamped line to hook-failures.log and exits 0 — gate enforcement never
disrupts normal operation.

## settings.json configuration

Add to ~/.claude/settings.json under "hooks" -> "PostToolUse":

    {
      "matcher": "mcp__lobster-inbox__mark_processed",
      "hooks": [
        {
          "type": "command",
          "command": "python3 /home/lobster/lobster/hooks/wos-execute-gate.py",
          "timeout": 10
        }
      ]
    }
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HOME = Path.home()
_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))

PROCESSED_DIR = _MESSAGES_DIR / "processed"
PROCESSING_DIR = _MESSAGES_DIR / "processing"
INBOX_DIR = _MESSAGES_DIR / "inbox"

LOG_FILE = _WORKSPACE / "logs" / "wos-execute-gate.log"
FAILURE_LOG = _WORKSPACE / "logs" / "hook-failures.log"

WOS_EXECUTE_TYPE = "wos_execute"
MARK_PROCESSED_TOOL = "mcp__lobster-inbox__mark_processed"

_ADMIN_CHAT_ID = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_gate_miss(message_id: str, msg_type: str, reason: str) -> None:
    """Append a gate-miss entry to wos-execute-gate.log. Silent on failure."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = json.dumps({
            "ts": ts,
            "event": "gate_miss",
            "gate": "wos_execute_gate",
            "message_id": message_id,
            "msg_type": msg_type,
            "reason": reason,
        })
        with LOG_FILE.open("a") as f:
            f.write(entry + "\n")
    except Exception:  # noqa: BLE001
        pass


def _log_failure(context: str, exc: Exception) -> None:
    """Append a timestamped hook-failure line to hook-failures.log. Silent."""
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with FAILURE_LOG.open("a") as f:
            f.write(f"[{ts}] wos-execute-gate: {context}: {exc}\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Message lookup
# ---------------------------------------------------------------------------


def _find_message_file(message_id: str) -> "Path | None":
    """Find the message JSON file by ID across processed/, processing/, inbox/.

    At PostToolUse time the file is normally already in processed/. The
    processing/ and inbox/ fallbacks cover edge cases (e.g. send_reply
    atomic path) where the hook fires before the rename completes.
    """
    for directory in (PROCESSED_DIR, PROCESSING_DIR, INBOX_DIR):
        candidate = directory / f"{message_id}.json"
        if candidate.exists():
            return candidate
    return None


def _read_message(message_id: str) -> "dict | None":
    """Return parsed message dict for message_id, or None if not found/unreadable."""
    path = _find_message_file(message_id)
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Gate violation reporter
# ---------------------------------------------------------------------------


def _build_observation_payload(message_id: str) -> dict:
    """Build a subagent_observation inbox payload for a gate miss (pure)."""
    now = datetime.now(timezone.utc)
    ts_ms = int(now.timestamp() * 1000)
    obs_id = f"{ts_ms}_observation_{uuid.uuid4().hex[:8]}"
    return {
        "id": obs_id,
        "type": "subagent_observation",
        "source": "hook",
        "chat_id": _ADMIN_CHAT_ID,
        "text": (
            f"gate=wos_execute_gate "
            f"condition=mark_processed_without_mark_processing "
            f"outcome=miss "
            f"message_id={message_id}"
        ),
        "category": "system_error",
        "timestamp": now.isoformat(),
    }


def _call_write_observation(message_id: str) -> None:
    """Write a gate-miss observation directly to the inbox as a JSON file.

    Avoids the MCP session handshake entirely — the dispatcher picks up the
    file on its next inbox poll.  On any I/O error, logs to hook-failures.log
    and returns — never raises.
    """
    try:
        payload = _build_observation_payload(message_id)
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        dest = INBOX_DIR / f"{payload['id']}.json"
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        tmp.replace(dest)
    except Exception as exc:  # noqa: BLE001
        _log_failure("write_observation inbox write failed", exc)


# ---------------------------------------------------------------------------
# Gate check (pure function — testable without I/O)
# ---------------------------------------------------------------------------


def check_gate(msg: dict) -> "tuple[bool, str]":
    """Return (gate_passed, reason) given a parsed message dict.

    Gate passes when:
    - Message type is not wos_execute, OR
    - Message type is wos_execute AND _processing_started_at is present
      (indicating mark_processing was called before mark_processed).

    Gate misses when:
    - Message type is wos_execute AND _processing_started_at is absent.
    """
    msg_type = msg.get("type", "")
    if msg_type != WOS_EXECUTE_TYPE:
        return True, f"type={msg_type!r} is not wos_execute — gate not applicable"

    if msg.get("_processing_started_at"):
        return True, "mark_processing was called (_processing_started_at present)"

    return False, "wos_execute message processed without prior mark_processing"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        _log_failure("failed to parse hook input JSON", exc)
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name != MARK_PROCESSED_TOOL:
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    message_id = tool_input.get("message_id", "").strip()
    if not message_id:
        sys.exit(0)

    msg = _read_message(message_id)
    if msg is None:
        # Message not found — cannot verify; allow and log.
        _log_failure(
            f"message_id={message_id!r} not found in processed/processing/inbox",
            Exception("file not found"),
        )
        sys.exit(0)

    gate_passed, reason = check_gate(msg)
    if gate_passed:
        sys.exit(0)

    # Gate miss — log and surface via write_observation, but do not block.
    msg_type = msg.get("type", "unknown")
    _log_gate_miss(message_id, msg_type, reason)
    _call_write_observation(message_id)

    # Exit 0 — never block mark_processed (stuck messages are worse than gate misses).
    sys.exit(0)


if __name__ == "__main__":
    main()
