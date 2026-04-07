"""
src/memory/attunement.py — Attunement Over Assumption evaluation for subagent results

Implements the Attunement Over Assumption principle from the dan-reviewer posture:

    "The presenting problem is rarely the problem. Look past surfaces — whether
    in directives, bugs, or code — to underlying intent and cause. Find root
    causes, not surface symptoms. State what layer the symptom is at and what
    layer the cause is likely at — before proposing a fix."

Primary operation:

    evaluate_attunement(result_text, original_request?) -> AttunementResult

Called by the dispatcher during the Result Evaluation hook (Epistemic Hook 3)
before relaying a subagent_result to the user. If the result addresses only
the surface layer, the dispatcher prepends an annotation before relay.

Pure-logic function — no I/O side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

DEPTH_VALUES = frozenset({"surface_only", "causal", "both", "indeterminate"})

# Layers where symptoms and causes can live. Ordered from outermost to deepest.
LAYERS = (
    "presentation",   # UI, message formatting, user-facing text
    "behavior",       # runtime behavior, feature logic, observable effects
    "integration",    # how components connect, API contracts, data flow
    "architecture",   # structural decisions, module boundaries, patterns
    "intent",         # why the system exists, what problem it solves
)


@dataclass
class AttunementResult:
    """Result of evaluating a subagent result for causal depth."""

    depth: str               # one of DEPTH_VALUES
    symptom_layer: str       # which LAYER the addressed symptom is at
    likely_cause_layer: str  # which LAYER the root cause likely lives at
    reason: str              # one-sentence explanation
    annotation: str = ""     # prepend this to relay if surface_only

    def needs_annotation(self) -> bool:
        return self.depth == "surface_only"


# ---------------------------------------------------------------------------
# Signal vocabulary
#
# These are token-level signals, not semantic analysis. They indicate whether
# the result text *discusses* surface vs. causal layers. A result that uses
# causal vocabulary may still be wrong about the cause — but a result with
# zero causal vocabulary is almost certainly surface-only.
# ---------------------------------------------------------------------------

# Phrases indicating the result addresses root causes or underlying structure
_CAUSAL_PHRASES: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\broot\s+cause\b",
        r"\bunderlying\b",
        r"\bfundamental(?:ly)?\b",
        r"\barchitectur(?:e|al)\b",
        r"\bstructural(?:ly)?\b",
        r"\bdesign\s+(?:issue|flaw|problem|decision)\b",
        r"\bbecause\s+the\b",
        r"\bthe\s+(?:real|actual|deeper)\s+(?:issue|problem|cause)\b",
        r"\bthis\s+(?:happens|occurs|fails)\s+because\b",
        r"\bthe\s+reason\b",
        r"\bupstream\b",
        r"\bsource\s+of\b",
        r"\bwhy\s+(?:this|it|the)\b",
    ]
]

# Phrases indicating the result addresses only surface symptoms
_SURFACE_PHRASES: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bworkaround\b",
        r"\bquick\s+fix\b",
        r"\btemporary\s+(?:fix|patch|solution)\b",
        r"\bband[\s-]?aid\b",
        r"\bhotfix\b",
        r"\bpatched?\b",
        r"\bfor\s+now\b",
        r"\bshort[\s-]?term\b",
        r"\bjust\s+(?:change|update|fix|set|add|remove)\b",
        r"\bsymptom\b",
    ]
]

# Phrases indicating explicit layer attribution (strong causal signal)
_LAYER_ATTRIBUTION_PHRASES: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\b(?:the\s+)?(?:symptom|issue|problem)\s+(?:is\s+)?(?:at|in)\s+the\b",
        r"\b(?:the\s+)?(?:cause|root)\s+(?:is\s+)?(?:at|in)\s+the\b",
        r"\b(?:surface|presentation|behavior|integration|architecture|intent)\s+layer\b",
        r"\bthis\s+is\s+a\s+(?:surface|structural|architectural|design|behavioral)\b",
    ]
]


def _count_matches(text: str, patterns: list[re.Pattern]) -> int:
    """Count how many distinct patterns match anywhere in the text."""
    return sum(1 for p in patterns if p.search(text))


# ---------------------------------------------------------------------------
# Layer inference
# ---------------------------------------------------------------------------

# Keywords associated with each layer — used to infer which layer the result
# is discussing. Not exhaustive; just enough to distinguish layers.
_LAYER_KEYWORDS: dict[str, list[str]] = {
    "presentation": [
        "message", "display", "format", "text", "ui", "output", "render",
        "label", "string", "template",
    ],
    "behavior": [
        "function", "method", "logic", "condition", "branch", "return",
        "parameter", "argument", "value", "error", "exception", "bug",
    ],
    "integration": [
        "api", "endpoint", "contract", "interface", "protocol", "schema",
        "connection", "request", "response", "handler", "route", "dispatch",
    ],
    "architecture": [
        "module", "component", "pattern", "design", "structure", "abstraction",
        "dependency", "coupling", "boundary", "layer", "system", "pipeline",
    ],
    "intent": [
        "purpose", "goal", "requirement", "need", "user wants", "the point",
        "objective", "why we", "mission", "use case",
    ],
}


def _infer_layer(text: str) -> str:
    """Infer the most-discussed layer from keyword frequency.

    Returns the layer with the highest keyword hit count.
    Defaults to 'behavior' when no strong signal.
    """
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for layer, keywords in _LAYER_KEYWORDS.items():
        scores[layer] = sum(1 for kw in keywords if kw in text_lower)

    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    if scores[best] == 0:
        return "behavior"
    return best


def _infer_cause_layer(symptom_layer: str, has_causal_signals: bool) -> str:
    """Given the symptom layer and whether causal analysis is present,
    infer where the root cause likely lives.

    Heuristic: causes tend to live one or two layers deeper than symptoms.
    If the result already provides causal analysis, trust the symptom layer
    (the result is already looking deeper). If surface-only, the cause is
    likely one layer deeper.
    """
    if has_causal_signals:
        return symptom_layer

    idx = LAYERS.index(symptom_layer) if symptom_layer in LAYERS else 1
    deeper = min(idx + 1, len(LAYERS) - 1)
    return LAYERS[deeper]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_attunement(
    result_text: str,
    original_request: str = "",
) -> AttunementResult:
    """Evaluate a subagent result for causal depth before relay.

    Args:
        result_text: The text content of the subagent_result message.
        original_request: Optional — the original user request that spawned
            the subagent. When provided, enables detection of request/result
            register mismatch (e.g., user asked "why" but result only says "what").

    Returns:
        AttunementResult with depth classification, layer attribution, and
        an annotation string to prepend if the result is surface-only.
    """
    if not result_text or not result_text.strip():
        return AttunementResult(
            depth="indeterminate",
            symptom_layer="behavior",
            likely_cause_layer="behavior",
            reason="Empty result text — cannot evaluate attunement.",
        )

    causal_count = _count_matches(result_text, _CAUSAL_PHRASES)
    surface_count = _count_matches(result_text, _SURFACE_PHRASES)
    layer_attr_count = _count_matches(result_text, _LAYER_ATTRIBUTION_PHRASES)

    has_causal = causal_count > 0 or layer_attr_count > 0
    has_surface = surface_count > 0

    # Check if the original request asked "why" but the result only says "what"
    why_requested = False
    if original_request:
        why_requested = bool(re.search(
            r"\bwhy\b|\broot\s+cause\b|\bwhat\s+(?:caused|causes)\b|\breason\b",
            original_request, re.IGNORECASE,
        ))

    symptom_layer = _infer_layer(result_text)
    likely_cause_layer = _infer_cause_layer(symptom_layer, has_causal)

    # Classify depth
    if has_causal and has_surface:
        depth = "both"
        reason = (
            f"Result addresses both surface ({surface_count} signal(s)) "
            f"and causal ({causal_count} signal(s)) layers."
        )
    elif has_causal:
        depth = "causal"
        reason = (
            f"Result provides causal analysis ({causal_count} signal(s), "
            f"{layer_attr_count} layer attribution(s))."
        )
    elif has_surface:
        depth = "surface_only"
        reason = (
            f"Result uses surface-fix vocabulary ({surface_count} signal(s)) "
            f"with no causal analysis."
        )
    elif why_requested:
        depth = "surface_only"
        reason = (
            "Original request asked 'why' but result provides no causal analysis."
        )
    else:
        depth = "indeterminate"
        reason = "No strong surface or causal signals detected."

    # Build annotation for surface-only results
    annotation = ""
    if depth == "surface_only":
        annotation = (
            f"[Surface addressed. Causal layer may need investigation: "
            f"symptom at {symptom_layer} layer, "
            f"cause likely at {likely_cause_layer} layer.]"
        )

    return AttunementResult(
        depth=depth,
        symptom_layer=symptom_layer,
        likely_cause_layer=likely_cause_layer,
        reason=reason,
        annotation=annotation,
    )
