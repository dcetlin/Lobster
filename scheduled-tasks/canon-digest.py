#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
canon-digest — weekly read-only Telegram summary of the artifact registry.

Type B (cron-direct) replacement for the retired `canon-reconciler` Type A
subagent job (dcetlin/Lobster canon-reconciliation, 2026-07-04). The
reconciler's actual registry mutation (Phase 0 scan: staleness flags, expiry
transitions, unregistered-artifact discovery, jobs.json diffing) now lives in
`lobster-meta` Step 2.75/2.76 (nightly) and `lobster-hygiene` Step 2c
(quarterly) — see .claude/agents/lobster-meta.md and .claude/agents/lobster-hygiene.md.
Running a second process on a different cadence against the same file would
reintroduce the dual-reconciler read-modify-write collision this retirement
was meant to eliminate.

This script only reads `data/artifact-registry.json` (already reconciled by
lobster-meta nightly) and `scheduled-jobs/jobs.json`, diffs them, and formats
the same weekly digest template the old canon-reconciler.md Step 6 used. It
performs no writes to the registry — a deterministic read-and-format job,
matching the Type B definition ("the same result would be produced on every
invocation regardless of model choice").

Cron schedule: weekly, Sundays at 03:00 UTC (same cadence the old
canon-reconciler job ran on):
    0 3 * * 0 cd ~/lobster && uv run scheduled-tasks/canon-digest.py >> ~/lobster-workspace/scheduled-jobs/logs/canon-digest.log 2>&1

Type B dispatch: cron calls this script directly (no inbox message, no LLM
round-trip). The jobs.json `enabled` gate is checked at the top of main() so
`wos start/stop`-style runtime toggling (direct jobs.json edits) is respected
without touching cron.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.jobs import is_job_enabled  # noqa: E402
from src.utils.artifact_registry import ArtifactState, UNOWNED  # noqa: E402

JOB_NAME = "canon-digest"
TELEGRAM_CHAT_ID = "8075091586"
WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
REGISTRY_PATH = WORKSPACE / "data" / "artifact-registry.json"
JOBS_PATH = WORKSPACE / "scheduled-jobs" / "jobs.json"
CANON_DOC_PATH = WORKSPACE / "CANON.md"


# ---------------------------------------------------------------------------
# Pure functions — no I/O, no side effects. Covered by
# tests/unit/test_scheduled_tasks/test_canon_digest.py.
# ---------------------------------------------------------------------------

def summarize_registry(artifacts: list[dict]) -> dict:
    """Reduce the artifact list to the counts/lists the digest reports.

    Does not mutate `state`, `notes`, or any other field — that is
    lobster-meta/lobster-hygiene's job. This is read-only summarization.
    """
    counts_by_state = dict(Counter(a["state"] for a in artifacts))
    orphans = sorted(a["id"] for a in artifacts if a["state"] == ArtifactState.ORPHAN)
    stale_flagged = sorted(
        a["id"] for a in artifacts if "STALE-" in (a.get("notes") or "")
    )
    needs_owner = sorted(
        a["id"]
        for a in artifacts
        if a.get("owner") == UNOWNED and a["state"] != ArtifactState.ORPHAN
    )
    return {
        "counts_by_state": counts_by_state,
        "orphans": orphans,
        "stale_flagged": stale_flagged,
        "needs_owner": needs_owner,
    }


def compute_job_diff(registry_job_ids: set[str], live_job_keys: set[str]) -> dict:
    """Diff registered `scheduled_job` entries against live jobs.json keys.

    `registry_job_ids` is the set of `id` values for artifacts with
    `artifact_class: scheduled_job`. The live registry uses a three-segment
    convention that embeds state, e.g. `jobs/cadence/lobster-meta` or
    `jobs/orphan/negentropic-sweep`; a flat `jobs/<key>` id is also accepted.
    The jobs.json key is always the *last* path segment. Non-job ids (e.g.
    `workstreams/foo`) are ignored.
    """
    registered_keys = {
        rid.rsplit("/", 1)[-1] for rid in registry_job_ids if rid.startswith("jobs/")
    }
    unregistered = sorted(live_job_keys - registered_keys)
    stale_registry_entries = sorted(registered_keys - live_job_keys)
    return {
        "unregistered": unregistered,
        "stale_registry_entries": stale_registry_entries,
    }


def _format_list(items: list[str]) -> str:
    if not items:
        return "  *(none)*"
    return "\n".join(f"  - {item}" for item in items)


def format_digest(date_str: str, summary: dict, job_diff: dict, total: int) -> str:
    """Format the weekly Telegram digest.

    Mirrors the template from the retired canon-reconciler.md Step 6, adapted
    to a read-only digest (no expiry-transition section — lobster-meta
    performs and reports those nightly, not this weekly digest).
    """
    violation_count = (
        len(summary["orphans"])
        + len(summary["stale_flagged"])
        + len(summary["needs_owner"])
        + len(job_diff["unregistered"])
        + len(job_diff["stale_registry_entries"])
    )

    if violation_count == 0:
        return (
            f"Canon digest — {date_str}\n"
            f"All {total} registered artifacts are invariant-compliant. No action needed."
        )

    counts_line = ", ".join(
        f"{state}={count}" for state, count in sorted(summary["counts_by_state"].items())
    )

    return (
        f"Canon digest — {date_str}\n\n"
        f"Registered artifacts: {total} ({counts_line})\n"
        f"Invariant violations found: {violation_count}\n\n"
        f"ORPHAN QUEUE (ready for quarantine/Dan decision):\n"
        f"{_format_list(summary['orphans'])}\n\n"
        f"STALE (flagged by lobster-meta/hygiene):\n"
        f"{_format_list(summary['stale_flagged'])}\n\n"
        f"NEEDS_OWNER:\n"
        f"{_format_list(summary['needs_owner'])}\n\n"
        f"UNREGISTERED JOBS (in jobs.json, not in registry):\n"
        f"{_format_list(job_diff['unregistered'])}\n\n"
        f"STALE REGISTRY ENTRIES (no matching live job):\n"
        f"{_format_list(job_diff['stale_registry_entries'])}\n\n"
        f"Read-only digest — no registry writes performed. "
        f"Registry maintained nightly by lobster-meta, reviewed quarterly by lobster-hygiene. "
        f"CANON.md at {CANON_DOC_PATH}."
    )


# ---------------------------------------------------------------------------
# I/O boundary
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def send_telegram(chat_id: str, text: str) -> None:
    """Write the digest to the outbox for the dispatcher to deliver."""
    outbox_dir = WORKSPACE.parent / "messages" / "outbox"
    if outbox_dir.exists():
        msg_file = outbox_dir / f"canon-digest-{_today_str()}.json"
        msg_file.write_text(json.dumps({
            "chat_id": chat_id,
            "text": text,
            "source": "telegram",
        }))


def main() -> int:
    if not is_job_enabled(JOB_NAME):
        print(f"{JOB_NAME}: disabled in jobs.json — skipping.", file=sys.stderr)
        return 0

    if not REGISTRY_PATH.exists():
        print(f"{JOB_NAME}: registry not found at {REGISTRY_PATH} — skipping.", file=sys.stderr)
        return 0

    registry = _load_json(REGISTRY_PATH)
    artifacts = registry.get("artifacts", [])
    summary = summarize_registry(artifacts)

    registry_job_ids = {
        a["id"] for a in artifacts if a.get("artifact_class") == "scheduled_job"
    }
    live_job_keys: set[str] = set()
    if JOBS_PATH.exists():
        jobs_data = _load_json(JOBS_PATH)
        live_job_keys = set(jobs_data.get("jobs", {}).keys())
    job_diff = compute_job_diff(registry_job_ids, live_job_keys)

    digest = format_digest(_today_str(), summary, job_diff, total=len(artifacts))
    send_telegram(TELEGRAM_CHAT_ID, digest)
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
