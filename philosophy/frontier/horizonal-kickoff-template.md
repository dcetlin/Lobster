# Horizonal Kickoff Template

A zero-bloat template for multi-scope, self-sustaining horizonal work sessions.
Use this to initialize any directed overnight or extended autonomous work period.

---

```markdown
# Nighttime Directive — [DATE]

**Issued:** [TIME]Z  
**Horizon:** [end time or "dawn"]  
**Recovery path:** Read README.md → log.md → re-enter at last checkpoint

## North Star
[One sentence stating what "done" looks like for the session.]

## Threads (max 3)
1. [Thread name] — Criterion: [one observable, binary closure signal]
2. [Thread name] — Criterion: [one observable, binary closure signal]
3. [Thread name] — Criterion: [one observable, binary closure signal]

## Governing Constraints
- Right model for the job (haiku for sweep, sonnet for design, opus for oracle)
- Every task leaves a durable artifact (JSONL entry at minimum)
- No spawn near compaction boundary — check inflight count before spawning batch
- Steady-state detected after 2h of prescriptions_this_cycle=0 → drop cron frequency

## Cron Rhythm
- `[name]-progress-log` — hourly; appends one line per thread to log.md; drops to 2h if steady-state
- `[name]-intent-reminder` — every 2h; surfaces accomplishments + next steps; invites context update

## Closure Protocol (run before dawn)
1. Each thread: did criteria close? If not, file a seed issue.
2. Each open PR: oracle dispatched? If not, dispatch now.
3. Each proposed-but-not-grounded item: file as issue or discard. No homeless seeds.
4. Update session note Open Tasks with routing hints (machine-actionable vs. needs-Dan).
5. Update vision/current_focus if stale > 7 days.
```

---

## Why these elements

**North Star** enforces a single closure signal for the whole session — work stops when criteria are met, not when tokens run out.

**Threads with binary criteria** give subagents a legible done state. Without a closure signal, threads run to token exhaustion.

**Governing Constraints** encode the four most common overnight failure modes: wrong model cost, artifact-less tasks, compaction-boundary spawning, and cron waste-heat in steady state.

**Cron Rhythm** creates a self-modifying circadian pattern (phenotypic plasticity) rather than a static reminder that fires indiscriminately.

**Closure Protocol** is the glymphatic flush: run before dawn to eliminate homeless seeds, stale docs, and oracle gaps that would otherwise carry forward as uncadenced juice.

---

*Extracted from ooda-retro-2026-04-16. For the metabolic taxonomy (shit/seed/pearl/heat) referenced in closure protocol, see `system-metabolism.md`.*
