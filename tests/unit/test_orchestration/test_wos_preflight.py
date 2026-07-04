"""
Tests for the WOS pre-flight protocol (issue #929).

Coverage:
- wos_preflight.is_still_needed: default-True path (no registered check)
- wos_preflight.is_still_needed: registered check returning True
- wos_preflight.is_still_needed: registered check returning False
- wos_preflight.is_still_needed: check raises — fail-open (returns True)
- wos_preflight.register_preflight_check: callable enforcement
- wos_preflight.register_preflight_check: idempotent re-registration
- Executor._run_execution: default-True path dispatches normally (regression guard)
- Executor._run_execution: False-path short-circuits — no subagent spawned
- Executor._run_execution: False-path marks UoW complete with outcome_category=heat
- Executor._run_execution: True-path proceeds to dispatch
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestration.registry import Registry
from orchestration.workflow_artifact import WorkflowArtifact, to_json
from orchestration.executor import (
    Executor,
    ExecutorOutcome,
    _result_json_path,
)
import orchestration.wos_preflight as preflight_mod
from orchestration.wos_preflight import (
    is_still_needed,
    register_preflight_check,
    _derive_preflight_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubUoW:
    """Minimal stub for a UoW object — just needs a type attribute."""

    def __init__(self, uow_type: str = "executable") -> None:
        self.type = uow_type
        self.id = "stub-uow-id"


class _StubMergeUoW:
    """Stub UoW with source_ref for merge-pr preflight tests."""

    def __init__(self, source_ref: str = "github:pr/42", uow_type: str = "executable") -> None:
        self.source_ref = source_ref
        self.type = uow_type
        self.id = "stub-merge-uow"


def _make_artifact(
    uow_id: str,
    executor_type: str = "functional-engineer",
    instructions: str = "Do the thing",
) -> str:
    artifact: WorkflowArtifact = {
        "uow_id": uow_id,
        "executor_type": executor_type,
        "constraints": [],
        "prescribed_skills": [],
        "instructions": instructions,
    }
    return to_json(artifact)


def _insert_uow(
    db_path: Path,
    uow_id: str,
    uow_type: str = "executable",
    executor_type: str = "functional-engineer",
    register: str = "operational",
    status: str = "ready-for-executor",
) -> None:
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
                summary, success_criteria, workflow_artifact, estimated_runtime,
                register
            ) VALUES (?, ?, 'test', ?, 'solo', ?, ?, 'Test UoW', 'done', ?, NULL, ?)
            """,
            (uow_id, uow_type, status, now, now, _make_artifact(uow_id, executor_type=executor_type), register),
        )
        conn.commit()
    finally:
        conn.close()


def _get_outcome_category(db_path: Path, uow_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT outcome_category FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["outcome_category"] if row else None
    finally:
        conn.close()


def _get_status(db_path: Path, uow_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def _get_output_ref(db_path: Path, uow_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT output_ref FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["output_ref"] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    return Registry(db_path)


@pytest.fixture(autouse=True)
def _clean_preflight_registry() -> "Generator[None, None, None]":
    """
    Restore the preflight registry to its original state after each test.

    Tests that register type-specific checks must not pollute the global
    registry for subsequent tests. This fixture saves and restores the
    _REGISTRY dict around each test.
    """
    import copy
    from typing import Generator
    saved = copy.copy(preflight_mod._REGISTRY)
    yield
    preflight_mod._REGISTRY.clear()
    preflight_mod._REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# Unit tests: wos_preflight module
# ---------------------------------------------------------------------------


class TestIsStillNeededDefault:
    """Default path: no registered check → always returns True."""

    def test_no_check_registered_returns_true(self) -> None:
        uow = _StubUoW(uow_type="executable")
        assert is_still_needed(uow) is True

    def test_unknown_type_returns_true(self) -> None:
        uow = _StubUoW(uow_type="an-unregistered-type")
        assert is_still_needed(uow) is True

    def test_empty_type_returns_true(self) -> None:
        uow = _StubUoW(uow_type="")
        assert is_still_needed(uow) is True

    def test_no_type_attribute_returns_true(self) -> None:
        """Object without .type attribute — is_still_needed must not raise."""
        class NoType:
            pass
        assert is_still_needed(NoType()) is True


class TestIsStillNeededRegistered:
    """Registered check paths."""

    def test_registered_check_returning_true_allows_dispatch(self) -> None:
        register_preflight_check("my-type", lambda uow: True)
        uow = _StubUoW(uow_type="my-type")
        assert is_still_needed(uow) is True

    def test_registered_check_returning_false_blocks_dispatch(self) -> None:
        register_preflight_check("my-type", lambda uow: False)
        uow = _StubUoW(uow_type="my-type")
        assert is_still_needed(uow) is False

    def test_registered_check_receives_uow_object(self) -> None:
        received: list[Any] = []
        def capturing_check(uow: Any) -> bool:
            received.append(uow)
            return True
        register_preflight_check("capture-type", capturing_check)
        uow = _StubUoW(uow_type="capture-type")
        is_still_needed(uow)
        assert len(received) == 1
        assert received[0] is uow

    def test_check_for_other_type_not_called(self) -> None:
        other_called: list[bool] = []
        register_preflight_check("other-type", lambda uow: other_called.append(True) or False)
        uow = _StubUoW(uow_type="different-type")
        result = is_still_needed(uow)
        assert result is True, "Different type should use default (True), not the registered check"
        assert len(other_called) == 0


class TestIsStillNeededFailOpen:
    """When check raises an exception, is_still_needed must return True (fail-open)."""

    def test_raising_check_returns_true(self) -> None:
        def bad_check(uow: Any) -> bool:
            raise RuntimeError("simulated check failure")
        register_preflight_check("bad-type", bad_check)
        uow = _StubUoW(uow_type="bad-type")
        # Must not raise; must return True
        result = is_still_needed(uow)
        assert result is True


class TestRegisterPreflightCheck:
    """register_preflight_check contract."""

    def test_register_callable(self) -> None:
        register_preflight_check("reg-type", lambda uow: True)
        uow = _StubUoW(uow_type="reg-type")
        assert is_still_needed(uow) is True

    def test_re_register_overwrites(self) -> None:
        """Idempotent: re-registering the same type overwrites the previous check."""
        register_preflight_check("overwrite-type", lambda uow: True)
        register_preflight_check("overwrite-type", lambda uow: False)
        uow = _StubUoW(uow_type="overwrite-type")
        assert is_still_needed(uow) is False

    def test_non_callable_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            register_preflight_check("bad", "not-a-function")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration tests: Executor wires is_still_needed into dispatch
# ---------------------------------------------------------------------------


class TestExecutorPreflightDefaultPath:
    """
    Default path: no type-specific check registered → dispatch proceeds normally.

    This is the regression guard: existing UoW types must not be affected.
    """

    def test_default_true_dispatches_normally(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uow_id = "preflight_default_001"
        _insert_uow(db_path, uow_id, uow_type="executable")

        dispatch_called: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            dispatch_called.append(uid)
            return "inbox-msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", capture)

        executor = Executor(registry)
        result = executor.execute_uow(uow_id)

        assert len(dispatch_called) == 1, "Dispatcher must be called when pre-flight returns True"
        assert dispatch_called[0] == uow_id
        assert result.outcome == ExecutorOutcome.COMPLETE
        assert result.success is True

    def test_registered_true_check_dispatches_normally(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registered check returning True must not block dispatch."""
        uow_id = "preflight_true_001"
        # Use "executable" (valid UoWType); always-True check should dispatch.
        register_preflight_check("executable", lambda uow: True)
        _insert_uow(db_path, uow_id, uow_type="executable")

        dispatch_called: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            dispatch_called.append(uid)
            return "inbox-msg-id"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", capture)

        executor = Executor(registry)
        result = executor.execute_uow(uow_id)

        assert len(dispatch_called) == 1
        assert result.outcome == ExecutorOutcome.COMPLETE
        assert result.success is True


class TestExecutorPreflightFalsePath:
    """
    False path: registered check returns False → no subagent spawned,
    UoW marked complete with outcome_category=heat.

    Tests use "executable" as the UoW type (a valid UoWType enum value) and
    distinguish UoWs by filtering on uow_id inside the check function, since
    the registry.get() call in the executor requires a valid type.
    """

    def test_false_check_does_not_spawn_subagent(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        uow_id = "preflight_false_no_dispatch_001"
        # Use "executable" — a valid UoWType; check identifies UoW by its id.
        register_preflight_check("executable", lambda uow: uow.id != uow_id)
        _insert_uow(db_path, uow_id, uow_type="executable")

        dispatch_called: list[str] = []

        def capture(instructions: str, uid: str) -> str:
            dispatch_called.append(uid)
            return "should-not-be-called"

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", capture)
        monkeypatch.setenv("WOS_OUTPUTS_DIR", str(tmp_path))

        executor = Executor(registry)
        result = executor.execute_uow(uow_id)

        assert len(dispatch_called) == 0, "Dispatcher must NOT be called when pre-flight returns False"
        assert result.outcome == ExecutorOutcome.COMPLETE
        assert result.success is True
        assert "pre-flight" in (result.reason or "").lower()

    def test_false_check_marks_uow_complete_with_heat(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        uow_id = "preflight_heat_outcome_001"
        register_preflight_check("executable", lambda uow: uow.id != uow_id)
        _insert_uow(db_path, uow_id, uow_type="executable")

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", lambda i, u: "noop")
        monkeypatch.setenv("WOS_OUTPUTS_DIR", str(tmp_path))

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        # Status must be ready-for-steward (complete_uow was called)
        status = _get_status(db_path, uow_id)
        assert status == "ready-for-steward", (
            f"UoW must be in ready-for-steward after pre-flight short-circuit, got {status!r}"
        )

        # outcome_category must be heat
        outcome_cat = _get_outcome_category(db_path, uow_id)
        assert outcome_cat == "heat", (
            f"outcome_category must be 'heat' after pre-flight short-circuit, got {outcome_cat!r}"
        )

    def test_false_check_writes_result_json(
        self, registry: Registry, db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        uow_id = "preflight_result_json_001"
        register_preflight_check("executable", lambda uow: uow.id != uow_id)
        _insert_uow(db_path, uow_id, uow_type="executable")

        import orchestration.executor as executor_mod
        monkeypatch.setattr(executor_mod, "_dispatch_via_inbox_functional_engineer", lambda i, u: "noop")
        monkeypatch.setenv("WOS_OUTPUTS_DIR", str(tmp_path))

        executor = Executor(registry)
        executor.execute_uow(uow_id)

        output_ref = _get_output_ref(db_path, uow_id)
        assert output_ref is not None
        result_data = json.loads(_result_json_path(output_ref).read_text())
        assert result_data["outcome"] == "complete"
        assert result_data["success"] is True
        assert result_data["uow_id"] == uow_id

    def test_injected_dispatcher_not_called_on_false(
        self, registry: Registry, db_path: Path, tmp_path: Path
    ) -> None:
        """
        Even with an injected dispatcher (test/CI path), False short-circuits
        without calling the dispatcher.
        """
        uow_id = "preflight_injected_false_001"
        register_preflight_check("executable", lambda uow: uow.id != uow_id)
        _insert_uow(db_path, uow_id, uow_type="executable")

        injected_called: list[str] = []

        def injected(instructions: str, uid: str) -> str:
            injected_called.append(uid)
            return "injected-run"

        import os
        os.environ["WOS_OUTPUTS_DIR"] = str(tmp_path)
        try:
            executor = Executor(registry, dispatcher=injected)
            executor.execute_uow(uow_id)
        finally:
            del os.environ["WOS_OUTPUTS_DIR"]

        assert len(injected_called) == 0, (
            "Injected dispatcher must NOT be called when pre-flight returns False"
        )


# ---------------------------------------------------------------------------
# Unit tests: merge-pr idempotency pre-flight check (issue #927)
# ---------------------------------------------------------------------------


class TestMergePrPreflightCheck:
    """Tests for the merge-pr idempotency pre-flight check (issue #927)."""

    def _make_gh_result(self, state: str, returncode: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=returncode,
            stdout=json.dumps({"state": state}),
            stderr="",
        )

    def test_merged_pr_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MERGED PR: is_still_needed returns False — executor short-circuits."""
        monkeypatch.setattr(
            "orchestration.wos_preflight._subprocess.run",
            lambda *a, **kw: self._make_gh_result("MERGED"),
        )
        uow = _StubMergeUoW(source_ref="github:pr/42")
        assert is_still_needed(uow) is False

    def test_open_pr_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPEN PR: is_still_needed returns True — executor dispatches normally."""
        monkeypatch.setattr(
            "orchestration.wos_preflight._subprocess.run",
            lambda *a, **kw: self._make_gh_result("OPEN"),
        )
        uow = _StubMergeUoW(source_ref="github:pr/42")
        assert is_still_needed(uow) is True

    def test_closed_pr_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLOSED PR: is_still_needed returns True (CLOSED != MERGED)."""
        monkeypatch.setattr(
            "orchestration.wos_preflight._subprocess.run",
            lambda *a, **kw: self._make_gh_result("CLOSED"),
        )
        uow = _StubMergeUoW(source_ref="github:pr/42")
        assert is_still_needed(uow) is True

    def test_gh_failure_returns_true_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh exit non-zero: is_still_needed returns True (fail-open)."""
        monkeypatch.setattr(
            "orchestration.wos_preflight._subprocess.run",
            lambda *a, **kw: self._make_gh_result("", returncode=1),
        )
        uow = _StubMergeUoW(source_ref="github:pr/42")
        assert is_still_needed(uow) is True

    def test_gh_exception_returns_true_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """subprocess.run raises: is_still_needed returns True (fail-open)."""
        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=15)
        monkeypatch.setattr("orchestration.wos_preflight._subprocess.run", raise_timeout)
        uow = _StubMergeUoW(source_ref="github:pr/42")
        assert is_still_needed(uow) is True

    def test_source_ref_without_pr_prefix_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-PR source_ref: check returns True without calling gh."""
        calls: list = []

        def should_not_call(*a, **kw):
            calls.append(True)
            return self._make_gh_result("MERGED")

        monkeypatch.setattr("orchestration.wos_preflight._subprocess.run", should_not_call)
        uow = _StubMergeUoW(source_ref="github:issue/123")
        assert is_still_needed(uow) is True
        assert len(calls) == 0, "gh must not be called for non-PR source_ref"

    def test_derive_preflight_key_pr_source(self) -> None:
        """source_ref='github:pr/99' maps to key 'merge-pr'."""
        uow = _StubMergeUoW(source_ref="github:pr/99")
        assert _derive_preflight_key(uow) == "merge-pr"

    def test_derive_preflight_key_non_pr_source(self) -> None:
        """source_ref='github:issue/99', type='executable' maps to key 'executable'."""
        uow = _StubMergeUoW(source_ref="github:issue/99", uow_type="executable")
        assert _derive_preflight_key(uow) == "executable"
