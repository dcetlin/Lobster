# WOS Data Access — Canonical Subagent Guide

**Audience:** Subagents (engineer agents, scheduled jobs, oracle agents) that need to read WOS state.
**Related docs:** `docs/wos-registry-reference.md` (status machine, field reference), `docs/wos-golden-pattern.md` (Python patterns)

---

## The Rule

**Never use raw `sqlite3` to query `registry.db` directly.** The schema evolves, column names change, and JSON fields require deserialization. Going around the access layer produces bugs that are invisible until production.

Use exactly one of the two access paths described below.

---

## Path 1: CLI queries (subagents, shell scripts, ad-hoc inspection)

`registry_cli.py` is the canonical entry point for all read queries. All output is JSON on stdout.

### Standard invocation pattern

```bash
# From any working directory — the CLI resolves the DB path via env/defaults
uv run /home/lobster/lobster/src/orchestration/registry_cli.py <subcommand> [args]
```

Or, in a subagent context using `REGISTRY_DB_PATH` to pin the DB:

```bash
REGISTRY_DB_PATH=~/lobster-workspace/orchestration/registry.db \
  uv run /home/lobster/lobster/src/orchestration/registry_cli.py status-breakdown
```

### Available read subcommands

| Subcommand | What it returns | When to use |
|---|---|---|
| `list` | All UoWs, or filtered by `--status` | Browse the full queue |
| `list --status <s>` | UoWs in a specific status | Get active, blocked, etc. |
| `get --id <uow-id>` | Single UoW object | Inspect one UoW by ID |
| `status-breakdown` | `{status: count}` — all statuses with counts | Dashboard, health checks |
| `escalation-candidates` | UoWs in `needs-human-review` | Operator triage, alerts |
| `stale [--buffer-seconds N]` | In-flight UoWs with silent heartbeats | Observation loop, health checks |
| `check-stale` | Active UoWs whose GitHub issue is closed | Source integrity checks |
| `gate-readiness` | Registry health metrics | WOS autonomy gate |

### Common query patterns

```bash
# How many UoWs are in each status?
uv run registry_cli.py status-breakdown
# → {"proposed": 2, "ready-for-steward": 1, "done": 14, ...}

# What UoWs need my attention?
uv run registry_cli.py escalation-candidates
# → [{id, status: "needs-human-review", summary, retry_count, ...}, ...]

# Are any executing UoWs stalled?
uv run registry_cli.py stale
# → [{id, status: "active", heartbeat_at, heartbeat_ttl, ...}, ...]

# What is in the steward queue right now?
uv run registry_cli.py list --status ready-for-steward

# Get the full record for a specific UoW
uv run registry_cli.py get --id uow_20260424_abc123
```

---

## Path 2: Programmatic Python (agents that process UoW objects)

Use the `Registry` class from `src/orchestration/registry.py`. This is the sole ORM — it handles connection management, WAL mode, JSON deserialization, and schema migrations.

### Standard import pattern

```python
import sys
from pathlib import Path

# Add src/ to the path — use the absolute path, not relative
_SRC_DIR = Path("/home/lobster/lobster/src")
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from orchestration.registry import Registry, UoWStatus

# Registry resolves the DB path automatically (via REGISTRY_DB_PATH env or
# paths.REGISTRY_DB canonical default). Pass no argument for normal operation.
registry = Registry()
```

### Common programmatic patterns

```python
# Get all UoWs in a specific status
uows = registry.list(status="ready-for-steward")

# Get one UoW by ID
uow = registry.get("uow_20260424_abc123")
if uow is None:
    # Not found
    ...

# Count by status (returns {status: count} dict)
# Prefer registry_cli.py status-breakdown for this — it's simpler from shell
# From Python, build the breakdown yourself via list():
from collections import Counter
all_uows = registry.list()
breakdown = Counter(u.status for u in all_uows)

# Find escalation candidates
escalated = registry.list(status="needs-human-review")

# Find stale in-flight UoWs (uses heartbeat fields, migration 0009+)
stale = registry.get_stale_heartbeat_uows(buffer_seconds=30)
```

### What you get back

`registry.list()` and `registry.get()` return typed `UoW` dataclass objects:

```python
uow.id             # str — "uow_20260424_abc123"
uow.status         # UoWStatus enum — use str(uow.status) for serialization
uow.summary        # str — issue title / task description
uow.source_issue_number  # int | None
uow.steward_cycles # int — how many steward cycles this UoW has consumed
uow.retry_count    # int — escalation retry count
uow.heartbeat_at   # str | None — ISO timestamp of last heartbeat
uow.heartbeat_ttl  # int — seconds before heartbeat silence triggers stall
uow.artifacts      # list | None — typed outcome refs [{type, ref, category}]
```

Fields are documented in `src/orchestration/registry.py` (the `UoW` dataclass).

---

## What NOT to do

### Never use raw sqlite3

```python
# BAD — bypasses deserialization, breaks on schema changes
import sqlite3
conn = sqlite3.connect("/home/lobster/lobster-workspace/orchestration/registry.db")
rows = conn.execute("SELECT * FROM uow_registry WHERE status = 'active'").fetchall()
```

### Never use sys.path tricks to import from an unknown location

```python
# BAD — fragile, depends on repo layout staying fixed at a specific path
sys.path.insert(0, '/home/lobster/lobster')  # absolute but brittle
from src.orchestration.uow_registry import UoWRegistry  # wrong class name too
```

### Never hardcode the DB path in queries

```python
# BAD — breaks in test environments and alternate workspaces
conn = sqlite3.connect("/home/lobster/lobster-workspace/orchestration/registry.db")
```

---

## Environment variables

| Variable | Purpose |
|---|---|
| `REGISTRY_DB_PATH` | Override the DB path (used in tests; not needed in production) |
| `LOBSTER_WORKSPACE` | Workspace root — the canonical DB lives at `$LOBSTER_WORKSPACE/orchestration/registry.db` |

Both are honored by `Registry()` and by `registry_cli.py`.

---

## Troubleshooting

**"file is not a database" or OperationalError on connect:** The path is wrong. Print `registry.db_path` to see what the Registry resolved.

**Missing columns / AttributeError on UoW fields:** The DB needs a migration. Run `uv run registry_cli.py gate-readiness` — the Registry constructor runs migrations automatically on init.

**Empty results when records should exist:** Check `REGISTRY_DB_PATH` is not accidentally set to a test DB.
