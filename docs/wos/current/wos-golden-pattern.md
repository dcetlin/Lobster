# WOS Python Golden Pattern

Reference sketch for new WOS implementation code. When in doubt about how to model a piece of the Work Orchestration System in Python, this document is the baseline.

**Related docs:** [wos-constitution.md](wos-constitution.md) — the founding metaphor and naming constraints that govern all WOS design decisions | [wos-v2-design.md](wos-v2-design.md) — the full WOS v2 specification

---

## The Pattern

```python
from enum import StrEnum
from dataclasses import dataclass, field
from typing import Protocol
from datetime import datetime

class UoWStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    READY_FOR_EXECUTOR = "ready-for-executor"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"

    def is_terminal(self) -> bool:
        return self in {UoWStatus.DONE, UoWStatus.FAILED, UoWStatus.EXPIRED}

    def is_in_flight(self) -> bool:
        return self in {UoWStatus.ACTIVE, UoWStatus.READY_FOR_EXECUTOR}

@dataclass(frozen=True, slots=True)
class UoW:
    id: str
    title: str
    status: UoWStatus
    vision_ref: str
    created_at: datetime
    updated_at: datetime
    steward_notes: str = ""
    success_criteria: str = ""

@dataclass(frozen=True, slots=True)
class GateStatus:
    met: bool
    days_running: int
    approval_rate: float
    reason: str

@dataclass(frozen=True)
class StewardConfirmed:
    uow: UoW

@dataclass(frozen=True)
class TransitionSkipped:
    reason: str

@dataclass(frozen=True)
class UoWNotFound:
    uow_id: str

StewardDecision = StewardConfirmed | TransitionSkipped | UoWNotFound

def approve(uow_id: str, registry) -> StewardDecision:
    uow = registry.get(uow_id)
    if uow is None:
        return UoWNotFound(uow_id=uow_id)
    match uow.status:
        case UoWStatus.PROPOSED:
            updated = dataclasses.replace(uow, status=UoWStatus.CONFIRMED)
            registry.save(updated)
            return StewardConfirmed(uow=updated)
        case _:
            return TransitionSkipped(reason=f"Cannot confirm UoW in status {uow.status!r}")

class IssueChecker(Protocol):
    def __call__(self, issue_number: int) -> bool: ...
```

---

## What each piece is for

### `UoWStatus(StrEnum)` — logic lives on the type

Status values are not bare strings or frozensets. `UoWStatus` is a `StrEnum` so it serializes cleanly to/from SQLite as a string, but status-related logic (`is_terminal`, `is_in_flight`) is a method on the type — not a module-level frozenset comparison at call site.

**Current code uses:**
```python
_NON_TERMINAL_STATUSES = frozenset({"proposed", "pending", "active", "blocked"})
_TERMINAL_STATUSES = frozenset({"done", "failed", "expired"})
```

**Golden pattern replaces this with:**
```python
uow.status.is_terminal()    # True for DONE, FAILED, EXPIRED
uow.status.is_in_flight()   # True for ACTIVE, READY_FOR_EXECUTOR
```

The frozenset comparison still works, but it scatters terminal/in-flight logic across callers. When the status enum grows, the method is the single update point.

### `UoW` dataclass — typed, frozen, slotted

`_row_to_dict` returns `dict[str, Any]`. Every caller must know the key names and types by convention. The golden pattern replaces this with a typed, frozen dataclass: fields are named, typed, and immutable by construction. `frozen=True` means you must use `dataclasses.replace()` to produce a modified copy — preventing accidental mutation of registry state.

### `GateStatus` — named return instead of `dict[str, Any]`

`gate_readiness()` currently returns `{"gate_met": True, "days_running": N, ...}`. The golden pattern replaces this with `GateStatus(met=bool, days_running=int, approval_rate=float, reason=str)`. The field names are enforced by the type, not by string key conventions in callers.

This is especially important for the rename described in issue #330: once `gate_readiness()` becomes `registry_health()`, the return type should also be named and stable.

### Named result types — no `dict` returns from decision functions

`approve()` (and any function that makes a transition decision) returns a union type: `StewardConfirmed | TransitionSkipped | UoWNotFound`. The caller is forced by the type checker to handle all three cases. Compare to returning `{"action": "skipped", "reason": "..."}` — where the caller can ignore the `action` field entirely with no type error.

### `match/case` on typed values — not `if/elif` on status strings

```python
# Avoid:
if uow["status"] == "proposed":
    ...
elif uow["status"] == "pending":
    ...

# Prefer:
match uow.status:
    case UoWStatus.PROPOSED:
        ...
    case UoWStatus.PENDING:
        ...
    case _:
        ...
```

The `match` variant is exhaustiveness-checkable by pyright/mypy. The `if/elif` variant silently falls through on unhandled statuses.

### `Protocol` for injectable dependencies

`IssueChecker` is a `Protocol` — a structural interface. Any callable with the right signature satisfies it. The production `gh`-backed implementation and the test mock both satisfy the same protocol without inheriting from a base class. This is the same pattern `conditions.py` uses for `GithubClient = Callable[[int], dict[str, Any]]` — the golden pattern just makes it explicit as a named `Protocol` when the callable has a non-trivial role.

---

## Key deltas from current code

| Current code | Golden pattern |
|---|---|
| `frozenset({"proposed", ...})` comparisons at call site | `UoWStatus.is_terminal()` / `is_in_flight()` methods |
| `_row_to_dict` → `dict[str, Any]` | `_row_to_uow` → `UoW` dataclass |
| `gate_readiness()` → `dict[str, Any]` | `registry_health()` → `GateStatus` dataclass |
| `if/elif` on status strings | `match/case` on `UoWStatus` values |
| `{"action": "skipped", ...}` return dicts | `TransitionSkipped(reason=...)` result types |
| `confirm()` function name | `approve()` (matches domain language) |

---

## Applying this to new code

When implementing Steward, Executor, or Observation Loop (issues #303, #305, #306, #307):

1. Accept `UoW` objects, not raw `dict[str, Any]` rows, wherever possible
2. Use `uow.status.is_terminal()` instead of `uow["status"] in {...}`
3. Return named result types from decision functions
4. Use `match/case` on `UoWStatus` enum values for state dispatch
5. Return `GateStatus` (or a similarly named dataclass) from health-check functions, not bare dicts

The existing `conditions.py` `evaluate_condition` function is a good example of the injectable-dependency pattern already in use — the `GithubClient` callable type alias is the right instinct; formalizing it as a `Protocol` when the callable grows is the natural next step.
