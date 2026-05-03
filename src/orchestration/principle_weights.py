"""
Epistemic principle weighting as a first-class dispatch primitive.

This module provides pure functions for assigning and formatting principle
weight vectors. Principle weights are explicit, never computed automatically --
they map task types to a fixed set of five epistemic principles with float
weights in [0.0, 1.0].

The five principles:
    pattern_perception         -- actively name structural patterns before conclusions
    structural_coherence       -- verify internal consistency before committing to output
    attunement_over_assumption -- name causal vs. symptom layers explicitly
    elegant_economy            -- stay near minimum viable output
    minimal_cognitive_friction -- lead with signal; key finding in paragraph 1

Usage:
    weights = assign_principle_weights("debugging")
    block = format_principle_weights_block(weights)
"""

from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Principle names (canonical order)
# ---------------------------------------------------------------------------

PRINCIPLE_NAMES: Final[tuple[str, ...]] = (
    "pattern_perception",
    "structural_coherence",
    "attunement_over_assumption",
    "elegant_economy",
    "minimal_cognitive_friction",
)

# ---------------------------------------------------------------------------
# Behavioral implications for each principle (rendered in the weight block)
# ---------------------------------------------------------------------------

PRINCIPLE_IMPLICATIONS: Final[dict[str, str]] = {
    "pattern_perception": "actively name structural patterns before conclusions",
    "structural_coherence": "verify internal consistency before committing to output",
    "attunement_over_assumption": "name causal vs. symptom layers explicitly",
    "elegant_economy": "stay near minimum viable output",
    "minimal_cognitive_friction": "lead with signal; key finding in paragraph 1",
}

# ---------------------------------------------------------------------------
# Weight profiles: task_type -> principle weight vector
# ---------------------------------------------------------------------------

PRINCIPLE_WEIGHT_PROFILES: Final[dict[str, dict[str, float]]] = {
    "debugging": {
        "pattern_perception": 0.7,
        "structural_coherence": 0.6,
        "attunement_over_assumption": 0.9,
        "elegant_economy": 0.5,
        "minimal_cognitive_friction": 0.7,
    },
    "mobile_response": {
        "pattern_perception": 0.5,
        "structural_coherence": 0.5,
        "attunement_over_assumption": 0.6,
        "elegant_economy": 0.7,
        "minimal_cognitive_friction": 0.9,
    },
    "design_inquiry": {
        "pattern_perception": 0.9,
        "structural_coherence": 0.8,
        "attunement_over_assumption": 0.7,
        "elegant_economy": 0.5,
        "minimal_cognitive_friction": 0.5,
    },
    "default": {
        "pattern_perception": 0.7,
        "structural_coherence": 0.7,
        "attunement_over_assumption": 0.7,
        "elegant_economy": 0.7,
        "minimal_cognitive_friction": 0.7,
    },
}

# ---------------------------------------------------------------------------
# Visibility threshold: principles with weight below this are omitted from
# the formatted block to avoid noise.
# ---------------------------------------------------------------------------

VISIBILITY_THRESHOLD: Final[float] = 0.6


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def assign_principle_weights(task_type: str) -> dict[str, float]:
    """Return the principle weight vector for the given task type.

    Falls back to the "default" profile for unknown task types.
    The returned dict is a fresh copy -- callers may mutate it freely.

    >>> assign_principle_weights("debugging")["attunement_over_assumption"]
    0.9
    >>> assign_principle_weights("unknown_type") == PRINCIPLE_WEIGHT_PROFILES["default"]
    True
    """
    profile = PRINCIPLE_WEIGHT_PROFILES.get(task_type, PRINCIPLE_WEIGHT_PROFILES["default"])
    return dict(profile)


def format_principle_weights_block(weights: dict[str, float]) -> str:
    """Render a principle weights block for inclusion in subagent prompts.

    Filters out principles with weight below VISIBILITY_THRESHOLD (0.6) to
    avoid noise. Each visible principle is rendered as:

        - <principle_name>: <weight> -- <behavioral_implication>

    Returns an empty string if no principles meet the threshold.

    >>> block = format_principle_weights_block({"pattern_perception": 0.9, "elegant_economy": 0.4})
    >>> "pattern_perception" in block
    True
    >>> "elegant_economy" in block
    False
    """
    lines: list[str] = []
    # Iterate in canonical order for deterministic output
    for name in PRINCIPLE_NAMES:
        weight = weights.get(name, 0.0)
        if weight >= VISIBILITY_THRESHOLD:
            implication = PRINCIPLE_IMPLICATIONS.get(name, "")
            lines.append(f"- {name}: {weight} — {implication}")

    return "\n".join(lines)
