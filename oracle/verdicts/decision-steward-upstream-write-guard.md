# Design Decision: Steward system_prompt Upstream Write Guard

**Date:** 2026-05-01
**Status:** ACCEPTED
**Vision anchor:** vision.yaml core.inviolable_constraints.constraint-3 (Encoded Orientation requires prior logged decision of same class and a traceable vision.yaml anchor)
**Linked commit:** fddeef4f (fix(wos): steward system_prompt hard-gates SiderealPress/Lobster as read-only write target)
**Linked UoW:** uow_20260501_0abdc0

## Decision

Authorize the upstream write guard embedded in `_llm_prescribe` in `src/orchestration/steward.py`:

The `HARD CONSTRAINT` injected into the steward's `system_prompt` instructs the LLM never to generate a prescription that targets `SiderealPress/Lobster` for any write operation — including PR comments, issue comments, PR updates, pushes, or any other mutation. Reading from `SiderealPress/Lobster` as a source of information (e.g. reading an issue for context) remains permitted; all write targets must be `dcetlin/lobster` or another non-upstream repo.

## What the guard does

The guard is a natural-language constraint injected verbatim into the LLM's `system_prompt` at every `_llm_prescribe` call. It applies globally — every UoW prescription, regardless of register or posture, receives this constraint. There is no runtime toggle; the constraint is structural.

## Why it was added

The observed failure mode: without this guard, steward prescription LLM calls occasionally generate instructions that target `SiderealPress/Lobster` for writes (e.g., "open a PR on SiderealPress/Lobster", "post a comment on issue #N in SiderealPress/Lobster"). This happens when UoW context mentions the upstream repo as a reference — the LLM incorrectly treats the referenced repo as a permissible write target.

`SiderealPress/Lobster` is the upstream public mirror. `dcetlin/Lobster` is the owner's private fork where all work lands. Allowing writes to the upstream would violate the ownership boundary — creating noise, unauthorized mutations, and potential confusion between upstream and fork.

## Vision anchor

This is an Encoded Orientation decision: the steward autonomously prevents certain prescription classes from being generated, without Dan's explicit input per invocation. Under `constraint-3`, this requires a prior logged decision of the same class and a traceable vision.yaml anchor.

The structural class is the same as `decision-github-rate-limit-gate.md` (pre-dispatch suppression) and `decision-needs-human-review-escalation.md` (escalation gate): an autonomous behavioral gate applied to system output, backed by a logged decision and anchored to constraint-3.

The vision.yaml anchor is `core.inviolable_constraints.constraint-3`: Encoded Orientation decisions require a prior logged decision of the same class and a traceable vision.yaml anchor. This document constitutes both.

## Constraints

- The guard is `system_prompt`-level — it applies to every `_llm_prescribe` invocation without exception
- Removing or bypassing the guard requires a code change (intentionally not runtime-configurable)
- The guard does not affect reads from `SiderealPress/Lobster` — only writes
- The guard does not validate the LLM's output structurally; it relies on the LLM's instruction-following. For structural validation of prescription write targets, a separate post-prescription check would be needed (out of scope here)
