#!/usr/bin/env python3
"""
cultivator.py — Philosophy session output cultivator.

Classifies philosophy session outputs as pearls (recognition events / distillations)
or seeds (actionable items), then routes each to the appropriate write-path:

  - Pearl → pending bootup candidate file OR frontier doc update
  - Seed  → GitHub issue (via gh CLI)

Two extraction paths:
  Path A — action_seeds YAML block present: delegate to philosophy_harvester.parse_action_seeds
  Path B — no YAML block: extract prose candidates from section headings and list items,
           then classify each with classify_item

This module is a separate pipeline from philosophy_harvester.py. It does NOT modify
philosophy_harvester.py, frontier_classifier.py, or frontier_router.py.

Usage:
    cd ~/lobster && uv run -m src.harvest.cultivator [--dry-run] [--no-pearls] [--no-seeds] <path>
    cd ~/lobster && uv run -m src.harvest.cultivator --help
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from src.harvest.philosophy_harvester import (
    MemoryObservation,
    parse_action_seeds,
    extract_yaml_block,
    write_bootup_candidate,
    BootupCandidate,
)
from src.orchestration.germinator import _PHILOSOPHICAL_TERMS


# ---------------------------------------------------------------------------
# Data types — frozen dataclasses (immutable by design)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PearlCandidate:
    text: str
    pearl_type: Literal["bootup", "frontier", "insight"]
    source_line: int | None = None


@dataclass(frozen=True)
class SeedCandidate:
    title: str
    body: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    source_line: int | None = None


@dataclass(frozen=True)
class CultivatorInput:
    pearls: tuple[PearlCandidate, ...]
    seeds: tuple[SeedCandidate, ...]
    memory_entries: tuple[MemoryObservation, ...]
    source_path: Path


@dataclass(frozen=True)
class CultivatorConfig:
    repo: str
    pending_bootup_dir: Path
    dry_run: bool


@dataclass(frozen=True)
class PearlRouteResult:
    candidate: PearlCandidate
    action: Literal["pending_file", "frontier", "skipped"]
    output_path: Path | None = None
    issue_url: str | None = None


@dataclass(frozen=True)
class SeedRouteResult:
    candidate: SeedCandidate
    issue_spec: dict  # {title, body, labels} — the spec that would be filed
    issue_url: str | None = None  # None in dry_run
    issue_number: int | None = None


# ---------------------------------------------------------------------------
# Pearl/seed classification — pure function
# ---------------------------------------------------------------------------

# Pearl signals: distillation language, completed insight, no action imperative.
_PEARL_PHRASES = frozenset({
    "i now see",
    "this settles",
    "recognition:",
    "what i understand now",
    "what i understand",
    "i understand now",
    "distillation:",
    "insight:",
    "i see now",
    "what becomes clear",
    "what is clear",
    "it is now clear",
})

_PEARL_PATTERNS = [
    r"\bi\s+now\s+see\b",
    r"\bthis\s+settles\b",
    r"\brecognition\s*:",
    r"\bwhat\s+i\s+understand\s+(now\b)?",
    r"\bdistillation\s*:",
    r"\binsight\s*:",
]

_PEARL_PATTERN_RE = re.compile(
    "|".join(_PEARL_PATTERNS),
    re.IGNORECASE,
)

# Seed signals: action verbs, imperative mood, system component references.
_SEED_ACTION_VERBS = frozenset({
    "implement", "build", "add", "fix", "create", "investigate", "write",
    "design", "refactor", "update", "remove", "delete", "migrate", "port",
    "deploy", "test", "document", "integrate", "extend", "replace", "improve",
    "enable", "disable", "configure", "scaffold", "wire", "hook", "expose",
    "extract", "move", "rename", "audit", "measure", "track", "monitor",
    "ensure", "enforce", "validate",
})

_SEED_ACTION_RE = re.compile(
    r"\b(" + "|".join(re.escape(v) for v in sorted(_SEED_ACTION_VERBS)) + r")\b",
    re.IGNORECASE,
)


def classify_item(text: str, item_type: str = "") -> Literal["pearl", "seed"]:
    """
    Classify a candidate text as "pearl" or "seed".

    Pearl signals:
      - Distillation language: "I now see", "This settles", "Recognition:", etc.
      - No action verb in leading position
      - Uses phenomenological vocabulary (from _PHILOSOPHICAL_TERMS)
      - Describes completed insight rather than a task

    Seed signals (any one is sufficient):
      - Action verb present at sentence-start or in imperative position
      - References a specific system component (code, file, module)
      - Matches GitHub issue patterns

    Default: "seed" — safer, seeds are filed for human review.

    item_type parameter is reserved for callers that have pre-determined type
    information (e.g., from YAML block keys). If item_type is "pearl" or "seed",
    returns that directly without re-classifying.
    """
    if item_type in ("pearl", "seed"):
        return item_type  # type: ignore[return-value]

    lowered = text.lower().strip()

    # Strong pearl signal: distillation language
    if _PEARL_PATTERN_RE.search(text):
        # But not if action verb appears in imperative position
        if not _SEED_ACTION_RE.match(lowered):
            return "pearl"

    # Strong seed signal: action verb at start (imperative mood)
    first_word_match = re.match(r"^([a-z]+)", lowered)
    if first_word_match:
        first_word = first_word_match.group(1)
        if first_word in _SEED_ACTION_VERBS:
            return "seed"

    # Seed signal: action verb anywhere in short text (title-like)
    word_count = len(text.split())
    if word_count <= 15 and _SEED_ACTION_RE.search(text):
        return "seed"

    # Pearl signal: phenomenological vocabulary and no action verb
    text_words = set(re.findall(r"\b\w+\b", lowered))
    single_philo_terms = frozenset(t for t in _PHILOSOPHICAL_TERMS if " " not in t)
    multi_philo_terms = frozenset(t for t in _PHILOSOPHICAL_TERMS if " " in t)
    philo_hit = bool(text_words & single_philo_terms) or any(
        phrase in lowered for phrase in multi_philo_terms
    )

    if philo_hit and not _SEED_ACTION_RE.search(text):
        return "pearl"

    # Seed signal: action verb present anywhere
    if _SEED_ACTION_RE.search(text):
        return "seed"

    # Phenomenological content without action verb → pearl
    if philo_hit:
        return "pearl"

    # Default: seed (safe — seeds go to GitHub for human review)
    return "seed"


# ---------------------------------------------------------------------------
# Session output parser — pure function
# ---------------------------------------------------------------------------

# Section headings that indicate classifiable content for prose extraction (Path B)
_PEARL_SECTION_HEADINGS = frozenset({
    "insights", "distillations", "recognition", "pearls",
    "pattern observed", "resonance", "what became clear",
})

_SEED_SECTION_HEADINGS = frozenset({
    "seeds", "action items", "next steps", "tasks", "issues",
    "action seeds",
})

_CLASSIFIABLE_SECTION_HEADINGS = _PEARL_SECTION_HEADINGS | _SEED_SECTION_HEADINGS

# Paragraph openings that signal pearl candidates
_PEARL_PARAGRAPH_STARTERS = [
    r"^i\s+now\s+see\b",
    r"^this\s+settles\b",
    r"^what\s+i\s+understand\b",
    r"^recognition\s*:",
]
_PEARL_PARAGRAPH_RE = re.compile(
    "|".join(_PEARL_PARAGRAPH_STARTERS),
    re.IGNORECASE,
)


def _extract_section_items(lines: list[str], section_start: int) -> list[tuple[str, int]]:
    """
    Extract bullet or numbered list items from a section starting at section_start.
    Returns list of (text, line_number) tuples.
    Stops at the next heading.
    """
    items: list[tuple[str, int]] = []
    for i in range(section_start + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        # Stop at next heading
        if re.match(r"^#{1,4}\s+", line):
            break
        # Bullet item
        bullet_match = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet_match:
            items.append((bullet_match.group(1).strip(), i))
            continue
        # Numbered item
        numbered_match = re.match(r"^\d+\.\s+(.+)$", stripped)
        if numbered_match:
            items.append((numbered_match.group(1).strip(), i))
    return items


def parse_session_output(md_path: Path) -> CultivatorInput:
    """
    Parse a philosophy session .md file and produce a CultivatorInput.

    Path A — action_seeds YAML block present:
      Delegates to philosophy_harvester.parse_action_seeds. Maps YAML items
      to typed candidates: issues → seeds, bootup_candidates → pearls,
      memory_observations → memory entries.

    Path B — no YAML block:
      Extracts prose candidates from section headings and list items in
      sections titled Insights, Distillations, Recognition, Pearls, Seeds,
      Action items, Next steps. Also matches paragraphs beginning with
      "I now see", "This settles", "What I understand", "Recognition:".
      Applies classify_item to each extracted candidate.
    """
    text = md_path.read_text(encoding="utf-8")
    yaml_text = extract_yaml_block(text)

    if yaml_text is not None:
        # Path A: YAML block present — delegate to philosophy_harvester
        action_seeds = parse_action_seeds(yaml_text)

        # issues → seeds
        seeds = tuple(
            SeedCandidate(
                title=spec.title,
                body=spec.body,
                labels=spec.labels,
                source_line=None,
            )
            for spec in action_seeds.issues
        )

        # bootup_candidates → pearls
        pearls = tuple(
            PearlCandidate(
                text=f"{bc.context}: {bc.text}",
                pearl_type="bootup",
                source_line=None,
            )
            for bc in action_seeds.bootup_candidates
        )

        memory_entries = action_seeds.memory_observations

        return CultivatorInput(
            pearls=pearls,
            seeds=seeds,
            memory_entries=memory_entries,
            source_path=md_path,
        )

    # Path B: No YAML block — extract from prose
    lines = text.splitlines()
    pearls: list[PearlCandidate] = []
    seeds: list[SeedCandidate] = []

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Check for section headings
        heading_match = re.match(r"^#{1,4}\s+(.+)$", stripped)
        if heading_match:
            heading_text = heading_match.group(1).lower().strip()

            if heading_text in _CLASSIFIABLE_SECTION_HEADINGS:
                items = _extract_section_items(lines, i)
                for item_text, line_no in items:
                    if not item_text:
                        continue

                    # Pre-classify based on section heading
                    if heading_text in _PEARL_SECTION_HEADINGS:
                        pre_type = "pearl"
                    elif heading_text in _SEED_SECTION_HEADINGS:
                        pre_type = "seed"
                    else:
                        pre_type = classify_item(item_text)

                    if pre_type == "pearl":
                        pearls.append(PearlCandidate(
                            text=item_text,
                            pearl_type="insight",
                            source_line=line_no,
                        ))
                    else:
                        pearls_or_seed = classify_item(item_text)
                        if pearls_or_seed == "pearl":
                            pearls.append(PearlCandidate(
                                text=item_text,
                                pearl_type="insight",
                                source_line=line_no,
                            ))
                        else:
                            seeds.append(SeedCandidate(
                                title=item_text[:100],
                                body=item_text,
                                labels=("design-seed",),
                                source_line=line_no,
                            ))
            continue

        # Check for pearl paragraph openers
        if _PEARL_PARAGRAPH_RE.match(stripped):
            pearls.append(PearlCandidate(
                text=stripped,
                pearl_type="insight",
                source_line=i,
            ))

    return CultivatorInput(
        pearls=tuple(pearls),
        seeds=tuple(seeds),
        memory_entries=(),
        source_path=md_path,
    )


# ---------------------------------------------------------------------------
# Write-path router — side effects isolated at boundary
# ---------------------------------------------------------------------------

def route_pearl(
    candidate: PearlCandidate,
    config: CultivatorConfig,
    dry_run: bool,
) -> PearlRouteResult:
    """
    Route a pearl to the write-path.

    Bootup/insight pearls → pending bootup candidate file in config.pending_bootup_dir.
    Frontier pearls → delegate to frontier_router (via dry-run path returns the path).

    Default: write pending file (not GitHub issue) — Dan reviews bootup changes.
    Does NOT modify bootup files directly.
    --file-bootup-issues flag is required to file as GitHub issue instead (not yet wired).
    """
    if candidate.pearl_type == "frontier":
        # Delegate to frontier_router — for now, return a pending file path
        # as frontier routing requires a full session context
        output_path = _write_pearl_pending_file(candidate, config, dry_run)
        return PearlRouteResult(
            candidate=candidate,
            action="frontier",
            output_path=output_path,
        )

    # bootup or insight → write pending file
    output_path = _write_pearl_pending_file(candidate, config, dry_run)
    return PearlRouteResult(
        candidate=candidate,
        action="pending_file",
        output_path=output_path,
    )


def _write_pearl_pending_file(
    candidate: PearlCandidate,
    config: CultivatorConfig,
    dry_run: bool,
) -> Path:
    """Write a pearl as a pending bootup candidate file. Returns the output path."""
    # Reuse philosophy_harvester.write_bootup_candidate to ensure consistent format
    bc = BootupCandidate(
        context=f"pearl:{candidate.pearl_type}",
        text=candidate.text,
        rationale=f"Classified as pearl ({candidate.pearl_type}) by cultivator.",
    )
    # Source path placeholder — cultivator doesn't always have a single source file
    source_md = Path("cultivator-output")
    return write_bootup_candidate(
        candidate=bc,
        source_md=source_md,
        pending_dir=config.pending_bootup_dir,
        dry_run=dry_run,
    )


def route_seed(
    candidate: SeedCandidate,
    config: CultivatorConfig,
    dry_run: bool,
) -> SeedRouteResult:
    """
    Route a seed to GitHub as a new issue.

    Files via gh issue create --repo <config.repo> using the design-seed template structure.
    In dry_run mode, returns the issue spec without filing.
    Labels: minimum "design-seed" plus any labels from the source candidate.
    Issue body includes Source: <session file path> and seed text.
    """
    labels = set(candidate.labels)
    labels.add("design-seed")

    body = _build_seed_issue_body(candidate, config)

    issue_spec = {
        "title": candidate.title,
        "body": body,
        "labels": sorted(labels),
    }

    if dry_run:
        return SeedRouteResult(
            candidate=candidate,
            issue_spec=issue_spec,
            issue_url=None,
            issue_number=None,
        )

    # File the issue via gh CLI
    cmd = ["gh", "issue", "create",
           "--repo", config.repo,
           "--title", candidate.title,
           "--body", body]
    for label in sorted(labels):
        cmd += ["--label", label]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh issue create failed for {candidate.title!r}: {result.stderr.strip()}"
        )

    url = result.stdout.strip()
    import re as _re
    number_match = _re.search(r"/issues/(\d+)$", url)
    number = int(number_match.group(1)) if number_match else None

    return SeedRouteResult(
        candidate=candidate,
        issue_spec=issue_spec,
        issue_url=url,
        issue_number=number,
    )


def _build_seed_issue_body(candidate: SeedCandidate, config: CultivatorConfig) -> str:
    """Build the GitHub issue body for a seed candidate using the design-seed template."""
    return f"""**Source:** {config.pending_bootup_dir.parent / "sessions"} (cultivator)

---

## What the seed surfaces

{candidate.body}

---

## Design question

<!-- Formulate the actionable design question this seed raises. -->

---

## Required agent posture

Required posture: <!-- e.g., lobster-oracle adversarial, meta/drift-detection -->

---

## Resolution condition

Resolution condition: <!-- What would have to be true for this issue to be closed? -->

---

## Dependency metadata

depends-on: <!-- [#X, #Y] -->
benefits-from: <!-- [#X, #Y] -->
enables: <!-- [#X, #Y] -->
"""


# ---------------------------------------------------------------------------
# Orchestration — composes pure parsing with isolated side effects
# ---------------------------------------------------------------------------

def cultivate(
    md_path: Path,
    config: CultivatorConfig,
    route_pearls: bool = True,
    route_seeds: bool = True,
) -> tuple[list[PearlRouteResult], list[SeedRouteResult], list[MemoryObservation], list[str]]:
    """
    Parse a philosophy session file and route all candidates.

    Returns (pearl_results, seed_results, memory_entries, errors).
    Pure parsing is separated from side-effectful routing.
    Errors are collected per-item rather than aborting the whole run.
    """
    cultivator_input = parse_session_output(md_path)

    pearl_results: list[PearlRouteResult] = []
    seed_results: list[SeedRouteResult] = []
    errors: list[str] = []

    if route_pearls:
        for pearl in cultivator_input.pearls:
            try:
                result = route_pearl(pearl, config, config.dry_run)
                pearl_results.append(result)
            except Exception as exc:
                errors.append(f"Pearl routing failed for {pearl.text[:60]!r}: {exc}")

    if route_seeds:
        for seed in cultivator_input.seeds:
            try:
                result = route_seed(seed, config, config.dry_run)
                seed_results.append(result)
            except Exception as exc:
                errors.append(f"Seed routing failed for {seed.title!r}: {exc}")

    return pearl_results, seed_results, list(cultivator_input.memory_entries), errors


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cultivator",
        description=(
            "Philosophy session output cultivator. Classifies session content as "
            "pearls (recognition/distillation) or seeds (actionable items), then "
            "routes each to the appropriate destination."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run -m src.harvest.cultivator output.md
  uv run -m src.harvest.cultivator --dry-run output.md
  uv run -m src.harvest.cultivator --no-pearls output.md
  uv run -m src.harvest.cultivator --no-seeds output.md
        """,
    )
    parser.add_argument(
        "md_file",
        nargs="?",
        help="Path to the philosophy-explore .md output file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and classify without routing (no files written, no issues filed)",
    )
    parser.add_argument(
        "--no-pearls",
        action="store_true",
        help="Skip pearl routing",
    )
    parser.add_argument(
        "--no-seeds",
        action="store_true",
        help="Skip seed routing",
    )
    parser.add_argument(
        "--repo",
        default="SiderealPress/lobster",
        help="GitHub repository to file seed issues against (default: SiderealPress/lobster)",
    )
    parser.add_argument(
        "--pending-dir",
        default=str(
            Path.home() / "lobster-user-config" / "memory" / "pending-bootup-candidates"
        ),
        help="Directory to write pending bootup candidate files",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.md_file is None:
        parser.print_help()
        return 0

    md_path = Path(args.md_file).expanduser().resolve()
    if not md_path.exists():
        print(f"Error: file not found: {md_path}", file=sys.stderr)
        return 1
    if not md_path.is_file():
        print(f"Error: not a file: {md_path}", file=sys.stderr)
        return 1

    pending_dir = Path(args.pending_dir).expanduser().resolve()
    config = CultivatorConfig(
        repo=args.repo,
        pending_bootup_dir=pending_dir,
        dry_run=args.dry_run,
    )

    print(f"Cultivating: {md_path}")
    if args.dry_run:
        print("Mode: dry-run (no files written, no issues filed)")

    pearl_results, seed_results, memory_entries, errors = cultivate(
        md_path=md_path,
        config=config,
        route_pearls=not args.no_pearls,
        route_seeds=not args.no_seeds,
    )

    print(f"\n{len(pearl_results)} pearls routed, "
          f"{len(seed_results)} seeds filed, "
          f"{len(memory_entries)} memory observations stored")

    if pearl_results:
        print("\nPearls:")
        for r in pearl_results:
            path_str = str(r.output_path) if r.output_path else "(dry-run)"
            print(f"  [{r.action}] {r.candidate.text[:60]}... → {path_str}")

    if seed_results:
        print("\nSeeds:")
        for r in seed_results:
            url_str = r.issue_url or "(dry-run)"
            print(f"  {r.candidate.title[:60]} → {url_str}")

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for err in errors:
            print(f"  - {err}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
