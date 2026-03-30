# Executor Result Contract

*Status: Active — 2026-03-30. Canonical reference for all Executor implementations (PR4/#305 and beyond).*

---

## Overview

Every Executor, upon completing execution of a UoW — whether the execution succeeded, failed, was blocked, or completed partially — **must** write a structured result file before transitioning the UoW to `ready-for-steward`. This document specifies the schema of that file, the Steward's interpretation table, and the rationale behind the posture boundary.

The result file path is derived from `output_ref`:

- **Primary convention:** `{output_ref}.result.json` — replace the extension (e.g. `foo.json` → `foo.result.json`)
- **Fallback convention:** append `.result.json` as a suffix when `output_ref` has no extension (e.g. `/path/to/artifact` → `/path/to/artifact.result.json`)

The Steward checks both paths, preferring the primary convention.

---

## Schema

`{output_ref}.result.json` must contain:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `uow_id` | `str` | yes | The UoW ID this result belongs to. Must match the UoW record's `id` field — the Steward validates this before reading any other field. |
| `outcome` | `"complete"` \| `"partial"` \| `"failed"` \| `"blocked"` | yes | Execution outcome (see Failure Taxonomy below). This is the primary signal the Steward routes on. |
| `success` | `bool` | yes | Backward-compatibility field. Derived from `outcome == "complete"`. Must be `True` if and only if `outcome == "complete"`. Executors must write both fields consistently. |
| `reason` | `str` | no | Human-readable explanation for any non-`complete` outcome. Required for `partial`, `failed`, and `blocked`; omitted for `complete`. |
| `steps_completed` | `int` | no | Number of discrete steps completed before the Executor stopped. Useful for `partial` outcomes and multi-step workflows. |
| `steps_total` | `int` | no | Total number of discrete steps in the prescribed workflow, if known at execution time. Enables the Steward to assess progress fraction. |
| `output_artifact` | `str` | no | Absolute path to the primary output artifact, if different from `output_ref` itself. Example: a synthesized document written to a path distinct from the Executor's working output file. |
| `executor_id` | `str` | no | Identifier of the agent or process that executed the UoW. For LLM subagent dispatch: the task ID or session ID. Useful for audit correlation. |

### Python type reference

```python
from enum import StrEnum
from dataclasses import dataclass

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

@dataclass(frozen=True, slots=True)
class ExecutorResult:
    uow_id:           str
    outcome:          ExecutorOutcome
    success:          bool             # must equal outcome.is_success()
    reason:           str | None = None
    steps_completed:  int | None = None
    steps_total:      int | None = None
    output_artifact:  str | None = None
    executor_id:      str | None = None
```

### Minimal valid result (complete)

```json
{
  "uow_id": "abc-123",
  "outcome": "complete",
  "success": true
}
```

### Minimal valid result (failed)

```json
{
  "uow_id": "abc-123",
  "outcome": "failed",
  "success": false,
  "reason": "Unhandled exception in workflow step 3: FileNotFoundError: /tmp/foo.json"
}
```

---

## Steward Interpretation Table

The Steward reads the result file in `_assess_completion()` after verifying that `output_ref` is valid and the re-entry posture is `execution_complete` or `startup_sweep_possibly_complete`.

| `outcome` | `success_criteria` present | `steward_cycles` | Steward action |
|-----------|---------------------------|------------------|----------------|
| `complete` | yes | any | Evaluate `output_ref` and `output_artifact` against `success_criteria`. If satisfied: transition to `done`, set `completed_at`, write closure diagnosis. If not satisfied: prescribe next cycle (increment `steward_cycles`). |
| `complete` | no | any | Transition to `done` on the basis of `outcome == complete`. Write closure note flagging missing `success_criteria` as a gap. |
| `partial` | yes | < 5 | Re-diagnose with `reason` and progress fraction as inputs. Prescribe continuation or course-correction. Increment `steward_cycles`. |
| `partial` | yes | >= 5 | Hard cap reached. Surface to Dan with full context: `reason`, `steps_completed / steps_total`, `steward_log`. Do not close. |
| `partial` | no | any | Re-diagnose. Treat `reason` as the primary signal. Prescribe continuation. |
| `failed` | yes | < 5 | Re-diagnose with `reason` as primary input. Options: prescribe retry (same primitive), prescribe corrective prescription, or surface to Dan. |
| `failed` | yes | >= 5 | Hard cap reached. Surface to Dan unconditionally. |
| `failed` | no | any | Re-diagnose. Prescribe retry or surface to Dan based on `reason` severity. |
| `blocked` | yes | any | Transition UoW to `blocked` status. Write `reason` to audit log. Surface to Dan with blocked context. Await Dan's `/decide` to resume. |
| `blocked` | no | any | Same as above. `blocked` always surfaces to Dan — the Executor has determined that external resolution is required. |
| *(file absent)* | yes | < 5 | Steward cannot declare done. Treat as inconclusive. Increment `steward_cycles`, prescribe investigation or retry. |
| *(file absent)* | yes | >= 5 | Hard cap reached. Surface to Dan. This is a contract violation — the Executor did not write a result file. |
| *(file absent)* | no | any | Fall back to posture-based heuristic (Phase 1 / legacy UoWs only). Write `success_criteria_missing` audit entry. |

**Key invariants:**
- The Steward never reads `success` without first checking `outcome`. `success` is a backward-compat convenience field; `outcome` is the routing signal.
- `blocked` always routes to Dan — no prescription is possible without external resolution.
- Absence of the result file when `success_criteria` is present is a contract violation, not an ambiguous state.

---

## Posture Rationale

### Why two actors with distinct postures?

The Steward/Executor split exists because **evaluation and execution are epistemically different activities** — mixing them in one agent produces systematic distortion.

An Executor that evaluates its own success has an inherent reporting bias: it applies its own interpretation of the task intent, which may have drifted from the UoW's original `success_criteria` during execution. This drift is invisible to the audit trail. The Steward, by contrast, re-reads the `success_criteria` fresh on every re-entry — before reading the Executor's result. This re-read-first protocol is the mechanism that prevents the Steward from being anchored to the Executor's framing.

**Executor posture (signal, not interpretation):**
- Reports what completed, what exit state occurred, what artifact exists
- Does not evaluate whether the output satisfies the UoW's intent
- Does not declare the UoW done
- Does not assess its own work quality
- Writes faithfully: "here is what happened, here is why I stopped"

**Steward posture (interpretation, not execution):**
- Holds the UoW's full history, `success_criteria`, `steward_agenda`, and Dan's current register
- Evaluates whether the Executor's output satisfies the original intent
- Decides: close, loop, or surface
- Never executes — only prescribes and evaluates

This separation means the Steward can detect when an Executor completed a task correctly but the *wrong* task — a subtle error that would be invisible if the Executor declared success. It also means the audit trail has two distinct perspectives on every UoW cycle: the Executor's faithful report of what happened, and the Steward's independent judgment about what it means.

### Why is `success_criteria` evaluated by the Steward, not the Executor?

`success_criteria` is the Seed's anchor — written at germination time, before execution begins. It captures the intent at the moment the work was defined, not the intent as understood by the agent doing the work. Giving the Executor access to `success_criteria` and asking it to self-evaluate against it would collapse the posture boundary: the Executor would be acting as its own judge. The Steward's re-read-first protocol keeps the evaluation authority with the actor that holds the full context.

---

## Failure Taxonomy

### `complete`

All prescribed steps executed. The primary output artifact exists and is non-empty. The Executor has no remaining work to do on this UoW under the current prescription.

**Examples:**
- A file was generated and written to `output_ref`.
- A GitHub issue was closed via `gh` and the closure was confirmed.
- A multi-step doc was written and all sections are present.

**Executor must NOT write `complete` if:**
- The output artifact is empty or missing.
- An exception was caught and swallowed — `failed` is the correct outcome.
- Some steps were skipped due to a condition check — `partial` may be more accurate.

---

### `partial`

Some steps completed; the Executor stopped intentionally before completing the full prescription. This is a planned stop, not an error — the Executor recognized a condition that makes continuation incorrect without re-prescription.

**Examples:**
- A spec-breakdown workflow completed the investigation phase but determined that the decomposition step requires a design decision the Executor cannot make unilaterally. Executor stops, writes `partial`, includes `reason: "decomposition requires architectural decision on fan-out strategy"`.
- A multi-file edit workflow completed 3 of 5 files before hitting a merge conflict. Executor stops, writes `partial`, includes `steps_completed: 3, steps_total: 5`.
- An investigation surfaced new constraints that invalidate the current prescription. Executor writes `partial` with `reason` explaining the constraint.

**Key distinction from `blocked`:** `partial` means "I can resume with updated instructions." `blocked` means "I cannot resume without external action."

---

### `failed`

Execution error: unhandled exception, non-zero exit, tool failure, or any condition that prevented the Executor from completing its work in a way that could be retried under the same prescription.

**Examples:**
- An unhandled `FileNotFoundError` terminated the workflow.
- A `gh` command returned a non-zero exit code and the Executor could not recover.
- The `workflow_artifact` path pointed to a non-existent file (the Executor cannot proceed).
- An LLM subagent returned a malformed result that the Executor cannot parse.

**Executor must write the result file even on failure.** The failure reason is the primary input for the Steward's re-diagnosis. An Executor that crashes without writing a result file produces an `executor_orphan` — which is classified by the startup sweep, not by the Steward's normal re-entry path.

**Write failure reason to `output_ref` as well**, so the artifact pointer is populated regardless of outcome.

---

### `blocked`

The Executor cannot proceed due to an external dependency that is outside the Executor's authority to resolve. This is not an error — it is the Executor correctly recognizing the boundary of its operating authority.

**Examples:**
- The prescribed workflow requires a GitHub approval that has not been granted.
- A required input file has not been produced by a preceding UoW that has not yet completed.
- The Executor needs Dan's explicit decision before it can choose between two equally-valid paths.
- An API rate limit or external service outage prevents any progress and is expected to clear within hours, not seconds.

**`blocked` always routes to Dan.** The Steward transitions the UoW to `blocked` status and surfaces the `reason` with full context. Dan resolves via `/decide`. The distinction from `partial` is authorization, not capability: a `partial` Executor could resume with a new prescription; a `blocked` Executor requires external action before any prescription is useful.

---

## Absence is a Contract Violation

If the result file is absent and the UoW has `success_criteria`, the Steward cannot declare done. The Steward will increment `steward_cycles` and re-prescribe, eventually surfacing to Dan at the hard cap. This creates unnecessary human interrupts that could have been avoided by a one-line file write.

**Crash recovery is not an excuse.** If the Executor crashes mid-execution and cannot write the result file, the startup sweep classifies the UoW as `crashed_no_output_ref` or `crashed_output_ref_missing` and surfaces it to the Steward. The Steward then re-diagnoses from the last known artifact state. This is the recovery path for genuine crashes. It is not the intended path for planned stops.

Every intentional exit — complete, partial, failed, or blocked — must produce a result file.

---

## Startup Sweep Classification Labels

When the startup sweep runs, it assigns classification labels to UoWs it recovers. These labels appear in the `classification` field of the audit log entry written by the sweep.

| Label | Condition | Steward treatment |
|-------|-----------|-------------------|
| `executor_orphan` | UoW stuck in `ready-for-executor` state beyond the orphan threshold (default: 1 hour). Executor was dispatched but never wrote a result file. | Treated as clean first execution: Steward re-diagnoses and re-prescribes from the current UoW state. |
| `diagnosing_orphan` | UoW in `diagnosing` state at sweep time — Steward crashed mid-diagnosis before completing the diagnosis cycle. | Re-diagnosed from current UoW state; steward_log may carry partial entries from the aborted cycle. |
| `crashed_no_output_ref` | UoW was `active` (Executor running) and no `output_ref` is set — Executor crashed before writing any artifact. | Steward re-diagnoses from scratch; no artifact to evaluate. |
| `crashed_output_ref_missing` | UoW was `active`, `output_ref` is set, but the file is absent — Executor crashed after registering the artifact path but before writing it. | Steward re-diagnoses; treats missing file as incomplete execution. |

---

## Contract Version

This document specifies contract v1, corresponding to WOS Phase 2. Future versions will be noted here with their effective date and the PRs that introduce changes.

| Version | Effective | Changes |
|---------|-----------|---------|
| v1 | 2026-03-30 | Initial formal contract. Adds `outcome` enum over the Phase 1 `success: bool` minimal schema. Backward-compatible: `success` field retained. |
