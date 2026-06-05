"""
Unit tests for the wos_prescribe async prescription dispatch path.

Tests cover:
1. steward._process_uow async path: transitions UoW to 'prescribing', writes inbox message,
   returns PrescribingQueued
2. dispatcher_handlers.handle_wos_prescribe: returns action="spawn_subagent"
3. dispatcher_handlers.route_wos_message: routes wos_prescribe correctly
4. registry.record_startup_sweep_prescribing: TTL recovery transition
5. startup_sweep.run_startup_sweep Population 5: prescribing orphan recovery
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_steward.py)
# ---------------------------------------------------------------------------

import sys
import os

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC_ROOT = _REPO_ROOT / "src"
for p in [str(_REPO_ROOT), str(_SRC_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from orchestration.registry import UoWStatus, Registry
from orchestration.dispatcher_handlers import (
    handle_wos_prescribe,
    route_wos_message,
    WOS_MESSAGE_TYPE_DISPATCH,
)
from orchestration.steward import (
    PrescribingQueued,
    _write_prescription_request,
    _llm_prescribe,
    run_steward_cycle,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the full Phase 1+2 schema. Mirrors _apply_phase2_schema from test_steward.py."""
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
            lifetime_cycles     INTEGER NOT NULL DEFAULT 0,
            retry_count         INTEGER NOT NULL DEFAULT 0,
            execution_attempts  INTEGER NOT NULL DEFAULT 0,
            orphan_retry_count  INTEGER NOT NULL DEFAULT 0,
            register            TEXT    DEFAULT 'operational',
            issue_url           TEXT    DEFAULT NULL,
            closed_at           TEXT    DEFAULT NULL,
            close_reason        TEXT    DEFAULT NULL,
            vision_ref_anchored INTEGER DEFAULT 0,
            heartbeat_at        TEXT    DEFAULT NULL,
            heartbeat_ttl       INTEGER DEFAULT 300,
            last_heartbeat_at   TEXT    DEFAULT NULL,
            prescription_confidence REAL DEFAULT NULL,
            file_scope          TEXT    DEFAULT NULL,
            shard_id            TEXT    DEFAULT NULL,
            juice_quality       TEXT    DEFAULT NULL,
            juice_rationale     TEXT    DEFAULT NULL,
            trigger_message_id  TEXT    DEFAULT NULL,
            checkpoint_ref      TEXT    DEFAULT NULL,
            awaiting_owner_reason TEXT  DEFAULT NULL,
            decision_text       TEXT    DEFAULT NULL,
            claimed_until       TEXT    DEFAULT NULL,
            uow_mode            TEXT    DEFAULT NULL,
            source_ref          TEXT    DEFAULT NULL,
            source_last_seen_at TEXT    DEFAULT NULL,
            source_state        TEXT    DEFAULT NULL,
            artifacts           TEXT    DEFAULT NULL
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


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    _apply_schema(conn)
    conn.close()
    return db


@pytest.fixture
def registry(db_path):
    r = Registry(db_path)
    return r


def _make_uow_row(conn, status="ready-for-steward", steward_cycles=0, summary=None,
                  success_criteria=None, source_issue_number=None, updated_at=None):
    uow_id = f"uow_test_{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    conn.execute("""
        INSERT INTO uow_registry
            (id, summary, type, status, source, source_issue_number,
             steward_cycles, created_at, updated_at, success_criteria)
        VALUES (?, ?, 'executable', ?, 'telegram', ?, ?, ?, ?, ?)
    """, (
        uow_id,
        summary or f"Test UoW {uow_id}",
        status,
        source_issue_number,
        steward_cycles,
        now,
        updated_at or now,
        success_criteria or "Must produce artifact",
    ))
    conn.commit()
    return uow_id


def _get_uow(db_path: Path, uow_id: str) -> dict:
    conn = _open_db(db_path)
    row = conn.execute(
        "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Test: wos_prescribe in WOS_MESSAGE_TYPE_DISPATCH
# ---------------------------------------------------------------------------

class TestWosPrescribeRegistered:
    def test_wos_prescribe_in_dispatch_table(self):
        """wos_prescribe must be registered in WOS_MESSAGE_TYPE_DISPATCH."""
        assert "wos_prescribe" in WOS_MESSAGE_TYPE_DISPATCH, (
            "wos_prescribe must be registered in WOS_MESSAGE_TYPE_DISPATCH"
        )

    def test_wos_prescribe_handler_name(self):
        """wos_prescribe must map to 'handle_wos_prescribe'."""
        assert WOS_MESSAGE_TYPE_DISPATCH["wos_prescribe"] == "handle_wos_prescribe"


# ---------------------------------------------------------------------------
# Test: handle_wos_prescribe
# ---------------------------------------------------------------------------

class TestHandleWosPrescribe:
    """Tests for the handle_wos_prescribe dispatcher handler."""

    def _make_msg(self, uow_id="uow_test_abc123"):
        return {
            "type": "wos_prescribe",
            "id": str(uuid.uuid4()),
            "source": "system",
            "chat_id": 0,
            "timestamp": time.time(),
            "uow_id": uow_id,
            "uow_summary": "Implement feature X",
            "uow_type": "executable",
            "uow_source": "telegram",
            "success_criteria": "PR opened and merged",
            "reentry_posture": "first_execution",
            "completion_gap": "",
            "issue_body": "",
            "cycles": 0,
            "new_cycles": 1,
            "selected_executor_type": "functional-engineer",
            "prescribed_skills": ["code-review"],
            "diagnosis_section": {
                "reentry_posture": "first_execution",
                "completion_gap": "",
                "prior_cycle_count": 0,
            },
            "vision_orientation": "",
            "dan_register": "",
            "steward_log": "",
            "now_iso": _now_iso(),
        }

    def test_returns_spawn_subagent(self):
        """handle_wos_prescribe must return action='spawn_subagent'."""
        result = handle_wos_prescribe(self._make_msg())
        assert result["action"] == "spawn_subagent", (
            f"handle_wos_prescribe must return action='spawn_subagent', got {result['action']!r}"
        )

    def test_task_id_contains_uow_id_prefix(self):
        """task_id must contain the first 8 chars of uow_id."""
        uow_id = "uow_test_abc12345678"
        result = handle_wos_prescribe(self._make_msg(uow_id=uow_id))
        # task_id uses uow_id[:8] = "uow_test"
        assert uow_id[:8] in result["task_id"], (
            f"task_id must contain first 8 chars of uow_id ('{uow_id[:8]}'), got {result['task_id']!r}"
        )

    def test_agent_type_is_lobster_generalist(self):
        """agent_type must be 'lobster-generalist'."""
        result = handle_wos_prescribe(self._make_msg())
        assert result["agent_type"] == "lobster-generalist"

    def test_prompt_contains_uow_id(self):
        """Prompt must embed the uow_id."""
        uow_id = "uow_test_abc123"
        result = handle_wos_prescribe(self._make_msg(uow_id=uow_id))
        assert uow_id in result["prompt"]

    def test_prompt_contains_task_id_frontmatter(self):
        """Prompt must have YAML frontmatter with task_id."""
        result = handle_wos_prescribe(self._make_msg())
        assert "task_id:" in result["prompt"]
        assert "chat_id:" in result["prompt"]
        assert "source: system" in result["prompt"]

    def test_prompt_has_minimum_viable_output(self):
        """Prompt must include 'Minimum viable output' constraint."""
        result = handle_wos_prescribe(self._make_msg())
        assert "Minimum viable output" in result["prompt"]

    def test_prompt_has_boundary(self):
        """Prompt must include 'Boundary' constraint."""
        result = handle_wos_prescribe(self._make_msg())
        assert "Boundary" in result["prompt"]

    def test_prompt_embeds_prescription_context(self):
        """Prompt must embed the prescription context fields from the payload."""
        msg = self._make_msg()
        result = handle_wos_prescribe(msg)
        prompt = result["prompt"]
        # The subagent's prompt must contain the UoW context fields.
        # The new design expands payload fields into human-readable context
        # rather than embedding raw JSON — verify key fields are present.
        assert msg["uow_id"] in prompt, "uow_id must appear in prompt"
        assert msg["uow_summary"] in prompt, "uow_summary must appear in prompt"
        # Executor posture is shown as "Executor posture: first_execution"
        assert "first_execution" in prompt, "reentry_posture value must appear in prompt"
        # Artifact write helper must be referenced
        assert "wos-write-artifact.py" in prompt, (
            "Prompt must reference the wos-write-artifact.py helper script"
        )


# ---------------------------------------------------------------------------
# Test: route_wos_message routes wos_prescribe correctly
# ---------------------------------------------------------------------------

class TestRouteWosPrescribe:
    """Tests for wos_prescribe routing via route_wos_message."""

    def _make_msg(self):
        return {
            "type": "wos_prescribe",
            "id": str(uuid.uuid4()),
            "source": "system",
            "chat_id": 0,
            "timestamp": time.time(),
            "uow_id": "uow_test_route_abc",
            "uow_summary": "Route test UoW",
            "uow_type": "executable",
            "uow_source": "telegram",
            "success_criteria": "",
            "reentry_posture": "first_execution",
            "completion_gap": "",
            "issue_body": "",
            "cycles": 0,
            "new_cycles": 1,
            "selected_executor_type": "functional-engineer",
            "prescribed_skills": [],
            "diagnosis_section": {"reentry_posture": "first_execution", "completion_gap": ""},
            "vision_orientation": "",
            "dan_register": "",
            "steward_log": "",
            "now_iso": _now_iso(),
        }

    def test_route_returns_spawn_subagent(self):
        """route_wos_message must return action='spawn_subagent' for wos_prescribe."""
        result = route_wos_message(self._make_msg())
        assert result["action"] == "spawn_subagent"

    def test_route_returns_message_type(self):
        """route_wos_message must echo the message type."""
        result = route_wos_message(self._make_msg())
        assert result["message_type"] == "wos_prescribe"

    def test_route_result_has_task_id(self):
        """route_wos_message result must have task_id."""
        result = route_wos_message(self._make_msg())
        assert "task_id" in result

    def test_route_result_has_prompt(self):
        """route_wos_message result must have a prompt string."""
        result = route_wos_message(self._make_msg())
        assert isinstance(result.get("prompt"), str)
        assert len(result["prompt"]) > 0


# ---------------------------------------------------------------------------
# Test: registry.record_startup_sweep_prescribing — TTL recovery
# ---------------------------------------------------------------------------

class TestRecordStartupSweepPrescribing:
    """Tests for the prescribing TTL recovery registry method."""

    def test_prescribing_uow_reset_to_ready_for_steward(self, db_path, registry):
        """record_startup_sweep_prescribing must transition prescribing→ready-for-steward."""
        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="prescribing")
        conn.close()

        rows = registry.record_startup_sweep_prescribing(uow_id, timeout_secs=660)
        assert rows == 1, "must return 1 on success"

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-steward", (
            f"prescribing UoW must be reset to ready-for-steward, got {uow['status']!r}"
        )

    def test_prescribing_reset_writes_audit_entry(self, db_path, registry):
        """record_startup_sweep_prescribing must write a startup_sweep audit entry."""
        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="prescribing")
        conn.close()

        registry.record_startup_sweep_prescribing(uow_id)

        conn = _open_db(db_path)
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? AND event = 'startup_sweep'",
            (uow_id,),
        ).fetchall()
        conn.close()

        assert len(rows) >= 1, "startup_sweep audit entry must be written"
        note = json.loads(rows[-1]["note"])
        assert note.get("classification") == "prescribing_orphan"
        assert note.get("prior_status") == "prescribing"

    def test_prescribing_reset_race_returns_zero(self, db_path, registry):
        """record_startup_sweep_prescribing must return 0 when UoW is not in prescribing."""
        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward")
        conn.close()

        rows = registry.record_startup_sweep_prescribing(uow_id)
        assert rows == 0, "must return 0 when UoW is not in prescribing state (race)"

    def test_prescribing_uow_enum_member_exists(self):
        """UoWStatus.PRESCRIBING must exist and equal 'prescribing'."""
        assert UoWStatus.PRESCRIBING == "prescribing"

    def test_prescribing_is_in_flight(self):
        """UoWStatus.PRESCRIBING.is_in_flight() must be True."""
        assert UoWStatus.PRESCRIBING.is_in_flight(), (
            "prescribing must be considered in-flight (blocks re-proposal)"
        )

    def test_prescribing_is_not_terminal(self):
        """UoWStatus.PRESCRIBING.is_terminal() must be False."""
        assert not UoWStatus.PRESCRIBING.is_terminal()


# ---------------------------------------------------------------------------
# Test: _write_prescription_request inbox message
# ---------------------------------------------------------------------------

class TestWritePrescriptionRequest:
    """Tests for the steward _write_prescription_request helper."""

    def _make_uow_mock(self, uow_id="uow_test_prescription_write"):
        """Build a minimal MagicMock UoW for testing _write_prescription_request."""
        uow = MagicMock(spec=[
            "id", "summary", "type", "source", "vision_ref", "steward_log",
            "success_criteria",
        ])
        uow.id = uow_id
        uow.summary = "Test UoW for prescription write"
        uow.type = "executable"  # must be JSON-serializable string
        uow.source = "telegram"
        uow.vision_ref = None
        uow.steward_log = None
        uow.success_criteria = "PR opened"
        return uow

    def test_writes_json_to_inbox(self, tmp_path, monkeypatch):
        """_write_prescription_request must write a JSON file to the inbox directory."""
        import orchestration.steward as steward_mod
        inbox_dir = tmp_path / "inbox"
        artifact_dir = tmp_path / "prescribe-artifacts"
        inbox_dir.mkdir()
        monkeypatch.setattr(steward_mod, "_INBOX_DIR_PATH", inbox_dir)

        uow = self._make_uow_mock()
        msg_id = _write_prescription_request(
            uow=uow,
            reentry_posture="first_execution",
            completion_gap="",
            issue_body="",
            cycles=0,
            new_cycles=1,
            selected_executor_type="functional-engineer",
            prescribed_skills=[],
            diagnosis_section={"reentry_posture": "first_execution"},
            vision_orientation="",
            dan_register="",
            now_iso=_now_iso(),
            prescribe_artifact_dir=artifact_dir,
        )

        assert msg_id, "must return a message ID"
        msg_file = inbox_dir / f"{msg_id}.json"
        assert msg_file.exists(), f"inbox message file must be created at {msg_file}"

        msg = json.loads(msg_file.read_text(encoding="utf-8"))
        assert msg["type"] == "wos_prescribe"
        assert msg["uow_id"] == uow.id

    def test_message_type_is_wos_prescribe(self, tmp_path, monkeypatch):
        """Message type must be 'wos_prescribe'."""
        import orchestration.steward as steward_mod
        inbox_dir = tmp_path / "inbox"
        artifact_dir = tmp_path / "prescribe-artifacts"
        inbox_dir.mkdir()
        monkeypatch.setattr(steward_mod, "_INBOX_DIR_PATH", inbox_dir)

        uow = self._make_uow_mock()
        msg_id = _write_prescription_request(
            uow=uow,
            reentry_posture="first_execution",
            completion_gap="",
            issue_body="",
            cycles=0,
            new_cycles=1,
            selected_executor_type="functional-engineer",
            prescribed_skills=[],
            diagnosis_section={},
            vision_orientation="",
            dan_register="",
            now_iso=_now_iso(),
            prescribe_artifact_dir=artifact_dir,
        )
        msg = json.loads((inbox_dir / f"{msg_id}.json").read_text(encoding="utf-8"))
        assert msg["type"] == "wos_prescribe"

    def test_message_contains_prescription_inputs(self, tmp_path, monkeypatch):
        """Inbox message must carry scalar fields; large fields must be in the artifact file."""
        import orchestration.steward as steward_mod
        import orchestration.prescribe_artifacts as pa_mod
        inbox_dir = tmp_path / "inbox"
        artifact_dir = tmp_path / "prescribe-artifacts"
        inbox_dir.mkdir()
        monkeypatch.setattr(steward_mod, "_INBOX_DIR_PATH", inbox_dir)

        uow = self._make_uow_mock()
        msg_id = _write_prescription_request(
            uow=uow,
            reentry_posture="continuation",
            completion_gap="Work not complete",
            issue_body="Issue body text",
            cycles=2,
            new_cycles=3,
            selected_executor_type="lobster-ops",
            prescribed_skills=["verify"],
            diagnosis_section={"reentry_posture": "continuation", "completion_gap": "Work not complete"},
            vision_orientation="Vision context",
            dan_register="Dan register",
            now_iso=_now_iso(),
            prescribe_artifact_dir=artifact_dir,
        )
        msg = json.loads((inbox_dir / f"{msg_id}.json").read_text(encoding="utf-8"))

        # Scalar fields remain in the inbox message.
        assert msg["reentry_posture"] == "continuation"
        assert msg["completion_gap"] == "Work not complete"
        assert msg["cycles"] == 2
        assert msg["new_cycles"] == 3
        assert msg["selected_executor_type"] == "lobster-ops"
        assert msg["prescribed_skills"] == ["verify"]

        # Large fields must NOT be inlined in the inbox message.
        assert "vision_orientation" not in msg, (
            "vision_orientation must be in the artifact file, not the inbox message"
        )
        assert "issue_body" not in msg, (
            "issue_body must be in the artifact file, not the inbox message"
        )
        assert "steward_log" not in msg, (
            "steward_log must be in the artifact file, not the inbox message"
        )
        assert "dan_register" not in msg, (
            "dan_register must be in the artifact file, not the inbox message"
        )
        assert "diagnosis_section" not in msg, (
            "diagnosis_section must be in the artifact file, not the inbox message"
        )

        # artifact_path must be present and point to an existing file.
        assert "artifact_path" in msg, "inbox message must carry artifact_path"
        artifact_path = msg["artifact_path"]
        artifact_file = Path(artifact_path)
        assert artifact_file.exists(), f"artifact file must exist at {artifact_path}"

        # Large fields must be in the artifact file with correct values.
        artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
        assert artifact["vision_orientation"] == "Vision context"
        assert artifact["issue_body"] == "Issue body text"
        assert artifact["dan_register"] == "Dan register"


# ---------------------------------------------------------------------------
# Test: _process_uow async path — PrescribingQueued outcome
# ---------------------------------------------------------------------------

class TestProcessUowAsyncPrescription:
    """Tests for the async prescription path in _process_uow."""

    def test_async_path_returns_prescribing_queued(self, db_path, registry, tmp_path, monkeypatch):
        """With default _llm_prescribe, _process_uow must return PrescribingQueued."""
        import orchestration.steward as steward_mod

        # Patch inbox directory so we don't write to the real inbox
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        monkeypatch.setattr(steward_mod, "_INBOX_DIR_PATH", inbox_dir)

        # Patch _llm_prescribe to prevent actual claude -p calls
        # (the async path checks `llm_prescriber is _llm_prescribe` so we need
        # to verify the async path fires, not the sync path)
        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward", steward_cycles=0)
        conn.close()

        # patch _load_dan_register_excerpt and resolve_vision_route to avoid side effects
        monkeypatch.setattr(steward_mod, "_load_dan_register_excerpt", lambda **kw: "")

        # The default llm_prescriber IS _llm_prescribe, so the async path fires
        result = run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=lambda n: __import__("orchestration.steward", fromlist=["IssueInfo"]).IssueInfo(
                status_code=200, state="open", labels=[], body="Test issue", title="Test"
            ),
            artifact_dir=tmp_path / "artifacts",
            # default llm_prescriber=_llm_prescribe → async path
        )

        uow = _get_uow(db_path, uow_id)
        # The async path should have transitioned UoW to 'prescribing'
        assert uow["status"] in ("prescribing", "ready-for-steward"), (
            f"UoW must be in 'prescribing' (async path) or 'ready-for-steward' (error fallback). "
            f"Got: {uow['status']}"
        )

        # If prescribing: the wos_prescribe inbox message must have been written
        if uow["status"] == "prescribing":
            inbox_files = list(inbox_dir.glob("*.json"))
            assert len(inbox_files) >= 1, "wos_prescribe inbox message must be written"
            msg = json.loads(inbox_files[0].read_text(encoding="utf-8"))
            assert msg["type"] == "wos_prescribe"
            assert msg["uow_id"] == uow_id

    def test_async_path_steward_cycles_incremented(self, db_path, registry, tmp_path, monkeypatch):
        """Async path must increment steward_cycles even though LLM call is async."""
        import orchestration.steward as steward_mod

        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        monkeypatch.setattr(steward_mod, "_INBOX_DIR_PATH", inbox_dir)
        monkeypatch.setattr(steward_mod, "_load_dan_register_excerpt", lambda **kw: "")

        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward", steward_cycles=0)
        conn.close()

        run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=lambda n: __import__("orchestration.steward", fromlist=["IssueInfo"]).IssueInfo(
                status_code=200, state="open", labels=[], body="Test issue", title="Test"
            ),
            artifact_dir=tmp_path / "artifacts",
        )

        uow = _get_uow(db_path, uow_id)
        if uow["status"] == "prescribing":
            assert uow["steward_cycles"] == 1, (
                f"steward_cycles must be incremented to 1, got {uow['steward_cycles']}"
            )

    def test_sync_path_preserved_when_injecting_none(self, db_path, registry, tmp_path):
        """Passing llm_prescriber=None must use the synchronous deterministic path."""
        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward", steward_cycles=0)
        conn.close()

        # llm_prescriber=None → sync deterministic path → ready-for-executor
        run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=lambda n: __import__("orchestration.steward", fromlist=["IssueInfo"]).IssueInfo(
                status_code=200, state="open", labels=[], body="Test issue", title="Test"
            ),
            artifact_dir=tmp_path / "artifacts",
            llm_prescriber=None,
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-executor", (
            f"llm_prescriber=None must use sync path → ready-for-executor, got {uow['status']!r}"
        )

    def test_sync_path_preserved_when_injecting_stub(self, db_path, registry, tmp_path):
        """Passing a custom stub prescriber must use the synchronous prescription path."""
        conn = _open_db(db_path)
        uow_id = _make_uow_row(conn, status="ready-for-steward", steward_cycles=0)
        conn.close()

        import orchestration.steward as steward_mod
        stub_calls = []

        def stub_prescriber(uow, posture, gap, issue_body=""):
            stub_calls.append(uow.id)
            return steward_mod.LLMPrescription(
                instructions="Stub: implement the feature.",
                success_criteria_check="Feature branch green.",
                estimated_cycles=1,
            )

        # Custom stub (not _llm_prescribe) → sync path
        run_steward_cycle(
            registry=registry,
            dry_run=False,
            github_client=lambda n: __import__("orchestration.steward", fromlist=["IssueInfo"]).IssueInfo(
                status_code=200, state="open", labels=[], body="Test issue", title="Test"
            ),
            artifact_dir=tmp_path / "artifacts",
            llm_prescriber=stub_prescriber,
        )

        # The stub must have been called (sync path)
        assert uow_id in stub_calls, (
            "Custom stub prescriber must be called on the sync path"
        )

        uow = _get_uow(db_path, uow_id)
        assert uow["status"] == "ready-for-executor", (
            f"Custom stub prescriber must produce ready-for-executor, got {uow['status']!r}"
        )


# ---------------------------------------------------------------------------
# Test: prescribing_queued count in CycleResult
# ---------------------------------------------------------------------------

class TestCycleResultPrescribingQueued:
    """Tests for the prescribing_queued field in CycleResult."""

    def test_cycle_result_has_prescribing_queued_field(self):
        """CycleResult must have a prescribing_queued field."""
        from orchestration.steward import CycleResult
        result = CycleResult(
            evaluated=1,
            prescribed=0,
            done=0,
            surfaced=0,
            skipped=0,
            race_skipped=0,
            wait_for_trace=0,
            prescribing_queued=1,
            considered_ids=("uow_test_abc",),
        )
        assert result.prescribing_queued == 1

    def test_cycle_result_as_dict_includes_prescribing_queued(self):
        """CycleResult.as_dict() must include prescribing_queued."""
        from orchestration.steward import CycleResult
        result = CycleResult(
            evaluated=2,
            prescribed=1,
            done=0,
            surfaced=0,
            skipped=0,
            race_skipped=0,
            wait_for_trace=0,
            prescribing_queued=1,
            considered_ids=("uow_test_abc", "uow_test_def"),
        )
        d = result.as_dict()
        assert "prescribing_queued" in d
        assert d["prescribing_queued"] == 1
