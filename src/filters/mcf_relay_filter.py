"""
src/filters/mcf_relay_filter.py — Minimal Cognitive Friction relay filter

Applies the MCF principle from the dan-reviewer posture to outgoing responses:

    "The reader should never need to backtrack. Not because the content is
    simplified, but because the structure tracks the reader's natural
    inference path."

This module provides a pure-function filter that analyzes outgoing text for
MCF friction signals. It does NOT rewrite text — it returns diagnostics that
the dispatcher can use to decide whether to restructure before sending.

Five friction signals are checked:

1. **Buried lead** — Key signal (action, answer, verdict) not in the first
   paragraph. Mobile readers see paragraph 1 first; burying the lead forces
   scrolling before comprehension.

2. **Forward reference** — A term, acronym, or concept used before it is
   introduced. Forces the reader to hold an unresolved token until the
   definition appears.

3. **Non-parallel list** — Bullet/numbered list items with inconsistent
   grammatical structure. Parallel structure lets the reader predict the shape
   of each item; broken parallelism forces re-parsing.

4. **Conclusion before evidence** — A recommendation, verdict, or action item
   placed before the reasoning that justifies it. The reader must accept the
   conclusion on faith, then retroactively validate.

5. **Wall of text** — A paragraph exceeding a length threshold without any
   structural break (bullets, headings, line breaks). Forces the reader to
   hold the entire block in working memory.

Usage:
    from filters.mcf_relay_filter import check_mcf, MCFResult

    result = check_mcf(response_text)
    if result.has_friction:
        # Log or restructure before sending
        for signal in result.signals:
            print(f"[{signal.kind}] {signal.description}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

SIGNAL_KINDS = frozenset({
    "buried_lead",
    "forward_reference",
    "non_parallel_list",
    "conclusion_before_evidence",
    "wall_of_text",
})


@dataclass
class FrictionSignal:
    """A single MCF friction signal detected in the text."""

    kind: str          # one of SIGNAL_KINDS
    description: str   # human-readable explanation
    location: str = "" # e.g. "paragraph 3", "bullet list at line 12"

    def __post_init__(self) -> None:
        if self.kind not in SIGNAL_KINDS:
            raise ValueError(f"Unknown signal kind: {self.kind!r}")


@dataclass
class MCFResult:
    """Result of an MCF check on outgoing text."""

    signals: list[FrictionSignal] = field(default_factory=list)

    @property
    def has_friction(self) -> bool:
        return len(self.signals) > 0

    @property
    def signal_kinds(self) -> set[str]:
        return {s.kind for s in self.signals}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

# Action / verdict / answer words that signal the "lead" of a response
_LEAD_WORDS = re.compile(
    r"\b(done|fixed|merged|deployed|approved|rejected|blocked|answer|verdict|"
    r"result|summary|recommendation|action|yes|no|confirmed|denied|error|"
    r"success|failed|complete|ready|shipped)\b",
    re.IGNORECASE,
)

# Conclusion/recommendation language
_CONCLUSION_WORDS = re.compile(
    r"\b(recommend|should|must|suggest|propose|verdict|decision|conclusion|"
    r"therefore|thus|hence|accordingly|in\s+summary)\b",
    re.IGNORECASE,
)

# Evidence / reasoning language
_EVIDENCE_WORDS = re.compile(
    r"\b(because|since|given\s+that|the\s+reason|evidence|data\s+shows|"
    r"analysis|investigation|found\s+that|observed|looking\s+at|"
    r"examining|reviewing)\b",
    re.IGNORECASE,
)

# Acronym pattern: 2+ uppercase letters, possibly with digits
_ACRONYM_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]{1,10}\b")

# Wall of text threshold (characters per paragraph)
_WALL_THRESHOLD = 600

# Minimum response length to bother checking (very short replies are fine)
_MIN_CHECK_LENGTH = 150


def _split_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs (blocks separated by blank lines)."""
    blocks = re.split(r"\n\s*\n", text.strip())
    return [b.strip() for b in blocks if b.strip()]


def _extract_list_items(text: str) -> list[tuple[int, list[str]]]:
    """Extract bullet/numbered list blocks with their starting line numbers.

    Returns a list of (start_line, [item_texts]) tuples. Each tuple represents
    a contiguous list block.
    """
    lines = text.split("\n")
    list_pattern = re.compile(r"^\s*(?:[-*+]|\d+[.)]) (.+)")

    blocks: list[tuple[int, list[str]]] = []
    current_items: list[str] = []
    current_start = 0
    in_list = False

    for i, line in enumerate(lines):
        match = list_pattern.match(line)
        if match:
            if not in_list:
                current_start = i + 1  # 1-indexed
                current_items = []
                in_list = True
            current_items.append(match.group(1).strip())
        else:
            if in_list and current_items:
                blocks.append((current_start, current_items))
            current_items = []
            in_list = False

    # Flush last block
    if in_list and current_items:
        blocks.append((current_start, current_items))

    return blocks


# ---------------------------------------------------------------------------
# Individual signal detectors
# ---------------------------------------------------------------------------


def _check_buried_lead(paragraphs: list[str]) -> FrictionSignal | None:
    """Check if the key signal is buried past paragraph 1.

    Only fires when:
    - There are 3+ paragraphs
    - Paragraph 1 has no lead words
    - A later paragraph does
    """
    if len(paragraphs) < 3:
        return None

    first_has_lead = bool(_LEAD_WORDS.search(paragraphs[0]))
    if first_has_lead:
        return None

    for i, para in enumerate(paragraphs[1:], start=2):
        if _LEAD_WORDS.search(para):
            return FrictionSignal(
                kind="buried_lead",
                description=(
                    f"Key signal appears in paragraph {i} but not paragraph 1. "
                    "Mobile readers see paragraph 1 first."
                ),
                location=f"paragraph {i}",
            )

    return None


def _check_forward_references(text: str) -> FrictionSignal | None:
    """Check if acronyms are used before being defined.

    Looks for patterns like "MCF" appearing before "Minimal Cognitive Friction (MCF)"
    or "MCF: Minimal Cognitive Friction" definitions.
    """
    # Find all acronyms and their first occurrence position
    acronyms: dict[str, int] = {}
    for match in _ACRONYM_PATTERN.finditer(text):
        acr = match.group()
        # Skip common acronyms that don't need definition
        if acr in {"PR", "CI", "CD", "API", "URL", "DB", "ID", "OK", "UI",
                    "UX", "AWS", "GCP", "SQL", "SSH", "HTTP", "HTTPS",
                    "JSON", "YAML", "CSS", "HTML", "DNS", "TCP", "UDP",
                    "CPU", "GPU", "RAM", "SSD", "EOF", "MCP", "CLI", "SDK",
                    "TODO", "README", "UTC", "ET", "EST", "EDT", "PDF",
                    "GMT", "ISO", "ASCII", "UTF"}:
            continue
        if acr not in acronyms:
            acronyms[acr] = match.start()

    # Check if any acronym appears before its definition
    for acr, first_use in acronyms.items():
        # Look for definition patterns: "Full Name (ACR)" or "ACR: Full Name"
        defn_parens = re.search(
            rf"\([^)]*{re.escape(acr)}[^)]*\)",
            text,
        )
        defn_colon = re.search(
            rf"\b{re.escape(acr)}\s*[:—–-]\s*[A-Z]",
            text,
        )

        if defn_parens and defn_parens.start() > first_use:
            return FrictionSignal(
                kind="forward_reference",
                description=(
                    f"Acronym '{acr}' is used before its definition. "
                    "Reader must hold an unresolved token."
                ),
                location=f"first use at position {first_use}",
            )
        if defn_colon and defn_colon.start() > first_use:
            return FrictionSignal(
                kind="forward_reference",
                description=(
                    f"Acronym '{acr}' is used before its definition. "
                    "Reader must hold an unresolved token."
                ),
                location=f"first use at position {first_use}",
            )

    return None


def _check_non_parallel_lists(text: str) -> FrictionSignal | None:
    """Check if list items within a block have inconsistent grammatical structure.

    Heuristic: items in the same list should start with the same part of speech
    pattern. We approximate by checking if items start with verbs (imperative)
    vs nouns vs other patterns. If >1 item deviates from the majority pattern,
    flag it.
    """
    blocks = _extract_list_items(text)

    for start_line, items in blocks:
        if len(items) < 3:
            continue

        # Classify first word of each item
        patterns: list[str] = []
        for item in items:
            first_word = item.split()[0].lower().rstrip("s") if item.split() else ""
            # Rough heuristic: common verb endings
            if first_word.endswith(("ed", "ing", "ize", "ify", "ate")):
                patterns.append("verb")
            elif first_word in {"add", "fix", "run", "set", "get", "use", "check",
                                "read", "write", "send", "move", "make", "find",
                                "test", "build", "deploy", "create", "update",
                                "delete", "remove", "install", "configure", "ensure",
                                "verify", "validate", "implement", "refactor"}:
                patterns.append("verb")
            elif first_word[0:1].isupper() if first_word else False:
                patterns.append("noun")
            else:
                patterns.append("other")

        if not patterns:
            continue

        # Find majority pattern
        from collections import Counter
        counts = Counter(patterns)
        majority, majority_count = counts.most_common(1)[0]
        deviation_count = len(patterns) - majority_count

        if deviation_count >= 2 and deviation_count / len(patterns) > 0.3:
            return FrictionSignal(
                kind="non_parallel_list",
                description=(
                    f"List at line {start_line} has mixed grammatical structure "
                    f"({deviation_count}/{len(items)} items deviate from the "
                    f"majority '{majority}' pattern). Parallel structure aids scanning."
                ),
                location=f"line {start_line}",
            )

    return None


def _check_conclusion_before_evidence(paragraphs: list[str]) -> FrictionSignal | None:
    """Check if conclusions/recommendations appear before supporting evidence.

    Only fires when:
    - A paragraph with conclusion language appears early
    - Evidence/reasoning language appears later
    - The evidence paragraph comes after the conclusion
    """
    if len(paragraphs) < 2:
        return None

    first_conclusion_idx: int | None = None
    first_evidence_idx: int | None = None

    for i, para in enumerate(paragraphs):
        if first_conclusion_idx is None and _CONCLUSION_WORDS.search(para):
            first_conclusion_idx = i
        if first_evidence_idx is None and _EVIDENCE_WORDS.search(para):
            first_evidence_idx = i

    if (first_conclusion_idx is not None
            and first_evidence_idx is not None
            and first_conclusion_idx < first_evidence_idx
            and first_conclusion_idx == 0):
        return FrictionSignal(
            kind="conclusion_before_evidence",
            description=(
                "Conclusion/recommendation in paragraph 1, but supporting "
                f"evidence doesn't appear until paragraph {first_evidence_idx + 1}. "
                "Reader must accept the conclusion on faith before seeing why."
            ),
            location=f"paragraph 1 vs paragraph {first_evidence_idx + 1}",
        )

    return None


def _check_wall_of_text(paragraphs: list[str]) -> FrictionSignal | None:
    """Check for paragraphs that exceed the wall-of-text threshold."""
    for i, para in enumerate(paragraphs, start=1):
        # Skip paragraphs that are actually lists or code blocks
        lines = para.split("\n")
        is_list = all(
            re.match(r"^\s*(?:[-*+]|\d+[.)])", line) for line in lines if line.strip()
        )
        is_code = para.startswith("```") or para.startswith("    ")
        if is_list or is_code:
            continue

        if len(para) > _WALL_THRESHOLD:
            return FrictionSignal(
                kind="wall_of_text",
                description=(
                    f"Paragraph {i} is {len(para)} characters with no structural "
                    f"break (threshold: {_WALL_THRESHOLD}). Reader must hold the "
                    "entire block in working memory."
                ),
                location=f"paragraph {i}",
            )

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def check_mcf(text: str) -> MCFResult:
    """Run all MCF friction checks on outgoing response text.

    Args:
        text: The response text to check.

    Returns:
        MCFResult with any detected friction signals.
    """
    if not text or len(text) < _MIN_CHECK_LENGTH:
        return MCFResult()

    paragraphs = _split_paragraphs(text)
    signals: list[FrictionSignal] = []

    # Run each detector; collect non-None results
    for detector in [
        lambda: _check_buried_lead(paragraphs),
        lambda: _check_forward_references(text),
        lambda: _check_non_parallel_lists(text),
        lambda: _check_conclusion_before_evidence(paragraphs),
        lambda: _check_wall_of_text(paragraphs),
    ]:
        result = detector()
        if result is not None:
            signals.append(result)

    return MCFResult(signals=signals)


def format_diagnostics(result: MCFResult) -> str:
    """Format MCF diagnostics as a human-readable string.

    Args:
        result: Output from check_mcf().

    Returns:
        One-line-per-signal summary, or empty string if no friction.
    """
    if not result.has_friction:
        return ""

    lines = [f"MCF filter: {len(result.signals)} friction signal(s) detected"]
    for signal in result.signals:
        loc = f" [{signal.location}]" if signal.location else ""
        lines.append(f"  - {signal.kind}{loc}: {signal.description}")
    return "\n".join(lines)
