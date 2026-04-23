"""
Unit tests for oracle_audit.py — emit_oracle_approved.

Covers:
- Empty uow_id → no-op (returns False, no DB write)
- DB not found → no-op (returns False silently)
- UoW not in registry → warning logged, returns False, no row written
- UoW exists → oracle_approved entry written to audit_log, returns True
- pr_ref included in entry when provided
- pr_ref omitted from entry when not provided
- Exception from registry → logged warning, returns False, does not raise
- CLI: --uow-id required; writes event; exits 0
- CLI: exits 0 even when DB absent (non-fatal)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestration.oracle_audit import emit_oracle_approved, _main
from orchestration.registry import Registry, UpsertInserted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(tmp_path: Path) -> tuple[Registry, str]:
    """Create a registry and insert a single UoW, returning (registry, uow_id)."""
    db_path = tmp_path / "registry.db"
    registry = Registry(db_path)
    result = registry.upsert(
        issue_number=9910,
        title="Oracle audit test UoW",
        success_criteria="emit_oracle_approved writes audit entry",
    )
    assert isinstance(result, UpsertInserted)
    return registry, result.id


def _fetch_audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    """Read all audit_log rows for the given uow_id."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _count_oracle_approved(db_path: Path, uow_id: str) -> int:
    """Count oracle_approved audit entries for the UoW."""
    entries = _fetch_audit_entries(db_path, uow_id)
    return sum(1 for e in entries if e.get("event") == "oracle_approved")


# ---------------------------------------------------------------------------
# Tests: emit_oracle_approved
# ---------------------------------------------------------------------------

class TestEmitOracleApprovedEmptyUowId:
    """Empty uow_id is a silent no-op."""

    def test_returns_false_for_empty_string(self, tmp_path: Path) -> None:
        db_path = tmp_path / "registry.db"
        Registry(db_path)  # create DB so absence is not the reason
        result = emit_oracle_approved(uow_id="", db_path=db_path)
        assert result is False

    def test_no_audit_entry_written_for_empty_string(self, tmp_path: Path) -> None:
        db_path = tmp_path / "registry.db"
        Registry(db_path)
        emit_oracle_approved(uow_id="", db_path=db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event = 'oracle_approved'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0


class TestEmitOracleApprovedDbAbsent:
    """When registry.db does not exist, silently return False."""

    def test_returns_false_when_db_absent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent.db"
        result = emit_oracle_approved(uow_id="wos_20260423_abc", db_path=db_path)
        assert result is False

    def test_does_not_raise_when_db_absent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent.db"
        # Must not raise; just return False
        try:
            emit_oracle_approved(uow_id="wos_20260423_abc", db_path=db_path)
        except Exception as exc:
            pytest.fail(f"emit_oracle_approved raised unexpectedly: {exc}")


class TestEmitOracleApprovedUowNotFound:
    """When the UoW is not in the registry, log a warning and return False."""

    def test_returns_false_for_unknown_uow(self, tmp_path: Path) -> None:
        db_path = tmp_path / "registry.db"
        Registry(db_path)  # create empty registry
        result = emit_oracle_approved(
            uow_id="wos_does_not_exist",
            db_path=db_path,
        )
        assert result is False

    def test_no_audit_entry_for_unknown_uow(self, tmp_path: Path) -> None:
        db_path = tmp_path / "registry.db"
        Registry(db_path)
        emit_oracle_approved(uow_id="wos_does_not_exist", db_path=db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event = 'oracle_approved'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0


class TestEmitOracleApprovedSuccess:
    """Happy path: UoW exists → oracle_approved entry written."""

    def test_returns_true_when_uow_exists(self, tmp_path: Path) -> None:
        registry, uow_id = _make_registry(tmp_path)
        result = emit_oracle_approved(
            uow_id=uow_id,
            pr_ref="PR #864",
            db_path=registry.db_path,
        )
        assert result is True

    def test_oracle_approved_entry_written(self, tmp_path: Path) -> None:
        registry, uow_id = _make_registry(tmp_path)
        emit_oracle_approved(uow_id=uow_id, pr_ref="PR #864", db_path=registry.db_path)
        assert _count_oracle_approved(registry.db_path, uow_id) == 1

    def test_event_field_is_oracle_approved(self, tmp_path: Path) -> None:
        registry, uow_id = _make_registry(tmp_path)
        emit_oracle_approved(uow_id=uow_id, db_path=registry.db_path)
        entries = _fetch_audit_entries(registry.db_path, uow_id)
        oracle_entries = [e for e in entries if e.get("event") == "oracle_approved"]
        assert len(oracle_entries) == 1
        assert oracle_entries[0]["event"] == "oracle_approved"

    def test_pr_ref_stored_in_note_when_provided(self, tmp_path: Path) -> None:
        registry, uow_id = _make_registry(tmp_path)
        emit_oracle_approved(uow_id=uow_id, pr_ref="PR #864", db_path=registry.db_path)
        entries = _fetch_audit_entries(registry.db_path, uow_id)
        oracle_entries = [e for e in entries if e.get("event") == "oracle_approved"]
        assert len(oracle_entries) == 1
        note_raw = oracle_entries[0].get("note") or ""
        note = json.loads(note_raw)
        assert note.get("pr_ref") == "PR #864"

    def test_pr_ref_absent_from_note_when_not_provided(self, tmp_path: Path) -> None:
        registry, uow_id = _make_registry(tmp_path)
        emit_oracle_approved(uow_id=uow_id, pr_ref=None, db_path=registry.db_path)
        entries = _fetch_audit_entries(registry.db_path, uow_id)
        oracle_entries = [e for e in entries if e.get("event") == "oracle_approved"]
        assert len(oracle_entries) == 1
        note_raw = oracle_entries[0].get("note") or ""
        note = json.loads(note_raw)
        assert "pr_ref" not in note

    def test_multiple_calls_write_multiple_entries(self, tmp_path: Path) -> None:
        """Two APPROVED verdicts for the same UoW write two entries (spiral counter)."""
        registry, uow_id = _make_registry(tmp_path)
        emit_oracle_approved(uow_id=uow_id, pr_ref="PR #864", db_path=registry.db_path)
        emit_oracle_approved(uow_id=uow_id, pr_ref="PR #867", db_path=registry.db_path)
        assert _count_oracle_approved(registry.db_path, uow_id) == 2

    def test_env_var_db_path_resolution(self, tmp_path: Path, monkeypatch) -> None:
        """When db_path is None, REGISTRY_DB_PATH env var is used."""
        registry, uow_id = _make_registry(tmp_path)
        monkeypatch.setenv("REGISTRY_DB_PATH", str(registry.db_path))
        result = emit_oracle_approved(uow_id=uow_id, pr_ref="PR #864", db_path=None)
        assert result is True
        assert _count_oracle_approved(registry.db_path, uow_id) == 1


class TestEmitOracleApprovedDoesNotRaise:
    """emit_oracle_approved must never raise, even on unexpected errors."""

    def test_does_not_raise_on_corrupt_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "corrupt.db"
        db_path.write_text("this is not a sqlite database")
        try:
            emit_oracle_approved(uow_id="wos_test", db_path=db_path)
        except Exception as exc:
            pytest.fail(f"emit_oracle_approved raised on corrupt DB: {exc}")


# ---------------------------------------------------------------------------
# Tests: CLI entry point (_main)
# ---------------------------------------------------------------------------

class TestCliMain:
    """CLI invocation via _main(argv)."""

    def test_requires_uow_id(self, tmp_path: Path) -> None:
        """Missing --uow-id raises SystemExit (argparse error)."""
        with pytest.raises(SystemExit):
            _main(["--pr-ref", "PR #864"])

    def test_exits_zero_on_success(self, tmp_path: Path) -> None:
        registry, uow_id = _make_registry(tmp_path)
        exit_code = _main([
            "--uow-id", uow_id,
            "--pr-ref", "PR #864",
            "--db-path", str(registry.db_path),
        ])
        assert exit_code == 0

    def test_writes_event_via_cli(self, tmp_path: Path) -> None:
        registry, uow_id = _make_registry(tmp_path)
        _main([
            "--uow-id", uow_id,
            "--pr-ref", "PR #864",
            "--db-path", str(registry.db_path),
        ])
        assert _count_oracle_approved(registry.db_path, uow_id) == 1

    def test_exits_zero_when_db_absent(self, tmp_path: Path) -> None:
        """DB absent is not a CLI error — exits 0."""
        exit_code = _main([
            "--uow-id", "wos_test_123",
            "--db-path", str(tmp_path / "nonexistent.db"),
        ])
        assert exit_code == 0

    def test_exits_zero_when_uow_not_found(self, tmp_path: Path) -> None:
        db_path = tmp_path / "registry.db"
        Registry(db_path)  # empty registry
        exit_code = _main([
            "--uow-id", "wos_does_not_exist",
            "--db-path", str(db_path),
        ])
        assert exit_code == 0
