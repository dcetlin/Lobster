"""
WOS Pre-flight Protocol — is_still_needed() hook.

This module defines the shared "is this work still needed?" pre-flight check
called by the WOS executor immediately before dispatching a subagent for any
UoW in ready-for-executor state.

Design motivation
-----------------
~40% of heat outcomes (already-done re-dispatches, merge-pr retries, scorer
re-runs) trace to a single architectural gap: the executor dispatches based on
"work exists in queue" but has no shared protocol for "work is still needed."
Each UoW type currently reinvents or forgets this check, leading to recurring
heat that requires case-by-case debugging.

The hook
--------
``is_still_needed(uow) -> bool``

Called once per UoW, immediately before the subagent is spawned.  If it returns
``False``, the executor short-circuits:
  - The subagent is NOT spawned.
  - The UoW is marked complete with ``outcome_category="heat"`` via the
    existing ``registry.complete_uow`` path.
  - A one-line log entry records the short-circuit.

If it returns ``True`` (the default), dispatch proceeds unchanged.

Extension point
---------------
Per-type implementations are registered via ``register_preflight_check``:

    from orchestration.wos_preflight import register_preflight_check

    def my_check(uow) -> bool:
        ...  # return False if work is no longer needed

    register_preflight_check("merge-pr", my_check)

Adding a check for a new UoW type does NOT require editing this file or the
executor dispatch core.

Intended first consumers
------------------------
- **Issue #927**: merge-pr idempotency gate — check whether the PR is already
  merged before dispatching a merge-pr subagent.
- **Issue #928**: scorer staleness gate — check whether the scored issue/PR is
  still open and needs scoring before dispatching a scorer subagent.

These issues are absorbed as sub-tasks of this architectural interface; their
implementations will register checks here rather than adding per-type logic to
the dispatch core.

Public API
----------
``is_still_needed(uow: UoW) -> bool``
    Call this before dispatching a subagent.  Returns True by default.

``register_preflight_check(uow_type: str, check_fn: PreflightCheck) -> None``
    Register a type-specific check.  Idempotent: re-registering the same type
    overwrites the previous entry (useful for tests and hot-reloading).

``PreflightCheck``
    Protocol type: ``(uow: UoW) -> bool``.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger("wos_preflight")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class PreflightCheck(Protocol):
    """
    Protocol for a UoW pre-flight check function.

    Implementors receive the full UoW object and return True when the work is
    still needed (dispatch should proceed) or False when it is not (short-circuit).
    """

    def __call__(self, uow: object) -> bool:
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Type-keyed registry mapping uow_type → check function.
#: Populated by ``register_preflight_check`` at import time or at runtime.
#: Issue #927 (merge-pr) and issue #928 (scorer) are the intended first entries.
_REGISTRY: dict[str, PreflightCheck] = {}


def register_preflight_check(uow_type: str, check_fn: PreflightCheck) -> None:
    """
    Register a pre-flight check for a given UoW type.

    Idempotent: re-registering the same type overwrites the previous entry.
    This is intentional — it simplifies testing (register a stub, test, restore)
    and supports hot-reloading in long-running environments.

    Args:
        uow_type: The UoW type string (e.g. "merge-pr", "scorer").
                  Must match the ``type`` field stored in uow_registry.
        check_fn: Callable conforming to ``PreflightCheck`` — takes a UoW
                  object and returns True (still needed) or False (short-circuit).

    Intended consumers:
        - Issue #927: ``register_preflight_check("merge-pr", <check_fn>)``
        - Issue #928: ``register_preflight_check("scorer", <check_fn>)``

    Raises:
        TypeError: If ``check_fn`` is not callable.
    """
    if not callable(check_fn):
        raise TypeError(
            f"register_preflight_check: check_fn for type {uow_type!r} "
            f"must be callable, got {type(check_fn)!r}"
        )
    _REGISTRY[uow_type] = check_fn
    log.debug(
        "wos_preflight: registered check for uow_type=%r (fn=%s)",
        uow_type,
        getattr(check_fn, "__qualname__", repr(check_fn)),
    )


# ---------------------------------------------------------------------------
# Default implementation
# ---------------------------------------------------------------------------

def _default_check(uow: object) -> bool:
    """
    Default pre-flight check — always returns True.

    Used for any UoW type that has not registered a type-specific check.
    Guarantees no regression: all existing UoW types dispatch normally until
    an explicit check is registered for their type.
    """
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def is_still_needed(uow: object) -> bool:
    """
    Run the pre-flight check for a UoW and return whether dispatch should proceed.

    Looks up the check function for the UoW's ``type`` field in the registry.
    Falls back to the default implementation (returns ``True``) when no
    type-specific check is registered.

    Args:
        uow: A UoW object (any object with a ``type`` attribute, e.g. the
             ``UoW`` namedtuple/dataclass returned by ``registry.get()``).

    Returns:
        True  — work is still needed; dispatch should proceed normally.
        False — work is no longer needed; executor must short-circuit:
                mark the UoW complete with outcome_category="heat" and skip
                subagent dispatch.

    Failure mode (check raises):
        If the registered check function raises an exception, the exception is
        caught, logged at WARNING level, and ``True`` is returned (fail-open).
        A failing check must not block dispatch — the executor must proceed
        unless explicitly told the work is not needed.
    """
    uow_type: str = getattr(uow, "type", "") or ""
    check_fn = _REGISTRY.get(uow_type, _default_check)

    try:
        result = check_fn(uow)
    except Exception as exc:
        log.warning(
            "wos_preflight: check for uow_type=%r raised %s: %s — defaulting to True (fail-open)",
            uow_type,
            type(exc).__name__,
            exc,
        )
        return True

    return bool(result)
