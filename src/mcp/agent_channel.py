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
    error: bool | None = None,
    error_type: str | None = None,
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

    Written compact (``indent=None``, single line), not pretty-printed:
    this file is a machine-to-machine API contract polled by an external
    reader (``lobster-chat.py`` or an agent's own client), never edited by
    a human. json.dumps's default `indent=2` is otherwise safe (its
    escaping is spec-correct regardless of indent), but a pretty-printed,
    multi-line payload silently breaks any reader that isn't fully
    JSON-aware — e.g. one that expects one JSON object per line, or reads
    only the first line of `cat`'s output. Single-line output removes that
    whole class of external-parser failure at zero cost to us.

    ``error``/``error_type`` (issue #1533): optional discriminator keys on
    this SAME envelope — not a second file, not a separate error channel.
    "Absent" is the only spelling of success: ``error`` is added to the
    payload only when truthy (``error=False`` is normalized to "not
    present," exactly like ``error=None``, so a caller can never accidentally
    write the disallowed ``"error": false`` into an otherwise-successful
    reply). ``error_type`` is independent of ``error`` — it is written
    whenever the caller supplies a non-``None`` value, whether or not
    ``error`` is also set, since it is documented as a hint rather than a
    field whose presence is gated on another field's value.
    """
    agent_reply: dict[str, Any] = {
        "request_id": request_id,
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat(),
        "in_reply_to": in_reply_to or request_id,
    }
    if error:
        agent_reply["error"] = True
    if error_type is not None:
        agent_reply["error_type"] = error_type
    reply_path = agent_replies_dir / f"{request_id}.json"
    reply_slot_created = atomic_create_json(reply_path, agent_reply, indent=None)
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


# =============================================================================
# Capability advertisement (agent-channel protocol v1.1) — see write_ack().
# =============================================================================
#
# What this server BUILD actually supports, written into the ack payload at
# claim time so a caller can do version negotiation without a protocol
# version bump: "what can this exchange do" is answered by reading the ack,
# not by assuming based on when the client's code was written or which
# protocol version string it was told about.
#
# A function, not a bare tuple inlined at the write_ack() call site: none of
# these three features happen to be runtime-gated today, but a future
# feature that IS gated behind a flag/rollout has exactly one place to
# report its actual availability. Hardcoding a literal list at the call
# site would keep working right up until the day a feature is gated and the
# ack silently lies about supporting it — this indirection is what keeps
# that from being possible by construction rather than by remembering to
# update two places in sync.
CAPABILITIES: tuple[str, ...] = (
    "write_progress",  # write_progress tool — repeatable status updates on an OPEN exchange (protocol v1 §2)
    "by_agent",  # by-agent pointer mailbox — agent-replies/by-agent/<agent-slug>/<request_id> (protocol v1 §3.2)
    "compact_json",  # replies/acks/pointers are written as compact single-line JSON, not pretty-printed (#1519)
)


def get_capabilities() -> list[str]:
    """Return the agent-channel features this server build actually supports.

    See the ``CAPABILITIES`` module constant for what each entry means and
    why this is a function rather than a literal inlined where it's used.
    """
    return list(CAPABILITIES)


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

    Written compact (``indent=None``, single line) for the same reason as
    ``write_reply`` — see its docstring. Same machine-polled-file class,
    same failure mode to avoid.

    The ack write is a hard part of the claim path (agent-channel protocol
    v1.1): it is always attempted, and a write failure never blocks the
    claim itself from succeeding — a message that was successfully claimed
    stays claimed even if this write fails; see the except branch below.
    What changes on failure is that the degradation is loud rather than
    silent: a claim whose ack failed is a v0 exchange (no capability
    advertisement, no progress visibility) masquerading as v1, and that
    must be visible in logs/metrics, not swallowed.

    Includes ``phase``/``pct`` (both ``None`` here) alongside ``text`` for
    the same reason write_progress() does — both functions write the SAME
    file (agent-replies/<request_id>.ack.json), so a reader must never see
    two different shapes for it depending on which writer landed last.
    """
    ack_payload = {
        "request_id": request_id,
        "ack": True,
        "capabilities": get_capabilities(),
        "phase": None,
        "pct": None,
        "text": ack_text,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        atomic_write_json(agent_replies_dir / f"{request_id}.ack.json", ack_payload, indent=None)
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
        # Ack write failed. The claim itself already succeeded (the message
        # was moved to processing/ before this function was ever called —
        # see handle_claim_and_ack) and stays succeeded: never block the
        # work over a best-effort status write. But this must be LOUD, not a
        # quiet downgrade to a v0 exchange: log at error level (with
        # request_id, so it's greppable/correlatable) and escalate the
        # audit event to severity="error" so it's counted in the event
        # bus's errors_last_1h metric (MetricsListener) — this is the
        # existing "metric" mechanism in this codebase for exactly this
        # kind of internal failure signal.
        #
        # write_observation (the user-facing dispatcher-inbox mechanism)
        # was considered and rejected for this: it requires a chat_id to
        # route to, and an agent-channel ack failure has none to give it —
        # by protocol design (agent_channel_schema.ADDRESSING) this
        # channel's traffic is never routed through Dan's chat_id at all.
        # severity="error" (not "critical") is deliberate for the same
        # reason: "critical" pages Dan on Telegram unconditionally
        # (CriticalAlertListener), which would be exactly the cross-channel
        # leak the protocol's fail-closed design exists to prevent.
        log.error(
            f"claim_and_ack: local-claude ack write FAILED for request_id={request_id!r} "
            f"(message_id={message_id!r}) — claim still succeeds, message stays in "
            f"processing/, stale recovery handles it: {e}"
        )
        emit_event(
            "agent_channel.ack",
            {"request_id": request_id, "message_id": message_id, "error": str(e)},
            severity="error",
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
    phase: str | None = None,
    pct: float | None = None,
) -> ProgressOutcome:
    """Write a repeatable current-status update for an OPEN local-claude exchange.

    Overwrites agent-replies/<request_id>.ack.json — the SAME file
    write_ack() above writes at claim time — with the latest status text.
    Last-write-wins: this is a status field, not an accumulating log (agent-
    channel protocol v1 §1, the "minimal mechanism" decision).

    Structured status shape (agent-channel protocol v1.1): the payload is
    ``{"capabilities": ..., "phase": ..., "pct": ..., "text": ..., ...}``
    rather than a flat ``{"text": ...}``. Only ``text`` (via ``status_text``)
    is required — ``phase``/``pct`` are optional and default to
    ``None``/``null``. ``capabilities`` is always re-derived from
    ``get_capabilities()`` (the same single source write_ack() reads), never
    carried over from whatever was already on disk — see the inline comment
    at the write site for why a progress write must never drop the
    capability list write_ack() set at claim time. This shape is shipped
    now, before write_progress has any live consumers, so a reader never
    has to handle two different payload shapes for the same file depending
    on when it was written.

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
       request_id — not two separate filesystem operations — so a late
       write_progress call can never land between "checked, terminal file
       absent" and "wrote" *relative to another write_progress call for the
       same request_id*. IMPORTANT SCOPE LIMIT: this lock is private to
       write_progress (`.{request_id}.progress.lock`) — write_reply() (the
       terminal-reply writer, above) does not take it and is not aware of
       it. So this guard serializes write_progress against itself; it does
       NOT make the terminal reply's arrival atomic with respect to this
       check. A write_progress call can still pass the "terminal file
       absent" check and then have write_reply() land — completely
       unlocked — before this call's own atomic_write_json finishes,
       leaving a stale status in .ack.json after the real answer already
       exists. That residual window is accepted, not closed: .ack.json is
       non-authoritative once the terminal reply exists — clients MUST
       treat agent-replies/<request_id>.json (the reply file) as the sole
       completion signal, never .ack.json. See
       ~/lobster-workspace/assessments/agent-channel-protocol-proposal.md
       for the accepted-risk writeup; closing this fully would require
       write_reply() to take the same flock, which is a change to the hot
       terminal-writer path shared by every reply on this channel and is
       deliberately out of scope here.
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
                "capabilities": get_capabilities(),
                "phase": phase,
                "pct": pct,
                "text": status_text,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            # Written compact (indent=None, single line), not pretty-printed —
            # same reasoning as write_ack/write_reply's compact-JSON fix
            # (PR #1519): .ack.json is a machine-polled API contract, and a
            # pretty-printed multi-line payload silently breaks any reader
            # that isn't fully JSON-aware. write_progress overwrites the same
            # file write_ack() writes at claim time, so it must match that
            # file's compact-JSON invariant or it would re-break it on the
            # next status update.
            #
            # `capabilities` (bloom adversarial review, PR #1530): write_ack()
            # and write_progress() write the SAME file
            # (agent-replies/<request_id>.ack.json) — write_ack() sets
            # "capabilities" at claim time, but write_progress() previously
            # built its payload from scratch without it, so the very first
            # progress update after a claim silently dropped the capability
            # list the ack had just advertised. Re-deriving it here from the
            # same get_capabilities() single source of truth (rather than,
            # say, reading the existing file and merging) keeps both writers
            # producing the identical payload shape for this file — a reader
            # never has to know which of the two functions wrote it last.
            atomic_write_json(ack_path, ack_payload, indent=None)
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
