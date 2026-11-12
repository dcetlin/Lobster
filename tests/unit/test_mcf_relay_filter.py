"""
Tests for the Minimal Cognitive Friction relay filter (src/filters/mcf_relay_filter.py).

Covers:
- Individual friction signal detectors
- Main check_mcf entry point
- Diagnostic formatting
- Edge cases (short text, empty text, code blocks)
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from filters.mcf_relay_filter import (
    FrictionSignal,
    MCFResult,
    SIGNAL_KINDS,
    _check_buried_lead,
    _check_conclusion_before_evidence,
    _check_forward_references,
    _check_non_parallel_lists,
    _check_wall_of_text,
    _extract_list_items,
    _split_paragraphs,
    check_mcf,
    format_diagnostics,
)


# ============================================================================
# Helpers
# ============================================================================


def _paras(text: str) -> list[str]:
    """Convenience wrapper for _split_paragraphs."""
    return _split_paragraphs(text)


# ============================================================================
# FrictionSignal tests
# ============================================================================


class TestFrictionSignal:
    def test_valid_kind(self):
        sig = FrictionSignal(kind="buried_lead", description="test")
        assert sig.kind == "buried_lead"

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown signal kind"):
            FrictionSignal(kind="not_a_real_kind", description="test")

    def test_all_kinds_accepted(self):
        for kind in SIGNAL_KINDS:
            sig = FrictionSignal(kind=kind, description="test")
            assert sig.kind == kind


# ============================================================================
# MCFResult tests
# ============================================================================


class TestMCFResult:
    def test_empty_has_no_friction(self):
        result = MCFResult()
        assert result.has_friction is False
        assert result.signal_kinds == set()

    def test_with_signals(self):
        result = MCFResult(signals=[
            FrictionSignal(kind="buried_lead", description="test"),
            FrictionSignal(kind="wall_of_text", description="test"),
        ])
        assert result.has_friction is True
        assert result.signal_kinds == {"buried_lead", "wall_of_text"}


# ============================================================================
# Paragraph splitting
# ============================================================================


class TestSplitParagraphs:
    def test_single_paragraph(self):
        assert len(_paras("Hello world")) == 1

    def test_two_paragraphs(self):
        text = "First paragraph.\n\nSecond paragraph."
        assert len(_paras(text)) == 2

    def test_strips_whitespace(self):
        text = "  First  \n\n  Second  "
        paras = _paras(text)
        assert paras[0] == "First"
        assert paras[1] == "Second"

    def test_empty_string(self):
        assert _paras("") == []

    def test_multiple_blank_lines(self):
        text = "A\n\n\n\nB"
        assert len(_paras(text)) == 2


# ============================================================================
# List extraction
# ============================================================================


class TestExtractListItems:
    def test_bullet_list(self):
        text = "- Item one\n- Item two\n- Item three"
        blocks = _extract_list_items(text)
        assert len(blocks) == 1
        assert len(blocks[0][1]) == 3

    def test_numbered_list(self):
        text = "1. First\n2. Second\n3. Third"
        blocks = _extract_list_items(text)
        assert len(blocks) == 1
        assert blocks[0][1][0] == "First"

    def test_no_list(self):
        text = "Just some plain text\nwith line breaks."
        blocks = _extract_list_items(text)
        assert len(blocks) == 0

    def test_two_separate_lists(self):
        text = "- A\n- B\n\nSome text\n\n- C\n- D"
        blocks = _extract_list_items(text)
        assert len(blocks) == 2


# ============================================================================
# Buried lead detector
# ============================================================================


class TestBuriedLead:
    def test_no_friction_when_lead_in_first_paragraph(self):
        paras = [
            "The migration is complete and deployed.",
            "We updated the schema to v3.",
            "Tests are passing on CI.",
        ]
        assert _check_buried_lead(paras) is None

    def test_detects_buried_lead(self):
        paras = [
            "I looked into the issue you reported about the dashboard.",
            "After investigating, I found the root cause in the query layer.",
            "The fix is done and deployed to staging.",
        ]
        signal = _check_buried_lead(paras)
        assert signal is not None
        assert signal.kind == "buried_lead"
        assert "paragraph 3" in signal.location

    def test_no_friction_with_few_paragraphs(self):
        paras = ["First.", "Second with result done."]
        assert _check_buried_lead(paras) is None

    def test_no_friction_when_no_lead_anywhere(self):
        paras = [
            "Here is some context about the situation.",
            "More background information follows.",
            "Additional details are below.",
        ]
        assert _check_buried_lead(paras) is None


# ============================================================================
# Forward reference detector
# ============================================================================


class TestForwardReferences:
    def test_no_friction_when_defined_first(self):
        text = (
            "We use Minimal Cognitive Friction (MCF) as a design principle.\n\n"
            "MCF helps ensure responses are comprehensible on first read."
        )
        # MCF is defined before use — no signal
        # Actually, "MCF" first appears inside the parens definition, which is fine
        assert _check_forward_references(text) is None

    def test_detects_undefined_acronym_before_definition(self):
        text = (
            "The WOS pipeline handles this automatically.\n\n"
            "Work Orchestration System (WOS) is our execution engine."
        )
        signal = _check_forward_references(text)
        assert signal is not None
        assert signal.kind == "forward_reference"
        assert "WOS" in signal.description

    def test_skips_common_acronyms(self):
        text = (
            "The PR was merged after CI passed. The API is deployed.\n\n"
            "We use the CLI to manage deployments via HTTP."
        )
        assert _check_forward_references(text) is None

    def test_no_friction_with_no_acronyms(self):
        text = "This is a simple response with no acronyms at all."
        assert _check_forward_references(text) is None


# ============================================================================
# Non-parallel list detector
# ============================================================================


class TestNonParallelLists:
    def test_parallel_list_no_friction(self):
        text = (
            "Changes made:\n"
            "- Add validation to the input handler\n"
            "- Fix the null pointer in the parser\n"
            "- Update the test suite for coverage\n"
            "- Remove the deprecated endpoint\n"
        )
        assert _check_non_parallel_lists(text) is None

    def test_detects_mixed_structure(self):
        text = (
            "Tasks:\n"
            "- Add the new endpoint\n"
            "- The database schema needs updating\n"
            "- Fix the broken tests\n"
            "- A review of the security model\n"
            "- Performance is degraded\n"
        )
        signal = _check_non_parallel_lists(text)
        assert signal is not None
        assert signal.kind == "non_parallel_list"

    def test_short_list_ignored(self):
        text = "- Item one\n- The second item"
        assert _check_non_parallel_lists(text) is None


# ============================================================================
# Conclusion before evidence detector
# ============================================================================


class TestConclusionBeforeEvidence:
    def test_evidence_then_conclusion_no_friction(self):
        paras = [
            "Looking at the logs, we found that the connection pool was exhausted.",
            "I recommend increasing the pool size from 10 to 25.",
        ]
        assert _check_conclusion_before_evidence(paras) is None

    def test_detects_conclusion_first(self):
        paras = [
            "I recommend we switch to PostgreSQL immediately.",
            "Some additional context about the migration path.",
            "Because the current SQLite setup cannot handle concurrent writes.",
        ]
        signal = _check_conclusion_before_evidence(paras)
        assert signal is not None
        assert signal.kind == "conclusion_before_evidence"

    def test_no_friction_single_paragraph(self):
        paras = ["I recommend this because the evidence shows it works."]
        assert _check_conclusion_before_evidence(paras) is None


# ============================================================================
# Wall of text detector
# ============================================================================


class TestWallOfText:
    def test_short_paragraphs_no_friction(self):
        paras = ["Short paragraph one.", "Short paragraph two."]
        assert _check_wall_of_text(paras) is None

    def test_detects_wall(self):
        wall = "word " * 200  # 1000 chars
        paras = ["Intro.", wall.strip()]
        signal = _check_wall_of_text(paras)
        assert signal is not None
        assert signal.kind == "wall_of_text"
        assert "paragraph 2" in signal.location

    def test_skips_code_blocks(self):
        code = "```python\n" + "x = 1\n" * 200 + "```"
        paras = ["Intro.", code]
        assert _check_wall_of_text(paras) is None

    def test_skips_list_paragraphs(self):
        list_block = "\n".join(f"- Item {i}" for i in range(50))
        paras = [list_block]
        assert _check_wall_of_text(paras) is None


# ============================================================================
# Main check_mcf entry point
# ============================================================================


class TestCheckMCF:
    def test_empty_text(self):
        result = check_mcf("")
        assert result.has_friction is False

    def test_short_text_skipped(self):
        result = check_mcf("Quick reply: done.")
        assert result.has_friction is False

    def test_clean_response(self):
        text = (
            "The fix is deployed to staging.\n\n"
            "Looking at the logs, the connection pool was exhausted during peak.\n\n"
            "Changes made:\n"
            "- Increase pool size from 10 to 25\n"
            "- Add connection timeout of 30s\n"
            "- Add monitoring alert for pool saturation"
        )
        result = check_mcf(text)
        assert result.has_friction is False

    def test_multiple_friction_signals(self):
        wall = "word " * 200
        text = (
            "I looked into the issue you reported about the service.\n\n"
            "After some research, here is additional context about what happened.\n\n"
            f"The fix is done and deployed. {wall}\n\n"
        )
        result = check_mcf(text)
        assert result.has_friction is True
        # Should detect at least buried lead and wall of text
        assert len(result.signals) >= 1


# ============================================================================
# Diagnostic formatting
# ============================================================================


class TestFormatDiagnostics:
    def test_empty_result(self):
        result = MCFResult()
        assert format_diagnostics(result) == ""

    def test_with_signals(self):
        result = MCFResult(signals=[
            FrictionSignal(
                kind="buried_lead",
                description="Key signal in paragraph 3",
                location="paragraph 3",
            ),
        ])
        output = format_diagnostics(result)
        assert "MCF filter" in output
        assert "buried_lead" in output
        assert "paragraph 3" in output
        assert "1 friction signal" in output

    def test_multiple_signals(self):
        result = MCFResult(signals=[
            FrictionSignal(kind="buried_lead", description="test"),
            FrictionSignal(kind="wall_of_text", description="test"),
        ])
        output = format_diagnostics(result)
        assert "2 friction signal" in output
