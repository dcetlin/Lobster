#!/usr/bin/env python3
"""
Scheduled job: retention sweep for ~/messages/agent-replies/.

The agent channel (source="local-claude", see docs/agent-channel.md) writes
one reply file per request (`<request_id>.json`) and, for delegated
requests, one ack file (`<request_id>.ack.json`). Nothing currently deletes
these — the directory grows without bound as long as the channel is used.

This is a Type B (cron-direct) job per CLAUDE.md's scheduling architecture:
deterministic, no LLM round-trip, safe to run frequently. It is deliberately
conservative:

- Only ever removes files whose name matches a sanitized `<request_id>.json`
  or `<request_id>.ack.json` pattern (the same charset allowlist the MCP
  server enforces when it writes them — agent-channel protocol spec,
  principle 6). Anything else in the directory is left untouched and logged.
- Only removes a file once it is older than the retention window. A reply or
  ack file existing at all means the request already has an answer/ack
  written for it (agent-replies/ has no representation of "still being
  worked" — that state lives in inbox/processing/, which this job never
  touches) — but the *client* may not have polled it yet, so age is the
  safety margin, not existence. Default retention is generous (7 days)
  relative to the client's default 90s poll timeout (see
  scripts/lobster-chat.py), so this never races a live poller.
- Never touches inbox/, processing/, or any other message directory —
  scope is agent-replies/ only.

Schedule: daily (see scripts/upgrade.sh migration for the cron entry).
Job name: agent-replies-sweep

Run standalone:
    uv run ~/lobster/scheduled-tasks/agent-replies-sweep.py [--dry-run] [--retention-hours N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Path setup — allow running as a script from any working directory
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.jobs import is_job_enabled  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "agent-replies-sweep"

# Mirrors the request_id charset allowlist enforced server-side in
# src/mcp/reliability.py (sanitize_request_id) — deliberately duplicated
# rather than imported so this cron-direct script never pulls in the MCP
# server's import graph (no scheduled-tasks/*.py script does today).
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REPLY_SUFFIX = ".json"
_ACK_SUFFIX = ".ack.json"

DEFAULT_RETENTION_HOURS = 24 * 7  # 7 days
RETENTION_HOURS_ENV = "LOBSTER_AGENT_REPLIES_RETENTION_HOURS"

MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages")))
AGENT_REPLIES_DIR = MESSAGES_DIR / "agent-replies"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SweepPlan:
    """One decision for one file — pure data, no I/O."""

    path: Path
    action: str  # "remove" | "keep_recent" | "skip_unrecognized" | "skip_unparseable"
    age_hours: float | None
    request_id: str | None


def _extract_request_id(filename: str) -> str | None:
    """Return the request_id if filename matches the reply/ack naming scheme, else None."""
    if filename.endswith(_ACK_SUFFIX):
        candidate = filename[: -len(_ACK_SUFFIX)]
    elif filename.endswith(_REPLY_SUFFIX):
        candidate = filename[: -len(_REPLY_SUFFIX)]
    else:
        return None
    if not _REQUEST_ID_PATTERN.match(candidate):
        return None
    return candidate


def _file_age_hours(payload: dict | None, mtime_epoch: float, now: datetime) -> float:
    """
    Age in hours, preferring the payload's own `ts` field (when present and
    parseable) over filesystem mtime — the `ts` field is the authoritative
    "when was this reply/ack actually written" timestamp and survives a
    cp/rsync that would otherwise reset mtime.
    """
    if payload:
        raw_ts = payload.get("ts")
        if raw_ts:
            try:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return (now - ts).total_seconds() / 3600.0
            except (ValueError, TypeError):
                pass
    mtime = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)
    return (now - mtime).total_seconds() / 3600.0


def plan_sweep(
    entries: list[tuple[str, float, dict | None]],
    retention_hours: float,
    now: datetime,
) -> list[SweepPlan]:
    """
    Pure decision function: given (filename, mtime_epoch, parsed_payload_or_None)
    tuples, return one SweepPlan per entry. No I/O.
    """
    plans: list[SweepPlan] = []
    for filename, mtime_epoch, payload in entries:
        request_id = _extract_request_id(filename)
        if request_id is None:
            plans.append(SweepPlan(Path(filename), "skip_unrecognized", None, None))
            continue
        age_hours = _file_age_hours(payload, mtime_epoch, now)
        action = "remove" if age_hours >= retention_hours else "keep_recent"
        plans.append(SweepPlan(Path(filename), action, age_hours, request_id))
    return plans


def summarize(plans: list[SweepPlan], retention_hours: float, dry_run: bool) -> str:
    """Compose a human-readable summary string. Pure function."""
    removed = [p for p in plans if p.action == "remove"]
    kept_recent = [p for p in plans if p.action == "keep_recent"]
    unrecognized = [p for p in plans if p.action == "skip_unrecognized"]
    unparseable = [p for p in plans if p.action == "skip_unparseable"]

    verb = "Would remove" if dry_run else "Removed"
    lines = [
        f"agent-replies-sweep — retention {retention_hours:.0f}h",
        f"{verb} {len(removed)} file(s), kept {len(kept_recent)} recent file(s) "
        f"out of {len(plans)} scanned.",
    ]
    if unrecognized:
        lines.append(
            f"Skipped {len(unrecognized)} unrecognized filename(s) "
            "(did not match <request_id>.json / <request_id>.ack.json) — left untouched."
        )
    if unparseable:
        lines.append(
            f"{len(unparseable)} file(s) had unparseable JSON — aged by mtime, "
            "still eligible for removal on the next run once past retention."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Side-effecting boundary functions
# ---------------------------------------------------------------------------

def scan_directory(directory: Path) -> list[tuple[str, float, dict | None]]:
    """List (filename, mtime_epoch, parsed_payload_or_None) for every file in directory."""
    entries: list[tuple[str, float, dict | None]] = []
    if not directory.exists():
        return entries
    for file_path in sorted(directory.iterdir()):
        if not file_path.is_file():
            continue
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            continue
        payload: dict | None = None
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            payload = None
        entries.append((file_path.name, mtime, payload))
    return entries


def apply_sweep(directory: Path, plans: list[SweepPlan], dry_run: bool) -> tuple[int, list[str]]:
    """
    Execute "remove" plans against the filesystem. Returns (removed_count, errors).

    Only ever unlinks a file whose action is exactly "remove" — every other
    action is a no-op here by construction, so a bug in plan_sweep that
    over-classifies is the only way this function could delete something it
    shouldn't; it can never delete based on a decision made outside plan_sweep.
    """
    removed = 0
    errors: list[str] = []
    for plan in plans:
        if plan.action != "remove":
            continue
        target = directory / plan.path.name
        if dry_run:
            removed += 1
            continue
        try:
            target.unlink()
            removed += 1
        except FileNotFoundError:
            # Already gone (e.g. removed by a concurrent sweep run) — not an error.
            removed += 1
        except OSError as exc:
            errors.append(f"{plan.path.name}: {exc}")
    return removed, errors


def write_task_output(task_outputs_dir: Path, job_name: str, summary: str, status: str) -> None:
    """
    Write job output to the task-outputs directory in the same format as the
    write_task_output MCP tool, so the dispatcher can pick it up via
    check_task_outputs. Mirrors export-logs.py's helper of the same name.
    """
    task_outputs_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%d-%H%M%S")
    output_data = {
        "job_name": job_name,
        "timestamp": now.isoformat(),
        "status": status,
        "output": summary,
    }
    output_file = task_outputs_dir / f"{timestamp_str}-{job_name}.json"
    with open(output_file, "w") as fh:
        json.dump(output_data, fh, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _resolve_retention_hours(cli_value: float | None) -> float:
    if cli_value is not None:
        return cli_value
    raw = os.environ.get(RETENTION_HOURS_ENV)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(DEFAULT_RETENTION_HOURS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-replies-sweep — retention cleanup for ~/messages/agent-replies/.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be removed; do not delete or write task output.")
    parser.add_argument("--retention-hours", type=float, default=None, help=f"Override retention window (default {DEFAULT_RETENTION_HOURS}h / env {RETENTION_HOURS_ENV}).")
    args = parser.parse_args(argv)

    if not is_job_enabled(JOB_NAME):
        print(f"[{JOB_NAME}] disabled in jobs.json — skipping.")
        return 0

    retention_hours = _resolve_retention_hours(args.retention_hours)
    now = datetime.now(timezone.utc)

    entries = scan_directory(AGENT_REPLIES_DIR)
    plans = plan_sweep(entries, retention_hours, now)
    removed_count, errors = apply_sweep(AGENT_REPLIES_DIR, plans, args.dry_run)

    summary = summarize(plans, retention_hours, args.dry_run)
    if errors:
        summary += f"\n{len(errors)} error(s):\n" + "\n".join(f"  {e}" for e in errors[:10])

    print(summary)

    if args.dry_run:
        print("[dry-run] Skipping task output write.")
        return 0

    task_outputs_dir = MESSAGES_DIR / "task-outputs"
    write_task_output(
        task_outputs_dir,
        job_name=JOB_NAME,
        summary=summary,
        status="failed" if errors else "success",
    )

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
