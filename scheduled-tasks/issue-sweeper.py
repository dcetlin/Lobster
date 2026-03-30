#!/usr/bin/env python3
"""
Issue Sweeper — UoW Registrar sweep script.

Scans the UoW registry for `pending` records and advances those whose trigger
condition is met to `ready-for-steward`.

Phase 2 dependency: this script requires the Phase 2 schema migration to have
been applied (scripts/migrate_add_steward_fields.py). The sweep calls
registry.transition() and registry.append_audit_log() which write Phase 2
fields. Do not run against a Phase 1-only database.

Design constraints:
- Only evaluates `pending` UoWs. UoWs in any other status are not touched.
- evaluate_condition(False) path: no state change, no audit entry, no log output.
- Optimistic lock on transition: if rows == 0 (another sweep won the race),
  skip silently — do not write audit entry, DEBUG log only.
- Does not crash the sweep when evaluate_condition raises (belt-and-suspenders:
  conditions.py is designed to not raise, but the sweep is defensive).

Run standalone:
    uv run ~/lobster/scheduled-tasks/issue-sweeper.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.registry import Registry
from src.orchestration.conditions import evaluate_condition

log = logging.getLogger("issue-sweeper")


# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_STATUS_PENDING = "pending"
_STATUS_READY_FOR_STEWARD = "ready-for-steward"
_ACTOR_REGISTRAR = "registrar"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_db_path() -> Path:
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    return workspace / "data" / "registry.db"


# ---------------------------------------------------------------------------
# Core sweep function — testable, injectable
# ---------------------------------------------------------------------------

def run_sweep(
    registry: Registry | None = None,
    github_client: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Sweep all `pending` UoWs and advance those whose trigger condition is met.

    Parameters
    ----------
    registry:
        Registry instance to use. If None, opens the production database at
        the path returned by _default_db_path().
    github_client:
        Optional callable(issue_number) → {"status_code": int, "state": str|None}.
        Passed through to evaluate_condition for issue_closed triggers.
        Defaults to the production gh CLI client inside evaluate_condition.

    Returns
    -------
    dict with keys:
        evaluated: int — number of pending UoWs evaluated
        advanced: int — number of UoWs transitioned to ready-for-steward
        skipped: int — number of UoWs where condition was False (no change)
        race_skipped: int — number of UoWs where transition returned 0 rows
    """
    if registry is None:
        registry = Registry(_default_db_path())

    pending_uows = registry.query(status=_STATUS_PENDING)
    log.debug("Sweep: %d pending UoWs found", len(pending_uows))

    evaluated = 0
    advanced = 0
    skipped = 0
    race_skipped = 0

    eval_kwargs: dict[str, Any] = {"registry": registry}
    if github_client is not None:
        eval_kwargs["github_client"] = github_client

    for uow in pending_uows:
        uow_id = uow.id

        try:
            condition_met = evaluate_condition(uow, **eval_kwargs)
        except Exception:
            log.exception("evaluate_condition raised unexpectedly for UoW %s — skipping", uow_id)
            skipped += 1
            evaluated += 1
            continue

        evaluated += 1

        if not condition_met:
            # Normal non-firing path — no state change, no audit entry, no log output.
            skipped += 1
            continue

        # Condition met — attempt optimistic-lock transition.
        rows = registry.transition(
            uow_id,
            to_status=_STATUS_READY_FOR_STEWARD,
            where_status=_STATUS_PENDING,
        )

        if rows == 1:
            # Transition succeeded — write trigger_fired audit entry.
            registry.append_audit_log(uow_id, {
                "event": "trigger_fired",
                "actor": _ACTOR_REGISTRAR,
                "uow_id": uow_id,
                "trigger": uow.trigger,
                "timestamp": _now_iso(),
            })
            advanced += 1
            log.info("UoW %s advanced to %s (trigger fired)", uow_id, _STATUS_READY_FOR_STEWARD)

        else:
            # rows == 0: another sweep already advanced this UoW — skip silently.
            race_skipped += 1
            log.debug("UoW %s: transition returned 0 rows (already advanced by another sweep)", uow_id)

    log.info(
        "Sweep complete: evaluated=%d advanced=%d skipped=%d race_skipped=%d",
        evaluated, advanced, skipped, race_skipped,
    )
    return {
        "evaluated": evaluated,
        "advanced": advanced,
        "skipped": skipped,
        "race_skipped": race_skipped,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = run_sweep()
    print(
        f"Issue sweeper done: evaluated={result['evaluated']} "
        f"advanced={result['advanced']} skipped={result['skipped']} "
        f"race_skipped={result['race_skipped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
