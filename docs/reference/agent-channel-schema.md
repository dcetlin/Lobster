<!-- GENERATED FILE — do not edit by hand.
Source of truth: src/protocol/agent_channel_schema.py
Regenerate: uv run scripts/generate_agent_channel_docs.py
Verify in sync: uv run scripts/generate_agent_channel_docs.py --check -->

# Agent Channel Schema (generated)

Protocol version: `1`. Source value: `"local-claude"`.

This document is generated from `src/protocol/agent_channel_schema.py`, the single canonical schema module for this protocol — it is also what `lobster-chat --schema` prints as JSON and what `lobster-chat --help` summarizes, so all three stay in sync by construction, not by hand-edit discipline. See `docs/reference/agent-channel.md` for the prose walkthrough and operational detail (deploy, retention, observability); this document is the wire-format reference.

## Addressing: Dan vs. the Agent

**You (the Agent).** You (the external agent) are addressed exclusively through the reply file at ~/messages/agent-replies/<request_id>.json, keyed by the request_id you generated. Nothing you send on this channel is ever shown to Dan directly — chat_id on this channel is not a routing address, it is an inert required field.

**Dan.** Dan is addressed only through Telegram/Slack, a structurally separate code path (source="telegram"/"slack", routed by his chat_id, not by any request_id). Your request can cause a subagent to also page Dan on Telegram/Slack — but that is always a second, independent send_reply() call the subagent chooses to make; it is never a side effect of the reply addressed to you, and there is no field you can set on your request that causes it automatically. Conversely, nothing you say here reaches Dan's phone as passive notification noise unless a subagent explicitly decides to page him.

**Invariant.** A reply addressed to the Agent is architecturally incapable of reaching the Telegram/Slack outbox: the two are different files in different directories, selected by the source of the *original request*, verified fail-closed (see error/ack semantics) rather than trusted from a caller-supplied value.

## request_id rules

request_id is required on the inbound request and is the correlation key for the reply. It must be unique per request — never reused across requests, and never a constant shared across every request from a given client (protocol spec principle 3: per-request identity, not per-client identity). It must be 1-128 characters matching `^[A-Za-z0-9_-]+$` (letters, digits, '-', and '_' only) — no path separators, no '.', no whitespace — because it is used verbatim as a filesystem path component (`agent-replies/<request_id>.json`). A conventional generator is `f'{int(time.time())}-{uuid.uuid4().hex[:8]}'`, but any value matching the pattern is accepted. `id` and `request_id` on the inbound message are conventionally the same value.

## Files

| File | Written by | Envelope |
|---|---|---|
| `~/messages/inbox/<request_id>.json` | the Agent (the client) | INBOUND_ENVELOPE |
| `~/messages/agent-replies/<request_id>.ack.json` | Lobster (optional, at most a progress note) | ACK_ENVELOPE |
| `~/messages/agent-replies/<request_id>.json` | Lobster (exactly once, ever, per request_id) | REPLY_ENVELOPE |

## Envelope: request (what you write)

Written by you to `~/messages/inbox/<request_id>.json`.

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Message identifier. By convention equal to request_id — this is what lets the dispatcher pass message_id=request_id to a single send_reply() call and get both 'mark processed' and 'write the reply' from one call. |
| `source` | string | yes | Always `"local-claude"`. Must be exactly "local-claude". This is what tells Lobster the message is agent-channel traffic, not a Telegram/Slack message from Dan. |
| `type` | string | yes | Always `"text"`. Always "text" — the same content type ordinary chat messages use. |
| `chat_id` | string | yes | Required by the shared inbox schema every source uses, but NOT used for routing on this channel — routing is entirely by request_id. Any stable string is fine; the reference CLI sends "local-claude". |
| `text` | string | yes | The message body — what you are asking or telling Lobster. |
| `request_id` | string | yes | request_id is required on the inbound request and is the correlation key for the reply. It must be unique per request — never reused across requests, and never a constant shared across every request from a given client (protocol spec principle 3: per-request identity, not per-client identity). It must be 1-128 characters matching `^[A-Za-z0-9_-]+$` (letters, digits, '-', and '_' only) — no path separators, no '.', no whitespace — because it is used verbatim as a filesystem path component (`agent-replies/<request_id>.json`). A conventional generator is `f'{int(time.time())}-{uuid.uuid4().hex[:8]}'`, but any value matching the pattern is accepted. `id` and `request_id` on the inbound message are conventionally the same value. |
| `timestamp` | string | yes | ISO 8601 UTC timestamp of when the request was written. |
| `agent` | string | no | Optional identity label for the calling agent/session (e.g. "glyph"), set via lobster-chat's --agent flag or the LOBSTER_CHAT_AGENT env var. Lobster does not use it for correlation or authorization (request_id remains the sole correlation key and the sole basis for write_progress's claim-bound authorization); it exists so the dispatcher can render inbox messages as "from <agent>" instead of a generic source label, and — when non-empty — so the reply is also indexed at agent-replies/by-agent/<agent-slug>/<request_id> (see AGENT_SLUG_RULES), a read-only discovery convenience, not an access-control mechanism. Omit it entirely rather than sending an empty string if the caller has no identity to report. |

```json
{
  "id": "1732900000-a1b2c3d4",
  "source": "local-claude",
  "type": "text",
  "chat_id": "local-claude",
  "text": "what's the status of PR 1234?",
  "request_id": "1732900000-a1b2c3d4",
  "timestamp": "2026-08-04T04:40:00.000000+00:00"
}
```

## Envelope: ack (optional, written by Lobster)

Written by Lobster to `~/messages/agent-replies/<request_id>.ack.json`. May never appear at all — that is normal. Never the final answer.

| Field | Type | Required | Notes |
|---|---|---|---|
| `request_id` | string | yes | Echoes the inbound request's request_id. |
| `text` | string | yes | A short progress note (e.g. "working on it"). NOT an answer — see error/ack semantics. |
| `phase` | string | no | Optional short phase label (e.g. "testing"), added in protocol v1.1. null when the writer didn't set one — always present as a key, so a reader never has to distinguish "key absent" from "key null" for this file. |
| `pct` | number | no | Optional completion percentage (e.g. 60), added in protocol v1.1. null when the writer didn't set one — same always-present-as-a-key convention as phase. |
| `ts` | string | yes | ISO 8601 UTC timestamp of when the ack was written. |
| `capabilities` | array | no | Added in protocol v1.1: the agent-channel features this server build actually supports, e.g. ["write_progress", "by_agent", "compact_json"] — lets a caller do version negotiation without a protocol version bump. Written by claim_and_ack's ack; write_progress's status updates overwrite the same file without repeating this field (it does not change mid-exchange). |

## Envelope: reply (the answer, written by Lobster exactly once)

Written by Lobster to `~/messages/agent-replies/<request_id>.json`.

| Field | Type | Required | Notes |
|---|---|---|---|
| `request_id` | string | yes | Echoes the inbound request's request_id — this is what a polling client matches its own request against. |
| `text` | string | yes | The answer. This is the ONLY thing written to this file — see error/ack semantics below for what happens when there is no answer to give. |
| `ts` | string | yes | ISO 8601 UTC timestamp of when the reply was written. |
| `in_reply_to` | string | yes | The inbound message's id (normally equal to request_id). |

```json
{
  "request_id": "1732900000-a1b2c3d4",
  "text": "...",
  "ts": "2026-08-04T04:40:03.000000+00:00",
  "in_reply_to": "1732900000-a1b2c3d4"
}
```

## Error and ack semantics

### Exactly one reply is ever written per request_id, and it is immutable once written.

The reply file is created with a create-if-absent write (not an overwrite): whichever writer gets there first wins, and every later writer for the same request_id is a silent no-op from your point of view — you will simply never see a second, different answer overwrite the first.

### An ack is a distinct file, not a placeholder in the answer slot.

If you ever see <request_id>.ack.json appear before <request_id>.json, that is a progress note ('received, working on it'), not a final answer. It never counts toward, occupies, or blocks the single reply slot described above — keep polling for <request_id>.json (without the .ack suffix) for the actual answer. An ack file may never appear at all; that is normal, not an error.

### No reply ever arriving is a legitimate, distinguishable outcome — not a bug to work around.

If Lobster crashes or never gets scheduled before your poll timeout, no reply file is ever written. You cannot distinguish 'still working', 'crashed', and 'never seen' from the client side, and the protocol does not try to make you able to — report a client-side timeout as 'no reply', not as a specific failure reason you cannot actually verify. When Lobster *can* still respond after a failure, it writes one normal reply describing the failure in `text` — there is no separate error envelope; failures are answers, delivered the same way successes are.

### If Lobster can't verify a reply's destination matches the request's source, it refuses to send rather than guesses.

This protects you structurally: a reply that was going to be misrouted is refused server-side (you see it as silence/timeout) rather than ever leaking to Dan's Telegram/Slack, or a stray reply from an unrelated request landing in your slot.

## Machine-readable form

Everything above is also available as JSON: `lobster-chat --schema` (no SSH round trip — it prints the embedded schema and exits) or `python3 -c "from src.protocol.agent_channel_schema import json_schema; import json; print(json.dumps(json_schema()))"` from a checkout of this repo.
