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

## infrastructure-vs-execution discriminator
**Signal:** a retry gate increments its counter on orphan recovery events (session TTL, executor kill, no result.json written) the same way it increments on genuine execution failures (agent ran, produced output, output indicated failure)
**Steward response:** do not apply MAX_RETRIES pressure against infrastructure events; maintain a separate `execution_attempts` counter incremented only when `return_reason` is NOT an orphan classification
**Oracle response:** when reviewing any PR that adds or modifies a retry gate, verify that the gate counter excludes orphan recovery events. A counter that increments on both is architecturally wrong regardless of increment size — infrastructure turbulence silently depletes execution budget and causes the system to surface to human review for problems that resolved themselves
**Sweep response:** if `needs-human-review` backlog is dominated by UoWs with `execution_attempts == 0` and all-orphan `return_reasons`, the retry gate is conflating infrastructure events with execution failures — apply the discriminator fix before processing the backlog

---

## form-function isomorphism (meta-pattern)

**Signal:** a component's code structure — naming, organization, module boundaries, hierarchy — does not mirror the conceptual structure of what the component does
**Oracle response:** when reviewing any architectural component (pipeline stage, handler, schema, configuration block), ask: does the form of this code mirror the conceptual structure of what it does? Apply the zero-excess test: every element should be load-bearing; nothing should be present that doesn't carry meaning, and nothing should be absent that the component needs. Two failure modes to check: (1) over-structuring — the structure is more elaborate than the concept requires, adding translation cost at every reading; (2) under-structuring — the structure is simpler than the concept requires, hiding information that is genuinely load-bearing. If a component has a name, an organization, or a boundary that requires explanation before it makes sense, it has not achieved form-function isomorphism.
**Steward response:** when prescribing new UoWs for architectural components, include a form-function check: does the proposed implementation structure match the conceptual model in the spec?
**Sweep response:** accumulated form-function drift is visible as documentation debt — when the prose explaining a component must carry information the code itself does not express, the structure is under-explaining its own logic

**Reference:** `~/lobster/philosophy/form-function-isomorphism-20260426.md`

---

## Notes on evolution
- New patterns emerge from oracle/learnings.md observations
- Threshold values (≥3 passes, ≥5 UoWs, etc.) are initial estimates; adjust via oracle decisions
- Pattern names should match the vocabulary used in oracle/learnings.md and sweep-context.md
