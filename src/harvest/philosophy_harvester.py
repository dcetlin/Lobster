#!/usr/bin/env python3
"""
philosophy_harvester.py — parse Action Seeds from a philosophy-explore .md file
and route each item to its destination: GitHub issues, bootup candidates queue,
and memory.db observations.

Usage:
    uv run src/harvest/philosophy_harvester.py <path_to_output_md>
    uv run src/harvest/philosophy_harvester.py --help
    uv run src/harvest/philosophy_harvester.py --dry-run <path_to_output_md>
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Data model (immutable-style dataclasses)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IssueSpec:
    title: str
    body: str
    labels: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BootupCandidate:
    context: str
    text: str
    rationale: str


@dataclass(frozen=True)
class MemoryObservation:
    text: str
    type: str = "pattern_observation"
    valence: str = "neutral"   # 'golden' | 'smell' | 'neutral'


@dataclass(frozen=True)
class ActionSeeds:
    issues: tuple[IssueSpec, ...]
    bootup_candidates: tuple[BootupCandidate, ...]
    memory_observations: tuple[MemoryObservation, ...]


@dataclass(frozen=True)
class HarvestResult:
    filed_issues: tuple[dict[str, Any], ...]   # {title, url, number}
    queued_bootup: tuple[Path, ...]             # paths to written pending files
    stored_observations: int
    errors: tuple[str, ...]


# ---------------------------------------------------------------------------
# Parsing — pure functions
# ---------------------------------------------------------------------------

def extract_yaml_block(markdown_text: str) -> str | None:
    """
    Extract the action_seeds YAML block from the end of a markdown file.

    The block may appear as a fenced ```yaml ... ``` block, or as a bare
    YAML section starting with `action_seeds:`. Both forms are accepted.
    """
    # Try fenced yaml block containing action_seeds
    fenced_pattern = re.compile(
        r"```yaml\s*\n(action_seeds:.*?)```",
        re.DOTALL | re.IGNORECASE,
    )
    match = fenced_pattern.search(markdown_text)
    if match:
        return match.group(1)

    # Try bare action_seeds: block (indented YAML at end of file)
    bare_pattern = re.compile(
        r"^(action_seeds:.*?)(?=\n##|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    match = bare_pattern.search(markdown_text)
    if match:
        return match.group(1)

    return None


def parse_action_seeds(yaml_text: str) -> ActionSeeds:
    """Parse a YAML string into an ActionSeeds structure. Pure function."""
    data = yaml.safe_load(yaml_text)
    raw = data.get("action_seeds", {}) or {}

    issues = tuple(
        IssueSpec(
            title=item["title"],
            body=item.get("body", ""),
            labels=tuple(item.get("labels", [])),
        )
        for item in (raw.get("issues") or [])
    )

    bootup_candidates = tuple(
        BootupCandidate(
            context=item["context"],
            text=item["text"],
            rationale=item.get("rationale", ""),
        )
        for item in (raw.get("bootup_candidates") or [])
    )

    _valid_valences = {"golden", "smell", "neutral"}
    memory_observations = tuple(
        MemoryObservation(
            text=item["text"],
            type=item.get("type", "pattern_observation"),
            valence=item.get("valence", "neutral") if item.get("valence") in _valid_valences else "neutral",
        )
        for item in (raw.get("memory_observations") or [])
    )

    return ActionSeeds(
        issues=issues,
        bootup_candidates=bootup_candidates,
        memory_observations=memory_observations,
    )


def load_action_seeds(md_path: Path) -> ActionSeeds | None:
    """Read a .md file and extract+parse its action_seeds block. Returns None if absent."""
    text = md_path.read_text(encoding="utf-8")
    yaml_text = extract_yaml_block(text)
    if yaml_text is None:
        return None
    return parse_action_seeds(yaml_text)


# ---------------------------------------------------------------------------
# Side-effectful operations — isolated at the boundary
# ---------------------------------------------------------------------------

def file_github_issue(spec: IssueSpec, repo: str, dry_run: bool) -> dict[str, Any]:
    """File a single GitHub issue via gh CLI. Returns {title, number, url}."""
    if dry_run:
        print(f"  [dry-run] Would file issue: {spec.title!r}")
        return {"title": spec.title, "number": 0, "url": "(dry-run)"}

    cmd = ["gh", "issue", "create",
           "--repo", repo,
           "--title", spec.title,
           "--body", spec.body]
    for label in spec.labels:
        cmd += ["--label", label]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh issue create failed for {spec.title!r}: {result.stderr.strip()}"
        )

    url = result.stdout.strip()
    # Extract issue number from URL: .../issues/42
    number_match = re.search(r"/issues/(\d+)$", url)
    number = int(number_match.group(1)) if number_match else 0
    return {"title": spec.title, "number": number, "url": url}


def write_bootup_candidate(
    candidate: BootupCandidate,
    source_md: Path,
    pending_dir: Path,
    dry_run: bool,
) -> Path:
    """Write a pending bootup candidate file. Returns the path written."""
    pending_dir.mkdir(parents=True, exist_ok=True)

    # Deterministic filename: timestamp + slugified context
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", candidate.context.lower()).strip("-")[:50]
    filename = f"{ts}-{slug}.md"
    out_path = pending_dir / filename

    content = f"""# Bootup Candidate — Pending Review

**Source file**: {source_md.name}
**Target context**: {candidate.context}
**Created**: {datetime.now(timezone.utc).isoformat()}

## Proposed Addition

{candidate.text}

## Rationale

{candidate.rationale}

---

*This file requires Dan's review before any bootup file is modified.*
*To accept: copy the text above into the appropriate bootup file.*
*To reject: delete this file.*
"""

    if dry_run:
        print(f"  [dry-run] Would write bootup candidate: {out_path}")
    else:
        out_path.write_text(content, encoding="utf-8")
        print(f"  Wrote bootup candidate: {out_path}")

    return out_path


def store_memory_observation(obs: MemoryObservation, dry_run: bool) -> bool:
    """
    Store an observation in memory.db via the lobster-inbox MCP server's
    memory_store tool, invoked through the MCP CLI.

    Falls back to writing a JSON record to ~/lobster-workspace/data/pending-observations.jsonl
    if the MCP call is unavailable (e.g. server not running during harvest).
    Returns True on success.
    """
    if dry_run:
        print(f"  [dry-run] Would store observation: {obs.text[:80]}...")
        return True

    # Attempt via mcp CLI if available
    mcp_cmd = ["uv", "run", "-m", "mcp", "call", "lobster-inbox", "memory_store",
               json.dumps({"content": obs.text, "type": "note",
                           "tags": [obs.type], "source": "internal",
                           "valence": obs.valence,
                           "task_id": "philosophy-harvester"})]

    result = subprocess.run(mcp_cmd, capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        print(f"  Stored observation via MCP: {obs.text[:60]}...")
        return True

    # Fallback: write to pending JSONL for later import
    pending_path = Path.home() / "lobster-workspace" / "data" / "pending-observations.jsonl"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "content": obs.text,
        "type": "note",
        "tags": [obs.type],
        "source": "philosophy-harvester",
        "valence": obs.valence,
    }
    with pending_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  Queued observation to {pending_path}: {obs.text[:60]}...")
    return True


def send_telegram_summary(
    chat_id: int,
    source_filename: str,
    filed: list[dict[str, Any]],
    queued_bootup: int,
    stored_obs: int,
    dry_run: bool,
) -> None:
    """Send a Telegram summary via the lobster-inbox send_reply tool."""
    if not filed and queued_bootup == 0 and stored_obs == 0:
        print("  No items to report — skipping Telegram notification.")
        return

    issue_lines = "\n".join(
        f"  • #{item['number']} {item['title']} — {item['url']}"
        for item in filed
    )

    parts = [f"Philosophy harvest: {source_filename}"]
    if filed:
        parts.append(f"\n{len(filed)} issue{'s' if len(filed) != 1 else ''} filed:")
        parts.append(issue_lines)
    if queued_bootup:
        parts.append(
            f"\n{queued_bootup} bootup candidate{'s' if queued_bootup != 1 else ''} queued for review "
            f"(~/lobster-workspace/philosophy-explore/pending-bootup-candidates/)"
        )
    if stored_obs:
        parts.append(
            f"\n{stored_obs} memory observation{'s' if stored_obs != 1 else ''} stored."
        )

    message = "\n".join(parts)

    if dry_run:
        print(f"  [dry-run] Would send Telegram to {chat_id}:\n{message}")
        return

    # Call send_reply via the MCP server
    mcp_cmd = ["uv", "run", "-m", "mcp", "call", "lobster-inbox", "send_reply",
               json.dumps({"chat_id": chat_id, "text": message})]
    result = subprocess.run(mcp_cmd, capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        print("  Telegram summary sent.")
    else:
        # Non-fatal: print instead of raising
        print(f"  Warning: Telegram send failed: {result.stderr.strip()}")
        print(f"  Summary would have been:\n{message}")


# ---------------------------------------------------------------------------
# Orchestration — composes pure parsing with isolated side effects
# ---------------------------------------------------------------------------

def harvest(
    md_path: Path,
    repo: str,
    pending_dir: Path,
    chat_id: int,
    dry_run: bool,
) -> HarvestResult:
    """
    Top-level harvest function. Reads the .md file, parses action_seeds,
    and dispatches each category to its destination.

    Structured as a pipeline of pure transformation followed by bounded
    side-effectful execution. Errors per-item are collected rather than
    aborting the whole run.
    """
    seeds = load_action_seeds(md_path)
    if seeds is None:
        print(f"No action_seeds block found in {md_path}. Nothing to harvest.")
        return HarvestResult(
            filed_issues=(),
            queued_bootup=(),
            stored_observations=0,
            errors=(),
        )

    total = (
        len(seeds.issues)
        + len(seeds.bootup_candidates)
        + len(seeds.memory_observations)
    )
    print(f"Found action_seeds: {len(seeds.issues)} issues, "
          f"{len(seeds.bootup_candidates)} bootup candidates, "
          f"{len(seeds.memory_observations)} memory observations "
          f"({total} total)")

    # --- File GitHub issues ---
    filed: list[dict[str, Any]] = []
    errors: list[str] = []

    for spec in seeds.issues:
        try:
            result = file_github_issue(spec, repo, dry_run)
            filed.append(result)
            print(f"  Filed issue #{result['number']}: {result['title']}")
        except Exception as exc:
            msg = f"Failed to file issue {spec.title!r}: {exc}"
            print(f"  ERROR: {msg}")
            errors.append(msg)

    # --- Queue bootup candidates ---
    queued: list[Path] = []

    for candidate in seeds.bootup_candidates:
        try:
            path = write_bootup_candidate(candidate, md_path, pending_dir, dry_run)
            queued.append(path)
        except Exception as exc:
            msg = f"Failed to write bootup candidate for {candidate.context!r}: {exc}"
            print(f"  ERROR: {msg}")
            errors.append(msg)

    # --- Store memory observations ---
    stored = 0
    for obs in seeds.memory_observations:
        try:
            if store_memory_observation(obs, dry_run):
                stored += 1
        except Exception as exc:
            msg = f"Failed to store observation: {exc}"
            print(f"  ERROR: {msg}")
            errors.append(msg)

    # --- Send Telegram summary ---
    try:
        send_telegram_summary(
            chat_id=chat_id,
            source_filename=md_path.name,
            filed=filed,
            queued_bootup=len(queued),
            stored_obs=stored,
            dry_run=dry_run,
        )
    except Exception as exc:
        errors.append(f"Telegram summary failed: {exc}")

    return HarvestResult(
        filed_issues=tuple(filed),
        queued_bootup=tuple(queued),
        stored_observations=stored,
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="philosophy_harvester",
        description=(
            "Parse the action_seeds block from a philosophy-explore .md output file "
            "and route each item to its destination: GitHub issues, bootup candidates "
            "queue, and memory.db observations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run src/harvest/philosophy_harvester.py output.md
  uv run src/harvest/philosophy_harvester.py --dry-run output.md
  uv run src/harvest/philosophy_harvester.py --repo dcetlin/Lobster output.md
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
        help="Parse and report what would happen without filing issues, writing files, or sending messages",
    )
    parser.add_argument(
        "--repo",
        default="dcetlin/Lobster",
        help="GitHub repository to file issues against (default: dcetlin/Lobster)",
    )
    parser.add_argument(
        "--pending-dir",
        default=str(
            Path.home() / "lobster-workspace" / "philosophy-explore" / "pending-bootup-candidates"
        ),
        help="Directory to write pending bootup candidate files",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        default=8075091586,
        help="Telegram chat ID for summary notification (default: 8075091586)",
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

    print(f"Harvesting: {md_path}")
    if args.dry_run:
        print("Mode: dry-run (no issues filed, no files written, no messages sent)")

    result = harvest(
        md_path=md_path,
        repo=args.repo,
        pending_dir=pending_dir,
        chat_id=args.chat_id,
        dry_run=args.dry_run,
    )

    print(f"\nHarvest complete:")
    print(f"  Issues filed:          {len(result.filed_issues)}")
    print(f"  Bootup candidates:     {len(result.queued_bootup)}")
    print(f"  Memory observations:   {result.stored_observations}")
    if result.errors:
        print(f"  Errors ({len(result.errors)}):")
        for err in result.errors:
            print(f"    - {err}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
