"""Tests for the HTML template registry (Phase 1-B).

Verifies:
- All 3 templates load without error
- select_template() returns the correct template for each document class
- get_template() works correctly
- list_templates() returns all expected ids
"""

import pytest

from src.htmlgen.templates.registry import (
    get_template,
    list_templates,
    load_registry,
    select_template,
)

EXPECTED_TEMPLATE_IDS = {"document-class", "dashboard-class", "spec-document"}


class TestLoadRegistry:
    def test_load_registry_returns_list(self):
        templates = load_registry()
        assert isinstance(templates, list)

    def test_load_registry_has_three_templates(self):
        templates = load_registry()
        assert len(templates) == 3

    def test_load_registry_all_have_ids(self):
        templates = load_registry()
        ids = {t.get("id") for t in templates}
        assert ids == EXPECTED_TEMPLATE_IDS

    def test_load_registry_all_have_required_fields(self):
        templates = load_registry()
        required_fields = {"id", "name", "description", "content_format", "components"}
        for template in templates:
            missing = required_fields - set(template.keys())
            assert not missing, f"Template '{template.get('id')}' missing fields: {missing}"

    def test_document_class_uses_markdown(self):
        templates = load_registry()
        doc_class = next(t for t in templates if t["id"] == "document-class")
        assert doc_class["content_format"] == "markdown"

    def test_dashboard_class_uses_json(self):
        templates = load_registry()
        dashboard = next(t for t in templates if t["id"] == "dashboard-class")
        assert dashboard["content_format"] == "json"

    def test_spec_document_uses_json(self):
        templates = load_registry()
        spec = next(t for t in templates if t["id"] == "spec-document")
        assert spec["content_format"] == "json"

    def test_document_class_does_not_have_clipboard_widget(self):
        """clipboard-copy-widget was removed — Section Comments panel is redundant."""
        templates = load_registry()
        doc_class = next(t for t in templates if t["id"] == "document-class")
        component_ids = [c if isinstance(c, str) else c for c in doc_class["components"]]
        assert "clipboard-copy-widget" not in component_ids

    def test_document_class_has_theme_toggle(self):
        templates = load_registry()
        doc_class = next(t for t in templates if t["id"] == "document-class")
        assert "theme-toggle" in doc_class["components"]

    def test_spec_document_does_not_have_clipboard_widget(self):
        """clipboard-copy-widget was removed — Section Comments panel is redundant."""
        templates = load_registry()
        spec = next(t for t in templates if t["id"] == "spec-document")
        assert "clipboard-copy-widget" not in spec["components"]

    def test_dashboard_class_has_theme_toggle(self):
        templates = load_registry()
        dashboard = next(t for t in templates if t["id"] == "dashboard-class")
        assert "theme-toggle" in dashboard["components"]


class TestGetTemplate:
    def test_get_document_class(self):
        t = get_template("document-class")
        assert t["id"] == "document-class"

    def test_get_dashboard_class(self):
        t = get_template("dashboard-class")
        assert t["id"] == "dashboard-class"

    def test_get_spec_document(self):
        t = get_template("spec-document")
        assert t["id"] == "spec-document"

    def test_get_nonexistent_raises_key_error(self):
        with pytest.raises(KeyError):
            get_template("nonexistent-template")

    def test_get_template_returns_dict(self):
        t = get_template("document-class")
        assert isinstance(t, dict)


class TestSelectTemplate:
    """Test that select_template() returns the correct template for each doc type."""

    # document-class cases
    @pytest.mark.parametrize(
        "doc_type",
        ["document", "document-class", "narrative", "report", "design", "proposal", "audit"],
    )
    def test_selects_document_class(self, doc_type):
        t = select_template(doc_type)
        assert t["id"] == "document-class", f"Expected document-class for '{doc_type}', got '{t['id']}'"

    # dashboard-class cases
    @pytest.mark.parametrize(
        "doc_type",
        ["dashboard", "dashboard-class", "interactive", "status", "live"],
    )
    def test_selects_dashboard_class(self, doc_type):
        t = select_template(doc_type)
        assert t["id"] == "dashboard-class", f"Expected dashboard-class for '{doc_type}', got '{t['id']}'"

    # spec-document cases
    @pytest.mark.parametrize(
        "doc_type",
        ["spec", "spec-document", "architecture", "specification", "versioned"],
    )
    def test_selects_spec_document(self, doc_type):
        t = select_template(doc_type)
        assert t["id"] == "spec-document", f"Expected spec-document for '{doc_type}', got '{t['id']}'"

    def test_unknown_type_falls_back_to_document_class(self):
        t = select_template("something-completely-unknown-xyz")
        assert t["id"] == "document-class"

    def test_select_template_case_insensitive(self):
        t = select_template("DOCUMENT")
        assert t["id"] == "document-class"

    def test_select_template_strips_whitespace(self):
        t = select_template("  spec  ")
        assert t["id"] == "spec-document"


class TestListTemplates:
    def test_list_templates_returns_all_ids(self):
        ids = list_templates()
        assert set(ids) == EXPECTED_TEMPLATE_IDS

    def test_list_templates_returns_list_of_strings(self):
        ids = list_templates()
        assert all(isinstance(i, str) for i in ids)
