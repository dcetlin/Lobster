"""
Unit tests for is_night4_rotation() in scheduled-tasks/decay-detector.py.

The function reads rotation-state.json from HYGIENE_DIR and returns True
only when current_night == 5 (meaning Night 4 just completed — the counter
is incremented after the sweep, so 5 is the post-Night-4 sentinel value).

All tests patch decay_detector.HYGIENE_DIR to a tmp_path fixture so no
real filesystem state is touched.

Named after behaviors, not mechanisms.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load the script module.
#
# decay-detector.py is a standalone script (not an importable package
# module), so we load it via importlib.  We do this inside a function so
# the module-level HYGIENE_DIR is set before exec_module runs, which means
# monkeypatching HYGIENE_DIR in individual tests is sufficient.
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parents[3] / "scheduled-tasks" / "decay-detector.py"
)

# Stub subprocess so the module-level code doesn't attempt real gh calls.
import types as _types

_subprocess_stub = _types.ModuleType("subprocess")
_subprocess_stub.run = lambda *a, **kw: None  # type: ignore[assignment]
_subprocess_stub.CalledProcessError = Exception  # type: ignore[assignment]


def _load_decay_detector():
    with patch.dict("sys.modules", {"subprocess": _subprocess_stub}):
        spec = importlib.util.spec_from_file_location(
            "decay_detector", SCRIPT_PATH
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Load once at module level; individual tests patch the module attribute.
decay_detector = _load_decay_detector()
is_night4_rotation = decay_detector.is_night4_rotation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(directory: Path, payload: object) -> None:
    """Write rotation-state.json into directory."""
    (directory / "rotation-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_raw(directory: Path, text: str) -> None:
    """Write raw (possibly invalid JSON) content into rotation-state.json."""
    (directory / "rotation-state.json").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIsNight4Rotation:
    """Behavioral tests for is_night4_rotation()."""

    def test_returns_true_when_current_night_is_5(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """current_night == 5 is the only value that should return True."""
        monkeypatch.setattr(decay_detector, "HYGIENE_DIR", tmp_path)
        _write_state(tmp_path, {"current_night": decay_detector.NIGHT4_ROTATION_STATE})
        assert is_night4_rotation() is True

    def test_returns_false_when_current_night_is_4(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off-by-one boundary: current_night == 4 means Night 4 has not yet finished."""
        monkeypatch.setattr(decay_detector, "HYGIENE_DIR", tmp_path)
        _write_state(tmp_path, {"current_night": 4})
        assert is_night4_rotation() is False

    def test_returns_false_when_current_night_is_6(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off-by-one boundary: current_night == 6 means Night 5 is now current."""
        monkeypatch.setattr(decay_detector, "HYGIENE_DIR", tmp_path)
        _write_state(tmp_path, {"current_night": 6})
        assert is_night4_rotation() is False

    def test_returns_false_when_current_night_is_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typical early-rotation value should return False."""
        monkeypatch.setattr(decay_detector, "HYGIENE_DIR", tmp_path)
        _write_state(tmp_path, {"current_night": 1})
        assert is_night4_rotation() is False

    def test_returns_false_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent rotation-state.json triggers the exception path → False."""
        monkeypatch.setattr(decay_detector, "HYGIENE_DIR", tmp_path)
        # No state file written — tmp_path is empty.
        assert is_night4_rotation() is False

    def test_returns_false_when_json_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed JSON triggers the exception path → False."""
        monkeypatch.setattr(decay_detector, "HYGIENE_DIR", tmp_path)
        _write_raw(tmp_path, "not-json")
        assert is_night4_rotation() is False

    def test_returns_false_when_key_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid JSON without 'current_night' key triggers the exception path → False."""
        monkeypatch.setattr(decay_detector, "HYGIENE_DIR", tmp_path)
        _write_state(tmp_path, {"other_key": decay_detector.NIGHT4_ROTATION_STATE})
        assert is_night4_rotation() is False

    def test_returns_false_when_value_not_castable_to_int(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """current_night value that cannot be cast to int triggers the exception path → False."""
        monkeypatch.setattr(decay_detector, "HYGIENE_DIR", tmp_path)
        _write_state(tmp_path, {"current_night": "abc"})
        assert is_night4_rotation() is False
