"""
Shard-stream parallel dispatch logic for WOS.

This module contains the pure functions that decide whether a candidate UoW
can safely be dispatched in parallel with other in-flight UoWs.

Design
------
The serial constraint on WOS execution is actually a *file-scope conflict*
constraint: two UoWs that touch the same files cannot run concurrently, but
two UoWs that touch different parts of the codebase can.

The dispatch gate checks three conditions before allowing parallel dispatch:

1. **max_parallel cap** — if the number of in-flight UoWs is already at or
   above max_parallel (read from wos-config.json, default 2), no new dispatch.

2. **shard serialization** — if any in-flight UoW shares the candidate's
   shard_id (non-null), the candidate is blocked until that shard is free.

3. **file_scope overlap** — if the candidate's file_scope overlaps with any
   in-flight UoW's file_scope, the candidate is blocked. UoWs with null
   file_scope are treated as **independent**: they do not conflict with each
   other or with annotated UoWs. A null file_scope means "no explicit scope
   annotation" — the UoW is allowed to proceed unless a shard_id constraint
   or explicit scope overlap fires.

   Prior behavior (removed in issue #912): null file_scope UoWs were treated
   as exclusive — a single in-flight null-scope UoW blocked ALL other
   null-scope candidates. This was over-conservative: UoWs without scope
   annotations are logically independent code changes on distinct files.

All functions are pure: they take explicit inputs and return typed results.
No DB connections, no side effects. Registry queries happen at the call site
(in steward.py) and the results are passed in as plain lists.

Named constants
---------------
- DEFAULT_MAX_PARALLEL: fallback when wos-config.json is absent or missing the key
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence


class PathSelection(StrEnum):
    FAST = "fast"
    THOROUGH = "thorough"

# Default cap: two non-overlapping UoWs may run in parallel before any
# Attunement evidence exists at higher concurrency.
DEFAULT_MAX_PARALLEL: int = 2


# ---------------------------------------------------------------------------
# Result type for dispatch eligibility checks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchAllowed:
    """Candidate may be dispatched: all gates passed."""
    reason: str = "shard-stream: all gates passed"


@dataclass(frozen=True)
class DispatchBlocked:
    """Candidate must not be dispatched this cycle."""
    reason: str


ShardDispatchDecision = DispatchAllowed | DispatchBlocked


# ---------------------------------------------------------------------------
# Pure helper: file scope overlap detection
# ---------------------------------------------------------------------------

def _scopes_overlap(scope_a: list[str], scope_b: list[str]) -> bool:
    """Return True if any path in scope_a is a prefix of (or equal to) any
    path in scope_b, or vice versa.

    We use prefix matching so that a directory entry like "src/orchestration/"
    covers any file under it (e.g. "src/orchestration/steward.py").

    Both lists must be non-empty strings. Trailing slashes are normalized away
    before comparison so "src/foo/" and "src/foo" are treated identically.

    Pure function: no IO, no side effects.
    """
    def _normalize(path: str) -> str:
        return path.rstrip("/")

    normalized_a = [_normalize(p) for p in scope_a]
    normalized_b = [_normalize(p) for p in scope_b]

    for path_a in normalized_a:
        for path_b in normalized_b:
            if path_a == path_b:
                return True
            # Prefix check: "src/orchestration" overlaps "src/orchestration/steward.py"
            if path_b.startswith(path_a + "/") or path_a.startswith(path_b + "/"):
                return True
    return False


# ---------------------------------------------------------------------------
# Core dispatch eligibility gate
# ---------------------------------------------------------------------------

def check_shard_dispatch_eligibility(
    candidate_file_scope: list[str] | None,
    candidate_shard_id: str | None,
    executing_uows: Sequence,  # sequence of UoW objects (duck typed)
    max_parallel: int = DEFAULT_MAX_PARALLEL,
) -> ShardDispatchDecision:
    """Determine whether a candidate UoW can be dispatched given in-flight UoWs.

    This is the authoritative gate for shard-stream parallel dispatch. It must
    be called by the steward before moving a UoW to ready-for-executor.

    The gate evaluates three conditions in order (first failure wins):

    1. max_parallel cap
       If len(executing_uows) >= max_parallel, block unconditionally.
       This prevents runaway parallelism regardless of scope.

    2. shard serialization
       If candidate_shard_id is non-null, block if any in-flight UoW has the
       same shard_id. UoWs in the same shard run serially.

    3. file_scope overlap
       Block only when BOTH the candidate and an in-flight UoW have explicit
       (non-null) file_scope lists that overlap via prefix matching. UoWs with
       null file_scope are treated as independent — they are allowed to proceed
       as long as the max_parallel cap is not reached and no shard constraint
       fires. Null file_scope means "no explicit scope annotation", not
       "touches everything" (issue #912 changed this semantics).

    If all conditions pass, return DispatchAllowed.

    Args:
        candidate_file_scope: Deserialized list of file/dir paths, or None.
        candidate_shard_id:   Shard name string, or None.
        executing_uows:       Sequence of UoW objects currently in-flight.
                              Each must have .file_scope (list|None) and
                              .shard_id (str|None) attributes.
        max_parallel:         Maximum concurrent in-flight UoWs. Read from
                              wos-config at the call site.

    Returns:
        DispatchAllowed — candidate may be dispatched.
        DispatchBlocked — candidate must wait; .reason describes why.
    """
    executing_count = len(executing_uows)

    # Gate 1: max_parallel cap
    if executing_count >= max_parallel:
        return DispatchBlocked(
            reason=(
                f"shard-stream: max_parallel={max_parallel} reached "
                f"({executing_count} UoWs in-flight)"
            )
        )

    # Gate 2: shard serialization
    # Null candidate shard_id means "no shard constraint" — gate skipped entirely.
    if candidate_shard_id is not None:
        for uow in executing_uows:
            if getattr(uow, "shard_id", None) == candidate_shard_id:
                return DispatchBlocked(
                    reason=(
                        f"shard-stream: shard {candidate_shard_id!r} already has "
                        f"in-flight UoW {uow.id!r} — shard runs serially"
                    )
                )

    # Gate 3: file_scope overlap — only when BOTH sides have explicit annotations.
    # Null file_scope (unannotated) UoWs are independent and do not conflict with
    # each other or with annotated UoWs. This replaces the prior exclusive-lock
    # semantics that blocked all null-scope UoWs when any one was in-flight.
    if candidate_file_scope is not None:
        for uow in executing_uows:
            in_flight_scope = getattr(uow, "file_scope", None)
            # Skip unannotated in-flight UoWs — they are independent.
            if in_flight_scope is None:
                continue
            if _scopes_overlap(candidate_file_scope, in_flight_scope):
                return DispatchBlocked(
                    reason=(
                        f"shard-stream: file_scope overlap with in-flight UoW {uow.id!r} "
                        f"(candidate={candidate_file_scope!r}, "
                        f"in-flight={in_flight_scope!r})"
                    )
                )

    return DispatchAllowed()


# ---------------------------------------------------------------------------
# Config reader (isolated here to keep steward.py calls readable)
# ---------------------------------------------------------------------------

def read_max_parallel() -> int:
    """Read max_parallel from wos-config.json.

    Returns DEFAULT_MAX_PARALLEL if the file is absent, unreadable, or the
    key is missing. Reads from disk on every call so that runtime changes
    take effect on the next steward heartbeat without a restart.

    Pure in intent: the only side effect is a disk read. Callers that need
    determinism in tests should monkeypatch this function.
    """
    try:
        from src.orchestration.dispatcher_handlers import read_wos_config
        config = read_wos_config()
        value = config.get("max_parallel", DEFAULT_MAX_PARALLEL)
        if isinstance(value, int) and value > 0:
            return value
    except Exception:
        pass
    return DEFAULT_MAX_PARALLEL
