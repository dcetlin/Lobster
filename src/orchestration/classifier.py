"""
WOS Phase 3 — Routing Classifier.

Loads ~/lobster-user-config/orchestration/classifier.yaml and evaluates rules
in descending priority order (first-match-wins). The catch-all ``default`` rule
(empty conditions list) always matches.

Condition evaluation semantics:
  eq  : uow.get(field) == value
  gt  : uow.get(field, 0) > value

All conditions in a rule use AND semantics: all must be true for the rule to
fire. Scoring, weighting, and additive semantics are explicitly NOT implemented.

Future ``op`` types (lt, contains, exists) are architecturally supported by the
YAML schema's ``op`` field even though only ``eq`` and ``gt`` are evaluated here.
Unknown op values raise ValueError so mis-typed configs fail loudly.

Usage:
    from src.orchestration.classifier import classify, ClassifierResult
    result = classify({"type": "seed"})
    # result.posture → "sequential"
    # result.rule_name → "design-first"
    # result.route_reason → "Rule 'design-first' matched: type=seed"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("classifier")

# ---------------------------------------------------------------------------
# Config path — resolvable at import time; override in tests via monkeypatch
# ---------------------------------------------------------------------------

CLASSIFIER_CONFIG_PATH: Path = Path.home() / "lobster-user-config" / "orchestration" / "classifier.yaml"

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassifierResult:
    """Named result from classify(); no dict[str, Any] at call sites."""

    posture: str
    """The matched rule's posture value (e.g. 'solo', 'sequential', 'fan-out', 'review-loop')."""

    rule_name: str
    """The name of the rule that fired."""

    route_reason: str
    """Rendered route_reason_template for the matching rule."""


# ---------------------------------------------------------------------------
# Internal — lazy config loader
# ---------------------------------------------------------------------------

_rules_cache: list[dict[str, Any]] | None = None


def _load_rules() -> list[dict[str, Any]]:
    """Load and cache rules from classifier.yaml, sorted by descending priority."""
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache

    config_path = CLASSIFIER_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"Classifier config not found: {config_path}. "
            "Create ~/lobster-user-config/orchestration/classifier.yaml."
        )

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rules: list[dict[str, Any]] = raw.get("rules") or []

    # Sort descending by priority so iteration order == evaluation order.
    rules_sorted = sorted(rules, key=lambda r: r.get("priority", 0), reverse=True)
    _rules_cache = rules_sorted
    log.debug("Classifier: loaded %d rules from %s", len(rules_sorted), config_path)
    return rules_sorted


def _clear_rules_cache() -> None:
    """Clear the rules cache. Used in tests to reload config after monkeypatching."""
    global _rules_cache
    _rules_cache = None


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------


def _evaluate_condition(uow: dict[str, Any], condition: dict[str, Any]) -> bool:
    """
    Evaluate a single condition against a UoW dict.

    Supports:
      eq  : uow.get(field) == value
      gt  : uow.get(field, 0) > value

    Raises ValueError for unknown op values so mis-typed configs fail loudly.
    """
    field: str = condition["field"]
    op: str = condition["op"]
    value: Any = condition["value"]

    match op:
        case "eq":
            return uow.get(field) == value
        case "gt":
            actual = uow.get(field, 0)
            return actual > value
        case _:
            # Unknown op: fail loudly rather than silently evaluating as False.
            # This surfaces YAML config errors at rule-evaluation time, not
            # buried in wrong routing decisions.
            raise ValueError(
                f"Classifier: unknown condition op {op!r} for field {field!r}. "
                "Supported: eq, gt. Future: lt, contains, exists."
            )


def _rule_matches(uow: dict[str, Any], rule: dict[str, Any]) -> bool:
    """
    Return True if all conditions in ``rule`` are satisfied by ``uow`` (AND semantics).

    An empty conditions list (the ``default`` catch-all rule) always matches.
    """
    conditions: list[dict[str, Any]] = rule.get("conditions") or []
    return all(_evaluate_condition(uow, cond) for cond in conditions)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(uow: dict[str, Any]) -> ClassifierResult:
    """
    Classify a UoW dict and return a ClassifierResult.

    Rules are evaluated in descending priority order; the first matching rule
    wins. The ``default`` rule (empty conditions, priority 0) is always present
    in the standard config as the catch-all.

    Args:
        uow: A dict representing the UoW record. The classifier reads only the
             fields referenced by rules (e.g. ``type``, ``risk``, ``files_touched``).
             Extra fields are ignored.

    Returns:
        ClassifierResult with posture, rule_name, and rendered route_reason.

    Raises:
        FileNotFoundError: If the classifier config YAML is absent.
        ValueError: If a condition uses an unknown ``op``.
        RuntimeError: If no rule matches (indicates a missing catch-all rule in config).
    """
    rules = _load_rules()

    for rule in rules:
        if _rule_matches(uow, rule):
            name: str = rule["name"]
            posture: str = rule["posture"]
            template: str = rule.get("route_reason_template") or f"Rule '{name}' matched"
            result = ClassifierResult(
                posture=posture,
                rule_name=name,
                route_reason=template,
            )
            log.debug(
                "Classifier: rule '%s' (priority=%s) matched UoW → posture=%s",
                name,
                rule.get("priority", 0),
                posture,
            )
            return result

    # Should never happen when the config contains a default rule.
    raise RuntimeError(
        "Classifier: no rule matched the UoW. Ensure the config contains a "
        "catch-all 'default' rule with empty conditions."
    )
