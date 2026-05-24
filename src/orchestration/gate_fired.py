"""
gate_fired registry column helpers — migration 0019 support.

Spec: docs/wos/wos-completion-report-spec.md §Schema Additions §1

Provides:
- Translation map from _check_dispatch_eligibility verdict to gate_fired label
- Severity ordering (spiral > dead_end > burst > none)
- translate_eligibility_to_gate: pure translator
- gate_fired_severity: ordinal lookup
- is_upgrade: pure predicate for upgrade-only write semantics

Naming: "gate_fired" labels describe the dispatch topology gate that was
most severe during the UoW's lifetime. The steward writes gate_fired to
the registry when _check_dispatch_eligibility returns a non-dispatch verdict.
Only upgrades are applied — the column records the most severe gate seen
across all dispatch attempts.

Usage:
    verdict = _check_eligibility(uow, ...)
    if verdict != "dispatch":
        gate = translate_eligibility_to_gate(verdict)
        registry.write_gate_fired(uow_id, gate)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Named constants — anchored to spec values
# ---------------------------------------------------------------------------

#: Column name in uow_registry added by migration 0019.
GATE_FIRED_COLUMN_NAME: str = "gate_fired"

#: Translation from _check_dispatch_eligibility verdict to gate_fired label.
#: Spec: "escalate" → "spiral", "pause" → "dead_end", "throttle" → "burst", "dispatch" → "none"
_GATE_TRANSLATION: dict[str, str] = {
    "escalate": "spiral",
    "pause": "dead_end",
    "throttle": "burst",
    "dispatch": "none",
}

#: Severity ordinal for each gate_fired value.
#: Spec: spiral > dead_end > burst > none. Once written, only upgrade.
_GATE_SEVERITY: dict[str, int] = {
    "spiral": 3,
    "dead_end": 2,
    "burst": 1,
    "none": 0,
}


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def translate_eligibility_to_gate(eligibility_verdict: str) -> str:
    """
    Translate a _check_dispatch_eligibility verdict string to a gate_fired label.

    Pure function — no I/O.

    Args:
        eligibility_verdict: One of "escalate", "pause", "throttle", "dispatch".

    Returns:
        The gate_fired label: "spiral", "dead_end", "burst", or "none".

    Raises:
        ValueError: If eligibility_verdict is not one of the four known verdicts.
    """
    if eligibility_verdict not in _GATE_TRANSLATION:
        raise ValueError(
            f"unknown eligibility verdict: {eligibility_verdict!r}. "
            f"Known values: {sorted(_GATE_TRANSLATION)}"
        )
    return _GATE_TRANSLATION[eligibility_verdict]


def gate_fired_severity(gate_name: str) -> int:
    """
    Return the ordinal severity of a gate_fired value.

    Pure function — no I/O.

    Args:
        gate_name: A gate_fired label (e.g. "spiral", "burst", "none").
                   Unknown names return 0 (safe default — treated as "none").

    Returns:
        Integer severity: spiral=3, dead_end=2, burst=1, none=0, unknown=0.
    """
    return _GATE_SEVERITY.get(gate_name, 0)


def is_upgrade(current: str, new: str) -> bool:
    """
    Return True when new gate has strictly higher severity than current.

    Implements the upgrade-only write semantics for the gate_fired column:
    once written, only upgrade — never downgrade.

    Pure function — no I/O.

    Args:
        current: The currently stored gate_fired value.
        new: The candidate new gate_fired value.

    Returns:
        True if gate_fired_severity(new) > gate_fired_severity(current).
    """
    return gate_fired_severity(new) > gate_fired_severity(current)
