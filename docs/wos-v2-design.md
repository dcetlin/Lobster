# Work Orchestration System v2 — Design Document

*Status: Active — 2026-03-30*

---

## Overview

The Work Orchestration System (WOS) is the pipeline that moves units of work from "filed as a GitHub issue" to "done and closed" — making every stage visible, auditable, and self-correcting. The problem it solves: Lobster generates observations and files them as issues, then the pipeline stalls at "filed." Issues accumulate; the archive grows; the activity metric (issues added) diverges from the outcome metric (work closed). WOS installs a **meter** — a structured path from observation to unit-of-work-in-motion to confirmed closure — and keeps that path visible at every stage.

The v2 model replaces the Phase 1 dispatcher-centric model with a two-actor **Steward/Executor** loop: a Steward that diagnoses and prescribes, and an Executor that carries out the prescription. The Steward owns each UoW's full lifecycle; the Executor does the work. Nothing exits via side-door.

---

## Vocabulary & Primitives

### Core Terms

**Unit of Work (UoW)** — The atomic unit of tracked, auditable work in the pipeline. Every piece of work that enters the execution substrate is a UoW. A UoW has a state, an audit trail, and a closure condition.

**UoWRegistry** — The execution substrate. A structured store (SQLite from Phase 1) holding one record per UoW. It is the single source of truth for what is running; no other lookup is ever needed to answer "what's active?"

**UoW Registrar** — The governance agent that watches the GitHub issue backlog and creates new UoWs for the orchestration engine. It performs four functions: (1) reads GitHub issues, (2) identifies qualifying ones (ready-to-execute, gate criteria met), (3) creates UoW entries in the UoWRegistry, (4) manages lifecycle (expired proposals, stale-active detection). "Sweeper" was the prior name; the actual job is registration and lifecycle management, hence the rename.

### Pre-Registry Vocabulary

**Seed** — Unclassified potential. An idea, observation, or open question that may or may not become executable work. Not yet in the UoWRegistry. Seeds can originate from philosophy sessions, Telegram observations, direct feature requests, or any source.

**Pearl** — A philosophy session output that is a distillation rather than an action item. Pearls route to the write-path (frontier docs, bootup candidates) via the Cultivator. They do not enter the UoWRegistry. Pearl outputs circulate via re-encounter rather than via a separate execution pipeline.

**Germination** — The classification event at which a seed's output type is resolved (pearl or executable work). For seeds that resolve to executable work, germination produces a GitHub issue.

**Sprout moment** — When the UoW Registrar identifies a qualifying GitHub issue and creates a UoW entry in the UoWRegistry. The issue enters the UoW execution pipeline at this point.

**Bootup candidate** — A specific pearl type: a proposed addition to Lobster's bootup or context files, produced by a philosophy session and routed by the Cultivator to the write-path as a GitHub issue (label: `bootup-candidate`). Bootup candidates do not automatically enter the UoWRegistry. They are design-gate UoWs: the gate is Dan's review. Once Dan approves (passes the gate), the issue qualifies for the UoW Registrar to pick up and register as an executable UoW.

### The Cultivator

The Cultivator is the philosophy pipeline's classification agent. It runs after a philosophy session and performs three operations:

1. **Distinguish** — classifies session outputs as pearls or seeds.
2. **Route pearls** — sends pearls to the appropriate write-path (frontier doc, bootup candidate). No UoWRegistry entry is created.
3. **File seeds** — files seeds as GitHub issues. Phase 1: with human review. Phase 2: programmatically.

The Cultivator's internal operations are classification (pearl or seed?) and triage (which path does this seed take?). "Classifier" and "triage agent" are names for the same role at different abstraction levels; the Cultivator is the unified name for the philosophy pipeline's sorting function.

The Cultivator's trigger is an open implementation question: on-demand (after each session), scheduled, or event-triggered. This is a Phase 2 design decision.

### GitHub Issues as the Universal Pre-Registry Substrate

GitHub issues are the pre-Registry substrate for all executable work. Every seed — whether originating from a philosophy session, a Telegram observation, or a direct feature request — eventually becomes a GitHub issue before entering the UoWRegistry.

For feature requests: the GitHub issue is the germinated seed. For specs: the spec issue is the seed; subissues are the UoW decomposition. The subissue-to-UoW mapping is implied by the parent/children fields in the Registry and will be specified in a Phase 2 design note.

### Full Pipeline

```
Philosophy session
  -> Cultivator                   [ASPIRATIONAL — not yet built]
    -> pearls -> write-path (frontier docs, bootup candidates)
    -> seeds -> GitHub issues
               -> UoW Registrar   [Phase 1 — operational]
                 -> UoWRegistry   [Phase 1 — operational]
                   -> Steward/Executor loop  [Phase 2 — not yet built]
                     -> artifacts / done
```

This pipeline applies beyond philosophy sessions: any source of seeds (Telegram observations, nightly health scans, direct requests) flows through the same funnel — GitHub issue as the universal entry point, UoW Registrar as the gate into the execution substrate.

**Operational status note:** The Cultivator is not yet built. Until it is, philosophy session outputs (bootup candidates, seeds) reach GitHub via manual filing — the Cultivator stage is bypassed entirely. The pipeline diagram above is the design target; the `ASPIRATIONAL` label marks stages that are not yet operational. Do not read the diagram as a description of current system behavior.

---

## State Machine

### States

| State | Semantics |
|-------|-----------|
| `proposed` | UoW Registrar created this record; awaiting Dan's confirmation. |
| `pending` | Dan confirmed via `/confirm`; queued for an agent to claim. |
| `ready-for-steward` | Active in the Steward/Executor loop; Steward's turn to diagnose. |
| `ready-for-executor` | Steward has prescribed a workflow; Executor's turn to run it. |
| `active` | Executor is currently running the prescribed workflow. |
| `blocked` | Execution paused; awaiting an external condition or human decision. |
| `done` | Steward has declared closure; output artifact written. Terminal state — no re-entry. If a closed UoW requires rework, a new UoW is filed referencing the prior UoW's ID. |
| `failed` | Execution failed; retry hook may re-queue. |
| `expired` | Proposed record older than 14 days with no action; excluded from active queries. |

### Transitions

| From | To | Actor | Trigger |
|------|-----|-------|---------|
| — | `proposed` | UoW Registrar | Nightly scan identifies a ready issue |
| `proposed` | `pending` | Dan | `/confirm <uow-id>` command |
| `proposed` | `expired` | UoW Registrar | Record age ≥ 14 days on nightly run |
| `pending` | `ready-for-steward` | UoW Registrar / Trigger evaluator | UoW is confirmed and trigger fires (default: immediate) |
| `ready-for-steward` | `ready-for-executor` | Steward | Diagnosis complete; workflow prescribed |
| `ready-for-steward` | `blocked` | Steward | Observation surfaced to Dan; awaiting orientation or decision |
| `ready-for-steward` | `done` | Steward | Convergence conditions met; closure declared |
| `blocked` | `ready-for-steward` | Dan | Dan provides orientation or confirms decision |
| `ready-for-executor` | `active` | Executor | Executor claims the UoW |
| `active` | `ready-for-steward` | Executor | Execution complete; results written |
| `active` | `failed` | Executor | Execution failed |
| `failed` | `ready-for-steward` | Hook (retry) | Retry hook re-queues after backoff |

Every transition is written to the audit log before it is considered to have happened (**Principle 1: No silent transitions**).

**Rework after closure:** `done` has no re-entry path. If a closed UoW's output proves wrong or requires follow-on work, a new UoW is filed that references the original UoW's ID in its description. This preserves the audit integrity of the closed record while creating a fresh execution chain.

---

## Composable Primitives

The Steward selects from a library of named workflow primitives. Each primitive is a well-specified unit — not a vague instruction. The Executor runs whichever primitive the Steward prescribes.

### Simple Primitives

| Primitive | Description | Output |
|-----------|-------------|--------|
| **Single assessment** | One subagent, one focused evaluation | Structured assessment artifact |
| **Investigation** | Exploratory pass — gather evidence, surface unknowns | Findings document |
| **Design review** | Structured critique of a proposed design or spec | Design review artifact |
| **Synthesis pass** | Takes multiple prior outputs, produces a unified view | Synthesis document |
| **Execution pass** | Runs a well-scoped piece of work confirmed ready to execute | Code, config, or operational output |

### Chain Primitives

| Primitive | Description | Output |
|-----------|-------------|--------|
| **Diverge → converge (1×)** | One divergent pass (alternatives/perspectives) + one convergence pass (synthesize) | Synthesized artifact |
| **Diverge → converge (2×)** | Two divergent passes before convergence — used when the problem space is large or stakes are high | Synthesized artifact |
| **Multi-perspective fan-out** | Spawn N subagents with distinct postures; convergence step synthesizes all readings | Synthesized artifact |
| **Spec breakdown** | Decomposes a design spec into executable sub-UoWs, each entering the queue | N new UoWs in UoWRegistry |

### Selection Rule

**Simple chains** apply when: the unit is well-specified, scope is narrow, or prior work has already converged orientation.

**Complex chains** apply when: the problem is novel, stakes are high, or orientation is contested.

The Steward must be able to cite a diagnosis reason for choosing complexity. A complex chain without a logged rationale is a prescription error.

---

## Core Processes

### UoW Registrar

Runs nightly at 3am. Scans the GitHub issue backlog for issues meeting defined conditions (ready-to-execute label, high-priority stall, stale with no activity). Creates `proposed` UoW records in the UoWRegistry via `registry_cli.py`. Checks stale-active records and runs `expire-proposals` on each pass. In Phase 1, all proposals require Dan's `/confirm` before advancing. In Phase 2+, the UoW Registrar writes labels autonomously after an explicit autonomy gate crossing.

The UoW Registrar is the bridge between the pre-Registry layer (GitHub issues as seed substrate) and the execution substrate (UoWRegistry). It performs four functions: (1) reads GitHub issues, (2) identifies qualifying ones (gate criteria met), (3) creates UoW entries in the UoWRegistry, (4) manages lifecycle (expired proposals, stale-active detection).

### Steward Heartbeat

Runs on a cron heartbeat (initially ~3 minutes). Queries for UoWs in `ready-for-steward` state. For each, the Steward:

1. **Diagnoses** — reads the UoW trail (original intent, prior prescriptions, execution logs, current UoWRegistry state, Vision Object context). Writes the diagnosis to the audit trail before prescribing.
2. **Prescribes** — selects a workflow primitive with a written rationale. Writes the prescription (named workflow + artifact path) to the audit trail. Transitions UoW to `ready-for-executor`.
3. **Evaluates** (on re-entry after execution) — reads execution results, re-diagnoses fresh, decides: loop again or declare closure.
4. **Closes** — writes a closing diagnosis when convergence conditions are met. Transitions to `done`.

The Steward surfaces to Dan under three conditions: (1) something is severely wrong and outside confident operating range; (2) Dan's perspective would materially change the prescription; (3) the Steward has prescribed the same primitive twice with no new input in the audit trail (convergence-velocity proxy for orientation distortion — the Steward cannot reliably detect its own distortion from inside it, so this is measured externally).

**Prescribed skills**: Steward diagnosis MAY include a `prescribed_skills` field — a list of skill IDs to be loaded by the executor at task start. This keeps methodology context out of the always-loaded context and activates it situationally. Examples:
- A bug-fix UoW → prescribe `systematic-debugging` (4-phase root-cause process)
- Any PR UoW → prescribe `verification-before-completion` (prove it works before marking done)
- A complex multi-agent UoW → prescribe `subagent-driven-development` (spec + quality review)

Skill content from frameworks like [Superpowers](https://github.com/Anysphere/superpowers) (118k stars, actively maintained) can be adapted into Lobster's skill library (`~/lobster-user-config/skills/`) and prescribed by the Steward. This keeps the methodology overhead zero for simple UoWs and available for complex ones.

The Steward's diagnostic function has a developmental dimension distinct from the UoW's internal requirements. The question is not only "what does this UoW need to move forward?" but "what does this UoW need relative to Dan's current orientation?" A UoW that is technically ready-for-executor may require a different prescription if Dan is in an exploratory register than if he is in an executive one. The Steward holds both dimensions simultaneously: the UoW's internal state and Dan's current developmental and attentional position. Diagnosis that ignores the second dimension produces technically correct prescriptions that land in the wrong register — which is a coupling failure, not a content failure.

### Executor

Picks up UoWs in `ready-for-executor` state. Carries out the prescribed workflow as specified in the workflow artifact. Writes execution results to an output artifact. Transitions the UoW back to `ready-for-steward` with the execution log. The Executor does not diagnose or decide — it executes and reports.

The Steward/Executor loop continues until the Steward declares convergence:

```
Steward → Executor → Steward → Executor → ... → Steward declares done
```

### Observation Loop

META monitors the UoWRegistry and audit log for degradation signals: dark pipeline (no audit entries for >3 days), orphaned active records, ready-queue growth without drain, stale count accumulation, issue-open rate exceeding close rate for >2 consecutive weeks. When a signal fires, META surfaces it to Dan with evidence, not guesswork.

### Dan Interrupt

Dan can plug into the Steward's loop at three defined points:

- **Observation surfacing**: Steward sends Dan a diagnosis when one of the three surface conditions is met. Context is organized for minimum cognitive friction.
- **Orientation input**: Dan injects nuance or alternative readings; Steward re-diagnoses before prescribing. Both the correction and re-diagnosis are written to the audit trail.
- **Human gate (`/decide`)**: Explicit confirmation required when the decision is substantial, fundamental, load-bearing, and sufficiently complex — all four. Steward presents synthesized orientation; Dan's answer becomes the prescription constraint. (Note: `/confirm` is the UoW Registrar proposal gate only; mid-execution human-gate responses use `/decide <uow-id> approve|reject|defer`.)

---

## Worked Example: Seed to Complete

### Simple Path

```
Issue #142: "Add per-job timeout support to scheduler"
  -> UoW Registrar creates UoW (proposed)
  -> Dan: /confirm uow_abc123 -> status: pending -> ready-for-steward

Steward cycle 1:
  Diagnosis: "Scope is clear, implementation is well-defined, no design ambiguity"
  Prescription: execution-pass workflow
  -> ready-for-executor

Executor: implements per-job timeout in task-runner.py, writes output artifact
  -> ready-for-steward

Steward cycle 2 (re-entry):
  Diagnosis: "Implementation complete, tests pass, output matches intent"
  Convergence: original intent satisfied, output artifact exists
  -> done
```

### Complex Path (Design-Heavy)

```
Issue #228: "UoW Steward -- per-issue diagnostic orchestrator"
  -> UoW Registrar creates UoW (proposed, type: design-seed)
  -> Dan: /confirm -> ready-for-steward

Steward cycle 1:
  Diagnosis: "Novel architecture question, high stakes, orientation not settled"
  Prescription: investigation workflow (gather evidence, surface unknowns)
  -> ready-for-executor

Executor: investigation pass -> findings doc
  -> ready-for-steward

Steward cycle 2:
  Diagnosis: "Findings reveal two competing models (deterministic script vs. LLM instructions)"
  Prescription: diverge -> converge (1x) -- generate both models concretely, then synthesize
  -> ready-for-executor

Executor: fan-out to two subagents (one per model), synthesis pass
  -> ready-for-steward

Steward cycle 3:
  Diagnosis: "Synthesis complete, design decision can be logged"
  Prescription: spec-breakdown -- decompose into N implementation UoWs
  -> ready-for-executor

Executor: spec breakdown -> N new UoWs enter queue
  -> ready-for-steward

Steward cycle 4:
  Diagnosis: "All spawned UoWs entered queue, this unit's work is done at this level"
  Convergence: spawned UoWs entered queue, synthesis complete
  -> done
```

### Dan-Intercepted Path

```
UoW: "Migrate registry schema to add steward_cycles table"
  -> ready-for-steward

Steward cycle 1:
  Diagnosis: "Schema migration touches shared infrastructure; Steward's biases feel strong
              toward minimal change but Dan's intuition on schema evolution may differ"
  Surface condition 2 (Dan's perspective would materially change the prescription)
  -> Surfaces to Dan: "Here's what the migration involves. My read: minimal change is right.
    Is there context I'm missing?"

Dan responds: "Add a JSONB audit_meta column while you're in there -- we'll need it for Phase 3"
  Orientation correction logged to audit trail
  -> ready-for-steward (re-diagnosis)

Steward cycle 1 (re-diagnosis):
  Diagnosis: "Migration scope updated per Dan's orientation: add audit_meta column"
  Prescription: execution-pass (schema migration + audit_meta column)
  -> ready-for-executor

Executor: runs migration, adds column, verifies schema
  -> ready-for-steward

Steward cycle 2:
  Diagnosis: "Migration complete, schema matches intent including Dan's correction"
  Convergence: original intent satisfied
  -> done
```

---

## Phase Roadmap (v2)

### Phase 1: UoWRegistry + UoW Registrar [current]

**What exists:** SQLite UoWRegistry at `~/lobster-workspace/orchestration/registry.db`, `registry_cli.py` CLI, nightly UoW Registrar, Dan-manual `/confirm` gate, `/wos status` dispatcher command.

**Done condition:** Dan can ask "what's running?" and get an answer from a single UoWRegistry query. No GitHub fallback. Dan has used `/confirm` at least once. UoW Registrar runs without errors, audit log contains transition events (not only `created`).

**Phase 1 completion status: COMPLETE as of 2026-03-30.** The qualitative threshold has been met:
- UoW Registrar running cleanly (8 UoWs tracked, proposed-to-confirmed ratio stable at 1.0)
- UoWRegistry populated with real work
- Oracle audit passed ("excellent enough to implement")
- Design doc stable and converged

**Phase 1 to Phase 2 transition:** Two pre-Phase-2 blocking gates remain before implementation begins. Both are owned by Dan and documented in Open Decisions: (1) workflow artifact format decision (deterministic script vs. LLM prompt instructions), and (2) trigger evaluation mode decision (polling vs. event-driven). Resolving these two gates is the immediate next step; Steward MVP build begins once both are cleared.

### Phase 2: Steward + Executor [next]

**Pre-Phase-2 gates (must be cleared before implementation begins):** (1) Workflow artifact format decision (deterministic script vs. LLM prompt instructions) — see Open Decisions. (2) Trigger evaluation mode (polling vs. event-driven for condition triggers) — see Open Decisions. Both gates are blocking; neither has a costly resolution path.

**What to build:** Steward agent (cron heartbeat, diagnose/prescribe/evaluate/close loop), Executor agent (picks up `ready-for-executor` UoWs, runs prescribed workflow, returns results). UoWRegistry extended with Steward-cycle audit fields. Routing Classifier added: rule engine evaluating `classifier.yaml`, assigning postures, writing `route_reason`. Conditional Hook System wired. Cultivator wired to file seeds from philosophy sessions programmatically (Phase 2 trigger design).

**Done condition:** A UoW completes a full Steward/Executor loop — Steward diagnoses, Executor runs, Steward re-diagnoses and closes. Audit trail shows all cycle entries. Classifier assigns posture and writes `route_reason`. At least one hook fires and appears in `hooks_applied`.

### Phase 3: Routing Classifier + Observation Loop

**What to build:** Full diverge/converge execution — decomposition agents create child UoWs, `all_children_done` hook triggers synthesis agent. Visualization: audit log to timeline/tree on request. Observation Loop: META monitoring degradation signals with structured surfacing. Classifier evolution: `route_reason` pattern analysis, first-match to weighted scoring if systematic misrouting detected.

**Done condition:** A fan-out UoW completes its full cycle — root created, children created, all children done, synthesis fires, root transitions to `done`. Parent/children traversal works. Dan can ask "show me the tree for [UoW]" and get a readable summary.

### Phase 4: Dan Interrupt + Multi-Perspective Chains

**What to build:** Full Dan Interrupt protocol — Observation surfacing, Orientation input, human-gate `/decide` command with `approve|reject|defer` semantics. Multi-perspective fan-out with configurable posture profiles. Passive behavioral signal capture: each Steward audit cycle logged as a training signal (#189).

**Done condition:** A UoW completes a Steward/Executor loop that includes at least one Dan Interrupt — observation surfaced, orientation received, re-diagnosis logged, prescription adjusted. Multi-perspective fan-out completes a full cycle. `get_priorities()` returns the ready queue as the single source of truth for Dan and Lobster.

---

## Open Decisions

**Workflow artifact format: deterministic script vs. LLM prompt instructions**

When the Steward prescribes a workflow, it writes a workflow artifact that the Executor follows. Two candidate forms:

| Form | Description | Trade-offs |
|------|-------------|-----------|
| **Deterministic script** | A structured script that kicks off specific agents in a defined sequence — explicit branching, named subagents, typed outputs | More predictable, easier to audit, harder to write for novel situations |
| **LLM prompt instructions** | A rich instruction document that an Executor-agent follows via its own judgment — natural language, composable, flexible | More adaptable, harder to audit deterministically, relies on Executor fidelity |

This is the **first implementation decision to resolve** before the Executor can be built. It determines the interface contract between Steward and Executor, the structure of workflow artifacts, and the degree of determinism in the system.

**Pre-Phase-2 gate.** Resolution owner: Dan. Cheapest-test path: produce a concrete example of each form applied to one real workflow type (e.g., investigation or design review), then a decision logged to the audit trail within the first week of Phase 2 design. Fallback default if gate is not cleared: LLM prompt instructions (lower implementation cost; switch to deterministic script if auditability requirements surface during Phase 2). Phase 2 implementation does not begin until this gate is cleared.

---

**Trigger evaluation mode for condition triggers: polling vs. event-driven**

For time-based triggers, polling (evaluator checks on each scheduled run) is sufficient. For state-transition hooks like `all_children_done` and `retry-on-failure`, event-driven semantics (hook fires in the same transaction as the state transition) are materially different from polling semantics.

**Pre-Phase-2 gate.** Resolution owner: Dan. Cheapest-test path: test both semantics against the `retry-on-failure` hook with a real failed UoW before committing to either model. Fallback default: polling (simpler, consistent with existing cron infrastructure). Until resolved, the `condition` trigger type in the schema is a reserved field and no hook implementation depends on event-driven semantics.

---

**Hook storage split: structural vs. behavioral**

Structural hooks (retry logic, loop guards) belong in the system repo. Behavioral hooks (escalation thresholds, notification preferences) belong in user-config. The boundary case — convergence-trigger hook — is structural behavior with a user-tunable parameter (which synthesis agent to use). Resolves when Phase 2 hook implementation classifies each required hook by this rule.
