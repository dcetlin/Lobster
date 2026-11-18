"""
Tests for the long-lived Observer reuse in wait_for_messages (issue #1560).

The previous implementation created a fresh watchdog Observer on every
wait_for_messages() call and tore it down with observer.join(timeout=1).
When the join timed out before the emitter thread closed its inotify fd,
the fd leaked — compounding at ~1 per call until fs.inotify.max_user_instances
was exhausted.

The fix reuses a single long-lived Observer across calls:
- _get_or_create_observer() returns the same Observer on repeated calls
- _get_or_create_observer() creates a new one only if the previous died
- Each WFM call schedule()s a handler and unschedule()s it in the finally block
- _shutdown_observer() cleanly stops the Observer at process exit

These tests exercise the Observer lifecycle functions in isolation.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Constants from the spec
# ---------------------------------------------------------------------------

# After N schedule/unschedule cycles the Observer count must stay at 1.
CYCLE_COUNT = 20


# ---------------------------------------------------------------------------
# Helpers — extract the Observer functions from inbox_server.py without
# importing the full module (which pulls in heavy MCP dependencies).
# ---------------------------------------------------------------------------

def _build_observer_namespace():
    """Build a namespace containing the Observer reuse functions.

    Reads the relevant code from inbox_server.py and executes it in an
    isolated namespace so we can test the pure lifecycle logic without
    importing the full MCP server.
    """
    import importlib.util
    import os
    import sys

    src_path = Path(__file__).parents[3] / "src" / "mcp" / "inbox_server.py"
    source = src_path.read_text()

    # Extract the module-level Observer block + the two functions.
    # We need: the imports, the globals, _get_or_create_observer, _shutdown_observer.
    ns = {}

    # Provide the required imports in the namespace
    exec(
        "import threading\n"
        "import atexit\n"
        "from watchdog.observers import Observer\n"
        "from watchdog.events import FileSystemEventHandler\n",
        ns,
    )

    # Extract the Observer singleton code block
    lines = source.split("\n")
    collecting = False
    block_lines = []
    # Find the start marker
    for i, line in enumerate(lines):
        if "_wfm_observer: Observer | None = None" in line:
            collecting = True
        if collecting:
            block_lines.append(line)
            # Stop after _shutdown_observer and atexit.register
            if "atexit.register(_shutdown_observer)" in line:
                break

    assert block_lines, "Could not find Observer singleton code block in inbox_server.py"

    # Execute the extracted block
    exec("\n".join(block_lines), ns)

    return ns


@pytest.fixture
def observer_ns():
    """Provide a fresh Observer namespace for each test.

    Resets the module-level _wfm_observer to None so tests are isolated.
    """
    ns = _build_observer_namespace()
    # Ensure clean state
    ns["_wfm_observer"] = None
    yield ns
    # Cleanup: stop any observer left running
    if ns.get("_wfm_observer") is not None:
        try:
            ns["_wfm_observer"].stop()
            ns["_wfm_observer"].join(timeout=2)
        except Exception:
            pass
        ns["_wfm_observer"] = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetOrCreateObserver:
    """_get_or_create_observer returns a reusable Observer singleton."""

    def test_returns_running_observer(self, observer_ns):
        """First call creates and starts an Observer."""
        get_or_create = observer_ns["_get_or_create_observer"]
        obs = get_or_create()
        assert obs is not None
        assert obs.is_alive()

    def test_returns_same_observer_on_repeated_calls(self, observer_ns):
        """Repeated calls return the same Observer instance — no new fds."""
        get_or_create = observer_ns["_get_or_create_observer"]
        obs1 = get_or_create()
        obs2 = get_or_create()
        obs3 = get_or_create()
        assert obs1 is obs2
        assert obs2 is obs3

    def test_replaces_dead_observer(self, observer_ns):
        """If the Observer thread dies, a new one is created."""
        get_or_create = observer_ns["_get_or_create_observer"]
        obs1 = get_or_create()
        # Kill the observer
        obs1.stop()
        obs1.join(timeout=2)
        assert not obs1.is_alive()

        obs2 = get_or_create()
        assert obs2 is not obs1
        assert obs2.is_alive()

    def test_observer_is_daemon(self, observer_ns):
        """Observer thread is daemonic so it dies with the process."""
        get_or_create = observer_ns["_get_or_create_observer"]
        obs = get_or_create()
        assert obs.daemon is True


class TestShutdownObserver:
    """_shutdown_observer cleanly stops the Observer."""

    def test_stops_running_observer(self, observer_ns):
        """Shutdown stops the Observer and sets the global to None."""
        get_or_create = observer_ns["_get_or_create_observer"]
        shutdown = observer_ns["_shutdown_observer"]

        obs = get_or_create()
        assert obs.is_alive()

        shutdown()
        assert observer_ns["_wfm_observer"] is None
        # The observer should no longer be alive (or at least stop was called)

    def test_shutdown_idempotent_when_no_observer(self, observer_ns):
        """Shutdown is a no-op when no Observer has been created."""
        shutdown = observer_ns["_shutdown_observer"]
        # Should not raise
        shutdown()
        assert observer_ns["_wfm_observer"] is None

    def test_shutdown_idempotent_on_double_call(self, observer_ns):
        """Calling shutdown twice is safe."""
        get_or_create = observer_ns["_get_or_create_observer"]
        shutdown = observer_ns["_shutdown_observer"]

        get_or_create()
        shutdown()
        shutdown()
        assert observer_ns["_wfm_observer"] is None


class TestObserverWatchCyclesBounded:
    """Schedule/unschedule cycles on the same Observer keep fd count bounded."""

    def test_watch_count_stays_bounded_across_cycles(self, observer_ns, tmp_path):
        """Simulates CYCLE_COUNT WFM calls: schedule → unschedule.

        The Observer should stay the same instance throughout, proving
        only one inotify fd is held.
        """
        get_or_create = observer_ns["_get_or_create_observer"]
        FileSystemEventHandler = observer_ns["FileSystemEventHandler"]

        obs = get_or_create()
        watch_dir = str(tmp_path)

        for _ in range(CYCLE_COUNT):
            handler = FileSystemEventHandler()
            watch = obs.schedule(handler, watch_dir, recursive=False)
            obs.unschedule(watch)

        # After all cycles, the observer is still the same instance and alive
        assert get_or_create() is obs
        assert obs.is_alive()

    def test_no_fd_leak_across_cycles(self, observer_ns, tmp_path):
        """After multiple schedule/unschedule cycles, the Observer holds
        no watches (fd count does not grow)."""
        get_or_create = observer_ns["_get_or_create_observer"]
        FileSystemEventHandler = observer_ns["FileSystemEventHandler"]

        obs = get_or_create()
        watch_dir = str(tmp_path)

        for _ in range(CYCLE_COUNT):
            handler = FileSystemEventHandler()
            watch = obs.schedule(handler, watch_dir, recursive=False)
            obs.unschedule(watch)

        # The observer's internal emitters list should be empty after
        # all watches are unscheduled.
        # Access internal _watches dict — if implementation changes this
        # test may need updating, but it verifies the core property.
        assert len(obs._watches) == 0, (
            f"Expected 0 active watches after {CYCLE_COUNT} cycles, "
            f"got {len(obs._watches)}"
        )
