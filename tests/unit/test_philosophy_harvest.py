"""Unit tests for scheduled-tasks/philosophy-harvest.py."""

import json
import sys
from pathlib import Path

import pytest

# Make the scheduled-tasks directory importable so the script can be imported
# without a package install. We import via importlib to avoid the top-level
# path-setup side effects running at collection time.
import importlib.util
import types

_REPO_ROOT = Path(__file__).parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scheduled-tasks" / "philosophy-harvest.py"


def _load_harvest_module() -> types.ModuleType:
    """Load philosophy-harvest.py as a module without executing __main__."""
    spec = importlib.util.spec_from_file_location(
        "philosophy_harvest_script",
        _SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Pre-populate sys.path so the module's path setup succeeds.
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


# Load once at module level — tests reference module attributes.
_mod = _load_harvest_module()


# ---------------------------------------------------------------------------
# _is_job_enabled — pure gate logic (no external calls needed for happy paths)
# ---------------------------------------------------------------------------


class TestIsJobEnabled:
    """Tests for the _is_job_enabled() gate function."""

    def test_returns_true_when_jobs_file_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        assert _mod._is_job_enabled("philosophy-harvest") is True

    def test_returns_true_when_job_missing_from_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        jobs_file = tmp_path / "scheduled-jobs" / "jobs.json"
        jobs_file.parent.mkdir(parents=True)
        jobs_file.write_text(json.dumps({"jobs": {}}))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        assert _mod._is_job_enabled("philosophy-harvest") is True

    def test_returns_true_when_enabled_is_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        jobs_file = tmp_path / "scheduled-jobs" / "jobs.json"
        jobs_file.parent.mkdir(parents=True)
        jobs_file.write_text(json.dumps({"jobs": {"philosophy-harvest": {"enabled": True}}}))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        assert _mod._is_job_enabled("philosophy-harvest") is True

    def test_returns_false_when_enabled_is_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        jobs_file = tmp_path / "scheduled-jobs" / "jobs.json"
        jobs_file.parent.mkdir(parents=True)
        jobs_file.write_text(json.dumps({"jobs": {"philosophy-harvest": {"enabled": False}}}))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        assert _mod._is_job_enabled("philosophy-harvest") is False

    def test_returns_true_on_malformed_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        jobs_file = tmp_path / "scheduled-jobs" / "jobs.json"
        jobs_file.parent.mkdir(parents=True)
        jobs_file.write_text("not valid json {{")
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        assert _mod._is_job_enabled("philosophy-harvest") is True


# ---------------------------------------------------------------------------
# extract_action_seeds — pure YAML extraction
# ---------------------------------------------------------------------------

SAMPLE_WITH_SEEDS = """\
## Philosophy Session

Some session content.

```yaml
action_seeds:
  bootup_candidates:
    - context: dispatcher
      text: Consider adding heartbeat gate
      rationale: Prevents race conditions
  memory_observations:
    - type: design_gap
      text: Missing observability in executor path
```
"""

SAMPLE_WITHOUT_SEEDS = """\
## Philosophy Session

```yaml
other_key:
  value: 42
```
"""

SAMPLE_EMPTY = "No YAML here."


class TestExtractActionSeeds:
    """Tests for extract_action_seeds pure function."""

    def test_extracts_seeds_from_fenced_block(self) -> None:
        result = _mod.extract_action_seeds(SAMPLE_WITH_SEEDS)
        assert result is not None
        assert "bootup_candidates" in result
        assert len(result["bootup_candidates"]) == 1

    def test_returns_none_when_no_action_seeds_key(self) -> None:
        result = _mod.extract_action_seeds(SAMPLE_WITHOUT_SEEDS)
        assert result is None

    def test_returns_none_for_plain_content(self) -> None:
        result = _mod.extract_action_seeds(SAMPLE_EMPTY)
        assert result is None


# ---------------------------------------------------------------------------
# format_bootup_notification — pure formatting
# ---------------------------------------------------------------------------


class TestFormatBootupNotification:
    """Tests for format_bootup_notification pure function."""

    def test_formats_dict_candidate(self) -> None:
        candidate = {
            "context": "dispatcher",
            "text": "Add heartbeat gate",
            "rationale": "Prevents race conditions",
        }
        result = _mod.format_bootup_notification(candidate, "session-2026-05-01")
        assert "Bootup candidate from session-2026-05-01" in result
        assert "Target: dispatcher" in result
        assert "Add heartbeat gate" in result
        assert "Prevents race conditions" in result

    def test_formats_string_candidate(self) -> None:
        result = _mod.format_bootup_notification("Plain string candidate", "session-x")
        assert "Bootup candidate from session-x" in result
        assert "Plain string candidate" in result


# ---------------------------------------------------------------------------
# find_philosophy_sessions — directory scanning
# ---------------------------------------------------------------------------


class TestFindPhilosophySessions:
    """Tests for find_philosophy_sessions pure function."""

    def test_returns_sorted_md_files(self, tmp_path: Path) -> None:
        (tmp_path / "b.md").write_text("b")
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.md").write_text("c")
        result = _mod.find_philosophy_sessions(tmp_path)
        names = [p.name for p in result]
        assert names == sorted(names)
        assert len(result) == 3

    def test_ignores_non_md_files(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("ignore me")
        (tmp_path / "session.md").write_text("include me")
        result = _mod.find_philosophy_sessions(tmp_path)
        assert len(result) == 1
        assert result[0].name == "session.md"


# ---------------------------------------------------------------------------
# load_state / save_state — JSON state persistence
# ---------------------------------------------------------------------------


class TestStateRoundtrip:
    """Tests for load_state and save_state."""

    def test_load_returns_empty_set_when_absent(self, tmp_path: Path) -> None:
        result = _mod.load_state(tmp_path / "nonexistent.json")
        assert result == set()

    def test_save_and_reload(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        _mod.save_state(state_path, {"philosophy/session-a.md", "philosophy/session-b.md"})
        reloaded = _mod.load_state(state_path)
        assert reloaded == {"philosophy/session-a.md", "philosophy/session-b.md"}

    def test_load_returns_empty_set_on_corrupt_json(self, tmp_path: Path) -> None:
        state_path = tmp_path / "state.json"
        state_path.write_text("corrupt{{")
        result = _mod.load_state(state_path)
        assert result == set()
