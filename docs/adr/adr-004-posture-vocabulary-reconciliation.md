# ADR-004: Posture Vocabulary Reconciliation

**Status:** PROPOSED  
**Date:** 2026-04-08  
**Issues:** #571, #567  
**References:** PR #564, wos-v2-design.md, work-orchestration-system.md

## Context

Three independent posture vocabularies coexist in the WOS codebase:

1. **Dispatch postures** (WOS v1, `uow_registry.posture`):
   - Values: `solo`, `fan-out`, `sequential`, `review-loop`, `human-gate`
   - Purpose: Governs execution structure (parallelism, dependencies, human gates)
   - Owner: Routing Classifier (Phase 3), currently hardcoded to `solo`
   - Location: UoW record `posture` field

2. **Trace postures** (WOS v2 design, PR #564):
   - Values: `orienting`, `clarifying`, `waiting_for_signal`, `scope_challenged`, `closing_with_discovery`
   - Purpose: Narrative descriptor of Steward's mode at each cycle
   - Owner: Steward (writes to `steward_agenda` cycle trace entries)
   - Location: `steward_agenda[].posture` field

3. **Reentry classification** (steward.py implementation):
   - Values: `execution_complete`, `first_execution`, `crashed_no_output`, `startup_sweep_possibly_complete`, `stall_detected`, `execution_failed`, `executor_orphan`, `diagnosing_orphan`, `crashed_zero_bytes`, `crashed_output_ref_missing`
   - Purpose: Diagnostic classification of what happened on the previous execution cycle
   - Owner: `_determine_reentry_posture()` function
   - Location: Internal steward diagnostic, currently leaked to trace `posture` field

The conflict: `_build_trace_entry()` writes `reentry_posture` values (backward-looking diagnostic) to the cycle trace's `posture` field, but the v2 design specifies forward-looking narrative postures.

Additionally, S3P2-B proposes a prescription front-matter `posture` field with values `solo`, `verify`, `explore`, `pivot`, `escalate` — potentially introducing a fourth vocabulary.

## Decision

### Vocabulary Authority

| Concern | Authoritative Vocabulary | Field Location | Purpose |
|---------|-------------------------|----------------|---------|
| **Dispatch strategy** | WOS v1: `solo`, `fan-out`, `sequential`, `review-loop`, `human-gate` | `uow_registry.posture` | How work is structured and parallelized |
| **Trace annotation** | WOS v2: `orienting`, `clarifying`, `waiting_for_signal`, `scope_challenged`, `closing_with_discovery` | `steward_agenda[].posture` | What the Steward is doing at each cycle |
| **Reentry diagnostic** | Renamed to `reentry_classification` | Internal steward diagnostic only | What happened on the previous cycle |
| **Prescription routing** (S3P2-B) | Inherits from dispatch posture | `workflow_artifact` front-matter | Routing hint, defaults to `solo` |

### Layering

The vocabularies are **layered**, not unified:

- **Dispatch posture** owns the UoW-level execution structure. The Routing Classifier (Phase 3) assigns these. Phase 2 defaults all UoWs to `solo`.
- **Trace posture** owns the narrative arc within each steward cycle. The steward derives this from UoW state + reentry classification.
- **Reentry classification** is internal diagnostic state, not a posture vocabulary. Rename to avoid collision.

### Trace Posture Derivation

The steward derives trace posture from reentry classification and UoW state:

| Reentry Classification | UoW State | Trace Posture |
|----------------------|-----------|---------------|
| `first_execution` | `steward_cycles == 0` | `orienting` |
| `execution_complete` | completion check pending | `clarifying` |
| `execution_complete` | is_complete == True | `closing_with_discovery` |
| `*_orphan`, `crashed_*`, `stall_detected` | any | `scope_challenged` |
| any | blocked on external | `waiting_for_signal` |

### S3P2-B Prescription Posture

The prescription front-matter `posture` field should **not** introduce new values. For Phase 2:
- Default to `solo` (the only active dispatch posture)
- When S3P2-F reconciliation lands, S3P2-B adopts the dispatch posture vocabulary

This avoids a fourth vocabulary. The prescription posture hints at dispatch strategy, not steward mode.

## Implementation

1. **Rename in steward.py:**
   - `_determine_reentry_posture` → `_determine_reentry_classification`
   - All references to `reentry_posture` → `reentry_classification`

2. **Add trace posture derivation:**
   - New function `_determine_trace_posture(reentry_classification, uow, diagnosis)` → trace posture
   - Update `_build_trace_entry()` to use this function

3. **Update related functions:**
   - `_posture_rationale()` → handle trace postures
   - `_posture_prediction()` → handle trace postures

4. **Document in S3P2-B:** Prescription front-matter `posture` defaults to `solo`; no new vocabulary.

## Consequences

- **Clarity:** Each vocabulary has a single authoritative owner and purpose
- **No breaking change:** Reentry classification values continue to work internally
- **Forward compatible:** When Routing Classifier lands (Phase 3), dispatch postures become active; trace postures remain independent
- **Trace readability:** Cycle traces now carry human-readable narrative postures, not diagnostic codes
