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
