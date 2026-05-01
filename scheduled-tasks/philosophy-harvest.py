#!/usr/bin/env python3
"""
philosophy-harvest.py -- harvest action_seeds from philosophy session files.

Scans ~/lobster/philosophy/**/*.md for YAML action_seeds blocks, sends
bootup_candidate notifications to Telegram, and appends design_gap/tension
observations to the reflective surface queue.

WOS-UoW: uow_20260427_6064ab
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup -- allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402

from src.orchestration.paths import SURFACE_QUEUE  # noqa: E402
from src.utils.inbox_write import _task_outputs_dir, write_inbox_message  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "philosophy-harvest"

# Regex to extract fenced YAML blocks from markdown
_YAML_FENCE_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)

# Observation types worth surfacing to the reflective queue
SURFACEABLE_TYPES = frozenset({"design_gap", "tension", "principle"})


# ---------------------------------------------------------------------------
# Pure data helpers
# ---------------------------------------------------------------------------


def find_philosophy_sessions(philosophy_dir: Path) -> list[Path]:
    """Return all .md files under philosophy_dir, sorted by name."""
    return sorted(philosophy_dir.rglob("*.md"))


def extract_action_seeds(content: str) -> dict | None:
    """
    Extract the action_seeds dict from the first YAML fence that contains one.

    Returns None if no action_seeds block is found.
    """
    for m in _YAML_FENCE_RE.finditer(content):
        try:
            data = yaml.safe_load(m.group(1))
            if isinstance(data, dict) and "action_seeds" in data:
                return data["action_seeds"]
        except yaml.YAMLError:
            continue
    return None


def load_state(state_path: Path) -> set[str]:
    """Load the set of already-harvested file paths from state JSON."""
    if not state_path.exists():
        return set()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return set(data.get("harvested", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_state(state_path: Path, harvested: set[str]) -> None:
    """Persist the harvested file set atomically."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"harvested": sorted(harvested)}, indent=2),
        encoding="utf-8",
    )
    tmp.rename(state_path)


def format_bootup_notification(candidate: dict | str, session_name: str) -> str:
    """Format a single bootup candidate as a Telegram-friendly notification.

    Candidates may be dicts (with context/text/rationale keys) or plain strings.
    """
    if isinstance(candidate, str):
        return "\n".join([
            f"Bootup candidate from {session_name}",
            "",
            candidate.strip(),
        ])
    context = candidate.get("context", "unknown context")
    text = candidate.get("text", "").strip()
    rationale = candidate.get("rationale", "").strip()
    return "\n".join([
        f"Bootup candidate from {session_name}",
        f"Target: {context}",
        "",
        text,
        "",
        f"Rationale: {rationale}",
    ])


def make_surface_item(obs: dict, session_name: str, timestamp: str) -> dict:
    """Build a surface-queue item from a memory observation."""
    return {
        "source_id": f"philosophy-harvest-{uuid.uuid4().hex[:8]}",
        "source_file": f"philosophy/{session_name}",
        "observation": obs.get("text", "").strip(),
        "surface_reason": (
            f"type={obs.get('type', 'unknown')} -- flagged by philosophy-harvest"
        ),
        "queued_at": timestamp,
        "delivered": False,
    }


# ---------------------------------------------------------------------------
# Side-effectful I/O (isolated at boundary)
# ---------------------------------------------------------------------------


def append_to_surface_queue(
    queue_path: Path,
    observations: list[dict],
    session_name: str,
    timestamp: str,
) -> None:
    """Append observation items to the reflective surface queue atomically."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if queue_path.exists():
        try:
            existing = json.loads(queue_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    new_items = [make_surface_item(obs, session_name, timestamp) for obs in observations]
    tmp = queue_path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(existing + new_items, indent=2),
        encoding="utf-8",
    )
    tmp.rename(queue_path)


def write_task_output_record(message: str, status: str, timestamp: str) -> None:
    """Write a task-output record for the job runner."""
    task_outputs = _task_outputs_dir()
    date_prefix = timestamp[:19].replace(":", "").replace("-", "").replace("T", "-")
    filename = f"{date_prefix}-{JOB_NAME}.json"
    record = {
        "job_name": JOB_NAME,
        "timestamp": timestamp,
        "status": status,
        "output": message,
    }
    out_path = task_outputs / filename
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def harvest_session(
    session_path: Path,
    repo_root: Path,
    chat_id: int,
    timestamp: str,
    dry_run: bool,
) -> tuple[int, int]:
    """
    Harvest a single session file. Returns (candidates_count, observations_count).

    Returns (0, 0) if the file has no action_seeds block.
    """
    content = session_path.read_text(encoding="utf-8")
    seeds = extract_action_seeds(content)
    if seeds is None:
        return 0, 0

    session_name = session_path.stem
    candidates = 0
    observations = 0

    # Bootup candidates -> inbox notifications
    for candidate in seeds.get("bootup_candidates", []):
        msg = format_bootup_notification(candidate, session_name)
        if dry_run:
            print(f"[DRY-RUN] Bootup notification for {session_name}:")
            print(msg)
            print("---")
        else:
            write_inbox_message(JOB_NAME, chat_id, msg, timestamp)
        candidates += 1

    # Memory observations -> surface queue (filtered by type)
    # Observations may be dicts (with type/text keys) or plain strings; skip strings.
    surfaceable = [
        o
        for o in seeds.get("memory_observations", [])
        if isinstance(o, dict) and o.get("type") in SURFACEABLE_TYPES
    ]
    if dry_run:
        for obs in surfaceable:
            print(f"[DRY-RUN] Surface queue item for {session_name}: {obs.get('type')}")
    elif surfaceable:
        append_to_surface_queue(SURFACE_QUEUE, surfaceable, session_name, timestamp)
    observations = len(surfaceable)

    return candidates, observations


def run(dry_run: bool = False) -> int:
    """
    Execute the philosophy harvest pipeline.

    Scans philosophy session files -> extracts action_seeds -> routes bootup
    candidates to inbox and surfaceable observations to the reflective surface
    queue -> updates state to ensure idempotence.

    Returns exit code: 0 for success, 1 for failure.
    """
    philosophy_dir = Path.home() / "lobster" / "philosophy"
    state_path = (
        Path.home() / "lobster-user-config" / "memory" / "philosophy-harvest-state.json"
    )
    chat_id = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not philosophy_dir.exists():
        print(f"Philosophy dir not found: {philosophy_dir}")
        return 1

    sessions = find_philosophy_sessions(philosophy_dir)
    harvested = load_state(state_path)

    total_candidates = 0
    total_observations = 0

    for session_path in sessions:
        rel = str(session_path.relative_to(Path.home() / "lobster"))
        if rel in harvested:
            continue

        candidates, observations = harvest_session(
            session_path,
            _REPO_ROOT,
            chat_id,
            timestamp,
            dry_run,
        )

        if candidates == 0 and observations == 0:
            # No action_seeds block -- skip without marking harvested
            continue

        total_candidates += candidates
        total_observations += observations

        if not dry_run:
            harvested.add(rel)

    if not dry_run:
        save_state(state_path, harvested)
        summary = (
            f"Harvested: {total_candidates} bootup candidate notification(s), "
            f"{total_observations} surface queue item(s)."
        )
        write_task_output_record(summary, "success", timestamp)

    print(f"Done. Candidates: {total_candidates}, observations queued: {total_observations}")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Harvest philosophy session action seeds"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and parse without writing anything",
    )
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
