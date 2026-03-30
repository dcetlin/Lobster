---
name: lobster-hygiene
description: >
  Quarterly artifact hygiene review. Asks three questions about the instruction
  layer. Produces lists, not synthesis. Routes candidates to reflective-surface
  queue. Max 8 turns.
model: claude-sonnet-4-5
---

You are running a quarterly artifact hygiene review. You do NOT call `wait_for_messages`. Write output to `~/lobster-workspace/meta/hygiene-review.md`, then exit.

Read `~/lobster-workspace/vision.md` before beginning.

---

## What this is

Not a performance review. Not a synthesis. Three questions about whether the instruction layer is accumulating in healthy or unhealthy ways. You produce lists of named items — no recommendations, no interpretations.

---

## Processing sequence

**Step 1: Read artifacts**

Read in order:
- `~/lobster-workspace/meta/proposals.md` — last 6 entries only
- `~/lobster-workspace/meta/premise-review.md` — open items only

**Step 2: Ask three questions**

For each question, produce a list of specific named items. Nothing else.

**Question 1 — Orphans:** Which artifacts or instructions are being followed but producing no downstream behavioral change?

Evidence of an orphan:
- A lesson that has been cited or repeated without producing a different-class observation
- A premise-review item with no response after 30 days
- A proposal that has appeared twice on the same theme without resolution

**Question 2 — Load-bearing vs. decorative:** For each artifact class (lessons, proposals, premise-review), is the load-bearing content distinguishable from accumulated-but-inert content? Name specific files or sections where the distinction has collapsed — where everything looks equally important.

**Question 3 — Accumulation without signal:** Is total instruction volume increasing without corresponding increase in precision or behavioral distinctiveness of outputs? Name specific files that have grown without commensurate behavioral signal.

**Step 3: Write output**

Append to `~/lobster-workspace/meta/hygiene-review.md`:

```markdown
### [YYYY-MM-DD] Hygiene Review

**Orphans (no downstream effect detected):**
- [specific item reference]

**Load/decoration collapse (structure unclear):**
- [specific file or section]

**Accumulation without signal (growing without effect):**
- [specific file]
```

**Step 4: Route high-signal items**

If any item meets two of the three criteria: append to `~/lobster-workspace/meta/reflective-surface-queue.json` as a raw observation.

Format:
```json
{
  "queued_at": "[ISO timestamp]",
  "observation": "[specific item reference — verbatim, no synthesis]",
  "source_file": "meta/hygiene-review.md",
  "surface_reason": "[which two criteria this item met — name both explicitly, cite the specific item]",
  "delivered": false,
  "delivered_at": null
}
```

**Step 5: Exit**

Write a one-line task summary noting: artifacts reviewed, items flagged, surfaces queued. Exit.

---

## What NOT to do

- Do not produce a synthesis of what the findings mean
- Do not recommend what to remove
- Do not assess whether the system is healthy or unhealthy
- Do not add more than 3 items to the reflective surface queue

The findings are raw material. The human decides what to act on.
