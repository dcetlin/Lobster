# Work Orchestration System v2 — Design Document

*Status: Active — 2026-03-30*

---

## Overview

The Work Orchestration System (WOS) is the pipeline that moves units of work from "filed as a GitHub issue" to "done and closed" — making every stage visible, auditable, and self-correcting. The problem it solves: Lobster generates observations and files them as issues, then the pipeline stalls at "filed." Issues accumulate; the archive grows; the activity metric (issues added) diverges from the outcome metric (work closed). WOS installs a **meter** — a structured path from observation to unit-of-work-in-motion to confirmed closure — and keeps that path visible at every stage.

The v2 model replaces the Phase 1 dispatcher-centric model with a two-actor **Steward/Executor** loop: a Steward that diagnoses and prescribes, and an Executor that carries out the prescription. The Steward owns each UoW's full lifecycle; the Executor does the work. Nothing exits via side-door.

**Already-embodied pattern:** Lobster's existing dispatcher/subagent architecture IS the Steward/Executor pattern operating informally. The dispatcher already acts as a Steward — it receives a message (a unit of work), diagnoses what kind of work it is, prescribes the appropriate subagent task, dispatches it, and evaluates the result. Background subagents are already Executors — they receive a structured task prompt (the WorkflowArtifact equivalent), execute it, and return results. WOS Phase 2 formalizes this into a tracked, auditable, crash-safe pipeline. The concepts are not new to Lobster; the infrastructure to make them durable and inspectable is what Phase 2 adds.

**Related docs:** [wos-constitution.md](wos-constitution.md) — the founding metaphor and naming constraints that govern WOS design decisions | [wos-golden-pattern.md](wos-golden-pattern.md) — canonical Python patterns for WOS implementation code

---

## Vocabulary & Primitives

### Core Terms

**Unit of Work (UoW)** — The atomic unit of tracked, auditable work in the pipeline. Every piece of work that enters the execution substrate is a UoW. A UoW has a state, an audit trail, and a closure condition.

**UoWRegistry** — The execution substrate. A structured store (SQLite from Phase 1) holding one record per UoW. It is the single source of truth for what is running; no other lookup is ever needed to answer "what's active?"

**UoW Registrar** — The governance agent that watches the GitHub issue backlog and creates new UoWs for the orchestration engine. It performs four functions: (1) reads GitHub issues, (2) identifies qualifying ones (ready-to-execute, gate criteria met), (3) creates UoW entries in the UoWRegistry, (4) manages lifecycle (expired proposals, stale-active detection). "Sweeper" was the prior name; the actual job is registration and lifecycle management, hence the rename.

### Pre-Registry Vocabulary

**Seed** — Unclassified potential. An idea, observation, or open question that may or may not become executable work. Not yet in the UoWRegistry. Seeds can originate from philosophy sessions, Telegram observations, direct feature requests, or any source.

**Seed spec and `success_criteria`:** A Seed that resolves to executable work becomes a GitHub issue. That issue body functions as the immutable Seed spec for the duration of the UoW's lifecycle. The `success_criteria` field (required, TEXT, prose) is the Seed's anchor: a human-readable statement of what completion looks like. It is written at germination time and does not change. The Steward evaluates `steward_agenda` against `success_criteria` at every re-entry — this is the mechanism that prevents both premature termination (declaring done before `success_criteria` is satisfied) and endless refinement (continuing past the point where `success_criteria` is satisfied). A Seed without `success_criteria` is invalid and is rejected at germination time. The field is prose, not a checklist: "implementation-ready spec for all 7 sub-issues" is sufficient. The Steward's judgment evaluates the current `output_ref` against this prose anchor; the anchor gives that judgment something principled to push against. Without it, termination behavior is inconsistent across UoW types.

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

### GitHub Issue Types

GitHub issues are the pre-UoWRegistry substrate for all seeds. Issues are typed by what they ARE at any moment — types are not permanent, they transition:

- **Type A — Ungerminated seed**: Unresolved output type or timing. Default state for new issues. Queued for the UoW Registrar.
- **Type B — UoW tracking issue**: Germinated; has a corresponding UoWRegistry entry. The issue is the handle for an active execution chain. No longer a seed.
- **Type C — Umbrella/epic**: Organizing structure with no direct execution intent. Children may be Type A or B. Never enters UoWRegistry as a single unit.
- **Type D — Historical record**: Closed, done. Lineage preserved only.

Transitions: A → B (on germination), B → D (on UoW completion), C stays C.

"When is an issue not a seed?" Two cases: (1) it germinated (now Type B tracker), (2) it was always Type C umbrella.

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
                   -> Steward/Executor loop  [Phase 2 — operational]
                     -> artifacts / done
```

This pipeline applies beyond philosophy sessions: any source of seeds (Telegram observations, nightly health scans, direct requests) flows through the same funnel — GitHub issue as the universal entry point, UoW Registrar as the gate into the execution substrate.

**Operational status note:** The Cultivator is not yet built. Until it is, philosophy session outputs (bootup candidates, seeds) reach GitHub via manual filing — the Cultivator stage is bypassed entirely. The Steward/Executor loop (Phase 2) is operational as of 2026-03-31. The pipeline diagram above reflects current system behavior at Phase 2; the `ASPIRATIONAL` label marks stages that remain unbuilt.

---

## State Machine

### States

| State | Semantics |
|-------|-----------|
| `proposed` | UoW Registrar created this record; awaiting Dan's confirmation. |
| `pending` | Dan confirmed via `/confirm`; queued for an agent to claim. |
| `ready-for-steward` | Active in the Steward/Executor loop; Steward's turn to diagnose. |
| `diagnosing` | Steward has claimed this UoW for a diagnosis pass (optimistic lock). Transient: transitions to `ready-for-executor`, `blocked`, or `done` within the same heartbeat invocation. If the Steward crashes mid-diagnosis, the startup sweep reclassifies it to `ready-for-steward` on the next heartbeat. |
| `ready-for-executor` | Steward has prescribed a workflow; Executor's turn to run it. |
| `active` | Executor is currently running the prescribed workflow. |
| `blocked` | Execution paused; awaiting an external condition or human decision. Set by the Steward when a surface condition fires (stuck threshold, severe error, Dan's input needed). Cleared by Dan via `/decide`. |
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
| `ready-for-steward` | `diagnosing` | Steward | Steward claims the UoW for diagnosis (optimistic lock) |
| `diagnosing` | `ready-for-executor` | Steward | Diagnosis complete; workflow prescribed |
| `diagnosing` | `blocked` | Steward | Stuck condition fires; surfacing to Dan |
| `diagnosing` | `done` | Steward | Convergence conditions met; closure declared |
| `diagnosing` | `ready-for-steward` | Startup sweep | Steward crashed mid-diagnosis; UoW reclassified |
| `blocked` | `ready-for-steward` | Dan | Dan provides orientation or confirms decision |
| `ready-for-executor` | `active` | Executor | Executor claims the UoW |
| `active` | `ready-for-steward` | Executor | Execution complete; results written |
| `active` | `failed` | Executor | Execution failed |
| `failed` | `ready-for-steward` | Hook (retry) | Retry hook re-queues after backoff |

Every transition is written to the audit log before it is considered to have happened (**Principle 1: No silent transitions**).

**Rework after closure:** `done` has no re-entry path. If a closed UoW's output proves wrong or requires follow-on work, a new UoW is filed that references the original UoW's ID in its description. This preserves the audit integrity of the closed record while creating a fresh execution chain.

> Rework convention: any rework, regardless of size, starts a fresh Type A seed (new GitHub issue) referencing the prior UoW ID in its description. The prior UoW stays done. No re-opening. This preserves audit integrity and eliminates re-open ambiguity.

---

## UoW Record Schema

Each UoW entry in the UoWRegistry has the following fields. Fields are written at creation unless noted.

| Field | Type | Written at | Description |
|-------|------|-----------|-------------|
| `id` | `TEXT` (UUID) | creation | Primary key. Unique per UoW. |
| `issue_id` | `TEXT` | creation | GitHub issue ID (e.g. `"SiderealPress/lobster#142"`). Idempotency key — duplicate proposals for the same issue are a no-op (see Crash Recovery and Idempotency). |
| `issue_url` | `TEXT` | creation | Full GitHub issue URL. |
| `title` | `TEXT` | creation | Issue title at proposal time. |
| `status` | `TEXT` | every transition | Current state (see State Machine above). |
| `proposed_at` | `TEXT` (ISO-8601) | creation | When the UoW Registrar created this record. |
| `confirmed_at` | `TEXT` (ISO-8601) \| `NULL` | Dan `/confirm` | When Dan confirmed. `NULL` until confirmed. |
| `claimed_at` | `TEXT` (ISO-8601) \| `NULL` | Executor claim | When an Executor claimed this UoW and set status to `active`. |
| `estimated_runtime` | `INTEGER` (seconds) \| `NULL` | creation or Steward prescription | Optional. Set by the proposer or Steward when scope is estimable. Used to compute `timeout_at`. |
| `timeout_at` | `TEXT` (ISO-8601) \| `NULL` | Executor claim | Computed as `claimed_at + estimated_runtime` if `estimated_runtime` is set; otherwise `claimed_at + 1800` (30 min default). The Observation Loop compares `NOW()` against `timeout_at` for any `active` record to detect silent stalls. |
| `output_file` | `TEXT` \| `NULL` | Executor claim | Written by the Executor when it claims the UoW. Full path to the Executor's output artifact. Enables crash recovery: if the Executor crashes, `output_file` is the last known artifact. The startup sweep checks `output_file` existence to classify stale-active records as potentially-complete vs. crashed. |
| `workflow_artifact` | `TEXT` \| `NULL` | Steward prescription | Path to the workflow artifact written by the Steward. |
| `prescribed_skills` | `TEXT` (JSON array) \| `NULL` | Steward prescription | Skill IDs to be loaded by the Executor at task start. |
| `success_criteria` | `TEXT NOT NULL` (new UoWs) / `TEXT NULL` (Phase 2 migration) | creation (germination) | Required for new UoWs. Prose description of what completion looks like for this UoW. Written at germination time; immutable thereafter. The Steward evaluates output against this field at every re-entry — it is the anchor that prevents premature termination and endless refinement. A new UoW without `success_criteria` is invalid and is rejected at germination time. **Phase 1→2 migration note:** The #309 migration adds this as `TEXT NULL` to preserve existing Phase 1 records. For pre-existing UoWs with `success_criteria = NULL`: the Steward falls back to evaluating against the `summary` field and writes a `success_criteria_missing` audit entry to flag the gap. |
| `steward_cycles` | `INTEGER NOT NULL DEFAULT 0` | Steward re-entry | Count of Steward diagnosis/prescription cycles completed on this UoW. Surface condition 3 (hard cap) fires at 5. |
| `steward_agenda` | `TEXT NULL` (JSON) | Steward only | Oracle-style forward forecast written by the Steward at first contact (`steward_cycles == 0`). List/tree of anticipated prescription nodes, each: `{posture, context, constraints, status: pending\|prescribed\|complete}`. Updated on each re-entry as new information arrives. **Steward-private — never read by the Executor.** |
| `steward_log` | `TEXT NULL` | Steward only | Append-only newline-delimited JSON log of every Steward decision point — diagnosis rationale, prescription decisions, surface-to-Dan trigger fires, agenda updates. Steward-to-future-self. **Steward-private — never read by the Executor.** |
| `audit_log` | JSON array \| external table | every event | Ordered audit entries. Each entry: `{event, actor, timestamp, note}`. Every state transition is appended here before the transition is considered complete. |
| `route_reason` | `TEXT` \| `NULL` | Classifier | Human-readable rationale for the posture assigned by the Routing Classifier. |
| `hooks_applied` | `TEXT` (JSON array) \| `NULL` | hook execution | Hook IDs that fired on this UoW. |
| `closed_at` | `TEXT` (ISO-8601) \| `NULL` | Steward closure | When the Steward declared `done`. |
| `parent_id` | `TEXT` \| `NULL` | creation | Parent UoW ID for sub-UoWs spawned by spec-breakdown. `NULL` for root UoWs. |

**IMPORTANT — Field naming:** The schema table above uses conceptual names. The actual SQLite schema uses different names. **All Phase 2 code must use the actual schema names:**

| Design doc name | Actual schema name | Notes |
|----------------|-------------------|-------|
| `issue_id` | `source_issue_number` | Stores integer issue number, not qualified string |
| `title` | `summary` | Already accurate |
| `claimed_at` | `started_at` | Same semantic role |
| `output_file` | `output_ref` | Live production data uses `output_ref` |
| `closed_at` | `completed_at` | Same pattern |

These are kept as-is to avoid breaking migrations. The design doc preserves conceptual names for readability; the actual schema is authoritative. See #309 for the full field mapping and migration spec.

**Column visibility contract:** Every new column added to `uow_registry` must declare its executor visibility at add time. Two options: (1) **Executor-accessible** — include in `executor_uow_view`; (2) **Steward-private or system-only** — explicitly exclude from `executor_uow_view` with a comment in the migration explaining why. `steward_agenda` and `steward_log` are excluded. All other fields listed above are Executor-accessible via the view. This is a standing convention that applies to all future columns.

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

**Nightly sweep algorithm:**
```
for each GitHub issue meeting gate criteria:
    if UoWRegistry.exists(issue_id=issue.id):
        continue  # idempotent — no duplicate UoWs
    create proposed UoW record with issue_id, issue_url, title, proposed_at
    write to audit_log: {event: "proposed", actor: "registrar", ...}

for each UoW where status == "proposed" and proposed_at < NOW() - 14d:
    transition to "expired"
    write to audit_log: {event: "expired", actor: "registrar", ...}

for each UoW where status == "active":
    if timeout_at IS NOT NULL and NOW() > timeout_at:
        surface to Steward: stall detected, timeout_at exceeded
```

### Steward Heartbeat

Runs on a cron heartbeat (initially ~3 minutes). On each invocation, executes three functions in order: (1) startup sweep (crash recovery), (2) Observation Loop (stall detection), (3) Steward main loop (diagnose and prescribe for all `ready-for-steward` UoWs).

For each `ready-for-steward` UoW, the Steward:

1. **Diagnoses** — reads the UoW trail (original intent, prior prescriptions, execution logs, current UoWRegistry state, Vision Object context). Writes the diagnosis to the audit trail before prescribing. **Diagnosis inputs:** `{uow_record, audit_log, output_ref contents if exists, steward_cycles count, steward_agenda, steward_log, Dan's current register from context}`. **Diagnosis output:** a written assessment logged to `audit_log` before any prescription is written.
2. **Prescribes** — selects a workflow primitive with a written rationale. Writes the prescription (named workflow + artifact path) to the audit trail. Transitions UoW to `ready-for-executor`. **Prescription output written to UoW record:** `{workflow_artifact: <path>, prescribed_skills: [...], route_reason: <rationale>, steward_agenda: <updated>, steward_log: <appended>}`.
3. **Evaluates** (on re-entry after execution) — reads execution results (from `output_ref` and `audit_log`), re-diagnoses fresh, decides: loop again or declare closure. Increments `steward_cycles`.
4. **Closes** — writes a closing diagnosis when convergence conditions are met. Transitions to `done`. Sets `completed_at`.

**Initialization ritual:** On first contact with a new UoW (`steward_cycles == 0` at the start of a diagnosis pass), the Steward's first act is to write an initial `steward_agenda` before any prescription decision is made. Forecast depth is calibrated by the Steward: well-defined UoWs (concrete deliverable, clear scope) get a full agenda upfront; open-ended UoWs (exploratory, ill-defined scope) get 1-2 steps with a `"pending evaluation"` marker for the remainder. The agenda is a structured forecast, not a contract.

**Re-entry decision protocol (summary):** On every Executor return, the Steward reads all inputs (Seed, `steward_agenda`, `steward_log`, Executor's structured return, `output_ref` contents, `steward_cycles`) before writing anything. Decision sequence: (1) parse the `return_reason` and classify (Normal / Blocked / Abnormal / Error / Orphan); (2) assess completion against the Seed and `success_criteria` — this is the primary gate, not `return_reason` alone; (3a) if complete: write closure, mark agenda nodes, set `completed_at`, transition to `done`; (3b) if incomplete: update `steward_agenda`, write next prescription, append `steward_log` entry, transition to `ready-for-executor`. Full re-entry spec lives in #303.

The Steward surfaces to Dan under three conditions: (1) something is severely wrong and outside confident operating range; (2) Dan's perspective would materially change the prescription; (3) `steward_cycles >= 5` — hard cap, surface unconditionally. (The prior surface condition 3 was "same primitive twice, no new input." The hard cap at 5 replaces this as the convergence-velocity proxy — it is externally measurable without requiring the Steward to classify its own state.)

**Executor isolation (`executor_uow_view`):** The Executor MUST query `executor_uow_view`, never the `uow_registry` table directly. This is a Phase 2 requirement. The view excludes `steward_agenda` and `steward_log` — the Executor cannot read steward-private fields at the DB layer, with no application enforcement needed. All Executor SQL must target the view.

**Observability:** Structured logging is required at every Steward decision point. Key metrics: `steward_cycles` distribution, agenda completion rate (nodes marked `complete` / nodes total at closure), `return_reason` distribution across all UoWs. These signals indicate whether the Steward is converging efficiently or looping without progress.

**Prescribed skills**: Steward diagnosis MAY include a `prescribed_skills` field — a list of skill IDs to be loaded by the executor at task start. This keeps methodology context out of the always-loaded context and activates it situationally. Examples:
- A bug-fix UoW → prescribe `systematic-debugging` (4-phase root-cause process)
- Any PR UoW → prescribe `verification-before-completion` (prove it works before marking done)
- A complex multi-agent UoW → prescribe `subagent-driven-development` (spec + quality review)

Skill content from frameworks like [Superpowers](https://github.com/Anysphere/superpowers) (118k stars, actively maintained) can be adapted into Lobster's skill library (`~/lobster-user-config/skills/`) and prescribed by the Steward. This keeps the methodology overhead zero for simple UoWs and available for complex ones.

The Steward's diagnostic function has a developmental dimension distinct from the UoW's internal requirements. The question is not only "what does this UoW need to move forward?" but "what does this UoW need relative to Dan's current orientation?" A UoW that is technically ready-for-executor may require a different prescription if Dan is in an exploratory register than if he is in an executive one. The Steward holds both dimensions simultaneously: the UoW's internal state and Dan's current developmental and attentional position. Diagnosis that ignores the second dimension produces technically correct prescriptions that land in the wrong register — which is a coupling failure, not a content failure.

### Executor

Picks up UoWs in `ready-for-executor` state. Carries out the prescribed workflow as specified in the workflow artifact. Writes execution results to an output artifact. Transitions the UoW back to `ready-for-steward` with the execution log. The Executor does not diagnose or decide — it executes and reports.

**Executor claim sequence (atomic, 6 steps — steps 2-6 in a single SQLite transaction):**
1. Read UoW — verify `status == 'ready-for-executor'`.
2. `UPDATE uow_registry SET status='active' WHERE id=? AND status='ready-for-executor'` — check rows affected; if 0, another Executor claimed first, abort.
3. Write `started_at = NOW()` to UoW record. (Actual schema field name; design doc called this `claimed_at`.)
4. Write `output_ref = <absolute path to output artifact>` to UoW record. This is the ground-truth re-entry point: if the Executor crashes mid-execution, `output_ref` is the last known artifact path and the startup sweep uses it to classify the record. (Actual schema field name; design doc called this `output_file`.)
5. Compute and write `timeout_at = started_at + estimated_runtime` (or default 1800s if `estimated_runtime` is NULL).
6. Append `{event: "claimed", actor: "executor", started_at, output_ref, timeout_at}` to audit_log. This INSERT is in the same transaction as steps 2-5.

Begin executing the prescribed workflow only after the transaction commits.

> **Atomicity lineage:** This claim sequence inherits the same atomic-claim pattern that Lobster's inbox uses to prevent concurrent double-processing. In Lobster's inbox, a message is claimed via an atomic filesystem move (`inbox/` → `processing/`); no two agents can claim the same file because the move is atomic at the OS level. WOS uses the equivalent mechanism at the database layer: the status transition from `ready-for-executor` to `active` is executed inside a SQLite transaction with optimistic locking — if two Executors race, only one wins the transition. The recovery equivalence also holds: Lobster's `processing/` sweep on startup (finding abandoned in-flight messages) maps directly to WOS's startup sweep over stale `active` records. Same pattern, different substrate.

**Executor Output Contract — required for every Executor implementation:**

Every Executor **must** write `{output_ref}.result.json` before transitioning the UoW to `ready-for-steward`. This applies to all exit paths: complete, partial, failed, and blocked. An Executor that transitions without writing this file is in contract violation.

Path derivation (mirrors `executor-contract.md §Schema`):
- Primary: replace extension → `foo.json` becomes `foo.result.json`
- Fallback (no extension): append suffix → `/path/to/artifact` becomes `/path/to/artifact.result.json`

Minimum required fields in the result file:

| Field | Required | Description |
|-------|----------|-------------|
| `uow_id` | yes | Must match the UoW's `id` field |
| `outcome` | yes | `"complete"` \| `"partial"` \| `"failed"` \| `"blocked"` |
| `success` | yes | `true` iff `outcome == "complete"` |
| `reason` | for non-complete | Human-readable explanation |

The Steward's `_assess_completion` reads this file to route the UoW. If the file is absent and `success_criteria` is set, the Steward cannot declare done — it will cycle to the hard cap (5 retries) and surface to Dan. **Absence of the result file is a contract violation, not an ambiguous state.**

Full schema, outcome enum, Steward interpretation table, and failure taxonomy: **[`docs/executor-contract.md`](executor-contract.md)**.

The Lobster `Executor` class (`src/orchestration/executor.py`) implements this contract: `_write_result_json()` is called at every intentional exit point, and the exception handler writes a `failed` result before re-raising. New Executor implementations (custom executor types, test doubles) must replicate this pattern.

The Steward/Executor loop continues until the Steward declares convergence:

```
Steward → Executor → Steward → Executor → ... → Steward declares done
```

### Steward↔Executor Contract

This contract governs the completion handshake between the Executor and the Steward. Every Executor implementation must honor it.

The full specification — schema, outcome enum, Steward interpretation table, posture rationale, and failure taxonomy — is in **[`docs/executor-contract.md`](executor-contract.md)**.

Summary:
- Executor must write `{output_ref}.result.json` before transitioning to `ready-for-steward`.
- `outcome` is the routing field: `"complete"` | `"partial"` | `"failed"` | `"blocked"`.
- Steward evaluates `success_criteria` against `output_ref`; Executor does not self-evaluate.
- Absence of the result file when `success_criteria` is present is a contract violation, not an ambiguous state.

### Observation Loop

META monitors the UoWRegistry and audit log for degradation signals: dark pipeline (no audit entries for >3 days), orphaned active records, ready-queue growth without drain, stale count accumulation, issue-open rate exceeding close rate for >2 consecutive weeks. When a signal fires, META surfaces it to Dan with evidence, not guesswork.

**Stall detection (MVP scope — Phase 2):** On each Observation Loop pass, for every UoW where `status = 'active'`: if `NOW() >= timeout_at` (or `started_at + 1800s` if `timeout_at` is NULL), the record is a stall candidate. Write audit entry `{event: "stall_detected", actor: "observation_loop", uow_id, started_at, timeout_at, output_ref, elapsed_seconds}` then transition to `ready-for-steward` via optimistic lock. The Steward classifies and decides — the Observation Loop only detects and surfaces, never acts unilaterally. Does NOT send Telegram messages directly. Phase 3 scope adds: dark pipeline detection, ready-queue growth signals, issue-open rate trend monitoring.

### Crash Recovery and Idempotency

These four properties are required for the system to be safe to operate continuously. They are sourced from battle-tested patterns in Lobster's `register_agent` / session_store system (`~/lobster/src/agents/tracker.py`, `~/lobster/src/mcp/inbox_server.py`) — the same durability properties that make Lobster's background agent lifecycle reliable.

**1. Timeout and estimated_runtime**

Every UoW in `active` state has a `timeout_at` timestamp computed at claim time. `estimated_runtime` (optional, set at proposal time or by the Steward) drives the computation; the default is 1800 seconds (30 min). The Observation Loop checks `timeout_at` on each pass. A UoW that exceeds `timeout_at` without transitioning is a silent stall — it is surfaced to the Steward for classification (crashed vs. slow vs. legitimate blocking condition). This prevents `active` records from disappearing into silence.

**2. Executor writes `output_ref` at claim time**

When an Executor claims a UoW, it writes the `output_ref` path to the UoW record before beginning execution. This is not optional. The `output_ref` field is the last known artifact pointer: if the Executor crashes mid-execution, the UoW record contains the path to whatever was written before the crash. The startup sweep (below) uses `output_ref` to distinguish completed-before-crash from nothing-written. (Actual schema field name; design doc originally called this `output_file`.)

**3. Steward startup sweep for orphaned UoWs**

Runs on every `steward-heartbeat.py` invocation (every 3 minutes), not only at process startup. Scans UoWRegistry for two classes of orphaned UoWs:

Class 1 — `active` UoWs (Executors that may have crashed mid-execution):
```
if output_ref IS NOT NULL and os.path.exists(output_ref) and file is non-empty:
    classification = 'possibly_complete' — Executor may have finished before crash.
elif output_ref IS NOT NULL and file exists but is 0 bytes:
    classification = 'crashed_zero_bytes'
elif output_ref IS NOT NULL and file does not exist:
    classification = 'crashed_output_ref_missing'
else (output_ref IS NULL):
    classification = 'crashed_no_output_ref'
```

Class 2 — `ready-for-executor` UoWs older than 1 hour (Executors that crashed before step 1 of the claim sequence, before ever transitioning to `active`):
```
    classification = 'executor_orphan'
```

For each: write `{event: "startup_sweep", classification: <value>}` to audit_log, then transition to `ready-for-steward` via optimistic lock. The sweep does not act unilaterally — it classifies and presents. The Steward makes the decision and writes it to the audit log.

**4. Idempotent proposal creation keyed on issue_id**

Creating a UoW proposal for a GitHub issue that already has a UoW in the Registry is a no-op — the UoW Registrar checks `issue_id` before creating new records and returns the existing record without modification. This prevents duplicate UoWs from accumulating across sweep re-runs, restarts, or manual triggers. The check is the first operation in the nightly sweep loop (see UoW Registrar algorithm above).

> **Golden patterns reference:** Items 1–4 above are directly sourced from Lobster's battle-tested agent lifecycle implementation in `~/lobster/src/agents/tracker.py` and `~/lobster/src/mcp/inbox_server.py`. The `register_agent` / `session_store` system implements the same four properties — liveness detection via `timeout_minutes` + mtime polling, `output_file` as ground-truth artifact pointer, startup sweep over stale active sessions, and idempotent session creation. WOS adopts these patterns at the UoW level.

---

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

**Phase 1 to Phase 2 transition:** Both pre-Phase-2 blocking gates are now resolved (2026-03-30). (1) Workflow artifact format — Option C: structured envelope + instructions field. (2) Trigger evaluation mode — polling via `evaluate_condition(uow)`. See Resolved Decisions for full rationale. Steward MVP build can begin.

### Phase 2: Steward + Executor [complete]

**Phase 2 completion status: COMPLETE as of 2026-03-31.** All seven PRs merged (see #301 umbrella):
- PR0 (#309): schema migration — Phase 2 fields + `executor_uow_view` — MERGED
- PR1 (#302): WorkflowArtifact struct (`src/orchestration/workflow_artifact.py`) — MERGED
- PR2 (#303): Steward heartbeat script (`scheduled-tasks/steward-heartbeat.py`) — MERGED
- PR3 (#304): `evaluate_condition(uow)` callable + Registrar sweep wiring — MERGED
- PR4 (#305): Executor MVP — 6-step atomic claim, LLM dispatch, `output_ref`, return to Steward — MERGED
- PR5 (#306): Observation Loop — stall detection for `active` UoWs within steward heartbeat — MERGED
- PR6 (#307): Startup sweep (crash recovery) — classify orphaned `active` and `ready-for-executor` UoWs — MERGED

**Runtime execution control (PR #428):** Executor dispatch is gated by `~/lobster-workspace/data/wos-config.json` (`execution_enabled: true/false`). Enabled via `wos start` / `wos stop` dispatcher commands. Default is `false` (safe) when the file is absent. This replaced the prior `BOOTUP_CANDIDATE_GATE` hardcoded constant for executor dispatch. Note: `BOOTUP_CANDIDATE_GATE` (bootup-candidate label filtering) remains active in the Steward and is a separate concern from executor dispatch.

**Note on Routing Classifier, Hook System, and Cultivator:** These are NOT Phase 2 deliverables. The Routing Classifier and Conditional Hook System are defined in issue #168 and are Phase 3 scope. The `route_reason` field exists in the schema and the Steward writes it as free text in Phase 2; the Routing Classifier that parses and assigns postures systematically is Phase 3. The Cultivator (philosophy pipeline classification agent) is aspirational and not in any current phase scope.

**Out of scope for Phase 2:** Multi-executor fan-out from a single Steward prescription (filed as #314 for Phase 3+). The Phase 2 design must not foreclose fan-out — the `executor_type` field in WorkflowArtifact and the `parent_id` field in the registry are the designed seams. Phase 2 delivers single-Executor-per-prescription only.

**Done condition:** A UoW completes a full Steward/Executor loop — Steward diagnoses (writes `steward_agenda` on first contact), Executor runs (via `executor_uow_view`), Steward re-diagnoses and closes. Audit trail shows all cycle entries. `success_criteria` evaluated at closure. `steward_cycles` incremented correctly.

### Steward↔Executor Contract

This contract governs the completion handshake between the Executor (PR4/#305) and the Steward. It is introduced by the Steward but must be fulfilled by the Executor — every Executor implementation must honor it.

**Executor MUST write a result file at completion**

When execution of a UoW finishes (success or failure), the Executor must write a structured result file before returning the UoW to `ready-for-steward`. The result file path is derived from `output_ref`:

- Primary convention: `{output_ref}.result.json` (replace the extension with `.result.json`)
- Fallback convention: `{output_ref}.result.json` appended as a suffix (i.e. `{output_ref}.result.json` where output_ref is treated as a full path with no extension replacement)

The Steward's `_assess_completion()` checks both paths, preferring the primary convention.

**Minimum required fields in the result file**

```json
{
  "success": true,
  "uow_id": "<uow_id>"
}
```

- `success` (bool, required): `true` if the UoW objective was achieved; `false` if not.
- `uow_id` (string, required): the UoW ID this result belongs to.
- `reason` (string, optional): human-readable explanation when `success` is `false`.

Additional fields are permitted and ignored by the Steward.

**What the Steward does with this file**

`_assess_completion()` reads the result file after verifying `output_ref` is valid and the re-entry posture is `execution_complete` or `startup_sweep_possibly_complete`. Behavior:

- `success: true` → Steward declares the UoW done, transitions to `done`.
- `success: false` → Steward treats the UoW as not complete; prescribes another cycle or surfaces to Dan depending on `steward_cycles` and stuck-condition detection.
- File absent → Steward treats the UoW as still running / inconclusive; will not declare done if `success_criteria` is present. (Phase 1 / legacy UoWs with no `success_criteria` fall through to a posture-based heuristic.)

**Absence is a contract violation, not a soft condition**

If the result file is absent and the UoW has `success_criteria`, the Steward cannot declare done and will surface to Dan after the hard cap is reached. Executors that skip writing the result file will cause unnecessary human interrupts.

### Phase 3: Routing Classifier + Hook System + Fan-out

**What to build:** Routing Classifier (#168) — rule engine evaluating `classifier.yaml`, assigning postures, writing `route_reason` to each UoW record. Conditional Hook System — structural hooks (retry logic, loop guards) in system repo; behavioral hooks in user-config. Full diverge/converge execution — decomposition agents create child UoWs, `all_children_done` hook triggers synthesis agent. Visualization: audit log to timeline/tree on request. Observation Loop: META monitoring degradation signals with structured surfacing. Classifier evolution: `route_reason` pattern analysis, first-match to weighted scoring if systematic misrouting detected.

**Done condition:** A new UoW arrives, Classifier assigns posture, `route_reason` in Registry reflects which rule fired. At least one hook fires and appears in `hooks_applied`. A fan-out UoW completes its full cycle — root created, children created, all children done, synthesis fires, root transitions to `done`. Parent/children traversal works. Dan can ask "show me the tree for [UoW]" and get a readable summary.

### Phase 4: Dan Interrupt + Multi-Perspective Chains

**What to build:** Full Dan Interrupt protocol — Observation surfacing, Orientation input, human-gate `/decide` command with `approve|reject|defer` semantics. Multi-perspective fan-out with configurable posture profiles. Passive behavioral signal capture: each Steward audit cycle logged as a training signal (#189).

**Done condition:** A UoW completes a Steward/Executor loop that includes at least one Dan Interrupt — observation surfaced, orientation received, re-diagnosis logged, prescription adjusted. Multi-perspective fan-out completes a full cycle. `get_priorities()` returns the ready queue as the single source of truth for Dan and Lobster.

---

## Design Principle

> **Optimize for current scale, place abstraction precisely at seams that will need to flex.** Simplest implementation that works now, with named interfaces at the points where the grain of the design indicates future change will be needed. This is not over-engineering — it is reading the grain.

This principle governs every architectural decision in WOS. When a decision is between "simpler now, harder to change later" and "named interface now, swap-out later," the choice depends on whether this seam is one where the design's grain indicates future change. At seams that will need to flex, the named interface is the simpler choice in total. At seams that will not, it is premature.

---

## Resolved Decisions

### Decision 1: Workflow artifact format — RESOLVED: Option C (structured envelope + instructions field)

**Status: Resolved. Phase 2 implementation proceeds on this basis.**

The `workflow_artifact` is a named struct (structured envelope) with a required `instructions` field containing natural language instructions the Executor follows. The envelope fields are load-bearing throughout Phase 2 — not ceremonial. The struct is:

```
workflow_artifact:
  uow_id:            TEXT        # which UoW this artifact belongs to
  executor_type:     TEXT        # which executor class should pick this up
  constraints:       TEXT[]      # hard constraints the Executor must honor
  prescribed_skills: TEXT[]      # skill IDs to load at task start
  instructions:      TEXT        # natural language — the Steward's prescription
```

**Rationale for implementers:**

(a) **Formalizing existing practice.** This is already how Lobster dispatches subagents today — a structured prompt with named fields and natural language instructions. Adopting this pattern at the Steward/Executor seam means no conceptual leap; the Executor is a subagent that follows a well-formed task spec.

(b) **Auditability without rigidity.** The envelope fields (`uow_id`, `executor_type`, `constraints`, `prescribed_skills`) give the audit log structured data to work with — you can query which executor type ran, what constraints were imposed, which skills were loaded — without locking the instruction content into a deterministic script format that cannot handle novel situations.

(c) **Abstraction at the right seam.** The envelope is the contract between Steward and Executor. The `instructions` field is the Steward's judgment, expressed in natural language. Separating these means the contract (envelope schema) can evolve independently from the instruction style. If the Steward/Executor contract needs to change in Phase 3, it is a schema change on the envelope — not a refactor of how instructions are written.

---

### Decision 2: Trigger evaluation mode — RESOLVED: polling via evaluate_condition(uow)

**Status: Resolved. Phase 2 implementation proceeds on this basis.**

For Phase 2, trigger evaluation is implemented as polling: the UoW Registrar calls `evaluate_condition(uow)` on each sweep pass. `evaluate_condition(uow)` checks the relevant external state (GitHub API, registry state, or other condition) and returns a boolean — fire or no-fire.

**`evaluate_condition(uow)` is a named callable.** It is not inlined logic in the sweep loop. This is the seam.

**Rationale for implementers:**

(a) **Native to the Registrar's scheduling model.** The Registrar already runs on a polling sweep. Adding trigger evaluation as a function call on each pass requires no new infrastructure — it is exactly what the Registrar loop already does.

(b) **At current scale, polling lag is irrelevant.** The conditions we are evaluating in Phase 2 (`all_children_done`, `retry-on-failure`, time-based triggers) have latency tolerance in the minutes range. Polling on the Registrar's sweep cadence (nightly for proposals; configurable for state checks) is accurate enough.

(c) **The abstraction makes Phase 3 a backend swap, not a refactor.** By naming the callable `evaluate_condition(uow)` and calling it consistently, the switch to event-driven semantics in Phase 3 means replacing the implementation of `evaluate_condition` — not touching the Registrar's loop structure. The Registrar does not need to know whether evaluation is polling or event-driven; it calls the function and gets a result.

---

## Open Decisions

**Hook storage split: structural vs. behavioral**

Structural hooks (retry logic, loop guards) belong in the system repo. Behavioral hooks (escalation thresholds, notification preferences) belong in user-config. The boundary case — convergence-trigger hook — is structural behavior with a user-tunable parameter (which synthesis agent to use). Resolves when Phase 2 hook implementation classifies each required hook by this rule.
