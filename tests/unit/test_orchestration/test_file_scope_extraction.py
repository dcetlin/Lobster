"""
Tests for _extract_file_scope in cultivator.py.
"""

import pytest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.cultivator import _extract_file_scope


class TestExtractFileScope:
    def test_single_py_file(self):
        body = "Fix the bug in `src/orchestration/steward.py` before release."
        result = _extract_file_scope(body)
        assert result == ["src/orchestration/steward.py"]

    def test_multiple_paths_returned_sorted(self):
        body = "Touches `tests/unit/test_foo.py` and `src/foo/bar.py`."
        result = _extract_file_scope(body)
        assert result == ["src/foo/bar.py", "tests/unit/test_foo.py"]

    def test_no_backtick_paths_returns_none(self):
        body = "This issue has only prose and no file references."
        result = _extract_file_scope(body)
        assert result is None

    def test_empty_string_returns_none(self):
        result = _extract_file_scope("")
        assert result is None

    def test_none_body_returns_none(self):
        result = _extract_file_scope(None)
        assert result is None

    def test_token_without_slash_not_extracted(self):
        body = "Look at `steward` for the issue."
        result = _extract_file_scope(body)
        assert result is None

    def test_deduplication(self):
        body = "See `src/orchestration/steward.py` and `src/orchestration/steward.py` again."
        result = _extract_file_scope(body)
        assert result == ["src/orchestration/steward.py"]

    def test_tests_prefix_extracted(self):
        body = "Update `tests/unit/test_cultivator.py`."
        result = _extract_file_scope(body)
        assert result == ["tests/unit/test_cultivator.py"]

    def test_scripts_prefix_extracted(self):
        body = "Modify `scripts/upgrade.sh` for migration."
        result = _extract_file_scope(body)
        assert result == ["scripts/upgrade.sh"]

    def test_oracle_prefix_extracted(self):
        body = "Record in `oracle/learnings.md`."
        result = _extract_file_scope(body)
        assert result == ["oracle/learnings.md"]

    def test_extension_match_without_known_prefix(self):
        body = "Edit `config/settings.yaml` for this feature."
        result = _extract_file_scope(body)
        assert result == ["config/settings.yaml"]

    def test_token_unknown_prefix_no_extension_not_extracted(self):
        body = "See `unknown/something` for context."
        result = _extract_file_scope(body)
        assert result is None
