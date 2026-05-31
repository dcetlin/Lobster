"""Tests for src/orchestration/metabolic_router.py — one positive + one negative per branch."""

import pytest

from src.orchestration.metabolic_router import MetabolicClass, classify_result


# ---------------------------------------------------------------------------
# Rule 1: HEAT — short text with no artifact signals
# ---------------------------------------------------------------------------

def test_heat_short_no_artifacts():
    r = classify_result("Done. Nothing found.", "misc-job", {})
    assert r.cls == MetabolicClass.HEAT


def test_heat_not_triggered_when_url_present():
    # < 120 chars but contains a URL → artifact signal present → not HEAT
    r = classify_result("See https://github.com/x/y/pull/1 for details.", "misc-job", {})
    assert r.cls != MetabolicClass.HEAT


# ---------------------------------------------------------------------------
# Rule 2: PEARL (strong) — oracle-approved review result
# ---------------------------------------------------------------------------

def test_pearl_oracle_approved():
    text = "VERDICT: APPROVED\n\n" + "x" * 200  # long enough to exceed rule 1
    r = classify_result(text, "review-pr-999", {})
    assert r.cls == MetabolicClass.PEARL


def test_pearl_oracle_approved_not_triggered_wrong_task_id():
    text = "VERDICT: APPROVED\n\n" + "x" * 200
    r = classify_result(text, "deploy-pr-999", {})
    # task_id does not start with "review-" → rule 2 does not fire
    assert r.cls != MetabolicClass.PEARL or r.rationale != "oracle-approved review result"


# ---------------------------------------------------------------------------
# Rule 3: PEARL (artifact-rich) — long + multiple artifact refs
# ---------------------------------------------------------------------------

def test_pearl_artifact_rich():
    text = (
        "Implementation complete. See PR #1234 and issue #567 for context. "
        "Files modified: /home/lobster/lobster/src/x.py and /home/lobster/lobster/src/y.py. "
    ) + "x" * 700  # push over 800 chars
    r = classify_result(text, "engineer-task", {})
    assert r.cls == MetabolicClass.PEARL


def test_pearl_artifact_rich_not_triggered_single_artifact():
    # Only one artifact signal (URL only — no overlapping patterns) — rule 3 requires 2+
    text = "See https://github.com/x/y for context. " + "x" * 790
    r = classify_result(text, "engineer-task", {})
    # Should not hit rule 3 (single artifact)
    assert not (r.cls == MetabolicClass.PEARL and r.rationale == "substantial result with multiple artifact references")


# ---------------------------------------------------------------------------
# Rule 4: SEED — forward-trajectory language, compact
# ---------------------------------------------------------------------------

def test_seed_open_question():
    # > 120 chars (skips HEAT) and < 600 chars (SEED fires before JUICE)
    text = (
        "Could be worth exploring whether the cache TTL should be shorter for this path. "
        "The current value may cause unnecessary churn for frequent callers."
    )
    assert len(text) > 120 and len(text) < 600
    r = classify_result(text, "misc-explore", {})
    assert r.cls == MetabolicClass.SEED


def test_seed_not_triggered_when_too_long():
    # > 600 chars with forward language → rule 4 does not fire
    r = classify_result("could " + "x" * 600, "misc-explore", {})
    assert r.cls != MetabolicClass.SEED


# ---------------------------------------------------------------------------
# Rule 5: SHIT — failure vocabulary, not oracle-approved
# ---------------------------------------------------------------------------

def test_shit_failure():
    # > 120 chars so rule 1 (HEAT) does not fire first
    text = "Task failed: traceback on line 42. " + "x" * 100
    assert len(text) >= 120
    r = classify_result(text, "some-task", {})
    assert r.cls == MetabolicClass.SHIT


def test_shit_not_triggered_when_oracle_approved():
    # "failed" in text but VERDICT: APPROVED present → rule 5 does not fire
    r = classify_result(
        "VERDICT: APPROVED\nSome tests failed in earlier cycles but this run passed.\n" + "x" * 200,
        "review-task",
        {},
    )
    assert r.cls != MetabolicClass.SHIT


# ---------------------------------------------------------------------------
# Rule 6: JUICE — completed + forward trajectory + artifact signals
# ---------------------------------------------------------------------------

def test_juice_completed_with_open_threads():
    # > 600 chars so rule 4 (SEED) does not fire; has forward language + artifact signals
    base = (
        "PR #1234 opened and merged. Could follow up with a cache invalidation improvement "
        "next sprint. See https://github.com/x/y/pull/1234 for the diff. "
    )
    text = base + "x" * 600  # total > 600 chars; skips SEED (< 600 threshold)
    assert len(text) > 600
    r = classify_result(text, "engineer-task", {})
    assert r.cls == MetabolicClass.JUICE


def test_juice_not_triggered_without_artifacts():
    # forward language + long but no artifact signals
    text = "could explore this further. might be worth it. next step is unclear. " + "x" * 400
    r = classify_result(text, "misc-task", {})
    assert r.cls != MetabolicClass.JUICE


# ---------------------------------------------------------------------------
# Rule 7: MIXED — default fallback
# ---------------------------------------------------------------------------

def test_mixed_fallback():
    # 300 chars of neutral filler — no artifact signals, no failure, no forward language
    r = classify_result("x" * 300, "ambiguous-task", {})
    assert r.cls == MetabolicClass.MIXED


def test_mixed_not_triggered_when_another_rule_fires():
    r = classify_result("Done.", "misc-job", {})
    # Short + no artifacts → HEAT, not MIXED
    assert r.cls != MetabolicClass.MIXED


# ---------------------------------------------------------------------------
# Structural contracts
# ---------------------------------------------------------------------------

def test_all_results_are_provisional():
    cases = [
        ("Done.", "misc"),
        ("VERDICT: APPROVED\n" + "x" * 200, "review-task"),
        ("failed: traceback", "some-task"),
        ("could be worth exploring", "explore"),
        ("x" * 300, "ambiguous"),
    ]
    for text, task_id in cases:
        r = classify_result(text, task_id, {})
        assert r.provisional is True, f"Expected provisional=True for text={text[:30]!r}"


def test_confidence_in_range():
    r = classify_result("Done.", "misc", {})
    assert 0.0 <= r.confidence <= 1.0
