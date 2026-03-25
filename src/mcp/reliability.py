"""
Reliability utilities for Lobster MCP server.

Addresses common agent failure patterns:
- Atomic file writes (prevents corruption on crash)
- Input validation (prevents silent bad data)
- Structured audit logging (observability)
- Idempotency helpers (prevents duplicate processing)
- Capability failure alerting (detects silent tool degradation)

Design principles:
- Each function is pure or has a single side effect
- No global mutable state
- Failures are explicit, never swallowed
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Re-export atomic filesystem helpers from the canonical location.
# ---------------------------------------------------------------------------
# The canonical implementations live in utils/fs.py.  We re-export them here
# so that existing callers of `from reliability import atomic_write_json`
# continue to work without change.

_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from utils.fs import atomic_write_json, safe_move  # noqa: E402, F401


# =============================================================================
# Input Validation
# =============================================================================
# Problem: Tool arguments from LLMs can be subtly wrong - wrong types,
# out-of-range values, missing fields. Without validation, these propagate
# as silent corruption (Composio issues #3, #7).
#
# Solution: Validate at the boundary. Return clear error messages.
# =============================================================================

class ValidationError(Exception):
    """Raised when input validation fails. Contains a user-facing message."""
    pass


def validate_send_reply_args(args: dict) -> dict:
    """Validate and normalize send_reply arguments.

    Returns normalized args dict.
    Raises ValidationError with descriptive message on invalid input.
    """
    chat_id = args.get("chat_id")
    text = args.get("text", "")
    source = args.get("source", "telegram").lower()

    # chat_id: required, must be int or non-empty string
    if chat_id is None:
        raise ValidationError("chat_id is required")
    if isinstance(chat_id, str) and not chat_id.strip():
        raise ValidationError("chat_id cannot be empty string")
    if isinstance(chat_id, float):
        chat_id = int(chat_id)  # LLMs sometimes send floats

    # text: required, non-empty, reasonable length
    if not text or not text.strip():
        raise ValidationError("text is required and cannot be empty")
    # Sanity cap only — do NOT truncate at Telegram's per-message limit here.
    # The bot's _prepare_send_items() pipeline handles splitting long messages
    # into multiple chunks before they reach the Telegram API. Truncating here
    # silently drops content for messages between 4096 and ~12000 chars.
    if len(text) > 100_000:
        text = text[:99_997] + "..."

    # source: must be a known source
    valid_sources = {"telegram", "slack", "sms", "signal", "whatsapp", "bisque"}
    if source not in valid_sources:
        raise ValidationError(
            f"Invalid source '{source}'. Must be one of: {', '.join(sorted(valid_sources))}"
        )

    return {
        **args,
        "chat_id": chat_id,
        "text": text,
        "source": source,
    }


def validate_message_id(message_id: Any) -> str:
    """Validate a message_id is a non-empty string.

    Raises ValidationError if invalid.
    """
    if not message_id:
        raise ValidationError("message_id is required")
    if not isinstance(message_id, str):
        message_id = str(message_id)
    if not message_id.strip():
        raise ValidationError("message_id cannot be empty")
    # Guard against path traversal
    if ".." in message_id or "/" in message_id:
        raise ValidationError("message_id contains invalid characters")
    return message_id


# =============================================================================
# Audit Logging
# =============================================================================
# Problem: No structured record of what the agent did. When things go wrong,
# there's no way to reconstruct what happened (Composio issues #6, #10).
#
# Solution: Append-only audit log with structured entries. Each entry records
# what tool was called, what arguments were passed, and the outcome.
# The log is separate from the application log to avoid noise.
# =============================================================================

_AUDIT_LOG_PATH = None  # Set during init


def init_audit_log(log_dir: Path) -> None:
    """Initialize the audit log file path."""
    global _AUDIT_LOG_PATH
    log_dir.mkdir(parents=True, exist_ok=True)
    _AUDIT_LOG_PATH = log_dir / "audit.jsonl"


def audit_log(
    tool: str,
    args: dict | None = None,
    result: str = "",
    error: str = "",
    duration_ms: int | None = None,
) -> None:
    """Append a structured audit entry (JSONL format).

    Each line is a self-contained JSON object, making the file:
    - Append-only (crash-safe, no corruption of existing entries)
    - Easy to grep/filter
    - Parseable line-by-line (no need to load entire file)

    Args:
        tool: Tool name that was called.
        args: Sanitized arguments (never log secrets).
        result: Brief result summary.
        error: Error message if the call failed.
        duration_ms: How long the call took.
    """
    if _AUDIT_LOG_PATH is None:
        return  # Not initialized yet

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
    }

    if args:
        # Sanitize: redact potential secrets, truncate large values
        sanitized = {}
        for k, v in args.items():
            if k in ("text", "output", "context", "body", "description"):
                s = str(v)
                sanitized[k] = s[:200] + "..." if len(s) > 200 else s
            elif k in ("token", "password", "secret", "api_key"):
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = v
        entry["args"] = sanitized

    if result:
        entry["result"] = result[:500]
    if error:
        entry["error"] = error[:500]
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms

    try:
        line = json.dumps(entry, default=str) + "\n"
        with open(_AUDIT_LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass  # Audit logging must never crash the main process


# =============================================================================
# Idempotency
# =============================================================================
# Problem: If the agent crashes after sending a reply but before marking
# the message as processed, it will re-process and send a duplicate reply
# on restart (Composio issue #9).
#
# Solution: Track recently processed message IDs in a set with TTL.
# Check before processing; skip if already seen.
# =============================================================================

class IdempotencyTracker:
    """Tracks recently processed items to prevent duplicate processing.

    Uses an in-memory set with TTL-based expiry. Not persistent across
    restarts (by design - the file-based state directories handle that).
    This catches duplicates within a single session.
    """

    def __init__(self, ttl_seconds: int = 600):
        self._seen: dict[str, float] = {}  # id -> timestamp
        self._ttl = ttl_seconds

    def check_and_mark(self, item_id: str) -> bool:
        """Check if item was recently processed. If not, mark it.

        Returns True if this is a NEW item (not seen before).
        Returns False if this is a DUPLICATE (already processed).
        """
        self._evict_expired()
        if item_id in self._seen:
            return False
        self._seen[item_id] = time.time()
        return True

    def _evict_expired(self) -> None:
        """Remove entries older than TTL."""
        now = time.time()
        cutoff = now - self._ttl
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}


# =============================================================================
# Circuit Breaker
# =============================================================================
# Problem: When an external service (Telegram API, GitHub, etc.) is down,
# the agent keeps retrying and wasting resources (Composio issue #5).
#
# Solution: Circuit breaker pattern from distributed systems.
# After N consecutive failures, stop trying for a cooldown period.
# =============================================================================

class CircuitBreaker:
    """Simple circuit breaker for external service calls.

    States:
    - CLOSED: Normal operation, calls pass through
    - OPEN: Too many failures, calls are rejected immediately
    - HALF_OPEN: After cooldown, allow one test call

    This is the standard circuit breaker pattern from Michael Nygard's
    "Release It!" - a well-established reliability pattern.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        cooldown_seconds: int = 60,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            # Check if cooldown has elapsed
            if time.time() - self._last_failure_time >= self.cooldown_seconds:
                self._state = self.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        s = self.state
        if s == self.CLOSED:
            return True
        if s == self.HALF_OPEN:
            return True  # Allow one test request
        return False  # OPEN - reject

    def record_success(self) -> None:
        """Record a successful call. Resets the breaker."""
        self._failure_count = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        """Record a failed call. May trip the breaker."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            self._state = self.OPEN

    def status(self) -> dict:
        """Return current status for observability."""
        return {
            "name": self.name,
            "state": self.state,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
        }


# =============================================================================
# Capability Failure Tracker
# =============================================================================
# Problem: When a core MCP tool (memory_store, send_reply, check_inbox) starts
# returning failure responses, the degradation goes undetected for hours because
# the dispatcher never surfaces it to the user (see 26-hour memory outage,
# 2026-03-23).
#
# Solution: Track consecutive failures per tool. When a monitored tool fails
# N consecutive times, emit a direct Telegram alert via the outbox (bypassing
# the dispatcher inbox so no tokens are burned and the alert is unconditional).
# Reset the counter on the first success. Re-alert on a cooldown interval to
# prevent spam while the tool remains degraded.
#
# Design:
# - Pure state transitions: record_success / record_failure return an
#   AlertDecision dataclass describing what action to take; the caller
#   performs the side effect (outbox write). This keeps the tracker testable
#   without mocking I/O.
# - No I/O inside the tracker — callers are responsible for alert delivery.
# =============================================================================

from dataclasses import dataclass


# Tools monitored by default — the critical path for Lobster's core operation.
DEFAULT_MONITORED_TOOLS: frozenset[str] = frozenset({
    "memory_store",
    "send_reply",
    "check_inbox",
    "wait_for_messages",
})

# Text fragments in tool responses that indicate a capability failure.
# These are matched as substrings (case-insensitive) of the first TextContent
# text returned by the tool handler.
FAILURE_RESPONSE_PATTERNS: tuple[str, ...] = (
    "memory system is not available",
    "error storing memory",
    "error in memory_store",
    "error in send_reply",
    "error in check_inbox",
    "error in wait_for_messages",
    "not available",
    "failed:",
)

# Patterns that look like failures but aren't — validation errors, empty inbox,
# etc. These short-circuit the failure-detection path.
NON_FAILURE_PATTERNS: tuple[str, ...] = (
    "validation error",          # tool input was bad, not the tool itself
    "no new messages",           # empty inbox is healthy
    "0 message",                 # empty inbox variants
    "0 new message",
)


def _response_is_failure(response_text: str) -> bool:
    """Return True if a tool response text indicates a capability failure.

    Pure function — no side effects, fully testable.

    Args:
        response_text: The text content returned by a tool handler.
    """
    lower = response_text.lower()

    # Non-failure patterns take precedence — check first.
    for non_failure in NON_FAILURE_PATTERNS:
        if non_failure in lower:
            return False

    for pattern in FAILURE_RESPONSE_PATTERNS:
        if pattern in lower:
            return True

    return False


@dataclass(frozen=True)
class AlertDecision:
    """Immutable result of a failure-tracking state transition.

    Returned by CapabilityFailureTracker.record_success/record_failure.
    The caller reads should_alert and, if True, delivers the alert message.
    """
    should_alert: bool
    alert_message: str
    consecutive_failures: int
    tool: str


class CapabilityFailureTracker:
    """Tracks consecutive failures per tool and signals when to alert.

    State transitions are pure — record_success/record_failure return an
    AlertDecision with the recommended action. I/O (outbox write) is the
    caller's responsibility, keeping this class fully unit-testable.

    Args:
        monitored_tools: Set of tool names to track.
        failure_threshold: Consecutive failures before first alert (default 3).
        alert_cooldown_seconds: Minimum seconds between repeat alerts for a
            tool that remains degraded (default 1800 = 30 min).
    """

    def __init__(
        self,
        monitored_tools: frozenset[str] = DEFAULT_MONITORED_TOOLS,
        failure_threshold: int = 3,
        alert_cooldown_seconds: int = 1800,
    ) -> None:
        self._monitored = monitored_tools
        self._threshold = failure_threshold
        self._cooldown = alert_cooldown_seconds
        # Per-tool state: consecutive failure count and last-alerted timestamp
        self._consecutive: dict[str, int] = {}
        self._last_alerted: dict[str, float] = {}

    def record_success(self, tool: str) -> AlertDecision:
        """Record a successful tool call, resetting its failure counter.

        Returns an AlertDecision with should_alert=False (success clears state).
        """
        if tool in self._monitored:
            self._consecutive[tool] = 0
        return AlertDecision(
            should_alert=False,
            alert_message="",
            consecutive_failures=0,
            tool=tool,
        )

    def record_failure(self, tool: str, response_text: str = "") -> AlertDecision:
        """Record a failed tool call and decide whether to alert.

        Returns an AlertDecision. The caller must deliver the alert if
        should_alert is True.

        Args:
            tool: The tool name that failed.
            response_text: The response text from the tool (used in alert body).
        """
        if tool not in self._monitored:
            return AlertDecision(
                should_alert=False,
                alert_message="",
                consecutive_failures=0,
                tool=tool,
            )

        count = self._consecutive.get(tool, 0) + 1
        self._consecutive[tool] = count

        if count < self._threshold:
            return AlertDecision(
                should_alert=False,
                alert_message="",
                consecutive_failures=count,
                tool=tool,
            )

        # At or above threshold — decide whether to alert based on cooldown.
        now = time.time()
        last = self._last_alerted.get(tool, 0.0)
        is_first_alert = count == self._threshold
        cooldown_elapsed = (now - last) >= self._cooldown

        if not (is_first_alert or cooldown_elapsed):
            return AlertDecision(
                should_alert=False,
                alert_message="",
                consecutive_failures=count,
                tool=tool,
            )

        self._last_alerted[tool] = now
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snippet = (response_text[:200] + "...") if len(response_text) > 200 else response_text
        message = (
            f"CAPABILITY ALERT [{ts}]\n"
            f"Tool '{tool}' has failed {count} consecutive time(s).\n"
            f"Last response: {snippet or '(no response text)'}\n"
            "This may indicate silent degradation. Check logs."
        )
        return AlertDecision(
            should_alert=True,
            alert_message=message,
            consecutive_failures=count,
            tool=tool,
        )

    def status(self) -> dict:
        """Return current per-tool failure counts for observability."""
        return {
            tool: {
                "consecutive_failures": self._consecutive.get(tool, 0),
                "last_alerted": self._last_alerted.get(tool, 0.0),
            }
            for tool in self._monitored
        }
