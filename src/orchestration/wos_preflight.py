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

Type key derivation
-------------------
``is_still_needed`` uses ``_derive_preflight_key(uow)`` rather than the raw
``type`` attribute to determine which check function to call.  This allows
semantic sub-types (e.g. "merge-pr") to be derived from other UoW fields:

- UoWs with ``source_ref`` starting with ``"github:pr/"`` → key "merge-pr".
- All other UoWs → key from ``str(uow.type)`` (backwards-compatible default).

Public API
----------
``is_still_needed(uow: UoW) -> bool``
    Call this before dispatching a subagent.  Returns True by default.

``register_preflight_check(uow_type: str, check_fn: PreflightCheck) -> None``
    Register a type-specific check.  Idempotent: re-registering the same type
    overwrites the previous entry (useful for tests and hot-reloading).

``_derive_preflight_key(uow: object) -> str``
    Private helper used by ``is_still_needed``. Exported for test access.

``PreflightCheck``
    Protocol type: ``(uow: UoW) -> bool``.
"""

from __future__ import annotations

import logging
import subprocess as _subprocess
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
# Key derivation
# ---------------------------------------------------------------------------

def _derive_preflight_key(uow: object) -> str:
    """
    Derive the preflight registry key for a UoW.

    Most UoWs use str(uow.type) directly. The exception is UoWs whose
    source_ref starts with "github:pr/" — these represent PRs to merge and
    use the semantic key "merge-pr" regardless of their .type field.

    This indirection lets is_still_needed dispatch to the merge-pr check
    without requiring a dedicated UoWType enum value.
    """
    source_ref: str = getattr(uow, "source_ref", "") or ""
    if source_ref.startswith("github:pr/"):
        return "merge-pr"
    uow_type: str = str(getattr(uow, "type", "") or "")
    return uow_type


# ---------------------------------------------------------------------------
# Merge-pr idempotency check (issue #927)
# ---------------------------------------------------------------------------

def _check_merge_pr(uow: object) -> bool:
    """
    Pre-flight check for merge-pr UoWs.

    Extracts the PR number from uow.source_ref ("github:pr/<number>"),
    calls ``gh pr view <number> --repo <repo> --json state``, and returns
    False (work no longer needed) when state == "MERGED".

    Fail-open: any gh failure, parse error, or missing field returns True
    so the executor proceeds with dispatch rather than silently suppressing
    a legitimate merge.

    The repo is read from the LOBSTER_WOS_REPO environment variable
    (default: "dcetlin/Lobster").
    """
    import json as _json
    import os as _os

    source_ref: str = getattr(uow, "source_ref", "") or ""
    if not source_ref.startswith("github:pr/"):
        return True
    try:
        pr_number = int(source_ref.split("/")[-1])
    except (ValueError, IndexError):
        log.warning(
            "_check_merge_pr: could not parse PR number from source_ref=%r — fail-open",
            source_ref,
        )
        return True

    repo = _os.environ.get("LOBSTER_WOS_REPO", "dcetlin/Lobster")
    try:
        result = _subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "state"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.warning(
                "_check_merge_pr: gh pr view failed for PR #%d (exit %d) — fail-open",
                pr_number, result.returncode,
            )
            return True
        data = _json.loads(result.stdout)
        state = data.get("state", "")
        if state == "MERGED":
            log.info(
                "_check_merge_pr: PR #%d is already MERGED — short-circuiting dispatch (heat)",
                pr_number,
            )
            return False
        return True
    except Exception as exc:
        log.warning(
            "_check_merge_pr: error checking PR #%d state — %s: %s — fail-open",
            pr_number, type(exc).__name__, exc,
        )
        return True


# Register the merge-pr idempotency check (issue #927).
# Must be registered at module load time so the executor's is_still_needed
# call picks it up without requiring any additional wiring.
register_preflight_check("merge-pr", _check_merge_pr)


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
    uow_key: str = _derive_preflight_key(uow)
    check_fn = _REGISTRY.get(uow_key, _default_check)

    try:
        result = check_fn(uow)
    except Exception as exc:
        log.warning(
            "wos_preflight: check for key=%r raised %s: %s — defaulting to True (fail-open)",
            uow_key,
            type(exc).__name__,
            exc,
        )
        return True

    return bool(result)
