"""
verdict_stats.py — Observability rollup for the verdict accumulator.

§3-I Adaptive Steward (wos-evolution-spec.md).

Provides read-path queries against the verdict_accumulator and normalization_log
tables in wos-metrics.db. These functions feed the observability dashboard
(normalization clustering health, hypothesis signal quality) and will be
consumed by the Orientation Layer Governor in Stage 3.

Design:
- Pure read path: all functions are SELECT-only, no writes.
- Non-fatal on missing DB: returns empty results when wos-metrics.db does not
  exist so callers do not need to guard against fresh installs.
- Named return types: typed dataclasses for all query results so callers get
  IDE support and the dashboard generator can pattern-match on fields.

Gate condition for Stage 2: accumulator_total_scored() >= GATE_SCORED_OUTCOMES_MIN.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("verdict_stats")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Gate condition: Stage 2 requires at least this many scored outcomes.
#: Spec §4 transition table: "verdict_accumulator has 20+ scored outcomes".
GATE_SCORED_OUTCOMES_MIN: int = 20

#: "Pearl" threshold: hypotheses with success rate ≥ this are considered pearls.
#: Spec §5 glossary: "Pearl from Adaptive Steward = hypothesis with >0.7 success rate after 5+ observations".
PEARL_SUCCESS_RATE_THRESHOLD: float = 0.7

#: Minimum observations before a hypothesis is considered stable signal.
#: Spec §5 glossary: "after 5+ observations".
PEARL_MIN_OBSERVATIONS: int = 5

#: Default path relative to LOBSTER_WORKSPACE.
_METRICS_DB_SUBPATH: str = "orchestration/wos-metrics.db"


def _metrics_db_path() -> Path:
    workspace = os.environ.get(
        "LOBSTER_WORKSPACE",
        str(Path.home() / "lobster-workspace"),
    )
    return Path(workspace) / _METRICS_DB_SUBPATH


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HypothesisRollup:
    """
    Aggregated scoring data for one normalized hypothesis across a register.

    Fields
    ------
    register : str
        The UoW attentional register.
    diagnosis_hypothesis : str
        The normalized hypothesis string.
    n_successes : int
        Total scored outcomes with outcome='pass'.
    n_failures : int
        Total scored outcomes with outcome='fail'.
    n_partial : int
        Total scored outcomes with outcome='partial'.
    total : int
        Sum of n_successes + n_failures + n_partial.
    success_rate : float
        n_successes / total (0.0 when total == 0).
    last_updated : str
        ISO timestamp of the last upsert.
    """

    register: str
    diagnosis_hypothesis: str
    n_successes: int
    n_failures: int
    n_partial: int
    total: int
    success_rate: float
    last_updated: str


@dataclass(frozen=True)
class NormalizationClusterHealth:
    """
    Clustering health report for one register.

    Used to detect hypothesis fragmentation (Churn Basin failure mode).
    A healthy register has a high raw-to-normalized compression ratio.

    Fields
    ------
    register : str
        The UoW attentional register.
    distinct_normalized : int
        Count of distinct normalized hypothesis strings in verdict_accumulator.
    distinct_raw : int
        Count of distinct raw hypothesis strings in normalization_log.
    mean_observations : float
        Mean (n_successes + n_failures + n_partial) across all hypothesis rows.
    churn_basin_risk : bool
        True when distinct_normalized > 100 AND mean_observations < 5.
        Spec §5.1 Churn Basin detection threshold.
    """

    register: str
    distinct_normalized: int
    distinct_raw: int
    mean_observations: float
    churn_basin_risk: bool


@dataclass(frozen=True)
class AccumulatorSummary:
    """
    High-level summary of the verdict accumulator state.

    Fields
    ------
    total_scored : int
        Total number of distinct (register, hypothesis) pairs with at least
        one scored outcome.
    gate_ready : bool
        True when total_scored >= GATE_SCORED_OUTCOMES_MIN (Stage 2 gate).
    pearl_count : int
        Hypotheses with success_rate >= PEARL_SUCCESS_RATE_THRESHOLD and
        total >= PEARL_MIN_OBSERVATIONS.
    """

    total_scored: int
    gate_ready: bool
    pearl_count: int


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------

def get_hypothesis_rollup(
    register: str | None = None,
    *,
    db_path: Path | None = None,
    min_observations: int = 0,
) -> list[HypothesisRollup]:
    """
    Return per-hypothesis aggregated scoring data, optionally filtered by register.

    Results are ordered by success_rate descending, then total descending.

    Args:
        register: Optional register filter. None returns all registers.
        db_path: Override metrics DB path (injectable for tests).
        min_observations: Only return rows with total >= this value.

    Returns:
        List of HypothesisRollup objects (empty if DB absent or no data).
    """
    effective_db_path = db_path or _metrics_db_path()
    if not Path(effective_db_path).exists():
        return []

    try:
        conn = _connect(effective_db_path)
        try:
            base_query = """
                SELECT
                    register,
                    diagnosis_hypothesis,
                    n_successes,
                    n_failures,
                    n_partial,
                    (n_successes + n_failures + n_partial) AS total,
                    CASE
                        WHEN (n_successes + n_failures + n_partial) = 0 THEN 0.0
                        ELSE CAST(n_successes AS REAL) / (n_successes + n_failures + n_partial)
                    END AS success_rate,
                    last_updated
                FROM verdict_accumulator
                WHERE (n_successes + n_failures + n_partial) >= ?
            """
            params: list = [min_observations]

            if register is not None:
                base_query += " AND register = ?"
                params.append(register)

            base_query += " ORDER BY success_rate DESC, total DESC"

            rows = conn.execute(base_query, params).fetchall()
            return [
                HypothesisRollup(
                    register=r["register"],
                    diagnosis_hypothesis=r["diagnosis_hypothesis"],
                    n_successes=r["n_successes"],
                    n_failures=r["n_failures"],
                    n_partial=r["n_partial"],
                    total=r["total"],
                    success_rate=r["success_rate"],
                    last_updated=r["last_updated"],
                )
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("get_hypothesis_rollup: query failed — %s: %s", type(exc).__name__, exc)
        return []


def get_normalization_cluster_health(
    *,
    db_path: Path | None = None,
) -> list[NormalizationClusterHealth]:
    """
    Return per-register normalization clustering health.

    Computes distinct_normalized (from verdict_accumulator) and distinct_raw
    (from normalization_log) per register to detect Churn Basin risk.

    Args:
        db_path: Override metrics DB path (injectable for tests).

    Returns:
        List of NormalizationClusterHealth objects (empty if DB absent or no data).
    """
    effective_db_path = db_path or _metrics_db_path()
    if not Path(effective_db_path).exists():
        return []

    try:
        conn = _connect(effective_db_path)
        try:
            # Accumulator stats per register
            accumulator_rows = conn.execute(
                """
                SELECT
                    register,
                    COUNT(DISTINCT diagnosis_hypothesis) AS distinct_normalized,
                    AVG(n_successes + n_failures + n_partial) AS mean_obs
                FROM verdict_accumulator
                GROUP BY register
                """
            ).fetchall()

            # Raw hypothesis count per register from normalization_log.
            # Note: normalization_log joins to prescription_hypothesis_log by uow_id
            # to get the register. We query prescription_hypothesis_log for register.
            raw_counts: dict[str, int] = {}
            try:
                raw_rows = conn.execute(
                    """
                    SELECT p.register, COUNT(DISTINCT n.raw) AS distinct_raw
                    FROM normalization_log n
                    JOIN prescription_hypothesis_log p ON n.uow_id = p.uow_id
                    GROUP BY p.register
                    """
                ).fetchall()
                raw_counts = {r["register"]: r["distinct_raw"] for r in raw_rows}
            except Exception as raw_exc:
                # prescription_hypothesis_log may not exist on pre-migration DBs
                log.debug(
                    "get_normalization_cluster_health: raw count query failed — %s",
                    raw_exc,
                )

            results = []
            for row in accumulator_rows:
                register = row["register"]
                distinct_normalized = row["distinct_normalized"]
                mean_obs = float(row["mean_obs"] or 0.0)
                distinct_raw = raw_counts.get(register, distinct_normalized)

                # Spec §5.1 Churn Basin detection:
                # >100 distinct hypotheses per register AND mean observations <5
                churn_basin_risk = (distinct_normalized > 100 and mean_obs < 5.0)

                results.append(NormalizationClusterHealth(
                    register=register,
                    distinct_normalized=distinct_normalized,
                    distinct_raw=distinct_raw,
                    mean_observations=mean_obs,
                    churn_basin_risk=churn_basin_risk,
                ))
            return results
        finally:
            conn.close()
    except Exception as exc:
        log.warning(
            "get_normalization_cluster_health: query failed — %s: %s",
            type(exc).__name__,
            exc,
        )
        return []


def accumulator_total_scored(
    *,
    db_path: Path | None = None,
) -> int:
    """
    Return the total number of distinct (register, hypothesis) pairs with
    at least one scored outcome.

    Gate condition check for Stage 2: when this value >= GATE_SCORED_OUTCOMES_MIN,
    the Governor can begin operating on real signal.

    Args:
        db_path: Override metrics DB path (injectable for tests).

    Returns:
        Count of distinct scored hypothesis rows. 0 if DB absent or error.
    """
    effective_db_path = db_path or _metrics_db_path()
    if not Path(effective_db_path).exists():
        return 0

    try:
        conn = _connect(effective_db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM verdict_accumulator "
                "WHERE (n_successes + n_failures + n_partial) > 0"
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()
    except Exception as exc:
        log.warning("accumulator_total_scored: query failed — %s: %s", type(exc).__name__, exc)
        return 0


def get_accumulator_summary(
    *,
    db_path: Path | None = None,
) -> AccumulatorSummary:
    """
    Return a high-level summary of the accumulator state.

    Includes gate_ready flag (Stage 2 gate condition) and pearl count.

    Args:
        db_path: Override metrics DB path (injectable for tests).

    Returns:
        AccumulatorSummary with total_scored, gate_ready, and pearl_count.
    """
    effective_db_path = db_path or _metrics_db_path()
    if not Path(effective_db_path).exists():
        return AccumulatorSummary(
            total_scored=0,
            gate_ready=False,
            pearl_count=0,
        )

    try:
        conn = _connect(effective_db_path)
        try:
            total_scored = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM verdict_accumulator "
                    "WHERE (n_successes + n_failures + n_partial) > 0"
                ).fetchone()["c"]
            )

            pearl_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM verdict_accumulator
                    WHERE (n_successes + n_failures + n_partial) >= ?
                    AND CAST(n_successes AS REAL) / (n_successes + n_failures + n_partial) >= ?
                    """,
                    (PEARL_MIN_OBSERVATIONS, PEARL_SUCCESS_RATE_THRESHOLD),
                ).fetchone()["c"]
            )

            return AccumulatorSummary(
                total_scored=total_scored,
                gate_ready=total_scored >= GATE_SCORED_OUTCOMES_MIN,
                pearl_count=pearl_count,
            )
        finally:
            conn.close()
    except Exception as exc:
        log.warning("get_accumulator_summary: query failed — %s: %s", type(exc).__name__, exc)
        return AccumulatorSummary(total_scored=0, gate_ready=False, pearl_count=0)


def get_top_priors(
    register: str,
    *,
    limit: int = 5,
    db_path: Path | None = None,
) -> list[str]:
    """
    Return the top *limit* hypothesis strings for *register* ordered by success rate.

    This is the Selector query: pure SQL on verdict_accumulator, ordered by
    success rate. Used to inject top-5 priors into the prescription prompt.
    No LLM call required.

    Spec §3-I: "Selector: Pure SQL query on verdict_accumulator ordered by success
    rate. Injects top-5 priors into prescription prompt."

    Args:
        register: The UoW attentional register.
        limit: Maximum number of hypotheses to return (default 5 per spec).
        db_path: Override metrics DB path (injectable for tests).

    Returns:
        List of normalized hypothesis strings (may be shorter than *limit*
        when insufficient data exists).
    """
    rollup = get_hypothesis_rollup(
        register=register,
        db_path=db_path,
        min_observations=1,
    )
    return [r.diagnosis_hypothesis for r in rollup[:limit]]
