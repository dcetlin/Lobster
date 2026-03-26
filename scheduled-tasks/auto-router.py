#!/usr/bin/env python3
"""
Reflective Surface Queue Auto-Router — Lobster Scheduled Job
=============================================================

Reads meta/reflective-surface-queue.json, evaluates each unrouted item against
a gate judgment, and writes subagent_result inbox messages that either:

  - implementation   → dispatches a functional-engineer to implement the change
  - design-surface   → surfaces the item to Dan asking for go/nogo or design direction

Gate criterion (from user.base.bootup.md Design Gate):
    Can you state, in one concrete sentence, what the output should be?
    Yes → implementation-ready.
    No  → design-open (unresolved premises, core tensions, directional choice needed).

Marks each processed item with routed_at and route_decision.

Run standalone:
    uv run ~/lobster/scheduled-tasks/auto-router.py
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUEUE_PATH = Path.home() / "lobster-workspace" / "hygiene" / "meta" / "reflective-surface-queue.json"

# Fall back to the old path if the new one doesn't exist
_OLD_QUEUE_PATH = Path.home() / "lobster-workspace" / "meta" / "reflective-surface-queue.json"

ADMIN_CHAT_ID = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))


# ---------------------------------------------------------------------------
# Pure data helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_routed(item: dict) -> bool:
    """Return True if this item has already been routed by the auto-router."""
    return bool(item.get("routed_at"))


def is_delivered(item: dict) -> bool:
    """Return True if the delivery job has already delivered this item."""
    return bool(item.get("delivered", False))


def is_archived(item: dict) -> bool:
    return bool(item.get("archived", False))


def select_unrouted(items: list[dict]) -> list[dict]:
    """Return items that are not yet routed, delivered, or archived."""
    return [
        i for i in items
        if not is_routed(i) and not is_delivered(i) and not is_archived(i)
    ]


# ---------------------------------------------------------------------------
# Gate judgment — pure, deterministic
# ---------------------------------------------------------------------------

# Surface-reason keywords that signal unresolved premises or directional tension.
# These are content signals — if any appear, the item is design-open unless
# overridden by an implementation_signal (below).
DESIGN_OPEN_SIGNALS: list[str] = [
    "founding premise",
    "load-bearing",
    "unresolved",
    "core tension",
    "design question",
    "directional",
    "misaligned",  # Misaligned items surface tension that needs Dan's judgment
    "questioned",  # Questioned verdict = open inquiry, not a concrete fix
    "whether",
    "may be",
    "not yet",
    "systematic pattern",
    "accumulation",
    "the question",
]

# Hygiene-review items are typically structural observations, not implementation specs.
# We treat them as design-open by source unless their observation names a very concrete fix.
DESIGN_OPEN_SOURCES: set[str] = {
    "meta/premise-review.md",
}


def _extract_text(item: dict) -> str:
    """Concatenate observation and surface_reason for signal scanning. Lowercased."""
    return (
        item.get("observation", "") + " " + item.get("surface_reason", "")
    ).lower()


def _has_concrete_output(item: dict) -> bool:
    """
    Heuristic: does the observation describe a concrete, buildable change with
    a clear output? Implementation-ready items describe *what to build*; design-open
    items describe *what to examine or decide*.

    Positive signals that suggest a concrete output exists:
    - Observation names a specific file, function, or system change
    - Surface reason describes an orphan artifact needing a clear fix
      (e.g., "never referenced", "no delivery mechanism")
    - Source is hygiene-review (structural gap → concrete action)

    This function returns True only for hygiene-review items whose surface_reason
    describes a missing delivery mechanism or orphan artifact — the clearest
    implementation-ready pattern in the queue.
    """
    source = item.get("source_file", "")
    text = _extract_text(item)

    # Hygiene-review items often describe concrete structural gaps
    if source == "meta/hygiene-review.md":
        concrete_gap_signals = [
            "no delivery mechanism",
            "never operated",
            "accumulating without",
            "orphan criterion",
            "never referenced",
            "no downstream",
        ]
        if any(sig in text for sig in concrete_gap_signals):
            return True

    return False


def gate_judgment(item: dict) -> str:
    """
    Return 'implementation' or 'design-surface'.

    The gate criterion (from user.base.bootup.md Design Gate):
        If you cannot state, in one concrete sentence, what the output should be,
        the design is not settled → design-surface.

    Heuristic decision tree:
    1. If the item has a concrete output signal → implementation.
    2. If the source or surface_reason contains design-open signals → design-surface.
    3. Default: design-surface (conservative — ambiguous items go to Dan).
    """
    if _has_concrete_output(item):
        return "implementation"

    text = _extract_text(item)
    source = item.get("source_file", "")

    # Sources that are almost always design-open
    if source in DESIGN_OPEN_SOURCES:
        return "design-surface"

    # Check for design-open signals in text
    if any(sig in text for sig in DESIGN_OPEN_SIGNALS):
        return "design-surface"

    # Default: conservative
    return "design-surface"


# ---------------------------------------------------------------------------
# Message formatting — pure
# ---------------------------------------------------------------------------

def _source_label(item: dict) -> str:
    source_map = {
        "meta/premise-review.md": "Premise Review",
        "meta/hygiene-review.md": "Hygiene Review",
        "meta/oracle/learnings.md": "Oracle Learnings",
    }
    return source_map.get(item.get("source_file", ""), item.get("source_file", "unknown"))


def format_implementation_message(item: dict) -> str:
    """
    Format a subagent_result message text that dispatches a functional-engineer
    to implement the change described by this item.
    """
    title = item.get("source_id", "unknown")
    source = _source_label(item)
    observation = item.get("observation", "").strip()
    surface_reason = item.get("surface_reason", "").strip()

    # Derive a task description from the observation
    # Hygiene-review items name a concrete structural gap → describe the fix
    task_description = (
        f"Address the following structural gap identified in {source}:\n\n"
        f"{observation}\n\n"
        f"Why this surfaced: {surface_reason}"
    )

    return (
        f"[auto-router: implementation-ready]\n\n"
        f"Item: {title}\n"
        f"Source: {source}\n"
        f"Verdict: implementation-ready\n\n"
        f"Task for functional-engineer:\n{task_description}"
    )


def format_design_surface_message(item: dict) -> str:
    """
    Format a subagent_result message text that surfaces the item to Dan,
    including key premises/tensions and a clear question.
    """
    title = item.get("source_id", item.get("queued_at", "unknown"))
    source = _source_label(item)
    observation = item.get("observation", "").strip()
    surface_reason = item.get("surface_reason", "").strip()

    # Extract a short lead from observation (first sentence or 200 chars)
    lead = observation.split(". ")[0]
    if len(lead) > 250:
        lead = lead[:247] + "..."

    return (
        f"[auto-router: design-open — needs your direction]\n\n"
        f"Item: {title}\n"
        f"Source: {source}\n\n"
        f"Observation (lead): {lead}\n\n"
        f"Why surfaced: {surface_reason}\n\n"
        f"Question for Dan: Is this worth a design investigation, a GitHub issue, "
        f"or should it be archived? If the former — go/nogo and any framing you want "
        f"the engineer to work from."
    )


# ---------------------------------------------------------------------------
# Queue I/O
# ---------------------------------------------------------------------------

def _resolve_queue_path() -> Path:
    """Return the queue path that exists, preferring the new hygiene/meta location."""
    if QUEUE_PATH.exists():
        return QUEUE_PATH
    if _OLD_QUEUE_PATH.exists():
        return _OLD_QUEUE_PATH
    # Return the canonical new path even if it doesn't exist yet — callers handle missing
    return QUEUE_PATH


def load_queue(path: Path) -> list[dict]:
    """Load and parse queue JSON. Returns empty list if missing or malformed."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_queue(path: Path, items: list[dict]) -> None:
    """Write queue back to disk atomically via tmp-then-replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(str(path) + ".tmp")
    tmp_path.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def mark_routed(item: dict, decision: str, timestamp: str) -> dict:
    """Return a new item dict with routed_at and route_decision set. Pure."""
    return {**item, "routed_at": timestamp, "route_decision": decision}


def apply_routing(
    items: list[dict],
    routed: list[tuple[dict, str]],  # (original_item, decision)
    timestamp: str,
) -> list[dict]:
    """
    Return a new items list with routed items updated.
    Identifies items by source_id (falling back to queued_at+observation hash).
    Pure: does not mutate input.
    """
    routed_map: dict[str, str] = {}
    for item, decision in routed:
        key = item.get("source_id") or f"{item.get('queued_at','')}:{item.get('observation','')[:40]}"
        routed_map[key] = decision

    def update(item: dict) -> dict:
        key = item.get("source_id") or f"{item.get('queued_at','')}:{item.get('observation','')[:40]}"
        if key in routed_map:
            return mark_routed(item, routed_map[key], timestamp)
        return item

    return [update(i) for i in items]


# ---------------------------------------------------------------------------
# Inbox / task-output I/O helpers
# ---------------------------------------------------------------------------

def _inbox_dir() -> Path:
    """Return the inbox directory path, creating it if needed."""
    messages_base = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    inbox = Path(messages_base) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def _task_outputs_dir() -> Path:
    """Return the task-outputs directory path, creating it if needed."""
    messages_base = os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))
    task_outputs = Path(messages_base) / "task-outputs"
    task_outputs.mkdir(parents=True, exist_ok=True)
    return task_outputs


def write_inbox_message(chat_id: int, text: str, timestamp: str) -> str:
    """
    Write a single subagent_result message to the inbox using atomic tmp-then-replace.
    Returns the message ID. Side effects isolated here.
    """
    inbox = _inbox_dir()
    msg_id = f"auto_router_{uuid.uuid4().hex}"
    msg = {
        "id": msg_id,
        "type": "subagent_result",
        "task_id": msg_id,
        "chat_id": chat_id,
        "source": "telegram",
        "text": text,
        "status": "success",
        "sent_reply_to_user": False,
        "timestamp": timestamp,
    }
    out_path = inbox / f"{msg_id}.json"
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    return msg_id


def write_task_output(output: str, status: str, timestamp: str) -> None:
    """Write a task output record to the task-outputs directory."""
    task_outputs = _task_outputs_dir()
    date_prefix = timestamp[:19].replace(":", "").replace("-", "").replace("T", "-")
    filename = f"{date_prefix}-auto-router.json"
    record = {
        "job_name": "auto-router",
        "timestamp": timestamp,
        "status": status,
        "output": output,
    }
    out_path = task_outputs / filename
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run() -> int:
    """
    Execute the auto-routing pipeline.

    Reads queue → selects unrouted items → applies gate judgment →
    writes inbox messages → marks items routed → saves queue.

    Returns exit code: 0 for success, 1 for failure.
    """
    reference = now_utc()
    timestamp_iso = reference.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[{timestamp_iso}] Starting auto-router")

    queue_path = _resolve_queue_path()
    print(f"  Queue path: {queue_path}")

    items = load_queue(queue_path)
    print(f"  Total items in queue: {len(items)}")

    unrouted = select_unrouted(items)
    print(f"  Unrouted items: {len(unrouted)}")

    if not unrouted:
        print("  Nothing to route.")
        write_task_output("No unrouted items in the reflective surface queue.", "success", timestamp_iso)
        return 0

    # Apply gate judgment to each unrouted item
    routed_pairs: list[tuple[dict, str]] = []
    impl_count = 0
    design_count = 0

    for item in unrouted:
        decision = gate_judgment(item)
        routed_pairs.append((item, decision))

        source_id = item.get("source_id", item.get("queued_at", "?"))
        print(f"  [{decision}] {source_id[:60]}")

        if decision == "implementation":
            msg_text = format_implementation_message(item)
            impl_count += 1
        else:
            msg_text = format_design_surface_message(item)
            design_count += 1

        write_inbox_message(ADMIN_CHAT_ID, msg_text, timestamp_iso)

    # Update queue
    updated_items = apply_routing(items, routed_pairs, timestamp_iso)
    save_queue(queue_path, updated_items)

    summary = (
        f"Routed {len(routed_pairs)} item(s): "
        f"{impl_count} implementation-ready, {design_count} design-open."
    )
    print(f"  {summary}")
    print(f"  Queue saved.")

    write_task_output(summary, "success", timestamp_iso)

    print(f"[{timestamp_iso}] auto-router complete")
    return 0


if __name__ == "__main__":
    sys.exit(run())
