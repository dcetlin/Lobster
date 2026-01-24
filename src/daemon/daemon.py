#!/usr/bin/env python3
"""
Hyperion Daemon - Always-on Claude Code message processor

This daemon monitors the inbox and invokes Claude to process messages.
Uses --resume to maintain a persistent session with full context.
"""

import asyncio
import json
import logging
import signal
import sys
import time
import uuid
from pathlib import Path

# Configuration
INBOX_DIR = Path.home() / "messages" / "inbox"
WORKSPACE = Path.home() / "hyperion-workspace"
LOG_DIR = WORKSPACE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "daemon.log"
SESSION_ID_FILE = WORKSPACE / ".hyperion_session_id"

POLL_INTERVAL = 5  # seconds between checks
IDLE_POLL_INTERVAL = 10  # seconds when no messages
CLAUDE_TIMEOUT = 300  # 5 minutes max per invocation

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("hyperion")

# Graceful shutdown support
shutdown_requested = False


def handle_sigterm(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    global shutdown_requested
    shutdown_requested = True
    log.info("Shutdown requested (SIGTERM), finishing current work...")


def handle_sigint(signum, frame):
    """Handle SIGINT (Ctrl+C) for graceful shutdown."""
    global shutdown_requested
    shutdown_requested = True
    log.info("Shutdown requested (SIGINT), finishing current work...")


# Register signal handlers
signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigint)


def get_or_create_session_id() -> str:
    """Get existing session ID or create a new one."""
    if SESSION_ID_FILE.exists():
        session_id = SESSION_ID_FILE.read_text().strip()
        if session_id:
            return session_id

    # Generate new UUID4 session ID
    session_id = str(uuid.uuid4())
    SESSION_ID_FILE.write_text(session_id)
    log.info(f"Created new session ID: {session_id}")
    return session_id


def session_has_been_used() -> bool:
    """Check if a session has been successfully used before."""
    marker = WORKSPACE / ".hyperion_session_used"
    return marker.exists()


def mark_session_used():
    """Mark that the session has been successfully used."""
    marker = WORKSPACE / ".hyperion_session_used"
    marker.write_text(str(time.time()))


def count_inbox_messages() -> int:
    """Count messages in inbox."""
    return len(list(INBOX_DIR.glob("*.json")))


def get_inbox_messages() -> list[dict]:
    """Read all messages from inbox."""
    messages = []
    for f in sorted(INBOX_DIR.glob("*.json")):
        try:
            messages.append(json.loads(f.read_text()))
        except Exception as e:
            log.warning(f"Failed to read {f}: {e}")
    return messages


async def process_messages() -> tuple[bool, str]:
    """
    Invoke Claude to process inbox messages.
    Uses --resume to maintain persistent session context.
    """
    prompt = """You are Hyperion. Check your inbox and process all messages.

For each message:
1. Use check_inbox to see the messages
2. Read and understand what the user wants
3. Compose a helpful, concise response (users are on mobile)
4. Use send_reply with the correct chat_id
5. Use mark_processed to clear the message

Process ALL messages in the inbox."""

    session_id = get_or_create_session_id()

    # Build command - use --resume to continue the persistent session
    if session_has_been_used():
        # Continue existing session with --resume <session-id>
        cmd = [
            "claude",
            "--resume", session_id,
            "-p", prompt,
            "--dangerously-skip-permissions",
        ]
        log.info(f"Invoking Claude (resuming {session_id[:8]}...)...")
    else:
        # First invocation - create session with known ID
        cmd = [
            "claude",
            "--session-id", session_id,
            "-p", prompt,
            "--dangerously-skip-permissions",
        ]
        log.info(f"Invoking Claude (new session {session_id[:8]}...)...")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=CLAUDE_TIMEOUT
        )

        output = stdout.decode().strip()
        errors = stderr.decode().strip()

        if proc.returncode != 0:
            log.error(f"Claude error (code {proc.returncode}): {errors}")
            return False, errors

        # Mark session as used after first successful run
        if not session_has_been_used():
            mark_session_used()
            log.info("Session marked for future resumes")

        log.info(f"Claude completed: {output[:200]}...")
        return True, output

    except asyncio.TimeoutError:
        log.error(f"Claude timed out after {CLAUDE_TIMEOUT}s")
        try:
            proc.kill()
        except:
            pass
        return False, "Timeout"

    except Exception as e:
        log.exception(f"Error invoking Claude: {e}")
        return False, str(e)


async def daemon_loop():
    """Main daemon loop."""
    global shutdown_requested

    log.info("=" * 60)
    log.info("Hyperion Daemon starting...")
    log.info(f"Workspace: {WORKSPACE}")
    log.info(f"Inbox: {INBOX_DIR}")
    log.info("=" * 60)

    # Ensure directories exist
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)

    consecutive_errors = 0
    max_errors = 5

    while not shutdown_requested:
        loop_start = time.time()

        try:
            msg_count = count_inbox_messages()

            if msg_count > 0:
                log.info(f"📬 {msg_count} message(s) in inbox")

                success, output = await process_messages()

                if success:
                    consecutive_errors = 0
                    remaining = count_inbox_messages()
                    processed = msg_count - remaining
                    log.info(f"✅ Processed {processed}, {remaining} remaining")
                else:
                    consecutive_errors += 1
                    log.warning(f"⚠️ Failed ({consecutive_errors}/{max_errors})")

                    if consecutive_errors >= max_errors:
                        log.error("Too many errors. Sleeping 60s...")
                        await asyncio.sleep(60)
                        consecutive_errors = 0

                poll = POLL_INTERVAL
            else:
                poll = IDLE_POLL_INTERVAL

        except Exception as e:
            log.exception(f"Loop error: {e}")
            consecutive_errors += 1
            poll = POLL_INTERVAL

        elapsed = time.time() - loop_start
        sleep_time = max(poll - elapsed, 1)
        await asyncio.sleep(sleep_time)

    log.info("Shutdown complete - daemon exiting gracefully")


def main():
    """Entry point."""
    try:
        asyncio.run(daemon_loop())
    except KeyboardInterrupt:
        log.info("Daemon stopped by user")
    except Exception as e:
        log.exception(f"Daemon crashed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
