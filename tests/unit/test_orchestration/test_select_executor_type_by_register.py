"""
Tests for register-based executor type selection in steward.py.

Issue #842: _select_executor_type must route based on the UoW register field,
not keyword-matching on the summary string. A philosophical UoW whose summary
contains "fix" must route to lobster-meta, not functional-engineer.

## Register → executor_type mapping

    REGISTER_TO_EXECUTOR_TYPE = {
        "operational":           "functional-engineer",
        "iterative-convergent":  "functional-engineer",
        "human-judgment":        "lobster-generalist",
        "philosophical":         "lobster-meta",
    }
    # unknown register → "lobster-generalist" (safe fallback, with log warning)

## What is tested

- Each of the four named registers routes to the correct executor_type
- Unknown register falls back to lobster-generalist (not functional-engineer)
- Register takes precedence over summary keyword matching
  (a philosophical UoW with "fix bug" in the summary routes to lobster-meta)
- Mapping is a pure function of the register field — no side effects, no I/O
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow imports from src/
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


# ---------------------------------------------------------------------------
# Import the function under test
# ---------------------------------------------------------------------------

from orchestration.steward import _select_executor_type  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal UoW stub — only the fields _select_executor_type reads
# ---------------------------------------------------------------------------

def _make_uow(register: str, summary: str = "do the work", source: str = "github:issue") -> MagicMock:
    uow = MagicMock()
    uow.register = register
    uow.summary = summary
    uow.source = source
    return uow


# ---------------------------------------------------------------------------
# Named register constants — mirror what the spec says, tested as literals
# ---------------------------------------------------------------------------

OPERATIONAL = "operational"
ITERATIVE_CONVERGENT = "iterative-convergent"
HUMAN_JUDGMENT = "human-judgment"
PHILOSOPHICAL = "philosophical"

EXPECTED_FUNCTIONAL_ENGINEER = "functional-engineer"
EXPECTED_LOBSTER_GENERALIST = "lobster-generalist"
EXPECTED_LOBSTER_META = "lobster-meta"


# ---------------------------------------------------------------------------
# Core mapping tests — each register routes to the correct executor_type
# ---------------------------------------------------------------------------

class TestRegisterToExecutorTypeMapping:
    """
    _select_executor_type returns the correct executor_type for each register.
    These tests verify the mapping as a pure function of the register field.
    """

    def test_operational_routes_to_functional_engineer(self) -> None:
        uow = _make_uow(OPERATIONAL)
        assert _select_executor_type(uow) == EXPECTED_FUNCTIONAL_ENGINEER

    def test_iterative_convergent_routes_to_functional_engineer(self) -> None:
        uow = _make_uow(ITERATIVE_CONVERGENT)
        assert _select_executor_type(uow) == EXPECTED_FUNCTIONAL_ENGINEER

    def test_human_judgment_routes_to_lobster_generalist(self) -> None:
        uow = _make_uow(HUMAN_JUDGMENT)
        assert _select_executor_type(uow) == EXPECTED_LOBSTER_GENERALIST

    def test_philosophical_routes_to_lobster_meta(self) -> None:
        uow = _make_uow(PHILOSOPHICAL)
        assert _select_executor_type(uow) == EXPECTED_LOBSTER_META

    def test_unknown_register_falls_back_to_lobster_generalist(self) -> None:
        """Unknown registers must fall back to lobster-generalist, not functional-engineer."""
        uow = _make_uow("completely-unknown-register")
        assert _select_executor_type(uow) == EXPECTED_LOBSTER_GENERALIST

    def test_empty_register_falls_back_to_lobster_generalist(self) -> None:
        uow = _make_uow("")
        assert _select_executor_type(uow) == EXPECTED_LOBSTER_GENERALIST


# ---------------------------------------------------------------------------
# Register takes precedence over summary keyword matching
# ---------------------------------------------------------------------------

class TestRegisterPrecedenceOverSummaryKeywords:
    """
    Before issue #842, _select_executor_type matched keywords in the summary
    string. This caused philosophical UoWs with "fix" or "bug" in the summary
    to route to functional-engineer instead of lobster-meta.

    After the fix, register is the primary routing signal. Summary keywords
    are irrelevant for the executor_type selection.
    """

    def test_philosophical_with_fix_keyword_routes_to_lobster_meta(self) -> None:
        """
        A philosophical UoW whose summary contains "fix" must route to lobster-meta,
        not functional-engineer.
        """
        uow = _make_uow(PHILOSOPHICAL, summary="fix the philosophical inconsistency in the system")
        assert _select_executor_type(uow) == EXPECTED_LOBSTER_META

    def test_philosophical_with_bug_keyword_routes_to_lobster_meta(self) -> None:
        uow = _make_uow(PHILOSOPHICAL, summary="bug in the reasoning model: attunement mismatch")
        assert _select_executor_type(uow) == EXPECTED_LOBSTER_META

    def test_philosophical_with_implement_keyword_routes_to_lobster_meta(self) -> None:
        uow = _make_uow(PHILOSOPHICAL, summary="implement a new epistemological framework")
        assert _select_executor_type(uow) == EXPECTED_LOBSTER_META

    def test_human_judgment_with_code_keywords_routes_to_lobster_generalist(self) -> None:
        """human-judgment UoWs about code decisions still route to lobster-generalist."""
        uow = _make_uow(HUMAN_JUDGMENT, summary="decide: should we refactor the executor or redesign?")
        assert _select_executor_type(uow) == EXPECTED_LOBSTER_GENERALIST

    def test_operational_with_ops_keywords_routes_to_functional_engineer(self) -> None:
        """operational UoWs with ops keywords still route to functional-engineer."""
        uow = _make_uow(OPERATIONAL, summary="deploy the new cron configuration")
        assert _select_executor_type(uow) == EXPECTED_FUNCTIONAL_ENGINEER

    def test_iterative_convergent_with_bug_keyword_routes_to_functional_engineer(self) -> None:
        """iterative-convergent UoWs with 'bug' still route to functional-engineer."""
        uow = _make_uow(ITERATIVE_CONVERGENT, summary="bug: iterate on the convergence loop")
        assert _select_executor_type(uow) == EXPECTED_FUNCTIONAL_ENGINEER


# ---------------------------------------------------------------------------
# Mapping is deterministic — same input always produces same output
# ---------------------------------------------------------------------------

class TestMappingIsDeterministic:
    """
    _select_executor_type is a pure function: same register → same result,
    regardless of how many times it's called or in what order.
    """

    def test_operational_is_idempotent(self) -> None:
        uow = _make_uow(OPERATIONAL)
        results = [_select_executor_type(uow) for _ in range(5)]
        assert all(r == EXPECTED_FUNCTIONAL_ENGINEER for r in results)

    def test_philosophical_is_idempotent(self) -> None:
        uow = _make_uow(PHILOSOPHICAL)
        results = [_select_executor_type(uow) for _ in range(5)]
        assert all(r == EXPECTED_LOBSTER_META for r in results)

    def test_human_judgment_is_idempotent(self) -> None:
        uow = _make_uow(HUMAN_JUDGMENT)
        results = [_select_executor_type(uow) for _ in range(5)]
        assert all(r == EXPECTED_LOBSTER_GENERALIST for r in results)
