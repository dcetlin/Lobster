"""
Unit tests for per-cycle steward trace logging (_append_cycle_trace).

Tests verify that:
- A .cycles.jsonl file is written when a UoW is prescribed
- Two JSONL entries are written over two cycles with correct cycle_num values
- subagent_excerpt is truncated to 200 chars with a trailing ellipsis
- Each JSONL entry has the required schema fields
- Appending to an existing file does not overwrite prior entries

All tests use tmp_path for artifact_dir so no production paths are touched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Named constants (mirror the spec to keep tests coupled to requirements)
# ---------------------------------------------------------------------------

EXCERPT_MAX_CHARS = 200
ELLIPSIS_CHAR = "\u2026"


# ---------------------------------------------------------------------------
# DB helpers (shared with test_steward.py pattern)
# ---------------------------------------------------------------------------

def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _apply_phase2_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS uow_registry (
            id                  TEXT    PRIMARY KEY,
            type                TEXT    NOT NULL DEFAULT 'executable',
            source              TEXT    NOT NULL,
            source_issue_number INTEGER,
            sweep_date          TEXT,
            status              TEXT    NOT NULL DEFAULT 'proposed',
            posture             TEXT    NOT NULL DEFAULT 'solo',
            agent               TEXT,
            children            TEXT    DEFAULT '[]',
            parent              TEXT,
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL,
            started_at          TEXT,
            completed_at        TEXT,
            summary             TEXT    NOT NULL,
            output_ref          TEXT,
            hooks_applied       TEXT    DEFAULT '[]',
            route_reason        TEXT,
            route_evidence      TEXT    DEFAULT '{}',
            trigger             TEXT    DEFAULT '{"type": "immediate"}',
            vision_ref          TEXT    DEFAULT NULL,
            workflow_artifact   TEXT    NULL,
            success_criteria    TEXT    NULL,
            prescribed_skills   TEXT    NULL,
            steward_cycles      INTEGER NOT NULL DEFAULT 0,
            timeout_at          TEXT    NULL,
            estimated_runtime   INTEGER NULL,
            steward_agenda      TEXT    NULL,
            steward_log         TEXT    NULL,
            UNIQUE(source_issue_number, sweep_date)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            uow_id      TEXT    NOT NULL,
            event       TEXT    NOT NULL,
            from_status TEXT,
            to_status   TEXT,
            agent       TEXT,
            note        TEXT
        );

        CREATE VIEW IF NOT EXISTS executor_uow_view AS
        SELECT id, type, source, source_issue_number, sweep_date,
               status, posture, agent, children, parent,
               created_at, updated_at, started_at, completed_at,
               summary, output_ref, hooks_applied,
               route_reason, route_evidence, trigger, vision_ref,
               workflow_artifact, success_criteria, prescribed_skills,
               steward_cycles, timeout_at, estimated_runtime
        FROM uow_registry;
    """)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_uow_row(
    conn: sqlite3.Connection,
    uow_id: str | None = None,
    status: str = "ready-for-steward",
    steward_cycles: int = 0,
    output_ref: str | None = None,
    audit_log_entries: list[dict] | None = None,
    steward_agenda: str | None = None,
    steward_log: str | None = None,
    source_issue_number: int = 42,
    summary: str = "Test UoW",
    success_criteria: str | None = "Output file exists with non-empty content",
) -> str:
    if uow_id is None:
        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO uow_registry
            (id, type, source, source_issue_number, sweep_date, status, posture,
             created_at, updated_at, summary, output_ref, steward_cycles,
             steward_agenda, steward_log, success_criteria,
             route_evidence, trigger)
        VALUES (?, 'executable', 'github:issue/42', ?, '2026-01-01', ?, 'solo',
                ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}')
        """,
        (uow_id, source_issue_number, status, now, now, summary,
         output_ref, steward_cycles, steward_agenda, steward_log,
         success_criteria),
    )
    if audit_log_entries:
        for entry in audit_log_entries:
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (_now_iso(), uow_id,
                 entry.get("event", "unknown"),
                 entry.get("from_status"),
                 entry.get("to_status"),
                 entry.get("agent"),
                 json.dumps(entry)),
            )
    conn.commit()
    return uow_id


def _get_uow(db_path: Path, uow_id: str) -> dict:
    conn = _open_db(db_path)
    row = conn.execute("SELECT * FROM uow_registry WHERE id = ?", (uow_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def _ensure_registry_has_phase2_methods(registry) -> None:
    """Patch registry with Phase 2 methods when not yet on main (pre-merge)."""
    import types

    if not hasattr(registry, "transition"):
        def transition(self, uow_id: str, to_status: str, where_status: str) -> int:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = _now_iso()
                cursor = conn.execute(
                    "UPDATE uow_registry SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (to_status, now, uow_id, where_status),
                )
                rows = cursor.rowcount
                conn.commit()
                return rows
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        registry.transition = types.MethodType(transition, registry)

    if not hasattr(registry, "append_audit_log"):
        def append_audit_log(self, uow_id: str, entry: dict) -> None:
            conn = self._connect()
            note_json = json.dumps(entry)
            try:
                conn.execute(
                    "INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note) VALUES (?, ?, ?, NULL, NULL, NULL, ?)",
                    (_now_iso(), uow_id, entry.get("event", "unknown"), note_json),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        registry.append_audit_log = types.MethodType(append_audit_log, registry)


def _mock_github_client_open(issue_number: int):
    from src.orchestration.steward import IssueInfo
    return IssueInfo(
        status_code=200,
        state="open",
        labels=[],
        body=f"Issue #{issue_number}: implement this feature.\n\nAcceptance criteria:\n- Feature works",
        title=f"Test issue {issue_number}",
    )


def _import_steward():
    from src.orchestration import steward
    return steward


def _read_cycles_jsonl(trace_path: Path) -> list[dict]:
    """Read all JSONL entries from a .cycles.jsonl file."""
    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "registry.db"
    conn = _open_db(path)
    _apply_phase2_schema(conn)
    conn.close()
    return path


@pytest.fixture
def registry(db_path: Path):
    from src.orchestration.registry import Registry
    return Registry(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStewardCycleTrace:
    """Per-cycle steward trace logging written to <artifact_dir>/<uow_id>.cycles.jsonl."""

    def test_cycle_trace_written_on_prescribe(self, db_path, registry, tmp_path):
        """A .cycles.jsonl file is created after a UoW is prescribed."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
        )
        conn.close()

        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
            llm_prescriber=None,
        )

        trace_path = artifact_dir / f"{uow_id}.cycles.jsonl"
        assert trace_path.exists(), (
            f".cycles.jsonl file must be written after prescription; "
            f"expected {trace_path}"
        )
        entries = _read_cycles_jsonl(trace_path)
        assert len(entries) >= 1, "At least one cycle trace entry must be written"
        assert entries[0]["next_action"] == "prescribed"

    def test_cycle_trace_two_cycles(self, db_path, registry, tmp_path):
        """Two steward cycles produce two JSONL entries with cycle_num 0 and 1."""
        _ensure_registry_has_phase2_methods(registry)
        steward = _import_steward()

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        conn = _open_db(db_path)
        uow_id = _make_uow_row(
            conn,
            status="ready-for-steward",
            steward_cycles=0,
        )
        conn.close()

        # First cycle: cycle_num 0
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
            llm_prescriber=None,
        )

        # Reset to ready-for-steward for second cycle
        conn = _open_db(db_path)
        conn.execute(
            "UPDATE uow_registry SET status = 'ready-for-steward' WHERE id = ?",
            (uow_id,),
        )
        conn.commit()
        conn.close()

        # Second cycle: cycle_num 1
        steward.run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=_mock_github_client_open,
            artifact_dir=artifact_dir,
            llm_prescriber=None,
        )

        trace_path = artifact_dir / f"{uow_id}.cycles.jsonl"
        assert trace_path.exists()
        entries = _read_cycles_jsonl(trace_path)
        assert len(entries) == 2, f"Expected 2 entries, got {len(entries)}: {entries}"

        cycle_nums = [e["cycle_num"] for e in entries]
        assert 0 in cycle_nums, f"Expected cycle_num=0 in entries: {entries}"
        assert 1 in cycle_nums, f"Expected cycle_num=1 in entries: {entries}"

    def test_subagent_excerpt_truncated(self, tmp_path):
        """subagent_excerpt is truncated to 200 chars with trailing ellipsis."""
        steward = _import_steward()

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"
        long_text = "x" * 300  # 300 chars, exceeds the 200-char limit

        steward._append_cycle_trace(
            uow_id=uow_id,
            cycle_num=0,
            subagent_excerpt=long_text,
            return_reason="observation_complete",
            next_action="prescribed",
            artifact_dir=artifact_dir,
        )

        trace_path = artifact_dir / f"{uow_id}.cycles.jsonl"
        assert trace_path.exists()
        entries = _read_cycles_jsonl(trace_path)
        assert len(entries) == 1
        excerpt = entries[0]["subagent_excerpt"]

        assert len(excerpt) == EXCERPT_MAX_CHARS + len(ELLIPSIS_CHAR), (
            f"Truncated excerpt must be {EXCERPT_MAX_CHARS} content chars + ellipsis; "
            f"got length {len(excerpt)}"
        )
        assert excerpt.endswith(ELLIPSIS_CHAR), (
            f"Truncated excerpt must end with ellipsis (U+2026); got: {excerpt[-5:]!r}"
        )
        assert excerpt[:EXCERPT_MAX_CHARS] == "x" * EXCERPT_MAX_CHARS

    def test_cycle_trace_fields_schema(self, tmp_path):
        """Each cycle trace entry contains exactly the required fields."""
        steward = _import_steward()

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"

        steward._append_cycle_trace(
            uow_id=uow_id,
            cycle_num=2,
            subagent_excerpt="output snippet",
            return_reason="needs_steward_review",
            next_action="prescribed",
            artifact_dir=artifact_dir,
        )

        trace_path = artifact_dir / f"{uow_id}.cycles.jsonl"
        entries = _read_cycles_jsonl(trace_path)
        assert len(entries) == 1
        entry = entries[0]

        required_fields = {"cycle_num", "subagent_excerpt", "return_reason", "next_action", "timestamp"}
        assert required_fields == set(entry.keys()), (
            f"Entry must have exactly {required_fields}; got {set(entry.keys())}"
        )

        assert entry["cycle_num"] == 2
        assert entry["subagent_excerpt"] == "output snippet"
        assert entry["return_reason"] == "needs_steward_review"
        assert entry["next_action"] == "prescribed"

        # Timestamp must be parseable as ISO 8601 UTC
        ts = datetime.fromisoformat(entry["timestamp"])
        assert ts.tzinfo is not None, "timestamp must include timezone info"

    def test_cycle_trace_appends_not_overwrites(self, tmp_path):
        """Calling _append_cycle_trace twice accumulates both entries (append semantics)."""
        steward = _import_steward()

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()

        uow_id = f"uow_test_{uuid.uuid4().hex[:6]}"

        steward._append_cycle_trace(
            uow_id=uow_id,
            cycle_num=0,
            subagent_excerpt="first output",
            return_reason="",
            next_action="prescribed",
            artifact_dir=artifact_dir,
        )
        steward._append_cycle_trace(
            uow_id=uow_id,
            cycle_num=1,
            subagent_excerpt="second output",
            return_reason="observation_complete",
            next_action="done",
            artifact_dir=artifact_dir,
        )

        trace_path = artifact_dir / f"{uow_id}.cycles.jsonl"
        entries = _read_cycles_jsonl(trace_path)

        assert len(entries) == 2, (
            f"Both entries must be present (append, not overwrite); got {len(entries)}: {entries}"
        )
        assert entries[0]["cycle_num"] == 0
        assert entries[0]["subagent_excerpt"] == "first output"
        assert entries[1]["cycle_num"] == 1
        assert entries[1]["subagent_excerpt"] == "second output"
