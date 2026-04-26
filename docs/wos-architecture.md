# WOS Architecture — Canonical Reference
*Produced: 2026-04-26*
*Covers PRs #965–#981*

Source files read:
- `~/lobster/src/orchestration/steward.py` — lifecycle, execution_attempts, orphan classification, escalation write paths
- `~/lobster/src/orchestration/dispatcher_handlers.py` — handle_wos_escalate, handle_wos_surface, handle_wos_diagnose, route_wos_message
- `~/lobster/src/orchestration/registry_cli.py` — cmd_trace, _suggest_diagnosis, _ORPHAN_RETURN_REASONS

---

## Section 1: UoW Lifecycle State Machine

```
UNIT OF WORK STATUS TRANSITIONS
=================================

                    [operator approves]
  proposed ─────────────────────────────> pending
                                              |
                                    [auto-advance on write]
                                              |
                                              v
                                    ready-for-steward
                                              |
                              steward-heartbeat.py runs
                              _process_uow()
                                              |
                              +───────────────+───────────────+
                              |                               |
                       [diagnosis loop]               [germination gate]
                       prescribe again                 issue closed/stale
                              |                               |
                              v                               v
                      ready-for-executor                  cancelled
                              |
                   executor-heartbeat.py
                   checks execution_enabled
                   + file_scope shard gate
                   dispatches wos_execute message
                              |
                              v
                           active
                              |
                   subagent runs, writes heartbeats
                   every 60–90 seconds
                              |
                   [on timeout without heartbeat]
                   Steward detects stall ──────────> orphan recovery
                              |
                   subagent writes result.json
                   and calls write_result()
                              |
                              v
                           executing
                              |
                   [next steward cycle reads result]
                              |
            +─────────────────+──────────────────+
            |                                    |
     outcome=complete                   outcome=failed/partial/blocked
            |                                    |
            v                                    v
          done                         return_reason classification
                                                 |
                     +───────────────────────────+───────────────────────────+
                     |                           |                           |
               is_infra_event=True       is_infra_event=False           error/abnormal
               (orphan return reason)    (confirmed execution)          (non-infra)
                     |                           |                           |
               execution_attempts         execution_attempts              execution_attempts
               NOT incremented             incremented +1                 incremented +1
                     |                           |                           |
                     +─────────────┬─────────────+                           |
                                   |                                         |
                     execution_attempts > MAX_RETRIES (3)?                   |
                     (PR #965: gates on confirmed dispatches only)           |
                                   |                                         |
                        +──────────+──────────+                              |
                        |                     |                              |
                      YES                    NO                              |
                        |                     |                              |
                        v                     v                              v
           registry transition:         prescribe again                 steward_cycles
           diagnosing →                 ready-for-executor               incremented,
           needs-human-review                                             prescribe again
           append_audit_log(retry_cap_exceeded)
                        |
           [escalation_notifier injected?]
                        |
              +─────────+─────────+
              |                   |
          collector path       direct path
          (consolidated)       (no collector)
              |                   |
              v                   v
       _collect_escalation()  _write_wos_escalate_message()
       append to              writes inbox JSON type="wos_escalate"
       _pending_escalations


TERMINAL STATES
================
  done                — all prescribed steps confirmed complete
  failed              — user_closed (decide-close) OR hard_cap_cleanup
  cancelled           — germination gate: issue closed
  needs-human-review  — retry cap exceeded; awaiting human decision
  expired             — proposal exceeded 14-day window without approval
```

---

## Section 2: Steward Cycle Flow

```
STEWARD CYCLE (steward-heartbeat.py → run_steward_cycle)
==========================================================

steward cycle begins
  _pending_escalations = []
        |
        v
  for each UoW in ready-for-steward:
  [BOOTUP_CANDIDATE_GATE: skip if issue has bootup-candidate label AND gate not cleared]
        |
        v
  ORPHAN RECOVERY ARC (PR #967, PR #968)
  ----------------------------------------
  Is UoW reentry posture in _ORPHAN_POSTURES?
  (executor_orphan, executing_orphan, diagnosing_orphan)
        |
       YES
        |
        v
  _classify_orphan_from_trace(trace_data, output_ref):
    Priority 1: result.json present alongside output_ref?
      → "completed_without_output"
    Priority 2: trace.json absent?
      → "kill_before_start" (default)
    Priority 3: trace.json has surprises or prescription_delta?
      → "kill_during_execution"
    Default:
      → "kill_before_start"

  Write orphan_kill_classified audit entry:
    {kill_type, heartbeats_before_kill, ts}

  (heartbeat-based classification added by PR #968 — distinguishes
   orphan_kill_before_start vs orphan_kill_during_execution)

  EXECUTION ATTEMPTS ACCOUNTING (PR #965)
  -----------------------------------------
  return_reason in ORPHAN_REASONS (executor_orphan / executing_orphan /
  diagnosing_orphan)?
        |
       YES → is_infra_event = True  → execution_attempts NOT incremented
        NO → is_infra_event = False → execution_attempts incremented +1

  new_execution_attempts > MAX_RETRIES (3)?
        |
  +─────+─────+
  |           |
 YES          NO
  |           |
  v           v
  RETRY CAP EXCEEDED        prescribe again
  ──────────────────        ready-for-executor
  transition: diagnosing → needs-human-review
  append_audit_log(retry_cap_exceeded)

  escalation_notifier injected? (only when NOT dry_run)
        |
  +─────+─────+
  |           |
  YES          NO
  |           |
  v           v
  _collect_escalation(uow):  _write_wos_escalate_message() immediately
    - read audit_log           type="wos_escalate"
    - extract execution_attempts from retry_cap_exceeded entry
    - determine reentry_posture
    - append EscalationRecord to _pending_escalations

  [end of UoW loop]

  EARLY WARNING CHECK
  --------------------
  lifetime_cycles + new_steward_cycles >= _EARLY_WARNING_CYCLES (4)?
    → write wos_early_warning message to inbox (informational, no routing)

  ESCALATION CONSOLIDATION (PR #966)
  ------------------------------------
  len(_pending_escalations) >= ESCALATION_CONSOLIDATION_THRESHOLD (3)?
        |
  +─────+─────+
  |           |
  YES          NO
  |           |
  v           v
  _send_consolidated_    for each EscalationRecord:
  escalation_            _write_wos_escalate_message()
  notification()         (individual wos_escalate per UoW)
    writes ONE message:
    type: "wos_surface"
    condition: "retry_cap_consolidated"
    escalation_count: N
    causes: [raw return_reasons]
    uow_ids: [all affected UoWs]
```

---

## Section 3: Escalation Paths (Two Tracks)

### Track A — wos_escalate (per-UoW)

```
STEWARD WRITES wos_escalate
=============================

  Condition: UoW exhausts retry cap
  Writer: _write_wos_escalate_message()
  Trigger: direct path (no collector) OR collector below threshold

  Message fields:
    type: "wos_escalate"
    uow_id: <str>
    uow_title: <str>
    register: <operational|human-judgment|philosophical|iterative-convergent>
    failure_history:
      execution_attempts: <int>          (confirmed dispatches, not total retries)
      return_reason_classification: <str> (orphan|error|abnormal)
      kill_type: <str>                   (orphan_kill_before_start|orphan_kill_during_execution)
      heartbeats_before_kill: <int>      (0 = killed before execution)
    posture: <str>                       (trace-diagnosed reentry posture)
    suggested_action: <str>             (mirrors 4-branch tree; informational)


DISPATCHER RECEIVES wos_escalate
===================================

  route_wos_message(msg) → handle_wos_escalate(msg)
  [wos_escalate runs before spawn-gate: legitimately returns send_reply OR spawn_subagent]

  Decision tree — checked in order (first match wins):

  ┌─ BRANCH 4: Human-judgment register ──────────────────────────────┐
  │  register in {"human-judgment", "philosophical"}?                 │
  │  → action=send_reply                                              │
  │    surface to Dan: [/decide proceed] or [/decide abandon]        │
  └───────────────────────────────────────────────────────────────────┘

  ┌─ BRANCH 3: Execution cap exhausted ──────────────────────────────┐
  │  execution_attempts >= 3?                                         │
  │  (prescription attempted 3+ times — retrying loops without       │
  │   diagnosis)                                                      │
  │  → action=send_reply                                              │
  │    surface to Dan: [/decide retry] or [/decide abandon]          │
  └───────────────────────────────────────────────────────────────────┘

  ┌─ BRANCH 1: Pure infrastructure failure ──────────────────────────┐
  │  execution_attempts == 0 AND classification == "orphan"?          │
  │  (UoW never executed — session killed before agent started)       │
  │  → action=spawn_subagent                                          │
  │    task_id: escalate-retry-<uow_id[:12]>                         │
  │    prompt: run steward-heartbeat.py                               │
  │    (auto-retry; no execution budget consumed)                     │
  └───────────────────────────────────────────────────────────────────┘

  ┌─ BRANCH 2: Mid-execution kill ────────────────────────────────────┐
  │  execution_attempts > 0 AND classification == "orphan"?           │
  │  (agent was executing when session was killed; partial work may   │
  │   exist)                                                           │
  │  → action=spawn_subagent                                          │
  │    task_id: escalate-midexec-<uow_id[:12]>                       │
  │    prompt: run steward-heartbeat.py                               │
  │    (retry with resume context)                                    │
  └───────────────────────────────────────────────────────────────────┘

  ┌─ DEFAULT: Unclassified failure ────────────────────────────────────┐
  │  → action=send_reply                                              │
  │    surface to Dan for review                                      │
  └───────────────────────────────────────────────────────────────────┘


DISPATCHER ACTS
================

  action=spawn_subagent                    action=send_reply
          |                                        |
          v                                        v
  spawn background subagent           send_reply(chat_id=ADMIN_CHAT_ID)
  runs steward-heartbeat.py                        |
          |                            Dan sees Telegram notification
          v                            /decide <uow_id> retry|abandon|defer
  steward re-queues UoW
  → ready-for-executor
  executor dispatches again
```

### Track B — wos_surface (batch kill-wave)

```
STEWARD WRITES wos_surface
============================

  Condition A (PR #966): >= 3 UoWs escalate in one steward cycle
  Writer: _send_consolidated_escalation_notification()
    condition: "retry_cap_consolidated"
    escalation_count: N
    causes: [return_reason per UoW, positionally aligned with uow_ids]
    uow_ids: [all affected UoW IDs]

  Condition B (fallback): _write_wos_escalate_message() raises OSError
  Writer: _send_escalation_notification()
    condition: "retry_cap"
    uow_id: <singular>
    (no causes list — falls through to surface-all-to-Dan branch)


DISPATCHER RECEIVES wos_surface (PR #981)
==========================================

  route_wos_message(msg) → handle_wos_surface(msg)
  [wos_surface runs before spawn-gate: same exemption as wos_escalate]

  Decision tree — checked in order (first match wins):

  ┌─ BRANCH: Pipeline paused ─────────────────────────────────────────┐
  │  is_execution_enabled() == False?                                  │
  │  (do not auto-retry into a stopped pipeline)                       │
  │  → action=send_reply                                               │
  │    notify Dan: pipeline is paused, list all UoW IDs               │
  │    suggest: /wos start, then /decide retry for each               │
  └────────────────────────────────────────────────────────────────────┘

  ┌─ BRANCH: All causes are orphan return_reasons ─────────────────────┐
  │  every causes[i] in _SURFACE_ORPHAN_RETURN_REASONS?                │
  │  (single infrastructure kill wave; no execution budget consumed)   │
  │  → action=spawn_subagent                                           │
  │    task_id: surface-batch-retry-<N>uow                            │
  │    prompt: run steward-heartbeat.py once (re-queues all)          │
  │    also sends Dan a brief summary notification (no action needed)  │
  └────────────────────────────────────────────────────────────────────┘

  ┌─ BRANCH: Mixed causes ─────────────────────────────────────────────┐
  │  some causes are orphan, some are not?                             │
  │  → action=send_reply                                               │
  │    surface non-orphan UoWs to Dan                                  │
  │    list orphan UoWs separately with /decide retry suggestion       │
  │    (note: handler returns one action; no concurrent spawn+reply)   │
  └────────────────────────────────────────────────────────────────────┘

  ┌─ DEFAULT: All non-orphan or no causes list ───────────────────────┐
  │  → action=send_reply                                               │
  │    surface all UoW IDs to Dan                                      │
  └────────────────────────────────────────────────────────────────────┘


_SURFACE_ORPHAN_RETURN_REASONS (raw return_reason strings):
  executor_orphan
  executing_orphan
  diagnosing_orphan
  orphan_kill_before_start
  orphan_kill_during_execution
```

---

## Section 4: Diagnosis Path

```
DIAGNOSIS PATH (PR #980)
==========================

  Dan types: "diagnose <uow_id>" in Telegram
          |
          v
  Dispatcher: parse_diagnose_command(text)
    - pattern: "diagnose " prefix (case-insensitive)
    - extracts uow_id token
    - returns uow_id or None
          |
          v  (non-None)
  Dispatcher writes wos_diagnose message to inbox:
    type: "wos_diagnose"
    uow_id: <str>
    escalation_id: "" (manual trigger)
    escalation_trigger: "manual"
    failure_history: {} (no pre-computed context)
          |
          v
  route_wos_message(msg) → handle_wos_diagnose(msg)
  [runs inside spawn-gate: always returns action="spawn_subagent"]
          |
          v
  _resolve_uow_id(raw_uow_id):
    Today: direct pass-through (full IDs unchanged)
    Future PR: short-ID lookup via registry
          |
          v
  Spawns diagnostic subagent:
    task_id: wos-diagnose-<uow_id[:12]>
    agent_type: "lobster-generalist"
          |
          v
  SUBAGENT RUNS DIAGNOSIS ALGORITHM
  -----------------------------------
  Step 1: uv run registry_cli.py trace --id <uow_id>
          Read: diagnosis_hint, return_reasons, execution_attempts,
                kill_classification
          |
  Step 2: Apply diagnosis algorithm (mirrors _suggest_diagnosis logic):
          ORPHAN_REASONS = {executor_orphan, executing_orphan,
                            diagnosing_orphan, orphan_kill_before_start,
                            orphan_kill_during_execution}

    - ALL return_reasons in ORPHAN_REASONS AND execution_attempts == 0?
        posture=reset, pattern="infrastructure-kill-wave"
    - ALL return_reasons in ORPHAN_REASONS AND
      kill_type == "orphan_kill_before_start"?
        posture=reset, pattern="kill-before-start"
    - ALL return_reasons in ORPHAN_REASONS AND
      kill_type == "orphan_kill_during_execution"?
        posture=reset, pattern="kill-during-execution"
    - execution_attempts >= MAX_RETRIES (3)?
        posture=surface-to-human, pattern="genuine-retry-cap"
        (also runs: registry_cli get --id <uow_id> for steward_log context)
    - lifetime_cycles >= HARD_CAP?
        posture=surface-to-human, pattern="hard-cap"
    - steward_cycles >= 3 AND execution_attempts == 0 AND no orphan reasons?
        posture=surface-to-human, pattern="dead-prescription-loop"
    - Otherwise:
        posture=surface-to-human, pattern="unrecognised"
          |
  Step 3: Check wos-config.json:
          execution_enabled == False?
          → override posture to surface-to-human (never reset into stopped pipeline)
          |
  Step 4: Status check before decide-retry:
          status == "needs-human-review"?
          → surface-to-human with note: "status must be blocked first"
          (registry_cli decide-retry accepts blocked or ready-for-steward only)
          |
  Step 5 (if posture=reset AND status is blocked/ready-for-steward):
          uv run registry_cli.py decide-retry --id <uow_id>
          |
  Step 6: write_result(task_id, chat_id=0, sent_reply_to_user=False)
          Outputs structured JSON diagnosis:
          {
            event, uow_id, escalation_id, escalation_trigger,
            pattern_matched, confidence, posture,
            action_taken, rationale,
            execution_attempts_at_diagnosis, lifetime_cycles_at_diagnosis,
            surface_message (if posture=surface-to-human),
            timestamp
          }
          |
  [Dispatcher receives write_result notification and decides
   whether to relay surface_message to Dan]


CONSTRAINTS
============
  - Max 3 shell commands total: trace + optionally get + optionally decide-retry
  - Never call decide-close (requires human confirmation)
  - Never send Telegram messages directly (write_result only)
  - One UoW per invocation — no batch loops
```

---

## Section 5: Forensics — registry_cli trace

```
REGISTRY_CLI TRACE COMMAND (PR #976)
=======================================

  Invocation: uv run registry_cli.py trace --id <uow_id>

  cmd_trace() joins five data sources:

  1. registry.get(uow_id)
     → current_state: {status, execution_attempts, lifetime_cycles,
                        steward_cycles, retry_count, heartbeat_at,
                        output_ref, close_reason, started_at}

  2. registry.fetch_audit_entries(uow_id)
     → audit_log: chronological list of all status transitions,
                   retry_cap events, orphan_kill_classified entries

  3. registry.fetch_corrective_traces(uow_id)
     → corrective_traces: executor observations per attempt
                           (partial work, context from prior runs)

  4. _extract_return_reasons(audit_entries)
     → return_reasons: [{ts, event, return_reason}] from audit note JSON

  5. _extract_kill_classification(audit_entries)
     → kill_classification: most recent orphan_kill_classified entry
       {kill_type, heartbeats_before_kill, ts} or null

  6. _read_trace_json(output_ref)
     → trace_json: parsed trace.json from output_ref path, or null
     Path derivation:
       Primary:  Path(output_ref).with_suffix(".trace.json")
       Fallback: Path(str(output_ref) + ".trace.json")

  7. _suggest_diagnosis(uow, return_reasons, kill_classification, trace_json)
     → diagnosis_hint: one-paragraph actionable summary

  Output: JSON to stdout (all fields above)


_suggest_diagnosis() PATTERN MATCHING (first match wins):
============================================================

  Pattern 1: infrastructure-kill-wave
    Condition: all return_reasons in _ORPHAN_RETURN_REASONS AND len >= 2
    Hint: "reset with decide-retry; investigate session TTL"

  Pattern 2: kill-before-start
    Condition: kill_classification present AND
               kill_type == "orphan_kill_before_start"
               OR (trace_json absent AND kill_type present)
    Hint: "reset with decide-retry; execution_attempts not charged"

  Pattern 3: kill-during-execution
    Condition: kill_classification present AND
               kill_type == "orphan_kill_during_execution"
    Hint: "reset with decide-retry; review trace_json.prescription_delta"

  Pattern 4: retry-cap-from-orphans
    Condition: status == needs-human-review AND execution_attempts == 0
    Hint: "reset with decide-retry after confirming executor dispatch healthy"

  Pattern 5: retry-cap
    Condition: status == needs-human-review AND execution_attempts > 0
    Hint: "review corrective_traces; decide-retry or decide-close"

  Pattern 6: dead-prescription-loop
    Condition: steward_cycles >= 3 AND execution_attempts == 0
    Hint: "check executor dispatch enabled; throttle clear"

  Pattern 7: early-stage
    Condition: status in (proposed, pending)

  Pattern 8: completed
    Condition: status == done

  Default: "No known failure pattern matched. Review audit_log."


_ORPHAN_RETURN_REASONS (module-level, importable by tests):
=============================================================
  executor_orphan
  executing_orphan
  diagnosing_orphan
  orphan_kill_before_start         (PR #968 — heartbeat-classified)
  orphan_kill_during_execution     (PR #968 — heartbeat-classified)


REGISTRY_CLI COMMAND REFERENCE
================================
  trace --id <uow_id>                full forensics view (start here)
  get --id <uow_id>                  raw UoW row with all fields
  list [--status <status>]           list UoWs, optional status filter
  approve --id <uow_id>              proposed → pending
  decide-retry --id <uow_id>         blocked/ready-for-steward → ready-for-steward
                                     (steward_cycles reset to 0)
  decide-close --id <uow_id>         blocked → failed (user_closed)
  status-breakdown                   count UoWs by status (JSON object)
  escalation-candidates              list needs-human-review UoWs
  stale [--buffer-seconds N]         list in-flight UoWs with silent heartbeats
  check-stale                        active UoWs whose source issue is closed
  expire-proposals                   expire proposed records older than 14 days
  gate-readiness                     WOS autonomy gate metric
  upsert --issue N --title T         propose a UoW for a GitHub issue
```

---

## Section 6: Gap Analysis

### WOS_MESSAGE_TYPE_DISPATCH (current state)

```python
WOS_MESSAGE_TYPE_DISPATCH = {
    "wos_execute":      "handle_wos_execute",      # IMPLEMENTED
    "steward_trigger":  "handle_steward_trigger",   # IMPLEMENTED
    "wos_escalate":     "handle_wos_escalate",      # IMPLEMENTED (PR #970)
    "wos_surface":      "handle_wos_surface",       # IMPLEMENTED (PR #981)
    "wos_diagnose":     "handle_wos_diagnose",      # IMPLEMENTED (PR #980)
}
```

### Gap 1: wos_surface mixed-branch cannot spawn + reply simultaneously

**Code location:** `handle_wos_surface()`, lines ~1038–1068 in dispatcher_handlers.py

In the mixed-causes branch (some orphans, some non-orphans), the handler returns
`action="send_reply"` and lists both categories for Dan. It cannot simultaneously
spawn a steward heartbeat for the orphan UoWs AND send Dan a reply about the
non-orphan UoWs — the architecture returns one action per message.

**Consequence:** Dan sees a list of both orphan and non-orphan UoWs and must issue
`/decide retry` for each orphan manually, even though they could be auto-retried.
This is documented in the handler comment: no workaround is currently wired.

**When it fires:** Only in the mixed-causes case. The all-orphan branch auto-retries
correctly. The no-causes / non-orphan branch surface-to-Dan correctly.

---

### Gap 2: wos_early_warning has no dispatcher handler

**Code location:** `_default_notify_dan_early_warning()` in steward.py (around line 3568)

Writes `type: "wos_early_warning"` to inbox. This type is absent from
`WOS_MESSAGE_TYPE_DISPATCH`. The message reaches Dan as a plain notification;
the dispatcher reads it as a regular text message and forwards it. There is no
programmatic routing, no acknowledgment, and no suppress path.

**Assessment:** Likely intentional — early warnings are informational, not
actionable. Whether a noop handler should be added for cleaner dispatch table
completeness is a design decision, not an engineering gap.

---

### Gap 3: wos_surface fallback path silently downgrades escalation

**Code location:** `_write_wos_escalate_message()` in steward.py (~line 3866),
`_send_escalation_notification()` (~line 3958)

When `_write_wos_escalate_message()` raises an OSError, the fallback calls
`_send_escalation_notification()` which writes `type: "wos_surface"` with
`condition: "retry_cap"` (singular, no causes list). This message falls through
to the surface-all-to-Dan branch in `handle_wos_surface` — losing the 4-branch
automated triage that `wos_escalate` would have triggered.

**Consequence:** A write failure silently demotes the escalation from automated
decision logic to manual operator review. No alert is emitted when the fallback
fires. Operator has no indication that the primary path failed.

---

### Gap 4: wos_diagnose result is not relayed to Dan by default

**Code location:** `handle_wos_diagnose()`, step 6 in subagent prompt:
`sent_reply_to_user: False`

The diagnostic subagent writes its result to `write_result(chat_id=0)`. The
dispatcher receives the `subagent_notification` but the prompt instructs
`sent_reply_to_user=False`. The dispatcher must then decide whether to relay
`surface_message` to Dan — this logic is not codified in dispatcher_handlers.py.
It relies on dispatcher prose behavior.

**Consequence:** The result is visible to the dispatcher session but not automatically
forwarded to Dan. For manual (`diagnose <uow_id>`) triggers where Dan expects a
response, this requires the dispatcher to read and relay the JSON result. This is
T2-A work: a `handle_wos_diagnose_result` path that routes completed diagnosis
results to Dan when surface_message is present.

---

### Gap 5: Steward does not yet integrate wos_diagnose into its escalation path

The `wos_diagnose` handler and diagnostic subagent are fully wired for the
Telegram-command path (`diagnose <uow_id>`). The steward does not yet write
`wos_diagnose` messages as part of the escalation path. The escalation flow
goes directly to `wos_escalate` or `wos_surface` without triggering diagnosis first.

**Consequence:** Automated diagnosis before escalation surfacing is not yet wired.
The dispatch architecture supports it (the message type exists and is handled), but
steward.py does not call `_write_wos_diagnose_message()` — that function does not
exist yet. This is explicitly tracked as T2-A.

---

### Gap 6: Hard-cap commit gate is not exhaustively tested against all return_reason values

**Code location:** PR #973 — exhaustiveness test for `_RETURN_REASON_CLASSIFICATIONS`

PR #973 added an exhaustiveness test that ensures all return_reason values in
`_RETURN_REASON_CLASSIFICATIONS` map to a classification. The test documents
the authoritative classification table. What remains is ensuring that any new
return_reason added in future PRs triggers a test failure rather than silently
defaulting. The mechanism is in place; discipline is required to maintain it.

---

## Summary Table

| Message type | Writer | Condition | Dispatcher handler | Automated decision | Status |
|---|---|---|---|---|---|
| wos_execute | executor-heartbeat.py | UoW is ready-for-executor | handle_wos_execute → spawn_subagent | Always spawns execution subagent | IMPLEMENTED |
| steward_trigger | wos_completion.py | UoW transitions executing → ready-for-steward | handle_steward_trigger → spawn_subagent | Always runs steward heartbeat immediately | IMPLEMENTED |
| wos_escalate | steward.py | retry cap exceeded (direct or collector below threshold) | handle_wos_escalate | 4-branch tree: auto-retry or surface-to-Dan | IMPLEMENTED (PR #970, #974) |
| wos_surface condition=retry_cap_consolidated | steward.py | >= 3 escalations in one cycle | handle_wos_surface | 4-branch tree: pipeline-paused / all-orphan auto-retry / mixed / surface | IMPLEMENTED (PR #966, #981) |
| wos_surface condition=retry_cap | steward.py | OSError fallback in _write_wos_escalate_message | handle_wos_surface | surface-all-to-Dan (no causes list) | IMPLEMENTED but with silent fallback downgrade (Gap 3) |
| wos_diagnose | dispatcher (on "diagnose" command) | Dan types "diagnose <uow_id>" | handle_wos_diagnose → spawn_subagent | Always spawns diagnosis subagent | IMPLEMENTED (PR #980) |
| wos_early_warning | steward.py | lifetime_cycles + steward_cycles >= 4 | None (not in dispatch table) | Forwarded as plain notification | INTENTIONAL or GAP (Gap 2) |

---

## Operator Decision Reference

```
OPERATOR COMMAND SURFACE
==========================

  Telegram commands:
    /approve <uow_id>                proposed → pending
    /decide <uow_id> proceed         blocked → ready-for-steward (cycles preserved)
    /decide <uow_id> retry           blocked → ready-for-steward (cycles reset)
    /decide <uow_id> retry force     blocked → ready-for-steward (override hard-cap)
    /decide <uow_id> abandon         blocked → failed (user_closed)
    /decide <uow_id> defer [note]    blocked (unchanged) + audit entry
    /wos status [status]             list UoWs by status
    /wos start                       set execution_enabled=true in wos-config.json
    /wos stop                        set execution_enabled=false in wos-config.json
    /wos unblock                     clear BOOTUP_CANDIDATE_GATE flag
    diagnose <uow_id>                spawn diagnostic subagent via wos_diagnose

  CLI (uv run registry_cli.py):
    trace --id <uow_id>              full forensics view
    decide-retry --id <uow_id>       same as /decide retry
    decide-close --id <uow_id>       same as /decide abandon
    escalation-candidates            list all needs-human-review UoWs
    stale                            list in-flight UoWs with silent heartbeats
    status-breakdown                 count by status

  Inline keyboard buttons (Telegram):
    [Retry] → callback: decide_retry:<uow_id>    → route_callback_message
    [Close] → callback: decide_close:<uow_id>    → route_callback_message
```
