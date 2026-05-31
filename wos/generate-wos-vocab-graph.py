"""
generate-wos-vocab-graph.py

Generates the §4 Load-Bearing Abstractions vocabulary graph for the WOS evolution spec.
Extracts the 12 terms from §4, models them as a D3 force-directed graph, and
renders to bisque via the htmlgen renderer.

Run from ~/lobster/:
    uv run wos/generate-wos-vocab-graph.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Repo root on sys.path (needed for src.htmlgen imports via renderer)
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.htmlgen.renderer import render_and_upload  # noqa: E402


# ---------------------------------------------------------------------------
# §4 Load-Bearing Abstractions — node model
#
# Tier assignments:
#   register  — top-level architectural concepts (the structural load-bearers)
#   param     — components that belong to / parametrize a register concept
#   cross     — failure modes or cross-cutting concerns
#
# Color palette (from component STANDARDS):
#   register: #6ea8fe  (blue)
#   param:    #7dd3fc  (light blue)
#   cross:    #86efac  (green)
# ---------------------------------------------------------------------------

NODES = [
    # ── Core execution concepts (register tier)
    {
        "id": "Prescription",
        "tier": "register",
        "color": "#6ea8fe",
        "def": "Typed PrescriptionObject output by Prescriber (not prose string). The structured signal that drives executor behavior.",
        "examples": [
            {"domain": "WOS", "text": "Prescriber emits a typed object with register targets and hypothesis type — not a freeform instruction string."}
        ],
    },
    {
        "id": "Verdict",
        "tier": "register",
        "color": "#6ea8fe",
        "def": "Scored outcome for a specific hypothesis after a UoW closes. The fundamental unit of feedback in the learning loop.",
        "examples": [
            {"domain": "WOS", "text": "After a UoW completes, the steward scores the hypothesis: success, failure, or partial."}
        ],
    },
    {
        "id": "Verdict Accumulator",
        "tier": "register",
        "color": "#6ea8fe",
        "def": "Persistent store mapping (register, hypothesis) → (n_successes, n_failures, n_partial). The memory substrate for learning.",
        "examples": [
            {"domain": "WOS", "text": "Each hypothesis type accumulates verdict history across all UoWs in that register."}
        ],
    },
    {
        "id": "Selector",
        "tier": "register",
        "color": "#6ea8fe",
        "def": "Component that reads the Verdict Accumulator and biases Prescriber toward high-accuracy hypothesis types.",
        "examples": [
            {"domain": "WOS", "text": "If 'decompose' hypotheses score 80% success in the code register, Selector upweights that type."}
        ],
    },
    # ── Portfolio / governance concepts (register tier)
    {
        "id": "Portfolio Prescription",
        "tier": "register",
        "color": "#a78bfa",
        "def": "Governor output: workstream emphasis weights + register targets. Portfolio-level, not UoW-level.",
        "examples": [
            {"domain": "WOS", "text": "Governor sets 60% capacity toward infra register, 40% toward product — Prescriber reads this when selecting next UoW."}
        ],
    },
    {
        "id": "Lever",
        "tier": "register",
        "color": "#a78bfa",
        "def": "Category of improvement action. Two kinds: scaffold lever (IFTTT/config) and learning lever (selector biasing).",
        "examples": [
            {"domain": "WOS", "text": "A scaffold lever adds an IFTTT rule. A learning lever reweights Selector output for a register."}
        ],
    },
    {
        "id": "Executor Mesh",
        "tier": "register",
        "color": "#f9a8d4",
        "def": "Multi-tier execution population: Tier 1 (ephemeral), Tier 2 (persistent domain), Tier 3 (external).",
        "examples": [
            {"domain": "WOS", "text": "A Tier 2 executor holds domain context across sessions; a Tier 1 executor is spun up and torn down per UoW."}
        ],
    },
    # ── Param tier — parametrize their parent register concepts
    {
        "id": "Germination Bias",
        "tier": "param",
        "register": "Portfolio Prescription",
        "color": "#7dd3fc",
        "def": "Per-workstream multiplier on issue promotion speed, derived from Portfolio Prescription.",
        "examples": [
            {"domain": "WOS", "text": "A workstream with high Portfolio emphasis gets a larger Germination Bias, surfacing its issues faster."}
        ],
    },
    {
        "id": "Class A/B Amendment",
        "tier": "param",
        "register": "Lever",
        "color": "#7dd3fc",
        "def": "Class A = auto-apply within bounded scope. Class B = requires Dan's explicit approval before applying.",
        "examples": [
            {"domain": "WOS", "text": "Adding an IFTTT rule for a known pattern is Class A. Changing the Prescriber weighting schema is Class B."}
        ],
    },
    {
        "id": "Claim Protocol",
        "tier": "param",
        "register": "Executor Mesh",
        "color": "#7dd3fc",
        "def": "Typed interface for executor capability matching and UoW claiming. Governs how UoWs are assigned to executors.",
        "examples": [
            {"domain": "WOS", "text": "An executor declares its capability set; the Claim Protocol matches UoW requirements against declared capabilities."}
        ],
    },
    {
        "id": "Capacity Event",
        "tier": "param",
        "register": "Executor Mesh",
        "color": "#7dd3fc",
        "def": "wos_capacity_available inbox message fired when an executor slot frees. Triggers next UoW dispatch.",
        "examples": [
            {"domain": "WOS", "text": "When a Tier 1 executor closes, it fires Capacity Event; the dispatcher polls for pending UoWs to fill the slot."}
        ],
    },
    # ── Cross tier — failure mode / cross-cutting concern
    {
        "id": "Coupled Goodhart",
        "tier": "cross",
        "color": "#86efac",
        "def": "Failure mode: Prescriber and Executor optimize against the same verdict context, reaching a Nash equilibrium rather than true improvement.",
        "examples": [
            {"domain": "WOS", "text": "Prescriber biases toward fast-completing UoWs because Executor scores them highest — metric gaming, not real progress."}
        ],
    },
]

# ---------------------------------------------------------------------------
# Edges — typed relationships between nodes
#
# Link types from the component schema:
#   param  — parametrizes / belongs to a register concept
#   cross  — cross-register relationship (failure mode, dependency)
#   hub    — structural flow (A → B in the execution pipeline)
# ---------------------------------------------------------------------------

LINKS = [
    # Verdict flow: Prescription → Verdict → Verdict Accumulator → Selector → Prescription (loop)
    {"source": "Prescription", "target": "Verdict", "type": "hub"},
    {"source": "Verdict", "target": "Verdict Accumulator", "type": "hub"},
    {"source": "Verdict Accumulator", "target": "Selector", "type": "hub"},
    {"source": "Selector", "target": "Prescription", "type": "hub"},

    # Portfolio → Prescription → Executor Mesh (governance chain)
    {"source": "Portfolio Prescription", "target": "Prescription", "type": "hub"},
    {"source": "Prescription", "target": "Executor Mesh", "type": "hub"},

    # Param links (param nodes → their parent register concept)
    {"source": "Germination Bias", "target": "Portfolio Prescription", "type": "param"},
    {"source": "Class A/B Amendment", "target": "Lever", "type": "param"},
    {"source": "Claim Protocol", "target": "Executor Mesh", "type": "param"},
    {"source": "Capacity Event", "target": "Executor Mesh", "type": "param"},

    # Lever connects to Prescription (levers amend the prescription/selector loop)
    {"source": "Lever", "target": "Selector", "type": "hub"},

    # Coupled Goodhart: cross-cutting failure involving Prescriber + Verdict Accumulator
    {"source": "Coupled Goodhart", "target": "Prescription", "type": "cross"},
    {"source": "Coupled Goodhart", "target": "Verdict Accumulator", "type": "cross"},
]

# ---------------------------------------------------------------------------
# Content manifest for the renderer
# ---------------------------------------------------------------------------

MANIFEST = {
    "doc_id": "wos-vocab-graph-s4",
    "title": "WOS Evolution Spec — §4 Load-Bearing Abstractions",
    "subtitle": "Force-directed vocabulary graph of the 12 locked abstractions",
    "version": "1.0",
    "updated_at": "2026-05-30",
    "template_id": "document-class",
    "comment_system": False,
    "sections": [
        {
            "id": "intro",
            "label": "§4",
            "title": "Load-Bearing Abstractions",
            "content": (
                "The 12 terms below are **status: LOCKED** in the WOS Evolution Spec §4. "
                "Each node in the graph represents one abstraction. "
                "Hover or click a node to see its definition. "
                "Use the filter buttons to narrow to register-tier concepts only or include parameter nodes.\n\n"
                "**Node tiers:**\n\n"
                "- **Concepts** (large circles) — top-level architectural abstractions\n"
                "- **Parameters** (medium circles) — components that parametrize a concept\n"
                "- **Cross-register** (dashed circles) — failure modes or cross-cutting concerns\n\n"
                "**Edge types:**\n\n"
                "- Solid lines — pipeline flow (hub)\n"
                "- Light solid — parameter relationship\n"
                "- Dashed — cross-register dependency"
            ),
            "content_format": "markdown",
        },
    ],
    "components": [
        {
            "id": "d3-vocabulary-network",
            "config": {
                "nodes": NODES,
                "links": LINKS,
            },
        },
        {
            "id": "theme-toggle",
            "config": {"mode": "js-toggle"},
        },
    ],
}


def main() -> None:
    manifest_path = Path(__file__).parent / "wos-vocab-graph-manifest.json"
    manifest_path.write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    print(f"Manifest written: {manifest_path}")

    url = render_and_upload(
        content_path=manifest_path,
        template_id="document-class",
        output_filename="wos-vocab-graph-s4.html",
    )
    print(f"Rendered: {url}")


if __name__ == "__main__":
    main()
