"""
prescription_metrics.py — Structured metrics logger for steward prescription events.

Provides PrescriptionMetricsLogger, which records two categories of steward
events for downstream analysis:

- log_prescription_generated: emitted each time the steward writes a new
  prescription for a UoW (once per dispatch cycle).
- log_uow_closed: emitted when a UoW transitions to Done/Failed and the
  steward records total lifecycle metrics.

Current implementation: no-op logger (events are emitted at DEBUG level only).
The intended backing store is wos-metrics.db (separate from the registry DB
to avoid write contention), but the DB schema and writer have not yet been
implemented. This stub was created to unblock executor-heartbeat after
PR #1305 introduced the import without providing the module.

When the full implementation is added, the DB schema and writer should be
introduced here; the method signatures are stable.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("prescription_metrics")


class PrescriptionMetricsLogger:
    """Logs prescription and closure events for WOS structural analysis.

    All methods are non-fatal: if logging fails (e.g. future DB write),
    the exception is caught and logged at WARNING level so the steward
    main loop is never interrupted by metrics collection failures.
    """

    def log_prescription_generated(
        self,
        *,
        uow_id: str,
        cycle: int,
        executor_type: str,
        has_minimum_viable_output: bool,
        has_boundary: bool,
        has_success_criteria_check: bool = False,
        estimated_cycles: int | None = None,
        word_count: int | None = None,
        step_count: int | None = None,
        source_issue: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Record that a prescription was generated for a UoW dispatch cycle.

        Args:
            uow_id: The UoW identifier.
            cycle: The dispatch cycle number at which this prescription was generated.
            executor_type: The selected executor type string (e.g. "functional-engineer").
            has_minimum_viable_output: Whether the prescription includes a
                "Minimum viable output" clause.
            has_boundary: Whether the prescription includes a "Boundary:" clause.
            has_success_criteria_check: Whether the UoW has non-empty success criteria.
            estimated_cycles: The estimated_cycles field value at prescription time.
            word_count: Word count of the generated instruction text.
            step_count: Number of "## " sections in the instruction text.
            source_issue: The originating GitHub issue URL or source field, if any.
            **kwargs: Reserved for future fields — accepted and ignored to allow
                call-site additions without breaking this signature.
        """
        log.debug(
            "prescription_generated uow_id=%s cycle=%d executor_type=%s "
            "has_mvo=%s has_boundary=%s words=%s steps=%s",
            uow_id,
            cycle,
            executor_type,
            has_minimum_viable_output,
            has_boundary,
            word_count,
            step_count,
        )

    def log_uow_closed(
        self,
        *,
        uow_id: str,
        total_cycles: int,
        closure_outcome: str,
        final_prescription_cycle: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Record UoW closure with final lifecycle metrics.

        Args:
            uow_id: The UoW identifier.
            total_cycles: Total number of dispatch cycles at closure.
            closure_outcome: Outcome label at closure (e.g. "success", "failed").
            final_prescription_cycle: The cycle number of the last generated
                prescription, if available.
            **kwargs: Reserved for future fields — accepted and ignored.
        """
        log.debug(
            "uow_closed uow_id=%s total_cycles=%d outcome=%s final_prescription_cycle=%s",
            uow_id,
            total_cycles,
            closure_outcome,
            final_prescription_cycle,
        )
