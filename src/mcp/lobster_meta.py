"""
_lobster_meta envelope classifier for inbox messages (issue #1023).

Populates a ``_lobster_meta`` dict on each incoming message at mark_processing()
time. Fields are hints only — the dispatcher must never trust them blindly (a
user message could trigger false matches). All classification is synchronous,
pure Python, and <5ms — no LLM calls on this path.

This module depends only on message_types.py (also dependency-free) and can be
imported and tested without pulling in the full inbox_server stack.

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

from src.mcp.message_types import INBOX_SYSTEM_TYPES as _SYSTEM_TYPES
from src.mcp.message_types import INBOX_USER_TYPES as _USER_TYPES
from src.mcp.message_types import MessageRelationship
from src.protocol.agent_channel_schema import SOURCE as _AGENT_CHANNEL_SOURCE

# Cross-Lobster bot-to-bot source (issue #1350). Structurally the same
# relationship as the local-claude agent channel below (dispatcher talking to
# another agent, not a human) but the peer is a remote Lobster's dispatcher
# over the bot-talk HTTP channel rather than a local Claude Code session.
# message_types.py owns INBOX_MESSAGE_SOURCES but doesn't export this as a
# named constant (unlike _AGENT_CHANNEL_SOURCE), so it's defined here.
_BOT_TALK_SOURCE: str = "bot-talk"


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

# bot-talk and gmail are system-originated; not user-facing even though they carry user content
_USER_FACING_SOURCES: frozenset[str] = frozenset({
    "telegram",
    "slack",
    "sms",
    "signal",
    "whatsapp",
    "bisque",
})


# ---------------------------------------------------------------------------
# Relationship classification (issue #1536, phase 1)
# ---------------------------------------------------------------------------

# Types produced by the write_result path (handle_write_result in
# inbox_server.py) that represent a subagent reporting back to the
# dispatcher. Checked before source below because external producers such as
# src/utils/inbox_write.py's write_inbox_message() write these types with
# source="telegram" for delivery purposes even though the sender is not a
# user — type is the unambiguous signal here, not source.
_SUBAGENT_RESULT_TYPES: frozenset[str] = frozenset({
    "subagent_result",
    "subagent_error",
    "subagent_notification",  # DEPRECATED alias for subagent_ack — write_result writes this literal string
    "subagent_ack",
})


def classify_relationship(msg: dict) -> str | None:
    """Return the relationship hint for a message, or None when ambiguous.

    Claim-time fallback classifier for external producers (issue #1536,
    phase 1): extends this module's existing precomputed-hints pattern
    (same pure/no-I/O, no-LLM shape as build_lobster_meta) with a
    `relationship` hint. Stamped by the caller as its own flat top-level
    field on the message (not nested under `_lobster_meta`) — see the
    phase-1 design note on message_types.MessageRelationship. Callers must
    only apply this fallback when `relationship` is not already present on
    the message (e.g. handle_write_result's ingestion-time stamp takes
    precedence over this fallback).

    Priority order (first match wins):
      1. type is a write_result-path subagent type (_SUBAGENT_RESULT_TYPES)
         -> MessageRelationship.SUBAGENT.
      2. source is a peer-agent channel — either the local-claude agent
         channel (a local Claude Code session over SSH) or bot-talk (another
         Lobster's dispatcher talking to this one over HTTP) — both are
         structurally the same relationship, just local vs. remote
         -> MessageRelationship.PEER_AGENT.
      3. source is a real user-facing channel AND type is a user-content type
         -> MessageRelationship.USER.
      4. Otherwise -> None (ambiguous cases are left unstamped, not guessed).

    Phase 1 is stamp-only: no reader branches on this field yet.
    """
    msg_type: str = (msg.get("type") or "").strip()
    source: str = (msg.get("source") or "").strip()

    if msg_type in _SUBAGENT_RESULT_TYPES:
        return MessageRelationship.SUBAGENT.value
    if source in (_AGENT_CHANNEL_SOURCE, _BOT_TALK_SOURCE):
        return MessageRelationship.PEER_AGENT.value
    if source in _USER_FACING_SOURCES and msg_type in _USER_TYPES:
        return MessageRelationship.USER.value
    return None


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
