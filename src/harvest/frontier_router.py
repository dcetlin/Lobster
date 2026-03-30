#!/usr/bin/env python3
"""
frontier_router.py — Route detected frontier advancements to living frontier documents.

When classify_session() identifies that a philosophy session constitutes genuine
re-engagement with one or more frontier domains, this module appends a structured
entry to the corresponding frontier document.

The frontier documents live in:
    ~/lobster-user-config/memory/canonical/frontiers/

Each domain has one file (e.g. frontier-orient.md). Entries are appended as
timestamped sections. The file structure is preserved for human readability
and future machine parsing.

The router does NOT overwrite any frontier document content — it appends only.
Replacement of the three-field structure (last coherence flash / open question /
posture held against) is a human action, not an automated one. The router's job
is to ensure that relevant session material is surfaced at the right document,
not to maintain the document's content.

Usage:
    from src.harvest.frontier_router import route_to_frontiers, RouteResult

    result = route_to_frontiers(
        classification=session_classification,
        session_text=text,
        source_filename="2026-03-29-2000-philosophy-explore.md",
        frontier_dir=Path("~/lobster-user-config/memory/canonical/frontiers/"),
        dry_run=False,
    )
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .frontier_classifier import DOMAINS, DomainSignal, SessionClassification


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DomainRoute:
    """Result of routing a single domain advancement."""
    domain: str
    label: str
    frontier_path: Path
    appended: bool          # True if entry was written
    entry_preview: str      # first 120 chars of the appended entry
    reason: str             # why this was routed (for logging)
    was_explicit: bool      # True if declared in frontier_advances


@dataclass(frozen=True)
class RouteResult:
    """Aggregate result of routing a complete session classification."""
    source_filename: str
    routes: tuple[DomainRoute, ...]
    errors: tuple[str, ...]

    @property
    def routed_count(self) -> int:
        return sum(1 for r in self.routes if r.appended)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.routes if not r.appended)


# ---------------------------------------------------------------------------
# Entry formatting — pure functions
# ---------------------------------------------------------------------------

_SEPARATOR = "---"

def _format_confidence_bar(confidence: float) -> str:
    """Render confidence as a simple text indicator. Pure function."""
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.6:
        return "medium-high"
    if confidence >= 0.45:
        return "medium"
    return "low"


def _extract_session_excerpt(
    text: str,
    domain_signal: DomainSignal,
    max_chars: int = 600,
) -> str:
    """
    Extract the most relevant excerpt from the session text for a given domain.

    Strategy: find the passage with the highest density of engagement pattern
    matches, then extract a window around it.

    Pure function.
    """
    if not domain_signal.evidence:
        # No specific evidence — take the first substantive paragraph
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 80]
        return paragraphs[0][:max_chars] + "..." if paragraphs else ""

    # Use the first evidence snippet as the anchor, then find it in the text
    # and expand to a full paragraph
    anchor_text = domain_signal.evidence[0].strip(".")
    # Remove the "..." wrapping
    anchor_clean = re.sub(r"^\.\.\.", "", anchor_text).strip()
    anchor_clean = re.sub(r"\.\.\.$", "", anchor_clean).strip()

    idx = text.find(anchor_clean[:30])  # search by prefix
    if idx == -1:
        return anchor_text[:max_chars]

    # Expand to nearest paragraph boundaries
    para_start = text.rfind("\n\n", 0, idx)
    para_start = para_start + 2 if para_start != -1 else max(0, idx - 200)
    para_end = text.find("\n\n", idx)
    para_end = para_end if para_end != -1 else min(len(text), idx + 400)

    excerpt = text[para_start:para_end].strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars] + "..."
    return excerpt


def _format_frontier_entry(
    source_filename: str,
    domain_signal: DomainSignal,
    session_text: str,
    was_explicit: bool,
    timestamp: datetime,
) -> str:
    """
    Format a timestamped entry to append to a frontier document.

    The entry records:
    - When this session ran
    - The detection signals that triggered routing
    - A passage from the session that most directly engages this domain
    - Whether it was explicitly declared or classifier-detected

    Pure function.
    """
    ts_str = timestamp.strftime("%Y-%m-%d %H:%M UTC")
    date_str = timestamp.strftime("%Y-%m-%d")
    confidence_label = _format_confidence_bar(domain_signal.confidence)
    detection_method = "explicit (frontier_advances)" if was_explicit else f"classifier ({confidence_label} confidence)"
    event_label = domain_signal.event_type.replace("_", "-")

    excerpt = _extract_session_excerpt(session_text, domain_signal)
    excerpt_lines = "\n".join(f"> {line}" for line in excerpt.splitlines()) if excerpt else "> (no excerpt)"

    entry = f"""
### Session entry — {ts_str}

**Source**: `{source_filename}` ({event_label})
**Detection**: {detection_method}
**Engagement signals**: {domain_signal.engagement_hit_count} pattern(s) matched
**Content orientation**: {domain_signal.content_orientation_score:.2f} (1.0 = working from live edge)

**Relevant passage**:

{excerpt_lines}

"""
    return entry.strip()


def _ensure_frontier_document(frontier_path: Path, domain_label: str, dry_run: bool) -> None:
    """
    Ensure the frontier document exists with a minimal scaffold.

    Only creates the file if it does not already exist. Does not overwrite.
    The scaffold is intentionally sparse — D3 will seed the three-field structure.
    """
    if frontier_path.exists() or dry_run:
        return

    frontier_path.parent.mkdir(parents=True, exist_ok=True)
    scaffold = f"""# Frontier: {domain_label}

*Living frontier document — tracks the moving edge of understanding.*
*Updated by Lobster when a session constitutes genuine re-engagement with this domain.*
*The three-field structure below (last coherence flash / open question / posture held against)*
*is populated by Dan, not by automated routing.*

---

## Frontier state

*(Not yet populated — this document was created by the routing system as a scaffold.)*
*(Populate with: last coherence flash, open question, posture held against.)*

---

## Session entries

*(Entries below are appended automatically when the classifier detects re-engagement.)*

"""
    frontier_path.write_text(scaffold, encoding="utf-8")


def _append_entry_to_frontier(
    frontier_path: Path,
    entry: str,
    dry_run: bool,
) -> None:
    """
    Append a formatted entry to a frontier document.

    Appends after the last `---` separator in the ## Session entries section,
    or at end of file if no such section exists.

    Side-effectful — writes to filesystem.
    """
    if dry_run:
        print(f"  [dry-run] Would append entry to {frontier_path}")
        print(f"  [dry-run] Entry preview: {entry[:100]}...")
        return

    current = frontier_path.read_text(encoding="utf-8") if frontier_path.exists() else ""

    # Ensure there's a ## Session entries section
    if "## Session entries" not in current:
        current = current.rstrip() + "\n\n## Session entries\n\n"

    current = current.rstrip() + f"\n\n{entry}\n"
    frontier_path.write_text(current, encoding="utf-8")


# ---------------------------------------------------------------------------
# Routing orchestration
# ---------------------------------------------------------------------------

def route_to_frontiers(
    classification: SessionClassification,
    session_text: str,
    source_filename: str,
    frontier_dir: Path,
    dry_run: bool = False,
    timestamp: datetime | None = None,
) -> RouteResult:
    """
    Route a classified session to all detected frontier domains.

    For each domain where re-engagement is detected (implicit or explicit),
    appends a structured entry to the corresponding frontier document.

    Structured as: pure decision logic → bounded side effects (file writes).
    Errors are collected per-domain and reported without aborting the run.
    """
    frontier_dir = frontier_dir.expanduser().resolve()
    ts = timestamp or datetime.now(timezone.utc)
    routes: list[DomainRoute] = []
    errors: list[str] = []

    re_engagement_domains = set(classification.re_engagement_domains)

    for domain_name, domain_spec in DOMAINS.items():
        signal = classification.domain_signals[domain_name]
        was_explicit = domain_name in classification.explicit_advances
        is_routed = domain_name in re_engagement_domains

        if not is_routed:
            routes.append(DomainRoute(
                domain=domain_name,
                label=domain_spec.label,
                frontier_path=frontier_dir / f"{domain_spec.file_stem}.md",
                appended=False,
                entry_preview="",
                reason="below re-engagement threshold",
                was_explicit=False,
            ))
            continue

        frontier_path = frontier_dir / f"{domain_spec.file_stem}.md"
        entry = _format_frontier_entry(
            source_filename=source_filename,
            domain_signal=signal,
            session_text=session_text,
            was_explicit=was_explicit,
            timestamp=ts,
        )

        try:
            _ensure_frontier_document(frontier_path, domain_spec.label, dry_run)
            _append_entry_to_frontier(frontier_path, entry, dry_run)

            method = "explicit" if was_explicit else f"confidence={signal.confidence:.2f}"
            routes.append(DomainRoute(
                domain=domain_name,
                label=domain_spec.label,
                frontier_path=frontier_path,
                appended=True,
                entry_preview=entry[:120],
                reason=f"re-engagement detected ({method})",
                was_explicit=was_explicit,
            ))
            if not dry_run:
                print(f"  Routed to frontier: {domain_spec.label} → {frontier_path.name}")
        except Exception as exc:
            msg = f"Failed to write frontier entry for {domain_name}: {exc}"
            print(f"  ERROR: {msg}")
            errors.append(msg)
            routes.append(DomainRoute(
                domain=domain_name,
                label=domain_spec.label,
                frontier_path=frontier_path,
                appended=False,
                entry_preview="",
                reason=f"write error: {exc}",
                was_explicit=was_explicit,
            ))

    return RouteResult(
        source_filename=source_filename,
        routes=tuple(routes),
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# CLI entrypoint — run as a standalone script on a single session file
# ---------------------------------------------------------------------------

def _run_cli() -> int:
    import argparse
    import sys
    import yaml

    from .frontier_classifier import classify_session

    parser = argparse.ArgumentParser(
        prog="frontier_router",
        description=(
            "Classify a philosophy session output for frontier domain advancement "
            "and route it to the appropriate living frontier documents."
        ),
    )
    parser.add_argument("md_file", nargs="?", help="Path to session .md output file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify and report without writing files")
    parser.add_argument(
        "--frontier-dir",
        default=str(Path.home() / "lobster-user-config" / "memory" / "canonical" / "frontiers"),
        help="Directory containing frontier documents",
    )
    args = parser.parse_args()

    if args.md_file is None:
        parser.print_help()
        return 0

    md_path = Path(args.md_file).expanduser().resolve()
    if not md_path.exists():
        print(f"Error: file not found: {md_path}", file=sys.stderr)
        return 1

    text = md_path.read_text(encoding="utf-8")
    frontier_dir = Path(args.frontier_dir).expanduser().resolve()

    # Try to parse action_seeds for explicit advances
    action_seeds = None
    try:
        fenced = re.search(r"```yaml\s*\n(action_seeds:.*?)```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            action_seeds = yaml.safe_load(fenced.group(1))
    except Exception:
        pass

    classification = classify_session(
        text=text,
        source_path=md_path,
        frontier_dir=frontier_dir,
        action_seeds=action_seeds,
    )

    print(f"\nSession: {md_path.name}")
    print(f"Event type: {classification.event_type}")
    print(f"Content orientation: {classification.content_orientation_score:.2f}")
    if classification.explicit_advances:
        print(f"Explicit advances: {', '.join(sorted(classification.explicit_advances))}")

    print("\nDomain signals:")
    for domain_name, sig in sorted(classification.domain_signals.items()):
        status = "RE-ENGAGEMENT" if sig.is_re_engagement else "skip"
        print(f"  {domain_name:30s}  hits={sig.engagement_hit_count}  "
              f"conf={sig.confidence:.2f}  [{status}]")

    if not classification.has_re_engagement():
        print("\nNo re-engagement detected — no frontier documents will be updated.")
        return 0

    print(f"\nRouting to: {', '.join(classification.re_engagement_domains)}")
    if args.dry_run:
        print("(dry-run — no files will be written)")

    result = route_to_frontiers(
        classification=classification,
        session_text=text,
        source_filename=md_path.name,
        frontier_dir=frontier_dir,
        dry_run=args.dry_run,
    )

    print(f"\nRoute result: {result.routed_count} routed, {result.skipped_count} skipped")
    if result.errors:
        print(f"Errors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  - {err}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_cli())
