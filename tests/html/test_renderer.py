"""
tests/html/test_renderer.py

Integration tests for src/html/renderer.py — full compilation pipeline.

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

from src.html.renderer import (
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

    def test_render_has_clipboard_widget_component(self, manifest_file: Path, output_file: Path):
        """document-class template requires clipboard-copy-widget component."""
        result = render(manifest_file, "document-class", output_file)
        assert 'id="comment-widget"' in result

    def test_render_clipboard_widget_has_all_section_inputs(
        self, manifest_file: Path, output_file: Path
    ):
        """Clipboard widget must reference all sections from the manifest."""
        result = render(manifest_file, "document-class", output_file)
        assert '"id": "s1"' in result or '"id":"s1"' in result.replace(" ", "")
        assert '"id": "s2"' in result or '"id":"s2"' in result.replace(" ", "")

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
        with patch("src.html.renderer._uploads_dir", return_value=tmp_path):
            with patch(
                "src.html.renderer._bisque_base_url",
                return_value="http://5.78.201.64:9101",
            ):
                url = render_and_upload(manifest_file, "document-class", "test-doc.html")
        assert isinstance(url, str)
        assert url.startswith("http://")
        assert "test-doc.html" in url

    def test_render_and_upload_writes_file(
        self, manifest_file: Path, tmp_path: Path
    ):
        with patch("src.html.renderer._uploads_dir", return_value=tmp_path):
            with patch(
                "src.html.renderer._bisque_base_url",
                return_value="http://5.78.201.64:9101",
            ):
                render_and_upload(manifest_file, "document-class", "test-doc.html")
        assert (tmp_path / "test-doc.html").exists()
        assert (tmp_path / "test-doc.html").stat().st_size > 0

    def test_render_and_upload_url_format(
        self, manifest_file: Path, tmp_path: Path
    ):
        with patch("src.html.renderer._uploads_dir", return_value=tmp_path):
            with patch(
                "src.html.renderer._bisque_base_url",
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
                sys.executable, "-m", "src.html.renderer",
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
                sys.executable, "-m", "src.html.renderer",
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
            [sys.executable, "-m", "src.html.renderer", "--content", "/tmp/x.json"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent.parent,
        )
        assert result.returncode != 0

    def test_cli_nonexistent_content_path_fails(self, tmp_path: Path):
        output = tmp_path / "out.html"
        result = subprocess.run(
            [
                sys.executable, "-m", "src.html.renderer",
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
