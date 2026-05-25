"""
Active user model: reads recent signals and produces a delta from the static bootup baseline.
Injected at session start to give the dispatcher a live snapshot beyond static context files.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict


LOBSTER_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
MESSAGES_ROOT = Path(os.environ.get("MESSAGES_ROOT", Path.home() / "messages"))
USER_CONFIG = Path(os.environ.get("LOBSTER_USER_CONFIG", Path.home() / "lobster-user-config"))


class UserModelDelta(TypedDict):
    register: str           # "technical" | "conversational" | "urgent" | "mixed"
    recent_topics: list[str]
    urgency_signal: str     # "high" | "medium" | "low"
    deviations: list[str]   # state claims in baseline that recent signals contradict
    generated_at: str       # ISO 8601


URGENCY_MARKERS = {"urgent", "asap", "need this now", "blocked", "broken", "critical", "immediately", "emergency"}
TECHNICAL_MARKERS = {"```", "uv run", "def ", "import ", "TypeError", "AttributeError", "error:", "traceback", "pr #", "issue #", "branch", "deploy", "sql", "json", "yaml"}
PERSONAL_MARKERS = {"how do you", "what do you think", "feeling", "worried", "excited", "frustrat", "should we", "what should"}


def _load_recent_processed_messages(n: int = 20) -> list[dict]:
    """Read most recent n processed messages from ~/messages/processed/."""
    processed = MESSAGES_ROOT / "processed"
    if not processed.exists():
        return []
    files = sorted(processed.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    messages = []
    for f in files[:n * 3]:  # over-fetch to account for system messages
        try:
            data = json.loads(f.read_text())
            text = data.get("text") or data.get("message", {}).get("text", "")
            if text and data.get("sender_type") in ("user", None):
                messages.append({"text": text, "ts": data.get("timestamp", "")})
        except Exception:
            continue
        if len(messages) >= n:
            break
    return messages


def _load_recent_voice_transcripts(n: int = 5) -> list[str]:
    """Read recent voice note transcriptions from sidecar .txt files in ~/messages/audio/."""
    audio_dir = MESSAGES_ROOT / "audio"
    if not audio_dir.exists():
        return []
    txts = sorted(audio_dir.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [f.read_text().strip() for f in txts[:n] if f.stat().st_size > 0]


def _classify_register(texts: list[str]) -> str:
    combined = "\n".join(texts).lower()
    urgency = sum(1 for m in URGENCY_MARKERS if m in combined)
    technical = sum(1 for m in TECHNICAL_MARKERS if m in combined)
    personal = sum(1 for m in PERSONAL_MARKERS if m in combined)
    if urgency >= 2:
        return "urgent"
    if technical > personal + 2:
        return "technical"
    if personal > technical + 2:
        return "conversational"
    if technical > 0 or personal > 0:
        return "mixed"
    return "conversational"


def _extract_topics(texts: list[str], max_topics: int = 5) -> list[str]:
    """Rough topic extraction: capitalized noun phrases and recurring keywords."""
    combined = " ".join(texts)
    # Lobster-specific artifact references (PR#, issue#, WOS, module names)
    artifact_refs = re.findall(r'\b(?:PR|issue|WOS|UoW|sprint)\s*#?\d+\b', combined, re.IGNORECASE)
    # Capitalized multi-word phrases (likely proper nouns / system names)
    caps_phrases = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', combined)
    # High-frequency single words (ignoring stopwords)
    stopwords = {"the", "a", "an", "is", "it", "in", "on", "at", "to", "for", "of", "and", "or", "I", "you", "we", "this", "that", "can", "be", "have", "has"}
    words = [w.lower() for w in re.findall(r'\b[a-zA-Z]{4,}\b', combined) if w not in stopwords]
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    top_words = [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:10]]
    candidates = artifact_refs + caps_phrases + top_words
    seen: set[str] = set()
    topics = []
    for c in candidates:
        key = c.lower().strip()
        if key not in seen:
            seen.add(key)
            topics.append(c.strip())
        if len(topics) >= max_topics:
            break
    return topics


def _urgency_level(texts: list[str]) -> str:
    combined = "\n".join(texts).lower()
    count = sum(1 for m in URGENCY_MARKERS if m in combined)
    if count >= 3:
        return "high"
    if count >= 1:
        return "medium"
    return "low"


def _compute_deviations(texts: list[str]) -> list[str]:
    """
    Compare recent message topics against state claims in the baseline context file.
    Returns brief strings describing mismatches.
    """
    baseline_path = USER_CONFIG / "agents" / "user.base.context.md"
    if not baseline_path.exists():
        return []
    baseline = baseline_path.read_text().lower()
    deviations = []
    combined = " ".join(texts).lower()

    # Look for "working on X" / "focused on X" claims in baseline
    # and check if X appears in recent messages at all
    working_on = re.findall(r'(?:working on|focused on|currently|active on)\s+([^\.\n,]{5,40})', baseline)
    for claim in working_on[:5]:
        claim_key = claim.strip().split()[0]  # first word as proxy
        if len(claim_key) > 3 and claim_key not in combined:
            deviations.append(f"baseline says 'working on {claim.strip()[:40]}' — not visible in recent messages")

    return deviations[:4]  # cap at 4 deviations to keep the block compact


def compute_delta(n_messages: int = 20) -> UserModelDelta:
    messages = _load_recent_processed_messages(n_messages)
    voices = _load_recent_voice_transcripts(5)
    texts = [m["text"] for m in messages] + voices

    return UserModelDelta(
        register=_classify_register(texts),
        recent_topics=_extract_topics(texts),
        urgency_signal=_urgency_level(texts),
        deviations=_compute_deviations(texts),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def format_delta_block(delta: UserModelDelta) -> str:
    lines = [
        "<active-user-model>",
        f"Register: {delta['register']}",
        f"Recent topics: {', '.join(delta['recent_topics']) or 'none detected'}",
        f"Urgency: {delta['urgency_signal']}",
    ]
    if delta["deviations"]:
        lines.append("Deviations from baseline:")
        for d in delta["deviations"]:
            lines.append(f"  - {d}")
    lines.append(f"Generated: {delta['generated_at']}")
    lines.append("</active-user-model>")
    return "\n".join(lines)


if __name__ == "__main__":
    delta = compute_delta()
    print(format_delta_block(delta))
