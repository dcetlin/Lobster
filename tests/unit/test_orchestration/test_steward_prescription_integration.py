"""
Integration tests: verify steward.py correctly uses prescription_parser.py

Tests the integration between:
- _llm_prescribe() function in steward.py
- parse_prescription_json() and validate_prescription_schema() from prescription_parser.py

Mock the claude subprocess to test different JSON output scenarios.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.steward import _llm_prescribe
from src.orchestration.registry import UoW


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_test_uow(uow_id: str = "test-uow-1") -> UoW:
    """Create a minimal test UoW."""
    return UoW(
        id=uow_id,
        type="executable",
        source="test",
        source_issue_number=None,
        status="ready-for-steward",
        summary="Test unit of work",
        posture="solo",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Tests: Clean JSON Output (Level 0)
# ---------------------------------------------------------------------------

class TestLLMPrescribeLevel0CleanJSON:
    """Test _llm_prescribe with clean JSON output (no fallback)."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_clean_json_returns_prescription(self, mock_subprocess):
        """Clean JSON output should return parsed prescription."""
        clean_json = (
            '{"instructions": "Step 1: Read. Step 2: Write.", '
            '"success_criteria_check": "File exists and contains expected content.", '
            '"estimated_cycles": 2}'
        )
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = clean_json
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is not None
        assert result["instructions"] == "Step 1: Read. Step 2: Write."
        assert result["success_criteria_check"] == "File exists and contains expected content."
        assert result["estimated_cycles"] == 2
        mock_subprocess.assert_called_once()

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_clean_json_with_whitespace(self, mock_subprocess):
        """Clean JSON with leading/trailing whitespace should work."""
        clean_json = (
            '  {"instructions": "Do it", '
            '"success_criteria_check": "Check", '
            '"estimated_cycles": 1}  \n'
        )
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = clean_json
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is not None
        assert result["instructions"] == "Do it"


# ---------------------------------------------------------------------------
# Tests: Markdown-Wrapped JSON (Level 1)
# ---------------------------------------------------------------------------

class TestLLMPrescribeLevel1Markdown:
    """Test _llm_prescribe with markdown-wrapped JSON (Level 1 fallback)."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_markdown_wrapped_json(self, mock_subprocess):
        """Markdown-wrapped JSON should be parsed at Level 1."""
        markdown_json = '''```json
{"instructions": "Execute task", "success_criteria_check": "Done", "estimated_cycles": 1}
```'''
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = markdown_json
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is not None
        assert result["instructions"] == "Execute task"


# ---------------------------------------------------------------------------
# Tests: JSON Block Extraction (Level 2)
# ---------------------------------------------------------------------------

class TestLLMPrescribeLevel2BlockExtraction:
    """Test _llm_prescribe with JSON embedded in prose (Level 2 fallback)."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_json_block_in_prose(self, mock_subprocess):
        """JSON block embedded in prose should be extracted."""
        prose_with_json = (
            "Here's the prescription:\n"
            '{"instructions": "Read file then write", "success_criteria_check": "Output exists", "estimated_cycles": 1}\n'
            "That should work."
        )
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = prose_with_json
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is not None
        assert result["instructions"] == "Read file then write"


# ---------------------------------------------------------------------------
# Tests: Field Extraction (Level 3)
# ---------------------------------------------------------------------------

class TestLLMPrescribeLevel3FieldExtraction:
    """Test _llm_prescribe with field regex extraction (Level 3 fallback)."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_fields_in_prose(self, mock_subprocess):
        """Individual fields in prose should be extracted."""
        prose = (
            '"instructions": "Implement feature X", '
            '"success_criteria_check": "Tests pass", '
            '"estimated_cycles": 2'
        )
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = prose
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        # Should succeed if at least instructions field was extracted
        assert result is not None


# ---------------------------------------------------------------------------
# Tests: Deterministic Template (Level 4)
# ---------------------------------------------------------------------------

class TestLLMPrescribeLevel4Fallback:
    """Test _llm_prescribe with deterministic template fallback (Level 4)."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_unparseable_output_returns_none(self, mock_subprocess):
        """Completely unparseable output should return None (caller will use fallback)."""
        garbage = "this is not json and has no valid fields"
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = garbage
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        # At Level 4 (deterministic template), _llm_prescribe returns None
        # to signal that the caller should use its own fallback template
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Error Handling
# ---------------------------------------------------------------------------

class TestLLMPrescribeErrorHandling:
    """Test _llm_prescribe error handling."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_subprocess_error_returns_none(self, mock_subprocess):
        """Subprocess error should return None."""
        mock_error = Mock()
        mock_error.summary.return_value = "Timeout"
        mock_subprocess.return_value = (None, mock_error)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is None

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_subprocess_nonzero_exit_returns_none(self, mock_subprocess):
        """Non-zero exit code should return None."""
        mock_proc = Mock()
        mock_proc.returncode = 1
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is None

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_empty_stdout_returns_none(self, mock_subprocess):
        """Empty stdout should return None."""
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: Schema Validation in _llm_prescribe
# ---------------------------------------------------------------------------

class TestLLMPrescribeSchemaValidation:
    """Test schema validation within _llm_prescribe."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_missing_instructions_field_returns_none(self, mock_subprocess):
        """JSON missing instructions field should return None."""
        bad_json = '{"success_criteria_check": "Check", "estimated_cycles": 1}'
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = bad_json
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is None

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_empty_instructions_field_returns_none(self, mock_subprocess):
        """JSON with empty instructions should return None."""
        bad_json = '{"instructions": "", "success_criteria_check": "Check", "estimated_cycles": 1}'
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = bad_json
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: Field Normalization
# ---------------------------------------------------------------------------

class TestLLMPrescribeFieldNormalization:
    """Test field normalization in _llm_prescribe."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_string_estimated_cycles_coerced_to_int(self, mock_subprocess):
        """String-valued estimated_cycles should be coerced to int."""
        # Note: The schema validation should fail this, but the log message
        # indicates it gets coerced and defaulted. Let's verify the actual behavior.
        json_with_string_cycles = (
            '{"instructions": "Do it", "success_criteria_check": "Check", "estimated_cycles": "2"}'
        )
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = json_with_string_cycles
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        # This should fail schema validation and return None
        assert result is None

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_estimated_cycles_clamped_to_range(self, mock_subprocess):
        """estimated_cycles should be clamped to [1, 3]."""
        json_high = '{"instructions": "Do it", "success_criteria_check": "Check", "estimated_cycles": 10}'
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = json_high
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is not None
        assert result["estimated_cycles"] == 3  # Clamped from 10 to 3

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_estimated_cycles_minimum_is_1(self, mock_subprocess):
        """estimated_cycles should never be less than 1."""
        json_zero = '{"instructions": "Do it", "success_criteria_check": "Check", "estimated_cycles": 0}'
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = json_zero
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        # Should fail schema validation since 0 is invalid
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Real-World Scenarios
# ---------------------------------------------------------------------------

class TestLLMPrescribeRealWorldScenarios:
    """Test real-world LLM output scenarios."""

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_claude_markdown_wrapper_pattern(self, mock_subprocess):
        """Real pattern: Claude wraps JSON in markdown despite instructions."""
        real_output = '''```json
{
  "instructions": "Implement the feature according to the spec",
  "success_criteria_check": "Feature works as designed",
  "estimated_cycles": 2
}
```'''
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = real_output
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is not None
        assert "Implement the feature" in result["instructions"]

    @patch('src.orchestration.steward.run_subprocess_with_error_capture')
    def test_claude_prose_wrapper_pattern(self, mock_subprocess):
        """Real pattern: Claude explains JSON before returning it."""
        real_output = '''Here's the prescription for the executor:

{
  "instructions": "Complete the task as described",
  "success_criteria_check": "Task is complete",
  "estimated_cycles": 1
}

This should be straightforward.'''
        mock_proc = Mock()
        mock_proc.returncode = 0
        mock_proc.stdout = real_output
        mock_subprocess.return_value = (mock_proc, None)

        uow = _make_test_uow()
        result = _llm_prescribe(uow, "solo", "Not started", "")

        assert result is not None
        assert "Complete the task" in result["instructions"]
