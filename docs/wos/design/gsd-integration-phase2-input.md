# GSD Integration — Phase 2 Design Input

**UoW:** uow_20260522_99a75f  
**Date:** 2026-05-22  
**Status:** design-settled — awaiting Phase 2 implementation

---

## 1. Wave Execution Model

### Schema diff

Add one column to `uow_registry` as migration 0027 (next available after 0026):

```sql
ALTER TABLE uow_registry ADD COLUMN depends_on TEXT DEFAULT '[]';
```

Add the corresponding field to the `UoW` dataclass in `src/orchestration/registry.py`,
immediately after the `children` field (line ~203):

```python
# depends_on: JSON array of UoW IDs that must reach `done` status before
# this UoW transitions from `pending` to `ready-for-executor`. Populated by
# the decomposition agent or gsd-ingest step at child UoW creation time.
# NULL / '[]' = no dependencies (dispatch immediately when status allows).
depends_on: list[str] = dataclasses.field(default_factory=list)
```

The field is a JSON array of UoW IDs (strings matching the `uow_YYYYMMDD_xxxxxx`
pattern). `NULL` and `'[]'` are both treated as "no dependencies" by the
wave-grouping logic.

### Where it lives in the schema

`depends_on` is a peer of `children` and `parent` in the `uow_registry` table.
The three fields together express the full dependency graph:

| Field | Meaning | Who writes it |
|---|---|---|
| `parent` | This UoW was spawned by `parent` | Decomposition agent or gsd-ingest |
| `children` | UoWs spawned from this one | Same writer that sets `parent` on children |
| `depends_on` | UoWs that must be `done` before this can execute | Decomposition agent or gsd-ingest |

`depends_on` entries do not need to be siblings (children of the same parent). The
wave-grouping algorithm operates on a list of candidate UoWs and groups them by
unresolved dependency count — it does not restrict cross-parent references.

### Wave completion definition

A wave is complete when **all UoWs in the wave have reached a terminal status**.
Terminal statuses are `done`, `failed`, and `expired` (per `UoWStatus` in
`registry.py`).

Operational rule for the fan-out dispatcher:

- **Proceed to next wave** only when all UoWs in the current wave are in `done` status.
- **Block and raise** if any UoW in the wave reaches `failed` or `expired` — transition
  the parent UoW to `blocked` with `steward_notes` set to:
  `"wave-N-blocked: uow_XXXX reached status=<failed|expired>"`
- A `blocked` parent UoW surfaces to the Steward for diagnosis and escalation.

### Wave-grouping algorithm (topological sort)

```
function group_into_waves(children: list[UoW]) -> list[list[UoW]]:
    resolved = set()
    waves = []
    remaining = list(children)
    while remaining:
        wave = [u for u in remaining if all(d in resolved for d in u.depends_on)]
        if not wave:
            raise CircularDependencyError(remaining)  # ← cycle detected here
        waves.append(wave)
        resolved.update(u.id for u in wave)
        remaining = [u for u in remaining if u not in wave]
    return waves
```

### Circular dependency detection

Circular dependency detection is a **dispatch-time concern**, not a schema
validation concern.

- The topological sort raises `CircularDependencyError` when it cannot advance
  (no node has all dependencies resolved but remaining nodes still exist).
- Schema cannot enforce this — SQLite has no mechanism to check graph acyclicity
  on array fields at INSERT time.
- The error surfaces during the fan-out dispatch handler's wave-grouping step.
  The parent UoW transitions to `blocked` with `steward_notes` explaining the cycle.
- The Steward escalates to Dan rather than attempting auto-repair.

### Posture extension

`depends_on` **extends `fan-out` posture** — it does not introduce a new posture name.

A `fan-out` UoW with children that have `depends_on` entries is wave-structured
fan-out. A `fan-out` UoW whose children all have empty `depends_on` is single-wave
fan-out (parallel dispatch, same as today's unimplemented intent). The dispatcher
handles both cases with the same wave-grouping algorithm — single-wave fan-out
simply returns one wave containing all children.

`UoWPosture.FAN_OUT` in `registry.py` remains the only fan-out posture value.
No addition to the `UoWPosture` enum is required for wave execution.

---

## 2. Context Saturation Monitoring

### Chosen option: Hybrid of Option C (primary) + Option A (convention layer)

Active PostToolUse threshold injection (the gsd approach) is **not recommended**
for Lobster WOS subagents. This is not a new judgment — issue #2056 (closed
2026-05-08) removed threshold-based wind-down from the dispatcher because it
races with CC's own compaction at the same ~70% usage boundary. The same race
applies to WOS subagents.

Current state of `hooks/context-monitor.py`: the hook exists, fires as a
PostToolUse hook when registered, reads transcript token usage, and logs to
`~/lobster-workspace/logs/context-monitor.log`. It does **not** inject warnings
or trigger interventions (wind-down mode removed in issue #2056). This logging
behavior is correct and should be preserved.

### Option C (primary gate): estimated_cycles cap

`estimated_cycles` is already in the UoW schema and is set at dispatch time by
the Steward. The Steward re-evaluates any UoW that exceeds its estimated cycle
count. This is the structural gate for long-running WOS executions — it bounds
the maximum context commitment per execution and forces a Steward re-entry when
the agent hasn't completed within the estimated budget.

Implementers must set `estimated_cycles > 1` for any UoW expected to span
multiple tool-use turns. The Steward's cycle cap check is the primary mechanism
for detecting context-saturated agents that produce low-quality output rather
than failing cleanly.

### Option A (convention layer): subagent bootup checkpoint instruction

Add the following block to `src/orchestration/steward.py`'s executor dispatch
template (the prompt prefix sent to WOS subagents), specifically in the
heartbeat contract section of `sys.subagent.bootup.md` or its equivalent
for WOS subagents:

```
For multi-cycle WOS tasks (estimated_cycles > 1):
- At each natural phase boundary (after each design deliverable, after each PR,
  after completing a named phase of work), write a checkpoint via write_observation:
    write_observation(text="checkpoint: <what was done>, <what remains>",
                      category="system_context", task_id=<current_task_id>)
- A checkpoint is not a completion — continue after writing it.
- If context fills before the next natural boundary, write a checkpoint
  immediately and call write_result with outcome="partial" explaining
  what remains. The Steward will re-dispatch.
```

This is convention-carried, not structurally enforced. It is appropriate here
because: (a) the structural gate (estimated_cycles) already exists; (b) the
PostToolUse injection approach carries an active race risk (issue #2056);
(c) the instruction only fires when the agent chooses to honor it, which is
sufficient for the partial-completion recovery path to work.

### Why not Option B (active PostToolUse injection)

The PostToolUse hook approach (restoring threshold-triggering to context-monitor.py)
was evaluated and rejected for WOS subagents for the same reason it was removed
from the dispatcher: CC compacts on its own terms at ~70% context used. Adding an
intervention at 35% remaining (65% used) puts the hook slightly ahead of CC's
compaction threshold, but the fundamental problem remains — there are now two
independent systems (the hook and CC) attempting to handle the same context-saturation
event with no coordination protocol between them.

The on-compact.py hook handles compaction recovery for the dispatcher. WOS subagents
do not have an equivalent recovery path after compaction — the correct response for
a WOS subagent that encounters context limits is to write a checkpoint and call
write_result with outcome="partial", not to compact and continue.

---

## 3. gsd-project Posture

### Qualification Criteria

A UoW qualifies for `posture: gsd-project` only when **ALL** of the following
checklist items are true. Evaluate in order — criterion 1 and 2 eliminate the
most false positives fastest.

```
[ ] 1. GREENFIELD SCOPE — the UoW does not modify an existing named file, function,
       or module. It creates a new code artifact, subsystem, feature area, or tool
       that does not yet exist. (Signal: no existing output_ref to update; issue body
       describes something absent, not something broken or improved.)

[ ] 2. MULTI-PHASE SHAPE — the UoW description implies at least two distinct phases
       of work, evidenced by ≥2 of: "requirements," "design," "plan," "build,"
       "implement," "ship," "research," "spec," "Phase 1," "Phase 2," "first then,"
       or equivalent phased language.

[ ] 3. NON-TRIVIAL SCOPE — expected execution exceeds a single subagent session.
       Heuristic: references >3 distinct files or subsystems, OR contains language
       like "multi-day," "complex," "substantial," "system," or "framework."

[ ] 4. NO DUPLICATE IN-FLIGHT — no UoW in proposed/pending/active/blocked status
       already covers the same scope. (Dedup check before creating the hiring UoW.)

[ ] 5. IMMEDIATELY STARTABLE — no human-gate dependency at start. The work can begin
       without waiting for a decision from Dan. (Human-gate posture handles those cases.)
```

**Disqualification rules:**
- Criterion 1 failing is **independently sufficient to disqualify** — fall back to
  `solo` (or `fan-out` if the scope is decomposable). A modification to an existing
  artifact is never a gsd-project regardless of scope.
- Criteria 2 and 3 together are **jointly necessary** — a large but single-phase
  task routes to `fan-out`, not `gsd-project`.
- Criteria 4 and 5 are **individually necessary** — either failing disqualifies.

### Artifact Mapping

**Granularity: one UoW per Phase** (not per Plan, not per top-level task).

gsd's Plan is the atomic execution unit (parallel to a Lobster UoW). But Lobster's
`fan-out` posture with `depends_on` can represent Plan-level granularity directly —
each Plan within a Phase becomes a child UoW of the Phase UoW, with `depends_on`
encoding wave structure. The Registry tracks the Phase as the meaningful unit of
work; Plans are implementation details of fan-out execution.

**UoW hierarchy:**

```
Hiring UoW        (posture: gsd-project, status: active while phases run)
  children: [phase-1-uow, phase-2-uow, phase-3-uow]

Phase-N UoW       (posture: fan-out, parent: hiring-uow)
  children: [plan-Na-uow, plan-Nb-uow, ...]
  depends_on: [phase-(N-1)-uow]  # empty for Phase 1

Plan-N-X UoW      (posture: solo, parent: phase-N-uow)
  depends_on: [plan-N-Y, ...]    # per gsd dependency declarations
```

**Who creates the child UoW tree: a dedicated `gsd-ingest` step**, not the
dispatcher directly. Flow:

1. Dispatcher dispatches the hiring UoW to a gsd-project executor agent.
2. The gsd executor initializes a gsd project at `$LOBSTER_PROJECTS/<project-slug>/`,
   runs the `new-project` + `roadmap-approval` workflow, and calls `write_result`
   with `outcome="partial"` and `output_ref` pointing to `.planning/`.
3. The Steward sees the partial completion and dispatches the `gsd-ingest` handler.
4. `gsd-ingest` reads `.planning/ROADMAP.md`, creates Phase UoWs with `depends_on`
   wiring, creates Plan UoWs as children of their Phase UoW, and transitions the
   hiring UoW to `active`.
5. Normal fan-out wave execution proceeds from there.

This two-step pattern keeps the dispatcher handler thin — it does not need to parse
gsd's `.planning/` format directly. The gsd-ingest step owns the mapping from
`.planning/` to Registry UoWs.

**What happens to `.planning/` artifacts after ingestion:**

The `.planning/` directory **persists** at `$LOBSTER_PROJECTS/<project-slug>/.planning/`.
It is not archived separately and not deleted. The hiring UoW's `output_ref` field
points to the `.planning/` root:

```
output_ref: /home/lobster/lobster-workspace/projects/<project-slug>/.planning/
```

Phase UoW `output_ref` values point to their phase subdirectory:

```
output_ref: .../projects/<project-slug>/.planning/phases/01-<phase-name>/
```

Plan UoW `output_ref` values point to their SUMMARY.md:

```
output_ref: .../projects/<project-slug>/.planning/phases/01-<phase-name>/01-01-SUMMARY.md
```

Artifacts persist because they are the primary audit trail for what the gsd
executor did. Future Steward diagnoses reference them directly.

### Routing Classifier Entry

Add the following rule to `~/lobster-user-config/orchestration/classifier.yaml`
at priority 7 (between `high-risk-review` at 9 and `parallelizable-multifile` at 8):

```yaml
  - name: gsd-project
    priority: 7
    conditions:
      - field: scope
        op: eq
        value: greenfield
      - field: phase_count
        op: gte
        value: 2
    posture: gsd-project
    route_reason_template: "Rule 'gsd-project' matched: scope=greenfield AND phase_count>=2 — multi-phase project work routed to gsd executor"
```

**Dispatch target:** a `gsd-project` posture UoW is dispatched to the
`gsd-project-executor` handler in `src/orchestration/executor.py` (to be
created in Phase 2). This handler initializes a gsd project workspace,
runs `new-project` → `roadmap-approval`, then calls `write_result` with
`outcome="partial"` to trigger the `gsd-ingest` step.

**Output contract:** The gsd-project executor produces a `.planning/` directory
at `$LOBSTER_PROJECTS/<project-slug>/.planning/` containing `PROJECT.md`,
`REQUIREMENTS.md`, and `ROADMAP.md`. The gsd-ingest step consumes these to
create the Phase and Plan UoW tree.

**UoWPosture addition required:** Add `GSD_PROJECT = "gsd-project"` to the
`UoWPosture` enum in `src/orchestration/registry.py`. This is the only registry
change required to register the new posture — no schema migration needed
(the `posture` column stores string values; adding a new enum value is
backwards-compatible with existing rows).

---

## Open Questions Closed

| Question | Answer |
|---|---|
| Qualification criteria for `gsd-project`? | 5-item checklist: greenfield scope, multi-phase shape, non-trivial scope, no duplicate in-flight, immediately startable. Criterion 1 (greenfield) alone is sufficient to disqualify. All 5 must be true to qualify. |
| `.planning/` → UoW Registry mapping? | 1 UoW per Phase (not per Plan). Plans become child UoWs of Phase UoW via `depends_on`. Hiring UoW → Phase UoWs created by a dedicated `gsd-ingest` step (not the dispatcher). `.planning/` persists at `$LOBSTER_PROJECTS/<slug>/.planning/`, linked via `output_ref`. |
| Context saturation monitoring location? | Hybrid: Option C (estimated_cycles cap, already implemented — primary gate) + Option A (lightweight checkpoint instruction in WOS subagent bootup template). Active PostToolUse injection not used — issue #2056 removed this for the same race-with-CC-compaction reason that applies equally to WOS subagents. |

---

## Schema Summary

Changes required before Phase 2 wave execution can be implemented:

| Change | Location | Migration / PR |
|---|---|---|
| Add `depends_on TEXT DEFAULT '[]'` to `uow_registry` | `schema.sql` + `scripts/upgrade.sh` | Migration 0027 |
| Add `depends_on: list[str]` field to `UoW` dataclass | `src/orchestration/registry.py` | Same PR as migration 0027 |
| Add `GSD_PROJECT = "gsd-project"` to `UoWPosture` | `src/orchestration/registry.py` | Phase 2 gsd-project PR |
| Add `gsd-project` rule to `classifier.yaml` | `~/lobster-user-config/orchestration/classifier.yaml` | Phase 2 gsd-project PR |
| Add checkpoint instruction to WOS subagent bootup | `.claude/sys.subagent.bootup.md` | Standalone PR, no migration |

---

## Links

- WOS umbrella: #171 (not found in SiderealPress/lobster at time of writing — issue number may be incorrect or not yet filed)
- WOS Phase 2 routing classifier: #168 (same — not found; see note in prior design doc `assessments/gsd-phase2-design.md`)
- Prior gsd analysis: `~/lobster-workspace/assessments/gsd-integration-analysis.md`
- Prior Phase 2 design decisions (2026-04-22): `~/lobster-workspace/assessments/gsd-phase2-design.md`
- Issue #2056 (wind-down removal rationale): closed in SiderealPress/lobster

---

*WOS-UoW: uow_20260522_99a75f*
