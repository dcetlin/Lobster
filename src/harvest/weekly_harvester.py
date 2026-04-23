"""
weekly_harvester.py

Processes weekly epistemic retro output files and dispatches action seeds.

Action seeds extracted from the `action_seeds` YAML block in retro files:
  - issues: list of strings → create GitHub issues
  - bootup_candidates: list of strings → log as memory observations tagged [bootup]
  - memory_observations: list of strings → store in memory via lobster-inbox
  - vision_object_proposals: list of dicts → validate and dispatch binary Telegram confirms
    See vision_inlet.py for the vision_object_proposals schema and eligible field rules.

Usage:
    uv run ~/lobster/src/harvest/weekly_harvester.py <path-to-weekly-retro.md>

Returns exit code 0 on success (even if no seeds found — empty seeds is valid).
"""

import sys
import re
import subprocess
import json
from pathlib import Path

import yaml


def extract_action_seeds(content: str) -> dict:
    """Extract the action_seeds YAML block from a retro markdown file.

    Supports:
      - issues: list of strings
      - bootup_candidates: list of strings
      - memory_observations: list of strings
      - vision_object_proposals: list of dicts (parsed via PyYAML)

    The vision_object_proposals key uses structured YAML dicts rather than plain
    strings. If found, the entire action_seeds block is parsed via yaml.safe_load
    to capture the nested structure faithfully.
    """
    # Try to find a ```yaml ... ``` block containing action_seeds
    pattern = r"```yaml\s*\naction_seeds:(.*?)```"
    match = re.search(pattern, content, re.DOTALL)

    if match:
        # Parse the full YAML block so we get nested types (dicts, lists)
        full_block = "action_seeds:" + match.group(1)
        try:
            parsed = yaml.safe_load(full_block)
            if isinstance(parsed, dict) and "action_seeds" in parsed:
                raw = parsed["action_seeds"] or {}
                seeds = {
                    "issues": raw.get("issues") or [],
                    "bootup_candidates": raw.get("bootup_candidates") or [],
                    "memory_observations": raw.get("memory_observations") or [],
                    "vision_object_proposals": raw.get("vision_object_proposals") or [],
                }
                # Ensure string lists are actually lists of strings
                for key in ("issues", "bootup_candidates", "memory_observations"):
                    seeds[key] = [str(x) for x in seeds[key] if x]
                return seeds
        except yaml.YAMLError:
            pass  # Fall through to regex parsing below

    # Bare YAML block outside a code fence — try regex extraction
    pattern2 = r"action_seeds:\s*\n((?:[ \t]+.*\n?)*)"
    match = re.search(pattern2, content)
    if not match:
        return {}

    block = match.group(0)
    try:
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and "action_seeds" in parsed:
            raw = parsed["action_seeds"] or {}
            seeds = {
                "issues": raw.get("issues") or [],
                "bootup_candidates": raw.get("bootup_candidates") or [],
                "memory_observations": raw.get("memory_observations") or [],
                "vision_object_proposals": raw.get("vision_object_proposals") or [],
            }
            for key in ("issues", "bootup_candidates", "memory_observations"):
                seeds[key] = [str(x) for x in seeds[key] if x]
            return seeds
    except yaml.YAMLError:
        pass

    # Last-resort: regex parsing for simple string list keys only
    seeds = {
        "issues": [],
        "bootup_candidates": [],
        "memory_observations": [],
        "vision_object_proposals": [],
    }

    for key in ("issues", "bootup_candidates", "memory_observations"):
        key_pattern = rf"{key}:\s*\[(.*?)\]"
        key_match = re.search(key_pattern, block, re.DOTALL)
        if key_match:
            items_str = key_match.group(1)
            items = re.findall(r'"([^"]+)"|\'([^\']+)\'|([^\[\],\n]+)', items_str)
            seeds[key] = [
                (a or b or c).strip()
                for a, b, c in items
                if (a or b or c).strip()
            ]
        else:
            key_multiline = rf"{key}:\s*\n((?:[ \t]*-[ \t]+.+\n?)*)"
            ml_match = re.search(key_multiline, block)
            if ml_match:
                lines = ml_match.group(1).strip().split("\n")
                seeds[key] = [
                    re.sub(r"^[ \t]*-[ \t]+", "", line).strip()
                    for line in lines
                    if line.strip()
                ]

    return seeds


def run_lobster_mcp(tool: str, params: dict) -> dict | None:
    """
    Call a lobster-inbox MCP tool via the lobster CLI if available,
    otherwise log what would have been called and return None gracefully.
    """
    try:
        cmd = ["lobster-mcp", tool, json.dumps(params)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return None


def create_github_issue(title: str, body: str) -> bool:
    """Create a GitHub issue in the lobster repo."""
    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", "dcetlin/Lobster",
                "--title", title,
                "--body", body,
                "--label", "action-seed",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"  Created issue: {result.stdout.strip()}")
            return True
        else:
            print(f"  Issue creation failed: {result.stderr.strip()}", file=sys.stderr)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("  gh CLI not available, skipping issue creation", file=sys.stderr)
    return False


def store_memory_observation(text: str, tag: str = "") -> bool:
    """
    Store a memory observation. Attempts lobster-mcp first; falls back to
    writing to a local observations log so nothing is silently lost.
    """
    content = f"[weekly-retro] {tag}{text}" if tag else f"[weekly-retro] {text}"

    result = run_lobster_mcp("memory_store", {"content": content, "source": "weekly-retro"})
    if result:
        return True

    # Fallback: append to a local log
    obs_log = Path.home() / "lobster-workspace" / "data" / "weekly-retro-observations.jsonl"
    try:
        obs_log.parent.mkdir(parents=True, exist_ok=True)
        with obs_log.open("a") as f:
            f.write(json.dumps({"content": content, "source": "weekly-retro"}) + "\n")
        print(f"  Logged observation to {obs_log}")
        return True
    except OSError as e:
        print(f"  Failed to log observation: {e}", file=sys.stderr)
    return False


def process_seeds(seeds: dict, source_file: str) -> dict:
    """Process all action seeds and return a summary."""
    summary = {
        "issues_created": 0,
        "observations_stored": 0,
        "bootup_logged": 0,
        "vision_proposals_dispatched": 0,
        "vision_proposals_rejected": 0,
    }

    issues = seeds.get("issues", [])
    bootup = seeds.get("bootup_candidates", [])
    observations = seeds.get("memory_observations", [])
    vision_proposals = seeds.get("vision_object_proposals", [])

    if not any([issues, bootup, observations, vision_proposals]):
        print("No action seeds found — nothing to dispatch.")
        return summary

    print(
        f"Processing {len(issues)} issues, {len(bootup)} bootup candidates, "
        f"{len(observations)} memory observations, "
        f"{len(vision_proposals)} vision object proposals"
    )

    for item in issues:
        title = item[:80] if len(item) > 80 else item
        body = (
            f"**Action seed from weekly epistemic retro**\n\n"
            f"Source: `{source_file}`\n\n"
            f"{item}\n\n"
            f"*Auto-generated by weekly_harvester.py*"
        )
        if create_github_issue(title, body):
            summary["issues_created"] += 1

    for item in bootup:
        if store_memory_observation(item, tag="[bootup-candidate] "):
            summary["bootup_logged"] += 1

    for item in observations:
        if store_memory_observation(item):
            summary["observations_stored"] += 1

    # Vision object proposals: validate and dispatch binary Telegram confirms
    if vision_proposals:
        _process_vision_proposals(vision_proposals, summary)

    return summary


def _process_vision_proposals(proposals: list, summary: dict) -> None:
    """Validate vision_object_proposals and dispatch binary Telegram confirms."""
    # Import inline to keep this module importable in envs where vision_inlet
    # may not be on sys.path yet (e.g., legacy scheduled tasks).
    try:
        from src.harvest.vision_inlet import (  # type: ignore[import]
            validate_vision_proposals,
            dispatch_vision_proposals,
        )
    except ImportError:
        # Fallback: try relative import for cron-direct invocations
        import importlib
        try:
            vi = importlib.import_module("vision_inlet")
        except ImportError:
            print(
                "  vision_inlet module not importable — skipping vision_object_proposals",
                file=sys.stderr,
            )
            summary["vision_proposals_rejected"] += len(proposals)
            return
        validate_vision_proposals = vi.validate_vision_proposals
        dispatch_vision_proposals = vi.dispatch_vision_proposals

    # Only accept dicts (skip stray strings or malformed entries)
    valid_dicts = [p for p in proposals if isinstance(p, dict)]
    invalid_count = len(proposals) - len(valid_dicts)
    if invalid_count:
        print(f"  Skipped {invalid_count} vision proposals: not dict format", file=sys.stderr)
        summary["vision_proposals_rejected"] += invalid_count

    valid, rejected = validate_vision_proposals(valid_dicts)
    summary["vision_proposals_rejected"] += len(rejected)

    if rejected:
        for r in rejected:
            print(
                f"  Rejected vision proposal for {r.get('field_path')}: "
                f"{r.get('rejection_reason')}",
                file=sys.stderr,
            )

    dispatched = dispatch_vision_proposals(valid)
    summary["vision_proposals_dispatched"] += len(dispatched)


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run weekly_harvester.py <path-to-weekly-retro.md>")
        sys.exit(1)

    retro_path = Path(sys.argv[1]).expanduser().resolve()
    if not retro_path.exists():
        print(f"File not found: {retro_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing weekly retro: {retro_path}")
    content = retro_path.read_text()
    seeds = extract_action_seeds(content)

    if not seeds:
        print("No action_seeds block found — running gracefully with empty seeds.")
        sys.exit(0)

    summary = process_seeds(seeds, str(retro_path))
    print(
        f"Done. Issues created: {summary['issues_created']}, "
        f"Bootup logged: {summary['bootup_logged']}, "
        f"Observations stored: {summary['observations_stored']}, "
        f"Vision proposals dispatched: {summary['vision_proposals_dispatched']}, "
        f"Vision proposals rejected: {summary['vision_proposals_rejected']}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
