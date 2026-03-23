"""
src/classifiers/quick_classifier.py — Layer 3 (Medium-quick) of the multi-timescale architecture.

Role
----
The quick-classifier runs near-immediately after new messages are received. It performs
first-pass classification using Signals A–E from the cycle spec, writes provisional tags
to memory.db, and exits. It is assumption-forward: false positives are acceptable; false
negatives are not.

The main dispatcher NEVER classifies inline. It reads whatever tags background processes
have written to memory.db. This process is the primary writer of those tags at message-
receipt time. The slow-reclassifier (Layer 4) will later revise them with deeper context.

Integration Points
------------------
- Reads from:  recent events in memory.db (events table)
- Writes to:   memory.db — table `classification_tags`, keyed by entry_id
- Consumed by: main dispatcher reads tags on its next loop pass
- Superseded by: slow-reclassifier rewrites the same keys with deeper judgment

Signals Applied (heuristically, assumption-forward)
----------------------------------------------------
Signal A — Structural reach: modifies routing, agent spawning, state storage, or context loading
Signal B — Instruction-layer modification: edits to .claude/, vision.md, CLAUDE.md, agent .md files
Signal C — Non-reversibility: schema changes, crontab, scheduled jobs, external API side effects
Signal D — Premise involvement: triggered by or references a premise-review / oracle finding
Signal E — Scope crossing: touches more than one system boundary simultaneously

Additional Classification Dimensions
-------------------------------------
signal_type — what kind of input this is:
    task_request, design_question, voice_note, status_check,
    system_observation, meta_reflection, casual

urgency — how time-sensitive:
    high, normal, low

posture_hint — which posture is most relevant:
    pattern_perception, structural_coherence, attunement,
    elegant_economy, minimal_cognitive_friction

Usage
-----
    uv run src/classifiers/quick_classifier.py --once
    uv run src/classifiers/quick_classifier.py --loop [--interval SECONDS]

Or as a triggered/scheduled job registered with the Lobster scheduler.

See Also
--------
- design/cycle-spec-design.md (full architecture)
- src/classifiers/slow_reclassifier.py (Layer 4 — revises these tags with accumulated context)
- GitHub: dcetlin/Lobster#42
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [quick-classifier] %(message)s")
log = logging.getLogger(__name__)

CLASSIFIER_VERSION = "quick-v1"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOBSTER_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
MEMORY_DB_PATH = LOBSTER_WORKSPACE / "data" / "memory.db"
LOG_PATH = LOBSTER_WORKSPACE / "logs" / "classifier.log"

# How many recent events to pull per classification pass
RECENT_EVENTS_LIMIT = 50

DEFAULT_LOOP_INTERVAL = 30  # seconds


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SignalFlags:
    """Which signals were detected for a given message/observation."""
    signal_a: bool = False  # Structural reach
    signal_b: bool = False  # Instruction-layer modification
    signal_c: bool = False  # Non-reversibility
    signal_d: bool = False  # Premise involvement
    signal_e: bool = False  # Scope crossing

    def any_significant(self) -> bool:
        return any([self.signal_a, self.signal_b, self.signal_c, self.signal_d, self.signal_e])

    def as_dict(self) -> dict:
        return {
            "signal_a": self.signal_a,
            "signal_b": self.signal_b,
            "signal_c": self.signal_c,
            "signal_d": self.signal_d,
            "signal_e": self.signal_e,
        }

    def active_names(self) -> list[str]:
        return [k for k, v in self.as_dict().items() if v]


@dataclass
class ClassificationTag:
    """A first-pass classification result for a single event/observation."""
    entry_id: str
    entry_type: str              # "event" | "message"
    signals: SignalFlags
    significant_change: bool
    signal_type: str = "system_observation"
    urgency: str = "normal"
    posture_hint: str = "minimal_cognitive_friction"
    confidence: str = "low"      # quick-classifier always emits low confidence
    classifier: str = CLASSIFIER_VERSION
    classified_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes: str = ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open memory.db with WAL mode and row factory."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure_classification_table(conn: sqlite3.Connection) -> None:
    """
    Create or migrate the classification_tags table.

    The table stores one row per (entry_id, classifier) pair. Columns beyond
    the original signals A–E are added via ALTER TABLE when the schema
    predates this classifier version, so existing installs upgrade cleanly.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS classification_tags (
            entry_id           TEXT NOT NULL,
            entry_type         TEXT NOT NULL,
            classifier         TEXT NOT NULL DEFAULT 'quick',
            significant        INTEGER NOT NULL DEFAULT 0,
            signal_a           INTEGER NOT NULL DEFAULT 0,
            signal_b           INTEGER NOT NULL DEFAULT 0,
            signal_c           INTEGER NOT NULL DEFAULT 0,
            signal_d           INTEGER NOT NULL DEFAULT 0,
            signal_e           INTEGER NOT NULL DEFAULT 0,
            signal_type        TEXT NOT NULL DEFAULT 'system_observation',
            urgency            TEXT NOT NULL DEFAULT 'normal',
            posture_hint       TEXT NOT NULL DEFAULT 'minimal_cognitive_friction',
            confidence         TEXT NOT NULL DEFAULT 'low',
            notes              TEXT DEFAULT '',
            classified_at      TEXT NOT NULL,
            PRIMARY KEY (entry_id, classifier)
        )
    """)
    # Idempotent column additions for installs that have the old schema without
    # the three new dimensions. ALTER TABLE IF NOT EXISTS column is SQLite 3.37+;
    # we guard each with a try/except for older SQLite versions.
    for col_ddl in [
        "ALTER TABLE classification_tags ADD COLUMN signal_type TEXT NOT NULL DEFAULT 'system_observation'",
        "ALTER TABLE classification_tags ADD COLUMN urgency TEXT NOT NULL DEFAULT 'normal'",
        "ALTER TABLE classification_tags ADD COLUMN posture_hint TEXT NOT NULL DEFAULT 'minimal_cognitive_friction'",
    ]:
        try:
            conn.execute(col_ddl)
        except sqlite3.OperationalError:
            # Column already exists — expected on new installs after CREATE TABLE
            pass
    conn.commit()


def write_tag(conn: sqlite3.Connection, tag: ClassificationTag) -> None:
    """Upsert a classification tag into memory.db."""
    conn.execute("""
        INSERT INTO classification_tags
            (entry_id, entry_type, classifier, significant,
             signal_a, signal_b, signal_c, signal_d, signal_e,
             signal_type, urgency, posture_hint,
             confidence, notes, classified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (entry_id, classifier) DO UPDATE SET
            significant   = excluded.significant,
            signal_a      = excluded.signal_a,
            signal_b      = excluded.signal_b,
            signal_c      = excluded.signal_c,
            signal_d      = excluded.signal_d,
            signal_e      = excluded.signal_e,
            signal_type   = excluded.signal_type,
            urgency       = excluded.urgency,
            posture_hint  = excluded.posture_hint,
            confidence    = excluded.confidence,
            notes         = excluded.notes,
            classified_at = excluded.classified_at
    """, (
        tag.entry_id,
        tag.entry_type,
        tag.classifier,
        int(tag.significant_change),
        int(tag.signals.signal_a),
        int(tag.signals.signal_b),
        int(tag.signals.signal_c),
        int(tag.signals.signal_d),
        int(tag.signals.signal_e),
        tag.signal_type,
        tag.urgency,
        tag.posture_hint,
        tag.confidence,
        tag.notes,
        tag.classified_at,
    ))
    conn.commit()


def read_unclassified_events(
    conn: sqlite3.Connection,
    limit: int = RECENT_EVENTS_LIMIT,
) -> list[dict]:
    """
    Return recent events from memory.db that have not yet been classified
    by this classifier version.

    Uses a LEFT JOIN against classification_tags to find entries with no
    existing tag from CLASSIFIER_VERSION, ensuring idempotency: already-
    classified entries are skipped without re-processing.
    """
    try:
        cursor = conn.execute("""
            SELECT e.id, e.type, e.source, e.content, e.created_at
            FROM events e
            LEFT JOIN classification_tags ct
                ON ct.entry_id = CAST(e.id AS TEXT)
                AND ct.classifier = ?
            WHERE ct.entry_id IS NULL
            ORDER BY e.created_at DESC
            LIMIT ?
        """, (CLASSIFIER_VERSION, limit))
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError as e:
        log.warning("Could not read events table: %s", e)
        return []


def count_already_classified(conn: sqlite3.Connection) -> int:
    """Return count of entries already tagged by this classifier version."""
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM classification_tags WHERE classifier = ?",
            (CLASSIFIER_VERSION,),
        )
        return cursor.fetchone()[0]
    except sqlite3.OperationalError:
        return 0


# ---------------------------------------------------------------------------
# Signal detection (heuristic, assumption-forward)
# ---------------------------------------------------------------------------

_SIGNAL_A_KEYWORDS = [
    "dispatcher", "routing", "route", "spawn", "subagent", "context load",
    "memory.db", "schema", "agent definition", "bootup", "sys.", "mcp server",
]
_SIGNAL_B_KEYWORDS = [
    ".claude/", "vision.md", "claude.md", "agent .md", "bootup.md",
    "sys.dispatcher", "sys.subagent", "user.base", "user.dispatcher",
]
_SIGNAL_C_KEYWORDS = [
    "crontab", "cron", "scheduled job", "schema change", "migration",
    "irreversible", "external api", "webhook", "database migration",
]
_SIGNAL_D_KEYWORDS = [
    "premise", "oracle", "oracle finding", "epistemic", "retro", "retrospective",
    "design review", "misaligned", "principle",
]
_SIGNAL_E_KEYWORDS = [
    "and also", "multiple systems", "across", "both", "simultaneously",
    "memory and", "dispatcher and", "db and",
]

# signal_type detection keywords
_SIGNAL_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("task_request", [
        "implement", "build", "create", "add", "fix", "write", "refactor",
        "update", "deploy", "ship", "make", "set up", "install",
    ]),
    ("design_question", [
        "how should", "what approach", "design", "architecture", "tradeoff",
        "should we", "what's the best", "which pattern", "proposal",
    ]),
    ("voice_note", [
        "voice note", "audio", "transcription", "dictated",
    ]),
    ("status_check", [
        "status", "how is", "what's happening", "progress", "update on",
        "any news", "where are we", "running?", "working?",
    ]),
    ("meta_reflection", [
        "meta", "retrospective", "reflection", "premise", "oracle", "principle",
        "alignment", "drift", "pattern we keep", "notice that", "keep doing",
    ]),
    ("casual", [
        "thanks", "ok", "sounds good", "got it", "great", "cool",
        "nice", "lol", "haha", "hey", "hi", "hello",
    ]),
    # system_observation is the fallback
]

# urgency detection keywords
_URGENCY_HIGH_KEYWORDS = [
    "urgent", "asap", "immediately", "critical", "broken", "down", "error",
    "failing", "blocked", "now", "right now", "emergency",
]
_URGENCY_LOW_KEYWORDS = [
    "whenever", "no rush", "low priority", "eventually", "someday",
    "when you get a chance", "not urgent", "backlog",
]

# posture_hint detection keywords
_POSTURE_PATTERNS: list[tuple[str, list[str]]] = [
    ("pattern_perception", [
        "pattern", "recurring", "keep seeing", "trend", "repeated",
        "every time", "always does", "notice that",
    ]),
    ("structural_coherence", [
        "coherent", "consistent", "architecture", "design gate", "structure",
        "system boundary", "integration", "schema",
    ]),
    ("attunement", [
        "feeling", "tone", "energy", "mood", "sense", "attuned",
        "resonating", "this lands", "feels right",
    ]),
    ("elegant_economy", [
        "simplify", "minimal", "lean", "fewer", "reduce", "trim",
        "unnecessary", "overhead", "cut", "streamline",
    ]),
    # minimal_cognitive_friction is the fallback
]


def _any_keyword(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(kw in t for kw in keywords)


def detect_signals(text: str) -> SignalFlags:
    """
    Heuristically detect which A–E signals are present.

    Assumption-forward: prefer false positives over false negatives.
    The slow-reclassifier will revise these judgments with accumulated context.
    """
    return SignalFlags(
        signal_a=_any_keyword(text, _SIGNAL_A_KEYWORDS),
        signal_b=_any_keyword(text, _SIGNAL_B_KEYWORDS),
        signal_c=_any_keyword(text, _SIGNAL_C_KEYWORDS),
        signal_d=_any_keyword(text, _SIGNAL_D_KEYWORDS),
        signal_e=_any_keyword(text, _SIGNAL_E_KEYWORDS),
    )


def detect_signal_type(text: str) -> str:
    """Return the most likely signal_type label for this text."""
    t = text.lower()
    for signal_type, keywords in _SIGNAL_TYPE_PATTERNS:
        if any(kw in t for kw in keywords):
            return signal_type
    return "system_observation"


def detect_urgency(text: str) -> str:
    """Return 'high', 'normal', or 'low' urgency."""
    if _any_keyword(text, _URGENCY_HIGH_KEYWORDS):
        return "high"
    if _any_keyword(text, _URGENCY_LOW_KEYWORDS):
        return "low"
    return "normal"


def detect_posture_hint(text: str, signals: SignalFlags) -> str:
    """
    Return the most relevant posture hint.

    Signals influence posture: structural signals (A, B) suggest
    structural_coherence; non-reversibility (C) suggests elegant_economy;
    premise signals (D) suggest pattern_perception.
    """
    t = text.lower()
    for posture, keywords in _POSTURE_PATTERNS:
        if any(kw in t for kw in keywords):
            return posture
    # Signal-derived fallbacks
    if signals.signal_b or signals.signal_a:
        return "structural_coherence"
    if signals.signal_c:
        return "elegant_economy"
    if signals.signal_d:
        return "pattern_perception"
    return "minimal_cognitive_friction"


def classify_event(event: dict) -> ClassificationTag:
    """
    Produce a ClassificationTag for a single event row (pure function).

    Combines signals A–E, signal_type, urgency, and posture_hint from
    lightweight keyword matching. No I/O — all classification is in-memory.
    """
    content = event.get("content", "") or ""
    event_type = event.get("type", "") or ""
    combined_text = f"{event_type} {content}"

    signals = detect_signals(combined_text)
    signal_type = detect_signal_type(combined_text)
    urgency = detect_urgency(combined_text)
    posture_hint = detect_posture_hint(combined_text, signals)

    active = signals.active_names()
    notes_parts = ["quick-pass; pending slow-reclassifier revision"]
    if active:
        notes_parts.append(f"signals: {', '.join(active)}")

    return ClassificationTag(
        entry_id=str(event["id"]),
        entry_type="event",
        signals=signals,
        significant_change=signals.any_significant(),
        signal_type=signal_type,
        urgency=urgency,
        posture_hint=posture_hint,
        notes="; ".join(notes_parts),
    )


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

def write_run_log(
    processed: int,
    skipped: int,
    elapsed_ms: float,
    db_path: Path = MEMORY_DB_PATH,
) -> None:
    """Append a one-line JSON entry to classifier.log after each pass."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "classifier": CLASSIFIER_VERSION,
        "db": str(db_path),
        "processed": processed,
        "skipped": skipped,
        "elapsed_ms": round(elapsed_ms, 1),
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Main classification pass
# ---------------------------------------------------------------------------

def run_pass(db_path: Path = MEMORY_DB_PATH) -> tuple[int, int]:
    """
    Open memory.db, classify unclassified events, write tags.

    Returns (processed, skipped) where:
    - processed: entries classified in this pass
    - skipped: entries already tagged by this classifier version (not re-processed)
    """
    if not db_path.exists():
        log.warning("memory.db not found at %s — skipping pass.", db_path)
        return 0, 0

    conn = get_connection(db_path)
    try:
        ensure_classification_table(conn)

        already_done = count_already_classified(conn)
        events = read_unclassified_events(conn)

        if not events:
            log.info("No unclassified events found (already tagged: %d).", already_done)
            return 0, already_done

        processed = 0
        for event in events:
            tag = classify_event(event)
            write_tag(conn, tag)
            processed += 1
            if tag.significant_change:
                log.info(
                    "Significant change in event %s [%s] (signals: %s)",
                    tag.entry_id,
                    tag.signal_type,
                    tag.signals.active_names(),
                )

        log.info(
            "Pass complete: %d classified, %d already tagged.",
            processed, already_done,
        )
        return processed, already_done

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quick-classifier: fast-path heuristic tagging of memory.db events.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Run one classification pass and exit (default if neither flag given).",
    )
    mode.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously, sleeping between passes.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_LOOP_INTERVAL,
        metavar="SECONDS",
        help=f"Sleep interval between loop passes (default: {DEFAULT_LOOP_INTERVAL}s). "
             "Ignored when --once is set.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=MEMORY_DB_PATH,
        metavar="PATH",
        help="Path to memory.db (default: $LOBSTER_WORKSPACE/data/memory.db).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=RECENT_EVENTS_LIMIT,
        metavar="N",
        help=f"Max events to process per pass (default: {RECENT_EVENTS_LIMIT}).",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Default to --once when neither flag is given
    run_loop = args.loop

    if run_loop:
        log.info(
            "Starting quick-classifier loop (interval: %ds, db: %s).",
            args.interval, args.db,
        )
        while True:
            t0 = time.monotonic()
            processed, skipped = run_pass(args.db)
            elapsed_ms = (time.monotonic() - t0) * 1000
            write_run_log(processed, skipped, elapsed_ms, args.db)
            time.sleep(args.interval)
    else:
        log.info("Running single classification pass (db: %s).", args.db)
        t0 = time.monotonic()
        processed, skipped = run_pass(args.db)
        elapsed_ms = (time.monotonic() - t0) * 1000
        write_run_log(processed, skipped, elapsed_ms, args.db)
        log.info("Done. elapsed_ms=%.1f", elapsed_ms)


if __name__ == "__main__":
    main()
