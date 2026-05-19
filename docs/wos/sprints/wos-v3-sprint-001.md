# WOS V3 Sprint 001 — Trace Substrate + Observability Baseline

*Status: Ready for execution — 2026-04-04*
*Document purpose: Self-contained execution guide for a fresh context window*

> **Registry operations**: See [docs/wos-registry-reference.md](wos-registry-reference.md) for injection pattern and status state machine.

---

## Purpose

This sprint installs the corrective trace substrate (PR A) and validates it against 10 real UoWs. The gate without a substrate problem: PR #607 already added a one-cycle trace gate to the Steward — it waits for `trace.json` before re-prescribing — but the Executor never writes it. Every Steward re-entry logs a contract violation and stalls. This sprint closes that gap.

The sprint answers one question before any further V3 work proceeds: is V2's 0.8% UoW success rate a developmental failure (register-blindness, fixable with PRs B-D) or a catastrophic failure (executor dispatch infrastructure broken, requiring lower-level investigation)? The 10-UoW run produces the evidence that gates the entire remaining PR sequence.

---

## Sprint Boundaries

**In scope:**
- PR A: `executor.py` writes `trace.json` at all 4 exit paths and inserts into `corrective_traces` DB table
- 10-UoW observability run: enable execution, observe cycle completions, collect trace data

**Out of scope (deferred until sprint evidence):**
- PR B: Register-appropriate executor routing (`executor_type` dispatch table, new preambles)
- PR C: Register-aware diagnosis + corrective trace injection (Steward reads traces)
- PR D: Register-mismatch gate + expanded Dan interrupt conditions
- S3: Observation Loop pattern synthesis (requires populated `corrective_traces` table)
- S4: Scaling governor (next-order design problem)

**Duration:** Open — complete when 10 UoWs have cycled through the full Steward → Executor → Steward loop and trace data is collected.

---

## Pre-Sprint State

**WOS execution status:** DISABLED.

Check `~/lobster-workspace/data/wos-config.json`:
```json
{"execution_enabled": false, "bootup_candidate_gate": false}
```
The executor-heartbeat reads this file. While `execution_enabled` is `false`, the heartbeat skips dispatch but TTL recovery still runs.

**Current executor behavior (before PR A):**
- `executor.py` implements the 6-step atomic claim sequence
- `_write_result_json()` is called at all intentional exit paths (complete, partial, failed, blocked) and in the exception handler
- There is NO `_write_trace_json()` counterpart
- The `corrective_traces` DB table exists (added by migration 0007) but no code writes to it
- The `executor_uow_view` exposes `register` and `uow_mode` (migration 0007) but the Executor reads neither

**Current steward behavior (before PR A):**
- PR #607 is already merged: the trace gate is live in the prescribe branch
- The trace gate checks whether `{output_ref}.trace.json` exists
- When trace is absent, the Steward sets `trace_gate_waited = true` on the first cycle
- On the second cycle, it logs a contract violation: `trace_gate_contract_violation`
- The gate does NOT block Steward re-entry — the UoW can still be re-prescribed
- In practice, every re-entry logs a contract violation because the Executor never writes the file

**What the trace gate currently does (the problem):**
Every UoW that completes executor dispatch and returns to the Steward encounters the trace gate:
1. First re-entry: gate sees no trace.json, sets `trace_gate_waited` flag, continues
2. Second re-entry: gate sees no trace.json again, logs `trace_gate_contract_violation`, continues anyway
3. This repeats for every cycle of every UoW — persistent noise in the audit log

**DB tables that PR A will write:**
- `corrective_traces` — columns: `id` (autoincrement PK), `uow_id` (TEXT), `register` (TEXT), `execution_summary` (TEXT), `surprises` (JSON TEXT), `prescription_delta` (TEXT), `gate_score` (JSON TEXT or null), `created_at` (TEXT, defaults to `datetime('now')`)
- Index: `idx_corrective_traces_uow_id` on `(uow_id)`
- DB path: `~/lobster-workspace/orchestration/registry.db` (canonical V3 path)

---

## PR A: Executor Writes trace.json

### Context and file location

All changes are in a single file: `~/lobster/src/orchestration/executor.py`.

Key landmarks in the current file:
- Line 195: `_write_result_json(output_ref, result)` — the model for the new `_write_trace_json()` function
- Line 196: `_result_json_path(output_ref)` — the path derivation helper to mirror
- Line 505: `_run_step_sequence()` — outer execution wrapper; exception handler is here (lines 507-520)
- Line 522: `_run_execution()` — inner execution; step 5 (line 553) calls `_write_result_json` and step 6 (line 557) calls `registry.complete_uow`
- Lines 565-615: `report_partial()` and `report_blocked()` — each calls `_write_result_json`

The executor already holds `self.registry` which provides `self.registry.db_path` needed for the DB write.

### New functions to add

**1. `_trace_json_path(output_ref: str) -> Path`**

Path derivation mirroring `_result_json_path()`:
```python
def _trace_json_path(output_ref: str) -> Path:
    p = Path(output_ref)
    if p.suffix:
        return p.with_suffix(".trace.json")
    return Path(output_ref + ".trace.json")
```

**2. `_build_trace(uow_id, register, outcome, execution_summary, surprises, prescription_delta, gate_score) -> dict`**

Pure constructor for the trace dict. All fields required; surprises defaults to empty list, others default to empty string or null as noted:

```python
def _build_trace(
    uow_id: str,
    register: str,
    outcome: ExecutorOutcome,
    execution_summary: str,
    surprises: list[str] | None = None,
    prescription_delta: str = "",
    gate_score: dict | None = None,
) -> dict:
    return {
        "uow_id": uow_id,
        "register": register,
        "execution_summary": execution_summary,
        "surprises": surprises or [],
        "prescription_delta": prescription_delta,
        "gate_score": gate_score,
        "timestamp": _now_iso(),
    }
```

**3. `_write_trace_json(output_ref: str, trace: dict) -> None`**

Mirrors `_write_result_json()` exactly. Write atomically (tmp → rename). Creates parent dir:

```python
def _write_trace_json(output_ref: str, trace: dict) -> None:
    trace_path = _trace_json_path(output_ref)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = trace_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(trace, indent=2))
    tmp_path.rename(trace_path)
```

**4. `_insert_corrective_trace(registry_db_path: Path, trace: dict) -> None`**

Best-effort INSERT — log on failure, do not raise (consistent with the V3 non-blocking contract):

```python
def _insert_corrective_trace(registry_db_path: Path, trace: dict) -> None:
    try:
        conn = sqlite3.connect(str(registry_db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT INTO corrective_traces
                (uow_id, register, execution_summary, surprises, prescription_delta, gate_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace["uow_id"],
                trace["register"],
                trace["execution_summary"],
                json.dumps(trace.get("surprises") or []),
                trace.get("prescription_delta") or "",
                json.dumps(trace.get("gate_score")) if trace.get("gate_score") else None,
                trace.get("timestamp", _now_iso()),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Executor: failed to insert corrective_trace for %s — %s", trace.get("uow_id"), e)
```

### The register value problem

The Executor does not currently read `register` from the workflow artifact or DB during execution. For PR A (trace write only), the simplest approach is to read it from the `executor_uow_view` during the claim's step-1 pre-flight read (already done) and pass it forward in `ClaimSucceeded`.

**Recommended for PR A:** Add `register: str = "operational"` to the `ClaimSucceeded` dataclass and populate it from the row in `_claim()` step 1:
```python
register = row["register"] if row["register"] else "operational"
return ClaimSucceeded(uow_id=uow_id, output_ref=output_ref, artifact=artifact, register=register)
```

Then pass `register` through `_run_step_sequence()` and `_run_execution()` signatures.

PR B (register-appropriate routing) will later use the full `executor_type` → dispatcher table, but PR A only needs `register` to populate the trace field.

### All exit paths that need trace writes

There are 4 intentional exit paths. Each needs a `_write_trace_json` call immediately after its `_write_result_json` call:

**Exit path 1 — Normal complete** (`_run_execution()`, step 5, ~line 553)

After `_write_result_json(output_ref, result)`:
```python
trace = _build_trace(
    uow_id=uow_id,
    register=register,  # passed in from ClaimSucceeded
    outcome=ExecutorOutcome.COMPLETE,
    execution_summary=f"Executor dispatched subagent {executor_id}, subprocess exit 0.",
    surprises=[],
    prescription_delta="",
    gate_score=None,  # operational UoWs; iterative-convergent gate_score handled in PR B
)
_write_trace_json(output_ref, trace)
_insert_corrective_trace(self.registry.db_path, trace)
```

**Exit path 2 — Partial** (`report_partial()`, after `_write_result_json`)

```python
trace = _build_trace(
    uow_id=uow_id,
    register="operational",  # register not available here without refactor; use default for PR A
    outcome=ExecutorOutcome.PARTIAL,
    execution_summary=reason,
    surprises=[reason],
    prescription_delta=f"partial completion — {steps_completed}/{steps_total} steps done" if steps_completed is not None else "partial completion",
)
_write_trace_json(output_ref, trace)
_insert_corrective_trace(self.registry.db_path, trace)
```

Note: `report_partial()` and `report_blocked()` are public methods that take `output_ref` but not `register`. For PR A, use `"operational"` as the register default in these methods; the field can be enriched in a later PR. The `uow_id` is available.

**Exit path 3 — Blocked** (`report_blocked()`, after `_write_result_json`)

```python
trace = _build_trace(
    uow_id=uow_id,
    register="operational",  # see note above
    outcome=ExecutorOutcome.BLOCKED,
    execution_summary=reason,
    surprises=[reason],
    prescription_delta="blocked — external resolution required before re-prescription",
)
_write_trace_json(output_ref, trace)
_insert_corrective_trace(self.registry.db_path, trace)
```

**Exit path 4 — Exception/crash** (`_run_step_sequence()` exception handler, after `_write_subagent_result`)

The exception handler does not have `register` readily available (it only has `uow_id`, `output_ref`, and `exc`). Use `"operational"` as the default:

```python
trace = _build_trace(
    uow_id=uow_id,
    register="operational",
    outcome=ExecutorOutcome.FAILED,
    execution_summary=f"Executor crashed: {type(exc).__name__}: {exc}",
    surprises=[str(exc)],
    prescription_delta="exception before subagent dispatch — check executor logs",
)
_write_trace_json(output_ref, trace)
_insert_corrective_trace(self.registry.db_path, trace)
```

### Fields the executor cannot fill reliably

For PR A (the trace substrate), these fields will be minimal:

| Field | PR A behavior | Why |
|---|---|---|
| `execution_summary` | Template string using available info (executor_id, outcome) | Executor knows dispatch outcome; doesn't know what the subagent did |
| `surprises` | Empty list `[]` for normal complete; `[reason]` for partial/blocked/crash | Subagent surprises require the subagent to report back — not yet wired |
| `prescription_delta` | Empty string `""` for normal complete; structured placeholder for others | Prescription delta requires reasoning about what would change — not available at executor level without subagent feedback |
| `gate_score` | `null` for all exits in PR A | Gate score requires running the gate command — iterative-convergent routing is PR B |

These fields will be enriched in PRs B-D when the subagent writes its own trace data and the Executor can read it back before final commit. For PR A, the goal is structural: get trace.json written at every exit path with well-formed schema, even if content is minimal.

### Testing PR A

**Verify file write:**
```bash
# After any UoW completes, check the outputs directory
ls ~/lobster-workspace/orchestration/outputs/ | grep trace
# Expected: {uow_id}.trace.json alongside {uow_id}.result.json
```

**Verify trace schema:**
```bash
cat ~/lobster-workspace/orchestration/outputs/{uow_id}.trace.json
# Expected: JSON with all schema fields present, uow_id matches
```

**Verify DB write:**
```bash
sqlite3 ~/lobster-workspace/orchestration/registry.db \
  "SELECT uow_id, register, execution_summary, surprises FROM corrective_traces LIMIT 5;"
# Expected: rows with non-null uow_id, register, execution_summary
```

**Verify Steward gate stops triggering contract violations:**
```bash
sqlite3 ~/lobster-workspace/orchestration/registry.db \
  "SELECT ts, note FROM audit_log WHERE note LIKE '%trace_gate_contract_violation%' ORDER BY ts DESC LIMIT 10;"
# Expected: no new contract violations after PR A ships
```

**Verify trace content for normal complete exit:**
```json
{
  "uow_id": "<some-id>",
  "register": "operational",
  "execution_summary": "Executor dispatched subagent <run_id>, subprocess exit 0.",
  "surprises": [],
  "prescription_delta": "",
  "gate_score": null,
  "timestamp": "2026-..."
}
```

---

## Sprint Execution

### Setup

1. **Enable WOS execution** — edit `~/lobster-workspace/data/wos-config.json`:
   ```json
   {"execution_enabled": true, "bootup_candidate_gate": false}
   ```
   The executor-heartbeat reads this on each cycle. No restart needed.

2. **Confirm PR A is merged** — the sprint requires `_write_trace_json` to be in the executor before enabling. Do not enable execution first.

3. **Select 10 UoWs** — query the registry for UoWs in ready-for-steward or pending states:
   ```bash
   sqlite3 ~/lobster-workspace/orchestration/registry.db \
     "SELECT id, summary, register, status FROM uow_registry WHERE status IN ('pending', 'ready-for-steward', 'proposed') LIMIT 20;"
   ```

4. **Register composition** — the 10 UoWs should skew heavily operational:
   - At least 7 operational-register UoWs (machine-observable gate commands; these are the primary signal)
   - 1-2 iterative-convergent if available (gate_score path, different trace content)
   - Avoid philosophical and human-judgment UoWs for this sprint — they will not complete without Dan interaction and will produce incomplete observations
   - Confirm register by checking the `register` column; if it says `operational` (the default), that is acceptable

5. **Baseline check** — confirm no trace.json files exist before enabling:
   ```bash
   ls ~/lobster-workspace/orchestration/outputs/*.trace.json 2>/dev/null | wc -l
   # Expected: 0
   ```

### Running

**Enable the executor:**
```bash
# Edit wos-config.json to set execution_enabled: true
# The executor-heartbeat runs on its own schedule; no manual trigger needed
```

**Monitor via audit log:**
```bash
# Watch for executor claims and completions
sqlite3 ~/lobster-workspace/orchestration/registry.db \
  "SELECT ts, uow_id, event, from_status, to_status FROM audit_log ORDER BY ts DESC LIMIT 20;"
```

**Watch trace file accumulation:**
```bash
# Run periodically — expect one .trace.json per completed UoW
ls ~/lobster-workspace/orchestration/outputs/*.trace.json 2>/dev/null | wc -l
```

**Self-check cadence:** Check every 30-60 minutes during an active window. The executor-heartbeat fires on its configured schedule; UoWs may take 15-45 minutes each depending on complexity.

### Observation Checklist (per UoW, after completion)

For each UoW that returns to `ready-for-steward` status, collect:

- [ ] `result.json` written? Check `ls {output_dir}/{uow_id}.result.json`
- [ ] `trace.json` written? (binary — this is the new signal) Check `ls {output_dir}/{uow_id}.trace.json`
- [ ] `trace.json` has non-null `execution_summary`? Check field is non-empty string
- [ ] `trace.json` has `surprises` field? (even empty list `[]` is valid for normal complete)
- [ ] `trace.json` has `prescription_delta`? (even empty string `""` is valid for normal complete)
- [ ] `gate_score` field present and null for operational UoWs? (null is correct here)
- [ ] `corrective_traces` row written to DB? Verify:
  ```bash
  sqlite3 ~/lobster-workspace/orchestration/registry.db \
    "SELECT id, uow_id, register, execution_summary FROM corrective_traces WHERE uow_id = '{uow_id}';"
  ```
- [ ] Steward re-entry: did the Steward process the UoW on next cycle without a `trace_gate_contract_violation`? Check audit_log for the UoW.
- [ ] Did cycle time (claim → ready-for-steward) change vs pre-PR-A baseline? (Check started_at / completed_at on the uow_registry row)

**SQLite queries for observation:**
```bash
# Full UoW lifecycle view
sqlite3 ~/lobster-workspace/orchestration/registry.db \
  "SELECT id, status, register, steward_cycles, started_at, completed_at FROM uow_registry WHERE id = '{uow_id}';"

# Audit log for this UoW
sqlite3 ~/lobster-workspace/orchestration/registry.db \
  "SELECT ts, event, from_status, to_status, note FROM audit_log WHERE uow_id = '{uow_id}' ORDER BY ts;"

# Steward trace gate events
sqlite3 ~/lobster-workspace/orchestration/registry.db \
  "SELECT ts, note FROM audit_log WHERE uow_id = '{uow_id}' AND note LIKE '%trace_gate%';"
```

### Aggregate Signals (after all 10 UoWs)

**Trace write rate: X/10**
- Anything less than 10/10 is a bug in PR A (missed exit path), not a signal about the system
- Query: `SELECT COUNT(*) FROM corrective_traces;` should equal total UoW completions

**Prescription_delta quality distribution:**
Classify each trace's `prescription_delta` field:
- Empty string `""` — expected for normal complete in PR A; not a problem
- Placeholder text (e.g., "exception before subagent dispatch") — structural, expected
- Rich content — any trace where the subagent wrote back actionable prescription changes

In PR A, most prescription_delta values will be empty or minimal. This is expected — the substrate is being installed, not the feedback loop. Rich content emerges in PRs B-D when subagents are instructed to self-reflect.

**Surprises field:**
- How many traces have non-empty `surprises`?
- What do they reveal? (Executor dispatch failures, timeout patterns, missing artifacts)
- Empty list is expected for normal complete in PR A

**Steward behavior change:**
Did any trace.json cause the Steward to behave differently on re-entry (vs pre-PR-A baseline where the gate always triggered contract_violation)? Check: no new `trace_gate_contract_violation` entries for completed UoWs.

**Gate score baseline:**
For any iterative-convergent UoWs in the set: did `gate_score` populate? Value should be null in PR A unless gate_score logic was added early (not in scope for PR A).

---

## Sprint Success Criteria

| Criterion | Target | Query |
|---|---|---|
| Trace write rate | 10/10 executor returns produce trace.json | `SELECT COUNT(*) FROM corrective_traces;` |
| Schema validity | All 10 traces pass schema validation (all required fields present) | Manual check of each .trace.json |
| DB write rate | 10/10 traces appear in corrective_traces table | Same query; count should match file count |
| No new contract violations | `trace_gate_contract_violation` count = 0 for the 10 UoWs | Query audit_log for this event |
| Steward gate passes | Steward re-entry does not stall on trace gate for these UoWs | Verify prescribe branch proceeds on cycle 2 without gate block |
| Cycle time regression | No regression >50% vs pre-PR-A baseline | Compare started_at → completed_at before and after |

Note: PR A traces will have minimal `surprises` and empty `prescription_delta` — this is not a failure criterion for Sprint 001. Rich trace content is a success criterion for a later sprint after PRs B-D ship.

---

## Sprint Failure Modes

| Failure | Interpretation | Action |
|---|---|---|
| Trace write rate < 10/10 | Missed exit path in PR A | Identify which exit path (check audit_log for the UoWs with no trace); fix and re-run |
| trace.json written but not in DB | `_insert_corrective_trace` silently failing | Check executor logs for the `failed to insert corrective_trace` warning; fix DB path or schema mismatch |
| trace.json schema invalid | `_build_trace` missing a field | Fix schema and re-run; traces from completed UoWs can be backfilled manually if needed |
| Steward still logs contract violations | Trace gate not reading the file correctly | Check path derivation: `{output_ref}.trace.json` vs `{output_ref}.result.json` pattern |
| Cycle time regresses >50% | trace write is blocking execution | Make trace write best-effort (already should be — verify `_insert_corrective_trace` does not raise) |
| 0 UoWs complete end-to-end | Catastrophic infrastructure failure | This is the critical signal: do NOT proceed to PRs B-D. Investigate executor dispatch (`_dispatch_via_claude_p` subprocess), heartbeat scheduling, DB connectivity |
| UoWs complete but all fail | Executor dispatch succeeds but subagent always fails | Check the functional-engineer subagent success rate; issue may be with UoW content quality, not executor infrastructure |
| 1-3 UoWs complete, rest fail | Partial infrastructure reliability | Could be developmental (bad UoW content) or early-stage catastrophic (intermittent dispatch failure) — examine whether failures cluster by register, time of day, or UoW complexity |

**The catastrophic vs developmental distinction:**

The sprint is designed to distinguish two very different system states:

- **Developmental failure:** Some UoWs complete, failure patterns vary by register or content, some cycles close successfully. This means the pipeline mechanics work and PRs B-D will improve routing precision.

- **Catastrophic failure:** Executor dispatch fails uniformly (0 or near-0 completions), failures are homogeneous regardless of register, no end-to-end cycle closes. This means something structural is broken (subprocess launch, DB connectivity, heartbeat scheduler) that must be fixed before routing precision work has any meaning.

V2's 50-UoW overnight run (250/252 failures) was likely catastrophic — the pipeline was running but dispatch was category-wrong for almost every UoW. This sprint will reveal whether the post-V2 infrastructure cleanup resolved that or whether a deeper issue persists.

---

## What This Sprint Gates

**If sprint succeeds** (10/10 trace write rate, no contract violations, at least some UoWs complete end-to-end):
- Dispatch PR B: register-appropriate executor routing via `executor_type` dispatch table
- Dispatch PR C: Steward reads corrective traces at diagnosis time (trace injection)
- These can be developed in parallel; PR B should land before PR C

**If sprint reveals catastrophic failure** (0 UoWs complete, or uniform dispatch failure):
- Do not proceed to PRs B-D
- Investigate executor dispatch infrastructure: `_dispatch_via_claude_p` subprocess launch, `claude` binary availability, DB connectivity, heartbeat scheduling
- Fix infrastructure reliability before routing precision work

**If sprint reveals partial/ambiguous results** (some UoWs complete, failure patterns unclear):
- Analyze failure clustering before deciding
- If failures cluster by UoW content quality (poorly-formed success criteria, ambiguous prescriptions): proceed to PR B/C, note content quality as a parallel investigation
- If failures cluster by time or appear random: likely infrastructure intermittency; investigate before PRs B-D

**The question answered:**
Is V2's 0.8% success rate from register-blindness (developmental — UoWs were dispatched into category-wrong execution contexts) or from executor dispatch failure (catastrophic — the pipeline itself was unreliable)? This sprint is the diagnostic instrument.

---

## Template Notes (for future sprints)

This document is Sprint 001 in the WOS V3 sprint series. Future sprint documents should follow this structure and naming convention.

**Required sections for all future sprint docs:**

- **Purpose** (2-3 sentences): Why this sprint exists, what question it answers, what it gates
- **Sprint Boundaries**: In scope / out of scope / duration
- **Pre-Sprint State**: Document system state before changes; include current config values, what is and is not yet implemented, which DB tables will be written
- **PR Being Tested**: Full implementation spec with function signatures, file locations with line references, all exit paths, schema, and what the executor/agent can and cannot fill
- **Testing the PR**: Gate commands to verify the change worked; DB queries to check after
- **Sprint Execution**: Setup steps, how to run, observation checklist (per-unit and aggregate)
- **Sprint Success Criteria**: Table format, binary criteria, specific queries
- **Sprint Failure Modes**: Table with failure, interpretation, and action for each mode
- **What This Sprint Gates**: Decision criteria for next action (dispatch vs investigate)

**Label pattern:** `wos-v3-sprint-NNN.md`

**Key design principle:** Each sprint doc must be executable from a fresh context window with no conversation history. Every file path, line reference, DB query, and decision criterion must be stated explicitly. Do not assume prior knowledge of WOS architecture — link to the canonical spec documents and quote the relevant sections.

**Canonical spec documents to link in each sprint:**
- `docs/wos-v3-proposal.md` — register taxonomy, trace.json schema, architecture
- `docs/wos-v3-steward-executor-spec.md` — 6 V3 changes, PR sequencing table
- `philosophy/frontier/wos-v3-convergence.md` — S1-S5 seeds, final bearings

---

## Related Documents

- **[wos-v3-steward-executor-spec.md](wos-v3-steward-executor-spec.md)** — Implementation spec for all 6 V3 changes, PR sequencing, testability notes. Change 6 (trace.json write requirement) is the full spec for PR A.
- **[wos-v3-proposal.md](wos-v3-proposal.md)** — Foundational V3 design: register taxonomy, corrective trace contract (section 4), trace.json schema (section 6), what's different from V2 (section 7).
- **[wos-v3-convergence.md](../philosophy/frontier/wos-v3-convergence.md)** — Final bearings: S1-S5 seeds, sprint sequencing rationale, developmental vs catastrophic failure framing.
- **[corrective-trace-loop-gain-research.md](corrective-trace-loop-gain-research.md)** — Loop gain bounding research (S1). Relevant for PR B when `prescription_delta` injection is added to the Steward.
- **migrations/0007_wos_v3_register_and_corrective_traces.sql** — DB schema for `corrective_traces` table and migration that added `register`, `uow_mode`, `closed_at`, `close_reason` fields.
