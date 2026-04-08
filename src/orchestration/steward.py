"""
Steward — WOS core diagnosis and prescription engine.

The Steward runs every 3 minutes (via steward-heartbeat.py). On each
invocation it processes all `ready-for-steward` UoWs through the
diagnosis→prescribe/close/surface cycle.

Design constraints enforced here:
- Audit-before-transition: every state change writes an audit entry BEFORE
  the transition. If the audit write fails, the transition does not happen.
- Optimistic lock: `UPDATE ... WHERE status = 'ready-for-steward'` checks
  rows affected. If 0, another Steward instance claimed it — skip silently.
- BOOTUP_CANDIDATE_GATE: when True, UoWs whose GitHub issue carries the
  `bootup-candidate` label are skipped. Default is True until the WOS
  validation sequence passes.
- Dry-run mode: diagnose without writing artifacts or transitioning state.
- All DB writes through Registry methods or direct connection (steward-private
  fields are written directly since they are not exposed via Registry's public
  API — this is intentional; the Steward is the sole writer of those fields).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.orchestration.registry import UoW
from src.orchestration.error_capture import (
    run_subprocess_with_error_capture,
    log_subprocess_error,
    classify_error,
    has_repeated_error,
)
from src.orchestration.config import TimeoutConfig

log = logging.getLogger("steward")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class LLMPrescriptionError(Exception):
    """Raised when LLM prescription fails and no fallback is permitted."""
    pass

# ---------------------------------------------------------------------------
# LLM prescription dispatch
# ---------------------------------------------------------------------------

def _get_llm_prescription_timeout() -> int:
    """Return the LLM prescription timeout in seconds.

    Uses centralized TimeoutConfig to read LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS
    from the environment. Falls back to default (600s) if absent or non-integer.
    Pure function with respect to state — env reads are isolated here so the
    rest of the module stays deterministic in tests (monkeypatch os.environ as needed).
    """
    return TimeoutConfig.llm_prescription_timeout_secs()


def _get_prescription_model() -> str:
    """Return the model to use for prescription dispatch.

    Resolves in order of precedence:
    1. LOBSTER_PRESCRIPTION_MODEL environment variable
    2. prescription_model field in wos-config.json
    3. Default: "opus"

    Supports budget flexibility by allowing non-Sonnet models (e.g., haiku)
    when available.
    """
    # Check environment variable first
    env_model = os.environ.get("LOBSTER_PRESCRIPTION_MODEL")
    if env_model:
        return env_model.strip()

    # Check wos-config.json
    try:
        from src.orchestration.dispatcher_handlers import read_wos_config
        config = read_wos_config()
        if "prescription_model" in config and config["prescription_model"]:
            return config["prescription_model"].strip()
    except Exception:
        # If config read fails, continue to default
        pass

    # Default fallback
    return "opus"

# claude binary — resolved from PATH at call time.
_CLAUDE_BIN = "claude"

# Number of consecutive LLM prescription fallbacks that trigger an early-warning
# inbox message.  Each cycle that falls back to deterministic increments the
# consecutive count.  A successful LLM call resets it to zero.
_LLM_FALLBACK_WARNING_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Status enum (golden pattern: StrEnum so values serialize as plain strings)
# ---------------------------------------------------------------------------

class UoWStatus(StrEnum):
    PROPOSED = "proposed"
    PENDING = "pending"
    READY_FOR_STEWARD = "ready-for-steward"
    DIAGNOSING = "diagnosing"
    READY_FOR_EXECUTOR = "ready-for-executor"
    ACTIVE = "active"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    EXPIRED = "expired"

    def is_terminal(self) -> bool:
        return self in {UoWStatus.DONE, UoWStatus.FAILED, UoWStatus.EXPIRED}

    def is_in_flight(self) -> bool:
        return self in {UoWStatus.ACTIVE, UoWStatus.READY_FOR_EXECUTOR, UoWStatus.DIAGNOSING}


# ---------------------------------------------------------------------------
# Named outcome types (golden pattern: typed return contract for _process_uow)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prescribed:
    uow_id: str
    cycles: int


@dataclass(frozen=True)
class Done:
    uow_id: str


@dataclass(frozen=True)
class Surfaced:
    uow_id: str
    condition: str


@dataclass(frozen=True)
class RaceSkipped:
    uow_id: str


StewardOutcome = Prescribed | Done | Surfaced | RaceSkipped


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Path to the file-flag that clears BOOTUP_CANDIDATE_GATE.
# When this file exists, the gate is cleared and bootup-candidate UoWs are
# processed normally. Create via `/wos unblock` dispatcher command.
_GATE_CLEARED_FLAG: Path = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
) / "data" / "wos-gate-cleared"


def is_bootup_candidate_gate_active() -> bool:
    """Return True if BOOTUP_CANDIDATE_GATE is active (blocking bootup-candidates).

    Returns False when the wos-gate-cleared file flag exists, indicating the
    WOS validation sequence has passed and all UoWs should be processed.

    This function reads from disk on every call so that the gate state is always
    current — cron processes get a fresh read on every invocation.
    """
    return not _GATE_CLEARED_FLAG.exists()


# When True, the Steward skips UoWs with the `bootup-candidate` label.
# Evaluated at module load; re-evaluated on each cron process start.
# To clear: create ~/lobster-workspace/data/wos-gate-cleared (or /wos unblock).
BOOTUP_CANDIDATE_GATE: bool = is_bootup_candidate_gate_active()

# Status values — use UoWStatus StrEnum (kept as aliases for backward compat)
_STATUS_READY_FOR_STEWARD = UoWStatus.READY_FOR_STEWARD
_STATUS_DIAGNOSING = UoWStatus.DIAGNOSING
_STATUS_READY_FOR_EXECUTOR = UoWStatus.READY_FOR_EXECUTOR
_STATUS_DONE = UoWStatus.DONE
_STATUS_BLOCKED = UoWStatus.BLOCKED

# Actor identifier written to audit entries
_ACTOR_STEWARD = "steward"

# Hard cap: surface to Dan unconditionally if lifetime_cycles >= this value.
# lifetime_cycles accumulates across all decide-retry resets, so this is a true
# per-UoW-lifetime circuit breaker. steward_cycles (per-attempt) is NOT used here.
_HARD_CAP_CYCLES = 5

# Early warning threshold: notify Dan when steward_cycles reaches this value
_EARLY_WARNING_CYCLES = 4

# Crash surface threshold: surface if crashed_no_output and steward_cycles >= this value.
# Uses per-attempt steward_cycles (not lifetime_cycles) — crash detection is per-attempt.
_CRASH_SURFACE_CYCLES = 2

# Fields required by the Steward for operation
_STEWARD_REQUIRED_FIELDS = frozenset({
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

# Executor types
_EXECUTOR_TYPE_GENERAL = "general"
_EXECUTOR_TYPE_FUNCTIONAL_ENGINEER = "functional-engineer"
_EXECUTOR_TYPE_LOBSTER_OPS = "lobster-ops"

# Return reason classifications
_CLASSIFICATION_NORMAL = "normal"
_CLASSIFICATION_BLOCKED = "blocked"
_CLASSIFICATION_ABNORMAL = "abnormal"
_CLASSIFICATION_ERROR = "error"
_CLASSIFICATION_ORPHAN = "orphan"

_RETURN_REASON_CLASSIFICATIONS: dict[str, str] = {
    "observation_complete": _CLASSIFICATION_NORMAL,
    "needs_steward_review": _CLASSIFICATION_NORMAL,
    "blocked": _CLASSIFICATION_BLOCKED,
    "timeout": _CLASSIFICATION_ABNORMAL,
    "stall_detected": _CLASSIFICATION_ABNORMAL,
    "execution_failed": _CLASSIFICATION_ERROR,
    "crashed_no_output": _CLASSIFICATION_ERROR,
    "crashed_zero_bytes": _CLASSIFICATION_ERROR,
    "crashed_output_ref_missing": _CLASSIFICATION_ERROR,
    "executor_orphan": _CLASSIFICATION_ORPHAN,
    "diagnosing_orphan": _CLASSIFICATION_ORPHAN,
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify_return_reason(return_reason: str | None) -> str:
    """Map a return_reason string to its classification. Unknown → 'error' (conservative)."""
    if return_reason is None:
        return _CLASSIFICATION_NORMAL
    return _RETURN_REASON_CLASSIFICATIONS.get(return_reason, _CLASSIFICATION_ERROR)


# ---------------------------------------------------------------------------
# Per-cycle steward trace logging
# ---------------------------------------------------------------------------

_CYCLE_TRACE_EXCERPT_MAX = 200
_DEFAULT_CYCLE_TRACE_DIR = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
) / "orchestration" / "artifacts"


def _append_cycle_trace(
    uow_id: str,
    cycle_num: int,
    subagent_excerpt: str,
    return_reason: str,
    next_action: str,
    artifact_dir: Path | None = None,
) -> None:
    """Append one JSONL entry to <artifact_dir>/<uow_id>.cycles.jsonl.

    Each entry records the outcome of a single steward cycle, enabling
    post-hoc debugging of multi-cycle UoW lifecycles.

    Args:
        uow_id: The UoW identifier.
        cycle_num: The current steward_cycles value (pre-increment).
        subagent_excerpt: Text from the executor output (output_ref), truncated
            to _CYCLE_TRACE_EXCERPT_MAX chars with a trailing ellipsis if longer.
        return_reason: The return_reason from diagnosis, or empty string.
        next_action: One of 'prescribed', 'done', 'surfaced', 'stuck'.
        artifact_dir: Override for the artifact directory. Defaults to
            ~/lobster-workspace/orchestration/artifacts.
    """
    resolved_dir = Path(artifact_dir) if artifact_dir is not None else _DEFAULT_CYCLE_TRACE_DIR
    resolved_dir.mkdir(parents=True, exist_ok=True)

    # Truncate excerpt with ellipsis if it exceeds the max length
    excerpt = subagent_excerpt
    if len(excerpt) > _CYCLE_TRACE_EXCERPT_MAX:
        excerpt = excerpt[:_CYCLE_TRACE_EXCERPT_MAX] + "\u2026"

    entry = {
        "cycle_num": cycle_num,
        "subagent_excerpt": excerpt,
        "return_reason": return_reason,
        "next_action": next_action,
        "timestamp": _now_iso(),
    }

    trace_path = resolved_dir / f"{uow_id}.cycles.jsonl"
    with trace_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _parse_audit_log(audit_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract structured entries from the audit_log rows passed in.
    Returns a list of audit entries (from the `note` field, JSON-parsed).
    """
    # Audit entries are passed as a list from the registry queries
    return audit_entries


def _most_recent_return_reason(audit_entries: list[dict]) -> str | None:
    """
    Extract the most recent return_reason from audit entries.
    Looks for the last audit_log entry with a `return_reason` key in its note.

    For `execution_complete` events: return `"execution_complete"` as the
    authoritative signal even when the note does not carry an explicit
    `return_reason` or `classification`.  Formerly, the absence of those fields
    caused the function to fall through and pick up the nearest prior
    `startup_sweep executor_orphan` entry — making the Steward treat a
    successful Executor dispatch as an orphan and re-prescribe indefinitely.
    """
    for entry in reversed(audit_entries):
        event = entry.get("event", "")
        note = entry.get("note")

        note_data: dict = {}
        if note:
            try:
                note_data = json.loads(note)
            except (json.JSONDecodeError, TypeError):
                pass

        # Explicit return_reason in note always wins regardless of event type.
        if "return_reason" in note_data:
            return note_data["return_reason"]

        # Event-type defaults: return the canonical reason for each terminal event.
        if event == "execution_complete":
            # Authoritative: Executor successfully dispatched.  Return immediately
            # so older startup_sweep entries cannot mask this completion.
            return "execution_complete"
        elif event == "startup_sweep":
            clf = note_data.get("classification")
            if clf:
                return clf
        elif event == "execution_failed":
            clf = note_data.get("return_reason") or note_data.get("classification")
            if clf:
                return clf

    return None


def _most_recent_classification(audit_entries: list[dict]) -> str | None:
    """
    Extract the most recent startup_sweep classification from audit entries.
    Returns the classification value (e.g. 'crashed_no_output') or None.
    """
    for entry in reversed(audit_entries):
        event = entry.get("event", "")
        if event == "startup_sweep":
            note = entry.get("note")
            if note:
                try:
                    data = json.loads(note)
                    return data.get("classification")
                except (json.JSONDecodeError, TypeError):
                    pass
    return None


def _output_ref_is_valid(output_ref: str | None) -> bool:
    """Return True if output_ref is a path to a non-empty file."""
    if not output_ref:
        return False
    try:
        p = Path(output_ref)
        return p.exists() and p.stat().st_size > 0
    except Exception as e:
        log.debug(f"Error checking output_ref {output_ref}: {type(e).__name__}: {e}", exc_info=True)
        return False


def _read_output_ref(output_ref: str | None) -> str:
    """Read and return output_ref file contents, or empty string."""
    if not output_ref:
        return ""
    try:
        return Path(output_ref).read_text(encoding="utf-8")
    except Exception as e:
        log.debug(f"Error reading output_ref {output_ref}: {type(e).__name__}: {e}", exc_info=True)
        return ""


def _determine_reentry_posture(
    audit_entries: list[dict],
    return_reason: str | None,
) -> str:
    """
    Determine the re-entry posture based on most recent audit event.

    Returns a string label:
    - 'execution_complete': normal re-entry
    - 'stall_detected': observation loop surfaced a timeout
    - 'startup_sweep_possibly_complete': crash recovery, partial output
    - 'crashed_no_output': crash with no usable output
    - 'execution_failed': executor failure
    - 'executor_orphan': executor never ran
    - 'first_execution': no prior execution cycle (steward_cycles == 0)
    """
    if not audit_entries:
        return "first_execution"

    if return_reason == "executor_orphan":
        return "executor_orphan"

    classification = _RETURN_REASON_CLASSIFICATIONS.get(return_reason or "", None)

    if classification == _CLASSIFICATION_NORMAL:
        return "execution_complete"
    elif classification == _CLASSIFICATION_ABNORMAL:
        return "stall_detected"
    elif classification == _CLASSIFICATION_ERROR:
        if return_reason == "crashed_no_output":
            return "crashed_no_output"
        return "execution_failed"
    elif classification == _CLASSIFICATION_ORPHAN:
        return "executor_orphan"
    else:
        # Fall back to audit event inspection
        for entry in reversed(audit_entries):
            event = entry.get("event", "")
            if event == "execution_complete":
                return "execution_complete"
            elif event == "stall_detected":
                return "stall_detected"
            elif event == "startup_sweep":
                note = entry.get("note", "")
                try:
                    data = json.loads(note) if note else {}
                except (json.JSONDecodeError, TypeError):
                    data = {}
                clf = data.get("classification", "")
                if clf == "possibly_complete":
                    return "startup_sweep_possibly_complete"
                elif clf in ("crashed_no_output", "crashed_zero_bytes", "crashed_output_ref_missing"):
                    return clf
                elif clf == "executor_orphan":
                    return "executor_orphan"
                elif clf == "diagnosing_orphan":
                    return "diagnosing_orphan"
            elif event == "execution_failed":
                return "execution_failed"

    return "first_execution"


def _assess_completion(
    uow: UoW,
    output_content: str,
    reentry_posture: str,
) -> tuple[bool, str, str | None]:
    """
    Assess whether the UoW output satisfies the original intent (Seed).

    Returns (is_complete: bool, rationale: str, executor_outcome: str | None).

    executor_outcome is the `outcome` field from the result file when found
    (e.g. "complete", "partial", "failed", "blocked"), or None when no valid
    result file was found. Callers must check executor_outcome == "blocked"
    to route immediately to Dan — the is_complete flag does not encode this.

    Completion requires ALL of:
    - output_ref is not NULL and file exists and is non-empty
    - Most recent execution cycle had execution_complete (not stall/crash)
    - Output content confirms original intent is addressed
    - lifetime_cycles < HARD_CAP_CYCLES
    """
    cycles = uow.lifetime_cycles
    if cycles >= _HARD_CAP_CYCLES:
        return False, f"hard_cap: lifetime_cycles={cycles} >= {_HARD_CAP_CYCLES}", None

    if reentry_posture == "first_execution":
        return False, "first_execution: awaiting executor dispatch", None

    output_ref = uow.output_ref
    if not _output_ref_is_valid(output_ref):
        return False, "output_ref is null or file does not exist or is empty", None

    if reentry_posture not in ("execution_complete", "startup_sweep_possibly_complete"):
        return False, f"re-entry posture is {reentry_posture!r} — not a normal completion", None

    if not output_content.strip():
        return False, "output file is empty", None

    # Deterministic completion check: look for a structured result file.
    # The Executor is expected to write `{output_ref}.result.json` with the
    # `outcome` field as the primary routing signal (executor-contract.md §Schema).
    # `success` is a backward-compat convenience field; `outcome` is always read first.
    output_ref = uow.output_ref
    if output_ref:
        result_file = Path(output_ref).with_suffix(".result.json")
        if not result_file.exists():
            # Also check the alternate naming convention: append .result.json suffix
            result_file_alt = Path(str(output_ref) + ".result.json")
            if result_file_alt.exists():
                result_file = result_file_alt
        if result_file.exists():
            try:
                result_data = json.loads(result_file.read_text(encoding="utf-8"))

                # Gap 2 (executor-contract.md): validate uow_id BEFORE reading any
                # other field. A misrouted result file must be treated as absence.
                result_uow_id = result_data.get("uow_id")
                if result_uow_id is not None and result_uow_id != uow.id:
                    log.warning(
                        "Result file %s has uow_id=%r but expected %r — "
                        "treating as absent (misrouted result file)",
                        result_file, result_uow_id, uow.id,
                    )
                    # Fall through to the no-result-file path below
                else:
                    # Gap 1 (executor-contract.md): `outcome` is the primary routing
                    # signal. Read it first; `success` is a backward-compat fallback.
                    outcome = result_data.get("outcome")
                    reason = result_data.get("reason", "no reason provided")

                    if outcome == "complete":
                        # PR C: Apply register-aware completion policy before closing.
                        policy = _register_completion_policy(uow.register)
                        if policy == "always-surface":
                            # philosophical: always surface to Dan — completion requires
                            # human judgment regardless of what result.json says.
                            return (
                                False,
                                f"register=philosophical: completion requires human judgment — "
                                f"surfacing to Dan (outcome={outcome})",
                                "philosophical_surface",
                            )
                        elif policy == "require-confirmation":
                            # human-judgment: requires Dan's explicit close_reason.
                            if uow.close_reason:
                                return True, f"outcome=complete: {result_file.name} (Dan confirmed)", "complete"
                            return (
                                False,
                                "register=human-judgment: awaiting Dan's explicit confirmation (close_reason not set)",
                                "human_judgment_pending",
                            )
                        # machine-gate (operational, iterative-convergent): fall through
                        return True, f"outcome=complete: {result_file.name}", "complete"
                    elif outcome == "blocked":
                        # Gap 3: `blocked` always routes to Dan — the Executor has
                        # determined that external resolution is required.
                        # Return is_complete=False so the normal prescription path
                        # is skipped; the caller must check executor_outcome for routing.
                        return False, f"outcome=blocked: {reason}", "blocked"
                    elif outcome in ("partial", "failed"):
                        return False, f"outcome={outcome}: {reason}", outcome
                    elif outcome is not None:
                        # Unknown outcome value — conservative non-completion
                        log.warning(
                            "Result file %s has unknown outcome=%r — treating as non-completion",
                            result_file, outcome,
                        )
                        return False, f"unknown outcome={outcome!r} in result file", outcome
                    else:
                        # No `outcome` field — fall back to `success` for backward
                        # compatibility with result files written before contract v1.
                        if result_data.get("success") is True:
                            return True, f"structured result file confirms success (legacy): {result_file.name}", None
                        elif result_data.get("success") is False:
                            return False, f"structured result file reports failure (legacy): {reason}", None
                        # If neither field is present, fall through to conservative check
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not parse result file %s: %s", result_file, e)

    # No structured result file found (or result file was invalid/misrouted).
    # Require a result file regardless of whether success_criteria is set.
    # A missing result.json means the Executor has not confirmed completion —
    # the subagent may have exited 0 without opening a PR or calling write_result.
    # Declaring done without evidence is the bug described in issue #648 Part B.
    success_criteria = uow.success_criteria
    if success_criteria:
        return False, (
            f"no structured result file ({output_ref}.result.json) found — "
            f"cannot verify success_criteria without Executor confirmation: {success_criteria[:80]}"
        ), None
    else:
        # Hard gate: require result.json even when success_criteria is NULL.
        # The legacy fallback (trust output_ref presence alone) is removed —
        # it could declare done when the subagent exited 0 without producing
        # any artifact (PR, write_result call, etc.). See issue #648 Part B.
        return False, (
            f"no structured result file ({output_ref}.result.json) found — "
            f"Executor confirmation required even when success_criteria is NULL: {uow.summary[:80]}"
        ), None


# ---------------------------------------------------------------------------
# Per-cycle trace entry builder (pure functions — no DB writes)
# ---------------------------------------------------------------------------

def _posture_rationale(diagnosis: dict) -> str:
    """
    Return a 1-sentence rationale for the current posture.

    Pure function: derives the string deterministically from diagnosis fields.
    No LLM, no DB reads. Called by _build_trace_entry().
    """
    posture = diagnosis.get("reentry_posture", "unknown")
    cycles = diagnosis.get("_cycles", 0)

    if posture == "first_execution":
        return "No prior audit entries — first steward contact, dispatching executor."
    elif posture == "execution_complete":
        return "Executor result file present and valid — assessing completion."
    elif posture == "crashed_output_ref_missing":
        return "Startup sweep detected missing output_ref — executor may have crashed."
    elif posture == "executor_orphan":
        return "UoW stuck in ready-for-executor beyond threshold — executor never claimed."
    elif posture == "diagnosing_orphan":
        return "Steward crashed mid-diagnosis — re-diagnosing from current state."
    elif posture == "steward_cycle_cap":
        return f"Steward cycle cap reached ({cycles} cycles) — surfacing to Dan."
    else:
        return f"Posture: {posture}."


def _extract_criteria_checks(diagnosis: dict) -> list[dict]:
    """
    Extract success criteria check results from the diagnosis dict.

    Pure function: maps is_complete + completion_rationale to a list of
    check dicts with {name, passed, evidence}. Always returns a list.
    """
    is_complete = diagnosis.get("is_complete", False)
    completion_rationale = diagnosis.get("completion_rationale", "")
    return [
        {
            "name": "completion_check",
            "passed": bool(is_complete),
            "evidence": completion_rationale,
        }
    ]


def _posture_prediction(diagnosis: dict) -> str | None:
    """
    Return a forward prediction string based on the diagnosis.

    Pure function: deterministic based on posture and completion state.
    Returns None only when there is genuinely nothing to predict (done).
    """
    posture = diagnosis.get("reentry_posture", "unknown")
    is_complete = diagnosis.get("is_complete", False)
    stuck_condition = diagnosis.get("stuck_condition")

    if stuck_condition:
        return "Will be surfaced to Dan — stuck condition detected."
    if is_complete:
        return "Closure will be declared — completion criteria satisfied."
    if posture == "first_execution":
        return "Executor will be dispatched for first execution pass."
    if posture == "execution_complete":
        return "Completion check will determine next action (prescribe or close)."
    if posture in ("crashed_no_output", "execution_failed"):
        return "Re-prescription will be issued after failure analysis."
    if posture == "executor_orphan":
        return "Re-prescription will be issued — executor never claimed UoW."
    return "Next prescription will be determined from diagnosis output."


def _build_trace_entry(diagnosis: dict, cycles: int) -> dict:
    """
    Build a single steward_agenda trace entry from a completed diagnosis.

    Pure function — no side effects, no DB writes. Called pre-branch in
    _process_uow() after diagnosis and before the stuck/done/prescribe split.

    Args:
        diagnosis: dict returned by _diagnose_uow().
        cycles: current uow.steward_cycles (pre-increment).

    Returns:
        Trace entry dict conforming to the v2 cycle trace entry schema.
    """
    # Inject cycles so _posture_rationale can access it for the cycle-cap case
    diagnosis_with_cycles = {**diagnosis, "_cycles": cycles}

    return {
        "cycle": cycles,
        "posture": diagnosis.get("reentry_posture", "unknown"),
        "posture_rationale": _posture_rationale(diagnosis_with_cycles),
        "success_criteria_checked": _extract_criteria_checks(diagnosis),
        "anomalies": (
            [diagnosis["stuck_condition"]]
            if diagnosis.get("stuck_condition")
            else []
        ),
        "prediction": _posture_prediction(diagnosis),
        "dispatch_instruction": None,  # filled in by prescribe branch if applicable
        "external_dependency": None,
        "discoveries": [],
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def _parse_steward_agenda(steward_agenda_str: str | None) -> list[dict]:
    """
    Parse steward_agenda JSON string into a list of dicts.

    Pure function. Returns [] on None, empty string, or parse failure.
    """
    if not steward_agenda_str:
        return []
    try:
        result = json.loads(steward_agenda_str)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _build_initial_agenda(uow: "UoW", issue_body: str) -> list[dict[str, Any]]:
    """
    Build the initial steward_agenda for a new UoW (steward_cycles == 0).

    Forecast depth calibrated by UoW type:
    - Well-defined (concrete deliverable): full agenda upfront
    - Open-ended (exploratory): 1-2 steps + 'pending evaluation' marker
    """
    summary = uow.summary
    success_criteria = uow.success_criteria or None

    # Heuristic: well-defined if success_criteria is present and summary is specific
    is_well_defined = bool(success_criteria and len(summary) > 20)

    if is_well_defined:
        return [
            {
                "posture": "solo",
                "context": f"Initial execution: {summary[:120]}",
                "constraints": [],
                "status": "pending",
            },
            {
                "posture": "verify",
                "context": "Steward verifies output against success_criteria",
                "constraints": [],
                "status": "pending",
            },
        ]
    else:
        return [
            {
                "posture": "explore",
                "context": f"Exploratory first step: {summary[:120]}",
                "constraints": [],
                "status": "pending",
            },
            {
                "posture": "pending_evaluation",
                "context": "pending evaluation — agenda will be updated after initial output",
                "constraints": [],
                "status": "pending",
            },
        ]


def _select_executor_type(uow: "UoW") -> str:
    """
    Select the executor type appropriate to the UoW's nature.

    Mapping:
    - GitHub issues about code bugs, features, or PRs → functional-engineer
    - Infrastructure or ops issues → lobster-ops
    - General or unclear → general

    Returns one of the _EXECUTOR_TYPE_* constants.
    """
    summary_lower = uow.summary.lower()
    source = (uow.source or "").lower()

    # Code / feature / bug signals checked first — these are strong indicators
    # that win over incidental ops-adjacent terms (e.g. "fix: setup script fails"
    # should route to functional-engineer, not lobster-ops).
    code_keywords = (
        "bug", "fix", "feat", "feature", "implement", "refactor", "test",
        "pr", "pull request", "issue", "error", "crash", "regression",
    )
    if any(kw in summary_lower for kw in code_keywords):
        return _EXECUTOR_TYPE_FUNCTIONAL_ENGINEER

    # Infrastructure / ops signals — checked after code keywords
    ops_keywords = (
        "install", "deploy", "cron", "systemd", "migration", "upgrade",
        "ops", "infra", "server", "config", "script", "setup", "lobster-ops",
    )
    if any(kw in summary_lower for kw in ops_keywords):
        return _EXECUTOR_TYPE_LOBSTER_OPS

    # Default: functional-engineer for anything sourced from a GitHub issue
    if "github:issue" in source:
        return _EXECUTOR_TYPE_FUNCTIONAL_ENGINEER

    return _EXECUTOR_TYPE_GENERAL


# Register → compatible executor types mapping (spec: Change 3).
# frontier-writer and design-review are V3 gated register names — they exist
# in the table to drive mismatch detection, but do not yet have dispatch
# implementations. When the mismatch gate fires for philosophical or
# human-judgment UoWs, the Steward surfaces to Dan for manual routing.
_REGISTER_COMPATIBLE_EXECUTORS: dict[str, frozenset[str]] = {
    "operational": frozenset({"functional-engineer", "lobster-ops", "general"}),
    "iterative-convergent": frozenset({"functional-engineer", "lobster-ops"}),
    "philosophical": frozenset({"frontier-writer"}),
    "human-judgment": frozenset({"design-review"}),
}


def _check_register_executor_compatibility(
    register: str,
    executor_type: str,
) -> tuple[bool, str]:
    """Check whether executor_type is compatible with a UoW's register.

    Pure function. Returns (is_compatible, reason).

    Compatible means the executor type is listed in the compatible set for
    the register. Unknown registers are treated as compatible with any executor
    (conservative — do not block unknown registers).

    Examples:
        ("philosophical", "functional-engineer") → (False, "philosophical→functional-engineer: ...")
        ("operational", "functional-engineer")   → (True, "")
    """
    compatible_types = _REGISTER_COMPATIBLE_EXECUTORS.get(register)
    if compatible_types is None:
        # Unknown register: allow through to avoid blocking unknown future registers
        return True, f"unknown register {register!r} — allowing through"

    if executor_type in compatible_types:
        return True, ""

    direction = f"{register}\u2192{executor_type}"
    reason = (
        f"register {register!r} is incompatible with executor_type {executor_type!r} "
        f"({direction}). Compatible types: {sorted(compatible_types)}"
    )
    return False, reason


def _select_prescribed_skills(uow: "UoW", reentry_posture: str) -> list[str]:
    """
    Select prescribed skills appropriate to the UoW type and posture.

    Returns a list of skill IDs.
    """
    summary = uow.summary.lower()
    skills = []

    if "bug" in summary or "fix" in summary or "error" in summary:
        skills.append("systematic-debugging")
    if "pr" in summary or "pull request" in summary or reentry_posture == "execution_complete":
        skills.append("verification-before-completion")
    if reentry_posture in ("crashed_no_output", "execution_failed"):
        if "systematic-debugging" not in skills:
            skills.append("systematic-debugging")

    return skills


def _extract_json_from_llm_output(raw_text: str) -> str:
    """Extract the JSON content from raw LLM output.

    Pure function. Handles three forms:
    1. Fenced block anywhere in the text (``` or ```json) — extracts content between
       the first opening fence and its closing fence.
    2. Plain JSON starting at the first '{' or '[' — returns the substring from
       that character to the end of the string.
    3. Text that is already bare JSON — returned as-is.

    Returns the extracted candidate string. The caller is responsible for
    calling json.loads and handling JSONDecodeError.
    """
    import re as _re

    # Strategy 1: find a fenced block (```json or ```) anywhere in the text
    fence_match = _re.search(r"```(?:json)?\s*\n(.*?)\n```", raw_text, _re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Strategy 2: find the first JSON object/array start character
    for i, ch in enumerate(raw_text):
        if ch in ("{", "["):
            return raw_text[i:].strip()

    # Strategy 3: return as-is (will likely fail json.loads — handled by caller)
    return raw_text


def _parse_workflow_artifact(raw_text: str) -> dict:
    """Parse a front-matter + prose prescription artifact.

    Pure function. Accepts text in the form:
        ---
        executor_type: functional-engineer
        estimated_cycles: 1
        success_criteria_check: Verify PR is open and tests pass
        ---

        <prose instructions here>

    Preamble prose before the opening --- is tolerated and discarded.
    This mirrors the robustness of the previous JSON extraction strategy
    and handles LLMs that add an introductory sentence before the artifact.

    Returns a dict with keys:
      - "executor_type": str (required — raises ValueError if absent)
      - "estimated_cycles": int (default 1)
      - "success_criteria_check": str (default "")
      - "instructions": str — the prose body after the closing ---

    Raises ValueError if executor_type is missing or no front-matter delimiter
    is found in the input.

    Implementation is deliberately dependency-free: no PyYAML, no regex,
    just line-by-line scanning so the parse contract is unambiguous.
    """
    text = raw_text.strip()
    if not text:
        raise ValueError("_parse_workflow_artifact: empty input")

    lines = text.splitlines()

    # Find the first --- delimiter (opening). Preamble prose before it is
    # tolerated so the parser is robust against LLM introductory sentences.
    opening_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            opening_idx = i
            break

    if opening_idx is None:
        raise ValueError(
            "_parse_workflow_artifact: no front-matter '---' delimiter found in input"
        )

    # Find the closing --- (first occurrence after the opening ---)
    closing_idx: int | None = None
    for i in range(opening_idx + 1, len(lines)):
        if lines[i].strip() == "---":
            closing_idx = i
            break

    # When no closing delimiter is found, treat everything after the opening
    # --- as front-matter (no prose body).
    if closing_idx is None:
        front_matter_lines = lines[opening_idx + 1:]
        prose_lines: list[str] = []
    else:
        front_matter_lines = lines[opening_idx + 1:closing_idx]
        prose_lines = lines[closing_idx + 1:]

    # Parse front-matter key: value pairs (no nested structures needed).
    front_matter: dict[str, str] = {}
    for line in front_matter_lines:
        if ":" not in line:
            continue
        parts = line.split(":", 1)
        key = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else ""
        front_matter[key] = value

    executor_type = front_matter.get("executor_type", "")
    if not executor_type:
        raise ValueError(
            "_parse_workflow_artifact: required field 'executor_type' is missing "
            "from front-matter"
        )

    raw_cycles = front_matter.get("estimated_cycles", "1")
    try:
        estimated_cycles = int(raw_cycles)
    except (TypeError, ValueError):
        estimated_cycles = 1

    success_criteria_check = front_matter.get("success_criteria_check", "")

    # Preserve the prose body exactly — strip only the leading blank line that
    # typically follows the closing --- delimiter.
    instructions = "\n".join(prose_lines).strip()

    return {
        "executor_type": executor_type,
        "estimated_cycles": estimated_cycles,
        "success_criteria_check": success_criteria_check,
        "instructions": instructions,
    }


def _llm_prescribe(
    uow: UoW,
    reentry_posture: str,
    completion_gap: str,
    issue_body: str = "",
) -> dict[str, Any] | None:
    """
    Call Claude to generate a tailored prescription for the given UoW.

    Dispatches via `claude -p` subprocess (the Lobster-standard LLM call path).
    No ANTHROPIC_API_KEY or anthropic SDK required — the claude CLI handles auth.

    Returns a dict with keys:
      - "instructions": str — full instruction block for the Executor
      - "success_criteria_check": str — how to verify completion
      - "estimated_cycles": int — expected execution passes needed

    Returns None if the subprocess fails, times out, or returns unparseable output.
    The caller must fall back to the deterministic template on None.

    This function is a pure side-effect boundary: the only observable effect
    is the claude -p subprocess call. All inputs are immutable value types.
    """
    # Build prior prescription summary from steward_log if available
    prior_prescriptions: list[str] = []
    if uow.steward_log:
        try:
            for line in uow.steward_log.strip().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    continue
                event = entry.get("event", "")
                if event in ("prescription", "reentry_prescription"):
                    assessment = entry.get("completion_assessment", "")
                    cycle = entry.get("steward_cycles", "?")
                    if assessment:
                        prior_prescriptions.append(
                            f"  - Cycle {cycle}: {assessment}"
                        )
        except (json.JSONDecodeError, KeyError):
            pass

    # Build the context block for the prompt
    context_parts: list[str] = [
        f"UoW ID: {uow.id}",
        f"Summary: {uow.summary}",
        f"Type: {uow.type}",
    ]

    if uow.success_criteria:
        context_parts.append(f"Success criteria: {uow.success_criteria}")
    elif issue_body:
        body_excerpt = issue_body.strip()
        if len(body_excerpt) > 2000:
            body_excerpt = body_excerpt[:2000] + "\n[...truncated]"
        context_parts.append(f"Issue body:\n{body_excerpt}")

    context_parts.append(f"Execution cycle: {uow.steward_cycles} (0 = first pass)")
    context_parts.append(f"Executor posture: {reentry_posture}")
    context_parts.append(f"Completion gap identified: {completion_gap}")

    if prior_prescriptions:
        context_parts.append(
            "Prior prescription history:\n" + "\n".join(prior_prescriptions)
        )

    uow_context = "\n".join(context_parts)

    system_prompt = (
        "You are prescribing work instructions for a Lobster subagent that will execute "
        "a Unit of Work (UoW) in a software development pipeline. "
        "Your prescription must be concrete, actionable, and directly executable. "
        "Avoid vague language. Use the success_criteria as your north star for what 'done' means. "
        "The Executor is a capable autonomous coding agent — write instructions at that level. "
        "The instructions you produce will be handed directly to a Lobster subagent dispatch call; "
        "they must conform to Lobster's subagent dispatch conventions so the executor can act on them correctly."
    )

    # Golden dispatch conventions injected into every prescription so the executor
    # agent that receives the prescription knows how to structure its own work.
    # uow.source is injected at generation time so the subagent prompt carries the
    # correct source value rather than a hardcoded platform assumption.
    _uow_source = uow.source or "telegram"
    _DISPATCH_CONVENTIONS = f"""\
## Lobster Subagent Dispatch Conventions

### Prompt YAML Frontmatter (required at top of every prompt)
---
task_id: <short-slug>
chat_id: <user's chat_id>
source: {_uow_source}
---

### Required fields in every subagent Task call
- run_in_background=True for user-facing subagents (required — violating this breaks the 7-second rule)
  Note: WOS executor tasks are already spawned as background claude -p processes; they use
  write_result with sent_reply_to_user=False instead of send_reply.
- subagent_type: see table below

### Agent type selection
- GitHub issue implementation, feature work, bug fix: functional-engineer
- Lobster system ops, infra, deploy, install tasks: lobster-ops
- General background tasks (default): lobster-generalist
- Default when uncertain: lobster-generalist

### Required prompt structure
Every prompt must include:
  Minimum viable output: <one concrete deliverable>
  Boundary: do not <X>

### Output delivery (subagent two-step)
1. send_reply(chat_id=<id>, text="<result>", task_id="<slug>")
2. write_result(task_id="<slug>", sent_reply_to_user=True)
For internal tasks (no user reply): write_result only with sent_reply_to_user=False
"""

    user_prompt = f"""Given this Unit of Work, write a precise prescription for the Executor.

{uow_context}

{_DISPATCH_CONVENTIONS}
Respond using front-matter + prose format. Output ONLY the prescription — no preamble, no explanation outside this structure:

---
executor_type: <agent type from the table above — e.g. functional-engineer>
estimated_cycles: <integer 1-3 — how many Executor passes this is expected to need>
success_criteria_check: <one or two sentences describing exactly how to verify the work is complete — what to check, what file exists, what content to confirm>
---

<complete, actionable instructions for the Executor — include the specific steps, what to produce, where to write output, and any constraints from the success criteria; embed the YAML frontmatter, Minimum viable output, Boundary, and agent_type lines as described above>"""

    # Combine system and user prompts into a single string for claude -p,
    # which does not accept a separate --system flag in basic invocation mode.
    prompt = f"{system_prompt}\n\n{user_prompt}"

    timeout_secs = _get_llm_prescription_timeout()
    model = _get_prescription_model()

    command = [_CLAUDE_BIN, "-p", prompt, "--output-format", "text", "--model", model]

    # Use error capture to detect and log subprocess failures with context
    proc, error = run_subprocess_with_error_capture(
        component="steward_prescription",
        uow_id=uow.id,
        command=command,
        timeout_seconds=timeout_secs,
        check=False,  # Don't auto-log; we handle errors gracefully with fallback
    )

    if error:
        log.warning(
            "_llm_prescribe: prescription failed for %s — %s (falling back to deterministic)",
            uow.id, error.summary(),
        )

        # Check for repeated failures (same error 3+ times in 5 min)
        if has_repeated_error("steward_prescription", uow.id, str(error.error_type), threshold=3):
            log.error(
                "_llm_prescribe: repeated prescription errors for %s — may need manual intervention",
                uow.id,
            )

        return None

    if proc is None or proc.returncode != 0:
        log.warning(
            "_llm_prescribe: claude -p exited %d for %s, falling back",
            proc.returncode if proc else None, uow.id,
        )
        return None

    raw_text = proc.stdout.strip()

    # Classify empty-output case separately: claude exited 0 but returned
    # nothing.  This typically means the binary is unavailable, the model
    # refused, or stdout was silently discarded.
    if not raw_text:
        log.warning(
            "_llm_prescribe: claude -p returned empty stdout for %s "
            "(exit 0), falling back",
            uow.id,
        )
        return None

    # Parse the front-matter + prose prescription format.
    try:
        parsed = _parse_workflow_artifact(raw_text)
    except ValueError as exc:
        log.warning(
            "_llm_prescribe: could not parse front-matter artifact for %s "
            "(%s) — output preview: %r, falling back",
            uow.id, exc, raw_text[:200],
        )
        return None

    instructions = parsed.get("instructions", "")
    success_criteria_check = parsed.get("success_criteria_check", "")
    estimated_cycles = parsed.get("estimated_cycles", 1)

    if not instructions:
        log.warning(
            "_llm_prescribe: LLM returned empty instructions field for %s, "
            "falling back",
            uow.id,
        )
        return None

    log.info(
        "_llm_prescribe: LLM prescription generated for %s (model=%s, estimated_cycles=%d)",
        uow.id, model, estimated_cycles,
    )
    return {
        "instructions": instructions,
        "success_criteria_check": success_criteria_check,
        "estimated_cycles": max(1, min(3, estimated_cycles)),
    }


def _fetch_prior_prescriptions(
    current_log_str: str | None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Extract the last N prescription entries from the steward_log text.

    The steward_log is a newline-delimited sequence of JSON objects.
    Prescription events have event == "prescription" or "reentry_prescription".

    Pure function: parses the log text and returns a list of at most `limit`
    prescription dicts, ordered oldest-first (most recent last).  Returns []
    when the log is absent, empty, or contains no prescription entries.

    Each returned dict contains the keys present in the prescription log entry:
    completion_assessment, next_posture_rationale, return_reason,
    steward_cycles, and timestamp.
    """
    if not current_log_str:
        return []

    prescriptions: list[dict[str, Any]] = []
    for line in current_log_str.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("event") in ("prescription", "reentry_prescription"):
            prescriptions.append(entry)

    # Return the last `limit` entries (oldest-first ordering preserved).
    return prescriptions[-limit:] if prescriptions else []


def _check_trace_gate_waited(steward_log: str | None) -> bool:
    """
    Return True if a 'trace_gate_waited' entry exists in the steward_log.

    Pure function. Scans newline-delimited JSON log entries and returns True
    when any entry has event == "trace_gate_waited". Returns False when the
    log is absent, empty, or contains no such entry.
    """
    if not steward_log:
        return False
    for line in steward_log.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(entry, dict) and entry.get("event") == "trace_gate_waited":
            return True
    return False


def _clear_trace_gate_waited(steward_log: str | None) -> str:
    """
    Return a new steward_log string with all 'trace_gate_waited' entries removed.

    Pure function. Filters out lines where event == "trace_gate_waited".
    Returns empty string when steward_log is None or empty.
    """
    if not steward_log:
        return steward_log or ""
    result_lines: list[str] = []
    for line in steward_log.splitlines():
        stripped = line.strip()
        if not stripped:
            result_lines.append(line)
            continue
        try:
            entry = json.loads(stripped)
            if isinstance(entry, dict) and entry.get("event") == "trace_gate_waited":
                continue
        except (json.JSONDecodeError, TypeError):
            pass
        result_lines.append(line)
    return "\n".join(result_lines)


# ---------------------------------------------------------------------------
# PR C: Register-aware diagnosis pure helpers
# ---------------------------------------------------------------------------

# Named constant: max chars for prescription_delta before bounding kicks in
_PRESCRIPTION_DELTA_MAX_CHARS = 500

# Named constant: consecutive non-improving gate cycles before surfacing
_NON_IMPROVING_GATE_THRESHOLD = 3


def _register_completion_policy(register: str) -> str:
    """
    Map a UoW register to its completion policy identifier.

    Returns one of:
    - "machine-gate"        for operational and iterative-convergent
    - "always-surface"      for philosophical
    - "require-confirmation" for human-judgment

    Unknown registers default to "machine-gate" (conservative pass-through).

    Pure function — no side effects.
    """
    _POLICY_MAP = {
        "operational": "machine-gate",
        "iterative-convergent": "machine-gate",
        "philosophical": "always-surface",
        "human-judgment": "require-confirmation",
    }
    return _POLICY_MAP.get(register, "machine-gate")


def _read_trace_json(output_ref: str | None, expected_uow_id: str) -> dict | None:
    """
    Read and validate a corrective trace file for the given output_ref.

    Tries two path conventions (mirroring result.json dual-path logic):
    - Primary:  Path(output_ref).with_suffix(".trace.json")
    - Fallback: Path(str(output_ref) + ".trace.json")

    Returns the parsed dict if the file exists and the uow_id field matches
    expected_uow_id.  Returns None on any error (absent, invalid JSON,
    uow_id mismatch).

    Pure function with respect to state — reads files only.
    """
    if not output_ref:
        return None

    trace_file = Path(output_ref).with_suffix(".trace.json")
    if not trace_file.exists():
        trace_file_alt = Path(str(output_ref) + ".trace.json")
        if trace_file_alt.exists():
            trace_file = trace_file_alt
        else:
            return None

    try:
        data = json.loads(trace_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict):
        return None

    # Guard: misrouted trace file
    trace_uow_id = data.get("uow_id")
    if trace_uow_id is not None and trace_uow_id != expected_uow_id:
        log.warning(
            "_read_trace_json: trace file %s has uow_id=%r but expected %r — "
            "treating as absent (misrouted trace file)",
            trace_file, trace_uow_id, expected_uow_id,
        )
        return None

    return data


def _bound_prescription_delta(delta: str, history: list[str]) -> str:
    """
    Bound a prescription_delta string to prevent loop gain instability.

    If delta exceeds _PRESCRIPTION_DELTA_MAX_CHARS, truncate it and append
    a trailing note indicating it was bounded.

    history is informational (for potential future smoothing) but does not
    change the bound threshold.

    Pure function — no side effects.
    """
    if not delta or len(delta) <= _PRESCRIPTION_DELTA_MAX_CHARS:
        return delta

    truncated = delta[:_PRESCRIPTION_DELTA_MAX_CHARS]
    return truncated + " ... [prescription_delta bounded — original exceeded limit]"


def _count_non_improving_gate_cycles(steward_log: str | None, n: int = _NON_IMPROVING_GATE_THRESHOLD) -> int:
    """
    Count consecutive non-improving gate_score cycles from the tail of steward_log.

    Reads trace_injection entries in order. A cycle is "non-improving" if its
    gate_score.score is not greater than the previous cycle's score (or if there
    is no gate_score, the entry is skipped entirely).

    Returns the count of consecutive non-improving cycles at the end of the log.
    Returns 0 when the log is absent, empty, has no gate_score entries, or the
    scores are improving.

    Pure function — no side effects.
    """
    if not steward_log:
        return 0

    # Collect gate_score entries from trace_injection events in order
    scores: list[float] = []
    for line in steward_log.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("event") != "trace_injection":
            continue
        gate_score = entry.get("gate_score")
        if gate_score is None:
            continue
        score_val = gate_score.get("score") if isinstance(gate_score, dict) else None
        if score_val is not None:
            try:
                scores.append(float(score_val))
            except (TypeError, ValueError):
                pass

    if len(scores) < 2:
        return 0

    # Find the tail run of non-improving cycles.
    # A "non-improving cycle" is one where the score did not increase vs. the previous.
    # We return the count of consecutive non-improving data points at the tail,
    # starting from 1 (the reference point after the last improvement).
    # With scores [0.5, 0.5, 0.5]: reference=index 0, tail=[1,2] are non-improving → count=3
    # (we include the starting point of the plateau to match the spec's "3 consecutive cycles").
    non_improving = 1  # start counting from the last improving point (or start of log)
    for i in range(len(scores) - 1, 0, -1):
        if scores[i] <= scores[i - 1]:
            non_improving += 1
        else:
            # Found an improvement — the plateau started AFTER this point
            # non_improving already counts from scores[i] forward
            return non_improving - 1  # exclude the improvement point itself

    # All scores are non-improving (or only 1 pair) — all data points are the plateau
    return non_improving


def _count_consecutive_llm_fallbacks(current_log_str: str | None) -> int:
    """
    Count how many consecutive prescription events at the tail of steward_log
    used the deterministic fallback path (prescription_path == "fallback").

    Scans prescription and reentry_prescription events in reverse order and
    stops at the first event that used the LLM path or at the beginning of the
    log.  Returns 0 when the log is absent, empty, or the last prescription
    used the LLM path.

    Pure function — reads only current_log_str; no side effects.
    """
    if not current_log_str:
        return 0

    prescription_events: list[dict[str, Any]] = []
    for line in current_log_str.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if entry.get("event") in ("prescription", "reentry_prescription"):
            prescription_events.append(entry)

    # Count consecutive fallbacks from the most recent prescription backwards.
    consecutive = 0
    for event in reversed(prescription_events):
        if event.get("prescription_path") == "fallback":
            consecutive += 1
        else:
            break

    return consecutive


def _notify_llm_fallback_warning(
    uow: UoW,
    consecutive_fallbacks: int,
) -> None:
    """
    Write an inbox message to Dan when _llm_prescribe has fallen back to the
    deterministic template for _LLM_FALLBACK_WARNING_THRESHOLD consecutive
    cycles on the same UoW.

    Uses the same inbox path as _default_notify_dan_early_warning.  In tests,
    the caller can skip this function entirely by checking the threshold before
    calling — or monkeypatch it to capture the call.
    """
    uow_id = uow.id
    admin_chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", _DAN_CHAT_ID)
    log.warning(
        "WOS LLM FALLBACK: UoW %s has fallen back to deterministic prescription "
        "%d consecutive times — LLM prescription path may be broken",
        uow_id, consecutive_fallbacks,
    )
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "chat_id": admin_chat_id,
        "text": (
            f"WOS: `{uow_id}` LLM prescription has fallen back to deterministic "
            f"for {consecutive_fallbacks} consecutive cycles. "
            "Check `LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS` and `claude -p` availability. "
            "Prescription quality is degraded until LLM path recovers."
        ),
        "timestamp": time.time(),
        "metadata": {
            "type": "wos_llm_fallback_warning",
            "uow_id": uow_id,
            "consecutive_llm_fallbacks": consecutive_fallbacks,
        },
    }
    inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"{msg_id}.json").write_text(
            json.dumps(msg, indent=2), encoding="utf-8"
        )
        log.info("WOS LLM fallback warning written to inbox: %s", msg_id)
    except OSError as exc:
        log.error("Failed to write WOS LLM fallback warning to inbox: %s", exc)


def _build_deterministic_prescription_instructions(
    uow: UoW,
    reentry_posture: str,
    completion_gap: str,
    issue_body: str = "",
    prior_prescriptions: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build natural language prescription instructions for the Executor using
    the deterministic keyword-matching template.

    This is the fallback path used when the LLM call fails or is unavailable.
    It is also the implementation called by _build_prescription_instructions
    when llm_prescriber returns None.

    NOTE: This path does not inject _DISPATCH_CONVENTIONS (YAML frontmatter,
    Minimum viable output, Boundary, agent type, run_in_background, two-step
    output delivery). The deterministic template produces minimal instructions
    only. Executors dispatched via this path may not conform to Lobster's
    subagent dispatch protocol without additional scaffolding at the call site.

    Args:
        uow: The Unit of Work being prescribed.
        reentry_posture: Categorized executor state from diagnosis.
        completion_gap: Human-readable rationale for why work is incomplete.
        issue_body: Raw GitHub issue body text. Used to compose context when
            success_criteria is absent. Pass empty string if unavailable.
        prior_prescriptions: List of prior steward_log prescription entries
            (from _fetch_prior_prescriptions). Injected into re-prescription
            context so the Steward can avoid repeating approaches that did not
            work. Pass None or [] for the first cycle.
    """
    summary = uow.summary
    success_criteria = uow.success_criteria
    cycles = uow.steward_cycles

    # Build the criteria/context block from whatever is available.
    # Priority: explicit success_criteria > issue body > nothing.
    if success_criteria:
        criteria_block = f"Success criteria: {success_criteria}"
    elif issue_body:
        # Truncate very long issue bodies to keep instructions readable.
        body_excerpt = issue_body.strip()
        if len(body_excerpt) > 1500:
            body_excerpt = body_excerpt[:1500] + "\n[...truncated]"
        criteria_block = f"Issue context:\n{body_excerpt}"
    else:
        criteria_block = ""

    if cycles == 0:
        parts = [
            "Execute the following task:",
            "",
            f"Summary: {summary}",
        ]
        if criteria_block:
            parts += ["", criteria_block]
        parts += ["", "Write your output to the output_ref path."]
        return "\n".join(parts)

    posture_context = {
        "execution_complete": "Previous execution completed but output needs improvement.",
        "stall_detected": "Previous execution stalled (timeout). Re-execute with focus on completing within time limits.",
        "crashed_no_output": "Previous execution crashed without producing output. Re-execute, adding error handling.",
        "execution_failed": "Previous execution failed. Diagnose the failure and re-execute.",
        "executor_orphan": "Executor never ran on this UoW. Execute fresh.",
        "diagnosing_orphan": "Steward crashed mid-diagnosis. Re-diagnosing from current state.",
    }

    posture_msg = posture_context.get(reentry_posture, "Continue from previous attempt.")

    parts = [
        f"Re-execution pass (cycle {cycles + 1}):",
        "",
        posture_msg,
        "",
        f"Gap identified: {completion_gap}",
        "",
        f"Original task: {summary}",
    ]
    if criteria_block:
        parts += ["", criteria_block]

    # Inject prior prescription attempts so the Executor avoids repeating
    # approaches that already failed.  Only included when prior data exists.
    if prior_prescriptions:
        prior_lines = ["", "Prior prescription attempts (do not repeat these approaches):"]
        for i, entry in enumerate(prior_prescriptions, start=1):
            assessment = entry.get("completion_assessment", "")
            rationale = entry.get("next_posture_rationale", "")
            cycle_num = entry.get("steward_cycles", "?")
            return_reason = entry.get("return_reason", "")
            prior_lines.append(
                f"  {i}. Cycle {cycle_num}: assessment={assessment!r}; "
                f"rationale={rationale!r}; return_reason={return_reason!r}"
            )
        parts += prior_lines

    return "\n".join(parts)


def _build_prescription_instructions(
    uow: UoW,
    reentry_posture: str,
    completion_gap: str,
    issue_body: str = "",
    llm_prescriber: Callable[..., dict[str, Any] | None] | None = _llm_prescribe,
    prior_prescriptions: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build natural language prescription instructions for the Executor.

    Uses the LLM-based prescription path via llm_prescriber. If the LLM prescription
    fails (API unavailable, timeout, parse failure), raises LLMPrescriptionError
    instead of falling back to deterministic.

    Args:
        uow: The Unit of Work being prescribed.
        reentry_posture: Categorized executor state from diagnosis.
        completion_gap: Human-readable rationale for why work is incomplete.
        issue_body: Raw GitHub issue body text. Used when success_criteria is absent.
        llm_prescriber: Callable that takes (uow, reentry_posture, completion_gap,
            issue_body) and returns a dict or None. Inject None or a stub in tests
            to bypass the LLM call. Defaults to _llm_prescribe.
        prior_prescriptions: List of prior steward_log prescription entries
            (from _fetch_prior_prescriptions). No longer used since deterministic
            fallback is removed. Kept for backward compatibility.

    Raises:
        LLMPrescriptionError: If the LLM prescriber returns None (failure, not bypass).
    """
    if llm_prescriber is None:
        # If llm_prescriber is explicitly None, this is a test/stub scenario
        # Fall back to deterministic only in this case
        return _build_deterministic_prescription_instructions(
            uow, reentry_posture, completion_gap, issue_body,
            prior_prescriptions=prior_prescriptions,
        )

    llm_result = llm_prescriber(uow, reentry_posture, completion_gap, issue_body)
    if llm_result is None:
        # LLM prescription failed — fail hard instead of falling back
        raise LLMPrescriptionError(
            f"LLM prescription failed for UoW {uow.id}. "
            "Check steward logs for details. No deterministic fallback is performed."
        )

    instructions = llm_result["instructions"]
    success_check = llm_result.get("success_criteria_check", "")
    # Append the success_criteria_check as a verification note so the
    # Executor has an explicit completion signal alongside the instructions.
    if success_check:
        instructions = (
            instructions.rstrip()
            + f"\n\nCompletion check: {success_check}"
        )
    return instructions


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_steward_schema(conn: sqlite3.Connection) -> None:
    """
    Validate that all fields required for Steward operation are present in uow_registry.

    Raises RuntimeError with a specific message if any required field is absent.
    Call this at Steward startup before processing any UoW.

    Args:
        conn: An open SQLite connection to the registry database.

    Raises:
        RuntimeError: If any required field is missing. Message includes
            "schema migration not applied" and the list of missing fields.
    """
    rows = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
    existing_cols = {row[1] for row in rows}
    missing = _STEWARD_REQUIRED_FIELDS - existing_cols
    if missing:
        missing_sorted = sorted(missing)
        raise RuntimeError(
            f"schema migration not applied — run scripts/migrate_add_steward_fields.py first. "
            f"Missing fields: {missing_sorted}"
        )


# Keep the old name as an alias so any existing callers continue to work.
validate_phase2_schema = validate_steward_schema


# ---------------------------------------------------------------------------
# Registry write helpers (steward-private field updates)
# ---------------------------------------------------------------------------

def _write_steward_fields(
    registry,
    uow_id: str,
    *,
    steward_agenda: str | None = None,
    steward_log: str | None = None,
    workflow_artifact: str | None = None,
    prescribed_skills: str | None = None,
    route_reason: str | None = None,
    steward_cycles: int | None = None,
    completed_at: str | None = None,
) -> None:
    """
    Write Steward-private and Steward-managed fields to the UoW row.

    Uses a direct connection from the Registry (bypasses the public API since
    these fields are Steward-private and not part of the Registry's public
    interface). Executes in a BEGIN IMMEDIATE transaction.
    """
    updates = {}
    if steward_agenda is not None:
        updates["steward_agenda"] = steward_agenda
    if steward_log is not None:
        updates["steward_log"] = steward_log
    if workflow_artifact is not None:
        updates["workflow_artifact"] = workflow_artifact
    if prescribed_skills is not None:
        updates["prescribed_skills"] = prescribed_skills
    if route_reason is not None:
        updates["route_reason"] = route_reason
    if steward_cycles is not None:
        updates["steward_cycles"] = steward_cycles
    if completed_at is not None:
        updates["completed_at"] = completed_at

    if not updates:
        return

    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [uow_id]

    conn = registry._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE uow_registry SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _append_steward_log_entry(
    registry,
    uow_id: str,
    current_log: str | None,
    entry: dict[str, Any],
) -> str:
    """
    Append a JSON entry to steward_log (newline-delimited).

    Returns the updated log string (does NOT write to DB — caller writes).
    The entry is JSON-encoded and appended on a new line.
    """
    entry["timestamp"] = _now_iso()
    entry_str = json.dumps(entry)
    if current_log:
        return current_log.rstrip("\n") + "\n" + entry_str
    return entry_str


def _update_agenda_node_status(
    agenda: list[dict[str, Any]],
    target_status: str,
    filter_status: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return a new agenda with nodes matching filter_status updated to target_status.
    If filter_status is None, all nodes are updated.
    Pure function — does not mutate the input.
    """
    return [
        {**node, "status": target_status}
        if (filter_status is None or node.get("status") == filter_status)
        else node
        for node in agenda
    ]


def _mark_current_agenda_node_prescribed(
    agenda: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Mark the first 'pending' agenda node as 'prescribed'.
    Pure function.
    """
    found = False
    result = []
    for node in agenda:
        if not found and node.get("status") == "pending":
            result.append({**node, "status": "prescribed"})
            found = True
        else:
            result.append(node)
    return result


# ---------------------------------------------------------------------------
# Stuck condition detection
# ---------------------------------------------------------------------------

def _detect_stuck_condition(
    uow: UoW,
    reentry_posture: str,
    return_reason: str | None,
) -> str | None:
    """
    Check whether the UoW has hit a stuck condition.

    Returns the condition name string if stuck, or None if not stuck.

    PR C additions (V3):
    - philosophical_register: fires when register=philosophical AND reentry_posture != first_execution.
      On first execution, wait for executor evidence before surfacing.
    - no_gate_improvement: fires for iterative-convergent when gate_score has not improved
      over the last _NON_IMPROVING_GATE_THRESHOLD consecutive cycles (reads from steward_log).
    """
    cycles = uow.lifetime_cycles

    if cycles >= _HARD_CAP_CYCLES:
        return "hard_cap"

    # crashed_no_output + cycles >= 2 (uses steward_cycles for per-attempt crash detection)
    if return_reason == "crashed_no_output" and uow.steward_cycles >= _CRASH_SURFACE_CYCLES:
        return "crash_repeated"

    # PR C, Change 4a: philosophical_register — surface after first execution
    if uow.register == "philosophical" and reentry_posture != "first_execution":
        return "philosophical_register"

    # PR C, Change 4c: no_gate_improvement — iterative-convergent stall detection
    if uow.register == "iterative-convergent":
        non_improving = _count_non_improving_gate_cycles(
            uow.steward_log, n=_NON_IMPROVING_GATE_THRESHOLD
        )
        if non_improving >= _NON_IMPROVING_GATE_THRESHOLD:
            return "no_gate_improvement"

    return None


# ---------------------------------------------------------------------------
# Core per-UoW diagnosis function (pure — returns diagnosis dict, no DB writes)
# ---------------------------------------------------------------------------

def _diagnose_uow(
    uow: UoW,
    audit_entries: list[dict],
    issue_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Produce a diagnosis for a single UoW.

    Pure function: reads inputs, returns a diagnosis dict with fields:
    - reentry_posture: str
    - return_reason: str | None
    - return_reason_classification: str
    - output_content: str
    - output_valid: bool
    - is_complete: bool
    - completion_rationale: str
    - stuck_condition: str | None
    - success_criteria_missing: bool
    """
    return_reason = _most_recent_return_reason(audit_entries)
    reentry_posture = _determine_reentry_posture(audit_entries, return_reason)
    classification = _classify_return_reason(return_reason)

    output_ref = uow.output_ref
    output_valid = _output_ref_is_valid(output_ref)
    output_content = _read_output_ref(output_ref) if output_valid else ""

    success_criteria_missing = not uow.success_criteria

    is_complete, completion_rationale, executor_outcome = _assess_completion(
        uow, output_content, reentry_posture
    )

    stuck_condition = _detect_stuck_condition(uow, reentry_posture, return_reason)

    # Gap 3 (executor-contract.md): `blocked` outcome always routes to Dan.
    # Override stuck_condition here so _process_uow uses the existing surface path.
    if executor_outcome == "blocked" and stuck_condition is None:
        stuck_condition = "executor_blocked"

    # Hard cap overrides completion
    if stuck_condition == "hard_cap":
        is_complete = False

    return {
        "reentry_posture": reentry_posture,
        "return_reason": return_reason,
        "return_reason_classification": classification,
        "output_content": output_content,
        "output_valid": output_valid,
        "is_complete": is_complete,
        "completion_rationale": completion_rationale,
        "stuck_condition": stuck_condition,
        "executor_outcome": executor_outcome,
        "success_criteria_missing": success_criteria_missing,
    }


# ---------------------------------------------------------------------------
# GitHub client helper
# ---------------------------------------------------------------------------

def _repo_from_issue_url(issue_url: str | None) -> str | None:
    """Extract 'owner/repo' from a GitHub issue URL.

    Pure function — no side effects.

    Examples:
        "https://github.com/dcetlin/Lobster/issues/42" → "dcetlin/Lobster"
        None → None
        "not-a-url" → None
    """
    if not issue_url:
        return None
    # URL form: https://github.com/{owner}/{repo}/issues/{number}
    prefix = "https://github.com/"
    if not issue_url.startswith(prefix):
        return None
    rest = issue_url[len(prefix):]
    parts = rest.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def _fetch_github_issue(issue_number: int, repo: str) -> dict[str, Any]:
    """
    Fetch issue info from GitHub using gh CLI for a given repo.

    Returns dict with keys: status_code, state, labels (list), body, title.
    On any error, returns status_code=0 with empty fields.
    """
    command = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "state,labels,body,title",
    ]

    # Use error capture to detect and log subprocess failures with context
    result, error = run_subprocess_with_error_capture(
        component="steward_github",
        uow_id=f"{repo}#{issue_number}",
        command=command,
        timeout_seconds=15,
        check=False,  # Don't auto-log; handle gracefully
    )

    if error:
        log.warning("GitHub fetch error for %s#%s: %s", repo, issue_number, error.summary())
        return {"status_code": 0, "state": None, "labels": [], "body": "", "title": ""}

    if result is None or result.returncode != 0:
        return {"status_code": 1, "state": None, "labels": [], "body": "", "title": ""}

    try:
        data = json.loads(result.stdout)
        labels = [l.get("name", "") for l in data.get("labels", [])]
        return {
            "status_code": 200,
            "state": data.get("state", "open"),
            "labels": labels,
            "body": data.get("body", ""),
            "title": data.get("title", ""),
        }
    except Exception as e:
        log.warning("GitHub parse error for issue %s (repo=%s): %s", issue_number, repo, e)
        return {"status_code": 0, "state": None, "labels": [], "body": "", "title": ""}


def _default_github_client(issue_number: int) -> dict[str, Any]:
    """
    Fetch issue info from GitHub using gh CLI.

    Falls back to the hardcoded 'dcetlin/Lobster' repo for UoWs that
    pre-date the issue_url field (migration 0005). New UoWs provide
    issue_url and the Steward loop calls _fetch_github_issue directly
    with the derived repo, bypassing this function.

    Returns dict with keys: status_code, state, labels (list), body, title.
    On any error, returns status_code=0 with empty fields.
    """
    return _fetch_github_issue(issue_number, repo="dcetlin/Lobster")


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def _write_workflow_artifact(
    uow_id: str,
    instructions: str,
    prescribed_skills: list[str],
    artifact_dir: Path | None = None,
    executor_type: str = _EXECUTOR_TYPE_GENERAL,
) -> str:
    """
    Write a WorkflowArtifact JSON file to disk.

    Returns the absolute path to the written file.
    artifact_dir: override for the artifact directory (used in tests).
    executor_type: the executor type to embed in the artifact (defaults to general).
    """
    try:
        from src.orchestration.workflow_artifact import WorkflowArtifact, to_json
        artifact = WorkflowArtifact(
            uow_id=uow_id,
            executor_type=executor_type,
            constraints=[],
            prescribed_skills=prescribed_skills,
            instructions=instructions,
        )
        artifact_json = to_json(artifact)
    except ImportError:
        # Fallback if workflow_artifact.py not yet on branch (pre-merge)
        artifact_data = {
            "uow_id": uow_id,
            "executor_type": executor_type,
            "constraints": [],
            "prescribed_skills": prescribed_skills,
            "instructions": instructions,
        }
        artifact_json = json.dumps(artifact_data)

    if artifact_dir is not None:
        artifact_dir = Path(artifact_dir)
        artifact_path = artifact_dir / f"{uow_id}.json"
    else:
        artifact_path = Path(os.path.expanduser(
            f"~/lobster-workspace/orchestration/artifacts/{uow_id}.json"
        ))

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(artifact_json, encoding="utf-8")

    return str(artifact_path.resolve())


# ---------------------------------------------------------------------------
# Dan notification
# ---------------------------------------------------------------------------

_DAN_CHAT_ID = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")


def _default_notify_dan(
    uow: UoW,
    condition: str,
    surface_log: str | None = None,
    return_reason: str | None = None,
) -> None:
    """
    Surface a UoW to Dan via the Lobster inbox.

    Writes a structured JSON message to ~/messages/inbox/ so the Lobster
    dispatcher surfaces it to Dan via Telegram. In tests this is replaced
    by a capturing mock via the `notify_dan` parameter.

    For hard_cap notifications, the body includes steward_log (diagnosis
    history across all cycles) and steward_agenda (what the Steward was
    trying to accomplish) so Dan can triage without leaving the inbox thread.
    """
    uow_id = uow.id
    # Use lifetime_cycles for hard_cap reporting (that's what triggered it);
    # steward_cycles for other conditions (per-attempt context).
    cycles = uow.lifetime_cycles if condition == "hard_cap" else uow.steward_cycles
    log.warning(
        "SURFACE TO DAN: UoW %s — condition=%s cycles=%s (lifetime_cycles=%s)",
        uow_id, condition, cycles, uow.lifetime_cycles,
    )
    msg_id = str(uuid.uuid4())
    if condition == "hard_cap":
        # Hard cap: exhaustive context so Dan can triage and act without
        # digging through logs. Include summary, agenda, log, and reason.
        body_lines = [
            f"WOS: UoW `{uow_id}` hit hard cap ({_HARD_CAP_CYCLES} lifetime cycles). "
            f"return_reason: {return_reason}.",
        ]

        # UoW summary — what was this trying to accomplish?
        summary = uow.summary
        if summary:
            body_lines.append(f"\nSummary: {summary}")

        # Success criteria — what would done look like?
        success_criteria = uow.success_criteria
        if success_criteria:
            body_lines.append(f"\nSuccess criteria: {success_criteria}")

        # Steward agenda — the structured forecast of what was planned
        steward_agenda_raw = uow.steward_agenda
        if steward_agenda_raw:
            try:
                agenda = json.loads(steward_agenda_raw)
                # Render agenda nodes as a compact list for readability
                agenda_lines: list[str] = []
                nodes = agenda if isinstance(agenda, list) else [agenda]
                for node in nodes:
                    posture = node.get("posture", "?")
                    status = node.get("status", "?")
                    context = node.get("context", "")
                    agenda_lines.append(f"  [{status}] {posture}: {context[:120]}")
                body_lines.append("\nSteward agenda:\n" + "\n".join(agenda_lines))
            except (json.JSONDecodeError, TypeError, AttributeError):
                # If agenda is not valid JSON or not a list, include raw text
                body_lines.append(f"\nSteward agenda (raw):\n{steward_agenda_raw[:500]}")

        # Steward log — full diagnosis history across all cycles (surface_log == current_log_str)
        if surface_log:
            # Show last N log lines to keep the message readable
            log_lines = [ln for ln in surface_log.strip().splitlines() if ln.strip()]
            _MAX_LOG_LINES = 20
            if len(log_lines) > _MAX_LOG_LINES:
                omitted = len(log_lines) - _MAX_LOG_LINES
                displayed = log_lines[-_MAX_LOG_LINES:]
                body_lines.append(
                    f"\nSteward log (last {_MAX_LOG_LINES} of {len(log_lines)} entries, "
                    f"{omitted} omitted):\n" + "\n".join(displayed)
                )
            else:
                body_lines.append(f"\nSteward log:\n" + "\n".join(log_lines))
    elif condition == "philosophical_register":
        body_lines = [
            f"WOS: UoW {uow_id!r} is in philosophical register — executor returned output "
            f"but completion requires human judgment. "
            f"See output at {uow.output_ref}. "
            f"Summary: {(uow.summary or '')[:200]}",
        ]
    elif condition == "register_mismatch":
        # Extract mismatch details from surface_log for structured message
        _mismatch_register = uow.register
        _mismatch_executor = "unknown"
        _mismatch_direction = f"{_mismatch_register}→unknown"
        if surface_log:
            for _line in reversed(surface_log.strip().splitlines()):
                _stripped = _line.strip()
                if not _stripped:
                    continue
                try:
                    _entry = json.loads(_stripped)
                    if _entry.get("event") == "register_mismatch":
                        _mismatch_executor = _entry.get("executor_type_attempted", "unknown")
                        _mismatch_direction = _entry.get("direction", _mismatch_direction)
                        break
                except (json.JSONDecodeError, TypeError):
                    pass
        body_lines = [
            f"WOS: UoW {uow_id} — register mismatch. "
            f"UoW register: {_mismatch_register}. "
            f"Prescribed executor type: {_mismatch_executor}. "
            f"A {_mismatch_register}-register UoW cannot be dispatched to {_mismatch_executor}. "
            "Manual routing required."
        ]
    elif condition == "no_gate_improvement":
        body_lines = [
            f"WOS: UoW {uow_id!r} — iterative-convergent gate not improving after "
            f"{_NON_IMPROVING_GATE_THRESHOLD} cycles. "
            f"See steward_log for gate_score history and prescription_delta.",
        ]
        if surface_log:
            body_lines.append(f"\nSteward log:\n{surface_log}")
    else:
        body_lines = [
            f"WOS SURFACE: UoW {uow_id} hit condition={condition} "
            f"(steward_cycles={cycles}). Needs human review.",
        ]
        if surface_log:
            body_lines.append(f"\nSteward log:\n{surface_log}")
    # Inline buttons let Dan resolve the stuck UoW without typing commands.
    # The dispatcher routes callback_data="decide_retry:<uow_id>" and
    # callback_data="decide_close:<uow_id>" to handle_decide_retry/close.
    buttons = [
        [
            {"text": "Retry", "callback_data": f"decide_retry:{uow_id}"},
            {"text": "Close", "callback_data": f"decide_close:{uow_id}"},
        ]
    ]
    msg = {
        "id": msg_id,
        "source": "system",
        "chat_id": _DAN_CHAT_ID,
        "text": "\n".join(body_lines),
        "buttons": buttons,
        "timestamp": time.time(),
        "metadata": {
            "type": "wos_surface",
            "uow_id": uow_id,
            "condition": condition,
            "steward_cycles": cycles,
            "return_reason": return_reason,
            "steward_log": surface_log,
            "steward_agenda": uow.steward_agenda,
        },
    }
    inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"{msg_id}.json").write_text(
            json.dumps(msg, indent=2), encoding="utf-8"
        )
        log.info("WOS surface message written to inbox: %s", msg_id)
    except OSError as e:
        log.error("Failed to write WOS surface message to inbox: %s", e)


def _default_notify_dan_early_warning(
    uow: UoW,
    return_reason: str | None,
    new_cycles: int | None = None,
) -> None:
    """
    Send an early-warning notification to Dan when steward_cycles reaches
    _EARLY_WARNING_CYCLES (4), one cycle before the hard cap.

    new_cycles is the post-prescription cycle count (uow.steward_cycles + 1).
    Pass it explicitly so the message reflects the cycle count after prescription,
    not the stale pre-prescription value on the UoW object.

    Uses the same inbox path as _default_notify_dan. In tests, override via
    the `notify_dan_early_warning` parameter on run_steward_cycle / _process_uow.
    """
    uow_id = uow.id
    cycles = new_cycles if new_cycles is not None else uow.steward_cycles
    admin_chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", _DAN_CHAT_ID)
    log.warning(
        "WOS EARLY WARNING: UoW %s at cycle %s — approaching hard cap (%s)",
        uow_id, cycles, _HARD_CAP_CYCLES,
    )
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "chat_id": admin_chat_id,
        "text": (
            f"⚠️ WOS: UoW `{uow_id}` at cycle {cycles} — "
            f"approaching hard cap ({_HARD_CAP_CYCLES}). "
            f"Last return_reason: {return_reason}"
        ),
        "timestamp": time.time(),
        "metadata": {
            "type": "wos_early_warning",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "return_reason": return_reason,
        },
    }
    inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"{msg_id}.json").write_text(
            json.dumps(msg, indent=2), encoding="utf-8"
        )
        log.info("WOS early-warning message written to inbox: %s", msg_id)
    except OSError as e:
        log.error("Failed to write WOS early-warning message to inbox: %s", e)


# ---------------------------------------------------------------------------
# DB fetch helpers
# ---------------------------------------------------------------------------

def _fetch_audit_entries(registry, uow_id: str) -> list[dict[str, Any]]:
    """Fetch all audit_log entries for a UoW, ordered by id ascending."""
    conn = registry._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-UoW processing
# ---------------------------------------------------------------------------

def _process_uow(
    uow: UoW,
    registry,
    audit_entries: list[dict[str, Any]],
    issue_info: dict[str, Any] | None,
    dry_run: bool,
    artifact_dir: Path | None,
    notify_dan: Callable | None,
    notify_dan_early_warning: Callable | None = None,
    llm_prescriber: Callable[..., dict[str, Any] | None] | None = _llm_prescribe,
    inline_executor: Callable[[str], Any] | None = None,
) -> StewardOutcome:
    """
    Process a single UoW through the full diagnosis + prescribe/close/surface cycle.

    Returns a StewardOutcome: Prescribed | Done | Surfaced | RaceSkipped.

    Args:
        inline_executor: Optional callable(uow_id) that is invoked immediately after
            the READY_FOR_EXECUTOR transition, collapsing the polling hop described in
            issue #648 Part A.  When provided, the Steward dispatches the Executor
            synchronously rather than waiting for the next heartbeat cycle (0–3 min).
            The Executor's optimistic lock protects against double-execution if a
            concurrent heartbeat fires between the transition and the inline call.
            Defaults to None (no inline dispatch — heartbeat remains the dispatch path).
    """
    uow_id = uow.id
    cycles = uow.steward_cycles

    # Step 1: Claim (optimistic lock) — only if not in dry-run mode
    if not dry_run:
        rows = registry.transition(uow_id, _STATUS_DIAGNOSING, _STATUS_READY_FOR_STEWARD)
        if rows == 0:
            log.debug("UoW %s already claimed by another Steward instance — skipping", uow_id)
            return RaceSkipped(uow_id=uow_id)

    # Step 2: Initialization ritual — write steward_agenda on first contact
    current_agenda_str = uow.steward_agenda
    current_log_str = uow.steward_log

    agenda: list[dict[str, Any]] = []
    if current_agenda_str:
        try:
            agenda = json.loads(current_agenda_str)
        except (json.JSONDecodeError, TypeError):
            agenda = []

    if cycles == 0:
        # Build initial agenda before any other action
        issue_body = issue_info.get("body", "") if issue_info else ""
        agenda = _build_initial_agenda(uow, issue_body)
        agenda_log_entry = {
            "event": "agenda_update",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "update_type": "initial",
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, agenda_log_entry)

        if not dry_run:
            _write_steward_fields(
                registry, uow_id,
                steward_agenda=json.dumps(agenda),
                steward_log=current_log_str,
            )
            # Write agenda_update to audit_log
            registry.append_audit_log(uow_id, {
                "event": "agenda_update",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "update_type": "initial",
                "timestamp": _now_iso(),
            })

    # Step 3: Diagnose
    diagnosis = _diagnose_uow(uow, audit_entries, issue_info)
    reentry_posture = diagnosis["reentry_posture"]
    return_reason = diagnosis["return_reason"]
    is_complete = diagnosis["is_complete"]
    completion_rationale = diagnosis["completion_rationale"]
    stuck_condition = diagnosis["stuck_condition"]
    success_criteria_missing = diagnosis["success_criteria_missing"]
    executor_outcome = diagnosis.get("executor_outcome")

    # Append diagnosis to steward_log
    diag_log_entry = {
        "event": "diagnosis",
        "uow_id": uow_id,
        "steward_cycles": cycles,
        "re_entry_posture": reentry_posture,
        "return_reason": return_reason,
        "is_complete": is_complete,
        "completion_rationale": completion_rationale,
        "stuck_condition": stuck_condition,
    }
    current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, diag_log_entry)

    # Write diagnosis audit entry BEFORE any prescription or transition
    if not dry_run:
        _write_steward_fields(registry, uow_id, steward_log=current_log_str)

        audit_note: dict[str, Any] = {
            "event": "steward_diagnosis",
            "actor": _ACTOR_STEWARD,
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "re_entry_posture": reentry_posture,
            "return_reason": return_reason,
            "is_complete": is_complete,
            "completion_rationale": completion_rationale,
            "timestamp": _now_iso(),
        }
        if success_criteria_missing:
            audit_note["success_criteria_missing"] = True
            audit_note["note"] = "evaluating against summary field as fallback"
        registry.append_audit_log(uow_id, audit_note)

    # Pre-branch trace write — unconditional, fires before stuck/done/prescribe split.
    # The agenda list at this point contains the initial agenda nodes (from the
    # initialization ritual above). We append one trace entry per cycle so
    # steward_agenda accumulates a full cycle history. Branch-level writes that
    # follow (done: mark nodes complete; prescribe: mark node prescribed) will
    # overwrite steward_agenda with the trace entry already present in the list.
    trace_entry = _build_trace_entry(diagnosis, cycles)
    trace_agenda = _parse_steward_agenda(uow.steward_agenda)
    # On cycle 0, the initial agenda was already written in the initialization
    # ritual above; re-read it from the in-memory `agenda` variable to avoid
    # an extra DB round-trip and to pick up that write's content.
    if cycles == 0:
        trace_agenda = list(agenda)
    trace_agenda.append(trace_entry)
    if not dry_run:
        _write_steward_fields(registry, uow_id, steward_agenda=json.dumps(trace_agenda))

    # Step 4: Convergence or prescription

    # 4a: Stuck condition check (fires before completion/prescription)
    if stuck_condition:
        surface_log = current_log_str

        surface_log_entry = {
            "event": "surface",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "surface_condition": stuck_condition,
            "return_reason": return_reason,
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, surface_log_entry)

        if not dry_run:
            _write_steward_fields(registry, uow_id, steward_log=current_log_str)
            registry.append_audit_log(uow_id, {
                "event": "steward_surface",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "surface_condition": stuck_condition,
                "return_reason": return_reason,
                "timestamp": _now_iso(),
            })

        # Surface to Dan (injectable for tests)
        _notify = notify_dan or _default_notify_dan
        _notify(uow, stuck_condition, surface_log=current_log_str, return_reason=return_reason)

        if not dry_run:
            registry.transition(uow_id, _STATUS_BLOCKED, _STATUS_DIAGNOSING)

        _append_cycle_trace(
            uow_id=uow_id,
            cycle_num=cycles,
            subagent_excerpt=_read_output_ref(uow.output_ref),
            return_reason=return_reason or "",
            next_action="stuck",
            artifact_dir=artifact_dir,
        )
        return Surfaced(uow_id=uow_id, condition=stuck_condition)

    # 4b: Declare done
    if is_complete:
        # Mark all agenda nodes complete (including the trace entry just appended)
        completed_agenda = _update_agenda_node_status(trace_agenda, "complete")

        closure_entry = {
            "event": "steward_closure",
            "actor": _ACTOR_STEWARD,
            "uow_id": uow_id,
            "assessment": completion_rationale,
            "timestamp": _now_iso(),
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, closure_entry)

        if not dry_run:
            _write_steward_fields(
                registry, uow_id,
                steward_agenda=json.dumps(completed_agenda),
                steward_log=current_log_str,
                completed_at=_now_iso(),
            )
            registry.append_audit_log(uow_id, {
                "event": "steward_closure",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "assessment": completion_rationale,
                "timestamp": _now_iso(),
            })
            # Primary done-transition: expects 'diagnosing' (set by the claim
            # step above). This is the normal path.
            #
            # Fallback done-transition (issue #671): a concurrent startup_sweep
            # may reset status from 'diagnosing' → 'ready-for-steward' between
            # the claim and this transition. When the primary WHERE fails (rows=0),
            # attempt a fallback transition from 'ready-for-steward' so the closure
            # is not silently lost.
            rows = registry.transition(uow_id, _STATUS_DONE, _STATUS_DIAGNOSING)
            if rows == 0:
                rows = registry.transition(uow_id, _STATUS_DONE, _STATUS_READY_FOR_STEWARD)
                if rows == 0:
                    log.warning(
                        "done-transition failed for %s — status was neither 'diagnosing' "
                        "nor 'ready-for-steward' (possible concurrent state change); "
                        "UoW may not have reached 'done'",
                        uow_id,
                    )
                else:
                    log.info(
                        "done-transition fallback succeeded for %s — "
                        "status was 'ready-for-steward' (startup_sweep reset race)",
                        uow_id,
                    )

        _append_cycle_trace(
            uow_id=uow_id,
            cycle_num=cycles,
            subagent_excerpt=_read_output_ref(uow.output_ref),
            return_reason=return_reason or "",
            next_action="done",
            artifact_dir=artifact_dir,
        )
        return Done(uow_id=uow_id)

    # 4c: Prescribe another Executor pass
    # executor-contract.md Steward Interpretation Table: `partial` and `failed`
    # require distinct re-diagnosis inputs. For `partial`, include
    # steps_completed/steps_total from the result file so the prescription
    # reflects how far the previous execution got. For `failed`, re-diagnose
    # with `reason` as the primary input (already in completion_rationale).

    # 4c-gate: Corrective trace one-cycle temporal gate (cristae-junction delay).
    # Before prescribing again after an executor return, the executor must have
    # written a corrective trace ({output_ref}.trace.json). This forces temporal
    # spacing between action and next prescription — the software equivalent of
    # the cristae geometry delay between proton pump action and ATP synthesis.
    # Gate applies only when result.json exists (executor actually returned).
    output_ref_for_gate = uow.output_ref
    result_file_exists = False
    if output_ref_for_gate:
        _rf = Path(output_ref_for_gate).with_suffix(".result.json")
        if not _rf.exists():
            _rf_alt = Path(str(output_ref_for_gate) + ".result.json")
            if _rf_alt.exists():
                _rf = _rf_alt
        result_file_exists = _rf.exists()

    if result_file_exists and output_ref_for_gate:
        trace_file = Path(output_ref_for_gate).with_suffix(".trace.json")
        if not trace_file.exists():
            trace_file = Path(str(output_ref_for_gate) + ".trace.json")
        trace_exists = trace_file.exists()

        if not trace_exists:
            # trace.json absent — apply one-cycle wait gate
            already_waited = _check_trace_gate_waited(current_log_str)
            if not already_waited:
                # First visit: log trace_gate_waited and skip prescription this cycle.
                # Transition back to ready-for-steward so next heartbeat picks it up.
                log.info(
                    "_process_uow: trace.json absent for %s — logging trace_gate_waited, "
                    "skipping prescription this cycle (cristae-junction delay)",
                    uow_id,
                )
                wait_entry = {
                    "event": "trace_gate_waited",
                    "uow_id": uow_id,
                    "steward_cycles": cycles,
                    "output_ref": output_ref_for_gate,
                    "timestamp": _now_iso(),
                }
                current_log_str = _append_steward_log_entry(
                    registry, uow_id, current_log_str, wait_entry
                )
                if not dry_run:
                    _write_steward_fields(registry, uow_id, steward_log=current_log_str)
                    registry.append_audit_log(uow_id, {
                        "event": "trace_gate_waited",
                        "actor": _ACTOR_STEWARD,
                        "uow_id": uow_id,
                        "steward_cycles": cycles,
                        "note": json.dumps({
                            "trace_gate_waited": _now_iso(),
                            "output_ref": output_ref_for_gate,
                        }),
                        "timestamp": _now_iso(),
                    })
                    registry.transition(uow_id, _STATUS_READY_FOR_STEWARD, _STATUS_DIAGNOSING)
                # Return a special Prescribed outcome with cycles unchanged to signal skip
                _append_cycle_trace(
                    uow_id=uow_id,
                    cycle_num=cycles,
                    subagent_excerpt=_read_output_ref(uow.output_ref),
                    return_reason=return_reason or "",
                    next_action="prescribed",
                    artifact_dir=artifact_dir,
                )
                return Prescribed(uow_id=uow_id, cycles=cycles)
            else:
                # Already waited one cycle — proceed with prescription, log contract violation
                log.warning(
                    "_process_uow: trace.json absent after one-cycle wait for %s — "
                    "proceeding with prescription (contract violation)",
                    uow_id,
                )
                violation_entry = {
                    "event": "trace_gate_contract_violation",
                    "uow_id": uow_id,
                    "steward_cycles": cycles,
                    "output_ref": output_ref_for_gate,
                    "message": "trace.json absent after one-cycle wait — proceeding with prescription (contract violation)",
                }
                current_log_str = _append_steward_log_entry(
                    registry, uow_id, current_log_str, violation_entry
                )
                if not dry_run:
                    _write_steward_fields(registry, uow_id, steward_log=current_log_str)
                    registry.append_audit_log(uow_id, {
                        "event": "trace_gate_contract_violation",
                        "actor": _ACTOR_STEWARD,
                        "uow_id": uow_id,
                        "steward_cycles": cycles,
                        "note": json.dumps({
                            "message": "trace.json absent after one-cycle wait — proceeding with prescription (contract violation)",
                            "output_ref": output_ref_for_gate,
                        }),
                        "timestamp": _now_iso(),
                    })
                _notify_cv = notify_dan or _default_notify_dan
                _notify_cv(
                    uow,
                    f"Executor contract violation: trace.json absent after one-cycle wait for UoW {uow_id}. "
                    f"Prescribing anyway — check executor output at {output_ref_for_gate}.",
                )
        else:
            # trace.json exists — clear any stale trace_gate_waited entries
            cleared_log = _clear_trace_gate_waited(current_log_str)
            if cleared_log != current_log_str:
                current_log_str = cleared_log
                if not dry_run:
                    _write_steward_fields(registry, uow_id, steward_log=current_log_str)

    # PR C, Change 2: Corrective trace injection.
    # When trace.json exists, read it and inject its content into the prescription context.
    # This happens after the trace gate check so we only inject when a valid trace is present.
    _trace_data: dict | None = None
    _trace_surprises: list[str] = []
    _trace_prescription_delta: str = ""
    _trace_gate_score: dict | None = None

    if output_ref_for_gate:
        _trace_data = _read_trace_json(output_ref_for_gate, expected_uow_id=uow_id)
        if _trace_data is not None:
            _trace_surprises = _trace_data.get("surprises") or []
            _raw_prescription_delta = _trace_data.get("prescription_delta") or ""
            # Read historical prescription_deltas from corrective_traces for bounding.
            # Routed through Registry to keep all corrective_traces reads behind the
            # abstraction layer — no raw sqlite3 connections at call sites.
            _prior_deltas: list[str] = registry.get_corrective_trace_history(uow_id)
            _trace_prescription_delta = _bound_prescription_delta(
                _raw_prescription_delta, history=_prior_deltas
            )
            _trace_gate_score = _trace_data.get("gate_score")

            # Write trace_injection entry to steward_log
            _trace_log_entry = {
                "event": "trace_injection",
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "register": _trace_data.get("register", uow.register),
                "gate_score": _trace_gate_score,
                "surprises_count": len(_trace_surprises),
                "prescription_delta_present": bool(_trace_prescription_delta),
                "timestamp": _now_iso(),
            }
            current_log_str = _append_steward_log_entry(
                registry, uow_id, current_log_str, _trace_log_entry
            )
            if not dry_run:
                _write_steward_fields(registry, uow_id, steward_log=current_log_str)
                registry.append_audit_log(uow_id, {
                    "event": "trace_injection",
                    "actor": _ACTOR_STEWARD,
                    "uow_id": uow_id,
                    "steward_cycles": cycles,
                    "note": json.dumps({
                        "execution_summary": _trace_data.get("execution_summary", ""),
                        "surprises_count": len(_trace_surprises),
                        "prescription_delta_present": bool(_trace_prescription_delta),
                        "gate_score": _trace_gate_score,
                    }),
                    "timestamp": _now_iso(),
                })

    new_cycles = cycles + 1
    prescribed_skills = _select_prescribed_skills(uow, reentry_posture)
    selected_executor_type = _select_executor_type(uow)

    # Register-mismatch gate (Change 3): check compatibility before writing artifact.
    # If the selected executor_type is incompatible with the UoW's register, block
    # dispatch and surface to Dan. The gate only fires in the prescribe branch (4c).
    is_compatible, mismatch_reason = _check_register_executor_compatibility(
        uow.register, selected_executor_type
    )
    if not is_compatible:
        log.warning(
            "_process_uow: register_mismatch for %s — register=%r executor_type=%r: %s",
            uow_id, uow.register, selected_executor_type, mismatch_reason,
        )
        _direction = f"{uow.register}\u2192{selected_executor_type}"
        mismatch_obs = {
            "event": "register_mismatch_observation",
            "uow_id": uow_id,
            "register": uow.register,
            "executor_type_attempted": selected_executor_type,
            "direction": _direction,
            "steward_cycles": cycles,
            "timestamp": _now_iso(),
        }
        mismatch_log_entry = {
            "event": "register_mismatch",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "register": uow.register,
            "executor_type_attempted": selected_executor_type,
            "direction": _direction,
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, mismatch_log_entry)
        if not dry_run:
            _write_steward_fields(registry, uow_id, steward_log=current_log_str)
            registry.append_audit_log(uow_id, {
                "event": "register_mismatch_observation",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "register": uow.register,
                "executor_type_attempted": selected_executor_type,
                "direction": _direction,
                "steward_cycles": cycles,
                "timestamp": _now_iso(),
            })
            registry.transition(uow_id, _STATUS_BLOCKED, _STATUS_DIAGNOSING)

        _notify_mismatch = notify_dan or _default_notify_dan
        _notify_mismatch(uow, "register_mismatch", surface_log=current_log_str, return_reason=return_reason)
        return Surfaced(uow_id=uow_id, condition="register_mismatch")

    partial_steps_context: str = ""
    if executor_outcome == "partial" and uow.output_ref:
        # Read steps_completed/steps_total from result file for partial continuation
        output_ref_path = uow.output_ref
        result_file = Path(output_ref_path).with_suffix(".result.json")
        if not result_file.exists():
            result_file_alt = Path(str(output_ref_path) + ".result.json")
            if result_file_alt.exists():
                result_file = result_file_alt
        if result_file.exists():
            try:
                result_data = json.loads(result_file.read_text(encoding="utf-8"))
                steps_completed = result_data.get("steps_completed")
                steps_total = result_data.get("steps_total")
                if steps_completed is not None or steps_total is not None:
                    partial_steps_context = (
                        f"steps_completed={steps_completed}, steps_total={steps_total}"
                    )
            except (json.JSONDecodeError, OSError):
                pass

    if executor_outcome == "partial" and partial_steps_context:
        completion_gap_for_prescription = (
            f"{completion_rationale} [{partial_steps_context}]"
        )
        route_reason = (
            f"steward: {reentry_posture} — partial continuation "
            f"({partial_steps_context}) — {completion_rationale[:80]}"
        )
    else:
        completion_gap_for_prescription = completion_rationale
        route_reason = f"steward: {reentry_posture} — {completion_rationale[:120]}"

    # PR C, Change 2: Inject trace content into prescription context.
    # Surprises and prescription_delta from the corrective trace are injected here
    # so the LLM prescriber sees them in the completion_gap context string.
    if _trace_surprises:
        surprises_text = "; ".join(str(s) for s in _trace_surprises)
        completion_gap_for_prescription = (
            f"Executor reported surprises: {surprises_text}. {completion_gap_for_prescription}"
        )
    if _trace_prescription_delta:
        completion_gap_for_prescription = (
            f"{completion_gap_for_prescription} "
            f"Executor recommends prescription change: {_trace_prescription_delta}"
        )
    # For iterative-convergent: include gate_score in completion_gap
    if uow.register == "iterative-convergent" and _trace_gate_score:
        score_val = _trace_gate_score.get("score", "unknown")
        gate_cmd = _trace_gate_score.get("command", "")
        completion_gap_for_prescription = (
            f"{completion_gap_for_prescription} "
            f"[gate_score={score_val}, cmd={gate_cmd!r}]"
        )

    issue_body = issue_info.get("body", "") if issue_info else ""

    # Fetch prior prescription attempts from steward_log when re-prescribing
    # (cycles > 0).  This lets the Executor see what was already tried so it
    # can avoid repeating approaches that did not work.
    prior_prescriptions = (
        _fetch_prior_prescriptions(current_log_str, limit=3)
        if cycles > 0
        else []
    )

    # Wrap llm_prescriber to capture which path was taken (llm vs fallback).
    # The sentinel records a non-None return, indicating the LLM path succeeded.
    _llm_path_taken: list[bool] = [False]

    def _capturing_prescriber(
        uow_arg: UoW,
        reentry_posture_arg: str,
        completion_gap_arg: str,
        issue_body_arg: str = "",
    ) -> dict[str, Any] | None:
        result = llm_prescriber(uow_arg, reentry_posture_arg, completion_gap_arg, issue_body_arg)  # type: ignore[misc]
        if result is not None:
            _llm_path_taken[0] = True
        return result

    effective_prescriber = _capturing_prescriber if llm_prescriber is not None else None

    try:
        instructions = _build_prescription_instructions(
            uow, reentry_posture, completion_gap_for_prescription, issue_body,
            llm_prescriber=effective_prescriber,
            prior_prescriptions=prior_prescriptions,
        )
    except LLMPrescriptionError as e:
        # LLM prescription failed hard — do not fall back, raise error
        log.error(
            "_process_uow: LLM prescription failed for %s — %s",
            uow_id, str(e),
        )
        # Write error audit entry
        if not dry_run:
            registry.append_audit_log(uow_id, {
                "event": "llm_prescription_error",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "error_message": str(e),
                "timestamp": _now_iso(),
            })
            # Transition back to ready-for-steward to allow retry
            # (or remain in diagnosing if transition fails)
            try:
                registry.transition(uow_id, _STATUS_READY_FOR_STEWARD, _STATUS_DIAGNOSING)
            except Exception as transition_err:
                log.error(
                    "_process_uow: failed to transition UoW %s back to ready-for-steward: %s",
                    uow_id, transition_err,
                )
        raise

    # Prescription always comes from LLM now; fallback path has been removed.
    # If LLM prescription fails, an exception is raised (fail-hard behavior).
    prescription_path = "llm"

    # Update agenda: mark current pending node as prescribed
    # Use trace_agenda (which includes the trace entry we just wrote) as the base
    # so the prescription status update is applied on top of the full trace.
    updated_agenda = _mark_current_agenda_node_prescribed(trace_agenda)

    prescription_log_entry = {
        "event": "reentry_prescription" if cycles > 0 else "prescription",
        "uow_id": uow_id,
        "steward_cycles": cycles,
        "return_reason": return_reason,
        "completion_assessment": completion_rationale,
        "prescription_path": prescription_path,
        "dod_revised": False,
        "agenda_revised": False,
        "next_posture_rationale": route_reason,
    }
    current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, prescription_log_entry)

    if not dry_run:
        # Write workflow artifact to disk first
        artifact_path = _write_workflow_artifact(
            uow_id=uow_id,
            instructions=instructions,
            prescribed_skills=prescribed_skills,
            artifact_dir=artifact_dir,
            executor_type=selected_executor_type,
        )

        # Audit-before-transition: write agenda_update audit entry BEFORE updating
        # steward_agenda in the DB. Only on re-entry (cycles > 0) — the initial
        # agenda_update is written in the cycles == 0 initialization block above.
        if cycles > 0:
            registry.append_audit_log(uow_id, {
                "event": "agenda_update",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "agenda_snapshot": updated_agenda,
                "timestamp": _now_iso(),
            })

        # Write all steward fields BEFORE status transition
        _write_steward_fields(
            registry, uow_id,
            steward_agenda=json.dumps(updated_agenda),
            steward_log=current_log_str,
            workflow_artifact=artifact_path,
            prescribed_skills=json.dumps(prescribed_skills),
            route_reason=route_reason,
            steward_cycles=new_cycles,
        )

        # Write prescription audit entry (before status transition)
        registry.append_audit_log(uow_id, {
            "event": "steward_prescription",
            "actor": _ACTOR_STEWARD,
            "uow_id": uow_id,
            "steward_cycles": new_cycles,
            "workflow_primitive": selected_executor_type,
            "prescribed_skills": prescribed_skills,
            "prescription_source": "llm" if _llm_path_taken[0] else "deterministic",
            "instructions_preview": instructions[:80],
            "prescription_path": prescription_path,
            "timestamp": _now_iso(),
        })

        # Transition status to ready-for-executor
        registry.transition(uow_id, _STATUS_READY_FOR_EXECUTOR, _STATUS_DIAGNOSING)

        # Issue #648 Part A — collapse the polling hop.
        # When inline_executor is provided, invoke the Executor immediately after
        # the READY_FOR_EXECUTOR transition rather than waiting for the next
        # heartbeat cycle (which would add 0–3 min of unnecessary latency).
        # The Executor's optimistic lock (step 2 in _claim) guards against
        # double-execution if a concurrent heartbeat fires between transition
        # and inline call — ClaimRejected is logged and re-raised, which the
        # caller's exception handler surfaces.
        if inline_executor is not None:
            try:
                inline_executor(uow_id)
                log.info(
                    "Steward: inline executor dispatch complete for UoW %s",
                    uow_id,
                )
            except Exception as exc:
                log.warning(
                    "Steward: inline executor dispatch failed for UoW %s — %s: %s. "
                    "UoW remains in ready-for-executor; heartbeat will retry.",
                    uow_id, type(exc).__name__, exc,
                )

    # Early warning: fire when new_cycles reaches the early-warning threshold.
    # Fires regardless of dry_run so tests can capture the notification.
    if new_cycles == _EARLY_WARNING_CYCLES:
        _notify_early = notify_dan_early_warning or _default_notify_dan_early_warning
        _notify_early(uow, return_reason, new_cycles)

    _append_cycle_trace(
        uow_id=uow_id,
        cycle_num=cycles,
        subagent_excerpt=_read_output_ref(uow.output_ref),
        return_reason=return_reason or "",
        next_action="prescribed",
        artifact_dir=artifact_dir,
    )
    return Prescribed(uow_id=uow_id, cycles=new_cycles)


# ---------------------------------------------------------------------------
# Main steward cycle (entry point for tests and heartbeat script)
# ---------------------------------------------------------------------------

def run_steward_cycle(
    registry=None,
    dry_run: bool = False,
    github_client: Callable[[int], dict[str, Any]] | None = None,
    artifact_dir: Path | None = None,
    notify_dan: Callable | None = None,
    notify_dan_early_warning: Callable | None = None,
    bootup_candidate_gate: bool | None = None,
    db_path: Path | None = None,
    llm_prescriber: Callable[..., dict[str, Any] | None] | None = _llm_prescribe,
    inline_executor: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """
    Execute one full Steward heartbeat cycle.

    Processes all `ready-for-steward` UoWs through the diagnosis loop.

    Parameters
    ----------
    registry:
        Registry instance. If None, opens production DB.
    dry_run:
        If True, diagnose without writing artifacts or transitioning state.
    github_client:
        Callable(issue_number) → {status_code, state, labels, body, title}.
        Defaults to the production gh CLI client.
    artifact_dir:
        Override for the artifact directory path. Used in tests.
    notify_dan:
        Callable(uow, condition, surface_log, return_reason) for surface-to-Dan
        notifications. Defaults to the production notification path.
    notify_dan_early_warning:
        Callable(uow, return_reason) for early-warning notifications when
        steward_cycles reaches _EARLY_WARNING_CYCLES (4). Defaults to the
        production notification path.
    bootup_candidate_gate:
        Override for BOOTUP_CANDIDATE_GATE. If None, uses the module constant.
    db_path:
        Path to registry DB. Only used if registry is None.
    llm_prescriber:
        Callable(uow, reentry_posture, completion_gap, issue_body) → dict | None.
        Called during prescription to generate LLM-quality instructions.
        Inject None to bypass LLM (tests), or a stub to capture calls.
        Defaults to _llm_prescribe (production path).
    inline_executor:
        Optional callable(uow_id) invoked immediately after the READY_FOR_EXECUTOR
        transition, collapsing the 0–3 min polling hop (issue #648 Part A).
        Defaults to None — heartbeat remains the sole dispatch path.

    Returns
    -------
    dict with keys:
        evaluated: int — UoWs processed
        prescribed: int — UoWs advanced to ready-for-executor
        done: int — UoWs closed as done
        surfaced: int — UoWs surfaced to Dan
        skipped: int — UoWs skipped (gate, race, etc.)
        race_skipped: int — UoWs skipped due to optimistic lock race
        considered_ids: list[str] — IDs of UoWs considered in this cycle
    """
    from src.orchestration.registry import Registry

    if registry is None:
        if db_path is None:
            workspace = Path(os.environ.get(
                "LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"
            ))
            db_path = workspace / "orchestration" / "registry.db"
        registry = Registry(db_path)

    _github_client = github_client or _default_github_client
    _gate = bootup_candidate_gate if bootup_candidate_gate is not None else BOOTUP_CANDIDATE_GATE

    # Step 0: Schema validation
    conn = registry._connect()
    try:
        validate_steward_schema(conn)
    finally:
        conn.close()

    # Ensure artifact directory exists
    if artifact_dir is not None:
        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
    else:
        default_artifact_dir = Path(os.path.expanduser(
            "~/lobster-workspace/orchestration/artifacts"
        ))
        default_artifact_dir.mkdir(parents=True, exist_ok=True)

    # Fetch all ready-for-steward UoWs
    try:
        uows = registry.query(status=_STATUS_READY_FOR_STEWARD)
    except AttributeError:
        # Fallback for pre-#327 registry (no query method yet)
        uows = registry.list(status=_STATUS_READY_FOR_STEWARD)

    log.debug("Steward cycle: %d ready-for-steward UoWs found", len(uows))

    evaluated = 0
    prescribed = 0
    done = 0
    surfaced = 0
    skipped = 0
    race_skipped = 0
    considered_ids = []

    for uow in uows:
        uow_id = uow.id
        source_issue_number = uow.source_issue_number
        considered_ids.append(uow_id)

        # Resolve the GitHub client for this UoW. When issue_url is present
        # (populated at proposal time since migration 0005), derive the repo
        # from the URL — no hardcoded repo slug. For pre-migration UoWs where
        # issue_url is NULL, fall back to _github_client (which uses the legacy
        # hardcoded repo). Pure resolution: no side effects.
        resolved_repo = _repo_from_issue_url(getattr(uow, "issue_url", None))
        def _resolve_issue_info(n: int) -> dict[str, Any]:
            if resolved_repo:
                return _fetch_github_issue(n, resolved_repo)
            return _github_client(n)

        # BOOTUP_CANDIDATE_GATE: skip if label present and gate is True
        if _gate and source_issue_number:
            issue_info = _resolve_issue_info(source_issue_number)
            labels = issue_info.get("labels", [])
            if "bootup-candidate" in labels:
                log.debug(
                    "UoW %s (issue #%s) skipped: bootup-candidate gate is active",
                    uow_id, source_issue_number
                )
                skipped += 1
                continue
        else:
            issue_info = (
                _resolve_issue_info(source_issue_number)
                if source_issue_number
                else None
            )

        evaluated += 1
        audit_entries = _fetch_audit_entries(registry, uow_id)

        # Backpressure gate (#617): skip re-prescription when the UoW was
        # returned from ready-for-executor via startup_sweep executor_orphan.
        # This happens when the executor queue is saturated — the startup sweep
        # transitions the UoW back to ready-for-steward, but prescribing again
        # consumes an LLM call without making progress.  Instead, log a
        # backpressure event and leave the UoW in ready-for-steward so it will
        # be re-evaluated on the next heartbeat after the queue drains.
        #
        # Note: _most_recent_classification scans for startup_sweep events only,
        # so executor_orphan return_reasons from execution_complete events (a
        # different scenario) are not intercepted here.
        #
        # Exception (#fix-backpressure-gate): if execution is currently enabled,
        # the executor_orphan classification is stale — it was written when the
        # executor was not running (e.g. execution_enabled=false at startup_sweep
        # time).  When execution is now active, the orphan hold is incorrect and
        # would permanently block the UoW.  Only apply the backpressure hold when
        # execution is disabled (the orphan condition is genuinely valid).
        _sweep_classification = _most_recent_classification(audit_entries)
        if _sweep_classification == "executor_orphan":
            try:
                from src.orchestration.dispatcher_handlers import is_execution_enabled
                _execution_currently_enabled = is_execution_enabled()
            except Exception:
                _execution_currently_enabled = False

            if _execution_currently_enabled:
                log.info(
                    "backpressure: uow_id=%s has stale executor_orphan classification "
                    "but execution is enabled — proceeding with re-prescription (cycle %d)",
                    uow_id,
                    uow.steward_cycles,
                )
            else:
                log.info(
                    "backpressure: uow_id=%s already in ready-for-executor, "
                    "skipping re-prescription (cycle %d)",
                    uow_id,
                    uow.steward_cycles,
                )
                if not dry_run:
                    registry.append_audit_log(uow_id, {
                        "event": "backpressure",
                        "actor": _ACTOR_STEWARD,
                        "uow_id": uow_id,
                        "steward_cycles": uow.steward_cycles,
                        "note": "executor_orphan: skipping re-prescription, execution disabled or queue saturated",
                        "timestamp": _now_iso(),
                    })
                skipped += 1
                continue

        try:
            result = _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=audit_entries,
                issue_info=issue_info,
                dry_run=dry_run,
                artifact_dir=artifact_dir,
                notify_dan=notify_dan,
                notify_dan_early_warning=notify_dan_early_warning,
                llm_prescriber=llm_prescriber,
                inline_executor=inline_executor,
            )
        except Exception:
            log.exception("Steward: unhandled error processing UoW %s — skipping", uow_id)
            skipped += 1
            continue

        match result:
            case Prescribed():
                prescribed += 1
            case Done():
                done += 1
            case Surfaced():
                surfaced += 1
            case RaceSkipped():
                race_skipped += 1
            case _:
                skipped += 1

    return {
        "evaluated": evaluated,
        "prescribed": prescribed,
        "done": done,
        "surfaced": surfaced,
        "skipped": skipped,
        "race_skipped": race_skipped,
        "considered_ids": considered_ids,
    }
