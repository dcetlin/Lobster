#!/usr/bin/env python3
"""
Scheduled job: retention sweep for ~/messages/agent-replies/.

The agent channel (source="local-claude", see docs/reference/agent-channel.md) writes
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

**by-agent/ pointer mailbox (agent-channel-protocol-proposal.md §3.2, §2.7).**
`agent-replies/by-agent/<agent-slug>/<request_id>` holds small/zero-byte
pointer files — one per (agent, request_id) pair, named by the bare
`request_id` with no suffix (distinct from the flat `<request_id>.json` /
`<request_id>.ack.json` shape above). This sweep also walks that tree:

- Recurses one level into `by-agent/<slug>/` (the flat scan above never
  recurses at all).
- Ages each pointer by its own mtime, using the *same* retention window as
  the flat sweep — one window governs content and pointers together, per
  the spec's resolved dial 1 (no differential retention).
- Removes a pointer whose target content file
  (`agent-replies/<request_id>.json` or `.ack.json`) no longer exists,
  regardless of the pointer's own age — a dangling pointer is not an error,
  it's an intermediate state the spec explicitly calls out as safe to GC
  silently.
- Cleans up now-empty `by-agent/<slug>/` directories after pointer removal,
  so slug directories don't accumulate indefinitely once their last pointer
  ages out.

The pre-existing flat sweep behavior (scope, filename shape, retention
semantics) is unchanged by this extension — by-agent/ handling is purely
additive.

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

# by-agent/ pointer mailbox — agent-channel-protocol-proposal.md §3.2, §2.7.
# Pointer files are named by the bare request_id (no suffix), one directory
# level below agent-replies/by-agent/, one subdirectory per agent-slug.
# Slugs are normalized lowercase at write time (proposal §6 dial 4); the
# request_id charset allowlist is reused unchanged for the pointer filename.
# Default path constant only — main() derives its working path as
# `AGENT_REPLIES_DIR / "by-agent"` at call time so that patching
# AGENT_REPLIES_DIR alone (the existing test pattern) is sufficient.
BY_AGENT_DIR = AGENT_REPLIES_DIR / "by-agent"


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
# by-agent/ pointer mailbox — pure helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PointerPlan:
    """One decision for one by-agent/<slug>/<request_id> pointer file — pure data, no I/O."""

    slug: str
    filename: str
    action: str  # "remove_stale" | "remove_dangling" | "keep" | "skip_unrecognized"
    age_hours: float | None
    request_id: str | None


def _extract_pointer_request_id(filename: str) -> str | None:
    """
    Return the request_id if filename matches the pointer naming scheme —
    the bare request_id, no suffix (agent-channel-protocol-proposal.md §3.2),
    distinct from the flat sweep's `<request_id>.json` / `.ack.json` shape.
    """
    if not _REQUEST_ID_PATTERN.match(filename):
        return None
    return filename


def plan_pointer_sweep(
    entries: list[tuple[str, str, float]],
    existing_request_ids: set[str],
    retention_hours: float,
    now: datetime,
) -> list[PointerPlan]:
    """
    Pure decision function: given (slug, filename, mtime_epoch) tuples for every
    file found under by-agent/<slug>/, plus the set of request_ids that still
    have a live target in agent-replies/ (flat sweep's decisions already
    applied), return one PointerPlan per entry. No I/O.

    A pointer whose target is gone is removed regardless of age — the spec
    calls dangling-pointer GC a non-error intermediate state, not subject to
    the retention clock. Otherwise pointers age by mtime under the same
    window as the flat reply/ack sweep (no differential retention, per the
    proposal's resolved dial 1).
    """
    plans: list[PointerPlan] = []
    for slug, filename, mtime_epoch in entries:
        request_id = _extract_pointer_request_id(filename)
        if request_id is None:
            plans.append(PointerPlan(slug, filename, "skip_unrecognized", None, None))
            continue
        if request_id not in existing_request_ids:
            plans.append(PointerPlan(slug, filename, "remove_dangling", None, request_id))
            continue
        mtime = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)
        age_hours = (now - mtime).total_seconds() / 3600.0
        action = "remove_stale" if age_hours >= retention_hours else "keep"
        plans.append(PointerPlan(slug, filename, action, age_hours, request_id))
    return plans


def summarize_pointers(plans: list[PointerPlan], dry_run: bool) -> str:
    """Compose a human-readable summary string for the pointer sweep. Pure function."""
    if not plans:
        return ""
    remove_stale = [p for p in plans if p.action == "remove_stale"]
    remove_dangling = [p for p in plans if p.action == "remove_dangling"]
    kept = [p for p in plans if p.action == "keep"]
    unrecognized = [p for p in plans if p.action == "skip_unrecognized"]

    verb = "Would remove" if dry_run else "Removed"
    lines = [
        f"by-agent/ pointers — {verb} {len(remove_stale)} stale, "
        f"{len(remove_dangling)} dangling, kept {len(kept)} "
        f"out of {len(plans)} scanned.",
    ]
    if unrecognized:
        lines.append(
            f"Skipped {len(unrecognized)} unrecognized pointer filename(s) "
            "(did not match the bare request_id shape) — left untouched."
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


def scan_by_agent_dir(by_agent_dir: Path) -> list[tuple[str, str, float]]:
    """
    Recurse one level into by-agent/<slug>/ and list every pointer file found.
    Returns (slug, filename, mtime_epoch) tuples. No I/O beyond stat().

    Unlike scan_directory() (flat, single level), this walks two levels:
    by-agent/ itself, then each slug subdirectory's contents. Non-directory
    entries directly under by-agent/ and non-file entries under a slug dir
    are silently skipped — same "leave anything unexpected alone" posture
    as the flat scan.
    """
    entries: list[tuple[str, str, float]] = []
    if not by_agent_dir.exists():
        return entries
    for slug_dir in sorted(by_agent_dir.iterdir()):
        if not slug_dir.is_dir():
            continue
        for pointer_path in sorted(slug_dir.iterdir()):
            if not pointer_path.is_file():
                continue
            try:
                mtime = pointer_path.stat().st_mtime
            except OSError:
                continue
            entries.append((slug_dir.name, pointer_path.name, mtime))
    return entries


def apply_pointer_sweep(
    by_agent_dir: Path, plans: list[PointerPlan], dry_run: bool
) -> tuple[int, list[str]]:
    """
    Execute "remove_stale" / "remove_dangling" plans against the filesystem.
    Returns (removed_count, errors). Mirrors apply_sweep()'s discipline: only
    ever unlinks a file whose plan says to remove it.
    """
    removed = 0
    errors: list[str] = []
    for plan in plans:
        if plan.action not in ("remove_stale", "remove_dangling"):
            continue
        target = by_agent_dir / plan.slug / plan.filename
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
            errors.append(f"by-agent/{plan.slug}/{plan.filename}: {exc}")
    return removed, errors


def cleanup_empty_agent_dirs(by_agent_dir: Path, dry_run: bool) -> list[str]:
    """
    Remove now-empty by-agent/<slug>/ directories after pointer removal, so
    slug directories don't accumulate indefinitely once their last pointer
    ages out. Returns the list of removed slug names (or, in dry-run, the
    slugs that *would* be removed).
    """
    removed_slugs: list[str] = []
    if not by_agent_dir.exists():
        return removed_slugs
    for slug_dir in sorted(by_agent_dir.iterdir()):
        if not slug_dir.is_dir():
            continue
        try:
            if any(slug_dir.iterdir()):
                continue
        except OSError:
            continue
        if dry_run:
            removed_slugs.append(slug_dir.name)
            continue
        try:
            slug_dir.rmdir()
            removed_slugs.append(slug_dir.name)
        except OSError:
            # Not empty anymore (race with a concurrent writer) — leave it.
            continue
    return removed_slugs


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

    # by-agent/ pointer mailbox sweep — additive, does not change the flat
    # sweep above in any way. Dangling-pointer GC is computed against the
    # flat sweep's *decisions* (request_ids not planned for "remove"), not
    # a filesystem re-scan, so dry-run previews the same outcome a real run
    # would produce regardless of whether apply_sweep actually deleted
    # anything.
    # Derived from AGENT_REPLIES_DIR at call time (not the module-level
    # BY_AGENT_DIR default) so tests that patch AGENT_REPLIES_DIR alone —
    # the existing pattern for this script — get consistent behavior without
    # also having to patch a second constant.
    by_agent_dir = AGENT_REPLIES_DIR / "by-agent"
    surviving_request_ids = {
        p.request_id for p in plans if p.request_id is not None and p.action != "remove"
    }
    pointer_entries = scan_by_agent_dir(by_agent_dir)
    pointer_plans = plan_pointer_sweep(pointer_entries, surviving_request_ids, retention_hours, now)
    pointer_removed_count, pointer_errors = apply_pointer_sweep(by_agent_dir, pointer_plans, args.dry_run)
    removed_empty_slugs = cleanup_empty_agent_dirs(by_agent_dir, args.dry_run)

    pointer_summary = summarize_pointers(pointer_plans, args.dry_run)
    if pointer_summary:
        summary += "\n" + pointer_summary
    if removed_empty_slugs:
        verb = "Would remove" if args.dry_run else "Removed"
        summary += f"\n{verb} {len(removed_empty_slugs)} now-empty by-agent/ slug dir(s)."
    if pointer_errors:
        summary += f"\n{len(pointer_errors)} pointer error(s):\n" + "\n".join(f"  {e}" for e in pointer_errors[:10])

    errors = errors + pointer_errors

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
