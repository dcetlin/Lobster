"""
Unit tests for the auto-archive-on-completion behavior added to
hooks/require-write-result.py.

Behavioral spec (Dan's bar, relayed via coordinator follow-up on PR #1467):
auto-archive-on-completion is only worth keeping if it is genuinely
structural — enforced by tooling that fires regardless of whether the
dispatched agent "remembered" a preamble step. This hook fires on every
SubagentStop/Stop event where CC observes a valid write_result call, so it
is the correct enforcement point (see
assessments/workstreams-canonicity-model-20260704.md §4).

Rules under test:
- A successful write_result call (status == "success", the server's own
  default when status is omitted) causes workstreams/<task_id>/ to move to
  workstreams/archive/<task_id>/, if that directory exists.
- No workstream directory for the task_id is the common case (most
  subagents never create one) and must be a silent no-op, not an error.
- A failed write_result call (status == "error") must NOT trigger archival —
  the reconciler needs to still find an in-progress/failed task's dir in
  the active namespace.
- task_id values are treated as untrusted input from tool-call arguments:
  anything that isn't a plain slug (alnum/dash/underscore) must be rejected
  before any path is built — this must hold even under adversarial-looking
  task_ids like "../sentinel.txt".
- An existing archive/<task_id>/ directory must never be overwritten.
- A non-directory at workstreams/<task_id> (unexpected, but must not crash
  or delete anything) is left alone.

Every test that exercises the archive logic passes an explicit
`workspace_root` (a `wsroot` directory nested under `tmp_path`, deliberately
NOT named "workspace" — the repo-wide autouse fixture
`isolate_inbox_server_paths` in tests/conftest.py already pre-creates
`tmp_path / "workspace"` for every test, so reusing that name here would
collide with its setup) so no test can ever touch the real
~/lobster-workspace/workstreams/ directory, regardless of the ambient
LOBSTER_WORKSPACE environment variable in this (production) box.

Named after behaviors, not mechanisms.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "require-write-result.py"


def _load_hook(monkeypatch, tmp_path):
    """Load require-write-result.py as a fresh module for each test.

    HOME is redirected to tmp_path so any code path that falls back to
    Path.home() (rather than an explicit workspace_root argument) still
    cannot reach the real filesystem.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    spec = importlib.util.spec_from_file_location("require_write_result_auto_archive", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_write_result_item(task_id: str, status: str | None = "success", chat_id=12345) -> dict:
    inp = {"chat_id": chat_id, "task_id": task_id, "text": "done"}
    if status is not None:
        inp["status"] = status
    return {"type": "tool_use", "name": "mcp__lobster-inbox__write_result", "input": inp}


# ---------------------------------------------------------------------------
# _archive_workstream_dir — direct unit tests, no hook stdin plumbing
# ---------------------------------------------------------------------------


class TestArchiveWorkstreamDir:
    def test_moves_directory_when_present(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        ws_dir = wsroot / "workstreams" / "my-slug"
        ws_dir.mkdir(parents=True)
        (ws_dir / "status.md").write_text("done")

        result = mod._archive_workstream_dir("my-slug", workspace_root=wsroot)

        assert result is True
        assert not ws_dir.exists()
        archived = wsroot / "workstreams" / "archive" / "my-slug"
        assert archived.is_dir()
        assert (archived / "status.md").read_text() == "done"

    def test_noop_when_workstream_dir_absent(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        wsroot.mkdir(parents=True, exist_ok=True)

        result = mod._archive_workstream_dir("never-created", workspace_root=wsroot)

        assert result is False
        assert not (wsroot / "workstreams" / "archive" / "never-created").exists()

    def test_rejects_path_traversal_slug(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        # A file that a traversal payload might target, one level above
        # workstreams/ — must never be touched.
        wsroot.mkdir(parents=True, exist_ok=True)
        sentinel = wsroot / "sentinel.txt"
        sentinel.write_text("do not touch")

        result = mod._archive_workstream_dir("../sentinel.txt", workspace_root=wsroot)

        assert result is False
        assert sentinel.read_text() == "do not touch"

    def test_rejects_slug_containing_path_separator(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        wsroot.mkdir(parents=True, exist_ok=True)

        result = mod._archive_workstream_dir("foo/bar", workspace_root=wsroot)

        assert result is False

    def test_rejects_empty_task_id(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        wsroot.mkdir(parents=True, exist_ok=True)

        assert mod._archive_workstream_dir("", workspace_root=wsroot) is False

    def test_does_not_overwrite_existing_archive_entry(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        ws_dir = wsroot / "workstreams" / "dup-slug"
        ws_dir.mkdir(parents=True)
        (ws_dir / "status.md").write_text("new content")

        archived_dir = wsroot / "workstreams" / "archive" / "dup-slug"
        archived_dir.mkdir(parents=True)
        (archived_dir / "status.md").write_text("original archived content")

        result = mod._archive_workstream_dir("dup-slug", workspace_root=wsroot)

        assert result is False
        # Neither side was touched.
        assert ws_dir.is_dir()
        assert (ws_dir / "status.md").read_text() == "new content"
        assert (archived_dir / "status.md").read_text() == "original archived content"

    def test_leaves_non_directory_alone(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        workstreams_root = wsroot / "workstreams"
        workstreams_root.mkdir(parents=True)
        stray_file = workstreams_root / "not-a-dir"
        stray_file.write_text("unexpected file, not a workstream dir")

        result = mod._archive_workstream_dir("not-a-dir", workspace_root=wsroot)

        assert result is False
        assert stray_file.read_text() == "unexpected file, not a workstream dir"


# ---------------------------------------------------------------------------
# _maybe_auto_archive_workstreams — status gating across multiple calls
# ---------------------------------------------------------------------------


class TestMaybeAutoArchiveWorkstreams:
    def test_archives_only_successful_status(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        (wsroot / "workstreams" / "task-a").mkdir(parents=True)
        (wsroot / "workstreams" / "task-b").mkdir(parents=True)

        items = [
            _make_write_result_item("task-a", status="success"),
            _make_write_result_item("task-b", status="error"),
        ]
        mod._maybe_auto_archive_workstreams(items, workspace_root=wsroot)

        assert not (wsroot / "workstreams" / "task-a").exists()
        assert (wsroot / "workstreams" / "archive" / "task-a").is_dir()
        # Failed task's dir is left in the active namespace for the reconciler.
        assert (wsroot / "workstreams" / "task-b").is_dir()
        assert not (wsroot / "workstreams" / "archive" / "task-b").exists()

    def test_missing_status_key_defaults_to_success(self, monkeypatch, tmp_path):
        """Mirrors the server's own default: status = args.get("status", "success")."""
        mod = _load_hook(monkeypatch, tmp_path)
        wsroot = tmp_path / "wsroot"
        (wsroot / "workstreams" / "task-no-status").mkdir(parents=True)

        items = [_make_write_result_item("task-no-status", status=None)]
        mod._maybe_auto_archive_workstreams(items, workspace_root=wsroot)

        assert (wsroot / "workstreams" / "archive" / "task-no-status").is_dir()


# ---------------------------------------------------------------------------
# Full hook integration — via stdin, exercising main() end to end
# ---------------------------------------------------------------------------


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    stdout_capture = StringIO()
    stderr_capture = StringIO()
    stdin_data = json.dumps(hook_input)
    exit_code = None
    with patch("sys.stdin", StringIO(stdin_data)), \
         patch("sys.stdout", stdout_capture), \
         patch("sys.stderr", stderr_capture):
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code
    return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()


def _write_jsonl_transcript(path: Path, messages: list) -> None:
    with open(path, "w") as fh:
        for msg in messages:
            fh.write(json.dumps(msg) + "\n")


def _subagentstop_transcript_entry(task_id: str, status: str, chat_id=12345) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "mcp__lobster-inbox__write_result",
                    "input": {"chat_id": chat_id, "task_id": task_id, "status": status, "text": "done"},
                }
            ],
        },
        "uuid": "test-uuid",
        "sessionId": "test-session",
    }


class TestFullHookArchivesOnSuccess:
    def test_archives_workstream_dir_end_to_end(self, monkeypatch, tmp_path):
        wsroot = tmp_path / "wsroot"
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(wsroot))
        mod = _load_hook(monkeypatch, tmp_path)

        (wsroot / "workstreams" / "e2e-task").mkdir(parents=True)

        transcript_file = tmp_path / "agent-sub.jsonl"
        _write_jsonl_transcript(
            transcript_file, [_subagentstop_transcript_entry("e2e-task", status="success")]
        )
        hook_input = {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-e2e",
            "agent_transcript_path": str(transcript_file),
        }

        exit_code, _stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"stderr={stderr}"
        assert not (wsroot / "workstreams" / "e2e-task").exists()
        assert (wsroot / "workstreams" / "archive" / "e2e-task").is_dir()

    def test_does_not_archive_on_error_status(self, monkeypatch, tmp_path):
        wsroot = tmp_path / "wsroot"
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(wsroot))
        mod = _load_hook(monkeypatch, tmp_path)

        (wsroot / "workstreams" / "e2e-failed-task").mkdir(parents=True)

        transcript_file = tmp_path / "agent-sub.jsonl"
        _write_jsonl_transcript(
            transcript_file, [_subagentstop_transcript_entry("e2e-failed-task", status="error")]
        )
        hook_input = {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-e2e-fail",
            "agent_transcript_path": str(transcript_file),
        }

        exit_code, _stdout, stderr = _run_hook(mod, hook_input)

        assert exit_code == 0, f"stderr={stderr}"
        # Failed task's workstream dir stays in the active namespace.
        assert (wsroot / "workstreams" / "e2e-failed-task").is_dir()
        assert not (wsroot / "workstreams" / "archive" / "e2e-failed-task").exists()
