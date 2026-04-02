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
- Default: Executor(...) defaults to _dispatch_via_inbox for backward
  compatibility (tests, CI, development). The heartbeat explicitly passes
  _dispatch_via_claude_p to enable real agent dispatch. Both are public and
  injectable — callers choose the dispatch strategy.

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
            dispatcher: Callable that dispatches the LLM subagent task. Defaults
                to _dispatch_via_inbox (writes a wos_execute ghost message) for
                backward compatibility. In production, executor-heartbeat.py passes
                _dispatch_via_claude_p to spawn a real functional-engineer subagent
                via `claude -p`. Injectable for tests.
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
            except Exception as e:
                logger.debug(
                    f"Rollback failed during exception handling: {type(e).__name__}: {e}",
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
    ) -> ExecutorResult:
        """
        Execute a claimed UoW through its full step sequence.

        On any unhandled exception: write result.json with outcome='failed',
        transition to 'failed' status via the Registry, then re-raise.
        """
        try:
            return self._run_execution(uow_id, output_ref, artifact)
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

        # Step 5: Write result.json (executor-contract.md: required at every intentional exit)
        _write_result_json(output_ref, result)
        _validate_result_json_written(uow_id, output_ref)

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
_CLAUDE_P_TIMEOUT_SECONDS: int = int(os.environ.get("WOS_EXECUTOR_TIMEOUT", "7200"))

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

    Builds a prompt from the prescription instructions and launches a
    synchronous subprocess. The executor blocks until the subprocess exits,
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
    prompt = _FUNCTIONAL_ENGINEER_PREAMBLE + instructions

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
        timeout_seconds=_CLAUDE_P_TIMEOUT_SECONDS,
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
            logger.debug(
                f"TTL recovery failed for UoW {uow_id}: {type(e).__name__}: {e}",
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
