"""
Exhaustiveness test for _RETURN_REASON_CLASSIFICATIONS in steward.py.

`_RETURN_REASON_CLASSIFICATIONS` is the authoritative mapping from return_reason
strings to classification buckets (normal / blocked / abnormal / error / orphan).
If a return_reason is produced by the system but absent from this dict, the
fallback is _CLASSIFICATION_ERROR — silent mis-routing that is hard to detect.

This test suite guards against that gap by:
1. Asserting that every return_reason value produced by the system (as reported
   via `ReentryPosture` or the additional heartbeat-classified kill types) appears
   as a key in `_RETURN_REASON_CLASSIFICATIONS`.
2. Asserting that every entry in `_RETURN_REASON_CLASSIFICATIONS` maps to a
   valid `ReturnReasonClassification` value — no typos in the classification side.
3. Asserting that the mapping is non-empty (sanity guard against accidental
   wholesale deletion).

Historical context: diagnosing_orphan was missing from the dict and caused a
silent fallthrough to first_execution (revealed by PR #559). orphan_kill_before_start
and orphan_kill_during_execution were added in #963. This test makes any
future omission a test failure.

Named constants from the module are used throughout — no magic literals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.steward import (
    _RETURN_REASON_CLASSIFICATIONS,
    ReentryPosture,
    ReturnReasonClassification,
)

# ---------------------------------------------------------------------------
# Return reasons produced by the system as return_reason values
#
# Not all ReentryPosture values become return_reasons — some are internal
# diagnosis states that never appear as the `return_reason` field written to
# the audit log. The subset below maps to values confirmed to be written as
# return_reasons in steward.py, startup_sweep.py, and registry.py.
#
# The two orphan kill types are produced by the heartbeat-based kill
# classification path (#963) and written as reentry_posture / return_reason
# strings — they are not (yet) members of the ReentryPosture StrEnum but are
# first-class return_reason values.
# ---------------------------------------------------------------------------

# ReentryPosture values that are also used as return_reason strings.
# Excludes: FIRST_EXECUTION, EXECUTION_COMPLETE, STARTUP_SWEEP_POSSIBLY_COMPLETE
# (these are internal diagnosis postures, not written as return_reasons to the log).
_REENTRY_POSTURE_RETURN_REASONS: frozenset[str] = frozenset({
    ReentryPosture.STALL_DETECTED,
    ReentryPosture.CRASHED_NO_OUTPUT,
    ReentryPosture.CRASHED_ZERO_BYTES,
    ReentryPosture.CRASHED_OUTPUT_REF_MISSING,
    ReentryPosture.EXECUTION_FAILED,
    ReentryPosture.EXECUTOR_ORPHAN,
    ReentryPosture.DIAGNOSING_ORPHAN,
    ReentryPosture.EXECUTING_ORPHAN,
})

# Additional return_reason string literals produced by the system that are not
# (yet) in the ReentryPosture enum. Each is guarded by its own named constant
# so that renaming them in the future produces a NameError here rather than a
# silent test pass.
_ORPHAN_KILL_BEFORE_START: str = "orphan_kill_before_start"
_ORPHAN_KILL_DURING_EXECUTION: str = "orphan_kill_during_execution"
_OBSERVATION_COMPLETE: str = "observation_complete"
_NEEDS_STEWARD_REVIEW: str = "needs_steward_review"
_BLOCKED: str = "blocked"
_TIMEOUT: str = "timeout"

_ADDITIONAL_RETURN_REASONS: frozenset[str] = frozenset({
    _ORPHAN_KILL_BEFORE_START,
    _ORPHAN_KILL_DURING_EXECUTION,
    _OBSERVATION_COMPLETE,
    _NEEDS_STEWARD_REVIEW,
    _BLOCKED,
    _TIMEOUT,
})

# The complete set of return_reason values the system is known to produce.
ALL_KNOWN_RETURN_REASONS: frozenset[str] = (
    _REENTRY_POSTURE_RETURN_REASONS | _ADDITIONAL_RETURN_REASONS
)


class TestReturnReasonClassificationsExhaustiveness:
    """
    _RETURN_REASON_CLASSIFICATIONS must cover every return_reason the system produces.

    A gap means the fallback classification (_CLASSIFICATION_ERROR) silently
    mis-routes UoWs. These tests make any future omission a loud failure.
    """

    def test_every_known_return_reason_has_a_classification(self) -> None:
        """Every return_reason the system produces appears as a key in the mapping."""
        missing = ALL_KNOWN_RETURN_REASONS - _RETURN_REASON_CLASSIFICATIONS.keys()
        assert not missing, (
            f"Return reasons produced by the system but missing from "
            f"_RETURN_REASON_CLASSIFICATIONS: {sorted(missing)}\n"
            f"Add each missing reason to _RETURN_REASON_CLASSIFICATIONS in steward.py."
        )

    def test_every_classification_value_is_a_valid_ReturnReasonClassification(
        self,
    ) -> None:
        """Every value in _RETURN_REASON_CLASSIFICATIONS is a valid classification enum value."""
        valid_classifications = {v.value for v in ReturnReasonClassification}
        invalid = {
            reason: clf
            for reason, clf in _RETURN_REASON_CLASSIFICATIONS.items()
            if clf not in valid_classifications
        }
        assert not invalid, (
            f"_RETURN_REASON_CLASSIFICATIONS contains invalid classification values: "
            f"{invalid}\n"
            f"Valid values are: {sorted(valid_classifications)}"
        )

    def test_classifications_mapping_is_not_empty(self) -> None:
        """_RETURN_REASON_CLASSIFICATIONS must be non-empty (sanity guard)."""
        assert len(_RETURN_REASON_CLASSIFICATIONS) > 0, (
            "_RETURN_REASON_CLASSIFICATIONS is empty — the mapping was accidentally cleared."
        )

    def test_orphan_kill_types_are_classified_as_orphan(self) -> None:
        """orphan_kill_before_start and orphan_kill_during_execution classify as orphan."""
        assert _RETURN_REASON_CLASSIFICATIONS[_ORPHAN_KILL_BEFORE_START] == ReturnReasonClassification.ORPHAN
        assert _RETURN_REASON_CLASSIFICATIONS[_ORPHAN_KILL_DURING_EXECUTION] == ReturnReasonClassification.ORPHAN

    def test_executor_orphan_variants_are_classified_as_orphan(self) -> None:
        """All executor/diagnosing/executing orphan variants classify as orphan."""
        for posture in (
            ReentryPosture.EXECUTOR_ORPHAN,
            ReentryPosture.DIAGNOSING_ORPHAN,
            ReentryPosture.EXECUTING_ORPHAN,
        ):
            classification = _RETURN_REASON_CLASSIFICATIONS[posture.value]
            assert classification == ReturnReasonClassification.ORPHAN, (
                f"{posture.value!r} should classify as orphan, got {classification!r}"
            )

    def test_crash_types_are_classified_as_error(self) -> None:
        """crashed_* postures and execution_failed classify as error."""
        for posture in (
            ReentryPosture.CRASHED_NO_OUTPUT,
            ReentryPosture.CRASHED_ZERO_BYTES,
            ReentryPosture.CRASHED_OUTPUT_REF_MISSING,
            ReentryPosture.EXECUTION_FAILED,
        ):
            classification = _RETURN_REASON_CLASSIFICATIONS[posture.value]
            assert classification == ReturnReasonClassification.ERROR, (
                f"{posture.value!r} should classify as error, got {classification!r}"
            )

    def test_observation_complete_and_needs_steward_review_classify_as_normal(
        self,
    ) -> None:
        """observation_complete and needs_steward_review classify as normal."""
        assert _RETURN_REASON_CLASSIFICATIONS[_OBSERVATION_COMPLETE] == ReturnReasonClassification.NORMAL
        assert _RETURN_REASON_CLASSIFICATIONS[_NEEDS_STEWARD_REVIEW] == ReturnReasonClassification.NORMAL

    def test_blocked_classifies_as_blocked(self) -> None:
        """blocked classifies as blocked."""
        assert _RETURN_REASON_CLASSIFICATIONS[_BLOCKED] == ReturnReasonClassification.BLOCKED

    def test_timeout_and_stall_detect_classify_as_abnormal(self) -> None:
        """timeout and stall_detected classify as abnormal."""
        assert _RETURN_REASON_CLASSIFICATIONS[_TIMEOUT] == ReturnReasonClassification.ABNORMAL
        assert _RETURN_REASON_CLASSIFICATIONS[ReentryPosture.STALL_DETECTED] == ReturnReasonClassification.ABNORMAL
