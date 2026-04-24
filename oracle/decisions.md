# Architecture Decisions

Logged durable design choices that affect observable system behavior.

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
