"""
WOS Execute Router Daemon — Issue #940

Polls the Lobster inbox every POLL_INTERVAL_SECONDS for ``wos_execute`` messages
and routes them mechanically, with no LLM involvement.  This offloads zero-
reasoning routing from the dispatcher's primary LLM context window.

Architecture
------------
The daemon is a persistent systemd service.  On each poll cycle it:

1. Reads all messages in the inbox directory (pure file I/O, no MCP).
2. Filters to ``type == "wos_execute"`` client-side.
3. For each matching message:
   a. Status guard: checks the UoW's registry status.  If the status is not
      ACTIVE or READY_FOR_EXECUTOR (e.g. already EXECUTING), moves the message
      directly to processed/ without dispatching — prevents duplicate dispatch.
   b. Claims it by moving it from inbox/ to processing/ (mark_processing).
   c. Calls ``route_wos_message()`` — a pure function that returns a routing
      decision dict without spawning anything.
   d. If the decision is ``action == "spawn_subagent"``, dispatches via
      ``_dispatch_via_inbox()`` from ``executor.py`` (the canonical inbox
      dispatch path).
   e. Moves the message from processing/ to processed/ (mark_processed).
   f. On hard error: writes a system alert to inbox/ via inbox_write.py and
      moves the message to failed/.
4. If a ``send_reply`` action is returned (spawn-gate alert from the pure
   router), writes a ``subagent_result`` inbox message so the dispatcher can
   surface the alert to the user.

Gates
-----
- ``wos-config.json`` ``execution_enabled`` gate: if False, skip routing.
- ``MAX_AGENTS_GATE``: if active agent count >= threshold, defer by leaving
  messages unclaimed and trying again on the next cycle.
- ``_DISPATCHABLE_STATUSES`` status guard: if the UoW's registry status is not
  ACTIVE or READY_FOR_EXECUTOR, the message is discarded (moved to processed/)
  without dispatch.  Prevents the infinite re-emission loop where the router
  re-dispatches already-executing UoWs every poll cycle.

Awareness path
--------------
The daemon does NOT send a notification for every individual ``wos_execute``
processed.  It writes a ``subagent_result`` inbox message only for:
- Exception alerts (routing failure, claim failure, dispatch failure).
- ``send_reply`` actions returned by the spawn-gate circuit-breaker in
  ``route_wos_message``.

Routine dispatch is silent — the dispatcher learns the outcome when the
subagent calls ``write_result`` at completion.

Design reference: ~/lobster-workspace/workstreams/wos/design/wos-execute-router-daemon.md
Related issue: dcetlin/Lobster #940
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running as a standalone script or as an importable module
# ---------------------------------------------------------------------------

_DAEMON_FILE = Path(__file__).resolve()
_SRC_ROOT = _DAEMON_FILE.parent.parent      # src/
_REPO_ROOT = _SRC_ROOT.parent               # lobster/

for _p in [str(_REPO_ROOT), str(_SRC_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Lazy imports from orchestration (deferred until after path setup)
# ---------------------------------------------------------------------------

from orchestration.dispatcher_handlers import route_wos_message, read_wos_config  # noqa: E402
from orchestration.executor import _dispatch_via_inbox  # noqa: E402
from orchestration.registry import Registry as _Registry, UoWStatus  # noqa: E402
from agents.session_store import get_active_sessions  # noqa: E402
from utils.inbox_write import write_inbox_message  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS: int = int(os.environ.get("WOS_ROUTER_POLL_INTERVAL", "30"))

# Maximum number of concurrent active agents before deferring new dispatches.
# Mirrors the executor-heartbeat throttle logic.
MAX_AGENTS_GATE: int = int(os.environ.get("WOS_ROUTER_MAX_AGENTS", "8"))

ADMIN_CHAT_ID: int = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

_MESSAGES_BASE = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
INBOX_DIR: Path = _MESSAGES_BASE / "inbox"
PROCESSING_DIR: Path = _MESSAGES_BASE / "processing"
PROCESSED_DIR: Path = _MESSAGES_BASE / "processed"
FAILED_DIR: Path = _MESSAGES_BASE / "failed"

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
LOG_FILE: Path = _WORKSPACE / "logs" / "wos-execute-router.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """Configure logging to both stderr and the daemon log file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s %(levelname)s [wos-router] %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    try:
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        handlers.append(fh)
    except OSError as exc:
        # Log file unavailable — stderr only, not fatal
        logging.warning("wos-execute-router: could not open log file %s: %s", LOG_FILE, exc)

    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt, handlers=handlers)


log = logging.getLogger("wos-execute-router")

# ---------------------------------------------------------------------------
# Signal handling — graceful shutdown on SIGTERM / SIGINT
# ---------------------------------------------------------------------------

_shutdown_requested: bool = False


def _handle_shutdown_signal(signum: int, _frame: object) -> None:
    global _shutdown_requested
    log.info("Received signal %d — shutting down after current cycle", signum)
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Pure helpers — inbox file I/O (no MCP dependency)
# ---------------------------------------------------------------------------

def _read_inbox_messages() -> list[dict]:
    """
    Return all parseable messages from the inbox directory.

    Mirrors the file-read logic in inbox_server.py handle_check_inbox().
    Returns an empty list (never raises) so a malformed file never crashes
    the poll cycle.
    """
    messages: list[dict] = []
    INBOX_DIR.mkdir(parents=True, exist_ok=True)

    for f in sorted(INBOX_DIR.glob("*.json")):
        try:
            msg = json.loads(f.read_text(encoding="utf-8"))
            msg["_filepath"] = str(f)
            messages.append(msg)
        except (json.JSONDecodeError, OSError):
            continue

    return messages


def _filter_wos_execute(messages: list[dict]) -> list[dict]:
    """Return only messages with type='wos_execute'."""
    return [m for m in messages if m.get("type") == "wos_execute"]


def _claim_message(msg: dict) -> bool:
    """
    Move message from inbox/ to processing/ (mark_processing equivalent).

    Returns True on success, False if the message was already claimed by
    another process (race condition) or could not be moved.
    """
    filepath = msg.get("_filepath")
    if not filepath:
        log.warning("claim: no _filepath in message %s — skipping", msg.get("id", "?"))
        return False

    src = Path(filepath)
    if not src.exists():
        # Already claimed by another process — expected in race conditions
        log.debug("claim: %s no longer in inbox — already claimed", src.name)
        return False

    PROCESSING_DIR.mkdir(parents=True, exist_ok=True)
    dest = PROCESSING_DIR / src.name

    # Stamp a _processing_started_at field into the message before moving so
    # the wos-execute-gate hook (issue #855) can verify mark_processing occurred.
    try:
        content = json.loads(src.read_text(encoding="utf-8"))
        content["_processing_started_at"] = datetime.now(timezone.utc).isoformat()
        tmp = src.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(src)
    except OSError as exc:
        log.warning("claim: could not stamp _processing_started_at on %s: %s", src.name, exc)
        # Non-fatal — continue with move even if stamp failed

    try:
        src.rename(dest)
        log.debug("claim: moved %s -> processing/", src.name)
        return True
    except OSError as exc:
        log.warning("claim: could not move %s to processing/: %s", src.name, exc)
        return False


def _release_message_processed(message_id: str) -> None:
    """
    Move message from processing/ to processed/ (mark_processed equivalent).

    Locates the file by message_id prefix in processing/, then renames it.
    Logs a warning if the file cannot be found.
    """
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    src = _find_in_dir(PROCESSING_DIR, message_id)
    if src is None:
        log.warning("mark_processed: %s not found in processing/", message_id)
        return

    dest = PROCESSED_DIR / src.name
    try:
        src.rename(dest)
        log.debug("mark_processed: moved %s -> processed/", src.name)
    except OSError as exc:
        log.error("mark_processed: failed to move %s: %s", src.name, exc)


def _release_message_failed(message_id: str) -> None:
    """Move message from processing/ to failed/ on unrecoverable error."""
    FAILED_DIR.mkdir(parents=True, exist_ok=True)

    src = _find_in_dir(PROCESSING_DIR, message_id)
    if src is None:
        # Try inbox/ as fallback (claim may not have succeeded)
        src = _find_in_dir(INBOX_DIR, message_id)
    if src is None:
        log.warning("mark_failed: %s not found in processing/ or inbox/", message_id)
        return

    dest = FAILED_DIR / src.name
    try:
        src.rename(dest)
        log.debug("mark_failed: moved %s -> failed/", src.name)
    except OSError as exc:
        log.error("mark_failed: failed to move %s: %s", src.name, exc)


def _find_in_dir(directory: Path, message_id: str) -> Path | None:
    """Return the first .json file in directory whose stem starts with message_id."""
    for f in directory.glob("*.json"):
        if f.stem == message_id or f.name.startswith(message_id):
            return f
    return None


def _discard_inbox_message(msg: dict) -> None:
    """
    Move a message directly from inbox/ to processed/ without claiming it.

    Used by the status guard to discard duplicate wos_execute messages for
    UoWs that are already executing (or otherwise not dispatchable).  Bypasses
    the claim/processing step so the message does not linger in processing/.

    Locates the file by ``_filepath`` key stamped by ``_read_inbox_messages``.
    Logs a warning but does not raise if the file is missing (another process
    may have claimed it in a race condition).
    """
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    filepath = msg.get("_filepath")
    if not filepath:
        log.warning("discard: no _filepath in message %s — cannot discard", msg.get("id", "?"))
        return

    src = Path(filepath)
    if not src.exists():
        log.debug("discard: %s no longer in inbox — already claimed or discarded", src.name)
        return

    dest = PROCESSED_DIR / src.name
    try:
        src.rename(dest)
        log.debug("discard: moved %s -> processed/ (status guard)", src.name)
    except OSError as exc:
        log.warning("discard: could not move %s to processed/: %s", src.name, exc)


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------

def _execution_enabled() -> bool:
    """Return True if wos-config.json has execution_enabled=true."""
    config = read_wos_config()
    return bool(config.get("execution_enabled", False))


def _active_agent_count() -> int:
    """Return the number of currently active (running/starting) agents."""
    try:
        sessions = get_active_sessions()
        return len(sessions)
    except Exception as exc:
        log.warning("active_agent_count: could not read session store: %s — assuming 0", exc)
        return 0


# Statuses that indicate a UoW is ready to be dispatched by the router.
# A wos_execute message whose UoW is NOT in one of these statuses must be
# discarded (marked processed without spawning), because:
#
# - UoWStatus.ACTIVE: executor_heartbeat has claimed the UoW and is in the
#   middle of the claim/dispatch transaction; dispatch is imminent and this
#   message represents the in-flight request — safe to route.
#
# - UoWStatus.READY_FOR_EXECUTOR: the UoW is waiting to be claimed; this
#   message is the initial dispatch request — safe to route.
#
# Any other status (EXECUTING, DONE, FAILED, READY_FOR_STEWARD, ...) means
# the UoW has already been dispatched or has completed — routing again would
# create a duplicate subagent and is the root cause of the observed loop.
_DISPATCHABLE_STATUSES: frozenset[UoWStatus] = frozenset({
    UoWStatus.ACTIVE,
    UoWStatus.READY_FOR_EXECUTOR,
})


def _uow_status_allows_dispatch(uow_id: str) -> bool:
    """
    Return True if the UoW's registry status permits dispatch.

    Queries the registry for the UoW's current status.  Returns True only when
    the status is in ``_DISPATCHABLE_STATUSES``; returns False for any other
    status (including EXECUTING, DONE, FAILED, etc.) and when the UoW is not
    found.

    On registry read error the function returns True (fail-open) so a transient
    DB hiccup does not permanently suppress a legitimate dispatch.  The error
    is logged at WARNING level.

    The fail-open posture is the safer default here: a false-positive dispatch
    (re-routing a genuinely in-flight UoW) is visible and recoverable via the
    existing TTL/sweep path; a false-negative (suppressing a legitimate dispatch)
    would silently stall the UoW with no recovery signal.
    """
    try:
        reg = _Registry()
        uow = reg.get(uow_id)
    except Exception as exc:
        log.warning(
            "status_guard: registry read failed for uow_id=%s — failing open: %s",
            uow_id, exc,
        )
        return True  # fail-open: allow dispatch on transient error

    if uow is None:
        # UoW not in registry — the message may reference a stale or deleted UoW.
        # Suppress dispatch (fail-closed for unknown UoWs — nothing to recover).
        log.info(
            "status_guard: uow_id=%s not found in registry — discarding message",
            uow_id,
        )
        return False

    if uow.status in _DISPATCHABLE_STATUSES:
        return True

    log.info(
        "status_guard: uow_id=%s has status=%s — not in dispatchable set %s; "
        "discarding duplicate wos_execute message",
        uow_id,
        uow.status,
        {s.value for s in _DISPATCHABLE_STATUSES},
    )
    return False


# ---------------------------------------------------------------------------
# Awareness path — inter-process notifications
# ---------------------------------------------------------------------------

def _write_alert(text: str) -> None:
    """
    Write a system alert to the inbox so the dispatcher can surface it.

    Uses write_inbox_message from inbox_write.py — the same mechanism used by
    executor-heartbeat and steward-heartbeat.  write_result is NOT used here
    because that requires a Claude session context this daemon does not have.
    """
    try:
        msg_id = write_inbox_message(
            job_name="wos-router-alert",
            chat_id=ADMIN_CHAT_ID,
            text=text,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        log.info("alert written to inbox: %s", msg_id)
    except Exception as exc:
        log.error("failed to write alert to inbox: %s", exc)


def _write_send_reply_alert(text: str) -> None:
    """
    Write a subagent_result inbox message for a send_reply alert from
    route_wos_message's spawn-gate circuit-breaker.

    The dispatcher picks this up and surfaces it to the user.
    """
    _write_alert(f"[WOS spawn-gate] {text}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# _dispatch_via_inbox is imported from orchestration.executor at the top of
# this module.  It writes a wos_execute message to ~/messages/inbox/ and
# returns immediately (fire-and-forget).  The Lobster dispatcher picks it up
# on its next cycle and spawns a background subagent via the Task tool.
#
# agent_type is extracted from the routing decision returned by
# route_wos_message and forwarded so the dispatcher can spawn the correct
# subagent type.  Defaults to "functional-engineer" when absent.

def _dispatch(
    instructions: str,
    uow_id: str,
    agent_type: str = "functional-engineer",
) -> str:
    """
    Dispatch a subagent via the canonical inbox path.

    Delegates to ``_dispatch_via_inbox`` from ``orchestration.executor``,
    which writes a ``wos_execute`` JSON message to ``~/messages/inbox/``
    and returns a message_id for audit correlation.  The Lobster dispatcher
    (main Claude loop) picks up the message and spawns the subagent via the
    Task tool — no subprocess is spawned here.

    The ``agent_type`` parameter is forwarded to ``_dispatch_via_inbox`` so
    the dispatcher can select the correct subagent type (e.g.
    ``"lobster-meta"`` for philosophical UoWs,
    ``"functional-engineer"`` for implementation tasks).

    Returns the inbox message_id (a UUID string) for audit correlation.

    Raises ``OSError`` if the inbox directory cannot be created or the
    message file cannot be written.
    """
    msg_id = _dispatch_via_inbox(instructions, uow_id, agent_type=agent_type)
    log.info(
        "dispatch: wrote wos_execute to inbox for uow_id=%s msg_id=%s agent_type=%s",
        uow_id, msg_id, agent_type,
    )
    return msg_id


# ---------------------------------------------------------------------------
# Core routing logic
# ---------------------------------------------------------------------------

def _route_single_message(msg: dict) -> None:
    """
    Route a single wos_execute message end-to-end.

    Status guard → Claim → route → dispatch (if spawn_subagent) → mark_processed.
    On failure: mark_failed + write alert.

    All exceptions are caught and logged — never propagated to the poll loop.

    Status guard: if the UoW's registry status is not in ``_DISPATCHABLE_STATUSES``
    (i.e. it is already EXECUTING, DONE, FAILED, etc.), the message is discarded
    by moving it to processed/ without dispatching.  This prevents the infinite
    re-emission loop where the router repeatedly re-dispatches an already-executing
    UoW every poll cycle.
    """
    message_id: str = msg.get("id", "")
    uow_id: str = msg.get("uow_id", "?")

    if not message_id:
        log.warning("route: message has no id field — skipping: %r", msg)
        return

    # -----------------------------------------------------------------------
    # Status guard (loop-prevention): skip dispatch if UoW is already in-flight.
    # A wos_execute message for a UoW that is already EXECUTING (or in any
    # non-dispatchable status) is a duplicate produced by a prior router cycle.
    # Claiming and re-dispatching it would spawn another subagent for the same
    # UoW, compound inbox flooding, and prevent natural TTL/sweep recovery.
    #
    # The guard runs BEFORE claim so the message file is never moved to
    # processing/ for duplicate messages — it moves directly to processed/.
    # -----------------------------------------------------------------------
    if not _uow_status_allows_dispatch(uow_id):
        log.info(
            "route: skipping duplicate wos_execute for uow_id=%s message_id=%s "
            "(UoW status not dispatchable — discarding without spawning)",
            uow_id, message_id,
        )
        # Move directly from inbox to processed — bypass claim/processing
        _discard_inbox_message(msg)
        return

    log.info("route: claiming wos_execute message_id=%s uow_id=%s", message_id, uow_id)

    if not _claim_message(msg):
        # Race condition — another process already claimed it; skip silently
        log.info("route: %s already claimed — skipping", message_id)
        return

    try:
        decision = route_wos_message(msg)
    except Exception as exc:
        log.error("route: route_wos_message raised for %s: %s", message_id, exc)
        _release_message_failed(message_id)
        _write_alert(
            f"wos-execute-router: route_wos_message raised an exception for "
            f"message_id={message_id} uow_id={uow_id}: {type(exc).__name__}: {exc}"
        )
        return

    action = decision.get("action")

    if action == "spawn_subagent":
        task_id: str = decision.get("task_id", f"wos-{uow_id}")
        prompt: str = decision.get("prompt", "")
        agent_type: str = decision.get("agent_type", "functional-engineer")
        # route_wos_message returns task_id as "wos-{uow_id}"; strip prefix
        # to get the bare uow_id expected by _dispatch_via_inbox
        bare_uow_id = task_id.removeprefix("wos-")

        log.info("route: dispatching subagent for uow_id=%s task_id=%s agent_type=%s", uow_id, task_id, agent_type)
        try:
            msg_id = _dispatch(instructions=prompt, uow_id=bare_uow_id, agent_type=agent_type)
            log.info("route: dispatch succeeded msg_id=%s uow_id=%s", msg_id, uow_id)
        except Exception as exc:
            log.error("route: _dispatch raised for %s: %s", uow_id, exc)
            _release_message_failed(message_id)
            _write_alert(
                f"wos-execute-router: inbox dispatch failed for "
                f"message_id={message_id} uow_id={uow_id}: {type(exc).__name__}: {exc}"
            )
            return

    elif action == "send_reply":
        # Spawn-gate circuit-breaker in route_wos_message fired — surface the
        # alert to the dispatcher via inbox
        alert_text = decision.get("text", "spawn-gate alert (no text)")
        log.error("route: spawn-gate alert for %s: %s", message_id, alert_text)
        _write_send_reply_alert(alert_text)

    else:
        log.error(
            "route: unexpected action=%r for %s — marking failed",
            action, message_id,
        )
        _release_message_failed(message_id)
        _write_alert(
            f"wos-execute-router: unexpected routing action {action!r} for "
            f"message_id={message_id} uow_id={uow_id}"
        )
        return

    _release_message_processed(message_id)
    log.info("route: completed message_id=%s uow_id=%s", message_id, uow_id)


def run_poll_cycle() -> int:
    """
    Execute one poll cycle: check gates, scan inbox, route wos_execute messages.

    Returns the number of messages routed (0 if gated out or no messages found).
    This is the primary unit-testable entry point for the routing loop.
    """
    if not _execution_enabled():
        log.debug("poll: execution_enabled=false — skipping")
        return 0

    active_count = _active_agent_count()
    if active_count >= MAX_AGENTS_GATE:
        log.info(
            "poll: active_agents=%d >= MAX_AGENTS_GATE=%d — deferring",
            active_count, MAX_AGENTS_GATE,
        )
        return 0

    messages = _read_inbox_messages()
    wos_messages = _filter_wos_execute(messages)

    if not wos_messages:
        log.debug("poll: no wos_execute messages in inbox")
        return 0

    log.info("poll: found %d wos_execute message(s)", len(wos_messages))

    for msg in wos_messages:
        if _shutdown_requested:
            log.info("poll: shutdown requested — stopping mid-batch")
            break
        _route_single_message(msg)

    return len(wos_messages)


# ---------------------------------------------------------------------------
# Daemon main loop
# ---------------------------------------------------------------------------

def run_daemon() -> None:
    """
    Run the routing daemon loop indefinitely.

    Polls every POLL_INTERVAL_SECONDS.  Exits cleanly on SIGTERM or SIGINT.
    Exceptions in the poll cycle are logged and the loop continues —
    a transient error never kills the daemon.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    log.info(
        "wos-execute-router starting: poll_interval=%ds max_agents=%d",
        POLL_INTERVAL_SECONDS, MAX_AGENTS_GATE,
    )

    while not _shutdown_requested:
        try:
            run_poll_cycle()
        except Exception as exc:
            log.error("poll cycle raised unexpected exception: %s", exc, exc_info=True)

        if _shutdown_requested:
            break

        time.sleep(POLL_INTERVAL_SECONDS)

    log.info("wos-execute-router stopped")


if __name__ == "__main__":
    _configure_logging()
    run_daemon()
