"""
src/classifiers/slow_reclassifier.py — Layer 4 (Medium-slow) of the multi-timescale architecture.

Role
----
The slow-reclassifier runs continuously on a longer cycle (default 10 minutes). Unlike the
quick-classifier which processes individual events in isolation, the slow-reclassifier:

1. Reads clusters of recent events — groups events by time proximity and sender (chat_id /
   source) within 30-minute windows.
2. Looks for cross-event patterns — sequences that individually look one way but together
   indicate something deeper: design sessions, brainstorm modes, complex multi-part requests,
   and meta threads.
3. Revises quick-classifier tags — when slow analysis produces higher-confidence classification,
   it overwrites the quick-classifier's tags in `classification_tags` using classifier='slow-v1',
   which has priority over 'quick-v1'.
4. Writes pattern observations — when a cross-event pattern is detected, logs it as a new
   event in the events table with event_type='pattern_observation'.

Pattern Detection Rules
-----------------------
- "design_session":  3+ design_question events within 60 minutes → signal_type='design_session',
                     posture_hint='structural_coherence'
- "brainstorm_mode": 3+ voice_note events within 30 minutes → signal_type='brainstorm',
                     posture_hint='pattern_perception'
- "complex_request": multiple short events (len < 50 chars) from same sender within 5 minutes →
                     signal_type='task_request', urgency='high'
- "meta_thread":     2+ meta_reflection events within 2 hours → signal_type='meta_thread',
                     posture_hint='structural_coherence'
- "philosophy_thread": 2+ philosophy events within 4 hours → signal_type='philosophy_thread',
                     posture_hint='attunement'

Integration Points
------------------
- Reads from:  events table + classification_tags (quick-v1 entries) in memory.db
- Writes to:   classification_tags (slow-v1 entries, which supersede quick-v1)
               events table (pattern_observation entries for cross-event patterns)
- Consumed by: main dispatcher reads classification_tags; slow-v1 entries take priority

Usage
-----
    uv run src/classifiers/slow_reclassifier.py --once
    uv run src/classifiers/slow_reclassifier.py --loop
    uv run src/classifiers/slow_reclassifier.py --loop --interval 300

See Also
--------
- design/cycle-spec-design.md (full architecture)
- src/classifiers/quick_classifier.py (Layer 3 — writes provisional tags this layer revises)
- GitHub: dcetlin/Lobster#43
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import struct

from src.classifiers.checkpoint import max_event_id, save_checkpoint, should_run

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [slow-reclassifier] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOBSTER_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
MEMORY_DB_PATH = LOBSTER_WORKSPACE / "data" / "memory.db"
LOG_PATH = LOBSTER_WORKSPACE / "logs" / "classifier.log"
CHECKPOINT_PATH = LOBSTER_WORKSPACE / "data" / "slow-reclassifier.checkpoint.json"

DEFAULT_INTERVAL_SECONDS = 600      # 10 minutes
CLUSTER_WINDOW_MINUTES = 30         # group events within 30 minutes by same source
LOOK_BACK_HOURS = 6                 # how far back to pull events for analysis

# Pattern thresholds
DESIGN_SESSION_THRESHOLD = 3        # 3+ design_question events within 60 minutes
DESIGN_SESSION_WINDOW_MINUTES = 60

BRAINSTORM_THRESHOLD = 3            # 3+ voice_note events within 30 minutes
BRAINSTORM_WINDOW_MINUTES = 30

COMPLEX_REQUEST_THRESHOLD = 2       # 2+ short events (len < 50) within 5 minutes
COMPLEX_REQUEST_CHAR_LIMIT = 50
COMPLEX_REQUEST_WINDOW_MINUTES = 5

META_THREAD_THRESHOLD = 2           # 2+ meta_reflection events within 2 hours
META_THREAD_WINDOW_MINUTES = 120

PHILOSOPHY_THREAD_THRESHOLD = 2    # 2+ philosophy events within 4 hours → philosophy_thread
PHILOSOPHY_THREAD_WINDOW_MINUTES = 240


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class EventRow:
    """A row from the events table, with parsed timestamp."""
    id: int
    timestamp: datetime
    event_type: str
    source: str
    content: str
    metadata: dict


@dataclass
class ClassificationTag:
    """A slow-v1 classification result for a single event."""
    entry_id: str
    entry_type: str = "event"
    classifier: str = "slow-v1"
    significant: bool = False
    signal_a: bool = False
    signal_b: bool = False
    signal_c: bool = False
    signal_d: bool = False
    signal_e: bool = False
    confidence: str = "medium"
    signal_type: str = "system_observation"
    urgency: str = "normal"
    posture_hint: str = "minimal_cognitive_friction"
    notes: str = ""
    classified_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


_GOLDEN_KEYWORDS = frozenset({"golden", "win", "strength"})
_SMELL_KEYWORDS = frozenset({"smell", "drift", "failure", "error"})


def classify_valence(pattern_type: str, description: str) -> str:
    """
    Classify a pattern observation as 'golden', 'smell', or 'neutral'.

    Applies keyword heuristics to pattern_type and description text.
    Pure function — no I/O.
    """
    combined = (pattern_type + " " + description).lower()
    if any(kw in combined for kw in _GOLDEN_KEYWORDS):
        return "golden"
    if any(kw in combined for kw in _SMELL_KEYWORDS):
        return "smell"
    return "neutral"


@dataclass
class PatternObservation:
    """A cross-event pattern detected across a cluster."""
    pattern_type: str           # design_session | brainstorm_mode | complex_request | meta_thread
    source: str                 # originating source/chat_id
    event_ids: list[int]        # contributing event IDs
    signal_type: str
    urgency: str
    posture_hint: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valence: str = "neutral"    # golden | smell | neutral


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Load sqlite-vec so events_vec INSERT works in write_pattern_event().
    conn.enable_load_extension(True)
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
    except Exception as exc:
        log.warning("sqlite_vec unavailable — pattern_observation embeddings will be skipped: %s", exc)
    conn.enable_load_extension(False)
    return conn


# ---------------------------------------------------------------------------
# Embedding helpers (same model as VectorMemory — all-MiniLM-L6-v2)
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 384
_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    return _embed_model


def _embed_content(text: str) -> bytes | None:
    """Return a 384-dim float32 blob for *text*, or None if embedding is unavailable."""
    try:
        model = _get_embed_model()
        vec = list(model.embed([text]))[0]
        floats = vec.tolist() if hasattr(vec, "tolist") else list(vec)
        return struct.pack(f"{len(floats)}f", *floats)
    except Exception as exc:
        log.warning("Embedding failed for pattern_observation content: %s", exc)
        return None


def ensure_classification_table(conn: sqlite3.Connection) -> None:
    """Ensure classification_tags table and required columns exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS classification_tags (
            entry_id        TEXT NOT NULL,
            entry_type      TEXT NOT NULL,
            classifier      TEXT NOT NULL DEFAULT 'quick-v1',
            significant     INTEGER NOT NULL DEFAULT 0,
            signal_a        INTEGER NOT NULL DEFAULT 0,
            signal_b        INTEGER NOT NULL DEFAULT 0,
            signal_c        INTEGER NOT NULL DEFAULT 0,
            signal_d        INTEGER NOT NULL DEFAULT 0,
            signal_e        INTEGER NOT NULL DEFAULT 0,
            confidence      TEXT NOT NULL DEFAULT 'low',
            notes           TEXT DEFAULT '',
            classified_at   TEXT NOT NULL,
            signal_type     TEXT NOT NULL DEFAULT 'system_observation',
            urgency         TEXT NOT NULL DEFAULT 'normal',
            posture_hint    TEXT NOT NULL DEFAULT 'minimal_cognitive_friction',
            PRIMARY KEY (entry_id, classifier)
        )
    """)
    # Guard migrations for existing installs that may lack the new columns.
    for col, definition in [
        ("signal_type", "TEXT NOT NULL DEFAULT 'system_observation'"),
        ("urgency",     "TEXT NOT NULL DEFAULT 'normal'"),
        ("posture_hint", "TEXT NOT NULL DEFAULT 'minimal_cognitive_friction'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE classification_tags ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def read_recent_events(conn: sqlite3.Connection, hours: int = LOOK_BACK_HOURS) -> list[EventRow]:
    """
    Read events from the last `hours` hours that have a quick-v1 classification tag
    but do NOT yet have a slow-v1 tag (i.e. not yet reclassified this cycle).

    Excludes pattern_observation events — those are synthetic outputs written by this
    classifier itself and must never be re-classified, or they create a feedback loop
    where pattern_observation events get tagged meta_reflection by the quick classifier,
    which then triggers more meta_thread detections and more pattern_observation writes.

    Returns them sorted by timestamp ascending (oldest first).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cursor = conn.execute("""
        SELECT e.id, e.timestamp, e.type, e.source, e.content, e.metadata
        FROM events e
        INNER JOIN classification_tags ct ON ct.entry_id = CAST(e.id AS TEXT)
            AND ct.classifier = 'quick-v1'
        LEFT JOIN classification_tags slow ON slow.entry_id = CAST(e.id AS TEXT)
            AND slow.classifier = 'slow-v1'
        WHERE e.timestamp >= ?
          AND e.type != 'pattern_observation'
          AND slow.entry_id IS NULL
        ORDER BY e.timestamp ASC
    """, (cutoff,))
    rows = []
    for r in cursor.fetchall():
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            ts = datetime.now(timezone.utc)
        rows.append(EventRow(
            id=r["id"],
            timestamp=ts,
            event_type=r["type"],
            source=r["source"] or "unknown",
            content=r["content"] or "",
            metadata=json.loads(r["metadata"] or "{}"),
        ))
    return rows


def read_quick_tag(conn: sqlite3.Connection, event_id: int) -> dict | None:
    """Read the quick-v1 classification tag for a given event, or None if absent."""
    row = conn.execute("""
        SELECT signal_type, urgency, posture_hint, significant, signal_a, signal_b,
               signal_c, signal_d, signal_e, confidence
        FROM classification_tags
        WHERE entry_id = ? AND classifier = 'quick-v1'
    """, (str(event_id),)).fetchone()
    return dict(row) if row else None


def write_tag(conn: sqlite3.Connection, tag: ClassificationTag) -> None:
    """Upsert a slow-v1 classification tag into memory.db."""
    conn.execute("""
        INSERT INTO classification_tags
            (entry_id, entry_type, classifier, significant, signal_a, signal_b,
             signal_c, signal_d, signal_e, confidence, notes, classified_at,
             signal_type, urgency, posture_hint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (entry_id, classifier) DO UPDATE SET
            significant   = excluded.significant,
            signal_a      = excluded.signal_a,
            signal_b      = excluded.signal_b,
            signal_c      = excluded.signal_c,
            signal_d      = excluded.signal_d,
            signal_e      = excluded.signal_e,
            confidence    = excluded.confidence,
            notes         = excluded.notes,
            classified_at = excluded.classified_at,
            signal_type   = excluded.signal_type,
            urgency       = excluded.urgency,
            posture_hint  = excluded.posture_hint
    """, (
        tag.entry_id,
        tag.entry_type,
        tag.classifier,
        int(tag.significant),
        int(tag.signal_a),
        int(tag.signal_b),
        int(tag.signal_c),
        int(tag.signal_d),
        int(tag.signal_e),
        tag.confidence,
        tag.notes,
        tag.classified_at,
        tag.signal_type,
        tag.urgency,
        tag.posture_hint,
    ))
    conn.commit()


def write_pattern_event(conn: sqlite3.Connection, obs: PatternObservation) -> int:
    """
    Write a pattern_observation event to the events table and events_vec.
    Returns the new event id.
    """
    metadata = json.dumps({
        "pattern_type": obs.pattern_type,
        "contributing_event_ids": obs.event_ids,
        "signal_type": obs.signal_type,
        "urgency": obs.urgency,
        "posture_hint": obs.posture_hint,
        "valence": obs.valence,
    })
    content = (
        f"pattern_observation | {obs.pattern_type} | source: {obs.source}\n"
        f"contributing events: {obs.event_ids}\n"
        f"signal_type: {obs.signal_type} | posture_hint: {obs.posture_hint}"
    )
    # Write valence to the events table column added by PR #181.
    # Fall back to INSERT without the column for databases that pre-date the migration
    # (the column has a DEFAULT 'neutral' so the value is still correct either way).
    try:
        cursor = conn.execute("""
            INSERT INTO events (timestamp, type, source, content, metadata, valence)
            VALUES (?, 'pattern_observation', ?, ?, ?, ?)
        """, (obs.detected_at.isoformat(), obs.source, content, metadata, obs.valence))
    except sqlite3.OperationalError:
        # valence column not yet present (pre-migration DB)
        cursor = conn.execute("""
            INSERT INTO events (timestamp, type, source, content, metadata)
            VALUES (?, 'pattern_observation', ?, ?, ?)
        """, (obs.detected_at.isoformat(), obs.source, content, metadata))
    event_id = cursor.lastrowid

    # Store embedding so this event is reachable via vector search.
    vec_blob = _embed_content(content)
    if vec_blob is not None:
        try:
            conn.execute(
                "INSERT INTO events_vec(rowid, embedding) VALUES (?, ?)",
                (event_id, vec_blob),
            )
        except Exception as exc:
            log.warning("Could not insert embedding for event %d: %s", event_id, exc)

    conn.commit()
    return event_id


def write_run_log(
    processed: int,
    revised: int,
    patterns_found: int,
    elapsed_ms: float,
) -> None:
    """Append a one-line JSON entry to classifier.log."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "classifier": "slow-v1",
        "events_processed": processed,
        "tags_revised": revised,
        "patterns_found": patterns_found,
        "elapsed_ms": round(elapsed_ms, 1),
    })
    with LOG_PATH.open("a") as f:
        f.write(entry + "\n")


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster_events_by_source_and_window(
    events: list[EventRow],
    window_minutes: int = CLUSTER_WINDOW_MINUTES,
) -> list[list[EventRow]]:
    """
    Group events into clusters where each cluster contains events from the same
    source within a rolling `window_minutes` window.

    Algorithm: for each source independently, walk events in time order and
    start a new cluster whenever the gap to the last event in the current cluster
    exceeds `window_minutes`.

    Returns a list of clusters (each cluster is a non-empty list of EventRows).
    """
    # Group by source first
    by_source: dict[str, list[EventRow]] = {}
    for ev in events:
        by_source.setdefault(ev.source, []).append(ev)

    clusters: list[list[EventRow]] = []
    window = timedelta(minutes=window_minutes)

    for source_events in by_source.values():
        source_events.sort(key=lambda e: e.timestamp)
        current_cluster: list[EventRow] = []
        for ev in source_events:
            if not current_cluster:
                current_cluster.append(ev)
            elif ev.timestamp - current_cluster[-1].timestamp <= window:
                current_cluster.append(ev)
            else:
                clusters.append(current_cluster)
                current_cluster = [ev]
        if current_cluster:
            clusters.append(current_cluster)

    return clusters


# ---------------------------------------------------------------------------
# Pattern detection (pure functions — no DB I/O)
# ---------------------------------------------------------------------------

def events_within_window(
    events: list[EventRow],
    window_minutes: int,
) -> Iterator[list[EventRow]]:
    """
    Yield all maximal sub-lists of `events` (sorted by timestamp) where all
    events fall within `window_minutes` of the earliest event in the sub-list.

    This is a sliding window: for each event i, collect all events j >= i
    where timestamp[j] - timestamp[i] <= window.
    """
    window = timedelta(minutes=window_minutes)
    for i, anchor in enumerate(events):
        group = [anchor]
        for ev in events[i + 1:]:
            if ev.timestamp - anchor.timestamp <= window:
                group.append(ev)
            else:
                break
        if len(group) >= 1:
            yield group


def detect_design_session(
    cluster: list[EventRow],
    quick_tags: dict[int, dict],
) -> list[PatternObservation]:
    """
    Detect design sessions: 3+ events tagged design_question within 60 minutes.
    Returns one PatternObservation per detected window (non-overlapping greedy).
    """
    design_events = [
        ev for ev in cluster
        if quick_tags.get(ev.id, {}).get("signal_type") == "design_question"
    ]
    design_events.sort(key=lambda e: e.timestamp)

    observations: list[PatternObservation] = []
    used: set[int] = set()

    for group in events_within_window(design_events, DESIGN_SESSION_WINDOW_MINUTES):
        if len(group) >= DESIGN_SESSION_THRESHOLD:
            new_ids = [e.id for e in group if e.id not in used]
            if len(new_ids) >= DESIGN_SESSION_THRESHOLD:
                used.update(new_ids)
                observations.append(PatternObservation(
                    pattern_type="design_session",
                    source=group[0].source,
                    event_ids=[e.id for e in group],
                    signal_type="design_session",
                    urgency="normal",
                    posture_hint="structural_coherence",
                    valence=classify_valence("design_session", ""),
                ))

    return observations


def detect_brainstorm_mode(
    cluster: list[EventRow],
    quick_tags: dict[int, dict],
) -> list[PatternObservation]:
    """
    Detect brainstorm mode: 3+ events tagged voice_note within 30 minutes.
    """
    voice_events = [
        ev for ev in cluster
        if quick_tags.get(ev.id, {}).get("signal_type") == "voice_note"
    ]
    voice_events.sort(key=lambda e: e.timestamp)

    observations: list[PatternObservation] = []
    used: set[int] = set()

    for group in events_within_window(voice_events, BRAINSTORM_WINDOW_MINUTES):
        if len(group) >= BRAINSTORM_THRESHOLD:
            new_ids = [e.id for e in group if e.id not in used]
            if len(new_ids) >= BRAINSTORM_THRESHOLD:
                used.update(new_ids)
                observations.append(PatternObservation(
                    pattern_type="brainstorm_mode",
                    source=group[0].source,
                    event_ids=[e.id for e in group],
                    signal_type="brainstorm",
                    urgency="normal",
                    posture_hint="pattern_perception",
                    valence=classify_valence("brainstorm_mode", ""),
                ))

    return observations


def detect_complex_request(
    cluster: list[EventRow],
    quick_tags: dict[int, dict],  # noqa: ARG001 — unused but kept for API consistency
) -> list[PatternObservation]:
    """
    Detect complex requests: 2+ short events (content len < 50) from same source
    within 5 minutes.

    Health-check events are excluded from the clustering window — their short,
    frequent payloads would otherwise trigger spurious complex_request detections.
    """
    short_events = [
        ev for ev in cluster
        if len(ev.content.strip()) < COMPLEX_REQUEST_CHAR_LIMIT
        and not ev.source.startswith("health-check")
    ]
    short_events.sort(key=lambda e: e.timestamp)

    observations: list[PatternObservation] = []
    used: set[int] = set()

    for group in events_within_window(short_events, COMPLEX_REQUEST_WINDOW_MINUTES):
        if len(group) >= COMPLEX_REQUEST_THRESHOLD:
            new_ids = [e.id for e in group if e.id not in used]
            if len(new_ids) >= COMPLEX_REQUEST_THRESHOLD:
                used.update(new_ids)
                observations.append(PatternObservation(
                    pattern_type="complex_request",
                    source=group[0].source,
                    event_ids=[e.id for e in group],
                    signal_type="task_request",
                    urgency="high",
                    posture_hint="minimal_cognitive_friction",
                    valence=classify_valence("complex_request", ""),
                ))

    return observations


def detect_meta_thread(
    cluster: list[EventRow],
    quick_tags: dict[int, dict],
) -> list[PatternObservation]:
    """
    Detect meta threads: 2+ events tagged meta_reflection within 2 hours.
    """
    meta_events = [
        ev for ev in cluster
        if quick_tags.get(ev.id, {}).get("signal_type") == "meta_reflection"
    ]
    meta_events.sort(key=lambda e: e.timestamp)

    observations: list[PatternObservation] = []
    used: set[int] = set()

    for group in events_within_window(meta_events, META_THREAD_WINDOW_MINUTES):
        if len(group) >= META_THREAD_THRESHOLD:
            new_ids = [e.id for e in group if e.id not in used]
            if len(new_ids) >= META_THREAD_THRESHOLD:
                used.update(new_ids)
                observations.append(PatternObservation(
                    pattern_type="meta_thread",
                    source=group[0].source,
                    event_ids=[e.id for e in group],
                    signal_type="meta_thread",
                    urgency="normal",
                    posture_hint="structural_coherence",
                    valence=classify_valence("meta_thread", ""),
                ))

    return observations


def detect_philosophy_thread(
    cluster: list[EventRow],
    quick_tags: dict[int, dict],
) -> list[PatternObservation]:
    """
    Detect philosophy threads: 2+ events tagged philosophy within 4 hours.

    Philosophy threads differ from meta_reflection threads in that they involve
    conceptual exploration, phenomenological language, ToL arc references, or
    epistemic framework questions — not operational retrospection. They route to
    the philosophy handler, not generic meta routing.
    """
    philosophy_events = [
        ev for ev in cluster
        if quick_tags.get(ev.id, {}).get("signal_type") == "philosophy"
    ]
    philosophy_events.sort(key=lambda e: e.timestamp)

    observations: list[PatternObservation] = []
    used: set[int] = set()

    for group in events_within_window(philosophy_events, PHILOSOPHY_THREAD_WINDOW_MINUTES):
        if len(group) >= PHILOSOPHY_THREAD_THRESHOLD:
            new_ids = [e.id for e in group if e.id not in used]
            if len(new_ids) >= PHILOSOPHY_THREAD_THRESHOLD:
                used.update(new_ids)
                observations.append(PatternObservation(
                    pattern_type="philosophy_thread",
                    source=group[0].source,
                    event_ids=[e.id for e in group],
                    signal_type="philosophy_thread",
                    urgency="normal",
                    posture_hint="attunement",
                    valence=classify_valence("philosophy_thread", ""),
                ))

    return observations


def detect_all_patterns(
    cluster: list[EventRow],
    quick_tags: dict[int, dict],
) -> list[PatternObservation]:
    """Run all five pattern detectors against a cluster. Pure function."""
    return (
        detect_design_session(cluster, quick_tags)
        + detect_brainstorm_mode(cluster, quick_tags)
        + detect_complex_request(cluster, quick_tags)
        + detect_meta_thread(cluster, quick_tags)
        + detect_philosophy_thread(cluster, quick_tags)
    )


# ---------------------------------------------------------------------------
# Tag revision logic (pure)
# ---------------------------------------------------------------------------

def build_revised_tag(
    event_id: int,
    pattern: PatternObservation,
    quick_tag: dict | None,
) -> ClassificationTag:
    """
    Build a slow-v1 ClassificationTag from a detected pattern, inheriting signal
    flags from the quick-v1 tag where available.
    """
    # Carry forward signal flags from quick-v1 if present
    sig_a = bool(quick_tag.get("signal_a", 0)) if quick_tag else False
    sig_b = bool(quick_tag.get("signal_b", 0)) if quick_tag else False
    sig_c = bool(quick_tag.get("signal_c", 0)) if quick_tag else False
    sig_d = bool(quick_tag.get("signal_d", 0)) if quick_tag else False
    sig_e = bool(quick_tag.get("signal_e", 0)) if quick_tag else False

    significant = sig_a or sig_b or sig_c or sig_d or sig_e

    return ClassificationTag(
        entry_id=str(event_id),
        entry_type="event",
        classifier="slow-v1",
        significant=significant,
        signal_a=sig_a,
        signal_b=sig_b,
        signal_c=sig_c,
        signal_d=sig_d,
        signal_e=sig_e,
        confidence="medium",
        signal_type=pattern.signal_type,
        urgency=pattern.urgency,
        posture_hint=pattern.posture_hint,
        notes=f"revised by slow-v1 | pattern: {pattern.pattern_type} | "
              f"contributing_events: {pattern.event_ids}",
    )


def build_passthrough_tag(event_id: int, quick_tag: dict | None) -> ClassificationTag:
    """
    Build a slow-v1 ClassificationTag for an event that was examined but matched
    no cross-event pattern. Carries forward the quick-v1 signal flags and dimensions
    unchanged, marking the event as 'reviewed, no pattern' so future passes skip it.

    Without this, events with no pattern match never receive a slow-v1 tag and are
    re-fetched (and re-examined for patterns) on every subsequent cycle.
    """
    sig_a = bool(quick_tag.get("signal_a", 0)) if quick_tag else False
    sig_b = bool(quick_tag.get("signal_b", 0)) if quick_tag else False
    sig_c = bool(quick_tag.get("signal_c", 0)) if quick_tag else False
    sig_d = bool(quick_tag.get("signal_d", 0)) if quick_tag else False
    sig_e = bool(quick_tag.get("signal_e", 0)) if quick_tag else False
    significant = sig_a or sig_b or sig_c or sig_d or sig_e

    return ClassificationTag(
        entry_id=str(event_id),
        entry_type="event",
        classifier="slow-v1",
        significant=significant,
        signal_a=sig_a,
        signal_b=sig_b,
        signal_c=sig_c,
        signal_d=sig_d,
        signal_e=sig_e,
        confidence="low",
        signal_type=quick_tag.get("signal_type", "system_observation") if quick_tag else "system_observation",
        urgency=quick_tag.get("urgency", "normal") if quick_tag else "normal",
        posture_hint=quick_tag.get("posture_hint", "minimal_cognitive_friction") if quick_tag else "minimal_cognitive_friction",
        notes="reviewed by slow-v1 | no cross-event pattern detected",
    )


# ---------------------------------------------------------------------------
# Main processing pass
# ---------------------------------------------------------------------------

def run_pass(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """
    One full processing pass.

    Returns (events_processed, tags_revised, patterns_found).
    """
    events = read_recent_events(conn)
    if not events:
        log.info("No recent classified events found — nothing to reclassify.")
        return 0, 0, 0

    # Fetch quick-v1 tags for all events in one query (avoid N+1)
    event_ids = [str(e.id) for e in events]
    placeholders = ",".join("?" * len(event_ids))
    cursor = conn.execute(f"""
        SELECT entry_id, signal_type, urgency, posture_hint,
               significant, signal_a, signal_b, signal_c, signal_d, signal_e, confidence
        FROM classification_tags
        WHERE classifier = 'quick-v1' AND entry_id IN ({placeholders})
    """, event_ids)
    quick_tags: dict[int, dict] = {
        int(r["entry_id"]): dict(r) for r in cursor.fetchall()
    }

    # Cluster events into 30-minute source windows
    clusters = cluster_events_by_source_and_window(events, CLUSTER_WINDOW_MINUTES)

    tags_revised = 0
    patterns_found = 0
    processed_event_ids: set[int] = set()
    pattern_event_ids: set[int] = set()

    for cluster in clusters:
        patterns = detect_all_patterns(cluster, quick_tags)
        for pattern in patterns:
            patterns_found += 1
            log.info(
                "Pattern detected: %s | source=%s | events=%s",
                pattern.pattern_type,
                pattern.source,
                pattern.event_ids,
            )

            # Write a pattern_observation event to the events table
            write_pattern_event(conn, pattern)

            # Revise the classification tag for each contributing event
            for event_id in pattern.event_ids:
                quick_tag = quick_tags.get(event_id)
                revised = build_revised_tag(event_id, pattern, quick_tag)
                write_tag(conn, revised)
                tags_revised += 1
                pattern_event_ids.add(event_id)

        processed_event_ids.update(e.id for e in cluster)

    # Write passthrough slow-v1 tags for events that were examined but matched no
    # pattern. Without this, they have no slow-v1 tag and are re-fetched and
    # re-examined on every subsequent cycle, producing identical "0 patterns found"
    # output indefinitely.
    unmatched_ids = processed_event_ids - pattern_event_ids
    for event_id in unmatched_ids:
        quick_tag = quick_tags.get(event_id)
        passthrough = build_passthrough_tag(event_id, quick_tag)
        write_tag(conn, passthrough)

    return len(processed_event_ids), tags_revised, patterns_found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Slow reclassifier — Layer 4 of the multi-timescale architecture."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once",
        action="store_true",
        help="Run one pass and exit.",
    )
    mode.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between loop iterations (default: {DEFAULT_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=MEMORY_DB_PATH,
        help="Path to memory.db (default: $LOBSTER_WORKSPACE/data/memory.db).",
    )
    return parser.parse_args()


def run_once(
    db_path: Path,
    checkpoint_path: Path = CHECKPOINT_PATH,
) -> None:
    """Run one classification pass and exit, saving a checkpoint on success."""
    if not db_path.exists():
        log.warning("memory.db not found at %s — skipping.", db_path)
        return

    t0 = time.monotonic()
    conn = open_db(db_path)
    try:
        ensure_classification_table(conn)
        processed, revised, patterns = run_pass(conn)
        elapsed_ms = (time.monotonic() - t0) * 1000
        write_run_log(processed, revised, patterns, elapsed_ms)
        log.info(
            "Pass complete: %d events processed, %d tags revised, %d patterns found (%.1f ms)",
            processed, revised, patterns, elapsed_ms,
        )
        # Save checkpoint so next invocation skips if no new events arrive.
        save_checkpoint(checkpoint_path, max_event_id(conn))
    finally:
        conn.close()


def run_loop(
    db_path: Path,
    interval: int,
    checkpoint_path: Path = CHECKPOINT_PATH,
) -> None:
    """Run continuously, sleeping `interval` seconds between passes."""
    log.info("Starting slow-reclassifier loop (interval=%ds).", interval)
    while True:
        if not should_run(checkpoint_path, db_path):
            log.info("Sleeping %d seconds (no new events).", interval)
            time.sleep(interval)
            continue
        run_once(db_path, checkpoint_path)
        log.info("Sleeping %d seconds.", interval)
        time.sleep(interval)


def main() -> None:
    args = parse_args()
    checkpoint_path = CHECKPOINT_PATH
    if args.once:
        # --once mode: exit immediately (code 0) if nothing new to classify.
        if not should_run(checkpoint_path, args.db):
            log.info("No new events since last checkpoint — exiting without classification.")
            return
        run_once(args.db, checkpoint_path)
    else:
        run_loop(args.db, args.interval, checkpoint_path)


if __name__ == "__main__":
    main()
