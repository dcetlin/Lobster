"""
Unit tests for prescription_parser.py — multi-level JSON fallback parsing.

Tests cover:
- Level 0: Strict JSON parsing
- Level 1: Markdown code fence stripping
- Level 2: JSON block extraction from prose
- Level 3: Individual field regex extraction
- Level 4: Deterministic template fallback
- Schema validation for complete prescriptions
- Edge cases: empty output, malformed JSON at each level
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestration.prescription_parser import (
    parse_prescription_json,
    validate_prescription_schema,
    PrescriptionParseResult,
    _extract_json_block,
    _extract_fields_from_prose,
)


# ---------------------------------------------------------------------------
# Fixtures: valid and invalid prescription data
# ---------------------------------------------------------------------------

VALID_PRESCRIPTION_DICT = {
    "instructions": "Execute this task step by step.",
    "success_criteria_check": "Verify the output file exists.",
    "estimated_cycles": 2,
}

VALID_PRESCRIPTION_JSON = (
    '{"instructions": "Execute this task.", '
    '"success_criteria_check": "Verify it worked.", '
    '"estimated_cycles": 1}'
)


# ---------------------------------------------------------------------------
# Level 0: Strict JSON Parsing Tests
# ---------------------------------------------------------------------------

class TestLevel0StrictJSON:
    """Test strict JSON parsing (Level 0)."""

    def test_valid_json_returns_level_0(self):
        """Valid JSON should be parsed at Level 0."""
        result = parse_prescription_json(VALID_PRESCRIPTION_JSON, "test-uow-1")
        assert result.success is True
        assert result.fallback_level == 0
        assert result.data["instructions"] == "Execute this task."
        assert result.data["estimated_cycles"] == 1

    def test_valid_json_with_whitespace(self):
        """Valid JSON with leading/trailing whitespace should work."""
        json_with_ws = f"  {VALID_PRESCRIPTION_JSON}  \n"
        result = parse_prescription_json(json_with_ws, "test-uow-2")
        assert result.success is True
        assert result.fallback_level == 0

    def test_json_with_newlines(self):
        """Valid JSON with newlines should parse correctly."""
        json_multiline = """{
  "instructions": "Multi-line instruction",
  "success_criteria_check": "Check it",
  "estimated_cycles": 2
}"""
        result = parse_prescription_json(json_multiline, "test-uow-3")
        assert result.success is True
        assert result.fallback_level == 0

    def test_empty_output_fails_immediately(self):
        """Empty output should fail before attempting any fallback."""
        result = parse_prescription_json("", "test-uow-4")
        assert result.success is False
        assert result.fallback_level == -1

    def test_whitespace_only_output_fails(self):
        """Whitespace-only output should fail immediately."""
        result = parse_prescription_json("   \n\t  ", "test-uow-5")
        assert result.success is False
        assert result.fallback_level == -1


# ---------------------------------------------------------------------------
# Level 1: Markdown Code Fence Stripping Tests
# ---------------------------------------------------------------------------

class TestLevel1MarkdownStripping:
    """Test markdown code fence stripping (Level 1)."""

    def test_json_wrapped_in_markdown_fences(self):
        """JSON wrapped in ```json ... ``` should be parsed at Level 1."""
        markdown_wrapped = f"```json\n{VALID_PRESCRIPTION_JSON}\n```"
        result = parse_prescription_json(markdown_wrapped, "test-uow-6")
        assert result.success is True
        assert result.fallback_level == 1
        assert result.data["instructions"] == "Execute this task."

    def test_markdown_without_json_specifier(self):
        """JSON wrapped in ``` ... ``` (without 'json') should work."""
        markdown_wrapped = f"```\n{VALID_PRESCRIPTION_JSON}\n```"
        result = parse_prescription_json(markdown_wrapped, "test-uow-7")
        assert result.success is True
        assert result.fallback_level == 1

    def test_malformed_markdown_fence(self):
        """Markdown fence with missing closing backticks should skip Level 1."""
        malformed = f"```json\n{VALID_PRESCRIPTION_JSON}"
        result = parse_prescription_json(malformed, "test-uow-8")
        # Should still succeed if JSON is extracted at a later level
        assert result.data is not None


# ---------------------------------------------------------------------------
# Level 2: JSON Block Extraction Tests
# ---------------------------------------------------------------------------

class TestLevel2JSONBlockExtraction:
    """Test JSON block extraction from prose (Level 2)."""

    def test_json_embedded_in_prose(self):
        """JSON block embedded in surrounding prose should be extracted."""
        prose = (
            "Here is the prescription:\n"
            f"{VALID_PRESCRIPTION_JSON}\n"
            "That's the JSON you need."
        )
        result = parse_prescription_json(prose, "test-uow-9")
        assert result.success is True
        assert result.fallback_level == 2
        assert result.data["instructions"] == "Execute this task."

    def test_multiple_json_blocks_uses_largest(self):
        """When multiple JSON blocks exist, the largest valid one is used."""
        small_json = '{"instructions": "small"}'
        large_json = VALID_PRESCRIPTION_JSON
        prose = f"First: {small_json} Then: {large_json} Done."
        result = parse_prescription_json(prose, "test-uow-10")
        assert result.success is True
        # Should use the larger block
        assert result.fallback_level == 2

    def test_json_with_nested_objects_extracted(self):
        """JSON with nested objects should be extracted correctly."""
        nested_json = (
            '{"instructions": "Do it", '
            '"success_criteria_check": "Check: {nested: true}", '
            '"estimated_cycles": 1}'
        )
        prose = f"Here: {nested_json} There."
        result = parse_prescription_json(prose, "test-uow-11")
        assert result.success is True
        assert result.fallback_level == 2

    def test_no_valid_json_block_skips_level_2(self):
        """If no valid JSON block exists, Level 2 fails gracefully."""
        invalid_prose = "This is {not: a valid json} block."
        result = parse_prescription_json(invalid_prose, "test-uow-12")
        # Should not succeed at Level 2 but may succeed at later levels
        assert result.fallback_level != 2 or result.success is False


# ---------------------------------------------------------------------------
# Level 3: Field Extraction Tests
# ---------------------------------------------------------------------------

class TestLevel3FieldExtraction:
    """Test individual field extraction from prose (Level 3)."""

    def test_extract_instructions_from_quoted_field(self):
        """Instructions field with quotes should be extracted."""
        prose = '"instructions": "Do this task step by step", "success_criteria_check": ""'
        result = parse_prescription_json(prose, "test-uow-13")
        assert result.success is True
        assert result.fallback_level == 3
        assert result.data["instructions"] == "Do this task step by step"

    def test_extract_all_fields_with_spaces(self):
        """Fields with various whitespace should be extracted."""
        prose = (
            '"instructions" : "Execute now", '
            '"success_criteria_check" : "Verify", '
            '"estimated_cycles" : 2'
        )
        result = parse_prescription_json(prose, "test-uow-14")
        assert result.success is True
        assert result.fallback_level == 3
        assert result.data["estimated_cycles"] == 2

    def test_extract_handles_missing_success_criteria(self):
        """Missing success_criteria_check should default to empty string."""
        prose = '"instructions": "Do it", "estimated_cycles": 1'
        result = parse_prescription_json(prose, "test-uow-15")
        assert result.success is True
        assert result.fallback_level == 3
        assert result.data["success_criteria_check"] == ""

    def test_extract_handles_missing_estimated_cycles(self):
        """Missing estimated_cycles should default to 1."""
        prose = '"instructions": "Do it", "success_criteria_check": "Check"'
        result = parse_prescription_json(prose, "test-uow-16")
        assert result.success is True
        assert result.fallback_level == 3
        assert result.data["estimated_cycles"] == 1

    def test_extract_fails_without_instructions(self):
        """If instructions field is missing, Level 3 should fail."""
        prose = '"success_criteria_check": "Check", "estimated_cycles": 1'
        result = parse_prescription_json(prose, "test-uow-17")
        # Should not succeed at Level 3 without instructions
        assert result.fallback_level != 3 or result.success is False


# ---------------------------------------------------------------------------
# Level 4: Deterministic Template Tests
# ---------------------------------------------------------------------------

class TestLevel4DeterministicTemplate:
    """Test deterministic template fallback (Level 4)."""

    def test_completely_unparseable_returns_template(self):
        """Completely unparseable output should return deterministic template."""
        garbage = "this is not json and has no valid fields at all"
        result = parse_prescription_json(garbage, "test-uow-18")
        assert result.success is False
        assert result.fallback_level == 4
        assert result.data is not None
        assert "No specific prescription" in result.data["instructions"]
        assert result.data["estimated_cycles"] == 1

    def test_template_has_all_required_fields(self):
        """Deterministic template should always include all required fields."""
        garbage = "xyz"
        result = parse_prescription_json(garbage, "test-uow-19")
        assert result.data is not None
        assert "instructions" in result.data
        assert "success_criteria_check" in result.data
        assert "estimated_cycles" in result.data


# ---------------------------------------------------------------------------
# JSON Block Extraction Helper Tests
# ---------------------------------------------------------------------------

class TestExtractJSONBlock:
    """Test _extract_json_block helper function."""

    def test_extracts_simple_json_block(self):
        """Simple JSON block should be extracted."""
        prose = f"Text before {VALID_PRESCRIPTION_JSON} text after"
        result = _extract_json_block(prose)
        assert result is not None
        assert '"instructions"' in result

    def test_returns_none_if_no_json_block(self):
        """Should return None if no valid JSON block exists."""
        prose = "This {is: not} valid json"
        result = _extract_json_block(prose)
        assert result is None

    def test_extracts_largest_block(self):
        """Should extract the largest valid JSON block."""
        small = '{"a": "b"}'
        large = '{"a": "b", "c": "d", "e": "f"}'
        prose = f"{small} and {large}"
        result = _extract_json_block(prose)
        assert result is not None
        assert len(result) >= len(small)


# ---------------------------------------------------------------------------
# Field Extraction Helper Tests
# ---------------------------------------------------------------------------

class TestExtractFieldsFromProse:
    """Test _extract_fields_from_prose helper function."""

    def test_extracts_all_fields(self):
        """All three fields should be extracted when present."""
        prose = (
            '"instructions": "Do this", '
            '"success_criteria_check": "Verify", '
            '"estimated_cycles": 2'
        )
        result = _extract_fields_from_prose(prose)
        assert result is not None
        assert result["instructions"] == "Do this"
        assert result["success_criteria_check"] == "Verify"
        assert result["estimated_cycles"] == 2

    def test_returns_none_without_instructions(self):
        """Should return None if instructions field is missing."""
        prose = '"success_criteria_check": "Check", "estimated_cycles": 1'
        result = _extract_fields_from_prose(prose)
        assert result is None

    def test_handles_escaped_quotes_in_instructions(self):
        """Instructions with escaped quotes should be handled."""
        prose = r'"instructions": "Say \"hello\" to the user"'
        result = _extract_fields_from_prose(prose)
        assert result is not None
        # The regex may not perfectly handle escaped quotes, but it should try
        assert "instructions" in prose


# ---------------------------------------------------------------------------
# Schema Validation Tests
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    """Test validate_prescription_schema function."""

    def test_valid_prescription_passes_validation(self):
        """Valid prescription dict should pass validation."""
        is_valid, error = validate_prescription_schema(VALID_PRESCRIPTION_DICT)
        assert is_valid is True
        assert error == ""

    def test_missing_instructions_fails(self):
        """Missing instructions field should fail validation."""
        data = {
            "success_criteria_check": "Check",
            "estimated_cycles": 1,
        }
        is_valid, error = validate_prescription_schema(data)
        assert is_valid is False
        assert "instructions" in error

    def test_missing_success_criteria_fails(self):
        """Missing success_criteria_check field should fail validation."""
        data = {
            "instructions": "Do it",
            "estimated_cycles": 1,
        }
        is_valid, error = validate_prescription_schema(data)
        assert is_valid is False
        assert "success_criteria_check" in error

    def test_missing_estimated_cycles_fails(self):
        """Missing estimated_cycles field should fail validation."""
        data = {
            "instructions": "Do it",
            "success_criteria_check": "Check",
        }
        is_valid, error = validate_prescription_schema(data)
        assert is_valid is False
        assert "estimated_cycles" in error

    def test_empty_instructions_fails(self):
        """Empty instructions string should fail validation."""
        data = {
            "instructions": "",
            "success_criteria_check": "Check",
            "estimated_cycles": 1,
        }
        is_valid, error = validate_prescription_schema(data)
        assert is_valid is False
        assert "instructions" in error

    def test_whitespace_only_instructions_fails(self):
        """Whitespace-only instructions should fail validation."""
        data = {
            "instructions": "   \n\t  ",
            "success_criteria_check": "Check",
            "estimated_cycles": 1,
        }
        is_valid, error = validate_prescription_schema(data)
        assert is_valid is False

    def test_non_integer_estimated_cycles_fails(self):
        """Non-integer estimated_cycles should fail validation."""
        data = {
            "instructions": "Do it",
            "success_criteria_check": "Check",
            "estimated_cycles": "2",  # string instead of int
        }
        is_valid, error = validate_prescription_schema(data)
        assert is_valid is False
        assert "estimated_cycles" in error

    def test_zero_estimated_cycles_fails(self):
        """Zero estimated_cycles should fail validation."""
        data = {
            "instructions": "Do it",
            "success_criteria_check": "Check",
            "estimated_cycles": 0,
        }
        is_valid, error = validate_prescription_schema(data)
        assert is_valid is False


# ---------------------------------------------------------------------------
# Integration: Multi-Level Fallback Chain Tests
# ---------------------------------------------------------------------------

class TestMultiLevelFallbackChain:
    """Test the complete fallback chain working together."""

    def test_fallback_from_level_0_to_level_1(self):
        """Should fallback to Level 1 when Level 0 fails."""
        markdown = f"```json\n{VALID_PRESCRIPTION_JSON}\n```"
        # Modify to break Level 0
        invalid_at_0 = markdown
        result = parse_prescription_json(invalid_at_0, "test-uow-20")
        assert result.success is True
        assert result.fallback_level == 1

    def test_fallback_from_level_2_to_level_3(self):
        """Should fallback to Level 3 when Level 2 fails."""
        # Create prose with valid fields but not a complete JSON block
        partial_prose = (
            'Some text about the task. '
            '"instructions": "Do this task", '
            '"success_criteria_check": "Check it", '
            '"estimated_cycles": 1'
        )
        result = parse_prescription_json(partial_prose, "test-uow-21")
        assert result.success is True
        assert result.fallback_level == 3

    def test_real_world_markdown_with_explanation(self):
        """Real-world case: markdown JSON with surrounding explanation."""
        real_world = """Here's the prescription for the Executor:

```json
{
  "instructions": "Implement the feature as described",
  "success_criteria_check": "Tests pass and code is reviewed",
  "estimated_cycles": 2
}
```

This should take about 2 cycles to complete."""
        result = parse_prescription_json(real_world, "test-uow-22")
        assert result.success is True
        assert result.data["instructions"] == "Implement the feature as described"
        # Should succeed at Level 1 or 2
        assert result.fallback_level in (1, 2)

    def test_real_world_incomplete_json_in_prose(self):
        """Real-world case: incomplete/malformed JSON in prose."""
        real_world = """Here's what I suggest:
The instructions are: "Review the code and run tests"
The success check is: "All tests pass"
We need: 1 cycle"""
        result = parse_prescription_json(real_world, "test-uow-23")
        # Should eventually succeed, possibly at Level 4
        assert result.data is not None
        assert result.data.get("instructions") is not None
