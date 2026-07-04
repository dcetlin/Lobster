"""
src/utils/artifact_registry.py — shared schema constants for
data/artifact-registry.json (the global artifact registry, per CANON.md).

Canonical home for the four-state law and the default `staleness_thresholds`
config, so `scripts/normalize-artifact-registry.py` and
`scheduled-tasks/canon-digest.py` — the two Type B/migration scripts that
read or write the registry — share one definition rather than each holding
its own copy that can silently drift (see oracle/golden-patterns.md,
"No split canonical homes").

`ArtifactState` follows this codebase's StrEnum convention for closed
string-discriminator sets (see oracle/golden-patterns.md, "PathSelection/type
discriminators get StrEnum"): `state` in the registry is always one of
exactly four values, never free text. `owner` is deliberately NOT modeled
here — CANON.md defines it as `Dan | lobster | <agent-name> | unowned`, an
open-ended field (agent names are not enumerable), so a plain string
sentinel (`"unowned"`) is correct for it, not a StrEnum.
"""

from __future__ import annotations

from enum import StrEnum


class ArtifactState(StrEnum):
    """The four states every artifact in the registry must be in exactly one of."""

    SEED = "seed"
    CADENCE = "cadence"
    ACTIVE_WIP = "active_wip"
    ORPHAN = "orphan"


UNOWNED = "unowned"

DEFAULT_STALENESS_THRESHOLDS = {
    "workstream_active_wip_days": 60,
    "repo_active_wip_days": 60,
    "cadence_missed_cycles": 2,
}

DEFAULT_CLASSIFICATIONS = {
    ArtifactState.SEED: "Reusable golden pattern or template. No time box. No expiry. Updated or deprecated, not killed.",
    ArtifactState.CADENCE: "Owned recurring process. Owner must be named. Liveness: last_activity within 2x schedule period.",
    ArtifactState.ACTIVE_WIP: "Owned, time-boxed work in progress. owner + convergence_target + expiry all required. Past expiry auto-transitions to orphan.",
    ArtifactState.ORPHAN: "No owner, no liveness, or expiry passed. Purge queue. Reversible quarantine -> 14-day dwell -> human-gate delete.",
}

DEFAULT_INVARIANT = (
    "Every artifact converges into a seed/cadence with an owner, or is "
    "stripped and killed — nothing idle+unstable+unowned."
)
