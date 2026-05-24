# WOS Observability Gap Analysis
*Produced: 2026-05-01*

---

## 1. What Exists — Structured Inventory

### 1.1 Registry Database Fields (SQLite, `registry.db`)

Every UoW row carries the following observability-relevant fields:

| Field | Type | Purpose |
|-------|------|---------|
| `id` | str | Stable UoW identifier (e.g. `uow_20260428_cac705`) |
| `status` | str | Current lifecycle state (17 valid values) |
| `summary` | str | Human-readable description |
| `source` | str | Origin reference (e.g. `github:issue/722`) |
| `source_issue_number` | int | GitHub issue number — links to external provenance |
| `register` | str | Attentional config (`operational`, `human-judgment`, etc.) |
| `posture` | str | Execution posture (`solo`, etc.) |
| `route_reason` | str | Why steward chose this execution path |
| `steward_cycles` | int | Number of steward cycles in current reset window |
| `lifetime_cycles` | int | Cumulative steward cycles across all retries |
| `retry_count` | int | Total Steward re-entry cycles (diagnostic) |
| `execution_attempts` | int | Confirmed dispatches — the retry budget counter |
| `heartbeat_at` | str | Last heartbeat from executing agent |
| `heartbeat_ttl` | int | Seconds of silence before stall detection fires |
| `started_at` | str | When executor claimed and dispatched |
| `closed_at` | str | When Steward confirmed completion |
| `close_reason` | str | Prose from Steward's closure decision |
| `output_ref` | str | Path to `.json` artifact in `outputs/` |
| `workflow_artifact` | str | Path to `.md` prescription in `artifacts/` |
| `prescribed_skills` | list | Skills injected into subagent prompt |
| `token_usage` | int | Tokens consumed (populated by `write_result`, often NULL) |
| `outcome_category` | str | Metabolic label: `pearl`, `seed`, `heat`, `shit` |
| `juice_quality` | str | Steward-assessed quality signal |
| `juice_rationale` | str | Rationale for juice_quality assessment |
| `prescription_confidence` | str | Steward confidence in prescription |
| `success_criteria` | str | Germination-time success definition |
| `file_scope` | str | Shard gate constraint |
| `shard_id` | str | Executor shard assignment |

**Audit log table** (`audit_log`): chronological event entries per UoW. Each entry: `{ts, event, note (JSON)}`. Includes status transitions, orphan kill classifications, retry cap events, and steward observations.

**Corrective traces table** (`corrective_traces`): per-execution partial progress notes written by executor for re-entry context.

**Heartbeat log table** (`uow_heartbeat_log`): per-heartbeat snapshots with `{uow_id, recorded_at, token_usage}`. Only rows with non-NULL token_usage are stored. Exists but is not surfaced by any current CLI command.

### 1.2 CLI Commands (`src/orchestration/registry_cli.py`)

| Command | What it does | Output format |
|---------|-------------|---------------|
| `status-breakdown` | Count of UoWs by status | JSON object |
| `list [--status]` | Full UoW list, optionally filtered by status | JSON array |
| `get --id` | Full raw UoW row with all fields | JSON |
| `trace --id` | Unified forensics: registry row + audit_log + corrective_traces + return_reasons + kill_classification + trace.json + diagnosis_hint | JSON |
| `report [--since H] [--from ISO]` | Time-windowed pipeline report: counts, throughput, median wall-clock, total tokens, outcome_category breakdown, per-UoW listing | Plain text |
| `stale [--buffer-seconds N]` | In-flight UoWs with silent heartbeats | JSON array |
| `escalation-candidates` | All `needs-human-review` UoWs | JSON array |
| `check-stale` | Active UoWs whose source GitHub issue is closed | JSON array |
| `gate-readiness` | WOS autonomy gate metric (approval rate, days running) | JSON |
| `expire-proposals` | Expire proposed records older than 14 days | JSON |
| `approve --id` | Advance proposed → pending | JSON |
| `decide-retry --id` | Reset stuck UoW → ready-for-steward | JSON |
| `decide-close --id` | Close stuck UoW → failed | JSON |
| `upsert --issue --title` | Propose a UoW for a GitHub issue | JSON |

### 1.3 File Artifacts (`~/lobster-workspace/orchestration/`)

| Path pattern | Contents | Written by |
|-------------|----------|-----------|
| `outputs/<uow_id>.json` | Full UoW snapshot at completion time | Executor |
| `outputs/<uow_id>.result.json` | `{uow_id, outcome, success, reason?}` — subagent write_result payload | `maybe_complete_wos_uow` |
| `outputs/<uow_id>.trace.json` | `{execution_summary, surprises, prescription_delta, gate_score}` — executor dispatch record | Executor |
| `artifacts/<uow_id>.md` | Prescription markdown — full subagent prompt | Steward |
| `artifacts/<uow_id>.cycles.jsonl` | Per-cycle log: `{cycle_num, subagent_excerpt, return_reason, next_action, timestamp}` | Steward |
| `failure-traces/<uow_id>.json` | Failure summary on hard-cap cleanup: `{reason, cycle_count_lifetime, archived_artifact_path}` | Steward |
| `prescription-drafts/` | Steward prescription drafts (observed empty in production) | Steward |
| `registry.db` | SQLite registry | Registry |

### 1.4 Heartbeat Infrastructure

- `registry.write_heartbeat(uow_id, token_usage?)` — updates `heartbeat_at` + inserts into `uow_heartbeat_log` if token_usage provided
- `registry.get_stale_heartbeat_uows()` — returns in-flight UoWs whose `heartbeat_at` + `heartbeat_ttl` + buffer has elapsed
- Steward observation loop reads `heartbeat_at` to detect stalls and classify orphans
- Agent-side heartbeat integration (issue #849) is **not yet implemented** — executing subagents do not currently call `write_heartbeat`

### 1.5 Design-Doc Observability (Section 6, `wos-architecture.md`)

The canonical architecture doc (`~/lobster/docs/wos-architecture.md`, Section 6) documents these gaps explicitly:
- Gap 1: `wos_surface` mixed-branch cannot spawn + reply simultaneously
- Gap 2: `wos_early_warning` has no dispatcher handler
- Gap 3: `wos_surface` OSError fallback silently downgrades escalation
- Gap 4: `wos_diagnose` result is not auto-relayed to Dan
- Gap 5: Steward does not yet write `wos_diagnose` into its escalation path
- Gap 6: Exhaustiveness test for return_reason classifications is structural but requires maintenance discipline

---

## 2. What Questions Can Be Answered Today

| Question | Command / Query |
|----------|----------------|
| How many UoWs are in each state right now? | `registry_cli.py status-breakdown` |
| What is the full history of a specific UoW (every event, every transition)? | `registry_cli.py trace --id <uow_id>` |
| What did the subagent actually execute and what files did it produce? | `registry_cli.py get --id <uow_id>` → `output_ref` field |
| How many UoWs completed in the last N hours? What was the throughput rate? | `registry_cli.py report --since N` |
| What is the median wall-clock for UoW execution? | `registry_cli.py report` → `Median wall` field |
| Which UoWs are stuck and need human decision? | `registry_cli.py escalation-candidates` |
| Which executing UoWs have silent heartbeats (possible stalls)? | `registry_cli.py stale` |
| What is the distribution of metabolic outcomes (pearl/seed/heat/shit)? | `registry_cli.py report` → `Outcomes` header + per-UoW `Category` column |
| Was this UoW killed before or during execution? | `registry_cli.py trace --id` → `kill_classification` field |
| What failure pattern is this? (orphan kill, retry-cap, dead-prescription-loop) | `registry_cli.py trace --id` → `diagnosis_hint` |
| Which active UoWs have source issues that are now closed? | `registry_cli.py check-stale` |
| Is the WOS autonomy gate met? | `registry_cli.py gate-readiness` |
| What GitHub issue does this UoW correspond to? | `registry_cli.py get --id` → `source_issue_number` |
| How many times has the steward cycled on a UoW without dispatching? | `registry_cli.py get --id` → `steward_cycles` / `lifetime_cycles` |
| What was the subagent's outcome narrative? | `outputs/<uow_id>.result.json` → `outcome` + `reason` fields |

---

## 3. What Questions Cannot Be Answered — Gaps by Impact

### Gap A — Token cost per UoW (HIGH IMPACT)

**Question:** How many tokens did this UoW consume? What does it cost per day to run WOS?

**Current state:** `token_usage` field exists in the registry and is part of the `report` command output. However, in the 168-hour sample report, `Total tokens : 0` — every row shows `Tokens: -`. The field is populated only when `write_result` includes a `token_usage` parameter, and subagents are not currently passing this value. The `uow_heartbeat_log` table captures per-heartbeat token snapshots when `write_heartbeat` is called with `token_usage`, but agent-side heartbeat integration (issue #849) is not yet shipped.

**Consequence:** Budget visibility is structurally present but effectively dark. There is no way to compute daily token spend, cost per UoW, or cost per register type.

---

### Gap B — Agent liveness vs. stall detection (HIGH IMPACT)

**Question:** Is the currently-executing subagent alive right now? How long ago did it last signal?

**Current state:** The `stale` command surfaces UoWs whose `heartbeat_at` has gone silent, but only when `heartbeat_at` is non-NULL. The `started_at` field marks dispatch time, not agent liveness. Because issue #849 (agent-side heartbeat integration) is not shipped, `heartbeat_at` is NULL for all currently executing UoWs — meaning `stale` always returns an empty list in practice. The 24-hour TTL orphan safety net in the executor is the only backstop.

**Consequence:** Stall detection is structurally ready but functionally inactive. A subagent hanging after dispatch is undetectable until the 24-hour TTL fires.

---

### Gap C — Failure pattern clustering across UoWs (MEDIUM IMPACT)

**Question:** Are there recurring failure patterns? Do the same orphan return_reasons cluster at the same times of day? Is there a class of UoWs that consistently fails?

**Current state:** `trace --id` provides per-UoW pattern matching (kill-before-start, kill-during-execution, dead-prescription-loop, retry-cap). There is no cross-UoW aggregation. The `report` command counts `failed` UoWs but does not break down by failure pattern, register, posture, or kill_type.

**Consequence:** Kill waves appear in individual traces but are invisible at the aggregate level. An operator cannot quickly answer "what fraction of failures this week were infrastructure kills vs. genuine task failures?"

---

### Gap D — Time-in-state histograms (MEDIUM IMPACT)

**Question:** How long do UoWs typically wait in `ready-for-steward` before being prescribed? In `ready-for-executor` before dispatch? What is the queue latency?

**Current state:** `started_at` and `closed_at` are recorded; `wall_clock_seconds` is computed at query time from their difference. But wall-clock covers the full lifecycle from dispatch to close — it does not decompose into queue wait vs. execution time. The audit_log has timestamps on every transition, so this data is present but requires a bespoke SQL join to extract.

**Consequence:** It is impossible to determine whether throughput degradation is caused by slow execution, slow prescription, or long queue wait. All three look the same at the wall-clock level.

---

### Gap E — Throughput trend over time (MEDIUM IMPACT)

**Question:** Is WOS getting faster or slower? Has throughput improved over the last week vs. the previous week?

**Current state:** `report --since N` gives throughput for a single window. There is no time-series comparison, no trend visualization, and no weekly summary that would show whether the `0.20 completions/hr` rate is improving.

**Consequence:** Regression detection requires manual comparison of multiple `report` invocations.

---

### Gap F — Register-level cost and throughput breakdown (LOW-MEDIUM IMPACT)

**Question:** Which register type (`operational`, `iterative-convergent`, etc.) consumes the most tokens? Which has the highest failure rate?

**Current state:** `register` is a field on every UoW and is included in `get --id` output. The `report` command does not break down counts, throughput, or token usage by register. The per-UoW listing in `report` does not include `register`.

---

### Gap G — wos_diagnose result delivery to operator (LOW IMPACT, KNOWN)

**Question:** When a `diagnose <uow_id>` command is issued, does the result automatically appear in Telegram?

**Current state:** The diagnosis subagent writes `write_result(chat_id=0, sent_reply_to_user=False)`. The dispatcher receives the `subagent_notification` but must manually read and relay the JSON result. This is documented in wos-architecture.md Gap 4 and tracked as T2-A work.

---

### Gap H — wos_early_warning acknowledgment (LOW IMPACT, INTENTIONAL)

**Question:** Can an early warning be acknowledged/suppressed programmatically?

**Current state:** `wos_early_warning` messages have no dispatcher handler. They reach Dan as plain text. There is no ack path, no suppress mechanism, and no tracking of whether a warning was acted upon. Architecture doc Gap 2 notes this may be intentional.

---

## 4. Recommended Additions

### R1 — `failure-breakdown` CLI command

A cross-UoW failure analysis command that aggregates the `audit_log` across all UoWs and produces a breakdown by pattern (kill-before-start, kill-during-execution, genuine-retry-cap, dead-prescription-loop) for a configurable time window.

**Rationale:** Closes Gap C. Kill waves are visible per-UoW but invisible in aggregate. This command would make infrastructure instability periods immediately apparent rather than requiring trace-by-trace investigation.

**Sketch:**
```
registry_cli.py failure-breakdown [--since HOURS] [--from ISO]
```
Output: JSON with `{total_failures, by_kill_type: {kill_before_start: N, ...}, by_register: {...}}`

---

### R2 — `queue-latency` CLI command

A command that computes time-in-state for UoWs that completed in a window, broken down by lifecycle phase: wait in `ready-for-steward`, wait in `ready-for-executor`, and actual execution time.

**Rationale:** Closes Gap D. The audit_log timestamps exist; this is a query engineering problem, not a data capture problem.

**Sketch:**
```
registry_cli.py queue-latency [--since HOURS]
```
Output: median and p90 for each phase, per-register breakdown.

---

### R3 — Agent-side heartbeat with token reporting (issue #849)

Ship issue #849: require all WOS execution subagents to call `write_heartbeat` every 60–90s with cumulative `token_usage`. This is a subagent prompt contract update, not a registry change.

**Rationale:** Closes Gaps A and B simultaneously. Without heartbeat integration, token_usage is dark and stall detection is inactive. Both the `report` token totals and the `stale` liveness command are non-functional without this.

**Note:** The registry infrastructure (heartbeat_log table, write_heartbeat method) is already live. Only the subagent contract update is needed.

---

### R4 — `report` command: add register breakdown and failure pattern column

Extend the existing `report` command output to include:
1. A `By register:` header line showing counts by register type
2. A `Kill` column showing the kill_type for failed UoWs (already done for some statuses — extend to all)
3. A failure pattern summary: `Infrastructure kills: N / Genuine failures: M`

**Rationale:** Closes Gaps C and F with minimal implementation surface. The data already flows through `report`; it needs a display pass.

---

### R5 — Structured result format for subagents

Establish a convention: WOS subagents should return a structured result in their `write_result` text using a parseable prefix format, e.g.:
```
outcome_category: seed
token_usage: 14500
pr_numbers: 1023
summary: implemented friction-trace harvesting
```

The `maybe_complete_wos_uow` function in `wos_completion.py` already extracts `refs` (PR numbers, issue numbers, file paths) from write_result text. Extending this parsing to extract `outcome_category` and `token_usage` directly from the subagent's result text would remove the dependency on subagents explicitly passing kwargs to write_result.

**Rationale:** Closes Gap A at the write_result boundary rather than requiring heartbeat integration. Simpler to deploy; less resilient (misses partial-completion token data).

---

## 5. Proposed Standards for Logging and Structured Data

### S1 — Subagent result contract

Every WOS execution subagent SHOULD include in its `write_result` text:
- `outcome_category: <pearl|seed|heat|shit>` — metabolic classification
- `token_usage: <N>` — cumulative input + output tokens
- `refs: <PR#N, issue#N, /path/to/file>` — artifact references

This contract should appear in the executor's subagent prompt template (which already includes UoW ID injection per issue #868).

### S2 — Heartbeat interval

Once issue #849 is shipped, the canonical interval is 60 seconds, with a 90-second max before the subagent must assume it has been orphaned and write a partial result. The heartbeat_ttl default (300 seconds) provides adequate buffer.

### S3 — trace.json as the canonical execution evidence file

`outputs/<uow_id>.trace.json` is the definitive record of what the executor observed. It should always be written, even on dispatch failure. Fields:
- `execution_summary` — one sentence
- `surprises` — list of unexpected observations
- `prescription_delta` — deviations from the prescription
- `gate_score` — oracle gate result if applicable

Currently some failed UoWs have no trace.json; the `kill-before-start` pattern partially relies on its absence for diagnosis. A minimal trace.json (even `{execution_summary: "dispatch failed"}`) would make diagnosis more deterministic.

### S4 — `failure-traces/` directory convention

The `failure-traces/` directory is written on hard-cap cleanup. Proposal: extend this to cover all terminal failure modes (not just hard-cap), with a consistent schema: `{uow_id, reason, final_return_reason, cycle_count_lifetime, summary, kill_classification, timestamp}`.

---

## 6. Open Question for Dan

**The token_usage gap is structurally solvable two ways — which matters more?**

Option A (heartbeat path, issue #849): agent calls `write_heartbeat` every 60s with cumulative token count. This also activates stall detection. Requires updating every subagent prompt template. Delivers continuous visibility mid-execution.

Option B (write_result path, R5 above): parse `token_usage:` from the subagent's result text. Zero infrastructure change; just a parsing rule in `maybe_complete_wos_uow`. Delivers total cost per completion but nothing during execution.

The gap analysis can recommend both, but the implementation sequence depends on which problem is more urgent: cost accounting after the fact (Option B, faster) or real-time stall detection with cost tracking (Option A, slower but richer). The current 24-hour TTL means a hung subagent wastes a full day of compute — that would make Option A the higher-leverage choice. But if the primary concern is budget accountability, Option B lands in days instead of weeks.

This is the one question the artifact trail alone cannot answer.

---

## Artifact Trail: UoW `uow_20260428_cac705` (Reference Case)

UoW selected: "Enhancement: harvester processes friction-trace as structured input, not appended prose" (GitHub issue #722). Status: `done`. Outcome: `complete`.

| Artifact | Location | Contents |
|----------|----------|----------|
| Registry row | `registry.db`, table `uow` | Full metadata: all fields listed in §1.1. status=done, execution_attempts=0, steward_cycles=1, heartbeat_at=2026-05-01T17:20:28Z, output_ref set |
| Output snapshot | `outputs/uow_20260428_cac705.json` | Full UoW dict at completion (empty — 0 bytes observed in read) |
| Result file | `outputs/uow_20260428_cac705.result.json` | `{"uow_id": "uow_20260428_cac705", "outcome": "complete", "success": true}` |
| Trace file | `outputs/uow_20260428_cac705.trace.json` | `{execution_summary, surprises: [], prescription_delta: "", gate_score: null}` — confirms dispatch via subagent 72d53477 |
| Prescription | `artifacts/uow_20260428_cac705.md` | Full subagent prompt markdown with task_id, constraints, prescribed_skills |
| Cycles log | `artifacts/uow_20260428_cac705.cycles.jsonl` | 3 lines: cycle 0 prescribed → cycle 0 diagnosing_orphan → cycle 1 execution_complete/done |
| Audit log | `registry.db`, table `audit_log` | Many `steward_diagnosis` entries (3-minute polling) + orphan classification + completion transition |
| GitHub issue | `#722` (via `source_issue_number`) | Source issue; UoW closure does not automatically close the GitHub issue (Steward does that separately) |
| token_usage | NULL — not reported | Gap A confirmed: subagent did not pass token count |
| outcome_category | NULL — not reported | Metabolic label absent; subagent did not classify |

This is the complete artifact trail for a successfully completed UoW. Eight artifact types across three locations (registry DB, outputs/, artifacts/). GitHub issue is the ninth artifact, linked by `source_issue_number` but not directly tied to closure.
