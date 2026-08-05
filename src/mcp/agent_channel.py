"""
Agent channel (``source="local-claude"``) — server-side protocol mechanics.

This module owns the parts of the agent-channel protocol that are *actually
specific to this channel and nothing else*: envelope construction, the
single-shot reply-slot write (and its lost-race branch), the ack write
(``.ack.json``), and the ``agent_channel.*`` audit emits.

What deliberately stays OUT of this module, and lives in
``inbox_server.py``'s ``handle_send_reply`` / ``handle_claim_and_ack``
instead: the fail-closed source-mismatch check and the ``mark_processed``
tail. Both are genuinely shared infrastructure that every source (including
this one) goes through — pulling them in here would just relocate the
"is this local-claude?" branch into a different file rather than dissolve
it, and would make this module need to know about Telegram/Slack
mark_processed semantics it has no business knowing about.

The agent-channel protocol itself (wire format, envelope shape, source
value) is owned by ``src/protocol/agent_channel_schema.py`` — the single
source of truth this module imports ``SOURCE`` from rather than redefining.
A round trip looks like: ``scripts/lobster-chat.py`` writes a request
envelope to ``~/messages/inbox/<request_id>.json`` over SSH; the dispatcher
claims and acks it (``handle_claim_and_ack``, audited via
``emit_request_audit`` below); a subagent answers by calling ``send_reply``
with ``source="local-claude"``, which lands here in ``write_reply`` and
occupies the single-shot slot at
``~/messages/agent-replies/<request_id>.json``; ``lobster-chat.py`` polls
that path for the reply. Pure shape change — no behavior change; every
correctness property from the agent-channel-hardening branch (single-shot
atomic slot, fail-closed source check, source-aware stale timeout,
per-request identity, ack != answer, request_id sanitization, audit events)
is preserved exactly.

Design constraint: functions here take their dependencies (paths, the event
emitter) as explicit parameters rather than importing them from
``inbox_server``. ``inbox_server.py`` imports this module, so the reverse
import would be circular; explicit parameters also keep this module directly
unit-testable without booting the whole MCP server.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Self-contained sys.path bootstrap (same pattern as reliability.py) so this
# module is importable both as a sibling of inbox_server.py (`from
# agent_channel import ...`, via src/mcp/'s own sys.path entry) and as
# `src.mcp.agent_channel` (e.g. from tests), without depending on the
# importer having already set up the repo-root sys.path entry.
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from src.utils.fs import atomic_write_json, atomic_create_json  # noqa: E402

# The channel's source name is owned by src/protocol/agent_channel_schema.py
# (the canonical, stdlib-only schema module) — imported rather than
# redefined so this module can never drift from the value enforced at the
# validation boundary (reliability.py) or described to an external agent.
from src.protocol.agent_channel_schema import SOURCE  # noqa: E402

log = logging.getLogger(__name__)

# An event emitter with the same shape as inbox_server._emit_mcp_event:
# (event_type, payload, severity="info", chat_id=None) -> None. Passed in by
# the caller rather than imported directly, since the real implementation
# lives in inbox_server.py (which imports this module — importing back would
# be circular) and depends on module-level EventBus availability state.
EmitEvent = Callable[..., None]


@dataclass(frozen=True)
class ReplyOutcome:
    """Result of attempting to write a local-claude answer.

    reply_slot_created: True if this call won the single-shot reply slot
    (agent-channel protocol spec principle 1: first writer wins). False
    means another caller already answered this request_id; the existing
    slot was left untouched.
    """

    request_id: str
    reply_slot_created: bool
    reply_path: Path


def write_reply(
    *,
    agent_replies_dir: Path,
    request_id: str,
    text: str,
    in_reply_to: str | None,
) -> ReplyOutcome:
    """Write a local-claude answer to its single-shot reply slot.

    Single-shot reply slot (agent-channel protocol spec, principle 1): "Once
    written, that slot is final — nothing may overwrite it with a different
    answer." atomic_write_json's rename-based atomicity only makes ONE write
    atomic; it does not stop a second writer from clobbering the first (e.g.
    the original subagent for a request finishing just as
    _recover_stale_processing() reclaims and re-dispatches the same request
    past the local-claude stale timeout). atomic_create_json closes that
    gap: the first writer's content lands, every later writer for the same
    request_id is told it lost the race and leaves the existing slot
    untouched.
    """
    agent_reply = {
        "request_id": request_id,
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat(),
        "in_reply_to": in_reply_to or request_id,
    }
    reply_path = agent_replies_dir / f"{request_id}.json"
    reply_slot_created = atomic_create_json(reply_path, agent_reply)
    if not reply_slot_created:
        log.warning(
            "send_reply(local-claude): reply slot already occupied for "
            f"request_id={request_id!r} — refusing to overwrite (first "
            "writer wins; agent-channel protocol spec principle 1)."
        )
    return ReplyOutcome(
        request_id=request_id,
        reply_slot_created=reply_slot_created,
        reply_path=reply_path,
    )


def emit_reply_audit(
    *,
    request_id: str,
    reply_slot_created: bool,
    text_len: int,
    emit_event: EmitEvent,
) -> None:
    """Emit the agent_channel.reply audit event.

    Previously this whole source was excluded from telegram.outbound with no
    replacement — the reply half of the channel was invisible in
    events.jsonl. Distinct event_type from telegram.outbound because this is
    a machine-to-machine reply, not a chat delivery. Emitted for BOTH
    outcomes — won the single-shot reply slot, or lost the race — so a
    lost-race conflict is visible in the audit trail instead of silently
    vanishing.
    """
    emit_event(
        "agent_channel.reply",
        {
            "request_id": request_id,
            "reply_slot_created": reply_slot_created,
            "text_len": text_len,
        },
        severity="info" if reply_slot_created else "warn",
    )


def format_reply_response(
    *,
    request_id: str,
    text: str,
    reply_slot_created: bool,
    mark_info: str,
) -> str:
    """Build the handle_send_reply response text for a local-claude reply."""
    preview = f"{text[:100]}{'...' if len(text) > 100 else ''}"
    if reply_slot_created:
        return f"✅ Reply written to agent-replies/{request_id}.json{mark_info}:\n\n{preview}"
    return (
        f"⚠️ No-op: agent-replies/{request_id}.json already exists — another "
        "caller already wrote the reply for this request_id (single-shot "
        "reply slot, first writer wins). Nothing was overwritten; the "
        f"request is already answered{mark_info}."
    )


def emit_request_audit(
    *,
    request_id: str,
    message_id: str,
    emit_event: EmitEvent,
) -> None:
    """Emit the agent_channel.request audit event.

    claim_and_ack previously never emitted anything for local-claude —
    neither the claim nor the ack — so the request half of the round trip
    was invisible in events.jsonl. This is the inbound counterpart to
    agent_channel.reply / agent_channel.ack; distinct event_type from
    telegram.inbound because this is a machine-to-machine request, not a
    chat message.
    """
    emit_event(
        "agent_channel.request",
        {"request_id": request_id, "message_id": message_id},
    )


def write_ack(
    *,
    agent_replies_dir: Path,
    request_id: str,
    ack_text: str,
    message_id: str,
    emit_event: EmitEvent,
) -> str:
    """Write a local-claude ack and return the handle_claim_and_ack response text.

    Ack != answer (agent-channel protocol spec, principle 4): the ack is
    written to a distinct slot, never to agent-replies/<request_id>.json —
    that file is the single-shot answer slot a later
    send_reply(source='local-claude', request_id=..., ...) call must still
    write. This bypasses handle_send_reply entirely so there is no code path
    from this ack into the answer slot.
    """
    ack_payload = {
        "request_id": request_id,
        "ack": True,
        "text": ack_text,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        atomic_write_json(agent_replies_dir / f"{request_id}.ack.json", ack_payload)
        log.info(f"claim_and_ack: local-claude ack written for request {request_id}")
        emit_event(
            "agent_channel.ack",
            {"request_id": request_id, "message_id": message_id, "text_len": len(ack_text)},
        )
        return (
            f"Claimed and acked (local-claude): {message_id}\n\n"
            f"Ack written to agent-replies/{request_id}.ack.json "
            "(answer slot untouched — still call send_reply(source='local-claude', "
            f"request_id='{request_id}', ...) with the real answer).\n\n"
            f"Ack: {ack_text[:100]}"
        )
    except Exception as e:
        # Ack write failed — message stays in processing/, stale recovery handles it
        log.warning(f"claim_and_ack: local-claude ack write failed (message stays in processing/): {e}")
        emit_event(
            "agent_channel.ack",
            {"request_id": request_id, "message_id": message_id, "error": str(e)},
            severity="warn",
        )
        return (
            f"Warning: message claimed but local-claude ack write failed: {e}\n"
            f"Message {message_id} remains in processing/. Stale recovery will handle it."
        )
