"""
tests/html/test_conventions.py

Tests for src/htmlgen/conventions.py — loader and query API.
Run with: uv run pytest tests/html/test_conventions.py -v
"""

import pytest

# Reset the lru_cache between test runs so file mutations don't bleed
from src.htmlgen import conventions as conv_module
from src.htmlgen.conventions import (
    build_css_custom_properties,
    get_all_color_tokens,
    get_color_token,
    get_conventions_version,
    get_dashboard_class_config,
    get_document_class_config,
    get_layout,
    get_required_meta_tags,
    get_section_id_scheme,
    get_section_label,
    get_typography,
    load_conventions,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the lru_cache before each test so tests are independent."""
    conv_module.load_conventions.cache_clear()
    yield
    conv_module.load_conventions.cache_clear()


# ---------------------------------------------------------------------------
# load_conventions
# ---------------------------------------------------------------------------


class TestLoadConventions:
    def test_loads_without_error(self):
        data = load_conventions()
        assert isinstance(data, dict)

    def test_returns_dict_with_required_keys(self):
        data = load_conventions()
        required = [
            "conventions_version",
            "color_tokens",
            "typography",
            "layout",
            "section_ids",
            "version_metadata",
            "document_class",
            "dashboard_class",
        ]
        for key in required:
            assert key in data, f"Missing top-level key: {key}"

    def test_conventions_version_is_string(self):
        data = load_conventions()
        assert isinstance(data["conventions_version"], str)
        assert data["conventions_version"] == "1.0"

    def test_is_cached(self):
        data1 = load_conventions()
        data2 = load_conventions()
        assert data1 is data2  # same object, not re-parsed


# ---------------------------------------------------------------------------
# get_conventions_version
# ---------------------------------------------------------------------------


class TestGetConventionsVersion:
    def test_returns_expected_version(self):
        assert get_conventions_version() == "1.0"

    def test_returns_string(self):
        assert isinstance(get_conventions_version(), str)


# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------


class TestGetColorToken:
    def test_dark_bg(self):
        assert get_color_token("bg", "dark") == "#0b0d14"

    def test_dark_surface(self):
        assert get_color_token("surface", "dark") == "#141720"

    def test_dark_accent(self):
        assert get_color_token("accent", "dark") == "#6ea8fe"

    def test_dark_warn(self):
        assert get_color_token("warn", "dark") == "#fb923c"

    def test_light_bg(self):
        assert get_color_token("bg", "light") == "#f8f9fb"

    def test_light_accent(self):
        # Light mode uses higher-contrast blue
        assert get_color_token("accent", "light") == "#2563eb"

    def test_light_warn(self):
        assert get_color_token("warn", "light") == "#ea580c"

    def test_dark_is_default_mode(self):
        # Default mode should be dark
        assert get_color_token("bg") == get_color_token("bg", "dark")

    def test_unknown_token_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown color token"):
            get_color_token("nonexistent_token", "dark")

    def test_unknown_mode_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown color mode"):
            get_color_token("bg", "sepia")


class TestGetAllColorTokens:
    def test_dark_returns_dict(self):
        tokens = get_all_color_tokens("dark")
        assert isinstance(tokens, dict)

    def test_dark_has_all_required_tokens(self):
        tokens = get_all_color_tokens("dark")
        required = [
            "bg", "surface", "surface2", "surface3",
            "border", "border2",
            "text", "text2", "text3",
            "accent", "accent2", "accent3",
            "warn",
        ]
        for t in required:
            assert t in tokens, f"Missing dark token: {t}"

    def test_light_has_all_required_tokens(self):
        tokens = get_all_color_tokens("light")
        required = [
            "bg", "surface", "surface2", "surface3",
            "border", "border2",
            "text", "text2", "text3",
            "accent", "accent2", "accent3",
            "warn",
        ]
        for t in required:
            assert t in tokens, f"Missing light token: {t}"

    def test_dark_and_light_have_different_bg(self):
        assert get_all_color_tokens("dark")["bg"] != get_all_color_tokens("light")["bg"]

    def test_unknown_mode_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown color mode"):
            get_all_color_tokens("neon")


# ---------------------------------------------------------------------------
# Typography and layout
# ---------------------------------------------------------------------------


class TestGetTypography:
    def test_returns_dict(self):
        t = get_typography()
        assert isinstance(t, dict)

    def test_has_font_stack(self):
        t = get_typography()
        assert "font_stack" in t
        assert "system-ui" in t["font_stack"]

    def test_has_mono_stack(self):
        t = get_typography()
        assert "mono_stack" in t

    def test_base_size_is_15px(self):
        t = get_typography()
        assert t["base_size"] == "15px"

    def test_base_line_height(self):
        t = get_typography()
        assert t["base_line_height"] == "1.7"


class TestGetLayout:
    def test_returns_dict(self):
        lay = get_layout()
        assert isinstance(lay, dict)

    def test_has_wrap_max_width(self):
        lay = get_layout()
        assert "wrap_max_width" in lay
        assert lay["wrap_max_width"] == "820px"

    def test_has_wrap_padding(self):
        lay = get_layout()
        assert "wrap_padding" in lay


# ---------------------------------------------------------------------------
# Section ID scheme
# ---------------------------------------------------------------------------


class TestGetSectionIdScheme:
    def test_returns_dict(self):
        s = get_section_id_scheme()
        assert isinstance(s, dict)

    def test_has_top_level_pattern(self):
        s = get_section_id_scheme()
        assert "top_level_pattern" in s

    def test_id_persistence_is_true(self):
        s = get_section_id_scheme()
        assert s["id_persistence"] is True


class TestGetSectionLabel:
    def test_top_level_s1(self):
        assert get_section_label("s1") == "§1"

    def test_top_level_s10(self):
        assert get_section_label("s10") == "§10"

    def test_subsection_s1_1(self):
        assert get_section_label("s1-1") == "§1.1"

    def test_subsection_s2_3(self):
        assert get_section_label("s2-3") == "§2.3"

    def test_sub_subsection_s1_1_1(self):
        assert get_section_label("s1-1-1") == "§1.1.1"

    def test_sub_subsection_s3_2_4(self):
        assert get_section_label("s3-2-4") == "§3.2.4"

    def test_invalid_id_raises_value_error(self):
        with pytest.raises(ValueError, match="does not match any recognised pattern"):
            get_section_label("section1")

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            get_section_label("")


# ---------------------------------------------------------------------------
# Version metadata
# ---------------------------------------------------------------------------


class TestGetRequiredMetaTags:
    def test_returns_list(self):
        tags = get_required_meta_tags()
        assert isinstance(tags, list)

    def test_has_doc_version_tag(self):
        tags = get_required_meta_tags()
        names = [t["name"] for t in tags]
        assert "doc-version" in names

    def test_has_doc_updated_tag(self):
        tags = get_required_meta_tags()
        names = [t["name"] for t in tags]
        assert "doc-updated" in names

    def test_tags_have_content_source(self):
        tags = get_required_meta_tags()
        for tag in tags:
            assert "content_source" in tag, f"Tag {tag['name']} missing content_source"


# ---------------------------------------------------------------------------
# Document and dashboard class configs
# ---------------------------------------------------------------------------


class TestGetDocumentClassConfig:
    def test_returns_dict(self):
        cfg = get_document_class_config()
        assert isinstance(cfg, dict)

    def test_required_components_includes_theme_toggle(self):
        cfg = get_document_class_config()
        assert "theme-toggle" in cfg["required_components"]

    def test_required_components_includes_clipboard(self):
        cfg = get_document_class_config()
        assert "clipboard-copy-widget" in cfg["required_components"]

    def test_section_id_required_is_true(self):
        cfg = get_document_class_config()
        assert cfg["section_id_required"] is True

    def test_bisque_url_required_is_true(self):
        cfg = get_document_class_config()
        assert cfg["bisque_url_required"] is True


class TestGetDashboardClassConfig:
    def test_returns_dict(self):
        cfg = get_dashboard_class_config()
        assert isinstance(cfg, dict)

    def test_required_components_includes_theme_toggle(self):
        cfg = get_dashboard_class_config()
        assert "theme-toggle" in cfg["required_components"]

    def test_section_id_required_is_false(self):
        cfg = get_dashboard_class_config()
        assert cfg["section_id_required"] is False


# ---------------------------------------------------------------------------
# CSS custom properties builder
# ---------------------------------------------------------------------------


class TestBuildCssCustomProperties:
    def test_returns_string(self):
        css = build_css_custom_properties("dark")
        assert isinstance(css, str)

    def test_contains_bg_token(self):
        css = build_css_custom_properties("dark")
        assert "--bg: #0b0d14;" in css

    def test_contains_accent_token(self):
        css = build_css_custom_properties("dark")
        assert "--accent: #6ea8fe;" in css

    def test_light_mode_bg(self):
        css = build_css_custom_properties("light")
        assert "--bg: #f8f9fb;" in css

    def test_light_mode_accent_is_higher_contrast(self):
        css = build_css_custom_properties("light")
        assert "--accent: #2563eb;" in css

    def test_no_selector_wrapper(self):
        # The function returns only property lines, not :root { } or a class
        css = build_css_custom_properties("dark")
        assert ":root" not in css
        assert "{" not in css
