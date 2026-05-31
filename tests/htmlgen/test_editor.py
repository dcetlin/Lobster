"""
tests/htmlgen/test_editor.py

Tests for src/htmlgen/editor.py — DOM-aware HTML editor (Phase 3).

Tests are named after behaviors (GP-3: abstract edit instructions, deterministic execution)
and verify the contract between LLM-expressed intent and deterministic DOM mutation.

Named constants for spec-mandated values:
    SECTION_CLASS = "section"  — the class all sections carry
    COMMENT_BTN_HTML = the emoji button pattern in headings
    TRACE_LOG_PATH = ~/lobster-workspace/data/html-edit-traces.jsonl
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from src.htmlgen.editor import (
    EditError,
    EditTrace,
    TRACE_LOG_PATH,
    apply_edit,
    apply_edits,
    find_sections_by_content,
    list_section_ids,
    parse_html,
)

# ---------------------------------------------------------------------------
# Named constants — spec-mandated values
# ---------------------------------------------------------------------------

SECTION_CLASS = "section"
COMMENT_BTN_FRAGMENT = 'class="comment-btn"'


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _minimal_html(
    sections: list[dict] | None = None,
    version: str = "1.0",
    updated: str = "2026-05-30T00:00:00Z",
) -> str:
    """Build a minimal well-formed HTML document for testing.

    Each section dict may have: id, label, title, content.
    """
    if sections is None:
        sections = [
            {"id": "s1", "label": "§1", "title": "Introduction", "content": "<p>Original content.</p>"},
            {"id": "s2", "label": "§2", "title": "Background", "content": "<p>Background content.</p>"},
        ]

    section_divs = ""
    for s in sections:
        sid = s.get("id", "s1")
        label = s.get("label", "")
        title = s.get("title", "")
        content = s.get("content", "")
        label_html = f'<div class="section-label">{label}</div>' if label else ""
        title_html = (
            f'<h2>{title} <button class="comment-btn" onclick="toggleComment(this)">&#128172;</button></h2>'
            if title else ""
        )
        comment_area_html = (
            f'<div class="comment-area"><textarea placeholder="[{label or sid}] "></textarea></div>'
            if title else ""
        )
        section_divs += (
            f'<div class="{SECTION_CLASS}" id="{sid}">'
            f"{label_html}{title_html}{comment_area_html}{content}"
            f"</div>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="doc-version" content="{version}">
  <meta name="doc-updated" content="{updated}">
  <title>Test Document</title>
</head>
<body>
  <div class="wrap">
    {section_divs}
    <footer class="doc-footer">
      <span>doc-id: test-doc</span>
    </footer>
  </div>
  <script data-block-id="main-init">// init code here</script>
</body>
</html>"""


@pytest.fixture
def html_file(tmp_path: Path) -> Path:
    """A minimal HTML file with two sections."""
    p = tmp_path / "test.html"
    p.write_text(_minimal_html(), encoding="utf-8")
    return p


@pytest.fixture
def html_file_single_section(tmp_path: Path) -> Path:
    """A minimal HTML file with one section."""
    p = tmp_path / "single.html"
    p.write_text(
        _minimal_html(sections=[
            {"id": "s1", "label": "§1", "title": "Only Section", "content": "<p>Only content.</p>"},
        ]),
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# replace_section_content tests
# ---------------------------------------------------------------------------


class TestReplaceSectionContent:
    """replace_section_content replaces body while preserving structural elements."""

    def test_new_content_appears_in_section(self, html_file: Path):
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Replaced content.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s1 = soup.find("div", {"class": "section", "id": "s1"})
        assert "Replaced content." in str(s1)

    def test_old_content_removed_from_section(self, html_file: Path):
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Replaced.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s1 = soup.find("div", {"class": "section", "id": "s1"})
        assert "Original content." not in str(s1)

    def test_section_label_preserved_after_replace(self, html_file: Path):
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>New body.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s1 = soup.find("div", {"class": "section", "id": "s1"})
        assert s1.find("div", class_="section-label") is not None

    def test_section_heading_preserved_after_replace(self, html_file: Path):
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>New body.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s1 = soup.find("div", {"class": "section", "id": "s1"})
        assert s1.find("h2") is not None

    def test_comment_area_preserved_after_replace(self, html_file: Path):
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>New body.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s1 = soup.find("div", {"class": "section", "id": "s1"})
        assert s1.find("div", class_="comment-area") is not None

    def test_other_sections_untouched_after_replace(self, html_file: Path):
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Changed.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s2 = soup.find("div", {"class": "section", "id": "s2"})
        assert "Background content." in str(s2)

    def test_plain_text_content_wrapped_in_paragraph(self, html_file: Path):
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "Just plain text.",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s1 = soup.find("div", {"class": "section", "id": "s1"})
        assert s1.find("p") is not None
        assert "Just plain text." in str(s1)

    def test_nonexistent_section_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError, match="s99"):
            apply_edit(html_file, {
                "op": "replace_section_content",
                "section_id": "s99",
                "new_content": "<p>x</p>",
            })

    def test_section_id_preserved_after_replace(self, html_file: Path):
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>New.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        assert soup.find("div", {"class": "section", "id": "s1"}) is not None


# ---------------------------------------------------------------------------
# add_section tests
# ---------------------------------------------------------------------------


class TestAddSection:
    """add_section inserts a new section after a named existing section."""

    def test_new_section_present_after_add(self, html_file: Path):
        apply_edit(html_file, {
            "op": "add_section",
            "section_id": "s3",
            "after_id": "s2",
            "title": "Conclusion",
            "label": "§3",
            "content": "<p>Concluding remarks.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        assert soup.find("div", {"class": "section", "id": "s3"}) is not None

    def test_new_section_appears_after_target(self, html_file: Path):
        apply_edit(html_file, {
            "op": "add_section",
            "section_id": "s3",
            "after_id": "s2",
            "title": "Conclusion",
            "label": "§3",
            "content": "<p>Content.</p>",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        sections = soup.find_all("div", class_="section")
        ids = [s.get("id") for s in sections]
        assert ids.index("s3") > ids.index("s2")

    def test_new_section_has_correct_title(self, html_file: Path):
        apply_edit(html_file, {
            "op": "add_section",
            "section_id": "s3",
            "after_id": "s2",
            "title": "My New Title",
            "label": "§3",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s3 = soup.find("div", {"class": "section", "id": "s3"})
        assert "My New Title" in str(s3)

    def test_new_section_has_comment_button(self, html_file: Path):
        apply_edit(html_file, {
            "op": "add_section",
            "section_id": "s3",
            "after_id": "s2",
            "title": "Section With Button",
            "label": "§3",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s3 = soup.find("div", {"class": "section", "id": "s3"})
        assert s3.find("button", class_="comment-btn") is not None

    def test_duplicate_section_id_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError, match="already exists"):
            apply_edit(html_file, {
                "op": "add_section",
                "section_id": "s1",  # already exists
                "after_id": "s2",
                "title": "Duplicate",
                "label": "§X",
            })

    def test_nonexistent_after_id_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError, match="s99"):
            apply_edit(html_file, {
                "op": "add_section",
                "section_id": "s3",
                "after_id": "s99",  # doesn't exist
                "title": "New",
                "label": "§3",
            })

    def test_existing_sections_untouched_after_add(self, html_file: Path):
        apply_edit(html_file, {
            "op": "add_section",
            "section_id": "s3",
            "after_id": "s2",
            "title": "Extra",
            "label": "§3",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        assert soup.find("div", {"class": "section", "id": "s1"}) is not None
        assert soup.find("div", {"class": "section", "id": "s2"}) is not None


# ---------------------------------------------------------------------------
# remove_section tests
# ---------------------------------------------------------------------------


class TestRemoveSection:
    """remove_section removes a section element entirely from the document."""

    def test_removed_section_absent_from_document(self, html_file: Path):
        apply_edit(html_file, {
            "op": "remove_section",
            "section_id": "s2",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        assert soup.find("div", {"class": "section", "id": "s2"}) is None

    def test_remaining_sections_intact_after_remove(self, html_file: Path):
        apply_edit(html_file, {
            "op": "remove_section",
            "section_id": "s2",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        assert soup.find("div", {"class": "section", "id": "s1"}) is not None

    def test_nonexistent_section_remove_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError, match="s99"):
            apply_edit(html_file, {
                "op": "remove_section",
                "section_id": "s99",
            })


# ---------------------------------------------------------------------------
# update_version_stamp tests
# ---------------------------------------------------------------------------


class TestUpdateVersionStamp:
    """update_version_stamp updates doc-version and doc-updated meta tags."""

    def test_version_updated_in_meta_tag(self, html_file: Path):
        apply_edit(html_file, {
            "op": "update_version_stamp",
            "version": "2.0",
            "timestamp": "2026-06-01T12:00:00Z",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        meta = soup.find("meta", {"name": "doc-version"})
        assert meta["content"] == "2.0"

    def test_timestamp_updated_in_meta_tag(self, html_file: Path):
        apply_edit(html_file, {
            "op": "update_version_stamp",
            "version": "2.0",
            "timestamp": "2026-06-01T12:00:00Z",
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        meta = soup.find("meta", {"name": "doc-updated"})
        assert meta["content"] == "2026-06-01T12:00:00Z"

    def test_version_only_update_leaves_timestamp_unchanged(self, html_file: Path):
        original_ts = "2026-05-30T00:00:00Z"
        apply_edit(html_file, {
            "op": "update_version_stamp",
            "version": "1.5",
            # timestamp omitted
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        meta_ts = soup.find("meta", {"name": "doc-updated"})
        assert meta_ts["content"] == original_ts

    def test_timestamp_only_update_leaves_version_unchanged(self, html_file: Path):
        apply_edit(html_file, {
            "op": "update_version_stamp",
            "timestamp": "2026-07-01T00:00:00Z",
            # version omitted
        })
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        meta_v = soup.find("meta", {"name": "doc-version"})
        assert meta_v["content"] == "1.0"

    def test_missing_doc_version_tag_raises_edit_error(self, tmp_path: Path):
        html = _minimal_html()
        html = html.replace('<meta name="doc-version" content="1.0">', "")
        p = tmp_path / "no-version.html"
        p.write_text(html, encoding="utf-8")
        with pytest.raises(EditError, match="doc-version"):
            apply_edit(p, {
                "op": "update_version_stamp",
                "version": "2.0",
            })


# ---------------------------------------------------------------------------
# patch_js_block tests
# ---------------------------------------------------------------------------


class TestPatchJsBlock:
    """patch_js_block replaces the content of a named script block."""

    def test_js_block_content_replaced(self, html_file: Path):
        apply_edit(html_file, {
            "op": "patch_js_block",
            "block_id": "main-init",
            "new_code": "console.log('replaced');",
        })
        text = html_file.read_text()
        assert "console.log('replaced');" in text
        assert "// init code here" not in text

    def test_nonexistent_block_id_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError, match="no-such-block"):
            apply_edit(html_file, {
                "op": "patch_js_block",
                "block_id": "no-such-block",
                "new_code": "// irrelevant",
            })

    def test_other_script_blocks_untouched(self, tmp_path: Path):
        html = (
            _minimal_html()
            + '<script data-block-id="other-block">// other</script>'
        )
        p = tmp_path / "two-scripts.html"
        p.write_text(html, encoding="utf-8")
        apply_edit(p, {
            "op": "patch_js_block",
            "block_id": "main-init",
            "new_code": "// changed",
        })
        text = p.read_text()
        assert "// other" in text


# ---------------------------------------------------------------------------
# Batch operations (apply_edits)
# ---------------------------------------------------------------------------


class TestBatchEdits:
    """apply_edits applies multiple instructions in order."""

    def test_batch_applies_all_edits(self, html_file: Path):
        apply_edits(html_file, [
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>New s1.</p>"},
            {"op": "update_version_stamp", "version": "1.1", "timestamp": "2026-06-01T00:00:00Z"},
        ])
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s1 = soup.find("div", {"class": "section", "id": "s1"})
        assert "New s1." in str(s1)
        meta = soup.find("meta", {"name": "doc-version"})
        assert meta["content"] == "1.1"

    def test_batch_order_respected(self, html_file: Path):
        # Two replaces of the same section — last one wins
        apply_edits(html_file, [
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>First.</p>"},
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>Second.</p>"},
        ])
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        s1 = soup.find("div", {"class": "section", "id": "s1"})
        assert "Second." in str(s1)
        assert "First." not in str(s1)

    def test_batch_stops_on_first_error(self, html_file: Path):
        """If instruction 1 fails, instruction 2 must not be applied."""
        with pytest.raises(EditError):
            apply_edits(html_file, [
                {"op": "replace_section_content", "section_id": "s99",  # doesn't exist
                 "new_content": "<p>x</p>"},
                {"op": "update_version_stamp", "version": "9.9"},
            ])
        # Version must still be 1.0 — second instruction didn't apply
        soup = BeautifulSoup(html_file.read_text(), "html.parser")
        meta = soup.find("meta", {"name": "doc-version"})
        assert meta["content"] == "1.0"

    def test_file_not_written_on_error(self, html_file: Path):
        """A failed batch must not corrupt the file."""
        original = html_file.read_text()
        with pytest.raises(EditError):
            apply_edits(html_file, [
                {"op": "replace_section_content", "section_id": "s99",
                 "new_content": "<p>x</p>"},
            ])
        # File content must be unchanged
        assert html_file.read_text() == original


# ---------------------------------------------------------------------------
# Utility API tests
# ---------------------------------------------------------------------------


class TestListSectionIds:
    """list_section_ids returns section IDs in document order."""

    def test_returns_all_section_ids_in_order(self, html_file: Path):
        ids = list_section_ids(html_file)
        assert ids == ["s1", "s2"]

    def test_empty_when_no_sections(self, tmp_path: Path):
        p = tmp_path / "no-sections.html"
        p.write_text("<html><body><p>No sections.</p></body></html>", encoding="utf-8")
        assert list_section_ids(p) == []

    def test_reflects_removal(self, html_file: Path):
        apply_edit(html_file, {"op": "remove_section", "section_id": "s2"})
        assert list_section_ids(html_file) == ["s1"]

    def test_reflects_addition(self, html_file: Path):
        apply_edit(html_file, {
            "op": "add_section",
            "section_id": "s3",
            "after_id": "s2",
            "title": "New",
            "label": "§3",
        })
        ids = list_section_ids(html_file)
        assert "s3" in ids
        assert ids.index("s3") > ids.index("s2")

    def test_raises_for_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            list_section_ids(tmp_path / "does-not-exist.html")


class TestParseHtml:
    """parse_html returns a BeautifulSoup tree for inspection."""

    def test_returns_beautifulsoup_instance(self, html_file: Path):
        soup = parse_html(html_file)
        assert isinstance(soup, BeautifulSoup)

    def test_can_query_sections_from_parsed_tree(self, html_file: Path):
        soup = parse_html(html_file)
        sections = soup.find_all("div", class_="section")
        assert len(sections) == 2

    def test_raises_for_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            parse_html(tmp_path / "does-not-exist.html")


# ---------------------------------------------------------------------------
# Error handling — unknown op
# ---------------------------------------------------------------------------


class TestUnknownOp:
    def test_unknown_op_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError, match="Unknown op"):
            apply_edit(html_file, {"op": "teleport_section", "section_id": "s1"})

    def test_missing_op_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError, match="missing required 'op'"):
            apply_edit(html_file, {"section_id": "s1", "new_content": "<p>x</p>"})

    def test_missing_required_field_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError):
            apply_edit(html_file, {"op": "replace_section_content", "section_id": "s1"})
            # missing new_content


# ---------------------------------------------------------------------------
# find_sections_by_content tests
# ---------------------------------------------------------------------------


class TestFindSectionsByContent:
    """find_sections_by_content locates sections by text or CSS without modifying the doc."""

    def test_text_mode_returns_section_id_for_matching_content(self, html_file: Path):
        ids = find_sections_by_content(html_file, "Original content")
        assert "s1" in ids

    def test_text_mode_match_is_case_insensitive(self, html_file: Path):
        ids = find_sections_by_content(html_file, "ORIGINAL CONTENT")
        assert "s1" in ids

    def test_text_mode_returns_empty_list_when_no_match(self, html_file: Path):
        ids = find_sections_by_content(html_file, "xyzzy-no-such-text")
        assert ids == []

    def test_text_mode_returns_only_matching_sections(self, html_file: Path):
        # "Background content" is in s2 only
        ids = find_sections_by_content(html_file, "Background content")
        assert "s2" in ids
        assert "s1" not in ids

    def test_text_mode_returns_multiple_sections_when_pattern_matches_several(
        self, tmp_path: Path
    ):
        p = tmp_path / "multi.html"
        p.write_text(
            _minimal_html(sections=[
                {"id": "s1", "label": "§1", "title": "Alpha", "content": "<p>shared keyword here</p>"},
                {"id": "s2", "label": "§2", "title": "Beta", "content": "<p>shared keyword here too</p>"},
                {"id": "s3", "label": "§3", "title": "Gamma", "content": "<p>no match</p>"},
            ]),
            encoding="utf-8",
        )
        ids = find_sections_by_content(p, "shared keyword")
        assert "s1" in ids
        assert "s2" in ids
        assert "s3" not in ids

    def test_css_mode_returns_section_containing_matching_element(self, html_file: Path):
        # Both sections contain <h2> elements
        ids = find_sections_by_content(html_file, "h2", mode="css")
        assert "s1" in ids
        assert "s2" in ids

    def test_css_mode_returns_empty_list_when_selector_matches_nothing(self, html_file: Path):
        ids = find_sections_by_content(html_file, "div.nonexistent-class", mode="css")
        assert ids == []

    def test_document_with_no_sections_returns_empty_list(self, tmp_path: Path):
        p = tmp_path / "no-sections.html"
        p.write_text("<html><body><p>No sections here.</p></body></html>", encoding="utf-8")
        assert find_sections_by_content(p, "No sections") == []

    def test_does_not_modify_document(self, html_file: Path):
        original = html_file.read_text()
        find_sections_by_content(html_file, "Original content")
        assert html_file.read_text() == original

    def test_returns_ids_in_document_order(self, tmp_path: Path):
        p = tmp_path / "ordered.html"
        p.write_text(
            _minimal_html(sections=[
                {"id": "s1", "label": "§1", "title": "A", "content": "<p>match</p>"},
                {"id": "s2", "label": "§2", "title": "B", "content": "<p>match</p>"},
                {"id": "s3", "label": "§3", "title": "C", "content": "<p>match</p>"},
            ]),
            encoding="utf-8",
        )
        ids = find_sections_by_content(p, "match")
        assert ids == ["s1", "s2", "s3"]


# ---------------------------------------------------------------------------
# EditTrace dataclass tests
# ---------------------------------------------------------------------------


class TestEditTraceOnSuccessfulEdit:
    """EditTrace fields are populated correctly after a successful single-section edit."""

    def test_edit_trace_returned_from_apply_edit(self, html_file: Path):
        result, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        assert isinstance(trace, EditTrace)

    def test_edit_trace_doc_path_matches_input(self, html_file: Path):
        _, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        assert trace.doc_path == str(html_file)

    def test_edit_trace_sections_total_reflects_document(self, html_file: Path):
        _, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        # html_file has 2 sections
        assert trace.sections_total == 2

    def test_edit_trace_sections_accessed_contains_targeted_section(self, html_file: Path):
        _, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        assert "s1" in trace.sections_accessed

    def test_edit_trace_operations_attempted_matches_instruction_count(self, html_file: Path):
        _, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        assert trace.operations_attempted == 1

    def test_edit_trace_operations_succeeded_matches_on_success(self, html_file: Path):
        _, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        assert trace.operations_succeeded == 1

    def test_edit_trace_write_succeeded_true_on_success(self, html_file: Path):
        _, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        assert trace.write_succeeded is True

    def test_edit_trace_operation_types_contains_op_name(self, html_file: Path):
        _, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        assert "replace_section_content" in trace.operation_types

    def test_edit_trace_doc_size_bytes_positive(self, html_file: Path):
        _, trace = apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Updated.</p>",
        })
        assert trace.doc_size_bytes > 0


class TestEditTraceOnBatchEdit:
    """EditTrace fields are correct after a batch operation touching multiple sections."""

    def test_batch_trace_sections_accessed_contains_all_targeted_sections(
        self, html_file: Path
    ):
        _, trace = apply_edits(html_file, [
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>A.</p>"},
            {"op": "replace_section_content", "section_id": "s2", "new_content": "<p>B.</p>"},
        ])
        assert "s1" in trace.sections_accessed
        assert "s2" in trace.sections_accessed

    def test_batch_trace_operations_attempted_matches_instruction_count(
        self, html_file: Path
    ):
        _, trace = apply_edits(html_file, [
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>A.</p>"},
            {"op": "update_version_stamp", "version": "1.1"},
        ])
        assert trace.operations_attempted == 2

    def test_batch_trace_operation_types_contains_all_op_names(self, html_file: Path):
        _, trace = apply_edits(html_file, [
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>A.</p>"},
            {"op": "update_version_stamp", "version": "1.1"},
        ])
        assert "replace_section_content" in trace.operation_types
        assert "update_version_stamp" in trace.operation_types

    def test_sections_accessed_deduplicated_across_repeated_ops_on_same_section(
        self, html_file: Path
    ):
        # Two replace ops on the same section must not produce ["s1", "s1"]
        _, trace = apply_edits(html_file, [
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>First.</p>"},
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>Second.</p>"},
        ])
        assert trace.sections_accessed.count("s1") == 1


class TestEditTraceOnFailedEdit:
    """EditTrace.write_succeeded is False when an edit errors out."""

    def test_failed_edit_raises_edit_error(self, html_file: Path):
        with pytest.raises(EditError):
            apply_edit(html_file, {
                "op": "replace_section_content",
                "section_id": "s99",  # doesn't exist
                "new_content": "<p>x</p>",
            })

    def test_apply_edits_still_returns_html_string_as_first_element(
        self, html_file: Path
    ):
        result, trace = apply_edits(html_file, [
            {"op": "replace_section_content", "section_id": "s1", "new_content": "<p>OK.</p>"},
        ])
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# JSONL trace log tests
# ---------------------------------------------------------------------------


class TestJsonlTraceLog:
    """After every apply_edit / apply_edits call, a trace is appended to the JSONL log."""

    def test_jsonl_file_created_after_edit(self, html_file: Path, tmp_path: Path, monkeypatch):
        log_path = tmp_path / "traces.jsonl"
        monkeypatch.setattr("src.htmlgen.editor.TRACE_LOG_PATH", log_path)
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Traced.</p>",
        })
        assert log_path.exists()

    def test_jsonl_entry_is_valid_json(self, html_file: Path, tmp_path: Path, monkeypatch):
        log_path = tmp_path / "traces.jsonl"
        monkeypatch.setattr("src.htmlgen.editor.TRACE_LOG_PATH", log_path)
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Traced.</p>",
        })
        line = log_path.read_text().strip()
        entry = json.loads(line)
        assert isinstance(entry, dict)

    def test_jsonl_entry_contains_timestamp(self, html_file: Path, tmp_path: Path, monkeypatch):
        log_path = tmp_path / "traces.jsonl"
        monkeypatch.setattr("src.htmlgen.editor.TRACE_LOG_PATH", log_path)
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Traced.</p>",
        })
        entry = json.loads(log_path.read_text().strip())
        assert "timestamp" in entry

    def test_jsonl_entry_contains_trace_fields(self, html_file: Path, tmp_path: Path, monkeypatch):
        log_path = tmp_path / "traces.jsonl"
        monkeypatch.setattr("src.htmlgen.editor.TRACE_LOG_PATH", log_path)
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>Traced.</p>",
        })
        entry = json.loads(log_path.read_text().strip())
        assert "doc_path" in entry
        assert "write_succeeded" in entry
        assert "operations_attempted" in entry

    def test_jsonl_appends_on_subsequent_calls(self, html_file: Path, tmp_path: Path, monkeypatch):
        log_path = tmp_path / "traces.jsonl"
        monkeypatch.setattr("src.htmlgen.editor.TRACE_LOG_PATH", log_path)
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s1",
            "new_content": "<p>First.</p>",
        })
        apply_edit(html_file, {
            "op": "replace_section_content",
            "section_id": "s2",
            "new_content": "<p>Second.</p>",
        })
        lines = [l for l in log_path.read_text().strip().splitlines() if l.strip()]
        assert len(lines) == 2
