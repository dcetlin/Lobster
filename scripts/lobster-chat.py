#!/usr/bin/env python3
"""
lobster-chat — talk to the Lobster dispatcher from your local machine over SSH.

RUNS ON YOUR LOCAL MACHINE (laptop), not on the VPS. It never touches the
lobster-mcp-local server directly; it only reads/writes files under
~/messages/ on the remote host via `ssh`, using the same "agent channel"
inbox/agent-replies contract the dispatcher speaks natively (see
docs/agent-channel.md).

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

Config (env var or flag, flag wins):
    --host / LOBSTER_CHAT_HOST       VPS hostname (required)
    --user / LOBSTER_CHAT_USER       SSH user (default: lobster)
    --timeout / LOBSTER_CHAT_TIMEOUT   seconds to wait for a reply (default: 90)
    --interval / LOBSTER_CHAT_INTERVAL  poll interval in seconds (default: 2)
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone


def ssh_run(target: str, remote_cmd: str, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", target, remote_cmd],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Chat with the Lobster dispatcher over SSH.")
    p.add_argument("text", nargs="+", help="Message to send")
    p.add_argument("--host", default=os.environ.get("LOBSTER_CHAT_HOST"))
    p.add_argument("--user", default=os.environ.get("LOBSTER_CHAT_USER", "lobster"))
    p.add_argument("--timeout", type=float, default=float(os.environ.get("LOBSTER_CHAT_TIMEOUT", 90)))
    p.add_argument("--interval", type=float, default=float(os.environ.get("LOBSTER_CHAT_INTERVAL", 2)))
    args = p.parse_args()

    if not args.host:
        print("Error: no host set. Pass --host or set LOBSTER_CHAT_HOST.", file=sys.stderr)
        return 1

    target = f"{args.user}@{args.host}"
    text = " ".join(args.text)
    request_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"

    message = {
        "id": request_id,
        "source": "local-claude",
        "type": "text",
        "chat_id": "local-claude",
        "text": text,
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    inbox_path = f"~/messages/inbox/{request_id}.json"
    write_cmd = f'f="{inbox_path}"; cat > "$f.tmp" && mv "$f.tmp" "$f"'
    result = ssh_run(target, write_cmd, stdin_text=json.dumps(message))
    if result.returncode != 0:
        print(f"Error: failed to write inbox message: {result.stderr.strip()}", file=sys.stderr)
        return 1

    reply_path = f"~/messages/agent-replies/{request_id}.json"
    deadline = time.monotonic() + args.timeout
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
        time.sleep(args.interval)

    print(
        f"No reply after {args.timeout:.0f}s (request_id={request_id}). "
        f"It may still arrive — check {target}:{reply_path}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
