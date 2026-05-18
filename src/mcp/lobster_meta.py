"""
_lobster_meta envelope classifier for inbox messages (issue #1023).

Populates a ``_lobster_meta`` dict on each incoming message at mark_processing()
time. Fields are hints only — the dispatcher must never trust them blindly (a
user message could trigger false matches). All classification is synchronous,
pure Python, and <5ms — no LLM calls on this path.

This module is intentionally dependency-free (like message_types.py) so it can
be imported and tested without pulling in the full inbox_server stack.

Fields populated:
  intent_class: "operational" | "emotional" | "code" | "question" | "reaction"
               | "system"
  urgency: "high" | "normal" | "low"
  is_user_facing: bool
  preprocessed_at: ISO 8601 UTC timestamp string

Classification scope (issue spec):
  - Start with is_user_facing and intent_class (required).
  - urgency implemented here; add more fields in follow-ups.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Intent class — keyword patterns (checked in order; first match wins)
# ---------------------------------------------------------------------------

# Each entry is (intent_class, compiled_pattern).
# Patterns are case-insensitive and match anywhere in the text.
_INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "code",
        re.compile(
            r"\b(bug|fix|error|traceback|exception|crash|pr|pull request|"
            r"commit|deploy|branch|test|lint|import|module|function|class|"
            r"variable|type error|attribute error|syntax|diff|patch|merge|"
            r"rebase|refactor|implement|feature|issue\s*#\d+)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "question",
        re.compile(
            r"(\?$|\?\s*$|^(what|who|where|when|why|how|which|can you|"
            r"could you|do you|did|does|is there|are there|tell me|"
            r"explain|show me))",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "emotional",
        re.compile(
            r"\b(feel|feeling|anxious|anxiety|scared|afraid|overwhelmed|"
            r"stressed|stress|worry|worried|sad|depressed|depression|"
            r"excited|frustrated|angry|upset|happy|grateful|thankful|"
            r"struggling|hard time|difficult|exhausted|tired|burnout|"
            r"lonely|alone|miss|love|hate|fear|hope|proud|shame|guilt|"
            r"nervous|panic|doubt|insecure|vulnerable)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "operational",
        re.compile(
            r"\b(schedule|remind|task|todo|calendar|meeting|appointment|"
            r"deadline|status|update|check|run|start|stop|restart|enable|"
            r"disable|config|setting|turn on|turn off|list|show|wos|"
            r"lobster|subagent|job|cron|deploy|upgrade|migrate|backup|"
            r"notification|alert|report|digest|sync)\b",
            re.IGNORECASE,
        ),
    ),
]

# Reaction-type: Telegram emoji reactions are classified separately
_REACTION_TYPE = "reaction"

# ---------------------------------------------------------------------------
# Urgency — keyword patterns
# ---------------------------------------------------------------------------

_URGENCY_HIGH_PATTERN: re.Pattern[str] = re.compile(
    r"\b(urgent|asap|as soon as possible|immediately|right now|broken|"
    r"down|outage|critical|emergency|help|fix now|need now|blocked|"
    r"p0|p1|hotfix|production|prod is down|failing)\b",
    re.IGNORECASE,
)

_URGENCY_LOW_PATTERN: re.Pattern[str] = re.compile(
    r"\b(whenever|no rush|low priority|eventually|when you get a chance|"
    r"someday|not urgent|backlog|nice to have|fyi|heads up|just letting "
    r"you know|when possible)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Sources that produce user-facing messages
# ---------------------------------------------------------------------------

_USER_FACING_SOURCES: frozenset[str] = frozenset({
    "telegram",
    "slack",
    "sms",
    "signal",
    "whatsapp",
    "bisque",
})

# System message types that are never user-facing regardless of source
_SYSTEM_TYPES: frozenset[str] = frozenset({
    "self_check",
    "subagent_result",
    "subagent_error",
    "subagent_ack",
    "subagent_notification",
    "subagent_observation",
    "subagent_stale_check",
    "subagent_recovered",
    "agent_failed",
    "compact_group",
    "compact_reminder",
    "cron_reminder",
    "scheduled_reminder",
    "update_notification",
    "consolidation",
    "observation",
    "health_check",
    "system",
    "task-output",
    "debug_observation",
    "session_note_reminder",
    "wos_execute",
})


def _classify_intent(text: str, msg_type: str) -> str:
    """Return the intent_class for a message.

    For system types, returns "system" immediately.
    For reaction types, returns "reaction".
    Otherwise, runs keyword patterns in order (first match wins).
    Falls back to "operational" when no pattern matches.
    """
    if msg_type in _SYSTEM_TYPES:
        return "system"
    if msg_type == "reaction":
        return "reaction"

    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(text):
            return intent

    return "operational"


def _classify_urgency(text: str, msg_type: str) -> str:
    """Return "high", "normal", or "low" based on keyword signals.

    System messages and reactions are always "normal".
    """
    if msg_type in _SYSTEM_TYPES or msg_type == "reaction":
        return "normal"
    if _URGENCY_HIGH_PATTERN.search(text):
        return "high"
    if _URGENCY_LOW_PATTERN.search(text):
        return "low"
    return "normal"


def _is_user_facing(source: str, chat_id: int | None, msg_type: str) -> bool:
    """Return True when the message is from a real user channel.

    Conditions for user-facing:
    - source is in _USER_FACING_SOURCES
    - chat_id is non-zero (0 = system/internal)
    - msg_type is not in _SYSTEM_TYPES
    """
    if msg_type in _SYSTEM_TYPES:
        return False
    if source == "system":
        return False
    if chat_id is not None and chat_id == 0:
        return False
    return source in _USER_FACING_SOURCES


def build_lobster_meta(msg: dict) -> dict:
    """Classify a message and return a ``_lobster_meta`` dict.

    This is the single entry point. Call it at mark_processing() time and
    attach the result to the message before writing it to the processing dir.

    Arguments:
        msg: The raw message dict. Reads: text, type, source, chat_id.

    Returns a dict with keys:
        intent_class, urgency, is_user_facing, preprocessed_at.

    This function is pure: no side effects, no I/O.
    """
    text: str = (msg.get("text") or msg.get("transcription") or "").strip()
    msg_type: str = (msg.get("type") or "").strip()
    source: str = (msg.get("source") or "").strip()
    chat_id: int | None = msg.get("chat_id")

    return {
        "intent_class": _classify_intent(text, msg_type),
        "urgency": _classify_urgency(text, msg_type),
        "is_user_facing": _is_user_facing(source, chat_id, msg_type),
        "preprocessed_at": datetime.now(timezone.utc).isoformat(),
    }
