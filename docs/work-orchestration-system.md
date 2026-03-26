# Work Orchestration System
*Design doc — first version: 2026-03-26 (as "Issue Sweeper"). Substantially rewritten: 2026-03-26. Audit-required changes applied: 2026-03-26.*

---

## 1. Vision

When this is working: Dan never wonders what the highest-leverage thing to work on is. He asks and gets a short, ordered list — each item scoped, labeled, and one step from execution. Lobster never duplicates in-flight work or stalls because it can't tell what is already running. META can tell, at any moment, whether the system is actually advancing work or just accumulating representations of it — and when the gap opens, META can surface it with evidence, not guesswork.

The specific failure mode being interrupted: Lobster generates observations, files them as issues, and then the pipeline stalls at "filed." The archive grows. The activity metric (issues added) diverges from the outcome metric (work closed). The system appears busy while delivering nothing. This design installs a **meter** — a structured, auditable path from "observation filed" to "unit of work in motion" to "done and closed" — and makes the pipeline visible at every stage.

Falsifiability test: On any given day, Dan should be able to ask "what's running and what should I work on?" and receive an answer from a single Registry query — no GitHub scanning, no multi-file triage. If that query requires more than one lookup, the system is not functioning.

---

## 2. Core Architectural Principles

These are constraints, not descriptions. Each rules something out.

**1. No silent transitions.**
Every state change in a Unit of Work — creation, routing, start, completion, failure — is written to the audit log before the transition is considered to have happened. A UoW that transitions without an audit entry does not count as transitioned. This rules out: fire-and-forget spawning, status updates that live only in an agent's context, and "soft" completions that are not written back.

**2. The Registry is the single source of truth for what is running.**
The dispatcher never falls back to GitHub polling or file scanning to answer "what's running?" If the Registry does not reflect reality, the Registry is wrong — not the dispatcher's responsibility to compensate. This rules out: agent status living only in the dispatcher's in-context memory, sweeper output files as the primary self-orientation mechanism, and "check GitHub first, then the Registry."

**3. Composability without permission.**
Any agent can create child UoWs, spawn subagents, and report completions. The orchestration system provides scaffolding (Registry, hooks, classifier), not a central coordinator that must be asked before action. This rules out: approval gates between decomposition and execution, agents that must wait for dispatcher acknowledgment before spawning children.

**4. Configuration rules routing; code rules the engine.**
Routing decisions (which posture for which UoW) and hook behaviors (what fires when) live in configuration files, not code. The engine that evaluates rules is code. This rules out: hardcoded routing decisions that require a code change to adjust, and configuration-less hook behaviors.

**5. Consumption gates before accumulation chains.**
No pipeline step that produces output for downstream consumption is complete without a mechanism to detect whether downstream is actually consuming. A sweeper that creates UoWs nobody picks up is not functioning — it is accumulating. Each phase of the system must specify its consumption gate before it is considered shipped. This rules out: measuring system health by inputs (issues scanned, UoWs created) without measuring throughput (UoWs completed and closed).

**6. Autonomy increases are explicit gate crossings.**
The sweeper starts in propose-only mode. Each increase in autonomy level (sweeper writes labels, sweeper creates UoWs, sweeper triggers execution agents) is a named gate that requires explicit confirmation — either via design decision documented here or via a human-gate UoW sent to Dan. This rules out: autonomy level increasing as a side effect of Phase 2 or Phase 3 implementation without an explicit decision point.

---

## 3. The Five Components

### 3.1 UoW Registry

**What it is:** A structured store (flat JSON in Phase 1, SQLite in Phase 2) holding one record per Unit of Work. It is the authoritative live state of all work: pending, active, blocked, done, failed.

**What it does:** Receives writes from all agents on state transitions. Answers dispatcher queries without requiring any other lookup. Maintains parent/child tree structure for fan-out UoWs.

**Interface:**
- *Inputs:* Any agent calls `registry.write(uow_record)` or `registry.update(uow_id, fields)` on create, status change, or completion.
- *Outputs:* Dispatcher (or any agent) calls `registry.query(filters)` to get current state. Returns a list of UoW records matching the filter.

**Schema (per record):**
```json
{
  "id":             "uow_20260326_abc123",
  "type":           "executable | design | research | operational | seed",
  "source":         "github:issue/142 | telegram:msg/1774 | cron:issue-sweeper",
  "status":         "proposed | pending | active | blocked | done | failed | expired",
  "posture":        "solo | fan-out | sequential | review-loop | human-gate",
  "agent":          "subagent-id or null",
  "children":       ["uow_...", "..."],
  "parent":         "uow_... or null",
  "created_at":     "ISO8601",
  "started_at":     "ISO8601 or null",
  "completed_at":   "ISO8601 or null",
  "summary":        "one-line description",
  "output_ref":     "path or URL to output artifact",
  "hooks_applied":  ["hook_id_1", "..."],
  "route_reason":   "classifier output: which rule fired and why (human-readable)",
  "route_evidence": {"rules_fired": ["..."], "scores": {}, "winning_rule": "..."},
  "trigger":        {"type": "immediate | time | condition", "fire_at": "ISO8601 or null", "condition": {}}
}
```

<!-- Added: audit synthesis 2026-03-26 -->
**Status vocabulary:**
- `proposed` — sweeper created this record; awaiting confirmation. Default status in Phase 1.
- `pending` — confirmed for execution; awaiting an agent to claim it.
- `active` — an agent is currently executing this UoW.
- `blocked` — execution paused, awaiting an external condition or human decision.
- `done` — execution complete; output written to `output_ref`.
- `failed` — execution failed; `retry-on-failure` hook may re-queue.
- `expired` — proposed record older than 14 days with no action; excluded from "what's pending?" queries but retained in audit.

The `proposed` / `pending` distinction is load-bearing: the Registry must not mix unconfirmed sweeper proposals with confirmed work queued for execution. "What's pending?" queries filter on `status=pending` only, not `status=proposed`. This prevents the Registry from accumulating phantom pending work that makes "what's running?" unreliable.
<!-- End added: audit synthesis 2026-03-26 -->

**Dispatcher queries (Phase 1 minimum):**
- `"what's running?"` → `status=active`
- `"what's queued?"` → `status=pending`
- `"what just finished?"` → `completed_at > (now - 1h)`
- `"what's stuck?"` → `status=blocked or (status=pending AND age > 3 days)`
- `"did anything fail?"` → `status=failed`

**Failure mode:** Registry drift — a UoW is `active` but no agent is alive for it and no recent audit entry exists. Detection: a sweep hook that checks for `active` records older than 2× the expected task duration and emits a stale-active warning. Without this sweep, orphaned active records accumulate and "what's running?" returns ghost entries.

**Phase 1 implementation:** Flat JSON file at `~/lobster-workspace/orchestration/registry.json`. Written via `src/orchestration/registry_cli.py`, invocable by scheduled subagents via `uv run`. No direct file append — the CLI is the canonical write path for all agents. No database dependency. Read by dispatcher via file read + JSON parse. Phase 1 does not require query performance — the registry will be small.

<!-- Added: audit synthesis 2026-03-26 -->
**Write protocol (required — not optional):** Every Registry state change is a two-phase write: (1) append to `audit.jsonl` first; (2) update the Registry record. On startup or health check, if `audit.jsonl` contains entries with no corresponding Registry update, replay the updates. This enforces Principle 1 (no silent transitions) structurally rather than by convention. The `registry_cli.py` CLI enforces this protocol so no agent can bypass it.

**Concurrent write safety (required — not optional):** `registry_cli.py` uses `fcntl.flock` for advisory locking on all writes. Writes are atomic: write to `registry.json.tmp`, then `os.rename()` to `registry.json` (atomic on Linux). Before every write, copy `registry.json` to `registry.json.bak`. Recovery path for corruption: detect empty/unparseable file on startup, restore from `.bak`, alert Dan via Telegram.

**UoW deduplication (required — not optional):** Before creating a UoW for a GitHub issue, the Registry CLI checks for existing records with `source=github:issue/N` and `status NOT IN (done, failed, expired)`. If found, skip creation and log the skip. This prevents daily duplicate UoWs for issues that remain in `proposed` state between sweep runs.

**Directory initialization:** `~/lobster-workspace/orchestration/` is created by `scripts/upgrade.sh` migration step. The `registry_cli.py` also performs `mkdir -p` on startup to handle manual installs.
<!-- End added: audit synthesis 2026-03-26 -->

---

### 3.2 Routing Classifier

**What it is:** A rule engine that evaluates incoming UoW properties and assigns an execution posture. Rules are YAML configuration. The engine is a Python evaluator. Phase 1 uses first-match-wins semantics; Phase 3 can add weighted scoring without a registry schema change.

**What it does:** Takes a UoW record with its properties, evaluates rules in priority order, and returns a posture assignment plus `route_reason` (which rule fired and why). Updates the UoW record in the Registry with posture and route_reason.

**Interface:**
- *Input:* UoW record (at creation time, or on re-routing request).
- *Output:* Updated UoW record with `posture` and `route_reason` set.

**Execution postures:**

| Posture | When used | What happens |
|---------|-----------|--------------|
| `solo` | Clear scope, low risk, single domain | One subagent runs to completion |
| `fan-out` | Independent subtasks; parallelism safe | Decomposition agent creates N child UoWs; N subagents run; convergence agent synthesizes |
| `sequential` | Steps have hard dependencies | Subagents run in order; each writes output for next |
| `review-loop` | High risk or output needs validation | Subagent produces draft; review agent validates; either done or re-queued |
| `human-gate` | Requires Dan's decision before proceeding | UoW pauses; Telegram ping sent; resumes on Dan's reply |

**Rule configuration (YAML at `~/lobster-user-config/orchestration/classifier.yaml`):**
```yaml
rules:
  - id: design-first
    condition:
      type: seed
    posture: sequential
    route_next: design-agent
    priority: 10

  - id: high-risk-review
    condition:
      risk: high
    posture: review-loop
    priority: 9

  - id: parallelizable-multifile
    condition:
      files_touched: "> 5"
      type: executable
    posture: fan-out
    priority: 8

  - id: default
    condition: {}
    posture: solo
    priority: 0
```

**Failure mode:** Systematic misrouting — a class of UoWs is consistently routed to the wrong posture (e.g., solo-routed UoWs that reliably need review-loop). Detection: META reviews `route_reason` field distributions in the audit log weekly. Refinement trigger: if the same rule fires on >30% of UoWs but those UoWs have a >40% stall or failure rate, that rule is misspecified and should be revised.

**Phase 1 implementation:** Classifier is not present in Phase 1. All UoWs default to `solo` posture. `route_reason` field is set to `"phase1-default: no classifier"`. This is explicit and auditable, not a quiet omission.

---

### 3.3 Conditional Hook System

**What it is:** An event-driven side-effect system. Hooks are IF/THEN rules that fire on UoW state transitions and trigger actions beyond posture changes — notifications, re-queuing, triggering dependent work.

**What it does:** Evaluates hook conditions at each trigger point (classify-time, status-transition, post-completion, temporal). When a condition matches, fires the specified action. Actions are structured (not free-form): `notify`, `re-queue`, `escalate`, `trigger-agent`, `apply-label`.

**Interface:**
- *Input:* UoW record + trigger event type (e.g., `status_changed_to_done`).
- *Output:* Side effects executed; hook IDs appended to `hooks_applied` field in the UoW record.

**Hook configuration (YAML at `~/lobster-user-config/orchestration/hooks.yaml`):**
```yaml
hooks:
  - id: notify-before-install-change
    trigger: classify
    condition:
      type: executable
      files_includes: "install.sh"
    action:
      type: human-gate
      message: "This UoW touches install.sh — confirm before running"

  - id: retry-on-failure
    trigger: status_changed_to_failed
    condition:
      retry_count: "< 3"
    action:
      type: re-queue
      backoff: exponential

  - id: convergence-trigger
    trigger: all_children_done
    condition:
      posture: fan-out
    action:
      type: trigger-agent
      agent: synthesis-agent

  - id: escalate-stalled-high-priority
    trigger: temporal
    schedule: "0 * * * *"
    condition:
      priority: high
      status: pending
      age: "> 3 days"
    action:
      type: escalate
      channel: telegram
```

**Trigger timing:**
- `classify`: fires when a UoW is first created and routed
- `status_changed_to_*`: fires on each status transition
- `all_children_done`: fires when the last child UoW of a fan-out parent completes
- `temporal`: evaluated on a cron schedule against all UoWs in the registry

**Failure mode:** Hook loop — a hook action causes a state change that re-triggers the same hook. Detection: `hooks_applied` field. If the same hook_id appears more than N times for a single UoW, a loop guard fires and freezes that UoW's hook evaluation. Without this guard, retry-on-failure + instant re-failure produces an infinite retry loop consuming agent capacity silently.

**Phase 1 implementation:** No hooks in Phase 1. Sweeper proposes actions via its output report; Dan confirms. Phase 1 makes the hook interface explicit (field in UoW schema, `hooks.yaml` location established) so Phase 2 can wire hooks without a schema change.

---

### 3.4 Diverge/Converge Execution

**What it is:** The structural pattern for parallel work. Any agent can decompose a UoW into child UoWs, launch parallel subagents, and trigger convergence when all children complete. The Registry tracks the tree regardless of depth.

**What it does:**
- *Diverge:* A decomposition agent creates N child UoW records in the Registry with `parent` pointing to the root UoW. N subagents are launched. Each subagent writes its output to `output_ref` and transitions its UoW to `done`.
- *Converge:* The `all_children_done` hook fires (see 3.3). A synthesis agent is spawned with all sibling `output_ref` values. It produces a unified output and transitions the root UoW to `done`.

**Composability:** The decomposer and synthesizer are agents, not special system processes. A subagent can itself spawn sub-subagents. The dispatcher can always see the full tree via `registry.query(parent=uow_id, recursive=True)` — even if it didn't initiate the fan-out.

**Interface:**
- *Any agent creating children:* `registry.write({...parent: root_uow_id...})` for each child.
- *Synthesis agent receiving work:* Receives list of `output_ref` values from sibling UoWs. Writes unified output to root UoW's `output_ref`.

**Example:**
```
root UoW: "Refactor scheduler to support per-job timeouts"
  posture: fan-out
  |
  +-- child UoW: "Update task-runner.py"        → subagent A
  +-- child UoW: "Update jobs.json schema"      → subagent B
  +-- child UoW: "Update install.sh migration"  → subagent C (hook: human-gate)
  |
  +-- [all done] → convergence hook fires
       → synthesis agent: verify consistency, write summary, update PR description
```

**Failure mode:** Orphaned children — root UoW fails to converge because a child never transitions from `active` or `pending` (agent crash, timeout). Detection: a temporal check (not a post-completion event — the completion event may never fire) that runs on each sweep cycle: any fan-out parent with `status=active` where all children have `status IN (done, failed)` for >1 hour triggers a reconciliation alert to Dan via Telegram and transitions the parent to `done` with a note. If children are still `active` or `pending` after 2× the typical task window, escalate separately.

<!-- Added: audit synthesis 2026-03-26 -->
Note on the detection trigger: the original design specified "post-completion sweep" — this is a design hole. The failure mode being detected is precisely that the `all_children_done` hook never fires (and thus the parent never reaches `done`). A detection that runs post-completion will never trigger if completion is the thing that never happens. The temporal scan above closes this gap.
<!-- End added: audit synthesis 2026-03-26 -->

**Phase 1 implementation:** Fan-out is not dispatched in Phase 1. All UoWs use solo posture. The Registry schema supports `parent/children` fields from day one so Phase 2 can add fan-out without a migration.

---

### 3.5 Observability Layer

**What it is:** An append-only audit log of every UoW state transition, plus the query interface that lets Dan and Lobster read the system's current state and history.

**What it does:** Writes one JSON line per event. Provides the raw material for: dispatcher self-orientation, Dan's status queries, META's health assessment, reflection/learning feedback loops.

**Interface — Audit log:**
```
~/lobster-workspace/orchestration/audit.jsonl
```
One JSON object per line:
```json
{
  "ts":          "ISO8601",
  "uow_id":      "uow_20260326_abc123",
  "event":       "created | status_change | hook_fired | child_spawned | completed | failed",
  "from_status": "pending",
  "to_status":   "active",
  "agent":       "subagent-id",
  "note":        "classifier routed to review-loop: risk=high"
}
```

**Interface — Dan's queries (answered from Registry + audit log):**
- `"what's running?"` → Registry, `status=active`
- `"what did we finish today?"` → audit log, `event=completed AND ts > today`
- `"why did X get routed to review-loop?"` → Registry, `route_reason` field for that UoW
- `"what's stuck?"` → Registry, `status=blocked or (status=pending AND age > threshold)`
- `"show me the tree for the scheduler refactor"` → Registry, `parent/children` traversal

**Failure mode:** Dark pipeline — audit log has no new entries for >3 days while the system is nominally running. Interpretation: either no work is transitioning (pipeline stalled), or work is happening outside the registry (outside the meter). Both are degradation states. META monitors this signal.

**Phase 1 implementation:** Audit log file created at first write. Sweeper writes an audit entry for each UoW it creates. Dispatcher can answer "what's running?" from Registry. No visualization in Phase 1 — plain query results are sufficient.

---

### 3.6 Trigger Substrate

<!-- Added: audit synthesis 2026-03-26 -->

**What it is:** A unified evaluation substrate for lifecycle activation triggers. A Trigger is the mechanism by which a sleeping UoW becomes active. The WOS Conditional Hook System (#168) and the Deferred Trigger system (#172) are both implementations of this single abstraction.

**Core insight:** A UoW in `status='proposed'` or `status='pending'` is sleeping until its trigger fires. The trigger can be time-based (fire at a wall-clock time) or condition-based (fire when a Registry or GitHub state condition is met). Both trigger types are evaluated by the same substrate.

**Trigger types:**

| `trigger_type` | Description | Example |
|---|---|---|
| `immediate` | No delay; UoW becomes active on confirmation | Default for most Phase 1 UoWs |
| `time` | Fire at a specified wall-clock time | `fire_at: "2026-04-01T09:00:00Z"` |
| `condition` | Fire when a Registry state condition is met | `{all_children_done: true, parent_id: "uow_..."}` |

**Relationship to Hook System (#168) and Deferred Triggers (#172):**

The Conditional Hook System's temporal hooks (e.g., `escalate-stalled-high-priority`) are time triggers. Its state-transition hooks (e.g., `all_children_done`, `status_changed_to_failed`) are condition triggers. The Deferred Trigger system (#172) is the mechanism for scheduling UoWs to activate at a future time — also a time trigger.

These are not two separate systems. They are the same Trigger evaluation engine with two trigger types. Building #172 first gives #168's temporal and condition hooks a substrate to build on, rather than reimplementing scheduling logic independently.

**Phase 2 implementation:** The Trigger evaluator is a lightweight component of the sweeper or a separate scheduled process. On each run, it checks all sleeping UoWs (`status IN (proposed, pending)` with a `trigger` field) and fires any whose conditions are met. Fired triggers transition the UoW to the next status (typically `active`) and write an audit entry.

**Why this matters for Phase 1:** The `trigger` field is added to the schema from day one (see §3.1 schema). Phase 1 UoWs carry `trigger: {type: immediate}` as the default. No trigger evaluation happens in Phase 1 — but the field's presence means Phase 2 can add trigger evaluation without a schema migration.

<!-- End added: audit synthesis 2026-03-26 -->

---

## 4. The Issue Sweeper (Governance Layer)

The sweeper is not a general orchestration component — it is one specific agent: the scanner that watches the GitHub issue backlog and creates new UoWs for the orchestration engine.

**Schedule:** Nightly at 3am (after negentropic sweep at 2am, so sweep output is available as sweeper input).

**What the sweeper scans and does:**

| Condition | Sweeper action | Phase 1 / Phase 2 |
|-----------|---------------|-------------------|
| `ready-to-execute` label, no linked PR, age > 3 days | Surface in ready queue output | Phase 1: propose. Phase 2: create UoW in Registry |
| Open > 14 days, no activity, no `on-hold` | Add `stale` label; queue for Dan review | Phase 1: propose. Phase 2: write label |
| `high-priority`, no recent comment, no linked PR | Telegram ping + create high-priority UoW | Phase 1: propose. Phase 2: autonomous |
| Has `design` label, no open questions, linked design doc exists | Add `ready-to-execute`, remove `needs-design`; create executable UoW | Phase 2 only |
| Linked PR merged | Close issue; mark UoW done in Registry | Phase 2 only |
| Has `on-hold` label | Periodic reminder in sweep output | Phase 1 |

**Phase 1 sweeper output** (written to `hygiene/YYYY-MM-DD-issue-sweep.md`):
1. State transitions proposed — which issues and why
2. Ready queue — ordered list of executable issues
3. Escalations — high-priority stalled items (Telegram ping)
4. Dan-blocked items — waiting on his action
5. UoWs created (Phase 1: proposed only)

<!-- Added: audit synthesis 2026-03-26 -->
**Deduplication requirement:** Before creating a UoW for any GitHub issue, the sweeper checks the Registry for existing records with `source=github:issue/N` and `status NOT IN (done, failed, expired)`. If found, the sweep logs a skip entry in its output (e.g., "Issue #142: UoW uow_20260326_abc123 already exists in proposed status — skipping") and no new record is created. This prevents daily duplicate UoWs for issues that remain in `proposed` state across sweep runs.
<!-- End added: audit synthesis 2026-03-26 -->

**Consumption gate for Phase 1:** The sweeper output is delivered; Dan acts on it. Healthy state: ready queue in sweep output contains ≤5 items per run, and items from prior sweeps appear as closed or in-progress within 5 days. Degradation state: sweep outputs accumulate unread or ready queue grows run-over-run without drain. If the output isn't being consumed, the sweeper is producing noise.

---

## 5. Units of Work — Type Reference

| Type | Description | Routing default |
|------|-------------|-----------------|
| **Design seed** | Raw observation requiring a design session | `sequential` (design-agent first) |
| **Design doc** | Settled design needing implementation | `solo` or `fan-out` based on scope |
| **Executable task** | Clear scope, buildable now | `solo` (Phase 1 default for all) |
| **Research** | Investigation required before design | `solo` (one research agent) |
| **Operational** | Running maintenance, not feature work | `solo` |

The sweeper enforces typing via labels. The ready queue contains only executable tasks and design docs — not seeds or research items. Seeds and research items require a design-phase UoW before they graduate to executable.

---

## 6. Phased Execution Plan

### Phase 1 — Minimal viable sweeper + Registry skeleton

**What gets built:**
- Scheduled job: `issue-sweeper`, nightly at 3am
- Operations: stale detection, ready-queue surfacing, Dan-blocked identification, Telegram escalation for high-priority stalled items
- UoW Registry: flat JSON at `~/lobster-workspace/orchestration/registry.json`
- Audit log: `~/lobster-workspace/orchestration/audit.jsonl`
- Sweeper creates UoW records for identified items (no autonomy yet — records reflect proposals, `status=proposed` until Dan confirms)
- Confirmed records transition to `status=pending`; `proposed` records older than 14 days transition to `status=expired` via a lightweight cron
- No classifier — all postures default to `solo`, `route_reason = "phase1-default"`
- No hooks — sweeper proposes, Dan confirms
<!-- Added: audit synthesis 2026-03-26 -->
- Registry write path: `registry_cli.py` CLI with `fcntl.flock`, atomic writes, and write-ahead-log protocol
- `~/lobster-workspace/orchestration/` directory initialized via `upgrade.sh` migration
<!-- End added: audit synthesis 2026-03-26 -->

**Phase 1 is done when:**
- The sweeper runs on its nightly schedule without errors
- `registry.json` contains at least one UoW record created by the sweeper
- `audit.jsonl` contains the corresponding creation event
- Dan can ask "what's running?" and get an answer from a single Registry query
- The ready queue in the sweep output is ordered and actionable (not just a dump of all open issues)

**Estimated scope:** One subagent session to write the task definition + sweeper agent context.

---

### Phase 2 — Routing classifier + conditional hooks + autonomous label writes

**What gets built:**
- Sweeper writes labels autonomously (after explicit gate crossing — see Principle 6)
- Classifier rule engine: evaluates `classifier.yaml`, assigns postures, writes `route_reason`
- Hook system: transition hooks + temporal hooks, evaluated from `hooks.yaml`
- `design-settled` detection heuristic (linked doc exists + no open questions)
- Registry queryable by dispatcher as primary self-orientation mechanism (no GitHub fallback)

**Phase 2 is done when:**
- A new UoW arrives, the classifier assigns it a posture, and `route_reason` in the Registry reflects which rule fired
- At least one transition hook fires and its ID appears in `hooks_applied`
- The dispatcher can answer "what's running?" from Registry without any GitHub API call
- The autonomy gate crossing for label writes is documented and confirmed

---

### Phase 3 — Full diverge/converge + observability + reflection

**What gets built:**
- Decomposition agents can create child UoWs; convergence hook triggers synthesis agent
- Synthesis agent pattern established
- Visualization: audit log to timeline/tree on request
- Reflection hooks: post-mortem agent after fan-out completions writes to `orchestration/reflections/`
- Classifier evolution: `route_reason` pattern analysis by META; first-match → weighted scoring if systematic misrouting is detected
- Ready queue exposed via `get_priorities()` as single source of truth for Dan and Lobster

**Phase 3 is done when:**
- A fan-out UoW completes its full cycle: root created, children created, all children done, synthesis agent fires, root transitions to `done`
- The audit log for that cycle is complete and parent/children traversal works
- Dan can ask "show me the tree for [UoW]" and get a readable summary
- `get_priorities()` returns the ready queue without requiring any additional lookup

---

## 7. META Section

*Written from META's perspective. Read this to orient on intent, assumptions, health signals, and when to surface a refinement proposal.*

---

### Intent

This system exists to convert the issue backlog from a permanent accumulation layer into a meter. The specific falsifiable claim: issues that are ready-to-execute should not remain in that state for more than ~5 days without a linked PR or a recorded reason for the hold. If that claim holds over time, the meter is working. If it does not, the meter is dark or the pipeline is stalled.

META's job is to watch the meter, not the mechanisms. The mechanisms (sweeper, classifier, hooks) are means. The outcome (work advancing, not just accumulating) is the target.

---

### Design Assumptions — Watch These

These are load-bearing. Flag when evidence contradicts them.

1. **GitHub issues are the right substrate.** If issue volume grows so large that scanning is noisy, or if issues are routinely created without the right labels, this breaks.

2. **Labels are reliable state signals.** The classifier and sweeper read `ready-to-execute`, `needs-design`, `stale`, etc. as ground truth. If labels are inconsistently applied, routing is corrupted.

3. **Nightly cadence is sufficient.** 24-hour sweeper latency is the upper bound on "time from issue ready to UoW created." If work velocity increases materially, this becomes a bottleneck.

4. **Solo posture is the safe default.** Phase 1 routes everything to solo. If most real work is actually multi-file or multi-domain, the default will systematically underroute — and the classifier's first dataset will be noisy.

5. **Dan will act on escalations.** Several hooks send Telegram pings and pause for human-gate responses. If Dan's response latency is high or escalations accumulate in a backlog, the human-gate posture amplifies rather than resolves blocking.

6. **Phase sequencing holds.** If Phase 1 is never completed, Phase 2 never starts. There is no graceful fallback. If Phase 1 stalls, the entire design is a document.

---

### Healthy Signals

- Ready queue **drains**: items labeled `ready-to-execute` are not sitting in that state >5 days without a linked PR or a hold reason.
- Sweeper runs on schedule: `hygiene/YYYY-MM-DD-issue-sweep.md` files appear dated to the current day. No gap >2 nights.
- Registry reflects reality: UoW count in Registry roughly tracks the `ready-to-execute` issue count. Systematic divergence (Registry near-empty, GitHub has 10+ ready) means the sweeper is not creating UoWs.
- Stale issues resolve: issues flagged `stale` either close, get `on-hold` with a reason, or re-enter ready queue within ~2 weeks.
- Audit log has transition events: `audit.jsonl` contains entries with `event != 'created'` at least weekly. A log containing only `created` events means the sweeper is running but no work is transitioning — this is a stalled pipeline signal, not a healthy one. Creation-only entries are necessary but not sufficient. <!-- Added: audit synthesis 2026-03-26 -->
- Failed UoW proportion is low: failed/total ratio below ~20% sustained over a week.
- "What's running?" is answerable in one query. If the dispatcher falls back to GitHub scanning, the Registry is not being maintained.

---

### Degradation Signals

| Signal | Interpretation |
|--------|---------------|
| Ready queue grows while `pending` UoWs accumulate | Sweeper is creating UoWs nobody is picking up. Detection is working; execution is not. |
| Registry empty or not updating | Phase 1 never completed, or sweeper is running but not writing. Observability layer is dark. |
| Stale count grows monotonically | Sweeper detects staleness; sweep output is not being acted on. |
| Audit log silent >3 days | No UoWs transitioning. Either no work happening, or work is happening outside the registry. |
| Issue count grows faster than closed count for >2 consecutive weeks | Accumulation mode resumed. The system is generating faster than it is resolving. |
| Labels diverge from expected sweep outputs | Sweeper ran but did not apply labels it said it would. Either propose-only mode (expected Phase 1) or writes are failing silently. |
| UoW Registry has orphaned `active` records | UoW is `active` but no agent is associated and no recent audit entry exists. Registry is drifting from actual state. |
| Escalations unacknowledged >5 days | Human-gate posture is accumulating a backlog silently. |

---

### Refinement Triggers

**Reconstitution triggers** (the design needs to change):

- Ready queue drains but issue resolution time is not decreasing. Bottleneck has shifted upstream (wrong issues being created) or downstream (postures are wrong). Classifier redesign warranted.
- More than 30% of UoWs stall at `blocked` for >7 days. Blocking model is not working — dependencies not resolving, or human-gate over-applied.
- The same UoW type fails repeatedly with the same error. Systematic execution failure, not one-off. That type's routing rule should be reconsidered.
- Phase 1 → Phase 2 transition pending >30 days with sweeper still in propose-only mode. The design is functioning as a document, not a system. Reconsider autonomy thresholds.
- `route_reason` pattern analysis reveals systematic mismatch (same rule fires consistently but posture is wrong). Signal for classifier evolution from first-match to weighted scoring — but only if the pattern is sustained across >20 UoWs, not occasional.

**Ceremonial closing triggers** (a component should be retired):

- A hook has never fired in 60+ days of operation. Either the target condition never occurs, or the detection logic is broken. Close or fix.
- An open question from section 8 has been empirically answered by 60+ days of operation. Formalize the answer, remove the question.
- A Phase 3 feature has been `pending` >60 days without progress. Either de-prioritize explicitly or close as "will not build in current cycle."

**Tuning triggers** (parameters need adjustment, not design):

- Stale threshold (14 days) consistently flags issues Dan considers actively in-progress. Raise the threshold.
- Nightly cadence produces consistently empty outputs. Reduce frequency or add trigger-based run.
- Ready queue routinely >10 items. Add a prioritization filter to the ready queue output.

---

## 8. Open Questions

*Only genuine open questions — design decisions that cannot be resolved without more information or observation. Each includes what would resolve it.*

**Q1: Registry persistence — when to migrate to SQLite?**
JSON is sufficient for Phase 1. At what UoW volume does query performance degrade enough to warrant SQLite? Unresolved because we don't yet know Phase 1 throughput. *Resolves when:* Phase 1 has been running for 30 days and we can measure actual UoW volume per week.

**Q2: Hook storage location — user-config vs. system repo?**
Behavioral hooks (escalation thresholds, notification preferences) belong in user-config; structural hooks (retry logic, loop guards) belong in system repo. The split makes sense in principle, but the boundary case — convergence-trigger hook — is structural behavior with a user-tunable parameter (what synthesis agent to use). *Resolves when:* Phase 2 hook implementation specifies which hooks it needs, and each is classified by the proposed split rule.

**Q3: Sweeper autonomy on label writes — when is Phase 1 propose-only sufficient?**
Phase 1 is propose-only. The gate crossing to autonomous label writes is Principle 6's requirement. *Resolves when:* Phase 1 has run for ≥2 weeks and META's ready-queue drain signal is positive. If the sweeper's proposals are consistently correct, the autonomy increase is low-risk.

**Q4: Stale thresholds — are 14 days / 7 days right for design seeds?**
Design-seed issues may legitimately sit for months. A 14-day stale threshold will generate noise for that type. *Resolves when:* Phase 1 sweeper output provides actual distribution of issue age-by-type. Adjust threshold per type if data warrants.

---

## 9. What This Is Not

This system is not a project manager. It does not set deadlines, estimate effort, or assign priorities from scratch. It operationalizes the priority signals already embedded in labels, routes work to the right execution posture, tracks what is in motion, and surfaces what is stalled.

The negentropic sweep handles structural hygiene (clean up, elevate patterns). The sweeper handles forward propulsion (advance work through stages). The orchestration engine handles execution structure (how work is decomposed, run, and synthesized). They are complementary, not duplicative.

The system's success condition is not that it looks busy. It is that Dan can tell at a glance what is actually moving — and what is not.
