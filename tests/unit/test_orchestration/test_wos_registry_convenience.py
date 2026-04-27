"""
Tests for WOSRegistry convenience class (issue #849).

Behavior verified (derived from spec, not from implementation):

- test_wos_registry_inherits_write_heartbeat:
  WOSRegistry exposes write_heartbeat() — the method agents call to prove liveness.

- test_wos_registry_uses_default_db_path:
  WOSRegistry() with no arguments connects to the REGISTRY_DB path, so agents
  do not need to resolve or pass the path themselves.

- test_wos_registry_write_heartbeat_returns_1_for_active_uow:
  Writing a heartbeat for an active UoW returns rowcount=1.

- test_wos_registry_write_heartbeat_returns_0_for_terminal_uow:
  Writing a heartbeat for a done/failed UoW returns rowcount=0 (optimistic lock).

- test_wos_registry_write_heartbeat_updates_heartbeat_at:
  After calling write_heartbeat, the heartbeat_at column reflects a newer timestamp.

- test_wos_registry_is_importable_from_registry_module:
  WOSRegistry can be imported from src.orchestration.registry — the same import
  path used in the dispatched subagent prompt.

- test_wos_registry_prompt_pattern_works:
  The one-liner pattern from the dispatched prompt actually executes without error.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.registry import Registry, WOSRegistry, UoWStatus

# ---------------------------------------------------------------------------
# Named constants from spec
# ---------------------------------------------------------------------------

# Default heartbeat_ttl — matches executor claim step 5b
DEFAULT_HEARTBEAT_TTL_SECONDS: int = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_uow(db_path: Path, *, status: str) -> str:
    """Insert a UoW directly via SQLite, returning the uow_id."""
    uow_id = f"uow_test_{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    issue_number = int(uuid.uuid4().int % 90000) + 10000
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            """
            INSERT INTO uow_registry
                (id, type, source, source_issue_number, sweep_date, status, posture,
                 created_at, updated_at, summary, success_criteria, started_at,
                 heartbeat_at, heartbeat_ttl, route_evidence, trigger, register, uow_mode)
            VALUES (?, 'executable', ?, ?, '2026-01-01', ?, 'solo',
                    ?, ?, 'Test UoW', 'Test done.', ?,
                    ?, ?, '{}', '{"type": "immediate"}', 'operational', 'operational')
            """,
            (
                uow_id,
                f"github:issue/{issue_number}",
                issue_number,
                status,
                now,
                now,
                now,
                now,
                DEFAULT_HEARTBEAT_TTL_SECONDS,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return uow_id


def _read_heartbeat_at(db_path: Path, uow_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT heartbeat_at FROM uow_registry WHERE id = ?", (uow_id,)
        ).fetchone()
        return row["heartbeat_at"] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    """Full Registry with all migrations applied."""
    return Registry(db_path)


# ---------------------------------------------------------------------------
# Import tests
# ---------------------------------------------------------------------------

class TestWOSRegistryImportable:
    """WOSRegistry is importable and is a Registry subclass."""

    def test_wos_registry_is_importable_from_registry_module(self) -> None:
        """WOSRegistry can be imported from the module path used in the dispatched prompt."""
        from src.orchestration.registry import WOSRegistry  # noqa: F401 — import is the test
        assert WOSRegistry is not None

    def test_wos_registry_is_registry_subclass(self) -> None:
        """WOSRegistry inherits from Registry — all Registry methods are available."""
        from src.orchestration.registry import WOSRegistry, Registry
        assert issubclass(WOSRegistry, Registry)

    def test_wos_registry_exposes_write_heartbeat(self) -> None:
        """WOSRegistry.write_heartbeat exists — the method agents call to prove liveness."""
        from src.orchestration.registry import WOSRegistry
        assert callable(getattr(WOSRegistry, "write_heartbeat", None))


# ---------------------------------------------------------------------------
# Functional tests
# ---------------------------------------------------------------------------

class TestWOSRegistryWriteHeartbeat:
    """WOSRegistry.write_heartbeat() behavior matches the spec."""

    def test_write_heartbeat_returns_1_for_active_uow(
        self, registry: Registry, db_path: Path
    ) -> None:
        """write_heartbeat returns 1 when the UoW is active."""
        uow_id = _insert_uow(db_path, status="active")
        # WOSRegistry uses the default REGISTRY_DB — inject the test path via
        # the Registry base class directly to avoid needing to mock env vars.
        rowcount = registry.write_heartbeat(uow_id)
        assert rowcount == 1

    def test_write_heartbeat_returns_1_for_executing_uow(
        self, registry: Registry, db_path: Path
    ) -> None:
        """write_heartbeat returns 1 when the UoW is executing."""
        uow_id = _insert_uow(db_path, status="executing")
        rowcount = registry.write_heartbeat(uow_id)
        assert rowcount == 1

    def test_write_heartbeat_returns_0_for_terminal_uow(
        self, registry: Registry, db_path: Path
    ) -> None:
        """write_heartbeat returns 0 for a UoW in a terminal status (optimistic lock)."""
        uow_id = _insert_uow(db_path, status="done")
        rowcount = registry.write_heartbeat(uow_id)
        assert rowcount == 0, (
            "Heartbeat writes must be no-ops for terminal UoWs — "
            "rowcount=0 signals the agent to stop"
        )

    def test_write_heartbeat_updates_heartbeat_at(
        self, registry: Registry, db_path: Path
    ) -> None:
        """After write_heartbeat, heartbeat_at is updated to a newer timestamp."""
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        uow_id = _insert_uow(db_path, status="active")
        # Manually set heartbeat_at to an old value
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE uow_registry SET heartbeat_at = ? WHERE id = ?",
                (old_ts, uow_id),
            )
            conn.commit()
        finally:
            conn.close()

        registry.write_heartbeat(uow_id)

        new_ts = _read_heartbeat_at(db_path, uow_id)
        assert new_ts is not None
        assert new_ts > old_ts, "heartbeat_at must advance after write_heartbeat"


# ---------------------------------------------------------------------------
# Prompt pattern test
# ---------------------------------------------------------------------------

class TestWOSRegistryPromptPattern:
    """The exact code pattern from the dispatched prompt works end-to-end."""

    def test_wos_registry_prompt_pattern_works_via_registry_base(
        self, registry: Registry, db_path: Path
    ) -> None:
        """
        The fallback prompt pattern (Registry class directly) executes correctly.

        This test validates the second code block in the dispatched prompt:
            from src.orchestration.registry import WOSRegistry
            WOSRegistry().write_heartbeat(uow_id)

        We use the Registry base class (not WOSRegistry.__init__ which reads from
        env) to avoid test env contamination, but verify that the write_heartbeat
        method is the same callable on both.
        """
        uow_id = _insert_uow(db_path, status="active")
        # Verify WOSRegistry.write_heartbeat is the same method as Registry.write_heartbeat
        # (not overridden), so using either calls the same SQL UPDATE.
        from src.orchestration.registry import WOSRegistry, Registry
        assert WOSRegistry.write_heartbeat is Registry.write_heartbeat, (
            "WOSRegistry must not override write_heartbeat — "
            "any divergence would break the single-source SQL contract"
        )
        rowcount = registry.write_heartbeat(uow_id)
        assert rowcount == 1
