#!/usr/bin/env python3
"""
Scheduled job: abandonment auto-complete (sliding-timer guardrail) for the
agent channel (source="local-claude"), agent-channel-protocol-proposal.md
§3.3 / issue #1525.

The problem this closes: `_LOCAL_CLAUDE_STALE_TIMEOUT_SECONDS` (600s,
`src/mcp/inbox_server.py`) only handles the *reclaim* case — a stale claim is
released and the message is moved back to `inbox/` so a NEW claimant can pick
it up. It does nothing for the *nobody-reclaims* case: a crashed claimant
with no reclaimer leaves the exchange sitting in OPEN (claimed, or released
back to `inbox/` and never re-claimed) forever, with the collaborator polling
until its own client-side timeout and the server-side state never resolving.

This is deliberately a NEW job, not an extension of `agent-replies-sweep.py`
— that script's own docstring disclaims touching `inbox/`, `processing/`, or
the claims DB, precisely because "still being worked" state lives there, not
in `agent-replies/`. This job cross-references three data sources that the
flat sweep never touches:

- `~/messages/processing/` — messages still claimed and in flight.
- The claims DB (`src/mcp/claims.py`) — `claimed_at` for the live claim row,
  when one still exists.
- `~/messages/agent-replies/` — `<request_id>.ack.json` (the most recent
  accepted `write_progress`/claim-time ack write) and `<request_id>.json`
  (the terminal reply, if any).

Sliding timer, not a fixed guillotine (Hydra timer+extension pattern, spec
§3.3): the deadline evaluated for each candidate exchange is
`max(claimed_at, last accepted .ack.json write) + ABANDONMENT_WINDOW_HOURS`.
Every accepted `write_progress` call extends the deadline forward (it
overwrites `.ack.json`, moving its timestamp/mtime later) — a claimant that
keeps working never trips the guardrail, regardless of how long the work
takes. Only once the deadline passes with NO extension and NO terminal reply
does this job write a synthetic terminal reply — via
`agent_channel.write_reply()`, the same single-shot, first-writer-wins
primitive the real reply path uses, so a real reply racing in concurrently
is never clobbered (write_reply's own atomic_create_json check-then-write is
the message-after-complete guard here; this job adds no locking of its own).

Conservative-by-construction candidate discovery (spec: "never auto-complete
an exchange that could still be legitimately in progress"):

- Candidates are discovered ONLY from `<request_id>.ack.json` files in
  `agent-replies/` (written exclusively by `write_ack`/`write_progress` —
  unambiguously local-claude) and from `processing/` files whose own
  `source` field is exactly `"local-claude"`. The claims DB is never scanned
  in bulk (it is shared by every source, not just this channel) — it is only
  ever queried by `request_id` for candidates already known to be
  local-claude via one of the two sources above, using `get_claimed_at()`
  (agent-channel protocol v1 §2.2's convention: `message_id == request_id`
  for this channel).
- The deadline anchor is the MAX of every available signal, never the min —
  any one live signal (an existing claim row, or a recent `.ack.json` write)
  is enough to keep an exchange out of the auto-complete path.
- A configured window below `MIN_ABANDONMENT_WINDOW_HOURS` is clamped up
  (with a warning), not honored — protects against an operator
  misconfiguration racing the 600s claim-liveness reclaim window (spec:
  the abandonment window "must still be materially larger than the 600s
  claim-liveness timeout").
- `write_reply()`'s own first-writer-wins semantics mean a real reply
  landing between this job's scan and its write is never overwritten — the
  call is simply a no-op for that request_id (`reply_slot_created=False`,
  same as any other lost race on this channel).

Known, accepted scope limit (documented, not a bug): an exchange whose
`write_ack` call failed at claim time (rare filesystem error — the
exception path in `agent_channel.write_ack()` leaves no `.ack.json`) AND
whose claim row is later released by `_recover_stale_processing()` (moving
the message back to `inbox/`, deleting the claim row) becomes
undiscoverable to this job once both of those have happened — it reverts to
plain "Requested, unclaimed" state with no OPEN-exchange signal anywhere.
That is outside the sliding-timer's scope by construction (the guardrail
only ever governs exchanges the protocol considers OPEN — see the state
table in the proposal's §3) — the same as any other never-claimed message
sitting in `inbox/`, and not a regression this job introduces.

No EventBus / events.jsonl integration (deliberate, matches
`agent-replies-sweep.py`'s existing precedent): this is a standalone
cron-direct script with no live EventBus listener config to attach to.
Auditability is via this job's own task-output write and log line, plus the
synthetic reply's own content, which says plainly that it was
auto-completed by this sweep.

Schedule: every 15 minutes (see scripts/upgrade.sh migration 144 for the
cron entry) — the abandonment window itself is hours, but frequent scanning
keeps the gap between "deadline lapses" and "synthetic reply exists" small
without needing sub-minute precision.
Job name: agent-channel-abandonment-sweep

Run standalone:
    uv run ~/lobster/scheduled-tasks/agent-channel-abandonment-sweep.py \\
        [--dry-run] [--window-hours N]
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
from src.mcp.agent_channel import write_reply  # noqa: E402
from src.mcp.claims import AtomicClaimDB  # noqa: E402
from src.protocol.agent_channel_schema import SOURCE as LOCAL_CLAUDE_SOURCE  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME = "agent-channel-abandonment-sweep"

# Mirrors the request_id charset allowlist enforced server-side
# (src/mcp/reliability.py: sanitize_request_id / src/protocol/agent_channel_schema.py:
# REQUEST_ID_PATTERN) — deliberately duplicated rather than imported so a
# malformed on-disk filename can never crash this scan (same defensive
# posture as agent-replies-sweep.py's own copy of this pattern).
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ACK_SUFFIX = ".ack.json"
_REPLY_SUFFIX = ".json"

# Spec-resolved default (agent-channel-protocol-proposal.md §6 dial 2):
# "sensible default + extendable" — ~24h, materially larger than the 600s
# claim-liveness reclaim timeout. A genuinely alive claimant that keeps
# calling write_progress never hits this regardless of the exact number.
DEFAULT_ABANDONMENT_WINDOW_HOURS = 24.0
ABANDONMENT_WINDOW_HOURS_ENV = "LOBSTER_AGENT_CHANNEL_ABANDONMENT_WINDOW_HOURS"

# Conservative floor: 12x the 600s (0.1667h) claim-liveness reclaim timeout.
# A configured window below this is clamped up (with a warning) rather than
# honored — an operator fat-fingering this env var must not be able to make
# the abandonment guardrail race the reclaim mechanism it is layered on top
# of.
MIN_ABANDONMENT_WINDOW_HOURS = 2.0

_LOCAL_CLAUDE_STALE_TIMEOUT_SECONDS = 600  # mirrors inbox_server.py's constant, for the docstring/comment above only

MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages")))
AGENT_REPLIES_DIR = MESSAGES_DIR / "agent-replies"
PROCESSING_DIR = MESSAGES_DIR / "processing"

# Mirrors claims.py's own _DEFAULT_DB_PATH computation. Recomputed here as a
# separate, patchable module attribute (rather than relying on
# AtomicClaimDB()'s no-arg default) so tests can point main() at a per-test
# SQLite file the same way they already patch AGENT_REPLIES_DIR/PROCESSING_DIR
# — claims.py's own default is fixed at import time from the process's
# LOBSTER_MESSAGES env var and does not observe a later patch.dict() call.
CLAIMS_DB_PATH = MESSAGES_DIR / "config" / "agent_sessions.db"


def _synthetic_reply_text(window_hours: float) -> str:
    """Compose the synthetic terminal reply text — spec's own phrasing."""
    return (
        "Exchange timed out, no result produced. No progress update or reply "
        f"was recorded for over {window_hours:g} hours after this request was "
        "claimed, so it was auto-completed by the abandonment sweep "
        f"({JOB_NAME}) — the claimant likely crashed or exited without "
        "answering."
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateSignals:
    """The raw timestamps collected for one candidate request_id — pure data, no I/O."""

    request_id: str
    ack_ts: datetime | None
    claimed_at_ts: datetime | None


@dataclass(frozen=True)
class AbandonmentPlan:
    """One decision for one candidate exchange — pure data, no I/O."""

    request_id: str
    action: str  # "auto_complete" | "keep_fresh" | "already_complete" | "no_signal"
    last_signal_ts: datetime | None
    age_hours: float | None


def _parse_iso_ts(raw: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp, tolerant of a trailing 'Z'. None on failure/absence."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def _ack_signal_ts(payload: dict | None, mtime_epoch: float) -> datetime:
    """
    Timestamp for one .ack.json file: prefer the payload's own `ts` field
    (survives a cp/rsync that would otherwise reset mtime) over filesystem
    mtime — same preference order as agent-replies-sweep.py's
    `_file_age_hours`.
    """
    parsed = _parse_iso_ts((payload or {}).get("ts"))
    if parsed is not None:
        return parsed
    return datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)


def resolve_last_signal(signals: CandidateSignals) -> datetime | None:
    """
    Sliding-timer anchor: max(claimed_at, last accepted .ack.json write).
    Any one live signal is sufficient — this is deliberately the MAX, not
    the min, so a candidate is never penalized for a missing signal as long
    as at least one is present. None only when neither signal exists (should
    not happen given how candidates are discovered — see plan_abandonment_sweep).
    """
    candidates = [ts for ts in (signals.ack_ts, signals.claimed_at_ts) if ts is not None]
    if not candidates:
        return None
    return max(candidates)


def plan_abandonment_sweep(
    candidates: list[CandidateSignals],
    reply_exists: set[str],
    window_hours: float,
    now: datetime,
) -> list[AbandonmentPlan]:
    """
    Pure decision function: given one CandidateSignals per discovered
    request_id, the set of request_ids that already have a terminal reply,
    the configured window, and "now", return one AbandonmentPlan per
    candidate. No I/O.
    """
    plans: list[AbandonmentPlan] = []
    for signals in candidates:
        if signals.request_id in reply_exists:
            plans.append(AbandonmentPlan(signals.request_id, "already_complete", None, None))
            continue
        last_signal = resolve_last_signal(signals)
        if last_signal is None:
            # Discovered as a candidate but neither signal actually resolved
            # (e.g. an unparseable claimed_at with no ack file at all) —
            # conservative: never guess, never auto-complete without a
            # signal to measure against.
            plans.append(AbandonmentPlan(signals.request_id, "no_signal", None, None))
            continue
        age_hours = (now - last_signal).total_seconds() / 3600.0
        action = "auto_complete" if age_hours >= window_hours else "keep_fresh"
        plans.append(AbandonmentPlan(signals.request_id, action, last_signal, age_hours))
    return plans


def summarize(plans: list[AbandonmentPlan], window_hours: float, dry_run: bool) -> str:
    """Compose a human-readable summary string. Pure function."""
    auto_completed = [p for p in plans if p.action == "auto_complete"]
    kept_fresh = [p for p in plans if p.action == "keep_fresh"]
    already_complete = [p for p in plans if p.action == "already_complete"]
    no_signal = [p for p in plans if p.action == "no_signal"]

    verb = "Would auto-complete" if dry_run else "Auto-completed"
    lines = [
        f"{JOB_NAME} — abandonment window {window_hours:g}h",
        f"{verb} {len(auto_completed)} abandoned exchange(s), kept {len(kept_fresh)} "
        f"within-window out of {len(plans)} candidate(s) scanned.",
    ]
    if already_complete:
        lines.append(f"{len(already_complete)} candidate(s) already had a terminal reply — skipped.")
    if no_signal:
        lines.append(
            f"{len(no_signal)} candidate(s) had no resolvable timestamp signal — left alone, not auto-completed."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Side-effecting boundary functions
# ---------------------------------------------------------------------------

def scan_ack_candidates(agent_replies_dir: Path) -> dict[str, tuple[dict | None, float]]:
    """
    Return {request_id: (parsed_ack_payload_or_None, mtime_epoch)} for every
    `<request_id>.ack.json` in agent_replies_dir. Only .ack.json is scanned
    here — .json (the terminal reply) is handled separately by
    scan_existing_replies(), and by-agent/ pointer files are out of scope
    (a different, unrelated mailbox feature).
    """
    found: dict[str, tuple[dict | None, float]] = {}
    if not agent_replies_dir.exists():
        return found
    for file_path in sorted(agent_replies_dir.iterdir()):
        if not file_path.is_file() or not file_path.name.endswith(_ACK_SUFFIX):
            continue
        candidate = file_path.name[: -len(_ACK_SUFFIX)]
        if not _REQUEST_ID_PATTERN.match(candidate):
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
        found[candidate] = (payload, mtime)
    return found


def scan_existing_replies(agent_replies_dir: Path) -> set[str]:
    """Return the set of request_ids that already have a terminal reply file."""
    found: set[str] = set()
    if not agent_replies_dir.exists():
        return found
    for file_path in sorted(agent_replies_dir.iterdir()):
        if not file_path.is_file():
            continue
        name = file_path.name
        if name.endswith(_ACK_SUFFIX) or not name.endswith(_REPLY_SUFFIX):
            continue
        candidate = name[: -len(_REPLY_SUFFIX)]
        if _REQUEST_ID_PATTERN.match(candidate):
            found.add(candidate)
    return found


def scan_processing_candidates(processing_dir: Path) -> set[str]:
    """
    Return the set of request_ids for every file in processing_dir whose
    parsed JSON has source == "local-claude" — this is the second candidate
    discovery source (alongside .ack.json files), catching the rare case
    where write_ack's own write failed at claim time and no .ack.json exists
    yet, but the exchange is still genuinely claimed and in flight.
    """
    found: set[str] = set()
    if not processing_dir.exists():
        return found
    for file_path in sorted(processing_dir.iterdir()):
        if not file_path.is_file():
            continue
        try:
            msg = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if msg.get("source") != LOCAL_CLAUDE_SOURCE:
            continue
        request_id = msg.get("request_id") or msg.get("id")
        if request_id and _REQUEST_ID_PATTERN.match(str(request_id)):
            found.add(str(request_id))
    return found


def build_candidates(
    ack_candidates: dict[str, tuple[dict | None, float]],
    processing_request_ids: set[str],
    claims_db: AtomicClaimDB,
) -> list[CandidateSignals]:
    """
    Merge the two discovery sources into one CandidateSignals per
    request_id, then query the claims DB by request_id (targeted lookup
    only — never a bulk scan, since the claims DB is shared by every source,
    not just local-claude) for the claimed_at signal.
    """
    all_ids = set(ack_candidates) | processing_request_ids
    candidates: list[CandidateSignals] = []
    for request_id in sorted(all_ids):
        ack_payload, ack_mtime = ack_candidates.get(request_id, (None, None))
        ack_ts = _ack_signal_ts(ack_payload, ack_mtime) if ack_mtime is not None else None
        claimed_at_ts = _parse_iso_ts(claims_db.get_claimed_at(request_id))
        candidates.append(CandidateSignals(request_id=request_id, ack_ts=ack_ts, claimed_at_ts=claimed_at_ts))
    return candidates


def apply_abandonment_sweep(
    agent_replies_dir: Path,
    plans: list[AbandonmentPlan],
    window_hours: float,
    dry_run: bool,
) -> tuple[int, list[str]]:
    """
    Execute "auto_complete" plans by writing a synthetic terminal reply via
    agent_channel.write_reply() — the same single-shot, first-writer-wins
    primitive the real reply path uses. Returns (auto_completed_count, errors).

    write_reply()'s own atomic_create_json check-then-write means a real
    reply racing in between this job's scan and this call is never
    clobbered: reply_slot_created comes back False and this function simply
    does not count that request_id, exactly like any other lost race on
    this channel (agent-channel protocol spec principle 1).
    """
    auto_completed = 0
    errors: list[str] = []
    text = _synthetic_reply_text(window_hours)
    for plan in plans:
        if plan.action != "auto_complete":
            continue
        if dry_run:
            auto_completed += 1
            continue
        try:
            outcome = write_reply(
                agent_replies_dir=agent_replies_dir,
                request_id=plan.request_id,
                text=text,
                in_reply_to=plan.request_id,
            )
            if outcome.reply_slot_created:
                auto_completed += 1
            # else: lost the race to a real reply that landed concurrently —
            # not an error, not counted, same as any other lost race.
        except Exception as exc:
            errors.append(f"{plan.request_id}: {exc}")
    return auto_completed, errors


def write_task_output(task_outputs_dir: Path, job_name: str, summary: str, status: str) -> None:
    """
    Write job output to the task-outputs directory in the same format as the
    write_task_output MCP tool. Mirrors agent-replies-sweep.py's helper of
    the same name.
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

def _resolve_window_hours(cli_value: float | None) -> float:
    if cli_value is not None:
        raw_value = cli_value
    else:
        raw_value = None
        env_raw = os.environ.get(ABANDONMENT_WINDOW_HOURS_ENV)
        if env_raw:
            try:
                raw_value = float(env_raw)
            except ValueError:
                raw_value = None
        if raw_value is None:
            raw_value = DEFAULT_ABANDONMENT_WINDOW_HOURS
    if raw_value < MIN_ABANDONMENT_WINDOW_HOURS:
        print(
            f"[{JOB_NAME}] configured window {raw_value:g}h is below the "
            f"{MIN_ABANDONMENT_WINDOW_HOURS:g}h floor — clamping up."
        )
        return MIN_ABANDONMENT_WINDOW_HOURS
    return raw_value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            f"{JOB_NAME} — abandonment auto-complete (sliding-timer guardrail) "
            "for the agent channel (source=\"local-claude\")."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be auto-completed; do not write or write task output.")
    parser.add_argument(
        "--window-hours",
        type=float,
        default=None,
        help=f"Override the abandonment window (default {DEFAULT_ABANDONMENT_WINDOW_HOURS:g}h / env {ABANDONMENT_WINDOW_HOURS_ENV}).",
    )
    args = parser.parse_args(argv)

    if not is_job_enabled(JOB_NAME):
        print(f"[{JOB_NAME}] disabled in jobs.json — skipping.")
        return 0

    window_hours = _resolve_window_hours(args.window_hours)
    now = datetime.now(timezone.utc)

    claims_db = AtomicClaimDB(path=CLAIMS_DB_PATH)

    ack_candidates = scan_ack_candidates(AGENT_REPLIES_DIR)
    processing_request_ids = scan_processing_candidates(PROCESSING_DIR)
    reply_exists = scan_existing_replies(AGENT_REPLIES_DIR)

    candidates = build_candidates(ack_candidates, processing_request_ids, claims_db)
    plans = plan_abandonment_sweep(candidates, reply_exists, window_hours, now)
    auto_completed_count, errors = apply_abandonment_sweep(AGENT_REPLIES_DIR, plans, window_hours, args.dry_run)

    summary = summarize(plans, window_hours, args.dry_run)
    planned_auto_complete = sum(1 for p in plans if p.action == "auto_complete")
    if not args.dry_run and auto_completed_count != planned_auto_complete:
        # A real reply landed concurrently between scan-time and apply-time
        # for at least one candidate — not an error, just worth surfacing:
        # write_reply()'s first-writer-wins semantics mean the lost race is
        # silently correct, not silently wrong.
        summary += (
            f"\n{planned_auto_complete - auto_completed_count} planned auto-complete(s) "
            "lost the race to a real reply landing concurrently — not written, not an error."
        )
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
