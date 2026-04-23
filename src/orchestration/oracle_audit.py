"""
Oracle audit event writer for the WOS spiral gate (issue #810).

Emits an oracle_approved audit event to the WOS registry audit_log when the
oracle agent issues an APPROVED verdict for a WOS-linked PR.

The spiral gate in steward.py reads oracle_approved entries to detect when a
UoW has cycled through oracle review >= SPIRAL_ORACLE_PASS_THRESHOLD times.
Without this write, _count_oracle_passes always returns 0 and the spiral gate
is structurally correct but permanently dormant.

## When to call

Call emit_oracle_approved from the oracle agent (lobster-oracle.md) immediately
after writing APPROVED to oracle/decisions.md, if a uow_id was provided in the
task prompt. Non-WOS oracle reviews (no uow_id) are silently skipped.

## Invocation from the oracle agent

    uv run ~/lobster/src/orchestration/oracle_audit.py \\
        --uow-id <uow_id> --pr-ref "<pr_ref>"

Or from Python:

    from orchestration.oracle_audit import emit_oracle_approved
    emit_oracle_approved(uow_id="wos_20260423_abc123", pr_ref="PR #864")

## Error handling

Errors are logged but never raised. A write failure must never block the oracle
agent's verdict delivery. The audit event is advisory — its absence leaves the
spiral gate dormant, which is the status quo ante.

## Idempotency

No idempotency guard is applied. If the oracle agent writes APPROVED twice for
the same UoW (unlikely but possible on retry), two oracle_approved entries are
written. The steward counts all entries, so a duplicate write increments the
spiral counter. This is acceptable — a genuine APPROVED verdict represents a
real oracle pass, and a duplicate is rarer than a missed write.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("oracle_audit")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_registry_db_path() -> Path:
    """Resolve the registry DB path using the same logic as wos_completion.py."""
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    workspace = Path(
        os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    )
    return workspace / "orchestration" / "registry.db"


def emit_oracle_approved(
    uow_id: str,
    pr_ref: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """
    Write an oracle_approved audit event to the WOS registry for the given UoW.

    This activates the spiral gate in steward._count_oracle_passes, which counts
    oracle_approved entries to detect when a UoW has cycled through oracle review
    too many times.

    Args:
        uow_id: The WOS unit-of-work ID (e.g. "wos_20260423_abc123").
        pr_ref: Human-readable PR reference (e.g. "PR #864"). Stored in the
                audit entry note for traceability. Optional.
        db_path: Override path to registry.db. If None, resolved from env.

    Returns:
        True if the event was written successfully, False otherwise.
        Errors are logged; the caller should not raise on False.
    """
    if not uow_id:
        log.debug("emit_oracle_approved: uow_id is empty — skipping")
        return False

    resolved_db_path = db_path or _default_registry_db_path()

    if not resolved_db_path.exists():
        log.debug(
            "emit_oracle_approved: registry DB not found at %s — "
            "skipping (no WOS install or test env)",
            resolved_db_path,
        )
        return False

    try:
        from orchestration.registry import Registry

        registry = Registry(resolved_db_path)

        # Verify the UoW exists before writing. append_audit_log does not check
        # existence — an audit entry for a nonexistent UoW is harmless (orphaned
        # row) but misleading. We check explicitly so the debug log is useful.
        uow = registry.get(uow_id)
        if uow is None:
            log.warning(
                "emit_oracle_approved: UoW %r not found in registry — "
                "oracle_approved event not written",
                uow_id,
            )
            return False

        entry: dict = {
            "event": "oracle_approved",
            "uow_id": uow_id,
            "ts": _now_iso(),
        }
        if pr_ref:
            entry["pr_ref"] = pr_ref

        registry.append_audit_log(uow_id, entry)
        log.info(
            "emit_oracle_approved: oracle_approved written for UoW %r (pr_ref=%r)",
            uow_id,
            pr_ref,
        )
        return True

    except Exception as exc:
        log.warning(
            "emit_oracle_approved: failed to write oracle_approved for UoW %r — %s: %s",
            uow_id,
            type(exc).__name__,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# CLI entry point — for invocation from the oracle agent via `uv run`
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    """
    CLI wrapper: emit an oracle_approved audit event.

    Usage:
        uv run ~/lobster/src/orchestration/oracle_audit.py \\
            --uow-id <uow_id> --pr-ref "<pr_ref>"

    Exit codes:
        0 — event written (or silently skipped because DB absent / uow_id empty)
        1 — write failed due to an exception (logged to stderr)
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    parser = argparse.ArgumentParser(
        description="Emit oracle_approved audit event for a WOS UoW",
    )
    parser.add_argument("--uow-id", required=True, help="WOS unit-of-work ID")
    parser.add_argument(
        "--pr-ref",
        default=None,
        help="Human-readable PR reference, e.g. 'PR #864'",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override path to registry.db (default: resolved from env)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db_path) if args.db_path else None
    success = emit_oracle_approved(
        uow_id=args.uow_id,
        pr_ref=args.pr_ref,
        db_path=db_path,
    )
    # Exit 0 even on False (DB absent / UoW not found) — these are not CLI errors.
    # Exit 1 only if emit_oracle_approved raised, which it does not (returns False).
    return 0 if success or True else 1  # always 0 — errors are logged, not fatal


if __name__ == "__main__":
    sys.exit(_main())
