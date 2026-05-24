# Canon and Presentation Layer Protocol

## The Distinction

**Canon layer** is the permanent, structured record:
- Committed to `~/lobster/workstreams/agent-council/canon/`
- Structured markdown with named fields and explicit relationships
- Git-versioned — every concept traceable to a commit
- Authoritative: if a concept is not in canon, it does not exist as council output

**Presentation layer** is the generative, explorable surface:
- Hosted as HTML artifacts on Bisque
- Renders canon content in explorable form (D3.js graphs, interactive filters)
- Ephemeral relative to canon — can be regenerated from canon

## The Core Principle: Canon First

Presentations should be *renderings of canon*, not sources of it. The correct flow:

1. Deliberation produces structured vocabulary → commit to canon
2. Canon entry triggers a presentation build → Bisque artifact published
3. Presentation may surface new observations → route back to canon as seeds

When this flow is inverted (presentation first, canon never), vocabulary is lost at compaction.

## The Routing Protocol

### At presentation delivery

When a Bisque artifact is delivered, the delivering agent must ask: **"If this file were deleted tomorrow, would the conceptual content still exist in the repo?"**

If no → canon extraction required before the work is marked complete.

### Canon extraction trigger conditions

A canon extraction pass is required when the presentation artifact contains any of:
- Named conceptual categories with definitions
- Parameter/measurement taxonomies with structured relationships
- Vocabulary coined or defined during the session (not just rendered from existing canon)
- Any term that would merit an index.md entry if it were a standalone file

### What belongs where

| Content type | Canon | Presentation |
|---|---|---|
| Named terms with definitions | ✓ | — |
| Taxonomies and hierarchies | ✓ | — |
| Principles and invariants | ✓ | — |
| Structural relationships | ✓ | — |
| Interactive graph layouts | — | ✓ |
| Color schemes, visual encoding | — | ✓ |
| Filter UI and interaction patterns | — | ✓ |
| Force simulation parameters | — | ✓ |

## The Canon-First Workflow (preferred)

1. Session produces vocabulary through deliberation
2. Commit vocabulary to canon as a draft/seed entry
3. Build the Bisque presentation from the canon file
4. Canon is already present — no extraction needed

## Audit Question

"If this Bisque file were deleted tomorrow, would the conceptual content still exist in the repo?"
If no: extraction required.
