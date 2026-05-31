# Management Surface Metric Design

*Status: Design proposal — 2026-05-22*
*WOS-UoW: uow_20260522_ec9d18*

---

## Overview

This document defines three things: (1) an operational definition of *management surface* as a measurable quantity, (2) a concrete posture for a meta/drift-detection agent that can assess whether development trends toward transparency or object-reliability, and (3) a direct answer to the question of whether that posture can operate upstream of proposal generation.

The underlying stakes: the violin analogy identifies two surface-identical states — the system running well because Dan manages it effectively (object-reliability) and the system running well because Dan's cognitive reach extends through it (transparency). Both produce the same operational signal. Distinguishing them requires a frame outside the reliability frame. That is what this document designs for.

---

## 1. Management Surface: Operational Definition

Management surface is the **total cognitive load Dan must bear to keep the system functioning** — measured not as task count but as the countable items of configuration, supervision, handoff documentation, and decision-demand that exist at any point in time.

This is not an abstraction. It is a load that is observable, countable, and changes at merge time.

### 1.1 Surface Categories

**Category A — Configuration artifacts Dan must author or review**

Items in this category include:
- CLAUDE.md entries added (behavioral instructions Dan must verify don't conflict)
- jobs.json entries added (scheduled job definitions requiring Dan to review cadence, permissions, failure behavior)
- IFTTT rule additions (conditions Dan must validate as correctly stated)
- vision.yaml structural field changes requiring Dan's confirmation (`core.*`, `active_project.phase_intent`)
- New environment variable requirements added to config.env
- New permissions entries in `settings.json` that expand system capability

A **reduction** in this category: an agent learns to infer a behavior from existing vision.yaml fields rather than requiring a new CLAUDE.md instruction. Or a default value is established that doesn't require per-instance configuration. The surface item that existed (the configuration artifact) is absorbed by existing structure — without removing the underlying capability.

What a reduction is not: removing a capability. Eliminating both the surface item and the function it configured is not absorption — it is subtraction.

**Category B — Supervision loops Dan must run**

Items in this category include:
- Scheduled job outputs Dan must check before trusting (new jobs in their first weeks)
- Agent behaviors Dan actively monitors after a change (the observation window before trust is established)
- PR batches Dan must review beyond the oracle-standard pass
- Dashboard states Dan must monitor that are not yet automated or summarized

A **reduction** in this category: a supervision loop that was running because Dan hadn't yet established trust reaches a natural termination after N successful cycles, without requiring a Dan-decision to close. The loop was absorbed by accumulated evidence. The key signal: the loop had a stated retirement condition that was met.

What a reduction is not: Dan stops checking because he forgot, or because he is comfortable with the risk.

**Category C — Handoff documentation Dan must maintain**

Items in this category include:
- Session notes and handoff documents that must be kept current for recovery
- Agent bootup files requiring periodic Dan-authored updates to remain accurate
- Operational runbooks describing how to recover from specific failure modes
- Vision Object fields requiring Dan-authored updates on a non-trivial cadence

A **reduction** in this category: a recovery sequence that previously required a documented runbook is absorbed by structural prevention — the failure mode is removed rather than better-documented. Or a prior handoff document section becomes system-generated rather than Dan-authored.

What a reduction is not: the document is eliminated because the information it tracked is no longer relevant.

**Category D — Decision-demand: moments where the system halts for Dan**

Items in this category include:
- Philosophical or human-judgment UoWs that surface to Dan for completion declaration
- Open decisions in vision.yaml that have not been resolved
- Oracle verdicts requiring Dan's review after three failed rounds
- Blocked tasks requiring Dan's answer before execution can proceed

A **reduction** in this category: a class of decisions that previously required Dan's input is absorbed by a structured decision framework — the system can route correctly without escalation, because the relevant vision.yaml field or logged decision covers the situation class.

What a reduction is not: the decision point disappears because the system avoids the situation that generated it.

### 1.2 The Reduction Criterion

A genuine management surface reduction satisfies: **a prior surface item is absorbed by the system without Dan-action**. This distinguishes reduction from elimination. Eliminating a capability removes both the surface item and the underlying function. Absorbing it keeps the function and removes the overhead.

Not a reduction:
- Removing a scheduled job (removes function + surface simultaneously)
- Changing the oracle round cap from 4 to 2 (changes threshold, not surface load)
- Moving handoff docs from one location to another (relocates, does not absorb)

A reduction:
- An oracle verdict pattern is stable enough that the oracle can auto-close a class of review without surface symptoms
- A bootup file entry is compressed into vision.yaml (agent now reads structural field rather than prose instruction)
- An observation loop closes without Dan's intervention because the signal already matches a logged decision class

### 1.3 PR Annotation: Management Surface Score at Merge Time

A lightweight tracking approach that creates the signal without requiring automation: **at merge time, the PR description includes a `surface:` annotation** for each category.

Format:

```
surface: A+1 B0 C-1 D0  net: 0
```

Where:
- `A` — configuration artifact load change (positive = added, negative = absorbed)
- `B` — supervision loop change
- `C` — handoff documentation change
- `D` — decision-demand change

The convention does not need to be enforced automatically. Even a norms-level commitment to annotate creates a durable signal: at quarterly hygiene time, a sweep of merged PRs reveals whether surface has been growing or shrinking. A pattern of consistent `A+1 C+1` with no negatives is the accumulation failure mode — the system is building up management weight without absorbing any.

The annotation is not a gate — it doesn't block merges. It is an observation type. Its value is as input to the drift-detection posture described in Section 2.

Oracle does not block on surface delta. It records it. The drift-detection posture reads the accumulated record.

---

## 2. Meta/Drift-Detection Agent Posture

This is a posture, not a review checklist. A posture is a persistent attentional orientation that shapes what gets noticed — not a sequence of steps that runs after the fact.

The posture's governing question is not "does this work?" (oracle's question) but **"does this make Dan more or less necessary as manager?"** These are orthogonal. A PR can be technically excellent, oracle-approved, and increase management surface simultaneously.

### 2.1 Activation Condition

The drift-detection posture activates on any of three triggers:

1. **Quarterly hygiene run** — The lobster-hygiene agent's quarterly pass explicitly includes a drift-detection phase, reading the last quarter's `surface:` PR annotations and drafting a trajectory signal.

2. **PR batch crosses a surface threshold** — When a batch of merged PRs accumulates a net management surface reduction of ≥3 points across categories (i.e., the system has demonstrably absorbed load, not just run well), the drift-detection posture runs to verify whether the reduction reflects genuine transparency movement or superficial load-shifting.

3. **Philosophy-explore session produces a new developmental observation** — Whenever a philosophy-explore session writes a new Developmental Map entry or updates the Theory of Learning, the drift-detection posture runs against recent development history to check whether current system development is consistent with the new developmental observation.

### 2.2 Operating Frame

The posture operates outside the reliability frame by holding a single question at its center: **Is Dan interacting with the system as an instrument he acts through, or as a reliable external object he maintains?**

The reliability frame produces outputs like: "tests pass," "oracle approved," "no user-reported issues," "throughput is up." These are all correct signals within their frame. The drift-detection posture is not interested in whether they are true. It is interested in whether the development pattern that produced them is moving toward a state where those signals become less relevant to Dan's experience — because the system is increasingly transparent.

Transparency in operation: Dan acts, outcomes happen, the system is not consciously experienced as an intermediary. Object-reliability in operation: Dan monitors, checks, manages, trusts — all of which require the system to remain in Dan's field of attention.

The posture reads:
- Surface delta annotations from recent PRs
- The corrective trace record — specifically, whether executors are operating more autonomously over time or generating more decision-demand
- The Developmental Map entries — what the current theoretical understanding predicts should be happening

The posture does not read code for correctness. It reads trajectories.

### 2.3 Output Format

The drift-detection posture produces a brief structured observation:

```
trajectory_signal: [toward_transparency | toward_object_reliability | ambiguous]
evidence: [one sentence — specific, verifiable, drawn from PR record or corrective traces]
implication: [one sentence — what this trajectory produces if extended 60 days]
surface_trend: [net surface delta over period assessed — e.g., "+4 over 12 PRs"]
```

Example (toward object-reliability):

```
trajectory_signal: toward_object_reliability
evidence: 8 of 12 PRs in the last quarter added Category B supervision loops without a stated retirement condition
implication: Dan's checking load is increasing with system capability rather than decreasing — if extended, each new executor type adds a permanent supervision overhead
surface_trend: +6 over 12 PRs
```

Example (toward transparency):

```
trajectory_signal: toward_transparency
evidence: PRs #1185–#1191 absorbed 3 supervision loops and compressed 2 CLAUDE.md entries into vision.yaml fields (net surface: A-2 B-3 C0 D0)
implication: Surface absorption pattern is consistent with transparency arc — observation loop closure is the natural next structural investment
surface_trend: -5 over 7 PRs
```

The output is terse because it is not a diagnosis — it is a signal input to the observation loop. It does not prescribe action. It adds a data point to the record.

### 2.4 Routing

Output goes to **`meta/drift-observations.md`** — append-only, timestamped. Not a PR comment. Not a blocking review. Not a task. It is an observation that enters the developmental history and is available as context for the next philosophy-explore session or quarterly hygiene pass.

The dispatcher surfaces it to Dan during the next philosophy-explore session intake, not as an alert.

### 2.5 Distinction from lobster-oracle

Oracle and drift-detection are orthogonal:

| Question | Agent |
|----------|-------|
| Is this code well-made? Does it solve the stated problem correctly? | lobster-oracle |
| Does this make Dan more or less necessary as manager? | drift-detection |

A PR can fail oracle (code has bugs) while moving toward transparency (the capability, once working, will reduce supervision load). A PR can pass oracle (technically excellent) while moving toward object-reliability (it adds a new supervision loop, a new configuration artifact, a new handoff doc). The postures are not in competition — they assess different things.

---

## 3. Upstream Posture: Design Question Answer

**Can the meta/drift-detection agent posture operate upstream of proposal generation — shaping what gets proposed — rather than only downstream as a post-hoc audit?**

**The direct answer: yes, with a specific mechanism. The mechanism is a `surface_constraint` field in the WOS UoW schema, populated at prescription time by the prescriber.**

### 3.1 The Two Positions

**Downstream (audit):** The drift-detection posture reads a completed proposal or merged PR and produces a trajectory signal. This is what Section 2 designs. It is useful for quarterly observation but cannot shape the proposal that already exists.

**Upstream (shaping):** The drift-detection frame is present when a proposal-generation agent is seeded. The agent generates a proposal that has already internalized the management-surface constraint — not as an afterthought, but as a first-class design criterion alongside correctness and reliability.

### 3.2 The oracle Analogy

The oracle posture provides the structural model. Oracle uses adversarial seeding: before seeing the implementation, the oracle agent receives a prior that asks "what could be wrong with this?" The adversarial frame is present before the evidence, which prevents post-hoc rationalization of whatever was built.

The management-surface frame needs the same structure: **seed before proposal generation, not after**. What the oracle does for correctness, `surface_constraint` does for management load.

### 3.3 The Structural Obstacle

Proposal-generating agents — functional-engineer, WOS executors, the prescription layer — are currently seeded with reliability-frame criteria:

- Success criteria (what completion looks like)
- Acceptance tests (how to verify it passed)
- Oracle criteria (is it well-made?)
- Register routing (is this the right executor type?)

The management-surface frame is absent at seed time. An executor reading a prescription has no instruction to prefer implementations that absorb surface over implementations that add surface, when both would satisfy the success criteria.

This is not a failure of the agents — they are correctly implementing the frame they received. The frame at dispatch time does not contain the transparency-arc constraint. Drift-detection can only observe this after the fact.

### 3.4 Proposed Mechanism: `surface_constraint` Field

Add a `surface_constraint` field to the WOS UoW schema at the prescription layer. The prescriber (the steward, or the upstream agent that generates the prescription) populates this field when generating a prescription.

Format:

```yaml
surface_constraint:
  target_delta: "0 or negative"
  prohibited_moves:
    - "Add a new configuration entry Dan must maintain without a corresponding absorption"
    - "Introduce a supervision loop without a stated retirement condition"
  preferred_moves:
    - "Absorb an existing handoff document section into system-generated output"
    - "Implement a decision class that currently routes to owner_decision_required"
```

**Effect at dispatch:** When the WOS executor reads the prescription, `surface_constraint` is a first-class field alongside `success_criteria` and `register`. The executor's approach generation is seeded with the management-surface frame before it generates a plan — not after the plan is reviewed.

**Effect at the prescription layer:** The steward or prescribing agent populates `surface_constraint` when writing prescriptions. This is where the upstream shift happens. The prescriber already operates in a context that has access to the Developmental Map and the current management surface trajectory (from `meta/drift-observations.md`). It is the correct point of injection.

**Three directions for `surface_constraint`:**

- `absorb` — The executor is instructed to prefer implementations that reduce an existing surface item. The executor's approach section must state which surface item it is targeting for absorption.
- `neutral` — The executor applies standard success-criteria-only evaluation. No surface preference.
- `exempt` — The executor is explicitly exempt from surface constraint (e.g., bootstrapping work that necessarily adds surface before it can absorb it). Exemption must be stated with a rationale.

The constraint is not a blocking gate — it does not prevent the executor from proceeding if it cannot find a surface-reducing implementation. It is a seed-time frame injection. The effect is that the executor reasons about management surface as it generates its approach, rather than discovering the constraint only after the work is done.

### 3.5 What This Does Not Require

The upstream mechanism does not require:
- Changes to individual executor agent postures (they read the field — no posture modification needed)
- A new agent type (the prescriber already exists — this adds a new population step)
- Automated surface measurement (the `surface:` PR annotation convention from Section 1.3 is sufficient input)

What it does require:
- The steward to read `meta/drift-observations.md` at prescription time
- A `surface_constraint` field in the UoW schema (schema addition)
- A populated `surface_constraint` in prescriptions (behavior change at the prescription layer)

---

## 4. Connection to Developmental Map

The Developmental Map (Theory of Learning, 2026-03-26 and 2026-03-27) identifies the **observation-to-behavioral-change loop** as the prerequisite coupling for the system's development. The claim: observations that do not route into behavioral change are not learning — they are record-keeping. The loop closes only when an observation produces a different behavior on the next encounter.

Management surface tracking is a candidate **observation type** for that loop. It produces a signal with the following properties:

- **Computable from existing artifacts** — PR descriptions with `surface:` annotations, UoW prescriptions with `surface_constraint` fields
- **Directional** — toward transparency or toward object-reliability, not just a count
- **Does not require Dan to decide anything** — the signal can flow into the Vision Object's Function 2 (observation-loop inlet) automatically

The Vision Object dual function entry in the Developmental Map establishes that Function 2 (observation-loop inlet) requires a mechanism that takes less than one Dan-decision to close a loop from observation to field change. Management surface tracking satisfies this: a drift-detection observation written to `meta/drift-observations.md` can compress into a `vision.yaml` field change by an agent, without requiring Dan's review — because the observation is structural, not judgmental.

**Sequencing implication:** The Developmental Map identifies the observation loop as the prerequisite bottleneck. This means management surface tracking is most valuable **after** the observation loop exists to carry the signal — not before. If the observation loop is not yet functional, management surface data accumulates alongside all other unprocessed observations.

The practical ordering:
1. Design and wire the observation loop (prerequisite — already identified in the Developmental Map)
2. Add management surface as an observation type flowing through that loop
3. Add the `surface_constraint` field to the UoW schema (upstream injection)
4. Add drift-detection posture activation to the quarterly hygiene run

This sequencing makes management surface tracking a natural beneficiary of the observation loop's construction — not a parallel track competing with it. The metric is ready. The loop is the prerequisite for the metric to do anything other than accumulate.

---

## See Also

- `~/lobster-user-config/agents/user.base.context.md` — Developmental Map, attentional budget constraint, Vision Object dual function
- `~/lobster-user-config/vision.yaml` — routing substrate and observation-loop inlet
- `meta/drift-observations.md` — routing target for drift-detection posture output (to be created when loop is active)
- `.claude/agents/lobster-hygiene.md` — quarterly hygiene agent that will host the drift-detection phase
- `docs/wos/design/wos-vision.md` — WOS vision and vocabulary
