"""GardenCaretaker — unified scan and tend orchestration for the WOS registry.

Replaces the split responsibility of cultivator.py and issue-sweeper.py with a
single component that:
  1. Discovers new issues and proposes them as UoWs (scan)
  2. Reconciles active UoW bindings against current source state (tend)

GardenCaretaker has zero GitHub knowledge. All source interaction is mediated
through the IssueSource protocol — concrete implementations (GitHubIssueSource
or any in-memory stub) are injected at construction time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from .issue_source import IssueSnapshot, IssueSource
from .registry import Registry, UoW, UoWStatus, UpsertInserted

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default qualification config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    # Labels that immediately qualify a proposed UoW → ready
    "qualifying_labels": {"ready-to-execute", "high-priority", "bug"},
    # Labels that block qualification regardless of other criteria
    "blocking_labels": {"wip", "blocked", "needs-design", "needs-discussion"},
    # Labels filtered at scan time (meta labels — not real work items)
    "meta_labels": {"wos-phase-2", "tracking", "meta"},
    # If no qualifying label, qualify after this many days open (if no blocking label)
    "qualify_after_days_open": 3,
    # Issue must have a non-empty body to qualify
    "require_body": True,
}

# ---------------------------------------------------------------------------
# Surface-to-Steward audit classification constants
# ---------------------------------------------------------------------------

_CLASSIFICATION_SOURCE_CLOSED_WHILE_ACTIVE = "source_closed_while_active"
_CLASSIFICATION_SOURCE_DELETED_WHILE_ACTIVE = "source_deleted_while_active"

# States where GardenCaretaker actively manages lifecycle
_ACTIVE_STATES = {
    UoWStatus.PROPOSED,
    UoWStatus.PENDING,
    UoWStatus.READY_FOR_STEWARD,
    UoWStatus.READY_FOR_EXECUTOR,
    UoWStatus.ACTIVE,
    UoWStatus.DIAGNOSING,
    UoWStatus.BLOCKED,
}

# States where source closing/deletion triggers archive (no in-flight work)
_ARCHIVE_ON_CLOSE_STATES = {UoWStatus.PROPOSED, UoWStatus.PENDING}

# States where source closing/deletion surfaces to Steward (in-flight work)
_SURFACE_ON_CLOSE_STATES = {
    UoWStatus.READY_FOR_STEWARD,
    UoWStatus.READY_FOR_EXECUTOR,
    UoWStatus.ACTIVE,
    UoWStatus.DIAGNOSING,
    UoWStatus.BLOCKED,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


# ---------------------------------------------------------------------------
# Pure qualification logic — no side effects, fully testable
# ---------------------------------------------------------------------------

def _has_qualifying_label(
    labels: tuple[str, ...],
    qualifying: set[str],
) -> bool:
    """Return True if any label is in the qualifying set."""
    return bool(set(labels) & qualifying)


def _has_blocking_label(
    labels: tuple[str, ...],
    blocking: set[str],
) -> bool:
    """Return True if any label is in the blocking set."""
    return bool(set(labels) & blocking)


def _is_old_enough(created_at_iso: str, min_days: int) -> bool:
    """Return True if the issue was created more than min_days ago."""
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        return (_utcnow() - created) >= timedelta(days=min_days)
    except (ValueError, AttributeError):
        return False


def _qualifies(snapshot: IssueSnapshot, config: dict[str, Any]) -> bool:
    """Pure predicate: does this snapshot meet qualification criteria?

    A proposed UoW qualifies (→ ready) if:
    - The issue has a non-empty body (when require_body is set)
    - AND no blocking labels are present
    - AND: has at least one qualifying label, OR has been open ≥ qualify_after_days_open
    """
    qualifying_labels: set[str] = set(config.get("qualifying_labels", _DEFAULT_CONFIG["qualifying_labels"]))
    blocking_labels: set[str] = set(config.get("blocking_labels", _DEFAULT_CONFIG["blocking_labels"]))
    require_body: bool = config.get("require_body", _DEFAULT_CONFIG["require_body"])
    qualify_after_days: int = config.get("qualify_after_days_open", _DEFAULT_CONFIG["qualify_after_days_open"])

    if require_body and not snapshot.body.strip():
        return False

    if _has_blocking_label(snapshot.labels, blocking_labels):
        return False

    if _has_qualifying_label(snapshot.labels, qualifying_labels):
        return True

    return _is_old_enough(snapshot.created_at, qualify_after_days)


def _is_meta_issue(snapshot: IssueSnapshot, config: dict[str, Any]) -> bool:
    """Return True if the issue should be skipped at scan time (meta labels)."""
    meta_labels: set[str] = set(config.get("meta_labels", _DEFAULT_CONFIG["meta_labels"]))
    return _has_qualifying_label(snapshot.labels, meta_labels)


# ---------------------------------------------------------------------------
# GardenCaretaker
# ---------------------------------------------------------------------------

class GardenCaretaker:
    """Unified scan-and-tend loop for keeping the WOS registry in sync with source.

    Construction injects all dependencies so the class itself is fully testable
    without subprocess or DB side effects.
    """

    def __init__(
        self,
        source: IssueSource,
        registry: Registry,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.source = source
        self.registry = registry
        self.config: dict[str, Any] = {**_DEFAULT_CONFIG, **(config or {})}

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Main entry point. Returns merged summary of scan and tend actions."""
        seed_results = self.scan()
        tend_results = self.tend()
        return {**seed_results, **tend_results}

    def scan(self) -> dict[str, int]:
        """Discover new issues from source and seed UoWs.

        For each IssueSnapshot from source.scan():
          - Skip meta-labelled issues
          - Skip issues already in registry (non-terminal UoW exists)
          - Create UoW in 'proposed' state via registry.upsert()
          - If snapshot passes qualification criteria → transition to 'ready'

        Returns:
            {"seeded": int, "qualified": int}
        """
        seeded = 0
        qualified = 0

        for snapshot in self.source.scan():
            if _is_meta_issue(snapshot, self.config):
                logger.debug("scan: skipping meta issue %s", snapshot.source_ref)
                continue

            issue_number = self._extract_issue_number(snapshot.source_ref)
            if issue_number is None:
                logger.warning("scan: cannot extract issue number from source_ref=%s", snapshot.source_ref)
                continue

            result = self.registry.upsert(
                issue_number=issue_number,
                title=snapshot.title,
            )

            if not isinstance(result, UpsertInserted):
                logger.debug("scan: skipped issue %s — %s", issue_number, getattr(result, "reason", "already exists"))
                continue

            seeded += 1
            logger.info("scan: proposed UoW %s for issue %s", result.id, issue_number)

            if _qualifies(snapshot, self.config):
                rows = self.registry.transition(
                    uow_id=result.id,
                    to_status=UoWStatus.READY_FOR_STEWARD,
                    where_status=UoWStatus.PROPOSED,
                )
                if rows == 1:
                    self.registry.append_audit_log(result.id, {
                        "event": "qualified",
                        "actor": "garden_caretaker",
                        "from_status": "proposed",
                        "to_status": "ready-for-steward",
                        "source_ref": snapshot.source_ref,
                        "timestamp": _now_iso(),
                    })
                    qualified += 1
                    logger.info("scan: qualified UoW %s → ready-for-steward", result.id)

        return {"seeded": seeded, "qualified": qualified}

    def tend(self) -> dict[str, int]:
        """Reconcile active UoWs against current source state.

        For each UoW in registry with a non-terminal state:
          - Fetch current snapshot from source.get_issue(uow.source_ref)
          - Apply reconciliation rules (see design doc)

        Returns:
            {
                "archived": int,
                "surfaced_to_steward": int,
                "reactivated": int,
                "no_change": int,
            }
        """
        archived = 0
        surfaced = 0
        reactivated = 0
        no_change = 0

        active_uows = self._fetch_active_uows()

        for uow in active_uows:
            if not uow.source:
                logger.debug("tend: skipping UoW %s — no source_ref", uow.id)
                no_change += 1
                continue

            try:
                snapshot = self.source.get_issue(uow.source)
            except Exception as exc:
                logger.warning("tend: error fetching source for UoW %s: %s — no state change", uow.id, exc)
                no_change += 1
                continue

            action = _reconcile(uow.status, snapshot)

            if action == "no_op":
                no_change += 1

            elif action == "archive":
                archived += self._archive_uow(uow, snapshot)

            elif action == "surface":
                classification = (
                    _CLASSIFICATION_SOURCE_DELETED_WHILE_ACTIVE
                    if snapshot is None
                    else _CLASSIFICATION_SOURCE_CLOSED_WHILE_ACTIVE
                )
                surfaced += self._surface_to_steward(uow, snapshot, classification)

            elif action == "reactivate":
                reactivated += self._reactivate_uow(uow)

            elif action == "warn":
                logger.warning(
                    "tend: unknown/error source state for UoW %s (source=%s) — no state change",
                    uow.id, uow.source,
                )
                no_change += 1

            else:
                logger.error("tend: unrecognized reconciliation action '%s' for UoW %s", action, uow.id)
                no_change += 1

        return {
            "archived": archived,
            "surfaced_to_steward": surfaced,
            "reactivated": reactivated,
            "no_change": no_change,
        }

    # -----------------------------------------------------------------------
    # Private helpers — pure-ish operations that write to registry
    # -----------------------------------------------------------------------

    def _fetch_active_uows(self) -> list[UoW]:
        """Return all UoWs that tend() must inspect.

        Includes non-terminal states (proposed, pending, ready, active, etc.)
        AND terminal states (expired, failed) because source re-opening can
        reactivate them. Done UoWs are excluded — the design doc says
        source state changes to done UoWs are always no-ops.
        """
        uows: list[UoW] = []
        for status in _ACTIVE_STATES:
            uows.extend(self.registry.query(status=str(status)))
        # Terminal states that can be reactivated if source reopens
        uows.extend(self.registry.query(status=str(UoWStatus.FAILED)))
        uows.extend(self.registry.query(status=str(UoWStatus.EXPIRED)))
        # UoWStatus.DONE is intentionally excluded — the design doc specifies
        # that source state changes to done UoWs are always no-ops.
        return uows

    def _archive_uow(self, uow: UoW, snapshot: IssueSnapshot | None) -> int:
        """Transition UoW to expired status and write audit entry. Returns 1 if done."""
        reason = "source_closed_before_execution" if snapshot is not None else "source_deleted"
        rows = self.registry.transition(
            uow_id=uow.id,
            to_status=UoWStatus.EXPIRED,
            where_status=str(uow.status),
        )
        if rows == 1:
            self.registry.append_audit_log(uow.id, {
                "event": "archived_by_caretaker",
                "actor": "garden_caretaker",
                "classification": reason,
                "from_status": str(uow.status),
                "to_status": "expired",
                "source_ref": uow.source,
                "timestamp": _now_iso(),
            })
            logger.info("tend: archived UoW %s (was %s) — %s", uow.id, uow.status, reason)
            return 1
        return 0

    def _surface_to_steward(
        self,
        uow: UoW,
        snapshot: IssueSnapshot | None,
        classification: str,
    ) -> int:
        """Surface in-flight UoW to Steward via audit log escalation. Returns 1 if done."""
        # Surface to Steward: transition to ready-for-steward so the steward
        # heartbeat picks it up with full context. The classification in the
        # audit entry tells the Steward what happened.
        rows = self.registry.transition(
            uow_id=uow.id,
            to_status=UoWStatus.READY_FOR_STEWARD,
            where_status=str(uow.status),
        )
        if rows == 1:
            self.registry.append_audit_log(uow.id, {
                "event": "surfaced_to_steward",
                "actor": "garden_caretaker",
                "classification": classification,
                "from_status": str(uow.status),
                "to_status": "ready-for-steward",
                "source_ref": uow.source,
                "timestamp": _now_iso(),
            })
            logger.info(
                "tend: surfaced UoW %s to steward (was %s) — %s",
                uow.id, uow.status, classification,
            )
            return 1
        return 0

    def _reactivate_uow(self, uow: UoW) -> int:
        """Reactivate an archived/terminal UoW back to proposed. Returns 1 if done."""
        # Use set_status_direct — the UoW is in a terminal state and we need
        # to bypass the conditional transition guard.
        try:
            self.registry.set_status_direct(uow.id, UoWStatus.PROPOSED)
            self.registry.append_audit_log(uow.id, {
                "event": "reactivated_by_caretaker",
                "actor": "garden_caretaker",
                "classification": "source_reopened",
                "from_status": str(uow.status),
                "to_status": "proposed",
                "source_ref": uow.source,
                "timestamp": _now_iso(),
            })
            logger.info("tend: reactivated UoW %s → proposed (source reopened)", uow.id)
            return 1
        except Exception as exc:
            logger.warning("tend: failed to reactivate UoW %s: %s", uow.id, exc)
            return 0

    @staticmethod
    def _extract_issue_number(source_ref: str) -> int | None:
        """Extract integer issue number from source_ref string.

        Returns None if the source_ref does not encode a numeric entity_id.
        """
        try:
            # source_ref format: "github:issue/42"
            _, rest = source_ref.split(":", 1)
            _, entity_id = rest.split("/", 1)
            return int(entity_id)
        except (ValueError, AttributeError):
            return None


# ---------------------------------------------------------------------------
# Pure reconciliation decision function — no side effects
# ---------------------------------------------------------------------------

def _reconcile(uow_status: UoWStatus, snapshot: IssueSnapshot | None) -> str:
    """Map (uow_status, source_snapshot) → action string.

    Returns one of: "no_op" | "archive" | "surface" | "reactivate" | "warn"

    This is a pure function — it only reads its arguments and returns a string.
    All side effects live in GardenCaretaker's action methods.

    Reconciliation decision table (from design doc):

    | Source State      | proposed | ready | active | done  | expired/failed |
    |-------------------|----------|-------|--------|-------|----------------|
    | open              | no_op    | no_op | no_op  | no_op | reactivate     |
    | closed            | archive  | archive | surface | no_op | no_op        |
    | deleted/not_found | archive  | archive | surface | no_op | archive      |
    | unknown/error     | warn     | warn  | warn   | warn  | warn           |

    "reopened" (open + terminal UoW) → reactivate to proposed (only for expired/failed)
    """
    # source=None means deleted/not_found (issue was removed from source system)
    if snapshot is None:
        return _reconcile_deleted(uow_status)

    source_state = snapshot.state.lower()

    if source_state == "open":
        # "reopened" case: source is open but UoW is in terminal state → reactivate
        if uow_status in (UoWStatus.EXPIRED, UoWStatus.FAILED):
            return "reactivate"
        # open + any other state: no-op (system drives state forward normally)
        return "no_op"

    if source_state == "closed":
        return _reconcile_closed(uow_status)

    if source_state in ("deleted", "not_found", "transferred"):
        return _reconcile_deleted(uow_status)

    # unknown/error — log warning, no state change
    return "warn"


def _reconcile_closed(uow_status: UoWStatus) -> str:
    """Reconciliation when source issue is closed."""
    if uow_status in _ARCHIVE_ON_CLOSE_STATES:
        return "archive"
    if uow_status in _SURFACE_ON_CLOSE_STATES:
        return "surface"
    # done + closed → no-op (completed work stands regardless of source state)
    # expired/failed + closed → no-op (already disposed)
    return "no_op"


def _reconcile_deleted(uow_status: UoWStatus) -> str:
    """Reconciliation when source issue is deleted/not_found/transferred."""
    if uow_status in _ARCHIVE_ON_CLOSE_STATES:
        return "archive"
    if uow_status in _SURFACE_ON_CLOSE_STATES:
        return "surface"
    if uow_status == UoWStatus.DONE:
        # done + deleted → no-op (completed work stands)
        return "no_op"
    # expired/failed + deleted → archive (remove stale binding per design doc)
    return "archive"
