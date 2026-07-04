"""
WOS V2 chain primitives — fan_out, diverge_converge, sub_uow.

Three multi-agent dispatch patterns that extend the executor beyond single-agent dispatch:

  fan_out          — dispatch N subagents in parallel, merge results into one output
  diverge_converge — dispatch N diverge agents in parallel, then one converge/synthesis agent
  sub_uow          — spawn child UoWs in the registry; parent waits for all children

Each primitive is a top-level function that accepts the same signature as the
single-agent dispatch path and returns an ExecutorResult. The executor routes
to these functions when chain_type on the UoW is not "single".

Dispatch model
--------------
fan_out and diverge_converge use the synchronous subprocess dispatcher
(_dispatch_via_claude_p) so they can block and collect all branch outputs.
ThreadPoolExecutor is used for parallel dispatch within a primitive.

sub_uow inserts child UoWs into the registry and transitions the parent to
waiting-on-children. The executor-heartbeat monitors children and calls
registry.check_and_complete_if_children_done() on each pass.

Chain config schema
-------------------
The WorkflowArtifact.instructions field is a JSON object for chain primitives.
Fields by chain_type:

  fan_out:
    {
      "branches": [
        {"instructions": "...", "executor_type": "lobster-generalist"},
        ...
      ],
      "merge_strategy": "concatenate"   // optional, default "concatenate"
    }

  diverge_converge:
    {
      "diverge_branches": [
        {"instructions": "...", "executor_type": "lobster-generalist"},
        ...
      ],
      "converge": {
        "instructions_template": "Synthesize: {outputs}",
        "executor_type": "lobster-generalist"
      }
    }

  sub_uow:
    {
      "child_uow_specs": [
        {
          "summary": "...",
          "success_criteria": "...",
          "workflow_artifact_json": "..."  // JSON string of the child WorkflowArtifact
        },
        ...
      ]
    }

Imports:
    from orchestration.chain_primitives import dispatch_fan_out
    from orchestration.chain_primitives import dispatch_diverge_converge
    from orchestration.chain_primitives import dispatch_sub_uow
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestration.registry import Registry

log = logging.getLogger("chain_primitives")

# ---------------------------------------------------------------------------
# ExecutorResult / ExecutorOutcome imports — avoid circular imports by importing
# from executor at function call time when needed, or by re-importing here.
# These are value objects with no side effects on import.
# ---------------------------------------------------------------------------

from orchestration.executor import ExecutorOutcome, ExecutorResult  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_chain_config(instructions_str: str, chain_type: str) -> dict:
    """
    Parse JSON chain config from the WorkflowArtifact instructions field.

    Raises ValueError if the instructions field is not valid JSON or missing
    required keys for the given chain_type.
    """
    try:
        config = json.loads(instructions_str)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"chain_primitives: chain_type={chain_type!r} requires instructions "
            f"to be a JSON object, but got invalid JSON: {e}"
        ) from e

    if not isinstance(config, dict):
        raise ValueError(
            f"chain_primitives: instructions must be a JSON object for chain_type={chain_type!r}"
        )

    if chain_type == "fan_out":
        if "branches" not in config or not isinstance(config["branches"], list):
            raise ValueError(
                "chain_primitives: fan_out requires instructions.branches (list of branch dicts)"
            )
        if not config["branches"]:
            raise ValueError("chain_primitives: fan_out requires at least one branch")
    elif chain_type == "diverge_converge":
        if "diverge_branches" not in config or not isinstance(config["diverge_branches"], list):
            raise ValueError(
                "chain_primitives: diverge_converge requires instructions.diverge_branches (list)"
            )
        if "converge" not in config or not isinstance(config["converge"], dict):
            raise ValueError(
                "chain_primitives: diverge_converge requires instructions.converge (dict)"
            )
        if not config["diverge_branches"]:
            raise ValueError("chain_primitives: diverge_converge requires at least one diverge branch")
    elif chain_type == "sub_uow":
        if "child_uow_specs" not in config or not isinstance(config["child_uow_specs"], list):
            raise ValueError(
                "chain_primitives: sub_uow requires instructions.child_uow_specs (list)"
            )
        if not config["child_uow_specs"]:
            raise ValueError("chain_primitives: sub_uow requires at least one child_uow_spec")

    return config


def _dispatch_branch(dispatcher_fn, instructions: str, uow_id: str) -> str:
    """Dispatch a single branch and return the executor_id string."""
    return dispatcher_fn(instructions, uow_id)


def _merge_outputs(outputs: list[str], merge_strategy: str = "concatenate") -> str:
    """
    Merge branch outputs into a single composite output.

    merge_strategy:
      "concatenate" — join outputs with double newlines (default)
    """
    if merge_strategy == "concatenate":
        return "\n\n---\n\n".join(outputs)
    # Unknown strategy falls back to concatenate.
    return "\n\n---\n\n".join(outputs)


# ---------------------------------------------------------------------------
# fan_out
# ---------------------------------------------------------------------------

def dispatch_fan_out(
    uow_id: str,
    output_ref: str,
    chain_config: dict,
    dispatcher_fn,
) -> ExecutorResult:
    """
    Dispatch N subagents in parallel, collect all results, merge into one output.

    Args:
        uow_id:        The parent UoW ID (used for branch task_id scoping).
        output_ref:    Output path for the composite result.
        chain_config:  Parsed chain config dict with 'branches' and optional 'merge_strategy'.
        dispatcher_fn: Synchronous dispatcher callable (instructions, uow_id) -> executor_id.

    Returns:
        ExecutorResult with outcome=COMPLETE if all branches succeed,
        PARTIAL if some fail, FAILED if all fail.
    """
    branches: list[dict] = chain_config["branches"]
    merge_strategy: str = chain_config.get("merge_strategy", "concatenate")

    log.info("fan_out: dispatching %d branches for UoW %s", len(branches), uow_id)

    # Dispatch all branches in parallel using ThreadPoolExecutor.
    branch_results: list[tuple[int, str | Exception]] = []

    with ThreadPoolExecutor(max_workers=len(branches)) as pool:
        future_to_idx = {
            pool.submit(
                _dispatch_branch,
                dispatcher_fn,
                b.get("instructions", ""),
                f"{uow_id}:branch:{i}",
            ): i
            for i, b in enumerate(branches)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                result = future.result()
                branch_results.append((idx, result))
            except Exception as exc:
                log.warning(
                    "fan_out: branch %d for UoW %s failed: %s", idx, uow_id, exc
                )
                branch_results.append((idx, exc))

    branch_results.sort(key=lambda t: t[0])
    successes = [(i, r) for i, r in branch_results if isinstance(r, str)]
    failures = [(i, r) for i, r in branch_results if isinstance(r, Exception)]

    if not successes:
        reason = f"all {len(branches)} fan_out branches failed"
        return ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.FAILED,
            success=False,
            reason=reason,
            steps_completed=0,
            steps_total=len(branches),
        )

    outputs = [r for _, r in successes]
    composite = _merge_outputs(outputs, merge_strategy)
    log.info(
        "fan_out: %d/%d branches succeeded for UoW %s",
        len(successes), len(branches), uow_id,
    )

    if failures:
        failed_indices = ", ".join(str(i) for i, _ in failures)
        reason = f"fan_out partial: branches {failed_indices} failed; {len(successes)}/{len(branches)} succeeded"
        return ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.PARTIAL,
            success=False,
            reason=reason,
            output_artifact=composite,
            steps_completed=len(successes),
            steps_total=len(branches),
        )

    return ExecutorResult(
        uow_id=uow_id,
        outcome=ExecutorOutcome.COMPLETE,
        success=True,
        output_artifact=composite,
        steps_completed=len(branches),
        steps_total=len(branches),
    )


# ---------------------------------------------------------------------------
# diverge_converge
# ---------------------------------------------------------------------------

def dispatch_diverge_converge(
    uow_id: str,
    output_ref: str,
    chain_config: dict,
    dispatcher_fn,
) -> ExecutorResult:
    """
    Phase 1 (diverge): dispatch N subagents in parallel; wait for all.
    Phase 2 (converge): dispatch one synthesis subagent with all Phase 1 outputs.

    Args:
        uow_id:        The parent UoW ID.
        output_ref:    Output path for the converge result.
        chain_config:  Parsed chain config dict with 'diverge_branches' and 'converge'.
        dispatcher_fn: Synchronous dispatcher callable (instructions, uow_id) -> executor_id.

    Returns:
        ExecutorResult with outcome from the converge subagent,
        FAILED if any diverge branch fails (Phase 2 is not run on partial diverge failure).
    """
    diverge_branches: list[dict] = chain_config["diverge_branches"]
    converge_cfg: dict = chain_config["converge"]

    log.info(
        "diverge_converge: Phase 1 — dispatching %d diverge branches for UoW %s",
        len(diverge_branches), uow_id,
    )

    # Phase 1: dispatch all diverge branches in parallel.
    phase1_results: list[tuple[int, str | Exception]] = []

    with ThreadPoolExecutor(max_workers=len(diverge_branches)) as pool:
        future_to_idx = {
            pool.submit(
                _dispatch_branch,
                dispatcher_fn,
                b.get("instructions", ""),
                f"{uow_id}:diverge:{i}",
            ): i
            for i, b in enumerate(diverge_branches)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                result = future.result()
                phase1_results.append((idx, result))
            except Exception as exc:
                log.warning(
                    "diverge_converge: diverge branch %d for UoW %s failed: %s",
                    idx, uow_id, exc,
                )
                phase1_results.append((idx, exc))

    phase1_results.sort(key=lambda t: t[0])
    failures = [(i, r) for i, r in phase1_results if isinstance(r, Exception)]

    if failures:
        failed_indices = ", ".join(str(i) for i, _ in failures)
        reason = (
            f"diverge_converge Phase 1 failed: diverge branches {failed_indices} failed — "
            f"Phase 2 (converge) not run"
        )
        log.warning("diverge_converge: halting before Phase 2 due to diverge failures: %s", reason)
        return ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.FAILED,
            success=False,
            reason=reason,
            steps_completed=len(diverge_branches) - len(failures),
            steps_total=len(diverge_branches) + 1,
        )

    # Phase 2: build converge input from all Phase 1 outputs.
    phase1_outputs = [r for _, r in phase1_results]
    combined = _merge_outputs(phase1_outputs, "concatenate")

    converge_template: str = converge_cfg.get(
        "instructions_template",
        "Synthesize the following outputs into a single coherent result:\n\n{outputs}",
    )
    converge_instructions = converge_template.format(outputs=combined)

    log.info("diverge_converge: Phase 2 — dispatching converge agent for UoW %s", uow_id)

    try:
        converge_id = dispatcher_fn(converge_instructions, f"{uow_id}:converge")
    except Exception as exc:
        reason = f"diverge_converge Phase 2 (converge) failed: {exc}"
        log.warning("diverge_converge: converge agent failed for UoW %s: %s", uow_id, exc)
        return ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.FAILED,
            success=False,
            reason=reason,
            steps_completed=len(diverge_branches),
            steps_total=len(diverge_branches) + 1,
        )

    return ExecutorResult(
        uow_id=uow_id,
        outcome=ExecutorOutcome.COMPLETE,
        success=True,
        executor_id=converge_id,
        output_artifact=converge_id,
        steps_completed=len(diverge_branches) + 1,
        steps_total=len(diverge_branches) + 1,
    )


# ---------------------------------------------------------------------------
# sub_uow
# ---------------------------------------------------------------------------

def dispatch_sub_uow(
    uow_id: str,
    output_ref: str,
    chain_config: dict,
    registry: "Registry",
) -> ExecutorResult:
    """
    Spawn child UoWs in the registry and transition parent to waiting-on-children.

    Each child spec must contain:
      summary: str
      success_criteria: str
      workflow_artifact_json: str  (JSON string of the child WorkflowArtifact)

    The parent UoW transitions to 'waiting-on-children'. The executor-heartbeat
    monitors children and calls registry.check_and_complete_if_children_done()
    on each pass to detect when all children reach terminal status.

    Args:
        uow_id:       The parent UoW ID.
        output_ref:   Output path (used when the parent eventually completes).
        chain_config: Parsed chain config dict with 'child_uow_specs'.
        registry:     Registry instance for inserting child UoWs and transitioning parent.

    Returns:
        ExecutorResult with outcome=COMPLETE immediately after spawning children
        (the parent is in waiting-on-children; "complete" from the executor's
        perspective means dispatch was successful, not that children finished).
    """
    child_specs: list[dict] = chain_config["child_uow_specs"]

    log.info(
        "sub_uow: spawning %d child UoWs for parent UoW %s", len(child_specs), uow_id
    )

    spawned_ids: list[str] = []
    failed_specs: list[tuple[int, Exception]] = []

    for i, spec in enumerate(child_specs):
        summary = spec.get("summary", f"child {i} of {uow_id}")
        success_criteria = spec.get("success_criteria", "")
        workflow_artifact_json = spec.get("workflow_artifact_json", "")

        if not workflow_artifact_json:
            exc = ValueError(f"child_uow_spec[{i}] missing workflow_artifact_json")
            log.warning("sub_uow: skipping child spec %d for %s: %s", i, uow_id, exc)
            failed_specs.append((i, exc))
            continue

        try:
            child_id = registry.insert_child_uow(
                parent_uow_id=uow_id,
                summary=summary,
                success_criteria=success_criteria,
                workflow_artifact_json=workflow_artifact_json,
                source="sub_uow",
            )
            spawned_ids.append(child_id)
            log.info(
                "sub_uow: spawned child UoW %s (spec %d) for parent %s",
                child_id, i, uow_id,
            )
        except Exception as exc:
            log.warning(
                "sub_uow: failed to insert child spec %d for parent %s: %s",
                i, uow_id, exc,
            )
            failed_specs.append((i, exc))

    if failed_specs and not spawned_ids:
        reason = f"sub_uow: all {len(child_specs)} child specs failed to insert"
        return ExecutorResult(
            uow_id=uow_id,
            outcome=ExecutorOutcome.FAILED,
            success=False,
            reason=reason,
            steps_completed=0,
            steps_total=len(child_specs),
        )

    if failed_specs:
        # Partial spawn — record failure, continue with spawned children.
        failed_indices = ", ".join(str(i) for i, _ in failed_specs)
        log.warning(
            "sub_uow: partial spawn for %s — specs %s failed; proceeding with %d/%d children",
            uow_id, failed_indices, len(spawned_ids), len(child_specs),
        )

    # Transition parent to waiting-on-children.
    registry.transition_to_waiting_on_children(uow_id)

    log.info(
        "sub_uow: parent %s transitioned to waiting-on-children with %d child(ren): %s",
        uow_id, len(spawned_ids), spawned_ids,
    )

    return ExecutorResult(
        uow_id=uow_id,
        outcome=ExecutorOutcome.COMPLETE,
        success=True,
        reason=f"sub_uow: spawned {len(spawned_ids)} child(ren), waiting on completion",
        steps_completed=len(spawned_ids),
        steps_total=len(child_specs),
    )
