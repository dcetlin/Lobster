# Agent Definition Governance

*WOS-UoW: uow_20260502_b0a89c*

This document defines the naming convention for agent definitions in `.claude/agents/`, explains the existing split between `lobster-*` and un-prefixed agents, and gives criteria for new agent authors.

---

## 1. Naming Convention

Agent names signal the **character** of the agent, not just its subject matter. The convention uses a single structural rule:

> **Use the `lobster-*` prefix for agents that embody a persistent system role. Omit the prefix for agents that execute a specific workflow or perform session-lifecycle plumbing.**

This gives three categories:

### Category A — Lobster role agents (`lobster-*`)

These agents embody a standing system function with a defined epistemic posture and a scope broader than any single task. The `lobster-*` prefix signals: "this is a named role within the Lobster system, callable across many contexts by the dispatcher or operator."

Defining characteristics:
- Has a named identity beyond its task ("auditor", "oracle", "operations specialist")
- Can be invoked in response to many different inputs, not just one trigger type
- Retains a consistent posture or stance across invocations (adversarial, investigative, hygiene-focused)
- Would be described as a *role* a person could hold, not a *pipeline* a person would run

Examples: lobster-auditor (system investigator), lobster-oracle (adversarial PR reviewer), lobster-ops (operations specialist).

### Category B — Workflow / task agents (no prefix)

These agents execute one named pipeline or work mode. They are named by what they *do*, not by a role they *inhabit*. The absence of `lobster-*` signals: "this agent is a specialized executor for a specific, bounded workflow."

Defining characteristics:
- Named for its workflow or process, not for a standing system role
- Has a specific entry point and a defined output artifact (PR, brain dump file, review report)
- Would be described as a *pipeline* or *workflow*, not a *role*

Examples: functional-engineer (issue→PR implementation pipeline), brain-dumps (voice note processing pipeline), review (PR or design review workflow).

### Category C — Session-lifecycle agents (no prefix)

These agents handle internal session and memory plumbing. They are triggered by session events (compaction, message count thresholds, scheduled timers) rather than by user requests or dispatcher dispatch. They are named descriptively for the exact plumbing operation they perform.

Defining characteristics:
- Triggered by session infrastructure events, not by user messages or dispatcher routing
- Named for the specific lifecycle operation: append, polish, consolidate, catch up
- Output is consumed by the session/memory system, not directly by the user

Examples: compact-catchup (post-compaction context recovery), session-note-appender (incremental session logging), session-note-polish (pre-compaction cleanup), nightly-consolidation (nightly memory synthesis).

---

## 2. Defining Criteria

Use this checklist when naming a new agent:

```
1. Does this agent embody a persistent, named system role with a consistent
   posture across many contexts?
   YES → use lobster-* prefix (Category A)

2. Does this agent execute a specific, bounded workflow or pipeline?
   YES → no prefix, name by the workflow (Category B)

3. Is this agent triggered by session/memory lifecycle events (compaction,
   cron timers, message count thresholds)?
   YES → no prefix, name by the plumbing operation (Category C)
```

The `lobster-*` prefix is **not** a Lobster membership badge. It is a structural signal — "standing system role" — and applying it to a workflow or lifecycle agent would misrepresent the agent's character. The absence of the prefix on non-role agents is a correct and intentional design choice, not an oversight.

**Key questions to distinguish A from B:**
- Would a human team use a job title to describe this agent? (lobster-oracle = "our adversarial reviewer") → Category A
- Would a human team use a verb phrase? ("the thing that runs when we get a GitHub issue") → Category B

---

## 3. Current Agent Inventory

| Agent | Category | Naming justification |
|---|---|---|
| `lobster-auditor` | A — Lobster role | Embodies the "system investigator" role; callable for any infrastructure health concern |
| `lobster-generalist` | A — Lobster role | Standing catch-all role for delegated background tasks without a more specialized agent |
| `lobster-hygiene` | A — Lobster role | Embodies a quarterly artifact review posture; a standing system function, not a one-time pipeline |
| `lobster-meta` | A — Lobster role | Embodies a nightly drift-detection posture with specific epistemic rules; a persistent system role |
| `lobster-ops` | A — Lobster role | Embodies the operations specialist role; callable for any troubleshooting or architecture question |
| `lobster-oracle` | A — Lobster role | Embodies the adversarial PR reviewer role with a defined epistemic prior; callable across all PR reviews |
| `brain-dumps` | B — Workflow | Executes the voice-note brain dump processing pipeline; named by the artifact it produces |
| `functional-engineer` | B — Workflow | Executes the GitHub issue → implementation → PR workflow; named by the engineering role it enacts |
| `review` | B — Workflow | Executes the PR or design review workflow; named by the action it performs |
| `compact-catchup` | C — Session lifecycle | Session plumbing: recovers dispatcher context after context compaction |
| `nightly-consolidation` | C — Session lifecycle | Session/memory plumbing: synthesizes 24h of memory events at 3 AM via cron trigger |
| `session-note-appender` | C — Session lifecycle | Session plumbing: appends incremental snapshot to session file every 20 messages |
| `session-note-polish` | C — Session lifecycle | Session plumbing: reorganizes session snapshots into a clean handoff before compaction |

---

## 4. Answers to the Three Governance Questions

**Q1: Should all Lobster system agents follow the `lobster-*` prefix?**
No. Only Category A (role agents) should use the prefix. Applying `lobster-*` to workflow or lifecycle agents would misrepresent their character — they are pipelines and plumbing, not standing roles.

**Q2: Are non-prefixed agents legitimately exempt or should they be renamed?**
Legitimately exempt by design. The prefix is a structural signal about the *kind* of agent, not a membership badge. Category B and C agents are correctly un-prefixed because they do not embody a persistent system role.

**Q3: What makes an agent Lobster-official vs. task-specific?**
A Lobster-official agent (Category A) embodies a persistent system role — it has an identity, a posture, and a scope that spans many contexts. A task-specific agent (Category B or C) executes one defined pipeline or lifecycle operation. The distinguishing test: would a human team describe it as a role someone *holds* (lobster-oracle = adversarial reviewer) or a process someone *runs* (functional-engineer = the issue-to-PR pipeline)?

---

## 5. Governance Notes

This convention applies exclusively to agent definitions in `.claude/agents/`. It does not govern:
- WOS subagent `task_id` slugs (e.g., `agent-naming-convention-doc`) — these are instance identifiers, not agent names
- Script filenames in `scheduled-tasks/` — these follow the `kebab-case` convention independently
- Directory names in `.claude/` or elsewhere in the repository
- Subagent type identifiers used as `subagent_type:` parameters in Task dispatches

Agent definitions in `~/lobster-user-config/agents/subagents/` (user-defined agents not committed to the repo) should follow the same convention for consistency, but are not subject to oracle review.
