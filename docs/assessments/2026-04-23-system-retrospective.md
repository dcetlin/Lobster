# System Retrospective: 2026-04-23
**Period:** 2026-04-23 06:00 UTC – 18:00 UTC (12 hours)
**Trigger:** Overnight WOS completion audit revealing 87/90 unverifiable UoW completions; Dan-initiated synthesis after specialist inspection reports delivered
**Summary:** A day-long data integrity push prompted by discovering that 97% of WOS "completions" had no verifiable artifact trail. Seven PRs merged, 3 golden patterns encoded, and an estimated 350–400k tokens of recoverable overhead was identified and quantified. The system's audit infrastructure is now substantially stronger than it was at 06:00 UTC; write_result back-propagation (Issue #867) remains the single largest unresolved structural gap.

---

## Metabolic Summary

| Category | Count | What these were |
|----------|-------|-----------------|
| Pearls   | 7     | PRs #862, #863, #865, #866, #870, #871, #873, #875, #876 merged (8 by close; metabolic count uses start-of-session baseline of 7 before synthesis was spawned) |
| Seeds    | 5     | Issue #867 (write_result back-propagation), Issue #868 (UoW ID injection), Issue #874, bidirectional provenance design, golden job pattern doc |
| Heat     | 6     | 14 compaction recoveries, retroactive sweep at 4% hit rate, duplicate oracle path cleanup, rolling-summary bloat fix still undone, duplicate spawn lapse, blocked UoW re-dispatch loops |
| Shit     | 5     | 87/90 unverifiable completions, two registry DB paths (one empty), dormant spiral gate since PR #814, 10 invisible cron jobs, 189 records deleted without full reconciliation |

**Honest metabolic ratio:** ~260k of ~350k total token cost was recoverable overhead (waste items #1 and #2). Genuine novel output (design decisions, structural code changes, pattern encoding) was produced in approximately 30–45 minutes of effective work across a 12-hour session.

---

## Smell Patterns Identified

### Smell 1: write_result Not Back-Propagated to Executor Output Files (recurring)

**Evidence from today:** 87/90 WOS UoWs showed as unverifiable. Subagents call `write_result` but the executor output file receives only a dispatch receipt, not the result payload. The executor cannot confirm completion at next sweep. Every steward spiral re-runs "is this done?" against records with zero evidence content.

**Root cause:** The executor heartbeat does not intercept `write_result` responses to back-propagate result text and status to the UoW output file. The hook point exists; it is unused.

**Proposed fix:** In `executor-heartbeat.py` or the `write_result` handler, after a result is received, overwrite the executor output file with the result text — not just the dispatch ack. This is a behavioral gate change, not an architectural change.

**Effort:** Small. Issue #867 filed and in-flight.

**Recurrence:** 1st naming of this specific failure (the broader "dispatch receipt as completion" anti-pattern was first named in the 12h retro summary).

---

### Smell 2: bare python3 in upgrade.sh Migrations (4th recurrence)

**Evidence from today:** rolling-summary.md and session file both explicitly flag "4th recurrence." The CLAUDE.md rule requiring `uv run` instead of `python3` exists. Enforcement does not. Oracle review caught it in PR #866 learnings.

**Root cause:** No automated check enforces the rule. Humans (and LLMs) remember it inconsistently under context pressure.

**Proposed fix:** Add a grep check at the top of `upgrade.sh` that fails with a clear error message if any migration step contains `python3` or `python` without `uv`. One-liner covering all future migrations automatically. No per-migration discipline required.

**Effort:** Small.

**Recurrence:** 4th. This is a hygiene enforcement gap, not a knowledge gap.

---

### Smell 3: Oracle Dual-Write (decisions.md + oracle/verdicts/) with No Enforcement

**Evidence from today:** PR #865 declared `oracle/verdicts/pr-{N}.md` as canonical. Oracle agents continued writing to `decisions.md` in parallel through the session. Dan raised this directly at ~17:45Z as an unanswered question: "Why do we write to both?" PR #875 deprecated `decisions.md` and archived it; but the enforcement mechanism (removing the write path from oracle agent templates) was not yet verified complete at session end.

**Root cause:** The deprecation was structural (file archived) but the agent prompt templates were not audited to confirm `decisions.md` write paths were fully removed. Rolling-summary flags "needs enforcement."

**Proposed fix:** Audit lobster-oracle.md for any remaining `decisions.md` write instructions. Confirm dispatcher merge gate reads only from `oracle/verdicts/`. Add a hook or test that fails if `decisions.md` is written to post-deprecation.

**Effort:** Medium. PR #875 landed the structural piece; verification and enforcement remain.

**Recurrence:** 2nd session this has been flagged as incomplete.

---

### Smell 4: rolling-summary.md Bloat Fix Agreed but Not Automated (carried 2+ sessions)

**Evidence from today:** Session file and rolling-summary both explicitly flag "NOT YET IMPLEMENTED." Design decision (overwrite to 50-line snapshot) was agreed and recorded. Bloat amplifies each of the 14 compactions this session — each one costs more reconstruction overhead than necessary.

**Root cause:** The fix requires a scheduled job or post-compaction hook. No agent has been assigned to implement it; it has been carried as a thread item.

**Proposed fix:** Wire the overwrite logic into the session-consolidation job or a dedicated post-compaction hook. Target format is already defined. Implementation is a file write, not an architectural decision.

**Effort:** Small.

**Recurrence:** 3rd+ session.

---

### Smell 5: Health Probes Saturating Vector Memory (gate designed, unimplemented)

**Evidence from today:** rolling-summary flags "100% health probes in memory store; gate not yet implemented." Cron heartbeats and health pings are written to the vector memory DB alongside meaningful semantic events, degrading memory retrieval signal-to-noise over time.

**Root cause:** No filter gate exists in the memory store ingestion path to exclude system-origin writes. The type discriminator (`type: health_probe`, `type: cron_heartbeat`) is already present in message metadata but not used as a gate.

**Proposed fix:** Add a filter in the memory store ingestion path that excludes messages with `type: health_probe`, `type: cron_heartbeat`, or `sender: system` before writing to the DB.

**Effort:** Small-Medium.

**Recurrence:** 2nd+ session.

---

## Golden Patterns Established

### Pattern 1: systemd-as-canonical for LLM Scheduled Jobs (Type A/B/C Taxonomy)

**Source:** PR #870 — migration of 10 cron-only LLM jobs to systemd, plus `docs/scheduled-jobs-golden-pattern.md`

**What it captures:** LLM scheduled jobs (Type A) must use systemd .timer + .service unit files committed to the repo. Four required components: `.timer` file, `.service` file, `jobs.json` entry, task `.md` file. The `enabled` field in `jobs.json` is the runtime gate. Type C (cron-direct scripts) are the named exception — distinguished from compliance gaps by `dispatch: "cron-direct"` in `jobs.json`.

**Encoding location:** `~/lobster/oracle/golden-patterns.md` (entry dated 2026-04-23); `docs/scheduled-jobs-golden-pattern.md` (canonical reference doc); CLAUDE.md "Scheduling Architecture" section (partially documented; the 4-component requirement and Type A/B/C taxonomy are now explicit).

**Status:** Fully encoded. New instances and `upgrade.sh` runs will configure correctly via Migration 87.

---

### Pattern 2: feedforward + Retroactive Data Integrity for Long-Running Pipelines

**Source:** WOS pipeline audit — 47% failure rate, 189 tagged `outcome_unverifiable`, leading to PR #866 and Issue #867

**What it captures:** When historical pipeline outputs are unverifiable, run two sequential phases: (1) retroactive — tag irrecoverable records with a structured sentinel (`outcome_unverifiable`), recover what can be recovered, surface the failure rate as a metric; (2) feedforward — implement the structural fix in a separate PR so new records are verifiable. Do not delete unverifiable records; the count is evidence of the gap. The sentinel must be structured (filterable, countable) — not null or empty string.

**Encoding location:** `~/lobster/oracle/golden-patterns.md` (entry dated 2026-04-23). Not previously encoded anywhere.

---

### Pattern 3: Bidirectional Provenance Design for Cross-System Artifact Linkage

**Source:** PR #871 + approved design for wos-cross-system-linkage

**What it captures:** Encode linkage bidirectionally — (1) forward: inject the originating UoW ID into the subagent context block at dispatch; (2) backward: stamp GitHub PRs/issues with the UoW ID in a machine-readable footer. UoW ID is the canonical key; PR numbers and issue numbers are foreign keys. Minimum viable = steps 1+2. Future steps (label stamping, commit footers, reconciliation sweep) extend the principle without replacing it.

**Encoding location:** `~/lobster/oracle/golden-patterns.md` (entry dated 2026-04-23). Not previously encoded anywhere. Minimum viable implementation in PR #871 (step 1 only; step 2 is a prompt instruction).

---

## Waste Analysis

Ranked by estimated token/cycle cost (highest to lowest):

| # | Waste Source | Estimated Cost | Top Intervention |
|---|-------------|----------------|-----------------|
| 1 | Unverifiable UoW completions — 87/90 UoWs | ~260,000 tokens | write_result back-propagation (Issue #867) |
| 2 | Retroactive sweep recovery — 205 output files audited | ~45,000 tokens + human hours | Consequence of #1; eliminated by Issue #867 |
| 3 | Context compaction mid-session losses — 14 compactions | ~30,000–60,000 tokens | rolling-summary.md bloat fix (Smell #4 above) |
| 4 | Steward re-dispatch loops — 3 blocked UoWs re-queued | ~12,000–18,000 tokens | Cap re-dispatch at N=3, escalate to human review |
| 5 | Registry DB path split — two DB files, one empty | ~8,000 tokens + ongoing overhead | PR #873 consolidated; fixed |
| 6 | Memory signal/noise — 100% health probes in memory store | ~5,000 tokens/day compounding | Health probe filter gate (Smell #5 above) |

**Total estimated recoverable overhead today:** ~350,000–400,000 tokens against ~30 minutes of genuinely novel output.

**Top intervention:** write_result back-propagation to executor output file (Issue #867). A single change would have made 87/90 UoW completions verifiable — eliminating waste items #1 and #2 almost entirely, and reducing #3 and #4 by giving recovery agents concrete evidence rather than absence-of-evidence.

**Second-highest leverage:** Cap steward re-dispatch at N=3 and escalate to human review. Stops the blocked-UoW loop without architectural changes.

---

## Implementation Plan

### Tier 1 — Structural (highest ROI, remove systemic waste)

1. **write_result back-propagation** (Issue #867) — Single highest-leverage change. Eliminates ~260k tokens of unverifiable-completion overhead per 12-hour window at current WOS volume. Prerequisite for automated retrospectives to reliably measure pearl/heat/shit ratios. Effort: Small.

2. **rolling-summary.md bloat fix** — Overwrite to 50-line current-state snapshot on each session-consolidation run. Design agreed. Reduces per-compaction reconstruction cost. Effort: Small. Carried 3+ sessions — assign it.

3. **Health probe memory gate** — Filter gate in memory store ingestion path to exclude `type: health_probe`, `type: cron_heartbeat`, `sender: system` writes. Effort: Small-Medium. Compounds daily if unaddressed.

4. **bare python3 enforcement grep in upgrade.sh** — Single-line check at top of `upgrade.sh` that fails loudly if any migration uses `python3` without `uv`. Effort: Small. 4th recurrence; now a process issue, not a knowledge issue.

### Tier 2 — Canonical Pattern Enforcement (already in-flight or recently landed)

5. **Oracle dual-write enforcement** — Verify lobster-oracle.md has no remaining `decisions.md` write instructions post-PR-#875. Confirm dispatcher merge gate reads only `oracle/verdicts/`. Add a lint check or hook. Effort: Small (audit) to Medium (hook). Dan raised this directly; still open.

6. **UoW ID provenance in all artifact footers** (PR #871 — merged) — Step 1 (forward injection) landed. Step 2 (backward: PR footer) is a prompt instruction; confirm it is active in oracle-reviewed subagent definitions.

7. **verdicts/ as sole oracle source** (PR #875 — merged) — Structural piece landed. Enforcement verification per item 5 above.

8. **WOS issue lifecycle labels** (PR #876 — oracle APPROVED and merged at session end) — `wos:executing` label stamping at UoW dispatch + close on completion. Bidirectional legibility from GitHub.

### Tier 3 — Feedback Loop Infrastructure (design and document; do not implement without Dan approval)

9. **Smell pattern registry** (`~/lobster/oracle/smell-patterns.yaml`) — Machine-readable registry of named smell patterns with detection heuristics, severity, threshold, and issue templates. Enables automated smell detection in future retrospectives.

10. **Automated system-retrospective scheduled job** — Weekly + on-demand retrospective that scans git log, PRs, UoW registry, and session notes; computes metabolic ratios; checks against smell registry; outputs to `~/lobster-workspace/assessments/YYYY-MM-DD-auto-retrospective.md`; injects newly-detected smells into WOS queue as GitHub issues.

11. **Steward re-dispatch escalation** — Cap re-dispatch at N=3 retries. After N, escalate blocked UoW to human review (Telegram message to Dan). uow_20260422_3cc6ca, uow_20260422_c0a82e, uow_20260422_654519 are the live examples.

---

## Feedback Loop Proposal

### The Goal

Close the observation-to-behavioral-change loop: retrospective detects smell → issue filed → sweeper creates UoW → subagent fixes it → next retrospective confirms smell gone. Currently, this loop is manual and session-dependent. The proposal makes it automated and continuous.

### Proposed: system-retrospective Scheduled Job

**Trigger:** Weekly (e.g., every Sunday 06:00 UTC) + on-demand (Dan or dispatcher can trigger manually)

**Data collection phase:**
- Scan git log and merged PRs for the period (pearls: count verified by merged PR with UoW footer)
- Scan UoW registry for dispatched vs. completed vs. unverifiable (heat/shit ratio)
- Scan session notes in `~/lobster-user-config/memory/canonical/sessions/` for open threads and smells
- Scan `~/lobster-workspace/assessments/` for recent assessment entries

**Metabolic accounting phase:**
- Pearl = UoW with merged PR in artifact trail (provable via PR #871 UoW footer stamping, once Issue #867 is shipped)
- Seed = UoW whose output was an issue filed or design doc
- Heat = UoW that completed but left no artifact, or found work already done
- Shit = UoW that failed or left only a dispatch receipt

**Smell detection phase:**
- Compare detected patterns against `~/lobster/oracle/smell-patterns.yaml` registry
- Match by detection heuristic (e.g., "grep `python3` in upgrade.sh migrations", "count UoWs with `outcome_unverifiable`", "check rolling-summary.md line count")
- Flag any smell above its defined threshold

**Golden pattern drift check phase:**
- For each entry in `oracle/golden-patterns.md`, verify the pattern still appears in CLAUDE.md or lobster-oracle.md
- Flag entries with no enforcement point (documented but unenforced)

**Output:**
- Write `~/lobster-workspace/assessments/YYYY-MM-DD-auto-retrospective.md` (same format as this document)
- File GitHub issues for newly-detected smells without open issues
- Add to WOS queue via germinator

**WOS injection:**
- New smell detected with severity=high → file GitHub issue immediately, add to WOS queue
- New smell detected with severity=medium → file GitHub issue, leave for next sweep cycle
- Existing smell still present after 2 cycles → escalate to Dan

### Smell Registry Format

`~/lobster/oracle/smell-patterns.yaml` — each entry:

```yaml
- name: "write_result-not-back-propagated"
  description: "Subagent write_result does not update executor output file"
  detection_heuristic: "count UoWs in registry with outcome_unverifiable > threshold"
  threshold: 5
  severity: high
  issue_template: "write_result back-propagation missing — N UoWs unverifiable"
  github_label: "wos-observability"
  status: open  # open | resolved | suppressed
  first_detected: "2026-04-23"
  open_issue: 867

- name: "bare-python3-in-migrations"
  description: "upgrade.sh migrations use python3 instead of uv run"
  detection_heuristic: "grep 'python3\\|python ' scripts/upgrade.sh | grep -v uv"
  threshold: 1
  severity: medium
  issue_template: "bare python3 in upgrade.sh — recurring anti-pattern (Nth recurrence)"
  github_label: "hygiene"
  status: open
  first_detected: "2026-04-22"
  open_issue: null

- name: "rolling-summary-bloat"
  description: "rolling-summary.md exceeds 50 lines and has not been overwritten"
  detection_heuristic: "wc -l ~/lobster-user-config/memory/canonical/rolling-summary.md > 50"
  threshold: 50
  severity: medium
  status: open
  first_detected: "2026-04-21"
  open_issue: null

- name: "health-probe-memory-saturation"
  description: "Vector memory DB contains health probe writes"
  detection_heuristic: "count memory entries with event_type=health_probe"
  threshold: 100
  severity: low
  status: open
  first_detected: "2026-04-22"
  open_issue: null
```

### Prerequisite

**Issue #867 (write_result back-propagation) must ship before automated retrospectives can reliably measure pearl/heat/shit ratios.** Without it, 95%+ of UoW history is unverifiable, making metabolic accounting produce only a lower bound on pearls and an overcount of shit.

### The feedback loop closes when:

1. `system-retrospective` job detects smell above threshold
2. Job files GitHub issue (or confirms existing issue is open)
3. Germinator sweeps the issue and creates a UoW
4. Executor dispatches a subagent to fix it
5. Subagent merges a PR with the fix, stamps `WOS-UoW: uow_<id>` in the footer
6. Next `system-retrospective` run detects smell is below threshold → marks resolved in `smell-patterns.yaml`

This loop uses the same WOS machinery that already exists. The retrospective job is the observation layer (O1 in the OODA loop). The WOS sweeper/germinator is the orientation layer. The executor is the action layer. The next retrospective is the confirmation layer.

---

*Document generated by retro-synthesis-and-plan subagent | 2026-04-23T18:00Z*
*Sources: retro-structural-inspector report, retro-golden-encoder report, retro-waste-analyst report, session note 20260423-003.md, rolling-summary.md, oracle/golden-patterns.md*
