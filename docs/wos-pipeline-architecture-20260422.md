# WOS Pipeline Architecture

**Date:** 2026-04-22
**Status:** Canonical (v3 — heartbeat locking, recurrence, lifecycle, loops)
**Scope:** Full cultivator-to-executor pipeline, including germinator register classification, routing classifier posture assignment, executor dispatch, lifecycle stages, and feedback loops.
**Workstream:** `~/lobster-workspace/workstreams/wos/README.md`

---

## Related docs

- [WOS-INDEX.md](WOS-INDEX.md) — Component glossary: authoritative naming reference for Germinator, Cultivator, Registry, Steward, and Executor
- [wos-vision.md](wos-vision.md) — Vision and premises (human-readable companion; authoritative intent is in `vision.yaml`)
- [wos-constitution.md](wos-constitution.md) — The founding metaphor and naming constraints that govern all WOS design decisions
- [wos-v3-proposal.md](wos-v3-proposal.md) — V3 design proposal: register taxonomy, corrective traces, delivery vs. closure
- [wos-v3-steward-executor-spec.md](wos-v3-steward-executor-spec.md) — Steward/Executor contract spec
- [wos-dispatch-failure-modes.md](wos-dispatch-failure-modes.md) — Known failure modes and mitigations in the dispatch path
- [wos-registry-reference.md](wos-registry-reference.md) — Registry schema and field reference
- [wos-design-audit-2026-04-08.md](wos-design-audit-2026-04-08.md) — April 2026 design audit findings
- [executor-contract.md](executor-contract.md) — Executor contract (in `docs/`)
- GH issue [#194](https://github.com/dcetlin/Lobster/issues/194) — Philosophy pipeline: multi-register coupling and behavioral gate architecture

---

## Pipeline Flowchart

```mermaid
flowchart TD
    GH["GitHub Issues\n(dcetlin/Lobster)"]

    subgraph INGEST ["Ingest Layer"]
        GC["GardenCaretaker\ngarden_caretaker.py\nscan → tend\nevery 15 min via cron"]
        CULT["github-issue-cultivator\n(LLM scheduled job)\nfetch → classify-priority → promote\nDaily at 06:00 UTC"]
    end

    SKIP{Skip condition?\nmeta/tracking label\nor already active UoW}
    SKIP_OUT["Skip\n(idempotent upsert)"]

    subgraph REGISTRATION ["Registration — synchronous on insert"]
        GERM["Germinator\ngerminator.py\nRegister classification at germination time"]

        subgraph GERM_GATES ["Germinator — 4-gate ordered register classifier"]
            G1{"Gate 1:\nMachine-executable\ncommand present?\n(pytest, bash, gh, make, ...)"}
            G2{"Gate 2 (if G1 yes):\nRequires iterations?\n(all, until, 100%, passing, ...)"}
            G3{"Gate 3:\nPhenomenological\nvocabulary?\n(poiesis, frontier, pearl, ...)"}
            G4["Gate 4 (default):\noperational"]

            G1 -->|Yes| G2
            G2 -->|Yes| REG_IC["register: iterative-convergent"]
            G2 -->|No| REG_OP1["register: operational"]
            G1 -->|No| G3
            G3 -->|Yes| REG_PH["register: philosophical"]
            G3 -->|No| G4
            G4 --> REG_OP2["register: operational\n(no hedge words in criteria)\nor human-judgment\n(hedge words present)"]
        end

        RC["Routing Classifier\nrouting_classifier.py\nPosture assignment via classifier.yaml\n(first-match-wins rules)"]

        subgraph RC_RULES ["Routing Classifier — rule examples (classifier.yaml)"]
            RC1["seed → sequential"]
            RC2["high-risk → review-loop"]
            RC3["large + executable → fan-out"]
            RC4["default → solo"]
        end

        UPSERT["Registry._upsert_typed\nINSERT with register + posture + route_reason\n(idempotent — existing active UoWs not re-inserted)\nstate: proposed → pending → ready-for-steward"]
    end

    subgraph STEWARD_LOOP ["Steward/Executor Loop (every 3 min)"]
        SHB["Steward Heartbeat\nsteward-heartbeat.py\nevery 3 min via cron\n(offset 0s)"]
        STARTUP["1. Startup Sweep\norphan recovery: active/executing/diagnosing\n→ reset to ready-for-steward"]
        OBS["2. Observation Loop\ndetect stalled UoWs via timeout_at\n→ surface to ready-for-steward"]
        STEWARD["3. Steward Main Loop\ndiagnose → prescribe/close/surface\noptimistic lock:\nWHERE status='ready-for-steward'"]
        PRESCRIBE["Prescribe\nwrite WorkflowArtifact\nstate: → ready-for-executor"]
        SURFACE["Surface to Dan\n(philosophical / human-judgment\nUoWs — cannot machine-close)"]
        DONE_CHECK{"Success criteria\nmet?"}
    end

    subgraph EXECUTOR_LOOP ["Executor Loop (every 3 min, +90s offset)"]
        HB["Executor Heartbeat\nexecutor-heartbeat.py\nevery 3 min via cron\n(90s after steward)"]
        WOS_GATE{wos-config.json\nexecution_enabled?}
        TTL["Phase 1: Orphan Safety Net\nrecover_ttl_exceeded_uows()\n24h TTL — orphaned UoWs evading observation loop"]
        PRIMARY["Primary Dispatch Path\n_dispatch_via_inbox\nwrites wos_execute message\nto ~/messages/inbox/\n(fire-and-forget, seconds latency)"]
        RECOVERY["Recovery Dispatch\n(heartbeat catches missed dispatches\n>5 min in ready-for-executor)"]

        LOCK["6-step Atomic Claim\nBEGIN IMMEDIATE transaction\nUPDATE WHERE status='ready-for-executor'\nrowcount=0 → ClaimRejected\n(optimistic lock prevents duplicate execution)"]
    end

    LOBSTER["Lobster Dispatcher\npicks up wos_execute message\non next cycle (~seconds)"]
    SUBAGENT["Register-Appropriate Subagent\nfunctional-engineer / frontier-writer / design-review\nexecutes UoW\nstate: active → executing"]
    RESULT["result.json + trace.json written\noutcome: complete / partial / failed / blocked\nstate: executing → ready-for-steward"]
    ORACLE["Oracle Review\noracle/verdicts/\nPR diff reviewed by oracle agent\nverdict written to oracle/verdicts/pr-NNN.md"]
    ORACLE_VERDICT{Verdict?}
    MERGE["Merge Agent\nmerges PR\nstate: → done"]
    FIX["Fix Agent\naddresses NEEDS_CHANGES"]
    DONE["UoW marked done\n(Steward only — executor never marks done)"]

    GH --> INGEST
    GC --> SKIP
    CULT --> SKIP
    SKIP -->|Yes| SKIP_OUT
    SKIP -->|No| GERM
    GERM --> GERM_GATES
    GERM_GATES --> RC
    RC --> RC_RULES
    RC_RULES --> UPSERT

    UPSERT --> SHB
    SHB --> STARTUP
    STARTUP --> OBS
    OBS --> STEWARD
    STEWARD --> PRESCRIBE
    STEWARD --> SURFACE
    STEWARD --> DONE_CHECK
    DONE_CHECK -->|Yes| DONE
    DONE_CHECK -->|No| PRESCRIBE

    PRESCRIBE --> HB
    HB --> WOS_GATE
    WOS_GATE -->|No| SKIP_OUT2["Dispatch skipped\n(TTL recovery still runs)"]
    WOS_GATE -->|Yes| TTL
    TTL --> PRIMARY
    PRIMARY --> LOCK
    LOCK -->|ClaimSucceeded| LOBSTER
    LOCK -->|ClaimRejected| SKIP_OUT3["Skip — another executor claimed first\n(optimistic lock)"]
    HB -.->|recovery net| RECOVERY
    RECOVERY --> LOCK

    LOBSTER --> SUBAGENT
    SUBAGENT --> RESULT
    RESULT --> STEWARD

    RESULT -->|code UoW| ORACLE
    ORACLE --> ORACLE_VERDICT
    ORACLE_VERDICT -->|APPROVED| MERGE
    ORACLE_VERDICT -->|NEEDS_CHANGES| FIX
    FIX --> SUBAGENT
    MERGE --> DONE

    subgraph METABOLIC ["Metabolic Output Layer (outcome_refs — Issue #880)"]
        OREFS["outcome_refs\n[{type, ref, category}]\nwritten by subagent in write_result\nprovenance carrier — makes stock traversable"]

        PEARL["Pearl\noutcome_category: pearl\nTyped stock with address\n(PR URL, file path, doc link)"]
        SEED["Seed\noutcome_category: seed\nTyped stock with address\n(GitHub Issue URL)"]
        HEAT["Heat\noutcome_category: heat\nEnergy with log entry\n(no downstream stock)"]
        SHIT["Shit\noutcome_category: shit\nEnergy with log entry\n(no downstream stock)"]

        PEARL_OUT["Artifact delivered\nSystem state changes\nObservable in repo / docs"]
        SEED_ISSUE["New Issue filed\n(ref in outcome_refs)"]
        SEED_UOW["New UoW\n(via Cultivator on next cycle)"]
        HEAT_LOG["Log entry only\nWork done, no artifact\n(healthy no-op)"]
        SHIT_LOG["Waste account entry\nFuture cleanup work\n(stale notes, accumulation)"]
    end

    RESULT --> OREFS
    OREFS -->|category=pearl| PEARL
    OREFS -->|category=seed| SEED
    OREFS -->|category=heat| HEAT
    OREFS -->|category=shit| SHIT
    PEARL --> PEARL_OUT
    SEED --> SEED_ISSUE
    SEED_ISSUE --> SEED_UOW
    SEED_UOW -.->|generative loop| GH
    HEAT --> HEAT_LOG
    SHIT --> SHIT_LOG
```

---

## Feedback Loops in the System

The WOS pipeline contains several explicit feedback loops:

1. **Steward re-prescription loop** — After a subagent writes `result.json`, the UoW transitions to `ready-for-steward`. The Steward re-evaluates success criteria; if unsatisfied, it re-prescribes and the UoW cycles back through the executor. This is the primary loop for iterative-convergent UoWs.

2. **Oracle → fix → re-oracle loop** — For code UoWs that open a PR: oracle agent reviews the diff, writes a verdict to `oracle/verdicts/pr-NNN.md`. A NEEDS_CHANGES verdict dispatches a fix agent which opens a new PR revision; the oracle re-runs. This loop repeats until APPROVED.

3. **Heartbeat stall detection loop** — The steward's observation loop checks `active` UoWs every 3 minutes. If `heartbeat_at` has been silent for more than `heartbeat_ttl` seconds (default: 300s), the UoW is transitioned back to `ready-for-steward` for re-diagnosis. The executor heartbeat retains a 24h TTL orphan safety net (`recover_ttl_exceeded_uows`) as a last-resort backstop for UoWs that somehow evade the observation loop, but it is no longer the primary stall detection mechanism.

4. **Startup sweep loop** — On every 3-minute steward heartbeat invocation, the startup sweep scans for orphaned UoWs in `active`, `executing`, and `diagnosing` states and resets them to `ready-for-steward`. This catches crashes between heartbeat cycles.

5. **GardenCaretaker tend loop** — Every 15 minutes, GardenCaretaker reconciles active UoW bindings against current GitHub issue state. If a source issue is closed or deleted, UoWs in non-executing states are archived; executing states are left alone.

---

## 8 Reflections Answered

### 1. Locking mechanism — heartbeat-based locking (PR #848, merged 2026-04-22)

The locking model has three coordinated components:

**Claim (atomic, optimistic lock):** The Executor performs a **6-step atomic claim sequence** inside a `BEGIN IMMEDIATE` SQLite transaction. Step 2 is the optimistic lock:

```sql
UPDATE uow_registry SET status='active', updated_at=?, heartbeat_ttl=?
WHERE id=? AND status='ready-for-executor'
```

If `rowcount=0` (another executor claimed first or status changed), the transaction rolls back and `ClaimRejected` is returned. At claim time, `heartbeat_ttl` is set (derived from `estimated_runtime`, default 300 seconds / 5 minutes).

**Heartbeat (continuous liveness signal):** The executing subagent writes a heartbeat every 60–90 seconds:

```sql
UPDATE uow_registry SET heartbeat_at=?, updated_at=?
WHERE id=? AND status IN ('active', 'executing')
```

`heartbeat_at` is proof-of-life. A silent agent is a stalled agent. The heartbeat is a fire-and-forget update — no transaction held open.

**Stall detection (steward observation loop):** The steward's observation loop runs every 3 minutes. It computes staleness using `heartbeat_at` if non-null, falling back to `started_at` for UoWs that predate the migration:

```python
reference = uow.heartbeat_at or uow.started_at
staleness_seconds = (now - reference).total_seconds()
if staleness_seconds > uow.heartbeat_ttl:
    registry.record_stall_detected(uow.id, stall_type="heartbeat")
```

A stall transitions the UoW from `active` → `ready-for-steward` for re-diagnosis. The `stall_type` field distinguishes heartbeat stalls (agent crash / network failure → re-execute) from TTL stalls (legacy path for pre-migration UoWs).

**Orphan safety net:** The executor heartbeat retains `recover_ttl_exceeded_uows()` with a **24h threshold** (not 4h) as a last-resort backstop for UoWs that evade the observation loop. This fires only on definitively orphaned UoWs.

**Summary:** Claim is atomic (optimistic lock), execution is continuously observed (heartbeat), release is transactional (`write_result`). The 4-hour fixed TTL has been retired as the primary mechanism; it lives only as a 24h orphan safety net.

The heartbeat's `_filter_stale_uows` adds a second layer for recovery dispatch: previously-orphaned UoWs require a 5-minute staleness gate before the heartbeat attempts recovery dispatch, avoiding races with the primary event-driven path.

### 2. Recurrence / trigger for each component

| Component | Trigger / Frequency |
|-----------|---------------------|
| **GardenCaretaker** | Every 15 min via cron (`*/15 * * * *`) — Type C, cron-direct |
| **github-issue-cultivator** | Daily at 06:00 UTC (`0 6 * * *`) — LLM scheduled job |
| **Steward Heartbeat** | Every 3 min via cron (`*/3 * * * *`), offset 0s |
| **Executor Heartbeat** | Every 3 min via cron (`*/3 * * * *`), offset +90s from steward |
| **Germinator** | Synchronous on every new UoW insert — not polled |
| **Routing Classifier** | Synchronous on every new UoW insert — not polled |
| **Negentropic Sweep** | Daily at 02:00 UTC (`0 2 * * *`) — LLM scheduled job |
| **Lobster Dispatcher** | Event-driven — picks up `wos_execute` messages within seconds |

### 3. Where is the Cultivator?

There are two ingest components with related roles:

- **`src/orchestration/cultivator.py`** — The original cultivator module (pure Python, standalone). Located at `~/lobster/src/orchestration/cultivator.py`.
- **`scheduled-tasks/garden-caretaker.py`** — The active heartbeat script that replaced the split `cultivator.py` / `issue-sweeper.py` responsibility. It runs every 15 minutes and calls `GardenCaretaker.run_reconciliation_cycle()` from `src/orchestration/garden_caretaker.py`.
- **`scheduled-tasks/tasks/github-issue-cultivator.md`** — A daily LLM job that performs the full fetch-classify-promote cycle as a language model task (daily at 06:00 UTC).

In production, the GardenCaretaker heartbeat is the live polling component; the LLM cultivator job handles deeper classification requiring language model judgment.

### 4. Why is the executor on a heartbeat, and how does it relate to the cultivator?

The **primary dispatch path is not the heartbeat** — it is event-driven. When the Steward prescribes a UoW, it transitions to `ready-for-executor` and the `_dispatch_via_inbox` function writes a `wos_execute` message directly to `~/messages/inbox/`. The Lobster dispatcher picks this up on its next cycle (seconds, not minutes).

The executor heartbeat exists as a **recovery net**, not the primary trigger. Its two roles are: (1) orphan safety net — marking UoWs stuck in `active`/`executing` for more than 24 hours as `failed` (last-resort backstop; primary stall detection is the steward's observation loop using heartbeat signals), and (2) recovery dispatch — catching `ready-for-executor` UoWs that the primary event-driven path missed (e.g., due to dispatcher downtime). The heartbeat deliberately runs 90 seconds after the steward heartbeat to give the steward time to prescribe before the executor checks for ready UoWs.

The cultivator (GardenCaretaker) and executor operate at different pipeline stages and are fully decoupled. The cultivator populates the registry with new UoWs from GitHub; the executor dispatches them only after the Steward has diagnosed and prescribed. They do not call each other.

### 5. How does this work for design sweeps?

The **Negentropic Sweep** is a daily LLM scheduled job (`negentropic-sweep`, runs at 02:00 UTC) that performs hygiene analysis across the codebase and surfaces findings. It is **not a WOS executor job** — it runs via the LLM dispatch path directly (jobs.json `dispatch: "llm"`) and writes its output to `~/lobster-workspace/hygiene/YYYY-MM-DD-sweep.md`.

For **design-type UoWs** entering the WOS pipeline, the register classifier routes them to:
- `philosophical` register — if phenomenological vocabulary is detected (poiesis, frontier, pearl). Routed to a **frontier-writer** subagent that produces synthesis output rather than code.
- `human-judgment` register — if success criteria contain hedge words. Routed to a **design-review** subagent that produces structured analysis for Dan's review; the Steward surfaces these to Dan and cannot machine-close them.

The `sequential` posture (assigned by the routing classifier for seed-type work) represents a design-first pattern where multiple agents run in defined sequence — e.g., a design agent followed by an implementation agent.

### 6. Lifecycle stages — seed, pearl, heat, and shit

See also: the **Metabolic Output Layer** subgraph in the Pipeline Flowchart above, and the Outcome Categories table below. The formal `outcome_refs` schema is specified in [Issue #880](https://github.com/dcetlin/Lobster/issues/880).

WOS uses two complementary vocabularies for lifecycle:

**Biological vocabulary (design/philosophy register):**
- **Seed** — an observation or idea that resolves to executable work. Becomes a GitHub issue at germination. In `write_result`, `outcome_category: "seed"` means intentional investment in future capability (infra fixes, tooling, instrumentation).
- **Pearl** — a recognition event that is already complete — not needing execution. A philosophy session that produced a settled frontier document is a pearl, not a seed. In `write_result`, `outcome_category: "pearl"` means direct high-value output (bugs caught, frameworks encoded, analysis acted on).
- **Heat** — `outcome_category: "heat"` means pure dissipation, no residue (empty checks, healthy no-ops). Work happened but left no artifact.
- **Shit** — `outcome_category: "shit"` means organic waste that persists and must be processed (stale notes, unread accumulation). The output creates future cleanup work.

**Operational status vocabulary (registry states):**

`proposed` → `pending` → `ready-for-steward` → `ready-for-executor` → `active` → `executing` → `ready-for-steward` (loop) → `done` | `failed` | `blocked` | `expired` | `cancelled`

The biological terms name the *character* of a UoW; the operational statuses track its *position in the pipeline*. Both registers apply simultaneously and are not competing.

### 7. Feedback loops in the system

See the "Feedback Loops" section above for the five explicit loops: Steward re-prescription, Oracle→fix→re-oracle, TTL recovery, startup sweep, and GardenCaretaker tend. The **represcription loop** is the most active: every completed subagent execution returns the UoW to the Steward for evaluation, and the Steward re-prescribes until success criteria are satisfied. The `steward_cycles` counter tracks depth; UoWs with `steward_cycles > 1` are escalated to a more capable model (Opus) for re-diagnosis.

### 8. Comparison to mitochondria modeling explorations

A 2026-04-07 philosophy session (`~/lobster-workspace/philosophy-explore/2026-04-07-1600-philosophy-explore.md`) developed an explicit structural isomorphism between WOS and mitochondrial governor-timing models. The central claim: WOS instantiates five rhythmic cycles (nightly consolidation, weekly sweep, RALPH cadence, WOS corrective-trace spacing, dispatcher hibernation) but they arrived through ad hoc engineering decisions — scheduled triggers, not permission gates. A **mitochondrial governor** decides whether to permit an action based on the current state of the whole system; the Lobster cron scheduler fires regardless of current system state.

The parallel to the pipeline diagram: WOS has the cycles (the heartbeats, the sweeps, the steward/executor loop) but lacks the **circadian coordination layer** that would know which cycle is due, prevent cycle collisions, and gate execution during repair windows. The 252-UoW overnight failure (250 of 252 failed) is cited as evidence of this gap: individual cycles were sound, but the closure gate was not coordinated with the dispatch rate at scale. The mitochondria modeling exploration names this as a Discernment-to-Attunement transition — the system can stumble into coherent cycles (Stage 2) but cannot yet sustain coordinated rhythm under load (Stage 3). The practical implication for the diagram: the `wos-config.json` execution gate (`wos start/stop`) is a manual approximation of governor control, not a sensing apparatus. A genuine rhythmic governance layer would replace it.

---

## Component Legend

| Component | File | Role |
|-----------|------|------|
| **GardenCaretaker** | `src/orchestration/garden_caretaker.py` | Unified scan-and-tend loop. Every 15 min: discovers new issues (scan) and reconciles active UoW bindings against source state (tend). Replaces the original split between cultivator.py and issue-sweeper.py. |
| **github-issue-cultivator** | `scheduled-tasks/tasks/github-issue-cultivator.md` | Daily LLM job (06:00 UTC). Fetches all open GitHub issues, applies skip conditions (meta-tracking labels, existing active UoWs), assigns priority, and promotes to WOS registry. |
| **Germinator** | `src/orchestration/germinator.py` | Classifies the attentional *register* of each UoW at germination time using a 4-gate ordered algorithm. Register is immutable after germination. Runs synchronously on insert. |
| **Routing Classifier** | `src/orchestration/routing_classifier.py` | Loads `~/lobster-user-config/orchestration/classifier.yaml` and applies first-match-wins rules to assign a *posture* (solo, sequential, review-loop, fan-out) and a `route_reason`. Falls back to `solo` if classifier YAML is absent. Runs synchronously on insert. |
| **Registry** | `src/orchestration/registry.py` | SQLite-backed UoW store. `_upsert_typed` inserts new UoWs with `register`, `posture`, and `route_reason` fields; idempotent on active UoWs. |
| **Steward Heartbeat** | `scheduled-tasks/steward-heartbeat.py` | Every 3 min via cron. Runs startup sweep (orphan recovery), observation loop (heartbeat stall detection — compares `heartbeat_at` or `started_at` against `heartbeat_ttl`), and Steward main loop (diagnose → prescribe/close/surface). |
| **Executor Heartbeat** | `scheduled-tasks/executor-heartbeat.py` | Every 3 min via cron (+90s offset). Checks `wos-config.json` execution gate, runs orphan safety net (`recover_ttl_exceeded_uows`, 24h threshold — Phase 1, always), then recovery-dispatches ready UoWs missed by the primary event-driven path (Phase 2). Primary stall detection is the steward's heartbeat observation loop, not this component. |
| **Executor** | `src/orchestration/executor.py` | Performs the 6-step atomic claim sequence (optimistic lock on `ready-for-executor` → `active`). Primary path: writes `wos_execute` inbox message and returns immediately (async/event-driven). Legacy path: `claude -p` subprocess (CI/dev). |
| **Register-Appropriate Subagent** | Dispatched by Lobster | functional-engineer (code), frontier-writer (philosophical synthesis), design-review (human-judgment analysis). Executes the UoW, writes `result.json` and `trace.json`. |
| **Oracle** | `oracle/verdicts/` | Reviews PR diffs and writes APPROVED / NEEDS_CHANGES verdicts to `oracle/verdicts/pr-NNN.md`. PR Merge Gate requires an APPROVED verdict before merge. |
| **Steward** | `src/orchestration/steward.py` | Evaluates completed UoWs against success criteria, diagnoses failures, re-prescribes, or surfaces to Dan. The only component authorized to mark a UoW `done`. |

---

## Register Types

| Register | Meaning |
|----------|---------|
| `operational` | Deterministic, machine-verifiable success criterion |
| `iterative-convergent` | Requires repeated execution until a gate command passes |
| `philosophical` | Requires Dan's attentional presence; originates from philosophy/frontier sessions |
| `human-judgment` | Success criteria contain hedge words; cannot be evaluated without reading output |

## Posture Types

| Posture | Meaning |
|---------|---------|
| `solo` | Single subagent executes end-to-end |
| `sequential` | Multiple agents in a defined sequence (design-first pattern) |
| `review-loop` | Execution followed by oracle review loop |
| `fan-out` | Work decomposed into parallel subagent tasks |

## Outcome Categories (write_result metabolic tags)

Spec: [Issue #880](https://github.com/dcetlin/Lobster/issues/880) — `outcome_refs` schema and traversal contract.

The metabolic primitives are **typed stock** that move through flows after UoW execution. Seeds and pearls carry addresses (refs) that make them machine-traceable; heat and shit are energy expenditure with log entries only — no downstream stock is created.

| Category | Stock type | `outcome_refs` required? | Downstream flow |
|----------|------------|--------------------------|-----------------|
| `pearl` | Artifact delivered | Yes — PR URL, file path, or doc link | System state changes; observable in repo/docs |
| `seed` | Future work spawned | Yes — GitHub Issue URL | New Issue → New UoW via Cultivator (generative loop) |
| `heat` | Pure dissipation | No | Log entry only; no artifact, no issue |
| `shit` | Organic waste | No | Waste account entry; signals future cleanup work |

**Key principle:** seeds and pearls are stock with addresses (refs); heat and shit are energy with logs (no downstream stock).

## UoW Lifecycle States

```
proposed → pending → ready-for-steward → ready-for-executor → active → executing
                          ↑                                                  │
                          └──────────── (Steward re-prescription loop) ──────┘
                                                                             │
                               done ← ready-for-steward ← result.json written
                               failed / blocked / expired / cancelled (terminal)
```
