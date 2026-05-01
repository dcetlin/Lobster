#!/usr/bin/env python3
"""
registry_cli.py — UoW Registry command-line interface.

All commands output JSON to stdout unless noted otherwise. All writes use
BEGIN IMMEDIATE transactions via the Registry class. Audit log entries are
written atomically with state changes.

Usage:
    uv run registry_cli.py upsert --issue <N> --title <T> [--sweep-date <YYYY-MM-DD>]
    uv run registry_cli.py get --id <uow-id>
    uv run registry_cli.py list [--status <status>]
    uv run registry_cli.py approve --id <uow-id>
    uv run registry_cli.py check-stale
    uv run registry_cli.py expire-proposals
    uv run registry_cli.py gate-readiness
    uv run registry_cli.py trace --id <uow-id>
    uv run registry_cli.py report [--since HOURS] [--from ISO_DATE]
    uv run registry_cli.py failure-breakdown [--since ISO_TIMESTAMP]

Environment:
    REGISTRY_DB_PATH — override the default db path (~/.../orchestration/registry.db)
"""

import argparse
import dataclasses
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow importing registry module whether run as script or via uv run
_SRC_DIR = Path(__file__).parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from orchestration.registry import (
    ApproveConfirmed,
    ApproveExpired,
    ApproveNotFound,
    ApproveSkipped,
    Registry,
    UoW,
    UpsertInserted,
    UpsertSkipped,
    _gh_issue_is_closed,
)


def _get_db_path() -> Path:
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    return Path(workspace) / "orchestration" / "registry.db"


def _uow_to_dict(uow: UoW) -> dict:
    """Serialize a UoW dataclass to a JSON-safe dict."""
    d = dataclasses.asdict(uow)
    d["status"] = str(uow.status)  # convert StrEnum to plain string
    return d


def _output(data: dict | list) -> None:
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_upsert(registry: Registry, args: argparse.Namespace) -> None:
    issue_body = getattr(args, "issue_body", None) or ""
    if issue_body:
        from orchestration.cultivator import _extract_success_criteria
        success_criteria = _extract_success_criteria(issue_body)
    else:
        success_criteria = ""
    # Enforce the germination contract: success_criteria must not be empty.
    # Fall back to the title so CLI users without --issue-body still succeed;
    # callers who need richer criteria should pass --issue-body.
    if not success_criteria or not success_criteria.strip():
        success_criteria = args.title
    result = registry.upsert(
        issue_number=args.issue,
        title=args.title,
        sweep_date=getattr(args, "sweep_date", None),
        success_criteria=success_criteria,
    )
    match result:
        case UpsertInserted(id=uow_id):
            _output({"id": uow_id, "action": "inserted"})
        case UpsertSkipped(id=uow_id, reason=reason):
            _output({"id": uow_id, "action": "skipped", "reason": reason})


def cmd_get(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.get(args.id)
    if result is None:
        _output({"error": "not found", "id": args.id})
    else:
        _output(_uow_to_dict(result))


def cmd_list(registry: Registry, args: argparse.Namespace) -> None:
    status = getattr(args, "status", None)
    records = registry.list(status=status)
    _output([_uow_to_dict(r) for r in records])


def cmd_approve(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.approve(args.id)
    match result:
        case ApproveConfirmed(id=uow_id):
            _output({"id": uow_id, "status": "pending", "previous_status": "proposed"})
        case ApproveNotFound(id=uow_id):
            _output({
                "error": "not found",
                "id": uow_id,
                "message": f"UoW `{uow_id}` not found. Run `list --status proposed` to see current proposals.",
            })
        case ApproveExpired(id=uow_id):
            _output({
                "error": "expired",
                "id": uow_id,
                "message": f"UoW `{uow_id}` has expired. Wait for the next sweep to re-propose.",
            })
        case ApproveSkipped(id=uow_id, current_status=current_status, reason=reason):
            _output({
                "id": uow_id,
                "status": current_status,
                "action": "noop",
                "reason": reason,
            })


def cmd_check_stale(registry: Registry, args: argparse.Namespace) -> None:
    stale = registry.check_stale(issue_checker=_gh_issue_is_closed)
    _output([_uow_to_dict(u) for u in stale])


def cmd_expire_proposals(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.expire_proposals()
    _output(result)


def cmd_gate_readiness(registry: Registry, args: argparse.Namespace) -> None:
    gs = registry.registry_health()
    _output({
        "gate_met": gs.gate_met,
        "phase": "wos_active",
        "days_running": gs.days_running,
        "proposed_to_confirmed_ratio_7d": gs.approval_rate,
        "reason": gs.reason,
    })


def cmd_decide_retry(registry: Registry, args: argparse.Namespace) -> None:
    """Handle decide-retry: reset a stuck UoW for a new Steward cycle."""
    uow_id = args.id
    rows = registry.decide_retry(uow_id)
    retryable = ", ".join(sorted(registry.RETRYABLE_STATUSES))
    if rows == 1:
        _output({
            "status": "ok",
            "id": uow_id,
            "message": f"UoW `{uow_id}` reset for retry \u2192 ready-for-steward (steward_cycles reset to 0)",
        })
    else:
        _output({
            "status": "not_retryable",
            "id": uow_id,
            "message": (
                f"UoW `{uow_id}` could not be retried — "
                f"it is not in a retryable status ({retryable})"
            ),
        })


def cmd_decide_close(registry: Registry, args: argparse.Namespace) -> None:
    """Handle decide-close: close a stuck UoW as user-requested failure."""
    uow_id = args.id
    rows = registry.decide_close(uow_id)
    if rows == 1:
        _output({
            "status": "ok",
            "id": uow_id,
            "message": f"UoW `{uow_id}` closed — blocked \u2192 failed (reason: user_closed)",
        })
    else:
        _output({
            "status": "not_blocked",
            "id": uow_id,
            "message": f"UoW `{uow_id}` could not be closed — it is not currently in `blocked` status",
        })


def cmd_status_breakdown(registry: Registry, args: argparse.Namespace) -> None:
    """
    Return a count of UoWs grouped by status.

    Output: JSON object mapping each status present in the DB to its count.
    Example: {"proposed": 3, "active": 1, "done": 12}

    This is the canonical query that subagents should use instead of raw SQL
    GROUP BY queries — those have repeatedly caused syntax errors in practice.
    """
    _output(registry.get_status_counts())


def cmd_escalation_candidates(registry: Registry, args: argparse.Namespace) -> None:
    """
    Return UoWs that require human decision-making.

    Escalation candidates are UoWs in 'needs-human-review' status — the Steward
    has already exhausted its retry budget and escalated. These UoWs are waiting
    for Dan to either retry, close, or defer them.

    Output: JSON array of UoW objects (same structure as 'list' output).
    """
    records = registry.list(status="needs-human-review")
    _output([_uow_to_dict(r) for r in records])


def _read_trace_json(output_ref: str | None) -> dict | None:
    """
    Read and parse the trace.json file associated with output_ref.

    Uses the same path-derivation logic as executor/_read_trace_json:
    - Primary:  Path(output_ref).with_suffix(".trace.json")
    - Fallback: Path(str(output_ref) + ".trace.json")

    Returns None if output_ref is None, the file is absent, or the file
    cannot be parsed as JSON.
    """
    if not output_ref:
        return None
    p = Path(output_ref)
    trace_file = p.with_suffix(".trace.json") if p.suffix else Path(str(output_ref) + ".trace.json")
    if not trace_file.exists():
        alt = Path(str(output_ref) + ".trace.json")
        if alt != trace_file and alt.exists():
            trace_file = alt
        else:
            return None
    try:
        return json.loads(trace_file.read_text())
    except Exception:
        return None


# Orphan return_reason constants — used for kill-wave detection in _suggest_diagnosis.
# Exposed as module-level names so tests can import them rather than mirror raw literals.
_RETURN_REASON_EXECUTOR_ORPHAN = "executor_orphan"
_RETURN_REASON_EXECUTING_ORPHAN = "executing_orphan"
_RETURN_REASON_DIAGNOSING_ORPHAN = "diagnosing_orphan"
_RETURN_REASON_ORPHAN_KILL_BEFORE_START = "orphan_kill_before_start"
_RETURN_REASON_ORPHAN_KILL_DURING_EXECUTION = "orphan_kill_during_execution"

# Orphan return_reason values — infrastructure kills, not execution outcomes.
# Canonical source: _RETURN_REASON_CLASSIFICATIONS in steward.py (keys whose value is
# _CLASSIFICATION_ORPHAN). ORPHAN_REASONS in steward.py covers only the pre-heartbeat-
# classification subset; use _RETURN_REASON_CLASSIFICATIONS for the authoritative list.
# Kept here so registry_cli.py can classify without importing steward.
_ORPHAN_RETURN_REASONS = frozenset({
    _RETURN_REASON_EXECUTOR_ORPHAN,
    _RETURN_REASON_EXECUTING_ORPHAN,
    _RETURN_REASON_DIAGNOSING_ORPHAN,
    _RETURN_REASON_ORPHAN_KILL_BEFORE_START,    # heartbeat-classified (#963)
    _RETURN_REASON_ORPHAN_KILL_DURING_EXECUTION,  # heartbeat-classified (#963)
})


def _extract_return_reasons(audit_entries: list[dict]) -> list[dict]:
    """
    Extract return_reason payloads from audit_log entries, in chronological order.

    Each entry in the returned list has:
      ts, return_reason, event (the originating audit event)

    Only entries whose note field contains a JSON object with a "return_reason" key
    are included.
    """
    reasons = []
    for entry in audit_entries:  # already ASC order from fetch_audit_entries
        note = entry.get("note")
        if not note:
            continue
        try:
            note_data = json.loads(note)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(note_data, dict) and "return_reason" in note_data:
            reasons.append({
                "ts": entry.get("ts"),
                "event": entry.get("event"),
                "return_reason": note_data["return_reason"],
            })
    return reasons


def _extract_kill_classification(audit_entries: list[dict]) -> dict | None:
    """
    Find the most recent audit entry with event='orphan_kill_classified' and
    return its kill_type / heartbeats_before_kill payload.

    Returns None if no such entry exists.
    """
    for entry in reversed(audit_entries):
        if entry.get("event") != "orphan_kill_classified":
            continue
        note = entry.get("note")
        if not note:
            continue
        try:
            note_data = json.loads(note)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(note_data, dict) and "kill_type" in note_data:
            return {
                "kill_type": note_data.get("kill_type"),
                "heartbeats_before_kill": note_data.get("heartbeats_before_kill"),
                "ts": entry.get("ts"),
            }
    return None


def _suggest_diagnosis(uow: UoW, return_reasons: list[dict],
                       kill_classification: dict | None,
                       trace_json: dict | None) -> str:
    """
    Produce a short, actionable diagnosis hint based on the forensics data.

    Pattern recognition order (first match wins):
    1. infrastructure-kill-wave — all return reasons are orphan types
    2. kill-before-start — kill_type is orphan_kill_before_start or trace absent
    3. kill-during-execution — kill_type is orphan_kill_during_execution
    4. dead-prescription-loop — steward_cycles ≥ 3 with no execution_attempts
    5. retry-cap — needs-human-review with execution_attempts > 0
    6. steady / unknown — default
    """
    all_orphan = (
        bool(return_reasons)
        and all(r["return_reason"] in _ORPHAN_RETURN_REASONS for r in return_reasons)
    )

    if all_orphan and len(return_reasons) >= 2:
        return (
            "infrastructure-kill-wave: all return reasons are orphan classifications. "
            "No execution budget was consumed. "
            "Posture: reset with decide-retry; do not retire. "
            "Investigate session TTL or executor dispatch scheduling."
        )

    if kill_classification:
        kt = kill_classification.get("kill_type", "")
        hb = kill_classification.get("heartbeats_before_kill", 0) or 0
        if kt == "orphan_kill_before_start" or (not trace_json and kt):
            return (
                f"kill-before-start: agent was killed before writing any output "
                f"(heartbeats_before_kill={hb}). "
                "execution_attempts was not charged. "
                "Posture: reset with decide-retry."
            )
        if kt == "orphan_kill_during_execution":
            return (
                f"kill-during-execution: agent was killed after starting "
                f"(heartbeats_before_kill={hb}). "
                "execution_attempts was charged. "
                "Posture: reset with decide-retry; review trace_json for progress made."
            )

    if uow.status == "needs-human-review" and uow.execution_attempts == 0:
        return (
            "retry-cap-from-orphans: reached needs-human-review with zero confirmed "
            "execution attempts — all retries were infrastructure kills. "
            "Posture: reset with decide-retry after confirming executor dispatch is healthy."
        )

    if uow.status == "needs-human-review" and uow.execution_attempts > 0:
        return (
            f"retry-cap: reached needs-human-review with {uow.execution_attempts} confirmed "
            "execution attempt(s). "
            "Posture: review corrective_traces and steward_log for root cause, "
            "then decide-retry or decide-close."
        )

    if uow.steward_cycles >= 3 and uow.execution_attempts == 0:
        return (
            "dead-prescription-loop: steward has cycled ≥3 times without a confirmed "
            "execution attempt. "
            "Posture: check whether executor dispatch is enabled and throttle is clear."
        )

    if str(uow.status) in ("proposed", "pending"):
        return "early-stage: UoW has not yet entered the executor pipeline. No diagnosis needed."

    if str(uow.status) in ("done",):
        return "completed: UoW reached done status. No failure to diagnose."

    return (
        f"status={uow.status!s} steward_cycles={uow.steward_cycles} "
        f"execution_attempts={uow.execution_attempts} lifetime_cycles={uow.lifetime_cycles}. "
        "No known failure pattern matched. Review audit_log and corrective_traces for context."
    )


def cmd_trace(registry: Registry, args: argparse.Namespace) -> None:
    """
    Produce a unified forensics view for a single UoW.

    Output fields:
      uow_id             — the ID requested
      current_state      — status, execution_attempts, lifetime_cycles, steward_cycles,
                           retry_count, heartbeat_at, output_ref, close_reason, started_at
      audit_log          — all audit_log rows, oldest first
      corrective_traces  — all corrective_traces rows, oldest first
      return_reasons     — chronological list of {ts, event, return_reason} extracted
                           from audit_log note payloads
      kill_classification — {kill_type, heartbeats_before_kill, ts} from the most recent
                            orphan_kill_classified audit entry, or null
      trace_json         — parsed trace.json from output_ref path, or null
      diagnosis_hint     — one-paragraph triage summary with suggested posture
    """
    uow_id = args.id
    uow = registry.get(uow_id)
    if uow is None:
        _output({"error": "not found", "id": uow_id})
        return

    audit_entries = registry.fetch_audit_entries(uow_id)
    corrective_traces = registry.fetch_corrective_traces(uow_id)
    return_reasons = _extract_return_reasons(audit_entries)
    kill_classification = _extract_kill_classification(audit_entries)
    trace_json = _read_trace_json(uow.output_ref)
    diagnosis = _suggest_diagnosis(uow, return_reasons, kill_classification, trace_json)

    _output({
        "uow_id": uow_id,
        "current_state": {
            "status": str(uow.status),
            "execution_attempts": uow.execution_attempts,
            "lifetime_cycles": uow.lifetime_cycles,
            "steward_cycles": uow.steward_cycles,
            "retry_count": uow.retry_count,
            "heartbeat_at": uow.heartbeat_at,
            "output_ref": uow.output_ref,
            "close_reason": uow.close_reason,
            "started_at": uow.started_at,
        },
        "audit_log": audit_entries,
        "corrective_traces": corrective_traces,
        "return_reasons": return_reasons,
        "kill_classification": kill_classification,
        "trace_json": trace_json,
        "diagnosis_hint": diagnosis,
    })


def cmd_stale(registry: Registry, args: argparse.Namespace) -> None:
    """
    Return UoWs whose heartbeat has gone silent beyond their TTL.

    A UoW is stale when:
    - status is 'active' or 'executing' (in-flight)
    - heartbeat_at is not NULL (agent has written at least one heartbeat)
    - (now - heartbeat_at) > heartbeat_ttl + buffer_seconds

    UoWs with no heartbeat_at are NOT returned — those use the legacy
    started_at-based TTL path.

    Output: JSON array of UoW objects with stale heartbeats.
    """
    buffer_seconds = getattr(args, "buffer_seconds", 30)
    records = registry.get_stale_heartbeat_uows(buffer_seconds=buffer_seconds)
    _output([_uow_to_dict(r) for r in records])


# ---------------------------------------------------------------------------
# report command — time-windowed pipeline visibility
# ---------------------------------------------------------------------------

# Status buckets used for the summary header.
_COMPLETE_STATUSES = frozenset({"done"})
_FAILED_STATUSES = frozenset({"failed", "cancelled"})
_ESCALATED_STATUSES = frozenset({"needs-human-review"})
_EXECUTING_STATUSES = frozenset({"active", "executing", "ready-for-steward",
                                  "ready-for-executor", "diagnosing"})

# Valid metabolic taxonomy labels — mirrors VALID_OUTCOME_CATEGORIES in inbox_server.
# Defined here so registry_cli can classify without importing the MCP server.
_VALID_OUTCOME_CATEGORIES = frozenset({"heat", "shit", "seed", "pearl"})

# Default look-back window when neither --since nor --from is specified.
DEFAULT_REPORT_HOURS = 24


def _window_start_iso(since_hours: float | None, from_iso: str | None) -> str:
    """
    Resolve the window start timestamp to an ISO 8601 UTC string.

    Priority: explicit --from timestamp > --since hours > DEFAULT_REPORT_HOURS.
    """
    if from_iso is not None:
        # Accept both offset-aware and naive ISO strings; treat naive as UTC.
        try:
            dt = datetime.fromisoformat(from_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError as exc:
            raise ValueError(f"--from value is not a valid ISO 8601 timestamp: {from_iso!r}") from exc
    hours = since_hours if since_hours is not None else DEFAULT_REPORT_HOURS
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _classify_status(status: str) -> str:
    """Map a raw status string to a report bucket label.

    Bucket hierarchy (first match wins):
      complete    — done
      failed      — failed, cancelled
      escalated   — needs-human-review
      executing   — statuses in _EXECUTING_STATUSES (active, executing,
                    ready-for-steward, ready-for-executor, diagnosing)
      in-pipeline — everything else (proposed, pending, blocked, expired)

    The "in-pipeline" catch-all is intentionally distinct from "executing" so
    that proposed UoWs — which have not yet been claimed by the Steward — are
    not reported as currently running.
    """
    if status in _COMPLETE_STATUSES:
        return "complete"
    if status in _FAILED_STATUSES:
        return "failed"
    if status in _ESCALATED_STATUSES:
        return "escalated"
    if status in _EXECUTING_STATUSES:
        return "executing"
    return "in-pipeline"


def _compute_summary(rows: list[dict], window_start_iso: str, now: datetime) -> dict:
    """
    Derive aggregate statistics from a list of window-filtered UoW dicts.

    Returns a plain dict with:
      total, complete, failed, escalated, executing, in_pipeline,
      throughput_per_hour, median_wall_clock_seconds, total_token_usage,
      outcome_category_counts
    """
    total = len(rows)
    buckets: dict[str, int] = {
        "complete": 0, "failed": 0, "escalated": 0, "executing": 0, "in-pipeline": 0,
    }
    for row in rows:
        bucket = _classify_status(row.get("status") or "")
        buckets[bucket] = buckets.get(bucket, 0) + 1

    # Wall-clock for completed UoWs only (column computed in fetch_in_window).
    wall_clocks = [
        row["wall_clock_seconds"]
        for row in rows
        if row.get("wall_clock_seconds") is not None
        and _classify_status(row.get("status") or "") == "complete"
    ]
    median_wc = statistics.median(wall_clocks) if wall_clocks else None

    # Throughput: complete UoWs / hours in window.
    try:
        window_dt = datetime.fromisoformat(window_start_iso)
        if window_dt.tzinfo is None:
            window_dt = window_dt.replace(tzinfo=timezone.utc)
        elapsed_hours = (now - window_dt).total_seconds() / 3600.0
    except (ValueError, TypeError):
        elapsed_hours = None
    throughput = (buckets["complete"] / elapsed_hours) if elapsed_hours else None

    total_tokens = sum(
        row["token_usage"]
        for row in rows
        if row.get("token_usage") is not None
    )

    # outcome_category breakdown: count UoWs per label (heat/shit/seed/pearl).
    # Only non-NULL values are counted; NULL means the subagent did not report one.
    outcome_counts: dict[str, int] = {}
    for row in rows:
        cat = row.get("outcome_category")
        if cat in _VALID_OUTCOME_CATEGORIES:
            outcome_counts[cat] = outcome_counts.get(cat, 0) + 1

    return {
        "total": total,
        "complete": buckets["complete"],
        "failed": buckets["failed"],
        "escalated": buckets["escalated"],
        "executing": buckets["executing"],
        "in_pipeline": buckets["in-pipeline"],
        "throughput_per_hour": throughput,
        "median_wall_clock_seconds": median_wc,
        "total_token_usage": total_tokens,
        "outcome_category_counts": outcome_counts,
    }


def _kill_label(audit_entries: list[dict]) -> str | None:
    """
    Return the kill_type from the most recent orphan_kill_classified audit
    entry, or None if no such entry exists.
    """
    kc = _extract_kill_classification(audit_entries)
    return kc["kill_type"] if kc else None


def _format_report(rows: list[dict], summary: dict, window_start_iso: str,
                   registry: "Registry") -> str:
    """
    Render the report as plain aligned text — no markdown tables.

    Layout:
      Header block  (window, counts, throughput, median wall-clock, tokens,
                     outcome_category breakdown)
      Blank line
      Per-UoW lines (one per row, most recent first — already ordered by query)
    """
    lines: list[str] = []

    # --- Header ---
    lines.append("WOS Pipeline Report")
    lines.append(f"Window start : {window_start_iso}")
    lines.append(
        f"Total UoWs   : {summary['total']}"
        f"  (complete: {summary['complete']}"
        f"  failed: {summary['failed']}"
        f"  escalated: {summary['escalated']}"
        f"  executing: {summary['executing']}"
        f"  in-pipeline: {summary['in_pipeline']})"
    )
    tph = summary["throughput_per_hour"]
    lines.append(f"Throughput   : {tph:.2f} completions/hr" if tph is not None else "Throughput   : n/a")
    mwc = summary["median_wall_clock_seconds"]
    lines.append(f"Median wall  : {int(mwc)}s" if mwc is not None else "Median wall  : n/a")
    lines.append(f"Total tokens : {summary['total_token_usage']}")

    # Outcome category breakdown — only emit the line when at least one UoW has one.
    oc_counts = summary.get("outcome_category_counts") or {}
    if oc_counts:
        # Emit in canonical order: pearl seed heat shit (positive → negative)
        parts = [
            f"{label}: {oc_counts[label]}"
            for label in ("pearl", "seed", "heat", "shit")
            if label in oc_counts
        ]
        lines.append(f"Outcomes     : {'  '.join(parts)}")

    if not rows:
        lines.append("")
        lines.append("(no UoWs in window)")
        return "\n".join(lines)

    lines.append("")
    lines.append("Per-UoW listing (most recent first):")
    lines.append("")

    # Column header
    lines.append(
        f"{'UoW ID':<26}  {'Status':<22}  {'Category':<8}  {'Wall(s)':>7}  {'Tokens':>8}  {'Issue':>6}  Kill"
    )
    lines.append("-" * 100)

    for row in rows:
        uow_id = row.get("id") or ""
        status = row.get("status") or ""
        cat = row.get("outcome_category")
        cat_str = f"[{cat}]" if cat in _VALID_OUTCOME_CATEGORIES else ""
        wc = row.get("wall_clock_seconds")
        wc_str = str(int(wc)) if wc is not None else "-"
        tok = row.get("token_usage")
        tok_str = str(tok) if tok is not None else "-"
        issue = row.get("source_issue_number")
        issue_str = f"#{issue}" if issue is not None else "-"

        # Fetch kill label only for UoWs that look like they were killed.
        kill_str = "-"
        if status in ("failed", "needs-human-review", "ready-for-steward"):
            audit = registry.fetch_audit_entries(uow_id)
            lbl = _kill_label(audit)
            if lbl:
                kill_str = lbl

        lines.append(
            f"{uow_id:<26}  {status:<22}  {cat_str:<8}  {wc_str:>7}  {tok_str:>8}  {issue_str:>6}  {kill_str}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# failure-breakdown command — cross-UoW failure pattern aggregation
# ---------------------------------------------------------------------------

# Canonical kill_type labels for the failure-breakdown command.
# Infrastructure kills are UoWs where the agent was terminated by the platform
# before it could complete — no execution budget was consumed.
# Genuine failures are UoWs where execution began but the agent returned failure.
KILL_TYPE_ORPHAN_BEFORE_START = "orphan_kill_before_start"
KILL_TYPE_ORPHAN_DURING_EXECUTION = "orphan_kill_during_execution"
KILL_TYPE_HARD_CAP = "hard_cap"
KILL_TYPE_USER_CLOSED = "user_closed"
KILL_TYPE_EXECUTION_FAILED = "execution_failed"
KILL_TYPE_UNKNOWN = "unknown"


def _classify_failure_kill_type(audit_entries: list[dict], close_reason: str | None) -> str:
    """
    Derive a kill_type label for a single failed UoW.

    Classification priority (first match wins):
    1. orphan_kill_classified audit event — infrastructure kill (before or during execution)
    2. close_reason == 'hard_cap_cleanup' — genuine retry cap exhaustion
    3. decide_close audit event — user explicitly closed
    4. execution_failed audit event — executor returned failure
    5. unknown — no matching signal

    This function is a pure classifier: it reads signals and returns a label.
    It does not query the DB.
    """
    # Priority 1: heartbeat-classified infrastructure kill
    kill_classification = _extract_kill_classification(audit_entries)
    if kill_classification:
        kt = kill_classification.get("kill_type", "")
        if kt == "orphan_kill_before_start":
            return KILL_TYPE_ORPHAN_BEFORE_START
        if kt == "orphan_kill_during_execution":
            return KILL_TYPE_ORPHAN_DURING_EXECUTION

    # Priority 2: hard cap retry exhaustion
    if close_reason == "hard_cap_cleanup":
        return KILL_TYPE_HARD_CAP

    # Priority 3 & 4: scan audit event types
    event_types = {entry.get("event") for entry in audit_entries}
    if "decide_close" in event_types:
        return KILL_TYPE_USER_CLOSED
    if "execution_failed" in event_types:
        return KILL_TYPE_EXECUTION_FAILED

    return KILL_TYPE_UNKNOWN


def _fetch_failed_uows_since(registry: "Registry", since_iso: str | None) -> list[dict]:
    """
    Return all failed UoWs as dicts, optionally filtered to those created on or
    after since_iso. Each dict has: id, register, close_reason, created_at.
    """
    import sqlite3 as _sqlite3
    conn = registry._connect()
    try:
        if since_iso:
            cursor = conn.execute(
                "SELECT id, register, close_reason, created_at FROM uow_registry "
                "WHERE status = 'failed' AND created_at >= ? ORDER BY created_at",
                (since_iso,),
            )
        else:
            cursor = conn.execute(
                "SELECT id, register, close_reason, created_at FROM uow_registry "
                "WHERE status = 'failed' ORDER BY created_at",
            )
        rows = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
    return rows


def _format_failure_breakdown(
    by_kill_type: dict[str, int],
    by_register: dict[str, int],
    total: int,
    since_iso: str | None,
) -> str:
    """
    Render failure counts as plain aligned text — no markdown tables.

    Layout:
      Header (total, optional since filter)
      Blank line
      By kill type (aligned columns: label, count, percentage)
      Blank line
      By register (aligned columns: label, count, percentage)
    """
    lines: list[str] = []

    lines.append("WOS Failure Breakdown")
    if since_iso:
        lines.append(f"Since        : {since_iso}")
    lines.append(f"Total failed : {total}")

    if total == 0:
        lines.append("")
        lines.append("(no failed UoWs found)")
        return "\n".join(lines)

    def _pct(count: int) -> str:
        return f"{100 * count / total:.0f}%"

    col_w = 32  # label column width

    lines.append("")
    lines.append("By kill type:")
    lines.append(f"  {'Kill type':<{col_w}}  {'Count':>5}  {'Pct':>5}")
    lines.append(f"  {'-' * col_w}  {'-' * 5}  {'-' * 5}")
    for label in sorted(by_kill_type):
        count = by_kill_type[label]
        lines.append(f"  {label:<{col_w}}  {count:>5}  {_pct(count):>5}")

    lines.append("")
    lines.append("By register:")
    lines.append(f"  {'Register':<{col_w}}  {'Count':>5}  {'Pct':>5}")
    lines.append(f"  {'-' * col_w}  {'-' * 5}  {'-' * 5}")
    for label in sorted(by_register):
        count = by_register[label]
        lines.append(f"  {label:<{col_w}}  {count:>5}  {_pct(count):>5}")

    return "\n".join(lines)


def cmd_failure_breakdown(registry: "Registry", args: argparse.Namespace) -> None:
    """
    Aggregate failed UoWs by kill_type and register.

    Kill type is derived from audit_log and close_reason — no schema changes required.

    Classification priority per UoW:
      1. orphan_kill_classified audit event → infrastructure kill (before/during execution)
      2. close_reason == 'hard_cap_cleanup' → genuine retry cap exhaustion
      3. decide_close audit event → user explicitly closed
      4. execution_failed audit event → executor returned failure
      5. unknown

    Options:
      --since ISO_TIMESTAMP  Filter to UoWs created on or after this timestamp
    """
    since_iso: str | None = getattr(args, "since", None)

    failed_rows = _fetch_failed_uows_since(registry, since_iso)
    total = len(failed_rows)

    by_kill_type: dict[str, int] = {}
    by_register: dict[str, int] = {}

    for row in failed_rows:
        audit_entries = registry.fetch_audit_entries(row["id"])
        kill_type = _classify_failure_kill_type(audit_entries, row.get("close_reason"))
        by_kill_type[kill_type] = by_kill_type.get(kill_type, 0) + 1

        reg = row.get("register") or "operational"
        by_register[reg] = by_register.get(reg, 0) + 1

    report = _format_failure_breakdown(by_kill_type, by_register, total, since_iso)
    print(report)


def cmd_report(registry: "Registry", args: argparse.Namespace) -> None:
    """
    Print a time-windowed pipeline visibility report to stdout.

    Output is plain aligned text (no markdown tables). The report covers all
    UoWs whose started_at or completed_at falls within the requested window.
    Includes outcome_category (heat/shit/seed/pearl) in the summary breakdown
    and per-UoW listing when present.

    Options:
      --since HOURS   Look back N hours from now (default: 24)
      --from ISO_DATE Start window at an explicit ISO 8601 timestamp
    """
    since_hours: float | None = getattr(args, "since", None)
    from_iso: str | None = getattr(args, "from_iso", None)

    try:
        window_start = _window_start_iso(since_hours, from_iso)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    rows = registry.fetch_in_window(window_start)
    summary = _compute_summary(rows, window_start, now)
    report = _format_report(rows, summary, window_start, registry)
    print(report)


# ---------------------------------------------------------------------------
# queue-latency command — decompose wall-clock into queue wait vs execution time
# ---------------------------------------------------------------------------

# Default look-back for queue-latency when --since is not specified.
DEFAULT_LATENCY_HOURS = 24


def _parse_seconds_between(ts_start: str, ts_end: str) -> float | None:
    """
    Return the number of seconds between two ISO 8601 timestamp strings.

    Returns None if either timestamp is missing or cannot be parsed.
    The result is always ts_end - ts_start; a negative value means ts_end
    precedes ts_start (data anomaly — callers decide how to handle).
    """
    if not ts_start or not ts_end:
        return None
    try:
        dt_start = datetime.fromisoformat(ts_start)
        dt_end = datetime.fromisoformat(ts_end)
        if dt_start.tzinfo is None:
            dt_start = dt_start.replace(tzinfo=timezone.utc)
        if dt_end.tzinfo is None:
            dt_end = dt_end.replace(tzinfo=timezone.utc)
        return (dt_end - dt_start).total_seconds()
    except (ValueError, TypeError):
        return None


def _percentile(sorted_values: list[float], p: float) -> float:
    """
    Compute the p-th percentile of a sorted list using linear interpolation.

    p must be in [0, 100]. sorted_values must be non-empty and already sorted
    ascending. This is the same interpolation method used by numpy.percentile
    with the default 'linear' method.
    """
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = (p / 100.0) * (n - 1)
    lower = int(rank)
    upper = lower + 1
    if upper >= n:
        return sorted_values[-1]
    fraction = rank - lower
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


def _compute_latency_stats(rows: list[dict]) -> dict:
    """
    Compute queue_wait and execution_time latency statistics from a list of UoW dicts.

    For each row:
      queue_wait     = started_at - created_at   (time in queue before Executor claims)
      execution_time = completed_at - started_at  (time from claim to completion)

    A row contributes to queue_wait stats only if started_at is present.
    A row contributes to execution_time stats only if both started_at and
    completed_at are present.

    Returns:
      {
        "queue_wait":     {"count": N, "p50": ..., "p90": ..., "p99": ..., "mean": ...},
        "execution_time": {"count": N, "p50": ..., "p90": ..., "p99": ..., "mean": ...},
      }

    All stat values are None when count == 0.
    """
    queue_waits: list[float] = []
    exec_times: list[float] = []

    for row in rows:
        created_at = row.get("created_at")
        started_at = row.get("started_at")
        completed_at = row.get("completed_at")

        qw = _parse_seconds_between(created_at, started_at)
        if qw is not None and qw >= 0:
            queue_waits.append(qw)

        et = _parse_seconds_between(started_at, completed_at)
        if et is not None and et >= 0:
            exec_times.append(et)

    def _stats(values: list[float]) -> dict:
        if not values:
            return {"count": 0, "p50": None, "p90": None, "p99": None, "mean": None}
        s = sorted(values)
        return {
            "count": len(s),
            "p50": round(_percentile(s, 50)),
            "p90": round(_percentile(s, 90)),
            "p99": round(_percentile(s, 99)),
            "mean": round(sum(s) / len(s), 1),
        }

    return {
        "queue_wait": _stats(queue_waits),
        "execution_time": _stats(exec_times),
    }


def _format_latency_report(stats: dict, since_hours: float, status_filter: str | None) -> str:
    """
    Render queue-latency stats as plain aligned text — no markdown tables.

    Layout:
      Header (title, window, status filter if set)
      Blank line
      Queue wait section   (count, p50, p90, p99, mean)
      Blank line
      Execution time section (count, p50, p90, p99, mean)
    """

    def _fmt_seconds(v: float | int | None) -> str:
        if v is None:
            return "n/a"
        return f"{int(v)}s"

    def _fmt_mean(v: float | None) -> str:
        if v is None:
            return "n/a"
        return f"{v:.1f}s"

    lines: list[str] = []
    lines.append("Queue Latency Report")
    lines.append(f"Window       : last {since_hours:.0f}h")
    if status_filter:
        lines.append(f"Status filter: {status_filter}")
    lines.append("")

    for label, key in [("Queue wait (created → started)", "queue_wait"),
                        ("Execution time (started → completed)", "execution_time")]:
        s = stats[key]
        count = s["count"]
        lines.append(label)
        lines.append(f"  Count : {count}")
        if count == 0:
            lines.append("  (no data)")
        else:
            lines.append(f"  p50   : {_fmt_seconds(s['p50'])}")
            lines.append(f"  p90   : {_fmt_seconds(s['p90'])}")
            lines.append(f"  p99   : {_fmt_seconds(s['p99'])}")
            lines.append(f"  mean  : {_fmt_mean(s['mean'])}")
        lines.append("")

    return "\n".join(lines)


def cmd_queue_latency(registry: "Registry", args: argparse.Namespace) -> None:
    """
    Print a latency decomposition report: queue wait vs. execution time.

    queue_wait     = started_at - created_at   (how long UoWs sit before dispatch)
    execution_time = completed_at - started_at  (how long execution takes)

    Reports p50, p90, p99, and mean for each dimension.

    Options:
      --since HOURS   Look back N hours from now (default: 24)
      --status STATUS Filter to a single status value (e.g. 'done')
    """
    since_hours: float = getattr(args, "since", None) or DEFAULT_LATENCY_HOURS
    status_filter: str | None = getattr(args, "status", None)

    window_start = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    rows = registry.fetch_for_latency(window_start, status=status_filter)
    stats = _compute_latency_stats(rows)
    report = _format_latency_report(stats, since_hours=since_hours, status_filter=status_filter)
    print(report)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="UoW Registry CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # upsert
    p_upsert = subparsers.add_parser("upsert", help="Propose a UoW for a GitHub issue")
    p_upsert.add_argument("--issue", type=int, required=True, help="GitHub issue number")
    p_upsert.add_argument("--title", required=True, help="Issue title / UoW summary")
    p_upsert.add_argument("--sweep-date", dest="sweep_date", default=None,
                          help="Sweep date (YYYY-MM-DD). Defaults to today.")
    p_upsert.add_argument("--issue-body", dest="issue_body", default=None,
                          help="Full GitHub issue body text (used to extract success criteria).")

    # get
    p_get = subparsers.add_parser("get", help="Get a UoW by id")
    p_get.add_argument("--id", required=True, help="UoW id")

    # list
    p_list = subparsers.add_parser("list", help="List UoWs, optionally filtered by status")
    p_list.add_argument("--status", default=None,
                        choices=["proposed", "pending", "active", "blocked", "done", "failed", "expired"],
                        help="Filter by status")

    # approve
    p_approve = subparsers.add_parser("approve", help="Approve a proposed UoW (proposed → pending)")
    p_approve.add_argument("--id", required=True, help="UoW id")

    # check-stale
    subparsers.add_parser("check-stale", help="Report active UoWs whose source issue is closed")

    # expire-proposals
    subparsers.add_parser("expire-proposals", help="Expire proposed records older than 14 days")

    # gate-readiness
    subparsers.add_parser("gate-readiness", help="Check WOS autonomy gate metric")

    # decide-retry
    p_decide_retry = subparsers.add_parser(
        "decide-retry",
        help="Reset a stuck UoW for a new Steward cycle (blocked → ready-for-steward)",
    )
    p_decide_retry.add_argument("--id", required=True, help="UoW id")

    # decide-close
    p_decide_close = subparsers.add_parser(
        "decide-close",
        help="Close a stuck UoW as user-requested failure (blocked → failed)",
    )
    p_decide_close.add_argument("--id", required=True, help="UoW id")

    # status-breakdown
    subparsers.add_parser(
        "status-breakdown",
        help="Count UoWs grouped by status (returns JSON object: {status: count})",
    )

    # escalation-candidates
    subparsers.add_parser(
        "escalation-candidates",
        help="List UoWs in needs-human-review status awaiting operator decision",
    )

    # stale
    p_stale = subparsers.add_parser(
        "stale",
        help="List in-flight UoWs whose heartbeat has gone silent beyond their TTL",
    )
    p_stale.add_argument(
        "--buffer-seconds",
        dest="buffer_seconds",
        type=int,
        default=30,
        help="Grace period added to heartbeat_ttl before declaring a stall (default: 30)",
    )

    # trace
    p_trace = subparsers.add_parser(
        "trace",
        help=(
            "Unified forensics view for a single UoW: registry row, audit_log, "
            "corrective_traces, trace.json, return reasons, kill classification, "
            "and a diagnosis hint. Start here for any WOS failure."
        ),
    )
    p_trace.add_argument("--id", required=True, help="UoW id")

    # failure-breakdown
    p_failure_breakdown = subparsers.add_parser(
        "failure-breakdown",
        help=(
            "Aggregate failed UoWs by kill_type and register. "
            "Plain text output showing count + percentage per category. "
            "Kill types: orphan_kill_before_start, orphan_kill_during_execution, "
            "hard_cap, user_closed, execution_failed, unknown."
        ),
    )
    p_failure_breakdown.add_argument(
        "--since",
        dest="since",
        default=None,
        metavar="ISO_TIMESTAMP",
        help="Filter to UoWs created on or after this ISO 8601 timestamp",
    )

    # queue-latency
    p_queue_latency = subparsers.add_parser(
        "queue-latency",
        help=(
            "Decompose UoW wall-clock time into queue wait (created→started) and "
            "execution time (started→completed). Reports p50, p90, p99, and mean "
            "for each dimension. Plain text output."
        ),
    )
    p_queue_latency.add_argument(
        "--since",
        type=float,
        default=None,
        metavar="HOURS",
        help=f"Look back N hours from now (default: {DEFAULT_LATENCY_HOURS})",
    )
    p_queue_latency.add_argument(
        "--status",
        default=None,
        metavar="STATUS",
        help="Filter to UoWs with a specific status (e.g. 'done')",
    )

    # report
    p_report = subparsers.add_parser(
        "report",
        help=(
            "Time-windowed pipeline visibility report. "
            "Plain text output (not JSON). Shows aggregate stats, throughput, "
            "median wall-clock, token usage, outcome_category breakdown, "
            "and a per-UoW listing."
        ),
    )
    p_report.add_argument(
        "--since",
        type=float,
        default=None,
        metavar="HOURS",
        help=f"Look back N hours from now (default: {DEFAULT_REPORT_HOURS})",
    )
    p_report.add_argument(
        "--from",
        dest="from_iso",
        default=None,
        metavar="ISO_DATE",
        help="Start window at an explicit ISO 8601 timestamp (overrides --since)",
    )

    return parser


_COMMAND_MAP = {
    "upsert": cmd_upsert,
    "get": cmd_get,
    "list": cmd_list,
    "approve": cmd_approve,
    "check-stale": cmd_check_stale,
    "expire-proposals": cmd_expire_proposals,
    "gate-readiness": cmd_gate_readiness,
    "decide-retry": cmd_decide_retry,
    "decide-close": cmd_decide_close,
    "status-breakdown": cmd_status_breakdown,
    "escalation-candidates": cmd_escalation_candidates,
    "stale": cmd_stale,
    "trace": cmd_trace,
    "failure-breakdown": cmd_failure_breakdown,
    "queue-latency": cmd_queue_latency,
    "report": cmd_report,
}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    db_path = _get_db_path()
    registry = Registry(db_path)

    handler = _COMMAND_MAP.get(args.command)
    if handler is None:
        print(json.dumps({"error": f"unknown command: {args.command}"}))
        sys.exit(1)

    handler(registry, args)


if __name__ == "__main__":
    main()
