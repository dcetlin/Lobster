"""
PrescriptionObject — typed representation of a Steward prescription for scoring.

§3-I Adaptive Steward (wos-evolution-spec.md).

PrescriptionObject captures the machine-comparable fields of a prescription at
dispatch time. Its primary role is to supply the `diagnosis_hypothesis` string
to the verdict scoring pipeline — the 140-character description of what the
Steward believes is wrong and how it plans to fix it.

Design:
- Frozen dataclass: prescriptions are immutable once written. No in-place edits.
- Separate from PrescriptionV2: PrescriptionV2 is the full 7-section LLM output
  consumed by the Executor. PrescriptionObject is a scoring-layer artifact that
  extracts the scoring-relevant fields for the verdict accumulator pipeline.
- module-level: no registry imports. Can be imported in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestration.registry import UoWRegister


# Spec-mandated maximum length for diagnosis_hypothesis (machine-comparable).
HYPOTHESIS_MAX_CHARS: int = 140


def _truncate_hypothesis(raw: str) -> str:
    """
    Truncate *raw* to HYPOTHESIS_MAX_CHARS characters.

    Pure function — no side effects.
    """
    return raw[:HYPOTHESIS_MAX_CHARS]


@dataclass(frozen=True)
class PrescriptionObject:
    """
    Typed representation of a Steward prescription for the verdict scoring pipeline.

    §3-I Adaptive Steward spec (wos-evolution-spec.md).

    Fields
    ------
    uow_id : str
        The UoW this prescription was written for.
    register : UoWRegister
        Attentional register of the UoW at prescription time.
    diagnosis_hypothesis : str
        Max 140 characters. Machine-comparable description of what the Steward
        believes is the root cause and how it plans to fix it. This is the
        string passed to the Haiku normalization call at verdict scoring time.
    proposed_steps : list[str]
        Ordered list of steps proposed by the prescription.
    confidence : float
        Steward's confidence in the prescription (0.0–1.0).
    counterfactual_question : str
        "What would falsify this hypothesis?" — provided for human review and
        future use as a secondary scoring signal.
    generated_at : str
        ISO 8601 UTC timestamp when this prescription was generated.
    selector_priors : list[str]
        Top-5 prior hypothesis IDs from verdict_accumulator that influenced
        this prescription. Empty list when the accumulator has no signal yet.
    """

    uow_id: str
    register: UoWRegister
    diagnosis_hypothesis: str
    proposed_steps: list[str]
    confidence: float
    counterfactual_question: str
    generated_at: str
    selector_priors: list[str]

    def __post_init__(self) -> None:
        # Enforce the 140-char limit at construction time so callers cannot
        # accidentally store an over-long hypothesis in the log.
        if len(self.diagnosis_hypothesis) > HYPOTHESIS_MAX_CHARS:
            raise ValueError(
                f"PrescriptionObject.diagnosis_hypothesis exceeds {HYPOTHESIS_MAX_CHARS} chars "
                f"(got {len(self.diagnosis_hypothesis)}): {self.diagnosis_hypothesis!r}"
            )


def build_prescription_object(
    uow_id: str,
    register: UoWRegister,
    hypothesis_raw: str,
    proposed_steps: list[str],
    confidence: float,
    counterfactual_question: str,
    generated_at: str,
    selector_priors: list[str] | None = None,
) -> PrescriptionObject:
    """
    Build a PrescriptionObject, truncating the hypothesis to spec max length.

    Use this factory rather than the constructor directly when the hypothesis
    comes from LLM output (which may exceed 140 chars).

    Pure function — no side effects.

    Args:
        uow_id: The UoW identifier.
        register: The attentional register.
        hypothesis_raw: The raw hypothesis string (will be truncated to 140 chars).
        proposed_steps: The ordered list of proposed steps.
        confidence: Prescription confidence (0.0–1.0).
        counterfactual_question: "What would falsify this hypothesis?"
        generated_at: ISO 8601 UTC timestamp.
        selector_priors: Top-5 hypothesis IDs from verdict_accumulator. Defaults to [].

    Returns:
        Frozen PrescriptionObject.
    """
    return PrescriptionObject(
        uow_id=uow_id,
        register=register,
        diagnosis_hypothesis=_truncate_hypothesis(hypothesis_raw),
        proposed_steps=list(proposed_steps),
        confidence=float(confidence),
        counterfactual_question=counterfactual_question,
        generated_at=generated_at,
        selector_priors=list(selector_priors or []),
    )


def hypothesis_from_uow_summary(summary: str) -> str:
    """
    Derive a machine-comparable hypothesis string from a UoW summary.

    Used when no explicit PrescriptionObject is available for a UoW (e.g. UoWs
    created before the Adaptive Steward was deployed). The summary is truncated
    to HYPOTHESIS_MAX_CHARS.

    Pure function — no side effects.
    """
    return _truncate_hypothesis((summary or "").strip())
