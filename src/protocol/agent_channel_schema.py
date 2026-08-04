"""
Canonical schema for the Lobster agent channel protocol (``source="local-claude"``).

This module is the SINGLE SOURCE OF TRUTH for the agent-channel wire format —
the envelope shapes, the request_id rules, the addressing model (Dan vs. the
Agent), and the error/ack semantics. Every artifact that describes this
protocol to a human or an external agent is generated from the data
structures in this module, so none of them can drift out of sync with each
other or with server-side validation:

- ``lobster-chat --schema`` / ``lobster-chat --help`` (the embedded block in
  ``scripts/lobster-chat.py``, between the ``BEGIN``/``END GENERATED`` markers)
- The generated schema doc, ``docs/agent-channel-schema.md``

Both are produced by ``scripts/generate_agent_channel_docs.py``, which imports
this module and either writes the generated artifacts or (with ``--check``)
verifies they are still in sync with it. ``src/mcp/reliability.py`` also
imports the ``REQUEST_ID_*`` constants from here rather than redefining them,
so the value enforced at the validation boundary and the value described to
an external agent are structurally the same value, not two copies someone
has to remember to keep matching.

Design constraint: this module is intentionally stdlib-only and free of any
other Lobster-internal imports (no ``src.mcp``, no ``src.utils``). It
describes the protocol as an external agent would encounter it — a laptop
process talking to the VPS over SSH, with none of the rest of this repo
installed — so it must not accidentally depend on anything that isn't
available there.

See ``docs/agent-channel.md`` for the prose walkthrough of the whole feature
and ``assessments/agent-channel-protocol-spec-2026-08-04.md`` for the
principles this schema exists to satisfy.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# Protocol identity
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "1"

# The only value INBOUND_ENVELOPE.source or a reply's source is ever allowed
# to be on this channel. Everything else described in this module exists to
# answer "given this one source value, what can the two sides say to each
# other, and where can it never go?"
SOURCE = "local-claude"

# ---------------------------------------------------------------------------
# request_id rules (protocol spec principle 3: per-request identity;
# principle 6: filesystem-safe request identity)
# ---------------------------------------------------------------------------

REQUEST_ID_MAX_LEN = 128
REQUEST_ID_CHARSET_DESCRIPTION = "letters, digits, '-', and '_' only"
REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]+$"

REQUEST_ID_RULES = (
    "request_id is required on the inbound request and is the correlation "
    "key for the reply. It must be unique per request — never reused across "
    "requests, and never a constant shared across every request from a given "
    "client (protocol spec principle 3: per-request identity, not per-client "
    "identity). It must be 1-128 characters matching "
    f"`{REQUEST_ID_PATTERN}` ({REQUEST_ID_CHARSET_DESCRIPTION}) — no path "
    "separators, no '.', no whitespace — because it is used verbatim as a "
    "filesystem path component (`agent-replies/<request_id>.json`). A "
    "conventional generator is `f'{int(time.time())}-{uuid.uuid4().hex[:8]}'`, "
    "but any value matching the pattern is accepted. `id` and `request_id` "
    "on the inbound message are conventionally the same value."
)

# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------
# Each field entry: {type, required, description, const?, format?}. This
# shape maps directly onto JSON Schema (see json_schema() below) and is also
# what render_markdown() walks to build the field tables in the docs.

INBOUND_ENVELOPE: dict[str, dict[str, Any]] = {
    "id": {
        "type": "string",
        "required": True,
        "description": (
            "Message identifier. By convention equal to request_id — this is "
            "what lets the dispatcher pass message_id=request_id to a single "
            "send_reply() call and get both 'mark processed' and 'write the "
            "reply' from one call."
        ),
    },
    "source": {
        "type": "string",
        "required": True,
        "const": SOURCE,
        "description": "Must be exactly \"local-claude\". This is what tells Lobster the message is agent-channel traffic, not a Telegram/Slack message from Dan.",
    },
    "type": {
        "type": "string",
        "required": True,
        "const": "text",
        "description": "Always \"text\" — the same content type ordinary chat messages use.",
    },
    "chat_id": {
        "type": "string",
        "required": True,
        "description": (
            "Required by the shared inbox schema every source uses, but NOT "
            "used for routing on this channel — routing is entirely by "
            "request_id. Any stable string is fine; the reference CLI sends "
            "\"local-claude\"."
        ),
    },
    "text": {
        "type": "string",
        "required": True,
        "description": "The message body — what you are asking or telling Lobster.",
    },
    "request_id": {
        "type": "string",
        "required": True,
        "pattern": REQUEST_ID_PATTERN,
        "max_length": REQUEST_ID_MAX_LEN,
        "description": REQUEST_ID_RULES,
    },
    "timestamp": {
        "type": "string",
        "required": True,
        "format": "date-time",
        "description": "ISO 8601 UTC timestamp of when the request was written.",
    },
}

REPLY_ENVELOPE: dict[str, dict[str, Any]] = {
    "request_id": {
        "type": "string",
        "required": True,
        "description": "Echoes the inbound request's request_id — this is what a polling client matches its own request against.",
    },
    "text": {
        "type": "string",
        "required": True,
        "description": "The answer. This is the ONLY thing written to this file — see error/ack semantics below for what happens when there is no answer to give.",
    },
    "ts": {
        "type": "string",
        "required": True,
        "format": "date-time",
        "description": "ISO 8601 UTC timestamp of when the reply was written.",
    },
    "in_reply_to": {
        "type": "string",
        "required": True,
        "description": "The inbound message's id (normally equal to request_id).",
    },
}

ACK_ENVELOPE: dict[str, dict[str, Any]] = {
    "request_id": {
        "type": "string",
        "required": True,
        "description": "Echoes the inbound request's request_id.",
    },
    "text": {
        "type": "string",
        "required": True,
        "description": "A short progress note (e.g. \"working on it\"). NOT an answer — see error/ack semantics.",
    },
    "ts": {
        "type": "string",
        "required": True,
        "format": "date-time",
        "description": "ISO 8601 UTC timestamp of when the ack was written.",
    },
}

# ---------------------------------------------------------------------------
# File locations — where each envelope actually lands, and who writes it.
# ---------------------------------------------------------------------------

FILE_LOCATIONS = {
    "request": {
        "path": "~/messages/inbox/<request_id>.json",
        "written_by": "the Agent (the client)",
        "envelope": "INBOUND_ENVELOPE",
    },
    "ack": {
        "path": "~/messages/agent-replies/<request_id>.ack.json",
        "written_by": "Lobster (optional, at most a progress note)",
        "envelope": "ACK_ENVELOPE",
    },
    "reply": {
        "path": "~/messages/agent-replies/<request_id>.json",
        "written_by": "Lobster (exactly once, ever, per request_id)",
        "envelope": "REPLY_ENVELOPE",
    },
}

# ---------------------------------------------------------------------------
# Addressing — Dan vs. the Agent (protocol spec section 1, "Actors")
# ---------------------------------------------------------------------------

ADDRESSING = {
    "agent": (
        "You (the external agent) are addressed exclusively through the "
        "reply file at ~/messages/agent-replies/<request_id>.json, keyed by "
        "the request_id you generated. Nothing you send on this channel is "
        "ever shown to Dan directly — chat_id on this channel is not a "
        "routing address, it is an inert required field."
    ),
    "dan": (
        "Dan is addressed only through Telegram/Slack, a structurally "
        "separate code path (source=\"telegram\"/\"slack\", routed by his "
        "chat_id, not by any request_id). Your request can cause a subagent "
        "to also page Dan on Telegram/Slack — but that is always a second, "
        "independent send_reply() call the subagent chooses to make; it is "
        "never a side effect of the reply addressed to you, and there is no "
        "field you can set on your request that causes it automatically. "
        "Conversely, nothing you say here reaches Dan's phone as passive "
        "notification noise unless a subagent explicitly decides to page him."
    ),
    "invariant": (
        "A reply addressed to the Agent is architecturally incapable of "
        "reaching the Telegram/Slack outbox: the two are different files in "
        "different directories, selected by the source of the *original "
        "request*, verified fail-closed (see error/ack semantics) rather "
        "than trusted from a caller-supplied value."
    ),
}

# ---------------------------------------------------------------------------
# Error / ack semantics (protocol spec principles 1, 4, 5)
# ---------------------------------------------------------------------------

ERROR_AND_ACK_SEMANTICS = [
    {
        "name": "single_shot_reply_slot",
        "summary": "Exactly one reply is ever written per request_id, and it is immutable once written.",
        "detail": (
            "The reply file is created with a create-if-absent write (not an "
            "overwrite): whichever writer gets there first wins, and every "
            "later writer for the same request_id is a silent no-op from "
            "your point of view — you will simply never see a second, "
            "different answer overwrite the first."
        ),
    },
    {
        "name": "ack_is_not_answer",
        "summary": "An ack is a distinct file, not a placeholder in the answer slot.",
        "detail": (
            "If you ever see <request_id>.ack.json appear before "
            "<request_id>.json, that is a progress note ('received, working "
            "on it'), not a final answer. It never counts toward, occupies, "
            "or blocks the single reply slot described above — keep polling "
            "for <request_id>.json (without the .ack suffix) for the actual "
            "answer. An ack file may never appear at all; that is normal, "
            "not an error."
        ),
    },
    {
        "name": "silence_is_a_sanctioned_failure",
        "summary": "No reply ever arriving is a legitimate, distinguishable outcome — not a bug to work around.",
        "detail": (
            "If Lobster crashes or never gets scheduled before your poll "
            "timeout, no reply file is ever written. You cannot distinguish "
            "'still working', 'crashed', and 'never seen' from the client "
            "side, and the protocol does not try to make you able to — "
            "report a client-side timeout as 'no reply', not as a specific "
            "failure reason you cannot actually verify. When Lobster *can* "
            "still respond after a failure, it writes one normal reply "
            "describing the failure in `text` — there is no separate "
            "error envelope; failures are answers, delivered the same way "
            "successes are."
        ),
    },
    {
        "name": "fail_closed_on_source_mismatch",
        "summary": "If Lobster can't verify a reply's destination matches the request's source, it refuses to send rather than guesses.",
        "detail": (
            "This protects you structurally: a reply that was going to be "
            "misrouted is refused server-side (you see it as silence/timeout) "
            "rather than ever leaking to Dan's Telegram/Slack, or a stray "
            "reply from an unrelated request landing in your slot."
        ),
    },
]

# ---------------------------------------------------------------------------
# JSON Schema rendering (for `lobster-chat --schema`)
# ---------------------------------------------------------------------------


def _field_to_json_schema_property(field: dict[str, Any]) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": field["type"], "description": field["description"]}
    if "const" in field:
        prop["const"] = field["const"]
    if "pattern" in field:
        prop["pattern"] = field["pattern"]
    if "max_length" in field:
        prop["maxLength"] = field["max_length"]
    if "format" in field:
        prop["format"] = field["format"]
    return prop


def _envelope_to_json_schema(title: str, envelope: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "title": title,
        "type": "object",
        "properties": {name: _field_to_json_schema_property(f) for name, f in envelope.items()},
        "required": [name for name, f in envelope.items() if f.get("required")],
        "additionalProperties": True,
    }


def json_schema() -> dict[str, Any]:
    """Return the full agent-channel protocol as one JSON-serializable schema dict.

    This is exactly what `lobster-chat --schema` prints (as JSON on stdout).
    It is the machine-readable counterpart to render_markdown() below — an
    external agent with no other Lobster context can parse this and know
    everything it needs to construct a request and interpret a reply.
    """
    return {
        "protocol": "lobster-agent-channel",
        "version": PROTOCOL_VERSION,
        "source": SOURCE,
        "request_id_rules": {
            "max_length": REQUEST_ID_MAX_LEN,
            "pattern": REQUEST_ID_PATTERN,
            "description": REQUEST_ID_RULES,
        },
        "files": FILE_LOCATIONS,
        "envelopes": {
            "request": _envelope_to_json_schema("AgentChannelRequest", INBOUND_ENVELOPE),
            "ack": _envelope_to_json_schema("AgentChannelAck", ACK_ENVELOPE),
            "reply": _envelope_to_json_schema("AgentChannelReply", REPLY_ENVELOPE),
        },
        "addressing": ADDRESSING,
        "error_and_ack_semantics": ERROR_AND_ACK_SEMANTICS,
    }


# ---------------------------------------------------------------------------
# Markdown rendering (for docs/agent-channel-schema.md and --help)
# ---------------------------------------------------------------------------


def _render_field_table(envelope: dict[str, dict[str, Any]]) -> str:
    lines = ["| Field | Type | Required | Notes |", "|---|---|---|---|"]
    for name, field in envelope.items():
        notes = field["description"]
        if "const" in field:
            notes = f'Always `"{field["const"]}"`. {notes}'
        lines.append(f"| `{name}` | {field['type']} | {'yes' if field.get('required') else 'no'} | {notes} |")
    return "\n".join(lines)


def render_markdown() -> str:
    """Render the full schema as a standalone Markdown document.

    Written verbatim to docs/agent-channel-schema.md by
    scripts/generate_agent_channel_docs.py. Audience: an external agent (or
    a person) with zero prior Lobster context.
    """
    parts = [
        "<!-- GENERATED FILE — do not edit by hand.",
        "Source of truth: src/protocol/agent_channel_schema.py",
        "Regenerate: uv run scripts/generate_agent_channel_docs.py",
        "Verify in sync: uv run scripts/generate_agent_channel_docs.py --check -->",
        "",
        "# Agent Channel Schema (generated)",
        "",
        f"Protocol version: `{PROTOCOL_VERSION}`. Source value: `\"{SOURCE}\"`.",
        "",
        "This document is generated from `src/protocol/agent_channel_schema.py`, "
        "the single canonical schema module for this protocol — it is also what "
        "`lobster-chat --schema` prints as JSON and what `lobster-chat --help` "
        "summarizes, so all three stay in sync by construction, not by hand-edit "
        "discipline. See `docs/agent-channel.md` for the prose walkthrough and "
        "operational detail (deploy, retention, observability); this document is "
        "the wire-format reference.",
        "",
        "## Addressing: Dan vs. the Agent",
        "",
        f"**You (the Agent).** {ADDRESSING['agent']}",
        "",
        f"**Dan.** {ADDRESSING['dan']}",
        "",
        f"**Invariant.** {ADDRESSING['invariant']}",
        "",
        "## request_id rules",
        "",
        REQUEST_ID_RULES,
        "",
        "## Files",
        "",
        "| File | Written by | Envelope |",
        "|---|---|---|",
    ]
    for key, loc in FILE_LOCATIONS.items():
        parts.append(f"| `{loc['path']}` | {loc['written_by']} | {loc['envelope']} |")
    parts += [
        "",
        "## Envelope: request (what you write)",
        "",
        f"Written by you to `{FILE_LOCATIONS['request']['path']}`.",
        "",
        _render_field_table(INBOUND_ENVELOPE),
        "",
        "```json",
        json.dumps(
            {
                "id": "1732900000-a1b2c3d4",
                "source": SOURCE,
                "type": "text",
                "chat_id": "local-claude",
                "text": "what's the status of PR 1234?",
                "request_id": "1732900000-a1b2c3d4",
                "timestamp": "2026-08-04T04:40:00.000000+00:00",
            },
            indent=2,
        ),
        "```",
        "",
        "## Envelope: ack (optional, written by Lobster)",
        "",
        f"Written by Lobster to `{FILE_LOCATIONS['ack']['path']}`. May never appear at all — that is normal. Never the final answer.",
        "",
        _render_field_table(ACK_ENVELOPE),
        "",
        "## Envelope: reply (the answer, written by Lobster exactly once)",
        "",
        f"Written by Lobster to `{FILE_LOCATIONS['reply']['path']}`.",
        "",
        _render_field_table(REPLY_ENVELOPE),
        "",
        "```json",
        json.dumps(
            {
                "request_id": "1732900000-a1b2c3d4",
                "text": "...",
                "ts": "2026-08-04T04:40:03.000000+00:00",
                "in_reply_to": "1732900000-a1b2c3d4",
            },
            indent=2,
        ),
        "```",
        "",
        "## Error and ack semantics",
        "",
    ]
    for item in ERROR_AND_ACK_SEMANTICS:
        parts.append(f"### {item['summary']}")
        parts.append("")
        parts.append(item["detail"])
        parts.append("")
    parts += [
        "## Machine-readable form",
        "",
        "Everything above is also available as JSON: `lobster-chat --schema` "
        "(no SSH round trip — it prints the embedded schema and exits) or "
        "`python3 -c \"from src.protocol.agent_channel_schema import json_schema; "
        "import json; print(json.dumps(json_schema()))\"` from a checkout of this "
        "repo.",
        "",
    ]
    return "\n".join(parts)


def render_cli_help_epilog() -> str:
    """Compact plain-text summary for lobster-chat's --help epilog.

    Short by design — the CLI's own --help should orient a first-time reader
    in a few lines, not reproduce the full doc. Point them at --schema and
    docs/agent-channel-schema.md for the rest.
    """
    return "\n".join(
        [
            "Protocol summary (see --schema for the full machine-readable form,",
            "or docs/agent-channel-schema.md in the lobster repo for the prose version):",
            "",
            "  - Every request gets a unique request_id (auto-generated) that is also",
            "    its reply's filename — never reuse one across requests.",
            "  - Your reply appears at agent-replies/<request_id>.json, written at most",
            "    once. An intermediate agent-replies/<request_id>.ack.json, if you see",
            "    one, is a progress note, not the answer — keep polling.",
            "  - No reply within --timeout is a legitimate outcome (Lobster may still",
            "    be working, or may have crashed) — this CLI reports it as \"no reply\",",
            "    not as a specific failure, because it cannot tell the difference.",
            "  - This channel never reaches Dan's Telegram/Slack, and nothing you send",
            "    is shown to Dan unless Lobster separately decides to page him.",
        ]
    )
