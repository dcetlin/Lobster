"""
tests/html/test_components.py

Tests for src/html/components/ — theme toggle, clipboard widget, D3 network.
Run with: uv run pytest tests/html/test_components.py -v

Note: We use Python's stdlib html.parser (via xml.etree.ElementTree on wrapped
fragments) rather than bs4 because conftest.py puts src/ on sys.path, which
shadows the stdlib html package and breaks bs4's import of html.entities.
"""

import re
import pytest

from src.html.components import (
    COMPONENTS,
    get_component,
    clipboard_copy_widget,
    d3_vocabulary_network,
    theme_toggle,
)


# ---------------------------------------------------------------------------
# HTML parse helper — stdlib only
# ---------------------------------------------------------------------------


class _TagCollector:
    """Minimal HTML tag scanner using Python's stdlib html.parser via importlib."""

    def __init__(self, html_str: str):
        self._html = html_str

    def find_tag(self, tag: str, attrs: dict | None = None) -> bool:
        """Return True if the HTML contains a given tag with optional attr matches."""
        # Use regex-based scanning to avoid any bs4 / html module dependency
        pattern = rf"<{re.escape(tag)}(\s[^>]*)?"
        for m in re.finditer(pattern, self._html, re.IGNORECASE):
            if attrs is None:
                return True
            full = m.group(0)
            all_match = True
            for attr_name, attr_val in attrs.items():
                # Check for attr="val" or attr='val'
                if not re.search(
                    rf'{re.escape(attr_name)}\s*=\s*["\']?{re.escape(attr_val)}["\']?',
                    full,
                    re.IGNORECASE,
                ):
                    all_match = False
                    break
            if all_match:
                return True
        return False

    def count_tags(self, tag: str) -> int:
        """Count occurrences of an opening tag."""
        return len(re.findall(rf"<{re.escape(tag)}[\s>]", self._html, re.IGNORECASE))


def _parse(html: str) -> "_TagCollector":
    """Return a tag scanner for the given HTML fragment."""
    return _TagCollector(html)


# ---------------------------------------------------------------------------
# Component registry
# ---------------------------------------------------------------------------


class TestComponentRegistry:
    def test_all_components_registered(self):
        assert "theme-toggle" in COMPONENTS
        assert "clipboard-copy-widget" in COMPONENTS
        assert "d3-vocabulary-network" in COMPONENTS

    def test_get_component_returns_module(self):
        mod = get_component("theme-toggle")
        assert mod is theme_toggle

    def test_get_component_raises_on_unknown(self):
        with pytest.raises(ValueError, match="Unknown component"):
            get_component("nonexistent-component")

    def test_all_components_have_required_attributes(self):
        for component_id, mod in COMPONENTS.items():
            assert hasattr(mod, "COMPONENT_ID"), f"{component_id} missing COMPONENT_ID"
            assert hasattr(mod, "COMPONENT_VERSION"), f"{component_id} missing COMPONENT_VERSION"
            assert hasattr(mod, "CONFIG_SCHEMA"), f"{component_id} missing CONFIG_SCHEMA"
            assert callable(getattr(mod, "render", None)), f"{component_id} missing render()"


# ---------------------------------------------------------------------------
# theme_toggle
# ---------------------------------------------------------------------------


class TestThemeToggle:
    def test_component_id(self):
        assert theme_toggle.COMPONENT_ID == "theme-toggle"

    def test_component_version_semver(self):
        parts = theme_toggle.COMPONENT_VERSION.split(".")
        assert len(parts) == 3, "COMPONENT_VERSION should be semver X.Y.Z"

    def test_config_schema_is_dict(self):
        assert isinstance(theme_toggle.CONFIG_SCHEMA, dict)

    def test_render_js_toggle_returns_nonempty_string(self):
        result = theme_toggle.render({"mode": "js-toggle"})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_js_toggle_contains_button(self):
        result = theme_toggle.render({"mode": "js-toggle"})
        soup = _parse(result)
        assert soup.find_tag("button", {"id": "theme-toggle"}), \
            "js-toggle mode must include <button id='theme-toggle'>"

    def test_render_js_toggle_contains_script(self):
        result = theme_toggle.render({"mode": "js-toggle"})
        soup = _parse(result)
        assert soup.count_tags("script") > 0, "js-toggle mode must include a <script> block"

    def test_render_js_toggle_contains_css(self):
        result = theme_toggle.render({"mode": "js-toggle"})
        soup = _parse(result)
        assert soup.count_tags("style") > 0, "js-toggle mode must include a <style> block"

    def test_render_media_query_returns_nonempty_string(self):
        result = theme_toggle.render({"mode": "media-query"})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_media_query_contains_style_no_button(self):
        result = theme_toggle.render({"mode": "media-query"})
        soup = _parse(result)
        assert not soup.find_tag("button", {"id": "theme-toggle"}), \
            "media-query mode must not include the toggle button"
        assert soup.count_tags("style") > 0

    def test_render_default_mode_is_js_toggle(self):
        result = theme_toggle.render({})
        soup = _parse(result)
        assert soup.find_tag("button", {"id": "theme-toggle"}), \
            "Default mode should produce the toggle button"

    def test_config_schema_mode_field(self):
        schema = theme_toggle.CONFIG_SCHEMA
        assert "properties" in schema
        assert "mode" in schema["properties"]
        assert "enum" in schema["properties"]["mode"]
        assert "js-toggle" in schema["properties"]["mode"]["enum"]
        assert "media-query" in schema["properties"]["mode"]["enum"]

    def test_js_toggle_contains_localstorage_key(self):
        result = theme_toggle.render({"mode": "js-toggle"})
        assert "localStorage" in result, "Must use localStorage for theme persistence"

    def test_js_toggle_contains_light_mode_class(self):
        result = theme_toggle.render({"mode": "js-toggle"})
        assert "light-mode" in result, "Must toggle 'light-mode' CSS class on body"


# ---------------------------------------------------------------------------
# clipboard_copy_widget
# ---------------------------------------------------------------------------


SAMPLE_SECTIONS = [
    {"id": "s1", "label": "§1"},
    {"id": "s2", "label": "§2"},
    {"id": "s3", "label": "§3"},
]


class TestClipboardCopyWidget:
    def test_component_id(self):
        assert clipboard_copy_widget.COMPONENT_ID == "clipboard-copy-widget"

    def test_component_version_semver(self):
        parts = clipboard_copy_widget.COMPONENT_VERSION.split(".")
        assert len(parts) == 3

    def test_config_schema_is_dict(self):
        assert isinstance(clipboard_copy_widget.CONFIG_SCHEMA, dict)

    def test_config_schema_requires_sections(self):
        schema = clipboard_copy_widget.CONFIG_SCHEMA
        assert "required" in schema
        assert "sections" in schema["required"]

    def test_render_returns_nonempty_string(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_contains_comment_widget_div(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        soup = _parse(result)
        assert soup.find_tag("div", {"id": "comment-widget"}), \
            "Output must contain <div id='comment-widget'>"

    def test_render_contains_comment_inputs_container(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        soup = _parse(result)
        assert soup.find_tag("div", {"id": "comment-inputs"})

    def test_render_contains_copy_button(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        soup = _parse(result)
        assert soup.find_tag("button", {"class": "copy-comments-btn"})

    def test_render_contains_style_block(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        soup = _parse(result)
        assert soup.count_tags("style") > 0

    def test_render_contains_script_block(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        soup = _parse(result)
        assert soup.count_tags("script") > 0

    def test_render_script_contains_section_ids(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        assert "s1" in result, "sections JSON must appear in script"

    def test_render_script_contains_clipboard_guard(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        assert "isSecureContext" in result, "Must include isSecureContext guard"
        assert "execCommand" in result, "Must include execCommand fallback"

    def test_render_empty_sections_list(self):
        result = clipboard_copy_widget.render({"sections": []})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_config_schema_sections_items_require_id_and_label(self):
        schema = clipboard_copy_widget.CONFIG_SCHEMA
        items = schema["properties"]["sections"]["items"]
        assert "required" in items
        assert "id" in items["required"]
        assert "label" in items["required"]

    def test_render_contains_feedback_div(self):
        result = clipboard_copy_widget.render({"sections": SAMPLE_SECTIONS})
        soup = _parse(result)
        assert soup.find_tag("div", {"id": "copy-feedback"})


# ---------------------------------------------------------------------------
# d3_vocabulary_network
# ---------------------------------------------------------------------------


SAMPLE_NODES = [
    {
        "id": "Concept A",
        "tier": "register",
        "color": "#6ea8fe",
        "def": "A top-level concept.",
        "examples": [{"domain": "example", "text": "sample text"}],
    },
    {
        "id": "Param X",
        "tier": "param",
        "register": "Concept A",
        "color": "#7dd3fc",
        "def": "A parameter of Concept A.",
        "examples": [],
    },
    {
        "id": "Cross Y",
        "tier": "cross",
        "color": "#86efac",
        "def": "A cross-register measurement.",
        "examples": [],
    },
]

SAMPLE_LINKS = [
    {"source": "Param X", "target": "Concept A", "type": "param"},
    {"source": "Cross Y", "target": "Concept A", "type": "cross"},
]


class TestD3VocabularyNetwork:
    def test_component_id(self):
        assert d3_vocabulary_network.COMPONENT_ID == "d3-vocabulary-network"

    def test_component_version_semver(self):
        parts = d3_vocabulary_network.COMPONENT_VERSION.split(".")
        assert len(parts) == 3

    def test_config_schema_is_dict(self):
        assert isinstance(d3_vocabulary_network.CONFIG_SCHEMA, dict)

    def test_config_schema_requires_nodes_and_links(self):
        schema = d3_vocabulary_network.CONFIG_SCHEMA
        assert "required" in schema
        assert "nodes" in schema["required"]
        assert "links" in schema["required"]

    def test_render_returns_nonempty_string(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_render_contains_svg_target(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        soup = _parse(result)
        assert soup.find_tag("svg", {"id": "vocabulary-graph"}), \
            "Output must contain <svg id='vocabulary-graph'>"

    def test_render_contains_tooltip_div(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        soup = _parse(result)
        assert soup.find_tag("div", {"id": "graph-tooltip"})

    def test_render_contains_legend_div(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        soup = _parse(result)
        assert soup.find_tag("div", {"id": "graph-legend"})

    def test_render_contains_filter_controls(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        soup = _parse(result)
        assert soup.find_tag("div", {"class": "graph-controls"})

    def test_render_contains_style_block(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        soup = _parse(result)
        assert soup.count_tags("style") > 0

    def test_render_contains_script_block(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        soup = _parse(result)
        assert soup.count_tags("script") > 0

    def test_render_script_contains_node_data(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        assert "Concept A" in result, "Node data must appear in script"

    def test_render_empty_graph(self):
        result = d3_vocabulary_network.render({"nodes": [], "links": []})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_config_schema_node_tier_enum(self):
        schema = d3_vocabulary_network.CONFIG_SCHEMA
        tier_schema = schema["properties"]["nodes"]["items"]["properties"]["tier"]
        assert "enum" in tier_schema
        assert "register" in tier_schema["enum"]
        assert "param" in tier_schema["enum"]
        assert "cross" in tier_schema["enum"]

    def test_config_schema_link_type_enum(self):
        schema = d3_vocabulary_network.CONFIG_SCHEMA
        link_type_schema = schema["properties"]["links"]["items"]["properties"]["type"]
        assert "enum" in link_type_schema
        assert "param" in link_type_schema["enum"]
        assert "cross" in link_type_schema["enum"]
        assert "hub" in link_type_schema["enum"]

    def test_render_script_uses_d3_force_simulation(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        assert "forceSimulation" in result, "D3 force simulation must be present"

    def test_render_contains_drag_behavior(self):
        result = d3_vocabulary_network.render(
            {"nodes": SAMPLE_NODES, "links": SAMPLE_LINKS}
        )
        assert "d3.drag" in result, "D3 drag behavior must be present"
