# DEPRECATED — DO NOT ADD NEW ENTRIES

This file is no longer the correct location for oracle decisions or ADRs.

**Correct pattern:** Each PR review produces an audit file at `oracle/verdicts/pr-{number}.md`.
Decisions, ADRs, and behavioral change authorizations belong in the relevant PR's verdict file — not here.

Existing entries below are preserved for historical reference only.

---

# Architecture Decisions

Logged durable design choices that affect observable system behavior.

---

## ADR-003: Prescription Throttle Gate — Encoded Orientation Behavioral Default

**Date:** 2026-04-24
**Status:** Accepted
**WOS Reference:** uow_20260421_f91285

### Context

By 2026-04-24, the WOS cultivator sweeps promoted every eligible GitHub issue into the UoW registry on each cycle. The registry had accumulated 184 open UoWs over a 7-day window with a consumption_rate of 0.43 (43% of UoWs opened in the window were closed). This rate was computed as `closed / (closed + open)` over a 7-day rolling window.

The oracle PR #913 review (Stage 1) raised a structural question before this ADR was written: the rate=0.43 could reflect either (a) overproduction — cultivator adds UoWs faster than executors close them — or (b) executor-side dysfunction — callback handlers (decide_retry, decide_close) are unimplemented, causing UoWs to stall in needs-human-review or blocked states regardless of cultivator behavior.

**Stage 1 UoW status breakdown (attempted 2026-04-24):** The wos.db registry was queried at the time of this ADR but contained 0 rows — the WOS registry had not yet been populated in this environment. The 184-UoW figure and the rate=0.43 observation originate from the motivating context documented in uow_20260421_f91285. The status breakdown (bucketing by status to distinguish overproduction from executor dysfunction) could not be performed from live data.

**The oracle PR #913 verdict names this explicitly as Stage 1 open question:** if the majority of the 184 UoWs were in `needs-human-review` or `blocked`, the low rate would indicate executor-side dysfunction rather than overproduction, and backpressure would not be the correct intervention. This question remains open and is recorded here as a named constraint on the behavioral change.

### What Changed

`promote_to_wos()` in `cultivator.py` previously promoted all eligible GitHub issues on every sweep. After this PR, that behavior is suppressed silently — returning `([], 0)` — when ALL of the following are true:

- `consumption_rate < 0.6` (fewer than 60% of UoWs in the 7-day window are closed)
- `backlog_depth >= 5` (at least 5 open UoWs in the 7-day window)

This is a durable behavioral default change, not a one-time operational action.

### Why This Is an Encoded Orientation Decision

The system now acts without Dan's real-time input when the throttle conditions are met. Per constraint-3 (vision.yaml `core.inviolable_constraints`), Encoded Orientation decisions require: (a) a prior logged decision of the same class, and (b) a traceable vision.yaml anchor.

This ADR satisfies both requirements for PR #913.

### Vision Anchor

**Primary:** `core.operating_principles.principle-1` — "Proactive resilience over reactive recovery. Structural prevention is preferred over better correction mechanisms."

Suppressing new prescriptions when the queue is demonstrably not draining is a structural prevention measure: it prevents the queue from growing in a way that would require reactive cleanup. This anchors the throttle gate to a durable principle rather than to the specific measurements that triggered its implementation.

**Secondary:** `active_project.phase_intent` — Phase 1 is complete when the Registry is live and populated with UoWs carrying vision_ref fields. A registry with unbounded growth and a 0.43 consumption rate does not satisfy "live and populated in a useful sense" — it satisfies a metric while defeating the intent. Queue stability is a prerequisite to Phase 1 completion in the sense that phase_intent intends.

### Threshold Values as Durable Defaults

- `threshold = 0.6`: If fewer than 60% of UoWs created in the last 7 days are closed, the queue is structurally undersized relative to production rate.
- `min_depth = 5`: A rate below threshold on a queue of 4 or fewer UoWs is statistical noise, not a systemic signal. The dual condition prevents false positives on low-volume periods.

These values are configuration parameters in `PrescriptionThrottleGate`, not hardcoded magic numbers. They may be tuned via `PrescriptionThrottleGate(monitor, threshold=X, min_depth=Y)` at the call site.

### Constraint: Stage 1 Open Question

This decision is made with the Stage 1 question unresolved. If a status breakdown of the 184 UoWs shows the majority were in `needs-human-review` or `blocked`, then the low rate was caused by executor dysfunction, not overproduction — and this throttle gate addresses a visible symptom while the root cause persists.

The correct follow-on action if the throttle remains active beyond one cultivator cycle:
1. Query the registry: `SELECT status, COUNT(*) FROM uow_registry GROUP BY status`
2. If the majority are in `needs-human-review` or `blocked`, prioritize implementing decide_retry and decide_close handlers over tuning the throttle

The state-change notification added in this PR (Telegram inbox message on first activation) is specifically designed to surface this case: if the throttle notification fires and persists, it is a signal to run the status query before assuming overproduction.

### Interaction with od-3 Override (ADR-002)

The od-3 override (ADR-002, 2026-04-24) authorizes age-based promotion of stuck proposed UoWs via `requalify_proposed()`. The GardenCaretaker's promotion path is not gated by this throttle. The effective behavior during sustained throttling:

- Cultivator: cannot promote new GitHub issues to the registry (throttled)
- GardenCaretaker: can still advance existing proposed UoWs to ready-for-steward (not throttled)

This is coherent — the goal is to drain the existing backlog before adding new work. But it means new GitHub issues are silently blocked from entering the pipeline, which is why the state-change notification is non-optional.

---

## ADR-002: Age-Based UoW Promotion in requalify_proposed() — Override of vision.yaml od-3

**Date:** 2026-04-24
**Status:** Accepted
**Overrides:** vision.yaml `open_decisions.od-3` (resolved 2026-04-22)

### Context

vision.yaml `open_decisions.od-3` (resolved 2026-04-22) prohibited age-only promotion of proposed UoWs to ready-for-steward. The original rationale: age-only advancement would promote issues the Steward has not yet reviewed via label signal. The label gate (ready-to-execute, high-priority, or bug) was required as the prior-decision anchor.

By 2026-04-24, 236 proposed UoWs had accumulated without qualifying labels, creating a stuck queue. The GardenCaretaker's requalify_proposed() method, bounded at 20 UoWs/cycle, was unable to drain the queue without operator intervention to apply labels manually.

### Authorization

Dan Cetlin explicitly authorized continuous flow: "I just want continuous flow" — 2026-04-24.

This in-conversation authorization overrides od-3 and approves age-based promotion (issue open >=3 days, no blocking labels) as a permanent criterion in requalify_proposed(). Dan accepted the trade-off: UoWs may now advance to ready-for-steward without Steward label signal, relying solely on the blocking-labels mechanism as the brake.

### Decision

The `require_label=True` parameter in the `requalify_proposed()` call site is changed to `require_label=False`. GardenCaretaker now promotes proposed UoWs to ready-for-steward when:
- The source GitHub issue is open, AND
- The issue has been open >= 3 days, AND
- The issue carries no blocking labels

Label qualification (ready-to-execute, high-priority, or bug) is no longer required. This is a durable default change — not a one-time operational action.

### Rationale

Continuous queue flow was prioritized over Steward label-gate review. The 236 stuck UoWs represent unreviewed proposed work that would not drain under the original od-3 constraint without manual label application. Dan authorized the label gate to be dropped in favor of throughput.

### Constraint

The blocking-labels mechanism (explicit hold via label on source issue) remains intact and is the only remaining brake on age-based promotion. Issues the Steward or Dan want to hold must carry a blocking label.

### Vision Anchor

vision.yaml `open_decisions.od-3` updated with `overridden_at: 2026-04-24` and `override_authority: Dan Cetlin (explicit in-conversation authorization)`. The original constraint is preserved in the override record for audit purposes.

---

## ADR-001: Juice-First Dispatch Ordering for Ready-for-Steward UoWs

**Date:** 2026-04-24
**Status:** Accepted

### Authorizing Design Thread

- Issue #886: Initial metabolic-juice concept and WOS integration intent
- Issue #888: Juice sensing protocol and signal definitions
- Issue #889: Observable signals distinguishing juice from indeterminate state

### Approval

Dan Cetlin approved: "good lets implement" — 2026-04-24.

### Decision

UoWs in the `ready-for-steward` state are dispatched in juice-first order:
UoWs with `juice_quality='juice'` are placed at the front of the dispatch
queue ahead of UoWs with null or absent juice quality.

### Constraint

Juice-first ordering activates only for `ready-for-steward` UoWs. The
shard-stream gate already ensures null-juice UoWs are not starved: they
remain eligible for dispatch and are not blocked by the presence of
juiced UoWs.

### Follow-on (Advisory from Oracle)

Starvation mitigation sequencing gap: the current implementation does not
guarantee a dispatch slot for null-juice UoWs within a bounded time window
when juiced UoWs are continuously present. A follow-on task should evaluate
whether a fairness slot (e.g., every N cycles, promote the oldest null-juice
ready-for-steward UoW to the front) is warranted as load increases.

---

## ADR-004: 'closed' Treated as Terminal in SQL Exclusion Contexts — Encoded Orientation

**Date:** 2026-04-27
**Status:** Accepted
**PR:** #1003 (fix/issue-dedup-reactivate)

### Context

The UoWStatus enum covers all current status values. However, the production
registry contains rows with `status='closed'` — a legacy value set externally
before enum enforcement was introduced. As of the time this ADR was written,
768 rows carried `status='closed'`.

`UoWStatus.is_terminal()` intentionally excludes 'closed' because it is not
a valid enum value — `UoWStatus('closed')` raises `ValueError`. Rows in
`'closed'` state cannot be represented as `UoWStatus` instances.

### Problem

In `_upsert_typed` and `has_active_uow_for_issue`, the SQL exclusion list
(the set of statuses that count as "already done, re-proposal is allowed")
did not include 'closed'. This caused legacy rows with `status='closed'` to
be treated as non-terminal — i.e., as if an active UoW already existed for
that issue — permanently blocking re-proposal for any issue whose only
registry row was in the 'closed' state.

### Decision

'closed' is treated as terminal in all SQL exclusion contexts. Specifically:

- `_upsert_typed` adds 'closed' to its NOT IN exclusion list alongside the
  enum terminal statuses (done, failed, expired, cancelled).
- `has_active_uow_for_issue` uses `_TERMINAL_STATUSES_FOR_ISSUE_CHECK` — a
  frozenset containing the same five values — as the authoritative source for
  its SQL NOT IN clause.

`UoWStatus.is_terminal()` is NOT updated. It covers only enum-representable
statuses. The docstring for `is_terminal()` explicitly names this boundary.

### Rationale

Semantically, 'closed' means the same thing as 'done': the work is finished
and re-proposal should be allowed. The only reason it was not enumerated is
that the enum did not exist when those rows were written. Treating 'closed'
as non-terminal was unintentional — an artifact of the enum boundary, not a
deliberate design choice.

### Constraint

New code must not write `status='closed'` directly. All new terminal writes
must use `UoWStatus.DONE` (or another enum value as appropriate). The 'closed'
treatment as terminal is for legacy-row compatibility only. The production
migration to reclassify existing 'closed' rows as 'done' is out of scope for
this PR and tracked as a follow-on.

---

## ADR-005: Fast-Path Classification Bypass for Writer-Provided signal_type_hint — Encoded Orientation

**Date:** 2026-05-01
**Status:** Accepted
**PR:** #1032 (mem-event-subject-tagging)
**WOS Reference:** uow_20260501_8de2bc

### Context

PR #1032 adds `subject` and `signal_type_hint` columns to the memory events table. The `signal_type_hint` field allows a producer (harvester, scheduled job, dispatcher) to pre-classify an event at write time. The slow-reclassifier's `run_pass` function uses this hint via a fast path: events with `signal_type_hint` set bypass all content-inference pattern detection and receive a `confidence="high"` slow-v1 tag using the caller's self-declared type.

This is an Encoded Orientation decision under vision.yaml `core.inviolable_constraints.constraint-3` because it changes a durable behavioral default in the classification pipeline without requiring Dan's real-time input on each invocation.

### What Changed

Before this PR, every event processed by the slow-reclassifier went through cluster-based pattern detection (design_session, brainstorm_mode, complex_request, meta_thread, philosophy_thread). The classification result depended on the event's content and its neighbors in time.

After this PR, events with `signal_type_hint` set are classified directly by the hint value with `confidence="high"` and are excluded from cluster-based pattern detection entirely. Events without `signal_type_hint` continue through pattern detection unchanged.

This creates a two-tier classification contract: structured producers (harvesters, scheduled jobs) self-classify at write time; ad-hoc writers go through content inference. The fast path is the operative change — it is not constrained, not temporary, and not reversible per-run.

### Why This Is an Encoded Orientation Decision

The fast-path decision is structural: every event carrying a `signal_type_hint` value will be classified with `confidence="high"` on the hint, regardless of content, without a human deciding this on a per-event basis. This satisfies all three of constraint-3's conditions: (a) the system acts without Dan's explicit input, (b) it changes a durable default in the classification pipeline, and (c) the behavioral change is encoded in code, not in a retrievable prompt or conversation.

The oracle PR #1032 verdict Round 1 confirms this is a constraint-3 gap and requires this ADR as the resolution.

### Vision Anchor

**Primary:** `core.inviolable_constraints.constraint-3` — "Every system decision traverses the full OODA loop at the appropriate register. Encoded Orientation decisions require a prior logged decision of the same class and a traceable vision.yaml anchor."

This ADR is the logged decision. It satisfies constraint-3 for the fast-path classification bypass.

**Secondary:** `core.operating_principles.principle-3` — "Determinism over judgment for conditionals. If-then logic and field checks are code, not LLM instructions. Use LLMs where genuine interpretation is required."

The fast path encodes the rule "if signal_type_hint is present, trust it" as code. This is correct — a producer writing a hint at event time has more context than the slow-reclassifier can recover from content alone. The deterministic path (trust hint) is preferable to re-running inference on content the producer already interpreted.

**Tertiary:** `current_focus.what_not_to_touch` — "New detection or classification rules — improve Orient routing before adding more detection."

The fast path does not add detection rules. It reduces detection: events with hints skip pattern detection entirely. This is complementary to the constraint's intent — it prevents over-detection on events where classification is already known.

### Authorization

Authorized by WOS UoW uow_20260501_8de2bc (mem-event-type-subject-tagging). The UoW is a dispatch record, not a decision record per learnings.md PR #913; this ADR is the decision record. The behavioral change is technically sound; the oracle Round 1 verdict (Alignment: Questioned, not Rejected) confirms the underlying logic is coherent while requiring this log entry as the structural anchor.

### Known Correction Gap

Events classified via the fast path (confidence="high") have no correction path if the producer's hint is wrong. The slow-reclassifier's `total_revised` counter conflates hinted events with pattern-revised events, making true reclassification rates harder to audit. These are accepted trade-offs at current scale given that structured producers (harvesters, scheduled jobs) have reliable self-knowledge of their signal type. If producer accuracy degrades, the correct response is to add a monitoring gap to the daily digest, not to remove the fast path.
