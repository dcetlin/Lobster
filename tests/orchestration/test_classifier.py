"""
Tests for WOS Phase 3 — Routing Classifier and Hook System.

Tests cover:
1. test_design_first_rule       — UoW with type=seed → posture sequential, rule_name design-first
2. test_high_risk_rule          — UoW with risk=high → review-loop
3. test_parallelizable_rule     — UoW with files_touched=7, type=executable → fan-out
4. test_default_rule            — UoW with no matching fields → solo
5. test_priority_order          — UoW with risk=high AND type=seed → sequential (design-first wins at 10)
6. test_loop_guard              — UoW with same hook_id appearing 3 times in hooks_applied →
                                  hook application returns empty list and hooks_frozen=True
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow importing from src/ in the worktree
# ---------------------------------------------------------------------------

import sys

_WORKTREE_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _WORKTREE_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def classifier_yaml(tmp_path: Path) -> Path:
    """Write a minimal classifier.yaml to a temp path and return it."""
    yaml_content = """\
rules:
  - name: design-first
    priority: 10
    conditions:
      - field: type
        op: eq
        value: seed
    posture: sequential
    route_reason_template: "Rule 'design-first' matched: type=seed"

  - name: high-risk-review
    priority: 9
    conditions:
      - field: risk
        op: eq
        value: high
    posture: review-loop
    route_reason_template: "Rule 'high-risk-review' matched: risk=high"

  - name: parallelizable-multifile
    priority: 8
    conditions:
      - field: files_touched
        op: gt
        value: 5
      - field: type
        op: eq
        value: executable
    posture: fan-out
    route_reason_template: "Rule 'parallelizable-multifile' matched: files_touched>5 AND type=executable"

  - name: default
    priority: 0
    conditions: []
    posture: solo
    route_reason_template: "Rule 'default' (catch-all) matched"
"""
    p = tmp_path / "classifier.yaml"
    p.write_text(yaml_content, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def patch_classifier_config(classifier_yaml: Path) -> None:
    """
    Patch CLASSIFIER_CONFIG_PATH and clear cache before each test so tests
    use the fixture config rather than the real ~/lobster-user-config/... path.
    """
    import orchestration.classifier as cls_module
    cls_module._clear_rules_cache()
    with patch.object(cls_module, "CLASSIFIER_CONFIG_PATH", classifier_yaml):
        cls_module._clear_rules_cache()
        yield
    cls_module._clear_rules_cache()


# ---------------------------------------------------------------------------
# Minimal registry stub for hook tests
# ---------------------------------------------------------------------------

def _make_stub_db(tmp_path: Path, *, hook_count: int = 0) -> tuple[Path, "RegistryStub"]:
    """
    Create a minimal SQLite DB with just enough schema for hooks.py to work,
    then insert a test UoW row with the given hook_count repeated hook_id.

    Returns (db_path, stub_registry).
    """
    db_path = tmp_path / "test_registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE uow_registry (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'proposed',
            hooks_applied TEXT DEFAULT '[]',
            hooks_frozen INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            route_reason TEXT,
            classifier_thrash INTEGER NOT NULL DEFAULT 0,
            rule_name TEXT
        )
    """)
    # Pre-populate hooks_applied with hook_count entries of 'retry-on-failure'
    hooks = json.dumps(["retry-on-failure"] * hook_count)
    conn.execute(
        "INSERT INTO uow_registry (id, status, hooks_applied) VALUES (?, 'failed', ?)",
        ("uow_test_001", hooks),
    )
    conn.commit()
    conn.close()

    class RegistryStub:
        def __init__(self, db: Path) -> None:
            self.db_path = db

    return db_path, RegistryStub(db_path)


# ---------------------------------------------------------------------------
# Test 1: design-first rule
# ---------------------------------------------------------------------------

def test_design_first_rule() -> None:
    """UoW with type=seed → posture sequential, rule_name design-first."""
    from orchestration.classifier import classify

    result = classify({"type": "seed"})
    assert result.posture == "sequential"
    assert result.rule_name == "design-first"
    assert "design-first" in result.route_reason


# ---------------------------------------------------------------------------
# Test 2: high-risk-review rule
# ---------------------------------------------------------------------------

def test_high_risk_rule() -> None:
    """UoW with risk=high → posture review-loop, rule_name high-risk-review."""
    from orchestration.classifier import classify

    result = classify({"risk": "high"})
    assert result.posture == "review-loop"
    assert result.rule_name == "high-risk-review"


# ---------------------------------------------------------------------------
# Test 3: parallelizable-multifile rule
# ---------------------------------------------------------------------------

def test_parallelizable_rule() -> None:
    """UoW with files_touched=7 and type=executable → fan-out."""
    from orchestration.classifier import classify

    result = classify({"files_touched": 7, "type": "executable"})
    assert result.posture == "fan-out"
    assert result.rule_name == "parallelizable-multifile"


# ---------------------------------------------------------------------------
# Test 4: default rule
# ---------------------------------------------------------------------------

def test_default_rule() -> None:
    """UoW with no matching fields → solo (catch-all default)."""
    from orchestration.classifier import classify

    result = classify({"completely": "irrelevant", "fields": True})
    assert result.posture == "solo"
    assert result.rule_name == "default"


# ---------------------------------------------------------------------------
# Test 5: priority order (design-first beats high-risk-review)
# ---------------------------------------------------------------------------

def test_priority_order() -> None:
    """UoW with risk=high AND type=seed → sequential (design-first wins at priority 10)."""
    from orchestration.classifier import classify

    # Both design-first (type=seed, priority 10) and high-risk-review (risk=high, priority 9)
    # match this UoW. design-first must win.
    result = classify({"type": "seed", "risk": "high"})
    assert result.posture == "sequential"
    assert result.rule_name == "design-first"


# ---------------------------------------------------------------------------
# Test 6: loop-guard
# ---------------------------------------------------------------------------

def test_loop_guard(tmp_path: Path) -> None:
    """
    UoW with same hook_id appearing 3 times in hooks_applied →
    hook application returns empty list and hooks_frozen=True.
    """
    from orchestration.hooks import apply_hooks

    # Seed the DB with 3 occurrences of 'retry-on-failure' in hooks_applied
    db_path, registry = _make_stub_db(tmp_path, hook_count=3)

    # apply_hooks on on_failure event — loop-guard should fire and freeze hooks
    fired = apply_hooks("uow_test_001", "on_failure", registry)

    # Loop-guard fired, so no hooks should be recorded in fired list
    assert fired == []

    # Verify hooks_frozen is set in DB
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT hooks_frozen FROM uow_registry WHERE id = ?", ("uow_test_001",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert bool(row["hooks_frozen"]) is True


# ---------------------------------------------------------------------------
# Bonus: verify route_reason is written to registry via classify_and_register
# ---------------------------------------------------------------------------

def test_classify_and_register_writes_route_reason(tmp_path: Path) -> None:
    """
    classify_and_register writes posture and route_reason to the registry record.
    """
    from orchestration.classify_intake import classify_and_register

    # Create a minimal DB with the required columns
    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE uow_registry (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'proposed',
            hooks_applied TEXT DEFAULT '[]',
            hooks_frozen INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            route_reason TEXT,
            classifier_thrash INTEGER NOT NULL DEFAULT 0,
            rule_name TEXT,
            posture TEXT DEFAULT 'solo'
        )
    """)
    conn.execute(
        "INSERT INTO uow_registry (id, status) VALUES (?, 'proposed')",
        ("uow_test_002",),
    )
    conn.commit()
    conn.close()

    class RegistryStub:
        def __init__(self, db: Path) -> None:
            self.db_path = db

    registry = RegistryStub(db_path)
    uow = {"type": "seed", "source_issue_number": 99}

    classify_and_register("uow_test_002", uow, registry)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT posture, route_reason, rule_name FROM uow_registry WHERE id = ?",
        ("uow_test_002",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["posture"] == "sequential"
    assert row["rule_name"] == "design-first"
    assert "design-first" in (row["route_reason"] or "")
