"""
routing_classifier.py — WOS routing classifier for UoW posture assignment.

Loads classifier.yaml from ~/lobster-user-config/orchestration/classifier.yaml
and applies first-match-wins rules to prescription metadata to determine posture
and route_reason for a UoW at germination time.

Rules structure (classifier.yaml):
  rules:
    - name: <str>
      priority: <int>          # higher = evaluated first
      conditions: []           # AND-joined; empty = catch-all
        - field: <str>         # field in the prescription metadata dict
          op: eq | gt | lt | contains
          value: <scalar>
      posture: <str>           # solo | sequential | review-loop | fan-out
      route_reason_template: <str>

Usage:
    from orchestration.routing_classifier import classify_posture, ClassifierResult

    result = classify_posture({"type": "seed", "risk": "high", "files_touched": 3})
    # result.posture == "sequential"
    # result.route_reason == "Rule 'design-first' matched: type=seed"

The classifier is loaded from disk once per process call (no caching — the file
is small and reads are cheap; avoids stale-cache bugs in long-running processes).
Falls back to solo posture with FALLBACK_ROUTE_REASON when the YAML is absent or
malformed — germination must never fail due to classifier unavailability.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("routing_classifier")

# Default classifier YAML path fallback — used when WOS_CLASSIFIER_YAML is not set.
# NOT read at import time; _default_classifier_path() is called lazily at classify_posture
# call time so that monkeypatch.setenv("WOS_CLASSIFIER_YAML", ...) takes effect in tests.
_CLASSIFIER_YAML_DEFAULT_FALLBACK = (
    Path.home() / "lobster-user-config" / "orchestration" / "classifier.yaml"
)


def _default_classifier_path() -> Path:
    """Return the default classifier path, reading WOS_CLASSIFIER_YAML at call time."""
    env_val = os.environ.get("WOS_CLASSIFIER_YAML")
    return Path(env_val) if env_val else _CLASSIFIER_YAML_DEFAULT_FALLBACK

# Written to route_reason when the classifier file is absent or cannot be parsed.
FALLBACK_ROUTE_REASON = "classifier-unavailable: defaulting to solo"

# Written to route_reason when no rule matches (should not happen if YAML has a catch-all).
NO_MATCH_ROUTE_REASON = "classifier: no rule matched — defaulting to solo"

# Default posture when classifier is unavailable or no rule matches.
FALLBACK_POSTURE = "solo"


@dataclass(frozen=True)
class ClassifierResult:
    """Result of running the routing classifier against prescription metadata."""
    posture: str
    route_reason: str
    rule_name: str | None = None


def _load_classifier_yaml(path: Path) -> list[dict] | None:
    """
    Load and return sorted classifier rules from the YAML file.

    Returns None when the file is absent or cannot be parsed — callers fall
    back to FALLBACK_POSTURE without raising.

    Rules are sorted by priority descending (highest priority evaluated first).
    """
    if not path.exists():
        log.debug("Classifier YAML not found at %s — using fallback posture", path)
        return None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        log.warning(
            "PyYAML not available — cannot load classifier.yaml. "
            "Install pyyaml to enable posture classification."
        )
        return None
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
        rules = data.get("rules", [])
        return sorted(rules, key=lambda r: r.get("priority", 0), reverse=True)
    except Exception as exc:
        log.warning("Failed to parse classifier YAML at %s — %s", path, exc)
        return None


def _evaluate_condition(condition: dict, metadata: dict[str, Any]) -> bool:
    """
    Evaluate a single condition against prescription metadata.

    Supported operators: eq, gt, lt, contains.
    Missing fields are treated as falsy — the condition fails safely.
    """
    field = condition.get("field", "")
    op = condition.get("op", "eq")
    expected = condition.get("value")
    actual = metadata.get(field)

    if actual is None:
        return False

    match op:
        case "eq":
            return str(actual) == str(expected)
        case "gt":
            try:
                return float(actual) > float(expected)
            except (TypeError, ValueError):
                return False
        case "lt":
            try:
                return float(actual) < float(expected)
            except (TypeError, ValueError):
                return False
        case "contains":
            return str(expected) in str(actual)
        case _:
            log.warning("Unknown classifier condition operator %r — treating as False", op)
            return False


def _rule_matches(rule: dict, metadata: dict[str, Any]) -> bool:
    """
    Return True if all conditions in the rule match the metadata (AND-joined).

    An empty conditions list is a catch-all — always matches.
    """
    conditions = rule.get("conditions", [])
    return all(_evaluate_condition(c, metadata) for c in conditions)


def classify_posture(
    metadata: dict[str, Any],
    classifier_path: Path | None = None,
) -> ClassifierResult:
    """
    Run first-match-wins classifier rules against prescription metadata.

    Args:
        metadata: Dict of prescription fields. Recognized keys per classifier.yaml:
            - type: "seed" | "executable" (UoW type from germination)
            - risk: "high" | "medium" | "low" (optional, from prescription)
            - files_touched: int (optional, from prescription)
        classifier_path: Override path to classifier.yaml. Defaults to
            ~/lobster-user-config/orchestration/classifier.yaml.
            Overridable via WOS_CLASSIFIER_YAML env var.

    Returns:
        ClassifierResult with posture and route_reason. Never raises — falls
        back to solo on any error so germination is never blocked.
    """
    path = classifier_path if classifier_path is not None else _default_classifier_path()
    rules = _load_classifier_yaml(path)

    if rules is None:
        return ClassifierResult(
            posture=FALLBACK_POSTURE,
            route_reason=FALLBACK_ROUTE_REASON,
            rule_name=None,
        )

    for rule in rules:
        if _rule_matches(rule, metadata):
            posture = rule.get("posture", FALLBACK_POSTURE)
            route_reason = rule.get("route_reason_template", f"Rule {rule.get('name')!r} matched")
            rule_name = rule.get("name")
            log.debug(
                "Classifier matched rule %r — posture=%s, route_reason=%r",
                rule_name, posture, route_reason,
            )
            return ClassifierResult(
                posture=posture,
                route_reason=route_reason,
                rule_name=rule_name,
            )

    log.warning("Classifier: no rule matched for metadata %r — defaulting to solo", metadata)
    return ClassifierResult(
        posture=FALLBACK_POSTURE,
        route_reason=NO_MATCH_ROUTE_REASON,
        rule_name=None,
    )
