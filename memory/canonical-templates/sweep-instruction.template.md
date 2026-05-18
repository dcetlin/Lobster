# Sweep Instruction Template

This template encodes three mandatory harness patterns that every sweep or diagnostic agent must
implement. The patterns exist to prevent two named failure classes:

- **Legibility failure** (`legibility_failure/silent-contract-violation` in smell-patterns.yaml):
  querying a resource that may not exist and treating empty results as valid signal.
- **Coherence failure** (`coherence_failure/missing-state-registry-read` in smell-patterns.yaml):
  classifying system state without reading the operational state registry first, making deliberate
  operational states invisible to the classifier.

Copy this template and fill in the bracketed sections. All three harness sections are mandatory.
Remove or skip none of them — if a section genuinely does not apply, state that explicitly and
explain why.

---

## Purpose

[One sentence: what system property does this sweep verify, and what failure mode does it detect?]

---

## Dependencies (contract declarations)

**Harness pattern 1 of 3 — prevents: legibility failure / silent contract violation**

List every external resource this sweep queries before writing any query logic.

| Resource | Type | Verification step |
|----------|------|-------------------|
| [label name] | GitHub label | `gh label list --repo <owner/repo> \| grep "<label>"` — abort if missing |
| [file path] | File | existence check before read — abort if missing |
| [API endpoint] | HTTP | probe call — abort if non-200 |

**Enforcement rule:** Before any query that depends on a resource in this table, verify the resource
exists using the verification step shown. If the resource is missing, the sweep must fail loudly
with an explicit error — it must NOT proceed with an empty result set that looks like valid signal.

Example enforcement (shell):
```bash
gh label list --repo dcetlin/Lobster | grep -q "wos:executing" || { echo "ERROR: label wos:executing missing — aborting sweep"; exit 1; }
```

Example enforcement (Python):
```python
labels = get_github_labels(repo)
if "wos:executing" not in labels:
    raise RuntimeError("Contract violation: label 'wos:executing' does not exist in repo — sweep cannot proceed")
```

---

## Operational Posture (state registry read)

**Harness pattern 2 of 3 — prevents: coherence failure / missing state registry read**

Read `~/lobster-workspace/data/wos-config.json` before any throughput, activity, or health
classification. Record the posture explicitly. All subsequent interpretations are conditioned on it.

```
execution_enabled: [true | false | file absent]
explicit_pause_flags: [list any additional pause/halt flags found]
posture_summary: [RUNNING | PAUSED | UNKNOWN]
```

**Enforcement rule:**
- If `execution_enabled` is `false` (or the file is absent): suppress all throughput/activity
  classifications. Record at the top of the detection pass: "System deliberately paused
  (execution_enabled=false) — throughput and activity checks skipped. This is not a stall."
  Do not escalate starvation or stall conditions while the system is paused.
- If `execution_enabled` is `true`: proceed with throughput checks as defined below.
- If the file is absent: treat as UNKNOWN posture, record it explicitly, and flag for investigation
  before any stall-related escalation.

---

## Classification States (completeness invariant)

**Harness pattern 3 of 3 — prevents: coherence failure / missing state registry read**

For each binary conclusion this sweep can reach, enumerate ALL states that can produce the
"no-problem" reading. Conclude via elimination — not via positive assertion.

The failure mode: a classifier that asserts "healthy because activity is present" or "stalled
because activity is absent" without enumerating what else can produce an absence-of-activity
reading will misclassify any legitimate no-activity state.

**Format for each classifier:**

```
Classifier: [stalled / healthy]
Conclusion: [what the sweep concludes when it finds no activity]

States that can produce the no-activity reading:
  [ ] System deliberately paused (checked: wos-config.json execution_enabled=false)
  [ ] [Any other legitimate no-activity state for this system]
  [ ] [...]

Elimination rule: only classify as [stalled] if ALL of the above states have been checked
and ruled out. If any state could not be checked (resource missing, permission error), do not
conclude — escalate instead.
```

**Example — WOS throughput classifier:**

```
Classifier: stalled / healthy
Conclusion: "stalled" when no UoW has been updated in >3 days

States that can produce the no-update reading:
  [ ] System deliberately paused (checked: wos-config.json execution_enabled=false)
  [ ] No UoWs currently open (checked: gh issue list --label wos:executing returns zero AND label exists)
  [ ] Executor heartbeat recently restarted (checked: executor-heartbeat.log last-entry timestamp)

Elimination rule: only classify as stalled if all three states above are checked and ruled out.
```

---

## Detection Pass

[Domain-specific detection instructions go here. These execute AFTER the three harness sections
above have been completed and their findings recorded.]

[Structure by domain as appropriate. Reference the posture and contract findings from the harness
sections when interpreting any metric.]

---

## Prescription Pass

[For each finding from the detection pass: prescribe an action, escalation, or autonomous fix.
Do not prescribe for conditions that are explained by the operational posture recorded above.]

---

## Output Format

Write findings to: [output file path, e.g., `~/lobster-workspace/hygiene/YYYY-MM-DD-sweep.md`]

Required sections in output:
- Harness summary (posture read, contract checks run, classification states verified)
- Detection pass findings (dissonance / golden patterns)
- Prescription pass (actions / escalations)
- Refactor pass (autonomous actions taken, with rationale)

---

## Notes on Template Use

- This template is versioned in `~/lobster/memory/canonical-templates/sweep-instruction.template.md`
- The three harness sections are mandatory. If a section genuinely does not apply (e.g., a sweep
  that reads only local files with no GitHub dependencies), state "N/A — [reason]" explicitly.
- The harness patterns correspond to named smells in `~/lobster/oracle/smell-patterns.yaml`:
  - Dependencies → `legibility_failure/silent-contract-violation`
  - Operational Posture → `coherence_failure/missing-state-registry-read`
  - Classification States → `coherence_failure/missing-state-registry-read`
- When a new sweep instruction is written, link to this template in its header (see negentropic-sweep.md for example).
