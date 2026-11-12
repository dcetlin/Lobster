# Meta-Thread Dispatcher Integration

This document describes how to wire `scripts/meta_threads.py` into the
dispatcher's message handling loop.

## What the integration does

Before the dispatcher processes each Telegram message, it calls
`meta_threads.search(message_text)`.  If any active meta-threads score above
the threshold (default 0.7 cosine similarity), their state is formatted and
prepended to the dispatcher's system context for that message only.

The match is against `inquiry_embedding` (the open question the thread is
tracking), not the category centroid.  This means a message about "the
dispatcher getting stuck" matches a thread asking "How should Lobster handle
split-brain scenarios?" even if the exact words don't overlap.

## Integration point

The integration belongs in the message processing path, just before the model
is invoked.  In the current dispatcher (`inbox_server.py` or equivalent), look
for the point where `system_context` is assembled for a given message.

### Minimal wiring (pseudocode)

```python
import sys
sys.path.insert(0, "/home/lobster/lobster/scripts")
import meta_threads

# --- inside the message handler, before calling the model ---
message_text = message.get("text", "")
if message_text:
    relevant = meta_threads.search(message_text, threshold=0.7)
    if relevant:
        thread_context = meta_threads.inject_context(relevant)
        system_context = thread_context + "\n\n" + system_context
```

### Performance

The search call completes in <100ms for typical thread counts because:
- Embeddings are cached in the process (`_embed_cache` dict, keyed by text)
- Similarity check is pure Python math over 384-dim vectors
- Storage is JSON files, not a database query

The first call for a given message text triggers fastembed model inference
(~50ms).  Subsequent calls for the same text are instant.

### Thread management

Create threads:
```bash
uv run ~/lobster/scripts/meta_threads.py list
```

Bootstrap from history (one-time, run after install):
```bash
uv run ~/lobster/scripts/meta_threads.py bootstrap --since-days 90
```

Update a thread's open question (recomputes inquiry embedding atomically):
```bash
uv run ~/lobster/scripts/meta_threads.py update <thread_id> \
  --question "What is the real root cause?"
```

Add an observation to a thread:
```bash
uv run ~/lobster/scripts/meta_threads.py update <thread_id> \
  --observation "The issue occurred again after the 2.3 deploy"
```

## What is NOT wired automatically

- The dispatcher does not currently call `search()` — this must be added
  manually to `inbox_server.py` at the system context assembly point.
- Meta-threads are not updated automatically when the dispatcher processes
  a message.  If you want threads to evolve from conversation, add a call to
  `meta_threads.update(thread_id, new_observation=summary)` after each
  relevant exchange.
- The bootstrap job is not scheduled.  Run it once manually, then add a
  weekly scheduled job if desired.

## File locations

| Purpose | Path |
|---------|------|
| Meta-thread storage | `~/lobster-user-config/memory/meta-threads/*.json` |
| Category storage | `~/lobster-user-config/memory/categories/*.json` |
| Scripts (committed) | `~/lobster/scripts/meta_threads.py`, `categorization.py` |
