# UoW Completion Surface Schema — Composable Metabolic Readout

*Status: Design — 2026-05-09*

---

## Purpose

When a UoW transitions to `done` (or `failed`), the system currently writes nothing to the operator's attention layer. The Done() branch in `steward.py` closes the audit trail and exits — no notification, no synthesis, no signal for the system's own optimization loop.

This document specifies the **completion surface schema**: the structured readout emitted at UoW completion that captures what happened, how hard the system worked, and what the topology reveals about problem decomposition health. The schema serves two consumers: Dan (operator visibility) and the WOS optimization feedback loop (self-calibration without a training loop).

---

## The Metabolic Readout Frame

The highest-signal completion frame is **outcome × topology**:

- `pearl` + single-cycle + no gate fired = clean execution
- `heat` + spiral gate = churn without value (the system is spinning)
- `shit` + dead-end + high execution_attempts = structural prompt or prescription failure
- Any primary outcome + seeds_surfaced > 0 = generative side-channel active

A readout that only says "done" is useless for either operator review or system learning. A readout that captures the outcome category AND the topological signals that got there is a full metabolic trace.

---

## Schema

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

PrimaryOutcome = Literal["pearl", "seed", "heat", "shit"]
GateFired = Literal["spiral", "dead_end", "burst", "none"]


@dataclass(frozen=True)
class SeedSurfaced:
    """
    An out-of-scope work item discovered during UoW execution.

    Seeds are side-product discoveries — a UoW with primary_outcome='pearl'
    can surface 0–N seeds. Seeds co-exist with any primary outcome; they are
    not a competing primary outcome category.
    """
    title: str
    description: str
    suggested_issue: str | None  # None when the subagent couldn't form one


@dataclass(frozen=True)
class UoWCompletionSurface:
    """
    Composable completion readout for a WOS Unit of Work.

    The same schema applies to successful ('done') and failed ('failed') UoWs.
    Failed UoWs populate all fields; some will be None or empty. The
    failure_summary field explains what broke — it is the failure-path analog
    of completion_rationale.

    All fields are read at Done()/Failed() transition time from the registry
    record and audit log. No LLM round-trip is required for construction.
    """

    # --- Identity ---
    uow_id: str
    uow_title: str                       # uow.summary
    register: str                        # uow.register (operational/iterative-convergent/philosophical/human-judgment)

    # --- Primary outcome ---
    # Single value: the metabolic classification of what this UoW produced.
    # Written at write_result time by the executing subagent via the
    # outcome_category field in write_result. Already stored in uow_registry.outcome_category.
    # Source: uow_registry.outcome_category (migration 0018).
    primary_outcome: PrimaryOutcome | None  # None when subagent did not report one

    # --- Seeds surfaced (side-product, not primary) ---
    # Out-of-scope work items discovered during execution. A UoW whose
    # primary_outcome is 'pearl' may have surfaced 2 seeds. Seeds never replace
    # the primary outcome — they extend the readout alongside it.
    # Source: NEW FIELD — structured from subagent write_result payload
    # (currently only captured implicitly via artifacts list with category='seed').
    seeds_surfaced: list[SeedSurfaced] = field(default_factory=list)

    # --- Topology signal ---
    # Which dispatch eligibility gate fired during steward cycles for this UoW.
    # Derived from _check_dispatch_eligibility() return value.
    # 'none' means all cycles returned 'dispatch' (clean execution).
    # Source: NEW FIELD — gate verdict not currently written to registry.
    gate_fired: GateFired = "none"

    # --- Cycle depth ---
    # How many steward diagnosis+prescription cycles this attempt consumed.
    # Per-attempt counter; reset on decide-retry. High steward_cycles with
    # low execution_attempts → prescription churn. High both → retry treadmill.
    # Source: uow_registry.steward_cycles (already present).
    steward_cycles: int = 0

    # --- Lifetime depth ---
    # Cumulative steward cycles across all decide-retry resets.
    # Used to distinguish "one hard attempt" from "multiple short attempts."
    # Source: uow_registry.lifetime_cycles (already present).
    lifetime_cycles: int = 0

    # --- CC draw ---
    # Total tokens (input + output) consumed across all execution attempts.
    # The subagent's write_result reports its own session total; this is
    # stored in uow_registry.token_usage (migration 0015).
    # Per-heartbeat snapshots are in uow_heartbeat_log; the registry field
    # holds the final confirmed total.
    # Source: uow_registry.token_usage (already present).
    token_usage: int | None = None

    # --- Execution attempts ---
    # Confirmed dispatches that consumed budget (orphan kills excluded).
    # Source: uow_registry.execution_attempts (already present).
    execution_attempts: int = 0

    # --- Proposal→dispatch delta (leftover juice) ---
    # Seed items proposed during execution that were not yet dispatched
    # as of completion time. Derived from artifacts with category='seed'
    # that have no corresponding UoW in the registry.
    # Source: computed at notification time from uow_registry.artifacts + registry query.
    # Not stored — computed on demand to avoid staleness.
    proposals_not_dispatched: int = 0

    # --- Completion rationale (success path) ---
    # The Steward's closure prose from steward_log (steward_closure event).
    # Source: uow_registry.steward_log, event='steward_closure', field='assessment'.
    completion_rationale: str | None = None

    # --- Failure summary (failure path) ---
    # Prose explaining why the UoW failed: close_reason when present, otherwise
    # synthesized from audit_log (retry_cap_exceeded, hard_cap_cleanup events).
    # None when primary outcome is success.
    # Source: uow_registry.close_reason + audit_log.
    failure_summary: str | None = None

    # --- Completion timestamp ---
    # When the UoW reached done or failed.
    # Source: uow_registry.completed_at (written by _write_steward_fields at Done()).
    completed_at: str | None = None
```

---

## Field-by-Field Source Reference

| Field | Source | Status |
|---|---|---|
| `uow_id` | `uow_registry.id` | Present |
| `uow_title` | `uow_registry.summary` | Present |
| `register` | `uow_registry.register` | Present |
| `primary_outcome` | `uow_registry.outcome_category` | Present (migration 0018) |
| `seeds_surfaced` | New structured field in `write_result` payload | **Missing** |
| `gate_fired` | `_check_dispatch_eligibility()` return value | **Missing** — not written to registry |
| `steward_cycles` | `uow_registry.steward_cycles` | Present |
| `lifetime_cycles` | `uow_registry.lifetime_cycles` | Present |
| `token_usage` | `uow_registry.token_usage` | Present (migration 0015) |
| `execution_attempts` | `uow_registry.execution_attempts` | Present (migration 0014) |
| `proposals_not_dispatched` | Computed from `artifacts` (category='seed') vs registry | Computed at notification time |
| `completion_rationale` | `steward_log` event `steward_closure`, field `assessment` | Present (unextracted) |
| `failure_summary` | `close_reason` + audit_log events | Present (unextracted) |
| `completed_at` | `uow_registry.completed_at` | Present |

---

## Plumbing Additions Required

### Addition 1: `gate_fired` registry field

**What is missing:** `_check_dispatch_eligibility()` computes a verdict (`spiral`/`dead_end`/`burst`/`dispatch`) but the result is consumed inline and discarded. There is no per-UoW record of which gate fired across cycles.

**What is needed:**
- Add `gate_fired TEXT NULL` column to `uow_registry` (new migration, e.g. 0019).
- In `_process_uow()`, when `_check_dispatch_eligibility()` returns a non-`dispatch` verdict, write it to the registry via an UPDATE (or pass through `_write_steward_fields`).
- Logic: track the highest-severity gate that fired during this UoW's lifecycle. Precedence: `spiral` > `dead_end` > `burst` > `none`. Once written, only upgrade — never downgrade.

**Size estimate:** Small. One migration (3 lines of SQL), one registry write in `_process_uow()` (~10 lines), one UoW dataclass field. No schema design work needed — the values are already defined as string literals.

---

### Addition 2: `seeds_surfaced` structured subagent reporting

**What is missing:** Seeds discovered during execution are currently captured only implicitly via the `artifacts` list (items with `category='seed'`). The `_extract_outcome_refs()` function in `wos_completion.py` auto-extracts issue refs as seeds, but there is no structured way for a subagent to explicitly report a new seed with a title, description, and suggested issue body.

**What is needed:**
- Extend the `write_result` call schema to accept an optional `seeds` list:
  ```json
  {
    "seeds": [
      {"title": "...", "description": "...", "suggested_issue": "..."}
    ]
  }
  ```
- In `maybe_complete_wos_uow()`, parse and store the seeds list alongside the existing `artifacts` update. The seeds can be stored in a new `seeds_surfaced TEXT NULL` JSON column, or appended to `artifacts` with `type='seed_explicit'` to distinguish structured reports from auto-extracted issue refs.
- The `UoWCompletionSurface` then reads from this structured field rather than inferring seeds from the artifacts list.

**Size estimate:** Medium. Requires: write_result protocol extension (must be backward compatible — `seeds` is optional), `maybe_complete_wos_uow()` update to parse and store, one new registry column (migration), and a read path in the surface-schema builder. No executor or steward changes. The subagent prompts must be updated to surface seeds explicitly, but this can be done incrementally — the system works without it.

**Interim approach (before Addition 2 lands):** The `seeds_surfaced` field in `UoWCompletionSurface` is populated from `artifacts` where `category='seed'` and `type='issue'`. This is an approximation — every auto-extracted issue ref is treated as a seed, which over-counts. The structured field improves precision.

---

## Composability: Failed UoWs Use the Same Schema

The `UoWCompletionSurface` schema is composable across outcomes. A failed UoW populates the same fields, with the failure-path fields filled:

```
primary_outcome: None (if the subagent never called write_result) or the last reported category
seeds_surfaced: [] (no structured seeds on failure path today; may be populated if subagent reported some before failing)
gate_fired: "dead_end" (typical for failure after retry_cap_exceeded)
steward_cycles: N (the total cycles consumed before the cap)
token_usage: N or None (present if the subagent called write_result before the failure path triggered)
execution_attempts: N (the confirmed dispatch count that hit MAX_RETRIES)
completion_rationale: None
failure_summary: "retry cap exceeded after N attempts — {close_reason}" or "user closed" or "hard cap"
completed_at: timestamp of failed transition
```

The notification format simply treats `failure_summary` as the body when `primary_outcome` is absent and `failed_summary` is present. No separate schema variant is needed.

---

## Notification Format

### Per-completion ping (written to inbox on Done() or Failed())

The notification is a new inbox message type: `wos_done`.

**Message fields:**
```json
{
  "type": "wos_done",
  "uow_id": "<id>",
  "uow_title": "<summary>",
  "register": "operational",
  "primary_outcome": "pearl",
  "seeds_surfaced_count": 2,
  "gate_fired": "none",
  "steward_cycles": 1,
  "lifetime_cycles": 1,
  "token_usage": 14230,
  "execution_attempts": 1,
  "completion_rationale": "PR #1234 merged and oracle approved",
  "failure_summary": null,
  "completed_at": "2026-05-09T14:32:00Z"
}
```

**Telegram surface (short form):**
```
UoW done: Add oauth2 support [pearl]
1 cycle · 14,230 tokens · 2 seeds surfaced
```

**Telegram surface (rich form, on request or for non-pearl outcomes):**
```
UoW done: Add oauth2 support
Outcome : pearl
Topology: clean (1 cycle, no gate fired)
Tokens  : 14,230
Seeds   : 2 new work items surfaced
Rationale: PR #1234 merged and oracle approved
```

**Failed UoW surface:**
```
UoW failed: Refactor auth module
Outcome : none reported
Topology: dead-end gate (3 cycles, 3 execution attempts)
Tokens  : 8,100
Failure : retry cap exceeded after 3 attempts — executor returned partial on all attempts
```

### Daily digest contribution

The `wos_metrics_report` job (or equivalent daily digest) should include a metabolic summary:

```
WOS Daily — 2026-05-09
Completed : 4 UoWs
  pearl 2  seed 1  heat 1
  Seeds surfaced: 3 new items
  Avg tokens: 11,400
  Avg cycles: 1.8
Failed    : 1 UoW (dead-end gate, retry cap)
Churn     : 0 spiral gates fired today
```

---

## Implementation Order

**1. Notification layer (wos_done message type and dispatcher handler)**
This produces the immediate value — Dan sees completions in real time. No schema additions required; reads existing registry fields. The `gate_fired` and `seeds_surfaced` fields are absent but the notification degrades gracefully (omits those lines). Insertion point: end of the Done() branch in `_process_uow()` at line ~4440 in `steward.py`, after `return Done()` is assembled but before it is returned (write the inbox message, then return Done). Non-fatal — inbox write failure must not block the Done transition.

**2. `gate_fired` registry field**
One migration, one write in `_process_uow()`, one UoW dataclass field. The notification layer immediately gains topology signal once this lands. Gate semantics are already computed; this is purely a persistence gap.

**3. CC aggregation per UoW**
The registry already has `token_usage` (final write_result total) and `uow_heartbeat_log` (per-heartbeat snapshots). For the notification, `token_usage` is sufficient — it is the subagent's reported total. If cumulative accounting across failed attempts is needed (e.g., total tokens burned on a UoW that hit retry cap), a query against `uow_heartbeat_log` aggregated by `uow_id` provides it. This is a query, not a new column. Defer until the notification layer is live and the need is confirmed.

**4. `seeds_surfaced` structured reporting**
Extend `write_result` protocol + add registry column. This is the most invasive addition (touches the wire protocol) and has the lowest immediate urgency — the approximation via `artifacts` works. Land after the notification layer is stable.

---

## What This Design Establishes

The completion surface schema is the sensory layer that makes WOS self-observing. Without it, UoWs close into silence: the audit trail is complete, but no one is listening. With it, each completion emits a structured signal that is simultaneously legible to Dan (short-form Telegram ping) and queryable by the optimization loop (daily digest, retrospective, gate calibration). The schema is composable because the same fields describe both success and failure — the failure path is not a special case, it is the same schema populated differently.

The key design decision — seeds as side-products rather than a competing primary outcome category — preserves the information that the current single-field schema loses. A `pearl` UoW that surfaced 2 seeds is meaningfully different from a `pearl` UoW that surfaced none. The single `outcome_category` field cannot express this. The composable schema can.
