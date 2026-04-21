# WOS Pipeline Health Loop

**Job**: ralph-loop
**Schedule**: Every 3 hours (`0 */3 * * *`)
**Purpose**: Self-diagnose, self-heal, and track robustness of the WOS steward/executor pipeline without human prompting.

## Overview

Each run performs one full WOS test run cycle: inject synthetic test UoWs, wait for execution, observe outcomes, report, fix fixable issues, and track progress toward the robustness goal (5 consecutive clean runs across 3+ UoW types).

**Robustness goal**: `consecutive_clean_runs >= 5` in ralph-state.json.

---

## Key Paths

- Registry DB: `/home/lobster/lobster-workspace/orchestration/registry.db`
- Pipeline health state: `/home/lobster/lobster-workspace/data/ralph-state.json`
- Steward log: `/home/lobster/lobster-workspace/scheduled-jobs/logs/steward-heartbeat.log`
- Executor log: `/home/lobster/lobster-workspace/scheduled-jobs/logs/executor-heartbeat.log`
- Reports dir: `/home/lobster/lobster-workspace/data/ralph-reports/`
- WOS config: `/home/lobster/lobster-workspace/data/wos-config.json`
- Lobster repo: `/home/lobster/lobster/`

---

## Step 0 — Load State and Check for In-Flight UoWs

Read ralph-state.json:

```bash
cat /home/lobster/lobster-workspace/data/ralph-state.json 2>/dev/null || echo '{"consecutive_clean_runs":0,"total_runs":0,"last_run_ts":null,"last_anomalies":[]}'
```

Then check if any previous WOS test UoWs are still in a non-terminal state:

```python
import sqlite3, json
conn = sqlite3.connect('/home/lobster/lobster-workspace/orchestration/registry.db')
cur = conn.cursor()
cur.execute("""
    SELECT id, status, summary, updated_at
    FROM uow_registry
    WHERE source = 'ralph-test'
      AND status NOT IN ('done', 'failed', 'expired')
    ORDER BY created_at DESC
""")
rows = cur.fetchall()
print(json.dumps([dict(zip(['id','status','summary','updated_at'], r)) for r in rows], indent=2))
```

**If any WOS test UoWs are non-terminal**: do NOT inject new ones. Skip to Step 3 (Observe) to check on their progress. Note this in the report.

---

## Step 1 — Inject Test UoWs (skip if non-terminal UoWs exist from Step 0)

Inject 3 synthetic test UoWs — one of each type — using direct SQLite insert. Use `source = 'ralph-test'` so they are distinguishable from production UoWs.

Rotate the UoW type based on `total_runs % 3`:
- Run 0, 3, 6... → inject one of each type (A + B + C)
- Always inject all three types; rotation just changes which variant of each type

**Additionally, on cycles where `cycle_number % 3 == 0`** (i.e., every third cycle), also inject a type-D long-running UoW. `cycle_number` is `total_runs` from the state loaded in Step 0 (before incrementing).

**Type A — simple-doc-write**: Write a short markdown file.

**Type D — long-running timing validation** (injected only when `cycle_number % 3 == 0`): A multi-step task that naturally takes 7–9 minutes to complete, designed to exercise the 300-second startup-sweep threshold from PR #555. The executor must complete this UoW without the startup sweep interrupting it. The task MUST run longer than 300 seconds so that the startup sweep fires during execution — only then can suppression be validated.

The type-D task must instruct the executor to:
1. Recursively find ALL `.md` files in `/home/lobster/lobster/` and `/home/lobster/lobster-workspace/design/` (expected: 100+ files)
2. For each file: read it, count its words, and write a one-line summary (filename + word count + first sentence) to `/tmp/ralph-test-{run_id}-d-summaries.txt`, appending after each file
3. Pause 2 seconds between each file read
4. After all files, query the WOS registry DB for the full schema (all tables, all columns) and row counts, and append a complete table report to the same file
5. Write a final line "RALPH type-D complete: <ISO timestamp>" to the file

This is intentionally multi-step and IO-heavy. The executor subprocess should run for roughly 7–9 minutes. The 2-second pause per file is required — do not skip it.

```python
import sqlite3, uuid, json
from datetime import datetime, timezone

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
now = datetime.now(timezone.utc).isoformat()
date = datetime.now(timezone.utc).date().isoformat()
run_id = uuid.uuid4().hex[:6]

# Load cycle_number from state (total_runs before this cycle's increment)
import json as _json
from pathlib import Path as _Path
_state_raw = _Path('/home/lobster/lobster-workspace/data/ralph-state.json').read_text() if _Path('/home/lobster/lobster-workspace/data/ralph-state.json').exists() else '{}'
_state = _json.loads(_state_raw) if _state_raw.strip() else {}
cycle_number = _state.get("total_runs", 0)
inject_type_d = (cycle_number % 3 == 0)

uows = [
    {
        "id": f"uow_{date.replace('-','')}_{run_id}_a",
        "source": "ralph-test",
        "summary": f"WOS test type-A: write markdown file to /tmp/ralph-test-{run_id}.md",
        "success_criteria": f"File /tmp/ralph-test-{run_id}.md exists and contains 'WOS test output'",
        "status": "ready-for-steward",
        "type": "executable",
        "posture": "solo",
        "output_ref": f"/tmp/ralph-test-{run_id}-a.md",
        "route_reason": "ralph-test injection",
        "notes": json.dumps({"ralph_run_id": run_id, "ralph_type": "simple-doc-write"}),
    },
    {
        "id": f"uow_{date.replace('-','')}_{run_id}_b",
        "source": "ralph-test",
        "summary": f"WOS test type-B: search /home/lobster/lobster/src for 'UoWStatus' and summarize all files found",
        "success_criteria": "A list of files containing 'UoWStatus' is produced and written to output_ref",
        "status": "ready-for-steward",
        "type": "executable",
        "posture": "solo",
        "output_ref": f"/tmp/ralph-test-{run_id}-b.txt",
        "route_reason": "ralph-test injection",
        "notes": json.dumps({"ralph_run_id": run_id, "ralph_type": "multi-step-search"}),
    },
    {
        "id": f"uow_{date.replace('-','')}_{run_id}_c",
        "source": "ralph-test",
        "summary": f"WOS test type-C: read /home/lobster/lobster/README.md and verify it exists",
        "success_criteria": "README.md is confirmed to exist and its first line is captured in output_ref",
        "status": "ready-for-steward",
        "type": "executable",
        "posture": "solo",
        "output_ref": f"/tmp/ralph-test-{run_id}-c.txt",
        "route_reason": "ralph-test injection",
        "notes": json.dumps({"ralph_run_id": run_id, "ralph_type": "expected-idempotent"}),
    },
]

if inject_type_d:
    uows.append({
        "id": f"uow_{date.replace('-','')}_{run_id}_d",
        "source": "ralph-test",
        "summary": (
            f"WOS test type-D: recursively find ALL .md files in /home/lobster/lobster/ and "
            f"/home/lobster/lobster-workspace/design/, then for each file read it, count its words, "
            f"and write a one-line summary (filename + word count + first sentence) to "
            f"/tmp/ralph-test-{run_id}-d-summaries.txt with a 2-second pause between each file. "
            f"After all files, query the full WOS registry DB schema (all tables, all columns) and "
            f"row counts, and append the complete table report. "
            f"This task is expected to take 7-9 minutes — the 2-second pause per file is required. "
            f"Finish by writing 'WOS test type-D complete: <ISO timestamp>' as the final line."
        ),
        "success_criteria": (
            f"File /tmp/ralph-test-{run_id}-d-summaries.txt exists, "
            f"contains one-line summaries for all discovered .md files (expected 100+), "
            f"contains a full WOS registry DB schema report with all tables and columns, "
            f"and ends with 'WOS test type-D complete:'. "
            f"VALIDATED only counts if UoW runtime exceeded 300s — check start/end timestamps "
            f"in registry DB (created_at vs updated_at for the type-D UoW record)."
        ),
        "status": "ready-for-steward",
        "type": "executable",
        "posture": "solo",
        "output_ref": f"/tmp/ralph-test-{run_id}-d-summaries.txt",
        "route_reason": "ralph-test injection",
        "notes": json.dumps({"ralph_run_id": run_id, "ralph_type": "long-running-timing-validation", "pr_555_validation": True}),
    })

conn = sqlite3.connect(db)
for u in uows:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("""
        INSERT INTO uow_registry
            (id, type, source, status, posture, summary, success_criteria,
             output_ref, route_reason, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        u["id"], u.get("type","executable"), u["source"], u["status"],
        u.get("posture","solo"), u["summary"], u["success_criteria"],
        u.get("output_ref"), u.get("route_reason"), u.get("notes","{}"),
        now, now
    ))
    conn.execute("""
        INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (now, u["id"], "injected", None, u["status"], "ralph-loop",
          f"WOS test injection: {u['notes']}"))
    conn.commit()
    print(f"Injected: {u['id']} ({u['summary'][:60]})")

conn.close()
print(f"Type-D injected this cycle: {inject_type_d} (cycle_number={cycle_number})")
```

**Important**: After injection, the steward and executor heartbeats pick up UoWs automatically (they run every 3 minutes). Do not call them manually.

---

## Step 2 — Wait and Poll (10 or 15 minutes max)

Poll every 60 seconds until all injected UoWs reach terminal state (`done`, `failed`, `expired`), or timeout.

**Timeout**: Use **15 minutes** if a type-D UoW was injected this cycle (`inject_type_d == True`); otherwise use the standard **10 minutes**. Type-D UoWs are designed to run for 7–9 minutes, so the extended window is required.

```python
import sqlite3, time, json
from datetime import datetime, timezone

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
run_id = "<run_id from Step 1>"  # substitute actual run_id
inject_type_d = <inject_type_d from Step 1>  # True or False
uow_ids = [f"uow_{date}_{run_id}_a", f"uow_{date}_{run_id}_b", f"uow_{date}_{run_id}_c"]
if inject_type_d:
    uow_ids.append(f"uow_{date}_{run_id}_d")

terminal = {'done', 'failed', 'expired'}
timeout_seconds = 900 if inject_type_d else 600  # 15 min for type-D cycles (7-9 min runtime), 10 min otherwise
deadline = time.time() + timeout_seconds

while time.time() < deadline:
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, status, steward_cycles, updated_at FROM uow_registry WHERE id IN ({','.join('?'*len(uow_ids))})",
        uow_ids
    )
    rows = {r[0]: r for r in cur.fetchall()}
    conn.close()

    all_terminal = all(rows.get(uid, (None,'pending'))[1] in terminal for uid in uow_ids)
    print(f"[{datetime.now(timezone.utc).isoformat()}] Status: " +
          str({uid: rows.get(uid,(None,'missing'))[1] for uid in uow_ids}))

    if all_terminal:
        print("All UoWs reached terminal state.")
        break
    time.sleep(60)
else:
    print(f"TIMEOUT: not all UoWs reached terminal state within {timeout_seconds // 60} minutes.")
```

---

## Step 3 — Observe

Collect outcomes and anomalies:

```python
import sqlite3, json
from datetime import datetime, timezone

db = '/home/lobster/lobster-workspace/orchestration/registry.db'

# Status summary for all ralph-test UoWs from this run
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("""
    SELECT id, status, steward_cycles, output_ref, updated_at,
           workflow_artifact IS NOT NULL as has_artifact, notes
    FROM uow_registry
    WHERE source = 'ralph-test'
    ORDER BY created_at DESC LIMIT 20
""")
rows = cur.fetchall()
cols = ['id','status','steward_cycles','output_ref','updated_at','has_artifact','notes']
uow_data = [dict(zip(cols, r)) for r in rows]
print(json.dumps(uow_data, indent=2))

# Posture anomalies: UoWs stuck in non-terminal state > 5 minutes
cur.execute("""
    SELECT id, status, updated_at FROM uow_registry
    WHERE source = 'ralph-test'
      AND status NOT IN ('done', 'failed', 'expired')
      AND updated_at < datetime('now', '-5 minutes')
""")
stalled = cur.fetchall()
print("Stalled UoWs:", stalled)
conn.close()
```

Also tail the recent logs:

```bash
tail -30 /home/lobster/lobster-workspace/scheduled-jobs/logs/steward-heartbeat.log 2>/dev/null | grep -v "^$" | tail -20
tail -30 /home/lobster/lobster-workspace/scheduled-jobs/logs/executor-heartbeat.log 2>/dev/null | grep -v "^$" | tail -20
```

**Anomaly checklist** — flag any of the following:
- Any UoW in non-terminal state after the polling timeout (10 min standard, 15 min for type-D cycles)
- `steward_cycles` = 0 after the wait period (steward never touched it)
- `has_artifact = 0` for a UoW that reached `done` (executor did not produce output)
- Errors in steward or executor logs during the window
- `wos-config.json` `execution_enabled = false` (executor paused — note this, do not treat as anomaly)
- **Type-D timing validation**: if a type-D UoW was injected and `steward_cycles > 2` for that UoW, flag as `pr_555_timing_fix_validation_failed` (the startup sweep re-prescribed while the subprocess was still running — see Step 4 for evaluation logic). Also verify UoW runtime exceeded 300s by comparing `created_at` to `updated_at` in registry DB — if runtime was under 300s, the sweep never fired during execution and VALIDATED is meaningless.

**Deep exchange audit** — after checking terminal states, run these four additional checks. "Checking did it complete" is not sufficient; the exchange quality must also be audited.

**Audit 1 — Steward cycle count:**

```python
import sqlite3, json

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
run_id = "<run_id from Step 1>"  # substitute actual run_id
inject_type_d = <inject_type_d from Step 1>  # True or False
uow_ids = [f"uow_{date}_{run_id}_a", f"uow_{date}_{run_id}_b", f"uow_{date}_{run_id}_c"]
if inject_type_d:
    uow_ids.append(f"uow_{date}_{run_id}_d")

conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute(
    f"SELECT id, steward_cycles FROM uow_registry WHERE id IN ({','.join('?'*len(uow_ids))})",
    uow_ids
)
for uow_id, cycles in cur.fetchall():
    print(f"{uow_id}: steward_cycles={cycles}")
    if cycles >= 3:
        print(f"  ANOMALY: steward_cycles={cycles} >= 3 (clean run expects <= 2)")
conn.close()
```

A clean first-execution run should have `steward_cycles <= 2` (1 to prescribe, 1 to close). If any UoW has `steward_cycles >= 3`, flag as anomaly type `steward_cycle_excess` with detail including the actual cycle count.

**Note for type-D**: If the type-D UoW has `steward_cycles > 2`, this specifically indicates the startup sweep re-prescribed during active execution — see Step 4 for the PR #555 validation outcome logic.

**Audit 2 — PRSC reasons from steward_log:**

```python
import sqlite3, json

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
run_id = "<run_id from Step 1>"  # substitute actual run_id
inject_type_d = <inject_type_d from Step 1>  # True or False
uow_ids = [f"uow_{date}_{run_id}_a", f"uow_{date}_{run_id}_b", f"uow_{date}_{run_id}_c"]
if inject_type_d:
    uow_ids.append(f"uow_{date}_{run_id}_d")

conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute(
    f"SELECT id, steward_log FROM uow_registry WHERE id IN ({','.join('?'*len(uow_ids))})",
    uow_ids
)

ACCEPTABLE_FIRST_EXEC_PRSC = {"first_execution", "never dispatched", "not yet dispatched"}
BAD_FIRST_EXEC_PRSC_PATTERNS = ["output_ref is null", "file does not exist", "is empty"]

for uow_id, steward_log_raw in cur.fetchall():
    if not steward_log_raw:
        continue
    try:
        log_entries = json.loads(steward_log_raw) if isinstance(steward_log_raw, str) else steward_log_raw
        if not isinstance(log_entries, list):
            log_entries = [log_entries]
    except Exception:
        log_entries = []

    for entry in log_entries:
        reason = str(entry.get("reason", entry.get("prsc_reason", ""))).lower()
        posture = str(entry.get("posture", entry.get("from_posture", ""))).lower()
        tag = str(entry.get("tag", "")).upper()

        if "[PRSC]" in str(entry) or tag == "PRSC":
            is_bad = any(pat in reason for pat in BAD_FIRST_EXEC_PRSC_PATTERNS)
            is_first_exec_posture = "first_execution" in posture
            if is_bad and is_first_exec_posture:
                print(f"  ANOMALY [{uow_id}]: first_execution UoW has bad PRSC reason: '{reason}'")
                print(f"    This means the steward conflates first_execution with crashed_output_ref")
            else:
                print(f"  OK [{uow_id}]: PRSC reason='{reason}' posture='{posture}'")

conn.close()
```

Acceptable PRSC reasons for a first-execution UoW: "first_execution", "never dispatched", or similar new-work reasons. NOT acceptable for a first-execution UoW: "output_ref is null or file does not exist or is empty" — this means the steward misread posture, treating a fresh UoW as a crashed one. Flag as anomaly type `prsc_first_exec_conflation`.

**Audit 3 — Duplicate dispatches:**

```python
import sqlite3, json

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
run_id = "<run_id from Step 1>"  # substitute actual run_id
inject_type_d = <inject_type_d from Step 1>  # True or False
uow_ids = [f"uow_{date}_{run_id}_a", f"uow_{date}_{run_id}_b", f"uow_{date}_{run_id}_c"]
if inject_type_d:
    uow_ids.append(f"uow_{date}_{run_id}_d")

conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute(
    f"""SELECT uow_id, COUNT(*) as dispatch_count
        FROM audit_log
        WHERE uow_id IN ({','.join('?'*len(uow_ids))})
          AND event IN ('dispatched', 'executor_dispatched', 'dispatch')
        GROUP BY uow_id
        HAVING COUNT(*) > 1""",
    uow_ids
)
duplicates = cur.fetchall()
for uow_id, count in duplicates:
    print(f"  ANOMALY [{uow_id}]: dispatched {count} times (expected 1)")
conn.close()
```

Any UoW appearing in `audit_log` with a dispatch event more than once is a duplicate dispatch anomaly. Flag as anomaly type `duplicate_dispatch`.

**Audit 4 — Posture transitions:**

```python
import sqlite3, json

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
run_id = "<run_id from Step 1>"  # substitute actual run_id
inject_type_d = <inject_type_d from Step 1>  # True or False
uow_ids = [f"uow_{date}_{run_id}_a", f"uow_{date}_{run_id}_b", f"uow_{date}_{run_id}_c"]
if inject_type_d:
    uow_ids.append(f"uow_{date}_{run_id}_d")

conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute(
    f"""SELECT uow_id, event, from_status, to_status, ts, note
        FROM audit_log
        WHERE uow_id IN ({','.join('?'*len(uow_ids))})
        ORDER BY uow_id, ts""",
    uow_ids
)
rows = cur.fetchall()
conn.close()

BAD_POSTURES = {"crashed_output_ref_missing", "steward_cycle_cap", "executor_orphan"}

for row in rows:
    uow_id, event, from_status, to_status, ts, note = row
    note_str = str(note or "").lower()
    # Check if any bad posture appears in the audit trail
    for bad in BAD_POSTURES:
        if bad in note_str or bad == from_status or bad == to_status:
            print(f"  ANOMALY [{uow_id}]: bad posture '{bad}' observed in audit trail at {ts}")
            print(f"    event={event} from={from_status} to={to_status}")

# Also check final posture in uow_registry
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute(
    f"SELECT id, posture, status FROM uow_registry WHERE id IN ({','.join('?'*len(uow_ids))})",
    uow_ids
)
for uow_id, posture, status in cur.fetchall():
    if posture in BAD_POSTURES:
        print(f"  ANOMALY [{uow_id}]: final posture='{posture}' (expected first_execution or execution_complete)")
    else:
        print(f"  OK [{uow_id}]: final posture='{posture}' status='{status}'")
conn.close()
```

Expected posture sequence for a healthy first-execution UoW: `first_execution → [dispatch] → execution_complete`. Any unexpected posture (`crashed_output_ref_missing`, `steward_cycle_cap`, `executor_orphan`) appearing in the audit trail or as the final posture is a `bad_posture_transition` anomaly.

**Audit 5 — Steward agenda trace completeness**

For each WOS test UoW that reached done or failed:
- Parse `steward_agenda` JSON
- Assert: count of entries with `"cycle"` key == `steward_cycles` for that UoW
- Assert: each trace entry has `posture` (non-empty string), `posture_rationale` (non-empty string), `success_criteria_checked` (list), `anomalies` (list), `prediction` (string or None), `timestamp` (non-empty string)
- Flag anomaly type `agenda_trace_missing` if trace count < `steward_cycles`
- Flag anomaly type `agenda_trace_malformed` if any entry fails field validation

**Audit 6 — Side effects validation**

For each UoW that reached `done` status:
- Parse `workflow_artifact` JSON
- Assert: `workflow_artifact.result.side_effects` exists (non-null)
- Assert: `side_effects` is a list or dict (populated, not empty)
- Assert: `side_effects` content logically matches the UoW's execution intent (infer from `summary` and `success_criteria`)
- Flag anomaly type `side_effects_missing` if `side_effects` is absent or null
- Flag anomaly type `side_effects_empty` if `side_effects` exists but is empty (no entries)
- Flag anomaly type `side_effects_mismatch` if `side_effects` content does not align with execution intent (e.g., type-A file write task reports zero files written, or type-B search reports zero matches found)

```python
import sqlite3, json

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
run_id = "<run_id from Step 1>"  # substitute actual run_id
inject_type_d = <inject_type_d from Step 1>  # True or False
uow_ids = [f"uow_{date}_{run_id}_a", f"uow_{date}_{run_id}_b", f"uow_{date}_{run_id}_c"]
if inject_type_d:
    uow_ids.append(f"uow_{date}_{run_id}_d")

conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute(
    f"SELECT id, status, summary, workflow_artifact FROM uow_registry WHERE id IN ({','.join('?'*len(uow_ids))})",
    uow_ids
)

for uow_id, status, summary, artifact_raw in cur.fetchall():
    if status != 'done':
        continue
    
    if not artifact_raw:
        print(f"  ANOMALY [{uow_id}]: status=done but no workflow_artifact")
        continue
    
    try:
        artifact = json.loads(artifact_raw) if isinstance(artifact_raw, str) else artifact_raw
        result = artifact.get("result", {})
        side_effects = result.get("side_effects")
        
        if side_effects is None:
            print(f"  ANOMALY [{uow_id}]: workflow_artifact.result.side_effects is missing (None)")
        elif not side_effects:
            print(f"  ANOMALY [{uow_id}]: workflow_artifact.result.side_effects is empty (expected populated)")
        else:
            # Check alignment with execution intent
            summary_lower = str(summary or "").lower()
            if "type-a" in summary_lower or "write markdown" in summary_lower:
                # Type-A: expect file write side effect
                if isinstance(side_effects, list) and len(side_effects) == 0:
                    print(f"  ANOMALY [{uow_id}]: type-A side_effects is empty list (expected file write)")
                else:
                    print(f"  OK [{uow_id}]: type-A side_effects present and populated")
            elif "type-b" in summary_lower or "search" in summary_lower:
                # Type-B: expect search/match results in side effects
                if isinstance(side_effects, list) and len(side_effects) == 0:
                    print(f"  ANOMALY [{uow_id}]: type-B side_effects is empty list (expected search results)")
                else:
                    print(f"  OK [{uow_id}]: type-B side_effects present and populated")
            else:
                print(f"  OK [{uow_id}]: side_effects present (content: {type(side_effects).__name__})")
    except Exception as exc:
        print(f"  ANOMALY [{uow_id}]: failed to parse workflow_artifact: {exc}")

conn.close()
```

**Audit 7 — Dan Interrupt trace validation**

For each UoW that has entries in `steward_agenda`:
- Search the `steward_agenda` entries for records with `external_dependency: "dan"` (Dan-interrupt markers)
- For each Dan Interrupt entry found:
  - Assert: `diagnosis` field is present and non-empty (why Dan was asked)
  - Assert: `interrupt_source` field is present and non-empty (which component/decision requested human input)
  - Assert: `timestamp` field is present (when the interrupt was recorded)
- Flag anomaly type `dan_interrupt_diagnosis_missing` if a Dan Interrupt lacks a diagnosis
- Flag anomaly type `dan_interrupt_source_missing` if a Dan Interrupt lacks interrupt_source
- If no Dan Interrupts found, this is OK (test may not have triggered human decision points)

```python
import sqlite3, json

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
run_id = "<run_id from Step 1>"  # substitute actual run_id
inject_type_d = <inject_type_d from Step 1>  # True or False
uow_ids = [f"uow_{date}_{run_id}_a", f"uow_{date}_{run_id}_b", f"uow_{date}_{run_id}_c"]
if inject_type_d:
    uow_ids.append(f"uow_{date}_{run_id}_d")

conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute(
    f"SELECT id, steward_agenda FROM uow_registry WHERE id IN ({','.join('?'*len(uow_ids))})",
    uow_ids
)

for uow_id, agenda_raw in cur.fetchall():
    if not agenda_raw:
        print(f"  OK [{uow_id}]: no steward_agenda recorded (no Dan Interrupts expected)")
        continue
    
    try:
        agenda_entries = json.loads(agenda_raw) if isinstance(agenda_raw, str) else agenda_raw
        if not isinstance(agenda_entries, list):
            agenda_entries = [agenda_entries]
        
        dan_interrupts = [e for e in agenda_entries if e.get("external_dependency") == "dan"]
        
        if not dan_interrupts:
            print(f"  OK [{uow_id}]: no Dan Interrupts in steward_agenda")
        else:
            for interrupt in dan_interrupts:
                diagnosis = interrupt.get("diagnosis", "").strip()
                interrupt_source = interrupt.get("interrupt_source", "").strip()
                timestamp = interrupt.get("timestamp", "").strip()
                
                if not diagnosis:
                    print(f"  ANOMALY [{uow_id}]: Dan Interrupt at {timestamp} missing diagnosis")
                if not interrupt_source:
                    print(f"  ANOMALY [{uow_id}]: Dan Interrupt at {timestamp} missing interrupt_source")
                if diagnosis and interrupt_source and timestamp:
                    print(f"  OK [{uow_id}]: Dan Interrupt recorded with diagnosis, source, and timestamp")
    except Exception as exc:
        print(f"  ANOMALY [{uow_id}]: failed to parse steward_agenda: {exc}")

conn.close()
```

**Audit 8 — Steward log↔agenda alignment**

For each UoW with both `steward_log` and `steward_agenda`:
- Parse both JSON structures
- Spot-check (sample 2-3 entries from each): for steward_log reasoning entries, verify that corresponding steward_agenda entries have a populated `rationale` field
- Assert: the reasoning recorded in steward_log reasoning entries semantically aligns with the rationale documented in steward_agenda
- Flag anomaly type `steward_reasoning_misalignment` if sampled log entries and agenda entries diverge significantly (e.g., log says "output missing, re-prescribing" but agenda says "success detected, completing")

```python
import sqlite3, json

db = '/home/lobster/lobster-workspace/orchestration/registry.db'
run_id = "<run_id from Step 1>"  # substitute actual run_id
inject_type_d = <inject_type_d from Step 1>  # True or False
uow_ids = [f"uow_{date}_{run_id}_a", f"uow_{date}_{run_id}_b", f"uow_{date}_{run_id}_c"]
if inject_type_d:
    uow_ids.append(f"uow_{date}_{run_id}_d")

conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute(
    f"SELECT id, steward_log, steward_agenda FROM uow_registry WHERE id IN ({','.join('?'*len(uow_ids))})",
    uow_ids
)

for uow_id, log_raw, agenda_raw in cur.fetchall():
    if not log_raw or not agenda_raw:
        print(f"  OK [{uow_id}]: insufficient data for alignment check (log={bool(log_raw)} agenda={bool(agenda_raw)})")
        continue
    
    try:
        log_entries = json.loads(log_raw) if isinstance(log_raw, str) else log_raw
        if not isinstance(log_entries, list):
            log_entries = [log_entries]
        
        agenda_entries = json.loads(agenda_raw) if isinstance(agenda_raw, str) else agenda_raw
        if not isinstance(agenda_entries, list):
            agenda_entries = [agenda_entries]
        
        # Spot-check: sample the first 2-3 reasoning entries from log
        reasoning_entries = [e for e in log_entries if "reasoning" in str(e).lower() or "reason" in str(e).lower()]
        sample_size = min(3, len(reasoning_entries))
        
        if sample_size == 0:
            print(f"  OK [{uow_id}]: no reasoning entries in steward_log (expected for simple UoWs)")
            continue
        
        misalignments = []
        for i, log_entry in enumerate(reasoning_entries[:sample_size]):
            log_reason = str(log_entry.get("reason", log_entry.get("reasoning", ""))).lower()
            # Find corresponding agenda entry (roughly by cycle or timestamp)
            cycle = log_entry.get("cycle")
            corresponding_agenda = None
            if cycle is not None:
                corresponding_agenda = next((e for e in agenda_entries if e.get("cycle") == cycle), None)
            
            if corresponding_agenda:
                agenda_rationale = str(corresponding_agenda.get("rationale", corresponding_agenda.get("posture_rationale", ""))).lower()
                # Check for semantic divergence: key contradiction words
                log_indicators = {"fail", "error", "missing", "mismatch", "re-prescrib", "retry", "crash"}
                agenda_indicators = {"success", "complete", "valid", "done"}
                
                log_has_fail = any(ind in log_reason for ind in log_indicators)
                agenda_has_success = any(ind in agenda_rationale for ind in agenda_indicators)
                
                if log_has_fail and agenda_has_success:
                    misalignments.append(f"cycle {cycle}: log says failure, agenda says success")
            else:
                print(f"  ANOMALY [{uow_id}]: reasoning entry at index {i} has no corresponding agenda entry")
        
        if misalignments:
            for misalign in misalignments:
                print(f"  ANOMALY [{uow_id}]: steward_reasoning_misalignment — {misalign}")
        else:
            print(f"  OK [{uow_id}]: sampled {sample_size} reasoning entries, all align with agenda rationale")
    except Exception as exc:
        print(f"  ANOMALY [{uow_id}]: failed to parse log/agenda for alignment: {exc}")

conn.close()
```

**Build `anomalies_this_run`** — construct a list of anomaly dicts from both the basic checklist above AND the four deep audit checks. This list is written to `last_anomalies` in Step 7 and is used by the next cycle's reproducibility gate in Step 6. Each entry should be a dict:

```python
anomalies_this_run = []
# For each anomaly found, append a dict like:
# {
#   "uow_id": "<id>",
#   "anomaly_type": "stalled|steward_cycles_zero|missing_artifact|log_error|steward_cycle_excess|prsc_first_exec_conflation|duplicate_dispatch|bad_posture_transition|pr_555_timing_fix_validation_failed|side_effects_missing|side_effects_empty|side_effects_mismatch|dan_interrupt_diagnosis_missing|dan_interrupt_source_missing|steward_reasoning_misalignment",
#   "detail": "<brief description including observed values>"
# }
# Examples:
# anomalies_this_run.append({"uow_id": "uow_20260401_abc123_a", "anomaly_type": "stalled", "detail": "still in ready-for-steward after 10 min"})
# anomalies_this_run.append({"uow_id": "uow_20260401_abc123_b", "anomaly_type": "steward_cycle_excess", "detail": "steward_cycles=4, expected <= 2"})
# anomalies_this_run.append({"uow_id": "uow_20260401_abc123_a", "anomaly_type": "prsc_first_exec_conflation", "detail": "[PRSC] output_ref is null or file does not exist for first_execution UoW"})
# anomalies_this_run.append({"uow_id": "uow_20260401_abc123_c", "anomaly_type": "duplicate_dispatch", "detail": "dispatched 2 times"})
# anomalies_this_run.append({"uow_id": "uow_20260401_abc123_b", "anomaly_type": "bad_posture_transition", "detail": "posture crashed_output_ref_missing observed in audit trail"})
# anomalies_this_run.append({"uow_id": "uow_20260401_abc123_d", "anomaly_type": "pr_555_timing_fix_validation_failed", "detail": "steward_cycles=3 for type-D UoW: sweep re-prescribed during active execution"})
# If no anomalies were found, anomalies_this_run remains [].
```

---

## Step 4 — Evaluate: Clean Run or Not?

A **clean run** is defined as:
- All injected UoWs reached `done` or `failed` within the polling timeout (10 min standard, 15 min for type-D cycles)
- No posture anomalies (no stalled UoWs)
- No errors in heartbeat logs during the window
- All five deep exchange audits from Step 3 are clean: `steward_cycles <= 2` per UoW, no bad PRSC reasons for first-execution UoWs, no duplicate dispatches, no unexpected posture transitions, and steward_agenda trace count equals steward_cycles with all entries passing field validation

**Note**: A UoW reaching `failed` is acceptable if the failure reason is expected (e.g., the executor correctly identified an unsolvable task). A `failed` outcome with a recorded `return_reason` counts as clean. A timeout or a UoW with `steward_cycles = 0` after the polling timeout is NOT clean.

**Type-D timing validation outcome** (only applies on cycles where `inject_type_d == True`):

**VALIDATED only counts if UoW runtime exceeded 300s.** First confirm the type-D UoW actually ran long enough to trigger a startup-sweep cycle. If it completed in under 300s, the sweep never fired during execution and no suppression was exercised — report as INCONCLUSIVE, not VALIDATED.

Check both runtime and `steward_cycles`:

```python
type_d_id = f"uow_{date}_{run_id}_d"
# Query steward_cycles and timestamps for type-D
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("SELECT steward_cycles, created_at, updated_at FROM uow_registry WHERE id = ?", (type_d_id,))
row = cur.fetchone()
conn.close()

if row is not None:
    type_d_steward_cycles, created_at, updated_at = row
    # Compute runtime in seconds
    from datetime import datetime, timezone
    try:
        t_start = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        t_end = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
        runtime_seconds = (t_end - t_start).total_seconds()
    except Exception:
        runtime_seconds = None

    runtime_exceeded_300s = runtime_seconds is not None and runtime_seconds > 300

    if not runtime_exceeded_300s:
        print(f"PR #555 timing fix validation INCONCLUSIVE: type-D runtime={runtime_seconds}s (<= 300s). The startup sweep never fired during execution — suppression was not exercised. Increase task workload.")
        # Do NOT add to anomalies_this_run — this is a test-design gap, not a pipeline failure.
    elif type_d_steward_cycles <= 2:
        print(f"PR #555 timing fix VALIDATED: type-D completed with steward_cycles={type_d_steward_cycles} (<= 2) and runtime={runtime_seconds:.0f}s (> 300s). Sweep fired but correctly suppressed re-prescription.")
    else:
        print(f"PR #555 timing fix validation FAILED: type-D UoW steward_cycles={type_d_steward_cycles} (> 2) and runtime={runtime_seconds:.0f}s. Startup sweep re-prescribed during active execution.")
        anomalies_this_run.append({
            "uow_id": type_d_id,
            "anomaly_type": "pr_555_timing_fix_validation_failed",
            "detail": f"steward_cycles={type_d_steward_cycles} for type-D UoW (runtime={runtime_seconds:.0f}s): startup sweep re-prescribed while subprocess was still active"
        })
```

Log the outcome in the Step 5 report under a dedicated "PR #555 Timing Validation" section. Include the runtime in seconds and whether the 300s threshold was exceeded.

Determine: `is_clean_run = True` or `False`.

---

## Step 5 — Report

Create the reports directory and write a markdown report:

```bash
mkdir -p /home/lobster/lobster-workspace/data/ralph-reports/
```

**Generate WOS reports** — after the WOS test run report is written, run wos_report.py on the injected UoW IDs to generate:
1. A summary PDF (sent to Dan via Telegram)
2. A full markdown investigation report (saved locally for deep debugging)

```bash
# Substitute actual UoW IDs from Step 1 (comma-separated, no spaces)
UOW_IDS="uow_<date>_<run_id>_a,uow_<date>_<run_id>_b,uow_<date>_<run_id>_c"
# If type-D was injected, add it: UOW_IDS="${UOW_IDS},uow_<date>_<run_id>_d"

REPORT_TS=$(date +%Y%m%d-%H%M%S)
SUMMARY_PDF="/home/lobster/messages/documents/wos-test-run-${REPORT_TS}.pdf"
FULL_MD="/home/lobster/lobster-workspace/data/ralph-reports/wos-full-${REPORT_TS}.md"

cd /home/lobster/lobster && uv run src/wos_report.py \
  --ids "${UOW_IDS}" \
  --output "${SUMMARY_PDF}" \
  --full-output "${FULL_MD}"
```

The summary PDF is automatically sent to Dan (chat_id 8075091586) with caption "WOS Registry (N UoWs, ids=N) -- timestamp". The full markdown report is saved to `ralph-reports/` for investigation.

If `wos_report.py` fails (e.g., DB not found), log the error and continue — report failure is not a pipeline anomaly.

Write a WOS test run report to `/home/lobster/lobster-workspace/data/ralph-reports/wos-test-run-<YYYY-MM-DD-HHMMSS>.md`:

```markdown
# WOS Test Run Report — <timestamp ET>

## Summary
- Run ID: <run_id>
- Clean run: <yes/no>
- UoWs injected: <3 or 4> (type-A, type-B, type-C[, type-D])
- Type-D injected: <yes/no> (cycle_number=<N>, cycle_number % 3 == 0: <True/False>)

## Outcomes
| UoW ID | Type | Final Status | Steward Cycles | Has Artifact |
|--------|------|-------------|----------------|--------------|
| ...    | ...  | ...         | ...            | ...          |

## Exchange Audit
| UoW ID | Steward Cycles OK? | PRSC Reason OK? | Duplicate Dispatch? | Posture Sequence OK? |
|--------|--------------------|-----------------|--------------------|-----------------------|
| ...    | yes/no (N cycles)  | yes/no (reason) | yes/no             | yes/no (posture)     |

## PR #555 Timing Validation
<!-- Only present when type-D was injected -->
- Type-D UoW: <uow_id>
- Runtime: <N>s (created_at to updated_at)
- Runtime exceeded 300s: <yes/no>
- Steward cycles: <N>
- Outcome: VALIDATED (runtime > 300s AND steward_cycles <= 2, sweep fired and was correctly suppressed) | FAILED (runtime > 300s AND steward_cycles > 2, sweep re-prescribed during active execution) | INCONCLUSIVE (runtime <= 300s, sweep never fired during execution — task did not run long enough)

## Anomalies
<list anomalies or "none" — include anomalies from both basic checklist and deep exchange audit>

## Pipeline Signals
- Steward: <last log line with timestamp>
- Executor: <last log line with timestamp>
- WOS execution_enabled: <true/false from wos-config.json>

## State Update
- consecutive_clean_runs: <new value>
- total_runs: <new value>

## Actions Taken
<what was done — see Step 6>
```

---

## Step 6 — Fix (implementation-level gaps only)

If anomalies were detected and the root cause is an **implementation gap** (a code path that is broken, a wiring issue, a missing handler) — NOT a design question — dispatch a functional-engineer subagent to fix it.

**Only dispatch a fix if**:
1. The anomaly is reproducible (happened this cycle and the prior cycle's `last_anomalies` shows the same pattern)
2. The fix is narrowly scoped to a single code path
3. You can describe the exact file and function that needs to change

**Do NOT dispatch a fix for**:
- Design questions (these warrant a GitHub issue, not a code change)
- One-off timing flukes
- Issues that require human decision (mark `blocked` and note in report)

To dispatch a fix, write a message to the inbox with type `subagent_task` and a precise description of the issue and the fix needed. Reference the specific anomaly and UoW IDs.

---

## Step 7 — Update State

Update `/home/lobster/lobster-workspace/data/ralph-state.json`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

state_file = Path('/home/lobster/lobster-workspace/data/ralph-state.json')
try:
    state = json.loads(state_file.read_text())
except Exception:
    state = {"consecutive_clean_runs": 0, "total_runs": 0, "last_run_ts": None, "last_anomalies": []}

# is_clean_run = True or False (determined in Step 4)
is_clean_run = ...  # fill in from your evaluation

state["total_runs"] = state.get("total_runs", 0) + 1
state["last_run_ts"] = datetime.now(timezone.utc).isoformat()

# Populate last_anomalies from the anomalies observed in Step 3.
# Each entry should be a dict with at minimum: {"uow_id": ..., "anomaly_type": ..., "detail": ...}
# Example anomaly types: "stalled" (non-terminal after polling timeout), "steward_cycles_zero",
# "missing_artifact" (done but no workflow_artifact), "log_error",
# "steward_cycle_excess" (steward_cycles >= 3), "prsc_first_exec_conflation"
# (steward treated first_execution UoW as crashed), "duplicate_dispatch",
# "bad_posture_transition" (unexpected posture in audit trail),
# "pr_555_timing_fix_validation_failed" (type-D UoW steward_cycles > 2, sweep interrupted active execution)
# Use the stalled list and outcome data from Step 3 to build this.
# If is_clean_run is True, this should be [].
state["last_anomalies"] = anomalies_this_run  # list of anomaly dicts from Step 3

if is_clean_run:
    state["consecutive_clean_runs"] = state.get("consecutive_clean_runs", 0) + 1
else:
    state["consecutive_clean_runs"] = 0

state_file.write_text(json.dumps(state, indent=2))
print(f"State updated: consecutive_clean={state['consecutive_clean_runs']} total={state['total_runs']}")
```

---

## Step 8 — Robustness Check and Escalation

After updating state:

**If `consecutive_clean_runs >= 5`**: Ping Dan via Telegram:
> "WOS pipeline health goal reached: 5 consecutive clean test runs across type-A/B/C UoWs. WOS pipeline is self-verified robust. Review the recent reports in ~/lobster-workspace/data/ralph-reports/ for a full audit trail."
>
> chat_id: 8075091586, source: telegram

**If `consecutive_clean_runs == 0` and `total_runs >= 3`**: Something is persistently broken. Ping Dan via Telegram:
> "WOS pipeline health alert: 3+ consecutive failed test runs (total_runs=N). Last anomalies: <brief list>. Check ~/lobster-workspace/data/ralph-reports/ for details."
>
> chat_id: 8075091586, source: telegram

Otherwise: no notification needed. Log the cycle result in the report and exit cleanly.

---

## Boundary Constraints

- Do NOT modify `executor.py`, `steward.py`, or any existing orchestration code directly.
- Do NOT re-enable the executor/steward heartbeats if they are disabled — respect `wos-config.json`.
- Do NOT modify existing (non-ralph-test) UoW records.
- Do NOT merge PRs. Any PRs opened by a fix agent require oracle review.
- WOS test UoWs use `source = 'ralph-test'` — never touch records with other source values.

**Minimum viable output**: `ralph-state.json` updated, one report written to `ralph-reports/`.
