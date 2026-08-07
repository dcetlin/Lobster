"""
Unit tests for the PID-based reconnect guard in _write_session_lost_reminder()
(issue #1429).

Root cause: the old guard checked lobster-state.json mtime (< 30 min) to detect
mid-session MCP reconnects.  Cron jobs that update the state file seconds before
an MCP restart caused false negatives — the guard saw a fresh file and suppressed
the session-lost reminder even though the dispatcher was actually dead.

Fix: check whether the dispatcher PID (from ~/messages/config/dispatcher.pid) is
alive via kill -0.  If the PID is alive → true reconnect → suppress.  If the PID
is absent or dead → real session loss → write the reminder.

These tests exercise _is_dispatcher_alive() in isolation (by loading the function
from inbox_server.py source) and verify the full _write_session_lost_reminder()
flow via the inbox_server module (using a minimal patched environment).

Also covers the compact-reminder dedup fix (postbounce #5): the reminder now
carries type="session_reconnect" instead of reusing type="compact-reminder",
so the dispatcher routes it through a lightweight re-orient handler instead of
unconditionally spawning the 10-15 min compact-catchup subagent.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers to extract _is_dispatcher_alive() from inbox_server.py source
# ---------------------------------------------------------------------------

_SRC_MCP_DIR = Path(__file__).parents[2] / "src" / "mcp"
_SERVER_PATH = _SRC_MCP_DIR / "inbox_server.py"

# Named constant matching the spec.
DISPATCHER_PID_FILENAME = "dispatcher.pid"


def _extract_is_dispatcher_alive(dispatcher_pid_path: Path):
    """Extract _is_dispatcher_alive() from inbox_server.py source into a callable.

    Returns a namespace dict containing the function.
    """
    src = _SERVER_PATH.read_text()

    start_marker = "def _is_dispatcher_alive("
    start_idx = src.find(start_marker)
    assert start_idx != -1, "_is_dispatcher_alive() not found in inbox_server.py"

    # Find the next top-level def/class after this function
    lines = src[start_idx:].split("\n")
    end_line = len(lines)
    for i, line in enumerate(lines[1:], 1):
        if line and line[0] not in (" ", "\t", "\n", "#", ""):
            end_line = i
            break

    snippet = "\n".join(lines[:end_line])

    ns = {
        "os": os,
        "Path": Path,
        "DISPATCHER_PID_FILE": dispatcher_pid_path,
        "log": _FakeLogger(),
    }
    exec(compile(snippet, "<is_dispatcher_alive>", "exec"), ns)
    return ns


class _FakeLogger:
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def debug(self, *a, **kw): pass


# ---------------------------------------------------------------------------
# Tests for _is_dispatcher_alive()
# ---------------------------------------------------------------------------

class TestIsDispatcherAlive:
    """_is_dispatcher_alive() — pure function tests."""

    def test_returns_false_when_pid_file_absent(self, tmp_path):
        """No dispatcher.pid → treat as real session loss → return False."""
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        # File does not exist
        ns = _extract_is_dispatcher_alive(pid_file)
        assert ns["_is_dispatcher_alive"]() is False

    def test_returns_true_when_pid_is_alive(self, tmp_path):
        """PID file contains a live process PID → true reconnect → return True."""
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        # os.getpid() is always alive
        pid_file.write_text(str(os.getpid()))
        ns = _extract_is_dispatcher_alive(pid_file)
        assert ns["_is_dispatcher_alive"]() is True

    def test_returns_false_when_pid_is_dead(self, tmp_path):
        """PID file contains a dead process PID → real session loss → return False."""
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        proc = subprocess.Popen(["true"])
        proc.wait()
        dead_pid = proc.pid
        pid_file.write_text(str(dead_pid))
        ns = _extract_is_dispatcher_alive(pid_file)
        assert ns["_is_dispatcher_alive"]() is False

    def test_returns_false_when_pid_file_is_empty(self, tmp_path):
        """Empty PID file → cannot parse → treat as dead → return False."""
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        pid_file.write_text("")
        ns = _extract_is_dispatcher_alive(pid_file)
        assert ns["_is_dispatcher_alive"]() is False

    def test_returns_false_when_pid_file_has_non_numeric_content(self, tmp_path):
        """Corrupt PID file → cannot parse → treat as dead → return False."""
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        pid_file.write_text("not-a-pid\n")
        ns = _extract_is_dispatcher_alive(pid_file)
        assert ns["_is_dispatcher_alive"]() is False

    def test_returns_false_when_pid_file_has_zero(self, tmp_path):
        """PID 0 is invalid — kill -0 0 is unsafe → return False."""
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        pid_file.write_text("0")
        ns = _extract_is_dispatcher_alive(pid_file)
        assert ns["_is_dispatcher_alive"]() is False


# ---------------------------------------------------------------------------
# Tests for _write_session_lost_reminder() via inbox_server module
# Uses a lightweight approach: patch the module-level globals to inject test paths.
# ---------------------------------------------------------------------------

def _call_write_session_lost_reminder(
    *,
    inbox_dir: Path,
    dispatcher_pid_path: Path,
    state_file: Path,
    dev_mode: str = "",
):
    """Call _write_session_lost_reminder() with injected test paths.

    Patches INBOX_DIR, DISPATCHER_PID_FILE, and LOBSTER_STATE_FILE at the
    module level for the duration of the call.
    """
    import sys
    import importlib

    # Set or clear LOBSTER_DEV_MODE
    old_dev = os.environ.get("LOBSTER_DEV_MODE")
    if dev_mode:
        os.environ["LOBSTER_DEV_MODE"] = dev_mode
    else:
        os.environ.pop("LOBSTER_DEV_MODE", None)

    try:
        # We need to import the module but it may be already imported.
        # Find it in sys.modules if present.
        mod = sys.modules.get("inbox_server")
        if mod is None:
            # Not loaded — add the src/mcp path and import
            sys.path.insert(0, str(_SRC_MCP_DIR))
            mod = importlib.import_module("inbox_server")

        # Patch the module-level globals
        with (
            patch.object(mod, "INBOX_DIR", inbox_dir),
            patch.object(mod, "DISPATCHER_PID_FILE", dispatcher_pid_path),
            patch.object(mod, "LOBSTER_STATE_FILE", state_file),
        ):
            mod._write_session_lost_reminder()
    finally:
        if old_dev is None:
            os.environ.pop("LOBSTER_DEV_MODE", None)
        else:
            os.environ["LOBSTER_DEV_MODE"] = old_dev


class TestSessionLostReminderPidGuard:
    """_write_session_lost_reminder() — PID-based suppression guard (issue #1429)."""

    def test_writes_reminder_when_dispatcher_pid_absent(self, tmp_path):
        """No dispatcher.pid → real session loss → reminder written to inbox."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text(json.dumps({"mode": "active"}))
        # PID file absent — dispatcher is dead

        _call_write_session_lost_reminder(
            inbox_dir=inbox_dir,
            dispatcher_pid_path=pid_file,
            state_file=state_file,
        )

        reminder_files = list(inbox_dir.glob("session-lost-*.json"))
        assert len(reminder_files) == 1, "Expected exactly one session-lost reminder"
        reminder = json.loads(reminder_files[0].read_text())
        # postbounce #5: type is "session_reconnect", not "compact-reminder" —
        # this is the dedup fix. Reusing "compact-reminder" made the dispatcher
        # unconditionally spawn compact-catchup even though nothing was lost.
        assert reminder["type"] == "session_reconnect"
        assert reminder.get("subtype") is None, (
            "session_reconnect must not also carry subtype='compact-reminder', "
            "or the dispatcher's compact-reminder handler (keyed on subtype) "
            "would still match it and spawn compact-catchup"
        )
        assert "SESSION LOST" in reminder["text"]
        assert "do NOT spawn compact-catchup" in reminder["text"]

    def test_suppresses_reminder_when_dispatcher_alive(self, tmp_path):
        """Live dispatcher PID → mid-session reconnect → reminder suppressed."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        pid_file.write_text(str(os.getpid()))  # current process — definitely alive
        state_file = tmp_path / "lobster-state.json"
        # State file recently touched (simulating cron-job update — old bug trigger)
        state_file.write_text(json.dumps({"mode": "active"}))

        _call_write_session_lost_reminder(
            inbox_dir=inbox_dir,
            dispatcher_pid_path=pid_file,
            state_file=state_file,
        )

        reminder_files = list(inbox_dir.glob("session-lost-*.json"))
        assert len(reminder_files) == 0, (
            "Reminder was written despite dispatcher being alive — "
            "this is a false positive that would disrupt an active session"
        )

    def test_writes_reminder_when_dispatcher_pid_is_dead(self, tmp_path):
        """Dead PID in dispatcher.pid + fresh state file → reminder written.

        This is exactly the cron-job false-negative scenario from issue #1429:
        a cron job touches lobster-state.json seconds before MCP restarts.
        The old mtime-based guard would suppress the reminder here.
        The new PID-based guard correctly writes it.
        """
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        proc = subprocess.Popen(["true"])
        proc.wait()
        pid_file.write_text(str(proc.pid))  # definitely dead now
        state_file = tmp_path / "lobster-state.json"
        # State file is fresh — old guard would see this as a reconnect
        state_file.write_text(json.dumps({"mode": "active"}))
        # mtime = now (within last few seconds, well within old 30-min window)

        _call_write_session_lost_reminder(
            inbox_dir=inbox_dir,
            dispatcher_pid_path=pid_file,
            state_file=state_file,
        )

        reminder_files = list(inbox_dir.glob("session-lost-*.json"))
        assert len(reminder_files) == 1, (
            "Expected reminder for dead dispatcher despite fresh state file — "
            "the cron-job false-negative scenario from issue #1429"
        )

    def test_suppresses_reminder_in_dev_mode(self, tmp_path):
        """LOBSTER_DEV_MODE=true → always suppress regardless of PID status."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        pid_file = tmp_path / DISPATCHER_PID_FILENAME
        # No PID file — would normally trigger a reminder
        state_file = tmp_path / "lobster-state.json"
        state_file.write_text(json.dumps({"mode": "active"}))

        _call_write_session_lost_reminder(
            inbox_dir=inbox_dir,
            dispatcher_pid_path=pid_file,
            state_file=state_file,
            dev_mode="true",
        )

        reminder_files = list(inbox_dir.glob("session-lost-*.json"))
        assert len(reminder_files) == 0, "Dev mode should always suppress session-lost reminder"


# ---------------------------------------------------------------------------
# Dedup fix (postbounce #5): session_reconnect is a registered P0 message
# type, distinct from compact-reminder, and routes without spawning catchup.
# ---------------------------------------------------------------------------

class TestSessionReconnectDedupFix:
    """session_reconnect: registered type, P0 priority, no compact-catchup."""

    def test_session_reconnect_is_registered_message_type(self):
        """session_reconnect must be a known type in message_types.py.

        Otherwise it would fail any code path that validates msg["type"]
        against INBOX_MESSAGE_TYPES (e.g. ingest-time schema checks).
        """
        from src.mcp.message_types import INBOX_SYSTEM_TYPES, INBOX_MESSAGE_TYPES

        assert "session_reconnect" in INBOX_SYSTEM_TYPES
        assert "session_reconnect" in INBOX_MESSAGE_TYPES

    def test_session_reconnect_is_p0_priority(self):
        """session_reconnect must resolve to P0 (0) so it is delivered first,
        matching the immediate-delivery guarantee _write_session_lost_reminder()
        previously got for free by reusing type="compact-reminder".
        """
        from src.mcp.inbox_server import _inbox_priority

        assert _inbox_priority({"type": "session_reconnect", "text": "SESSION LOST"}) == 0

    def test_session_reconnect_does_not_match_compact_reminder_subtype_gate(self):
        """A session_reconnect message (type only, no subtype) must NOT match
        the subtype-keyed compact-reminder P0 gate — confirms the two message
        shapes are independent and cannot be cross-matched by accident.
        """
        from src.mcp.inbox_server import _INBOX_P0_SUBTYPES

        msg = {"type": "session_reconnect", "text": "SESSION LOST", "subtype": None}
        assert msg.get("subtype") not in _INBOX_P0_SUBTYPES
