# Decision: Natural Language Routing for /todo Commands

**Date:** 2026-05-10
**Status:** Accepted
**PR:** #1127 (feat/todo-telegram-handler)
**Anchored to:** `core.inviolable_constraints.constraint-3`, `core.operating_principles.principle-3`

---

## What the NL routing does

The bootup doc for the dispatcher (`sys.dispatcher.bootup.md`) instructs the dispatcher
to recognize natural-language phrases as equivalent to explicit `/todo` subcommands and
route them via `route_todo_command`:

- "add X to my list" / "remind me to X" → construct synthetic `/todo add X`
- "mark X done" / "X is done" → construct synthetic `/todo done X`

The dispatcher extracts the intended item text from the message, constructs a synthetic
`msg["text"]` value (e.g. `/todo add <extracted text>`), and passes it directly to
`route_todo_command`. No subagent dispatch occurs — the operation runs synchronously on
the main thread, the same as an explicit `/todo` subcommand.

---

## Why this is an Encoded Orientation decision

This is a durable behavioral default encoded in the dispatcher's bootup document.
Under `core.inviolable_constraints.constraint-3`, Encoded Orientation decisions require:
(a) a prior logged decision of the same class, and (b) a traceable vision.yaml anchor.

The NL routing satisfies all three conditions for constraint-3:
1. The system acts without Dan's real-time input — the dispatcher routes silently when
   it identifies these patterns.
2. It changes a durable behavioral default in the dispatch path — this is not a one-time
   operational action; it applies on every matching message.
3. The behavioral change is encoded in the dispatcher's context document, not in a
   retrievable per-session prompt.

This document is the logged prior decision for that constraint.

---

## Vision anchor

**Primary:** `core.inviolable_constraints.constraint-3` — "Every system decision traverses
the full OODA loop at the appropriate register. Encoded Orientation decisions require a
prior logged decision of the same class and a traceable vision.yaml anchor."

This document satisfies constraint-3 for the NL routing dispatch default.

**Secondary:** `core.operating_principles.principle-3` — "Determinism over judgment for
conditionals. If-then logic and field checks are code, not LLM instructions. Use LLMs
where genuine interpretation is required."

Natural language intent extraction is judgment, not deterministic logic. The NL routing
path intentionally invokes judgment (LLM pattern recognition) to classify phrases like
"remind me to X" as TODO additions. This is a bounded, scoped application of principle-3's
exception clause: "Use LLMs where genuine interpretation is required." Recognizing intent
in free-text phrases is precisely the case where LLM interpretation is required and
deterministic pattern-matching is insufficient.

The `/todo add|done|snooze` routing itself (for explicit commands) remains deterministic —
regex-matched and code-routed. Only the NL extraction path invokes judgment.

---

## Failure mode: extraction errors produce silently incorrect adds

The structured command path (`route_todo_command`) assumes precise input — the item text
is exactly what the user intended to add. The NL extraction path produces approximate
input: the dispatcher infers intent from free text and constructs a synthetic command.

When the extraction is imprecise, `route_todo_command` receives the extracted text with
no awareness that it came from NL extraction. Examples of extraction failure:

- "remind me to call Sarah tomorrow" → `/todo add call Sarah tomorrow` — "tomorrow" is
  part of the item text, not a scheduling instruction. The item is added with the word
  "tomorrow" in its text, which becomes stale.
- "I need to add this to my list too" → `/todo add this` — the extraction picks up
  "this" rather than a specific item.

There is no error surface in `route_todo_command` when the extraction is wrong — the item
is silently added with extracted text. The user sees a confirmation like 'Added #42: "call
Sarah tomorrow"', which may not match their intent.

### Accepted risk

This failure mode is accepted at the current scale of the feature for two reasons:

1. The NL routing is advisory context in a dispatcher document, not hardwired dispatch
   logic. The dispatcher uses its LLM judgment to decide whether a phrase matches — it
   can decline to route when the intent is ambiguous.
2. The cost of a misadd is low: the user can see the confirmation and reply `/todo done
   <id>` to close the incorrectly-added item. No destructive mutation occurs.

### How to disable

To disable NL routing without removing the explicit `/todo` command routing:

1. Remove the "Natural language triggers" section from the bootup doc
   (`sys.dispatcher.bootup.md`) under `## LOS (Life Operating System) Commands`.
2. The explicit `/todo add|done|snooze` routing is unaffected — it is governed by a
   separate dispatcher routing condition and does not depend on NL extraction.

Alternatively, to scope NL routing to specific phrases only, replace the open-ended
instruction with an explicit pattern list (e.g. exact substring match on
"add to my list" or "remind me to") and a fallback that declines to route on all other
natural-language patterns.

---

## Scope of this decision

This decision authorizes the NL routing behavioral default as documented in the PR #1127
bootup doc change. The scope is limited to:

- Phrases that unambiguously express TODO addition or completion intent
- Synchronous, inline routing (no new subagent dispatch)
- Item text extracted from the matched phrase only (no external data access)

This authorization does not cover:
- NL routing for `/todo snooze` (not included in the PR #1127 bootup doc)
- NL extraction from voice messages (handled separately by the voice routing path)
- Any LLM-driven auto-prioritization or scheduling interpretation of item text
