#!/usr/bin/env python3
"""
Pattern Candidate Sweep — Lobster Scheduled Job
================================================

Observation-first semantic pattern surfacing. Scans the accumulated message
record for recurring themes that don't have a predefined question yet —
surfacing them as meta-thread candidates for Dan's review.

This implements the "incremental observation-first bootstrap" described in the
iteration-reflection-20260323 document: continuous accumulation with periodic
surfacing of candidates, running alongside (not replacing) question-driven
matching.

Design constraints:
- Observe patterns in Dan's own words, not summaries of them.
- Surface candidates, not conclusions. The question "does this want to become
  a meta-thread?" is for Dan to answer, not this script.
- 3 real candidates beats 10 generic ones. Threshold is 3+ messages.
- Report in Dan's register: concrete, spare, no AI-normalized language.
- Write sweep artifacts to ~/lobster-user-config/memory/signal-sweeps/ for
  continuity and review across sessions.

Run standalone:
    uv run ~/lobster/scheduled-tasks/pattern-candidate-sweep.py
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Stop words — common English terms that carry no semantic signal
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset("""
a about above after again against all also am an and any are aren't as at
be because been before being below between both but by can't cannot could
couldn't did didn't do does doesn't doing don't down during each few for from
further get got had hadn't has hasn't have haven't having he he'd he'll he's
her here here's hers herself him himself his how how's i i'd i'll i'm i've if
in into is isn't it it's its itself just let's me more most mustn't my myself
no nor not of off on once only or other ought our ours ourselves out over own
same shan't she she'd she'll she's should shouldn't so some such than that
that's the their theirs them themselves then there there's these they they'd
they'll they're they've this those through to too under until up very was
wasn't we we'd we'll we're we've were weren't what what's when when's where
where's which while who who's whom why why's will with won't would wouldn't
you you'd you'll you're you've your yours yourself yourselves
it's i'm you're let's don't doesn't can't won't isn't aren't couldn't
would've could've should've been also just well still even like get got
make use want need know see think look something going really make sure
""".split())


# ---------------------------------------------------------------------------
# Pure data helpers
# ---------------------------------------------------------------------------

def resolve_paths() -> dict:
    home = Path.home()
    messages_dir = Path(os.environ.get("LOBSTER_MESSAGES", home / "messages"))
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", home / "lobster-workspace"))
    return {
        "processed_dir": messages_dir / "processed",
        "sweeps_dir": home / "lobster-user-config" / "memory" / "signal-sweeps",
    }


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def window_start(days: int = 7) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def sweep_artifact_path(sweeps_dir: Path, date: str) -> Path:
    return sweeps_dir / f"sweep-{date}.md"


# ---------------------------------------------------------------------------
# Message loading
# ---------------------------------------------------------------------------

def load_recent_messages(processed_dir: Path, since: datetime, dan_chat_id: int) -> list:
    """Load inbound text/voice messages from Dan in the past window."""
    if not processed_dir.exists():
        return []

    messages = []
    since_ts = since.timestamp() * 1000  # filenames use millisecond timestamps

    for path in processed_dir.glob("*.json"):
        stem = path.stem
        parts = stem.split("_", 1)
        if not parts[0].isdigit():
            continue
        if int(parts[0]) < since_ts:
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if data.get("chat_id") != dan_chat_id:
            continue
        if data.get("type") not in ("text", "voice"):
            continue
        text = data.get("text") or data.get("transcription") or ""
        if not text or len(text) < 20:
            continue

        ts_raw = data.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            ts = datetime.now(timezone.utc)

        messages.append({"id": data.get("id", path.stem), "timestamp": ts, "text": text})

    messages.sort(key=lambda m: m["timestamp"])
    return messages


# ---------------------------------------------------------------------------
# Term extraction
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z][a-z'\-]{2,}")


def extract_terms(text: str) -> list:
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) >= 3]


def extract_bigrams(terms: list) -> list:
    return [f"{terms[i]} {terms[i+1]}" for i in range(len(terms) - 1)]


# ---------------------------------------------------------------------------
# Pattern candidate identification
# ---------------------------------------------------------------------------

def find_pattern_candidates(messages: list, min_doc_freq: int = 3) -> list:
    """Find recurring semantic patterns. Returns list sorted by doc frequency."""
    doc_unigrams = []
    doc_bigrams = []

    for msg in messages:
        terms = extract_terms(msg["text"])
        doc_unigrams.append(set(terms))
        doc_bigrams.append(set(extract_bigrams(terms)))

    unigram_df: Counter = Counter()
    bigram_df: Counter = Counter()
    for terms in doc_unigrams:
        unigram_df.update(terms)
    for bigrams in doc_bigrams:
        bigram_df.update(bigrams)

    candidates = {}

    # Bigrams first (richer signal)
    for bigram, df in bigram_df.items():
        if df >= min_doc_freq:
            candidates[bigram] = {"term": bigram, "doc_count": df, "is_bigram": True}

    # Unigrams not already covered
    covered = set()
    for bg in candidates:
        covered.update(bg.split())
    for term, df in unigram_df.items():
        if df >= min_doc_freq and term not in covered:
            candidates[term] = {"term": term, "doc_count": df, "is_bigram": False}

    # Attach examples and timestamps
    for cand in candidates.values():
        words = set(cand["term"].split())
        examples, timestamps = [], []
        for msg in messages:
            if all(w in msg["text"].lower() for w in words):
                anchor = list(words)[0]
                idx = msg["text"].lower().find(anchor)
                start = max(0, idx - 15)
                end = min(len(msg["text"]), idx + 70)
                snippet = ("…" if start > 0 else "") + msg["text"][start:end].strip()
                if end < len(msg["text"]):
                    snippet += "…"
                examples.append(snippet)
                timestamps.append(msg["timestamp"])
        cand["examples"] = examples[:3]
        if timestamps:
            cand["first_seen"] = min(timestamps).strftime("%Y-%m-%d")
            cand["last_seen"] = max(timestamps).strftime("%Y-%m-%d")

    return sorted(candidates.values(), key=lambda c: (c["doc_count"], c["is_bigram"]), reverse=True)


# ---------------------------------------------------------------------------
# Artifact formatting
# ---------------------------------------------------------------------------

def format_sweep_artifact(candidates: list, messages: list, date: str, window_days: int = 7) -> str:
    n = len(messages)
    lines = [
        f"# Pattern Candidate Sweep — {date}",
        "",
        f"*Observation-first scan: {n} messages over past {window_days} days.*",
        "*Minimum threshold: 3+ messages. These are candidates, not conclusions.*",
        "",
        "---",
        "",
    ]

    if not candidates:
        lines += [
            "## Result",
            "",
            "No patterns met the 3-message threshold this sweep.",
            "Record is too sparse or too diverse.",
        ]
        return "\n".join(lines)

    lines += ["## Pattern Candidates", ""]
    for i, cand in enumerate(candidates[:5], 1):
        lines += [
            f"### {i}. `{cand['term']}`",
            f"- Frequency: {cand['doc_count']} messages",
            f"- Window: {cand.get('first_seen', '?')} → {cand.get('last_seen', '?')}",
            "",
            "**Context:**",
        ]
        for ex in cand.get("examples", []):
            lines.append(f"> {ex}")
        lines.append("")

    lines += [
        "---",
        "",
        "## Disposition",
        "",
        "For each candidate: does this feel like it wants to become a meta-thread?",
        "If yes — formulate an open inquiry. If no — let it accumulate or discard.",
    ]
    return "\n".join(lines)


def format_telegram_summary(candidates: list, date: str, n_messages: int) -> str:
    if not candidates:
        return (
            f"Pattern sweep — {date}\n\n"
            f"Scanned {n_messages} messages. No patterns reached threshold (3 msgs).\n"
            "Sparse week. Check again next sweep."
        )
    top = candidates[:3]
    lines = [f"Pattern sweep — {date}", f"{n_messages} messages, past 7 days", ""]
    lines.append("Recurring themes in your messages:")
    for c in top:
        lines.append(f"• `{c['term']}` — {c['doc_count']} msgs")
    if len(candidates) > 3:
        lines.append(f"  (+{len(candidates)-3} more in sweep artifact)")
    lines += ["", "Do any of these want to become a meta-thread?"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write + deliver
# ---------------------------------------------------------------------------

def write_artifact(sweeps_dir: Path, path: Path, content: str) -> None:
    sweeps_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def deliver_and_log(summary: str, artifact_path_str: str) -> None:
    chat_id = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
    prompt = f"""Make exactly these two calls:

1. send_reply with chat_id={chat_id}, source="telegram", text={json.dumps(summary)}

2. write_task_output with job_name="pattern-candidate-sweep",
   output="Sweep complete. Artifact: {artifact_path_str}. Summary sent to Telegram.",
   status="success"

Make both calls, then stop. No commentary.
"""
    subprocess.run(
        ["claude", "-p", prompt, "--dangerously-skip-permissions", "--max-turns", "5"],
        timeout=120,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> int:
    paths = resolve_paths()
    date = today_iso()
    dan_chat_id = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

    print(f"[{date}] Pattern candidate sweep starting")

    since = window_start(7)
    messages = load_recent_messages(paths["processed_dir"], since, dan_chat_id)
    print(f"  {len(messages)} inbound messages in past 7 days")

    if len(messages) < 3:
        print("  Insufficient data (<3 messages). Skipping.")
        subprocess.run(
            ["claude", "-p",
             'Call write_task_output with job_name="pattern-candidate-sweep", '
             f'output="Skipped: only {len(messages)} messages in window.", status="success". Stop.',
             "--dangerously-skip-permissions", "--max-turns", "3"],
            timeout=60,
        )
        return 0

    candidates = find_pattern_candidates(messages, min_doc_freq=3)
    print(f"  {len(candidates)} pattern candidates found")

    artifact = format_sweep_artifact(candidates, messages, date)
    art_path = sweep_artifact_path(paths["sweeps_dir"], date)
    write_artifact(paths["sweeps_dir"], art_path, artifact)
    print(f"  Artifact: {art_path}")

    summary = format_telegram_summary(candidates, date, len(messages))
    deliver_and_log(summary, str(art_path))

    print(f"[{date}] Sweep complete")
    return 0


if __name__ == "__main__":
    sys.exit(run())
