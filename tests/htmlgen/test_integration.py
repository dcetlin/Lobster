"""
tests/html/test_integration.py

End-to-end integration tests for the Phase 1 HTML document model.

Verifies:
  1. Layers A (conventions), B (templates), C (components) import together without conflicts.
  2. Renderer produces valid HTML from a minimal content fixture.

Run with: uv run pytest tests/html/test_integration.py -v
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import all four layers — confirms no circular imports or namespace collisions
# ---------------------------------------------------------------------------

from src.htmlgen.conventions import load_conventions, get_color_token, build_css_custom_properties
from src.htmlgen.templates.registry import load_registry, get_template
from src.htmlgen.components import COMPONENTS, get_component
from src.htmlgen.renderer import render, render_and_upload, validate_html, ValidationError

# Layer A — individual module imports
from src.htmlgen.conventions import (
    get_all_color_tokens,
    get_typography,
    get_layout,
    get_section_id_scheme,
    get_required_meta_tags,
)

# Layer B — template modules
from src.htmlgen.templates import registry as template_registry_mod

# Layer C — component modules
from src.htmlgen.components import (
    theme_toggle,
    clipboard_copy_widget,
    d3_vocabulary_network,
)


# ---------------------------------------------------------------------------
# Smoke test fixture
# ---------------------------------------------------------------------------

SMOKE_MANIFEST = {
    "doc_id": "integration-smoke-test",
    "title": "Integration Smoke Test",
    "subtitle": "Phase 1 end-to-end validation",
    "version": "1.1",
    "updated_at": "2026-05-30T00:00:00Z",
    "template_id": "spec-document",
    "sections": [
        {
            "id": "s1",
            "label": "§1",
            "title": "Purpose",
            "content": "Validates that all four layers work together.",
            "addressed": True,
            "tags": ["scope"],
        },
        {
            "id": "s2",
            "label": "§2",
            "title": "Scope",
            "content": "Covers conventions, templates, components, and renderer.",
            "addressed": True,
            "tags": [],
        },
        {
            "id": "s2-1",
            "label": "§2.1",
            "title": "Conventions Layer",
            "content": "The conventions layer provides design tokens.",
        },
        {
            "id": "s2-2",
            "label": "§2.2",
            "title": "Component Layer",
            "content": "The component layer provides reusable UI primitives.",
        },
    ],
    "components": [],
}


@pytest.fixture
def smoke_manifest_file(tmp_path: Path) -> Path:
    p = tmp_path / "smoke.json"
    p.write_text(json.dumps(SMOKE_MANIFEST), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Layer coexistence tests
# ---------------------------------------------------------------------------


class TestLayerImports:
    """All four layers must import together without conflicts."""

    def test_conventions_importable(self):
        conv = load_conventions()
        assert isinstance(conv, dict)
        assert "color_tokens" in conv

    def test_template_registry_importable(self):
        templates = load_registry()
        assert isinstance(templates, list)
        assert len(templates) >= 3

    def test_components_importable(self):
        assert "theme-toggle" in COMPONENTS
        assert "clipboard-copy-widget" in COMPONENTS  # still in registry; not in templates
        assert "d3-vocabulary-network" in COMPONENTS
        assert "vocab-tooltip" in COMPONENTS

    def test_renderer_importable(self):
        # Just confirm the key functions are callable
        assert callable(render)
        assert callable(render_and_upload)
        assert callable(validate_html)

    def test_all_component_modules_have_render_callable(self):
        for comp_id, mod in COMPONENTS.items():
            assert callable(getattr(mod, "render", None)), f"{comp_id} missing render()"

    def test_conventions_and_components_no_name_collision(self):
        """Verify that importing both doesn't shadow each other."""
        conv_tokens = get_all_color_tokens("dark")
        assert conv_tokens["bg"] == "#0b0d14"

        mod = get_component("theme-toggle")
        assert mod.COMPONENT_ID == "theme-toggle"

    def test_all_three_templates_registered(self):
        assert get_template("document-class")["id"] == "document-class"
        assert get_template("dashboard-class")["id"] == "dashboard-class"
        assert get_template("spec-document")["id"] == "spec-document"


# ---------------------------------------------------------------------------
# End-to-end renderer tests
# ---------------------------------------------------------------------------


class TestEndToEndRender:
    def test_render_produces_valid_html(self, smoke_manifest_file: Path, tmp_path: Path):
        out = tmp_path / "smoke.html"
        result = render(smoke_manifest_file, "spec-document", out)
        assert "<!DOCTYPE html>" in result
        assert "<html" in result
        assert "</html>" in result

    def test_render_output_file_written(self, smoke_manifest_file: Path, tmp_path: Path):
        out = tmp_path / "smoke.html"
        render(smoke_manifest_file, "spec-document", out)
        assert out.exists()
        assert out.stat().st_size > 100  # non-trivial content

    def test_render_has_all_section_ids(self, smoke_manifest_file: Path, tmp_path: Path):
        out = tmp_path / "smoke.html"
        result = render(smoke_manifest_file, "spec-document", out)
        for sid in ["s1", "s2", "s2-1", "s2-2"]:
            assert f'id="{sid}"' in result, f"Section {sid} missing from output"

    def test_render_has_doc_version_meta(self, smoke_manifest_file: Path, tmp_path: Path):
        out = tmp_path / "smoke.html"
        result = render(smoke_manifest_file, "spec-document", out)
        assert re.search(r'<meta[^>]+name=["\']doc-version["\']', result, re.IGNORECASE)

    def test_render_version_value_matches_manifest(
        self, smoke_manifest_file: Path, tmp_path: Path
    ):
        out = tmp_path / "smoke.html"
        result = render(smoke_manifest_file, "spec-document", out)
        assert '1.1' in result  # version from manifest

    def test_render_title_in_output(self, smoke_manifest_file: Path, tmp_path: Path):
        out = tmp_path / "smoke.html"
        result = render(smoke_manifest_file, "spec-document", out)
        assert "Integration Smoke Test" in result

    def test_render_css_custom_properties_from_conventions(
        self, smoke_manifest_file: Path, tmp_path: Path
    ):
        """Conventions color tokens must appear as CSS custom properties."""
        out = tmp_path / "smoke.html"
        result = render(smoke_manifest_file, "spec-document", out)
        # Dark mode bg token from conventions.yaml
        assert "--bg:" in result
        # Dark mode accent
        assert "--accent:" in result
        # The actual hex values from conventions
        assert "#0b0d14" in result  # dark.bg
        assert "#6ea8fe" in result  # dark.accent

    def test_render_includes_theme_toggle(self, smoke_manifest_file: Path, tmp_path: Path):
        out = tmp_path / "smoke.html"
        result = render(smoke_manifest_file, "spec-document", out)
        assert 'id="theme-toggle"' in result

    def test_render_section_comments_absent(self, smoke_manifest_file: Path, tmp_path: Path):
        """Section Comments widget (clipboard-copy-widget) must not appear in spec-document output.

        Dan confirmed this panel is redundant — the inline comment buttons and
        'Copy all comments' button already provide this functionality.
        """
        out = tmp_path / "spec.html"
        result = render(smoke_manifest_file, "spec-document", out)
        assert 'id="comment-widget"' not in result
        assert "Section Comments" not in result

    def test_render_passes_post_render_validation(
        self, smoke_manifest_file: Path, tmp_path: Path
    ):
        """render() must not raise ValidationError for a well-formed manifest."""
        out = tmp_path / "spec.html"
        # If this doesn't raise, validation passed
        result = render(smoke_manifest_file, "spec-document", out)
        assert result  # non-empty

    def test_render_no_d3_cdn_without_d3_component(
        self, smoke_manifest_file: Path, tmp_path: Path
    ):
        out = tmp_path / "spec.html"
        result = render(smoke_manifest_file, "spec-document", out)
        assert "d3js.org" not in result

    def test_render_document_class_and_spec_document_both_work(self, tmp_path: Path):
        """Both templates must render without errors."""
        manifest = dict(SMOKE_MANIFEST)
        for template_id in ["document-class", "spec-document"]:
            manifest["template_id"] = template_id
            p = tmp_path / f"{template_id}.json"
            p.write_text(json.dumps(manifest), encoding="utf-8")
            out = tmp_path / f"{template_id}.html"
            result = render(p, template_id, out)
            assert "<!DOCTYPE html>" in result, f"{template_id} render failed"


# ---------------------------------------------------------------------------
# Convention → Component integration
# ---------------------------------------------------------------------------


class TestConventionComponentIntegration:
    """Verify that conventions flow into components correctly."""

    def test_theme_toggle_uses_css_vars_from_conventions(self):
        """Theme toggle CSS should use CSS custom properties defined in conventions."""
        result = theme_toggle.render({"mode": "js-toggle"})
        # Theme toggle CSS uses var(--surface2), var(--border), etc.
        assert "var(--surface" in result or "var(--border" in result

    def test_clipboard_widget_uses_css_vars(self):
        """Clipboard widget CSS should use CSS custom properties."""
        result = clipboard_copy_widget.render({
            "sections": [{"id": "s1", "label": "§1"}]
        })
        assert "var(--" in result

    def test_conventions_and_theme_toggle_bg_token_consistent(self):
        """The dark bg color from conventions must match what theme toggle expects."""
        dark_bg = get_color_token("bg", "dark")
        assert dark_bg == "#0b0d14"
        # theme toggle in js-toggle mode doesn't hardcode colors — uses CSS vars
        # this test just confirms the design system token is correct
        assert dark_bg.startswith("#")
