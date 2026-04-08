"""
Tests for the executor false-complete fix (issue #669).

Spec: executor marks `execution_complete` at dispatch time instead of when
`write_result` is received.

Expected behavior per issue #669:
- For async inbox dispatch (functional-engineer, lobster-ops, general executor types):
  UoW transitions active → executing at dispatch time.
  execution_complete and executing → ready-for-steward happen only when the
  subagent calls write_result (i.e. Registry.complete_uow called from write_result handler).
- For synchronous subprocess dispatch (frontier-writer, design-review, injected overrides):
  Behavior unchanged — complete_uow is called after the subprocess exits.
- UoWStatus.EXECUTING is in-flight (blocks re-proposal).
- TTL recovery covers 'executing' UoWs alongside 'active'.
- Registry.transition_to_executing writes executor_dispatch audit entry.
- Registry.complete_uow on an 'executing' UoW writes execution_complete audit entry.
- _maybe_complete_wos_uow transitions executing → ready-for-steward on write_result.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from orchestration.registry import Registry, UoWStatus
from orchestration.workflow_artifact import WorkflowArtifact, to_json
from orchestration.executor import (
    Executor,
    ExecutorOutcome,
    _dispatch_via_inbox,
    _noop_dispatcher,
    _dispatcher_is_async,
    _ASYNC_EXECUTOR_TYPES,
    recover_ttl_exceeded_uows,
    TTL_EXCEEDED_HOURS,
)


# ---------------------------------------------------------------------------
# Named constants derived from the spec (issue #669)
# ---------------------------------------------------------------------------

#: Executor types that must use async inbox dispatch and the executing intermediate status.
INBOX_EXECUTOR_TYPES = frozenset({"functional-engineer", "lobster-ops", "general"})

#: Executor types that must use synchronous subprocess dispatch (no executing intermediate).
SUBPROCESS_EXECUTOR_TYPES = frozenset({"frontier-writer", "design-review"})

#: Status set by async inbox dispatch before subagent confirms.
STATUS_EXECUTING = "executing"

#: Status after subagent confirms via write_result.
STATUS_READY_FOR_STEWARD = "ready-for-steward"

#: Audit event written at inbox dispatch time.
AUDIT_EXECUTOR_DISPATCH = "executor_dispatch"

#: Audit event that must only appear after write_result confirms.
AUDIT_EXECUTION_COMPLETE = "execution_complete"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


def _make_artifact(uow_id: str, executor_type: str = "general") -> str:
    """Return JSON-encoded WorkflowArtifact for the given executor_type."""
    artifact: WorkflowArtifact = {
        "uow_id": uow_id,
        "executor_type": executor_type,
        "constraints": [],
        "prescribed_skills": [],
        "instructions": "Do the thing",
    }
    return to_json(artifact)


def _insert_uow(db_path: Path, uow_id: str, workflow_artifact: str | None = None) -> None:
    """Directly insert a UoW into the registry for test setup."""
    import sqlite3
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, workflow_artifact
            ) VALUES (?, 'executable', 'test', ?, 'solo', ?, ?, 'Test UoW', 'test done', ?)
            """,
            (uow_id, "ready-for-executor", now, now, workflow_artifact),
        )
        conn.commit()
    finally:
        conn.close()


def _get_uow_status(db_path: Path, uow_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status FROM uow_registry WHERE id = ?", (uow_id,)).fetchone()
        return row["status"] if row else ""
    finally:
        conn.close()


def _get_audit_events(db_path: Path, uow_id: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event FROM audit_log WHERE uow_id = ? ORDER BY id", (uow_id,)
        ).fetchall()
        return [r["event"] for r in rows]
    finally:
        conn.close()


def _insert_uow_with_status(db_path: Path, uow_id: str, status: str, started_at: str | None = None) -> None:
    """Insert a UoW with a specific status for TTL recovery testing."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry (
                id, type, source, status, posture, created_at, updated_at,
                summary, success_criteria, started_at
            ) VALUES (?, 'executable', 'test', ?, 'solo', ?, ?, 'Test UoW', 'done', ?)
            """,
            (uow_id, status, now, now, started_at or now),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests: UoWStatus.EXECUTING is a valid in-flight status
# ---------------------------------------------------------------------------

class TestExecutingStatus:
    def test_executing_is_in_flight(self) -> None:
        """EXECUTING status blocks re-proposal — it is an in-flight state."""
        assert UoWStatus.EXECUTING.is_in_flight() is True

    def test_executing_is_not_terminal(self) -> None:
        """EXECUTING status is not terminal — the UoW is not done or failed."""
        assert UoWStatus.EXECUTING.is_terminal() is False

    def test_executing_string_value(self) -> None:
        """UoWStatus.EXECUTING must serialize to the string 'executing'."""
        assert str(UoWStatus.EXECUTING) == STATUS_EXECUTING
        assert UoWStatus("executing") == UoWStatus.EXECUTING


# ---------------------------------------------------------------------------
# Tests: _dispatcher_is_async correctly identifies async vs sync paths
# ---------------------------------------------------------------------------

class TestDispatcherIsAsync:
    def test_inbox_executor_types_are_async(self) -> None:
        """All inbox executor types must be identified as async dispatch."""
        for executor_type in INBOX_EXECUTOR_TYPES:
            assert _dispatcher_is_async(None, executor_type) is True, (
                f"executor_type={executor_type!r} must be async (inbox dispatch)"
            )

    def test_subprocess_executor_types_are_sync(self) -> None:
        """Subprocess executor types must NOT be identified as async dispatch."""
        for executor_type in SUBPROCESS_EXECUTOR_TYPES:
            assert _dispatcher_is_async(None, executor_type) is False, (
                f"executor_type={executor_type!r} must be sync (subprocess dispatch)"
            )

    def test_injected_dispatcher_is_always_sync(self) -> None:
        """Any injected dispatcher override is treated as synchronous."""
        # Even if executor_type would normally be async, override → sync
        for executor_type in INBOX_EXECUTOR_TYPES:
            assert _dispatcher_is_async(_noop_dispatcher, executor_type) is False, (
                f"Injected dispatcher must always be treated as sync "
                f"(executor_type={executor_type!r})"
            )

    def test_unknown_executor_type_is_sync_without_override(self) -> None:
        """An unknown executor_type with no override defaults to sync (safe default)."""
        assert _dispatcher_is_async(None, "unknown-type") is False


# ---------------------------------------------------------------------------
# Tests: Async dispatch (inbox path) — status transitions
# ---------------------------------------------------------------------------

class TestAsyncDispatchStatusTransitions:
    def test_inbox_dispatch_transitions_to_executing_not_ready_for_steward(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Async inbox dispatch must leave UoW in 'executing', not 'ready-for-steward'.

        The UoW status must not jump to ready-for-steward at dispatch time —
        that would produce a false execution_complete before any work is done.
        """
        uow_id = "uow_async_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id, "functional-engineer"))

        dispatched: list[str] = []

        def fake_inbox_dispatcher(instructions: str, uid: str) -> str:
            dispatched.append(uid)
            return f"msg-{uid}"

        # Simulate async inbox dispatch: no dispatcher_override, executor_type is async.
        # We inject a fake that behaves like _dispatch_via_inbox (fire-and-forget).
        # To trigger the async path, we patch the dispatch table lookup.
        import orchestration.executor as executor_mod
        original = executor_mod._dispatch_via_inbox
        try:
            executor_mod._dispatch_via_inbox = fake_inbox_dispatcher  # type: ignore[attr-defined]
            executor = Executor(registry)  # dispatcher=None → uses dispatch table
            executor.execute_uow(uow_id)
        finally:
            executor_mod._dispatch_via_inbox = original

        status = _get_uow_status(db_path, uow_id)
        assert status == STATUS_EXECUTING, (
            f"Async inbox dispatch must leave UoW in 'executing', got {status!r}. "
            "execution_complete must not fire at dispatch time (issue #669)."
        )

    def test_inbox_dispatch_does_not_write_execution_complete_audit_entry(
        self, registry: Registry, db_path: Path
    ) -> None:
        """execution_complete must NOT appear in audit_log at dispatch time for async path."""
        uow_id = "uow_async_002"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id, "lobster-ops"))

        def fake_inbox_dispatcher(instructions: str, uid: str) -> str:
            return f"msg-{uid}"

        import orchestration.executor as executor_mod
        original = executor_mod._dispatch_via_inbox
        try:
            executor_mod._dispatch_via_inbox = fake_inbox_dispatcher  # type: ignore[attr-defined]
            executor = Executor(registry)
            executor.execute_uow(uow_id)
        finally:
            executor_mod._dispatch_via_inbox = original

        events = _get_audit_events(db_path, uow_id)
        assert AUDIT_EXECUTION_COMPLETE not in events, (
            f"execution_complete must not appear at dispatch time for async path. "
            f"Audit events: {events}"
        )

    def test_inbox_dispatch_writes_executor_dispatch_audit_entry(
        self, registry: Registry, db_path: Path
    ) -> None:
        """executor_dispatch audit entry must appear after async inbox dispatch."""
        uow_id = "uow_async_003"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id, "general"))

        def fake_inbox_dispatcher(instructions: str, uid: str) -> str:
            return f"msg-{uid}"

        import orchestration.executor as executor_mod
        original = executor_mod._dispatch_via_inbox
        try:
            executor_mod._dispatch_via_inbox = fake_inbox_dispatcher  # type: ignore[attr-defined]
            executor = Executor(registry)
            executor.execute_uow(uow_id)
        finally:
            executor_mod._dispatch_via_inbox = original

        events = _get_audit_events(db_path, uow_id)
        assert AUDIT_EXECUTOR_DISPATCH in events, (
            f"executor_dispatch audit entry must appear after async inbox dispatch. "
            f"Audit events: {events}"
        )


# ---------------------------------------------------------------------------
# Tests: Synchronous dispatch (injected override / subprocess) — unchanged behavior
# ---------------------------------------------------------------------------

class TestSyncDispatchStatusTransitions:
    def test_sync_dispatch_transitions_to_ready_for_steward(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Synchronous dispatch (injected override) must still transition to ready-for-steward."""
        uow_id = "uow_sync_001"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        status = _get_uow_status(db_path, uow_id)
        assert status == STATUS_READY_FOR_STEWARD, (
            f"Sync dispatch must transition to ready-for-steward. Got {status!r}."
        )

    def test_sync_dispatch_writes_execution_complete_audit_entry(
        self, registry: Registry, db_path: Path
    ) -> None:
        """Synchronous dispatch must write execution_complete audit entry immediately."""
        uow_id = "uow_sync_002"
        _insert_uow(db_path, uow_id, workflow_artifact=_make_artifact(uow_id))

        executor = Executor(registry, dispatcher=_noop_dispatcher)
        executor.execute_uow(uow_id)

        events = _get_audit_events(db_path, uow_id)
        assert AUDIT_EXECUTION_COMPLETE in events, (
            f"Sync dispatch must write execution_complete audit entry. "
            f"Audit events: {events}"
        )


# ---------------------------------------------------------------------------
# Tests: Registry.transition_to_executing
# ---------------------------------------------------------------------------

class TestTransitionToExecuting:
    def test_transition_to_executing_sets_status(self, registry: Registry, db_path: Path) -> None:
        """transition_to_executing must set status to 'executing'."""
        uow_id = "uow_te_001"
        _insert_uow(db_path, uow_id)
        # Manually put it in 'active' state (as the claim sequence would)
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET status = 'active' WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        registry.transition_to_executing(uow_id, "executor-id-abc")

        assert _get_uow_status(db_path, uow_id) == STATUS_EXECUTING

    def test_transition_to_executing_writes_audit_entry(self, registry: Registry, db_path: Path) -> None:
        """transition_to_executing must write an executor_dispatch audit entry."""
        uow_id = "uow_te_002"
        _insert_uow(db_path, uow_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET status = 'active' WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        registry.transition_to_executing(uow_id, "executor-id-xyz")

        events = _get_audit_events(db_path, uow_id)
        assert AUDIT_EXECUTOR_DISPATCH in events


# ---------------------------------------------------------------------------
# Tests: Registry.complete_uow from executing state
# ---------------------------------------------------------------------------

class TestCompleteUowFromExecuting:
    def test_complete_uow_from_executing_sets_ready_for_steward(
        self, registry: Registry, db_path: Path
    ) -> None:
        """complete_uow on an 'executing' UoW must transition to ready-for-steward."""
        uow_id = "uow_cu_001"
        _insert_uow(db_path, uow_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET status = 'executing' WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        registry.complete_uow(uow_id, "/tmp/output_ref.json")

        assert _get_uow_status(db_path, uow_id) == STATUS_READY_FOR_STEWARD

    def test_complete_uow_from_executing_writes_execution_complete_audit(
        self, registry: Registry, db_path: Path
    ) -> None:
        """complete_uow on 'executing' UoW must write execution_complete audit entry."""
        uow_id = "uow_cu_002"
        _insert_uow(db_path, uow_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET status = 'executing' WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        registry.complete_uow(uow_id, "/tmp/output_ref.json")

        events = _get_audit_events(db_path, uow_id)
        assert AUDIT_EXECUTION_COMPLETE in events

    def test_complete_uow_from_active_still_works(self, registry: Registry, db_path: Path) -> None:
        """complete_uow on 'active' UoW (sync dispatch path) must still work."""
        uow_id = "uow_cu_003"
        _insert_uow(db_path, uow_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET status = 'active' WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        registry.complete_uow(uow_id, "/tmp/output_ref.json")

        assert _get_uow_status(db_path, uow_id) == STATUS_READY_FOR_STEWARD
        events = _get_audit_events(db_path, uow_id)
        assert AUDIT_EXECUTION_COMPLETE in events


# ---------------------------------------------------------------------------
# Tests: TTL recovery covers 'executing' UoWs
# ---------------------------------------------------------------------------

class TestTtlRecoveryCoversExecuting:
    def test_ttl_recovery_fails_executing_uow_past_ttl(self, registry: Registry, db_path: Path) -> None:
        """TTL recovery must include 'executing' UoWs that have exceeded TTL_EXCEEDED_HOURS."""
        from datetime import datetime, timezone, timedelta

        uow_id = "uow_ttl_exec_001"
        # started_at is well past the TTL cutoff
        stale_started_at = (
            datetime.now(timezone.utc) - timedelta(hours=TTL_EXCEEDED_HOURS + 1)
        ).isoformat()
        _insert_uow_with_status(db_path, uow_id, "executing", started_at=stale_started_at)

        recovered = recover_ttl_exceeded_uows(registry)

        assert uow_id in recovered, (
            f"TTL recovery must recover 'executing' UoWs past TTL. "
            f"Recovered: {recovered}"
        )
        assert _get_uow_status(db_path, uow_id) == "failed"

    def test_ttl_recovery_does_not_fail_fresh_executing_uow(
        self, registry: Registry, db_path: Path
    ) -> None:
        """TTL recovery must NOT fail 'executing' UoWs that are still within TTL."""
        from datetime import datetime, timezone, timedelta

        uow_id = "uow_ttl_exec_002"
        # started_at is recent — well within TTL
        fresh_started_at = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat()
        _insert_uow_with_status(db_path, uow_id, "executing", started_at=fresh_started_at)

        recovered = recover_ttl_exceeded_uows(registry)

        assert uow_id not in recovered, (
            f"TTL recovery must not touch 'executing' UoWs within TTL. "
            f"Recovered: {recovered}"
        )
        assert _get_uow_status(db_path, uow_id) == "executing"


# ---------------------------------------------------------------------------
# Tests: _maybe_complete_wos_uow (inbox_server integration)
# ---------------------------------------------------------------------------

class TestMaybeCompleteWosUow:
    """Tests for _maybe_complete_wos_uow.

    This function is defined in src/mcp/inbox_server.py but tested here via the
    wos_completion module (same logic, importable without inbox_server's heavy deps).
    """

    def test_completes_executing_uow_on_success_write_result(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_maybe_complete_wos_uow must transition executing → ready-for-steward on success."""
        uow_id = "uow_mcwu_001"
        _insert_uow(db_path, uow_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET status = 'executing' WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        monkeypatch.setenv("REGISTRY_DB_PATH", str(db_path))

        from orchestration.wos_completion import maybe_complete_wos_uow
        maybe_complete_wos_uow(f"wos-{uow_id}", "success")

        assert _get_uow_status(db_path, uow_id) == STATUS_READY_FOR_STEWARD

    def test_does_not_complete_for_non_wos_task_id(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """maybe_complete_wos_uow must be a no-op for task_ids that don't start with 'wos-'."""
        uow_id = "uow_mcwu_002"
        _insert_uow(db_path, uow_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET status = 'executing' WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        monkeypatch.setenv("REGISTRY_DB_PATH", str(db_path))

        from orchestration.wos_completion import maybe_complete_wos_uow
        maybe_complete_wos_uow("issue-42-pr-review", "success")  # not a wos- task_id

        # Status must remain unchanged
        assert _get_uow_status(db_path, uow_id) == "executing"

    def test_does_not_complete_on_failed_write_result(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """maybe_complete_wos_uow must NOT complete UoW when status='error'."""
        uow_id = "uow_mcwu_003"
        _insert_uow(db_path, uow_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE uow_registry SET status = 'executing' WHERE id = ?", (uow_id,))
        conn.commit()
        conn.close()

        monkeypatch.setenv("REGISTRY_DB_PATH", str(db_path))

        from orchestration.wos_completion import maybe_complete_wos_uow
        maybe_complete_wos_uow(f"wos-{uow_id}", "error")  # failed write_result

        # Status must remain in executing — TTL recovery handles this
        assert _get_uow_status(db_path, uow_id) == "executing"

    def test_does_not_complete_uow_not_in_executing_status(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """maybe_complete_wos_uow must skip UoWs already past 'executing' (idempotency)."""
        uow_id = "uow_mcwu_004"
        _insert_uow(db_path, uow_id)
        # Already in ready-for-steward (e.g. from TTL recovery or duplicate write_result)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE uow_registry SET status = 'ready-for-steward' WHERE id = ?", (uow_id,)
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("REGISTRY_DB_PATH", str(db_path))

        from orchestration.wos_completion import maybe_complete_wos_uow
        # Must not raise; must be a no-op
        maybe_complete_wos_uow(f"wos-{uow_id}", "success")

        assert _get_uow_status(db_path, uow_id) == "ready-for-steward"
