# src/ooda/fast_thorough_selector.py
"""
Fast/Thorough Path meta-selector for OODA loop routing.

Mirrors the quick-classifier / slow-reclassifier architecture at the
decision layer. Selection criterion: can a vision.yaml anchor or a prior
logged decision be cited as the basis?

Fast Path  → Yes (well-encoded, low-stakes, prior exists or vision anchor traceable)
Thorough Path → No (novel, high-stakes, no prior, no anchor)

This selector is inserted as a gate before Decide dispatch in the OODA loop.
It does not replace the existing quick-classifier or slow-reclassifier; it
operates at the routing/decision layer, not the observation/orientation layer.

See: ~/lobster-workspace/design/human-ai-ooda-protocol.md
See: vision.yaml constraint-3 (Encoded Orientation gating)
"""

from __future__ import annotations

import logging

from src.orchestration.shard_dispatch import PathSelection

log = logging.getLogger(__name__)


def cite_basis(context: dict) -> str | None:
    """
    Return the vision.yaml field name or prior decision ID that justified
    Fast Path selection, or None if Thorough Path was selected.

    This is the traceability requirement: every Fast Path selection must
    name its anchor so the decision is auditable.

    Args:
        context: Dict with at minimum:
            - situation_class (str): classification of the situation
            - stakes (str): "low" | "high"
            - prior_decisions (list): list of dicts with at least a
              'situation_class' key
            - vision_anchor (str | None): vision.yaml field name, or None

    Returns:
        str | None: The anchor field name or prior decision ID, or None if
        Thorough Path applies.
    """
    stakes = context.get("stakes", "high")
    vision_anchor = context.get("vision_anchor")
    prior_decisions = context.get("prior_decisions", [])
    situation_class = context.get("situation_class", "")

    # Fast Path requires low stakes as a prerequisite
    if stakes != "low":
        return None

    # Prefer vision.yaml anchor if present and non-empty
    if vision_anchor and isinstance(vision_anchor, str) and vision_anchor.strip():
        return vision_anchor

    # Fall back to a prior decision of the same class
    for decision in prior_decisions:
        if isinstance(decision, dict):
            decision_class = decision.get("situation_class", "")
            if decision_class == situation_class:
                decision_id = decision.get("id") or decision.get("decision_id")
                if decision_id:
                    return str(decision_id)
                # Return a synthetic reference if no explicit id
                return f"prior_decision:{decision_class}"

    return None


def select_path(context: dict) -> PathSelection:
    """
    Select the routing path for an OODA loop decision: "fast" or "thorough".

    Fast Path is selected when ALL of the following are true:
    1. stakes == "low"
    2. Either:
       a. vision_anchor is not None and non-empty (traceable to vision.yaml), OR
       b. prior_decisions contains a decision of the same situation_class

    Thorough Path is selected in all other cases:
    - Novel situation (no prior of same class, no vision anchor)
    - High stakes (even if anchor or prior exists)
    - No vision anchor and no matching prior decision

    Args:
        context: Dict with at minimum:
            - situation_class (str): classification of the situation
            - stakes (str): "low" | "high"
            - prior_decisions (list): list of dicts with at least a
              'situation_class' key
            - vision_anchor (str | None): vision.yaml field name, or None

    Returns:
        "fast" | "thorough"
    """
    basis = cite_basis(context)

    if basis is not None:
        log.info(
            "[fast-thorough-selector] Fast Path selected | "
            "situation_class=%s stakes=%s basis=%s",
            context.get("situation_class", "<unknown>"),
            context.get("stakes", "<unknown>"),
            basis,
        )
        return PathSelection.FAST

    log.info(
        "[fast-thorough-selector] Thorough Path selected | "
        "situation_class=%s stakes=%s vision_anchor=%s prior_count=%d",
        context.get("situation_class", "<unknown>"),
        context.get("stakes", "<unknown>"),
        context.get("vision_anchor"),
        len(context.get("prior_decisions", [])),
    )
    return PathSelection.THOROUGH
