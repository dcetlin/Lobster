# Council Sunday Sweep

Autonomous council sweep. Runs weekly on Sunday. Processes any unprocessed notes in the ergonomics frontier and any topics in the pending queue.

## Steps

1. Read `~/lobster-workspace/workstreams/agent-council/council-state.json` for:
   - `last_deliberation_at` — timestamp of last run
   - `notes_processed_count` — total notes processed so far
   - `pending_queue` — topics deferred from previous runs

2. List files in `~/lobster-workspace/workstreams/ergonomics-orient/notes/` (create the directory if it does not exist).
   - Identify notes newer than `last_deliberation_at` (or all notes if `last_deliberation_at` is null)
   - These are unprocessed notes to deliberate on

3. For each unprocessed note:
   - Read the note
   - Run a council deliberation inline (condensed — you are acting as all three roles):
     - Researcher: what does the note claim? What does the frontier doc say about this?
     - Synthesizer: what is the strongest structural synthesis? What zone does it belong to?
     - Canon-Keeper: is it specific enough and grounded enough to commit?
   - If COMMITTED: write the canon entry to `~/lobster-workspace/workstreams/agent-council/canon/[zone]/[slug].md`
     and update `~/lobster-workspace/workstreams/agent-council/canon/index.md`
   - If DEFERRED: add to pending_queue in council-state.json with a note

4. For each item in `pending_queue`, attempt to resolve it against current material. Remove from queue if committed; update note if still insufficient.

5. Update `council-state.json`:
   - Set `last_deliberation_at` to now
   - Reset `notes_since_last_run` to 0
   - Append a run summary to `runs`

6. If any entries were committed:
   - Call `write_result` with:
     - `task_id`: "council-sunday-sweep"
     - `chat_id`: ADMIN_CHAT_ID (read from `LOBSTER_ADMIN_CHAT_ID` env var)
     - `text`: "Council committed [N] new canon entries from [N] notes. Latest: [committed claim in one sentence]."
     - `sent_reply_to_user`: False
     - `status`: "success"

7. If nothing was committed (no notes, or all deferred):
   - Call `write_result` with:
     - `task_id`: "council-sunday-sweep"
     - `chat_id`: 0
     - `text`: "no action taken — no committable entries from [N] notes reviewed"
     - `sent_reply_to_user`: False
     - `status`: "success"
   - (The dispatcher will silent-drop this result — no notification to Dan)

## Domain context paths

- Frontier doc: `~/lobster-workspace/workstreams/ergonomics-orient/frontier.md`
- Notes directory: `~/lobster-workspace/workstreams/ergonomics-orient/notes/`
- Canon path: `~/lobster-workspace/workstreams/agent-council/canon/`
- Council state: `~/lobster-workspace/workstreams/agent-council/council-state.json`
