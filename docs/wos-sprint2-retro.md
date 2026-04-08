# WOS Sprint 2 — Retrospective & Artifact Index

*April 2026*

---

## What We Built

Sprint 2 targeted two tracks in parallel: finishing the WOS V3 infrastructure sprint (trace substrate, register-aware diagnosis, seam cleanup) and running the first real end-to-end pipeline execution of three Sprint 2 UoWs (S2-A, S2-B, S2-C). Both tracks completed — all Sprint 2 UoWs are merged on dcetlin/Lobster — but the path to completion exposed a cluster of executor dispatch bugs that required a second track of emergency fixes during and immediately after the sprint.

The core story of Sprint 2 is that the executor dispatch infrastructure was broken in multiple ways simultaneously. S2-A and S2-B required 4–5 manual decide-retry resets each, two emergency mid-sprint PRs (#667, #668), and manual ops-agent interventions before they could execute. S2-C benefited from those fixes and ran nearly cleanly. The post-sprint fix campaign (PRs #683–#688) then closed the bugs identified but not resolved during the sprint itself.

---

## Artifacts

| Document | Location | Description |
|----------|----------|-------------|
| Sprint report | [docs/sprint2-uow-pipeline-report.md](sprint2-uow-pipeline-report.md) | Full audit trail for S2-A, S2-B, S2-C UoWs — registry records, steward logs, cycle traces, timeline |
| WOS Sprint 001 spec | [docs/wos-v3-sprint-001.md](wos-v3-sprint-001.md) | The execution guide for the trace substrate sprint (PR A): what to build, pre-sprint state, DB schema |
| Steward-executor spec | [docs/wos-v3-steward-executor-spec.md](wos-v3-steward-executor-spec.md) | Steward/Executor boundary design: state machine, handoff contract, executor-contract.md reference |
| WOS V3 proposal | [docs/wos-v3-proposal.md](wos-v3-proposal.md) | Full V3 design — register model, corrective trace loop, commitment gate, mitochondrial framing |
| Design audit | [docs/wos-design-audit-2026-04-08.md](wos-design-audit-2026-04-08.md) | April 8 post-sprint audit — vision alignment, 8 implementation gaps, 5 new issues filed |
| Oracle decisions | [oracle/decisions.md](../oracle/decisions.md) | All oracle verdicts for PRs reviewed during and before Sprint 2 |
| Oracle learnings | [oracle/learnings.md](../oracle/learnings.md) | Golden patterns extracted from oracle reviews |

---

## PRs Completed

### Core Sprint PRs (V3 infrastructure + seam cleanup)

| PR | Title | Merged | Key oracle finding |
|----|-------|--------|--------------------|
| [#653](https://github.com/dcetlin/Lobster/pull/653) | feat(wos-v3): register-aware diagnosis + corrective trace injection (PR C) | 2026-04-07 | APPROVED. `_count_non_improving_gate_cycles` off-by-one diagnosed in post-merge audit (#687 follow-up). 4 pre-existing `TestBackpressureSkipsRePrescription` failures noted, not introduced. |
| [#658](https://github.com/dcetlin/Lobster/pull/658) | cleanup: fix stale docstring and misleading test name | 2026-04-07 | APPROVED. Sprint-2 cleanup; no oracle issues. |
| [#659](https://github.com/dcetlin/Lobster/pull/659) | cleanup: extract `_dispatch_via_stub` to eliminate duplicate dispatchers | 2026-04-07 | APPROVED. Sprint-2 cleanup; consolidated two identical stub dispatchers. |
| [#660](https://github.com/dcetlin/Lobster/pull/660) | cleanup: route `corrective_traces` reads through Registry method | 2026-04-07 | APPROVED. Registry seam fix — `get_corrective_trace_history()` added. |
| [#662](https://github.com/dcetlin/Lobster/pull/662) | fix(wos): eliminate polling hop and hard-gate legacy done fallback | 2026-04-07 | APPROVED. Inline executor wiring seam; `result.json` now required (legacy `success` field removed). |

### Fix Campaign PRs (post-sprint discoveries)

| PR | Title | Merged | What broke / what was fixed |
|----|-------|--------|------------------------------|
| [#683](https://github.com/dcetlin/Lobster/pull/683) | fix(wos): protect executing UoWs from caretaker teardown; fix wos_execute routing | 2026-04-08 | Caretaker was expiring active/executing UoWs when source issue closed. Added in-flight detection; fixed wos_execute routing. |
| [#684](https://github.com/dcetlin/Lobster/pull/684) | fix: expand decide-retry to allow recovery from ready-for-steward status | 2026-04-08 | `decide-retry` rejected `ready-for-steward` and `blocked` inputs. Expanded accepted states. |
| [#685](https://github.com/dcetlin/Lobster/pull/685) | fix: gate `execution_complete` on `write_result`, not inbox dispatch (#669) | 2026-04-08 | Executor marked `execution_complete` at dispatch time (when inbox message queued), not when subagent returned work — causing false closures. Fixed to write result.json before transitioning. |
| [#686](https://github.com/dcetlin/Lobster/pull/686) | fix(wos): remove empty legacy registry DB and add startup warning | 2026-04-08 | `data/wos-registry.db` was empty alongside active `orchestration/registry.db`. Removed legacy path; added startup warning if both exist. |
| [#687](https://github.com/dcetlin/Lobster/pull/687) | fix(wos): lifetime_cycles — cumulative hard-cap counter that survives decide-retry resets | 2026-04-08 | `steward_cycles` was reset to 0 on each decide-retry. Added `lifetime_cycles` field that accumulates across all rounds. Advisory: early-warning threshold still uses per-attempt `steward_cycles` — needs follow-up. |
| [#688](https://github.com/dcetlin/Lobster/pull/688) | fix(wos): add fallback WHERE guard to steward done-transition (issue #671) | 2026-04-08 | Steward done-transition lacked a WHERE guard against startup_sweep race condition. Added guard. |

---

## Oracle Learnings

### Golden patterns added during Sprint 2

From `oracle/learnings.md`:

**State Machines (PR #607):** Comment/code mismatch at a state-machine transition causes silent state divergence. When a code comment says "transition to X" but the call transitions to Y, the UoW ends in the wrong state — which may or may not be a pickup state for the next heartbeat. State and comment must be synchronized; any mismatch is a reliability liability, not a documentation issue.

**Contract & Interface Design (PR #607):** A recovery gate that tolerates an open executor contract gap inverts principle-1 ("proactive resilience over reactive recovery"). The correct fix layer is the producer (close the contract at executor exit), not the consumer (add a steward-side wait). Once a steward gate gracefully tolerates a gap, pressure to close the upstream contract decreases.

**Classification & Detection (PR #602):** Boolean frozenset intersection is over-eager when technical vocabulary overlaps with domain vocabulary — a single shared term fires the gate at full confidence. Weighted scoring or frequency thresholds are more appropriate than presence-only detection. Classification results should be typed frozen dataclasses with observability fields (`gate_fired`, `evidence`, `confidence`) rather than bare return values.

### Key NEEDS_CHANGES verdicts requiring revision

**PR #607 (corrective trace temporal gate) — NEEDS_CHANGES → APPROVED:**
Three items required fixing before merge: (1) the skip-path state transition comment said "ready-for-steward" but the call was correctly transitioning to `ready-for-steward` — confirmed not a bug, documented; (2) `wait_entry` log dict was missing a `timestamp` field; (3) the contract violation path lacked a `notify_dan` call. All three were fixed in commit 7fa2c63. Residual: `violation_entry` dict lacks a timestamp while sibling `wait_entry` has one — minor schema inconsistency.

**PR #602 (register classification) — fix applied before merge:**
`"register"` was in `_PHILOSOPHICAL_TERMS`, causing Gate 3 to fire on V3 meta-issues that contain the word "register" in technical context. Single-line removal applied before merge.

**PR #673 (Sprint 2 UoW execution — prescription format) — NEEDS_CHANGES → APPROVED:**
Oracle R1 found that `partition(":")` silently truncates field values that contain colons (only the part before the first colon is retained). Fixed in commit 726f1b7 to `split(":", 1)`. Test `test_parse_workflow_artifact_value_with_colon` added.

### Design audit "Questioned" verdicts

The April 8 design audit (Issues #678–#682) raised five design gaps under the adversarial prior that the system is optimizing for executor throughput before installing the governing structures the mitochondrial model requires:

- **Issue #678** (open): Commitment gate has no cleanup arc — hard cap is a pause, not a gate. When a UoW hits hard cap, nothing is cleaned up. The mitochondrial model requires irreversible commitment with resource recovery.
- **Issue #679** (open): `steward_cycles` resets to 0 on decide-retry — no cumulative lifetime protection. S2-A ran ~20 diagnosis cycles but shows `steward_cycles = 1` in the registry.
- **Issue #680** (open): Corrective trace absence is logged but not blocking — removes mandatory temporal spacing. The cristae-junction analog is present as observability but not as mandatory delay structure.
- **Issue #681** (open): Asymmetric governor missing — no alert on backlog starvation (zero queue for extended period) or backlog toxicity (queue growing beyond executor capacity).
- **Issue #682** (closed — fixed by PR #686): Registry path inconsistency.

The audit verdict was not "this work is wrong" but "this work may be advancing the wrong dimension" — execution throughput improvements on a pipeline that still lacks the governing structures that would make execution self-regulating.

---

## Open Issues Spilling Forward

| Issue | Title | Status | Priority signal |
|-------|-------|--------|----------------|
| [#664](https://github.com/dcetlin/Lobster/issues/664) | Eliminate executor polling hop via MCP inbox dispatch (`_dispatch_via_inbox` at executor.py:1138) | Closed (addressed in PR #662) | — |
| [#678](https://github.com/dcetlin/Lobster/issues/678) | Hard cap surfaces UoW but does not trigger cleanup arc (commitment is not irreversible) | Open | High — the hard cap is not a commitment gate without this |
| [#679](https://github.com/dcetlin/Lobster/issues/679) | `steward_cycles` resets to 0 on decide-retry — hard cap provides no cumulative protection | Open | High — enables indefinite retry without lifetime tracking |
| [#680](https://github.com/dcetlin/Lobster/issues/680) | Corrective trace absence not enforced as blocking gate before re-prescription | Open | Medium — trace loop is observed but not closed |
| [#681](https://github.com/dcetlin/Lobster/issues/681) | Asymmetric governor missing — no alert on backlog starvation or toxicity | Open | Medium — both failure directions are invisible |
| [#682](https://github.com/dcetlin/Lobster/issues/682) | Registry path inconsistency — empty legacy DB alongside active registry | Closed (PR #686) | — |
| Advisory | Early-warning threshold uses per-attempt `steward_cycles` not `lifetime_cycles` | Not yet filed | Low — follow-up from PR #687 oracle |
| Pre-existing | `TestBackpressureSkipsRePrescription`: 4 test failures on main | Open | Low — pre-existing, not introduced by Sprint 2 |
| Pre-existing | github-issue-cultivator: task file missing, auto-disabled | Open | Low — cultivator not proposing new work |

---

## What We Learned (Structural)

The most important structural learning from Sprint 2 is that the executor dispatch layer contained multiple co-occurring bugs that were individually invisible but collectively catastrophic. The staleness gate bug (PR #667 — fixed during sprint), the startup-sweep age anchor bug (PR #668 — fixed during sprint), and the false-complete bug (PR #685 — fixed post-sprint) each would have been manageable in isolation. Together, they meant that freshly-prescribed UoWs were never stale enough to claim, previously-orphaned UoWs were continuously reclassified, and UoWs that were claimed marked completion at dispatch rather than at work-done. The pipeline was structured correctly at the boundary level — the Steward/Executor separation, the atomic claim, the TTL recovery — but the dispatch logic itself was wrong in three distinct ways. The takeaway is that the executor-heartbeat's claim and dispatch logic requires integration testing against the full state machine, not just unit tests of individual functions. S2-C's clean run (one diagnosis, one prescription, one execution, seven minutes total) demonstrates that the pipeline works when the dispatch layer is correct.

The second structural learning is about the relationship between observability and correctness. Sprint 2 shipped observability PRs (cycle trace logging in S2-A, prescription format improvements in S2-B, pipeline metrics in S2-C) over a pipeline that was failing for structural reasons visible in existing logs without any of those additions. The design audit's adversarial prior — "instrumenting a pipeline that requires rescue operations does not make it self-governing" — partially survived: the metrics layer in PR #674 is correct but the denominator is not yet trustworthy because the audit events it aggregates include race conditions, orphan misclassifications, and false-complete records from the same sprint. This is not a reason to avoid observability work — instrumentation is necessary — but it is a reminder that metrics become meaningful only after the underlying state machine is correct. The correct sequencing is: fix dispatch → collect clean data → measure convergence.

The third structural learning is about the commitment gate. The mitochondrial model that WOS is grounded in treats the hard cap not as a retry mechanism but as an irreversible commitment — once threshold is crossed, the system recovers resources and commits to escalation. What Sprint 2 revealed is that the current hard cap is a pause: it surfaces to Dan, but nothing is cleaned up, the UoW can be manually reset indefinitely, and the `steward_cycles` field resets to zero on each decide-retry (S2-A showed ~20 actual diagnosis cycles but a final registry value of 1). The post-sprint additions of `lifetime_cycles` (PR #687) and the caretaker in-flight protection (PR #683) move in the right direction, but the cleanup arc (artifacts archived, failure trace written to garden, source issue updated) and the lifetime hard cap (not resettable without explicit override) remain unbuilt. Until the commitment gate is actually irreversible, the system will require human rescue on every UoW that hits a structural dispatch failure — because the mechanisms that should recover resources and stop the retry loop do not yet exist.
