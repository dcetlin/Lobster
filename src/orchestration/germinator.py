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

See docs/wos/current/INDEX.md for the full component glossary.

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
   Unambiguous terms (poiesis, attunement, phenomenology, frontier, aletheia,
   thrownness, lichtung, givenness, dwelling, presencing, autopoiesis, etc.)
   contribute a fixed weight. Ambiguous terms (e.g. "register") only contribute
   weight when philosophical co-terms outnumber technical co-terms in a context
   window around the occurrence. Gate fires when the aggregated score >= 0.3.
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
    # "clearing" was removed — causes false positives on engineering issues mentioning
    # flag-clearing, state-clearing, cache-clearing, etc. Replaced with Heidegger-specific
    # compound and German term that only appear in phenomenological context.
    "lichtung",            # Heidegger's German term for the clearing-of-being
    "ontological clearing", # compound only used in phenomenological discourse
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


# ---------------------------------------------------------------------------
# Gate 3 scoring — co-occurrence disambiguation
# ---------------------------------------------------------------------------

# Score threshold for Gate 3. An unambiguous term alone (weight 0.3) meets it.
# Ambiguous terms require co-occurrence evidence to reach the threshold.
GATE3_THRESHOLD: float = 0.3
_PHILOSOPHICAL_TERM_WEIGHT: float = 0.3  # each unambiguous term fires Gate 3 alone
_AMBIGUOUS_TERM_WEIGHT: float = 0.15     # per philosophical co-term hit for ambiguous terms

# Terms that appear in both philosophical and technical/operational prose.
# They do not contribute directly — they require co-occurrence with
# _PHILOSOPHICAL_CO_TERMS outnumbering _TECHNICAL_CO_TERMS in a context window.
_AMBIGUOUS_TERMS: frozenset[str] = frozenset({
    "register",   # philosophical: "developmental register", "attentional register"
                  # technical:     "register field", "register mismatch gate"
})

# Words that signal a philosophical context near an ambiguous term.
_PHILOSOPHICAL_CO_TERMS: frozenset[str] = frozenset({
    "attunement", "poiesis", "poiema", "frontier", "developmental",
    "phenomenological", "phenomenology", "embodiment", "negentropic",
    "discernment", "coherence", "proprioceptive", "ontological", "epistemic",
    "somatic", "intersubjective", "liminal", "aletheia", "thrownness",
    "givenness", "dwelling", "presencing", "autopoiesis", "dasein",
    "noema", "noesis", "logos", "weltanschauung",
})

# Words that signal a technical context near an ambiguous term.
_TECHNICAL_CO_TERMS: frozenset[str] = frozenset({
    "field", "schema", "gate", "mismatch", "classification",
    "germination", "routing", "dispatch", "executor", "payload",
    "parser", "queue", "json", "yaml", "api", "endpoint",
    "migration", "cron", "scheduler",
})

# Words on each side of an ambiguous term to scan for co-occurrence.
_CO_OCCURRENCE_WINDOW: int = 40


def _philosophical_score(title: str, body: str) -> float:
    """
    Return a confidence score [0.0, 1.0] for philosophical register classification.

    Unambiguous terms from _PHILOSOPHICAL_TERMS each contribute
    _PHILOSOPHICAL_TERM_WEIGHT. Ambiguous terms from _AMBIGUOUS_TERMS
    contribute _AMBIGUOUS_TERM_WEIGHT per philosophical co-term found in a
    context window, but only when philosophical co-term hits outnumber
    technical co-term hits. Origin signals (philosophy session, frontier doc)
    return 1.0 immediately.
    """
    combined_raw = title + " " + body
    if bool(_PHILOSOPHICAL_ORIGIN_RE.search(combined_raw)):
        return 1.0

    combined = combined_raw.lower()
    words = re.findall(r"\b\w+\b", combined)
    word_set = set(words)
    score = 0.0

    single_word_terms = frozenset(t for t in _PHILOSOPHICAL_TERMS if " " not in t)
    multi_word_terms = frozenset(t for t in _PHILOSOPHICAL_TERMS if " " in t)

    for term in single_word_terms:
        if term in word_set:
            score += _PHILOSOPHICAL_TERM_WEIGHT

    for phrase in multi_word_terms:
        if phrase in combined:
            score += _PHILOSOPHICAL_TERM_WEIGHT

    for i, word in enumerate(words):
        if word in _AMBIGUOUS_TERMS:
            window_start = max(0, i - _CO_OCCURRENCE_WINDOW)
            window_end = min(len(words), i + _CO_OCCURRENCE_WINDOW)
            window = set(words[window_start:window_end])
            phil_hits = len(window & _PHILOSOPHICAL_CO_TERMS)
            tech_hits = len(window & _TECHNICAL_CO_TERMS)
            if phil_hits > tech_hits:
                score += _AMBIGUOUS_TERM_WEIGHT * phil_hits

    return min(score, 1.0)


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

    # Gate 3: philosophical register vocabulary (co-occurrence scored)
    if _philosophical_score(title, body) >= GATE3_THRESHOLD:
        return RegisterClassification(
            register="philosophical",
            gate_matched="3",
            confidence="medium",
            rationale=(
                "Issue title or body scored at or above the philosophical threshold — "
                "contains phenomenological vocabulary or a philosophical origin signal."
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
