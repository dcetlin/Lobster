"""
Tests for evaluate_condition(uow) — conditions.py

TDD: tests written first, implementation follows.

Coverage:
- NULL trigger field → True (backward compat)
- immediate trigger → True
- issue_closed: issue open → False
- issue_closed: issue closed → True
- issue_closed: GitHub API returns 403 → False + audit entry
- issue_closed: GitHub API returns 404 → False + audit entry
- issue_closed: GitHub API non-200 (500) → False + audit entry
- registry_state: target UoW in specified state → True
- registry_state: target UoW NOT in specified state → False
- registry_state: non-existent UoW ID → False + audit entry
- registry_state: unreadable registry → False (no crash)
- Malformed JSON trigger → True + audit entry
- Unknown trigger type → True + audit entry
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def registry(db_path: Path):
    from src.orchestration.registry import Registry
    return Registry(db_path)


def _make_uow(
    uow_id: str = "uow_test_abc",
    trigger_json: str | None = '{"type": "immediate"}',
    source_issue_number: int = 42,
    status: str = "pending",
) -> dict[str, Any]:
    """Build a minimal UoW dict as returned by _row_to_dict."""
    return {
        "id": uow_id,
        "status": status,
        "source_issue_number": source_issue_number,
        "trigger": json.loads(trigger_json) if trigger_json is not None else None,
    }


def _audit_entries(db_path: Path, uow_id: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id",
        (uow_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# NULL trigger (backward compat)
# ---------------------------------------------------------------------------

class TestNullTrigger:
    def test_null_trigger_returns_true(self, db_path):
        from src.orchestration.conditions import evaluate_condition
        uow = _make_uow(uow_id="uow_null_1", trigger_json=None)
        result = evaluate_condition(uow)
        assert result is True

    def test_null_trigger_writes_no_audit(self, registry, db_path):
        from src.orchestration.conditions import evaluate_condition
        uow = _make_uow(uow_id="uow_null_2", trigger_json=None)
        # Insert UoW so audit_log writes work
        from src.orchestration.registry import Registry
        reg = Registry(db_path)
        # Just evaluate — no audit entry expected
        evaluate_condition(uow)
        # No audit entries should exist for this UoW
        entries = _audit_entries(db_path, "uow_null_2")
        assert entries == []


# ---------------------------------------------------------------------------
# immediate trigger
# ---------------------------------------------------------------------------

class TestImmediateTrigger:
    def test_immediate_returns_true(self):
        from src.orchestration.conditions import evaluate_condition
        uow = _make_uow(trigger_json='{"type": "immediate"}')
        assert evaluate_condition(uow) is True

    def test_immediate_already_dict(self):
        """_row_to_dict deserializes trigger JSON — ensure dict is handled."""
        from src.orchestration.conditions import evaluate_condition
        uow = {
            "id": "uow_imm_2",
            "trigger": {"type": "immediate"},
            "source_issue_number": 1,
        }
        assert evaluate_condition(uow) is True


# ---------------------------------------------------------------------------
# issue_closed trigger
# ---------------------------------------------------------------------------

class TestIssueClosedTrigger:
    def _make_closed_trigger(self, number: int = 42) -> dict:
        return _make_uow(
            uow_id=f"uow_ic_{number}",
            trigger_json=json.dumps({"type": "issue_closed", "number": number}),
            source_issue_number=number,
        )

    def test_issue_open_returns_false(self):
        from src.orchestration.conditions import evaluate_condition
        uow = self._make_closed_trigger(100)

        def mock_github_client(issue_number: int) -> dict:
            return {"status_code": 200, "state": "open"}

        result = evaluate_condition(uow, github_client=mock_github_client)
        assert result is False

    def test_issue_closed_returns_true(self):
        from src.orchestration.conditions import evaluate_condition
        uow = self._make_closed_trigger(101)

        def mock_github_client(issue_number: int) -> dict:
            return {"status_code": 200, "state": "closed"}

        result = evaluate_condition(uow, github_client=mock_github_client)
        assert result is True

    def test_github_api_403_returns_false_with_audit(self, registry, db_path):
        from src.orchestration.conditions import evaluate_condition
        uow_id = "uow_ic_403"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json='{"type": "issue_closed", "number": 200}',
        )

        def mock_github_client(issue_number: int) -> dict:
            return {"status_code": 403, "state": None}

        result = evaluate_condition(uow, github_client=mock_github_client, registry=registry)
        assert result is False

        entries = _audit_entries(db_path, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "condition_eval_failed"
        note = json.loads(entries[0]["note"])
        assert note["error_code"] == 403
        assert note["trigger_type"] == "issue_closed"

    def test_github_api_404_returns_false_with_audit(self, registry, db_path):
        from src.orchestration.conditions import evaluate_condition
        uow_id = "uow_ic_404"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json='{"type": "issue_closed", "number": 201}',
        )

        def mock_github_client(issue_number: int) -> dict:
            return {"status_code": 404, "state": None}

        result = evaluate_condition(uow, github_client=mock_github_client, registry=registry)
        assert result is False

        entries = _audit_entries(db_path, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "condition_eval_failed"
        note = json.loads(entries[0]["note"])
        assert note["error_code"] == 404

    def test_github_api_500_returns_false_with_audit(self, registry, db_path):
        from src.orchestration.conditions import evaluate_condition
        uow_id = "uow_ic_500"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json='{"type": "issue_closed", "number": 202}',
        )

        def mock_github_client(issue_number: int) -> dict:
            return {"status_code": 500, "state": None}

        result = evaluate_condition(uow, github_client=mock_github_client, registry=registry)
        assert result is False

        entries = _audit_entries(db_path, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "condition_eval_failed"
        note = json.loads(entries[0]["note"])
        assert note["error_code"] == 500

    def test_no_audit_when_issue_open(self, registry, db_path):
        """False condition (issue open) must not write audit entry."""
        from src.orchestration.conditions import evaluate_condition
        uow_id = "uow_ic_noaudit"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json='{"type": "issue_closed", "number": 300}',
        )

        def mock_github_client(issue_number: int) -> dict:
            return {"status_code": 200, "state": "open"}

        evaluate_condition(uow, github_client=mock_github_client, registry=registry)
        entries = _audit_entries(db_path, uow_id)
        assert entries == []


# ---------------------------------------------------------------------------
# registry_state trigger
# ---------------------------------------------------------------------------

class TestRegistryStateTrigger:
    def test_target_uow_in_specified_state_returns_true(self, registry, db_path):
        from src.orchestration.conditions import evaluate_condition

        # Insert a target UoW into the registry and advance it to "done"
        result = registry.upsert(issue_number=99, title="target UoW")
        target_id = result["id"]
        registry.confirm(target_id)
        registry.set_status_direct(target_id, "done")

        uow_id = "uow_rs_match"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json=json.dumps({"type": "registry_state", "uow_id": target_id, "state": "done"}),
        )
        result = evaluate_condition(uow, registry=registry)
        assert result is True

    def test_target_uow_not_in_specified_state_returns_false(self, registry, db_path):
        from src.orchestration.conditions import evaluate_condition

        result = registry.upsert(issue_number=100, title="pending target")
        target_id = result["id"]
        # UoW stays in "proposed" state

        uow_id = "uow_rs_nomatch"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json=json.dumps({"type": "registry_state", "uow_id": target_id, "state": "done"}),
        )
        result = evaluate_condition(uow, registry=registry)
        assert result is False

    def test_nonexistent_uow_id_returns_false_with_audit(self, registry, db_path):
        from src.orchestration.conditions import evaluate_condition
        uow_id = "uow_rs_notfound"
        nonexistent_id = "uow_does_not_exist_xyz"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json=json.dumps({"type": "registry_state", "uow_id": nonexistent_id, "state": "done"}),
        )
        result = evaluate_condition(uow, registry=registry)
        assert result is False

        entries = _audit_entries(db_path, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "condition_eval_error"
        note = json.loads(entries[0]["note"])
        assert "not found" in note["note"]

    def test_no_audit_when_state_not_matched(self, registry, db_path):
        """False condition (state not matched) must not write audit entry."""
        from src.orchestration.conditions import evaluate_condition
        result = registry.upsert(issue_number=101, title="still proposed")
        target_id = result["id"]

        uow_id = "uow_rs_silent"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json=json.dumps({"type": "registry_state", "uow_id": target_id, "state": "done"}),
        )
        evaluate_condition(uow, registry=registry)
        entries = _audit_entries(db_path, uow_id)
        assert entries == []


# ---------------------------------------------------------------------------
# Malformed JSON trigger
# ---------------------------------------------------------------------------

class TestMalformedTrigger:
    def test_malformed_json_string_returns_true_with_audit(self, registry, db_path):
        """When trigger is stored as a malformed JSON string, return True + audit."""
        from src.orchestration.conditions import evaluate_condition
        uow_id = "uow_malformed"
        # Simulate a UoW that arrived with trigger as a raw string (pre-deserialized)
        uow = {
            "id": uow_id,
            "trigger": "not-valid-json{{{",  # raw string, not dict
            "source_issue_number": 1,
        }
        result = evaluate_condition(uow, registry=registry)
        assert result is True

        entries = _audit_entries(db_path, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "condition_eval_error"
        note = json.loads(entries[0]["note"])
        assert "not valid JSON" in note["note"]

    def test_trigger_as_non_dict_non_string_returns_true_with_audit(self, registry, db_path):
        """When trigger is some unexpected type (e.g., list), treat as error → True + audit."""
        from src.orchestration.conditions import evaluate_condition
        uow_id = "uow_badtype"
        uow = {
            "id": uow_id,
            "trigger": [1, 2, 3],  # unexpected type
            "source_issue_number": 1,
        }
        result = evaluate_condition(uow, registry=registry)
        assert result is True
        entries = _audit_entries(db_path, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "condition_eval_error"


# ---------------------------------------------------------------------------
# Unknown trigger type
# ---------------------------------------------------------------------------

class TestUnknownTriggerType:
    def test_unknown_type_returns_true_with_audit(self, registry, db_path):
        from src.orchestration.conditions import evaluate_condition
        uow_id = "uow_unknown"
        uow = _make_uow(
            uow_id=uow_id,
            trigger_json='{"type": "webhook"}',
        )
        result = evaluate_condition(uow, registry=registry)
        assert result is True

        entries = _audit_entries(db_path, uow_id)
        assert len(entries) == 1
        assert entries[0]["event"] == "condition_eval_error"
        note = json.loads(entries[0]["note"])
        assert "unknown trigger type" in note["note"]
        assert "webhook" in note["note"]
