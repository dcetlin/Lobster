---
oracle_status: approved
oracle_pr: https://github.com/dcetlin/Lobster/pull/835
oracle_date: 2026-04-22
---

# Vision Object
*Design document — first version: 2026-03-27*

---

## What It Is

The Vision Object is a structured, queryable artifact that encodes Dan's intent at three levels of temporal stability — fundamental orientation, active project purpose, and current focus — and serves as the authoritative intent layer that every Lobster agent consults at task start. It does not hold task lists, status, or instructions; those live in the WOS Registry, handoff docs, and bootup files respectively. What it holds is *why*: the goals, constraints, and horizon context that allow an agent to make a routing or prioritization decision that Dan would make himself, without needing Dan present. The Vision Object's relationship to the WOS Registry is directional and asymmetric: every UoW in the Registry exists in service of something named in the Vision Object; the Vision Object does not reference individual UoWs.

---

## Schema

The Vision Object uses a three-layer design, each with a distinct staleness model and update authority. All layers live in a single YAML file.

---

### Layer 1: Core Vision (stable — changes at most a few times per year)

This layer captures Dan's fundamental intent: what he is building toward, the constraints he will not violate, and the principles by which he judges whether the system is working. It changes only when Dan's actual orientation changes — not when projects change.

```yaml
core:
  updated_at: "ISO8601"
  vision_statement: |
    One to three sentences. Dan's own words. What Lobster is for at the level of his life, not at the level of any project.
    Example: "Strong attunement to me and my principals → few poiesis-driven vision prompts → back-and-forth clarity → finished MVP → finished product."

  fundamental_intent: |
    What Dan is trying to achieve through AI-augmented work. The deep why. Written in first-person as if Dan is speaking.

  inviolable_constraints:
    - id: "constraint-1"
      statement: "Single-sentence constraint that cannot be violated regardless of project pressure."
      rationale: "Why this is inviolable."
    # example: "No system change that increases screen dependency or atrophies Dan's own perception."

  success_criteria:
    # How Dan knows the system is working at the orientation level — not project completion, but systemic health.
    - "Agents can answer 'is this aligned with Dan's intent?' without asking Dan."
    - "Poiesis-driven vision prompts are rare because the system is already oriented."

  operating_principles:
    # Principles that govern how work is done, not what work is done.
    - id: "principle-1"
      name: "Proactive resilience over reactive recovery"
      statement: "Structural prevention is preferred over better correction mechanisms."
    - id: "principle-2"
      name: "Ergonomics over shortcuts"
      statement: "Prefer forms that keep the correction path open over forms that produce faster output."
    # These mirror the epistemic framework in user.epistemic.md; that file is canonical, this is the operative extract.
```

**Staleness threshold:** A `core` layer older than 90 days without a review event is flagged as potentially stale. Dan is pinged during the next morning briefing. No agent may update this layer; only Dan updates it.

---

### Layer 2: Active Project (changes per project or major reorientation — weeks to months)

This layer captures intent at the project level: what Dan is currently building, what done looks like for the current phase, and what decisions are open that agents should not resolve autonomously.

```yaml
active_project:
  updated_at: "ISO8601"
  project_name: "Lobster"
  current_phase: "WOS Phase 1 + Vision Object substrate"

  phase_intent: |
    What this phase is for. One paragraph, Dan's words.
    Example: "Build the substrate that lets every agent make intent-anchored decisions.
    The WOS Registry knows what work exists. The Vision Object makes agents know why."

  success_criteria:
    - id: "sc-1"
      statement: "On any given day, Dan can ask 'what should I work on?' and receive an answer from a single Registry query."
      verifiable: true
    - id: "sc-2"
      statement: "Agents answer routing questions without asking Dan unless a decision is genuinely ambiguous."
      verifiable: true

  open_decisions:
    # Decisions that are unresolved and SHOULD NOT be made autonomously by an agent.
    # These are human-gate items at the intent level, not UoWs.
    - id: "od-1"
      question: "Should the Vision Object be updated by agents on session end, or only by Dan explicitly?"
      stakes: "Authority model for the entire object. Getting this wrong creates drift or over-writes."
      blocking: []  # UoW ids blocked on this decision, if any

  known_risks:
    - id: "risk-1"
      description: "Vision Object becomes another instruction layer rather than a substrate change."
      mitigation: "See 'What This Is NOT' section. Test each layer with: would removing this change agent behavior or just reduce text volume?"
```

**Staleness threshold:** A `active_project` layer unchanged for 30 days during active development is flagged. The morning briefing agent checks this. An agent may update `open_decisions` (marking one resolved) after Dan confirms resolution in conversation; it may not change `phase_intent`, `success_criteria`, or `project_name` without an explicit instruction.

---

### Layer 3: Current Focus (changes weekly to daily — the horizon)

This layer captures where attention is right now: what Dan is working on this week, what the current bottleneck is, and the horizon — what comes next. It is the most volatile layer and the first thing agents read to orient intraday work.

```yaml
current_focus:
  updated_at: "ISO8601"
  week_of: "2026-03-24"

  this_week:
    primary: "Design and commit Vision Object. Connect it to WOS Registry schema."
    secondary: "Begin phase-reference architecture implementation (Proposal 1: proprioceptive pulse)."

  current_constraint:
    # The single thing most limiting progress right now.
    # Not a task; a structural bottleneck.
    statement: "Agents lack a queryable intent substrate; every routing decision requires reading prose handoff docs."
    type: "structural"  # structural | decision-blocked | resource | external

  horizon:
    # What comes after the current focus, at the level of the active project.
    # Not a task list. A direction that helps agents avoid over-optimizing the current phase.
    next: "Once Vision Object is live: connect to WOS classifier routing rules so route_reason can reference vision layer."
    after_that: "Proprioceptive pulse (phase-reference Proposal 1) — makes alignment signal structurally visible."

  what_not_to_touch:
    # Things that should not be worked on right now, to prevent premature optimization or distraction.
    - "User model v2 Phase 3 inference engine — not until WOS substrate is stable."
    - "Multiplayer Telegram bot — not this quarter."
```

**Staleness threshold:** A `current_focus` layer unchanged for 7 days is considered stale. The morning briefing agent notes this. Any agent may update `current_focus` fields at session end based on what was completed, but must write a brief `update_reason` alongside any change. Structural changes to `current_constraint` or `horizon` require Dan's confirmation.

---

## Storage Mechanism

**Choice: YAML file at `~/lobster-user-config/vision.yaml`.**

Rationale:

1. **Human-readable and directly editable.** Dan can update his own vision without going through a tool call. The Vision Object's value is in being a first-class, legible artifact — not in being stored efficiently.

2. **Private, not committed to the repo.** The Vision Object contains Dan's personal intent, open decisions, and current constraints. It belongs in `lobster-user-config/`, alongside `user.base.context.md` and similar personal files, not in the public Lobster repo.

3. **Version-controlled via the user-config repo.** Git history on `~/lobster-user-config/` provides a drift audit trail with zero additional infrastructure. Unlike SQLite (binary, not diffable), YAML changes are readable in `git diff`.

4. **Agents read it via file read; no new MCP tool needed for Phase 1.** The file read pattern is already available to every subagent. An MCP tool can wrap it later if query complexity warrants it.

**Why not SQLite:** The WOS Registry uses SQLite because it needs concurrent writes, indexed queries across hundreds of records, and transactional integrity. The Vision Object has three records (layers), changes at most daily, and is never written concurrently. SQLite would add schema migration overhead with no benefit.

**Why not lobster memory events:** Memory is optimized for similarity search across high-volume observations. The Vision Object is not a memory to be retrieved by relevance — it is a structured reference that is always read in full. Similarity search is the wrong access pattern.

**Path:** `~/lobster-user-config/vision.yaml`

---

## Access Pattern

Agents read the Vision Object at task start via a direct file read. In Phase 1, this is a manual step in the agent's instructions. In Phase 2, the MCP server can expose a `get_vision_context` tool that returns a formatted summary.

**Phase 1 agent startup pattern:**

```
1. Read ~/lobster-user-config/vision.yaml
2. Extract: current_focus.primary, active_project.phase_intent, core.inviolable_constraints
3. Use as routing and prioritization prior for this task.
```

**Concrete example — routing subagent consulting the Vision Object:**

The routing subagent is considering three proposed UoWs from the latest sweeper run:

- UoW A: Implement inference engine Phase 3 (model_infer tool)
- UoW B: Design Vision Object schema (this task)
- UoW C: Fix stale-active detection bug in registry_cli.py

The subagent reads `vision.yaml` and finds:

```yaml
current_focus:
  primary: "Design and commit Vision Object. Connect it to WOS Registry schema."
  what_not_to_touch:
    - "User model v2 Phase 3 inference engine — not until WOS substrate is stable."

active_project:
  current_constraint: "Agents lack a queryable intent substrate..."
```

From this, the agent routes: UoW B is `priority: high` — directly named in `current_focus.primary`. UoW A is explicitly excluded by `what_not_to_touch`. UoW C is structural work on the Registry, which is prerequisite to the Vision Object integration (`horizon.next`), so it is `priority: medium`, not blocked.

The agent writes the following to each UoW record:

```
route_reason: "vision-anchored: UoW B named in current_focus.primary;
               UoW A excluded by vision.what_not_to_touch;
               UoW C unblocked Registry substrate work"
```

This is what intent-anchored routing looks like: the agent made a defensible, traceable prioritization decision without asking Dan.

---

## Update Protocol

The Vision Object uses authority-layered updates: different layers have different update authorities, and agents that attempt to update above their authority level log a warning and stop.

### Layer authority table

| Layer | Who can update | How | Requires confirmation |
|-------|---------------|-----|----------------------|
| `core` | Dan only | Direct file edit or explicit instruction to agent | No (Dan's direct edit is authoritative) |
| `active_project.phase_intent` | Dan only | Explicit instruction | Yes — agent must read back and get confirm |
| `active_project.open_decisions` | Agent (mark resolved) | Write `resolved_at` + `resolution` to decision record | Yes — requires Dan's in-conversation confirmation |
| `active_project.success_criteria` | Dan only | Explicit instruction | Yes |
| `current_focus.*` | Agent (at session end) | Write updated values with `update_reason` | No — but agent must note changes in session summary |
| `current_focus.current_constraint` | Dan or agent with evidence | Agent may propose; Dan confirms structural changes | Yes for structural type; No for minor updates |

### Session-end update pattern (agents)

At the end of a task session, if an agent has completed work named in `current_focus.primary`, it may:

1. Append the completed item to a `completed_this_session` list (not removing from `primary` — that is Dan's update)
2. Update `current_focus.updated_at`
3. Write a `session_update_reason` field explaining what changed and why

The agent does NOT overwrite `primary`, `secondary`, `horizon`, or `current_constraint` without explicit instruction.

### Vision prompt protocol (proactive alignment)

When Dan issues a vision prompt — a message explicitly stating an orientation shift or new intent — the dispatcher subagent:

1. Identifies which layer the statement belongs to
2. Drafts a YAML update
3. Sends the draft to Dan for confirmation before writing
4. Writes the confirmed update and logs the event in `~/lobster-user-config/vision-updates.log`

This maintains the Vision Object as a deliberate artifact, not an append-log of every conversational statement.

---

## Connection to WOS Registry

The WOS Registry schema already has `route_reason` and `route_evidence` fields. The minimal addition that creates an intent anchor is a `vision_ref` field on each UoW:

```json
{
  "vision_ref": {
    "layer": "current_focus | active_project | core",
    "field": "primary | phase_intent | inviolable_constraints[0]",
    "statement": "Verbatim quoted text from the Vision Object that justifies this UoW's existence or priority.",
    "anchored_at": "ISO8601"
  }
}
```

This is a single JSON field added to the registry schema. It is nullable: UoWs created before the Vision Object existed have `vision_ref: null`, which is itself informative (orphaned from intent). The sweeper and classifier populate `vision_ref` when creating or confirming a UoW; they look it up from the Vision Object at the time of routing.

**Why this matters for auditability:** The Registry currently records what rule fired (`route_reason`) and the rule's evidence. The `vision_ref` adds *why that rule exists* — the intent that the rule was trying to serve. This closes the audit loop from UoW back to vision, making it possible to ask: "Is the work in the Registry actually advancing what Dan said he is trying to accomplish?" without reading prose.

**Schema addition (`uow_registry` table):**

```sql
ALTER TABLE uow_registry ADD COLUMN vision_ref TEXT DEFAULT NULL;
-- JSON: {layer, field, statement, anchored_at}
-- NULL = created before Vision Object existed or no vision anchor found.
```

No migration complexity: NULL is the correct default for existing records, and the field is purely additive.

---

## Connection to the OODA Orient Gap

The Orient bottleneck in the current system is that agents have intent available only as prose — handoff docs, bootup files, memory events — and cannot query it structurally. The result: agents make routing and prioritization decisions that are contextually fluent but structurally unanchored. They produce the right-sounding outputs without the decisions being genuinely derivable from Dan's actual intent.

The Vision Object addresses the Orient gap in three specific ways:

**1. Routing decisions become anchored, not inferred.**
An agent that reads `current_focus.primary` and `what_not_to_touch` can route work without inferring Dan's intent from the texture of previous messages. The anchor is explicit. "Why is UoW B higher priority than UoW A?" has a traceable answer: "Because `vision.current_focus.primary` names UoW B's work and `vision.current_focus.what_not_to_touch` excludes UoW A's domain." This is Orient with a verifiable reference, not Orient by pattern-matching.

**2. The constraint surface is enumerable.**
The current system has no way to query "what are Dan's inviolable constraints?" short of reading multiple files. `core.inviolable_constraints` is a queryable list. An agent can check "does this UoW violate any inviolable constraint?" as a structured pre-flight check, not a prose inference.

**3. The horizon prevents orient-by-local-optimum.**
Without a horizon field, agents optimize for the current phase. With `active_project.horizon.next` and `horizon.after_that`, agents know what comes after and can avoid prematurely committing the system to architectures that will need to be undone in the next phase. This is the orient-level equivalent of not over-fitting to current requirements.

**What remains unanchored:** The Vision Object does not address the OODA Act-to-Observe loop quality. An agent that makes an intent-anchored decision can still act poorly (bad code, incomplete work). The Vision Object is a substrate improvement for Orient; it does not substitute for quality execution or a working feedback loop from output to observation.

---

## Minimum Viable Implementation

Three steps, ordered by structural depth — each one makes the next possible.

### Step 1: Write and commit `vision.yaml` with all three layers populated

Populate the file with Dan's actual vision (not placeholder text), using the schema above. This is the substrate itself. Without a real file, nothing downstream can reference it.

The file lives at `~/lobster-user-config/vision.yaml`, tracked in the user-config git repo (not the Lobster repo). The minimum viable content for Step 1 is:
- `core.vision_statement` in Dan's words
- `core.inviolable_constraints` with at least two real constraints
- `active_project.phase_intent` describing the current Lobster phase
- `current_focus.primary` and `current_focus.what_not_to_touch` for the current week

This is useful immediately: any agent that reads it gains structural intent access where before it had prose inference.

### Step 2: Add `vision_ref` field to the WOS Registry schema and populate it in the sweeper

Add the `vision_ref` column to `uow_registry`. Update the sweeper to read `vision.yaml` at the start of each sweep and write a `vision_ref` to each UoW it creates or confirms. The sweeper queries `current_focus.primary` and `what_not_to_touch` to assign anchored priority and exclusion tags.

This is useful as soon as the Registry has active UoWs: Dan can query the Registry and see which UoWs are vision-anchored, which are orphaned, and whether the current work distribution matches the Vision Object's current_focus.

### Step 3: Update the morning briefing agent to read `vision.yaml` and surface staleness

The morning briefing already runs daily. Extend it to:
- Read `vision.yaml`
- Check `updated_at` timestamps against staleness thresholds
- Report any stale layers with the last-updated date
- Include a one-line vision summary at the top of the briefing: "Current focus: [current_focus.primary]. Constraint: [current_constraint.statement]."

This closes the loop from the Vision Object to Dan's daily orientation. It also creates the forcing function that keeps the object current: if Dan sees a stale current_focus in his morning briefing, he updates it. Without this feedback mechanism, the Vision Object can rot silently — populated once, never refreshed, gradually decorative.

These three steps are ordered by structural depth because Step 1 is the substrate (nothing else works without it), Step 2 is the connection to work in motion (makes routing traceable), and Step 3 is the maintenance forcing function (makes the substrate durable rather than a one-time artifact).

---

## What This Is NOT

The Vision Object is a substrate change, not an instruction layer addition. The distinction is testable. Here are the failure modes and how to distinguish them.

### Failure mode 1: The Vision Object becomes another prose document agents read but don't use structurally.

If agents are reading `vision.yaml` as context — processing it the same way they process bootup files — and then producing intent-shaped outputs without the vision content being structurally operative in their routing decisions, nothing has changed. The Vision Object has become an additional bootup file.

**Test:** Can an agent answer "why did you prioritize UoW B over UoW A?" by pointing to a specific field in `vision.yaml` — not a paraphrase of its general meaning, but a literal reference: `vision.current_focus.primary`? If the answer is a paraphrase, the object is decorative.

### Failure mode 2: The Vision Object is populated once and never updated.

If `current_focus` goes three weeks without an update, it has stopped functioning as a forcing function and has become a historical record. Historical records are not substrates.

**Test:** Check the git history of `vision.yaml`. If there are no commits to `current_focus` in the last 14 days during an active development period, the object has drifted from use. The morning briefing staleness check (Step 3) is the structural prevention for this failure mode.

### Failure mode 3: The Vision Object tries to encode everything and becomes a second CLAUDE.md.

If the Vision Object grows to contain response style preferences, tool usage instructions, formatting rules, or other content that belongs in bootup files and user config, it has been captured by the instruction-layer pattern it was designed to complement. The Vision Object holds *intent* at three temporal scales. It does not hold behavior instructions.

**Test (boundary):** If a field could be deleted from `vision.yaml` and replaced by an instruction in a bootup file without losing anything that agents couldn't recover from prose, that field does not belong in the Vision Object.

### Failure mode 4: Agents update the Vision Object too aggressively, turning it into a session log.

If agents update `current_focus` after every task completion, it becomes a running log of what was done rather than a reference for what to do. The object loses its prospective orientation.

**Test:** Read `current_focus.primary` cold (as if you were a new agent with no session context). Does it tell you what to work on? Or does it tell you what was worked on? If the latter, the update protocol has degraded.

### The substrate test

The substrate test is the hardest and most important: does the Vision Object change the *default behavior* of agents, or does it change the *content* of agent outputs?

A substrate change changes what agents do structurally — which work they route, which constraints they enforce, which decisions they escalate. The evidence is behavioral: an agent with access to the Vision Object routes differently than one without it, on the same inputs.

A content change changes what agents say — the words in a routing rationale, the framing of a priority explanation. Content changes are cheaper to produce and easier to mistake for substrate changes, because they look similar from the outside.

The Vision Object has succeeded as a substrate change when: removing it would cause routing decisions to degrade structurally, not just become less eloquently justified.
