"""
conventions.py — Loader for the Lobster HTML design system conventions.

Reads conventions.yaml and provides a clean query API for the renderer.
Importable without side effects.
"""

from __future__ import annotations

import re
from pathlib import Path
from functools import lru_cache
from typing import Any

import yaml

_CONVENTIONS_PATH = Path(__file__).parent / "conventions.yaml"


@lru_cache(maxsize=1)
def load_conventions() -> dict[str, Any]:
    """Load and return the parsed conventions.yaml as a dict.

    Cached after first call — safe to call repeatedly.
    Raises FileNotFoundError if conventions.yaml is missing.
    Raises yaml.YAMLError if the file is malformed.
    """
    with open(_CONVENTIONS_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    _validate(data)
    return data


def _validate(data: dict[str, Any]) -> None:
    """Basic schema check — raises ValueError on structural problems."""
    required_top_keys = [
        "conventions_version",
        "color_tokens",
        "typography",
        "layout",
        "section_ids",
        "version_metadata",
        "document_class",
        "dashboard_class",
    ]
    for key in required_top_keys:
        if key not in data:
            raise ValueError(
                f"conventions.yaml is missing required top-level key: '{key}'"
            )

    color_tokens = data["color_tokens"]
    for mode in ("dark", "light"):
        if mode not in color_tokens:
            raise ValueError(
                f"conventions.yaml color_tokens is missing '{mode}' section"
            )
        required_tokens = [
            "bg", "surface", "surface2", "surface3",
            "border", "border2",
            "text", "text2", "text3",
            "accent", "accent2", "accent3",
            "warn",
        ]
        for token in required_tokens:
            if token not in color_tokens[mode]:
                raise ValueError(
                    f"conventions.yaml color_tokens.{mode} is missing token '{token}'"
                )


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------


def get_conventions_version() -> str:
    """Return the conventions_version string (e.g. '1.0')."""
    return load_conventions()["conventions_version"]


def get_color_token(name: str, mode: str = "dark") -> str:
    """Return the hex value for a color token in the given mode.

    Args:
        name: Token name, e.g. 'bg', 'accent', 'text2'.
        mode: 'dark' or 'light'. Defaults to 'dark'.

    Returns:
        Hex color string, e.g. '#0b0d14'.

    Raises:
        KeyError: If the token or mode is not found.
    """
    tokens = load_conventions()["color_tokens"]
    if mode not in tokens:
        raise KeyError(f"Unknown color mode: '{mode}'. Expected 'dark' or 'light'.")
    if name not in tokens[mode]:
        raise KeyError(f"Unknown color token: '{name}' in mode '{mode}'.")
    return tokens[mode][name]


def get_all_color_tokens(mode: str = "dark") -> dict[str, str]:
    """Return all color tokens for the given mode as {name: hex} dict."""
    tokens = load_conventions()["color_tokens"]
    if mode not in tokens:
        raise KeyError(f"Unknown color mode: '{mode}'. Expected 'dark' or 'light'.")
    return dict(tokens[mode])


def get_typography() -> dict[str, str]:
    """Return the typography settings dict."""
    return dict(load_conventions()["typography"])


def get_layout() -> dict[str, str]:
    """Return the layout settings dict."""
    return dict(load_conventions()["layout"])


def get_section_id_scheme() -> dict[str, Any]:
    """Return the section_ids configuration dict."""
    return dict(load_conventions()["section_ids"])


def get_section_label(section_id: str) -> str:
    """Derive the display label (e.g. '§1', '§1.1') from a section ID.

    Mapping rules:
        s1       → §1
        s1-1     → §1.1
        s1-1-1   → §1.1.1

    Raises:
        ValueError: If section_id does not match any known pattern.
    """
    # Validate against known patterns
    scheme = load_conventions()["section_ids"]
    patterns = [
        scheme["top_level_pattern"],
        scheme["subsection_pattern"],
        scheme["sub_subsection_pattern"],
    ]
    if not any(re.match(p, section_id) for p in patterns):
        raise ValueError(
            f"Section ID '{section_id}' does not match any recognised pattern. "
            "Expected format: 's1', 's1-1', 's1-1-1'."
        )

    # Remove leading 's', replace '-' with '.'
    numeric_part = section_id[1:].replace("-", ".")
    return f"§{numeric_part}"


def get_required_meta_tags() -> list[dict[str, str]]:
    """Return the list of required meta tag definitions from version_metadata."""
    return list(load_conventions()["version_metadata"]["required_meta_tags"])


def get_document_class_config() -> dict[str, Any]:
    """Return the document_class conventions dict."""
    return dict(load_conventions()["document_class"])


def get_dashboard_class_config() -> dict[str, Any]:
    """Return the dashboard_class conventions dict."""
    return dict(load_conventions()["dashboard_class"])


def build_css_custom_properties(mode: str = "dark") -> str:
    """Generate CSS custom property declarations from color tokens.

    Returns a string of '--name: value;' lines (no selector wrapper).
    The caller is responsible for wrapping in :root { } or a class.
    """
    tokens = get_all_color_tokens(mode)
    lines = [f"  --{name}: {value};" for name, value in tokens.items()]
    return "\n".join(lines)
