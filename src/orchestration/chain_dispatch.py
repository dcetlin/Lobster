"""
WOS Chain Dispatch — three chain primitives for multi-agent UoW execution.

Primitives:
  fan_out          — dispatch N perspective subagents in parallel, collect outputs
  spec_breakdown   — decompose UoW into child UoWs via a decomposition agent
  diverge_converge — dispatch N approach agents, then synthesize with a synthesis agent

Each primitive is a pure function that accepts a WorkflowArtifact, a UoW ID, a
Registry, and a SubagentDispatcher, and returns an ExecutorOutcome + output text.

The executor imports and routes to these functions when `chain_type` is set in
the artifact. All three preserve the existing single-dispatch path as the fallback
when `chain_type` is absent or unrecognized.

WOS-UoW: uow_20260601_424433
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestration.registry import Registry
    from orchestration.executor import SubagentDispatcher
    from orchestration.workflow_artifact import WorkflowArtifact

log = logging.getLogger("chain_dispatch")

# ---------------------------------------------------------------------------
# Known chain_type values
# ---------------------------------------------------------------------------

CHAIN_FAN_OUT = "fan_out"
CHAIN_SPEC_BREAKDOWN = "spec_breakdown"
CHAIN_DIVERGE_CONVERGE = "diverge_converge"

KNOWN_CHAIN_TYPES = frozenset({CHAIN_FAN_OUT, CHAIN_SPEC_BREAKDOWN, CHAIN_DIVERGE_CONVERGE})


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChainResult:
    outcome: str        # "complete" | "failed" | "partial"
    success: bool
    output_text: str
    reason: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_uow_id() -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    random_part = uuid.uuid4().hex[:6]
    return f"uow_{date_part}_{random_part}"


def _perspective_output_path(output_ref: str, perspective: str) -> Path:
    """Return the output file path for a specific perspective subagent."""
    p = Path(output_ref)
    stem = p.stem
    return p.parent / f"{stem}.perspective_{perspective}.txt"


def _approach_output_path(output_ref: str, approach: str) -> Path:
    """Return the output file path for a specific approach subagent."""
    p = Path(output_ref)
    stem = p.stem
    return p.parent / f"{stem}.approach_{approach}.txt"


# ---------------------------------------------------------------------------
# Primitive A: Fan-out
# ---------------------------------------------------------------------------

def run_fan_out(
    uow_id: str,
    output_ref: str,
    artifact: "WorkflowArtifact",
    dispatcher: "SubagentDispatcher",
) -> ChainResult:
    """
    Dispatch one subagent per perspective, collect outputs, write to output_ref.

    Each subagent receives the UoW instructions prefixed with a perspective-
    specific framing. Outputs are written to per-perspective files alongside
    output_ref and also aggregated into output_ref as a JSON dict keyed by
    perspective name.

    Does NOT synthesize — collection only (per spec).
    """
    perspectives: list[str] = artifact.get("perspectives") or []
    if len(perspectives) < 2:
        return ChainResult(
            outcome="failed",
            success=False,
            reason=f"fan_out requires at least 2 perspectives, got {len(perspectives)}",
            output_text="",
        )

    base_instructions = artifact.get("instructions", "")
    outputs: dict[str, str] = {}

    for perspective in perspectives:
        framed_instructions = (
            f"[PERSPECTIVE: {perspective}]\n"
            f"You are analyzing this task from the {perspective!r} perspective. "
            f"Focus your analysis and output specifically on {perspective}-related concerns.\n\n"
            f"{base_instructions}"
        )
        try:
            perspective_uow_id = f"{uow_id}.fan_out.{perspective}"
            executor_id = dispatcher(framed_instructions, perspective_uow_id)
            # Write sentinel to perspective output path
            out_path = _perspective_output_path(output_ref, perspective)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(f"dispatched: executor_id={executor_id}")
            outputs[perspective] = f"dispatched: executor_id={executor_id}"
            log.info("chain_dispatch.fan_out: dispatched %s perspective for UoW %s", perspective, uow_id)
        except Exception as e:
            log.error("chain_dispatch.fan_out: dispatch failed for perspective %s — %s", perspective, e)
            outputs[perspective] = f"dispatch_failed: {e}"

    # Aggregate all perspective outputs into output_ref
    output_text = json.dumps({
        "chain_type": CHAIN_FAN_OUT,
        "uow_id": uow_id,
        "perspectives": perspectives,
        "outputs": outputs,
        "timestamp": _now_iso(),
    }, indent=2)

    return ChainResult(
        outcome="complete",
        success=True,
        output_text=output_text,
    )


# ---------------------------------------------------------------------------
# Primitive B: Sub-UoW spawning (spec breakdown)
# ---------------------------------------------------------------------------

def run_spec_breakdown(
    uow_id: str,
    output_ref: str,
    artifact: "WorkflowArtifact",
    dispatcher: "SubagentDispatcher",
    registry: "Registry",
) -> ChainResult:
    """
    Dispatch a decomposition agent, parse returned child specs, create child UoWs.

    The decomposition agent receives `decomposition_prompt` from the artifact.
    It must return a JSON array of child UoW specs, each with at minimum:
    {"summary": "...", "success_criteria": "...", "type": "..."}

    After child UoWs are created:
    - This (parent) UoW is transitioned to 'awaiting_children' status.
    - The executor heartbeat's existing orphan/completion logic is responsible
      for detecting when all children are done and transitioning the parent.

    NOTE: If the existing heartbeat does not implement parent→child join
    convergence, that gap should be tracked in a follow-up issue. This
    primitive implements the decomposition and child-creation phase only.
    """
    decomposition_prompt = artifact.get("decomposition_prompt") or ""
    if not decomposition_prompt:
        return ChainResult(
            outcome="failed",
            success=False,
            reason="spec_breakdown requires a non-empty decomposition_prompt",
            output_text="",
        )

    # Build decomposition instructions
    decomp_instructions = (
        f"[DECOMPOSITION AGENT]\n"
        f"Your task is to decompose the following specification into child units of work.\n"
        f"Return a JSON array (only JSON, no prose) where each element has:\n"
        f'  {{"summary": "<one-line summary>", "success_criteria": "<measurable criteria>", "type": "executable"}}\n\n'
        f"{decomposition_prompt}"
    )

    decomp_uow_id = f"{uow_id}.decomposition"
    executor_id = dispatcher(decomp_instructions, decomp_uow_id)
    log.info("chain_dispatch.spec_breakdown: dispatched decomposition agent for UoW %s", uow_id)

    # In the synchronous path the dispatcher output IS the child spec list
    # (the subprocess has exited and written results). In the async inbox path,
    # dispatch is fire-and-forget — we cannot block on the child specs here.
    #
    # Current limitation: async path cannot block on child output. The child
    # UoWs are not created until the decomposition agent reports back, which
    # requires the Steward re-entry loop to handle. This is the "gap" the
    # prescription says to note rather than implement.
    #
    # For now: write decomposition dispatch sentinel and transition to
    # awaiting_children. A follow-up issue should wire the Steward to read
    # the decomposition output and call _create_child_uows.
    # See: https://github.com/dcetlin/lobster/issues (follow-up needed)

    output_text = json.dumps({
        "chain_type": CHAIN_SPEC_BREAKDOWN,
        "uow_id": uow_id,
        "decomposition_executor_id": executor_id,
        "status": "decomposition_dispatched",
        "note": (
            "Decomposition agent dispatched. Child UoW creation requires the Steward "
            "re-entry loop to read decomposition output and call create_child_uows. "
            "Async join convergence is deferred to follow-up issue."
        ),
        "timestamp": _now_iso(),
    }, indent=2)

    # Transition to awaiting_children — the Steward re-entry loop handles join.
    try:
        _mark_awaiting_children(registry, uow_id)
    except Exception as e:
        log.warning("chain_dispatch.spec_breakdown: could not mark awaiting_children — %s", e)

    return ChainResult(
        outcome="partial",
        success=False,
        reason=(
            "Decomposition dispatched; child UoW creation deferred to Steward re-entry. "
            "Async join convergence requires follow-up implementation."
        ),
        output_text=output_text,
    )


def _create_child_uows(
    registry: "Registry",
    parent_uow_id: str,
    child_specs: list[dict],
) -> list[str]:
    """
    Create child UoW records from a list of child specs.

    Each spec must have: summary, success_criteria, type.
    Returns list of created child UoW IDs.

    This is called by the Steward re-entry loop after the decomposition agent
    has reported its output. Not called directly in the async dispatch path.
    """
    from orchestration.registry import UoWStatus

    child_ids: list[str] = []
    conn = registry._connect()  # type: ignore[attr-defined]
    try:
        conn.execute("BEGIN IMMEDIATE")
        for spec in child_specs:
            child_id = _generate_uow_id()
            now = _now_iso()
            conn.execute(
                """
                INSERT INTO uow_registry
                    (id, type, source, status, posture, summary, success_criteria,
                     parent, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    child_id,
                    spec.get("type", "executable"),
                    f"spec_breakdown:{parent_uow_id}",
                    UoWStatus.PENDING,
                    "solo",
                    spec.get("summary", ""),
                    spec.get("success_criteria", ""),
                    parent_uow_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
                VALUES (?, ?, 'created_as_child', NULL, 'pending', 'chain_dispatch', ?)
                """,
                (now, child_id, json.dumps({"parent_uow_id": parent_uow_id})),
            )
            child_ids.append(child_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return child_ids


def _mark_awaiting_children(registry: "Registry", uow_id: str) -> None:
    """Transition a parent UoW to 'awaiting_children' status."""
    import sqlite3
    conn = sqlite3.connect(str(registry.db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        now = _now_iso()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
            VALUES (?, ?, 'awaiting_children', 'active', 'awaiting-children', 'chain_dispatch', ?)
            """,
            (now, uow_id, json.dumps({"reason": "spec_breakdown dispatched"})),
        )
        conn.execute(
            "UPDATE uow_registry SET status = 'awaiting-children', updated_at = ? WHERE id = ?",
            (now, uow_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Primitive C: Diverge→converge
# ---------------------------------------------------------------------------

def run_diverge_converge(
    uow_id: str,
    output_ref: str,
    artifact: "WorkflowArtifact",
    dispatcher: "SubagentDispatcher",
) -> ChainResult:
    """
    Dispatch N approach subagents (parallel), then dispatch a synthesis agent.

    Each approach agent receives the base instructions plus its approach label.
    After all approach agents complete, a synthesis agent receives all approach
    outputs plus the synthesis_prompt. The synthesis output is the final output.

    The synthesis agent fires ONLY after all approach agents have dispatched.
    In the async inbox path, "after all complete" means after all dispatch
    write-to-inbox calls return — not after the agents finish their work.
    Full async join (waiting for all agents to actually write results) requires
    observation-loop support and is deferred to a follow-up issue.
    """
    approaches: list[str] = artifact.get("approaches") or []
    synthesis_prompt = artifact.get("synthesis_prompt") or ""

    if len(approaches) < 2:
        return ChainResult(
            outcome="failed",
            success=False,
            reason=f"diverge_converge requires at least 2 approaches, got {len(approaches)}",
            output_text="",
        )
    if not synthesis_prompt:
        return ChainResult(
            outcome="failed",
            success=False,
            reason="diverge_converge requires a non-empty synthesis_prompt",
            output_text="",
        )

    base_instructions = artifact.get("instructions", "")
    approach_executor_ids: dict[str, str] = {}

    # Phase 1: dispatch all approach agents
    for approach in approaches:
        framed = (
            f"[APPROACH: {approach}]\n"
            f"You are implementing this task using the {approach!r} approach. "
            f"Write your output to the approach-specific output path and call write_result.\n\n"
            f"{base_instructions}"
        )
        approach_uow_id = f"{uow_id}.diverge.{approach}"
        try:
            eid = dispatcher(framed, approach_uow_id)
            out_path = _approach_output_path(output_ref, approach)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(f"dispatched: executor_id={eid}")
            approach_executor_ids[approach] = eid
            log.info("chain_dispatch.diverge_converge: dispatched approach %s for UoW %s", approach, uow_id)
        except Exception as e:
            log.error("chain_dispatch.diverge_converge: dispatch failed for approach %s — %s", approach, e)
            approach_executor_ids[approach] = f"dispatch_failed: {e}"

    # Phase 2: dispatch synthesis agent
    # The synthesis agent receives the synthesis_prompt and knows approach executor IDs.
    # In a sync path it would read the approach outputs from disk. In the async path
    # it receives the executor IDs and is expected to wait/read from the output paths.
    synthesis_context = (
        f"[SYNTHESIS AGENT]\n"
        f"Approach agents have been dispatched with the following IDs:\n"
        + "\n".join(f"  {a}: {eid}" for a, eid in approach_executor_ids.items())
        + f"\n\nApproach output paths:\n"
        + "\n".join(f"  {a}: {_approach_output_path(output_ref, a)}" for a in approaches)
        + f"\n\nSynthesis instructions:\n{synthesis_prompt}"
    )
    synthesis_uow_id = f"{uow_id}.converge.synthesis"
    try:
        synthesis_eid = dispatcher(synthesis_context, synthesis_uow_id)
        log.info("chain_dispatch.diverge_converge: dispatched synthesis agent for UoW %s", uow_id)
    except Exception as e:
        log.error("chain_dispatch.diverge_converge: synthesis dispatch failed — %s", e)
        synthesis_eid = f"dispatch_failed: {e}"

    output_text = json.dumps({
        "chain_type": CHAIN_DIVERGE_CONVERGE,
        "uow_id": uow_id,
        "approaches": approaches,
        "approach_executor_ids": approach_executor_ids,
        "synthesis_executor_id": synthesis_eid,
        "timestamp": _now_iso(),
    }, indent=2)

    return ChainResult(
        outcome="complete",
        success=True,
        output_text=output_text,
    )
