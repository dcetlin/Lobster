---
status: design spec
occasion: Implementation sequencing document for WOS five structural leaps
created: 2026-05-29
prerequisite_reading: wos-bold-evolution.md, sia-hexo-synthesis.md
---

# WOS Evolution Spec — Design and Implementation Sequencing

*Produced 2026-05-29. Covers five structural leaps: Adaptive Steward, Event-Native Nervous System, Executor Mesh, Closed-Loop Self-Amendment, Orientation Layer. Grounded in current WOS codebase (src/orchestration/) and the architectural documents in docs/.*

---

## §1 End State Definition

The end state is a **self-orienting, self-amending, distributed task execution system** in which Dan's cognitive role is reviewer and orientation-setter rather than prescriber and debugger. The system maintains a learning ledger of its own diagnostic accuracy across registers, responds to work events within seconds rather than polling on a 3-minute clock, routes tasks across a typed mesh of execution contexts with different capability profiles, proposes its own behavioral amendments in bounded authority classes, and maintains an ongoing portfolio prescription that aligns its workload with an explicit telos rather than processing whatever the backlog contains.

This is what "autopoietic task execution" means concretely: the system's metabolism — cultivation of issues, germination into UoWs, prescription by the steward, execution by the executor mesh, closure, and metabolic output classification — operates as a self-renewing loop that improves without requiring Dan to author each improvement. Dan's role at full build-out is: set orientation (the Governor layer), approve or reject the system's own amendment proposals (Class B Self-Amendment), and handle the residual human-judgment register UoWs that the system correctly surfaces.

### Observable Signals of the End State

- `verdict_accumulator` table contains at minimum 50 scored prescription hypothesis outcomes per register, with Prescriber accuracy trending upward.
- Mean time from issue-open to germination: **under 60 seconds** (event-driven path active).
- Mean time from UoW completion to next-UoW dispatch: **under 15 seconds** (capacity event fires immediately).
- At least two executor tiers active: Local Claude subagents + one persistent specialized-context agent.
- Self-amendment log non-empty: at least one Class A amendment applied automatically with Dan's retroactive review.
- Portfolio prescription visible as a readable, auditable document.
- Register classification accuracy above 90% (mismatch rate below 10% on 30-day rolling window).

---

## §2 Architecture Diagrams

### Current Architecture

```
CLOCK-DRIVEN POLLING LOOP (current)

  GitHub Issues
       │
       ▼ (every 15 min — GardenCaretaker / daily — github-issue-cultivator)
  ┌─────────────────┐
  │   Cultivator    │  promotes GitHub issues → uow_registry (proposed)
  └────────┬────────┘
           │ auto-advance
           ▼
  ┌─────────────────┐
  │   Germinator    │  classify_register() — 4-gate ordered algorithm
  │                 │  register: operational | iterative-convergent |
  │                 │            philosophical | human-judgment
  └────────┬────────┘
           │ → ready-for-steward
           ▼
  ┌─────────────────────────────────────────┐
  │       STEWARD HEARTBEAT (every 3 min)   │
  │  Phase 0: Stale agent cleanup           │
  │  Phase 1: Startup sweep (orphan)        │
  │  Phase 2: Observation loop + stall      │
  │  Phase 3: LLM prescription (Sonnet/Opus)│
  │  Phase 4: Post-completion GitHub sync   │
  └────────┬────────────────────────────────┘
           ▼
  ┌──────────────────────────────────────────┐
  │       EXECUTOR HEARTBEAT (every 3 min)   │
  │  execution_enabled + scaling_governor    │
  │  → wos_execute inbox message             │
  │  → Lobster dispatcher spawns subagent    │
  └────────┬─────────────────────────────────┘
           ▼
  Claude Subagent → Oracle Gate → write_result()
  → Metabolic Classification (pearl/seed/heat/shit)
  → Steward closes or re-prescribes

REGISTRY: SQLite (wos.db) — uow_registry, audit_log, dispatch_skip_log
METRICS:  SQLite (wos-metrics.db) — prescription_events, closure_events
```

### Target Architecture (All Five Layers)

```
EVENT-NATIVE SELF-ORIENTING MESH (target)

  GitHub Webhooks / 30s delta poller
       │ wos_issue_created (typed inbox event)
       ▼
  ┌───────────────────────────────────────────┐
  │         GOVERNOR (Orientation Layer)      │
  │  Reads: workstreams, health, priorities   │
  │  Writes: portfolio_prescription           │
  │          germination_bias                 │
  │  Paired: Socratic advisor (no directives) │
  └─────────────┬─────────────────────────────┘
                │ germination_bias
                ▼
  ┌──────────────────────────────────────────────────┐
  │       ADAPTIVE GERMINATOR + ADAPTIVE STEWARD     │
  │  Prescriber → PrescriptionObject (typed)         │
  │  Selector → reads verdict_accumulator top-5      │
  │  Verdict Accumulator → (register, hypothesis)    │
  │             → (n_successes, n_failures, n_partial)│
  └─────────────┬────────────────────────────────────┘
                │ wos_uow_completed (typed event)
                ▼
  ┌──────────────────────────────────────────────────┐
  │              EXECUTOR MESH                        │
  │  Tier 1: Local Claude subagents (ephemeral)      │
  │  Tier 2: Persistent specialized agents           │
  │  Tier 3: External executor contract protocol     │
  │  wos_capacity_available event on slot free       │
  └─────────────┬────────────────────────────────────┘
                ▼
  ┌──────────────────────────────────────────────────┐
  │           SELF-AMENDMENT COMPONENT                │
  │  Class A: IFTTT rule changes (auto-apply)        │
  │  Class B: Dan approval required                  │
  │  GUARD: Class A cannot modify amendment logic    │
  └──────────────────────────────────────────────────┘

NEW TABLES: verdict_accumulator, prescription_hypothesis_log,
            amendment_log, executor_registry, event_log,
            portfolio_prescription, germination_bias
EVENT BUS: typed inbox messages (file-based, existing protocol extended)
```

---

## §3 Spine Layer Design

### I. Adaptive Steward (Priority 1)

**New typed output: PrescriptionObject**

```python
@dataclass(frozen=True)
class PrescriptionObject:
    uow_id: str
    register: UoWRegister
    diagnosis_hypothesis: str        # max 140 chars — machine-comparable
    proposed_steps: list[str]
    confidence: float                # 0.0–1.0
    counterfactual_question: str     # "what would falsify this?"
    generated_at: str
    selector_priors: list[str]       # top-5 prior hypothesis IDs
```

**New schema tables:**

```sql
CREATE TABLE IF NOT EXISTS verdict_accumulator (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    register             TEXT    NOT NULL,
    diagnosis_hypothesis TEXT    NOT NULL,
    n_successes          INTEGER NOT NULL DEFAULT 0,
    n_failures           INTEGER NOT NULL DEFAULT 0,
    n_partial            INTEGER NOT NULL DEFAULT 0,
    last_updated         TEXT    NOT NULL,
    UNIQUE(register, diagnosis_hypothesis)
);

CREATE TABLE IF NOT EXISTS prescription_hypothesis_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uow_id       TEXT    NOT NULL,
    hypothesis   TEXT    NOT NULL,
    register     TEXT    NOT NULL,
    generated_at TEXT    NOT NULL,
    outcome      TEXT    NULL,
    scored_at    TEXT    NULL
);
```

**Selector:** Pure SQL query on `verdict_accumulator` ordered by success rate. Injects top-5 priors into prescription prompt. No LLM call required.

**Verdict hook:** `score_prescription_hypothesis(uow_id, outcome)` called in `_close_uow`. Idempotent (checks `scored_at IS NULL`). Non-fatal on DB error.

**Test harness (no LLM required):** Unit tests for accumulator upsert, Selector retrieval query, PrescriptionObject serialization. Integration test: close UoW → assert n_successes incremented.

**Key files:** `src/orchestration/steward.py`, `src/orchestration/schema.sql`, `src/orchestration/registry.py`, `src/orchestration/prescription_metrics.py`, `scripts/upgrade.sh`.

---

### II. Event-Native Nervous System (Priority 3, parallel with Stage 1)

**Three new typed inbox message types:**

```
wos_issue_created:    issue_number, issue_url, title, labels, triggered_at
wos_uow_completed:    uow_id, outcome, register, output_ref, triggered_at
wos_capacity_available: freed_uow_id, freed_at, current_active_count, max_parallel
```

**New module:** `src/orchestration/wos_events.py` — three pure emit functions writing JSON to `~/messages/inbox/`.

**Issue delta poller:** `scheduled-tasks/wos-issue-delta-poller.py` (Type B cron, every 30 seconds). Reads GitHub since-cursor, emits `wos_issue_created` on delta.

**Heartbeat demotion:** Behind `event_native_active: true` feature flag in `wos-config.json`. Steward heartbeat: `*/3` → `*/5` (orphan recovery only). Executor heartbeat: unchanged. GardenCaretaker: retained as reconciliation backstop.

**New schema:** `event_log` table.

**Key files:** `src/orchestration/wos_events.py` (new), `scheduled-tasks/wos-issue-delta-poller.py` (new), `src/orchestration/dispatcher_handlers.py`, `src/orchestration/result_writer.py`, `src/orchestration/executor.py`.

---

### III. Executor Mesh (Priority 4, after learning layer)

**Claim Protocol:**

```python
@dataclass(frozen=True)
class ExecutorCapabilityProfile:
    executor_id: str
    supported_registers: list[str]
    supported_postures: list[str]
    max_concurrent: int
    capability_tags: list[str]
    claim_endpoint: str | None    # None = local subagent

@dataclass(frozen=True)
class ClaimResponse:
    accepted: bool
    executor_id: str
    reason: str | None
```

**Tier 2 agent design:** Long-running Claude Code session with domain-scoped bootup file. Writes state file after each task; reads on activation. Domain: `tier2-lobster-codebase` (knows lobster codebase, accumulates architectural patterns).

**Tier 3:** Publish-subscribe on existing inbox protocol. UoW appears in `wos-proposals/` directory. First valid claim within 10-second window wins (optimistic lock).

**New schema:** `executor_registry` table.

**New module:** `src/orchestration/executor_mesh.py`.

---

### IV. Closed-Loop Self-Amendment (Priority 2)

**Amendment classes:**
- **Class A** (auto-apply): IFTTT rule changes, routing config updates. Bounded to `CLASS_A_ALLOWED_COMPONENTS = frozenset({"ifttt_rule", "routing_config"})`. Structural guard: PermissionError if out-of-scope component attempted.
- **Class B** (Dan approval): All other changes. Surfaced via Telegram with Yes/No buttons. Proposal includes rationale, evidence, specific before/after change.

**Detection rules:**
1. IFTTT rule overridden 4+ times in 30 days → propose deletion (Class A)
2. Label combination routes to register X but 70%+ steward-escalation rate → propose germinator pre-filter (Class B)
3. Same `diagnosis_hypothesis` in 3+ consecutive failures for same register → prescription recycling flag (Class B)

**New schema:** `amendment_log` table.

**New modules:** `src/orchestration/self_amendment.py`, `scheduled-tasks/self-amendment-generator.py` (weekly cron).

---

### V. Orientation Layer (Priority 5, last)

**Governor output:** `PortfolioPrescription` — workstream emphasis weights, register targets, suppressed workstreams, Socratic exchange record.

**Socratic advisor:** Question-only session (Haiku). Structurally prevented from issuing directives (system prompt constraint + optional output validation). Must pose minimum 3 questions before Governor finalizes prescription.

**Germination bias:** `GerminationBias` per workstream (0.1 = suppressed, 1.0 = neutral, 2.0 = accelerated). Applied by Germinator at issue promotion time.

**New schema:** `portfolio_prescription`, `germination_bias` tables.

**New modules:** `src/orchestration/governor.py`, `src/orchestration/socratic_advisor.py`, `scheduled-tasks/governor-weekly.py` (weekly LLM task).

---

## §4 Load-Bearing Abstractions

| Term | Definition |
|------|-----------|
| **Prescription** | Typed PrescriptionObject output by Prescriber (not prose string) |
| **Verdict** | Scored outcome for a specific hypothesis after UoW closes |
| **Verdict Accumulator** | Persistent store: (register, hypothesis) → (n_successes, n_failures, n_partial) |
| **Selector** | Component that reads Verdict Accumulator and biases Prescriber toward high-accuracy hypothesis types |
| **Portfolio Prescription** | Governor output: workstream emphasis weights + register targets (portfolio-level, not UoW-level) |
| **Germination Bias** | Per-workstream multiplier on issue promotion speed (derived from portfolio prescription) |
| **Lever** | Category of improvement action: scaffold lever (IFTTT/config), learning lever (selector biasing) |
| **Class A / Class B Amendment** | A = auto-apply within bounded scope; B = requires Dan's explicit approval |
| **Executor Mesh** | Multi-tier execution population: Tier 1 (ephemeral), Tier 2 (persistent domain), Tier 3 (external) |
| **Claim Protocol** | Typed interface for executor capability matching and UoW claiming |
| **Capacity Event** | `wos_capacity_available` inbox message fired when executor slot frees |
| **Coupled Goodhart** | Failure mode where Prescriber and Executor optimize against same verdict context (Nash equilibrium, not true improvement) |

---

## §5 Implementation Sequencing

| Stage | Evolution | Prerequisite | Estimate | Gate Condition |
|-------|-----------|-------------|----------|----------------|
| 1 | Adaptive Steward | None | 3–5 days | verdict_accumulator has 20+ scored outcomes |
| 2 | Event-Native (parallel with 1) | None | 2–3 days | 10 consecutive event-driven completions without heartbeat gap |
| 3 | Self-Amendment Class A | Stage 1 complete | 3–4 days | At least 1 Class A amendment applied and Dan-reviewed |
| 4 | Executor Mesh Tiers 1+2 | Stages 1–3 stable | 4–6 days | Tier 2 agent handles 20+ UoWs; capability matching proven |
| 5 | Orientation Layer | All prior + 90d verdict data | 5–7 days | First portfolio prescription reviewed and approved by Dan |

---

## §6 Design Decision Forks

Three blocking forks require Dan's input. Two non-blocking forks are decided by the implementation team.

### Fork 1 — Verdict Matching Strategy (BLOCKING, needed before Stage 1)

- **Option A:** Exact string match (normalized). Simple, instant, testable. Risk: synonym fragmentation.
- **Option B:** Semantic clustering via embedding similarity. Better learning signal. Requires vector infrastructure.
- **Option C:** LLM-assisted normalization at scoring time. Most expensive.

**Dan's question:** Start with exact match (A) and migrate to embedding clustering (B) when fragmentation is observed, or build B now so the ledger starts clean?

### Fork 2 — Tier 2 Agent State Persistence (BLOCKING, needed before Stage 4)

- **Option A:** Isolated state file (JSON per domain, no shared infrastructure). Simple, bounded.
- **Option B:** Shared memory DB with domain partition field. Cross-pollination possible.
- **Option C:** Dedicated memory partition (migration required). Cleanest isolation.

**Dan's question:** Should Tier 2 domain knowledge be accessible to the dispatcher and other agents (Option B) or strictly isolated to the Tier 2 agent (Option A)?

### Fork 3 — Governor Authority Model (BLOCKING, needed before Stage 5)

- **Option A:** Promote-only. Governor can accelerate germination but cannot suppress.
- **Option B:** Full bias authority (promote and suppress within defined bounds).
- **Option C:** Advisory only. Governor produces a readable prescription; Dan manually adjusts.

**Dan's question:** This is the most philosophically load-bearing decision in the spec. How much autonomous authority should the Governor have over work prioritization? Recommend starting with Option A and expanding authority as trust develops — but this requires your explicit input.

### Fork 4 — Event Bus Transport (Non-blocking)

**Decision:** File-based inbox protocol (Option A). Durability over sub-second latency.

### Fork 5 — Socratic Advisor Constraint Enforcement (Non-blocking)

**Decision:** System prompt constraint only (Option A) with output validation (Option B) added if constraint breaks in practice.

---

## §7 Claude API and Model Usage Design

| Component | Model Tier | Prompt Caching |
|-----------|-----------|----------------|
| Prescriber (first attempt) | Sonnet | Yes — static system prompt cached |
| Prescriber (escalation, cycles > 1) | Opus | Yes |
| Selector retrieval | None (SQL) | N/A |
| Verdict normalization (if Option C) | Haiku | Yes |
| Governor | Sonnet | Yes — static context cached |
| Socratic advisor | Haiku | Yes — no-directive system prompt cached |
| Self-amendment generator | Sonnet | Yes — detection rules cached |
| Executor Tier 1 | Sonnet | Unchanged |
| Executor Tier 2 | Sonnet / Opus (escalation) | Yes — domain bootup file cached |

**Caching pattern (all new components):**

```python
messages = [{
    "role": "user",
    "content": [
        {
            "type": "text",
            "text": STATIC_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"}
        },
        {
            "type": "text",
            "text": dynamic_uow_context  # not cached
        }
    ]
}]
```

**Scaffold testing isolation:** Every new component has a pure-Python non-LLM path. LLM calls are isolated in named functions (`_call_llm_prescriber`, `_generate_portfolio_prescription`, etc.) that are mockable in unit tests. All unit tests pass without an API key. Integration tests requiring an API key are clearly marked.

**Model config in `wos-config.json`:**

```json
{
    "prescription_model": "opus",
    "governor_model": "sonnet",
    "socratic_advisor_model": "haiku",
    "amendment_generator_model": "sonnet"
}
```

---

## What Is Needed From Dan Before Implementation

**Stages 1, 2, and 3 can begin immediately** — no blocking forks apply.

**Blocking inputs required:**
1. **Fork 1** (verdict matching): exact match or embedding clustering from the start? (needed before Stage 1 commit)
2. **Fork 2** (Tier 2 state): isolated state file or shared memory partition? (needed before Stage 4)
3. **Fork 3** (Governor authority): promote-only, full bias, or advisory? (needed before Stage 5 — this is the most important)

---

*Design spec complete. No implementation changes to production WOS. Bisque URL: http://5.78.201.64:9101/files/wos-evolution-spec.html*
