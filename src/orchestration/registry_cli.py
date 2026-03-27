#!/usr/bin/env python3
"""
registry_cli.py — UoW Registry command-line interface.

All commands output JSON to stdout. All writes use BEGIN IMMEDIATE transactions
via the Registry class. Audit log entries are written atomically with state changes.

Usage:
    uv run registry_cli.py upsert --issue <N> --title <T> [--sweep-date <YYYY-MM-DD>]
    uv run registry_cli.py get --id <uow-id>
    uv run registry_cli.py list [--status <status>]
    uv run registry_cli.py confirm --id <uow-id>
    uv run registry_cli.py check-stale
    uv run registry_cli.py expire-proposals
    uv run registry_cli.py gate-readiness

Environment:
    REGISTRY_DB_PATH — override the default db path (~/.../orchestration/registry.db)
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Allow importing registry module whether run as script or via uv run
_SRC_DIR = Path(__file__).parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from orchestration.registry import Registry, _gh_issue_is_closed


def _get_db_path() -> Path:
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    return Path(workspace) / "orchestration" / "registry.db"


def _output(data: dict | list) -> None:
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_upsert(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.upsert(
        issue_number=args.issue,
        title=args.title,
        sweep_date=getattr(args, "sweep_date", None),
    )
    _output(result)


def cmd_get(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.get(args.id)
    _output(result)


def cmd_list(registry: Registry, args: argparse.Namespace) -> None:
    status = getattr(args, "status", None)
    result = registry.list(status=status)
    _output(result)


def cmd_confirm(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.confirm(args.id)
    _output(result)


def cmd_check_stale(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.check_stale(issue_checker=_gh_issue_is_closed)
    _output(result)


def cmd_expire_proposals(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.expire_proposals()
    _output(result)


def cmd_gate_readiness(registry: Registry, args: argparse.Namespace) -> None:
    result = registry.gate_readiness()
    _output(result)


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

    # get
    p_get = subparsers.add_parser("get", help="Get a UoW by id")
    p_get.add_argument("--id", required=True, help="UoW id")

    # list
    p_list = subparsers.add_parser("list", help="List UoWs, optionally filtered by status")
    p_list.add_argument("--status", default=None,
                        choices=["proposed", "pending", "active", "blocked", "done", "failed", "expired"],
                        help="Filter by status")

    # confirm
    p_confirm = subparsers.add_parser("confirm", help="Confirm a proposed UoW (proposed → pending)")
    p_confirm.add_argument("--id", required=True, help="UoW id")

    # check-stale
    subparsers.add_parser("check-stale", help="Report active UoWs whose source issue is closed")

    # expire-proposals
    subparsers.add_parser("expire-proposals", help="Expire proposed records older than 14 days")

    # gate-readiness
    subparsers.add_parser("gate-readiness", help="Check Phase 1 → Phase 2 autonomy gate metric")

    return parser


_COMMAND_MAP = {
    "upsert": cmd_upsert,
    "get": cmd_get,
    "list": cmd_list,
    "confirm": cmd_confirm,
    "check-stale": cmd_check_stale,
    "expire-proposals": cmd_expire_proposals,
    "gate-readiness": cmd_gate_readiness,
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
