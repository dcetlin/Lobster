# Operativity Quarterly Review

**Job**: operativity-quarterly-review
**Schedule**: First day of each quarter at 9:00 AM (0 9 1 1,4,7,10 *)

---

## Context

You are a lobster-generalist subagent executing the quarterly approximate embodiment operativity test. This job measures whether Lobster's approximate embodiment (its degree of attractor-convergence across four operational registers) is holding, degrading, or improving.

The spec is authoritative. Read it before doing anything else.

---

## Setup

```yaml
task_id: operativity-quarterly-review-YYYY-QN
chat_id: 6036
source: scheduled
```

Replace `YYYY-QN` with the current year and quarter (e.g., `2026-Q3`).

---

## Instructions

### Step 1: Read the spec

Read `~/lobster/docs/wos/design/approximate-embodiment-operativity-spec.md` in full before beginning any measurements. The spec is the authority on probe design, pass thresholds, and failure signals. Do not improvise probes.

### Step 2: Determine output filename

Compute the current quarter:
- January–March: Q1
- April–June: Q2
- July–September: Q3
- October–December: Q4

Output file: `~/lobster-workspace/assessments/operativity-YYYY-QN.md`

### Step 3: Create the output file

Copy the Baseline Measurement Template from the spec into your output file. Replace the header date and executor fields. Do this before running any cells.

### Step 4: Run all 12 cells in order

Execute each cell's **Probe** exactly as specified. For each cell:
1. Execute the probe
2. Record the raw **Observation** (the actual number, fraction, or finding)
3. Apply the **Pass threshold** — record PASS or FAIL
4. If FAIL, describe the observed **Failure signal** in the Notes field

**Cell order:**
1. Philosophical/Semantic × Landscape Density
2. Philosophical/Semantic × Convergence Reliability *(note: this probe requires 3 sessions across 3 days — if running as a one-shot job, sample from the 3 most recent philosophy-explore sessions instead and note the substitution)*
3. Philosophical/Semantic × Trajectory Continuity
4. Loop/Pipeline × Landscape Density
5. Loop/Pipeline × Convergence Reliability
6. Loop/Pipeline × Trajectory Continuity
7. Execution × Landscape Density
8. Execution × Convergence Reliability
9. Execution × Trajectory Continuity
10. Proprioceptive × Landscape Density
11. Proprioceptive × Convergence Reliability
12. Proprioceptive × Trajectory Continuity

### Step 5: Compute scores

- Landscape Density score: PASS count among cells 1, 4, 7, 10
- Convergence Reliability score: PASS count among cells 2, 5, 8, 11
- Trajectory Continuity score: PASS count among cells 3, 6, 9, 12
- Overall: total PASS count out of 12

### Step 6: Check escalation threshold

If any dimension score is below 2/4, escalate immediately:

```
send_reply(chat_id=6036, text="OPERATIVITY ALERT: [dimension] below threshold. [N]/4 cells passing. Failing cells: [list with observed failure signals]")
```

Do not wait for the summary — escalate immediately when detected.

### Step 7: Compare to prior quarter

Read the most recent prior operativity file in `~/lobster-workspace/assessments/` (if any). Note any dimensions that changed by ≥1 cell (improved or degraded) in the "Comparison to Prior Quarter" section.

### Step 8: Record calibration notes

For any cell marked `[CALIBRATE AFTER FIRST RUN]` in the spec, record the baseline value you observed so future runs can score against it.

### Step 9: Send summary to Dan

Send a reply to chat_id 6036:

```
Operativity Q[N] complete: [X]/12 cells passing

Landscape Density: [N]/4 | Convergence Reliability: [N]/4 | Trajectory Continuity: [N]/4

[List any FAIL cells with one-sentence description of observed failure signal]
[Note any calibration values recorded for first-run cells]
[Note any dimension that changed from prior quarter]
```

### Step 10: Call write_result

```python
write_result(
    task_id="operativity-quarterly-review-YYYY-QN",
    sent_reply_to_user=True,
    status="success",
    text="Operativity Q[N]: [X]/12 cells passing. Results at ~/lobster-workspace/assessments/operativity-YYYY-QN.md"
)
```

---

## Constraints

- Do not run WOS UoWs or make code changes as part of this job — measurement only
- Do not update the spec based on what you observe — flag calibration values in the output file and Open Questions if a probe cannot be executed as written
- If a probe cannot be executed as written (e.g., required log file absent), record `[PROBE NOT EXECUTABLE: reason]` in the cell result rather than improvising an alternative
