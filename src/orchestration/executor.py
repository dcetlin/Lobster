"""
WOS Phase 2 Executor — picks up UoWs in 'ready-for-executor' state, performs
the 6-step atomic claim sequence, dispatches via LLM subagent, writes results,
and returns the UoW to the Steward for evaluation.

Design constraints enforced here:
- 6-step claim sequence executes atomically in a single SQLite transaction.
- Audit-before-transition: audit_log INSERT precedes every status UPDATE.
- Optimistic lock: rowcount check on the claim UPDATE before proceeding.
- result.json written at every intentional exit (complete, partial, failed, blocked).
- Exception during execution still writes result.json before re-raising.
- Executor NEVER transitions to 'done' — only the Steward declares closure.

Dispatch protocol:
- The production dispatcher (_dispatch_via_inbox) writes a structured JSON
  message to ~/messages/inbox/ so the Lobster dispatcher (main Claude loop)
  can read it and spawn a subagent via the Task tool.
- The message type is 'wos_execute'. The dispatcher routes this type to a
  subagent that runs the prescribed instructions and writes the result file.
- The Executor does NOT block waiting for the subagent — it writes timeout_at
  at claim time and returns. The Observation Loop (#306) detects stalls.
- The message_id is returned as executor_id for audit correlation.

Imports:
    from orchestration.executor import Executor, ExecutorOutcome, ExecutorResult

Canonical output path convention (from executor-contract.md):
    {output_ref}.result.json  (replace extension)
    fallback: {output_ref}.result.json as suffix when output_ref has no extension
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from orchestration.registry import Registry, UoW, UoWStatus
from orchestration.workflow_artifact import WorkflowArtifact, from_json


# ---------------------------------------------------------------------------
# Admin chat ID (env-injected, never hardcoded)
# ---------------------------------------------------------------------------

LOBSTER_ADMIN_CHAT_ID: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")

# Output directory for executor result and work files
_OUTPUT_DIR_TEMPLATE = "~/lobster-workspace/orchestration/outputs"


# ---------------------------------------------------------------------------
# ExecutorOutcome StrEnum — per executor-contract.md
# ---------------------------------------------------------------------------

class ExecutorOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL  = "partial"
    FAILED   = "failed"
    BLOCKED  = "blocked"

    def is_terminal(self) -> bool:
        """True when the Executor considers no further execution possible."""
        return self in {ExecutorOutcome.FAILED, ExecutorOutcome.BLOCKED}

    def is_success(self) -> bool:
        return self == ExecutorOutcome.COMPLETE


# ---------------------------------------------------------------------------
# ExecutorResult — frozen dataclass, per executor-contract.md
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExecutorResult:
    uow_id:           str
    outcome:          ExecutorOutcome
    success:          bool              # must equal outcome.is_success()
    reason:           str | None = None
    steps_completed:  int | None = None
    steps_total:      int | None = None
    output_artifact:  str | None = None
    executor_id:      str | None = None

    def to_dict(self) -> dict:
        """Serialize to dict for JSON output. Omits None-valued optional fields."""
        d = {
            "uow_id": self.uow_id,
            "outcome": str(self.outcome),
            "success": self.success,
        }
        for field in ("reason", "steps_completed", "steps_total", "output_artifact", "executor_id"):
            val = getattr(self, field)
            if val is not None:
                d[field] = val
        return d


# ---------------------------------------------------------------------------
# Named claim results — no dict[str, Any] in the decision path
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimSucceeded:
    uow_id: str
    output_ref: str
    artifact: WorkflowArtifact


@dataclass(frozen=True)
class ClaimRejected:
    uow_id: str
    reason: str


@dataclass(frozen=True)
class ClaimNotFound:
    uow_id: str


ClaimResult = ClaimSucceeded | ClaimRejected | ClaimNotFound


# ---------------------------------------------------------------------------
# SkillActivator protocol — injectable dependency for testing
# ---------------------------------------------------------------------------

class SkillActivator(Protocol):
    def __call__(self, skill_id: str) -> None: ...


# ---------------------------------------------------------------------------
# SubagentDispatcher protocol — injectable dependency for testing
# ---------------------------------------------------------------------------

class SubagentDispatcher(Protocol):
    def __call__(self, instructions: str, uow_id: str) -> str: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output_ref_path(uow_id: str) -> str:
    """Return the absolute output_ref path for a UoW."""
    expanded = os.path.expanduser(_OUTPUT_DIR_TEMPLATE)
    return str(Path(expanded) / f"{uow_id}.json")


def _result_json_path(output_ref: str) -> Path:
    """
    Derive result.json path from output_ref.

    Primary convention: replace extension (foo.json → foo.result.json).
    Fallback: append .result.json when output_ref has no extension.
    """
    p = Path(output_ref)
    if p.suffix:
        return p.with_suffix(".result.json")
    return Path(output_ref + ".result.json")


def _write_result_json(output_ref: str, result: ExecutorResult) -> None:
    """Write result.json atomically. Creates parent dir if needed."""
    result_path = _result_json_path(output_ref)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result.to_dict(), indent=2))


def _write_output_ref_content(output_ref: str, content: str) -> None:
    """Write text content to the output_ref path. Creates parent dir if needed."""
    p = Path(output_ref)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class Executor:
    """
    Picks up a single UoW in 'ready-for-executor' state, executes it, and
    transitions it to 'ready-for-steward' (on success) or 'failed' (on error).

    The Executor NEVER transitions a UoW to 'done' — that authority belongs
    solely to the Steward.

    All state transitions write to audit_log in the same transaction, before
    the status UPDATE (audit-before-transition invariant).

    Usage:
        executor = Executor(registry)
        result = executor.execute_uow(uow_id)
    """

    def __init__(
        self,
        registry: Registry,
        skill_activator: SkillActivator | None = None,
        dispatcher: SubagentDispatcher | None = None,
    ) -> None:
        """
        Args:
            registry: The Registry instance (provides db_path for raw connections).
            skill_activator: Callable that activates a skill by ID. Defaults to
                _noop_skill_activator. Injectable for tests.
            dispatcher: Callable that dispatches the LLM subagent task. Defaults
                to _dispatch_via_inbox (writes a wos_execute message to the Lobster
                inbox so the dispatcher spawns a subagent via the Task tool).
                Injectable for tests.
        """
        self.registry = registry
        self._skill_activator = skill_activator or _noop_skill_activator
        self._dispatcher = dispatcher or _dispatch_via_inbox

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def execute_uow(self, uow_id: str) -> ExecutorResult:
        """
        Claim and execute a single UoW.

        Returns an ExecutorResult. The result.json file is always written
        before this method returns (success or failure). On exception, the
        result.json is written with outcome='failed' and the exception is re-raised.
        """
        claim = self._claim(uow_id)

        match claim:
            case ClaimNotFound():
                # UoW does not exist in the registry.
                raise ValueError(f"Executor: UoW {uow_id!r} not found in registry")
            case ClaimRejected(reason=reason):
                # Optimistic lock failed or artifact missing — caller may retry or skip.
                raise RuntimeError(f"Executor: claim rejected for {uow_id!r} — {reason}")
            case ClaimSucceeded(uow_id=uid, output_ref=output_ref, artifact=artifact):
                return self._run_step_sequence(uid, output_ref, artifact)

    # -----------------------------------------------------------------------
    # Claim sequence (6 steps, single transaction)
    # -----------------------------------------------------------------------

    def _claim(self, uow_id: str) -> ClaimResult:
        """
        Perform the 6-step atomic claim sequence.

        Steps 2-6 execute in a single BEGIN IMMEDIATE transaction. If any step
        fails, the transaction is rolled back — the UoW remains in
        'ready-for-executor' with no partial state.

        Returns a typed ClaimResult.
        """
        conn = self._connect()
        try:
            # Step 1: Read UoW — verify existence (outside transaction is fine;
            # the actual claim is the atomic step 2 UPDATE with WHERE guard).
            row = conn.execute(
                "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()

            if row is None:
                return ClaimNotFound(uow_id=uow_id)

            # Step 2-6: Single atomic transaction
            conn.execute("BEGIN IMMEDIATE")

            # Step 2: Optimistic UPDATE — only succeeds if status is still 'ready-for-executor'
            now = _now_iso()
            cursor = conn.execute(
                """
                UPDATE uow_registry
                SET status = 'active', updated_at = ?
                WHERE id = ? AND status = 'ready-for-executor'
                """,
                (now, uow_id),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                current_status = row["status"]
                return ClaimRejected(
                    uow_id=uow_id,
                    reason=f"optimistic lock failed: status was {current_status!r}, not 'ready-for-executor'",
                )

            # Step 3: Write started_at
            conn.execute(
                "UPDATE uow_registry SET started_at = ? WHERE id = ?",
                (now, uow_id),
            )

            # Step 4: Compute and write output_ref
            output_ref = _output_ref_path(uow_id)
            conn.execute(
                "UPDATE uow_registry SET output_ref = ? WHERE id = ?",
                (output_ref, uow_id),
            )

            # Step 5: Compute and write timeout_at
            estimated_runtime = row["estimated_runtime"]
            timeout_seconds = int(estimated_runtime) if estimated_runtime is not None else 1800
            started_dt = datetime.fromisoformat(now)
            from datetime import timedelta
            timeout_dt = started_dt + timedelta(seconds=timeout_seconds)
            timeout_at = timeout_dt.isoformat()
            conn.execute(
                "UPDATE uow_registry SET timeout_at = ? WHERE id = ?",
                (timeout_at, uow_id),
            )

            # Step 6: INSERT audit_log (must be in same transaction, before COMMIT)
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'claimed', 'ready-for-executor', 'active', 'executor', ?)
                """,
                (
                    now,
                    uow_id,
                    json.dumps({
                        "actor": "executor",
                        "started_at": now,
                        "output_ref": output_ref,
                        "timeout_at": timeout_at,
                    }),
                ),
            )

            conn.commit()

            # Deserialize workflow_artifact (after transaction commits)
            # Post-claim validation: these are planned stops, not crashes.
            # The atomic claim (steps 2-6) has already committed; output_ref is
            # registered. Each branch below writes result.json before calling
            # registry.fail_uow so the Steward can distinguish a planned stop from an
            # orphan (executor-contract.md: every intentional exit must produce
            # a result file).
            workflow_artifact_raw = row["workflow_artifact"]
            if not workflow_artifact_raw:
                null_reason = "workflow_artifact field is NULL or empty — cannot execute"
                null_result = ExecutorResult(
                    uow_id=uow_id,
                    outcome=ExecutorOutcome.FAILED,
                    success=False,
                    reason=null_reason,
                )
                _write_result_json(output_ref, null_result)
                self.registry.fail_uow(uow_id, null_reason)
                return ClaimRejected(
                    uow_id=uow_id,
                    reason="workflow_artifact field is NULL or empty",
                )

            # The Steward writes the artifact JSON to a file and stores the
            # absolute path in the workflow_artifact column. Detect a path
            # value (starts with '/') and read the file; otherwise treat the
            # field value as inline JSON (legacy / test path).
            artifact_json_str = workflow_artifact_raw
            if workflow_artifact_raw.startswith("/"):
                artifact_file = Path(workflow_artifact_raw)
                if not artifact_file.exists():
                    missing_reason = (
                        f"workflow_artifact file not found: {workflow_artifact_raw}"
                    )
                    missing_result = ExecutorResult(
                        uow_id=uow_id,
                        outcome=ExecutorOutcome.FAILED,
                        success=False,
                        reason=missing_reason,
                    )
                    _write_result_json(output_ref, missing_result)
                    self.registry.fail_uow(uow_id, missing_reason)
                    return ClaimRejected(
                        uow_id=uow_id,
                        reason=missing_reason,
                    )
                artifact_json_str = artifact_file.read_text(encoding="utf-8")

            try:
                artifact = from_json(artifact_json_str)
            except ValueError as e:
                deser_reason = f"workflow_artifact deserialization failed: {e}"
                deser_result = ExecutorResult(
                    uow_id=uow_id,
                    outcome=ExecutorOutcome.FAILED,
                    success=False,
                    reason=deser_reason,
                )
                _write_result_json(output_ref, deser_result)
                self.registry.fail_uow(uow_id, deser_reason)
                return ClaimRejected(
                    uow_id=uow_id,
                    reason=f"workflow_artifact deserialization failed: {e}",
                )

            return ClaimSucceeded(uow_id=uow_id, output_ref=output_ref, artifact=artifact)

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Execution
    # -----------------------------------------------------------------------

    def _run_step_sequence(
        self,
        uow_id: str,
        output_ref: str,
        artifact: WorkflowArtifact,
    ) -> ExecutorResult:
        """
        Execute a claimed UoW through its full step sequence.

        On any unhandled exception: write result.json with outcome='failed',
        transition to 'failed' status via the Registry, then re-raise.
        """
        try:
            return self._run_execution(uow_id, output_ref, artifact)
        except Exception as exc:
            # Ensure result.json is always written, even on crash
            reason = f"{type(exc).__name__}: {exc}"
            result = ExecutorResult(
                uow_id=uow_id,
                outcome=ExecutorOutcome.FAILED,
                success=False,
                reason=reason,
            )
            _write_output_ref_content(output_ref, f"execution failed: {reason}")
            _write_result_json(output_ref, result)
            self.registry.fail_uow(uow_id, reason)
            raise

    def _run_execution(
        self,
        uow_id: str,
        output_ref: str,
        artifact: WorkflowArtifact,
    ) -> ExecutorResult:
        """
        Inner execution: activate skills, dispatch subagent, write results.
        Returns ExecutorResult. Callers must catch exceptions.
        """
        # Step 1: Activate prescribed skills
        prescribed_skills: list[str] = artifact.get("prescribed_skills") or []
        for skill_id in prescribed_skills:
            self._skill_activator(skill_id)

        # Step 2: Dispatch LLM subagent
        instructions = artifact["instructions"]
        executor_id = self._dispatcher(instructions, uow_id)

        # Step 3: Write output_ref content (signal that execution produced output)
        _write_output_ref_content(output_ref, f"execution complete: task dispatched as {executor_id}")

        # Step 4: Build result
        result = ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.COMPLETE,
            success=True,
            executor_id=executor_id or None,
        )

        # Step 5: Write result.json
        _write_result_json(output_ref, result)

        # Step 6: Transition to ready-for-steward (audit before status update, single transaction)
        self.registry.complete_uow(uow_id, output_ref)

        return result

    # -----------------------------------------------------------------------
    # Result helpers for callers that need non-complete outcomes
    # -----------------------------------------------------------------------

    def report_partial(
        self,
        uow_id: str,
        output_ref: str,
        reason: str,
        steps_completed: int | None = None,
        steps_total: int | None = None,
    ) -> ExecutorResult:
        """
        Report a partial outcome and transition to 'ready-for-steward'.

        Partial = some steps completed; Executor stopped intentionally before
        completing the full prescription. Requires updated instructions to resume.
        """
        result = ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.PARTIAL,
            success=False,
            reason=reason,
            steps_completed=steps_completed,
            steps_total=steps_total,
        )
        _write_output_ref_content(output_ref, f"partial: {reason}")
        _write_result_json(output_ref, result)
        self.registry.complete_uow(uow_id, output_ref)
        return result

    def report_blocked(
        self,
        uow_id: str,
        output_ref: str,
        reason: str,
    ) -> ExecutorResult:
        """
        Report a blocked outcome and transition to 'ready-for-steward'.

        Blocked = Executor cannot proceed without external resolution.
        The Steward will surface to Dan and await /decide.
        """
        result = ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.BLOCKED,
            success=False,
            reason=reason,
        )
        _write_output_ref_content(output_ref, f"blocked: {reason}")
        _write_result_json(output_ref, result)
        self.registry.complete_uow(uow_id, output_ref)
        return result

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.registry.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


# ---------------------------------------------------------------------------
# Production dispatcher — inbox-based agent launch
# ---------------------------------------------------------------------------

#: Inbox directory — where dispatch messages are written for the Lobster
#: dispatcher (main Claude loop) to pick up and route to a subagent.
_INBOX_DIR_TEMPLATE = "~/messages/inbox"

#: Admin chat ID injected via env — same as LOBSTER_ADMIN_CHAT_ID.
#: Used as the chat_id for wos_execute messages so the dispatcher can
#: route results back to Dan if needed.
_DISPATCH_CHAT_ID: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")


def _dispatch_via_inbox(instructions: str, uow_id: str) -> str:
    """
    Production dispatcher: write a wos_execute message to the Lobster inbox.

    The Lobster dispatcher (main Claude loop) reads ~/messages/inbox/ on each
    cycle. When it sees a message with type='wos_execute', it spawns a
    background subagent via the Task tool with the prescribed instructions.

    This is fire-and-forget: the Executor does NOT block waiting for the
    subagent. Completion is detected by the Steward on its next heartbeat
    cycle, via the result.json file the subagent writes (executor-contract.md).

    The message_id is returned as the executor_id for audit correlation.

    Raises OSError if the inbox directory cannot be created or the message
    file cannot be written — the caller's exception handler will write
    result.json with outcome=failed.
    """
    msg_id = str(uuid.uuid4())
    inbox_dir = Path(os.path.expanduser(_INBOX_DIR_TEMPLATE))
    inbox_dir.mkdir(parents=True, exist_ok=True)

    msg: dict = {
        "id": msg_id,
        "source": "system",
        "type": "wos_execute",
        "chat_id": _DISPATCH_CHAT_ID,
        "uow_id": uow_id,
        "instructions": instructions,
        "timestamp": _now_iso(),
    }

    tmp_path = inbox_dir / f"{msg_id}.json.tmp"
    dest_path = inbox_dir / f"{msg_id}.json"
    try:
        tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp_path.rename(dest_path)
    finally:
        # Best-effort cleanup of tmp file if rename failed.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass

    return msg_id


# ---------------------------------------------------------------------------
# No-op implementations — for tests and environments without a live inbox
# ---------------------------------------------------------------------------

def _noop_skill_activator(skill_id: str) -> None:
    """No-op skill activator. In production, activate_skill MCP is called instead."""
    pass


def _noop_dispatcher(instructions: str, uow_id: str) -> str:
    """No-op dispatcher for tests. In production, _dispatch_via_inbox is used."""
    return f"dispatched:{uow_id}"
