"""
Scaling Governor — progressive dispatch gate for WOS executor cycles.

Prevents catastrophic failure modes by capping UoW injection when Attunement
evidence at the requested scale is absent. This is a developmental mechanism:
the goal is to make failure developmental (specific, traceable, small) rather
than catastrophic (uniform, scale-filling).

Design reference: WOS UoW uow_20260423_c87689.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config path (overridable for tests)
# ---------------------------------------------------------------------------

_DEFAULT_WOS_CONFIG_PATH: Optional[Path] = None  # Resolved lazily if None


def _get_wos_config_path() -> Path:
    """Return the wos-config.json path, using paths.py default if not overridden."""
    if _DEFAULT_WOS_CONFIG_PATH is not None:
        return _DEFAULT_WOS_CONFIG_PATH
    try:
        from src.orchestration.paths import WOS_CONFIG
        return WOS_CONFIG
    except ImportError:
        import os
        workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
        return workspace / "data" / "wos-config.json"


# ---------------------------------------------------------------------------
# GovernorDecision dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GovernorDecision:
    proposed_n: int           # how many UoWs were eligible
    allowed_n: int            # how many are permitted this cycle
    capped: bool              # True when allowed_n < proposed_n
    cap_reason: Optional[str] # human-readable, None when not capped
    attunement_scale: int     # largest N with >= 80% success evidence (0 if none)


# ---------------------------------------------------------------------------
# Pure computation function (testable without a live DB)
# ---------------------------------------------------------------------------

def _compute_decision(proposed_n: int, attunement_scale: int) -> GovernorDecision:
    """
    Compute the GovernorDecision from proposed_n and attunement_scale.

    This is a pure function — no I/O, no DB access. ScalingGovernor.check()
    calls this after querying the DB and checking the override flag.

    Cap logic:
    - If attunement_scale >= proposed_n: no cap.
    - If attunement_scale < proposed_n: cap at max(1, proposed_n // 5).
    """
    if attunement_scale >= proposed_n:
        return GovernorDecision(
            proposed_n=proposed_n,
            allowed_n=proposed_n,
            capped=False,
            cap_reason=None,
            attunement_scale=attunement_scale,
        )

    allowed_n = max(1, proposed_n // 5)
    cap_reason = (
        f"no Attunement evidence at scale {proposed_n} "
        f"(largest clean window: {attunement_scale}); "
        f"capping at {allowed_n} (N/5)"
    )
    return GovernorDecision(
        proposed_n=proposed_n,
        allowed_n=allowed_n,
        capped=True,
        cap_reason=cap_reason,
        attunement_scale=attunement_scale,
    )


# ---------------------------------------------------------------------------
# ScalingGovernor class
# ---------------------------------------------------------------------------

class ScalingGovernor:
    """
    Progressive dispatch gate that caps UoW injection when Attunement evidence
    at the requested scale is absent.

    Usage:
        governor = ScalingGovernor(registry.db_path)
        decision = governor.check(proposed_n=eligible_count)
        if decision.capped:
            eligible_uows = eligible_uows[:decision.allowed_n]
    """

    def __init__(
        self,
        registry_db_path,
        wos_config_path: Optional[Path] = None,
    ) -> None:
        try:
            self._db_path = Path(registry_db_path)
        except (TypeError, ValueError) as exc:
            log.warning(
                "ScalingGovernor: invalid registry_db_path %r — %s; DB queries will fail safely",
                registry_db_path,
                exc,
            )
            self._db_path = None  # type: ignore[assignment]
        self._wos_config_path = wos_config_path  # None = use default

    def _resolve_config_path(self) -> Path:
        if self._wos_config_path is not None:
            return self._wos_config_path
        return _get_wos_config_path()

    def _read_override_flag(self) -> bool:
        """Return True if scaling_governor_override is set in wos-config.json."""
        try:
            config_path = self._resolve_config_path()
            with config_path.open() as fh:
                config = json.load(fh)
            return bool(config.get("scaling_governor_override", False))
        except Exception:
            return False

    def _query_attunement_scale(self, proposed_n: int) -> int:
        """
        Query the registry DB for the most recent 500 terminal UoWs and compute
        attunement_scale: the largest power-of-2 window size w where the most
        recent w UoWs have success rate >= 80%.

        Returns 0 if the DB is unavailable or has no terminal UoWs.
        """
        if self._db_path is None:
            log.warning("ScalingGovernor: db_path unavailable — treating attunement_scale=0")
            return 0

        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5.0)
            try:
                rows = conn.execute(
                    """
                    SELECT status FROM uow_registry
                    WHERE status IN ('done', 'failed')
                    ORDER BY updated_at DESC
                    LIMIT 500
                    """,
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            log.warning("ScalingGovernor: DB query failed — %s; treating attunement_scale=0", exc)
            return 0

        if not rows:
            return 0

        statuses = [r[0] for r in rows]
        n_rows = len(statuses)

        # Check windows of size w (powers of 2) up to proposed_n.
        # Find the largest w <= proposed_n where the most recent w UoWs
        # have success rate >= 80%.
        best_w = 0
        w = 1
        while w <= proposed_n:
            if w <= n_rows:
                window = statuses[:w]
                success_count = sum(1 for s in window if s == "done")
                rate = success_count / w
                if rate >= 0.80:
                    best_w = w
            w *= 2

        return best_w

    def check(self, proposed_n: int) -> GovernorDecision:
        """
        Check whether the proposed dispatch count should be capped.

        Never raises — all exceptions are caught and result in a safe cap.
        """
        try:
            # Compute attunement_scale first (needed for override reporting too).
            attunement_scale = self._query_attunement_scale(proposed_n)

            # Check override flag.
            if self._read_override_flag():
                log.warning(
                    "ScalingGovernor: scaling_governor_override=true — bypassing cap "
                    "(attunement_scale=%d, proposed_n=%d)",
                    attunement_scale,
                    proposed_n,
                )
                decision = GovernorDecision(
                    proposed_n=proposed_n,
                    allowed_n=proposed_n,
                    capped=False,
                    cap_reason=None,
                    attunement_scale=attunement_scale,
                )
            else:
                decision = _compute_decision(proposed_n, attunement_scale)

            log.info(
                "ScalingGovernor: proposed=%d allowed=%d capped=%s "
                "attunement_scale=%d cap_reason=%s",
                decision.proposed_n,
                decision.allowed_n,
                decision.capped,
                decision.attunement_scale,
                decision.cap_reason,
            )
            return decision

        except Exception as exc:
            log.error(
                "ScalingGovernor: unexpected error — %s; applying safe cap (proposed_n=%d)",
                exc,
                proposed_n,
                exc_info=True,
            )
            # Safe fallback: apply cap with attunement_scale=0.
            fallback = _compute_decision(proposed_n, 0)
            log.info(
                "ScalingGovernor: proposed=%d allowed=%d capped=%s "
                "attunement_scale=%d cap_reason=%s",
                fallback.proposed_n,
                fallback.allowed_n,
                fallback.capped,
                fallback.attunement_scale,
                fallback.cap_reason,
            )
            return fallback
