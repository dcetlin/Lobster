"""Convenience re-export for the template registry.

The canonical implementation is src/htmlgen/templates/registry.py.
This module re-exports the public API for callers that prefer the
top-level import path: `from src.htmlgen.template_registry import get_template`.
"""

from src.htmlgen.templates.registry import (  # noqa: F401 — re-exports
    get_template,
    list_templates,
    load_registry,
    select_template,
)
