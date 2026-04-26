#!/usr/bin/env python3
"""
registry_cli.py — UoW Registry command-line interface.

All commands output JSON to stdout. All writes use BEGIN IMMEDIATE transactions
via the Registry class. Audit log entries are written atomically with state changes.

Usage:
    uv run registry_cli.py upsert --issue <N> --title <T> [--sweep-date <YYYY-MM-DD>]
    uv run registry_cli.py get --id <uow-id>
    uv run registry_cli.py list [--status <status>]
    uv run registry_cli.py approve --id <uow-id>
    uv run registry_cli.py check-stale
    uv run registry_cli.py expire-proposals
    uv run registry_cli.py gate-readiness
    uv run registry_cli.py trace --id <uow-id>

Environment:
    REGISTRY_DB_PATH — override the default db path (~/.../orchestration/registry.db)
"""

import argparse
import dataclasses
import json
import os
import sys
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


# Orphan return_reason values — infrastructure kills, not execution outcomes.
# Must match ORPHAN_REASONS in steward.py; kept here as a read-only constant
# so registry_cli.py can classify without importing steward.
_ORPHAN_RETURN_REASONS = frozenset({
    "executor_orphan",
    "executing_orphan",
    "diagnosing_orphan",
    "orphan_kill_before_start",
    "orphan_kill_during_execution",
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
