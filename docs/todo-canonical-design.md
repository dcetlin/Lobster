# Canonical TODO Store — Design

**Date:** 2026-05-10  
**Status:** Implemented (initial canonicalization complete)

---

## 1. The Canonical Model

**Single source of truth:** `~/lobster-user-config/data/self_action_items.db`

`self_action_items.db` is the authoritative store. Everything else is either a view generated from it (ACTIVE TODOS.md) or an ingestion path into it (Obsidian journals, Telegram messages, voice notes). No other file owns the state of a TODO.

The Obsidian file `✅ ACTIVE TODOS.md` is a **generated view** — written by the LOS sweep, committed to the vault, pushed to the Mac. It is human-readable and editable, but changes Dan makes to it are read back into the DB on the next sync pass, not treated as authoritative in isolation.

---

## 2. Schema

Existing `action_items` table, extended with new fields added via `ALTER TABLE`:

```sql
-- Existing fields (preserved as-is)
id               INTEGER PRIMARY KEY AUTOINCREMENT
text             TEXT NOT NULL
source           TEXT NOT NULL          -- 'obsidian:<filename>' | 'telegram' | 'voice_note' | 'manual'
source_message_id TEXT
extracted_at     TEXT NOT NULL          -- ISO-8601
priority         INTEGER DEFAULT 5      -- 1=urgent, 5=normal, 7=someday; lower = higher priority
mention_count    INTEGER DEFAULT 1
status           TEXT DEFAULT 'open'    -- 'open' | 'done' | 'archived' | 'snoozed' | 'dismissed'
snoozed_until    TEXT
done_at          TEXT
dismissed_at     TEXT
notes            TEXT
dedup_key        TEXT

-- New fields added for canonical design
workstream       TEXT                   -- e.g. 'music', 'work-angellist', 'lobster', 'home', 'relationships', 'health', 'finance', 'someday'
project          TEXT                   -- optional sub-grouping within workstream
action_type      TEXT                   -- 'task' | 'habit' | 'vision' | 'reminder'
github_issue_url TEXT                   -- populated at extraction-time or enrichment pass
archived_at      TEXT                   -- set when moved to archived status
last_activity_at TEXT                   -- updated on any status change, mention, or note addition
```

Migration SQL (additive, non-destructive):

```sql
ALTER TABLE action_items ADD COLUMN workstream TEXT;
ALTER TABLE action_items ADD COLUMN project TEXT;
ALTER TABLE action_items ADD COLUMN action_type TEXT DEFAULT 'task';
ALTER TABLE action_items ADD COLUMN github_issue_url TEXT;
ALTER TABLE action_items ADD COLUMN archived_at TEXT;
ALTER TABLE action_items ADD COLUMN last_activity_at TEXT;
```

**Priority scale (integer):** 1–3 = urgent/this-week, 4–6 = active, 7–9 = someday/aspirational. The LOS sweep already uses this scale; we formalize it here.

**Archival rule:** items with `last_activity_at < now - 7 days` AND `status = 'open'` are moved to `status = 'archived'` by the nightly LOS sweep. Archived items are retained in the DB; they do not appear in ACTIVE TODOS.md unless explicitly queried.

---

## 3. Workstream Taxonomy

The top-level workstream field organizes items for the generated view:

```
work-angellist    — AngelList engineering work items
work-general      — general professional tasks
lobster           — Lobster system development  
music             — album, violin, cello, recording
source-code       — Source Code sessions, website, brand
relationships     — people to reach out to, social commitments
tania             — items specifically involving Tania
health            — body, wellness, sleep, habits
home              — logistics, home infrastructure, gear
finance           — crypto, income, financial sovereignty
learning          — books, study, skills
someday           — visions, aspirational, open-ended
```

Items that cross boundaries get the primary workstream; the `project` field handles sub-grouping (e.g. `workstream=music, project=album-release`).

---

## 4. Bidirectional Sync Architecture

```
Obsidian vault (~/obsidian-vault/)
  ↕ git push/pull (obsidian-git on Mac)

VPS ~/obsidian-vault/✅ ACTIVE TODOS.md
  ↑ written by LOS sweep (Lobster → Obsidian direction)
  ↓ read by sync job (Obsidian → Lobster direction)

self_action_items.db  ←── CANONICAL SOURCE OF TRUTH
  ↑ Telegram messages + voice notes (Lobster ingestion)
  ↑ Obsidian journal sweep (LOS nightly pass)
  ↑ Obsidian bidirectional sync (reads edits Dan makes to ACTIVE TODOS.md)
```

### 4a. Lobster → Obsidian (write direction)

**Sole writer:** `todo_obsidian_sync.py` (runs every 30 min) is the **exclusive** writer of
`ACTIVE TODOS.md`. No other job — including any future LOS nightly sweep — should call
`render_active_todos()` or write this file directly.

Rationale: if a second job regenerates the file independently, it creates a race condition
where Dan's unsynced Obsidian checkmarks are overwritten before `todo_obsidian_sync.py` can
read and persist them to the DB. See the module docstring in `todo_obsidian_sync.py` for
the full failure sequence.

Any future LOS nightly sweep should write **only** to the DB (archival, extraction, etc.).
The file regeneration step is `todo_obsidian_sync.py`'s responsibility.

Process (performed by `todo_obsidian_sync.py` in a single atomic pass):
1. git pull the vault (get latest Mac edits)
2. Read Dan's checkmarks from ACTIVE TODOS.md
3. Persist checkmark changes to DB
4. Query `action_items` where `status IN ('open', 'snoozed')` ordered by priority, workstream, extracted_at.
5. Render `✅ ACTIVE TODOS.md` with the symbol hierarchy (see Section 6).
6. Commit to `~/obsidian-vault/` and push.

### 4b. Obsidian → Lobster (read direction)

Trigger: nightly, after the Mac has had a chance to push vault changes.

Process:
1. Read `✅ ACTIVE TODOS.md` from vault.
2. Parse all checkboxes: `- [x] item text` = completed, `- [ ] item text` = open.
3. For each `- [x]` item: find matching row in DB by text fuzzy match (normalized), set `status = 'done'`, `done_at = now`.
4. For new `- [ ]` items not in DB (detected by absence of dedup_key match): insert with `source = 'obsidian:ACTIVE TODOS.md'`, `action_type = 'task'`.
5. For rearrangements (priority changes by section): update `priority` field if the item moved to a different section.

**Dedup key:**
```python
import hashlib, re
def dedup_key(text: str) -> str:
    n = re.sub(r'[^a-z0-9 ]', '', text.lower())
    n = re.sub(r'\s+', ' ', n).strip()
    return hashlib.sha256(n.encode()).hexdigest()[:16]
```

### 4c. Telegram → DB (ingestion)

When Dan says "add to my list: X" or "remind me to X", the dispatcher extracts the item and inserts directly into DB with `source = 'telegram'`.

Voice notes go through the brain-dump pipeline which calls `extract_action_items` and inserts into DB with `source = 'voice_note'`.

---

## 5. Archival Policy

Items are archived (not deleted) when:
- `status = 'open'` AND `last_activity_at < now - 7 days` — moved to `status = 'archived'`
- OR user marks as "not relevant" via Telegram button — moved to `status = 'dismissed'`

Archived items do not appear in `ACTIVE TODOS.md`. They remain in the DB and are queryable via `status = 'archived'`.

Any future LOS nightly sweep runs archival before the next `todo_obsidian_sync.py` pass regenerates ACTIVE TODOS.md. The sweep writes only to the DB; `todo_obsidian_sync.py` picks up the archival changes on its next run and omits archived items from the rendered file.

---

## 6. ACTIVE TODOS.md Format

Symbol hierarchy (no markdown tables, consistent with Dan's preferences):

```markdown
# ✅ ACTIVE TODOS
*Generated by LOS — N open items as of YYYY-MM-DD*

## Urgent / This Week (P1–P3)
- [ ] Item text  *(source)*

## Active (P4–P6)

### work-angellist
- [ ] Item text  *(source)*

### music
- [ ] Item text  *(source)*

### [other workstreams...]

## Someday / Aspirational (P7–P9)
- [ ] Item text  *(source)*

---
*To mark done, dismiss, or snooze: tell Lobster via Telegram, or check the box in Obsidian.*
*Next auto-sweep: nightly, ~02:30.*
```

---

## 7. Subtasks

### Schema

A `parent_id` column links a subtask to its parent:

```sql
parent_id INTEGER REFERENCES action_items(id)
```

Added via `_SCHEMA_MIGRATIONS` in `src/los/db.py`. NULL means top-level item.

### Depth Cap

Maximum 2 levels: task → subtask. Grandchildren are not supported. Enforcement:
- DB: no constraint (kept simple); enforced in `handle_todo_add` and sync parsing.
- `handle_todo_add` rejects `--parent <id>` if the target item already has a `parent_id`.
- `todo_obsidian_sync.py` never nests indented items further than one level.

### Obsidian Rendering

Top-level items render with an `<!-- id:N -->` anchor comment:

```markdown
- [ ] Fix the sync bug <!-- id:42 -->
  - [ ] Write failing test <!-- id:43 parent:42 -->
  - [ ] Implement fix <!-- id:44 parent:42 -->
```

Rules:
- Subtasks appear immediately after their parent (not in a separate section).
- 2-space indent is the visual signal in Obsidian; `parent:N` is the machine signal.
- Only open/snoozed subtasks are rendered (done subtasks are excluded).

### Sync Parsing

`parse_active_todos()` distinguishes:
- `^- \[.?\] ` (no leading spaces) → top-level item
- `^  - \[.?\] ` (2-space indent) → subtask

For subtasks:
- `parent_id` is extracted from `<!-- parent:N -->` comment.
- `[x]` → `mark_done()` for that item.
- New `[ ]` items (no id in DB): inserted with `parent_id` set.
- Existing `[ ]` items (found by dedup_key): no-op or priority update (same as top-level).

### Telegram Command

```
/todo add <text> --parent <id>
```

- `--parent` must come last.
- Errors:
  - "No item with ID N" — if parent does not exist.
  - "Cannot nest deeper than 2 levels" — if parent already has a parent_id.

---

## 8. GitHub Links — Phase 2

GitHub issue URLs are stored in `github_issue_url` on each action item.

**Phase 1 (now):** field exists but is empty. Items extracted from journal text that contain `#NNN` issue references get the URL populated at extraction time.

**Phase 2 (enrichment pass):** a scheduled job runs weekly. For each item with `github_issue_url IS NULL`, it runs a fuzzy match against open issues in `dcetlin/lobster` and `angellist/eng` (if accessible). On a confident match (threshold TBD), it populates the field. The job does not overwrite user-set URLs.

Extraction-time population: when the LOS journal sweep finds text like `(#113)` or `angellist/eng#4521` adjacent to an item, it captures it immediately.

---

## 8. Implementation Notes

### What Was Built in This Pass

1. Schema migration: added `workstream`, `project`, `action_type`, `github_issue_url`, `archived_at`, `last_activity_at` columns to `action_items`.
2. Initial canonicalization: items from journals 117–118 (April 29 – May 4, 2026) inserted into DB; items from TODO-Aggregated.md that were missing from DB added; workstream tags applied to all 50 existing items.
3. ACTIVE TODOS.md regenerated from DB and committed to vault.
4. Crypto/finance items (BTC cold storage, Lightning node, Nostr) inserted — they were in TODO-Aggregated but missing from DB.

### What Is Not Yet Built

- The bidirectional sync script (reads ACTIVE TODOS.md back into DB on vault push)
- GitHub issue enrichment pass (Phase 2)
- Telegram "add to list" handler
- Archival automation in LOS nightly sweep

These are Tier 1/Tier 2 work from the personal-todo-system workstream design.
