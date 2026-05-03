"""
UoW Registry — SQLite-backed store for Units of Work.

Design constraints enforced here:
- All writes use BEGIN IMMEDIATE transactions.
- Audit log entry is written in the same transaction as the registry change.
  If either fails, both roll back (Principle 1: no silent transitions).
- WAL mode is enabled on every connection for concurrent read safety.
- The UNIQUE(source_issue_number, sweep_date) constraint is the DB-level
  dedup gate; the pre-write decision table adds cross-sweep-date logic on top.
- INSERT OR REPLACE is never used — it would silently discard execution state.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("registry")


# ---------------------------------------------------------------------------
# Schema — loaded from schema.sql (the authoritative source of truth)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()

# Canonical route_reason written when no classifier is present (legacy default).
# All existing DB rows use this exact string. If you change it here, add a
# Migration to upgrade.sh to UPDATE existing rows to the new value.
_LEGACY_ROUTE_REASON = "phase1-default: no classifier"


# ---------------------------------------------------------------------------
# Status enum — logic lives on the type, not scattered at call sites
# ---------------------------------------------------------------------------

class UoWStatus(StrEnum):
    PROPOSED = "proposed"
    PENDING = "pending"
    READY_FOR_STEWARD = "ready-for-steward"
    READY_FOR_EXECUTOR = "ready-for-executor"
    ACTIVE = "active"
    # EXECUTING: inbox message written, subagent dispatched but write_result not yet received.
    # Transitions: active → executing (at inbox dispatch) → ready-for-steward (at write_result).
    # This intermediate state prevents false-complete UoWs: execution_complete is only written
    # when the subagent confirms completion via write_result (issue #669).
    EXECUTING = "executing"
    DIAGNOSING = "diagnosing"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"
    # CANCELLED: terminal status used when a UoW is explicitly cancelled.
    # Treated as terminal (allows re-proposal). Previously a legacy DB-only value
    # not covered by the enum — UoWStatus('cancelled') raised ValueError.
    CANCELLED = "cancelled"
    # NEEDS_HUMAN_REVIEW: UoW has exceeded MAX_RETRIES re-dispatch attempts.
    # Steward escalates to Dan rather than continuing to re-dispatch.
    # Treated as non-terminal (does not allow automatic re-proposal).
    NEEDS_HUMAN_REVIEW = "needs-human-review"

    def is_terminal(self) -> bool:
        """True for statuses that allow re-proposal (done, failed, expired, cancelled).

        Note: 'closed' is NOT included here. 'closed' is a legacy DB status that
        predates the UoWStatus enum — it was set externally on rows that existed
        before enum enforcement was introduced. Semantically it means "done", but
        it cannot be expressed as a UoWStatus value (UoWStatus('closed') raises
        ValueError). SQL queries that need to exclude terminal rows therefore handle
        'closed' separately via _TERMINAL_STATUSES_FOR_ISSUE_CHECK; this method
        covers only the enum-representable statuses.
        """
        return self in {UoWStatus.DONE, UoWStatus.FAILED, UoWStatus.EXPIRED, UoWStatus.CANCELLED}

    def is_in_flight(self) -> bool:
        """True for statuses that block re-proposal (active, executing, pending, ready-for-steward, ready-for-executor, diagnosing)."""
        return self in {
            UoWStatus.ACTIVE,
            UoWStatus.EXECUTING,
            UoWStatus.PENDING,
            UoWStatus.READY_FOR_STEWARD,
            UoWStatus.READY_FOR_EXECUTOR,
            UoWStatus.DIAGNOSING,
        }


# ---------------------------------------------------------------------------
# Named result types — no dict[str, Any] from decision functions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UpsertInserted:
    id: str


@dataclass(frozen=True)
class UpsertSkipped:
    id: str
    reason: str


UpsertResult = UpsertInserted | UpsertSkipped


@dataclass(frozen=True)
class ApproveConfirmed:
    id: str


@dataclass(frozen=True)
class ApproveSkipped:
    id: str
    reason: str
    current_status: str


@dataclass(frozen=True)
class ApproveNotFound:
    id: str


@dataclass(frozen=True)
class ApproveExpired:
    id: str


ApproveResult = ApproveConfirmed | ApproveSkipped | ApproveNotFound | ApproveExpired


@dataclass(frozen=True, slots=True)
class GateStatus:
    """Named return type for registry_health() — replaces dict[str, Any]."""
    gate_met: bool
    days_running: int
    approval_rate: float
    reason: str


# ---------------------------------------------------------------------------
# IssueChecker protocol — explicit structural interface for DI
# ---------------------------------------------------------------------------

class IssueChecker(Protocol):
    def __call__(self, issue_number: int) -> bool: ...


# ---------------------------------------------------------------------------
# UoW value object — typed, frozen
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UoW:
    """Typed value object for a Unit of Work row."""
    id: str
    status: UoWStatus
    summary: str
    source: str
    source_issue_number: int | None
    created_at: str
    updated_at: str
    sweep_date: str | None = None
    type: str = "executable"
    posture: str = "solo"
    route_reason: str | None = None
    steward_notes: str = ""
    success_criteria: str = ""
    output_ref: str | None = None
    # trigger: deserialized from JSON. dict for structured triggers, str if malformed JSON,
    # None for NULL rows. evaluate_condition handles all three cases.
    trigger: dict | str | None = None
    # Steward/Executor fields (populated after schema migration)
    workflow_artifact: str | None = None
    prescribed_skills: list | None = None
    steward_cycles: int = 0
    # lifetime_cycles: cumulative steward_cycles across all decide-retry resets.
    # Never reset. Used for the hard-cap circuit-breaker check.
    lifetime_cycles: int = 0
    timeout_at: str | None = None
    estimated_runtime: str | None = None
    steward_agenda: str | None = None
    steward_log: str | None = None
    proposed_at: str | None = None
    # started_at: ISO timestamp when the Executor claimed the UoW and transitioned
    #   to 'active' status. Used by startup sweep for per-UoW age threshold when
    #   combined with estimated_runtime (#572). NULL until execution begins.
    started_at: str | None = None
    # GardenCaretaker source tracking fields (populated after migration 0004)
    source_ref: str | None = None
    source_last_seen_at: str | None = None
    source_state: str | None = None
    # Canonical GitHub issue URL (populated after migration 0005)
    # Eliminates hardcoded repo references in Steward and Executor.
    issue_url: str | None = None
    # V3 register fields (populated after migration 0007)
    # register: attentional configuration required for completion evaluation.
    #   Values: operational | iterative-convergent | philosophical | human-judgment
    #   Immutable after germination — written at INSERT time by the Germinator.
    #   If the Steward detects a mismatch on diagnosis, it surfaces to Dan rather
    #   than reclassifying autonomously.
    # uow_mode: mirrors register; used for execution context selection by Executor.
    #   Kept separate to allow future divergence without a schema change.
    register: str = "operational"
    uow_mode: str | None = None
    # Delivery≠closure fields (populated after migration 0007)
    # closed_at: ISO timestamp written by Steward when it declares the loop done.
    #   Distinct from completed_at (Executor delivery). NULL until Steward closes.
    # close_reason: prose explaining the Steward's closure decision.
    #   Required at done transition. Enables post-hoc audit of closure rationale.
    closed_at: str | None = None
    close_reason: str | None = None
    # vision_ref: JSON {layer, field, statement, anchored_at} populated by the
    # issue-sweeper when creating UoWs. Consumed by vision_routing.resolve_vision_route()
    # to produce vision-anchored route_reason values.
    # NULL = created before Vision Object existed or no vision anchor found.
    vision_ref: dict | None = None
    # Heartbeat locking fields (populated after migration 0009)
    # heartbeat_at: ISO timestamp updated periodically by the executing agent to prove
    #   liveness. NULL until first heartbeat write. The observation loop uses this
    #   (when non-NULL) instead of started_at for staleness detection.
    # heartbeat_ttl: Maximum seconds of silence before the steward treats the UoW as
    #   stalled. Set at claim time from estimated_runtime; default 300 (5 minutes).
    heartbeat_at: str | None = None
    heartbeat_ttl: int = 300
    # retry_count: diagnostic counter — total Steward re-entry cycles (populated after migration 0010).
    #   Incremented on every re-entry regardless of return_reason.
    #   Used in escalation notifications for visibility. NOT the retry budget gate.
    retry_count: int = 0
    # execution_attempts: confirmed execution attempts — retry budget counter (migration 0014).
    #   Incremented only when return_reason is NOT an orphan classification
    #   (executor_orphan, executing_orphan, diagnosing_orphan). MAX_RETRIES gates on this.
    execution_attempts: int = 0
    # artifacts: typed outcome refs extracted from write_result payload (migration 0011).
    #   JSON array of {type, ref, category, description?} objects. NULL until populated.
    #   Populated by wos_completion.py after successful UoW completion.
    #   Types: "pr", "issue", "file", "commit".
    artifacts: list | None = None
    # Shard-stream fields (populated after migration 0012).
    #
    # file_scope: JSON array of file/directory paths the UoW is expected to touch.
    #   NULL = unknown scope. Unknown scope is treated as "exclusive": the UoW is only
    #   dispatched when zero other UoWs are executing (preserves prior serial behavior).
    #   Example: '["src/orchestration/steward.py", "tests/unit/test_orchestration/"]'
    #   Populated at UoW creation time by the cultivator or manually.
    file_scope: list | None = None
    # shard_id: named dispatch stream. UoWs in the same shard run serially (only one
    #   active per shard at a time). UoWs in different shards can run in parallel
    #   subject to file_scope overlap checks. NULL = no shard constraint.
    #   Example: 'wos-core', 'docs', 'tests'
    shard_id: str | None = None
    # Juice dispatch priority fields (populated after migration 0013).
    #
    # juice_quality: 'juice' when the steward asserts live generative momentum;
    #   NULL when not asserted. Re-evaluated on every prescription cycle (Option C
    #   from the spec: juice must be re-earned each cycle, not persisted automatically).
    #   The dispatch query sorts juice='juice' UoWs first among ready-for-steward
    #   candidates (juice wins LIFO tiebreaker).
    # juice_rationale: mandatory prose when juice_quality='juice'. Records *what*
    #   is alive and why (the "What is the juice?" calibration from the frontier doc).
    #   NULL when juice_quality is NULL.
    juice_quality: str | None = None
    juice_rationale: str | None = None
    # prescription_confidence: steward's confidence in the prescription at dispatch time.
    #   Float 0.0–1.0. NULL when not yet written (UoWs prescribed before migration 0016,
    #   or first-cycle prescriptions before the field is populated).
    #   Computed deterministically by _compute_prescription_confidence() in steward.py.
    #   Steward-private (excluded from executor_uow_view). Data collection only.
    prescription_confidence: float | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_uow_id() -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    random_part = uuid.uuid4().hex[:6]
    return f"uow_{date_part}_{random_part}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class Registry:
    """
    All public methods are pure write-then-read operations that keep the
    connection open only for the duration of the operation.

    The `db_path` is the only mutable state. Every method opens a fresh
    connection, executes within a BEGIN IMMEDIATE transaction, and closes.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        # Centralise the canonical path resolution here so callers that pass
        # no argument always reach the live registry.
        #
        # Resolution order (first match wins):
        # 1. Explicit db_path argument from caller.
        # 2. REGISTRY_DB_PATH env var (overrides canonical default for tests/CI).
        # 3. Canonical default: ~/lobster-workspace/orchestration/registry.db
        #
        # The env var is re-read at call time (not at module import time) so that
        # tests can set it via monkeypatch before constructing a Registry instance
        # without needing to reload the paths module.
        if db_path is None:
            import os
            env_override = os.environ.get("REGISTRY_DB_PATH")
            if env_override:
                db_path = Path(env_override)
            else:
                from src.orchestration.paths import REGISTRY_DB  # local to avoid circular imports
                db_path = REGISTRY_DB
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._run_migrations()

    # -----------------------------------------------------------------------
    # Migration bootstrap — called once at Registry init
    # -----------------------------------------------------------------------

    def _run_migrations(self) -> None:
        """Apply all pending numbered migrations via migrate.run_migrations().

        This replaces the old _init_schema() / executescript(schema.sql) path.
        The initial schema is now migration 0001, so new and existing DBs are
        both handled by the migration runner.
        """
        from src.orchestration.migrate import run_migrations  # local import avoids circular
        run_migrations(self.db_path)

    # -----------------------------------------------------------------------
    # Public audit interface
    # -----------------------------------------------------------------------

    def fetch_audit_entries(self, uow_id: str) -> list[dict]:
        """
        Return all audit log entries for the given UoW, ordered by id ASC.

        Returns an empty list if the UoW has no audit history or is not found.
        Each entry is a plain dict with at minimum 'event' and 'to_status' keys.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
                (uow_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        # Deserialize JSON-stored fields.
        # prescribed_skills follows the same pattern as hooks_applied:
        #   NULL stored value → None (not [])
        #   '[]' stored value → [] (not None) — distinct semantics
        #   '["skill-a"]' stored value → ["skill-a"]
        # steward_agenda and steward_log are Steward-private and are returned
        # as raw strings (or None) — they are NOT deserialized here.
        for field in ("children", "hooks_applied", "route_evidence", "trigger",
                      "vision_ref", "prescribed_skills"):
            raw = d.get(field)
            if raw is not None and isinstance(raw, str):
                try:
                    d[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def _row_to_uow(self, row: sqlite3.Row) -> UoW:
        """Convert a sqlite3.Row to a typed UoW value object."""
        d = dict(row)

        def _deserialize_json(raw: Any) -> Any:
            if raw is not None and isinstance(raw, str):
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    pass
            return raw

        # trigger: deserialize from JSON string → dict, or keep None
        trigger_raw = d.get("trigger")
        trigger: dict | None = None
        if trigger_raw is not None:
            parsed = _deserialize_json(trigger_raw)
            if isinstance(parsed, dict):
                trigger = parsed

        # prescribed_skills: NULL → None, '[]' → [], '["a"]' → ["a"]
        prescribed_skills_raw = d.get("prescribed_skills")
        prescribed_skills: list | None = None
        if prescribed_skills_raw is not None:
            parsed_ps = _deserialize_json(prescribed_skills_raw)
            if isinstance(parsed_ps, list):
                prescribed_skills = parsed_ps

        # vision_ref: NULL → None, '{"layer":...}' → dict
        vision_ref_raw = d.get("vision_ref")
        vision_ref: dict | None = None
        if vision_ref_raw is not None:
            parsed_vr = _deserialize_json(vision_ref_raw)
            if isinstance(parsed_vr, dict):
                vision_ref = parsed_vr

        # vision_ref: NULL → None, '{"layer":...}' → dict
        vision_ref_raw = d.get("vision_ref")
        vision_ref: dict | None = None
        if vision_ref_raw is not None:
            parsed_vr = _deserialize_json(vision_ref_raw)
            if isinstance(parsed_vr, dict):
                vision_ref = parsed_vr

        return UoW(
            id=d["id"],
            status=UoWStatus(d["status"]),
            summary=d.get("summary") or "",
            source=d.get("source") or "",
            source_issue_number=d.get("source_issue_number"),
            created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "",
            sweep_date=d.get("sweep_date"),
            type=d.get("type") or "executable",
            posture=d.get("posture") or "solo",
            route_reason=d.get("route_reason"),
            steward_notes=d.get("steward_notes") or "",
            success_criteria=d.get("success_criteria") or "",
            output_ref=d.get("output_ref"),
            trigger=trigger,
            workflow_artifact=d.get("workflow_artifact"),
            prescribed_skills=prescribed_skills,
            steward_cycles=d.get("steward_cycles") or 0,
            lifetime_cycles=d.get("lifetime_cycles") or 0,
            timeout_at=d.get("timeout_at"),
            estimated_runtime=d.get("estimated_runtime"),
            steward_agenda=d.get("steward_agenda"),
            steward_log=d.get("steward_log"),
            started_at=d.get("started_at"),
            source_ref=d.get("source_ref"),
            source_last_seen_at=d.get("source_last_seen_at"),
            source_state=d.get("source_state"),
            issue_url=d.get("issue_url"),
            register=d.get("register") or "operational",
            uow_mode=d.get("uow_mode"),
            closed_at=d.get("closed_at"),
            close_reason=d.get("close_reason"),
            vision_ref=vision_ref,
            heartbeat_at=d.get("heartbeat_at"),
            heartbeat_ttl=d.get("heartbeat_ttl") or 300,
            retry_count=d.get("retry_count") or 0,
            execution_attempts=d.get("execution_attempts") or 0,
            artifacts=_deserialize_json(d.get("artifacts")) if d.get("artifacts") else None,
            file_scope=_deserialize_json(d.get("file_scope")) if d.get("file_scope") else None,
            shard_id=d.get("shard_id"),
            juice_quality=d.get("juice_quality"),
            juice_rationale=d.get("juice_rationale"),
            prescription_confidence=d.get("prescription_confidence"),
        )

    def _write_audit(
        self,
        conn: sqlite3.Connection,
        uow_id: str,
        event: str,
        from_status: str | None = None,
        to_status: str | None = None,
        agent: str | None = None,
        note: str | None = None,
    ) -> None:
        """Write a single audit log entry. Must be called inside an active transaction."""
        conn.execute(
            """
            INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_now_iso(), uow_id, event, from_status, to_status, agent, note),
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def upsert(
        self,
        issue_number: int,
        title: str,
        sweep_date: str | None = None,
        uow_type: str = "executable",
        success_criteria: str = "",
        source_repo: str | None = None,
        issue_url: str | None = None,
        register: str = "operational",
        source_ref: str | None = None,
        file_scope: list[str] | None = None,
    ) -> UpsertResult:
        """
        Propose a UoW for a GitHub issue.

        Returns a typed UpsertResult:
        - UpsertInserted: new proposed UoW was created
        - UpsertSkipped: existing non-terminal record prevents creation

        Decision table (evaluated before any write):
        - No existing non-terminal record → INSERT new proposed record
        - Existing proposed (any sweep_date) → SKIP
        - Existing pending/active/blocked (any sweep_date) → SKIP
        - Existing done/failed/expired (any sweep_date) → INSERT new proposed record
        - UNIQUE(issue, sweep_date) conflict + existing is proposed → UPDATE fields
        - UNIQUE(issue, sweep_date) conflict + existing is non-proposed → no-op update (fields unchanged)

        Args:
            issue_number: GitHub issue number.
            title: Issue title used as the UoW summary.
            sweep_date: ISO date string; defaults to today (UTC).
            uow_type: UoW type tag (default "executable").
            success_criteria: Prose completion statement. Must not be an empty string;
                raises ValueError if blank so that germination always has a verifiable goal.
            source_repo: GitHub repo slug, e.g. "owner/repo". Used to derive issue_url
                when issue_url is not supplied explicitly.
            issue_url: Canonical GitHub issue URL. If omitted and source_repo is provided,
                derived as "https://github.com/{source_repo}/issues/{issue_number}".
            register: Attentional configuration required for completion evaluation.
                Classified by the Germinator at germination time and written here.
                Values: operational | iterative-convergent | philosophical | human-judgment.
                Immutable after germination — the Steward surfaces mismatch to Dan rather
                than reclassifying autonomously.
                Default: 'operational' (safe fallback for callers that do not invoke
                the Germinator).
            source_ref: Canonical source reference (e.g. "github:issue/42"). If provided,
                used directly; otherwise derived from issue_number for backwards compatibility.
            file_scope: List of file/directory paths touched by this issue (extracted from
                issue body). Used by shard_dispatch to detect overlap between concurrent UoWs.
                None means the UoW is treated as independent (serial execution is safe default).
        """
        if not success_criteria or not success_criteria.strip():
            raise ValueError(
                f"success_criteria must not be empty for issue #{issue_number}. "
                "Provide a prose completion statement that describes what 'done' means."
            )
        return self._upsert_typed(
            issue_number, title, sweep_date, uow_type, success_criteria,
            source_repo=source_repo, issue_url=issue_url,
            register=register, source_ref=source_ref,
            file_scope=file_scope,
        )

    def _upsert_typed(
        self,
        issue_number: int,
        title: str,
        sweep_date: str | None = None,
        uow_type: str = "executable",
        success_criteria: str = "",
        source_repo: str | None = None,
        issue_url: str | None = None,
        register: str = "operational",
        source_ref: str | None = None,
        file_scope: list[str] | None = None,
    ) -> UpsertResult:
        """Core upsert logic returning typed UpsertResult."""
        if sweep_date is None:
            sweep_date = datetime.now(timezone.utc).date().isoformat()

        # Derive issue_url from source_repo when not explicitly provided.
        # Pure computation: no side effects, deterministic given inputs.
        resolved_issue_url: str | None = issue_url
        if resolved_issue_url is None and source_repo:
            resolved_issue_url = f"https://github.com/{source_repo}/issues/{issue_number}"

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Cross-sweep-date pre-check: any non-terminal record for this issue?
            # Uses _TERMINAL_STATUSES_FOR_ISSUE_CHECK — single source of truth
            # shared with has_active_uow_for_issue() — so both dedup paths
            # stay in sync when terminal statuses are added or removed.
            _terminal = self._TERMINAL_STATUSES_FOR_ISSUE_CHECK
            _placeholders = ", ".join("?" * len(_terminal))
            existing = conn.execute(
                f"""
                SELECT id, status FROM uow_registry
                WHERE source_issue_number = ?
                  AND status NOT IN ({_placeholders})
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (issue_number, *_terminal),
            ).fetchone()

            if existing:
                skip_reason = f"existing record {existing['id']} is in '{existing['status']}' status"
                self._write_audit(
                    conn,
                    uow_id=existing["id"],
                    event="skipped",
                    note=f"upsert skipped: {skip_reason}",
                )
                conn.commit()
                return UpsertSkipped(id=existing["id"], reason=skip_reason)

            # Check for same-date UNIQUE conflict (terminal record from a prior run today).
            # This is rare but possible: same issue went terminal and is being re-swept on
            # the same calendar date. In this case we skip to avoid a phantom audit entry.
            same_date_row = conn.execute(
                """
                SELECT id, status FROM uow_registry
                WHERE source_issue_number = ? AND sweep_date = ?
                """,
                (issue_number, sweep_date),
            ).fetchone()

            if same_date_row:
                # Terminal row exists for this exact (issue, sweep_date) — skip for today.
                # The next sweep on a new date will create a fresh record.
                skip_reason = (
                    f"terminal record {same_date_row['id']} (status={same_date_row['status']}) "
                    f"already exists for sweep_date={sweep_date}; will re-propose on next sweep date"
                )
                self._write_audit(
                    conn,
                    uow_id=same_date_row["id"],
                    event="skipped",
                    note=f"upsert skipped: {skip_reason}",
                )
                conn.commit()
                return UpsertSkipped(id=same_date_row["id"], reason=skip_reason)

            uow_id = _generate_uow_id()
            now = _now_iso()
            # Use source_ref from IssueSnapshot if provided; fall back for backwards compatibility.
            source = source_ref if source_ref else f"github:issue/{issue_number}"

            # Classify posture and route_reason via routing_classifier.
            # Runs the first-match-wins rules from classifier.yaml against the
            # prescription metadata available at germination time (type, and any
            # other fields callers choose to provide in the future).
            # Falls back to solo/classifier-unavailable if the YAML is absent.
            try:
                from orchestration.routing_classifier import classify_posture
                classifier_result = classify_posture({"type": uow_type})
                germination_posture = classifier_result.posture
                germination_route_reason = classifier_result.route_reason
            except Exception as _classifier_exc:
                log.warning(
                    "routing_classifier failed for UoW (issue #%s) — using legacy defaults: %s",
                    issue_number, _classifier_exc,
                )
                germination_posture = "solo"
                germination_route_reason = _LEGACY_ROUTE_REASON

            # Audit entry is written BEFORE the INSERT (Principle 1: audit first).
            # If the INSERT fails, both roll back together.
            self._write_audit(conn, uow_id=uow_id, event="created", to_status="proposed")

            file_scope_json = json.dumps(file_scope) if file_scope is not None else None

            # register is classified by the Germinator before this call.
            # uow_mode mirrors register at INSERT time — kept separate to allow
            # future divergence between routing register and execution mode.
            conn.execute(
                """
                INSERT INTO uow_registry (
                    id, type, source, source_issue_number, sweep_date,
                    status, posture, created_at, updated_at, summary,
                    success_criteria, route_reason, route_evidence, trigger,
                    issue_url, register, uow_mode, source_ref, file_scope
                ) VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, '{}', '{"type": "immediate"}', ?, ?, ?, ?, ?)
                """,
                (
                    uow_id,
                    uow_type,
                    source,
                    issue_number,
                    sweep_date,
                    germination_posture,
                    now,
                    now,
                    title,
                    success_criteria,
                    germination_route_reason,
                    resolved_issue_url,
                    register,
                    register,  # uow_mode mirrors register at germination time
                    source_ref,
                    file_scope_json,
                ),
            )
            conn.commit()
            return UpsertInserted(id=uow_id)

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def approve(self, uow_id: str) -> ApproveResult:
        """
        Transition a UoW from proposed → ready-for-steward (atomically, via pending).

        The pending status is no longer a resting state: on /approve, the UoW
        transitions proposed → pending → ready-for-steward in a single transaction.
        Both audit entries are written in the same transaction so the full history
        is preserved without leaving the UoW stranded in pending awaiting the
        6am GardenCaretaker run.

        Returns a typed ApproveResult:
        - ApproveConfirmed: transition succeeded
        - ApproveSkipped: UoW exists but is not in a confirmable state
        - ApproveNotFound: no UoW with that id
        - ApproveExpired: UoW has expired
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                "SELECT id, status FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()

            if row is None:
                conn.commit()
                return ApproveNotFound(id=uow_id)

            current_status = UoWStatus(row["status"])

            match current_status:
                case UoWStatus.EXPIRED:
                    conn.commit()
                    return ApproveExpired(id=uow_id)
                case UoWStatus.PROPOSED:
                    now = _now_iso()
                    # Write two audit entries — proposed→pending then pending→ready-for-steward —
                    # so the full history is preserved even though pending is never a resting state.
                    self._write_audit(
                        conn,
                        uow_id=uow_id,
                        event="status_change",
                        from_status=UoWStatus.PROPOSED,
                        to_status=UoWStatus.PENDING,
                    )
                    self._write_audit(
                        conn,
                        uow_id=uow_id,
                        event="status_change",
                        from_status=UoWStatus.PENDING,
                        to_status=UoWStatus.READY_FOR_STEWARD,
                        note="auto-advanced: pending is not a resting state",
                    )
                    conn.execute(
                        "UPDATE uow_registry SET status = 'ready-for-steward', updated_at = ? WHERE id = ?",
                        (now, uow_id),
                    )
                    conn.commit()
                    return ApproveConfirmed(id=uow_id)
                case _:
                    conn.commit()
                    return ApproveSkipped(
                        id=uow_id,
                        reason=f"already {current_status} — no action taken.",
                        current_status=str(current_status),
                    )

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, uow_id: str) -> UoW | None:
        """Return a typed UoW value object by id, or None if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_uow(row)
        finally:
            conn.close()

    def list(self, status: str | None = None) -> list[UoW]:
        """Return all UoW records, optionally filtered by status.

        When filtering by status='ready-for-steward', results are ordered so
        that juice-quality UoWs ('juice') sort first among candidates:

            ORDER BY
              CASE WHEN juice_quality = 'juice' THEN 0 ELSE 1 END ASC,
              created_at DESC

        This implements the dispatch priority signal from the juice integration
        spec: high-juice UoWs bubble to the front of the steward's selection
        queue while preserving LIFO ordering among same-juice-status candidates.

        For all other status filters, ordering remains created_at DESC (LIFO).
        """
        conn = self._connect()
        try:
            if status == "ready-for-steward":
                # Juice-priority ordering: juice UoWs first, then LIFO.
                rows = conn.execute(
                    """
                    SELECT * FROM uow_registry
                    WHERE status = 'ready-for-steward'
                    ORDER BY
                      CASE WHEN juice_quality = 'juice' THEN 0 ELSE 1 END ASC,
                      created_at DESC
                    """,
                ).fetchall()
            elif status:
                rows = conn.execute(
                    "SELECT * FROM uow_registry WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM uow_registry ORDER BY created_at DESC"
                ).fetchall()
            return [self._row_to_uow(r) for r in rows]
        finally:
            conn.close()

    def get_status_counts(self) -> dict[str, int]:
        """Return a count of UoWs grouped by status.

        Returns a dict mapping each status present in the DB to its count,
        e.g. {"proposed": 3, "active": 1, "done": 12}.

        Prefer this over a raw GROUP BY query — connection setup and WAL mode
        are handled consistently via _connect(), and the result is typed.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM uow_registry GROUP BY status ORDER BY status"
            ).fetchall()
            return {row["status"]: row["cnt"] for row in rows}
        finally:
            conn.close()

    def expire_proposals(self) -> dict[str, Any]:
        """
        Transition proposed records older than 14 days to 'expired'.
        Writes an audit entry for each expiry in the same transaction.
        Returns {"expired_count": N, "ids": [...]}.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            to_expire = conn.execute(
                """
                SELECT id FROM uow_registry
                WHERE status = 'proposed' AND created_at <= ?
                """,
                (cutoff,),
            ).fetchall()

            expired_ids = [r["id"] for r in to_expire]
            now = _now_iso()

            for uow_id in expired_ids:
                self._write_audit(
                    conn,
                    uow_id=uow_id,
                    event="expired",
                    from_status=UoWStatus.PROPOSED,
                    to_status=UoWStatus.EXPIRED,
                    note="auto-expired: proposed for >14 days",
                )

            if expired_ids:
                placeholders = ",".join("?" * len(expired_ids))
                conn.execute(
                    f"UPDATE uow_registry SET status='expired', updated_at=? WHERE id IN ({placeholders})",
                    [now] + expired_ids,
                )

            conn.commit()
            return {"expired_count": len(expired_ids), "ids": expired_ids}

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def check_stale(
        self,
        issue_checker: IssueChecker | None = None,
    ) -> list[UoW]:
        """
        Return active UoWs whose source GitHub issue is closed.

        The `issue_checker` is a callable that takes an issue number and returns
        True if the issue is closed. In production, this calls `gh issue view`.
        Injecting it as a parameter makes the function testable without subprocess.
        """
        if issue_checker is None:
            issue_checker = _gh_issue_is_closed

        conn = self._connect()
        try:
            active_rows = conn.execute(
                """
                SELECT * FROM uow_registry
                WHERE status = 'active' AND source_issue_number IS NOT NULL
                """
            ).fetchall()
        finally:
            conn.close()

        stale = []
        for row in active_rows:
            issue_num = row["source_issue_number"]
            if issue_checker(issue_num):
                stale.append(self._row_to_uow(row))
        return stale

    def set_status_direct(self, uow_id: str, new_status: str) -> None:
        """
        Direct status set — bypasses the confirm flow.
        Used in tests and for terminal status transitions (done, failed, expired).
        Writes an audit entry in the same transaction.

        Raises ValueError if new_status is not a valid UoWStatus value.
        """
        # Coerce via UoWStatus to catch invalid strings early — raises ValueError
        # for any value not in the enum (e.g. "complete", "completed").
        UoWStatus(new_status)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                conn.commit()
                return
            old_status = row["status"]
            now = _now_iso()
            self._write_audit(
                conn,
                uow_id=uow_id,
                event="status_change",
                from_status=old_status,
                to_status=new_status,
                note="direct status set",
            )
            conn.execute(
                "UPDATE uow_registry SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def query(self, status: str) -> list[UoW]:
        """
        Return all UoW records with the given status.

        This is a named alias for `list(status=...)` with an explicit status
        parameter — used by the Registrar sweep to fetch only `pending` UoWs.
        """
        return self.list(status=status)

    # Terminal statuses for has_active_uow_for_issue — mirrors the exclusion list
    # in _upsert_typed. 'closed' is a legacy DB status treated as terminal here.
    _TERMINAL_STATUSES_FOR_ISSUE_CHECK = frozenset({
        "done", "failed", "expired", "cancelled", "closed",
    })

    def has_active_uow_for_issue(self, issue_number: int) -> bool:
        """Return True if a non-terminal UoW already exists for the given issue number.

        Used by GardenCaretaker._reactivate_uow to guard against creating a
        duplicate when scan() has already created a new proposed UoW for the
        same issue on a different sweep_date while the old UoW was temporarily
        in a terminal state.

        Mirrors the exclusion logic in _upsert_typed exactly so the two
        idempotency checks stay in sync.
        """
        conn = self._connect()
        try:
            placeholders = ", ".join("?" * len(self._TERMINAL_STATUSES_FOR_ISSUE_CHECK))
            row = conn.execute(
                f"""
                SELECT id FROM uow_registry
                WHERE source_issue_number = ?
                  AND status NOT IN ({placeholders})
                LIMIT 1
                """,
                (issue_number, *self._TERMINAL_STATUSES_FOR_ISSUE_CHECK),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def reactivate_if_no_active(self, uow_id: str, issue_number: int) -> bool:
        """Atomically reactivate a terminal UoW to proposed IFF no non-terminal row exists.

        Combines the has_active_uow_for_issue check and the status update in a
        single BEGIN IMMEDIATE transaction to eliminate the TOCTOU race between
        the check and the write.

        Returns True if the UoW was reactivated, False if skipped (because a
        non-terminal row already exists for this issue).
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            _terminal = self._TERMINAL_STATUSES_FOR_ISSUE_CHECK
            _placeholders = ", ".join("?" * len(_terminal))
            conflict = conn.execute(
                f"""
                SELECT id FROM uow_registry
                WHERE source_issue_number = ?
                  AND status NOT IN ({_placeholders})
                LIMIT 1
                """,
                (issue_number, *_terminal),
            ).fetchone()

            if conflict:
                conn.commit()
                return False

            row = conn.execute(
                "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                conn.commit()
                return False

            old_status = row["status"]
            now = _now_iso()
            self._write_audit(
                conn,
                uow_id=uow_id,
                event="status_change",
                from_status=old_status,
                to_status=UoWStatus.PROPOSED,
                note="reactivation (atomic dedup guard)",
            )
            conn.execute(
                "UPDATE uow_registry SET status = ?, updated_at = ? WHERE id = ?",
                (UoWStatus.PROPOSED, now, uow_id),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def transition(
        self,
        uow_id: str,
        to_status: str,
        where_status: str,
    ) -> int:
        """
        Conditional status transition — optimistic lock pattern.

        Atomically updates status to `to_status` only if the current status
        equals `where_status`. Returns the number of rows affected (0 or 1).

        Callers must check the return value:
        - 1: transition succeeded — write audit entry, proceed.
        - 0: another sweep already advanced this UoW — skip silently.

        Does NOT write an audit entry (the caller is responsible for that,
        conditioned on rows == 1). This keeps audit responsibility at the
        sweep layer where the business context is available.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = _now_iso()
            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (to_status, now, uow_id, where_status),
            )
            rows_affected = cursor.rowcount
            conn.commit()
            return rows_affected
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_audit_log(self, uow_id: str, entry: dict[str, Any]) -> None:
        """
        Append an unstructured audit log entry for a UoW.

        The `entry` dict is serialized as JSON into the `note` column.
        The `entry['event']` key is used as the audit event name.

        This method is used by callers (e.g., the sweep loop, conditions.py)
        that need to write rich structured events beyond the structured fields
        in `_write_audit`. The entry dict may contain any keys the caller
        finds useful for diagnostics.

        Writes atomically in a BEGIN IMMEDIATE transaction. Does not write
        a from_status or to_status — this is an annotation, not a transition.
        """
        event = entry.get("event", "unknown")
        note_json = json.dumps(entry)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, note)
                VALUES (?, ?, ?, ?)
                """,
                (_now_iso(), uow_id, event, note_json),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_stall_detected(
        self,
        uow_id: str,
        stall_reason: str,
        started_at: str | None,
        timeout_at: str | None,
        output_ref: str | None,
        elapsed_seconds: float | None,
    ) -> int:
        """
        Atomically write a stall_detected audit entry and transition the UoW
        from 'active' to 'ready-for-steward'.

        This method enforces the Observation Loop contract from #306:
        - Idempotency guard: if audit_log already contains a stall_detected
          entry with the same timeout_at value, no write is performed.
        - Audit-before-transition: the audit INSERT precedes the UPDATE in the
          same transaction (Principle 1). If the UPDATE fails, both roll back.
        - Optimistic lock: UPDATE uses WHERE status = 'active'. Returns the
          number of rows affected (0 or 1). If 0, another component already
          advanced this UoW — no audit entry is written.

        Returns 1 if the stall was recorded and transition succeeded; 0 if
        another component already advanced this UoW or idempotency guard fired.
        """
        now = _now_iso()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Idempotency guard: check for an existing stall_detected entry
            # with the same timeout_at value. Prevents double-writes on
            # partial-failure repeat passes.
            existing_stall = conn.execute(
                """
                SELECT id FROM audit_log
                WHERE uow_id = ?
                  AND event = 'stall_detected'
                  AND json_extract(note, '$.timeout_at') IS ?
                """,
                (uow_id, timeout_at),
            ).fetchone()

            if existing_stall is not None:
                conn.commit()
                return 0

            # Build the structured audit entry per the #306 spec.
            note_payload = json.dumps({
                "event": "stall_detected",
                "actor": "observation_loop",
                "uow_id": uow_id,
                "started_at": started_at,
                "timeout_at": timeout_at,
                "output_ref": output_ref,
                "elapsed_seconds": elapsed_seconds,
                "reason": stall_reason,
                "timestamp": now,
            })

            # Transition first (optimistic lock) — audit only if rows == 1
            # so we never write an audit entry for a race-lost transition.
            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'ready-for-steward', updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, uow_id),
            )
            rows_affected = cursor.rowcount

            if rows_affected == 1:
                # Audit write AFTER the optimistic lock succeeds but BEFORE
                # commit — both roll back together if the INSERT fails.
                conn.execute(
                    """
                    INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                    VALUES (?, ?, 'stall_detected', 'active', 'ready-for-steward', 'observation_loop', ?)
                    """,
                    (now, uow_id, note_payload),
                )

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_active_for_observation(self) -> list[UoW]:
        """
        Return all UoWs with status = 'active'.

        Named alias used by the Observation Loop — makes the intent explicit
        and avoids a bare list(status='active') call at the call site.
        """
        return self.list(status=UoWStatus.ACTIVE)

    def list_executing(self) -> list[UoW]:
        """
        Return all UoWs currently in-flight for the shard-stream dispatch gate.

        "In-flight" means status IN ('active', 'executing', 'ready-for-executor')
        — any state where the UoW has been dispatched to an executor and is
        consuming file-scope resources. Used by the steward parallel dispatch
        logic to determine whether a candidate UoW can safely be dispatched
        without conflicting with in-flight work.

        Named explicitly so the shard-stream gate's intent is readable at
        the call site without reconstructing the status set from first principles.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM uow_registry
                WHERE status IN (?, ?, ?)
                ORDER BY updated_at DESC
                """,
                (
                    UoWStatus.ACTIVE.value,
                    UoWStatus.EXECUTING.value,
                    UoWStatus.READY_FOR_EXECUTOR.value,
                ),
            ).fetchall()
            return [self._row_to_uow(r) for r in rows]
        finally:
            conn.close()

    def get_started_at(self, uow_id: str) -> str | None:
        """
        Return the started_at timestamp string for a UoW, or None if the UoW
        is not found or started_at is NULL.

        started_at is set by the Executor at claim time and is not exposed on
        the UoW dataclass (it is not needed by most callers). This method
        provides public access without requiring callers to open a raw
        connection via registry._connect().
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT started_at FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                return None
            return row["started_at"]
        finally:
            conn.close()

    def get_corrective_trace_history(self, uow_id: str, limit: int = 3) -> list[str]:
        """
        Return the most recent ``prescription_delta`` values from
        ``corrective_traces`` for *uow_id*, newest first.

        Returns an empty list if no rows exist or if the table is absent
        (e.g. running against a pre-migration DB in tests). Callers should
        treat an empty list as "no history available" and proceed without
        bounding.

        This is the canonical read path for corrective-trace history —
        callers must not open a raw sqlite3 connection to query this table.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT prescription_delta FROM corrective_traces WHERE uow_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (uow_id, limit),
            ).fetchall()
            return [row["prescription_delta"] for row in rows if row["prescription_delta"]]
        except Exception:
            return []
        finally:
            conn.close()

    def fetch_corrective_traces(self, uow_id: str) -> list[dict]:
        """
        Return all corrective_traces rows for the given UoW, ordered by created_at ASC.

        Each row is a plain dict with all table columns.  Returns an empty list when no
        rows exist or when the table is absent (pre-migration DB).

        Use this for the forensics trace view; use get_corrective_trace_history() when
        only prescription_delta values are needed.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, uow_id, register, execution_summary, surprises, "
                "prescription_delta, gate_score, created_at "
                "FROM corrective_traces WHERE uow_id = ? ORDER BY created_at ASC",
                (uow_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def record_startup_sweep_active(
        self,
        uow_id: str,
        classification: str,
        output_ref: str | None,
        extra: dict[str, Any] | None = None,
    ) -> int:
        """
        Atomically write a startup_sweep audit entry and transition an `active`
        UoW to `ready-for-steward`.

        Used for the four crash classifications that originate from `active`:
        possibly_complete, crashed_zero_bytes, crashed_output_ref_missing,
        crashed_no_output_ref.

        Follows the optimistic-lock + audit pattern (Principle 1):
        - UPDATE uses WHERE status = 'active'.
        - Audit INSERT is written in the same transaction only if rows == 1.
        - Returns 1 on success, 0 if another process already advanced this UoW.
        """
        now = _now_iso()
        note: dict[str, Any] = {
            "event": "startup_sweep",
            "actor": "steward",
            "classification": classification,
            "output_ref": output_ref,
            "uow_id": uow_id,
            "timestamp": now,
        }
        if extra:
            note.update(extra)
        note_json = json.dumps(note)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'ready-for-steward', updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, uow_id),
            )
            rows_affected = cursor.rowcount

            if rows_affected == 1:
                conn.execute(
                    """
                    INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                    VALUES (?, ?, 'startup_sweep', 'active', 'ready-for-steward', 'steward', ?)
                    """,
                    (now, uow_id, note_json),
                )

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_startup_sweep_executor_orphan(
        self,
        uow_id: str,
        proposed_at: str,
        age_seconds: float,
        threshold_seconds: int = 3600,
    ) -> int:
        """
        Atomically write a startup_sweep audit entry and transition a
        `ready-for-executor` UoW to `ready-for-steward`.

        Used exclusively for the executor_orphan classification: UoWs stuck
        in ready-for-executor for longer than threshold_seconds.

        Follows the optimistic-lock + audit pattern (Principle 1):
        - UPDATE uses WHERE status = 'ready-for-executor'.
        - Audit INSERT written in same transaction only if rows == 1.
        - Returns 1 on success, 0 if another process already advanced this UoW.
        """
        now = _now_iso()
        note_json = json.dumps({
            "event": "startup_sweep",
            "actor": "steward",
            "classification": "executor_orphan",
            "output_ref": None,
            "uow_id": uow_id,
            "timestamp": now,
            "prior_status": "ready-for-executor",
            "proposed_at": proposed_at,
            "age_seconds": age_seconds,
            "threshold_seconds": threshold_seconds,
        })

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'ready-for-steward', updated_at = ?
                WHERE id = ? AND status = 'ready-for-executor'
                """,
                (now, uow_id),
            )
            rows_affected = cursor.rowcount

            if rows_affected == 1:
                conn.execute(
                    """
                    INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                    VALUES (?, ?, 'startup_sweep', 'ready-for-executor', 'ready-for-steward',
                            'steward', ?)
                    """,
                    (now, uow_id, note_json),
                )

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def has_executor_orphan_history(self, uow_id: str) -> bool:
        """
        Return True if this UoW has ever been classified as executor_orphan
        in the audit_log.

        Used by the executor-heartbeat staleness filter to distinguish:
        - Fresh UoWs (never orphaned): pass through immediately for dispatch
        - Previously-orphaned UoWs: apply the RECOVERY_STALE_MINUTES gate

        The executor_orphan classification is written by
        record_startup_sweep_executor_orphan when the Steward detects a UoW
        stuck in ready-for-executor. Its presence means the primary inbox
        dispatch path already had a chance and missed this UoW — the heartbeat
        is now the correct dispatch path.

        Returns False on any query error (safe default: treat as fresh UoW,
        allow immediate dispatch).
        """
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) as c FROM audit_log
                WHERE uow_id = ?
                  AND event = 'startup_sweep'
                  AND note LIKE '%"classification": "executor_orphan"%'
                """,
                (uow_id,),
            ).fetchone()
            return (row["c"] > 0) if row else False
        except Exception:
            return False
        finally:
            conn.close()

    def get_executor_dispatch_timestamp(self, uow_id: str) -> str | None:
        """
        Return the timestamp of the most recent executor_dispatch audit entry
        for this UoW, or None if no such entry exists.

        Called by startup_sweep at classification time to determine whether
        any heartbeat writes occurred AFTER the subagent was dispatched. This
        enables distinguishing kill-before-start from kill-during-execution:

        - If heartbeat_at is None or <= dispatch_ts + DISPATCH_WINDOW_SECONDS:
          the agent was killed before establishing any working state.
        - If heartbeat_at > dispatch_ts + DISPATCH_WINDOW_SECONDS:
          the agent was actively working when killed.

        The timestamp is extracted from the `note` JSON field of the
        executor_dispatch audit entry (key: "timestamp"). Falls back to the
        `ts` column if the note is absent or unparseable.

        Returns None on any query error (safe default: treat as no dispatch,
        classify as orphan_kill_before_start).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT ts, note FROM audit_log
                WHERE uow_id = ?
                  AND event = 'executor_dispatch'
                ORDER BY id DESC
                LIMIT 1
                """,
                (uow_id,),
            ).fetchall()
            if not rows:
                return None
            row = rows[0]
            # Prefer the timestamp from the note JSON (written at dispatch time).
            note_str = row["note"] if hasattr(row, "__getitem__") else None
            if note_str:
                try:
                    data = json.loads(note_str)
                    ts = data.get("timestamp")
                    if ts:
                        return str(ts)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
            # Fallback: use the audit_log ts column
            return str(row["ts"]) if row["ts"] else None
        except Exception:
            return None
        finally:
            conn.close()

    def record_startup_sweep_diagnosing(
        self,
        uow_id: str,
    ) -> int:
        """
        Atomically write a startup_sweep audit entry and transition a
        `diagnosing` UoW back to `ready-for-steward`.

        Used when the Steward crashed mid-diagnosis. The next heartbeat
        re-diagnoses cleanly from ready-for-steward.

        Returns 1 on success, 0 if another process already advanced this UoW.
        """
        now = _now_iso()
        note_json = json.dumps({
            "event": "startup_sweep",
            "actor": "steward",
            "classification": "diagnosing_orphan",
            "output_ref": None,
            "uow_id": uow_id,
            "timestamp": now,
            "prior_status": "diagnosing",
        })

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'ready-for-steward', updated_at = ?
                WHERE id = ? AND status = 'diagnosing'
                """,
                (now, uow_id),
            )
            rows_affected = cursor.rowcount

            if rows_affected == 1:
                conn.execute(
                    """
                    INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                    VALUES (?, ?, 'startup_sweep', 'diagnosing', 'ready-for-steward',
                            'steward', ?)
                    """,
                    (now, uow_id, note_json),
                )

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_startup_sweep_executing(
        self,
        uow_id: str,
        started_at: str | None,
        age_seconds: float,
        threshold_seconds: int,
        kill_classification: str = "orphan_kill_before_start",
    ) -> int:
        """
        Atomically write a startup_sweep audit entry and transition an
        `executing` UoW to `ready-for-steward`.

        Used for UoWs that have been stuck in `executing` for longer than
        threshold_seconds without a heartbeat — i.e. the subagent that was
        dispatched never called write_result (crashed, lost context, or used
        the wrong task_id). These UoWs are invisible to the normal
        ready-for-steward processing loop.

        Follows the optimistic-lock + audit pattern (Principle 1):
        - UPDATE uses WHERE status = 'executing'.
        - Audit INSERT written in same transaction only if rows == 1.
        - Returns 1 on success, 0 if another process already advanced this UoW.

        Args:
            uow_id: The UoW identifier.
            started_at: ISO timestamp when the UoW was claimed (for audit note).
            age_seconds: How long the UoW has been in executing status.
            threshold_seconds: The threshold that was exceeded (for audit note).
            kill_classification: Heartbeat-derived kill type. One of:
                'orphan_kill_before_start': no heartbeat evidence after dispatch
                    — agent killed before establishing working state.
                'orphan_kill_during_execution': heartbeat written after dispatch
                    — agent was actively working when killed.
                Defaults to 'orphan_kill_before_start' (safe default when
                heartbeat evidence is absent or the registry method is not
                available).
        """
        now = _now_iso()
        note_json = json.dumps({
            "event": "startup_sweep",
            "actor": "steward",
            "classification": kill_classification,
            "kill_classification": kill_classification,
            "output_ref": None,
            "uow_id": uow_id,
            "timestamp": now,
            "prior_status": "executing",
            "started_at": started_at,
            "age_seconds": age_seconds,
            "threshold_seconds": threshold_seconds,
        })

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'ready-for-steward', updated_at = ?
                WHERE id = ? AND status = 'executing'
                """,
                (now, uow_id),
            )
            rows_affected = cursor.rowcount

            if rows_affected == 1:
                conn.execute(
                    """
                    INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                    VALUES (?, ?, 'startup_sweep', 'executing', 'ready-for-steward',
                            'steward', ?)
                    """,
                    (now, uow_id, note_json),
                )

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def transition_to_executing(self, uow_id: str, executor_id: str) -> None:
        """
        Transition a UoW from 'active' to 'executing' after inbox dispatch.

        Called by the Executor immediately after writing the wos_execute inbox
        message (fire-and-forget async dispatch). The UoW remains in 'executing'
        until write_result is received from the subagent, at which point
        complete_uow transitions it to 'ready-for-steward'.

        This prevents false-complete UoWs: execution_complete is only written
        when the subagent confirms completion via write_result (issue #669).

        Single transaction: audit INSERT before status UPDATE
        (audit-before-transition invariant).
        """
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'executor_dispatch', 'active', 'executing', 'executor', ?)
                """,
                (now, uow_id, json.dumps({"actor": "executor", "executor_id": executor_id, "timestamp": now})),
            )
            conn.execute(
                "UPDATE uow_registry SET status = 'executing', updated_at = ? WHERE id = ?",
                (now, uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete_uow(
        self,
        uow_id: str,
        output_ref: str,
        token_usage: int | None = None,
        outcome_category: str | None = None,
    ) -> None:
        """
        Transition a UoW to 'ready-for-steward' with an execution_complete audit entry.

        For async inbox dispatch: transitions 'executing' → 'ready-for-steward'.
        Called by the write_result MCP handler when the subagent reports completion.

        For synchronous subprocess dispatch (frontier-writer, design-review):
        transitions 'active' → 'ready-for-steward'. Called by the Executor after
        the subprocess exits.

        The from_status is derived from the current DB state so the audit entry
        is accurate regardless of which dispatch path was used.

        token_usage: optional integer (input + output tokens) reported by the subagent
        via write_result. Written to the registry when provided; NULL otherwise.
        wall_clock_seconds is derived at query time from completed_at - started_at.

        outcome_category: optional metabolic taxonomy label (heat/shit/seed/pearl)
        reported by the subagent via write_result. Written to the registry when
        provided; NULL otherwise. Validation (membership in VALID_OUTCOME_CATEGORIES)
        is the caller's responsibility — this method stores whatever is passed.

        Single transaction: audit INSERT before status UPDATE
        (audit-before-transition invariant).
        The Executor NEVER transitions to 'done' — that authority belongs
        solely to the Steward.
        """
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")
            # Derive current status for accurate audit log from_status.
            row = conn.execute(
                "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            current_status = row["status"] if row else "active"
            audit_note = json.dumps({
                "actor": "executor",
                "output_ref": output_ref,
                "timestamp": now,
                **({"token_usage": token_usage} if token_usage is not None else {}),
                **({"outcome_category": outcome_category} if outcome_category is not None else {}),
            })
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'execution_complete', ?, 'ready-for-steward', 'executor', ?)
                """,
                (now, uow_id, current_status, audit_note),
            )
            conn.execute(
                """
                UPDATE uow_registry
                SET status = 'ready-for-steward', updated_at = ?, completed_at = ?,
                    token_usage = COALESCE(?, token_usage),
                    outcome_category = COALESCE(?, outcome_category)
                WHERE id = ?
                """,
                (now, now, token_usage, outcome_category, uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_artifacts(self, uow_id: str, artifacts: list) -> None:
        """
        Store extracted outcome refs in the registry artifacts field for a UoW.

        Called by wos_completion.py after extracting artifact refs from the
        write_result payload. Overwrites any existing value — the list is derived
        fresh from result_text each time and replacement is idempotent.

        No-op when:
        - artifacts is empty (no refs were extracted — avoids noisy NULL→'[]' writes)
        - uow_id does not exist in the registry (graceful skip)

        Non-transactional: this is an advisory enrichment. If it fails, the UoW
        transition already succeeded. Callers must not rely on this field for
        correctness — only for observability and steward queries.

        Args:
            uow_id: The WOS unit-of-work ID.
            artifacts: List of typed ref dicts. Each item has at minimum:
                       {type: str, ref: str, category: str}.
        """
        if not artifacts:
            return

        conn = self._connect()
        try:
            conn.execute(
                "UPDATE uow_registry SET artifacts = ?, updated_at = ? WHERE id = ?",
                (json.dumps(artifacts), _now_iso(), uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fail_uow(self, uow_id: str, reason: str) -> None:
        """
        Transition a UoW to 'failed'.

        Handles UoWs in 'active' or 'executing' status. The from_status in the
        audit entry reflects the actual current status for accurate history.

        Single transaction: audit INSERT before status UPDATE
        (audit-before-transition invariant).
        """
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")
            # Derive current status for accurate audit log from_status.
            row = conn.execute(
                "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            current_status = row["status"] if row else "active"
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'execution_failed', ?, 'failed', 'executor', ?)
                """,
                (now, uow_id, current_status, json.dumps({"actor": "executor", "reason": reason, "timestamp": now})),
            )
            conn.execute(
                "UPDATE uow_registry SET status = 'failed', updated_at = ? WHERE id = ?",
                (now, uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Statuses from which decide-retry may recover a UoW.
    # 'blocked' covers the normal hard-cap / stuck-steward case.
    # 'ready-for-steward' covers UoWs that false-completed at executor dispatch
    # time (issue #669) and are looping in the steward queue without advancing.
    RETRYABLE_STATUSES: frozenset[str] = frozenset({"blocked", "ready-for-steward"})

    # Sentinel value returned by decide_retry when the UoW was cleaned up by
    # the hard-cap arc and a bare retry is rejected. Callers check for this
    # value to produce a user-visible error without raising an exception.
    DECIDE_RETRY_BLOCKED_BY_HARD_CAP = -1

    def decide_retry(self, uow_id: str, *, force: bool = False) -> int:
        """
        Reset a stuck UoW so it re-enters the Steward queue.

        Intended for use when Dan selects "Retry" after the Steward surfaces a
        UoW that has hit the hard cap, or when a UoW has false-completed
        at executor dispatch time (issue #669) and is looping in ready-for-steward.

        Transitions: blocked → ready-for-steward
                     ready-for-steward → ready-for-steward (with cycle reset)
        Resets steward_cycles to 0 so the Steward treats it as a fresh start,
        but first adds the current steward_cycles value to lifetime_cycles so
        cumulative effort is never lost. The hard-cap check uses lifetime_cycles,
        so repeated retries do not silently bypass the circuit breaker.

        Hard-cap commitment gate (S3-A): if close_reason == "hard_cap_cleanup",
        a bare decide_retry is rejected (returns DECIDE_RETRY_BLOCKED_BY_HARD_CAP).
        Pass force=True to override the gate after manual operator review.

        Returns:
            1 on success
            0 if UoW is not in a retryable status
            DECIDE_RETRY_BLOCKED_BY_HARD_CAP (-1) if rejected by hard-cap commitment gate

        Writes audit entry atomically in the same transaction as the UPDATE.
        """
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")

            # Read current status and cycle counts:
            # - from_status: for audit log (records actual pre-transition status)
            # - steward_cycles, lifetime_cycles: to accumulate lifetime effort before reset
            # - close_reason: to enforce hard-cap commitment gate
            placeholders = ",".join("?" * len(self.RETRYABLE_STATUSES))
            row = conn.execute(
                f"SELECT status, steward_cycles, lifetime_cycles, close_reason FROM uow_registry WHERE id = ? AND status IN ({placeholders})",
                (uow_id, *self.RETRYABLE_STATUSES),
            ).fetchone()
            from_status = row["status"] if row else None

            if row is None:
                conn.rollback()
                return 0

            # Hard-cap commitment gate: reject bare retry if cleanup arc has run.
            close_reason = row["close_reason"]
            if close_reason == "hard_cap_cleanup" and not force:
                conn.rollback()
                return self.DECIDE_RETRY_BLOCKED_BY_HARD_CAP

            current_cycles: int = row["steward_cycles"] or 0
            current_lifetime: int = row["lifetime_cycles"] or 0
            new_lifetime: int = current_lifetime + current_cycles

            note_json = json.dumps({
                "event": "decide_retry",
                "actor": "user",
                "uow_id": uow_id,
                "timestamp": now,
                "from_status": from_status,
                "force_override": force,
                "note": (
                    f"user requested retry — steward_cycles reset to 0, "
                    f"lifetime_cycles updated from {current_lifetime} to {new_lifetime}"
                    + (" (hard-cap force override)" if force else "")
                ),
            })

            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'decide_retry', ?, 'ready-for-steward', 'user', ?)
                """,
                (now, uow_id, from_status, note_json),
            )

            cursor = conn.execute(
                f"""
                UPDATE uow_registry
                SET status = 'ready-for-steward',
                    steward_cycles = 0,
                    lifetime_cycles = ?,
                    close_reason = NULL,
                    closed_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (new_lifetime, now, uow_id, *self.RETRYABLE_STATUSES),
            )
            rows_affected = cursor.rowcount

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def decide_proceed(self, uow_id: str) -> int:
        """
        Unblock a stuck UoW and re-queue it to the Steward without resetting cycles.

        Used when Dan sends `/decide <uow-id> proceed` after the Steward surfaces
        a blocked UoW. Unlike decide_retry, this preserves steward_cycles so the
        Steward knows how many attempts have already been made.

        Use case: external blocker was resolved and the UoW should resume where it
        left off. Use decide_retry when a full fresh start is needed.

        Transitions: blocked → ready-for-steward (optimistic lock on blocked).
        Does NOT reset steward_cycles.

        Returns rows_affected (1 on success, 0 if UoW is not in blocked status).
        Writes audit entry atomically in the same transaction as the UPDATE.
        """
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")

            note_json = json.dumps({
                "event": "decide_proceed",
                "actor": "user",
                "uow_id": uow_id,
                "timestamp": now,
                "note": "user requested proceed — steward_cycles preserved",
            })

            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'decide_proceed', 'blocked', 'ready-for-steward', 'user', ?)
                """,
                (now, uow_id, note_json),
            )

            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'ready-for-steward',
                    updated_at = ?
                WHERE id = ? AND status = 'blocked'
                """,
                (now, uow_id),
            )
            rows_affected = cursor.rowcount

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def decide_close(self, uow_id: str) -> int:
        """
        Close a stuck UoW as user-requested failure.

        Intended for use when Dan selects "Close" after the Steward surfaces a
        UoW that has hit the 5-cycle hard cap or another stuck condition.

        Transitions: blocked → failed (optimistic lock on blocked).
        Sets route_reason to record the user closure decision.

        Returns rows_affected (1 on success, 0 if UoW is not in blocked status).
        Writes audit entry atomically in the same transaction as the UPDATE.
        """
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")

            note_json = json.dumps({
                "event": "decide_close",
                "actor": "user",
                "uow_id": uow_id,
                "timestamp": now,
                "reason": "user_closed",
            })

            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'decide_close', 'blocked', 'failed', 'user', ?)
                """,
                (now, uow_id, note_json),
            )

            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'failed',
                    route_reason = 'user_closed',
                    updated_at = ?
                WHERE id = ? AND status = 'blocked'
                """,
                (now, uow_id),
            )
            rows_affected = cursor.rowcount

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def decide_defer(self, uow_id: str, *, note: str = "") -> int:
        """
        Defer a blocked UoW — leave it in `blocked` status with an audit note.

        Intended for use when Dan sends `/decide <uow-id> defer [note]` to
        explicitly acknowledge a blocked UoW without yet choosing to retry or close
        it. The UoW remains in `blocked` status; the audit entry records the deferral
        decision and any operator note for future context.

        No status transition occurs — this is a record-only operation. The UoW will
        remain blocked until a subsequent decide-retry, decide-proceed, or decide-close.

        Returns rows_affected (1 on success, 0 if UoW is not in blocked status).
        Writes audit entry atomically in the same transaction as the SELECT-guard.
        """
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")

            # Optimistic lock: only write the audit entry if the UoW is still blocked.
            row = conn.execute(
                "SELECT id FROM uow_registry WHERE id = ? AND status = 'blocked'",
                (uow_id,),
            ).fetchone()

            if row is None:
                conn.rollback()
                return 0

            note_json = json.dumps({
                "event": "decide_defer",
                "actor": "user",
                "uow_id": uow_id,
                "timestamp": now,
                "note": note or "user deferred — UoW remains blocked pending future decision",
            })

            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'decide_defer', 'blocked', 'blocked', 'user', ?)
                """,
                (now, uow_id, note_json),
            )

            # Touch updated_at so callers can detect a recent decision was recorded.
            conn.execute(
                "UPDATE uow_registry SET updated_at = ? WHERE id = ? AND status = 'blocked'",
                (now, uow_id),
            )

            conn.commit()
            return 1

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def registry_health(self) -> GateStatus:
        """
        Report registry health / autonomy gate status.

        Returns a typed GateStatus dataclass. gate_met is always True —
        the calendar gate was removed when the system was declared ready
        on 2026-03-30 (commit 2900900). days_running and approval_rate are
        retained for observability.
        """
        conn = self._connect()
        try:
            oldest = conn.execute(
                "SELECT MIN(created_at) as oldest FROM uow_registry"
            ).fetchone()["oldest"]

            if oldest is None:
                return GateStatus(
                    gate_met=True,
                    days_running=0,
                    approval_rate=0.0,
                    reason="no UoWs recorded yet",
                )

            oldest_dt = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            days_running = (now_dt - oldest_dt).days

            # Ratio over last 7 days — retained for observability only
            seven_days_ago = (now_dt - timedelta(days=7)).isoformat()
            proposed_last_7d = conn.execute(
                """
                SELECT COUNT(*) as c FROM uow_registry
                WHERE created_at >= ?
                """,
                (seven_days_ago,),
            ).fetchone()["c"]

            confirmed_last_7d = conn.execute(
                """
                SELECT COUNT(*) as c FROM audit_log
                WHERE event = 'status_change'
                  AND to_status = 'pending'
                  AND ts >= ?
                """,
                (seven_days_ago,),
            ).fetchone()["c"]

            ratio = (confirmed_last_7d / proposed_last_7d) if proposed_last_7d > 0 else 0.0

            return GateStatus(
                gate_met=True,
                days_running=days_running,
                approval_rate=round(ratio, 4),
                reason="gate always met",
            )
        finally:
            conn.close()

    def update_source_tracking(
        self,
        uow_id: str,
        source_ref: str,
        source_last_seen_at: str,
        source_state: str,
    ) -> None:
        """
        Write source tracking fields for a UoW.

        Called by GardenCaretaker.tend() after each successful source.get_issue()
        call. All three fields are written together — they represent a single
        consistent snapshot of the source at one point in time.

        Args:
            uow_id: The UoW identifier.
            source_ref: Canonical SourceRef string, e.g. "github:issue/42".
            source_last_seen_at: ISO 8601 timestamp of the get_issue() call.
            source_state: Last known state from source: "open", "closed",
                          "deleted", or another substrate-defined value.

        Raises:
            sqlite3.OperationalError: If the migration has not been applied
                (columns absent).
            ValueError: If uow_id does not exist in the registry.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE uow_registry
                   SET source_ref          = ?,
                       source_last_seen_at = ?,
                       source_state        = ?,
                       updated_at          = ?
                 WHERE id = ?
                """,
                (source_ref, source_last_seen_at, source_state, _now_iso(), uow_id),
            )
            rows_affected = cursor.rowcount
            if rows_affected == 0:
                raise ValueError(f"uow_id not found: {uow_id}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Heartbeat locking methods (migration 0009)
    # -----------------------------------------------------------------------

    def write_heartbeat(self, uow_id: str, token_usage: int | None = None) -> int:
        """
        Update heartbeat_at for an executing UoW to prove agent liveness.

        Uses an optimistic lock on status IN ('active', 'executing') so that
        a heartbeat write after the UoW has been recovered (transitioned to
        ready-for-steward) is a no-op rather than a silent overwrite.

        Returns rowcount: 1 on success, 0 if the UoW is no longer in an
        executing state (e.g. already recovered by the observation loop).

        The write is fire-and-forget from the agent's perspective — agents
        call this unconditionally on a regular interval (every 60–90s) and
        do not need to act on the return value.

        Args:
            uow_id: The UoW identifier.
            token_usage: Optional cumulative token count at the time of this
                heartbeat (input + output tokens combined). When provided,
                a snapshot row is written to uow_heartbeat_log so the steward
                can compute per-interval token deltas and detect stuck agents.
                Pass None (the default) to remain backwards compatible with
                agents that do not track token usage.
        """
        conn = self._connect()
        try:
            now = _now_iso()
            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('active', 'executing')
                """,
                (now, now, uow_id),
            )
            rowcount = cursor.rowcount
            # Write per-heartbeat token snapshot when token_usage is provided.
            # The uow_heartbeat_log table is created by migration 0017. If the
            # table does not yet exist (pre-migration install), we skip silently
            # so existing deploys are not broken.
            if token_usage is not None:
                try:
                    conn.execute(
                        """
                        INSERT INTO uow_heartbeat_log (uow_id, recorded_at, token_usage)
                        VALUES (?, ?, ?)
                        """,
                        (uow_id, now, token_usage),
                    )
                except Exception as log_exc:
                    # Non-fatal: table may not exist on pre-0017 deploys.
                    # Log and continue — the heartbeat_at update already succeeded.
                    log.debug(
                        "write_heartbeat: could not write heartbeat log for uow_id=%s — %s: %s",
                        uow_id, type(log_exc).__name__, log_exc,
                    )
            conn.commit()
            return rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_heartbeat_log(self, uow_id: str) -> list[dict]:
        """
        Return heartbeat log rows for a UoW, ordered oldest-first.

        Each row is a dict with keys: id, uow_id, recorded_at, token_usage.
        Only rows with non-NULL token_usage are included — NULL rows carry no
        progress signal and are irrelevant to stuck-agent detection.

        Returns an empty list when no token-bearing heartbeats have been recorded
        or when the uow_heartbeat_log table does not yet exist (pre-0017 deploys).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, uow_id, recorded_at, token_usage
                FROM uow_heartbeat_log
                WHERE uow_id = ? AND token_usage IS NOT NULL
                ORDER BY recorded_at ASC
                """,
                (uow_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            log.debug(
                "get_heartbeat_log: failed for uow_id=%s — %s: %s",
                uow_id, type(exc).__name__, exc,
            )
            return []
        finally:
            conn.close()

    def get_executing_uows_with_heartbeats(self) -> list[dict]:
        """
        Return all executing UoWs that have at least 2 token-bearing heartbeat
        log rows — the minimum needed to compute a progress delta.

        Used by the steward's stuck-agent detector (Phase 2c). Returns a list
        of dicts with keys: id, status, heartbeat_at, heartbeat_ttl.
        Only UoWs in 'active' or 'executing' status are included.

        Returns an empty list when the uow_heartbeat_log table does not yet
        exist (pre-0017 deploys).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT u.id, u.status, u.heartbeat_at, u.heartbeat_ttl
                FROM uow_registry u
                WHERE u.status IN (?, ?)
                  AND (
                    SELECT COUNT(*)
                    FROM uow_heartbeat_log h
                    WHERE h.uow_id = u.id AND h.token_usage IS NOT NULL
                  ) >= 2
                """,
                (UoWStatus.ACTIVE.value, UoWStatus.EXECUTING.value),
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            log.debug(
                "get_executing_uows_with_heartbeats: failed — %s: %s",
                type(exc).__name__, exc,
            )
            return []
        finally:
            conn.close()

    def set_heartbeat_ttl(self, uow_id: str, heartbeat_ttl: int) -> None:
        """
        Set heartbeat_ttl and initialize heartbeat_at at claim time.

        Called by the Executor after claiming a UoW (after started_at is written).
        Sets heartbeat_ttl to the provided value (derived from estimated_runtime
        or the default 300s). Also writes the initial heartbeat_at timestamp so
        the observation loop has a baseline from which to measure silence.

        Args:
            uow_id: The UoW identifier.
            heartbeat_ttl: Maximum silence threshold in seconds before the
                observation loop considers this UoW stalled.
        """
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE uow_registry
                SET heartbeat_at = ?, heartbeat_ttl = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, heartbeat_ttl, now, uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_stale_heartbeat_uows(self, buffer_seconds: int = 30) -> list["UoW"]:
        """
        Return UoWs where the heartbeat has gone stale.

        Staleness condition (all must hold):
        1. status IN ('active', 'executing') — UoW is in-flight
        2. heartbeat_at IS NOT NULL — agent has written at least one heartbeat
        3. (now - heartbeat_at) > heartbeat_ttl + buffer_seconds

        The buffer_seconds argument adds a grace period on top of heartbeat_ttl
        to absorb minor clock skew and scheduling jitter between the agent's
        heartbeat write and the steward's observation check. Default: 30s.

        UoWs with heartbeat_at = NULL are NOT returned — those use the legacy
        started_at-based TTL path in the existing observation loop.

        Args:
            buffer_seconds: Grace period added to heartbeat_ttl before declaring
                a stall. Default 30 to absorb scheduling jitter.

        Returns:
            List of UoW objects with stale heartbeats (may be empty).
        """
        conn = self._connect()
        try:
            now = _now_iso()
            rows = conn.execute(
                """
                SELECT * FROM uow_registry
                WHERE status IN ('active', 'executing')
                  AND heartbeat_at IS NOT NULL
                  AND (
                    CAST((julianday(?) - julianday(heartbeat_at)) * 86400 AS INTEGER)
                    > heartbeat_ttl + ?
                  )
                ORDER BY heartbeat_at ASC
                """,
                (now, buffer_seconds),
            ).fetchall()
            return [self._row_to_uow(r) for r in rows]
        finally:
            conn.close()

    def record_heartbeat_stall(
        self,
        uow_id: str,
        heartbeat_at: str | None,
        heartbeat_ttl: int,
        silence_seconds: float,
    ) -> int:
        """
        Atomically write a heartbeat_stall audit entry and transition the UoW
        from ('active' or 'executing') to 'ready-for-steward'.

        Follows the same optimistic-lock + audit pattern as record_stall_detected:
        - Transition first (optimistic lock on status IN ('active', 'executing')).
        - Audit INSERT only if rows_affected == 1 (prevents phantom audit on race).
        - Both roll back together if either fails.

        Returns 1 if the transition succeeded; 0 if another component already
        advanced this UoW (race-safe — no audit entry written in that case).
        """
        now = _now_iso()
        note_payload = json.dumps({
            "event": "stall_detected",
            "stall_type": "heartbeat_stall",
            "actor": "observation_loop",
            "uow_id": uow_id,
            "heartbeat_at": heartbeat_at,
            "heartbeat_ttl": heartbeat_ttl,
            "silence_seconds": silence_seconds,
            "timestamp": now,
        })

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'ready-for-steward', updated_at = ?
                WHERE id = ? AND status IN ('active', 'executing')
                """,
                (now, uow_id),
            )
            rows_affected = cursor.rowcount

            if rows_affected == 1:
                conn.execute(
                    """
                    INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                    VALUES (?, ?, 'stall_detected', 'active', 'ready-for-steward',
                            'observation_loop', ?)
                    """,
                    (now, uow_id, note_payload),
                )

            conn.commit()
            return rows_affected

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Time-window query (used by registry_cli report command)
    # -----------------------------------------------------------------------

    def fetch_in_window(self, since_iso: str) -> list[dict]:
        """
        Return all UoW rows whose started_at or completed_at falls at or after
        since_iso (an ISO 8601 UTC timestamp string).

        Returns raw dicts rather than UoW value objects so that callers can
        access columns not yet mapped into the UoW dataclass (e.g. token_usage,
        completed_at, outcome_category). Each dict is keyed by column name.

        Results are ordered by most-recent-first (COALESCE(completed_at,
        started_at, created_at) DESC).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT *,
                       CASE
                         WHEN started_at IS NOT NULL AND completed_at IS NOT NULL
                         THEN CAST(
                               (julianday(completed_at) - julianday(started_at)) * 86400
                               AS INTEGER)
                         ELSE NULL
                       END AS wall_clock_seconds
                FROM uow_registry
                WHERE started_at >= ?
                   OR completed_at >= ?
                ORDER BY COALESCE(completed_at, started_at, created_at) DESC
                """,
                (since_iso, since_iso),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Latency query (used by registry_cli queue-latency command)
    # -----------------------------------------------------------------------

    def fetch_for_latency(self, since_iso: str, status: str | None = None) -> list[dict]:
        """
        Return UoW rows needed to compute queue-wait and execution-time latency.

        Selects only the columns required for latency computation:
          created_at, started_at, completed_at, status.

        Filters to rows where started_at is not NULL and falls at or after
        since_iso (the window start). An optional status filter narrows further.

        Read-only. Never modifies registry state.
        """
        conn = self._connect()
        try:
            if status is not None:
                rows = conn.execute(
                    """
                    SELECT id, created_at, started_at, completed_at, status
                    FROM uow_registry
                    WHERE started_at >= ?
                      AND status = ?
                    ORDER BY started_at ASC
                    """,
                    (since_iso, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, created_at, started_at, completed_at, status
                    FROM uow_registry
                    WHERE started_at >= ?
                    ORDER BY started_at ASC
                    """,
                    (since_iso,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Juice dispatch priority methods (migration 0013)
    # -----------------------------------------------------------------------

    def write_juice(
        self,
        uow_id: str,
        juice_quality: str | None,
        juice_rationale: str | None,
    ) -> None:
        """
        Write juice_quality and juice_rationale to a UoW row.

        Called by the steward at dispatch time after compute_juice() evaluates
        the UoW's generative momentum. The steward is the sole writer of these
        fields — the same convention that applies to steward_notes, steward_log,
        and steward_agenda.

        juice_quality='juice' asserts live generative momentum. None clears it.
        juice_rationale must be non-None when juice_quality='juice' (spec requirement:
        a prescription without a named rationale should not assert juice).

        Enforces the delta-update contract at the call site (JUICE_UPDATE_DELTA=0.05):
        callers are expected to only call write_juice when the score has changed
        materially. This method does not check the delta — it writes unconditionally.

        Args:
            uow_id: The UoW identifier.
            juice_quality: 'juice' to assert, or None to clear.
            juice_rationale: Mandatory prose when juice_quality='juice'. Pass None
                when clearing juice_quality.

        Raises:
            ValueError: If juice_quality='juice' but juice_rationale is falsy.
        """
        if juice_quality == "juice" and not juice_rationale:
            raise ValueError(
                f"write_juice: juice_rationale is required when juice_quality='juice' "
                f"(uow_id={uow_id}). The spec requires naming what is alive and why."
            )
        conn = self._connect()
        try:
            now = _now_iso()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE uow_registry
                SET juice_quality = ?, juice_rationale = ?, updated_at = ?
                WHERE id = ?
                """,
                (juice_quality, juice_rationale, now, uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Steward/Executor schema validation
# ---------------------------------------------------------------------------

# Fields required for Steward/Executor operation.
# If any are absent, the Steward must exit with a clear error rather than
# silently failing mid-execution.
_STEWARD_EXECUTOR_REQUIRED_FIELDS = frozenset({
    "workflow_artifact",
    "success_criteria",
    "prescribed_skills",
    "steward_cycles",
    "lifetime_cycles",
    "timeout_at",
    "estimated_runtime",
    "steward_agenda",
    "steward_log",
})


def validate_steward_executor_schema(conn: sqlite3.Connection) -> None:
    """
    Validate that all fields required for Steward/Executor operation are present
    in uow_registry.

    Raises RuntimeError with a specific message if any required field is absent
    from the table. Call this at Steward startup before processing any UoW.

    Args:
        conn: An open SQLite connection to the registry database.

    Raises:
        RuntimeError: If any required field is missing. Message includes
            "schema migration not applied" and the list of missing fields.
    """
    rows = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
    existing_cols = {row[1] for row in rows}
    missing = _STEWARD_EXECUTOR_REQUIRED_FIELDS - existing_cols
    if missing:
        missing_sorted = sorted(missing)
        raise RuntimeError(
            f"schema migration not applied — run scripts/migrate_add_steward_fields.py first. "
            f"Missing fields: {missing_sorted}"
        )


# Keep the old name as an alias so any existing callers continue to work.
validate_phase2_schema = validate_steward_executor_schema


# ---------------------------------------------------------------------------
# WOSRegistry — convenience wrapper that uses the default REGISTRY_DB path
# ---------------------------------------------------------------------------

class WOSRegistry(Registry):
    """
    Convenience subclass of Registry that uses the canonical REGISTRY_DB path.

    Allows WOS subagents to write heartbeats without needing to import or
    resolve the DB path themselves:

        from src.orchestration.registry import WOSRegistry
        WOSRegistry().write_heartbeat(uow_id)

    This is equivalent to:

        from src.orchestration.paths import REGISTRY_DB
        from src.orchestration.registry import Registry
        Registry(REGISTRY_DB).write_heartbeat(uow_id)

    WOSRegistry is intentionally minimal — it adds no methods beyond the
    Registry base class. Its sole purpose is to eliminate the path-resolution
    boilerplate that was causing agent-side heartbeat calls to fail when
    prompts referenced this class name before it existed.

    Design note (issue #849): Agent-side heartbeat emission is now backed by
    two mechanisms:
    1. Prompt instructions (handle_wos_execute) — agents are told to call
       write_heartbeat at regular checkpoints.
    2. Heartbeat sidecar (heartbeat_sidecar.py) — structural enforcement via
       the executor-heartbeat cron, which writes heartbeats for all in-flight
       UoWs every ~3 minutes regardless of whether the agent called them.

    WOSRegistry enables mechanism 1 to work reliably by providing the class
    name referenced in the dispatched prompt.
    """

    def __init__(self) -> None:
        from src.orchestration.paths import REGISTRY_DB
        super().__init__(REGISTRY_DB)


# ---------------------------------------------------------------------------
# NoteAccessor — thin wrapper for the notes JSONB column in uow_registry
# ---------------------------------------------------------------------------

class NoteAccessor:
    """
    Thin wrapper for reads/writes to the `notes` JSONB column in uow_registry.

    Enforces the following invariants:
    - Keys must not contain '.' — nested path writes are not supported.
    - All writes are atomic UPDATE statements (BEGIN IMMEDIATE transaction).
    - Uses the Registry's existing connection pattern — no new DB connections.

    Usage:
        notes = NoteAccessor(registry)
        notes.set("uow_20260330_abc123", "deploy_tag", "v1.2.3")
        tag = notes.get("uow_20260330_abc123", "deploy_tag")   # → "v1.2.3"
        notes.append_log("uow_20260330_abc123", "step 1 complete")
    """

    def __init__(self, registry: "Registry") -> None:
        self._registry = registry

    @staticmethod
    def _validate_key(key: str) -> None:
        """Reject keys containing '.' — no nested path writes."""
        if "." in key:
            raise ValueError(
                f"NoteAccessor: key {key!r} contains '.' — nested path writes are not supported"
            )

    def get(self, uow_id: str, key: str) -> Any | None:
        """
        Read a specific key from the notes JSON for a UoW.

        Returns the value associated with `key`, or None if:
        - the UoW does not exist
        - the notes field is NULL or not valid JSON
        - the key is absent
        """
        self._validate_key(key)
        conn = self._registry._connect()
        try:
            row = conn.execute(
                "SELECT notes FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                return None
            raw = row["notes"]
            if not raw:
                return None
            try:
                data: dict = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None
            return data.get(key)
        finally:
            conn.close()

    def set(self, uow_id: str, key: str, value: Any) -> None:
        """
        Write or update a single key in the notes JSON for a UoW.

        Uses an atomic JSON_patch UPDATE — reads the current notes, merges
        the new key, and writes back in a single BEGIN IMMEDIATE transaction.
        """
        self._validate_key(key)
        conn = self._registry._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT notes FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return
            raw = row["notes"]
            try:
                data: dict = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            data[key] = value
            conn.execute(
                "UPDATE uow_registry SET notes = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data), _now_iso(), uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_log(self, uow_id: str, entry: str) -> None:
        """
        Append a string entry to notes['log'] list.

        Creates the list if it does not exist. Atomic: reads current notes,
        appends the entry, and writes back in a single BEGIN IMMEDIATE
        transaction.
        """
        conn = self._registry._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT notes FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return
            raw = row["notes"]
            try:
                data: dict = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            log_entries: list = data.get("log") or []
            log_entries = list(log_entries)
            log_entries.append(entry)
            data["log"] = log_entries
            conn.execute(
                "UPDATE uow_registry SET notes = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data), _now_iso(), uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Production issue checker (subprocess to gh CLI)
# ---------------------------------------------------------------------------

def _gh_issue_is_closed(issue_number: int) -> bool:
    """Return True if the GitHub issue is closed. Uses gh CLI."""
    import subprocess
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", "dcetlin/Lobster",
             "--json", "state", "--jq", ".state"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip().upper() == "CLOSED"
    except Exception:
        # If we can't check, assume open (conservative: don't flag as stale)
        return False
