#!/usr/bin/env python3
"""
WOS Metabolic Digest — daily pipeline health signal.

Runs once per day (default 10:00 UTC / 6 AM ET). On each invocation:
1. Reads UoWs that transitioned to a terminal status (done/failed/expired/cancelled)
   in the past 24h.
2. Uses outcome_category from uow_registry (real-time classification set at
   write_result time) — no heuristic re-classification needed.
3. Computes aggregates: total by outcome category, average tokens, average cycles,
   seeds surfaced (interim: from artifacts table), gate churn (from gate_fired column).
4. Formats a Telegram-friendly digest per the WOS completion report spec.
5. Writes an inbox message for the dispatcher to relay, then writes job output.

Classification source:
  outcome_category is set at write_result time by the subagent (pearl/heat/seed/shit).
  UoWs without outcome_category (pre-migration, or subagent did not classify) are
  counted as 'shit' (no verifiable outcome signal) for digest purposes.

Digest suppression:
  - Zero completed UoWs today → no message sent (idle-day suppression per spec).
  - 1–2 completed UoWs → one-liner format.
  - 3+ completed UoWs → full digest format per spec.

Gate churn (requires migration 0019 gate_fired column):
  - spiral   — escalate verdict (infinite-loop risk)
  - dead_end — pause verdict (stuck UoW)
  - burst    — throttle verdict (queue overload)
  - none     — clean dispatch path

Seeds surfaced (interim approximation):
  Count of artifacts with category='seed' across completed UoWs (over-counts
  auto-extracted issue refs but acceptable until migration 0020 lands structured seeds).

Cron schedule (daily at 10:00 UTC / 6 AM ET):
    0 10 * * * cd ~/lobster && uv run scheduled-tasks/wos-metabolic-digest.py >> ~/lobster-workspace/scheduled-jobs/logs/wos-metabolic-digest.log 2>&1 # LOBSTER-WOS-METABOLIC-DIGEST

Type B dispatch: cron calls this script directly (no inbox/ message, no LLM round-trip).
The jobs.json enabled gate is checked at the top of main() so that runtime enable/disable
is respected without touching cron.

Run standalone:
    uv run ~/lobster/scheduled-tasks/wos-metabolic-digest.py [--dry-run] [--hours N]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.jobs import is_job_enabled  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("wos-metabolic-digest")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME: str = "wos-metabolic-digest"

# Default lookback window for the digest
DEFAULT_LOOKBACK_HOURS: int = 24

# Admin chat_id for Telegram delivery (env-injected, falls back to hardcoded)
ADMIN_CHAT_ID: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")

# Terminal statuses that indicate a UoW completed in some form
TERMINAL_STATUSES: tuple[str, ...] = ("done", "failed", "expired", "cancelled")

# Outcome category names per WOS metabolic taxonomy
OUTCOME_PEARL = "pearl"
OUTCOME_HEAT = "heat"
OUTCOME_SEED = "seed"
OUTCOME_SHIT = "shit"

# All valid outcome categories (any other value or NULL → shit)
VALID_OUTCOME_CATEGORIES: frozenset[str] = frozenset({
    OUTCOME_PEARL, OUTCOME_HEAT, OUTCOME_SEED, OUTCOME_SHIT,
})

# Gate churn labels (from migration 0019 gate_fired column)
GATE_SPIRAL = "spiral"
GATE_DEAD_END = "dead_end"
GATE_BURST = "burst"
GATE_NONE = "none"

# Minimum UoW count to emit the full digest format (per spec)
FULL_DIGEST_THRESHOLD: int = 3

# Default stall threshold for still-running UoWs
STALL_THRESHOLD_HOURS: int = 6


# ---------------------------------------------------------------------------
# Workspace / path helpers
# ---------------------------------------------------------------------------

def _workspace() -> Path:
    return Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))


def _registry_db_path() -> Path:
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    return _workspace() / "orchestration" / "registry.db"


def _inbox_dir() -> Path:
    messages_base = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_base / "inbox"


# ---------------------------------------------------------------------------
# Registry query — reads completed UoWs in the lookback window
# ---------------------------------------------------------------------------

def query_completed_uows(db_path: Path, lookback_hours: int) -> list[dict]:
    """
    Return UoWs that transitioned to a terminal status in the past lookback_hours.

    Uses audit_log to find UoWs that entered a terminal state (done, failed,
    expired, cancelled) within the window. Returns full UoW rows for each match,
    including outcome_category (migration 0018), gate_fired (migration 0019),
    token_usage (migration 0015), and steward_cycles.

    gate_fired and seeds_surfaced columns are selected defensively — they fall back
    to NULL when the column is absent (pre-migration instance).

    Returns [] if DB absent or table missing.
    """
    if not db_path.exists():
        log.warning("Registry DB not found at %s", db_path)
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    terminal_placeholders = ",".join("?" * len(TERMINAL_STATUSES))

    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            # Find UoW IDs that entered a terminal status within the window
            transition_rows = conn.execute(
                f"""
                SELECT DISTINCT uow_id FROM audit_log
                WHERE to_status IN ({terminal_placeholders})
                  AND ts >= ?
                """,
                (*TERMINAL_STATUSES, cutoff),
            ).fetchall()

            if not transition_rows:
                return []

            uow_ids = [r["uow_id"] for r in transition_rows]
            id_placeholders = ",".join("?" * len(uow_ids))

            # Check which optional columns exist (defensive for pre-migration instances)
            col_info = conn.execute(
                "PRAGMA table_info(uow_registry)"
            ).fetchall()
            col_names = {row["name"] for row in col_info}

            gate_fired_col = "gate_fired" if "gate_fired" in col_names else "NULL AS gate_fired"
            seeds_surfaced_col = "seeds_surfaced" if "seeds_surfaced" in col_names else "NULL AS seeds_surfaced"

            rows = conn.execute(
                f"""
                SELECT id, status, summary, register, close_reason, output_ref,
                       started_at, closed_at, updated_at, created_at, completed_at,
                       steward_cycles, token_usage,
                       outcome_category,
                       {gate_fired_col},
                       {seeds_surfaced_col}
                FROM uow_registry
                WHERE id IN ({id_placeholders})
                  AND status IN ({terminal_placeholders})
                """,
                (*uow_ids, *TERMINAL_STATUSES),
            ).fetchall()

            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Completed UoW query failed: %s", exc)
        return []


def query_seeds_from_artifacts(db_path: Path, uow_ids: list[str]) -> int:
    """
    Interim seeds count from artifacts table (migration 0020 approximation).

    Counts artifacts with category='seed' across the given UoW IDs.
    Over-counts auto-extracted issue refs but is acceptable until structured
    seeds_surfaced field (migration 0020) lands.

    Returns 0 if the artifacts table is absent or query fails.
    """
    if not db_path.exists() or not uow_ids:
        return 0

    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            id_placeholders = ",".join("?" * len(uow_ids))
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt FROM artifacts
                WHERE uow_id IN ({id_placeholders})
                  AND category = 'seed'
                """,
                uow_ids,
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def query_still_running(db_path: Path, threshold_hours: int) -> list[dict]:
    """
    Return UoWs in 'active' or 'executing' that have been running >threshold_hours.
    Used to populate the 'Stalled' section of the digest.
    """
    if not db_path.exists():
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=threshold_hours)).isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, status, summary, started_at, created_at
                FROM uow_registry
                WHERE status IN ('active', 'executing')
                  AND (
                    (started_at IS NOT NULL AND started_at <= ?)
                    OR (started_at IS NULL AND created_at <= ?)
                  )
                ORDER BY created_at ASC
                """,
                (cutoff, cutoff),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Still-running query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Classification — reads outcome_category from the DB row
# ---------------------------------------------------------------------------

def resolve_outcome_category(uow: dict) -> str:
    """
    Return the metabolic outcome category for a UoW.

    Uses outcome_category if present and valid. Falls back to 'shit' for:
    - NULL outcome_category (subagent did not classify, or pre-migration)
    - unrecognized outcome_category value
    - non-done terminal statuses (failed, expired, cancelled) regardless of
      outcome_category (these are definitively shit unless reclassified)

    Note: unlike the old heuristic classifier, this function trusts the
    subagent's classification from write_result time. The only override is
    terminal failure: failed/expired/cancelled always map to shit.
    """
    status = (uow.get("status") or "").lower()

    # Hard override: failure/expiry/cancellation → shit regardless of outcome_category
    if status in ("failed", "expired", "cancelled"):
        return OUTCOME_SHIT

    cat = (uow.get("outcome_category") or "").lower()
    if cat in VALID_OUTCOME_CATEGORIES:
        return cat

    # Null or unrecognized outcome_category → shit (no verifiable signal)
    return OUTCOME_SHIT


def aggregate_by_category(uows: list[dict]) -> dict[str, list[dict]]:
    """
    Group UoWs by resolved outcome category.

    Returns a dict mapping category → list of uow dicts.
    All four keys are always present (possibly empty lists).
    """
    groups: dict[str, list[dict]] = {
        OUTCOME_PEARL: [],
        OUTCOME_HEAT: [],
        OUTCOME_SEED: [],
        OUTCOME_SHIT: [],
    }
    for uow in uows:
        label = resolve_outcome_category(uow)
        groups[label].append(uow)
    return groups


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------

def _avg_int(values: list[int | None]) -> float | None:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 1) if nums else None


def aggregate_token_usage(uows: list[dict]) -> int | None:
    values = [u["token_usage"] for u in uows if u.get("token_usage") is not None]
    return sum(values) if values else None


def aggregate_gate_churn(uows: list[dict]) -> dict[str, int]:
    """
    Count UoWs by gate_fired value.

    Returns dict with keys spiral, dead_end, burst (zero-filled for absent values).
    The 'none' gate (clean path) is excluded from the churn signal — churn is
    specifically about non-clean paths.
    """
    churn: dict[str, int] = {GATE_SPIRAL: 0, GATE_DEAD_END: 0, GATE_BURST: 0}
    for uow in uows:
        gate = (uow.get("gate_fired") or GATE_NONE).lower()
        if gate in churn:
            churn[gate] += 1
    return churn


def count_seeds_surfaced(uows: list[dict], db_path: Path) -> int:
    """
    Return total seeds surfaced across completed UoWs.

    Prefers the structured seeds_surfaced column (migration 0020) per UoW.
    Falls back to the artifacts-table approximation when seeds_surfaced is absent
    or NULL for all UoWs (interim until migration 0020 is universally deployed).
    """
    # Try structured seeds_surfaced column first
    structured_values = []
    for uow in uows:
        sv = uow.get("seeds_surfaced")
        if sv is not None:
            try:
                parsed = json.loads(sv) if isinstance(sv, str) else sv
                count = len(parsed) if isinstance(parsed, list) else int(parsed)
                structured_values.append(count)
            except (ValueError, TypeError):
                pass

    if structured_values:
        return sum(structured_values)

    # Interim: count from artifacts table
    uow_ids = [u["id"] for u in uows]
    return query_seeds_from_artifacts(db_path, uow_ids)


# ---------------------------------------------------------------------------
# Digest formatter — pure function, returns Telegram-ready text
# ---------------------------------------------------------------------------

def format_digest(
    groups: dict[str, list[dict]],
    stalled: list[dict],
    lookback_hours: int,
    now_dt: datetime,
    seeds_total: int,
) -> str | None:
    """
    Format the metabolic digest as a Telegram message.

    Returns None if no UoWs completed (idle-day suppression per spec).
    Returns a one-liner for 1-2 UoWs, full format for 3+.

    Args:
        groups:         Classification groups from aggregate_by_category.
        stalled:        UoWs still running past the stall threshold.
        lookback_hours: The window used for the digest.
        now_dt:         Current datetime (UTC) for the report header.
        seeds_total:    Total seeds surfaced count.
    """
    pearl_count = len(groups[OUTCOME_PEARL])
    heat_count = len(groups[OUTCOME_HEAT])
    seed_count = len(groups[OUTCOME_SEED])
    shit_count = len(groups[OUTCOME_SHIT])
    total = pearl_count + heat_count + seed_count + shit_count

    # Idle-day suppression: no UoWs completed → no digest
    if total == 0:
        return None

    all_uows = [u for uows in groups.values() for u in uows]
    date_str = now_dt.strftime("%Y-%m-%d")

    # One-liner for < FULL_DIGEST_THRESHOLD completed UoWs
    if total < FULL_DIGEST_THRESHOLD:
        total_tokens = aggregate_token_usage(all_uows)
        token_str = f" — {total_tokens:,} tokens" if total_tokens is not None else ""
        parts = []
        if pearl_count:
            parts.append(f"pearl {pearl_count}")
        if heat_count:
            parts.append(f"heat {heat_count}")
        if seed_count:
            parts.append(f"seed {seed_count}")
        if shit_count:
            parts.append(f"shit {shit_count}")
        categories = ", ".join(parts) if parts else "none"
        return f"WOS: {total} completed ({categories}){token_str}"

    # Full digest format (per spec)
    lines = [
        f"WOS Daily — {date_str}",
        f"Completed : {total} UoW(s)",
        f"  pearl {pearl_count}  seed {seed_count}  heat {heat_count}  shit {shit_count}",
    ]

    # Seeds surfaced
    if seeds_total > 0:
        lines.append(f"  Seeds surfaced: {seeds_total}")

    # Average tokens and cycles
    avg_tokens = _avg_int([u.get("token_usage") for u in all_uows])
    avg_cycles = _avg_int([u.get("steward_cycles") for u in all_uows])
    if avg_tokens is not None:
        lines.append(f"  Avg tokens: {int(avg_tokens):,}")
    if avg_cycles is not None:
        lines.append(f"  Avg cycles: {avg_cycles}")

    # Failed UoWs (non-done terminal statuses are already counted in shit)
    failed_uows = [u for u in all_uows if u.get("status") in ("failed", "expired", "cancelled")]
    if failed_uows:
        # Dominant gate among failed UoWs
        gate_counts: dict[str, int] = {}
        for u in failed_uows:
            g = (u.get("gate_fired") or GATE_NONE).lower()
            gate_counts[g] = gate_counts.get(g, 0) + 1
        dominant_gate = max(gate_counts, key=gate_counts.__getitem__) if gate_counts else GATE_NONE
        lines.append(f"Failed    : {len(failed_uows)} UoW(s) ({dominant_gate})")

    # Gate churn across all completed UoWs
    churn = aggregate_gate_churn(all_uows)
    churn_total = sum(churn.values())
    if churn_total > 0:
        churn_parts = []
        if churn[GATE_SPIRAL]:
            churn_parts.append(f"{churn[GATE_SPIRAL]} spiral")
        if churn[GATE_DEAD_END]:
            churn_parts.append(f"{churn[GATE_DEAD_END]} dead-end")
        if churn[GATE_BURST]:
            churn_parts.append(f"{churn[GATE_BURST]} burst")
        lines.append(f"Churn     : {' / '.join(churn_parts)} today")

    # Stalled UoWs
    if stalled:
        stalled_ids = ", ".join(u["id"] for u in stalled[:5])
        stall_line = f"Stalled   : {stalled_ids}"
        if len(stalled) > 5:
            stall_line += f" (+{len(stalled) - 5} more)"
        lines.append(stall_line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Inbox message writer — sends digest via inbox → Telegram
# ---------------------------------------------------------------------------

def _write_inbox_digest(text: str, dry_run: bool = False) -> None:
    """
    Write a wos_metabolic_digest inbox message for the dispatcher to relay via Telegram.

    In dry_run mode: logs the message but does not write the file.
    """
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "type": "wos_metabolic_digest",
        "chat_id": ADMIN_CHAT_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
    }

    if dry_run:
        log.info("DRY RUN: would send digest:\n%s", text)
        return

    try:
        inbox = _inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        tmp_path = inbox / f"{msg_id}.json.tmp"
        dest_path = inbox / f"{msg_id}.json"
        tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp_path.rename(dest_path)
        log.info("Wrote metabolic digest inbox message %s", msg_id)
    except Exception as exc:
        log.warning("Failed to write digest inbox message: %s", exc)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_digest(db_path: Path, lookback_hours: int, dry_run: bool = False) -> dict:
    """
    Run the metabolic digest and return a structured result dict.
    """
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    # Query completed UoWs
    completed = query_completed_uows(db_path, lookback_hours)
    log.info("Completed UoWs in last %dh: %d", lookback_hours, len(completed))

    # Group by outcome_category
    groups = aggregate_by_category(completed)
    for label, uows in groups.items():
        log.info("  %s: %d", label, len(uows))

    # Seeds surfaced (structured or interim artifact approximation)
    seeds_total = count_seeds_surfaced(completed, db_path)

    # Stalled UoWs (>6h in flight)
    stalled = query_still_running(db_path, threshold_hours=STALL_THRESHOLD_HOURS)
    if stalled:
        log.warning("Still running >%dh: %d", STALL_THRESHOLD_HOURS, len(stalled))

    # Format digest (may return None for idle days)
    digest_text = format_digest(groups, stalled, lookback_hours, now_dt, seeds_total)

    if digest_text is None:
        log.info("No UoWs completed in last %dh — suppressing idle-day digest", lookback_hours)
    else:
        log.info("Digest:\n%s", digest_text)
        _write_inbox_digest(digest_text, dry_run=dry_run)

    all_uows = [u for uows in groups.values() for u in uows]
    total_tokens = aggregate_token_usage(all_uows)
    churn = aggregate_gate_churn(all_uows)

    return {
        "timestamp": now_iso,
        "lookback_hours": lookback_hours,
        "completed_count": len(completed),
        "pearl": len(groups[OUTCOME_PEARL]),
        "heat": len(groups[OUTCOME_HEAT]),
        "seed": len(groups[OUTCOME_SEED]),
        "shit": len(groups[OUTCOME_SHIT]),
        "seeds_surfaced": seeds_total,
        "stalled_count": len(stalled),
        "gate_churn_spiral": churn[GATE_SPIRAL],
        "gate_churn_dead_end": churn[GATE_DEAD_END],
        "gate_churn_burst": churn[GATE_BURST],
        "total_tokens": total_tokens,
        "suppressed": digest_text is None,
        "dry_run": dry_run,
        "digest_text": digest_text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="WOS metabolic digest — daily pipeline health signal")
    parser.add_argument("--dry-run", action="store_true", help="Compute digest but do not send Telegram message")
    parser.add_argument("--hours", type=int, default=DEFAULT_LOOKBACK_HOURS,
                        help=f"Lookback window in hours (default: {DEFAULT_LOOKBACK_HOURS})")
    args = parser.parse_args()

    if not is_job_enabled(JOB_NAME):
        log.info("Job '%s' is disabled in jobs.json — skipping", JOB_NAME)
        return 0

    db_path = _registry_db_path()
    log.info(
        "Running WOS metabolic digest — registry: %s lookback=%dh dry_run=%s",
        db_path, args.hours, args.dry_run,
    )

    result = run_digest(db_path, lookback_hours=args.hours, dry_run=args.dry_run)
    log.info(
        "Digest complete — pearl=%d heat=%d seed=%d shit=%d stalled=%d suppressed=%s",
        result["pearl"], result["heat"], result["seed"], result["shit"],
        result["stalled_count"], result["suppressed"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
