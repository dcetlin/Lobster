"""
Tests for the StrEnum read-boundary enforcement: UoWType, UoWPosture, UoWRegister.

The read boundary is _row_to_uow in Registry. Every raw DB row must be
converted through the StrEnum there — raw string comparisons are eliminated.

Tests are named after the behavior being verified, not the mechanism.
"""

import sqlite3
from pathlib import Path

import pytest

from src.orchestration.registry import (
    Registry,
    UoW,
    UoWRegister,
    UoWStatus,
    UoWType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path) -> Registry:
    import os
    os.environ["REGISTRY_DB_PATH"] = str(db_path)
    reg = Registry(db_path=db_path)
    yield reg
    del os.environ["REGISTRY_DB_PATH"]


def _insert_raw_uow(
    db_path: Path,
    uow_id: str,
    status: str,
    type_: str,
    posture: str,
    register: str,
) -> None:
    """Insert a raw UoW row, bypassing Registry to test read-boundary coercion."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = _open_db(db_path)
    conn.execute(
        """
        INSERT INTO uow_registry
          (id, type, source, source_issue_number, sweep_date, status, posture,
           created_at, updated_at, summary, success_criteria)
        VALUES (?, ?, 'test', 1, '2026-01-01', ?, ?,
                ?, ?, 'test summary', 'test done')
        """,
        (uow_id, type_, status, posture, now, now),
    )
    conn.execute(
        "UPDATE uow_registry SET register = ? WHERE id = ?",
        (register, uow_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# UoWType StrEnum
# ---------------------------------------------------------------------------

class TestUoWTypeEnum:
    def test_canonical_string_values(self):
        """UoWType values must match the DB defaults exactly."""
        assert UoWType.EXECUTABLE == "executable"
        assert UoWType.INLINE == "inline"

    def test_is_str_subclass(self):
        """UoWType must be StrEnum so it serializes as a plain string."""
        assert isinstance(UoWType.EXECUTABLE, str)
        assert str(UoWType.EXECUTABLE) == "executable"

    def test_row_to_uow_coerces_type_to_enum(self, registry: Registry, db_path: Path):
        """_row_to_uow must coerce the raw DB 'type' string into UoWType."""
        _insert_raw_uow(db_path, "uow_type_test_1", "proposed", "executable", "solo", "operational")
        uow = registry.get("uow_type_test_1")
        assert uow is not None
        assert isinstance(uow.type, UoWType)
        assert uow.type == UoWType.EXECUTABLE

    def test_row_to_uow_coerces_inline_type(self, registry: Registry, db_path: Path):
        """_row_to_uow must coerce 'inline' type correctly."""
        _insert_raw_uow(db_path, "uow_type_test_2", "proposed", "inline", "solo", "operational")
        uow = registry.get("uow_type_test_2")
        assert uow is not None
        assert uow.type == UoWType.INLINE

    def test_unknown_type_falls_back_to_executable(self, registry: Registry, db_path: Path):
        """Legacy rows with unknown type values fall back to EXECUTABLE without raising."""
        _insert_raw_uow(db_path, "uow_type_test_3", "proposed", "legacy-unknown", "solo", "operational")
        uow = registry.get("uow_type_test_3")
        assert uow is not None
        # Falls back to EXECUTABLE default (same as if NULL)
        assert uow.type == UoWType.EXECUTABLE


# ---------------------------------------------------------------------------
# UoWRegister StrEnum
# ---------------------------------------------------------------------------

VALID_REGISTERS = [
    ("operational", "OPERATIONAL"),
    ("iterative-convergent", "ITERATIVE_CONVERGENT"),
    ("philosophical", "PHILOSOPHICAL"),
    ("human-judgment", "HUMAN_JUDGMENT"),
]


class TestUoWRegisterEnum:
    def test_canonical_string_values(self):
        """UoWRegister values must match the strings used throughout the codebase."""
        assert UoWRegister.OPERATIONAL == "operational"
        assert UoWRegister.ITERATIVE_CONVERGENT == "iterative-convergent"
        assert UoWRegister.PHILOSOPHICAL == "philosophical"
        assert UoWRegister.HUMAN_JUDGMENT == "human-judgment"

    def test_is_str_subclass(self):
        """UoWRegister must be StrEnum."""
        assert isinstance(UoWRegister.OPERATIONAL, str)
        assert str(UoWRegister.HUMAN_JUDGMENT) == "human-judgment"

    @pytest.mark.parametrize("raw_value, attr_name", VALID_REGISTERS)
    def test_row_to_uow_coerces_register_to_enum(
        self, registry: Registry, db_path: Path, raw_value: str, attr_name: str
    ):
        """_row_to_uow must coerce every valid register string to UoWRegister."""
        uow_id = f"uow_reg_test_{attr_name.lower()}"
        _insert_raw_uow(db_path, uow_id, "proposed", "executable", "solo", raw_value)
        uow = registry.get(uow_id)
        assert uow is not None
        assert isinstance(uow.register, UoWRegister)
        expected = getattr(UoWRegister, attr_name)
        assert uow.register == expected

    def test_unknown_register_falls_back_to_operational(self, registry: Registry, db_path: Path):
        """Legacy rows with unknown register values fall back to OPERATIONAL without raising."""
        _insert_raw_uow(db_path, "uow_reg_unknown", "proposed", "executable", "solo", "legacy-unknown")
        uow = registry.get("uow_reg_unknown")
        assert uow is not None
        assert uow.register == UoWRegister.OPERATIONAL

    def test_omitted_register_defaults_to_operational(self, registry: Registry, db_path: Path):
        """Rows where register is omitted from INSERT use the DB DEFAULT 'operational'.

        The column has NOT NULL + DEFAULT 'operational', so NULL is not a valid
        stored value. But the _coerce_enum path still defaults to OPERATIONAL for
        any missing or unrecognised value — verified here via the DB DEFAULT path.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = _open_db(db_path)
        # Insert without specifying register — DB DEFAULT 'operational' applies.
        conn.execute(
            """
            INSERT INTO uow_registry
              (id, type, source, source_issue_number, sweep_date, status, posture,
               created_at, updated_at, summary, success_criteria)
            VALUES (?, 'executable', 'test', 99, '2026-01-01', 'proposed', 'solo',
                    ?, ?, 'summary', 'done')
            """,
            ("uow_reg_omit", now, now),
        )
        conn.commit()
        conn.close()
        uow = registry.get("uow_reg_omit")
        assert uow is not None
        assert uow.register == UoWRegister.OPERATIONAL


# ---------------------------------------------------------------------------
# UoWStatus import from steward matches registry definition
# ---------------------------------------------------------------------------

class TestStewardImportsRegistryStatus:
    def test_steward_uses_registry_uow_status(self):
        """steward.py must import UoWStatus from registry, not define its own."""
        from src.orchestration import steward
        from src.orchestration import registry

        # After deduplication, the UoWStatus in steward's module namespace
        # must be the same object as registry.UoWStatus.
        steward_status = getattr(steward, "UoWStatus", None)
        assert steward_status is not None, "UoWStatus not found in steward module"
        assert steward_status is registry.UoWStatus, (
            "steward.UoWStatus is not the same object as registry.UoWStatus — "
            "duplicate definition still present"
        )


# ---------------------------------------------------------------------------
# UoWType appears on UoW dataclass field
# ---------------------------------------------------------------------------

class TestUoWDataclassTyping:
    def test_uow_type_field_is_enum_after_read(self, registry: Registry, db_path: Path):
        """After a round-trip through Registry.get(), uow.type is UoWType, not str."""
        _insert_raw_uow(db_path, "uow_dc_type", "proposed", "executable", "solo", "operational")
        uow = registry.get("uow_dc_type")
        assert isinstance(uow.type, UoWType)

    def test_uow_register_field_is_enum_after_read(self, registry: Registry, db_path: Path):
        """After a round-trip through Registry.get(), uow.register is UoWRegister, not str."""
        _insert_raw_uow(db_path, "uow_dc_reg", "proposed", "executable", "solo", "philosophical")
        uow = registry.get("uow_dc_reg")
        assert isinstance(uow.register, UoWRegister)
        assert uow.register == UoWRegister.PHILOSOPHICAL

    def test_uow_register_equality_with_raw_string(self, registry: Registry, db_path: Path):
        """UoWRegister is StrEnum, so uow.register == 'operational' still works."""
        _insert_raw_uow(db_path, "uow_dc_eq", "proposed", "executable", "solo", "operational")
        uow = registry.get("uow_dc_eq")
        # StrEnum: the enum value IS a str — backward-compat comparisons continue to work
        assert uow.register == "operational"
        assert uow.register == UoWRegister.OPERATIONAL

    def test_uow_type_equality_with_raw_string(self, registry: Registry, db_path: Path):
        """UoWType is StrEnum, so uow.type == 'executable' still works."""
        _insert_raw_uow(db_path, "uow_dc_type_eq", "proposed", "executable", "solo", "operational")
        uow = registry.get("uow_dc_type_eq")
        assert uow.type == "executable"
        assert uow.type == UoWType.EXECUTABLE
