# Obsidian Active Todos — Canonical UX Enumeration

Version: 1.0
Last updated: 2026-05-11
Scope: All user-facing actions on `✅ ACTIVE TODOS.md` (vault-watcher Tier 1 live)

Symbol key:
  ✓ LIVE — fully implemented and running
  ◉ SCAFFOLDED — code exists, wired, but not end-to-end validated
  ○ PLANNED — designed or implied, not yet implemented

---

## 1. Overview

`✅ ACTIVE TODOS.md` is the primary LOS interface in Dan's Obsidian vault. It is a rendered, structure-preserving markdown file managed bidirectionally between Obsidian and the `self_action_items.db` SQLite DB on the VPS.

The pipeline is push-triggered (Tier 1): Dan saves in Obsidian → Obsidian git plugin pushes to remote → vault-watcher detects the new HEAD → vault-processor pulls, syncs, re-renders, commits, and pushes back.

The file is rendered from the DB on every sync cycle via `apply_status_delta()`, which only rewrites checkbox prefixes — all other structure (headers, subbullets, blank lines, prose) is preserved in-place. Items added via Telegram appear in a `<!-- lobster-additions -->` block at the bottom.

---

## 2. Actions Taxonomy

### 2a. Status Changes

**CHECK A TODO ITEM (mark done)**
- Trigger: Dan toggles `[ ]` → `[x]` in Obsidian
- Status: ✓ LIVE
- Lifecycle:
  1. Dan checks the box in Obsidian; Obsidian git plugin pushes
  2. vault-watcher detects new remote HEAD (within ~30s)
  3. After debounce (~60s), vault-processor fires
  4. `git pull --rebase --autostash` merges latest
  5. `sync_obsidian_to_db()` scans parsed items; `[x]` line → `mark_done()` in DB
  6. `apply_status_delta()` runs; DB says `done`, file already shows `[x]` → no change
  7. Commit + push (if any other changes)
- Failure modes:
  - Dan checks but Obsidian git plugin doesn't push → no sync fires; item stays open in DB
  - Dan unchecks before debounce fires → watcher will see latest state (unchecked); DB is unaffected
- Recovery: Dan can use `/todo done <id>` via Telegram at any time

**UNCHECK A TODO ITEM (reopen)**
- Trigger: Dan toggles `[x]` → `[ ]` in Obsidian
- Status: ✓ LIVE
- Lifecycle:
  1. Push detected; vault-processor fires
  2. `sync_obsidian_to_db()` sees item as open; if DB status is `done` → skipped (DB is authoritative for done)
  3. `apply_status_delta()`: DB says `done`, file shows `[ ]` → flips back to `[x]`
- Note: DB is authoritative for `done` status. Once marked done via DB (checkbox or Telegram), unchecking in Obsidian reverts on the next sync. To reopen a done item, use Telegram (`/todo` commands) or the Telegram callback buttons.
- Recovery path: Reopening done items is ○ PLANNED (no current Telegram command). The Tier 2b surfacing pass may address this.

**SNOOZE A TODO ITEM**
- Trigger: `/todo snooze <query> [days]` via Telegram; or Telegram inline button `todo-snooze-{id}-{date}`
- Status: ✓ LIVE
- Lifecycle:
  1. `mark_snoozed(conn, item_id, until_date)` sets `status='snoozed'`, `snoozed_until=YYYY-MM-DD`
  2. On next `apply_status_delta()`: snoozed items with `snoozed_until` in the future → treated as open for file rendering
  3. Snoozed items whose `snoozed_until` is past → treated as open again by `get_open_items()`
- Note: No snooze mechanism exists in Obsidian directly; Telegram-only today.

**DISMISS A TODO ITEM**
- Trigger: Telegram inline button `todo-dismiss-{id}`
- Status: ✓ LIVE
- Lifecycle:
  1. `mark_dismissed()` sets `status='dismissed'`, records `dismissed_at`
  2. Dismissed items are retained in DB (reviewable in weekly review)
  3. On next vault sync: absent from file → `sync_obsidian_to_db()` detects deletion of obsidian-sourced items only
- Note: Dismissed items from Telegram source are not subject to file-deletion detection (only `source='obsidian:ACTIVE TODOS.md'` items are).

---

### 2b. Content Changes

**ADD A NEW ITEM (inline in Obsidian)**
- Trigger: Dan types a new `- [ ] <text>` line anywhere in the file
- Status: ✓ LIVE
- Lifecycle:
  1. Push detected; vault-processor fires
  2. `sync_obsidian_to_db()` → `parse_active_todos()` sees new open item with no matching `dedup_key` in DB → `insert_action_item()` with `source='obsidian:ACTIVE TODOS.md'` and priority derived from section
  3. `apply_status_delta()` sees item is in DB (open) and in file (open) → no change to line
  4. On next full render: item appears with `<!-- id:N -->` HTML comment appended
- Priority assignment: determined by the section header the item falls under (Urgent P3, Active P5, Someday P8)
- Workstream assignment: determined by the `### <name>` subsection header directly above the item
- Note: New items without `<!-- id:N -->` comments are recognized by `dedup_key` (SHA-256 of normalized text). IDs are embedded on the next `apply_status_delta()` pass only if `render_active_todos()` is called (bootstrap path). In the delta path, IDs are not back-annotated — items are tracked by dedup_key.

**ADD A SUBTASK UNDER A PARENT ITEM**
- Trigger: Dan adds `  - [ ] <text>` (exactly 2-space indent) under an existing `- [ ]` parent
- Status: ✓ LIVE
- Lifecycle:
  1. `parse_active_todos()` uses `SUBTASK_CHECKBOX_RE` (2-space prefix); inherits `parent_id` from the most recent top-level `<!-- id:N -->` comment seen above
  2. `insert_action_item()` with `parent_id` set
  3. `apply_status_delta()` handles subtask lines the same as top-level (dedup_key lookup)
- Failure mode: If parent item has no `<!-- id:N -->` comment (newly added parent not yet synced), `parent_id` may be `None` on first pass. Resolved on subsequent sync once parent is in DB.
- Note: Depth cap is 2 levels (enforced in `handle_todo_add` for Telegram-sourced items; not enforced for Obsidian-sourced items in `sync_obsidian_to_db`).

**ADD A SUBTASK VIA TELEGRAM**
- Trigger: `/todo add <text> --parent <id>`
- Status: ✓ LIVE
- Lifecycle:
  1. `handle_todo_add()` validates parent exists and is not itself a subtask
  2. `insert_action_item()` with `parent_id` set and `source='telegram'`
  3. On next `apply_status_delta()`: subtask appears in `<!-- lobster-additions -->` block at bottom of file (○ PLANNED — subtask rendering in additions block not yet confirmed; parent items only are currently appended)

**MOVE OR REORDER AN ITEM**
- Trigger: Dan cuts/pastes or drags an item to a different position or section in Obsidian
- Status: ✓ LIVE (partial)
- Lifecycle:
  1. If item moves to a different section (e.g., Urgent → Someday), `sync_obsidian_to_db()` detects priority band change → `_update_priority()` called
  2. If item moves within the same section → no DB change; file order preserved
  3. `apply_status_delta()` writes the file back preserving the new order (only checkbox prefixes touched)
- Note: Position within a section is determined by file order, which Lobster preserves. Moving items between sections changes their priority band.

**EDIT AN ITEM'S TEXT**
- Trigger: Dan edits the text of a `- [ ] <old text>` line to `- [ ] <new text>`
- Status: ◉ SCAFFOLDED (partial)
- Lifecycle:
  1. `sync_obsidian_to_db()` computes `dedup_key` for new text; no match in DB → `insert_action_item()` as new item
  2. Old item (now absent from file) is detected by the deletion scan: `mark_deleted()` if `source='obsidian:ACTIVE TODOS.md'`
  3. Result: old item becomes `status='deleted'`, new text becomes a new row
- Failure mode: If the item was originally sourced from Telegram (not `obsidian:`), the deletion scan skips it — old item remains open in DB while new item is also inserted (duplicate risk). The dedup mechanism will not catch this because the text changed.
- Recovery: ○ PLANNED — no current UI to merge/reconcile these duplicates.

**DELETE AN ITEM (remove line from file)**
- Trigger: Dan deletes the `- [ ] <text>` line from the file entirely (without checking it)
- Status: ✓ LIVE
- Lifecycle:
  1. `sync_obsidian_to_db()` runs deletion scan: DB `open` items with `source='obsidian:ACTIVE TODOS.md'` whose `dedup_key` was not seen in this sync pass → `mark_deleted()` sets `status='deleted'`, records `deleted_at`
  2. Deleted items do not reappear in `apply_status_delta()` (not in `open/snoozed/done` set)
- Note: Only items originally sourced from `obsidian:ACTIVE TODOS.md` are subject to file-deletion detection. Telegram-sourced items removed from the file are NOT automatically deleted in DB — they reappear in the `<!-- lobster-additions -->` block on the next render pass.

**ADD SUBBULLETS OR NOTES UNDER A ITEM**
- Trigger: Dan adds non-checkbox lines (plain text, dash bullets without `[ ]`) under a todo
- Status: ✓ LIVE (structure preserved)
- Lifecycle:
  1. `apply_status_delta()` passes non-checkbox lines through unchanged
  2. `parse_active_todos()` ignores non-checkbox lines
  3. Subbullets are not synced to DB; they exist only in the file
- Note: Non-checkbox content is fully preserved across sync cycles. The delta approach explicitly avoids full-file rewriting for this reason.

---

### 2c. Annotation Protocol

**ADD AN @LOBSTER ANNOTATION**
- Trigger: Dan types `@lobster <command text>` anywhere in a vault `.md` file (or as a suffix on a todo line)
- Status: ✓ LIVE
- Lifecycle:
  1. vault-watcher detects push; vault-processor fires after debounce
  2. Step 5 of `run_processor()`: `_collect_files_to_scan()` returns files based on `annotation_scope` config (`all` = all `.md` files; `watched_only` = only watched files)
  3. `process_annotations_in_file()` calls `parse_lobster_annotations()` — any line containing `@lobster <text>` without a `dispatched_at:` marker is returned
  4. For each annotation:
     a. Line is marked in-place: `@lobster <text> <!-- dispatched_at: 2026-… -->`
     b. Inbox JSON is written to `~/messages/inbox/vault-lobster-annotation-*.json` with `type: "user_message"`, `source: "telegram"`, `chat_id: lobster_chat_id`, `text: <command_text>`
     c. Annotation is removed from the line (or whole line deleted if annotation was the entire line)
  5. Modified files are `git add`ed; committed and pushed with the sync commit
  6. Dispatcher picks up the inbox JSON on next poll iteration and processes it as a normal user message
- Deduplication: Each annotation gets a SHA-256 content-hash `message_id` (`vault-annotation-{sha}`). If the same annotation fires twice (retry scenario), the dispatcher's dedup gate prevents double-processing.
- Current state: Fully wired. The annotation reaches the Lobster inbox and is processed as a normal Telegram message. **No Telegram ping is sent back to Dan when an annotation is dispatched.** The annotation is silently consumed — Dan sees the effect (Lobster acts on the command) but receives no acknowledgement message unless the dispatched command itself produces a reply.

**@LOBSTER WITH SPECIFIC COMMANDS**
- All recognized Telegram commands work: `/todo add`, `/todo done`, natural-language task requests, questions, etc.
- The annotation text becomes the message `text` field verbatim, so `/todo add Fix the login flow` dispatched from a vault annotation is handled identically to Dan typing it in Telegram.
- Status: ✓ LIVE (annotation dispatch); ✓ LIVE (command routing via dispatcher)
- Gap: No special @lobster command vocabulary beyond what Telegram accepts. The annotation is not parsed for vault-specific semantics (e.g., "link this item to issue #123"). ○ PLANNED for Tier 2 extensions.

---

### 2d. Structural / System Actions

**CHECK THE DISABLE PROCESSING GUARD**
- Trigger: Dan checks `- [ ] 🔒 DISABLE PROCESSING` → `- [x] 🔒 DISABLE PROCESSING` in Obsidian
- Status: ✓ LIVE
- Lifecycle:
  1. Push detected; vault-processor fires
  2. Step 4 of `run_processor()`: `check_disable_processing_guard()` scans first 10 lines
  3. Guard found and checked (State 2) → processor skips all sync steps; writes Telegram alert: "DISABLE PROCESSING is active — skipping this sync cycle."
  4. `last_processed_head` is advanced (avoids infinite retry)
- Use case: Dan wants to make extensive edits in Obsidian without triggering mid-edit syncs.

**UNCHECK THE DISABLE PROCESSING GUARD (resume)**
- Trigger: Dan unchecks `- [x] 🔒 DISABLE PROCESSING` → `- [ ] 🔒 DISABLE PROCESSING`
- Status: ✓ LIVE
- Lifecycle:
  1. Push detected; next processor cycle sees guard unchecked (State 1) → proceeds normally
  2. Full sync runs; all changes since last processed HEAD are incorporated

**DELETE OR CORRUPT THE DISABLE PROCESSING GUARD**
- Trigger: Dan accidentally removes or garbles the guard line
- Status: ✓ LIVE (detection + alert)
- Lifecycle:
  1. `check_disable_processing_guard()` scans first 10 lines; no match → State 3 (guard absent)
  2. Processor skips all sync steps; writes Telegram alert: "DISABLE PROCESSING guard not found — processing paused. Restore the guard line (- [ ] 🔒 DISABLE PROCESSING) within the first 10 lines to resume."
  3. `last_processed_head` IS advanced (to avoid infinite alert loop on the same commit)
- Recovery: Dan restores the guard line manually in Obsidian and pushes.

**DELETE OR CORRUPT THE LOBSTER-ADDITIONS COMMENT TAGS**
- Trigger: Dan removes or edits `<!-- lobster-additions -->` or `<!-- /lobster-additions -->` markers at the bottom of the file
- Status: ✓ LIVE (graceful rebuild)
- Lifecycle:
  1. `apply_status_delta()` scans `out_lines` for `_LOBSTER_ADDITIONS_MARKER`
  2. If not found, `tail_start` is `None` — the additions block is simply appended fresh at the end
  3. If only the closing tag is missing, the block from the opening marker to EOF is replaced
- Recovery: Automatic on next sync — additions block is rebuilt from DB state.

**EDIT SECTION HEADERS OR STRUCTURE**
- Trigger: Dan renames, adds, or removes `## Urgent`, `## Active`, `## Someday` section headers, or `### <workstream>` subsection headers
- Status: ✓ LIVE (partial)
- Lifecycle:
  1. `parse_active_todos()` uses regex matching for section headers (`URGENT_HEADER_RE`, `ACTIVE_HEADER_RE`, `SOMEDAY_HEADER_RE`)
  2. If a header is renamed, items under it lose their section assignment → default to `active` priority band
  3. Workstream subsections (`### <name>`) are parsed by `WORKSTREAM_SECTION_RE` — renaming changes the workstream tag in DB on next sync
- Failure mode: Renaming headers to non-matching strings silently degrades priority assignment for all items in that section. No alert is sent.
- Recovery: Dan should use the canonical header text or contact Lobster to update the regex.

**ADD A TODO VIA TELEGRAM, SEE IT IN VAULT**
- Trigger: `/todo add <text>` via Telegram
- Status: ✓ LIVE
- Lifecycle:
  1. `handle_todo_add()` → `insert_action_item()` with `source='telegram'`
  2. On next vault sync (vault push or watcher debounce expiry), `apply_status_delta()` runs
  3. Pass 3 of `apply_status_delta()`: item is open in DB but not in file (dedup_key not in `file_dedup_keys`) → appended to `<!-- lobster-additions -->` block as `- [ ] <text> <!-- id:N -->`
  4. Vault-processor commits and pushes; Obsidian pulls on next sync
- Latency: Telegram-added item appears in vault after the next vault-push cycle (triggered by any vault push, or after the watcher's max_debounce_seconds if no push occurs).
- Note: Telegram items appear in the additions block, not in the priority sections. Dan can move them to the correct section manually; they will be recognized by dedup_key on the next sync and priority-updated accordingly.

---

## 3. Failure Modes and Recovery Paths

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Git conflict markers in file | `has_conflict_markers()` checks first 50 lines; processor halts + Telegram alert | Dan resolves the conflict in Obsidian; pushes; processor resumes |
| DISABLE PROCESSING guard missing | `check_disable_processing_guard()` → State 3; Telegram alert | Dan restores guard line in first 10 lines; pushes |
| DISABLE PROCESSING guard checked | State 2; Telegram alert | Dan unchecks the guard; pushes |
| Git pull fails (rebase failure) | `git_pull()` returns False; processor aborts; `last_processed_head` NOT advanced | Vault-watcher will retry on next debounce expiry; Dan may need to manually resolve conflict |
| Obsidian git plugin not pushing | vault-watcher never detects a new HEAD; no sync fires | Tier 0 cron (`todo_obsidian_sync.py`) fires every 30 min as fallback during validation window |
| Annotation dispatch fails (OSError) | `_dispatch_annotation()` returns False; line reverts to unprocessed state | Retried on next processor run (annotation still in file without `dispatched_at:` marker) |
| Item text edited in Obsidian | Old item becomes `status='deleted'`; new text inserted as new row | If item was Telegram-sourced: DB has two rows (old open + new open). No automated deduplication. ○ PLANNED |
| lobster-additions block corrupted | `apply_status_delta()` rebuilds block from scratch | Automatic |
| vault-processor crashes mid-run | Lock released on process exit; next watcher cycle fires a fresh run | Idempotent: all steps safe to repeat from the top |
| DB unreachable | `connect()` raises; vault-processor exits with error; Telegram alert not sent | VPS admin action required |

---

## 4. @lobster Annotation Protocol

### Full Lifecycle (Current State)

```
Dan writes: "do something @lobster remind me about X"
       |
       v (Dan saves in Obsidian, git plugin pushes)
       |
vault-watcher detects new remote HEAD (~30s polling)
       |
       v (after debounce, ~60s)
vault-processor.run_processor()
  Step 5: _collect_files_to_scan() → files per annotation_scope
       |
  parse_lobster_annotations():
    - Scans for lines matching /@lobster (.+)/i
    - Skips lines with "dispatched_at:" already present
       |
  process_annotations_in_file():
    For each annotation:
    1. Mark line: "@lobster X <!-- dispatched_at: 2026-… -->"
    2. Write ~/messages/inbox/vault-lobster-annotation-*.json
       { type: "user_message", source: "telegram",
         chat_id: lobster_chat_id, text: "X", ... }
    3. Remove @lobster from line (or delete whole line if annotation-only)
       |
  git add modified files
       |
  (later in run_processor) git commit + push
       |
       v
Lobster dispatcher polls inbox → picks up JSON
  → processes "X" as a normal user message
  → routes to commands / subagents / etc.
```

### Current Status

- Annotation parsing: ✓ LIVE
- Inbox dispatch: ✓ LIVE (writes `user_message` JSON to `~/messages/inbox/`)
- Deduplication gate: ✓ LIVE (SHA-256 content-hash message_id)
- Dispatcher routing of annotation-sourced messages: ✓ LIVE
- Telegram acknowledgement ping to Dan: NOT IMPLEMENTED
  - After dispatch, Dan receives no "I got your annotation" message. The annotation is silently consumed and the command is processed.
  - This is a gap: Dan cannot tell from the vault UI whether his annotation was dispatched. The `dispatched_at:` comment is written (briefly) and then removed, leaving no trace in the file.
- annotation_scope config: ✓ LIVE (`all` = all vault `.md` files; `watched_only` = only watched files list)
- lobster_chat_id config: ✓ LIVE (provisioned as 8075091586)

### Gap: No @lobster Dispatch Acknowledgement

When Dan writes `@lobster do X` in a vault file, the annotation is dispatched and processed, but:
1. No Telegram message is sent saying "Received: do X from vault/file.md"
2. The dispatched_at comment is written and then removed (invisible to Dan in the final file)
3. Dan must infer dispatch happened from the annotation line disappearing on next pull

Planned mitigation (○ PLANNED, Tier 2a): The Obsidian plugin will show an inline status decoration (`dispatched / open / done`) on `@lobster` lines, giving Dan visible feedback without requiring a Telegram ping.

### Planned @lobster Extensions (○ PLANNED)

- Structured commands beyond raw Telegram text (e.g., `@lobster todo: X`, `@lobster snooze: X`)
- Vault-specific semantics (e.g., `@lobster link to #github-issue`)
- Reply threading: annotation results sent back to the line's context in Obsidian (Tier 3)

---

## 5. Lifecycle Diagram

```
Dan edits ACTIVE TODOS.md in Obsidian
         |
         | (Obsidian git plugin auto-commit + push)
         v
 obsidian-vault GitHub remote (origin)
         |
         | (~30s polling: git fetch + rev-parse origin/HEAD)
         v
  vault-watcher.py (Type B cron, every 30s)
    - Compares remote HEAD to last_known_head
    - If changed: records last_push_at, starts debounce timer
    - If debounce expired (default 60s) OR max_debounce exceeded (default 300s):
         |
         v
  vault-processor.py (invoked synchronously by watcher)
    1. git pull --rebase --autostash
    2. Read ACTIVE TODOS.md
    3. Check for conflict markers (first 50 lines)
    4. Check DISABLE PROCESSING guard (first 10 lines)
    5. Scan vault files for @lobster annotations → dispatch to inbox
    6. git add annotation-modified files
    7. sync_obsidian_to_db(): parse + DB sync
    8. apply_status_delta(): rewrite checkbox prefixes + append new items
    9. git add ACTIVE TODOS.md
   10. git commit + push ("vault-watcher: sync [timestamp]")
         |
         v
 self_action_items.db (updated)
         |
         +---> Lobster dispatcher inbox (if @lobster annotations dispatched)
         |          |
         |          v
         |     Lobster processes annotation as user_message
         |
         v
 obsidian-vault GitHub remote (new commit pushed)
         |
         | (Obsidian git plugin auto-pull on next sync interval)
         v
 Dan's Obsidian app shows updated ACTIVE TODOS.md
```

---

## 6. Open / Future

### Currently Open (○ PLANNED)

- `/todos` command: display open items with inline buttons (done/snooze/dismiss) — LIVE per PR #1097 but not documented here (dispatcher-level, not vault-level)
- No acknowledgement ping for @lobster annotation dispatch (Tier 2a Obsidian plugin will provide inline decorations instead)
- Reopen a done item: no Telegram command today; requires direct DB manipulation
- Edit text of an existing item: creates duplicate in DB; no merge/reconcile UI
- Subtask rendering in lobster-additions block: needs verification
- Tier 2a Obsidian plugin (Mac): hotkey capture, quick-add modal, @lobster inline decorations, DISABLE PROCESSING ribbon toggle, status bar
- Tier 2b surfacing: idle nudges for items open >72h, today-view panel
- GitHub webhook mode: replace git-fetch polling with push webhook (~1s vs. ~30s detection)
- Retire `todo_obsidian_sync.py` cron: scheduled for 2026-05-13 after 48h validation

### Explicitly Excluded

- Signal channel (behavioral metadata): implicit observation (dwell time, return frequency, scroll depth) — excluded by design, not deferred
- Full file rewrite on every sync: apply_status_delta() is the architecture; render_active_todos() is bootstrap-only
