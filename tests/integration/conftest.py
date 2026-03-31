"""
Shared fixtures for WOS integration tests.

Provides a `db` fixture that creates a fresh SQLite database and applies all
real migration files from src/orchestration/migrations/ in order. Tests that
need a fully-migrated schema should use this fixture instead of copy-pasting
DDL.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Generator

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.migrate import run_migrations
from orchestration.registry import Registry


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """
    Fresh SQLite DB path with all real migrations applied.

    Creates the DB in a tmp_path directory unique to each test, applies all
    migration files from src/orchestration/migrations/ in order, and returns
    the path. Tests that need a Registry can construct one from this path.
    """
    db_path = tmp_path / "wos_test.db"
    run_migrations(db_path)
    return db_path


@pytest.fixture
def db_registry(db: Path) -> Registry:
    """Registry constructed on top of the migrated `db` fixture."""
    return Registry(db)


@pytest.fixture
def db_conn(db: Path, db_registry: Registry) -> Generator[sqlite3.Connection, None, None]:
    """
    Open raw SQLite connection to the migrated DB for direct SQL assertions.
    Closed after the test.
    """
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()
