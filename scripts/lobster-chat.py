#!/usr/bin/env python3
"""
lobster-chat — talk to the Lobster dispatcher from your local machine over SSH.

RUNS ON YOUR LOCAL MACHINE (laptop), not on the VPS. It never touches the
lobster-mcp-local server directly; it only reads/writes files under
~/messages/ on the remote host via `ssh`, using the same "agent channel"
inbox/agent-replies contract the dispatcher speaks natively (see
docs/reference/agent-channel.md).

Flow:
  1. Generate a unique request_id.
  2. SSH in and atomically write ~/messages/inbox/<request_id>.json
     (source="local-claude", type="text") — the dispatcher's normal
     wait_for_messages loop picks this up like any other inbox message.
  3. Poll ~/messages/agent-replies/<request_id>.json over SSH until the
     dispatcher's send_reply(source="local-claude", ...) call writes it,
     or the timeout expires.
  4. Print the reply text and exit 0 (or exit 1 on timeout/error).

Prerequisite: passwordless SSH key auth to the VPS (ssh-copy-id, or an entry
in ~/.ssh/config) — this script assumes `ssh <target>` just works.

Usage:
    uv run scripts/lobster-chat.py "what's the status of PR 1234?"
    LOBSTER_CHAT_HOST=vps.example.com lobster-chat.py "hi"

    # No Lobster context at all? Start here — no SSH round trip:
    uv run scripts/lobster-chat.py --schema
    uv run scripts/lobster-chat.py --help

    # Freshly-respawned session, no request_id memory, only your own identity?
    # Discover everything waiting for you (protocol v1, Rung A durability layer):
    uv run scripts/lobster-chat.py --for glyph

Config (env var or flag, flag wins):
    --host / LOBSTER_CHAT_HOST       VPS hostname (required)
    --user / LOBSTER_CHAT_USER       SSH user (default: lobster)
    --timeout / LOBSTER_CHAT_TIMEOUT   seconds to wait for a reply (default: 300)
    --interval / LOBSTER_CHAT_INTERVAL  poll interval in seconds (default: 2)
    --agent / LOBSTER_CHAT_AGENT     optional identity label (e.g. "glyph") shown
                                      in the dispatcher's inbox instead of a
                                      generic source label (default: unset)
    --for <agent>                    discovery mode: list and print everything
                                      waiting for <agent> in
                                      agent-replies/by-agent/<agent-slug>/ (both
                                      in-progress and completed exchanges), then
                                      exit. No new request is sent.
    --schema                         print the protocol schema as JSON and exit

request_id is always printed to stderr immediately after the inbox message is
written — not only on timeout — so a dropped SSH connection or crashed CLI
never loses the correlation key needed to check
~/messages/agent-replies/<request_id>.json manually later.

While waiting for a reply, the poll loop also watches the sibling
<request_id>.ack.json progress file (protocol v1, Rung A) and prints its
status text to stderr whenever it changes, so a long-running exchange shows
"working…" progress instead of silence. This is purely observational — the
terminal reply file remains the sole, authoritative completion signal.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

# === BEGIN GENERATED SCHEMA (see scripts/generate_agent_channel_docs.py) ===
# Do not hand-edit this block. It is generated from the single canonical
# schema module, src/protocol/agent_channel_schema.py, so that --schema and
# --help can never drift from the server-side protocol they describe.
# Regenerate: uv run scripts/generate_agent_channel_docs.py
# Verify in sync: uv run scripts/generate_agent_channel_docs.py --check
_SCHEMA_JSON = '{\n  "protocol": "lobster-agent-channel",\n  "version": "1",\n  "source": "local-claude",\n  "request_id_rules": {\n    "max_length": 128,\n    "pattern": "^[A-Za-z0-9_-]+$",\n    "description": "request_id is required on the inbound request and is the correlation key for the reply. It must be unique per request \\u2014 never reused across requests, and never a constant shared across every request from a given client (protocol spec principle 3: per-request identity, not per-client identity). It must be 1-128 characters matching `^[A-Za-z0-9_-]+$` (letters, digits, \'-\', and \'_\' only) \\u2014 no path separators, no \'.\', no whitespace \\u2014 because it is used verbatim as a filesystem path component (`agent-replies/<request_id>.json`). A conventional generator is `f\'{int(time.time())}-{uuid.uuid4().hex[:8]}\'`, but any value matching the pattern is accepted. `id` and `request_id` on the inbound message are conventionally the same value."\n  },\n  "files": {\n    "request": {\n      "path": "~/messages/inbox/<request_id>.json",\n      "written_by": "the Agent (the client)",\n      "envelope": "INBOUND_ENVELOPE"\n    },\n    "ack": {\n      "path": "~/messages/agent-replies/<request_id>.ack.json",\n      "written_by": "Lobster (optional, at most a progress note)",\n      "envelope": "ACK_ENVELOPE"\n    },\n    "reply": {\n      "path": "~/messages/agent-replies/<request_id>.json",\n      "written_by": "Lobster (exactly once, ever, per request_id)",\n      "envelope": "REPLY_ENVELOPE"\n    }\n  },\n  "envelopes": {\n    "request": {\n      "title": "AgentChannelRequest",\n      "type": "object",\n      "properties": {\n        "id": {\n          "type": "string",\n          "description": "Message identifier. By convention equal to request_id \\u2014 this is what lets the dispatcher pass message_id=request_id to a single send_reply() call and get both \'mark processed\' and \'write the reply\' from one call."\n        },\n        "source": {\n          "type": "string",\n          "description": "Must be exactly \\"local-claude\\". This is what tells Lobster the message is agent-channel traffic, not a Telegram/Slack message from Dan.",\n          "const": "local-claude"\n        },\n        "type": {\n          "type": "string",\n          "description": "Always \\"text\\" \\u2014 the same content type ordinary chat messages use.",\n          "const": "text"\n        },\n        "chat_id": {\n          "type": "string",\n          "description": "Required by the shared inbox schema every source uses, but NOT used for routing on this channel \\u2014 routing is entirely by request_id. Any stable string is fine; the reference CLI sends \\"local-claude\\"."\n        },\n        "text": {\n          "type": "string",\n          "description": "The message body \\u2014 what you are asking or telling Lobster."\n        },\n        "request_id": {\n          "type": "string",\n          "description": "request_id is required on the inbound request and is the correlation key for the reply. It must be unique per request \\u2014 never reused across requests, and never a constant shared across every request from a given client (protocol spec principle 3: per-request identity, not per-client identity). It must be 1-128 characters matching `^[A-Za-z0-9_-]+$` (letters, digits, \'-\', and \'_\' only) \\u2014 no path separators, no \'.\', no whitespace \\u2014 because it is used verbatim as a filesystem path component (`agent-replies/<request_id>.json`). A conventional generator is `f\'{int(time.time())}-{uuid.uuid4().hex[:8]}\'`, but any value matching the pattern is accepted. `id` and `request_id` on the inbound message are conventionally the same value.",\n          "pattern": "^[A-Za-z0-9_-]+$",\n          "maxLength": 128\n        },\n        "timestamp": {\n          "type": "string",\n          "description": "ISO 8601 UTC timestamp of when the request was written.",\n          "format": "date-time"\n        },\n        "agent": {\n          "type": "string",\n          "description": "Optional identity label for the calling agent/session (e.g. \\"glyph\\"), set via lobster-chat\'s --agent flag or the LOBSTER_CHAT_AGENT env var. Purely cosmetic \\u2014 Lobster does not use it for routing, correlation, or authorization (request_id remains the sole correlation key); it exists so the dispatcher can render inbox messages as \\"from <agent>\\" instead of a generic source label when multiple external agent sessions share this channel. Omit it entirely rather than sending an empty string if the caller has no identity to report."\n        }\n      },\n      "required": [\n        "id",\n        "source",\n        "type",\n        "chat_id",\n        "text",\n        "request_id",\n        "timestamp"\n      ],\n      "additionalProperties": true\n    },\n    "ack": {\n      "title": "AgentChannelAck",\n      "type": "object",\n      "properties": {\n        "request_id": {\n          "type": "string",\n          "description": "Echoes the inbound request\'s request_id."\n        },\n        "text": {\n          "type": "string",\n          "description": "A short progress note (e.g. \\"working on it\\"). NOT an answer \\u2014 see error/ack semantics."\n        },\n        "ts": {\n          "type": "string",\n          "description": "ISO 8601 UTC timestamp of when the ack was written.",\n          "format": "date-time"\n        }\n      },\n      "required": [\n        "request_id",\n        "text",\n        "ts"\n      ],\n      "additionalProperties": true\n    },\n    "reply": {\n      "title": "AgentChannelReply",\n      "type": "object",\n      "properties": {\n        "request_id": {\n          "type": "string",\n          "description": "Echoes the inbound request\'s request_id \\u2014 this is what a polling client matches its own request against."\n        },\n        "text": {\n          "type": "string",\n          "description": "The answer. This is the ONLY thing written to this file \\u2014 see error/ack semantics below for what happens when there is no answer to give."\n        },\n        "ts": {\n          "type": "string",\n          "description": "ISO 8601 UTC timestamp of when the reply was written.",\n          "format": "date-time"\n        },\n        "in_reply_to": {\n          "type": "string",\n          "description": "The inbound message\'s id (normally equal to request_id)."\n        }\n      },\n      "required": [\n        "request_id",\n        "text",\n        "ts",\n        "in_reply_to"\n      ],\n      "additionalProperties": true\n    }\n  },\n  "addressing": {\n    "agent": "You (the external agent) are addressed exclusively through the reply file at ~/messages/agent-replies/<request_id>.json, keyed by the request_id you generated. Nothing you send on this channel is ever shown to Dan directly \\u2014 chat_id on this channel is not a routing address, it is an inert required field.",\n    "dan": "Dan is addressed only through Telegram/Slack, a structurally separate code path (source=\\"telegram\\"/\\"slack\\", routed by his chat_id, not by any request_id). Your request can cause a subagent to also page Dan on Telegram/Slack \\u2014 but that is always a second, independent send_reply() call the subagent chooses to make; it is never a side effect of the reply addressed to you, and there is no field you can set on your request that causes it automatically. Conversely, nothing you say here reaches Dan\'s phone as passive notification noise unless a subagent explicitly decides to page him.",\n    "invariant": "A reply addressed to the Agent is architecturally incapable of reaching the Telegram/Slack outbox: the two are different files in different directories, selected by the source of the *original request*, verified fail-closed (see error/ack semantics) rather than trusted from a caller-supplied value."\n  },\n  "error_and_ack_semantics": [\n    {\n      "name": "single_shot_reply_slot",\n      "summary": "Exactly one reply is ever written per request_id, and it is immutable once written.",\n      "detail": "The reply file is created with a create-if-absent write (not an overwrite): whichever writer gets there first wins, and every later writer for the same request_id is a silent no-op from your point of view \\u2014 you will simply never see a second, different answer overwrite the first."\n    },\n    {\n      "name": "ack_is_not_answer",\n      "summary": "An ack is a distinct file, not a placeholder in the answer slot.",\n      "detail": "If you ever see <request_id>.ack.json appear before <request_id>.json, that is a progress note (\'received, working on it\'), not a final answer. It never counts toward, occupies, or blocks the single reply slot described above \\u2014 keep polling for <request_id>.json (without the .ack suffix) for the actual answer. An ack file may never appear at all; that is normal, not an error."\n    },\n    {\n      "name": "silence_is_a_sanctioned_failure",\n      "summary": "No reply ever arriving is a legitimate, distinguishable outcome \\u2014 not a bug to work around.",\n      "detail": "If Lobster crashes or never gets scheduled before your poll timeout, no reply file is ever written. You cannot distinguish \'still working\', \'crashed\', and \'never seen\' from the client side, and the protocol does not try to make you able to \\u2014 report a client-side timeout as \'no reply\', not as a specific failure reason you cannot actually verify. When Lobster *can* still respond after a failure, it writes one normal reply describing the failure in `text` \\u2014 there is no separate error envelope; failures are answers, delivered the same way successes are."\n    },\n    {\n      "name": "fail_closed_on_source_mismatch",\n      "summary": "If Lobster can\'t verify a reply\'s destination matches the request\'s source, it refuses to send rather than guesses.",\n      "detail": "This protects you structurally: a reply that was going to be misrouted is refused server-side (you see it as silence/timeout) rather than ever leaking to Dan\'s Telegram/Slack, or a stray reply from an unrelated request landing in your slot."\n    }\n  ]\n}'

_HELP_EPILOG = 'Protocol summary (see --schema for the full machine-readable form,\nor docs/reference/agent-channel-schema.md in the lobster repo for the prose version):\n\n  - Every request gets a unique request_id (auto-generated) that is also\n    its reply\'s filename — never reuse one across requests.\n  - Your reply appears at agent-replies/<request_id>.json, written at most\n    once. An intermediate agent-replies/<request_id>.ack.json, if you see\n    one, is a progress note, not the answer — keep polling.\n  - No reply within --timeout is a legitimate outcome (Lobster may still\n    be working, or may have crashed) — this CLI reports it as "no reply",\n    not as a specific failure, because it cannot tell the difference.\n  - This channel never reaches Dan\'s Telegram/Slack, and nothing you send\n    is shown to Dan unless Lobster separately decides to page him.'
# === END GENERATED SCHEMA ===


# request_id charset/length allowlist, mirrored from the schema's
# request_id_rules block above (^[A-Za-z0-9_-]+$, max 128) — used defensively
# when interpolating filenames *listed by the remote host itself* (the
# by-agent discovery directory) back into a second shell command, so a
# malformed or hostile entry in that directory can't smuggle shell syntax
# into the follow-up `cat`.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Agent-slug charset allowlist for the --for discovery flag and --agent
# label, per agent-channel-protocol-proposal.md §6 dial 4: case-fold to
# lowercase (case is not semantically load-bearing), then strict-reject
# everything outside request_id's own charset — no silent stripping or
# substitution of invalid characters.
_AGENT_SLUG_PATTERN = re.compile(r"^[a-z0-9_-]{1,128}$")


def normalize_agent_slug(agent: str) -> str:
    """Normalize an identity string into its by-agent directory slug.

    Case-folds to lowercase, then strict-validates against the same
    charset/length allowlist request_id already uses
    (^[A-Za-z0-9_-]{1,128}$, case-insensitive after folding). Raises
    ValueError (fail-closed) rather than silently stripping or substituting
    invalid characters — whitespace and special characters must fail loudly,
    per the protocol proposal's resolved Open Dial 4.
    """
    slug = agent.lower()
    if not _AGENT_SLUG_PATTERN.match(slug):
        raise ValueError(
            f"invalid agent identity {agent!r}: must be 1-128 characters "
            "matching [A-Za-z0-9_-] (case-insensitive, no whitespace)"
        )
    return slug


def _safe_json_loads(raw: str | None):
    """Parse `raw` as JSON, tolerating None/empty/malformed input.

    Returns None instead of raising for anything that isn't a clean JSON
    document — empty string, whitespace-only, absent file (empty stdout from
    `cat ... 2>/dev/null`), or malformed JSON. Every caller in this module
    treats "no parseable content yet" as a legitimate, non-error state: an
    ack file may never appear at all, and a by-agent pointer may reference a
    content file that hasn't been written yet — both are normal intermediate
    states, not corruption.
    """
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def parse_ack_status(raw_text: str | None) -> str | None:
    """Extract the progress-note text from a raw <request_id>.ack.json read.

    Pure function over the already-fetched file contents so the read loop's
    print-on-change logic (below) is testable without a real SSH round trip.
    Returns None when there is nothing to report: empty/missing file,
    malformed JSON, non-object payload, or an empty/missing `text` field.
    """
    ack = _safe_json_loads(raw_text)
    if not isinstance(ack, dict):
        return None
    text = ack.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text or None


def format_for_agent_entry(request_id: str, reply_raw: str | None, ack_raw: str | None) -> str:
    """Format one `--for <agent>` discovery entry as printable text.

    Pure function over the raw file contents already fetched via ssh, so the
    formatting logic is testable without a real SSH round trip. Prefers the
    terminal reply when present (it's the authoritative completion signal);
    falls back to the ack/progress text when the exchange is still open;
    falls back to a plain "nothing yet" line when neither file has landed —
    a bare pointer with no content file yet is a legitimate intermediate
    state (protocol proposal §3.2), not an error.
    """
    lines = [f"=== {request_id} ==="]
    reply = _safe_json_loads(reply_raw)
    reply_text = reply.get("text") if isinstance(reply, dict) else None
    if isinstance(reply_text, str) and reply_text.strip():
        lines.append(f"[reply] {reply_text.strip()}")
        return "\n".join(lines)

    ack_text = parse_ack_status(ack_raw)
    if ack_text:
        lines.append(f"[status] {ack_text} (no reply yet)")
    else:
        lines.append("(no reply or status yet)")
    return "\n".join(lines)


def ssh_run(target: str, remote_cmd: str, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", target, remote_cmd],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI's argument parser.

    Pure and side-effect-free (reads only os.environ for defaults) so tests
    can construct a parser and assert on its defaults/flags directly, without
    going through main()'s SSH round trip.
    """
    p = argparse.ArgumentParser(
        description="Chat with the Lobster dispatcher over SSH.",
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("text", nargs="*", help="Message to send")
    p.add_argument("--host", default=os.environ.get("LOBSTER_CHAT_HOST"))
    p.add_argument("--user", default=os.environ.get("LOBSTER_CHAT_USER", "lobster"))
    p.add_argument("--timeout", type=float, default=float(os.environ.get("LOBSTER_CHAT_TIMEOUT", 300)))
    p.add_argument("--interval", type=float, default=float(os.environ.get("LOBSTER_CHAT_INTERVAL", 2)))
    p.add_argument(
        "--agent",
        default=os.environ.get("LOBSTER_CHAT_AGENT"),
        help="Optional identity label (e.g. \"glyph\") shown in the dispatcher's inbox "
        "instead of a generic source label. Purely cosmetic — not used for routing.",
    )
    p.add_argument(
        "--schema",
        action="store_true",
        help="Print the full agent-channel protocol schema as JSON and exit (no SSH round trip). "
        "For an external agent with no other Lobster context: this is everything needed to "
        "construct a request and interpret a reply.",
    )
    p.add_argument(
        "--for",
        dest="for_agent",
        default=None,
        metavar="AGENT",
        help="Discovery mode: list and print everything waiting for AGENT in "
        "agent-replies/by-agent/<agent-slug>/ (both in-progress and completed "
        "exchanges), then exit. No new request is sent. AGENT is lowercase-"
        "normalized and strict-validated; invalid characters are rejected, "
        "not silently stripped.",
    )
    return p


def build_request_message(text: str, request_id: str, agent: str | None = None) -> dict:
    """Build the AgentChannelRequest envelope written to ~/messages/inbox/.

    Pure function: given the same inputs, always produces the same envelope
    (modulo the timestamp). `agent` is optional and omitted entirely from the
    envelope when falsy, matching the schema's "omit rather than send empty
    string" guidance (src/protocol/agent_channel_schema.py, INBOUND_ENVELOPE).
    """
    message = {
        "id": request_id,
        "source": "local-claude",
        "type": "text",
        "chat_id": "local-claude",
        "text": text,
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if agent:
        message["agent"] = agent
    return message


def run_for_agent_discovery(target: str, agent: str) -> int:
    """`--for <agent>` discovery mode: print everything waiting for `agent`.

    Read-only. Lists agent-replies/by-agent/<agent-slug>/ — small/zero-byte
    pointer files, one per request_id, written per the durability layer in
    agent-channel-protocol-proposal.md §3.2 — then cats each referenced
    request_id's reply/ack content. This is how a freshly-respawned
    collaborator session that only knows its own identity string (no
    request_id memory) recovers what was waiting for it.

    Client-only: this reads a path convention the spec defines
    (agent-replies/by-agent/<slug>/<request_id>) but does not require the
    server-side write path for that directory to exist yet — an empty or
    absent directory is handled the same way as "no messages found".
    """
    try:
        slug = normalize_agent_slug(agent)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    by_agent_dir = f"$HOME/messages/agent-replies/by-agent/{slug}"
    result = ssh_run(target, f'ls -1 "{by_agent_dir}/" 2>/dev/null')
    request_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    if result.returncode != 0 or not request_ids:
        print(f"No messages found for agent '{slug}'.", file=sys.stderr)
        return 0

    for request_id in request_ids:
        if not _REQUEST_ID_PATTERN.match(request_id):
            # Defensive: skip anything the remote directory listing returns
            # that isn't a well-formed request_id, rather than interpolating
            # it into the follow-up `cat` command below.
            print(f"Skipping malformed entry in by-agent dir: {request_id!r}", file=sys.stderr)
            continue
        reply_result = ssh_run(
            target, f'cat "$HOME/messages/agent-replies/{request_id}.json" 2>/dev/null'
        )
        ack_result = ssh_run(
            target, f'cat "$HOME/messages/agent-replies/{request_id}.ack.json" 2>/dev/null'
        )
        print(format_for_agent_entry(request_id, reply_result.stdout, ack_result.stdout))

    return 0


def main() -> int:
    p = build_parser()
    args = p.parse_args()

    if args.schema:
        print(_SCHEMA_JSON)
        return 0

    if args.for_agent:
        if not args.host:
            print("Error: no host set. Pass --host or set LOBSTER_CHAT_HOST.", file=sys.stderr)
            return 1
        return run_for_agent_discovery(f"{args.user}@{args.host}", args.for_agent)

    if not args.text:
        p.error("the following arguments are required: text (unless --schema or --for is given)")

    if not args.host:
        print("Error: no host set. Pass --host or set LOBSTER_CHAT_HOST.", file=sys.stderr)
        return 1

    target = f"{args.user}@{args.host}"
    text = " ".join(args.text)
    request_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    message = build_request_message(text, request_id, agent=args.agent)

    inbox_path = f"$HOME/messages/inbox/{request_id}.json"
    write_cmd = f'f="{inbox_path}"; cat > "$f.tmp" && mv "$f.tmp" "$f"'
    result = ssh_run(target, write_cmd, stdin_text=json.dumps(message))
    if result.returncode != 0:
        print(f"Error: failed to write inbox message: {result.stderr.strip()}", file=sys.stderr)
        return 1

    # Surface request_id unconditionally, right after the write succeeds —
    # not only in the timeout message below — so a dropped SSH connection or
    # a crashed CLI never loses the one value needed to check the reply file
    # manually later (agent-channel protocol spec: silence is a sanctioned
    # failure, but it shouldn't also be an unrecoverable one for the caller).
    print(f"request_id={request_id}", file=sys.stderr)

    reply_path = f"$HOME/messages/agent-replies/{request_id}.json"
    ack_path = f"$HOME/messages/agent-replies/{request_id}.ack.json"
    deadline = time.monotonic() + args.timeout
    last_ack_status: str | None = None
    while time.monotonic() < deadline:
        result = ssh_run(target, f'cat "{reply_path}" 2>/dev/null')
        if result.returncode == 0 and result.stdout.strip():
            try:
                reply = json.loads(result.stdout)
            except json.JSONDecodeError:
                time.sleep(args.interval)
                continue
            print(reply.get("text", "").strip())
            return 0

        # Protocol v1, Rung A: alongside the authoritative terminal-reply
        # check above, also poll the sibling .ack.json progress file and
        # print its status to stderr whenever it changes, so a long-running
        # exchange shows "working…" instead of silence. Print-on-change only
        # (dedupe repeated identical status) — this is purely observational
        # and never affects the loop's completion condition.
        ack_result = ssh_run(target, f'cat "{ack_path}" 2>/dev/null')
        if ack_result.returncode == 0:
            ack_status = parse_ack_status(ack_result.stdout)
            if ack_status and ack_status != last_ack_status:
                print(f"[status] {ack_status}", file=sys.stderr)
                last_ack_status = ack_status

        time.sleep(args.interval)

    print(
        f"No reply after {args.timeout:.0f}s (request_id={request_id}). "
        f"It may still arrive — check {target}:{reply_path}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
