"""
Vision-anchored routing for WOS UoWs.

This module provides the consumption pathway for vision_ref, completing the
loop from vision.yaml → issue-sweeper population → routing decisions.

The resolve_vision_route function reads vision_ref from a UoW and produces
a vision-anchored route_reason. When vision_ref is null, the fallback is
explicit and logged (never silent).

Phase 1 success criteria addressed:
- sc-3: vision_ref content surfaced in morning briefing staleness check
- sc-4: disabling vision_ref produces measurably different routing outcomes

Usage:
    from src.orchestration.vision_routing import resolve_vision_route

    result = resolve_vision_route(uow)
    # result.route_reason: vision-anchored or fallback reason
    # result.anchored: True if vision_ref was used
    # result.fallback_logged: True if null fallback was triggered
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestration.registry import UoW

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Route reason prefixes — distinguish vision-anchored from heuristic routing
# ---------------------------------------------------------------------------

# Prefix for vision-anchored route_reason values
VISION_ROUTE_PREFIX = "vision-anchored"

# Prefix for fallback (no vision_ref) route_reason values
FALLBACK_ROUTE_PREFIX = "heuristic-fallback"


# ---------------------------------------------------------------------------
# VisionRouteResult — typed result for routing decisions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class VisionRouteResult:
    """Result of vision-based routing.

    route_reason: The routing reason string, suitable for writing to the UoW.
    anchored: True if vision_ref was present and used for routing.
    fallback_logged: True if the null fallback path was logged.
    vision_layer: The layer from vision_ref (current_focus, active_project, core), or None.
    vision_field: The field from vision_ref, or None.
    stale: True if the vision anchor is older than the staleness threshold for its layer.
    """
    route_reason: str
    anchored: bool
    fallback_logged: bool
    vision_layer: str | None = None
    vision_field: str | None = None
    stale: bool = False


# ---------------------------------------------------------------------------
# Staleness thresholds (days) — aligned with vision-object.md
# ---------------------------------------------------------------------------

STALENESS_THRESHOLDS = {
    "current_focus": 7,
    "active_project": 30,
    "core": 90,
}


def _check_anchor_staleness(anchored_at: str | None, layer: str) -> bool:
    """Check if the vision anchor is stale based on its layer's threshold.

    Returns True if anchored_at is older than the layer's staleness threshold.
    """
    if anchored_at is None:
        return True  # Missing anchor timestamp treated as stale

    threshold_days = STALENESS_THRESHOLDS.get(layer, 30)

    try:
        # Parse ISO timestamp — handle both Z and +00:00 formats
        if anchored_at.endswith("Z"):
            anchored_at = anchored_at[:-1] + "+00:00"
        anchor_dt = datetime.fromisoformat(anchored_at)
        now = datetime.now(timezone.utc)
        age_days = (now - anchor_dt).days
        return age_days > threshold_days
    except (ValueError, TypeError):
        logger.warning(
            "vision_routing: invalid anchored_at timestamp %r, treating as stale",
            anchored_at,
        )
        return True


# ---------------------------------------------------------------------------
# Main routing function
# ---------------------------------------------------------------------------

def resolve_vision_route(
    uow: "UoW",
    *,
    log_fallback: bool = True,
) -> VisionRouteResult:
    """
    Resolve a vision-anchored route_reason for a UoW.

    Reads vision_ref from the UoW. When present and valid, produces a
    vision-anchored route_reason referencing the vision layer and field.
    When vision_ref is null or invalid, produces an explicit fallback
    reason and logs the fallback (unless log_fallback=False for testing).

    Args:
        uow: The Unit of Work to route.
        log_fallback: Whether to log when falling back to heuristic routing.

    Returns:
        VisionRouteResult with:
        - route_reason: vision-anchored or fallback reason string
        - anchored: True if vision_ref was used
        - fallback_logged: True if fallback was logged
        - vision_layer/vision_field: extracted from vision_ref, or None
        - stale: True if the vision anchor exceeds staleness threshold
    """
    vision_ref = uow.vision_ref

    # Fallback path: vision_ref is null or not a dict
    if vision_ref is None or not isinstance(vision_ref, dict):
        fallback_reason = (
            f"{FALLBACK_ROUTE_PREFIX}: vision_ref null — "
            f"UoW {uow.id} routed by register ({uow.register})"
        )
        if log_fallback:
            logger.info(
                "vision_routing: UoW %s has no vision_ref, falling back to heuristic routing",
                uow.id,
            )
        return VisionRouteResult(
            route_reason=fallback_reason,
            anchored=False,
            fallback_logged=log_fallback,
        )

    # Extract vision_ref fields
    layer = vision_ref.get("layer")
    field = vision_ref.get("field")
    statement = vision_ref.get("statement")
    anchored_at = vision_ref.get("anchored_at")

    # Validate required fields
    if not layer or not field:
        fallback_reason = (
            f"{FALLBACK_ROUTE_PREFIX}: vision_ref malformed (missing layer/field) — "
            f"UoW {uow.id} routed by register ({uow.register})"
        )
        if log_fallback:
            logger.warning(
                "vision_routing: UoW %s has malformed vision_ref %r, falling back to heuristic",
                uow.id,
                vision_ref,
            )
        return VisionRouteResult(
            route_reason=fallback_reason,
            anchored=False,
            fallback_logged=log_fallback,
        )

    # Check staleness
    stale = _check_anchor_staleness(anchored_at, layer)

    # Build vision-anchored route_reason
    # Format: "vision-anchored: vision.{layer}.{field} — {statement_excerpt}"
    statement_excerpt = ""
    if statement:
        # Truncate statement to first 60 chars for readability
        statement_excerpt = statement[:60].replace("\n", " ").strip()
        if len(statement) > 60:
            statement_excerpt += "..."

    route_reason = f"{VISION_ROUTE_PREFIX}: vision.{layer}.{field}"
    if statement_excerpt:
        route_reason += f" — {statement_excerpt}"

    if stale:
        route_reason += " [STALE]"
        logger.warning(
            "vision_routing: UoW %s has stale vision anchor (layer=%s, anchored_at=%s)",
            uow.id,
            layer,
            anchored_at,
        )

    return VisionRouteResult(
        route_reason=route_reason,
        anchored=True,
        fallback_logged=False,
        vision_layer=layer,
        vision_field=field,
        stale=stale,
    )


# ---------------------------------------------------------------------------
# Morning briefing helper — surface vision_ref staleness
# ---------------------------------------------------------------------------

def check_vision_ref_staleness(uows: list["UoW"]) -> list[dict]:
    """
    Check a list of UoWs for stale vision anchors.

    Returns a list of dicts describing stale anchors, suitable for
    morning briefing output.

    Used by morning-briefing.md to satisfy Phase 1 success criterion sc-3.
    """
    stale_anchors = []

    for uow in uows:
        if uow.vision_ref is None:
            continue

        vision_ref = uow.vision_ref
        if not isinstance(vision_ref, dict):
            continue

        layer = vision_ref.get("layer", "unknown")
        field = vision_ref.get("field", "unknown")
        anchored_at = vision_ref.get("anchored_at")

        if _check_anchor_staleness(anchored_at, layer):
            threshold = STALENESS_THRESHOLDS.get(layer, 30)
            stale_anchors.append({
                "uow_id": uow.id,
                "summary": uow.summary[:50],
                "vision_layer": layer,
                "vision_field": field,
                "anchored_at": anchored_at,
                "threshold_days": threshold,
            })

    return stale_anchors


def format_staleness_report(stale_anchors: list[dict]) -> str:
    """
    Format a staleness report for morning briefing output.

    Returns a human-readable string summarizing stale vision anchors.
    """
    if not stale_anchors:
        return "No stale vision anchors found."

    lines = [f"**Stale Vision Anchors** ({len(stale_anchors)} found):"]
    for anchor in stale_anchors[:5]:  # Limit to first 5
        lines.append(
            f"- {anchor['uow_id']}: vision.{anchor['vision_layer']}.{anchor['vision_field']} "
            f"(anchored {anchor['anchored_at']}, threshold {anchor['threshold_days']}d)"
        )

    if len(stale_anchors) > 5:
        lines.append(f"... and {len(stale_anchors) - 5} more")

    return "\n".join(lines)
