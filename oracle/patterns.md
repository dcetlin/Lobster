# WOS Loop Pattern Register

Canonical taxonomy of convergence/divergence loop signatures observed in the WOS pipeline.
Read-only for oracle, steward, and sweep. Updated only via oracle decisions or human edit.

---

## spiral
**Signal:** oracle_pass_count ≥ 3 for a single PR/UoW
**Steward response:** pause additional UoW dispatch on this workstream; write escalation prescription
**Oracle response:** flag pattern in review; note pass count; check if scope needs reduction
**Sweep response:** count spirals/week; if >2, prescribe "oracle gate needs earlier spec clarity"

---

## cascade
**Signal:** a merged PR generates ≥2 new UoW prescriptions within 24h
**Steward response:** sequence cascaded UoWs by dependency; don't dispatch all at once
**Oracle response:** note cascade potential when reviewing PRs that touch shared infrastructure
**Sweep response:** count cascades/week; if cascade chain depth >3, prescribe "spec the full chain before starting"

---

## burst
**Signal:** negentropic sweep generates ≥5 UoWs in a single run
**Steward response:** batch into groups of 3; sequence by priority; don't saturate executor
**Oracle response:** N/A (burst is upstream of oracle)
**Sweep response:** if burst UoWs have high failure rate, prescribe "scope UoWs more narrowly before dispatch"

---

## dead-end
**Signal:** a UoW reaches failed or blocked state ≥2 times without a fix UoW completing
**Steward response:** suppress re-dispatch; write blocker prescription with root-cause investigation task
**Oracle response:** flag when a PR is a repeated attempt at previously-failed work
**Sweep response:** count dead-ends/week; if >3, prescribe "executor needs better blocker detection"

---

## steady-state
**Signal:** executor dispatches 1-3 UoWs per heartbeat, all complete in <2 oracle passes
**Steward response:** maintain cadence; no intervention needed
**Oracle response:** standard review; no escalation
**Sweep response:** steady-state is healthy; note if throughput drops below 1 UoW/day for >3 days

---

## Notes on evolution
- New patterns emerge from oracle/learnings.md observations
- Threshold values (≥3 passes, ≥5 UoWs, etc.) are initial estimates; adjust via oracle decisions
- Pattern names should match the vocabulary used in oracle/learnings.md and sweep-context.md
