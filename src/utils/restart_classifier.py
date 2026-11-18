"""
Restart type discriminator for the Lobster dispatcher (issue #1561).

Three mutually exclusive restart types exist:

  COMPACTION     — context was compacted; compact-catchup IS needed.
  SESSION_ROTATION — the Claude Code process was replaced (session rotation,
                     health-check restart) but the MCP server wrote a
                     session_reconnect message indicating the reconnect is
                     lightweight. Compact-catchup is NOT needed; the startup
                     protocol's step 4 (startup-catchup) handles any context
                     gap.
  FRESH_RESTART  — a genuine cold start where compact-catchup IS needed
                   (and no session_reconnect contradicts it).

This module is dependency-free so it can be imported and tested without
pulling in the full MCP stack. The classifier is a pure function: all
signals are passed as arguments, not read from the filesystem.
"""

from enum import StrEnum


class RestartType(StrEnum):
    """Discriminator for dispatcher restart classification."""

    COMPACTION = "compaction"
    SESSION_ROTATION = "session_rotation"
    FRESH_RESTART = "fresh_restart"


def classify_restart(
    *,
    startup_cause: str,
    session_reconnect_queued: bool,
) -> RestartType:
    """Classify the dispatcher restart from two code-level signals.

    Parameters
    ----------
    startup_cause:
        The value read from ``last-startup-cause.json`` by
        ``inject-bootup-context.py`` and injected as the
        ``<!-- startup-cause: ... -->`` banner. One of ``"compaction"``
        or ``"restart"`` (the only values ``read_and_reset_startup_cause``
        returns).

    session_reconnect_queued:
        True when at least one ``type: "session_reconnect"`` message
        exists in ``~/messages/inbox/`` at the time of classification.
        The MCP server writes this message via
        ``_write_session_lost_reminder()`` when it starts and finds
        the dispatcher PID dead — signalling that the reconnect is
        lightweight and compact-catchup is unnecessary.

    Returns
    -------
    RestartType
        The classification that determines whether compact-catchup
        should be spawned.

    Priority rules
    --------------
    1. ``startup_cause == "compaction"`` always wins → COMPACTION.
       on-compact.py wrote the cause file within the last 5 minutes;
       the signal is authoritative regardless of other inbox contents.

    2. ``session_reconnect_queued`` and cause is not compaction →
       SESSION_ROTATION.  The MCP server's session_reconnect is a
       code-level signal that the reconnect is lightweight. This
       takes precedence over any safety-net compact-reminder that
       ``on-fresh-start.py`` might inject, resolving the conflicting-
       signal problem described in issue #1561.

    3. Otherwise → FRESH_RESTART.  No contradicting signal exists;
       compact-catchup should proceed.
    """
    if startup_cause == "compaction":
        return RestartType.COMPACTION

    if session_reconnect_queued:
        return RestartType.SESSION_ROTATION

    return RestartType.FRESH_RESTART
