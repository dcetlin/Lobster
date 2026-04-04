"""
WOS V3 Germinator — register classification at germination time.

Naming note
-----------
The V3 proposal uses "Cultivator" to describe the pearl-vs-seed classifier that
decides whether a philosophy session output becomes a garden artifact or a GitHub
issue. The existing ``cultivator.py`` module is the *GitHub Issue Cultivator* —
it promotes open GitHub issues into the WOS registry. These are different concerns.

To avoid propagating the naming ambiguity:
- This module is called ``germinator.py`` — it classifies the register of a UoW
  at the moment it is germinated from a GitHub issue into the registry.
- The scheduled job ``github-issue-cultivator`` retains its name — it is an
  established job name in jobs.json.
- New code and docstrings use "Germinator" when referring to register classification.

See docs/WOS-INDEX.md for the full component glossary.

Register classification
-----------------------
Register is the attentional configuration a UoW requires for correct completion
evaluation. Register-mismatch produces coupling failure even when execution
mechanics succeed (root cause of the 0.8% V2 success rate).

The classification algorithm is an ordered gate evaluated at germination time.
Register is **immutable** after germination. If the Steward detects a mismatch
on diagnosis, it surfaces to Dan — it does not reclassify autonomously.

Algorithm (ordered; first matching gate wins):

1. Does the UoW body contain a machine-executable gate command?
   (bash, pytest, make, gh, rg, grep, python, uv, cargo, go, npm)
   YES → operational or iterative-convergent (see gate 2)
   NO  → continue

2. (If gate 1 matched) Does the work require multiple iterations against the gate?
   (keywords: "all", "fix all", "until", "100%", "passing", "clean", "zero")
   YES → iterative-convergent
   NO  → operational

3. Does the UoW originate from a philosophy session, frontier doc, or contain
   vocabulary from Dan's phenomenological register?
   (keywords: poiesis, register, attunement, phenomenology, frontier, pearl,
    aletheia, thrownness, clearing, givenness, dwelling, presencing, autopoiesis)
   YES → philosophical
   NO  → continue

4. Is the success_criteria evaluable without reading the output?
   (heuristic: no hedge words like "appropriate", "good", "well-designed",
    "better", "improve", "consider", "look into")
   NO  → human-judgment
   YES → operational (default)

Usage:
    from src.orchestration.germinator import classify_register

    register = classify_register(title="fix failing tests", body=issue_body)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Register type
# ---------------------------------------------------------------------------

Register = Literal["operational", "iterative-convergent", "philosophical", "human-judgment"]

# ---------------------------------------------------------------------------
# Gate 1 — machine-executable gate command detection
# ---------------------------------------------------------------------------

# Commands that indicate a machine-verifiable gate is present in the issue body.
# Checked as whole words or at start of code blocks to reduce false positives.
_GATE_COMMAND_PATTERNS = [
    r"\bpytest\b",
    r"\bmake\b\s+\w",          # "make test", "make lint", etc.
    r"\bgh\s+(?:pr|issue|run)\b",
    r"\brg\b",
    r"\bgrep\b",
    r"\buv\s+run\b",
    r"\bcargo\s+(?:test|build|check)\b",
    r"\bgo\s+(?:test|build|vet)\b",
    r"\bnpm\s+(?:test|run|build)\b",
    r"```(?:bash|sh|shell)\s",  # fenced code block with bash/sh
    r"\$\s+(?:pytest|make|uv|cargo|npm|go)\b",  # shell prompt style
]

_GATE_COMMAND_RE = re.compile(
    "|".join(_GATE_COMMAND_PATTERNS),
    re.IGNORECASE,
)


def _has_gate_command(text: str) -> bool:
    """Return True if the text contains a machine-executable gate command."""
    return bool(_GATE_COMMAND_RE.search(text))


# ---------------------------------------------------------------------------
# Gate 2 — iterative convergence signal
# ---------------------------------------------------------------------------

# Words/phrases that indicate the work requires multiple cycles to converge.
_ITERATIVE_PATTERNS = [
    r"\bfix\s+all\b",
    r"\ball\s+(?:test|tests|failures|errors|warnings)\b",
    r"\buntil\s+(?:all|100|passing|clean|zero)\b",
    r"\b100\s*%",                # "100%", "100% passing", "100% coverage"
    r"\bpassing\b",              # "make tests passing", "all tests passing"
    r"\bzero\s+(?:error|warning|failure)\b",
    r"\bclean\b",                # "mypy clean", "lint clean"
    r"\bno\s+(?:error|warning|failure)\b",
    r"\bconverge\b",
]

_ITERATIVE_RE = re.compile(
    "|".join(_ITERATIVE_PATTERNS),
    re.IGNORECASE,
)


def _requires_iteration(text: str) -> bool:
    """Return True if the text signals multi-cycle convergence work."""
    return bool(_ITERATIVE_RE.search(text))


# ---------------------------------------------------------------------------
# Gate 3 — philosophical / phenomenological register vocabulary
# ---------------------------------------------------------------------------

# Dan's phenomenological vocabulary. Presence in title or body signals
# philosophical register. This list is conservative — prefer false negatives
# (default to operational) over false positives (misrouting to philosophical).
_PHILOSOPHICAL_TERMS = frozenset({
    "poiesis",
    "attunement",
    "phenomenology",
    "phenomenological",
    "frontier",
    "pearl",
    "aletheia",
    "thrownness",
    "clearing",
    "givenness",
    "dwelling",
    "presencing",
    "autopoiesis",
    "logos",
    "noema",
    "noesis",
    "dasein",
    "weltanschauung",
})

# Structural origin signals — these appear in issue bodies when the issue
# originates from a philosophy session or frontier document.
_PHILOSOPHICAL_ORIGIN_PATTERNS = [
    r"philosophy\s+session",
    r"frontier\s+doc",
    r"pearl\s+candidate",
    r"wos-philosophical",         # label name if present in body
    r"from\s+a\s+(?:dream|vision|reflection)",
]

_PHILOSOPHICAL_ORIGIN_RE = re.compile(
    "|".join(_PHILOSOPHICAL_ORIGIN_PATTERNS),
    re.IGNORECASE,
)


def _is_philosophical(title: str, body: str) -> bool:
    """Return True if the UoW originates from philosophical/phenomenological register."""
    combined = (title + " " + body).lower()
    # Check for phenomenological vocabulary (at least one strong term)
    word_tokens = set(re.findall(r"\b\w+\b", combined))
    vocab_hit = bool(word_tokens & _PHILOSOPHICAL_TERMS)
    # Check for structural origin signals
    origin_hit = bool(_PHILOSOPHICAL_ORIGIN_RE.search(title + " " + body))
    return vocab_hit or origin_hit


# ---------------------------------------------------------------------------
# Gate 4 — human-judgment signal (success_criteria evaluability)
# ---------------------------------------------------------------------------

# Hedge words that indicate the success criteria cannot be evaluated without
# reading the output — i.e., they require human judgment to assess.
_HUMAN_JUDGMENT_PATTERNS = [
    r"\bappropriate\b",
    r"\bwell[- ]designed\b",
    r"\bwell[- ]written\b",
    r"\bgood\b",
    r"\bbetter\b",
    r"\bimprove(?:d|ment)?\b",
    r"\bconsider\b",
    r"\blook\s+into\b",
    r"\bexplore\b",
    r"\bthink\s+about\b",
    r"\breviewed?\b",            # "reviewed and approved" = human judgment
    r"\bapproved?\b",
    r"\bshould\b",               # "should be cleaner" = subjective
    r"\bseems?\b",
]

_HUMAN_JUDGMENT_RE = re.compile(
    "|".join(_HUMAN_JUDGMENT_PATTERNS),
    re.IGNORECASE,
)


def _is_human_judgment(success_criteria: str) -> bool:
    """Return True if the success criteria requires human judgment to evaluate.

    Heuristic: presence of hedge words signals criteria that cannot be evaluated
    by reading an output. Absence of hedge words suggests objective criteria.
    Empty criteria default to human-judgment (no measurable outcome declared).
    """
    if not success_criteria or not success_criteria.strip():
        return True  # No criteria = no machine-observable gate = human judgment
    return bool(_HUMAN_JUDGMENT_RE.search(success_criteria))


# ---------------------------------------------------------------------------
# Classification result — typed, frozen
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RegisterClassification:
    """Result of register classification at germination time.

    register: the classified attentional register.
    gate_matched: which gate fired (1, 2, 3, 4, or "default").
    confidence: "high" | "medium" | "low" — for observability logging.
    rationale: one-sentence explanation of why this register was selected.
    """
    register: Register
    gate_matched: str
    confidence: Literal["high", "medium", "low"]
    rationale: str


# ---------------------------------------------------------------------------
# Main classification function
# ---------------------------------------------------------------------------

def classify_register(
    title: str,
    body: str,
    success_criteria: str = "",
) -> RegisterClassification:
    """
    Classify the register of a UoW at germination time.

    Args:
        title: GitHub issue title.
        body: GitHub issue body (full text).
        success_criteria: Extracted success criteria prose. May be empty.

    Returns:
        RegisterClassification with register, gate_matched, confidence, rationale.

    Register is immutable after germination. The caller is responsible for writing
    the returned register value to the UoW at INSERT time.

    Algorithm (ordered gate — first match wins):
        Gate 1: machine-executable gate command present → operational or iterative
        Gate 2: (if gate 1) iterative convergence signal → iterative-convergent
        Gate 3: philosophical/phenomenological vocabulary → philosophical
        Gate 4: success criteria evaluability → human-judgment or operational
    """
    combined_text = title + "\n" + body

    # Gate 1: machine-executable gate command
    if _has_gate_command(combined_text):
        # Gate 2: does it require multiple iterations?
        if _requires_iteration(combined_text):
            return RegisterClassification(
                register="iterative-convergent",
                gate_matched="2",
                confidence="high",
                rationale=(
                    "Issue body contains a machine-executable gate command and "
                    "signals multi-cycle convergence work."
                ),
            )
        return RegisterClassification(
            register="operational",
            gate_matched="1",
            confidence="high",
            rationale=(
                "Issue body contains a machine-executable gate command with no "
                "iteration signal — single-pass operational work."
            ),
        )

    # Gate 3: philosophical register vocabulary
    if _is_philosophical(title, body):
        return RegisterClassification(
            register="philosophical",
            gate_matched="3",
            confidence="medium",
            rationale=(
                "Issue title or body contains phenomenological vocabulary or "
                "a philosophical origin signal."
            ),
        )

    # Gate 4: success criteria evaluability
    if _is_human_judgment(success_criteria):
        return RegisterClassification(
            register="human-judgment",
            gate_matched="4",
            confidence="medium",
            rationale=(
                "Success criteria contains hedge words or is empty — "
                "cannot be evaluated without human reading."
            ),
        )

    # Default: operational
    return RegisterClassification(
        register="operational",
        gate_matched="default",
        confidence="low",
        rationale=(
            "No gate fired — defaulting to operational. "
            "Steward may surface for reclassification if register mismatch detected."
        ),
    )
