"""
tests/html/test_renderer.py

Integration tests for src/htmlgen/renderer.py — full compilation pipeline.

Run with: uv run pytest tests/html/test_renderer.py -v

Notes:
  - Do NOT use bs4/beautifulsoup4 — conftest.py adds src/ to sys.path which
    shadows stdlib html and breaks bs4's html.entities import.
  - Use stdlib regex/string checks only for HTML validation.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.htmlgen.renderer import (
    ValidationError,
    load_content_manifest,
    render,
    render_and_upload,
    validate_html,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_MANIFEST = {
    "doc_id": "test-doc",
    "title": "Test Document",
    "subtitle": "A minimal test",
    "version": "1.0",
    "updated_at": "2026-05-30T00:00:00Z",
    "template_id": "document-class",
    "sections": [
        {
            "id": "s1",
            "label": "§1",
            "title": "Introduction",
            "content": "This is section one content.",
        },
        {
            "id": "s2",
            "label": "§2",
            "title": "Details",
            "content": "This is section two content.",
        },
    ],
    "components": [],
}


@pytest.fixture
def manifest_file(tmp_path: Path) -> Path:
    """Write a minimal manifest to a temp file and return its path."""
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(MINIMAL_MANIFEST), encoding="utf-8")
    return p


@pytest.fixture
def output_file(tmp_path: Path) -> Path:
    """Return a temp path for rendered HTML output."""
    return tmp_path / "output.html"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _has_pattern(html: str, pattern: str, flags: int = re.IGNORECASE) -> bool:
    return bool(re.search(pattern, html, flags))


def _count_pattern(html: str, pattern: str, flags: int = re.IGNORECASE) -> int:
    return len(re.findall(pattern, html, flags))


# ---------------------------------------------------------------------------
# load_content_manifest tests
# ---------------------------------------------------------------------------


class TestLoadContentManifest:
    def test_loads_valid_manifest(self, manifest_file: Path):
        data = load_content_manifest(manifest_file)
        assert isinstance(data, dict)
        assert data["doc_id"] == "test-doc"

    def test_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_content_manifest(tmp_path / "nonexistent.json")

    def test_raises_value_error_on_invalid_json(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json }", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_content_manifest(bad)

    def test_raises_value_error_missing_required_field(self, tmp_path: Path):
        missing_title = {k: v for k, v in MINIMAL_MANIFEST.items() if k != "title"}
        p = tmp_path / "missing.json"
        p.write_text(json.dumps(missing_title), encoding="utf-8")
        with pytest.raises(ValueError, match="title"):
            load_content_manifest(p)

    def test_raises_value_error_sections_not_list(self, tmp_path: Path):
        bad = dict(MINIMAL_MANIFEST)
        bad["sections"] = "not a list"
        p = tmp_path / "bad_sections.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError, match="sections"):
            load_content_manifest(p)

    def test_returns_all_required_fields(self, manifest_file: Path):
        data = load_content_manifest(manifest_file)
        for field in ["doc_id", "title", "version", "template_id", "sections"]:
            assert field in data


# ---------------------------------------------------------------------------
# validate_html tests
# ---------------------------------------------------------------------------


class TestValidateHtml:
    def test_passes_valid_html(self):
        html = (
            '<meta name="doc-version" content="1.0">'
            '<div id="s1">Section 1</div>'
        )
        manifest = {"sections": [{"id": "s1"}]}
        # Should not raise
        validate_html(html, manifest)

    def test_catches_missing_doc_version_meta(self):
        html = '<div id="s1">No meta tag here</div>'
        manifest = {"sections": [{"id": "s1"}]}
        with pytest.raises(ValidationError, match="doc-version"):
            validate_html(html, manifest)

    def test_catches_missing_section_id(self):
        html = '<meta name="doc-version" content="1.0"><div id="s1">ok</div>'
        manifest = {"sections": [{"id": "s1"}, {"id": "s99"}]}
        with pytest.raises(ValidationError, match="s99"):
            validate_html(html, manifest)

    def test_catches_unresolved_placeholder(self):
        html = (
            '<meta name="doc-version" content="1.0">'
            '<div id="s1">{{placeholder}}</div>'
        )
        manifest = {"sections": [{"id": "s1"}]}
        with pytest.raises(ValidationError, match="placeholder"):
            validate_html(html, manifest)

    def test_catches_mismatched_script_tags(self):
        html = (
            '<meta name="doc-version" content="1.0">'
            '<div id="s1">ok</div>'
            '<script>var x = 1;</script>'
            '<script>var y = 2;'  # unclosed
        )
        manifest = {"sections": [{"id": "s1"}]}
        with pytest.raises(ValidationError, match="script"):
            validate_html(html, manifest)

    def test_empty_sections_list_passes(self):
        html = '<meta name="doc-version" content="1.0">'
        manifest = {"sections": []}
        validate_html(html, manifest)  # should not raise

    def test_multiple_errors_reported(self):
        html = "<div>no meta, no section</div>"
        manifest = {"sections": [{"id": "s1"}]}
        with pytest.raises(ValidationError) as exc_info:
            validate_html(html, manifest)
        msg = str(exc_info.value)
        assert "doc-version" in msg
        assert "s1" in msg


# ---------------------------------------------------------------------------
# render() function tests
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_returns_nonempty_string(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_produces_output_file(self, manifest_file: Path, output_file: Path):
        render(manifest_file, "document-class", output_file)
        assert output_file.exists()
        assert output_file.stat().st_size > 0

    def test_render_output_file_matches_return_value(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        written = output_file.read_text(encoding="utf-8")
        assert result == written

    def test_render_has_doctype(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert "<!DOCTYPE html>" in result

    def test_render_has_html_tag(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert _has_pattern(result, r"<html")

    def test_render_has_head_and_body(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert _has_pattern(result, r"<head")
        assert _has_pattern(result, r"<body")

    def test_render_has_doc_version_meta(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert _has_pattern(result, r'<meta[^>]+name=["\']doc-version["\']')

    def test_render_version_matches_manifest(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert 'content="1.0"' in result or "content='1.0'" in result

    def test_render_has_doc_updated_meta(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert _has_pattern(result, r'<meta[^>]+name=["\']doc-updated["\']')

    def test_render_has_section_ids(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert 'id="s1"' in result
        assert 'id="s2"' in result

    def test_render_has_section_titles(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert "Introduction" in result
        assert "Details" in result

    def test_render_has_section_content(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert "section one content" in result
        assert "section two content" in result

    def test_render_conventions_color_tokens_in_css(self, manifest_file: Path, output_file: Path):
        """CSS custom properties from conventions.yaml must appear in the output."""
        result = render(manifest_file, "document-class", output_file)
        # Dark mode tokens from conventions.yaml
        assert "--bg:" in result or "--bg :" in result
        assert "--accent:" in result or "--accent :" in result
        assert "#0b0d14" in result  # dark bg hex value from conventions.yaml

    def test_render_has_theme_toggle_component(self, manifest_file: Path, output_file: Path):
        """document-class template requires theme-toggle component."""
        result = render(manifest_file, "document-class", output_file)
        assert 'id="theme-toggle"' in result

    def test_render_section_comments_widget_absent(self, manifest_file: Path, output_file: Path):
        """Section Comments widget (clipboard-copy-widget) must NOT appear in document-class output.

        Dan confirmed this panel is redundant — inline comment buttons and the
        'Copy all comments' button already provide this functionality.
        """
        result = render(manifest_file, "document-class", output_file)
        assert 'id="comment-widget"' not in result
        assert "Section Comments" not in result
        assert "comment-widget-title" not in result

    def test_render_title_in_output(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert "Test Document" in result

    def test_render_subtitle_in_output(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        assert "A minimal test" in result

    def test_render_lobster_components_meta(self, manifest_file: Path, output_file: Path):
        """lobster-components meta tag should list component IDs and versions."""
        result = render(manifest_file, "document-class", output_file)
        assert _has_pattern(result, r'<meta[^>]+name=["\']lobster-components["\']')
        assert "@1.0.0" in result  # at least one component version present

    def test_render_raises_file_not_found_for_bad_content_path(
        self, output_file: Path
    ):
        with pytest.raises(FileNotFoundError):
            render(Path("/nonexistent/manifest.json"), "document-class", output_file)

    def test_render_raises_key_error_for_unknown_template(
        self, manifest_file: Path, output_file: Path
    ):
        with pytest.raises(KeyError):
            render(manifest_file, "nonexistent-template", output_file)

    def test_render_spec_document_template(self, tmp_path: Path):
        """spec-document template must also render without errors."""
        manifest = dict(MINIMAL_MANIFEST)
        manifest["template_id"] = "spec-document"
        p = tmp_path / "spec.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        out = tmp_path / "spec.html"
        result = render(p, "spec-document", out)
        assert "<!DOCTYPE html>" in result
        assert 'id="s1"' in result

    def test_render_creates_output_directory_if_missing(
        self, manifest_file: Path, tmp_path: Path
    ):
        """render() must create the output directory if it doesn't exist."""
        nested_output = tmp_path / "new_dir" / "subdir" / "output.html"
        render(manifest_file, "document-class", nested_output)
        assert nested_output.exists()

    def test_render_no_unclosed_script_tags(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        open_count = _count_pattern(result, r"<script[\s>]")
        close_count = _count_pattern(result, r"</script>")
        assert open_count == close_count, (
            f"Mismatched script tags: {open_count} open, {close_count} close"
        )

    def test_render_no_unresolved_placeholders(self, manifest_file: Path, output_file: Path):
        result = render(manifest_file, "document-class", output_file)
        placeholders = re.findall(r"\{\{[^}]+\}\}", result)
        assert not placeholders, f"Unresolved placeholders: {placeholders}"

    def test_render_css_contains_primitives_version_comment(
        self, manifest_file: Path, output_file: Path
    ):
        """CSS output must include /* lobster-html-primitives vX.Y */ version stamp."""
        result = render(manifest_file, "document-class", output_file)
        assert re.search(r"/\* lobster-html-primitives v[\d.]+ \*/", result), (
            "Expected CSS version comment '/* lobster-html-primitives vX.Y */' in rendered HTML"
        )

    def test_render_css_version_comment_matches_conventions_version(
        self, manifest_file: Path, output_file: Path
    ):
        """The CSS version stamp must match conventions.yaml conventions_version."""
        from src.htmlgen.conventions import get_conventions_version
        result = render(manifest_file, "document-class", output_file)
        version = get_conventions_version()
        expected_comment = f"/* lobster-html-primitives v{version} */"
        assert expected_comment in result, (
            f"Expected '{expected_comment}' in rendered HTML, "
            f"but it was absent. conventions_version={version!r}"
        )

    def test_render_css_version_comment_is_in_root_block(
        self, manifest_file: Path, output_file: Path
    ):
        """The version comment must appear in (or immediately before) the :root block."""
        result = render(manifest_file, "document-class", output_file)
        # Find the :root block and the comment — the comment should precede :root {
        root_pos = result.find(":root {")
        comment_pos = result.find("/* lobster-html-primitives v")
        assert comment_pos != -1, "Version comment not found"
        assert root_pos != -1, ":root block not found"
        # Comment must appear before the :root { open-brace (within 120 chars)
        assert comment_pos < root_pos, "Version comment must precede :root { block"
        assert (root_pos - comment_pos) < 120, (
            "Version comment is too far from :root block"
        )


# ---------------------------------------------------------------------------
# CSS versioning tests (unit-level: _build_global_css)
# ---------------------------------------------------------------------------


class TestCssVersioning:
    """CSS primitives version stamp is embedded in the global CSS block.

    Spec from open-threads.md item 2:
      Add /* lobster-html-primitives v1 */ to the :root block.
    The version is read from conventions.yaml `conventions_version` so it
    stays in sync automatically when conventions are bumped.
    """

    CSS_PRIMITIVES_VERSION_COMMENT_PATTERN = re.compile(
        r"/\* lobster-html-primitives v[\d.]+ \*/"
    )

    def test_build_global_css_contains_primitives_comment(self):
        """_build_global_css must include the version comment."""
        from src.htmlgen.conventions import load_conventions
        from src.htmlgen.renderer import _build_global_css
        conventions = load_conventions()
        css = _build_global_css(conventions)
        assert self.CSS_PRIMITIVES_VERSION_COMMENT_PATTERN.search(css), (
            "Expected '/* lobster-html-primitives vX.Y */' in _build_global_css output"
        )

    def test_build_global_css_version_matches_conventions_version(self):
        """Version in the CSS comment must equal conventions.yaml conventions_version."""
        from src.htmlgen.conventions import get_conventions_version, load_conventions
        from src.htmlgen.renderer import _build_global_css
        conventions = load_conventions()
        css = _build_global_css(conventions)
        version = get_conventions_version()
        expected = f"/* lobster-html-primitives v{version} */"
        assert expected in css, (
            f"CSS must contain '{expected}' but got CSS starting with: {css[:200]}"
        )

    def test_build_global_css_comment_precedes_root_block(self):
        """Version comment must appear before :root { in the CSS."""
        from src.htmlgen.conventions import load_conventions
        from src.htmlgen.renderer import _build_global_css
        conventions = load_conventions()
        css = _build_global_css(conventions)
        comment_pos = css.find("/* lobster-html-primitives v")
        root_pos = css.find(":root {")
        assert comment_pos != -1, "Version comment not found in CSS"
        assert root_pos != -1, ":root block not found in CSS"
        assert comment_pos < root_pos, (
            "Version comment must appear before :root { in the CSS output"
        )

    def test_build_global_css_still_contains_color_tokens(self):
        """Adding the version comment must not break existing color token output."""
        from src.htmlgen.conventions import load_conventions
        from src.htmlgen.renderer import _build_global_css
        conventions = load_conventions()
        css = _build_global_css(conventions)
        assert "--bg:" in css
        assert "--accent:" in css
        assert "#0b0d14" in css  # dark bg from conventions.yaml

    def test_build_global_css_version_is_v1_point_0(self):
        """conventions_version is '1.0', so the comment must read v1.0."""
        from src.htmlgen.conventions import load_conventions
        from src.htmlgen.renderer import _build_global_css
        conventions = load_conventions()
        css = _build_global_css(conventions)
        assert "/* lobster-html-primitives v1.0 */" in css


class TestRenderValidationEnforcement:
    """Tests that post-render validation correctly rejects malformed output."""

    def test_validation_catches_injected_placeholder(self, tmp_path: Path):
        """Manifest content with {{placeholder}} text must fail validation."""
        manifest = dict(MINIMAL_MANIFEST)
        manifest["sections"] = [
            {
                "id": "s1",
                "label": "§1",
                "title": "Test",
                "content": "This has a {{placeholder}} in it",
            }
        ]
        p = tmp_path / "placeholder.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        out = tmp_path / "out.html"
        with pytest.raises(ValidationError, match="placeholder"):
            render(p, "document-class", out)


# ---------------------------------------------------------------------------
# render_and_upload() tests
# ---------------------------------------------------------------------------


class TestRenderAndUpload:
    def test_render_and_upload_returns_bisque_url_string(
        self, manifest_file: Path, tmp_path: Path
    ):
        # Patch the uploads dir to use tmp_path
        with patch("src.htmlgen.renderer._uploads_dir", return_value=tmp_path):
            with patch(
                "src.htmlgen.renderer._bisque_base_url",
                return_value="http://5.78.201.64:9101",
            ):
                url = render_and_upload(manifest_file, "document-class", "test-doc.html")
        assert isinstance(url, str)
        assert url.startswith("http://")
        assert "test-doc.html" in url

    def test_render_and_upload_writes_file(
        self, manifest_file: Path, tmp_path: Path
    ):
        with patch("src.htmlgen.renderer._uploads_dir", return_value=tmp_path):
            with patch(
                "src.htmlgen.renderer._bisque_base_url",
                return_value="http://5.78.201.64:9101",
            ):
                render_and_upload(manifest_file, "document-class", "test-doc.html")
        assert (tmp_path / "test-doc.html").exists()
        assert (tmp_path / "test-doc.html").stat().st_size > 0

    def test_render_and_upload_url_format(
        self, manifest_file: Path, tmp_path: Path
    ):
        with patch("src.htmlgen.renderer._uploads_dir", return_value=tmp_path):
            with patch(
                "src.htmlgen.renderer._bisque_base_url",
                return_value="http://5.78.201.64:9101",
            ):
                url = render_and_upload(manifest_file, "document-class", "my-doc.html")
        assert url == "http://5.78.201.64:9101/files/my-doc.html"


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestRendererCLI:
    def test_cli_produces_html_file(self, manifest_file: Path, tmp_path: Path):
        """CLI invocation must produce an HTML file at the specified output path."""
        output = tmp_path / "cli-output.html"
        result = subprocess.run(
            [
                sys.executable, "-m", "src.htmlgen.renderer",
                "--content", str(manifest_file),
                "--template", "document-class",
                "--output", str(output),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,  # repo root
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        assert output.exists()
        assert output.stat().st_size > 0

    def test_cli_output_file_is_valid_html(self, manifest_file: Path, tmp_path: Path):
        output = tmp_path / "cli-html.html"
        subprocess.run(
            [
                sys.executable, "-m", "src.htmlgen.renderer",
                "--content", str(manifest_file),
                "--template", "document-class",
                "--output", str(output),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
        )
        content = output.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "<html" in content

    def test_cli_missing_required_arg_fails(self):
        result = subprocess.run(
            [sys.executable, "-m", "src.htmlgen.renderer", "--content", "/tmp/x.json"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
        )
        assert result.returncode != 0

    def test_cli_nonexistent_content_path_fails(self, tmp_path: Path):
        output = tmp_path / "out.html"
        result = subprocess.run(
            [
                sys.executable, "-m", "src.htmlgen.renderer",
                "--content", "/nonexistent/manifest.json",
                "--template", "document-class",
                "--output", str(output),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
        )
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# D3 component injection tests
# ---------------------------------------------------------------------------


class TestD3ComponentInjection:
    """Tests that d3-vocabulary-network triggers D3 CDN script injection."""

    def test_d3_cdn_script_injected_when_d3_component_present(self, tmp_path: Path):
        manifest = dict(MINIMAL_MANIFEST)
        manifest["sections"] = [
            {"id": "s1", "label": "§1", "title": "Graph", "content": "See the graph."}
        ]
        manifest["components"] = [
            {
                "id": "d3-vocabulary-network",
                "config": {
                    "nodes": [
                        {
                            "id": "Concept A",
                            "tier": "register",
                            "color": "#6ea8fe",
                            "def": "A concept.",
                        }
                    ],
                    "links": [],
                },
            }
        ]
        p = tmp_path / "d3manifest.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        out = tmp_path / "d3.html"
        result = render(p, "document-class", out)
        assert "d3js.org/d3.v7.min.js" in result, (
            "D3 CDN script tag must be injected when d3-vocabulary-network is used"
        )

    def test_d3_cdn_not_injected_without_d3_component(
        self, manifest_file: Path, output_file: Path
    ):
        """document-class without d3 component must NOT include the D3 CDN tag."""
        result = render(manifest_file, "document-class", output_file)
        assert "d3js.org" not in result, (
            "D3 CDN script must not be present when d3 component is not used"
        )


# ---------------------------------------------------------------------------
# Vocab tooltip component tests
# ---------------------------------------------------------------------------


VOCAB_MANIFEST_TERMS = {
    "Stiffness": "Resistance to deformation; preserves geometry under load.",
    "Elasticity": "Deformation with return; receives perturbation without losing yourself.",
    "Damping": "Absorbs vibration; prevents irrelevant oscillation from propagating.",
}


class TestVocabTooltipComponent:
    """Tests for the vocab tooltip system (Task 3 — vocab index).

    The vocab field in the manifest drives both:
    1. Inline hover tooltips on term occurrences in section text.
    2. A collapsible vocab index panel listing all terms alphabetically with definitions.
    """

    def _make_vocab_manifest(self, tmp_path: Path) -> Path:
        manifest = dict(MINIMAL_MANIFEST)
        manifest["sections"] = [
            {
                "id": "s1",
                "label": "§1",
                "title": "Concepts",
                "content": "Stiffness is key. Elasticity enables return. Damping prevents noise.",
            }
        ]
        manifest["vocab"] = VOCAB_MANIFEST_TERMS
        p = tmp_path / "vocab-manifest.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        return p

    def test_vocab_panel_present_when_vocab_field_in_manifest(self, tmp_path: Path):
        """When manifest includes vocab field, a vocab index panel must appear in the output."""
        manifest_path = self._make_vocab_manifest(tmp_path)
        out = tmp_path / "vocab-out.html"
        result = render(manifest_path, "document-class", out)
        assert "vocab-index" in result or "vocab-panel" in result, (
            "Vocab index panel must appear when manifest has vocab field"
        )

    def test_vocab_terms_appear_in_panel(self, tmp_path: Path):
        """Each vocab term must appear in the vocab index panel."""
        manifest_path = self._make_vocab_manifest(tmp_path)
        out = tmp_path / "vocab-out.html"
        result = render(manifest_path, "document-class", out)
        for term in VOCAB_MANIFEST_TERMS:
            assert term in result, f"Vocab term '{term}' missing from rendered output"

    def test_vocab_definitions_appear_in_panel(self, tmp_path: Path):
        """Each vocab definition must appear in the rendered output."""
        manifest_path = self._make_vocab_manifest(tmp_path)
        out = tmp_path / "vocab-out.html"
        result = render(manifest_path, "document-class", out)
        for _term, defn in VOCAB_MANIFEST_TERMS.items():
            # Definition text must be present somewhere in the output
            assert defn[:40] in result, f"Definition '{defn[:40]}...' missing from rendered output"

    def test_vocab_tooltip_markup_present(self, tmp_path: Path):
        """Tooltip markup (vocab-term class or data-def attribute) must be present."""
        manifest_path = self._make_vocab_manifest(tmp_path)
        out = tmp_path / "vocab-out.html"
        result = render(manifest_path, "document-class", out)
        assert "vocab-term" in result, (
            "vocab-term CSS class must be used to mark tooltip-enhanced terms"
        )

    def test_no_vocab_panel_without_vocab_field(self, manifest_file: Path, output_file: Path):
        """When manifest has no vocab field, no vocab panel must appear."""
        result = render(manifest_file, "document-class", output_file)
        assert "vocab-index" not in result and "vocab-panel" not in result, (
            "Vocab panel must not appear when manifest has no vocab field"
        )

    def test_vocab_panel_alphabetically_sorted(self, tmp_path: Path):
        """Terms in the vocab index panel must appear in alphabetical order."""
        manifest_path = self._make_vocab_manifest(tmp_path)
        out = tmp_path / "vocab-out.html"
        result = render(manifest_path, "document-class", out)
        # Find where each term appears in the vocab panel section
        vocab_panel_start = result.find("vocab-index")
        if vocab_panel_start == -1:
            vocab_panel_start = result.find("vocab-panel")
        panel_section = result[vocab_panel_start:] if vocab_panel_start != -1 else result
        sorted_terms = sorted(VOCAB_MANIFEST_TERMS.keys())
        prev_pos = 0
        for term in sorted_terms:
            pos = panel_section.find(term, prev_pos)
            assert pos >= prev_pos, (
                f"Term '{term}' is out of alphabetical order in vocab panel"
            )
            prev_pos = pos
