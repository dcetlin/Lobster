"""
src/mcp/user_model/session_delta.py — Live user state delta for dispatcher session start.

Computes a compact markdown block from recent signals (messages DB, brain dumps,
memory events) and returns it for injection into the dispatcher session context.

Design constraints:
  - No MCP dependencies — stdlib + sqlite3 + pathlib only (runs inside SessionStart hook
    before the MCP server is available).
  - Graceful degradation — each data source is wrapped in its own try/except. If a
    source fails, that section is omitted. Never raises.
  - Token budget — returned string is capped at roughly 400 tokens (~1600 chars).

Public API: compute_session_delta() -> str
  Returns the ## Active User State Delta markdown block, or "" if no data is available.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_MESSAGES_DB = Path(os.environ.get("LOBSTER_MESSAGES_DB", str(_MESSAGES_DIR / "messages.db")))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_BRAIN_DUMPS_DIR = _WORKSPACE / "brain-dumps"
_MEMORY_EVENTS = _WORKSPACE / "data" / "memory-events.jsonl"
_LOCAL_TZ = ZoneInfo("America/Chicago")

_MAX_CHARS = 1600  # ~400 tokens

_URGENCY_WORDS = frozenset({"urgent", "asap", "need", "stuck", "broken", "critical", "blocker", "blocked"})

# Common English stop words to skip in topic extraction
_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "that", "this", "it", "is", "are", "was",
    "were", "be", "been", "have", "has", "had", "do", "does", "did", "can",
    "could", "would", "will", "shall", "may", "might", "i", "you", "we",
    "he", "she", "they", "me", "us", "him", "her", "them", "my", "your",
    "our", "his", "its", "their", "not", "no", "so", "as", "if", "what",
    "how", "when", "where", "why", "which", "who", "just", "also", "about",
    "up", "out", "into", "than", "then", "now", "get", "got", "going",
    "think", "know", "see", "make", "like", "want", "need", "let", "put",
    "new", "way", "some", "all", "any", "one", "two", "here", "there",
    "ve", "re", "ll", "t", "s", "m", "d",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _open_messages_db() -> sqlite3.Connection | None:
    """Open messages.db read-only; return None if unavailable."""
    if not _MESSAGES_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{_MESSAGES_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _local_ts(iso_ts: str | None) -> str:
    """Convert ISO UTC timestamp to a human-readable local time string."""
    if not iso_ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        local = dt.astimezone(_LOCAL_TZ)
        return local.strftime("%b %d %I:%M %p %Z")
    except Exception:
        return iso_ts


def _local_ts_header() -> str:
    """Current local time for the block header."""
    try:
        now = datetime.now(tz=_LOCAL_TZ)
        return now.strftime("%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_topics(texts: list[str]) -> list[str]:
    """Extract up to 4 recurring noun-phrase-ish keywords from a list of texts."""
    word_counts: Counter[str] = Counter()
    for text in texts:
        words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
        for w in words:
            if w not in _STOP_WORDS:
                word_counts[w] += 1
    return [w for w, count in word_counts.most_common(8) if count >= 2][:4]


def _infer_register(texts: list[str], n_questions: int) -> str:
    """Infer conversational register from message characteristics."""
    if not texts:
        return "unknown"
    avg_len = sum(len(t) for t in texts) / len(texts)
    has_urgency = any(
        any(uw in t.lower() for uw in _URGENCY_WORDS)
        for t in texts
    )
    question_ratio = n_questions / len(texts) if texts else 0

    if has_urgency:
        return "urgent, direct"
    if avg_len < 60 and question_ratio < 0.2:
        return "terse, direct"
    if question_ratio > 0.4:
        return "exploratory, questioning"
    if avg_len > 200:
        return "detailed, thorough"
    return "conversational"


def _count_urgency_words(texts: list[str]) -> int:
    count = 0
    for t in texts:
        tl = t.lower()
        count += sum(1 for uw in _URGENCY_WORDS if uw in tl)
    return count


# ---------------------------------------------------------------------------
# Data source 1: recent messages
# ---------------------------------------------------------------------------

def _read_messages() -> dict:
    """Query messages DB for the last 20 conversation messages."""
    result: dict = {}
    conn = _open_messages_db()
    if conn is None:
        return result
    try:
        rows = conn.execute(
            """
            SELECT direction, text, timestamp
            FROM messages
            WHERE text IS NOT NULL AND text != ''
              AND (direction = 'out' OR (direction = 'in' AND source NOT IN ('system')))
            ORDER BY timestamp DESC
            LIMIT 20
            """,
        ).fetchall()
        if not rows:
            return result

        texts = [r["text"] for r in rows if r["text"]]
        n_questions = sum(1 for t in texts if "?" in t)
        last_ts = rows[0]["timestamp"] if rows else None

        # Urgency pattern: count urgency words + messages in last 48h
        now_utc = datetime.now(timezone.utc)
        cutoff_48h = now_utc - timedelta(hours=48)
        recent_count = 0
        for r in rows:
            try:
                ts = datetime.fromisoformat((r["timestamp"] or "").replace("Z", "+00:00"))
                if ts >= cutoff_48h:
                    recent_count += 1
            except Exception:
                pass

        urgency_word_count = _count_urgency_words(texts)
        if urgency_word_count >= 3 or (recent_count >= 12 and urgency_word_count >= 1):
            urgency = "high"
        elif urgency_word_count >= 1 or recent_count >= 6:
            urgency = "medium"
        else:
            urgency = "low"

        result["register"] = _infer_register(texts, n_questions)
        result["topics"] = _extract_topics(texts)
        result["urgency"] = urgency
        result["recent_48h_count"] = recent_count
        result["last_ts"] = _local_ts(last_ts)
        result["last_ts_raw"] = last_ts

        # Delta observation: detect quiet vs active
        if recent_count == 0:
            result["delta_obs"] = "No messages in the last 48h — unusually quiet."
        elif recent_count >= 10:
            result["delta_obs"] = f"High-frequency session: {recent_count} messages in last 48h."
        else:
            result["delta_obs"] = None

    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Data source 2: brain dumps
# ---------------------------------------------------------------------------

def _read_brain_dumps() -> list[str]:
    """Return titles of brain dump files modified in the last 7 days."""
    results: list[str] = []
    try:
        if not _BRAIN_DUMPS_DIR.exists():
            return results
        cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
        for f in sorted(_BRAIN_DUMPS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix not in (".md", ".txt") or f.stat().st_mtime < cutoff:
                continue
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip().lstrip("#").strip()
                    if line:
                        results.append(line[:80])
                        break
            except Exception:
                pass
            if len(results) >= 3:
                break
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# Data source 3: memory events
# ---------------------------------------------------------------------------

def _read_memory_events() -> list[str]:
    """Extract unique event types from the last 60 lines of memory-events.jsonl."""
    results: list[str] = []
    try:
        if not _MEMORY_EVENTS.exists():
            return results
        import json as _json
        lines = _MEMORY_EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()
        seen: set[str] = set()
        for line in reversed(lines[-60:]):
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
                event_type = obj.get("type") or obj.get("event_type") or obj.get("category")
                if event_type and event_type not in seen:
                    seen.add(event_type)
                    results.append(event_type)
            except Exception:
                pass
        results = results[:8]
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_session_delta() -> str:
    """
    Compute the Active User State Delta block.

    Returns a markdown string for injection into the dispatcher session context,
    or "" if no data is available from any source.
    """
    msg_data = {}
    brain_dumps: list[str] = []
    mem_events: list[str] = []

    try:
        msg_data = _read_messages()
    except Exception:
        pass

    try:
        brain_dumps = _read_brain_dumps()
    except Exception:
        pass

    try:
        mem_events = _read_memory_events()
    except Exception:
        pass

    if not msg_data and not brain_dumps and not mem_events:
        return ""

    header_ts = _local_ts_header()

    # Register
    register = msg_data.get("register", "unknown")

    # Topics
    topics_list = msg_data.get("topics", [])
    topics = ", ".join(topics_list) if topics_list else "insufficient data"

    # Urgency
    urgency = msg_data.get("urgency", "low")
    recent_count = msg_data.get("recent_48h_count", 0)

    # Last active
    last_ts = msg_data.get("last_ts", "unknown")

    # Brain dumps
    if brain_dumps:
        bd_str = "; ".join(f'"{d}"' for d in brain_dumps)
    else:
        bd_str = "none in last 7 days"

    # Delta observation
    delta_obs_base = msg_data.get("delta_obs")
    topics_context = ""
    if topics_list:
        topics_context = f" Most recent messages centre on: {', '.join(topics_list[:2])}."
    if delta_obs_base:
        delta = delta_obs_base + topics_context
    elif topics_list:
        delta = f"Active on recent topics ({', '.join(topics_list[:2])}).{' Brain dumps suggest ongoing work.' if brain_dumps else ''}"
    else:
        delta = "Insufficient signal to characterise deviation from baseline."

    lines = [
        f"## Active User State Delta [{header_ts}]",
        "*(live signals — supplement to static bootup context)*",
        "",
        f"**Register:** {register}",
        f"**Active topics:** {topics}",
        f"**Urgency pattern:** {urgency} ({recent_count} messages in last 48h)",
        f"**Last active:** {last_ts}",
        f"**Recent brain dumps:** {bd_str}",
        f"**Delta from baseline:** {delta}",
    ]

    if mem_events:
        lines.append(f"**Recent memory event types:** {', '.join(mem_events[:6])}")

    output = "\n".join(lines)

    # Enforce token budget (~1600 chars ≈ 400 tokens)
    if len(output) > _MAX_CHARS:
        output = output[:_MAX_CHARS].rsplit("\n", 1)[0] + "\n*(truncated)*"

    return output
