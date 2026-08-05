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
mark_processed semantics it has no business knowing about. See
``~/dancetlin-infra/agent-chat/lobster-003.md`` for the agreed design and
``glyph-005.md`` / ``glyph-006.md`` for the structural review that prompted
this extraction. Pure shape change — no behavior change; every correctness
property from the agent-channel-hardening branch (single-shot atomic slot,
fail-closed source check, source-aware stale timeout, per-request identity,
ack != answer, request_id sanitization, audit events) is preserved exactly.

Design constraint: functions here take their dependencies (paths, the event
emitter) as explicit parameters rather than importing them from
``inbox_server``. ``inbox_server.py`` imports this module, so the reverse
import would be circular; explicit parameters also keep this module directly
unit-testable without booting the whole MCP server.
"""

from __future__ import annotations

import fcntl
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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


# =============================================================================
# write_progress (agent-channel protocol v1, §2) — repeatable, claim-bound
# status writes for an OPEN exchange.
# =============================================================================
#
# `.ack.json` was previously reachable only through claim_and_ack's one-time
# write_ack() call above. write_progress is a genuinely new callable surface
# (not a relaxed gate on write_ack — handle_claim_and_ack is single-shot for
# two compounding reasons: the SQLite claim succeeds once, and claiming also
# moves the file inbox/ -> processing/) that makes that same file
# (agent-replies/<request_id>.ack.json) repeatably overwritable while the
# exchange is OPEN: last-write-wins, current status only, never an appended
# log. See ~/lobster-workspace/assessments/agent-channel-protocol-proposal.md
# §2 for the full design and the adversarial review that shaped it.

# Debounce interval (Open Dial 5, resolved 2026-08-05): a write_progress call
# landing within this many seconds of the last *actual* filesystem write for
# the same request_id is still accepted (not an error) but the byte write
# itself is skipped. There is deliberately no separate debounce-timer field
# or store — "the last write" is read directly from .ack.json's own mtime,
# consistent with §3.3's "no new field, no new tool" framing (the future
# abandonment-sliding-deadline job, not built in this chunk, is meant to read
# that same timestamp).
DEBOUNCE_INTERVAL_SECONDS = 10.0

# Claim-row status values that mean "this exchange is still OPEN and may
# accept a write_progress call." Any other status (or no row at all) means
# the exchange was never claimed, or has already reached a terminal claim
# state — refuse.
_OPEN_CLAIM_STATUS = "processing"


@dataclass(frozen=True)
class ProgressOutcome:
    """Result of a write_progress call.

    accepted: False only when claim-bound authorization failed — the caller
        does not currently hold an open claim for request_id. True in every
        other case (written, debounced, or already_complete), because none
        of those are errors from the caller's point of view — see reason.
    written: True only if this call actually put new bytes on disk.
    reason: one of "written", "debounced", "already_complete", "unauthorized".
    """

    request_id: str
    accepted: bool
    written: bool
    reason: str


def write_progress(
    *,
    claims_db: Any,
    agent_replies_dir: Path,
    request_id: str,
    status_text: str,
    emit_event: EmitEvent,
) -> ProgressOutcome:
    """Write a repeatable current-status update for an OPEN local-claude exchange.

    Overwrites agent-replies/<request_id>.ack.json — the SAME file
    write_ack() above writes at claim time — with the latest status text.
    Last-write-wins: this is a status field, not an accumulating log (agent-
    channel protocol v1 §1, the "minimal mechanism" decision).

    Three guardrails, in order:

    1. Claim-bound authorization (§2.2): `claims_db` is queried for
       `request_id`'s *current* claim-row status, not compared against a
       caller-supplied session identity. session_id frequently collapses to
       the literal string "dispatcher" across distinct delegated subagents
       (inbox_server.py's `_get_current_http_session_id() or "dispatcher"`),
       so a naive `claimed_by == session_id` check would not tell one
       subagent's exchange apart from another's. Binding to "does an active
       claim row for this exact request_id currently exist and remain
       OPEN (status='processing')" is what request_id itself already
       provides — request_id is the bearer credential a caller must already
       know (per-request, never reused, never a shared client constant —
       protocol spec principle 3) — combined with the claims DB confirming
       the exchange hasn't since been reclaimed away, completed, or failed.
       message_id == request_id for this channel by convention (both are
       the inbound envelope's `id`, sanitize_request_id'd at the boundary
       before this function is ever called), so request_id is used directly
       as the claims_db lookup key.
    2. Message-after-complete guard (§2.5) and debounce (§6 dial 5) are both
       evaluated inside ONE flock-guarded critical section scoped to
       request_id — not two separate filesystem operations — so a late call
       can never land between "checked, terminal file absent" and "wrote."
    3. Within that same critical section: if agent-replies/<request_id>.json
       (the terminal reply) already exists, refuse (no-op, logged) — the
       exchange is COMPLETE and no further status write is meaningful.
       Otherwise, if the existing .ack.json was written less than
       DEBOUNCE_INTERVAL_SECONDS ago, skip the byte write (still accepted,
       not an error) to bound filesystem churn from a spammy caller.
       Otherwise, write.
    """
    reply_path = agent_replies_dir / f"{request_id}.json"
    ack_path = agent_replies_dir / f"{request_id}.ack.json"
    lock_path = agent_replies_dir / f".{request_id}.progress.lock"

    # --- 1. Claim-bound authorization ---
    claim_status = claims_db.get_claim_status(request_id)
    if claim_status != _OPEN_CLAIM_STATUS:
        log.warning(
            f"write_progress: unauthorized — no OPEN claim for request_id={request_id!r} "
            f"(claim_status={claim_status!r}); refusing to write (claim-bound authorization, "
            "not session_id — agent-channel protocol v1 §2.2)."
        )
        emit_event(
            "agent_channel.progress",
            {"request_id": request_id, "outcome": "unauthorized", "claim_status": claim_status},
            severity="warn",
        )
        return ProgressOutcome(request_id=request_id, accepted=False, written=False, reason="unauthorized")

    # --- 2 & 3. One lock-guarded critical section: terminal check, debounce, write ---
    agent_replies_dir.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as lockfile:
        fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
        try:
            if reply_path.exists():
                log.info(
                    f"write_progress: no-op — exchange already COMPLETE for "
                    f"request_id={request_id!r} (message-after-complete guard, §2.5)."
                )
                emit_event(
                    "agent_channel.progress",
                    {"request_id": request_id, "outcome": "already_complete"},
                )
                return ProgressOutcome(
                    request_id=request_id, accepted=True, written=False, reason="already_complete"
                )

            if ack_path.exists():
                try:
                    last_write_ts = ack_path.stat().st_mtime
                except OSError:
                    last_write_ts = 0.0
                if (time.time() - last_write_ts) < DEBOUNCE_INTERVAL_SECONDS:
                    emit_event(
                        "agent_channel.progress",
                        {"request_id": request_id, "outcome": "debounced"},
                    )
                    return ProgressOutcome(
                        request_id=request_id, accepted=True, written=False, reason="debounced"
                    )

            ack_payload = {
                "request_id": request_id,
                "ack": True,
                "text": status_text,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(ack_path, ack_payload)
        finally:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)

    log.info(f"write_progress: status written for request_id={request_id!r}")
    emit_event(
        "agent_channel.progress",
        {"request_id": request_id, "outcome": "written", "text_len": len(status_text)},
    )
    return ProgressOutcome(request_id=request_id, accepted=True, written=True, reason="written")


# =============================================================================
# by-agent pointer mailbox (agent-channel protocol v1, §3.2) — durability
# layer. Purely additive, read-only discovery index: a zero-byte pointer
# file per (agent, request_id) pair, written at the same moment the
# dispatcher writes into agent-replies/<request_id>.* for a request whose
# inbound envelope carried a non-empty `agent` field. Not an access-control
# mechanism — see AGENT_SLUG_RULES in src/protocol/agent_channel_schema.py.
# =============================================================================


def write_pointer(
    *,
    agent_replies_dir: Path,
    agent_slug: str,
    request_id: str,
) -> Path:
    """Write a zero-byte pointer file at by-agent/<agent_slug>/<request_id>.

    `agent_slug` must already be sanitized (reliability.sanitize_agent_slug)
    before calling this — this function does not validate it, matching
    write_reply/write_ack's assumption that request_id arrives pre-sanitized.

    Idempotent and safe to call repeatedly for the same pair: a plain
    `touch()` (not atomic_create_json — there is no payload, so there is
    nothing to race over; two callers touching the same path just both
    succeed at leaving the same empty file in place).
    """
    pointer_dir = agent_replies_dir / "by-agent" / agent_slug
    pointer_dir.mkdir(parents=True, exist_ok=True)
    pointer_path = pointer_dir / request_id
    pointer_path.touch(exist_ok=True)
    return pointer_path
