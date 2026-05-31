#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Dead-Letter Sweep — weekly summary of undeliverable messages.

Scans ~/messages/dead-letter/, groups items by inferred error category,
and sends a summary to the admin chat. Read-only: does not delete or move items.

Schedule: Monday 09:00 (0 9 * * 1)
Job name: dead-letter-sweep

Run standalone:
    uv run ~/lobster/scheduled-tasks/dead-letter-sweep.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.inbox_write import _task_outputs_dir, write_inbox_message  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "dead-letter-sweep"

# Known invalid placeholder chat_ids that indicate a misconfigured sender
PLACEHOLDER_CHAT_IDS = frozenset({"0", "123456", "1234567890", "-1", "etcpasswd", "dcetlin"})

ADMIN_CHAT_ID: int = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

DEAD_LETTER_DIR = Path(os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))) / "dead-letter"

# Maximum characters from error string to use as fallback group key
ERROR_PREFIX_LENGTH = 80


# ---------------------------------------------------------------------------
# Error category inference
# ---------------------------------------------------------------------------

def infer_error_category(record: dict, filename: str) -> str:
    """
    Infer an error category for a dead-letter record.

    Priority order:
    1. Explicit error_type field
    2. Explicit error field (first ERROR_PREFIX_LENGTH chars)
    3. Heuristics derived from record content
    4. "unknown"
    """
    if record.get("error_type"):
        return str(record["error_type"])

    if record.get("error"):
        return str(record["error"])[:ERROR_PREFIX_LENGTH]

    # Heuristic: invalid/placeholder chat_id
    chat_id = str(record.get("chat_id", ""))
    if chat_id in PLACEHOLDER_CHAT_IDS:
        return f"invalid-chat-id:{chat_id}"

    # Heuristic: workspace_auth messages that couldn't be delivered
    if "workspace_auth" in filename:
        return "undeliverable-workspace-auth"

    # Heuristic: group by message source type
    source = record.get("source", "")
    msg_type = record.get("type", "")
    if source or msg_type:
        tag = f"{source}/{msg_type}" if msg_type else source
        return f"unrouted:{tag}"

    return "unknown"


# ---------------------------------------------------------------------------
# Scanning and grouping
# ---------------------------------------------------------------------------

def _parse_timestamp(record: dict, file_path: Path) -> datetime:
    """Return a timezone-aware datetime from the record or file mtime."""
    for field in ("timestamp", "created_at"):
        raw = record.get(field)
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
    # Fall back to file modification time
    return datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)


def scan_dead_letter(directory: Path) -> tuple[list[dict], list[str]]:
    """
    Scan all files in the dead-letter directory.

    Returns:
        items   — list of dicts with keys: category, timestamp, source, filename
        errors  — list of warning strings for unparseable files
    """
    items: list[dict] = []
    parse_errors: list[str] = []

    if not directory.exists():
        return items, parse_errors

    for file_path in sorted(directory.iterdir()):
        if not file_path.is_file():
            continue
        try:
            record = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            parse_errors.append(f"  Warning: could not parse {file_path.name}: {exc}")
            continue

        items.append({
            "category": infer_error_category(record, file_path.name),
            "timestamp": _parse_timestamp(record, file_path),
            "source": record.get("source", ""),
            "filename": file_path.name,
        })

    return items, parse_errors


def group_by_category(items: list[dict]) -> dict[str, list[dict]]:
    """Group items by their inferred error category."""
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["category"], []).append(item)
    return groups


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def format_summary(items: list[dict], groups: dict[str, list[dict]], parse_errors: list[str]) -> str:
    """Compose the human-readable summary string."""
    if not items:
        return "Dead-letter sweep — 0 items found. Directory is empty or does not exist."

    total = len(items)
    timestamps = [item["timestamp"] for item in items]
    oldest_date = _fmt_date(min(timestamps))
    newest_date = _fmt_date(max(timestamps))

    oldest_item = min(items, key=lambda x: x["timestamp"])

    lines: list[str] = [
        f"Dead-letter sweep — {total} items ({oldest_date} – {newest_date})",
        "",
        "By error type:",
    ]

    # Sort groups by count descending, then alphabetically
    for category, group_items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        group_oldest = _fmt_date(min(i["timestamp"] for i in group_items))
        lines.append(f"• {category}: {len(group_items)} item(s) (oldest: {group_oldest})")

    lines.append("")
    lines.append(
        f"Oldest item: {oldest_item['category']} | source: {oldest_item['source'] or 'n/a'}"
        f" | {_fmt_date(oldest_item['timestamp'])}"
    )
    lines.append("")
    lines.append("Reply to this message if you want to clear or retry a category.")

    if parse_errors:
        lines.append("")
        lines.append(f"Parse warnings ({len(parse_errors)}):")
        lines.extend(parse_errors[:5])
        if len(parse_errors) > 5:
            lines.append(f"  (and {len(parse_errors) - 5} more)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery and logging
# ---------------------------------------------------------------------------

def write_task_output_record(output: str, status: str, timestamp: str) -> None:
    task_outputs = _task_outputs_dir()
    date_prefix = timestamp[:19].replace(":", "").replace("-", "").replace("T", "-")
    filename = f"{date_prefix}-{JOB_NAME}.json"
    record = {
        "job_name": JOB_NAME,
        "timestamp": timestamp,
        "status": status,
        "output": output,
    }
    out_path = task_outputs / filename
    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Dead-letter sweep — weekly summary.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary to stdout; do not send Telegram message.")
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[{timestamp}] Scanning {DEAD_LETTER_DIR} ...")
    items, parse_errors = scan_dead_letter(DEAD_LETTER_DIR)
    print(f"  {len(items)} item(s) parsed ({len(parse_errors)} parse error(s))")

    groups = group_by_category(items)
    summary = format_summary(items, groups, parse_errors)

    print()
    print(summary)

    if args.dry_run:
        print()
        print("[dry-run] Skipping Telegram send and task output write.")
        return 0

    write_inbox_message(JOB_NAME, ADMIN_CHAT_ID, summary, timestamp)
    write_task_output_record(
        f"Sweep complete. {len(items)} items across {len(groups)} categories.",
        "ok",
        timestamp,
    )
    print(f"[{timestamp}] Summary sent and task output written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
