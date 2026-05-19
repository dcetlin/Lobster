# LOS Attribution System — Formal Proposal

**Status**: Proposed  
**Date**: 2026-05-11  
**Authors**: Dan Cetlin + Lobster

---

## Problem

Active Todos is a convergence surface — items arrive from vault documents, Telegram messages, voice notes, and in-situ additions. Once an item lands in the list, its origin is invisible. Without provenance, two things become impossible: navigating back to context (what else is in that meeting doc? what was the thread of reasoning in that voice note?) and understanding what kind of thinking generated the item in the first place (ambient vs. deliberate vs. in-situ capture encode meaningfully different levels of commitment and context). Attribution restores both.

---

## The Grammar

The attribution system defines five formats, divided into two registers:

**Navigation register** — wiki-links that function as portals back to an origin document. Obsidian resolves these as clickable links; the link *is* the attribution, not a label for it.

**Archaeology register** — temporal anchors for ephemeral sources. These cannot be hyperlinked in Obsidian because the source is a message or audio file, not a vault document. The format encodes medium and date instead.

### Full Format Specification

```
[[doc-name]]                Navigation    From a vault document
[[doc-name#section]]        Navigation    From a specific section of a vault doc
[voice · Mon May 11]        Archaeology   From a voice note (ambient capture)
[tg · Mon May 11]           Archaeology   From Telegram (deliberate add)
[direct · Mon May 11]       Archaeology   Added in-situ in Active Todos
```

### Format Details

**`[[doc-name]]`**
- Meaning: Item originated in a named vault document. The document is the canonical home; Active Todos is a projection.
- Navigable: Yes — Obsidian resolves to the source file.
- Character: Structural. Items from documents tend to be deliberate, pre-organized, and nested in a body of related thought.

**`[[doc-name#section]]`**
- Meaning: Item originated in a specific section of a vault document. Section context is an actionability signal — a section headed "Next steps" vs. "Open questions" vs. "Someday maybe" carries different weight.
- Navigable: Yes — Obsidian resolves to the file and scrolls to the heading.
- Character: Structural + scoped. Finer-grained than doc-level; enables aspirational vs. actionable distinction at the source.

**`[voice · Mon May 11]`**
- Meaning: Item was captured from a voice note on the given date.
- Navigable: No — audio files are not vault documents. The date is the retrieval anchor.
- Character: Ambient. Voice capture is typically low-friction, high-volume, and context-rich but implicit. Reviewing the audio recovers what Active Todos cannot preserve.

**`[tg · Mon May 11]`**
- Meaning: Item arrived via Telegram on the given date.
- Navigable: No — Telegram messages are outside the vault. Date + medium are the retrieval anchors.
- Character: Deliberate. Sending a Telegram message to add a todo is an intentional act; these items tend to be crisper and more committed than ambient voice captures.

**`[direct · Mon May 11]`**
- Meaning: Item was typed directly into Active Todos, either by Dan or by Lobster acting on a direct instruction without an external source.
- Navigable: No — the item *is* the origin.
- Character: In-situ. Maximum immediacy; no upstream document or message. Context lives entirely in the item text.

---

## Display Form

Attribution renders as a sub-bullet directly beneath the todo item in Obsidian. The item text is untouched; attribution is a second line.

```markdown
- [ ] Plan the Tania ads campaign
      [[meeting-tania-2026-05-10#Next steps]]
- [ ] Follow up on law hosting decision
      [tg · Mon May 11]
- [ ] Think through vault-watcher Tier 2
      [voice · Sun May 10]
- [ ] Draft the Q3 retrospective agenda
      [[weekly-review-2026-05-08]]
- [ ] Book dentist appointment
      [direct · Mon May 11]
```

Sub-bullet indentation (spaces, not a `-`) keeps attribution visually subordinate to the item without creating a nested checkbox. Obsidian renders the wiki-link format as a live hyperlink in preview mode; archaeology anchors render as plain text.

---

## Data Model

The current `self_action_items.db` schema includes a `source` column (`telegram | obsidian | voice | direct`). This encodes medium but not location or time, making it insufficient for full attribution rendering.

### Proposed Schema Additions

```sql
ALTER TABLE action_items ADD COLUMN source_ref TEXT;
ALTER TABLE action_items ADD COLUMN source_section TEXT;
ALTER TABLE action_items ADD COLUMN source_date TEXT;
```

**`source_ref TEXT`**
- For `obsidian` source: the vault-relative path to the source document (e.g., `meetings/meeting-tania-2026-05-10.md`). Used to construct the `[[doc-name]]` wiki-link.
- For `telegram` source: the Telegram message ID. Not rendered in Obsidian output; available for audit/retrieval.
- For `voice` source: the audio filename or message ID. Same — not rendered, available for retrieval.
- Nullable. Existing items gain no provenance retroactively.

**`source_section TEXT`**
- For `obsidian` source: the section heading under which the `@lobster` annotation appeared (e.g., `Next steps`). Used to construct `[[doc-name#section]]`.
- Nullable. Items without section context render as `[[doc-name]]` only.

**`source_date TEXT`**
- ISO 8601 date string (e.g., `2026-05-11`). Used to render the date component of archaeology anchors.
- For `obsidian` source: the document's creation or last-modified date, or the date embedded in its filename.
- For `telegram` / `voice` / `direct` source: the date the item was created.
- Nullable. Items without source_date omit the date component in archaeology anchors.

These columns are additive and nullable. No existing items are modified. New items populate all applicable columns at creation time. The schema migration is a single `ALTER TABLE` sequence with no data backfill.

---

## Implementation Phases

### Phase 1 — Display Pass (no DB changes)

Render attribution in Obsidian sync output from the existing `source` field alone. No schema migration required.

**Scope**: `vault-processor.py` and `todo_obsidian_sync.py` output rendering.

**Behavior**:
- Each item in the Active Todos sync output gets a sub-bullet attribution line.
- `source = obsidian`: render `[[doc-name]]` — derive doc-name from existing source metadata (filename without extension). No section until Phase 2.
- `source = telegram`: render `[tg · Mon May 11]` — derive date from item creation timestamp.
- `source = voice`: render `[voice · Mon May 11]` — derive date from item creation timestamp.
- `source = direct`: render `[direct · Mon May 11]` — derive date from item creation timestamp.

Phase 1 is a pure rendering change. It makes provenance visible from what the DB already knows, without requiring any new capture logic.

### Phase 2 — DB Schema + source_ref Population

Add `source_ref`, `source_section`, and `source_date` columns via migration. Populate at item creation time in each handler:

- **vault-processor**: Knows the source document path and the section heading from the `@lobster` annotation context. Populates `source_ref` (doc path) and `source_section` (heading).
- **todo_obsidian_sync**: Knows the section heading from its heading scan. Can populate `source_section` for obsidian-sourced items that arrived without it.
- **Telegram handler**: Knows the message ID. Populates `source_ref` (message ID) and `source_date` (message timestamp).

With `source_section` populated, obsidian-sourced items upgrade from `[[doc-name]]` to `[[doc-name#section]]` in the sync output.

### Phase 3 — Bidirectionality

When vault-processor renders Active Todos, origin documents receive backlinks. Provenance becomes a live status indicator — not just "where did this come from" but "what happened to it."

**Format in origin document** (written by vault-processor at sync time):
```
@lobster → "Plan the Tania ads campaign" (open)
@lobster → "Plan the Tania ads campaign" (done 2026-05-14)
```

The backlink is written adjacent to the `@lobster` annotation that generated the item. When the item completes, vault-processor updates the status inline.

This phase is vault-watcher Tier 2 territory. Full bidirectionality depends on vault-processor having write access to source documents (Tier 2a — requires the Obsidian local REST plugin or equivalent). The read path (Active Todos → origin) is available in Phase 2; the write path (origin → Active Todos status) is a Phase 3 addition.

---

## What Doesn't Change

The following are out of scope for this proposal and will not be modified:

- DB write commands and their signatures
- Telegram `/todo` command handling and UX
- The `lobster-additions` block self-healing logic in `todo_obsidian_sync.py`
- The `DISABLE PROCESSING` guard in vault-processor

Attribution is additive. The existing system continues to function identically; Phase 1 adds a rendering layer on top of current output.

---

## Open Questions

These three questions require Dan's decision before Phase 1 build begins.

**1. Sub-bullet vs. inline suffix**

This proposal uses a sub-bullet (separate line, indented beneath the item). The alternative is an inline suffix at the end of the item text:

```markdown
- [ ] Plan the Tania ads campaign  [[meeting-tania-2026-05-10#Next steps]]
- [ ] Follow up on law hosting decision  [tg · Mon May 11]
```

Sub-bullet keeps the item text clean and attribution visually subordinate. Inline suffix keeps each item to one line and is more scannable in list view. Which register?

**2. Date format in archaeology anchors**

Three candidates:

- `Mon May 11` — human-readable, matches how Dan sketched it, no year (assumes current year)
- `2026-05-11` — ISO sortable, unambiguous across years, less readable at a glance
- `Mon 5.11` — Dan's original sketch format, compact

These appear inside Obsidian list items that are already dated by the vault's daily note structure in most workflows. Which format?

**3. Phase 1 scope: forward-only vs. retroactive best-effort**

Phase 1 can operate two ways:

- **Forward-only**: Attribution appears only on items created after Phase 1 ships. Existing items in Active Todos have no attribution line. Clean; no backfill logic.
- **Retroactive best-effort**: For existing items, derive attribution from the current `source` field and creation timestamp. Items would get medium + approximate date but no `source_ref` or `source_section`. Adds a one-time backfill pass to the migration.

Retroactive best-effort would make the entire existing Active Todos list attributed immediately, at the cost of a backfill step. Forward-only is simpler and leaves no ambiguity about attribution quality. Which scope?
