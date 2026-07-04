"""Detect whether a user message is a deep work / async artifact request."""
import re
from dataclasses import dataclass

_DEEP_WORK_PATTERNS = [
    r"research\s+.+\s+and\s+(write|produce|give me|summarize|draft)",
    r"(write|draft|produce)\s+(me\s+)?(a\s+)?(summary|report|analysis|overview|assessment|proposal|brief|document)",
    r"(summarize|synthesize|analyze)\s+.+\s+and\s+(write|give me|produce)",
    r"(can you|please|could you)\s+(research|investigate|look into|deep.?dive)",
    r"async\s+(research|artifact|task|deep.?work)",
    r"(put together|compile|create)\s+(a\s+)?(research|summary|report|analysis|brief)",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _DEEP_WORK_PATTERNS]


@dataclass
class DeepWorkSignal:
    is_deep_work: bool
    confidence: float          # 0.0–1.0
    suggested_title: str       # for artifact slug generation
    estimated_minutes: int     # rough estimate for scope planning


def classify(text: str) -> DeepWorkSignal:
    """Return DeepWorkSignal for the given message text."""
    matches = sum(1 for p in _COMPILED if p.search(text))
    if matches == 0:
        return DeepWorkSignal(False, 0.0, "", 0)

    confidence = min(0.5 + (matches * 0.2), 1.0)
    title = _extract_title_hint(text)
    estimated = _estimate_minutes(text)
    return DeepWorkSignal(True, confidence, title, estimated)


def _extract_title_hint(text: str) -> str:
    """Best-effort extraction of a topic phrase for artifact naming."""
    # Strip leading imperative verbs
    stripped = re.sub(
        r"^(research|write|draft|summarize|analyze|investigate|can you|please|could you|put together|compile|create)\s+",
        "", text.strip(), flags=re.IGNORECASE
    )
    # Truncate at 60 chars, strip trailing prepositions
    stripped = re.sub(r"\s+(and|for|with|on|in|of)\s*$", "", stripped[:60], flags=re.IGNORECASE)
    return stripped.strip() or "Deep Work Artifact"


def _estimate_minutes(text: str) -> int:
    depth_words = ["comprehensive", "thorough", "complete", "detailed", "in-depth", "deep dive", "full"]
    if any(w in text.lower() for w in depth_words):
        return 45
    return 25
