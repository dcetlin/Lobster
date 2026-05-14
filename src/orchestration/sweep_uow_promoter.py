"""
Sweep → UoW Promoter

After the negentropic sweep files a new GitHub issue, it calls this module
to create a proposed WOS Unit of Work sourced from that issue.

Design constraints:
- Pure function at the core: promote_sweep_issue() takes explicit dependencies.
- Dedup by issue_number: registry.upsert() already enforces cross-sweep-date
  dedup via its pre-write decision table.  No separate dedup layer needed here.
- Source tagged "sweep": distinguishes these UoWs from cultivator-promoted ones.
- Priority: low — sweep findings are background hygiene work, not urgent items.
- Job-enabled gate: reads jobs.json so the promotion can be disabled without
  touching code (consistent with all other Type B/C job gates).

This module is intentionally small.  The registry handles schema validation,
audit logging, and transaction safety.  The promoter's only job is to map
the sweep's issue data onto the registry API call with correct field values.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestration.registry import Registry

log = logging.getLogger("sweep-uow-promoter")

# ---------------------------------------------------------------------------
# Named constants matching the spec
# ---------------------------------------------------------------------------

# Source tag written to uow_registry.source for sweep-promoted UoWs.
# Distinguishes these from cultivator-promoted UoWs (which use "github:issue/N").
SWEEP_SOURCE = "sweep"

# Default success criterion for sweep-promoted UoWs.
# Written at promotion time so registry.upsert() never receives an empty value.
# The Steward refines this at germination if the issue body contains a richer
# acceptance criteria section.
_DEFAULT_SUCCESS_CRITERIA = (
    "The structural smell identified in the linked sweep escalation issue is "
    "remediated: the defect is fixed or the finding is documented as won't-fix "
    "with a rationale."
)

# Job name whose enabled flag gates sweep UoW promotion.
# Using the sweep job name ensures the gate is co-located with the sweep itself:
# disabling negentropic-sweep also disables UoW creation from sweep issues.
_GATE_JOB_NAME = "negentropic-sweep"


# ---------------------------------------------------------------------------
# Result type — named enum avoids bool/None ambiguity at call sites
# ---------------------------------------------------------------------------

class PromoteResult(Enum):
    CREATED = "created"
    SKIPPED_DEDUP = "skipped_dedup"
    SKIPPED_JOB_DISABLED = "skipped_job_disabled"


# ---------------------------------------------------------------------------
# Job-enabled gate — pure function, reads jobs.json
# ---------------------------------------------------------------------------

def _is_job_enabled(job_name: str, jobs_json_path: Path) -> bool:
    """
    Return True if the named job is enabled in jobs.json, False if explicitly
    disabled.  Defaults to True when the file is absent, the entry is missing,
    or the file is unreadable/malformed.

    Pure function of the file contents — no side effects.
    """
    try:
        data = json.loads(jobs_json_path.read_text())
        return bool(data.get("jobs", {}).get(job_name, {}).get("enabled", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Core promotion logic
# ---------------------------------------------------------------------------

def promote_sweep_issue(
    issue_number: int,
    title: str,
    issue_url: str,
    registry: "Registry",
    jobs_json_path: Path | None = None,
) -> PromoteResult:
    """
    Create a proposed WOS UoW for a GitHub issue filed by the negentropic sweep.

    Args:
        issue_number: GitHub issue number.
        title: Issue title — used as the UoW summary.
        issue_url: Canonical GitHub issue URL, e.g.
            "https://github.com/dcetlin/Lobster/issues/42".
        registry: Initialized Registry instance.  Injected for testability.
        jobs_json_path: Path to jobs.json.  Defaults to the canonical runtime
            path at ~/lobster-workspace/scheduled-jobs/jobs.json.

    Returns:
        PromoteResult.CREATED — a new proposed UoW was inserted.
        PromoteResult.SKIPPED_DEDUP — a non-terminal UoW already exists for
            this issue_number; no new record created.
        PromoteResult.SKIPPED_JOB_DISABLED — the negentropic-sweep job is
            disabled in jobs.json; promotion is suppressed.
    """
    # Resolve jobs.json path — defer to canonical default if not supplied.
    if jobs_json_path is None:
        workspace = Path.home() / "lobster-workspace"
        jobs_json_path = workspace / "scheduled-jobs" / "jobs.json"

    if not _is_job_enabled(_GATE_JOB_NAME, jobs_json_path):
        log.info(
            "sweep-uow-promote: job %r is disabled — skipping UoW creation for issue #%d",
            _GATE_JOB_NAME, issue_number,
        )
        return PromoteResult.SKIPPED_JOB_DISABLED

    from src.orchestration.registry import UpsertInserted, UpsertSkipped

    result = registry.upsert(
        issue_number=issue_number,
        title=title,
        success_criteria=_DEFAULT_SUCCESS_CRITERIA,
        issue_url=issue_url,
        source_ref=SWEEP_SOURCE,
    )

    if isinstance(result, UpsertInserted):
        log.info(
            "sweep-uow-promote: created UoW %s for sweep issue #%d (%s)",
            result.id, issue_number, issue_url,
        )
        return PromoteResult.CREATED
    elif isinstance(result, UpsertSkipped):
        log.info(
            "sweep-uow-promote: skipped issue #%d — %s",
            issue_number, result.reason,
        )
        return PromoteResult.SKIPPED_DEDUP
    else:
        # Future-proof: unknown result variant — treat as dedup to be safe.
        log.warning(
            "sweep-uow-promote: unexpected upsert result type %r for issue #%d",
            type(result).__name__, issue_number,
        )
        return PromoteResult.SKIPPED_DEDUP
