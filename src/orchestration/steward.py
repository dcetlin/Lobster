"""
Steward — WOS Phase 2 core diagnosis and prescription engine.

The Steward runs every 3 minutes (via steward-heartbeat.py). On each
invocation it processes all `ready-for-steward` UoWs through the
diagnosis→prescribe/close/surface cycle.

Design constraints enforced here:
- Audit-before-transition: every state change writes an audit entry BEFORE
  the transition. If the audit write fails, the transition does not happen.
- Optimistic lock: `UPDATE ... WHERE status = 'ready-for-steward'` checks
  rows affected. If 0, another Steward instance claimed it — skip silently.
- BOOTUP_CANDIDATE_GATE: when True, UoWs whose GitHub issue carries the
  `bootup-candidate` label are skipped. Default is True until the Phase 2
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
import sys
import time
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.orchestration.registry import UoW

log = logging.getLogger("steward")

# ---------------------------------------------------------------------------
# LLM prescription model
# ---------------------------------------------------------------------------

# Use haiku for cost efficiency — prescriptions are structured short outputs.
_LLM_PRESCRIPTION_MODEL = "claude-haiku-4-5"

# Hard timeout for the LLM prescription call (seconds). If exceeded, falls
# back to the deterministic template.
_LLM_PRESCRIPTION_TIMEOUT_SECS = 30


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
    Phase 2 validation sequence has passed and all UoWs should be processed.

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

# Hard cap: surface to Dan unconditionally if steward_cycles >= this value
_HARD_CAP_CYCLES = 5

# Early warning threshold: notify Dan when steward_cycles reaches this value
_EARLY_WARNING_CYCLES = 4

# Crash surface threshold: surface if crashed_no_output and cycles >= this value
_CRASH_SURFACE_CYCLES = 2

# Phase 2 fields required by the Steward
_PHASE2_REQUIRED_FIELDS = frozenset({
    "workflow_artifact",
    "success_criteria",
    "prescribed_skills",
    "steward_cycles",
    "timeout_at",
    "estimated_runtime",
    "steward_agenda",
    "steward_log",
})

# Executor types
_EXECUTOR_TYPE_GENERAL = "general"

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
    except Exception:
        return False


def _read_output_ref(output_ref: str | None) -> str:
    """Read and return output_ref file contents, or empty string."""
    if not output_ref:
        return ""
    try:
        return Path(output_ref).read_text(encoding="utf-8")
    except Exception:
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
    - steward_cycles < HARD_CAP_CYCLES
    """
    cycles = uow.steward_cycles
    if cycles >= _HARD_CAP_CYCLES:
        return False, f"hard_cap: steward_cycles={cycles} >= {_HARD_CAP_CYCLES}", None

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
    success_criteria = uow.success_criteria
    if success_criteria:
        # Conservative fallback: without a valid result file we cannot
        # deterministically verify completion against success_criteria.
        # Do not declare done — require the Executor to write a result file.
        return False, (
            f"no structured result file ({output_ref}.result.json) found — "
            f"cannot verify success_criteria without Executor confirmation: {success_criteria[:80]}"
        ), None
    else:
        # Phase 1 / legacy fallback: no success_criteria and no result file.
        # Trust the output_ref + execution_complete posture.
        return True, f"success_criteria is NULL — output_ref present with execution_complete posture: {uow.summary[:80]}", None


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


def _llm_prescribe(
    uow: UoW,
    reentry_posture: str,
    completion_gap: str,
    issue_body: str = "",
) -> dict[str, Any] | None:
    """
    Call Claude to generate a tailored prescription for the given UoW.

    Returns a dict with keys:
      - "instructions": str — full instruction block for the Executor
      - "success_criteria_check": str — how to verify completion
      - "estimated_cycles": int — expected execution passes needed

    Returns None if the LLM call fails, is unavailable, or times out.
    The caller must fall back to the deterministic template on None.

    This function is a pure side-effect boundary: the only observable effect
    is the Anthropic API call. All inputs are immutable value types.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning(
            "LLM prescription unavailable: ANTHROPIC_API_KEY not set — using deterministic fallback"
        )
        return None

    try:
        import anthropic  # local import — optional dependency
    except ImportError:
        log.debug("_llm_prescribe: anthropic package not installed, skipping LLM call")
        return None

    # Build prior prescription summary from steward_log if available
    prior_prescriptions: list[str] = []
    if uow.steward_log:
        try:
            for line in uow.steward_log.strip().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
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
        "The Executor is a capable autonomous coding agent — write instructions at that level."
    )

    user_prompt = f"""Given this Unit of Work, write a precise prescription for the Executor.

{uow_context}

Respond with a JSON object only (no markdown, no explanation outside the JSON):
{{
  "instructions": "<complete, actionable instructions for the Executor — include the specific steps, what to produce, where to write output, and any constraints from the success criteria>",
  "success_criteria_check": "<one or two sentences describing exactly how to verify the work is complete — what to check, what file exists, what content to confirm>",
  "estimated_cycles": <integer 1-3 — how many Executor passes this is expected to need>
}}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=_LLM_PRESCRIPTION_MODEL,
            max_tokens=1024,
            timeout=_LLM_PRESCRIPTION_TIMEOUT_SECS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            raw_text = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()

        parsed = json.loads(raw_text)

        instructions = str(parsed.get("instructions", "")).strip()
        success_criteria_check = str(parsed.get("success_criteria_check", "")).strip()
        estimated_cycles = int(parsed.get("estimated_cycles", 1))

        if not instructions:
            log.warning("_llm_prescribe: LLM returned empty instructions, falling back")
            return None

        log.debug(
            "_llm_prescribe: LLM prescription generated for %s (estimated_cycles=%d)",
            uow.id, estimated_cycles,
        )
        return {
            "instructions": instructions,
            "success_criteria_check": success_criteria_check,
            "estimated_cycles": max(1, min(3, estimated_cycles)),
        }

    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_llm_prescribe: LLM call failed for %s (%s: %s), falling back to deterministic template",
            uow.id, type(exc).__name__, exc,
        )
        return None


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
        if entry.get("event") in ("prescription", "reentry_prescription"):
            prescriptions.append(entry)

    # Return the last `limit` entries (oldest-first ordering preserved).
    return prescriptions[-limit:] if prescriptions else []


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

    Tries the LLM-class prescription path first (via llm_prescriber). If that
    returns None (API unavailable, timeout, parse failure), falls back to the
    deterministic keyword-matching template.

    Args:
        uow: The Unit of Work being prescribed.
        reentry_posture: Categorized executor state from diagnosis.
        completion_gap: Human-readable rationale for why work is incomplete.
        issue_body: Raw GitHub issue body text. Used when success_criteria is absent.
        llm_prescriber: Callable that takes (uow, reentry_posture, completion_gap,
            issue_body) and returns a dict or None. Inject None or a stub in tests
            to bypass the LLM call. Defaults to _llm_prescribe.
        prior_prescriptions: List of prior steward_log prescription entries
            (from _fetch_prior_prescriptions). Passed to the deterministic fallback
            to inject re-prescription context. Ignored on the LLM path (LLM reads
            steward_log directly via uow). Pass None or [] for the first cycle.
    """
    if llm_prescriber is not None:
        llm_result = llm_prescriber(uow, reentry_posture, completion_gap, issue_body)
        if llm_result is not None:
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

    # Deterministic fallback
    return _build_deterministic_prescription_instructions(
        uow, reentry_posture, completion_gap, issue_body,
        prior_prescriptions=prior_prescriptions,
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_phase2_schema(conn: sqlite3.Connection) -> None:
    """
    Validate that all Phase 2 fields are present in uow_registry.

    Raises RuntimeError with a specific message if any Phase 2 field is absent.
    Call this at Steward startup before processing any UoW.

    Args:
        conn: An open SQLite connection to the registry database.

    Raises:
        RuntimeError: If any Phase 2 field is missing. Message includes
            "schema migration not applied" and the list of missing fields.
    """
    rows = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
    existing_cols = {row[1] for row in rows}
    missing = _PHASE2_REQUIRED_FIELDS - existing_cols
    if missing:
        missing_sorted = sorted(missing)
        raise RuntimeError(
            f"schema migration not applied — run scripts/migrate_add_steward_fields.py first. "
            f"Missing fields: {missing_sorted}"
        )


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
    """
    cycles = uow.steward_cycles

    if cycles >= _HARD_CAP_CYCLES:
        return "hard_cap"

    # crashed_no_output + cycles >= 2
    if return_reason == "crashed_no_output" and cycles >= _CRASH_SURFACE_CYCLES:
        return "crash_repeated"

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

def _default_github_client(issue_number: int) -> dict[str, Any]:
    """
    Fetch issue info from GitHub using gh CLI.

    Returns dict with keys: status_code, state, labels (list), body, title.
    On any error, returns status_code=0 with empty fields.
    """
    import subprocess
    try:
        result = subprocess.run(
            [
                "gh", "issue", "view", str(issue_number),
                "--repo", "dcetlin/Lobster",
                "--json", "state,labels,body,title",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return {"status_code": 1, "state": None, "labels": [], "body": "", "title": ""}
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
        log.warning("GitHub client error for issue %s: %s", issue_number, e)
        return {"status_code": 0, "state": None, "labels": [], "body": "", "title": ""}


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def _write_workflow_artifact(
    uow_id: str,
    instructions: str,
    prescribed_skills: list[str],
    artifact_dir: Path | None = None,
) -> str:
    """
    Write a WorkflowArtifact JSON file to disk.

    Returns the absolute path to the written file.
    artifact_dir: override for the artifact directory (used in tests).
    """
    try:
        from src.orchestration.workflow_artifact import WorkflowArtifact, to_json
        artifact = WorkflowArtifact(
            uow_id=uow_id,
            executor_type=_EXECUTOR_TYPE_GENERAL,
            constraints=[],
            prescribed_skills=prescribed_skills,
            instructions=instructions,
        )
        artifact_json = to_json(artifact)
    except ImportError:
        # Fallback if workflow_artifact.py not yet on branch (pre-merge)
        artifact_data = {
            "uow_id": uow_id,
            "executor_type": _EXECUTOR_TYPE_GENERAL,
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
    """
    uow_id = uow.id
    cycles = uow.steward_cycles
    log.warning(
        "SURFACE TO DAN: UoW %s — condition=%s cycles=%s",
        uow_id, condition, cycles,
    )
    msg_id = str(uuid.uuid4())
    if condition == "hard_cap":
        body_lines = [
            f"🚨 WOS: UoW `{uow_id}` hit cycle cap ({_HARD_CAP_CYCLES}). "
            f"return_reason: {return_reason}. Surfacing for Dan review.",
        ]
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
) -> StewardOutcome:
    """
    Process a single UoW through the full diagnosis + prescribe/close/surface cycle.

    Returns a StewardOutcome: Prescribed | Done | Surfaced | RaceSkipped.
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

        return Surfaced(uow_id=uow_id, condition=stuck_condition)

    # 4b: Declare done
    if is_complete:
        # Mark all agenda nodes complete
        completed_agenda = _update_agenda_node_status(agenda, "complete")

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
            registry.transition(uow_id, _STATUS_DONE, _STATUS_DIAGNOSING)

        return Done(uow_id=uow_id)

    # 4c: Prescribe another Executor pass
    # executor-contract.md Steward Interpretation Table: `partial` and `failed`
    # require distinct re-diagnosis inputs. For `partial`, include
    # steps_completed/steps_total from the result file so the prescription
    # reflects how far the previous execution got. For `failed`, re-diagnose
    # with `reason` as the primary input (already in completion_rationale).
    new_cycles = cycles + 1
    prescribed_skills = _select_prescribed_skills(uow, reentry_posture)

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

    instructions = _build_prescription_instructions(
        uow, reentry_posture, completion_gap_for_prescription, issue_body,
        llm_prescriber=effective_prescriber,
        prior_prescriptions=prior_prescriptions,
    )

    prescription_path = "llm" if _llm_path_taken[0] else "fallback"

    # Update agenda: mark current pending node as prescribed
    updated_agenda = _mark_current_agenda_node_prescribed(agenda)

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
            "workflow_primitive": _EXECUTOR_TYPE_GENERAL,
            "prescribed_skills": prescribed_skills,
            "instructions_preview": instructions[:80],
            "timestamp": _now_iso(),
        })

        # Transition status to ready-for-executor
        registry.transition(uow_id, _STATUS_READY_FOR_EXECUTOR, _STATUS_DIAGNOSING)

    # Early warning: fire when new_cycles reaches the early-warning threshold.
    # Fires regardless of dry_run so tests can capture the notification.
    if new_cycles == _EARLY_WARNING_CYCLES:
        _notify_early = notify_dan_early_warning or _default_notify_dan_early_warning
        _notify_early(uow, return_reason, new_cycles)

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
            db_path = workspace / "data" / "registry.db"
        registry = Registry(db_path)

    _github_client = github_client or _default_github_client
    _gate = bootup_candidate_gate if bootup_candidate_gate is not None else BOOTUP_CANDIDATE_GATE

    # Step 0: Schema validation
    conn = registry._connect()
    try:
        validate_phase2_schema(conn)
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

        # BOOTUP_CANDIDATE_GATE: skip if label present and gate is True
        if _gate and source_issue_number:
            issue_info = _github_client(source_issue_number)
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
                _github_client(source_issue_number)
                if source_issue_number
                else None
            )

        evaluated += 1
        audit_entries = _fetch_audit_entries(registry, uow_id)

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
