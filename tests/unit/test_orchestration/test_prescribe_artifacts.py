"""
Unit tests for the slim wos_prescribe design.

Covers:
1. prescribe_artifacts.write_prescribe_artifact — writes artifact file with correct content
2. prescribe_artifacts.read_prescribe_artifact — reads file, returns None for missing
3. Inbox message is small: large fields absent from inbox, artifact_path present
4. handle_wos_prescribe with artifact_path — reads artifact, builds prompt with full context
5. Backward-compat: handle_wos_prescribe with inline large fields (no artifact_path)
6. LARGE_FIELDS constant covers all expected fields
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import sys

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SRC_ROOT = _REPO_ROOT / "src"
for p in [str(_REPO_ROOT), str(_SRC_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from orchestration.prescribe_artifacts import (  # noqa: E402
    LARGE_FIELDS,
    read_prescribe_artifact,
    write_prescribe_artifact,
)
from orchestration.dispatcher_handlers import handle_wos_prescribe  # noqa: E402
from orchestration.steward import _write_prescription_request  # noqa: E402

# Constants matching the spec — named so tests stay readable if the value changes.
EXPECTED_LARGE_FIELDS = frozenset({
    "issue_body",
    "steward_log",
    "dan_register",
    "vision_orientation",
    "diagnosis_section",
})


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _make_uow_mock(uow_id: str = "uow_test_slim_abc") -> MagicMock:
    uow = MagicMock(spec=["id", "summary", "type", "source", "vision_ref", "steward_log", "success_criteria"])
    uow.id = uow_id
    uow.summary = "Test slim prescription UoW"
    uow.type = "executable"
    uow.source = "telegram"
    uow.vision_ref = None
    uow.steward_log = '{"event": "prescription", "steward_cycles": 0, "completion_assessment": ""}'
    uow.success_criteria = "PR merged"
    return uow


# ---------------------------------------------------------------------------
# Tests: prescribe_artifacts module
# ---------------------------------------------------------------------------

class TestWritePrescribeArtifact:
    """Tests for write_prescribe_artifact."""

    def test_creates_artifact_file(self, tmp_path):
        """write_prescribe_artifact must create a JSON file keyed by uow_id."""
        uow_id = "uow_test_artifact_write"
        artifact_path = write_prescribe_artifact(
            uow_id,
            artifact_dir=tmp_path,
            issue_body="Issue text",
            steward_log="log line",
            dan_register="dan register text",
            vision_orientation="vision context",
            diagnosis_section={"key": "value"},
        )
        assert artifact_path.exists(), f"artifact file must exist at {artifact_path}"

    def test_artifact_filename_contains_uow_id(self, tmp_path):
        """Artifact filename must contain uow_id for easy lookup."""
        uow_id = "uow_test_filename_check"
        artifact_path = write_prescribe_artifact(uow_id, artifact_dir=tmp_path)
        assert uow_id in artifact_path.name, (
            f"uow_id must appear in artifact filename, got {artifact_path.name!r}"
        )

    def test_artifact_contains_all_large_fields(self, tmp_path):
        """Artifact file must contain all LARGE_FIELDS with correct values."""
        uow_id = "uow_test_fields"
        artifact_path = write_prescribe_artifact(
            uow_id,
            artifact_dir=tmp_path,
            issue_body="My issue body",
            steward_log="My steward log",
            dan_register="My dan register",
            vision_orientation="My vision",
            diagnosis_section={"reentry_posture": "first_execution"},
        )
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert payload["issue_body"] == "My issue body"
        assert payload["steward_log"] == "My steward log"
        assert payload["dan_register"] == "My dan register"
        assert payload["vision_orientation"] == "My vision"
        assert payload["diagnosis_section"]["reentry_posture"] == "first_execution"

    def test_artifact_contains_uow_id(self, tmp_path):
        """Artifact file must embed uow_id for integrity verification."""
        uow_id = "uow_test_embed_id"
        artifact_path = write_prescribe_artifact(uow_id, artifact_dir=tmp_path)
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert payload["uow_id"] == uow_id

    def test_creates_directory_if_missing(self, tmp_path):
        """write_prescribe_artifact must create the artifact directory if it doesn't exist."""
        nested_dir = tmp_path / "nested" / "prescribe-artifacts"
        assert not nested_dir.exists()
        write_prescribe_artifact("uow_test_mkdir", artifact_dir=nested_dir)
        assert nested_dir.exists(), "directory must be created automatically"

    def test_overwrites_existing_artifact(self, tmp_path):
        """Repeated calls for the same uow_id must overwrite (idempotent)."""
        uow_id = "uow_test_overwrite"
        write_prescribe_artifact(uow_id, artifact_dir=tmp_path, issue_body="first")
        write_prescribe_artifact(uow_id, artifact_dir=tmp_path, issue_body="second")
        artifact_file = tmp_path / f"{uow_id}.prescribe.json"
        payload = json.loads(artifact_file.read_text(encoding="utf-8"))
        assert payload["issue_body"] == "second", "second write must overwrite first"


class TestReadPrescribeArtifact:
    """Tests for read_prescribe_artifact."""

    def test_reads_written_artifact(self, tmp_path):
        """read_prescribe_artifact must return the payload written by write_prescribe_artifact."""
        uow_id = "uow_test_read_back"
        artifact_path = write_prescribe_artifact(
            uow_id,
            artifact_dir=tmp_path,
            issue_body="Round-trip body",
            vision_orientation="Round-trip vision",
        )
        payload = read_prescribe_artifact(artifact_path)
        assert payload is not None
        assert payload["issue_body"] == "Round-trip body"
        assert payload["vision_orientation"] == "Round-trip vision"

    def test_returns_none_for_missing_file(self, tmp_path):
        """read_prescribe_artifact must return None when the artifact file is missing."""
        missing = tmp_path / "nonexistent_uow.prescribe.json"
        result = read_prescribe_artifact(missing)
        assert result is None, (
            "read_prescribe_artifact must return None for missing files "
            "(backward-compat for pre-slim-message inbox messages)"
        )

    def test_accepts_string_path(self, tmp_path):
        """read_prescribe_artifact must accept a string path as well as a Path object."""
        uow_id = "uow_test_str_path"
        artifact_path = write_prescribe_artifact(uow_id, artifact_dir=tmp_path)
        payload = read_prescribe_artifact(str(artifact_path))
        assert payload is not None

    def test_returns_dict(self, tmp_path):
        """read_prescribe_artifact must return a dict."""
        uow_id = "uow_test_returns_dict"
        artifact_path = write_prescribe_artifact(uow_id, artifact_dir=tmp_path)
        payload = read_prescribe_artifact(artifact_path)
        assert isinstance(payload, dict)


class TestLargeFieldsConstant:
    """Tests for the LARGE_FIELDS module-level constant."""

    def test_large_fields_covers_expected_set(self):
        """LARGE_FIELDS must cover all fields identified as large in the spec."""
        assert LARGE_FIELDS == EXPECTED_LARGE_FIELDS, (
            f"LARGE_FIELDS must exactly match the spec set.\n"
            f"  Expected: {EXPECTED_LARGE_FIELDS}\n"
            f"  Got: {LARGE_FIELDS}"
        )

    def test_large_fields_is_frozenset(self):
        """LARGE_FIELDS must be a frozenset (immutable, importable by tests)."""
        assert isinstance(LARGE_FIELDS, frozenset)


# ---------------------------------------------------------------------------
# Tests: inbox message slimness after write_prescription_request
# ---------------------------------------------------------------------------

class TestInboxMessageIsSlim:
    """Verify the inbox message stays small after the slim-message redesign."""

    def test_large_fields_absent_from_inbox_message(self, tmp_path, monkeypatch):
        """Large fields must not appear in the inbox message JSON."""
        import orchestration.steward as steward_mod
        inbox_dir = tmp_path / "inbox"
        artifact_dir = tmp_path / "prescribe-artifacts"
        inbox_dir.mkdir()
        monkeypatch.setattr(steward_mod, "_INBOX_DIR_PATH", inbox_dir)

        uow = _make_uow_mock()
        msg_id = _write_prescription_request(
            uow=uow,
            reentry_posture="first_execution",
            completion_gap="",
            issue_body="A" * 5000,      # 5 KB issue body
            cycles=0,
            new_cycles=1,
            selected_executor_type="functional-engineer",
            prescribed_skills=[],
            diagnosis_section={"reentry_posture": "first_execution"},
            vision_orientation="B" * 3000,  # 3 KB vision text
            dan_register="C" * 2000,    # 2 KB dan register
            now_iso=_now_iso(),
            prescribe_artifact_dir=artifact_dir,
        )

        msg = json.loads((inbox_dir / f"{msg_id}.json").read_text(encoding="utf-8"))

        for field in LARGE_FIELDS:
            assert field not in msg, (
                f"Large field '{field}' must not be in the inbox message "
                f"(it belongs in the artifact file)"
            )

    def test_inbox_message_carries_artifact_path(self, tmp_path, monkeypatch):
        """Inbox message must carry artifact_path pointing to the sidecar file."""
        import orchestration.steward as steward_mod
        inbox_dir = tmp_path / "inbox"
        artifact_dir = tmp_path / "prescribe-artifacts"
        inbox_dir.mkdir()
        monkeypatch.setattr(steward_mod, "_INBOX_DIR_PATH", inbox_dir)

        uow = _make_uow_mock()
        msg_id = _write_prescription_request(
            uow=uow,
            reentry_posture="first_execution",
            completion_gap="",
            issue_body="issue body",
            cycles=0,
            new_cycles=1,
            selected_executor_type="functional-engineer",
            prescribed_skills=[],
            diagnosis_section={},
            vision_orientation="vision",
            dan_register="dan",
            now_iso=_now_iso(),
            prescribe_artifact_dir=artifact_dir,
        )

        msg = json.loads((inbox_dir / f"{msg_id}.json").read_text(encoding="utf-8"))
        assert "artifact_path" in msg, "inbox message must carry artifact_path"
        assert Path(msg["artifact_path"]).exists(), (
            f"artifact file must exist at {msg['artifact_path']}"
        )

    def test_inbox_message_under_size_budget(self, tmp_path, monkeypatch):
        """Inbox message with 10 KB of large fields must stay under 2 KB."""
        import orchestration.steward as steward_mod
        inbox_dir = tmp_path / "inbox"
        artifact_dir = tmp_path / "prescribe-artifacts"
        inbox_dir.mkdir()
        monkeypatch.setattr(steward_mod, "_INBOX_DIR_PATH", inbox_dir)

        uow = _make_uow_mock()
        msg_id = _write_prescription_request(
            uow=uow,
            reentry_posture="first_execution",
            completion_gap="Some gap description",
            issue_body="X" * 5000,
            cycles=1,
            new_cycles=2,
            selected_executor_type="functional-engineer",
            prescribed_skills=["verify", "code-review"],
            diagnosis_section={"reentry_posture": "first_execution", "large_key": "Y" * 2000},
            vision_orientation="Z" * 3000,
            dan_register="W" * 2000,
            now_iso=_now_iso(),
            prescribe_artifact_dir=artifact_dir,
        )

        msg_text = (inbox_dir / f"{msg_id}.json").read_text(encoding="utf-8")
        msg_size = len(msg_text.encode("utf-8"))
        SIZE_BUDGET_BYTES = 2048
        assert msg_size < SIZE_BUDGET_BYTES, (
            f"Inbox message must be under {SIZE_BUDGET_BYTES} bytes, "
            f"got {msg_size} bytes. "
            f"Large fields must not be inlined."
        )


# ---------------------------------------------------------------------------
# Tests: handle_wos_prescribe reads artifact for large fields
# ---------------------------------------------------------------------------

class TestHandleWosPrescribeArtifactPath:
    """Tests for handle_wos_prescribe with artifact_path (new slim-message path)."""

    def _make_msg_with_artifact(self, uow_id: str, artifact_dir: Path) -> dict:
        """Build a slim wos_prescribe message with an artifact sidecar.

        Note: success_criteria is intentionally empty so issue_body from the
        artifact is included in the prompt (the prompt builder uses issue_body
        as a fallback only when success_criteria is absent).
        """
        artifact_path = write_prescribe_artifact(
            uow_id,
            artifact_dir=artifact_dir,
            issue_body="Artifact issue body",
            steward_log="",
            dan_register="Artifact dan register",
            vision_orientation="Artifact vision",
            diagnosis_section={"reentry_posture": "first_execution"},
        )
        return {
            "type": "wos_prescribe",
            "id": str(uuid.uuid4()),
            "source": "system",
            "chat_id": 0,
            "timestamp": time.time(),
            "uow_id": uow_id,
            "uow_summary": "Implement feature Y",
            "uow_type": "executable",
            "uow_source": "telegram",
            "success_criteria": "",  # empty so issue_body appears in prompt as fallback
            "reentry_posture": "first_execution",
            "completion_gap": "",
            "cycles": 0,
            "new_cycles": 1,
            "selected_executor_type": "functional-engineer",
            "prescribed_skills": ["code-review"],
            "now_iso": _now_iso(),
            "artifact_path": str(artifact_path),
            # Large fields intentionally absent — they're in the artifact file.
        }

    def test_prompt_contains_vision_from_artifact(self, tmp_path):
        """handle_wos_prescribe must embed vision_orientation from the artifact in the prompt."""
        uow_id = "uow_test_vision_artifact"
        msg = self._make_msg_with_artifact(uow_id, tmp_path)
        result = handle_wos_prescribe(msg)
        assert "Artifact vision" in result["prompt"], (
            "vision_orientation from artifact file must appear in the prompt"
        )

    def test_prompt_contains_dan_register_from_artifact(self, tmp_path):
        """handle_wos_prescribe must embed dan_register from the artifact in the prompt."""
        uow_id = "uow_test_dan_artifact"
        msg = self._make_msg_with_artifact(uow_id, tmp_path)
        result = handle_wos_prescribe(msg)
        assert "Artifact dan register" in result["prompt"], (
            "dan_register from artifact file must appear in the prompt"
        )

    def test_prompt_contains_issue_body_from_artifact(self, tmp_path):
        """handle_wos_prescribe must embed issue_body from the artifact in the prompt."""
        uow_id = "uow_test_issue_artifact"
        msg = self._make_msg_with_artifact(uow_id, tmp_path)
        result = handle_wos_prescribe(msg)
        assert "Artifact issue body" in result["prompt"], (
            "issue_body from artifact file must appear in the prompt"
        )

    def test_returns_spawn_subagent(self, tmp_path):
        """handle_wos_prescribe must return action='spawn_subagent' for artifact-path messages."""
        uow_id = "uow_test_artifact_action"
        msg = self._make_msg_with_artifact(uow_id, tmp_path)
        result = handle_wos_prescribe(msg)
        assert result["action"] == "spawn_subagent"


class TestHandleWosPrescribeBackwardCompat:
    """Tests for handle_wos_prescribe with inline large fields (backward-compat path)."""

    def _make_inline_msg(self, uow_id: str = "uow_test_inline_compat") -> dict:
        """Build a pre-slim wos_prescribe message with inline large fields.

        Note: success_criteria is intentionally empty so issue_body appears
        in the prompt (the prompt builder uses issue_body as a fallback only
        when success_criteria is absent).
        """
        return {
            "type": "wos_prescribe",
            "id": str(uuid.uuid4()),
            "source": "system",
            "chat_id": 0,
            "timestamp": time.time(),
            "uow_id": uow_id,
            "uow_summary": "Inline feature",
            "uow_type": "executable",
            "uow_source": "telegram",
            "success_criteria": "",  # empty so issue_body appears in prompt as fallback
            "reentry_posture": "first_execution",
            "completion_gap": "",
            # Large fields inline (old format — no artifact_path):
            "issue_body": "Inline issue body",
            "steward_log": "",
            "dan_register": "Inline dan register",
            "vision_orientation": "Inline vision",
            "diagnosis_section": {"reentry_posture": "first_execution"},
            "cycles": 0,
            "new_cycles": 1,
            "selected_executor_type": "functional-engineer",
            "prescribed_skills": [],
            "now_iso": _now_iso(),
            # artifact_path intentionally absent — old message shape
        }

    def test_inline_message_returns_spawn_subagent(self):
        """Inline large-field message (old shape) must still return action='spawn_subagent'."""
        result = handle_wos_prescribe(self._make_inline_msg())
        assert result["action"] == "spawn_subagent"

    def test_prompt_contains_inline_vision(self):
        """Inline vision_orientation must appear in the prompt for old-shape messages."""
        result = handle_wos_prescribe(self._make_inline_msg())
        assert "Inline vision" in result["prompt"], (
            "Inline vision_orientation must appear in the prompt (backward-compat)"
        )

    def test_prompt_contains_inline_dan_register(self):
        """Inline dan_register must appear in the prompt for old-shape messages."""
        result = handle_wos_prescribe(self._make_inline_msg())
        assert "Inline dan register" in result["prompt"], (
            "Inline dan_register must appear in the prompt (backward-compat)"
        )

    def test_prompt_contains_inline_issue_body(self):
        """Inline issue_body must appear in the prompt for old-shape messages."""
        result = handle_wos_prescribe(self._make_inline_msg())
        assert "Inline issue body" in result["prompt"], (
            "Inline issue_body must appear in the prompt (backward-compat)"
        )

    def test_missing_artifact_file_falls_back_to_inline(self, tmp_path):
        """artifact_path present but file missing must fall back to inline fields."""
        msg = self._make_inline_msg()
        msg["artifact_path"] = str(tmp_path / "nonexistent.prescribe.json")
        # File doesn't exist — should fall back to inline fields gracefully.
        result = handle_wos_prescribe(msg)
        assert result["action"] == "spawn_subagent"
        assert "Inline vision" in result["prompt"], (
            "When artifact file is missing, inline fields must be used as fallback"
        )
