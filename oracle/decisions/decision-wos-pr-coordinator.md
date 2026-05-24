# Decision: WOS PR Results Route to wos-pr-coordinator Agent

**Date:** 2026-05-19
**Status:** Decided — routing default encoded in dispatcher_handlers.py
**Related PR:** #1223 (feat/wos: WOS PR pipeline coordinator)
**WOS Reference:** uow_20260516_71b777

## Decision

WOS PR results (subagent results where `task_id` starts with `"wos-"` and the
result text contains a GitHub PR URL) are routed to the `wos-pr-coordinator`
agent instead of being handed directly to the existing oracle review agent path.

This is a durable behavioral default change, not a one-time operational action.

## Behavioral Change Named

Before this PR, every completed subagent result containing a GitHub PR URL
entered the dispatcher's ENGINEER → REVIEWER routing block and spawned an oracle
review agent directly. The oracle agent reported back to the dispatcher; the
dispatcher then spawned a fix agent or merge agent on the next round-trip. Each
PR required 3–5 dispatcher round-trips to complete (open PR → oracle → fix →
re-oracle → merge).

After this PR, WOS-originated PRs (`task_id.startswith("wos-")`) are caught by
`route_wos_pr_result()` in `src/orchestration/dispatcher_handlers.py` before
the existing review agent block runs. A `wos-pr-coordinator` agent is spawned
instead. The coordinator internalizes the full oracle→fix→merge loop, returning
exactly one `write_result` call to the dispatcher when the PR is either merged
or escalated. Non-WOS PRs receive `action="fallthrough"` and continue through
the existing review agent path unchanged.

## Why This Is a Behavioral Default Change

The system now routes WOS PR results without the dispatcher deciding per-message
whether to use the coordinator. The routing key (`task_id.startswith("wos-")`)
is deterministic and encodes the decision at the code level. Every WOS-originated
PR result will spawn the coordinator automatically from this point forward. This
satisfies the conditions under `core.inviolable_constraints.constraint-3`: (a)
the system acts without Dan's real-time input per PR, (b) it changes a durable
default in the ENGINEER → REVIEWER routing path, and (c) the behavioral change
is encoded in `route_wos_pr_result()`, not in a retrievable prompt.

## Vision Anchor

**Primary:** `core.operating_principles.principle-3` — "Determinism over
judgment for conditionals. If-then logic and field checks are code, not LLM
instructions. Use LLMs where genuine interpretation is required."

The routing predicate (`task_id.startswith("wos-")`) is a deterministic field
check. It requires no LLM interpretation — a WOS-originated PR is unambiguously
identified by its task_id prefix. Encoding this in `route_wos_pr_result()` is
the correct structural expression of the decision.

**Secondary:** `core.operating_principles.principle-4` — "Integration rate
before new feature rate. Wire what exists before building more. The missing
arrows between existing systems are the velocity multiplier."

The coordinator agent and the WOS pipeline both existed as concepts before this
PR. The missing arrow was between the dispatcher's ENGINEER → REVIEWER block and
a WOS-aware coordinator that could internalize the pipeline stages. This PR
wires that arrow without adding new capabilities — it composes existing
mechanisms (oracle agent, fix agent, merge agent) behind a single coordinator
interface.

## Routing Default as Durable

The `route_wos_pr_result()` function is wired into the dispatcher's
ENGINEER → REVIEWER routing section as a guard before the existing review agent
call. This default is not experimental or temporary. It is the correct routing
strategy for WOS-originated PRs at current and expected scale.

Non-WOS PRs are structurally unaffected — the existing oracle review agent path
is reachable only after `route_wos_pr_result()` returns `action="fallthrough"`.
This means the PR introduces no regression risk for non-WOS code paths.

## What This Decision Does NOT Do

- It does not modify the oracle agent itself.
- It does not change the oracle→fix→merge logic — those stages remain operative
  inside the coordinator.
- It does not establish a precedent that all PR routing should be collapsed into
  a coordinator. The coordinator is WOS-specific because WOS generates
  high-volume PR bursts where dispatcher round-trip reduction has material impact
  on context compaction. Non-WOS PRs do not exhibit the same burst pattern.

## Authorization

Authorized by WOS UoW uow_20260516_71b777. The oracle PR #1223 verdict (Round 1)
identified the missing decision record as a blocking gap. This document is the
required logged prior for the behavioral default change under constraint-3.
