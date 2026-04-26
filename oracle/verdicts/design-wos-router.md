VERDICT: APPROVED
Design: wos-execute-router daemon
Round: 3

---

## Round 3 — 2026-04-25

### Prior gap tracking (Round 2 gaps, current status)

- **Gap 1 (BLOCKING — `_dispatch_via_claude_p` call signature):** ADDRESSED. All three locations (routing path diagram, Key Interfaces table, Gap 1 resolution block) now show the corrected two-argument keyword form: `_dispatch_via_claude_p(instructions=decision["prompt"], uow_id=decision["task_id"].removeprefix("wos-"))`. The `uow_id` derivation (strip `"wos-"` prefix from `task_id`) is explicitly stated in each location with an inline comment. Three-location consistency verified.
- **Gap 2 (check_inbox type filter):** Closed — Round 2. Still closed.
- **Gap 3 (write_result vs. inbox write):** Closed — Round 2. Still closed.
- **Gap 4 (Encoded Orientation logged prior):** Deferred per Round 1 contract, correctly formed. Still closed.

---

### Stage 1: Vision Alignment (carried from Round 1 — does not change)

**Prior entering review:** This design is solving the wrong problem, or solving the right problem in a direction that forecloses better paths.

The theory of change is consistent with `principle-3` (determinism over judgment for conditionals) and `principle-4` (wire what exists before building more). Pulling zero-reasoning routing out of the dispatcher's LLM context is the right architectural direction. The daemon pattern cleanly separates mechanical routing from LLM reasoning, which is what the active_project phase_intent calls for.

**Alignment verdict:** Questioned — not Misaligned. The "Questioned" status from Round 1 reflected the foundational architecture gap (Gap 1), not a problem with the direction. With Gap 1 resolved, the design's mechanism matches its claimed outcome. The question has been answered.

---

### Stage 2: Quality Review — Round 3

Gap 1 is addressed. The three required locations are consistent and correct:

**Routing path diagram:** Shows `_dispatch_via_claude_p(instructions=decision["prompt"], uow_id=decision["task_id"].removeprefix("wos-"))` with inline comment stating the prefix origin.

**Key Interfaces table:** Signature shown as `_dispatch_via_claude_p(instructions, uow_id)`. Notes column explicitly states `uow_id` is `task_id.removeprefix("wos-")`.

**Gap 1 resolution block:** Identical call form to the routing diagram. Consistent.

No new gaps surface. The threading deference note (sequential execution acceptable at current throughput; threading deferred to implementation if throughput grows) is correctly scoped and does not block the design. The awareness path (inbox_write.py), throttle gate (active session count check), WOS config gate (wos-config.json), Phase 2 ADR pre-condition block, and migration path are unchanged from Round 2 and remain sound.

**Encoded Orientation check (constraint-3):** Phase 1 is additive — the dispatcher skip-gate does not remove existing behavior, it gates it. No Encoded Orientation decision required for Phase 1. Phase 2 pre-condition block correctly specifies the ADR requirements before Phase 2 PR can be opened. Constraint-3 compliance structure is correct.

---

### Patterns Applied

**From learnings.md `Design: wos-execute-router R2`** ("A gap resolution that names the correct mechanism but uses the wrong call signature is still a blocking gap. The design-doc call pseudocode is load-bearing specification — if it is wrong, the implementation will be wrong"): This entry was written from Round 2 of this specific review. Entering Round 3, it constrained the verification to all three locations rather than accepting a partial fix. Without this pattern as an active prior, I might have verified only the routing diagram and treated the fix as complete. The three-location check was caused by this pattern; the all-three-consistent result confirms the fix is structurally complete.

**From golden-patterns.md "seam-first abstraction" (2026-03-30):** The execution seam (`_dispatch_via_claude_p`) is now correctly specified at its boundary — two-parameter keyword form, `uow_id` derivation explicit, `instructions` mapped to the prompt. The seam is now defined correctly at the connection point between `route_wos_message`'s output and the execution call.

---

## Round 2 — 2026-04-25

### Prior gap tracking (Round 1 gaps, current status)

- **Gap 1 (BLOCKING — subagent spawning mechanism):** OPEN. The design names `_dispatch_via_claude_p` as the spawn mechanism, which is the correct direction. But the call signature in the design is wrong. See Gap 1 finding below.
- **Gap 2 (check_inbox type filter):** ADDRESSED. The design correctly states client-side filtering, removes the false `type=` parameter claim, and includes the filter code.
- **Gap 3 (write_result vs. inbox write):** ADDRESSED. The design correctly substitutes `inbox_write.py` / `write_inbox_message` throughout — both in the Awareness Path prose and in the Key Interfaces table. The stated rationale ("write_result requires a Claude session context that the daemon does not have") is accurate.
- **Gap 4 (Encoded Orientation logged prior):** DEFERRED (acceptable per Round 1 contract). The Phase 2 section contains an explicit pre-condition block with required ADR content (behavioral change named, vision.yaml anchors cited by field path, traceability to this doc). The deferred resolution is correctly formed.

---

### Stage 1: Vision Alignment (carried from Round 1 — does not change)

**Prior entering review:** This implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths.

The theory of change is consistent with `principle-3` (determinism over judgment for conditionals) and `principle-4` (wire what exists before building more). The direction — pulling zero-reasoning routing out of the dispatcher's LLM context — is the right architectural move. The alignment verdict from Round 1 stands: **Questioned, not Misaligned.** The problem is real. The mechanism gap in Gap 1 remains partly open after revision.

---

### Stage 2: Quality Review — Round 2

#### Gap 1 (new finding): `_dispatch_via_claude_p` signature mismatch

The Round 2 design names `_dispatch_via_claude_p` as the execution mechanism for Gap 1 resolution. This is correct at the architectural level — this function is the right call path. However, the design's invocation is wrong.

**Design shows:**
```python
_dispatch_via_claude_p(
    decision["agent_type"],
    decision["prompt"],
    decision["task_id"]
)
```

**Actual function signature (src/orchestration/executor.py:996):**
```python
def _dispatch_via_claude_p(instructions: str, uow_id: str) -> str:
```

Two parameters, not three. The design's call passes `decision["agent_type"]` as `instructions` (wrong — the prompt should go there), `decision["prompt"]` as `uow_id` (wrong — the UoW ID should go there), and `decision["task_id"]` as a third argument that does not exist. This would raise a `TypeError` at runtime.

Additionally, `route_wos_message` returns `task_id` as `f"wos-{uow_id}"` (e.g., `"wos-abc123"`), while `_dispatch_via_claude_p` expects the raw `uow_id` (e.g., `"abc123"`) — so even if the argument ordering were fixed, passing `decision["task_id"]` directly would require stripping the `wos-` prefix.

The correct call form is:
```python
_dispatch_via_claude_p(
    instructions=decision["prompt"],
    uow_id=decision["task_id"].removeprefix("wos-"),
)
```

This is a design-level specification error, not an implementation typo. The design document is the artifact under review, and the call pseudocode is the load-bearing specification for the implementation. Shipping this call form would produce a runtime failure on the first dispatch attempt.

**Resolution contract for Gap 1 (revised):** Correct the `_dispatch_via_claude_p` call in the routing path diagram and in the Gap 1 resolution section to match the actual function signature: two arguments (`instructions`, `uow_id`). State explicitly that `uow_id` is derived from `decision["task_id"]` by stripping the `"wos-"` prefix (or verify an alternative extraction path if the `uow_id` is carried separately in the decision dict). Either addressed or disputed — if `_dispatch_via_claude_p` has been updated to accept the three-argument form before this design is re-reviewed, cite the PR that made that change.

#### Gaps 2, 3, 4 — confirmed closed

Gap 2 (client-side filter): The routing path diagram, Key Interfaces table, and Gap 2 resolution section are all consistent and accurate. The `check_inbox()` call with client-side type filtering matches the actual MCP interface. Closed.

Gap 3 (write_result replaced): The Awareness Path section and Key Interfaces table both specify `inbox_write.py` / `write_inbox_message`. The rationale is correct. Gap 3 resolution section is accurate. Closed.

Gap 4 (Phase 2 ADR pre-condition): The Phase 2 section contains an explicit ADR pre-condition block with the required checklist items (behavioral change named, vision.yaml field paths cited, traceability to this design doc). The deferred resolution is correctly formed and the deferred scope is correctly bounded (Phase 1 does not require the ADR; Phase 2 does). Closed.

#### Unaddressed Open Question 3 (restart recovery) — not a blocking gap

Round 1 raised a question about whether `messages/processing/` accumulation from a daemon crash is handled. The design still says "TTL-based orphan recovery should handle this." This is acceptable — the Oracle Round 1 verdict flagged this as a question, not a gap. The implementation checklist should include a verification step, but this does not block the design from proceeding.

#### Encoded Orientation check (constraint-3)

Phase 1 is additive — dispatcher skip-gate does not remove an existing behavior, it gates it. No Encoded Orientation decision required for Phase 1. Phase 2 Encoded Orientation check passes: the design explicitly names the pre-condition, the ADR content requirements are spelled out, and the Round 2 oracle approval (once the signature gap is fixed) serves as the originating analysis record. Constraint-3 compliance structure is correct.

---

### Patterns Applied

**From golden-patterns.md "seam-first abstraction" (2026-03-30):** The Gap 1 signature finding is a direct application of this pattern. `route_wos_message` is the routing seam; `_dispatch_via_claude_p` is the execution seam. The design correctly names both. The signature mismatch is at the connection point between these two seams — exactly where seam-first abstraction says the interface must be explicitly correct, because that is where future callers will anchor.

**From learnings.md PR #717 ("Public method accepts new parameter but does not forward it to the private implementation method"):** The `_dispatch_via_claude_p` signature mismatch is structurally the same failure mode in reverse: the design calls a function with arguments the function does not accept. PR #717's detection rule — "trace every parameter through the delegation call" — when applied here, requires tracing `decision["agent_type"]`, `decision["prompt"]`, and `decision["task_id"]` through the `_dispatch_via_claude_p` call to verify each maps to a real parameter. This check is what surfaces the mismatch. Without this pattern as an active prior, the call pseudocode might have been read for logical intent rather than signature correctness.

---

### Revision Contract — Round 2

Gap 1 is the only remaining blocking issue. Resolution must:
- Show the corrected `_dispatch_via_claude_p` call with the correct two-argument form
- State explicitly how `uow_id` is derived from the decision dict (strip `"wos-"` prefix from `task_id`, or identify a different extraction path)
- Ensure the routing path diagram and Gap 1 resolution section are consistent with the corrected call

Gaps 2, 3, and 4 do not require any further revision.

---

## Round 1 — 2026-04-25

VERDICT: NEEDS_CHANGES
Design: wos-execute-router daemon
Round: 1

---

## Stage 1: Vision Alignment

**Prior entering review:** This design is solving the wrong problem, or solving the right problem in a direction that forecloses better paths.

**Theory of change in vision.yaml:** The active_project phase_intent is to build the substrate that lets every agent make intent-anchored decisions. The current_focus is WOS observability and reliability. principle-4 ("Integration rate before new feature rate — wire what exists before building more") and principle-3 ("Determinism over judgment for conditionals") are directly relevant.

**What would have to be true for this to be the right path?**

1. The dispatcher's LLM context window is actually being meaningfully degraded by `wos_execute` message routing.
2. A pure Python daemon can take over this routing without architectural modifications to either `route_wos_message` or the way subagents are spawned.
3. The improvement in dispatcher efficiency is worth the added operational complexity of a new always-on systemd service.

**Cheaper test not yet run:** Before designing a daemon, the cheaper test is: how many `wos_execute` messages arrive per hour? What fraction of dispatcher turns do they consume? If the answer is "4-6 per hour and each takes 3-5 seconds of dispatcher time," the optimization yields under a minute of context improvement per hour — not worth the complexity. The design states "a significant and growing tax" but provides no measurement. This is an optimization without a profiled baseline.

**What does this work foreclose?** The daemon pattern as described introduces a permanent split between where subagents are *dispatched-from* (daemon) and where WOS *state is observed* (dispatcher via notifications). This creates a structural coupling: any future change to how `wos_execute` messages are processed now requires updating both the daemon logic and the dispatcher's notification handler. If the daemon becomes the authoritative execution path, the dispatcher's awareness of WOS execution becomes derivative and notification-dependent rather than direct.

**Alignment verdict:** Questioned — not Misaligned. The architectural direction (mechanical routing out of LLM context) is consistent with principle-3 and matches the stated UoW v3 direction. But the design contains a foundational architecture gap (Gap 1 below) that makes this design unsound as written. The problem it is solving is real; the proposed solution has not resolved the mechanism by which subagents actually get spawned.

---

## Stage 2: Quality Review

### What the design gets right

The direction is correct. Pulling zero-reasoning routing out of the dispatcher's LLM context is consistent with principle-3 (determinism over judgment for conditionals) and principle-4 (wire what exists). The WOS config gate, the throttle gate, and the two-phase migration path are all sound structural decisions. The observation that this is the first concrete step toward UoW v3 decoupling is well-grounded.

### Gap 1: The daemon cannot actually spawn subagents — the design's core mechanism is broken

**This is the foundational gap.** The design states:

> The daemon calls `route_wos_message(msg)` from `dispatcher_handlers.py` and marks messages processed.

Reading the actual implementation: `route_wos_message` is a pure function. It does not spawn subagents. It returns a dict with `action: "spawn_subagent"` and a `prompt` field. The docstring is explicit: *"The dispatcher is still responsible for spawning the subagent Task and for all mark_processing / mark_processed bookkeeping — this function is pure."*

The dispatcher then uses Claude Code's `Task` tool to actually launch the functional-engineer subagent with that prompt. The `Task` tool is a Claude Code LLM tool — it is not callable from a Python daemon.

**The result:** if the daemon calls `route_wos_message`, it gets back `{"action": "spawn_subagent", "prompt": "...", ...}` and has no way to act on it. The `wos_execute` message would be claimed and marked processed, but no subagent would ever run. The UoW would silently orphan.

**The existing `_dispatch_via_claude_p` path in executor.py is the analog mechanism** — it spawns subagents from Python via `claude -p` subprocess. This is the CI/dev path and is described as "legacy" for production. A Python daemon that wants to dispatch Claude subagents must either: (a) use the `claude -p` subprocess path explicitly (synchronous, with PR #914's known violation of the non-blocking requirement), or (b) write a `wos_execute` message back into the inbox and let the dispatcher handle it — which is exactly what already happens today, making the daemon redundant.

The design does not address this mechanism gap. It treats `route_wos_message` as if it performs the spawn, when it only builds the spawn instructions.

**Resolution contract for this gap:** The design must specify how the daemon actually executes the subagent dispatch. Three options exist, each with different tradeoffs:
- Option A: Daemon uses `claude -p` subprocess (synchronous; makes daemon a blocking process; introduces PR #914's known violation risk)
- Option B: Daemon writes a `wos_execute` message *back* to a separate daemon-specific queue, and a separate Claude session picks it up (adds indirection, defeats the goal)
- Option C: Redesign `route_wos_message` so it performs the spawn directly (via Claude Agent SDK or equivalent) and the daemon calls it as an actual execution primitive, not just a prompt-builder

The design must name and defend one of these options, or identify a mechanism not listed here, before implementation begins.

### Gap 2: `check_inbox` has no `type` filter parameter

The design shows:
```python
check_inbox(source=internal, type=wos_execute)
```

The `check_inbox` MCP tool's `handle_check_inbox` implementation accepts only `source`, `limit`, and `since_ts` parameters. There is no `type` filter. The daemon would need to either: (a) filter on the client side after receiving messages (inefficient if non-WOS messages dominate the inbox), or (b) a new `type` filter parameter must be added to `check_inbox`. Neither option is mentioned in the design. At current WOS throughput this is a minor inefficiency, but it is an implementation-blocking gap — the daemon as written calls an interface that does not exist.

**Resolution contract:** State whether the daemon will filter client-side or whether `check_inbox` will be extended with a `type` parameter. If client-side, acknowledge the false-claim about the current MCP interface and verify the approach is sufficient at expected message rates.

### Gap 3: Notification mechanism presupposes the daemon has a Claude session with `write_result` access

The design specifies:
> daemon calls `write_result(type=subagent_notification)` after each successful dispatch batch

`write_result` is a Lobster MCP tool, callable only within a Claude subagent context. A standalone Python daemon process does not have a Claude session and cannot call MCP tools via the normal session-bound mechanism. The existing pattern for Python daemons to write to the dispatcher is the `write_inbox_message` / `inbox_write.py` pattern (used by executor-heartbeat, steward-heartbeat, and auto-router) — not `write_result`.

This is a smaller gap than Gap 1 but blocks the awareness path described in the design.

**Resolution contract:** Replace `write_result` with the correct inter-process notification mechanism for Python daemons (direct inbox file write via `inbox_write.py` / `write_inbox_message`, producing a `subagent_notification` type message the dispatcher can read). If a different approach is intended, state it explicitly.

### Gap 4: Encoded Orientation decision check (constraint-3)

The design proposes removing `wos_execute` processing from the dispatcher's primary loop (Phase 2) and redirecting it to a daemon. This is an Encoded Orientation change: it changes a behavioral default in the dispatcher's gate register (the `WOS Execute Gate` in CLAUDE.md) and modifies how the dispatcher identifies and handles a message type.

Per constraint-3, this requires: (a) a prior logged decision in `oracle/verdicts/` or `vision.yaml open_decisions`, and (b) a traceable `vision.yaml` anchor.

The design doc itself is not a logged decision. The related issue (#940) is not a logged decision per the learnings.md PR #894 pattern ("A GitHub issue describing a design decision is not equivalent to a logged oracle/vision decision"). The UoW v3 connection described in the design is aspirational context, not a logged decision.

The vision.yaml anchor exists at `principle-3` (determinism over judgment for conditionals) and `active_project.current_phase` (WOS Phase 1). The anchor is real. The missing piece is the logged prior decision.

**Resolution contract:** Before implementation begins (not at this design review stage, but before the first implementation PR), log a decision in `oracle/decisions.md` or equivalent that: (a) names the behavioral change (removing `wos_execute` from dispatcher loop), (b) cites the vision.yaml anchor by field path (`principle-3`, `active_project.current_phase`), and (c) is traceable to this design review as the originating analysis. This is required for Phase 2 of the migration. Phase 1 (daemon runs alongside dispatcher) does not require this — the dispatcher skip-gate is additive, not a removal.

### Open Questions Assessment

The four open questions in the design are correctly identified. Two additional observations:

**On Open Question 3 (restart recovery):** The TTL-based orphan recovery in steward-heartbeat handles UoWs that were dispatched but never completed. It does not handle messages that were claimed by the daemon (`mark_processing`) but never reached `mark_processed` due to a crash. The existing `messages/processing/` directory accumulates such messages until a recovery sweep runs. The design should confirm that the existing `mark_failed` path or the orphan recovery sweep covers this case, rather than assuming "TTL recovery should handle this."

**On Open Question 4 (systemd vs. cron-direct):** The design correctly identifies this as systemd. However, the codebase's CLAUDE.md convention for always-on daemons (Type B) is systemd, while Type C (periodic scripts) is cron-direct. This daemon is Type B — it runs continuously, polling every 30s. The implementation checklist correctly calls it a systemd service. Confirm that the daemon's restart policy (`Restart=on-failure` vs. `Restart=always`) is specified in the unit file design, since a daemon that crashes mid-routing with a claimed message is a meaningful failure mode.

---

## Patterns Applied

**From learnings.md PR #914** ("Synchronous subprocess call inside a dispatch routing function violates the dispatcher's 7-second rule"): This pattern shaped Gap 1's analysis directly. The design proposes having the daemon call `route_wos_message` as if it performs the spawn — but as PR #914 established, any handler that does real work synchronously inside the routing function is a violation. The daemon is essentially proposing to move this violation from the dispatcher to a new process, but the fundamental question (who actually launches the subagent, and how) remains unresolved. Without reading PR #914's pattern, Gap 1 might have been filed only as "the daemon can't call the Task tool" — but the PR #914 context clarifies that the correct resolution is specifically about whether work is deferred vs. executed synchronously.

**From golden-patterns.md "seam-first abstraction" (2026-03-30):** This pattern confirms the architectural direction. The seam between "message routing instruction" and "subagent execution" is exactly the kind of boundary the seam-first pattern names as needing explicit abstraction. `route_wos_message` was built as the routing seam; the daemon design needs an execution seam to match. Gap 1 is a naming issue: the design conflates the routing seam with the execution seam.

**From learnings.md PR #882** ("Data file with behavioral intent added without design decision anchor"): Gap 4 (Encoded Orientation without logged prior) was weighted more heavily because this pattern established the precedent that even data-layer changes implying automated behavior require constraint-3 compliance. A daemon change to the dispatcher's gate register is more clearly an Encoded Orientation change than a YAML file — the pattern raises the bar, not lowers it.

---

## Revision Contract

Each gap must be resolved in one of three ways before this design is approved and an implementation PR is opened:

- **Gap 1 (subagent spawning mechanism):** addressed — design names the specific mechanism (claude -p subprocess, Agent SDK, or other); or disputed — author explains why `route_wos_message` is sufficient and identifies the spawn path I missed; or deferred — author acknowledges the gap and states what must be resolved before the implementation PR can be opened.

- **Gap 2 (`check_inbox` type filter):** addressed — design specifies client-side filtering OR proposes `check_inbox` extension; or disputed — author identifies where the type filter exists in the current interface.

- **Gap 3 (`write_result` vs. inbox write):** addressed — design substitutes the correct inter-process notification mechanism; or disputed — author identifies how the daemon obtains a Claude session context to call `write_result`.

- **Gap 4 (Encoded Orientation logged prior):** deferred is acceptable — acknowledge that Phase 2 requires a logged decision before implementation and note that this design review serves as the pre-decision analysis. Phase 1 implementation can proceed without the logged prior.

Generic "improvement" without tracing to a named gap does not count as resolution.
