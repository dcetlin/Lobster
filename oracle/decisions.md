# Architecture Decisions

Logged durable design choices that affect observable system behavior.

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
