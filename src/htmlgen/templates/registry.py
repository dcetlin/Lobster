"""Template registry loader for the Lobster HTML document model.

Provides:
  load_registry() -> list[dict]   — all templates from registry.yaml
  get_template(template_id) -> dict — single template by id
  select_template(doc_type) -> dict — choose template for a given document type

This module is importable without side effects. All I/O is deferred until a
function is called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

_REGISTRY_FILE = Path(__file__).parent / "registry.yaml"

# Mapping of doc_type keywords to template ids for select_template()
_TYPE_TO_TEMPLATE: dict[str, str] = {
    # document-class signals
    "document": "document-class",
    "document-class": "document-class",
    "narrative": "document-class",
    "report": "document-class",
    "design": "document-class",
    "proposal": "document-class",
    "audit": "document-class",
    "synthesis": "document-class",
    # dashboard-class signals
    "dashboard": "dashboard-class",
    "dashboard-class": "dashboard-class",
    "interactive": "dashboard-class",
    "status": "dashboard-class",
    "live": "dashboard-class",
    "data": "dashboard-class",
    # spec-document signals
    "spec": "spec-document",
    "spec-document": "spec-document",
    "architecture": "spec-document",
    "specification": "spec-document",
    "versioned": "spec-document",
}

_DEFAULT_TEMPLATE_ID = "document-class"


def load_registry() -> list[dict]:
    """Load and return all templates from registry.yaml.

    Returns a list of template dicts in the order they appear in the registry.
    Raises FileNotFoundError if registry.yaml does not exist.
    Raises ValueError if the registry file is malformed.
    """
    if not _REGISTRY_FILE.exists():
        raise FileNotFoundError(f"Template registry not found: {_REGISTRY_FILE}")

    with _REGISTRY_FILE.open() as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "templates" not in data:
        raise ValueError(
            f"Registry file malformed: expected top-level 'templates' key in {_REGISTRY_FILE}"
        )

    templates = data["templates"]
    if not isinstance(templates, list):
        raise ValueError(
            f"Registry file malformed: 'templates' must be a list in {_REGISTRY_FILE}"
        )

    return templates


def get_template(template_id: str) -> dict:
    """Return the template dict for the given id.

    Raises KeyError if no template with that id exists.
    """
    templates = load_registry()
    for template in templates:
        if template.get("id") == template_id:
            return template
    raise KeyError(
        f"Template '{template_id}' not found. "
        f"Available: {[t.get('id') for t in templates]}"
    )


def select_template(doc_type: str) -> dict:
    """Select the appropriate template for the given document type string.

    Applies the decision rule from spec §3:
      - narrative-first, linear reading, may have comment threads → document-class
      - data-driven, regenerated frequently, action buttons or filter bars → dashboard-class
      - long-lived, multi-session editing, section coverage tracking → spec-document

    The doc_type string is matched case-insensitively against known keywords.
    Falls back to document-class (the default) if no keyword matches.

    Returns the template dict.
    """
    normalized = doc_type.lower().strip()
    template_id = _TYPE_TO_TEMPLATE.get(normalized, _DEFAULT_TEMPLATE_ID)
    return get_template(template_id)


def list_templates() -> list[str]:
    """Return a list of all available template ids."""
    return [t.get("id", "") for t in load_registry()]
