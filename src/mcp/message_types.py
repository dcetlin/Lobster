"""
Formal message type taxonomy for the Lobster inbox bus (issue #156).

This module is intentionally dependency-free so it can be imported and tested
without pulling in the full inbox_server stack (MCP, SQLite, watchdog, etc.).

Every inbox message carries two required routing fields:
  source — who sent it (telegram, slack, sms, system, …)
  type   — what kind of content (text, voice, subagent_result, …)

The constants here are the single source of truth. inbox_server.py imports
from this module; nothing else should define its own ad-hoc type strings.

The agent-channel source name ("local-claude") is itself owned by
src/protocol/agent_channel_schema.py, the canonical schema module for that
protocol — imported below rather than redefined so the source string
recognized here and the one described to an external agent can't drift
apart.
"""

from enum import StrEnum

from src.protocol.agent_channel_schema import SOURCE as _AGENT_CHANNEL_SOURCE

# ---------------------------------------------------------------------------
# Message relationship (issue #1536, phase 1 — additive stamping only)
# ---------------------------------------------------------------------------


class MessageRelationship(StrEnum):
    """Which of the three dispatcher relationships a message belongs to.

    The dispatcher's inbox conflates three distinct relationships into one
    envelope shape (dispatcher<->user, dispatcher<->subagent,
    dispatcher<->peer-agent), which today gets reconstructed at read time
    from incidental fields (request_id presence, sent_reply_to_user, an
    error/error_type discriminator). This StrEnum names the three
    relationships explicitly so a future phase can stamp — and eventually
    route on — the relationship directly instead of re-deriving it.

    Phase 1 is stamp-only (see message_types.py module docstring and
    lobster_meta.classify_relationship): the field is written wherever the
    source is unambiguous, or via a claim-time fallback classifier for
    external producers. No reader branches on this field yet — that is
    deferred to a later phase of issue #1536.
    """

    USER = "user"        # a human on a real chat channel (telegram, slack, sms, signal, whatsapp, bisque)
    SUBAGENT = "subagent"  # a Lobster subagent reporting back via the write_result path
    PEER_AGENT = "peer_agent"  # another agent/session on the local-claude agent channel


# ---------------------------------------------------------------------------
# User-initiated types  (source = telegram | slack | sms | signal | whatsapp | bisque)
# ---------------------------------------------------------------------------
INBOX_USER_TYPES: frozenset[str] = frozenset({
    "text",       # plain text message
    "message",    # DEPRECATED alias — normalizes to "text" on ingest
    "photo",      # image/photo attachment
    "image",      # DEPRECATED alias — normalizes to "photo" on ingest (Slack producer)
    "voice",      # voice/audio message (needs transcription)
    "audio",      # DEPRECATED alias — normalizes to "voice" on ingest
    "video",      # video attachment
    "document",   # file/document attachment
    "sticker",    # sticker message
    "location",   # location pin
    "callback",   # inline keyboard button press
    "reaction",   # Telegram emoji reaction (fields: emoji, reacted_to_text, telegram_message_id)
})

# ---------------------------------------------------------------------------
# System-generated types  (source = system)
# ---------------------------------------------------------------------------
INBOX_SYSTEM_TYPES: frozenset[str] = frozenset({
    "self_check",             # periodic health/reminder injection
    "subagent_result",        # subagent completed work (fields: task_id, payload, artifacts?)
    "subagent_error",         # subagent failed (fields: task_id, error, retry_count)
    "subagent_ack",           # subagent already sent reply via send_reply (no re-delivery); canonical name
    "subagent_notification",  # DEPRECATED alias — use subagent_ack; kept for backward compat
    "subagent_observation",   # subagent noticed something in passing (debug/context)
    "subagent_stale_check",   # dispatch registry found agent with stale heartbeat
    "subagent_recovered",     # subagent fallback recovery event (chat_id unknown; dispatcher handles, never relay directly)
    "agent_failed",           # reconciler/agent-monitor detected dead agent (chat_id=0; dispatcher decides re-queue vs escalate vs drop)
    "compact_group",          # grouped compact messages (internal, produced by check_inbox)
    "compact_reminder",       # on-compact hook reminder (hooks/on-compact.py)
    "cron_reminder",          # DEPRECATED alias — normalizes to "scheduled_reminder" on ingest
    "scheduled_reminder",     # scheduled reminder (scripts/post-reminder.sh, scheduled-tasks/dispatch-job.sh)
    "update_notification",    # system update available (scripts/check-updates.sh)
    "consolidation",          # nightly consolidation result
    "observation",            # OOM-monitor or similar system observation
    "health_check",           # health check output (replaces "task-output" and "system" from health check scripts)
    "system",                 # DEPRECATED alias — normalizes to "health_check" on ingest (from health check scripts)
    "task-output",            # DEPRECATED alias — normalizes to "health_check" on ingest (scripts/daily-health-check.sh)
    "debug_observation",      # debug output from inbox_server.py internals; excluded from skill processing
    "session_note_reminder",  # MCP counter reached 20 user messages — dispatcher should spawn session-note-appender
    "wos_execute",            # WOS executor dispatched a UoW — dispatcher must call route_wos_message() to spawn subagent (issue #856)
    "wos_prescribe",          # WOS steward dispatched a prescription task — dispatcher must call route_wos_message() to spawn prescription subagent
    "wos_escalate",           # WOS subagent escalated a UoW for owner attention
    "wos_done",               # WOS UoW reached terminal done state
    "wos_surface",            # WOS surfacing event — steward surfacing a completed/notable UoW result
    "steward_trigger",        # WOS steward-heartbeat trigger message
    "wos_uow_completed",      # WOS UoW completed and result is ready
    "wos_capacity_available", # WOS executor capacity became available (slot freed after completion)
    "wos_owner_required",     # WOS subagent escalated with outcome=owner_decision_required; UoW is awaiting-owner; dispatcher relays to Dan
    "scheduled_task_crash",   # heartbeat script caught an unhandled exception and wrote a crash alert (fields: job_name, text)
    "pr_review_request",      # PR-review sweeper coordinator requests oracle review for a specific PR (source: pr_review_sweeper)
    "wos_pr_sweep_result",    # WOS PR sweeper reports stale/merged PRs with pending UoWs (source: wos_pr_sweep)
})

# ---------------------------------------------------------------------------
# Combined set — all known types
# ---------------------------------------------------------------------------
INBOX_MESSAGE_TYPES: frozenset[str] = INBOX_USER_TYPES | INBOX_SYSTEM_TYPES

# ---------------------------------------------------------------------------
# Known sources
# ---------------------------------------------------------------------------
INBOX_MESSAGE_SOURCES: frozenset[str] = frozenset({
    "telegram",
    "slack",
    "sms",
    "signal",
    "whatsapp",
    "bisque",
    "system",
    "bot-talk",           # cross-Lobster bot-to-bot messages (issue #1350)
    "gmail",              # email poller injects messages with source="gmail"
    "pr_review_sweeper",  # PR-review sweep coordinator — dispatches pr_review_request messages (issue #1268)
    "wos_pr_sweep",       # WOS PR sweep cron script — reports stale/merged PRs (scheduled-tasks/wos-pr-sweeper.py)
    _AGENT_CHANNEL_SOURCE,  # "local-claude" — agent channel: local Claude Code session (SSH) talking to the dispatcher — see docs/reference/agent-channel.md and docs/reference/agent-channel-schema.md
    "cron",               # cron-triggered job messages (e.g. scheduled_job_trigger from dispatch-job.sh)
    "hook",               # hook-injected messages (e.g. write_observation from hooks/decision-router.py)
    "test",               # test harness messages (integration tests)
})

# ---------------------------------------------------------------------------
# Types that represent direct user-facing messages requiring a reply.
# Used by mark_processed to guard against dropping human messages silently.
# subagent_result / subagent_error are excluded: they are system routing
# messages even though they carry source="telegram" for delivery purposes.
# ---------------------------------------------------------------------------
USER_FACING_TYPES: frozenset[str] = INBOX_USER_TYPES
