"""
Juice sensing for the WOS Steward dispatch layer.

Juice, as defined in philosophy/frontier/metabolic-juice.md, is pre-metabolic
aliveness without determinate form. In the WOS pipeline, it manifests as a
quality on the steward prescription — a signal that a thread has live
generative momentum and should be prioritized for dispatch.

Design principles (from the juice-uow-integration-spec.md):

1. Juice is re-evaluated on every steward cycle (Option C). It is never
   automatically persisted from one cycle to the next — the steward must
   re-assess it each time. This prevents stale juice from accumulating.

2. juice_rationale is mandatory when juice is asserted. A prescription that
   cannot name *what is alive and why* should not assert juice.

3. Juice is a soft priority signal (slot acquisition, not preemption). High-
   juice UoWs go to the front of the dispatch queue; they do not preempt
   in-flight UoWs.

4. All logic here is in pure functions — no side effects, no DB writes.
   The caller (steward.py) is responsible for writing juice_quality and
   juice_rationale back to the registry.

Observable signals that distinguish juice from indeterminate (issue #889):
  - oracle_approval_rate: ratio of oracle_approved to total execution cycles
    in the UoW's audit history. High rate → execution is consistently passing
    oracle review → thread is productive, not stuck.
  - completion_rate: ratio of done/pearl outcomes to total execution outcomes.
    High rate → UoW threads in this family are closing productively.
  - completed_prerequisites: count of completed prerequisite UoWs (done/pearl).
    More completed prerequisites → clearer path for this UoW.
  - recent_oracle_approved: whether the most recent oracle verdict was approved.
    Recency matters — a recent approval is stronger signal than a historical one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.orchestration.registry import Registry, UoW

log = logging.getLogger("juice")


# ---------------------------------------------------------------------------
# Named threshold constants — derived from spec signals.
# All thresholds reference these constants so tests and implementation agree.
# ---------------------------------------------------------------------------

# Minimum oracle approval rate to qualify for juice (50% of execution cycles
# passed oracle review). Prescriptions below this rate are more often rejected
# than approved — not a productive thread.
JUICE_MIN_ORACLE_APPROVAL_RATE: float = 0.5

# Minimum number of recent oracle approvals to trust the rate signal.
# With fewer executions, the rate is too noisy to be meaningful.
JUICE_MIN_ORACLE_SAMPLE_SIZE: int = 2

# Minimum completion rate (done outcomes / total executions) to qualify.
# At least 30% of executions on related threads should have resolved productively.
JUICE_MIN_COMPLETION_RATE: float = 0.3

# Number of completed prerequisites that adds a positive signal.
# At least 1 completed prerequisite means the UoW has some cleared ground.
JUICE_PREREQUISITE_POSITIVE_THRESHOLD: int = 1

# Weight contributions for each signal to the aggregate juice score.
# Scores are in [0.0, 1.0]. The aggregate score is a weighted average.
# Weights sum to 1.0 — no normalization needed after the weighted sum.
_WEIGHT_ORACLE_RATE = 0.45
_WEIGHT_COMPLETION_RATE = 0.35
_WEIGHT_PREREQUISITES = 0.20


# ---------------------------------------------------------------------------
# Input/output value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JuiceSignals:
    """
    Observable signals extracted from audit history for a UoW.

    All fields are derived from public registry data — no private DB access.
    This type is pure data; it carries no methods that produce side effects.

    Fields:
        oracle_approval_count: Number of audit entries with event='oracle_approved'.
        total_execution_cycles: Number of audit entries with event='execution_complete'.
        done_outcome_count: Number of audit entries with to_status='done'.
        completed_prerequisite_count: Number of completed prerequisite UoWs.
        recent_oracle_approved: True if the most recent oracle verdict was approved.
    """
    oracle_approval_count: int
    total_execution_cycles: int
    done_outcome_count: int
    completed_prerequisite_count: int
    recent_oracle_approved: bool


@dataclass(frozen=True)
class JuiceAssessment:
    """
    Result of a juice evaluation for a single UoW.

    Fields:
        score: Float in [0.0, 1.0]. Higher = more juice evidence.
            None when there is insufficient data to score (e.g. no execution history).
        has_juice: True when score >= the juice threshold AND rationale is non-empty.
        rationale: Machine-generated prose explaining the juice assessment.
            Non-empty only when has_juice=True.
    """
    score: float | None
    has_juice: bool
    rationale: str


# ---------------------------------------------------------------------------
# Signal extraction (pure functions)
# ---------------------------------------------------------------------------

def _extract_signals(audit_entries: list[dict[str, Any]]) -> JuiceSignals:
    """
    Extract observable juice signals from a UoW's audit log.

    Pure function — reads from the provided list, no DB access.

    Args:
        audit_entries: List of audit log dicts for a single UoW, as returned
            by _fetch_audit_entries() in steward.py. Each dict has at least
            'event', 'to_status', and optionally 'note' keys.

    Returns:
        JuiceSignals with extracted counts and flags.
    """
    oracle_approval_count = 0
    total_execution_cycles = 0
    done_outcome_count = 0
    recent_oracle_approved = False
    last_oracle_verdict: bool | None = None

    for entry in audit_entries:
        event = entry.get("event", "")
        to_status = entry.get("to_status")

        if event == "oracle_approved":
            oracle_approval_count += 1
            last_oracle_verdict = True
        elif event == "oracle_rejected":
            last_oracle_verdict = False

        if event == "execution_complete":
            total_execution_cycles += 1

        if to_status == "done":
            done_outcome_count += 1

    if last_oracle_verdict is not None:
        recent_oracle_approved = last_oracle_verdict

    return JuiceSignals(
        oracle_approval_count=oracle_approval_count,
        total_execution_cycles=total_execution_cycles,
        done_outcome_count=done_outcome_count,
        # completed_prerequisite_count is passed in from outside — the steward
        # or the registry caller provides this from related UoW queries.
        completed_prerequisite_count=0,
        recent_oracle_approved=recent_oracle_approved,
    )


def _extract_signals_with_prerequisites(
    audit_entries: list[dict[str, Any]],
    completed_prerequisite_count: int,
) -> JuiceSignals:
    """
    Extract signals including prerequisite completion count.

    This is the full signal extraction path. Use this when the caller
    can supply the prerequisite count from a registry query.

    Args:
        audit_entries: UoW audit log entries.
        completed_prerequisite_count: Number of done/completed prerequisite UoWs.
            Pass 0 if the UoW has no prerequisites or they are unknown.

    Returns:
        JuiceSignals with all fields populated.
    """
    base = _extract_signals(audit_entries)
    return JuiceSignals(
        oracle_approval_count=base.oracle_approval_count,
        total_execution_cycles=base.total_execution_cycles,
        done_outcome_count=base.done_outcome_count,
        completed_prerequisite_count=completed_prerequisite_count,
        recent_oracle_approved=base.recent_oracle_approved,
    )


# ---------------------------------------------------------------------------
# Scoring (pure functions)
# ---------------------------------------------------------------------------

def _score_oracle_approval(signals: JuiceSignals) -> float:
    """
    Compute the oracle approval sub-score in [0.0, 1.0].

    Returns 0.0 when there is no execution history (no information = no juice
    signal from oracle). Returns 0.0 when sample size is below the minimum
    threshold (rate is too noisy to trust).

    Pure function — no side effects.
    """
    if signals.total_execution_cycles < JUICE_MIN_ORACLE_SAMPLE_SIZE:
        return 0.0
    rate = signals.oracle_approval_count / signals.total_execution_cycles
    # Recent approval multiplier: a recent approval amplifies the rate signal.
    recency_boost = 1.2 if signals.recent_oracle_approved else 1.0
    return min(1.0, rate * recency_boost)


def _score_completion_rate(signals: JuiceSignals) -> float:
    """
    Compute the completion rate sub-score in [0.0, 1.0].

    Higher done outcome rate within the UoW's execution history = more
    productive thread. Returns 0.0 when there are no execution cycles.

    Pure function — no side effects.
    """
    if signals.total_execution_cycles == 0:
        return 0.0
    return min(1.0, signals.done_outcome_count / signals.total_execution_cycles)


def _score_prerequisites(signals: JuiceSignals) -> float:
    """
    Compute the prerequisite completion sub-score in [0.0, 1.0].

    Binary: 1.0 when at least JUICE_PREREQUISITE_POSITIVE_THRESHOLD prerequisites
    are completed; 0.0 otherwise. The steward passes this in from related UoW
    queries via compute_juice().

    Pure function — no side effects.
    """
    if signals.completed_prerequisite_count >= JUICE_PREREQUISITE_POSITIVE_THRESHOLD:
        return 1.0
    return 0.0


def _aggregate_score(signals: JuiceSignals) -> float:
    """
    Aggregate the three sub-scores into a single juice score in [0.0, 1.0].

    Weights: oracle_rate=0.45, completion_rate=0.35, prerequisites=0.20.
    These weights reflect the spec's emphasis on oracle approval as the
    strongest single signal of a productive thread.

    Pure function — no side effects.
    """
    oracle_score = _score_oracle_approval(signals)
    completion_score = _score_completion_rate(signals)
    prerequisite_score = _score_prerequisites(signals)

    return (
        _WEIGHT_ORACLE_RATE * oracle_score
        + _WEIGHT_COMPLETION_RATE * completion_score
        + _WEIGHT_PREREQUISITES * prerequisite_score
    )


# ---------------------------------------------------------------------------
# Threshold — the score above which a UoW is considered juiced
# ---------------------------------------------------------------------------

# Minimum aggregate score to assert juice. Set above the oracle-only minimum
# so that a barely-passing oracle rate alone is insufficient — at least one
# other signal must contribute positively.
JUICE_SCORE_THRESHOLD: float = 0.35

# Delta threshold for writing juice back to the registry.
# Juice is only written if the new score differs from the stored float by at
# least this much — avoids churn on stable threads.
JUICE_UPDATE_DELTA: float = 0.05


# ---------------------------------------------------------------------------
# JuiceSensor — the primary public interface
# ---------------------------------------------------------------------------

class JuiceSensor:
    """
    Computes juice signals from observable UoW history.

    Design:
    - JuiceSensor is stateless. Instantiate once and reuse across the steward cycle.
    - All sensing logic is delegated to pure functions. JuiceSensor is the
      composition layer, not the computation layer.
    - The registry is used only for the prerequisite count query. All other
      signals come from the audit_entries already fetched by the steward.

    Usage in steward.py:
        sensor = JuiceSensor()
        assessment = sensor.assess(uow, audit_entries, registry)
        if assessment.has_juice:
            # write juice_quality='juice', juice_rationale=assessment.rationale
    """

    def assess(
        self,
        uow: "UoW",
        audit_entries: list[dict[str, Any]],
        registry: "Registry",
    ) -> JuiceAssessment:
        """
        Assess the juice quality of a UoW from its observable signals.

        This method is the single entry point for juice assessment. It:
        1. Queries the registry for completed prerequisite count.
        2. Extracts signals from audit_entries.
        3. Computes an aggregate score.
        4. Returns a JuiceAssessment with has_juice, score, and rationale.

        No writes occur here. The caller is responsible for writing
        juice_quality and juice_rationale back to the registry if has_juice=True.

        Args:
            uow: The Unit of Work being assessed.
            audit_entries: All audit log entries for this UoW (already fetched).
            registry: Registry instance for prerequisite count query.

        Returns:
            JuiceAssessment with score, has_juice, and rationale.
        """
        completed_prereqs = _count_completed_prerequisites(uow, registry)
        signals = _extract_signals_with_prerequisites(audit_entries, completed_prereqs)
        return _assess_from_signals(uow.id, signals)


def _count_completed_prerequisites(uow: "UoW", registry: "Registry") -> int:
    """
    Count the number of completed prerequisite UoWs for this UoW.

    A prerequisite is any UoW that is referenced by this UoW's trigger
    and has reached done/failed/expired terminal status. We count only
    'done' status as completed (failed/expired do not indicate productive
    completion).

    Returns 0 when the UoW has no trigger, the trigger references no
    prerequisite UoWs, or the registry query fails (safe default).

    Pure in effect — reads only, no writes.
    """
    trigger = uow.trigger
    if not trigger or not isinstance(trigger, dict):
        return 0

    prerequisite_ids: list[str] = []
    if "prerequisites" in trigger:
        raw = trigger["prerequisites"]
        if isinstance(raw, list):
            prerequisite_ids = [str(p) for p in raw if p]

    if not prerequisite_ids:
        return 0

    count = 0
    for prereq_id in prerequisite_ids:
        try:
            prereq_uow = registry.get(prereq_id)
            if prereq_uow is not None and str(prereq_uow.status) == "done":
                count += 1
        except Exception:
            pass  # Registry error: treat this prerequisite as not completed

    return count


def _assess_from_signals(uow_id: str, signals: JuiceSignals) -> JuiceAssessment:
    """
    Compute a JuiceAssessment from extracted signals.

    Separated from JuiceSensor.assess() so tests can exercise the scoring
    logic directly without needing a registry or UoW instance.

    Pure function — no side effects.

    Args:
        uow_id: Used only for log messages.
        signals: Extracted juice signals.

    Returns:
        JuiceAssessment with score, has_juice, and rationale.
    """
    # No execution history at all — insufficient data, cannot assert juice.
    if signals.total_execution_cycles == 0:
        log.debug(
            "juice_assess: %s — no execution history, score=None, has_juice=False",
            uow_id,
        )
        return JuiceAssessment(score=None, has_juice=False, rationale="")

    score = _aggregate_score(signals)

    log.debug(
        "juice_assess: %s — score=%.3f (oracle_rate=%.2f/%d, done=%d, prereqs=%d, recent_approved=%s)",
        uow_id,
        score,
        signals.oracle_approval_count,
        signals.total_execution_cycles,
        signals.done_outcome_count,
        signals.completed_prerequisite_count,
        signals.recent_oracle_approved,
    )

    if score >= JUICE_SCORE_THRESHOLD:
        rationale = _build_rationale(signals)
        return JuiceAssessment(score=score, has_juice=True, rationale=rationale)

    return JuiceAssessment(score=score, has_juice=False, rationale="")


def _build_rationale(signals: JuiceSignals) -> str:
    """
    Build a human-readable rationale string for a juiced prescription.

    The rationale names the alive thread and the signals that support the
    juice assertion — directly satisfying the spec's "What is the juice?"
    calibration requirement. Non-empty only when juice is being asserted.

    Pure function — no side effects.
    """
    parts: list[str] = []

    if signals.total_execution_cycles > 0:
        rate = signals.oracle_approval_count / signals.total_execution_cycles
        parts.append(
            f"oracle approval rate {rate:.0%} over {signals.total_execution_cycles} cycle(s)"
        )
    if signals.recent_oracle_approved:
        parts.append("most recent oracle verdict: approved")
    if signals.done_outcome_count > 0:
        parts.append(f"{signals.done_outcome_count} completed execution(s)")
    if signals.completed_prerequisite_count >= JUICE_PREREQUISITE_POSITIVE_THRESHOLD:
        parts.append(
            f"{signals.completed_prerequisite_count} completed prerequisite(s)"
        )

    if not parts:
        return "thread shows generative momentum"

    return "live thread: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Primary public function (spec interface: compute_juice)
# ---------------------------------------------------------------------------

def compute_juice(uow_id: str, registry: "Registry") -> float | None:
    """
    Compute the juice score for a UoW, identified by ID.

    This is the primary entry point specified in the task brief. It:
    1. Fetches the UoW from the registry (returns None if not found).
    2. Fetches audit entries for the UoW.
    3. Runs JuiceSensor.assess() and returns the numeric score.

    Returns None when:
    - The UoW is not found.
    - There is no execution history (insufficient data to score).

    Returns a float in [0.0, 1.0] otherwise.

    Side effects: none — all writes are the caller's responsibility.

    Args:
        uow_id: The UoW identifier.
        registry: Registry instance.

    Returns:
        float in [0.0, 1.0] if scoreable, None otherwise.
    """
    uow = registry.get(uow_id)
    if uow is None:
        log.warning("compute_juice: UoW %s not found", uow_id)
        return None

    # Fetch audit entries via the registry's internal connection — same pattern
    # as _fetch_audit_entries in steward.py.
    conn = registry._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        audit_entries = [dict(r) for r in rows]
    finally:
        conn.close()

    sensor = JuiceSensor()
    assessment = sensor.assess(uow, audit_entries, registry)
    return assessment.score
