"""
Metabolic classification for subagent results arriving at the dispatcher relay.

Pure-function module — classify_result() has no side effects.
emit_sweep_signal() is the one exception: file append, no LLM, no network.

Vocabulary follows registry.py outcome_category: pearl, seed, shit, heat.
JUICE and MIXED extend that set for relay classification purposes.

All results are provisional=True in cycle 1 (heuristic-only).
LLM classification deferred to cycle 2.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class MetabolicClass(str, Enum):
    PEARL = "pearl"
    SEED = "seed"
    SHIT = "shit"
    HEAT = "heat"
    JUICE = "juice"
    MIXED = "mixed"


@dataclass
class ClassificationResult:
    cls: MetabolicClass
    confidence: float       # 0.0–1.0
    rationale: str
    artifacts: list[str]    # extracted URLs, PR links, file paths, issue refs
    provisional: bool       # True = heuristic-only; False = LLM-confirmed


# ---------------------------------------------------------------------------
# Artifact extraction
# ---------------------------------------------------------------------------

ARTIFACT_PATTERNS = [
    r"https?://\S+",
    r"#\d{2,5}\b",           # issue/PR refs
    r"/home/\S+",            # file paths
    r"oracle/verdicts/",
    r"VERDICT:",
    r"PR #\d+",
    r"Issue #\d+",
]


def extract_artifacts(text: str) -> list[str]:
    found: list[str] = []
    for pat in ARTIFACT_PATTERNS:
        found.extend(re.findall(pat, text))
    return list(set(found))


# ---------------------------------------------------------------------------
# Vocabulary sets (checked against lowercased text unless noted)
# ---------------------------------------------------------------------------

_FORWARD_TRAJECTORY = [
    "could",
    "might",
    "next step",
    "worth exploring",
    "follow-up",
    "open question",
    "future",
]

# Checked against original text (preserves "FAILED" as uppercase signal)
_FAILURE_VOCABULARY = [
    "failed",
    "error",
    "exception",
    "traceback",
    "FAILED",
    "broken",
    "could not",
]


# ---------------------------------------------------------------------------
# Classification (pure function)
# ---------------------------------------------------------------------------

def classify_result(
    text: str,
    task_id: str,
    metadata: dict[str, Any],
) -> ClassificationResult:
    """
    Classify a subagent result using ordered heuristic rules.

    All results are marked provisional=True — LLM confirmation deferred to cycle 2.
    Rules apply in order; first match wins.
    """
    artifacts = extract_artifacts(text)
    text_len = len(text)
    text_lower = text.lower()

    has_artifact_signals = len(artifacts) >= 1
    has_two_plus_artifacts = len(artifacts) >= 2
    has_forward = any(kw in text_lower for kw in _FORWARD_TRAJECTORY)
    has_failure = any(kw in text for kw in _FAILURE_VOCABULARY)

    # Rule 1: HEAT — short, no artifacts
    if text_len < 120 and not has_artifact_signals:
        return ClassificationResult(
            cls=MetabolicClass.HEAT,
            confidence=0.8,
            rationale="short, no artifacts → residual trace",
            artifacts=artifacts,
            provisional=True,
        )

    # Rule 2: PEARL (strong) — oracle-approved review result
    if task_id.startswith("review-") and "VERDICT: APPROVED" in text:
        return ClassificationResult(
            cls=MetabolicClass.PEARL,
            confidence=0.95,
            rationale="oracle-approved review result",
            artifacts=artifacts,
            provisional=True,
        )

    # Rule 3: PEARL (artifact-rich) — substantial text with multiple artifact refs
    if text_len > 800 and has_two_plus_artifacts:
        return ClassificationResult(
            cls=MetabolicClass.PEARL,
            confidence=0.75,
            rationale="substantial result with multiple artifact references",
            artifacts=artifacts,
            provisional=True,
        )

    # Rule 4: SEED — forward-trajectory language, compact (not a completed artifact)
    if has_forward and text_len < 600:
        return ClassificationResult(
            cls=MetabolicClass.SEED,
            confidence=0.7,
            rationale="result is a suggestion or open thread, not a completed artifact",
            artifacts=artifacts,
            provisional=True,
        )

    # Rule 5: SHIT — explicit failure vocabulary, not oracle-approved
    if has_failure and "VERDICT: APPROVED" not in text:
        return ClassificationResult(
            cls=MetabolicClass.SHIT,
            confidence=0.85,
            rationale="result signals failure or degradation",
            artifacts=artifacts,
            provisional=True,
        )

    # Rule 6: JUICE — completed something AND opened threads (forward + artifact-rich + long)
    if text_len > 400 and has_forward and has_artifact_signals:
        return ClassificationResult(
            cls=MetabolicClass.JUICE,
            confidence=0.65,
            rationale="completed deliverable with visible forward trajectory",
            artifacts=artifacts,
            provisional=True,
        )

    # Rule 7: MIXED — default when no clear signal
    return ClassificationResult(
        cls=MetabolicClass.MIXED,
        confidence=0.5,
        rationale="ambiguous — relay as-is",
        artifacts=artifacts,
        provisional=True,
    )


# ---------------------------------------------------------------------------
# Side-effect helpers (called by dispatcher, not by classify_result)
# ---------------------------------------------------------------------------

def emit_sweep_signal(task_id: str, text: str) -> None:
    """Append a sweep candidate entry to ~/lobster-workspace/data/sweep-candidates.jsonl."""
    path = Path.home() / "lobster-workspace" / "data" / "sweep-candidates.jsonl"
    entry = {
        "task_id": task_id,
        "flagged_at": datetime.now(tz=timezone.utc).isoformat(),
        "reason": "metabolic-shit",
        "text_preview": text[:200],
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
