# WOS Pipeline Architecture

**Date:** 2026-04-22
**Status:** Canonical
**Scope:** Full cultivator-to-executor pipeline, including germinator register classification, routing classifier posture assignment, and executor dispatch.

---

## Pipeline Flowchart

```mermaid
flowchart TD
    GH["GitHub Issues\n(dcetlin/Lobster)"]
    CULT["Cultivator\ncultivator.py\nfetch → classify-priority → promote"]
    SKIP{Skip condition?}
    SKIP_OUT["Skip\n(meta-tracking or\nalready active UoW)"]
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

    UPSERT["Registry._upsert_typed\nINSERT with register + posture + route_reason\n(idempotent — existing active UoWs not re-inserted)"]

    HB["Executor Heartbeat\n(every 3 min)\nexecutor-heartbeat.py"]
    WOS_GATE{wos-config.json\nexecution_enabled?}
    TTL["TTL Recovery\nrecover_ttl_exceeded_uows()\nfails stuck 'active' UoWs"]
    DISPATCH["Executor\nexecutor.py\n6-step atomic claim sequence\nwrites wos_execute message to inbox"]
    LOBSTER["Lobster Dispatcher\npicks up wos_execute message\non next cycle"]
    SUBAGENT["Functional-Engineer Subagent\n(or register-appropriate agent)\nexecutes UoW"]
    RESULT["result.json written\n(complete / partial / failed / blocked)"]
    ORACLE["Oracle Review\noracle/\nPR diff reviewed by oracle agent\nverdict written to oracle/decisions.md"]
    ORACLE_VERDICT{Verdict?}
    MERGE["Merge Agent\nmerges PR"]
    FIX["Fix Agent\naddresses NEEDS_CHANGES\nthen re-oracle"]
    DONE["UoW marked done\n(Steward declares closure)"]

    GH --> CULT
    CULT --> SKIP
    SKIP -->|Yes| SKIP_OUT
    SKIP -->|No| GERM
    GERM --> GERM_GATES
    GERM_GATES --> RC
    RC --> RC_RULES
    RC_RULES --> UPSERT
    UPSERT --> HB
    HB --> WOS_GATE
    WOS_GATE -->|No| SKIP_OUT2["Dispatch skipped\n(TTL recovery still runs)"]
    WOS_GATE -->|Yes| TTL
    TTL --> DISPATCH
    DISPATCH --> LOBSTER
    LOBSTER --> SUBAGENT
    SUBAGENT --> RESULT
    RESULT --> ORACLE
    ORACLE --> ORACLE_VERDICT
    ORACLE_VERDICT -->|APPROVED| MERGE
    ORACLE_VERDICT -->|NEEDS_CHANGES| FIX
    FIX --> ORACLE
    MERGE --> DONE
```

---

## Component Legend

| Component | File | Role |
|-----------|------|------|
| **Cultivator** | `src/orchestration/cultivator.py` | Fetches all open GitHub issues, applies skip conditions (meta-tracking labels, existing active UoWs), assigns priority (high/medium/low from labels), and promotes to WOS registry. |
| **Germinator** | `src/orchestration/germinator.py` | Classifies the attentional *register* of each UoW at germination time using a 4-gate ordered algorithm. Register is immutable after germination. |
| **Routing Classifier** | `src/orchestration/routing_classifier.py` | Loads `~/lobster-user-config/orchestration/classifier.yaml` and applies first-match-wins rules to assign a *posture* (solo, sequential, review-loop, fan-out) and a `route_reason`. Falls back to `solo` if classifier YAML is absent. |
| **Registry** | `src/orchestration/registry.py` | SQLite-backed UoW store. `_upsert_typed` inserts new UoWs with `register`, `posture`, and `route_reason` fields; idempotent on active UoWs. |
| **Executor Heartbeat** | `scheduled-tasks/executor-heartbeat.py` | Runs every 3 minutes via cron. Checks `wos-config.json` execution gate, runs TTL recovery, then dispatches ready UoWs via the Executor. |
| **Executor** | `src/orchestration/executor.py` | Performs the 6-step atomic claim sequence (optimistic lock on `ready-for-executor` → `active`). Writes a `wos_execute` inbox message; the Lobster dispatcher spawns the subagent. |
| **Functional-Engineer Subagent** | Dispatched by Lobster | Executes the UoW, writes `result.json`. Register-specific agent routing is applied at dispatch time. |
| **Oracle** | `oracle/` | Reviews PR diffs and writes APPROVED / NEEDS_CHANGES verdicts to `oracle/decisions.md`. PR Merge Gate requires an APPROVED verdict before merge. |
| **Steward** | `src/orchestration/steward.py` | Evaluates completed UoWs, diagnoses failures, and is the only component authorized to mark a UoW `done`. |

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
