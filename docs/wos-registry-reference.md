# WOS Registry Reference

**Audience:** Agents and operators working with the Work Orchestration System.  
**Source of truth:** `src/orchestration/registry.py`, `src/orchestration/registry_cli.py`

---

## Registry Location

The registry is a SQLite database at:

```
~/lobster-workspace/orchestration/registry.db
```

Managed exclusively by the `Registry` class in `src/orchestration/registry.py`.
All writes use `BEGIN IMMEDIATE` transactions with an atomic audit log entry.

**Do not write to:** `data/wos/`, `data/`, or any path outside `orchestration/`.

The path can be overridden with the `REGISTRY_DB_PATH` environment variable
(used in tests; not needed in normal operation).

---

## UoW Status State Machine

### All Statuses

| Status | Terminal? | Notes |
|---|---|---|
| `proposed` | No | Newly cultivated, awaiting approval |
| `pending` | No | Transitional only — passed through atomically on approve |
| `ready-for-steward` | No | Approved and queued for Steward |
| `ready-for-executor` | No | Steward has prescribed work; queued for Executor |
| `active` | No | Executor is currently working this UoW |
| `diagnosing` | No | Steward is diagnosing a return |
| `blocked` | No | Steward surfaced a blocker; awaiting user `decide` action |
| `done` | **Yes** | Steward declared loop closed with `close_reason` |
| `failed` | **Yes** | Terminated as failed (system or user-initiated) |
| `expired` | **Yes** | Proposed UoW expired before approval (14-day timeout) |

### Terminal vs. Non-Terminal

`is_terminal()` returns `True` for `done`, `failed`, and `expired`.

**Terminal statuses allow re-injection:** `upsert` will create a new `proposed`
record for the same issue if the existing record is terminal.

**Non-terminal statuses block re-injection:** `upsert` returns `UpsertSkipped`
if any non-terminal record exists for the issue. The existing record must be
transitioned to a terminal status first.

Note: there is no `cancelled` status. The closest equivalent is `failed`
(user-initiated via `decide-close`).

---

## Status Flow

```
                    ┌─ expired (terminal)
                    │
proposed ──approve──> [pending] ──> ready-for-steward ──> ready-for-executor
                                           │                       │
                                        blocked               active / diagnosing
                                           │                       │
                                    decide-retry ◄────────────────-┘
                                    decide-close ──> failed (terminal)
                                                         │
                                                        done (terminal)
                                                     (via Steward close)
```

Simplified linear path:

```
proposed → [pending] → ready-for-steward → ready-for-executor → active → done
                                │                                    └──> failed
                             blocked ──> (retry or close)            └──> expired
```

`pending` is no longer a resting state. The `approve` command transitions
`proposed → pending → ready-for-steward` atomically in a single transaction.

---

## Injection Pattern

Use this pattern to inject a UoW when a blocking non-terminal record may exist.

### 1. Check for blocking records

```bash
uv run src/orchestration/registry_cli.py list --status proposed
uv run src/orchestration/registry_cli.py list --status active
uv run src/orchestration/registry_cli.py list --status blocked
```

Check all non-terminal statuses if you are unsure: `proposed`, `pending`,
`active`, `blocked`.

Note: `ready-for-steward`, `ready-for-executor`, and `diagnosing` are valid
internal statuses but are not accepted as `--status` filter values by the CLI.
To inspect records in those states, omit `--status` to list all records and
filter by eye, or query the registry database directly.

### 2. Clear blocking records (if needed)

If a record is `blocked`, use the decide commands:

```bash
# Reset to ready-for-steward (full fresh start, resets steward_cycles):
uv run src/orchestration/registry_cli.py decide-retry --id <uow-id>

# Close as failed (user-requested termination):
uv run src/orchestration/registry_cli.py decide-close --id <uow-id>
```

For other non-terminal statuses that are stuck, use `set_status_direct` from
Python (not exposed as a CLI command) or wait for the natural state machine.

### 3. Inject

```bash
uv run src/orchestration/registry_cli.py upsert \
  --issue <N> \
  --title "<Issue title>"
```

With success criteria from the issue body:

```bash
uv run src/orchestration/registry_cli.py upsert \
  --issue <N> \
  --title "<Issue title>" \
  --issue-body "<full issue body text>"
```

A successful insert returns `{"action": "inserted", "id": "<uow-id>"}`.  
A skipped insert returns `{"action": "skipped", "reason": "..."}` — inspect
the reason to find which existing record is blocking.

### 4. Approve

```bash
uv run src/orchestration/registry_cli.py approve --id <uow-id>
```

Transitions `proposed → pending → ready-for-steward` atomically.

### 5. Verify

```bash
# List all pending/active records to confirm the UoW is visible:
uv run src/orchestration/registry_cli.py list --status pending
uv run src/orchestration/registry_cli.py list --status active
# Or list all and locate the new UoW by its ID:
uv run src/orchestration/registry_cli.py get --id <uow-id>
```

---

## Upsert Decision Table

The `upsert` method evaluates these rules before any write:

| Condition | Result |
|---|---|
| No existing non-terminal record for this issue | INSERT new `proposed` record |
| Existing `proposed` record (any sweep date) | SKIP |
| Existing `pending` / `active` / `blocked` / `ready-*` / `diagnosing` | SKIP |
| Existing `done` / `failed` / `expired` record | INSERT new `proposed` record |
| Same `(issue, sweep_date)` UNIQUE conflict, existing is `proposed` | UPDATE fields in place |
| Same `(issue, sweep_date)` UNIQUE conflict, existing is non-proposed | No-op |

---

## CLI Reference

All commands are run from the lobster repo root with `uv run`.

```
uv run src/orchestration/registry_cli.py <command> [args]
```

| Command | Description |
|---|---|
| `upsert --issue N --title T [--sweep-date D] [--issue-body B]` | Propose a UoW for issue N |
| `get --id ID` | Get a single UoW by ID |
| `list [--status S]` | List UoWs, optionally filtered by status |
| `approve --id ID` | Approve proposed UoW (proposed → ready-for-steward) |
| `decide-retry --id ID` | Reset blocked UoW for a new Steward cycle |
| `decide-close --id ID` | Close blocked UoW as user-requested failure |
| `check-stale` | Report active UoWs whose source GitHub issue is closed |
| `expire-proposals` | Expire proposed records older than 14 days |
| `gate-readiness` | Check WOS autonomy gate metric |

All commands output JSON to stdout.

Valid `--status` filter values: `proposed`, `pending`, `active`, `blocked`,
`done`, `failed`, `expired`.

---

## Advanced Operations

### Retry a blocked UoW without resetting the steward cycle count

`decide-retry` (CLI) resets the `steward_cycles` counter to zero as part of
its full-fresh-start semantics. If you want to retry a blocked UoW while
preserving its accumulated cycle count, call `Registry.decide_proceed(uow_id)`
directly in Python — no CLI equivalent exists:

```python
from src.orchestration.registry import Registry
reg = Registry()
reg.decide_proceed("<uow-id>")
```

This transitions the UoW from `blocked` back to `active` without touching
`steward_cycles`.

---

## Common Mistakes

**Wrong path:** Agents sometimes write UoW artifacts to `data/wos/` or `data/`.
The registry is only at `orchestration/registry.db`. There is no `data/wos/`
directory.

**Expecting `cancelled` to be terminal:** There is no `cancelled` status.
Non-terminal records that need to be cleared should be transitioned to `failed`
via `decide-close` (if `blocked`) or by other explicit state transitions.

**Assuming `approve` leaves UoW in `pending`:** Since V3, `approve` is a
single-transaction operation that takes a UoW from `proposed` all the way to
`ready-for-steward`. The `pending` status appears in the audit log as a
transition record but is not a resting state you will observe in `list` output.
