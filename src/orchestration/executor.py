"""
WOS Executor — picks up UoWs in 'ready-for-executor' state, performs
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
- The production dispatcher (_dispatch_via_claude_p) spawns a functional-engineer
  subagent via `claude -p` (subprocess, synchronous). The subagent reads the
  GitHub issue, implements the prescription, opens a PR, and calls write_result.
  The executor waits for the subprocess to complete before transitioning the UoW.
- TTL recovery: UoWs stuck in 'active' state for more than TTL_EXCEEDED_HOURS are
  transitioned to 'failed' with return_reason='ttl_exceeded'. Call
  recover_ttl_exceeded_uows(registry) at heartbeat startup before the dispatch
  cycle so the Steward can re-diagnose stalled UoWs on its next pass.
- Default: Executor(...) with dispatcher=None activates the dispatch table
  (_EXECUTOR_TYPE_TO_DISPATCHER). The heartbeat passes dispatcher=None so
  register-appropriate routing activates in production. Tests inject
  _noop_dispatcher or a stub to suppress real agent dispatch. The inbox-based
  fallback (_dispatch_via_inbox) is available for dev/CI environments that
  lack a local claude-p subprocess.

Imports:
    from orchestration.executor import Executor, ExecutorOutcome, ExecutorResult
    from orchestration.executor import recover_ttl_exceeded_uows

Canonical output path convention (from executor-contract.md):
    {output_ref}.result.json  (replace extension)
    fallback: {output_ref}.result.json as suffix when output_ref has no extension
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from orchestration.config import TimeoutConfig

log = logging.getLogger("executor")

from orchestration.registry import Registry, UoW, UoWStatus
from orchestration.result_writer import write_result as _write_subagent_result
from orchestration.workflow_artifact import WorkflowArtifact, from_json
from orchestration.error_capture import (
    run_subprocess_with_error_capture,
    log_subprocess_error,
    classify_error,
    has_repeated_error,
)


# ---------------------------------------------------------------------------
# Admin chat ID (env-injected, never hardcoded)
# ---------------------------------------------------------------------------

LOBSTER_ADMIN_CHAT_ID: str = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")

# Output directory for executor result and work files
_OUTPUT_DIR_TEMPLATE = "~/lobster-workspace/orchestration/outputs"

# UoWs stuck in 'active' state longer than this are considered TTL-exceeded
# and marked 'failed' by recover_ttl_exceeded_uows() at heartbeat startup.
TTL_EXCEEDED_HOURS: int = 4


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
    register: str = "operational"


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


# ---------------------------------------------------------------------------
# Trace helpers — V3 corrective trace contract
# ---------------------------------------------------------------------------

def _trace_json_path(output_ref: str) -> Path:
    """
    Derive trace.json path from output_ref.

    Mirrors _result_json_path: replace extension (foo.json → foo.trace.json).
    Fallback: append .trace.json when output_ref has no extension.
    """
    p = Path(output_ref)
    if p.suffix:
        return p.with_suffix(".trace.json")
    return Path(output_ref + ".trace.json")


def _build_trace(
    uow_id: str,
    register: str,
    outcome: ExecutorOutcome,
    execution_summary: str,
    surprises: list[str] | None = None,
    prescription_delta: str = "",
    gate_score: dict | None = None,
) -> dict:
    """
    Pure constructor for the V3 trace dict.

    All fields required; surprises defaults to [] (not None) per schema contract.
    gate_score defaults to None for PR A — iterative-convergent gate scoring is PR B.
    """
    return {
        "uow_id": uow_id,
        "register": register,
        "execution_summary": execution_summary,
        "surprises": surprises or [],
        "prescription_delta": prescription_delta,
        "gate_score": gate_score,
        "timestamp": _now_iso(),
    }


def _write_trace_json(output_ref: str, trace: dict) -> None:
    """
    Write trace.json atomically (tmp → rename). Creates parent dir if needed.

    Mirrors _write_result_json but uses tmp→rename for atomicity, consistent
    with the _dispatch_via_inbox pattern. The Steward reads this file at
    diagnosis time; partial writes must not be visible.
    """
    trace_path = _trace_json_path(output_ref)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = trace_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(trace, indent=2))
    tmp_path.rename(trace_path)


def _insert_corrective_trace(registry_db_path: Path, trace: dict) -> None:
    """
    Best-effort INSERT into corrective_traces table.

    Does not raise on failure — logs a warning and returns. This matches the
    V3 non-blocking contract: trace absence is logged as a contract violation
    but does not block Steward re-entry.
    """
    try:
        conn = sqlite3.connect(str(registry_db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT INTO corrective_traces
                (uow_id, register, execution_summary, surprises, prescription_delta, gate_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace["uow_id"],
                trace["register"],
                trace["execution_summary"],
                json.dumps(trace.get("surprises") or []),
                trace.get("prescription_delta") or "",
                json.dumps(trace.get("gate_score")) if trace.get("gate_score") else None,
                trace.get("timestamp", _now_iso()),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(
            "Executor: failed to insert corrective_trace for %s — %s",
            trace.get("uow_id"),
            e,
        )


def _write_output_ref_content(output_ref: str, content: str) -> None:
    """Write text content to the output_ref path. Creates parent dir if needed."""
    p = Path(output_ref)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _validate_result_json_written(uow_id: str, output_ref: str) -> None:
    """
    Warn if result.json was not written at an intentional exit point.

    This is a contract violation guard (executor-contract.md): every intentional
    exit (complete, partial, failed, blocked) must produce a result file. This
    function logs a WARNING — it does not raise — so the UoW transition still
    proceeds. The Steward will detect the missing file and surface to Dan if needed.

    Called after every _write_result_json() call at intentional exit points.
    """
    result_path = _result_json_path(output_ref)
    if not result_path.exists():
        log.warning(
            "Executor contract violation: result.json not found after write for UoW %s "
            "(expected: %s). Steward will be unable to assess completion deterministically. "
            "See docs/executor-contract.md.",
            uow_id,
            result_path,
        )


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
            dispatcher: Callable that dispatches the LLM subagent task. When None
                (the default), the Executor uses the register-appropriate dispatch
                table (_resolve_dispatcher) to select a dispatcher based on the
                executor_type in the workflow artifact. When explicitly provided,
                it takes precedence over the dispatch table — this preserves
                backward compatibility for tests and CI environments.
                In production, executor-heartbeat.py passes _dispatch_via_claude_p
                to spawn a real functional-engineer subagent via `claude -p`.
        """
        self.registry = registry
        self._skill_activator = skill_activator or _noop_skill_activator
        # None sentinel: use dispatch table in _run_execution.
        # Non-None: caller-injected override — takes precedence over the table.
        self._dispatcher_override: SubagentDispatcher | None = dispatcher

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def execute_uow(self, uow_id: str) -> ExecutorResult:
        """
        Claim and execute a single UoW.

        Returns an ExecutorResult. The result.json and trace.json files are
        always written before this method returns (success or failure). On
        exception, both files are written with outcome='failed' before re-raising.
        """
        claim = self._claim(uow_id)

        match claim:
            case ClaimNotFound():
                # UoW does not exist in the registry.
                raise ValueError(f"Executor: UoW {uow_id!r} not found in registry")
            case ClaimRejected(reason=reason):
                # Optimistic lock failed or artifact missing — caller may retry or skip.
                raise RuntimeError(f"Executor: claim rejected for {uow_id!r} — {reason}")
            case ClaimSucceeded(uow_id=uid, output_ref=output_ref, artifact=artifact, register=register):
                return self._run_step_sequence(uid, output_ref, artifact, register)

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
            # Step 1: Read UoW from executor_uow_view — enforces read-path
            # isolation (no steward-private fields; only UoWs in
            # 'ready-for-executor' state are visible via the view).
            # The actual claim is the atomic step 2 UPDATE with WHERE guard
            # on uow_registry; this is a pre-flight read only.
            row = conn.execute(
                "SELECT * FROM executor_uow_view WHERE id = ?", (uow_id,)
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

            # Sentinel file — written immediately after the claim commits so
            # the output_ref path exists on disk while the subprocess runs.
            # This prevents the startup sweep from misclassifying a live
            # executor as `crashed_output_ref_missing` during the window
            # between commit and subprocess completion.
            # The subprocess (or _run_execution) overwrites this sentinel with
            # real content when done; the startup sweep reads only size, not
            # content, so a non-empty sentinel is classified `possibly_complete`
            # rather than `crashed_output_ref_missing`.
            try:
                _sentinel_path = Path(output_ref)
                _sentinel_path.parent.mkdir(parents=True, exist_ok=True)
                _sentinel_path.write_text("executor_claimed")
            except OSError as _e:
                log.warning(
                    "Executor: could not write sentinel to output_ref %s — %s. "
                    "Proceeding; startup sweep may misclassify if heartbeat fires "
                    "before subprocess completes.",
                    output_ref, _e,
                )

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
                null_trace = _build_trace(
                    uow_id=uow_id,
                    register=row["register"] if row["register"] else "operational",
                    outcome=ExecutorOutcome.FAILED,
                    execution_summary=null_reason,
                    surprises=[null_reason],
                    prescription_delta="workflow_artifact must be non-null before executor can run",
                )
                _write_trace_json(output_ref, null_trace)
                _insert_corrective_trace(self.registry.db_path, null_trace)
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
                    missing_trace = _build_trace(
                        uow_id=uow_id,
                        register=row["register"] if row["register"] else "operational",
                        outcome=ExecutorOutcome.FAILED,
                        execution_summary=missing_reason,
                        surprises=[missing_reason],
                        prescription_delta="workflow_artifact file path must exist on disk before executor can run",
                    )
                    _write_trace_json(output_ref, missing_trace)
                    _insert_corrective_trace(self.registry.db_path, missing_trace)
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
                deser_trace = _build_trace(
                    uow_id=uow_id,
                    register=row["register"] if row["register"] else "operational",
                    outcome=ExecutorOutcome.FAILED,
                    execution_summary=deser_reason,
                    surprises=[str(e)],
                    prescription_delta="workflow_artifact JSON must be valid and match WorkflowArtifact schema",
                )
                _write_trace_json(output_ref, deser_trace)
                _insert_corrective_trace(self.registry.db_path, deser_trace)
                self.registry.fail_uow(uow_id, deser_reason)
                return ClaimRejected(
                    uow_id=uow_id,
                    reason=f"workflow_artifact deserialization failed: {e}",
                )

            register = row["register"] if row["register"] else "operational"
            return ClaimSucceeded(uow_id=uow_id, output_ref=output_ref, artifact=artifact, register=register)

        except Exception:
            try:
                conn.rollback()
            except Exception as e:
                log.debug(
                    "Rollback failed during exception handling: %s: %s",
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
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
        register: str = "operational",
    ) -> ExecutorResult:
        """
        Execute a claimed UoW through its full step sequence.

        On any unhandled exception: write result.json and trace.json with
        outcome='failed', transition to 'failed' status via the Registry,
        then re-raise.
        """
        try:
            return self._run_execution(uow_id, output_ref, artifact, register)
        except Exception as exc:
            # Ensure result.json is always written, even on crash.
            # Use result_writer so the Steward gets a result file in the
            # standard subagent contract format (status/outcome/success/summary)
            # even when the Executor itself failed before dispatching the subagent.
            reason = f"{type(exc).__name__}: {exc}"
            _write_output_ref_content(output_ref, f"execution failed: {reason}")
            _write_subagent_result(
                output_ref,
                status="failed",
                summary=f"executor error before subagent dispatch: {reason}",
            )
            # V3: write trace.json alongside result.json at the crash exit path.
            # register defaults to "operational" — the crash handler has no access
            # to the register value if it was never passed in from ClaimSucceeded.
            trace = _build_trace(
                uow_id=uow_id,
                register=register,
                outcome=ExecutorOutcome.FAILED,
                execution_summary=f"Executor crashed: {type(exc).__name__}: {exc}",
                surprises=[str(exc)],
                prescription_delta="exception before subagent dispatch — check executor logs",
            )
            _write_trace_json(output_ref, trace)
            _insert_corrective_trace(self.registry.db_path, trace)
            self.registry.fail_uow(uow_id, reason)
            raise

    def _run_execution(
        self,
        uow_id: str,
        output_ref: str,
        artifact: WorkflowArtifact,
        register: str = "operational",
    ) -> ExecutorResult:
        """
        Inner execution: activate skills, dispatch subagent, write results.
        Returns ExecutorResult. Callers must catch exceptions.
        """
        # Step 1: Activate prescribed skills
        prescribed_skills: list[str] = artifact.get("prescribed_skills") or []
        for skill_id in prescribed_skills:
            self._skill_activator(skill_id)

        # Step 2: Dispatch LLM subagent via register-appropriate dispatcher.
        # If a dispatcher was explicitly injected (tests, CI), use it directly
        # with the raw instructions (no preamble prepended — caller's responsibility).
        # Otherwise, resolve from the dispatch table and prepend the register-appropriate
        # preamble from _EXECUTOR_TYPE_TO_PREAMBLE before passing to the dispatcher.
        raw_instructions = artifact["instructions"]
        if self._dispatcher_override is not None:
            executor_id = self._dispatcher_override(raw_instructions, uow_id)
        else:
            executor_type = artifact.get("executor_type", "functional-engineer")
            preamble = _EXECUTOR_TYPE_TO_PREAMBLE.get(executor_type, "")
            instructions = preamble + raw_instructions
            dispatcher = _resolve_dispatcher(executor_type)
            executor_id = dispatcher(instructions, uow_id)

        # Step 3: Write output_ref content (signal that execution produced output)
        _write_output_ref_content(output_ref, f"execution complete: task dispatched as {executor_id}")

        # Step 4: Build result
        result = ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.COMPLETE,
            success=True,
            executor_id=executor_id or None,
        )

        # Step 5: Write result.json (executor-contract.md: required at every intentional exit)
        _write_result_json(output_ref, result)
        _validate_result_json_written(uow_id, output_ref)

        # Step 5b: Write trace.json (V3 corrective trace contract — required alongside result.json)
        trace = _build_trace(
            uow_id=uow_id,
            register=register,
            outcome=ExecutorOutcome.COMPLETE,
            execution_summary=f"Executor dispatched subagent {executor_id}, subprocess exit 0.",
            surprises=[],
            prescription_delta="",
            gate_score=None,  # gate_score enrichment is subagent-level; always null from executor
        )
        _write_trace_json(output_ref, trace)
        _insert_corrective_trace(self.registry.db_path, trace)

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
        _validate_result_json_written(uow_id, output_ref)

        # V3: write trace.json alongside result.json.
        # register is not available here without refactoring the public API; use "operational"
        # as the default for PR A. The field will be enriched in a later PR.
        steps_desc = (
            f"partial completion — {steps_completed}/{steps_total} steps done"
            if steps_completed is not None
            else "partial completion"
        )
        trace = _build_trace(
            uow_id=uow_id,
            register="operational",
            outcome=ExecutorOutcome.PARTIAL,
            execution_summary=reason,
            surprises=[reason],
            prescription_delta=steps_desc,
        )
        _write_trace_json(output_ref, trace)
        _insert_corrective_trace(self.registry.db_path, trace)

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
        _validate_result_json_written(uow_id, output_ref)

        # V3: write trace.json alongside result.json.
        # register is not available here without refactoring the public API; use "operational"
        # as the default for PR A. The field will be enriched in a later PR.
        trace = _build_trace(
            uow_id=uow_id,
            register="operational",
            outcome=ExecutorOutcome.BLOCKED,
            execution_summary=reason,
            surprises=[reason],
            prescription_delta="blocked — external resolution required before re-prescription",
        )
        _write_trace_json(output_ref, trace)
        _insert_corrective_trace(self.registry.db_path, trace)

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
# Production dispatcher — functional-engineer agent via claude -p
# ---------------------------------------------------------------------------

#: Timeout for the claude -p subprocess in seconds. Matched to the default
#: UoW estimated_runtime ceiling (30 minutes) plus a generous buffer.
#: Uses centralized TimeoutConfig instead of direct env read.
def _get_claude_p_timeout() -> int:
    """Return the claude -p subprocess timeout in seconds."""
    return TimeoutConfig.claude_dispatch_timeout_secs()

#: claude binary — resolved from PATH at call time so tests can override via
#: a mock binary on PATH without patching the module.
_CLAUDE_BIN = "claude"

#: Functional-engineer agent prompt preamble — sets context before the
#: prescription body. The subagent receives the full prescription as the
#: prompt body and is responsible for reading the issue, implementing,
#: opening a PR, and calling write_result.
_FUNCTIONAL_ENGINEER_PREAMBLE = """\
You are a functional-engineer subagent operating inside the WOS (Work Orchestration
System) pipeline. Your job is to implement the following prescription and open a PR.

Follow the functional-engineer protocol:
1. Read the GitHub issue identified in the prescription (use gh issue view).
2. Create a worktree branch and implement the changes.
3. Run tests, then open a PR on the repo identified in the prescription's issue URL.
4. Call write_result with the PR URL and outcome when done.

Do NOT call send_reply. Do NOT call wait_for_messages.
Write result via: mcp__lobster-inbox__write_result

Prescription:
"""


def _dispatch_via_claude_p(instructions: str, uow_id: str) -> str:
    """
    Production dispatcher: spawn a functional-engineer subagent via `claude -p`.

    Launches a synchronous subprocess with the instructions as-is (preamble is
    prepended by _run_execution before this function is called — see
    _EXECUTOR_TYPE_TO_PREAMBLE). The executor blocks until the subprocess exits,
    then inspects the return code to determine success or failure.

    Returns a run_id string for audit correlation (uow_id + timestamp).

    Raises subprocess.CalledProcessError on non-zero exit — the caller's
    exception handler writes result.json with outcome=failed and transitions
    the UoW to 'failed' so the Steward can re-diagnose.
    Raises subprocess.TimeoutExpired if the agent exceeds _CLAUDE_P_TIMEOUT_SECONDS
    — same failure path applies.
    Raises FileNotFoundError if the claude binary is not on PATH — caught by
    the caller's exception handler.
    """
    run_id = f"{uow_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    prompt = instructions

    command = [
        _CLAUDE_BIN,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--max-turns", "40",
    ]

    # Use error capture to detect and log subprocess failures with context
    proc, error = run_subprocess_with_error_capture(
        component="executor",
        uow_id=uow_id,
        command=command,
        timeout_seconds=_get_claude_p_timeout(),
        check=True,  # Log errors at ERROR level for fatal issues
    )

    # If error occurred, classify and decide whether to raise
    if error:
        classification = classify_error(error)
        log.error(
            "Executor(%s): %s dispatch failed — %s (fatal=%s)",
            uow_id, classification.classification, error.summary(), classification.is_fatal,
        )

        # Check for repeated failures (same error 3+ times in 5 min)
        if has_repeated_error("executor", uow_id, str(error.error_type), threshold=3):
            log.critical(
                "Executor(%s): repeated %s errors detected — manual intervention likely needed",
                uow_id, error.error_type,
            )

        # Re-raise for the caller to catch and mark UoW as failed
        raise subprocess.CalledProcessError(
            error.exit_code or 1,
            error.command,
            stderr=error.stderr,
            stdout=error.stdout,
        )

    return run_id


# ---------------------------------------------------------------------------
# Register-appropriate dispatchers — PR B (executor_type dispatch table)
# ---------------------------------------------------------------------------

#: Frontier-writer agent prompt preamble.
#: Used for philosophical-register UoWs that require phenomenological synthesis
#: output rather than code implementation. Does NOT open a PR — writes to
#: output_ref and writes trace.json for the Steward's re-entry loop.
_FRONTIER_WRITER_PREAMBLE = """\
You are a frontier-writer subagent operating inside the WOS (Work Orchestration
System) pipeline. Your job is to produce a phenomenological synthesis output
for the following prescription.

Follow the frontier-writer protocol:
1. Read the prescription carefully — this is a philosophical or creative UoW,
   not an implementation task. Do NOT open a GitHub PR.
2. Write your synthesis output to the output_ref path specified in the prescription.
3. Write trace.json alongside the output with your execution_summary and any
   surprises or prescription_delta observations.
4. Call write_result with outcome and a brief summary when done.

Do NOT call send_reply. Do NOT call wait_for_messages.
Do NOT open a GitHub PR or create a branch — this is a synthesis task.
Write result via: mcp__lobster-inbox__write_result

Prescription:
"""

#: Design-review agent prompt preamble.
#: Used for human-judgment-register UoWs that require structured analysis
#: for Dan's review before the UoW can be closed. Does NOT open a PR.
_DESIGN_REVIEW_PREAMBLE = """\
You are a design-review subagent operating inside the WOS (Work Orchestration
System) pipeline. Your job is to produce a structured design analysis output
for the following prescription, for Dan's review.

Follow the design-review protocol:
1. Read the prescription carefully — this is a human-judgment UoW that requires
   structured analysis, not implementation. Do NOT open a GitHub PR.
2. Write a structured analysis to the output_ref path specified in the prescription.
   Include: context summary, options considered, recommendation, open questions for Dan.
3. Write trace.json alongside the output with your execution_summary.
4. Call write_result with outcome and a brief summary when done.
   The Steward will surface this to Dan for confirmation.

Do NOT call send_reply. Do NOT call wait_for_messages.
Do NOT open a GitHub PR or create a branch — this is a design review task.
Write result via: mcp__lobster-inbox__write_result

Prescription:
"""

#: Preamble map: executor_type → preamble string to prepend before calling dispatcher.
#: _run_execution looks up the preamble here and prepends it to the prescription
#: instructions before passing to the dispatcher. This keeps dispatchers stateless
#: (they receive a fully-formed prompt) and makes preamble selection testable
#: via monkeypatching — the dispatcher can be replaced with a stub that checks
#: what instructions it received.
#: executor_type values not in this map get no preamble (safe default).
_EXECUTOR_TYPE_TO_PREAMBLE: dict[str, str] = {
    "functional-engineer": _FUNCTIONAL_ENGINEER_PREAMBLE,
    "lobster-ops": _FUNCTIONAL_ENGINEER_PREAMBLE,
    "general": _FUNCTIONAL_ENGINEER_PREAMBLE,
    "frontier-writer": _FRONTIER_WRITER_PREAMBLE,
    "design-review": _DESIGN_REVIEW_PREAMBLE,
}


def _dispatch_via_frontier_writer(instructions: str, uow_id: str) -> str:
    """
    Dispatcher for philosophical-register UoWs.

    This sprint: same subprocess mechanism as _dispatch_via_claude_p.
    The register-appropriate preamble is prepended by _run_execution before
    this function is called. Full semantic distinction (different model,
    different output format) is a later sprint.
    """
    run_id = f"{uow_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    command = [
        _CLAUDE_BIN,
        "-p", instructions,
        "--dangerously-skip-permissions",
        "--max-turns", "40",
    ]

    proc, error = run_subprocess_with_error_capture(
        component="executor",
        uow_id=uow_id,
        command=command,
        timeout_seconds=_get_claude_p_timeout(),
        check=True,
    )

    if error:
        classification = classify_error(error)
        log.error(
            "Executor(%s): frontier-writer dispatch failed — %s (fatal=%s)",
            uow_id, error.summary(), classification.is_fatal,
        )
        if has_repeated_error("executor", uow_id, str(error.error_type), threshold=3):
            log.critical(
                "Executor(%s): repeated %s errors in frontier-writer — manual intervention likely needed",
                uow_id, error.error_type,
            )
        raise subprocess.CalledProcessError(
            error.exit_code or 1,
            error.command,
            stderr=error.stderr,
            stdout=error.stdout,
        )

    return run_id


def _dispatch_via_design_review(instructions: str, uow_id: str) -> str:
    """
    Dispatcher for human-judgment-register UoWs.

    This sprint: same subprocess mechanism as _dispatch_via_claude_p.
    The register-appropriate preamble is prepended by _run_execution before
    this function is called. Full semantic distinction is a later sprint.
    """
    run_id = f"{uow_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    command = [
        _CLAUDE_BIN,
        "-p", instructions,
        "--dangerously-skip-permissions",
        "--max-turns", "40",
    ]

    proc, error = run_subprocess_with_error_capture(
        component="executor",
        uow_id=uow_id,
        command=command,
        timeout_seconds=_get_claude_p_timeout(),
        check=True,
    )

    if error:
        classification = classify_error(error)
        log.error(
            "Executor(%s): design-review dispatch failed — %s (fatal=%s)",
            uow_id, error.summary(), classification.is_fatal,
        )
        if has_repeated_error("executor", uow_id, str(error.error_type), threshold=3):
            log.critical(
                "Executor(%s): repeated %s errors in design-review — manual intervention likely needed",
                uow_id, error.error_type,
            )
        raise subprocess.CalledProcessError(
            error.exit_code or 1,
            error.command,
            stderr=error.stderr,
            stdout=error.stdout,
        )

    return run_id


#: Dispatch table mapping executor_type to the attribute name of its dispatcher
#: in this module. Values are strings so _resolve_dispatcher can look up the
#: CURRENT module attribute at call time — this ensures monkeypatching in tests
#: is respected (a captured function reference would bypass the patch).
#: functional-engineer, lobster-ops, and general all use _dispatch_via_claude_p
#: because they share the same execution mechanism (implement → PR).
#: frontier-writer and design-review use their own dispatchers as stubs for
#: future register-specific model/mechanism differentiation.
_EXECUTOR_TYPE_TO_DISPATCHER: dict[str, str] = {
    "functional-engineer": "_dispatch_via_claude_p",
    "lobster-ops": "_dispatch_via_claude_p",
    "general": "_dispatch_via_claude_p",
    "frontier-writer": "_dispatch_via_frontier_writer",
    "design-review": "_dispatch_via_design_review",
}


def _resolve_dispatcher(executor_type: str) -> SubagentDispatcher:
    """
    Dispatch table lookup: executor_type → SubagentDispatcher.

    Returns the dispatcher registered for executor_type, or falls back to
    _dispatch_via_claude_p for unknown types (safe default — operational UoWs
    always work, unknown register types get functional-engineer behavior
    until a specialized dispatcher is registered).

    Resolves via globals() at call time so that monkeypatching the module
    attribute in tests is respected — the dict stores attribute names, not
    captured function references.

    Called by _run_execution() when no dispatcher was explicitly injected
    via Executor.__init__.
    """
    name = _EXECUTOR_TYPE_TO_DISPATCHER.get(executor_type, "_dispatch_via_claude_p")
    return globals()[name]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Fallback dispatcher — inbox-based ghost message (dev / CI environments)
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
    Fallback dispatcher: write a wos_execute message to the Lobster inbox.

    This is the original ghost-message path retained for environments without
    a live functional-engineer (development, CI). No subprocess is spawned;
    the message is fire-and-forget. Use this by passing dispatcher=_dispatch_via_inbox
    to Executor(...) when a real claude -p execution is not desired.

    The Lobster dispatcher (main Claude loop) reads ~/messages/inbox/ on each
    cycle. When it sees a message with type='wos_execute', it spawns a
    background subagent via the Task tool with the prescribed instructions.

    The message_id is returned as the executor_id for audit correlation.

    Raises OSError if the inbox directory cannot be created or the message
    file cannot be written.
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
# TTL recovery — mark stuck 'active' UoWs as failed
# ---------------------------------------------------------------------------
# TODO: Remove after PR #584 merge (executor subprocess → inbox pattern).
# TTL recovery is a post-hoc band-aid for subprocess fragility. Once the executor
# uses the MCP inbox pattern instead of subprocess dispatch, long-lived UoWs will
# have natural heartbeat presence and won't need TTL-based recovery.

def recover_ttl_exceeded_uows(registry: "Registry") -> list[str]:
    """
    Scan for UoWs in 'active' state that have exceeded TTL_EXCEEDED_HOURS and
    transition them to 'failed' with return_reason='ttl_exceeded'.

    Call this at executor-heartbeat startup, before the dispatch cycle, so
    the Steward can re-diagnose stalled UoWs on its next pass.

    Returns the list of uow_ids that were recovered (may be empty).

    Design: uses optimistic lock on fail_uow — if another process already
    transitioned the UoW, the update silently skips (rowcount=0 path in
    fail_uow's WHERE clause). This is safe for concurrent heartbeat runs.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TTL_EXCEEDED_HOURS)
    cutoff_iso = cutoff.isoformat()

    conn = sqlite3.connect(str(registry.db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    try:
        rows = conn.execute(
            """
            SELECT id FROM uow_registry
            WHERE status = 'active'
              AND started_at IS NOT NULL
              AND started_at < ?
            """,
            (cutoff_iso,),
        ).fetchall()
    finally:
        conn.close()

    recovered: list[str] = []
    for row in rows:
        uow_id = row["id"]
        try:
            registry.fail_uow(
                uow_id,
                f"ttl_exceeded: UoW was in active state for more than {TTL_EXCEEDED_HOURS}h",
            )
            recovered.append(uow_id)
        except Exception as e:
            log.debug(
                "TTL recovery failed for UoW %s: %s: %s",
                uow_id,
                type(e).__name__,
                e,
                exc_info=True,
            )
            # Non-fatal: the UoW remains active and will be caught on the next heartbeat cycle.

    return recovered


# ---------------------------------------------------------------------------
# No-op implementations — for tests and environments without a live inbox
# ---------------------------------------------------------------------------

def _noop_skill_activator(skill_id: str) -> None:
    """No-op skill activator. In production, activate_skill MCP is called instead."""
    pass


def _noop_dispatcher(instructions: str, uow_id: str) -> str:
    """No-op dispatcher for tests. In production, _dispatch_via_claude_p is used."""
    return f"dispatched:{uow_id}"
