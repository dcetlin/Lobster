#!/usr/bin/env python3
"""
frontier_classifier.py — Three-signal classifier for frontier document routing.

Determines whether a philosophy session output constitutes genuine re-engagement
with one or more frontier domains (and should trigger a frontier document update),
versus a status review or peripheral mention (no update warranted).

Architecture: three signals combined into a classification decision.

Signal 1 — Event type
    Inferred from the source file's path/name pattern.
    A philosophy-explore file carries more re-engagement weight than a status
    check or passing reference.

Signal 2 — Content orientation
    Does the session work forward from a live edge? Or does it ask "where are we"
    and re-describe known territory? Detected via keyword/phrase patterns.

Signal 3 — Time since last touch
    A weak prior only. Longer since last touch slightly raises re-engagement
    probability. It does not determine classification.

The five frontier domains:
    orient              — Vision Object, routing quality, decision discipline
    collapse_topology   — System transitions mapped to ToL stages
    poiesis             — Creative expression, poiema risk
    tol_arc             — ToL 5-stage arc as Lobster diagnostic
    approximate_embodiment — Partial fluency, borrowing motion from recognition

Usage (programmatic — not a CLI entrypoint):
    from src.harvest.frontier_classifier import classify_session

    result = classify_session(
        text=session_text,
        source_path=Path("2026-03-29-2000-philosophy-explore.md"),
        frontier_dir=Path("~/lobster-user-config/memory/canonical/frontiers/"),
    )
    for domain, signal in result.domain_signals.items():
        if signal.is_re_engagement:
            print(f"Re-engagement detected: {domain} (confidence {signal.confidence:.2f})")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Domain definitions — each domain has a name, keyword signals, and a
# canonical file stem for the frontier document.
# ---------------------------------------------------------------------------

DOMAINS: dict[str, "DomainSpec"] = {}  # populated below


@dataclass(frozen=True)
class DomainSpec:
    """Static definition of a frontier domain."""
    name: str                        # canonical identifier (snake_case)
    label: str                       # human-readable label
    file_stem: str                   # filename stem in frontiers/ directory
    # Keyword patterns that signal content engagement with this domain.
    # Each pattern is a compiled regex matched against the session text.
    engagement_patterns: tuple[re.Pattern, ...]
    # Patterns whose presence suggests a status-review posture rather than
    # re-engagement. These raise the status-review signal.
    status_review_patterns: tuple[re.Pattern, ...]


def _compile(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns)


DOMAINS = {
    "orient": DomainSpec(
        name="orient",
        label="Orient",
        file_stem="frontier-orient",
        engagement_patterns=_compile(
            r"\bvision object\b",
            r"\bjourney guide\b",
            r"\brouting quality\b",
            r"\bdecision.{0,20}discipline\b",
            r"\borientation quality\b",
            r"\battunement.{0,30}gradient\b",
            r"\bbefore.{0,30}loading\b",
            r"\bsensing.{0,30}before\b",
            r"\bkoan.{0,20}scaffold\b",
            r"\borient.{0,20}stage\b",
            r"\bdiscernment.{0,30}phase\b",
        ),
        status_review_patterns=_compile(
            r"\bwhere (are|is|do) (we|the system|it)\b",
            r"\bcurrent.{0,20}status of.{0,20}orient\b",
            r"\bstate of.{0,20}orient\b",
        ),
    ),
    "collapse_topology": DomainSpec(
        name="collapse_topology",
        label="Collapse topology",
        file_stem="frontier-collapse-topology",
        engagement_patterns=_compile(
            r"\bcollapse.{0,30}topolog\b",
            r"\bcollapse.{0,20}risk\b",
            r"\bcollapse.{0,20}mechanics\b",
            r"\brecognition.{0,30}collapse\b",
            r"\btol.{0,30}stage.{0,30}transition\b",
            r"\bsystem.{0,30}transition.{0,30}tol\b",
            r"\bcollapse.{0,30}trigger\b",
            r"\bcollapse.{0,30}pattern\b",
            r"\bbasin capture\b",
            r"\battractor.{0,30}basin\b",
        ),
        status_review_patterns=_compile(
            r"\bwhere (are|is) (we|the system).{0,30}collapse\b",
            r"\bcurrent state.{0,20}collapse\b",
        ),
    ),
    "poiesis": DomainSpec(
        name="poiesis",
        label="Poiesis / poiema",
        file_stem="frontier-poiesis",
        engagement_patterns=_compile(
            r"\bpoiesis\b",
            r"\bpoiema\b",
            r"\bpoietic\b",
            r"\bcreative.{0,30}expression.{0,30}technical\b",
            r"\btechnical.{0,30}creative.{0,30}continuum\b",
            r"\bmaking.{0,30}relation\b",
            r"\bpoiesis.{0,30}production\b",
            r"\bproduction.{0,30}poiesis\b",
            r"\bcreative.{0,30}continuous\b",
        ),
        status_review_patterns=_compile(
            r"\bwhat is.{0,20}poiesis\b",
            r"\bdefinition of.{0,20}poiesis\b",
        ),
    ),
    "tol_arc": DomainSpec(
        name="tol_arc",
        label="ToL arc",
        file_stem="frontier-tol-arc",
        engagement_patterns=_compile(
            r"\btheory of learning\b",
            r"\btol.{0,15}arc\b",
            r"\btol.{0,15}stage\b",
            r"\bstage [1-5].{0,30}(discernment|coherence|embodiment|tol|arc)\b",
            r"\b(discernment|coherence|embodiment).{0,30}stage\b",
            r"\blobster.{0,30}develop\b",
            r"\bdevelopmental.{0,30}diagnostic\b",
            r"\bstage [1-5] characteristics\b",
            r"\bstage [1-5] behavior\b",
        ),
        status_review_patterns=_compile(
            r"\bwhat (are|is) the (tol|theory of learning) stages\b",
            r"\bexplain (the )?tol\b",
        ),
    ),
    "approximate_embodiment": DomainSpec(
        name="approximate_embodiment",
        label="Approximate embodiment",
        file_stem="frontier-approximate-embodiment",
        engagement_patterns=_compile(
            r"\bapproximate embodiment\b",
            r"\battractor convergence\b",
            r"\blandscape density\b",
            r"\bconvergence reliability\b",
            r"\btrajectory continuity\b",
            r"\bborrowing motion from recognition\b",
            r"\bpartial fluency\b",
            r"\bprocedural memory\b",
            r"\bprompt.{0,30}compressed.{0,30}attunement\b",
            r"\bembodiment.{0,30}paradox\b",
            r"\btoken.{0,30}footprint\b",
            r"\bminimum.{0,30}sufficient.{0,30}token\b",
        ),
        status_review_patterns=_compile(
            r"\bwhat is approximate embodiment\b",
            r"\bdefine approximate embodiment\b",
        ),
    ),
}


# ---------------------------------------------------------------------------
# Event type classification — Signal 1
# ---------------------------------------------------------------------------

class EventType(StrEnum):
    """Enumeration of session event types inferred from file naming."""
    PHILOSOPHY_EXPLORE = "philosophy_explore"
    SYNTHESIS = "synthesis"
    WEEKLY_RETRO = "weekly_retro"
    VOICE_NOTE = "voice_note"
    NAVIGATION_NOTE = "navigation_note"
    UNKNOWN = "unknown"


# Weight multipliers for each event type. Higher = more likely to be
# classified as re-engagement.
EVENT_TYPE_WEIGHTS: dict[str, float] = {
    EventType.PHILOSOPHY_EXPLORE: 1.0,
    EventType.SYNTHESIS: 1.3,         # syntheses are deliberately convergent
    EventType.WEEKLY_RETRO: 0.8,
    EventType.VOICE_NOTE: 1.1,        # voice notes carry spontaneous re-engagement
    EventType.NAVIGATION_NOTE: 1.2,
    EventType.UNKNOWN: 0.7,
}


def classify_event_type(source_path: Path) -> EventType:
    """Classify the event type from the source file's name. Pure function."""
    name = source_path.name.lower()
    if "philosophy-explore" in name or "philosophy_explore" in name:
        return EventType.PHILOSOPHY_EXPLORE
    if "synthesis" in name:
        return EventType.SYNTHESIS
    if "weekly" in name:
        return EventType.WEEKLY_RETRO
    if "voice" in name or "audio" in name:
        return EventType.VOICE_NOTE
    if "navigation" in name or "attractor" in name:
        return EventType.NAVIGATION_NOTE
    return EventType.UNKNOWN


# ---------------------------------------------------------------------------
# Content orientation — Signal 2
# ---------------------------------------------------------------------------

# Phrases that indicate working forward from a live edge (re-engagement)
_FORWARD_EDGE_PATTERNS: tuple[re.Pattern, ...] = _compile(
    r"\bhas not been examined\b",
    r"\bdid not get past\b",
    r"\bwhat the.{0,20}session did not\b",
    r"\bopen question\b",
    r"\blive edge\b",
    r"\bnew aperture\b",
    r"\bgenuine limit\b",
    r"\bproductive recursion\b",
    r"\bprecision gap\b",
    r"\bstructural claim\b",
    r"\bidentif(y|ied|ies) a.{0,20}(limit|gap|tension|paradox)\b",
    r"\bnot yet (asked|examined|resolved)\b",
    r"\bstated (as )?precisely\b",
    r"\bfind(s)? something real\b",
    r"\bis this (thread|domain|tension|question) live\b",
    r"\bgenuine re.{0,5}engagement\b",
    r"\bworking forward\b",
    r"\bnew formulation\b",
)

# Phrases that indicate status review posture
_STATUS_REVIEW_PATTERNS: tuple[re.Pattern, ...] = _compile(
    r"\bwhere (are|is|do) we\b",
    r"\bcurrent state\b",
    r"\bsummary of\b",
    r"\bbrief(ly)? (describe|recap|review|cover|outline)\b",
    r"\bstatus (check|review|report)\b",
    r"\bremind me\b",
    r"\bwhat (have|has) (we|the system|lobster) (done|accomplished|established)\b",
    r"\bwhat (are|were) the (main|key|primary) (findings|conclusions|takeaways)\b",
    r"\brecap\b",
)


def score_content_orientation(text: str) -> float:
    """
    Score text for re-engagement posture vs status-review posture.

    Returns a float in [0.0, 1.0] where:
        1.0 = strongly re-engagement (working forward from a live edge)
        0.0 = strongly status-review
        0.5 = ambiguous / neither signal dominant

    Pure function.
    """
    forward_hits = sum(1 for p in _FORWARD_EDGE_PATTERNS if p.search(text))
    review_hits = sum(1 for p in _STATUS_REVIEW_PATTERNS if p.search(text))

    total = forward_hits + review_hits
    if total == 0:
        return 0.5  # no signal — ambiguous

    return forward_hits / total


# ---------------------------------------------------------------------------
# Time-since-last-touch — Signal 3
# ---------------------------------------------------------------------------

def seconds_since_last_touch(frontier_path: Path) -> float | None:
    """
    Return seconds since the frontier document was last modified.
    Returns None if the document does not yet exist (first touch ever).
    Pure function (reads filesystem metadata only).
    """
    if not frontier_path.exists():
        return None
    mtime = frontier_path.stat().st_mtime
    now = datetime.now(timezone.utc).timestamp()
    return max(0.0, now - mtime)


def time_prior_weight(seconds: float | None) -> float:
    """
    Convert seconds-since-last-touch to a weight adjustment.

    Weak prior only — per the architecture spec in issue #248.
    The adjustment is bounded to ±0.1 so it cannot dominate the other signals.
    Returns a float to be added to the confidence score.

    Pure function.
    """
    if seconds is None:
        # Never touched — weak positive prior (first genuine touch is likely re-engagement)
        return 0.05
    hours = seconds / 3600
    if hours > 72:
        return 0.08   # more than 3 days: mild boost
    if hours > 24:
        return 0.04   # more than 1 day: slight boost
    if hours < 1:
        return -0.04  # touched in the last hour: slight suppression (might be status review)
    return 0.0        # recent but not very recent: no adjustment


# ---------------------------------------------------------------------------
# Domain-level signal scoring
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DomainSignal:
    """Classification result for a single frontier domain."""
    domain: str
    label: str
    engagement_hit_count: int         # number of engagement patterns matched
    status_review_hit_count: int      # number of domain-specific status patterns matched
    content_orientation_score: float  # 0.0–1.0 from score_content_orientation
    event_type: EventType
    event_weight: float
    time_prior: float
    confidence: float                 # combined score in [0.0, 1.0]
    is_re_engagement: bool            # final classification
    # Snippets of text that triggered the engagement patterns (first match per pattern)
    evidence: tuple[str, ...]


def _extract_evidence(text: str, patterns: tuple[re.Pattern, ...]) -> tuple[str, ...]:
    """Extract up to 3 snippet strings for each matched pattern. Pure function."""
    snippets = []
    for p in patterns:
        m = p.search(text)
        if m:
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            snippet = text[start:end].replace("\n", " ").strip()
            snippets.append(f"...{snippet}...")
            if len(snippets) >= 3:
                break
    return tuple(snippets)


def _compute_domain_confidence(
    engagement_hits: int,
    status_hits: int,
    content_score: float,
    event_weight: float,
    time_prior: float,
) -> float:
    """
    Combine all three signals into a confidence score in [0.0, 1.0].

    The formula weights Signal 2 (content orientation) most heavily,
    with Signal 1 (event type) as a multiplier and Signal 3 (time) as a trim.

    Pure function.
    """
    if engagement_hits == 0:
        return 0.0

    # Base: how many engagement patterns hit, scaled to [0, 1] with diminishing returns
    engagement_base = min(engagement_hits / 4.0, 1.0)

    # Reduce by status hits (domain-specific status review signals)
    status_penalty = min(status_hits * 0.15, 0.4)

    # Content orientation score blends in (weight 0.4)
    content_contribution = content_score * 0.4

    raw = (engagement_base - status_penalty) * event_weight + content_contribution + time_prior
    return max(0.0, min(1.0, raw))


# Threshold for classification as re-engagement
RE_ENGAGEMENT_THRESHOLD = 0.45


def classify_domain(
    text: str,
    domain: DomainSpec,
    event_type: EventType,
    content_orientation: float,
    time_prior: float,
) -> DomainSignal:
    """
    Classify whether a session text constitutes re-engagement with a frontier domain.

    Pure function — all I/O (reading files, checking mtimes) happens outside.
    """
    engagement_hits = sum(1 for p in domain.engagement_patterns if p.search(text))
    status_hits = sum(1 for p in domain.status_review_patterns if p.search(text))
    event_weight = EVENT_TYPE_WEIGHTS.get(event_type, 0.7)

    confidence = _compute_domain_confidence(
        engagement_hits=engagement_hits,
        status_hits=status_hits,
        content_score=content_orientation,
        event_weight=event_weight,
        time_prior=time_prior,
    )

    evidence = _extract_evidence(text, domain.engagement_patterns) if engagement_hits else ()

    return DomainSignal(
        domain=domain.name,
        label=domain.label,
        engagement_hit_count=engagement_hits,
        status_review_hit_count=status_hits,
        content_orientation_score=content_orientation,
        event_type=event_type,
        event_weight=event_weight,
        time_prior=time_prior,
        confidence=confidence,
        is_re_engagement=confidence >= RE_ENGAGEMENT_THRESHOLD,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Explicit frontier_advances from action_seeds YAML
# ---------------------------------------------------------------------------

def extract_explicit_advances(action_seeds: dict | None) -> frozenset[str]:
    """
    Extract explicitly declared frontier advances from the action_seeds block.

    Session outputs may include a `frontier_advances` list in their action_seeds
    YAML block. This allows session authors to declare domain advancement directly
    rather than relying solely on keyword detection.

    Example YAML:
        action_seeds:
          frontier_advances:
            - orient
            - approximate_embodiment

    Returns a frozenset of domain names. Pure function.
    """
    if not action_seeds:
        return frozenset()
    raw = action_seeds.get("action_seeds", {}) or {}
    explicit = raw.get("frontier_advances") or []
    return frozenset(
        name.strip().lower().replace(" ", "_").replace("-", "_")
        for name in explicit
        if isinstance(name, str)
    )


# ---------------------------------------------------------------------------
# Top-level session classifier
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionClassification:
    """Complete classification result for one session output."""
    source_path: Path
    event_type: EventType
    content_orientation_score: float
    domain_signals: dict[str, DomainSignal]
    explicit_advances: frozenset[str]

    @property
    def re_engagement_domains(self) -> list[str]:
        """Domains classified as genuine re-engagement (implicit + explicit)."""
        implicit = [
            name for name, sig in self.domain_signals.items()
            if sig.is_re_engagement
        ]
        # Explicit advances always count, even if below the confidence threshold
        explicit_valid = [
            name for name in self.explicit_advances
            if name in DOMAINS
        ]
        return sorted(set(implicit) | set(explicit_valid))

    def has_re_engagement(self) -> bool:
        return bool(self.re_engagement_domains)


def classify_session(
    text: str,
    source_path: Path,
    frontier_dir: Path,
    action_seeds: dict | None = None,
) -> SessionClassification:
    """
    Classify a session output for frontier domain advancement.

    Applies the three-signal architecture:
        Signal 1: event type from source_path name
        Signal 2: content orientation from session text
        Signal 3: time-since-last-touch from frontier document mtime

    Also extracts any explicit `frontier_advances` from the action_seeds block.

    This function reads filesystem metadata (mtime) but does not write anything.
    It is the pure detection step; routing happens in frontier_router.py.
    """
    event_type = classify_event_type(source_path)
    content_orientation = score_content_orientation(text)
    explicit_advances = extract_explicit_advances(action_seeds)

    domain_signals: dict[str, DomainSignal] = {}
    for domain_name, domain_spec in DOMAINS.items():
        frontier_path = frontier_dir / f"{domain_spec.file_stem}.md"
        elapsed = seconds_since_last_touch(frontier_path)
        prior = time_prior_weight(elapsed)

        domain_signals[domain_name] = classify_domain(
            text=text,
            domain=domain_spec,
            event_type=event_type,
            content_orientation=content_orientation,
            time_prior=prior,
        )

    return SessionClassification(
        source_path=source_path,
        event_type=event_type,
        content_orientation_score=content_orientation,
        domain_signals=domain_signals,
        explicit_advances=explicit_advances,
    )
