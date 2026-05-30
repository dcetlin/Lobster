"""Convenience re-export for the template registry.

The canonical implementation is src/html/templates/registry.py.
This module re-exports the public API for callers that prefer the
top-level import path: `from src.html.template_registry import get_template`.
"""

from src.html.templates.registry import (  # noqa: F401 — re-exports
    get_template,
    list_templates,
    load_registry,
    select_template,
)
