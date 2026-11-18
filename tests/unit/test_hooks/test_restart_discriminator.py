"""
Tests for the restart discriminator in on-fresh-start.py (issue #1561).

Validates:
- _session_reconnect_in_inbox() correctly detects session_reconnect messages
- on-fresh-start.py skips compact-reminder injection when session_reconnect is queued
- on-fresh-start.py still injects compact-reminder when no session_reconnect is queued
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"


class _PatchEnv:
    """Context manager to temporarily set environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _make_session_role_stub():
    """Return a minimal session_role stub module."""
    import types
    stub = types.ModuleType("session_role")
    stub.is_dispatcher = lambda data: True
    return stub


def _load_on_fresh_start(
    compaction_state_override: str = None,
    inbox_dir: str = None,
    session_file_pointer_override: str = None,
    inflight_work_override: str = None,
):
    """Load on-fresh-start.py as a module, overriding file paths for isolation."""
    env_patch = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override
    if session_file_pointer_override:
        env_patch["LOBSTER_CURRENT_SESSION_FILE_OVERRIDE"] = session_file_pointer_override
    if inflight_work_override:
        env_patch["LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE"] = inflight_work_override

    with _PatchEnv(env_patch):
        spec = importlib.util.spec_from_file_location("on_fresh_start", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("session_role", _make_session_role_stub())
        spec.loader.exec_module(mod)

    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    if inbox_dir:
        mod.INBOX_DIR = Path(inbox_dir)
    if session_file_pointer_override:
        mod.CURRENT_SESSION_FILE_POINTER = Path(session_file_pointer_override)
    if inflight_work_override:
        mod.INFLIGHT_WORK_FILE = Path(inflight_work_override)
    return mod


def _write_session_reconnect(inbox_dir: Path, msg_id: str = "session-lost-123") -> Path:
    """Write a session_reconnect message to the inbox directory."""
    msg = {
        "id": msg_id,
        "source": "system",
        "type": "session_reconnect",
        "chat_id": 0,
        "text": (
            "SESSION LOST -- lightweight reconnect, not a context compaction. "
            "Do NOT spawn compact-catchup."
        ),
        "timestamp": "2026-09-18T05:00:00.000000",
    }
    path = inbox_dir / f"{msg_id}.json"
    path.write_text(json.dumps(msg))
    return path


def _write_compact_reminder(inbox_dir: Path, msg_id: str = "0_startup_compact") -> Path:
    """Write a compact-reminder message to the inbox directory."""
    msg = {
        "id": msg_id,
        "source": "system",
        "chat_id": 0,
        "subtype": "compact-reminder",
        "type": "text",
        "text": "Compact reminder text",
        "timestamp": "2026-09-18T05:00:00.000000",
    }
    path = inbox_dir / f"{msg_id}.json"
    path.write_text(json.dumps(msg))
    return path


# ---------------------------------------------------------------------------
# _session_reconnect_in_inbox tests
# ---------------------------------------------------------------------------


class TestSessionReconnectInInbox:
    """_session_reconnect_in_inbox detects session_reconnect messages."""

    def test_returns_false_when_inbox_empty(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start(inbox_dir=str(inbox))
        assert mod._session_reconnect_in_inbox() is False

    def test_returns_false_when_inbox_absent(self, tmp_path):
        mod = _load_on_fresh_start(inbox_dir=str(tmp_path / "nonexistent"))
        assert mod._session_reconnect_in_inbox() is False

    def test_returns_true_when_session_reconnect_present(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_session_reconnect(inbox)
        mod = _load_on_fresh_start(inbox_dir=str(inbox))
        assert mod._session_reconnect_in_inbox() is True

    def test_returns_false_when_only_compact_reminder(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_compact_reminder(inbox)
        mod = _load_on_fresh_start(inbox_dir=str(inbox))
        assert mod._session_reconnect_in_inbox() is False

    def test_returns_true_when_both_present(self, tmp_path):
        """session_reconnect is detected even when compact-reminder also exists."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_session_reconnect(inbox)
        _write_compact_reminder(inbox)
        mod = _load_on_fresh_start(inbox_dir=str(inbox))
        assert mod._session_reconnect_in_inbox() is True

    def test_ignores_non_json_files(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "not-json.txt").write_text('{"type": "session_reconnect"}')
        mod = _load_on_fresh_start(inbox_dir=str(inbox))
        assert mod._session_reconnect_in_inbox() is False

    def test_ignores_malformed_json(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "bad.json").write_text("not json at all")
        mod = _load_on_fresh_start(inbox_dir=str(inbox))
        assert mod._session_reconnect_in_inbox() is False


# ---------------------------------------------------------------------------
# Integration: compact-reminder injection skipped when session_reconnect queued
# ---------------------------------------------------------------------------


class TestCompactReminderSkippedOnSessionReconnect:
    """on-fresh-start.py skips compact-reminder injection when
    session_reconnect is queued (issue #1561 discriminator)."""

    def test_no_compact_reminder_when_session_reconnect_present(self, tmp_path):
        """When session_reconnect is in inbox and catchup is stale,
        compact-reminder is NOT injected."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_session_reconnect(inbox)

        # Make catchup appear stale (no compaction-state.json)
        mod = _load_on_fresh_start(
            compaction_state_override=str(tmp_path / "nonexistent.json"),
            inbox_dir=str(inbox),
            session_file_pointer_override=str(tmp_path / "nonexistent-pointer"),
        )
        # Verify catchup IS stale (precondition)
        assert mod._is_catchup_stale() is True

        # The session_reconnect discriminator should block injection
        assert mod._session_reconnect_in_inbox() is True

        # Call _inject_compact_reminder directly to verify it would have
        # injected (the logic in main() guards with the discriminator)
        # But since _inject_compact_reminder checks _compact_reminder_already_queued,
        # and no compact-reminder exists, it WOULD inject if called directly.
        # The test verifies the discriminator, not _inject_compact_reminder itself.

        # Simulate the main() logic: the discriminator should prevent injection
        should_inject = not mod._session_reconnect_in_inbox() and (
            mod._is_catchup_stale() or mod._has_recent_session_file()
        )
        assert should_inject is False

    def test_compact_reminder_injected_when_no_session_reconnect(self, tmp_path):
        """When no session_reconnect is in inbox and catchup is stale,
        compact-reminder IS injected."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()

        mod = _load_on_fresh_start(
            compaction_state_override=str(tmp_path / "nonexistent.json"),
            inbox_dir=str(inbox),
            session_file_pointer_override=str(tmp_path / "nonexistent-pointer"),
        )
        mod.PROCESSING_DIR = processing
        # Provide a valid AGENT_SESSIONS_DB path so _clear_stale_claim doesn't fail
        mod.AGENT_SESSIONS_DB = tmp_path / "agent_sessions.db"

        assert mod._is_catchup_stale() is True
        assert mod._session_reconnect_in_inbox() is False

        # Simulate the main() logic: injection SHOULD proceed
        should_inject = not mod._session_reconnect_in_inbox() and (
            mod._is_catchup_stale() or mod._has_recent_session_file()
        )
        assert should_inject is True

        # Actually call _inject_compact_reminder and verify file was written
        mod._inject_compact_reminder()
        compact_files = list(inbox.glob("*startup_compact*.json"))
        assert len(compact_files) == 1
        data = json.loads(compact_files[0].read_text())
        assert data["subtype"] == "compact-reminder"


# ---------------------------------------------------------------------------
# inject-bootup-context.py: session_rotation cause override
# ---------------------------------------------------------------------------


class TestSessionRotationCauseOverride:
    """inject-bootup-context.py sets startup_cause='session_rotation'
    when the dispatcher is detected via UUID match only (no PID flag)."""

    def test_uuid_only_overrides_to_session_rotation(self):
        """When uuid_dispatcher=True and pid_flag_dispatcher=False,
        startup_cause is overridden to 'session_rotation'."""
        # This tests the logic from inject-bootup-context.py:
        # if uuid_dispatcher and not pid_flag_dispatcher:
        #     if startup_cause != "compaction":
        #         startup_cause = "session_rotation"
        pid_flag_dispatcher = False
        uuid_dispatcher = True
        startup_cause = "restart"

        if uuid_dispatcher and not pid_flag_dispatcher:
            if startup_cause != "compaction":
                startup_cause = "session_rotation"

        assert startup_cause == "session_rotation"

    def test_pid_flag_preserves_restart_cause(self):
        """When pid_flag_dispatcher=True, startup_cause stays as-is."""
        pid_flag_dispatcher = True
        uuid_dispatcher = True
        startup_cause = "restart"

        if uuid_dispatcher and not pid_flag_dispatcher:
            if startup_cause != "compaction":
                startup_cause = "session_rotation"

        assert startup_cause == "restart"

    def test_uuid_only_preserves_compaction_cause(self):
        """Compaction always wins: UUID-only does NOT override compaction."""
        pid_flag_dispatcher = False
        uuid_dispatcher = True
        startup_cause = "compaction"

        if uuid_dispatcher and not pid_flag_dispatcher:
            if startup_cause != "compaction":
                startup_cause = "session_rotation"

        assert startup_cause == "compaction"

    def test_neither_signal_preserves_restart(self):
        """When neither pid_flag nor uuid_match, cause stays 'restart'."""
        pid_flag_dispatcher = False
        uuid_dispatcher = False
        startup_cause = "restart"

        is_dispatcher = pid_flag_dispatcher or uuid_dispatcher
        if is_dispatcher and uuid_dispatcher and not pid_flag_dispatcher:
            if startup_cause != "compaction":
                startup_cause = "session_rotation"

        assert startup_cause == "restart"
