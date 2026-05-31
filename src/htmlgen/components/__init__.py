"""
HTML component library — reusable UI primitives for Lobster HTML documents.

Each component module exports:
  - COMPONENT_ID: str — stable identifier
  - COMPONENT_VERSION: str — semver
  - CONFIG_SCHEMA: dict — JSON Schema for config validation
  - render(config: dict) -> str — returns HTML+CSS+JS fragment
"""

from . import clipboard_copy_widget
from . import d3_vocabulary_network
from . import theme_toggle
from . import vocab_tooltip

COMPONENTS: dict = {
    "theme-toggle": theme_toggle,
    "clipboard-copy-widget": clipboard_copy_widget,
    "d3-vocabulary-network": d3_vocabulary_network,
    "vocab-tooltip": vocab_tooltip,
}


def get_component(component_id: str):
    """Return the component module for the given ID.

    Raises ValueError if the component is not registered.
    """
    if component_id not in COMPONENTS:
        raise ValueError(
            f"Unknown component: {component_id!r}. "
            f"Available: {sorted(COMPONENTS.keys())}"
        )
    return COMPONENTS[component_id]


__all__ = [
    "COMPONENTS",
    "get_component",
    "theme_toggle",
    "clipboard_copy_widget",
    "d3_vocabulary_network",
    "vocab_tooltip",
]
