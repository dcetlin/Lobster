"""
Unit tests for _extract_success_criteria in src/orchestration/cultivator.py.

Covers all branches of the pure extraction function:
- Empty body → returns ''
- Body with no matching heading → falls back to first non-heading paragraph
- Heading present but section content is empty → skips to next heading or falls back
- Fallback skips leading headings (paragraphs starting with '#' are not returned)
- Truncation: body exceeds 500 char limit → truncated at exactly 500 chars
- Happy path: well-formed body with Acceptance Criteria heading → returns section content
- All recognised heading variants are matched
- Content stops at next ## heading (not at end of body)
- Body ending without trailing newline → section captured correctly
"""

from __future__ import annotations

import pytest

# Import directly — _extract_success_criteria is a pure function with no I/O
from src.orchestration.cultivator import _extract_success_criteria


# ---------------------------------------------------------------------------
# Empty / missing body
# ---------------------------------------------------------------------------

class TestEmptyBody:
    def test_empty_string_returns_empty(self) -> None:
        assert _extract_success_criteria("") == ""

    def test_whitespace_only_body_returns_empty(self) -> None:
        # No body content and no paragraph to fall back on
        # whitespace-only paragraphs are stripped by paragraph.strip(), so skipped
        assert _extract_success_criteria("   \n\n   ") == ""

    def test_only_headings_returns_empty(self) -> None:
        # All paragraphs start with '#' — fallback skips them, returns ""
        body = "# Title\n\n## Section\n\n### Subsection"
        assert _extract_success_criteria(body) == ""


# ---------------------------------------------------------------------------
# No matching heading → fallback to first paragraph
# ---------------------------------------------------------------------------

class TestNoMatchingHeading:
    def test_fallback_returns_first_paragraph(self) -> None:
        body = "This is the description.\n\nMore details here."
        result = _extract_success_criteria(body)
        assert result == "This is the description."

    def test_fallback_skips_leading_heading_paragraphs(self) -> None:
        # First paragraph is a heading — must be skipped
        body = "## Overview\n\nActual description here."
        result = _extract_success_criteria(body)
        assert result == "Actual description here."

    def test_fallback_skips_multiple_leading_headings(self) -> None:
        body = "# Title\n\n## Subtitle\n\nFirst real paragraph."
        result = _extract_success_criteria(body)
        assert result == "First real paragraph."

    def test_fallback_returns_empty_when_all_paragraphs_are_headings(self) -> None:
        body = "# Title\n\n## Section"
        result = _extract_success_criteria(body)
        assert result == ""

    def test_fallback_with_multiline_paragraph(self) -> None:
        body = "Line one.\nLine two.\nLine three."
        result = _extract_success_criteria(body)
        assert result == "Line one.\nLine two.\nLine three."


# ---------------------------------------------------------------------------
# Heading present but section content is empty
# ---------------------------------------------------------------------------

class TestEmptySection:
    def test_empty_section_before_next_heading_returns_empty(self) -> None:
        # Heading exists but nothing between it and the next heading.
        # The fallback loop splits by "\n\n"; every paragraph starts with "##"
        # so all are skipped. Function returns "".
        body = (
            "## Summary\nSome description.\n\n"
            "## Acceptance Criteria\n\n"
            "## Other Section\nContent."
        )
        result = _extract_success_criteria(body)
        assert result == ""

    def test_empty_section_at_end_of_body_returns_empty(self) -> None:
        # Heading at end with no content after it.
        # All "\n\n"-split paragraphs start with "##" → fallback skips all → "".
        body = "## Summary\nSome description.\n\n## Acceptance Criteria\n"
        result = _extract_success_criteria(body)
        assert result == ""

    def test_empty_section_with_plain_paragraph_elsewhere(self) -> None:
        # Body contains a plain paragraph not under any heading.
        # Fallback loop finds it and returns it.
        body = (
            "Plain description here.\n\n"
            "## Acceptance Criteria\n\n"
            "## Other Section\nContent."
        )
        result = _extract_success_criteria(body)
        assert result == "Plain description here."


# ---------------------------------------------------------------------------
# Truncation at 500 characters
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_fallback_paragraph_truncated_at_500_chars(self) -> None:
        long_paragraph = "x" * 600
        body = f"No headings here.\n\n{long_paragraph}"
        result = _extract_success_criteria(body)
        # First paragraph is "No headings here." (17 chars) — not truncated
        # Second paragraph is 600 chars — but first paragraph is returned
        assert result == "No headings here."

    def test_single_long_paragraph_truncated_at_500_chars(self) -> None:
        # Single paragraph longer than 500 chars — must be truncated
        long_paragraph = "a" * 600
        result = _extract_success_criteria(long_paragraph)
        assert len(result) == 500
        assert result == "a" * 500

    def test_exactly_500_chars_not_truncated(self) -> None:
        exact = "b" * 500
        result = _extract_success_criteria(exact)
        assert result == exact
        assert len(result) == 500

    def test_499_chars_not_truncated(self) -> None:
        body = "c" * 499
        result = _extract_success_criteria(body)
        assert result == body

    def test_501_chars_truncated_to_500(self) -> None:
        body = "d" * 501
        result = _extract_success_criteria(body)
        assert len(result) == 500


# ---------------------------------------------------------------------------
# Happy path: well-formed heading with content
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_acceptance_criteria_heading_extracted(self) -> None:
        body = (
            "## Summary\nFix the thing.\n\n"
            "## Acceptance Criteria\n"
            "- It works\n"
            "- Tests pass\n\n"
            "## Notes\nSome notes."
        )
        result = _extract_success_criteria(body)
        assert result == "- It works\n- Tests pass"

    def test_success_criteria_heading_extracted(self) -> None:
        body = "## Success Criteria\n- Done when green\n\n## Footer\nIgnored."
        result = _extract_success_criteria(body)
        assert result == "- Done when green"

    def test_definition_of_done_heading_extracted(self) -> None:
        body = "## Definition of Done\n- PR merged\n- Tests pass"
        result = _extract_success_criteria(body)
        assert result == "- PR merged\n- Tests pass"

    def test_lowercase_heading_variants_matched(self) -> None:
        for heading in ("## acceptance criteria", "## success criteria", "## definition of done"):
            body = f"## Preamble\nText.\n\n{heading}\n- criterion one"
            result = _extract_success_criteria(body)
            assert result == "- criterion one", f"Failed for heading: {heading!r}"

    def test_content_stops_at_next_heading(self) -> None:
        body = (
            "## Acceptance Criteria\n"
            "- Criterion one\n"
            "- Criterion two\n\n"
            "## Irrelevant Section\n"
            "This must not appear."
        )
        result = _extract_success_criteria(body)
        assert "Irrelevant Section" not in result
        assert "This must not appear" not in result
        assert "Criterion one" in result

    def test_body_without_trailing_newline_captured(self) -> None:
        # No trailing newline after the section content — must still be extracted
        body = "## Acceptance Criteria\n- Must work without trailing newline"
        result = _extract_success_criteria(body)
        assert result == "- Must work without trailing newline"

    def test_first_matching_heading_wins(self) -> None:
        # Both "Acceptance Criteria" and "Success Criteria" present — first wins
        body = (
            "## Acceptance Criteria\n- AC criterion\n\n"
            "## Success Criteria\n- SC criterion"
        )
        result = _extract_success_criteria(body)
        assert result == "- AC criterion"

    def test_multiline_criteria_section_preserved(self) -> None:
        body = (
            "## Acceptance Criteria\n"
            "- Line one\n"
            "- Line two\n"
            "- Line three"
        )
        result = _extract_success_criteria(body)
        assert result == "- Line one\n- Line two\n- Line three"
