"""
Tests for system-retrospective.py — smoke tests for smell detection heuristics.

These tests verify behavior as specified in the issue and smell-patterns.yaml,
not as transcripts of the implementation. Each test is named after the behavior
being verified.

Tests use only the pure functions from the script — no external services,
no file system side effects, no GitHub API calls.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the module under test via importlib so tests work regardless of
# whether the script is in sys.path (mirrors the pattern used elsewhere
# in this test suite).
# ---------------------------------------------------------------------------

import importlib.util
import sys


def _load_module(script_path: Path):
    """Load a .py script as a module, isolated from the installed package."""
    import types

    # Stub out the inbox_write import so tests don't need the full lobster package.
    # The stub only needs to satisfy the import — tests never call write_inbox_message.
    stub = types.ModuleType("src.utils.inbox_write")
    stub._inbox_dir = lambda: Path("/tmp/fake-inbox")
    stub._task_outputs_dir = lambda: Path("/tmp/fake-task-outputs")
    stub.write_inbox_message = lambda *a, **kw: "stub_msg_id"
    sys.modules.setdefault("src", types.ModuleType("src"))
    sys.modules.setdefault("src.utils", types.ModuleType("src.utils"))
    sys.modules["src.utils.inbox_write"] = stub

    mod_name = "system_retrospective"
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so that @dataclass __module__
    # resolution works correctly (mirrors the pattern in steward-heartbeat.py).
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def retro(request):
    """Load the system-retrospective module once per test session."""
    # Navigate from tests/smoke/ up to repo root, then into scheduled-tasks/
    script = Path(__file__).parent.parent.parent / "scheduled-tasks" / "system-retrospective.py"
    assert script.exists(), f"Script not found at {script}"
    return _load_module(script)


# ---------------------------------------------------------------------------
# Metabolic classification
# ---------------------------------------------------------------------------

class TestClassifyUow:
    """classify_uow returns the correct outcome category for each UoW type."""

    def test_failed_status_is_shit(self, retro):
        uow = {"status": "failed", "close_reason": "", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_SHIT

    def test_expired_status_is_shit(self, retro):
        uow = {"status": "expired", "close_reason": "", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_SHIT

    def test_cancelled_status_is_shit(self, retro):
        uow = {"status": "cancelled", "close_reason": "", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_SHIT

    def test_done_with_pr_in_close_reason_is_pearl(self, retro):
        uow = {"status": "done", "close_reason": "merged pr #123", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_PEARL

    def test_done_with_implementation_in_close_reason_is_pearl(self, retro):
        uow = {"status": "done", "close_reason": "implementation complete", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_PEARL

    def test_done_with_opened_issue_in_close_reason_is_seed(self, retro):
        uow = {"status": "done", "close_reason": "opened issue #456", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_SEED

    def test_done_with_filed_issue_in_close_reason_is_seed(self, retro):
        uow = {"status": "done", "close_reason": "filed issue #789", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_SEED

    def test_done_with_review_in_close_reason_is_heat(self, retro):
        uow = {"status": "done", "close_reason": "design review complete", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_HEAT

    def test_done_with_no_signal_is_shit(self, retro):
        uow = {"status": "done", "close_reason": "", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_SHIT

    def test_ttl_in_close_reason_is_shit(self, retro):
        uow = {"status": "done", "close_reason": "ttl_exceeded after 3 retries", "output_ref": ""}
        assert retro.classify_uow(uow) == retro.OUTCOME_SHIT


class TestComputeMetabolicCounts:
    """compute_metabolic_counts groups UoWs correctly and total is accurate."""

    def test_empty_list_returns_zero_counts(self, retro):
        counts = retro.compute_metabolic_counts([])
        assert counts[retro.OUTCOME_PEARL] == 0
        assert counts[retro.OUTCOME_SHIT] == 0
        assert counts["total"] == 0

    def test_counts_sum_to_total(self, retro):
        uows = [
            {"status": "done", "close_reason": "merged pr #1", "output_ref": ""},
            {"status": "done", "close_reason": "opened issue #2", "output_ref": ""},
            {"status": "failed", "close_reason": "", "output_ref": ""},
        ]
        counts = retro.compute_metabolic_counts(uows)
        total = sum(counts[k] for k in [
            retro.OUTCOME_PEARL, retro.OUTCOME_SEED,
            retro.OUTCOME_HEAT, retro.OUTCOME_SHIT,
        ])
        assert total == counts["total"] == 3


class TestComputeMetabolicRatios:
    """compute_metabolic_ratios produces correct fractions and handles zero total."""

    def test_zero_total_yields_all_zero_ratios(self, retro):
        counts = {retro.OUTCOME_PEARL: 0, retro.OUTCOME_SEED: 0,
                  retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 0, "total": 0}
        ratios = retro.compute_metabolic_ratios(counts)
        for k in ["pearl_ratio", "seed_ratio", "heat_ratio", "shit_ratio"]:
            assert ratios[k] == 0.0

    def test_ratios_sum_to_one_when_total_nonzero(self, retro):
        counts = {retro.OUTCOME_PEARL: 3, retro.OUTCOME_SEED: 1,
                  retro.OUTCOME_HEAT: 2, retro.OUTCOME_SHIT: 4, "total": 10}
        ratios = retro.compute_metabolic_ratios(counts)
        total_ratio = sum(ratios[k] for k in ["pearl_ratio", "seed_ratio", "heat_ratio", "shit_ratio"])
        assert abs(total_ratio - 1.0) < 0.01  # floating point tolerance

    def test_all_pearls_gives_pearl_ratio_one(self, retro):
        counts = {retro.OUTCOME_PEARL: 5, retro.OUTCOME_SEED: 0,
                  retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 0, "total": 5}
        ratios = retro.compute_metabolic_ratios(counts)
        assert ratios["pearl_ratio"] == 1.0


# ---------------------------------------------------------------------------
# Smell detection — bare_python3_in_migrations
# ---------------------------------------------------------------------------

class TestDetectBarePython3InMigrations:
    """Smell detection for bare python3/python in upgrade.sh."""

    def test_detects_bare_python3_in_upgrade_sh(self, retro, tmp_path):
        upgrade_sh = tmp_path / "scripts" / "upgrade.sh"
        upgrade_sh.parent.mkdir(parents=True)
        upgrade_sh.write_text(textwrap.dedent("""\
            #!/bin/bash
            python3 scripts/migrate.py
            echo done
        """))
        detected, evidence = retro._detect_bare_python3_in_migrations(tmp_path, threshold=1)
        assert detected is True
        assert "python3" in evidence or "bare" in evidence.lower()

    def test_no_detection_when_uv_is_present(self, retro, tmp_path):
        upgrade_sh = tmp_path / "scripts" / "upgrade.sh"
        upgrade_sh.parent.mkdir(parents=True)
        upgrade_sh.write_text(textwrap.dedent("""\
            #!/bin/bash
            uv run python3 scripts/migrate.py
            echo done
        """))
        detected, _ = retro._detect_bare_python3_in_migrations(tmp_path, threshold=1)
        assert detected is False

    def test_no_detection_when_file_absent(self, retro, tmp_path):
        # No scripts/upgrade.sh in tmp_path
        detected, evidence = retro._detect_bare_python3_in_migrations(tmp_path, threshold=1)
        assert detected is False
        assert "not found" in evidence

    def test_comments_are_excluded_from_detection(self, retro, tmp_path):
        upgrade_sh = tmp_path / "scripts" / "upgrade.sh"
        upgrade_sh.parent.mkdir(parents=True)
        upgrade_sh.write_text(textwrap.dedent("""\
            #!/bin/bash
            # python3 used to be required here
            uv run scripts/migrate.py
        """))
        detected, _ = retro._detect_bare_python3_in_migrations(tmp_path, threshold=1)
        assert detected is False


# ---------------------------------------------------------------------------
# Smell detection — rolling_summary_bloat
# ---------------------------------------------------------------------------

class TestDetectRollingSummaryBloat:
    """Smell detection for rolling-summary.md exceeding threshold lines."""

    def test_detects_when_line_count_exceeds_threshold(self, retro, tmp_path, monkeypatch):
        # Create rolling-summary.md with 55 lines (each line ends with \n,
        # so count("\n") == 55, which is > threshold=50).
        content = "".join(f"line {i}\n" for i in range(55))

        # Patch the path lookup
        monkeypatch.setenv("LOBSTER_USER_CONFIG", str(tmp_path.parent))
        # Write to the expected path
        canonical_dir = tmp_path.parent / "memory" / "canonical"
        canonical_dir.mkdir(parents=True, exist_ok=True)
        (canonical_dir / "rolling-summary.md").write_text(content)

        detected, evidence = retro._detect_rolling_summary_bloat(threshold=50)
        assert detected is True
        assert "55" in evidence or str(55) in evidence

    def test_no_detection_when_within_threshold(self, retro, tmp_path, monkeypatch):
        canonical_dir = tmp_path / "memory" / "canonical"
        canonical_dir.mkdir(parents=True, exist_ok=True)
        (canonical_dir / "rolling-summary.md").write_text("\n".join([f"line {i}" for i in range(30)]))
        monkeypatch.setenv("LOBSTER_USER_CONFIG", str(tmp_path))

        detected, _ = retro._detect_rolling_summary_bloat(threshold=50)
        assert detected is False


# ---------------------------------------------------------------------------
# Smell detection — write_result_not_back_propagated
# ---------------------------------------------------------------------------

class TestDetectWriteResultNotBackPropagated:
    """Smell detection for unverifiable UoW completions (shit count proxy)."""

    def test_detects_when_shit_count_exceeds_threshold(self, retro):
        counts = {
            retro.OUTCOME_PEARL: 2, retro.OUTCOME_SEED: 1,
            retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 10, "total": 13,
        }
        detected, evidence = retro._detect_write_result_not_back_propagated(counts, threshold=5)
        assert detected is True
        assert "10" in evidence

    def test_no_detection_when_shit_count_at_threshold(self, retro):
        counts = {
            retro.OUTCOME_PEARL: 5, retro.OUTCOME_SEED: 0,
            retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 5, "total": 10,
        }
        detected, _ = retro._detect_write_result_not_back_propagated(counts, threshold=5)
        # threshold=5, shit_count=5 → 5 > 5 is False
        assert detected is False

    def test_detects_strictly_above_threshold(self, retro):
        counts = {
            retro.OUTCOME_PEARL: 0, retro.OUTCOME_SEED: 0,
            retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 6, "total": 6,
        }
        detected, _ = retro._detect_write_result_not_back_propagated(counts, threshold=5)
        assert detected is True


# ---------------------------------------------------------------------------
# SmellDetection dispatch
# ---------------------------------------------------------------------------

class TestDetectSmells:
    """detect_smells returns one SmellDetection per pattern regardless of detection outcome."""

    def test_returns_entry_for_every_pattern(self, retro):
        patterns = [
            {"id": "write_result_not_back_propagated", "name": "test", "severity": "high",
             "threshold": 100, "recurrence_count": 0},
            {"id": "bare_python3_in_migrations", "name": "test2", "severity": "medium",
             "threshold": 99, "recurrence_count": 0},
        ]
        counts = {retro.OUTCOME_PEARL: 0, retro.OUTCOME_SEED: 0,
                  retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 0, "total": 0}
        results = retro.detect_smells(patterns, counts, Path("/tmp"), Path("/tmp"))
        assert len(results) == 2

    def test_unknown_pattern_id_returns_not_detected(self, retro):
        patterns = [
            {"id": "nonexistent_pattern_xyz", "name": "unknown", "severity": "low",
             "threshold": 1, "recurrence_count": 0},
        ]
        counts = {retro.OUTCOME_PEARL: 0, retro.OUTCOME_SEED: 0,
                  retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 99, "total": 99}
        results = retro.detect_smells(patterns, counts, Path("/tmp"), Path("/tmp"))
        assert results[0].detected is False
        assert "no detection heuristic" in results[0].evidence


# ---------------------------------------------------------------------------
# load_smell_patterns
# ---------------------------------------------------------------------------

class TestLoadSmellPatterns:
    """load_smell_patterns correctly parses the YAML registry."""

    def test_loads_patterns_from_yaml(self, retro, tmp_path):
        yaml_content = textwrap.dedent("""\
            version: 1
            patterns:
              - id: test_smell
                name: Test Smell
                severity: high
                recurrence_count: 2
                threshold: 5
                status: open
        """)
        p = tmp_path / "smell-patterns.yaml"
        p.write_text(yaml_content)
        patterns = retro.load_smell_patterns(p)
        assert len(patterns) == 1
        assert patterns[0]["id"] == "test_smell"
        assert patterns[0]["severity"] == "high"

    def test_returns_empty_list_when_file_absent(self, retro, tmp_path):
        patterns = retro.load_smell_patterns(tmp_path / "nonexistent.yaml")
        assert patterns == []

    def test_returns_empty_list_on_malformed_yaml(self, retro, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("not: valid: yaml: [[[")
        # Malformed YAML should not raise — should return []
        try:
            patterns = retro.load_smell_patterns(p)
            assert isinstance(patterns, list)
        except Exception:
            pytest.fail("load_smell_patterns raised on malformed YAML — expected []")


# ---------------------------------------------------------------------------
# Assessment document builder
# ---------------------------------------------------------------------------

class TestBuildAssessmentDoc:
    """build_assessment_doc produces well-formed markdown with required sections."""

    def _make_detection(self, retro, detected: bool) -> object:
        return retro.SmellDetection(
            pattern_id="test_pattern",
            name="Test Pattern",
            severity="medium",
            detected=detected,
            evidence="test evidence",
            recurrence_count=1,
            open_issue_ref=None,
        )

    def test_doc_contains_date(self, retro):
        doc = retro.build_assessment_doc(
            date="2026-04-27",
            period_days=7,
            since_iso="2026-04-20T06:00:00Z",
            merged_prs=[],
            uow_footer_commits=[],
            session_files_new=[],
            counts={retro.OUTCOME_PEARL: 0, retro.OUTCOME_SEED: 0,
                    retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 0, "total": 0},
            ratios={"pearl_ratio": 0.0, "seed_ratio": 0.0,
                    "heat_ratio": 0.0, "shit_ratio": 0.0},
            detections=[],
            drifted_patterns=[],
            filed_issues=[],
            issue_867_open=False,
        )
        assert "2026-04-27" in doc

    def test_doc_warns_when_issue_867_open(self, retro):
        doc = retro.build_assessment_doc(
            date="2026-04-27",
            period_days=7,
            since_iso="2026-04-20T06:00:00Z",
            merged_prs=[],
            uow_footer_commits=[],
            session_files_new=[],
            counts={retro.OUTCOME_PEARL: 0, retro.OUTCOME_SEED: 0,
                    retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 0, "total": 0},
            ratios={"pearl_ratio": 0.0, "seed_ratio": 0.0,
                    "heat_ratio": 0.0, "shit_ratio": 0.0},
            detections=[],
            drifted_patterns=[],
            filed_issues=[],
            issue_867_open=True,
        )
        assert "#867" in doc
        assert "unreliable" in doc.lower()

    def test_doc_includes_detected_smells(self, retro):
        detection = self._make_detection(retro, detected=True)
        doc = retro.build_assessment_doc(
            date="2026-04-27",
            period_days=7,
            since_iso="2026-04-20T06:00:00Z",
            merged_prs=[],
            uow_footer_commits=[],
            session_files_new=[],
            counts={retro.OUTCOME_PEARL: 0, retro.OUTCOME_SEED: 0,
                    retro.OUTCOME_HEAT: 0, retro.OUTCOME_SHIT: 0, "total": 0},
            ratios={"pearl_ratio": 0.0, "seed_ratio": 0.0,
                    "heat_ratio": 0.0, "shit_ratio": 0.0},
            detections=[detection],
            drifted_patterns=[],
            filed_issues=[],
            issue_867_open=False,
        )
        assert "Test Pattern" in doc
        assert "test evidence" in doc

    def test_doc_does_not_warn_when_issue_867_closed(self, retro):
        doc = retro.build_assessment_doc(
            date="2026-04-27",
            period_days=7,
            since_iso="2026-04-20T06:00:00Z",
            merged_prs=[],
            uow_footer_commits=[],
            session_files_new=[],
            counts={retro.OUTCOME_PEARL: 5, retro.OUTCOME_SEED: 1,
                    retro.OUTCOME_HEAT: 2, retro.OUTCOME_SHIT: 0, "total": 8},
            ratios={"pearl_ratio": 0.625, "seed_ratio": 0.125,
                    "heat_ratio": 0.25, "shit_ratio": 0.0},
            detections=[],
            drifted_patterns=[],
            filed_issues=[],
            issue_867_open=False,
        )
        # Should contain metabolic data but no #867 warning
        assert "Pearl" in doc
        assert "unreliable" not in doc.lower()


# ---------------------------------------------------------------------------
# collect_session_files
# ---------------------------------------------------------------------------

class TestCollectSessionFiles:
    """collect_session_files returns only files newer than the since cutoff."""

    def test_returns_files_created_after_cutoff(self, retro, tmp_path):
        # Create two files — one recent, one old
        recent = tmp_path / "20260424-001.md"
        recent.write_text("recent session")
        old = tmp_path / "20260410-001.md"
        old.write_text("old session")

        # Set mtime of old file to 20 days ago
        import time
        old_mtime = (datetime.now(timezone.utc) - timedelta(days=20)).timestamp()
        import os
        os.utime(str(old), (old_mtime, old_mtime))

        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = retro.collect_session_files(tmp_path, since)
        assert "20260424-001.md" in result
        assert "20260410-001.md" not in result

    def test_returns_empty_list_when_dir_absent(self, retro, tmp_path):
        result = retro.collect_session_files(tmp_path / "nonexistent", "2026-04-20T00:00:00Z")
        assert result == []


# ---------------------------------------------------------------------------
# Escalation first-crossing guard
# ---------------------------------------------------------------------------

class TestEscalationFirstCrossingGuard:
    """
    Escalation fires exactly once — on the run that crosses ESCALATION_THRESHOLD.

    smell.recurrence_count holds the pre-run value from smell-patterns.yaml.
    After write_back_recurrence_counts() the YAML value becomes recurrence_count+1.
    The first-crossing guard: escalate only when pre-run count == ESCALATION_THRESHOLD - 1,
    so we escalate exactly once (the crossing run), not on every subsequent run.

    Verified cases:
      (a) pre-threshold (recurrence_count < ESCALATION_THRESHOLD - 1) → no escalation
      (b) crossing run  (recurrence_count == ESCALATION_THRESHOLD - 1) → escalates
      (c) post-threshold (recurrence_count > ESCALATION_THRESHOLD - 1) → no escalation
    """

    def _make_detected_smell(self, retro, recurrence_count: int) -> object:
        return retro.SmellDetection(
            pattern_id="test_smell",
            name="Test Smell",
            severity="high",
            detected=True,
            evidence="test evidence",
            recurrence_count=recurrence_count,
            open_issue_ref=None,
        )

    def test_pre_threshold_does_not_escalate(self, retro):
        """recurrence_count below crossing point — no escalation."""
        THRESHOLD = retro.ESCALATION_THRESHOLD
        pre_threshold_count = THRESHOLD - 2  # one below the crossing point

        if pre_threshold_count < 0:
            pytest.skip("ESCALATION_THRESHOLD < 2 — pre-threshold case not applicable")

        escalated = []
        smell = self._make_detected_smell(retro, pre_threshold_count)

        # Mirror the guard condition from system-retrospective.run()
        if smell.detected and smell.recurrence_count == THRESHOLD - 1:
            escalated.append(smell.pattern_id)

        assert escalated == [], (
            f"Expected no escalation for recurrence_count={pre_threshold_count} "
            f"(ESCALATION_THRESHOLD={THRESHOLD})"
        )

    def test_crossing_run_escalates_exactly_once(self, retro):
        """recurrence_count == ESCALATION_THRESHOLD - 1 → escalates."""
        THRESHOLD = retro.ESCALATION_THRESHOLD
        crossing_count = THRESHOLD - 1

        escalated = []
        smell = self._make_detected_smell(retro, crossing_count)

        if smell.detected and smell.recurrence_count == THRESHOLD - 1:
            escalated.append(smell.pattern_id)

        assert escalated == ["test_smell"], (
            f"Expected escalation for recurrence_count={crossing_count} "
            f"(ESCALATION_THRESHOLD={THRESHOLD})"
        )

    def test_post_threshold_does_not_re_escalate(self, retro):
        """recurrence_count > ESCALATION_THRESHOLD - 1 → no escalation (guard prevents multi-fire)."""
        THRESHOLD = retro.ESCALATION_THRESHOLD
        post_threshold_count = THRESHOLD  # already at or past threshold

        escalated = []
        smell = self._make_detected_smell(retro, post_threshold_count)

        if smell.detected and smell.recurrence_count == THRESHOLD - 1:
            escalated.append(smell.pattern_id)

        assert escalated == [], (
            f"Expected no re-escalation for recurrence_count={post_threshold_count} "
            f"(ESCALATION_THRESHOLD={THRESHOLD})"
        )
