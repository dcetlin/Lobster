# Vision Coherence Anchor — Standard Preamble for Scheduled Jobs

All scheduled jobs should include a Vision Coherence Anchor section at the top of their `## Instructions` block. This makes vision-based coherence checking structural rather than conventional — each executor reads the same fields and applies the same filter logic.

## Standard Preamble (copy into task files)

```markdown
### Vision Coherence Anchor

Before executing, read `~/lobster-user-config/vision.yaml` (if it exists) and extract:
- `current_focus.this_week.primary` — the active work intent
- `current_focus.current_constraint.statement` — the binding constraint
- `current_focus.what_not_to_touch` — items explicitly excluded this week

Hold these as a coherence filter on the outputs of this job: does what you produce, surface, or flag serve the active intent? Does anything you are about to do touch an excluded item? If so, skip it and note why.

If vision.yaml is missing, continue without it and note the absence in write_task_output.

---
```

## Usage by Job Type

| Job type | What to filter | How to apply |
|----------|---------------|-------------|
| Synthesis (async-deep-work) | Themes surfaced, tension identified | Name the vision field the tension relates to; skip `what_not_to_touch` items |
| Philosophy exploration | Threads chosen, questions raised | Prefer threads that serve `this_week.primary`; note if session is off-focus |
| Hygiene/sweep | Items flagged for Dan's attention | Suppress items that touch `what_not_to_touch`; surface items that bear on `this_week.primary` |
| Classifier loops | Routing decisions | Not directly applicable — classifiers run on raw events; omit filter |
| Surface queue delivery | Items prioritized for delivery | Score items that relate to `current_focus` higher |
| Weekly retro | Themes and tensions surfaced | Apply same filter as synthesis jobs |

## Rationale

The Vision Object exists to make agent decisions traceable to intent rather than inferred from conversational texture. Without a structural read step, jobs absorb vision context indirectly (from handoff docs, memory) and produce outputs whose connection to intent cannot be verified. With the preamble, each executor can answer: "What vision field does this output serve?"

See also: `~/lobster-user-config/vision.yaml` (private, not in repo).
