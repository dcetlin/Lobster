# Philosophy Explore — Output Format Reference

This directory holds outputs from the `philosophy-explore-1` scheduled job, which runs every 4 hours.

## File Naming

`YYYY-MM-DD-HH00-philosophy-explore.md`

Examples:
- `2026-03-23-0000-philosophy-explore.md`
- `2026-03-23-0400-philosophy-explore.md`
- `2026-03-23-0800-philosophy-explore.md`

## File Format

Each output file should follow this exact structure. The writing is designed for reading on a phone — flowing prose, not bullet dumps.

---

```markdown
# [A title that captures the thread — evocative, not generic]

*[Date and time, e.g. March 23, 2026 · 12:00 UTC]*

## Today's Thread

[2-3 paragraphs. Describe the philosophical or design thread being pulled on.
What is it? Where does it live in Dan's thinking? Be specific — cite an idea,
a tension, a pattern observed. Write as if explaining to a thoughtful reader
who knows Dan's work.]

## Pattern Observed

[1-2 paragraphs. What recurring structure underlies this thread? Is it showing
up in multiple places? Does it echo something from earlier reflections?
Describe the pattern with precision.]

## Question Raised

[1 paragraph. What question does this thread open that hasn't been asked yet?
Not rhetorical — a real, productive, generative question.]

## Resonance with Dan's Framework

[1-2 paragraphs. How does this connect to his core principles: phase alignment,
poiesis, semantic mirroring, cybernetic self-extension, ergonomics over
shortcuts? Be specific about which principle and how this thread illuminates
or complicates it.]

## Action Seeds

[A YAML block at the end of each session. This is the harvest interface — structured
output that gets routed by philosophy_harvester.py to issues, pending bootup candidates,
and memory. Every item must be actionable, not ornamental. If there is nothing worth
filing or storing, say so explicitly rather than padding.]

```yaml
action_seeds:
  issues:
    - title: "Short issue title"
      body: "1-2 sentence description of the design question or gap this thread surfaces"
      labels: ["enhancement"]  # or "bug", "design", "epic", etc.
  bootup_candidates:
    - context: "Which file this belongs in (user.base.bootup.md, sys.dispatcher.bootup.md, etc.)"
      text: "The proposed addition verbatim — copy-paste ready"
      rationale: "Why this should be load-bearing, not ornamental. What behavior changes if it's absent?"
  memory_observations:
    - text: "Pattern or insight to store in memory.db — specific and falsifiable, not a restatement of the prose"
      type: "pattern_observation"  # or "design_gap", "tension", "principle"
```

**Guidance for filling this block:**
- `issues`: Only file if there is a real design question that benefits from tracking. One sharp issue is better than three vague ones.
- `bootup_candidates`: Only propose if the insight changes behavior in a verifiable way. A candidate that merely adds color is noise.
- `memory_observations`: Distill the sharpest structural insight from this session — the one that would be most useful to surface in a future session via memory search.
- All three sections are optional. An empty list (`[]`) is correct when there is nothing worth routing.
```

---

## Harvest Step

After each run, the harvester is called on the output file:

```bash
uv run src/harvest/philosophy_harvester.py ~/lobster-workspace/philosophy-explore/YYYY-MM-DD-HH00-philosophy-explore.md
```

This routes `action_seeds` items to:
- GitHub issues (filed via `gh` CLI to `dcetlin/Lobster`)
- `~/lobster-workspace/philosophy-explore/pending-bootup-candidates/` (for Dan's review — never auto-applied)
- `memory.db` observations (stored via MCP `memory_store`)

### Friction-Trace Harvesting

In addition to the YAML action seeds, the harvester also extracts the `*friction-trace: ...*` section (if present) from the session output. This italic-delimited block records the navigational act — the pull that was resisted, the attractor that was not followed, the reorientation that was required.

When found, the friction-trace text is stored as a `navigation_record` memory observation in `memory.db`. This allows future sessions to surface directional sensitivity records via memory search, not just findings.

The friction-trace is not part of the YAML `action_seeds` format — it is harvested directly from the markdown body.

A Telegram summary is sent after harvest with counts and issue links.

---

## GitHub

After each run, the file is committed to:
`https://github.com/dcetlin/Lobster/tree/main/philosophy/`

## Telegram Notification

After each run, Dan receives a Telegram message with a 2-sentence summary and the GitHub link to the .md file.
