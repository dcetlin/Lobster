"""
Steward — WOS core diagnosis and prescription engine.

The Steward runs every 3 minutes (via steward-heartbeat.py). On each
invocation it processes all `ready-for-steward` UoWs through the
diagnosis→prescribe/close/surface cycle.

Design constraints enforced here:
- Audit-before-transition: every state change writes an audit entry BEFORE
  the transition. If the audit write fails, the transition does not happen.
- Optimistic lock: `UPDATE ... WHERE status = 'ready-for-steward'` checks
  rows affected. If 0, another Steward instance claimed it — skip silently.
- BOOTUP_CANDIDATE_GATE: when True, UoWs whose GitHub issue carries the
  `bootup-candidate` label are skipped. Default is True until the WOS
  validation sequence passes.
- Dry-run mode: diagnose without writing artifacts or transitioning state.
- All DB writes through Registry methods or direct connection (steward-private
  fields are written directly since they are not exposed via Registry's public
  API — this is intentional; the Steward is the sole writer of those fields).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.orchestration.registry import UoW
from src.orchestration.paths import WOS_GATE_CLEARED_FLAG as _GATE_CLEARED_FLAG
from src.orchestration.error_capture import (
    run_subprocess_with_error_capture,
    log_subprocess_error,
    classify_error,
    has_repeated_error,
)
from src.orchestration.config import TimeoutConfig
from src.orchestration.vision_routing import resolve_vision_route
from src.ooda.fast_thorough_selector import select_path as _ooda_select_path, cite_basis as _ooda_cite_basis

log = logging.getLogger("steward")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class LLMPrescriptionError(Exception):
    """Raised when LLM prescription fails and no fallback is permitted."""
    pass

# ---------------------------------------------------------------------------
# LLM prescription dispatch
# ---------------------------------------------------------------------------

def _get_llm_prescription_timeout() -> int:
    """Return the LLM prescription timeout in seconds.

    Uses centralized TimeoutConfig to read LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS
    from the environment. Falls back to default (600s) if absent or non-integer.
    Pure function with respect to state — env reads are isolated here so the
    rest of the module stays deterministic in tests (monkeypatch os.environ as needed).
    """
    return TimeoutConfig.llm_prescription_timeout_secs()


# ---------------------------------------------------------------------------
# Model tiering constants
# ---------------------------------------------------------------------------

# Named model tier constants — reference these instead of raw strings so that
# a model rename is a one-line change and tests catch mismatches.
MODEL_TIER_SONNET = "sonnet"
MODEL_TIER_HAIKU = "haiku"
MODEL_TIER_OPUS = "opus"

# steward_cycles threshold above which a UoW is considered escalated.
# Spec: "steward_cycles > 1 → opus", so the threshold is 1.
# A UoW at steward_cycles == ESCALATION_THRESHOLD is not yet escalated.
# A UoW at steward_cycles > ESCALATION_THRESHOLD is escalated → opus.
ESCALATION_THRESHOLD: int = 1

# UoW types that are pure routing or classification decisions → haiku.
# These types represent pass-through decisions with no deep reasoning required.
_ROUTING_UOW_TYPES: frozenset[str] = frozenset({"routing", "classification"})


def _read_prescription_model_config() -> str | None:
    """Read the prescription_model override from wos-config.json.

    Returns the model string if present and non-empty, else None.
    Extracted as a named function so tests can patch it directly
    without needing to mock the full dispatcher_handlers module.
    """
    try:
        from src.orchestration.dispatcher_handlers import read_wos_config
        config = read_wos_config()
        model = config.get("prescription_model", "")
        if model:
            return model.strip()
    except Exception:
        pass
    return None


def select_steward_model(uow: "UoW") -> str:  # noqa: F821 — UoW imported below module level
    """Select the model tier for a steward prescription, given UoW signals.

    Pure function: all inputs are read from `uow` and the environment;
    no side effects. Safe to call in tests with no DB connection.

    Resolution order (first match wins):
    1. LOBSTER_PRESCRIPTION_MODEL env var — global override, always wins.
    2. prescription_model in wos-config.json — config-level override.
    3. Routing/classification UoW type → haiku (cheapest; no reasoning needed).
    4. Escalated (steward_cycles > ESCALATION_THRESHOLD) → opus (deep reasoning).
    5. Default (first pass or non-escalated non-routing) → sonnet.

    The safe default on missing signals is sonnet (cost-conservative per spec).

    Args:
        uow: The Unit of Work being prescribed.

    Returns:
        A model tier string: one of MODEL_TIER_SONNET, MODEL_TIER_HAIKU,
        MODEL_TIER_OPUS, or an override string from env/config.
    """
    # 1. Environment variable override — highest precedence.
    env_model = os.environ.get("LOBSTER_PRESCRIPTION_MODEL", "").strip()
    if env_model:
        return env_model

    # 2. Config file override — second precedence.
    config_model = _read_prescription_model_config()
    if config_model:
        return config_model

    # 3. Routing/classification decisions → haiku regardless of cycle count.
    #    These are definitionally pass-through — no reasoning depth needed.
    if uow.type in _ROUTING_UOW_TYPES:
        return MODEL_TIER_HAIKU

    # 4. Escalated UoWs (cycled more than once) → opus for full reasoning depth.
    if uow.steward_cycles > ESCALATION_THRESHOLD:
        return MODEL_TIER_OPUS

    # 5. Default: first-pass or non-escalated executable → sonnet.
    return MODEL_TIER_SONNET

# claude binary — resolved from PATH at call time.
_CLAUDE_BIN = "claude"

# Number of consecutive LLM prescription fallbacks that trigger an early-warning
# inbox message.  Each cycle that falls back to deterministic increments the
# consecutive count.  A successful LLM call resets it to zero.
_LLM_FALLBACK_WARNING_THRESHOLD = 3

# Path to Claude Code credentials (OAuth tokens).
_CREDENTIALS_PATH = Path(os.path.expanduser("~/.claude/.credentials.json"))

# Warn (and attempt refresh) when the token expires within this many seconds (2 hours).
_TOKEN_EXPIRY_WARN_SECONDS = 2 * 3600

# Unix timestamps above this threshold are assumed to be in milliseconds rather
# than seconds.  A value of 1e11 seconds would be ~year 5138, so any realistic
# "seconds since epoch" value will be well below this.  Claude Code's credentials
# store uses milliseconds (matching JS Date.now()), so we normalise those here.
_MILLISECOND_TIMESTAMP_THRESHOLD = 1e11

# Anthropic OAuth 2.0 token refresh endpoint.
_ANTHROPIC_OAUTH_TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"

# Timeout for the token refresh HTTP request (seconds).
_TOKEN_REFRESH_HTTP_TIMEOUT = 10


class _TokenStatus:
    """Return values for _check_token_expiry — distinguishes actionable states."""
    FRESH = "fresh"          # token valid, no refresh needed
    NEAR_EXPIRY = "near_expiry"  # within warning window — attempt refresh
    EXPIRED = "expired"      # already expired — attempt refresh
    UNKNOWN = "unknown"      # expiresAt absent or unparseable — do nothing


def _normalise_timestamp(expires_at: int | float) -> float:
    """Convert a numeric expiresAt to Unix seconds, handling millisecond values.

    Claude Code's credentials.json stores expiresAt as a JavaScript timestamp
    (milliseconds since epoch).  Python's datetime.fromtimestamp() expects
    seconds.  Values above _MILLISECOND_TIMESTAMP_THRESHOLD are divided by
    1000 before conversion.

    Returns a float Unix timestamp in seconds.
    """
    if expires_at > _MILLISECOND_TIMESTAMP_THRESHOLD:
        return expires_at / 1000
    return float(expires_at)


def _check_token_expiry(expires_at: object) -> str:
    """Check whether the OAuth token is near expiry or already expired.

    Handles ISO 8601 strings, Unix timestamps in seconds (int/float), and
    Unix timestamps in milliseconds (detected by magnitude).  If the value is
    absent, None, or unparseable, logs at DEBUG level and returns UNKNOWN —
    this is not treated as an error.

    Returns one of the _TokenStatus constants.
    """
    if expires_at is None:
        log.debug("_build_claude_env: expiresAt not present in credentials.json — skipping expiry check")
        return _TokenStatus.UNKNOWN

    now = datetime.now(timezone.utc)

    try:
        if isinstance(expires_at, (int, float)):
            expiry = datetime.fromtimestamp(_normalise_timestamp(expires_at), tz=timezone.utc)
        else:
            expiry = datetime.fromisoformat(str(expires_at))
            # Attach UTC if the parsed datetime is naive.
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError) as exc:
        log.debug(
            "_build_claude_env: could not parse expiresAt %r: %s — skipping expiry check",
            expires_at,
            exc,
        )
        return _TokenStatus.UNKNOWN

    hours_remaining = (expiry - now).total_seconds() / 3600

    if hours_remaining < 0:
        log.error(
            "Claude API token expired %.1f hours ago — attempting refresh",
            abs(hours_remaining),
        )
        return _TokenStatus.EXPIRED
    elif hours_remaining * 3600 < _TOKEN_EXPIRY_WARN_SECONDS:
        log.warning(
            "Claude API token expires in %.1f hours — attempting refresh",
            hours_remaining,
        )
        return _TokenStatus.NEAR_EXPIRY
    else:
        return _TokenStatus.FRESH


def _refresh_oauth_token(refresh_token: str, credentials_path: Path) -> str | None:
    """Exchange a refresh token for a new access token via the Anthropic OAuth endpoint.

    Sends a standard OAuth 2.0 refresh_token grant to
    https://platform.claude.com/v1/oauth/token, writes the new accessToken
    and expiresAt back to credentials_path (preserving all other fields), and
    returns the new access token string.

    On any failure (network error, HTTP error, malformed response) logs an
    error and returns None — the caller should fall back to the existing token.
    This function never raises.

    Args:
        refresh_token:     The long-lived refresh token from credentials.json.
        credentials_path:  Path to the credentials.json file to update on success.

    Returns:
        New access token string on success, None on failure.
    """
    import urllib.request

    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()

    req = urllib.request.Request(
        _ANTHROPIC_OAUTH_TOKEN_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_TOKEN_REFRESH_HTTP_TIMEOUT) as resp:
            body = resp.read()
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        log.error(
            "_refresh_oauth_token: HTTP %d from Anthropic token endpoint — refresh failed",
            exc.code,
        )
        return None
    except urllib.error.URLError as exc:
        log.error(
            "_refresh_oauth_token: network error contacting Anthropic token endpoint: %s",
            exc,
        )
        return None
    except json.JSONDecodeError as exc:
        log.error(
            "_refresh_oauth_token: could not parse response from token endpoint: %s",
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("_refresh_oauth_token: unexpected error during refresh: %s", exc)
        return None

    new_access_token = data.get("access_token", "").strip()
    if not new_access_token:
        log.error(
            "_refresh_oauth_token: token endpoint response missing 'access_token' field"
        )
        return None

    # Compute new expiresAt from the expires_in field (seconds) returned by the
    # server.  Store as a Unix timestamp in seconds (float) — callers that need
    # the millisecond format used by Claude Code can multiply by 1000; we prefer
    # the simpler seconds format here so that _check_token_expiry handles it
    # without ambiguity.
    expires_in: int = data.get("expires_in", 0)
    new_expiry_ts: float = (datetime.now(timezone.utc).timestamp() + expires_in)

    # Merge into the existing credentials.json, preserving all other fields.
    try:
        raw = credentials_path.read_text()
        creds = json.loads(raw)
        creds.setdefault("claudeAiOauth", {})
        creds["claudeAiOauth"]["accessToken"] = new_access_token
        creds["claudeAiOauth"]["expiresAt"] = new_expiry_ts
        credentials_path.write_text(json.dumps(creds, indent=2))
        log.info(
            "_refresh_oauth_token: credentials.json updated with new token (expires in %dh)",
            expires_in // 3600,
        )
    except (OSError, json.JSONDecodeError) as exc:
        # Disk write failed — we still have the new token in memory.  Log the
        # error but return the token so the caller can use it for this cycle.
        log.error(
            "_refresh_oauth_token: could not write updated credentials.json: %s",
            exc,
        )

    return new_access_token


def _build_claude_env() -> dict[str, str]:
    """Build an environment dict suitable for spawning a `claude -p` subprocess.

    Guarantees that `CLAUDE_CODE_OAUTH_TOKEN` is present so the subprocess can
    authenticate without a browser session.  Resolution order:

    1. Current process environment — used as-is when `CLAUDE_CODE_OAUTH_TOKEN`
       is already set (e.g. when the steward runs inside an active CC session).
    2. `~/.claude/.credentials.json` — used when the parent process is a cron
       job that has no inherited OAuth token.  Reads the stored `accessToken`
       from the `claudeAiOauth` sub-object.

    When the slow path reads a token, it also checks `expiresAt`.  If the token
    is within _TOKEN_EXPIRY_WARN_SECONDS of expiry (or already expired) AND a
    `refreshToken` is present, a refresh is attempted before returning.  On a
    successful refresh the new token is used; on failure the old token is
    returned with an error logged.

    If neither source provides a token the env is returned without
    `CLAUDE_CODE_OAUTH_TOKEN` and the subprocess will attempt its own refresh
    (which may fail in headless environments).

    PATH augmentation: always prepends ~/.local/bin to PATH so the `claude`
    binary (installed there by the Claude Code installer) is found even when
    invoked from cron, which strips the user's PATH to /usr/bin:/bin.

    Returns a copy of `os.environ` augmented with the resolved token and PATH,
    ensuring the subprocess inherits all library paths from the parent while
    having a valid auth token and access to ~/.local/bin binaries.
    """
    env = dict(os.environ)

    # Always ensure ~/.local/bin is in PATH so `claude` is found from cron.
    # Cron environments typically only have /usr/bin:/bin; the Claude Code
    # installer puts the binary at ~/.local/bin/claude.
    _local_bin = str(Path.home() / ".local" / "bin")
    current_path = env.get("PATH", "")
    path_entries = current_path.split(":") if current_path else []
    if _local_bin not in path_entries:
        env["PATH"] = _local_bin + (":" + current_path if current_path else "")

    # Fast path: token already present in environment (interactive CC session).
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return env

    # Slow path: read token from credentials.json (cron / headless session).
    try:
        raw = _CREDENTIALS_PATH.read_text()
        creds = json.loads(raw)
        oauth = creds.get("claudeAiOauth", {})
        token = oauth.get("accessToken", "").strip()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            log.debug("_build_claude_env: injected CLAUDE_CODE_OAUTH_TOKEN from credentials.json")

        status = _check_token_expiry(oauth.get("expiresAt"))

        # Attempt refresh when the token is near expiry or already expired.
        if status in (_TokenStatus.NEAR_EXPIRY, _TokenStatus.EXPIRED):
            refresh_token = oauth.get("refreshToken", "").strip()
            if refresh_token:
                new_token = _refresh_oauth_token(refresh_token, _CREDENTIALS_PATH)
                if new_token:
                    env["CLAUDE_CODE_OAUTH_TOKEN"] = new_token
                    log.info("_build_claude_env: token refreshed successfully")
                else:
                    log.error(
                        "_build_claude_env: token refresh failed — proceeding with existing token"
                    )
            else:
                log.warning(
                    "_build_claude_env: token near expiry but no refreshToken in credentials.json"
                    " — cannot refresh automatically"
                )

    except (OSError, json.JSONDecodeError, KeyError) as exc:
        log.warning("_build_claude_env: could not read credentials.json: %s", exc)

    return env


# ---------------------------------------------------------------------------
# Status enum (golden pattern: StrEnum so values serialize as plain strings)
# ---------------------------------------------------------------------------

class UoWStatus(StrEnum):
    PROPOSED = "proposed"
    PENDING = "pending"
    READY_FOR_STEWARD = "ready-for-steward"
    DIAGNOSING = "diagnosing"
    READY_FOR_EXECUTOR = "ready-for-executor"
    ACTIVE = "active"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    EXPIRED = "expired"
    # NEEDS_HUMAN_REVIEW: retry cap exceeded; UoW awaits human decision.
    NEEDS_HUMAN_REVIEW = "needs-human-review"

    def is_terminal(self) -> bool:
        return self in {UoWStatus.DONE, UoWStatus.FAILED, UoWStatus.EXPIRED}

    def is_in_flight(self) -> bool:
        return self in {UoWStatus.ACTIVE, UoWStatus.READY_FOR_EXECUTOR, UoWStatus.DIAGNOSING}


# ---------------------------------------------------------------------------
# Named outcome types (golden pattern: typed return contract for _process_uow)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prescribed:
    uow_id: str
    cycles: int


@dataclass(frozen=True)
class Done:
    uow_id: str


@dataclass(frozen=True)
class Surfaced:
    uow_id: str
    condition: str


@dataclass(frozen=True)
class RaceSkipped:
    uow_id: str


@dataclass(frozen=True)
class WaitForTrace:
    """
    Returned when the corrective trace (trace.json) is absent on the first
    visit to the prescribe branch.  The UoW is left in ``diagnosing`` state;
    the startup_sweep on the next heartbeat will reset it to ``ready-for-steward``
    so the trace gate can be re-evaluated.

    This is the S3-B one-cycle temporal gate outcome — the cristae-junction
    analog that enforces mandatory dwell time between executor return and
    re-prescription.
    """
    uow_id: str


StewardOutcome = Prescribed | Done | Surfaced | RaceSkipped | WaitForTrace


# ---------------------------------------------------------------------------
# Steward decision enums (golden pattern: StrEnum for exhaustiveness checks)
# ---------------------------------------------------------------------------

class ReentryPosture(StrEnum):
    """Categorized executor state from audit trail analysis."""
    FIRST_EXECUTION = "first_execution"
    EXECUTION_COMPLETE = "execution_complete"
    STALL_DETECTED = "stall_detected"
    STARTUP_SWEEP_POSSIBLY_COMPLETE = "startup_sweep_possibly_complete"
    CRASHED_NO_OUTPUT = "crashed_no_output"
    CRASHED_ZERO_BYTES = "crashed_zero_bytes"
    CRASHED_OUTPUT_REF_MISSING = "crashed_output_ref_missing"
    EXECUTION_FAILED = "execution_failed"
    EXECUTOR_ORPHAN = "executor_orphan"
    DIAGNOSING_ORPHAN = "diagnosing_orphan"
    EXECUTING_ORPHAN = "executing_orphan"  # subagent dispatched via inbox, write_result never received (#858)


class ReturnReasonClassification(StrEnum):
    """Classification of return_reason strings."""
    NORMAL = "normal"
    BLOCKED = "blocked"
    ABNORMAL = "abnormal"
    ERROR = "error"
    ORPHAN = "orphan"


class StuckCondition(StrEnum):
    """Conditions that trigger surface-to-Dan."""
    HARD_CAP = "hard_cap"
    CRASH_REPEATED = "crash_repeated"
    PHILOSOPHICAL_REGISTER = "philosophical_register"
    NO_GATE_IMPROVEMENT = "no_gate_improvement"
    EXECUTOR_BLOCKED = "executor_blocked"
    REGISTER_MISMATCH = "register_mismatch"


# ---------------------------------------------------------------------------
# Typed diagnostic and result structures (golden pattern: frozen dataclasses)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Diagnosis:
    """
    Result of diagnosing a UoW's execution state.

    All fields are immutable. Created by _diagnose_uow() as a pure
    computation from UoW + audit trail inputs.
    """
    reentry_posture: str  # Use str to allow ReentryPosture or legacy string values
    return_reason: str | None
    return_reason_classification: str  # Use str to allow ReturnReasonClassification or legacy
    output_content: str
    output_valid: bool
    is_complete: bool
    completion_rationale: str
    stuck_condition: str | None  # Use str to allow StuckCondition or legacy string values
    executor_outcome: str | None
    success_criteria_missing: bool


@dataclass(frozen=True, slots=True)
class IssueInfo:
    """
    GitHub issue information fetched via gh CLI.

    Replaces dict[str, Any] return from _fetch_github_issue.
    """
    status_code: int
    state: str | None
    labels: list[str]
    body: str
    title: str


@dataclass(frozen=True, slots=True)
class LLMPrescription:
    """
    Result from LLM-based prescription generation.

    Replaces dict[str, Any] return from _llm_prescribe.
    """
    instructions: str
    success_criteria_check: str
    estimated_cycles: int


@dataclass(frozen=True, slots=True)
class CycleResult:
    """
    Result of a complete Steward heartbeat cycle.

    Replaces dict[str, Any] return from run_steward_cycle.
    """
    evaluated: int
    prescribed: int
    done: int
    surfaced: int
    skipped: int
    race_skipped: int
    wait_for_trace: int
    considered_ids: tuple[str, ...]  # Use tuple for hashability with frozen=True
    # shard_blocked: UoWs skipped this cycle because the shard-stream parallel
    # dispatch gate blocked them (file_scope conflict, shard serialization, or
    # max_parallel cap). They will be retried on the next heartbeat.
    shard_blocked: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Convert to dict for backward compatibility with callers expecting dict."""
        return {
            "evaluated": self.evaluated,
            "prescribed": self.prescribed,
            "done": self.done,
            "surfaced": self.surfaced,
            "skipped": self.skipped,
            "race_skipped": self.race_skipped,
            "wait_for_trace": self.wait_for_trace,
            "shard_blocked": self.shard_blocked,
            "considered_ids": list(self.considered_ids),
        }


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# _GATE_CLEARED_FLAG is imported from src.orchestration.paths (WOS_GATE_CLEARED_FLAG).
# See paths.py for the single canonical definition.


def is_bootup_candidate_gate_active() -> bool:
    """Return True if BOOTUP_CANDIDATE_GATE is active (blocking bootup-candidates).

    Returns False when the wos-gate-cleared file flag exists, indicating the
    WOS validation sequence has passed and all UoWs should be processed.

    This function reads from disk on every call so that the gate state is always
    current — cron processes get a fresh read on every invocation.
    """
    return not _GATE_CLEARED_FLAG.exists()


# When True, the Steward skips UoWs with the `bootup-candidate` label.
# Evaluated at module load; re-evaluated on each cron process start.
# To clear: create ~/lobster-workspace/data/wos-gate-cleared (or /wos unblock).
BOOTUP_CANDIDATE_GATE: bool = is_bootup_candidate_gate_active()

# Status values — use UoWStatus StrEnum (kept as aliases for backward compat)
_STATUS_READY_FOR_STEWARD = UoWStatus.READY_FOR_STEWARD
_STATUS_DIAGNOSING = UoWStatus.DIAGNOSING
_STATUS_READY_FOR_EXECUTOR = UoWStatus.READY_FOR_EXECUTOR
_STATUS_DONE = UoWStatus.DONE
_STATUS_BLOCKED = UoWStatus.BLOCKED
_STATUS_NEEDS_HUMAN_REVIEW = UoWStatus.NEEDS_HUMAN_REVIEW

# Actor identifier written to audit entries
_ACTOR_STEWARD = "steward"

# Hard cap: surface to Dan unconditionally if lifetime_cycles >= this value.
# lifetime_cycles accumulates across all decide-retry resets, so this is a true
# per-UoW-lifetime circuit breaker. steward_cycles (per-attempt) is NOT used here.
_HARD_CAP_CYCLES = 9

# Retry cap: maximum number of confirmed execution attempts before escalating a UoW to
# needs-human-review. Applied at re-dispatch time and gates on execution_attempts
# (confirmed dispatches), NOT retry_count (total steward cycles) or steward_cycles.
#
# Rationale: infrastructure kill events (session TTL, orphan recovery) must not consume
# the retry budget. Only cycles where an agent confirmed execution (return_reason is not
# an orphan classification) count toward this cap. See issue #962.
MAX_RETRIES: int = 3

# Return reasons that represent infrastructure kill events, not confirmed executions.
# When return_reason is in this set, execution_attempts must NOT be incremented and
# the retry cap must NOT apply. The agent session was killed before or during dispatch —
# no execution outcome was produced.
#
# Mapping to _RETURN_REASON_CLASSIFICATIONS:
#   executor_orphan   → _CLASSIFICATION_ORPHAN (session killed before dispatch)
#   executing_orphan  → _CLASSIFICATION_ORPHAN (subagent dispatched, write_result never received)
#   diagnosing_orphan → _CLASSIFICATION_ORPHAN (startup sweep classified during diagnosis)
ORPHAN_REASONS: frozenset[str] = frozenset({
    "executor_orphan",
    "executing_orphan",
    "diagnosing_orphan",
})


def _is_infrastructure_event(return_reason: str | None) -> bool:
    """
    Return True when return_reason represents an infrastructure kill event that
    must NOT consume execution_attempts budget.

    Pure function — no side effects, no I/O.

    Infrastructure events (ORPHAN_REASONS) are session kills or dispatch failures
    where no agent confirmed execution. None (first execution) is not infrastructure.

    Used in the retry-cap check in _process_uow to gate MAX_RETRIES on
    execution_attempts rather than retry_count.
    """
    if return_reason is None:
        return False
    return return_reason in ORPHAN_REASONS


# close_reason written by the cleanup arc when a UoW hits the hard cap.
# Used to gate decide-retry: bare retry is rejected; explicit override required.
CLOSE_REASON_HARD_CAP_CLEANUP = "hard_cap_cleanup"

# Default path for failure trace files written by the cleanup arc.
_DEFAULT_FAILURE_TRACES_DIR = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
) / "orchestration" / "failure-traces"

# Default path for archived artifact directories written by the cleanup arc.
_DEFAULT_ARTIFACTS_ARCHIVED_DIR = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
) / "orchestration" / "artifacts" / "archived"

# Default path for UoW artifact directories (active, pre-archival).
_DEFAULT_ARTIFACTS_DIR = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
) / "orchestration" / "artifacts"

# Early warning threshold: notify Dan when lifetime_cycles + steward_cycles reaches this value.
# Uses cumulative lifetime_cycles + new_cycles (post-prescription) so the warning fires based
# on total cycles across all decide-retry rounds, not just the current attempt.
_EARLY_WARNING_CYCLES = 4

# Crash surface threshold: surface if crashed_no_output and steward_cycles >= this value.
# Uses per-attempt steward_cycles (not lifetime_cycles) — crash detection is per-attempt.
_CRASH_SURFACE_CYCLES = 2

# Fields required by the Steward for operation
_STEWARD_REQUIRED_FIELDS = frozenset({
    "workflow_artifact",
    "success_criteria",
    "prescribed_skills",
    "steward_cycles",
    "lifetime_cycles",
    "timeout_at",
    "estimated_runtime",
    "steward_agenda",
    "steward_log",
})

# Executor types
_EXECUTOR_TYPE_GENERAL = "general"
_EXECUTOR_TYPE_FUNCTIONAL_ENGINEER = "functional-engineer"
_EXECUTOR_TYPE_LOBSTER_OPS = "lobster-ops"
_EXECUTOR_TYPE_LOBSTER_GENERALIST = "lobster-generalist"
_EXECUTOR_TYPE_LOBSTER_META = "lobster-meta"

#: Register → executor_type mapping (issue #842).
#: Register is the primary routing signal — it is classified at germination time
#: by the Germinator and is stable. Keyword-matching on the summary was the prior
#: approach; it is replaced by this declarative table.
#:
#: Fallback for unknown registers: lobster-generalist (safe default — generalist
#: handles ambiguous cases without risking implementation work on philosophical UoWs).
_REGISTER_TO_EXECUTOR_TYPE: dict[str, str] = {
    "operational": _EXECUTOR_TYPE_FUNCTIONAL_ENGINEER,
    "iterative-convergent": _EXECUTOR_TYPE_FUNCTIONAL_ENGINEER,
    "human-judgment": _EXECUTOR_TYPE_LOBSTER_GENERALIST,
    "philosophical": _EXECUTOR_TYPE_LOBSTER_META,
}

# Return reason classifications
_CLASSIFICATION_NORMAL = "normal"
_CLASSIFICATION_BLOCKED = "blocked"
_CLASSIFICATION_ABNORMAL = "abnormal"
_CLASSIFICATION_ERROR = "error"
_CLASSIFICATION_ORPHAN = "orphan"

_RETURN_REASON_CLASSIFICATIONS: dict[str, str] = {
    "observation_complete": _CLASSIFICATION_NORMAL,
    "needs_steward_review": _CLASSIFICATION_NORMAL,
    "blocked": _CLASSIFICATION_BLOCKED,
    "timeout": _CLASSIFICATION_ABNORMAL,
    "stall_detected": _CLASSIFICATION_ABNORMAL,
    "execution_failed": _CLASSIFICATION_ERROR,
    "crashed_no_output": _CLASSIFICATION_ERROR,
    "crashed_zero_bytes": _CLASSIFICATION_ERROR,
    "crashed_output_ref_missing": _CLASSIFICATION_ERROR,
    "executor_orphan": _CLASSIFICATION_ORPHAN,
    "diagnosing_orphan": _CLASSIFICATION_ORPHAN,
    "executing_orphan": _CLASSIFICATION_ORPHAN,  # subagent dispatched but write_result never received (#858)
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify_return_reason(return_reason: str | None) -> str:
    """Map a return_reason string to its classification. Unknown → 'error' (conservative)."""
    if return_reason is None:
        return _CLASSIFICATION_NORMAL
    return _RETURN_REASON_CLASSIFICATIONS.get(return_reason, _CLASSIFICATION_ERROR)


# ---------------------------------------------------------------------------
# Per-cycle steward trace logging
# ---------------------------------------------------------------------------

_CYCLE_TRACE_EXCERPT_MAX = 200
_DEFAULT_CYCLE_TRACE_DIR = Path(
    os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
) / "orchestration" / "artifacts"


def _append_cycle_trace(
    uow_id: str,
    cycle_num: int,
    subagent_excerpt: str,
    return_reason: str,
    next_action: str,
    artifact_dir: Path | None = None,
) -> None:
    """Append one JSONL entry to <artifact_dir>/<uow_id>.cycles.jsonl.

    Each entry records the outcome of a single steward cycle, enabling
    post-hoc debugging of multi-cycle UoW lifecycles.

    Args:
        uow_id: The UoW identifier.
        cycle_num: The current steward_cycles value (pre-increment).
        subagent_excerpt: Text from the executor output (output_ref), truncated
            to _CYCLE_TRACE_EXCERPT_MAX chars with a trailing ellipsis if longer.
        return_reason: The return_reason from diagnosis, or empty string.
        next_action: One of 'prescribed', 'done', 'surfaced', 'stuck'.
        artifact_dir: Override for the artifact directory. Defaults to
            ~/lobster-workspace/orchestration/artifacts.
    """
    resolved_dir = Path(artifact_dir) if artifact_dir is not None else _DEFAULT_CYCLE_TRACE_DIR
    resolved_dir.mkdir(parents=True, exist_ok=True)

    # Truncate excerpt with ellipsis if it exceeds the max length
    excerpt = subagent_excerpt
    if len(excerpt) > _CYCLE_TRACE_EXCERPT_MAX:
        excerpt = excerpt[:_CYCLE_TRACE_EXCERPT_MAX] + "\u2026"

    entry = {
        "cycle_num": cycle_num,
        "subagent_excerpt": excerpt,
        "return_reason": return_reason,
        "next_action": next_action,
        "timestamp": _now_iso(),
    }

    trace_path = resolved_dir / f"{uow_id}.cycles.jsonl"
    with trace_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _parse_audit_log(audit_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract structured entries from the audit_log rows passed in.
    Returns a list of audit entries (from the `note` field, JSON-parsed).
    """
    # Audit entries are passed as a list from the registry queries
    return audit_entries


def _most_recent_return_reason(audit_entries: list[dict]) -> str | None:
    """
    Extract the most recent return_reason from audit entries.
    Looks for the last audit_log entry with a `return_reason` key in its note.

    For `execution_complete` events: return `"execution_complete"` as the
    authoritative signal even when the note does not carry an explicit
    `return_reason` or `classification`.  Formerly, the absence of those fields
    caused the function to fall through and pick up the nearest prior
    `startup_sweep executor_orphan` entry — making the Steward treat a
    successful Executor dispatch as an orphan and re-prescribe indefinitely.
    """
    for entry in reversed(audit_entries):
        event = entry.get("event", "")
        note = entry.get("note")

        note_data: dict = {}
        if note:
            try:
                note_data = json.loads(note)
            except (json.JSONDecodeError, TypeError):
                pass

        # Explicit return_reason in note always wins regardless of event type.
        if "return_reason" in note_data:
            return note_data["return_reason"]

        # Event-type defaults: return the canonical reason for each terminal event.
        if event == "execution_complete":
            # Authoritative: Executor successfully dispatched.  Return immediately
            # so older startup_sweep entries cannot mask this completion.
            return "execution_complete"
        elif event == "startup_sweep":
            clf = note_data.get("classification")
            if clf:
                return clf
        elif event == "execution_failed":
            clf = note_data.get("return_reason") or note_data.get("classification")
            if clf:
                return clf

    return None


def _most_recent_classification(audit_entries: list[dict]) -> str | None:
    """
    Extract the most recent startup_sweep classification from audit entries.
    Returns the classification value (e.g. 'crashed_no_output') or None.
    """
    for entry in reversed(audit_entries):
        event = entry.get("event", "")
        if event == "startup_sweep":
            note = entry.get("note")
            if note:
                try:
                    data = json.loads(note)
                    return data.get("classification")
                except (json.JSONDecodeError, TypeError):
                    pass
    return None


def _output_ref_is_valid(output_ref: str | None) -> bool:
    """Return True if output_ref is a path to a non-empty file."""
    if not output_ref:
        return False
    try:
        p = Path(output_ref)
        return p.exists() and p.stat().st_size > 0
    except Exception as e:
        log.debug(f"Error checking output_ref {output_ref}: {type(e).__name__}: {e}", exc_info=True)
        return False


def _read_output_ref(output_ref: str | None) -> str:
    """Read and return output_ref file contents, or empty string."""
    if not output_ref:
        return ""
    try:
        return Path(output_ref).read_text(encoding="utf-8")
    except Exception as e:
        log.debug(f"Error reading output_ref {output_ref}: {type(e).__name__}: {e}", exc_info=True)
        return ""


def _determine_reentry_posture(
    audit_entries: list[dict],
    return_reason: str | None,
) -> str:
    """
    Determine the re-entry posture based on most recent audit event.

    Returns a string label:
    - 'execution_complete': normal re-entry
    - 'stall_detected': observation loop surfaced a timeout
    - 'startup_sweep_possibly_complete': crash recovery, partial output
    - 'crashed_no_output': crash with no usable output
    - 'execution_failed': executor failure
    - 'executor_orphan': executor never ran
    - 'first_execution': no prior execution cycle (steward_cycles == 0)
    """
    if not audit_entries:
        return "first_execution"

    if return_reason == "executor_orphan":
        return "executor_orphan"

    if return_reason == "executing_orphan":
        return "executing_orphan"

    classification = _RETURN_REASON_CLASSIFICATIONS.get(return_reason or "", None)

    if classification == _CLASSIFICATION_NORMAL:
        return "execution_complete"
    elif classification == _CLASSIFICATION_ABNORMAL:
        return "stall_detected"
    elif classification == _CLASSIFICATION_ERROR:
        if return_reason == "crashed_no_output":
            return "crashed_no_output"
        return "execution_failed"
    elif classification == _CLASSIFICATION_ORPHAN:
        return "executor_orphan"
    else:
        # Fall back to audit event inspection
        for entry in reversed(audit_entries):
            event = entry.get("event", "")
            if event == "execution_complete":
                return "execution_complete"
            elif event == "stall_detected":
                return "stall_detected"
            elif event == "startup_sweep":
                note = entry.get("note", "")
                try:
                    data = json.loads(note) if note else {}
                except (json.JSONDecodeError, TypeError):
                    data = {}
                clf = data.get("classification", "")
                if clf == "possibly_complete":
                    return "startup_sweep_possibly_complete"
                elif clf in ("crashed_no_output", "crashed_zero_bytes", "crashed_output_ref_missing"):
                    return clf
                elif clf == "executor_orphan":
                    return "executor_orphan"
                elif clf == "diagnosing_orphan":
                    return "diagnosing_orphan"
                elif clf == "executing_orphan":
                    return "executing_orphan"
            elif event == "execution_failed":
                return "execution_failed"

    return "first_execution"


def _assess_completion(
    uow: UoW,
    output_content: str,
    reentry_posture: str,
) -> tuple[bool, str, str | None]:
    """
    Assess whether the UoW output satisfies the original intent (Seed).

    Returns (is_complete: bool, rationale: str, executor_outcome: str | None).

    executor_outcome is the `outcome` field from the result file when found
    (e.g. "complete", "partial", "failed", "blocked"), or None when no valid
    result file was found. Callers must check executor_outcome == "blocked"
    to route immediately to Dan — the is_complete flag does not encode this.

    Completion requires ALL of:
    - output_ref is not NULL and file exists and is non-empty
    - Most recent execution cycle had execution_complete (not stall/crash)
    - Output content confirms original intent is addressed
    - lifetime_cycles < HARD_CAP_CYCLES
    """
    cycles = uow.lifetime_cycles
    if cycles >= _HARD_CAP_CYCLES:
        return False, f"hard_cap: lifetime_cycles={cycles} >= {_HARD_CAP_CYCLES}", None

    if reentry_posture == "first_execution":
        return False, "first_execution: awaiting executor dispatch", None

    output_ref = uow.output_ref
    if not _output_ref_is_valid(output_ref):
        return False, "output_ref is null or file does not exist or is empty", None

    if reentry_posture not in ("execution_complete", "startup_sweep_possibly_complete"):
        return False, f"re-entry posture is {reentry_posture!r} — not a normal completion", None

    if not output_content.strip():
        return False, "output file is empty", None

    # Deterministic completion check: look for a structured result file.
    # The Executor is expected to write `{output_ref}.result.json` with the
    # `outcome` field as the primary routing signal (executor-contract.md §Schema).
    # `success` is a backward-compat convenience field; `outcome` is always read first.
    output_ref = uow.output_ref
    if output_ref:
        result_file = Path(output_ref).with_suffix(".result.json")
        if not result_file.exists():
            # Also check the alternate naming convention: append .result.json suffix
            result_file_alt = Path(str(output_ref) + ".result.json")
            if result_file_alt.exists():
                result_file = result_file_alt
        if result_file.exists():
            try:
                result_data = json.loads(result_file.read_text(encoding="utf-8"))

                # Gap 2 (executor-contract.md): validate uow_id BEFORE reading any
                # other field. A misrouted result file must be treated as absence.
                result_uow_id = result_data.get("uow_id")
                if result_uow_id is not None and result_uow_id != uow.id:
                    log.warning(
                        "Result file %s has uow_id=%r but expected %r — "
                        "treating as absent (misrouted result file)",
                        result_file, result_uow_id, uow.id,
                    )
                    # Fall through to the no-result-file path below
                else:
                    # Gap 1 (executor-contract.md): `outcome` is the primary routing
                    # signal. Read it first; `success` is a backward-compat fallback.
                    outcome = result_data.get("outcome")
                    reason = result_data.get("reason", "no reason provided")

                    if outcome == "complete":
                        # PR C: Apply register-aware completion policy before closing.
                        policy = _register_completion_policy(uow.register)
                        if policy == "always-surface":
                            # philosophical: always surface to Dan — completion requires
                            # human judgment regardless of what result.json says.
                            return (
                                False,
                                f"register=philosophical: completion requires human judgment — "
                                f"surfacing to Dan (outcome={outcome})",
                                "philosophical_surface",
                            )
                        elif policy == "require-confirmation":
                            # human-judgment: requires Dan's explicit close_reason.
                            if uow.close_reason:
                                return True, f"outcome=complete: {result_file.name} (Dan confirmed)", "complete"
                            return (
                                False,
                                "register=human-judgment: awaiting Dan's explicit confirmation (close_reason not set)",
                                "human_judgment_pending",
                            )
                        # machine-gate (operational, iterative-convergent): fall through
                        return True, f"outcome=complete: {result_file.name}", "complete"
                    elif outcome == "blocked":
                        # Gap 3: `blocked` always routes to Dan — the Executor has
                        # determined that external resolution is required.
                        # Return is_complete=False so the normal prescription path
                        # is skipped; the caller must check executor_outcome for routing.
                        return False, f"outcome=blocked: {reason}", "blocked"
                    elif outcome in ("partial", "failed"):
                        return False, f"outcome={outcome}: {reason}", outcome
                    elif outcome is not None:
                        # Unknown outcome value — conservative non-completion
                        log.warning(
                            "Result file %s has unknown outcome=%r — treating as non-completion",
                            result_file, outcome,
                        )
                        return False, f"unknown outcome={outcome!r} in result file", outcome
                    else:
                        # No `outcome` field — fall back to `success` for backward
                        # compatibility with result files written before contract v1.
                        if result_data.get("success") is True:
                            return True, f"structured result file confirms success (legacy): {result_file.name}", None
                        elif result_data.get("success") is False:
                            return False, f"structured result file reports failure (legacy): {reason}", None
                        # If neither field is present, fall through to conservative check
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not parse result file %s: %s", result_file, e)

    # No structured result file found (or result file was invalid/misrouted).
    # Require a result file regardless of whether success_criteria is set.
    # A missing result.json means the Executor has not confirmed completion —
    # the subagent may have exited 0 without opening a PR or calling write_result.
    # Declaring done without evidence is the bug described in issue #648 Part B.
    success_criteria = uow.success_criteria
    if success_criteria:
        return False, (
            f"no structured result file ({output_ref}.result.json) found — "
            f"cannot verify success_criteria without Executor confirmation: {success_criteria[:80]}"
        ), None
    else:
        # Hard gate: require result.json even when success_criteria is NULL.
        # The legacy fallback (trust output_ref presence alone) is removed —
        # it could declare done when the subagent exited 0 without producing
        # any artifact (PR, write_result call, etc.). See issue #648 Part B.
        return False, (
            f"no structured result file ({output_ref}.result.json) found — "
            f"Executor confirmation required even when success_criteria is NULL: {uow.summary[:80]}"
        ), None


# ---------------------------------------------------------------------------
# Per-cycle trace entry builder (pure functions — no DB writes)
# ---------------------------------------------------------------------------

def _posture_rationale(diagnosis: Diagnosis, cycles: int, trace_posture: str | None = None) -> str:
    """
    Return a 1-sentence rationale for the current posture.

    Pure function: derives the string deterministically from diagnosis fields.
    No LLM, no DB reads. Called by _build_trace_entry().

    Args:
        diagnosis: Typed Diagnosis dataclass returned by _diagnose_uow().
        cycles: Current steward_cycles count.
        trace_posture: Optional trace posture (v2 vocabulary per ADR-004). If provided,
            rationale is written for the trace posture; otherwise falls back to reentry_posture.
    """
    posture = diagnosis.reentry_posture
    stuck_condition = diagnosis.stuck_condition

    # Use trace posture vocabulary if provided (S3P2-F reconciliation)
    if trace_posture:
        if trace_posture == "orienting":
            return "First contact — establishing scope and dispatching initial execution."
        elif trace_posture == "clarifying":
            if posture == "execution_complete":
                return "Executor result present — assessing completion criteria."
            return "Evaluating current state to determine next action."
        elif trace_posture == "waiting_for_signal":
            return "Blocked on external input — awaiting response before proceeding."
        elif trace_posture == "scope_challenged":
            if stuck_condition:
                return f"Anomaly detected: {stuck_condition} — recovery in progress."
            if posture in ("crashed_no_output", "crashed_output_ref_missing"):
                return "Executor crash detected — analyzing failure for recovery."
            if posture == "executor_orphan":
                return "Executor never claimed UoW — re-prescribing."
            if posture == "executing_orphan":
                return "Subagent dispatched but write_result never received — re-prescribing."
            return "Unexpected state encountered — investigating."
        elif trace_posture == "closing_with_discovery":
            return "Completion criteria satisfied — closing with findings documented."
        else:
            return f"Trace posture: {trace_posture}."

    # Fallback: use reentry_posture vocabulary (internal diagnostic codes)
    match posture:
        case "first_execution":
            return "No prior audit entries — first steward contact, dispatching executor."
        case "execution_complete":
            return "Executor result file present and valid — assessing completion."
        case "crashed_output_ref_missing":
            return "Startup sweep detected missing output_ref — executor may have crashed."
        case "executor_orphan":
            return "UoW stuck in ready-for-executor beyond threshold — executor never claimed."
        case "diagnosing_orphan":
            return "Steward crashed mid-diagnosis — re-diagnosing from current state."
        case "executing_orphan":
            return "UoW stuck in executing — subagent dispatched but write_result never received (#858)."
        case "steward_cycle_cap":
            return f"Steward cycle cap reached ({cycles} cycles) — surfacing to Dan."
        case _:
            return f"Posture: {posture}."


def _extract_criteria_checks(diagnosis: Diagnosis) -> list[dict]:
    """
    Extract success criteria check results from the Diagnosis.

    Pure function: maps is_complete + completion_rationale to a list of
    check dicts with {name, passed, evidence}. Always returns a list.
    """
    return [
        {
            "name": "completion_check",
            "passed": bool(diagnosis.is_complete),
            "evidence": diagnosis.completion_rationale,
        }
    ]


def _posture_prediction(diagnosis: Diagnosis) -> str | None:
    """
    Return a forward prediction string based on the diagnosis.

    Pure function: deterministic based on posture and completion state.
    Returns None only when there is genuinely nothing to predict (done).
    """
    posture = diagnosis.reentry_posture
    is_complete = diagnosis.is_complete
    stuck_condition = diagnosis.stuck_condition

    if stuck_condition:
        return "Will be surfaced to Dan — stuck condition detected."
    if is_complete:
        return "Closure will be declared — completion criteria satisfied."

    match posture:
        case "first_execution":
            return "Executor will be dispatched for first execution pass."
        case "execution_complete":
            return "Completion check will determine next action (prescribe or close)."
        case "crashed_no_output" | "execution_failed":
            return "Re-prescription will be issued after failure analysis."
        case "executor_orphan":
            return "Re-prescription will be issued — executor never claimed UoW."
        case "executing_orphan":
            return "Re-prescription will be issued — subagent dispatched but write_result never received."
        case _:
            return "Next prescription will be determined from diagnosis output."


# ---------------------------------------------------------------------------
# Trace posture derivation (S3P2-F vocabulary reconciliation)
# ---------------------------------------------------------------------------

def _determine_trace_posture(diagnosis: Diagnosis) -> str:
    """
    Derive the narrative trace posture from reentry classification and diagnosis state.

    This function reconciles the internal diagnostic classification (reentry_posture)
    with the v2 design trace posture vocabulary per ADR-004.

    Trace posture vocabulary (wos-v2-design.md / PR #564):
    - orienting: First contact, establishing scope
    - clarifying: Assessing completion, determining next steps
    - waiting_for_signal: Blocked on external input (Dan, system)
    - scope_challenged: Anomaly detected, recovery in progress
    - closing_with_discovery: Completion confirmed, wrapping up

    Returns:
        One of the five trace posture values.
    """
    reentry_classification = diagnosis.reentry_posture
    is_complete = diagnosis.is_complete
    stuck_condition = diagnosis.stuck_condition
    executor_outcome = diagnosis.executor_outcome

    # Stuck conditions → scope_challenged (anomaly recovery)
    if stuck_condition:
        return "scope_challenged"

    # Completion confirmed → closing_with_discovery
    if is_complete:
        return "closing_with_discovery"

    # First execution → orienting (establishing scope)
    if reentry_classification == "first_execution":
        return "orienting"

    # Blocked outcomes → waiting_for_signal
    if executor_outcome == "blocked":
        return "waiting_for_signal"

    # Crash/failure states → scope_challenged (anomaly recovery)
    if reentry_classification in (
        "crashed_no_output",
        "crashed_zero_bytes",
        "crashed_output_ref_missing",
        "execution_failed",
        "executor_orphan",
        "diagnosing_orphan",
        "executing_orphan",
        "stall_detected",
    ):
        return "scope_challenged"

    # Normal re-entry with work to do → clarifying
    if reentry_classification in ("execution_complete", "startup_sweep_possibly_complete"):
        return "clarifying"

    # Default fallback → clarifying (assessing state)
    return "clarifying"


def _build_trace_entry(diagnosis: Diagnosis, cycles: int) -> dict:
    """
    Build a single steward_agenda trace entry from a completed diagnosis.

    Pure function — no side effects, no DB writes. Called pre-branch in
    _process_uow() after diagnosis and before the stuck/done/prescribe split.

    Args:
        diagnosis: typed Diagnosis returned by _diagnose_uow().
        cycles: current uow.steward_cycles (pre-increment).

    Returns:
        Trace entry dict conforming to the v2 cycle trace entry schema.
    """
    # Derive narrative trace posture from diagnostic classification (S3P2-F)
    trace_posture = _determine_trace_posture(diagnosis)

    return {
        "cycle": cycles,
        "posture": trace_posture,
        "posture_rationale": _posture_rationale(diagnosis, cycles, trace_posture),
        "success_criteria_checked": _extract_criteria_checks(diagnosis),
        "anomalies": (
            [diagnosis.stuck_condition]
            if diagnosis.stuck_condition
            else []
        ),
        "prediction": _posture_prediction(diagnosis),
        "dispatch_instruction": None,  # filled in by prescribe branch if applicable
        "external_dependency": None,
        "discoveries": [],
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def _parse_steward_agenda(steward_agenda_str: str | None) -> list[dict]:
    """
    Parse steward_agenda JSON string into a list of dicts.

    Pure function. Returns [] on None, empty string, or parse failure.
    """
    if not steward_agenda_str:
        return []
    try:
        result = json.loads(steward_agenda_str)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _build_initial_agenda(uow: "UoW", issue_body: str) -> list[dict[str, Any]]:
    """
    Build the initial steward_agenda for a new UoW (steward_cycles == 0).

    Forecast depth calibrated by UoW type:
    - Well-defined (concrete deliverable): full agenda upfront
    - Open-ended (exploratory): 1-2 steps + 'pending evaluation' marker
    """
    summary = uow.summary
    success_criteria = uow.success_criteria or None

    # Heuristic: well-defined if success_criteria is present and summary is specific
    is_well_defined = bool(success_criteria and len(summary) > 20)

    if is_well_defined:
        return [
            {
                "posture": "solo",
                "context": f"Initial execution: {summary[:120]}",
                "constraints": [],
                "status": "pending",
            },
            {
                "posture": "verify",
                "context": "Steward verifies output against success_criteria",
                "constraints": [],
                "status": "pending",
            },
        ]
    else:
        return [
            {
                "posture": "explore",
                "context": f"Exploratory first step: {summary[:120]}",
                "constraints": [],
                "status": "pending",
            },
            {
                "posture": "pending_evaluation",
                "context": "pending evaluation — agenda will be updated after initial output",
                "constraints": [],
                "status": "pending",
            },
        ]


def _select_executor_type(uow: "UoW") -> str:
    """
    Select the executor type for the UoW based on its register field.

    The register is the primary routing signal — it is classified at germination
    time by the Germinator and is stable across the UoW lifecycle. Keyword-
    matching on the summary was the prior approach (issue #842 — a philosophical
    UoW with "fix" in the summary would incorrectly route to functional-engineer).

    Mapping (see _REGISTER_TO_EXECUTOR_TYPE):
        operational          → functional-engineer
        iterative-convergent → functional-engineer
        human-judgment       → lobster-generalist
        philosophical        → lobster-meta
        <unknown>            → lobster-generalist  (safe fallback, warns in log)

    Returns one of the _EXECUTOR_TYPE_* constants.
    """
    register = uow.register or ""
    executor_type = _REGISTER_TO_EXECUTOR_TYPE.get(register)
    if executor_type is not None:
        return executor_type

    # Unknown register — warn and fall back to lobster-generalist (safe default).
    # lobster-generalist handles ambiguous work without risking implementation
    # on philosophical or human-judgment UoWs that need different treatment.
    log.warning(
        "_select_executor_type: unknown register %r for UoW %s — falling back to %s",
        register,
        getattr(uow, "id", "<unknown>"),
        _EXECUTOR_TYPE_LOBSTER_GENERALIST,
    )
    return _EXECUTOR_TYPE_LOBSTER_GENERALIST


# Register → compatible executor types mapping (spec: Change 3).
# frontier-writer and design-review are V3 gated register names — they exist
# in the table to drive mismatch detection, but do not yet have dispatch
# implementations. When the mismatch gate fires for philosophical or
# human-judgment UoWs, the Steward surfaces to Dan for manual routing.
_REGISTER_COMPATIBLE_EXECUTORS: dict[str, frozenset[str]] = {
    "operational": frozenset({"functional-engineer", "lobster-ops", "general"}),
    "iterative-convergent": frozenset({"functional-engineer", "lobster-ops"}),
    "philosophical": frozenset({"lobster-meta", "frontier-writer"}),
    "human-judgment": frozenset({"lobster-generalist", "design-review"}),
}


def _check_register_executor_compatibility(
    register: str,
    executor_type: str,
) -> tuple[bool, str]:
    """Check whether executor_type is compatible with a UoW's register.

    Pure function. Returns (is_compatible, reason).

    Compatible means the executor type is listed in the compatible set for
    the register. Unknown registers are treated as compatible with any executor
    (conservative — do not block unknown registers).

    Examples:
        ("philosophical", "functional-engineer") → (False, "philosophical→functional-engineer: ...")
        ("operational", "functional-engineer")   → (True, "")
    """
    compatible_types = _REGISTER_COMPATIBLE_EXECUTORS.get(register)
    if compatible_types is None:
        # Unknown register: allow through to avoid blocking unknown future registers
        return True, f"unknown register {register!r} — allowing through"

    if executor_type in compatible_types:
        return True, ""

    direction = f"{register}\u2192{executor_type}"
    reason = (
        f"register {register!r} is incompatible with executor_type {executor_type!r} "
        f"({direction}). Compatible types: {sorted(compatible_types)}"
    )
    return False, reason


def _build_prescription_route_reason(
    uow: "UoW",
    reentry_posture: str,
    executor_outcome: str,
    partial_steps_context: str,
    completion_rationale: str,
) -> str:
    """
    Build the route_reason string for a prescription cycle.

    When vision_ref is present on the UoW, the route_reason is prefixed with
    the vision-anchored reason from resolve_vision_route(). This changes the
    actual pipeline decision: a vision-anchored UoW gets a structurally
    different route_reason than a heuristic-routed UoW, which is visible in
    audit logs, steward_log, and the DB.

    When vision_ref is absent, falls back to the steward heuristic string.

    Fast/Thorough Path gate (OODA Decide layer):
    Before building the route_reason, the meta-selector from
    src/ooda/fast_thorough_selector.py is consulted. If "fast" is selected,
    routing proceeds immediately (Decide is logged but non-blocking). If
    "thorough" is selected, the route_reason is annotated to signal that
    explicit Decide traceability is required before Action.

    Pure function: produces no side effects, safe to call from tests.
    """
    if executor_outcome == "partial" and partial_steps_context:
        heuristic_reason = (
            f"steward: {reentry_posture} — partial continuation "
            f"({partial_steps_context}) — {completion_rationale[:80]}"
        )
    else:
        heuristic_reason = f"steward: {reentry_posture} — {completion_rationale[:120]}"

    # Fast/Thorough Path gate: consult meta-selector before routing.
    # Derives vision_anchor and prior_decisions from UoW fields.
    vision_anchor = None
    if uow.vision_ref and isinstance(uow.vision_ref, dict):
        field = uow.vision_ref.get("field")
        layer = uow.vision_ref.get("layer")
        if field and layer:
            vision_anchor = f"vision.{layer}.{field}"

    # Derive stakes from the UoW register: high-stakes registers require Thorough Path
    _HIGH_STAKES_REGISTERS = frozenset({"philosophical_register", "human-judgment"})
    stakes = "high" if uow.register in _HIGH_STAKES_REGISTERS else "low"

    selector_context = {
        "situation_class": reentry_posture,
        "stakes": stakes,
        "prior_decisions": [],  # future: populate from steward_log audit entries
        "vision_anchor": vision_anchor,
    }
    ooda_path = _ooda_select_path(selector_context)
    ooda_basis = _ooda_cite_basis(selector_context)

    if ooda_path == "thorough":
        # Thorough Path: annotate route_reason to require explicit Decide traceability.
        heuristic_reason = f"[thorough-path-required] {heuristic_reason}"

    # Vision-anchored path: when vision_ref is present, prepend vision routing result.
    # This changes the route_reason from a pure heuristic string to one that references
    # the vision anchor — a real pipeline decision, not just metadata.
    if uow.vision_ref is not None:
        vision_result = resolve_vision_route(uow, log_fallback=False)
        if vision_result.anchored:
            route_reason = f"{vision_result.route_reason} | {heuristic_reason}"
            if ooda_path == "fast" and ooda_basis:
                route_reason += f" | ooda:fast-path basis={ooda_basis}"
            return route_reason

    return heuristic_reason


def _select_prescribed_skills(uow: "UoW", reentry_posture: str) -> list[str]:
    """
    Select prescribed skills appropriate to the UoW type and posture.

    Returns a list of skill IDs.
    """
    summary = uow.summary.lower()
    skills = []

    if "bug" in summary or "fix" in summary or "error" in summary:
        skills.append("systematic-debugging")
    if "pr" in summary or "pull request" in summary or reentry_posture == "execution_complete":
        skills.append("verification-before-completion")
    if reentry_posture in ("crashed_no_output", "execution_failed"):
        if "systematic-debugging" not in skills:
            skills.append("systematic-debugging")

    return skills


def _extract_json_from_llm_output(raw_text: str) -> str:
    """Extract the JSON content from raw LLM output.

    Pure function. Handles three forms:
    1. Fenced block anywhere in the text (``` or ```json) — extracts content between
       the first opening fence and its closing fence.
    2. Plain JSON starting at the first '{' or '[' — returns the substring from
       that character to the end of the string.
    3. Text that is already bare JSON — returned as-is.

    Returns the extracted candidate string. The caller is responsible for
    calling json.loads and handling JSONDecodeError.
    """
    import re as _re

    # Strategy 1: find a fenced block (```json or ```) anywhere in the text
    fence_match = _re.search(r"```(?:json)?\s*\n(.*?)\n```", raw_text, _re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Strategy 2: find the first JSON object/array start character
    for i, ch in enumerate(raw_text):
        if ch in ("{", "["):
            return raw_text[i:].strip()

    # Strategy 3: return as-is (will likely fail json.loads — handled by caller)
    return raw_text


def _parse_workflow_artifact(raw_text: str) -> dict:
    """Parse a front-matter + prose prescription artifact.

    Pure function. Accepts text in the form:
        ---
        executor_type: functional-engineer
        estimated_cycles: 1
        success_criteria_check: Verify PR is open and tests pass
        ---

        <prose instructions here>

    Preamble prose before the opening --- is tolerated and discarded.
    This mirrors the robustness of the previous JSON extraction strategy
    and handles LLMs that add an introductory sentence before the artifact.

    Returns a dict with keys:
      - "executor_type": str (required — raises ValueError if absent)
      - "estimated_cycles": int (default 1)
      - "success_criteria_check": str (default "")
      - "instructions": str — the prose body after the closing ---

    Raises ValueError if executor_type is missing or no front-matter delimiter
    is found in the input.

    Implementation is deliberately dependency-free: no PyYAML, no regex,
    just line-by-line scanning so the parse contract is unambiguous.
    """
    text = raw_text.strip()
    if not text:
        raise ValueError("_parse_workflow_artifact: empty input")

    lines = text.splitlines()

    # Find the first --- delimiter (opening). Preamble prose before it is
    # tolerated so the parser is robust against LLM introductory sentences.
    opening_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            opening_idx = i
            break

    if opening_idx is None:
        raise ValueError(
            "_parse_workflow_artifact: no front-matter '---' delimiter found in input"
        )

    # Find the closing --- (first occurrence after the opening ---)
    closing_idx: int | None = None
    for i in range(opening_idx + 1, len(lines)):
        if lines[i].strip() == "---":
            closing_idx = i
            break

    # When no closing delimiter is found, treat everything after the opening
    # --- as front-matter (no prose body).
    if closing_idx is None:
        front_matter_lines = lines[opening_idx + 1:]
        prose_lines: list[str] = []
    else:
        front_matter_lines = lines[opening_idx + 1:closing_idx]
        prose_lines = lines[closing_idx + 1:]

    # Parse front-matter key: value pairs (no nested structures needed).
    front_matter: dict[str, str] = {}
    for line in front_matter_lines:
        if ":" not in line:
            continue
        parts = line.split(":", 1)
        key = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else ""
        front_matter[key] = value

    executor_type = front_matter.get("executor_type", "")
    if not executor_type:
        raise ValueError(
            "_parse_workflow_artifact: required field 'executor_type' is missing "
            "from front-matter"
        )

    raw_cycles = front_matter.get("estimated_cycles", "1")
    try:
        estimated_cycles = int(raw_cycles)
    except (TypeError, ValueError):
        estimated_cycles = 1

    success_criteria_check = front_matter.get("success_criteria_check", "")

    # Preserve the prose body exactly — strip only the leading blank line that
    # typically follows the closing --- delimiter.
    instructions = "\n".join(prose_lines).strip()

    return {
        "executor_type": executor_type,
        "estimated_cycles": estimated_cycles,
        "success_criteria_check": success_criteria_check,
        "instructions": instructions,
    }


def _llm_prescribe(
    uow: UoW,
    reentry_posture: str,
    completion_gap: str,
    issue_body: str = "",
) -> LLMPrescription | None:
    """
    Call Claude to generate a tailored prescription for the given UoW.

    Dispatches via `claude -p` subprocess (the Lobster-standard LLM call path).
    No ANTHROPIC_API_KEY or anthropic SDK required — the claude CLI handles auth.

    Returns a typed LLMPrescription dataclass containing:
      - instructions: str — full instruction block for the Executor
      - success_criteria_check: str — how to verify completion
      - estimated_cycles: int — expected execution passes needed

    Returns None if the subprocess fails, times out, or returns unparseable output.
    The caller must fall back to the deterministic template on None.

    This function is a pure side-effect boundary: the only observable effect
    is the claude -p subprocess call. All inputs are immutable value types.
    """
    # Build prior prescription summary from steward_log if available
    prior_prescriptions: list[str] = []
    if uow.steward_log:
        try:
            for line in uow.steward_log.strip().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    continue
                event = entry.get("event", "")
                if event in ("prescription", "reentry_prescription"):
                    assessment = entry.get("completion_assessment", "")
                    cycle = entry.get("steward_cycles", "?")
                    if assessment:
                        prior_prescriptions.append(
                            f"  - Cycle {cycle}: {assessment}"
                        )
        except (json.JSONDecodeError, KeyError):
            pass

    # Build the context block for the prompt
    context_parts: list[str] = [
        f"UoW ID: {uow.id}",
        f"Summary: {uow.summary}",
        f"Type: {uow.type}",
    ]

    if uow.success_criteria:
        context_parts.append(f"Success criteria: {uow.success_criteria}")
    elif issue_body:
        body_excerpt = issue_body.strip()
        if len(body_excerpt) > 2000:
            body_excerpt = body_excerpt[:2000] + "\n[...truncated]"
        context_parts.append(f"Issue body:\n{body_excerpt}")

    context_parts.append(f"Execution cycle: {uow.steward_cycles} (0 = first pass)")
    context_parts.append(f"Executor posture: {reentry_posture}")
    context_parts.append(f"Completion gap identified: {completion_gap}")

    if prior_prescriptions:
        context_parts.append(
            "Prior prescription history:\n" + "\n".join(prior_prescriptions)
        )

    uow_context = "\n".join(context_parts)

    system_prompt = (
        "You are prescribing work instructions for a Lobster subagent that will execute "
        "a Unit of Work (UoW) in a software development pipeline. "
        "Your prescription must be concrete, actionable, and directly executable. "
        "Avoid vague language. Use the success_criteria as your north star for what 'done' means. "
        "The Executor is a capable autonomous coding agent — write instructions at that level. "
        "The instructions you produce will be handed directly to a Lobster subagent dispatch call; "
        "they must conform to Lobster's subagent dispatch conventions so the executor can act on them correctly."
    )

    # Golden dispatch conventions injected into every prescription so the executor
    # agent that receives the prescription knows how to structure its own work.
    # uow.source is injected at generation time so the subagent prompt carries the
    # correct source value rather than a hardcoded platform assumption.
    _uow_source = uow.source or "telegram"
    _DISPATCH_CONVENTIONS = f"""\
## Lobster Subagent Dispatch Conventions

### Prompt YAML Frontmatter (required at top of every prompt)
---
task_id: <short-slug>
chat_id: <user's chat_id>
source: {_uow_source}
---

### Required fields in every subagent Task call
- run_in_background=True for user-facing subagents (required — violating this breaks the 7-second rule)
  Note: WOS executor tasks are already spawned as background claude -p processes; they use
  write_result with sent_reply_to_user=False instead of send_reply.
- subagent_type: see table below

### Agent type selection
- GitHub issue implementation, feature work, bug fix: functional-engineer
- Lobster system ops, infra, deploy, install tasks: lobster-ops
- General background tasks (default): lobster-generalist
- Default when uncertain: lobster-generalist

### Required prompt structure
Every prompt must include:
  Minimum viable output: <one concrete deliverable>
  Boundary: do not <X>

### Output delivery (subagent two-step)
1. send_reply(chat_id=<id>, text="<result>", task_id="<slug>")
2. write_result(task_id="<slug>", sent_reply_to_user=True)
For internal tasks (no user reply): write_result only with sent_reply_to_user=False
"""

    user_prompt = f"""Given this Unit of Work, write a precise prescription for the Executor.

{uow_context}

{_DISPATCH_CONVENTIONS}
Respond using front-matter + prose format. Output ONLY the prescription — no preamble, no explanation outside this structure:

---
executor_type: <agent type from the table above — e.g. functional-engineer>
estimated_cycles: <integer 1-3 — how many Executor passes this is expected to need>
success_criteria_check: <one or two sentences describing exactly how to verify the work is complete — what to check, what file exists, what content to confirm>
---

<complete, actionable instructions for the Executor — include the specific steps, what to produce, where to write output, and any constraints from the success criteria; embed the YAML frontmatter, Minimum viable output, Boundary, and agent_type lines as described above>"""

    # Combine system and user prompts into a single string for claude -p,
    # which does not accept a separate --system flag in basic invocation mode.
    prompt = f"{system_prompt}\n\n{user_prompt}"

    timeout_secs = _get_llm_prescription_timeout()
    model = select_steward_model(uow)

    command = [_CLAUDE_BIN, "-p", prompt, "--output-format", "text", "--model", model]

    # Build an env dict that guarantees CLAUDE_CODE_OAUTH_TOKEN is present.
    # Without this, cron-spawned steward instances cannot authenticate since
    # they inherit a clean environment without the token.
    claude_env = _build_claude_env()

    # Use error capture to detect and log subprocess failures with context
    proc, error = run_subprocess_with_error_capture(
        component="steward_prescription",
        uow_id=uow.id,
        command=command,
        timeout_seconds=timeout_secs,
        check=False,  # Don't auto-log; we handle errors gracefully with fallback
        env=claude_env,
    )

    if error:
        # Log stderr to expose the actual failure reason (e.g. "401 Unauthorized",
        # "Failed to authenticate") which is otherwise invisible when check=False.
        stderr_preview = (error.stderr or "<no stderr>")[:500].strip()
        log.warning(
            "_llm_prescribe: prescription failed for %s — %s | stderr: %s",
            uow.id, error.summary(), stderr_preview,
        )

        # Check for repeated failures (same error 3+ times in 5 min)
        if has_repeated_error("steward_prescription", uow.id, str(error.error_type), threshold=3):
            log.error(
                "_llm_prescribe: repeated prescription errors for %s — may need manual intervention",
                uow.id,
            )

        return None

    if proc is None or proc.returncode != 0:
        log.warning(
            "_llm_prescribe: claude -p exited %d for %s",
            proc.returncode if proc else None, uow.id,
        )
        return None

    raw_text = proc.stdout.strip()

    # Classify empty-output case separately: claude exited 0 but returned
    # nothing.  This typically means the binary is unavailable, the model
    # refused, or stdout was silently discarded.
    if not raw_text:
        log.warning(
            "_llm_prescribe: claude -p returned empty stdout for %s "
            "(exit 0), falling back",
            uow.id,
        )
        return None

    # Parse the front-matter + prose prescription format.
    try:
        parsed = _parse_workflow_artifact(raw_text)
    except ValueError as exc:
        log.warning(
            "_llm_prescribe: could not parse front-matter artifact for %s "
            "(%s) — output preview: %r, falling back",
            uow.id, exc, raw_text[:200],
        )
        return None

    instructions = parsed.get("instructions", "")
    success_criteria_check = parsed.get("success_criteria_check", "")
    estimated_cycles = parsed.get("estimated_cycles", 1)

    if not instructions:
        log.warning(
            "_llm_prescribe: LLM returned empty instructions field for %s, "
            "falling back",
            uow.id,
        )
        return None

    log.info(
        "_llm_prescribe: LLM prescription generated for %s (model=%s, estimated_cycles=%d)",
        uow.id, model, estimated_cycles,
    )
    return LLMPrescription(
        instructions=instructions,
        success_criteria_check=success_criteria_check,
        estimated_cycles=max(1, min(3, estimated_cycles)),
    )


def _fetch_prior_prescriptions(
    current_log_str: str | None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Extract the last N prescription entries from the steward_log text.

    The steward_log is a newline-delimited sequence of JSON objects.
    Prescription events have event == "prescription" or "reentry_prescription".

    Pure function: parses the log text and returns a list of at most `limit`
    prescription dicts, ordered oldest-first (most recent last).  Returns []
    when the log is absent, empty, or contains no prescription entries.

    Each returned dict contains the keys present in the prescription log entry:
    completion_assessment, next_posture_rationale, return_reason,
    steward_cycles, and timestamp.
    """
    if not current_log_str:
        return []

    prescriptions: list[dict[str, Any]] = []
    for line in current_log_str.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("event") in ("prescription", "reentry_prescription"):
            prescriptions.append(entry)

    # Return the last `limit` entries (oldest-first ordering preserved).
    return prescriptions[-limit:] if prescriptions else []


def _check_trace_gate_waited(steward_log: str | None) -> bool:
    """
    Return True if a 'trace_gate_waited' entry exists in the steward_log.

    Pure function. Scans newline-delimited JSON log entries and returns True
    when any entry has event == "trace_gate_waited". Returns False when the
    log is absent, empty, or contains no such entry.
    """
    if not steward_log:
        return False
    for line in steward_log.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(entry, dict) and entry.get("event") == "trace_gate_waited":
            return True
    return False


def _clear_trace_gate_waited(steward_log: str | None) -> str:
    """
    Return a new steward_log string with all 'trace_gate_waited' entries removed.

    Pure function. Filters out lines where event == "trace_gate_waited".
    Returns empty string when steward_log is None or empty.
    """
    if not steward_log:
        return steward_log or ""
    result_lines: list[str] = []
    for line in steward_log.splitlines():
        stripped = line.strip()
        if not stripped:
            result_lines.append(line)
            continue
        try:
            entry = json.loads(stripped)
            if isinstance(entry, dict) and entry.get("event") == "trace_gate_waited":
                continue
        except (json.JSONDecodeError, TypeError):
            pass
        result_lines.append(line)
    return "\n".join(result_lines)


# ---------------------------------------------------------------------------
# PR C: Register-aware diagnosis pure helpers
# ---------------------------------------------------------------------------

# Named constant: max chars for prescription_delta before bounding kicks in
_PRESCRIPTION_DELTA_MAX_CHARS = 500

# Named constant: consecutive non-improving gate cycles before surfacing
_NON_IMPROVING_GATE_THRESHOLD = 3


def _register_completion_policy(register: str) -> str:
    """
    Map a UoW register to its completion policy identifier.

    Returns one of:
    - "machine-gate"        for operational and iterative-convergent
    - "always-surface"      for philosophical
    - "require-confirmation" for human-judgment

    Unknown registers default to "machine-gate" (conservative pass-through).

    Pure function — no side effects.
    """
    _POLICY_MAP = {
        "operational": "machine-gate",
        "iterative-convergent": "machine-gate",
        "philosophical": "always-surface",
        "human-judgment": "require-confirmation",
    }
    return _POLICY_MAP.get(register, "machine-gate")


def _read_trace_json(output_ref: str | None, expected_uow_id: str) -> dict | None:
    """
    Read and validate a corrective trace file for the given output_ref.

    Tries two path conventions (mirroring result.json dual-path logic):
    - Primary:  Path(output_ref).with_suffix(".trace.json")
    - Fallback: Path(str(output_ref) + ".trace.json")

    Returns the parsed dict if the file exists and the uow_id field matches
    expected_uow_id.  Returns None on any error (absent, invalid JSON,
    uow_id mismatch).

    Pure function with respect to state — reads files only.
    """
    if not output_ref:
        return None

    trace_file = Path(output_ref).with_suffix(".trace.json")
    if not trace_file.exists():
        trace_file_alt = Path(str(output_ref) + ".trace.json")
        if trace_file_alt.exists():
            trace_file = trace_file_alt
        else:
            return None

    try:
        data = json.loads(trace_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict):
        return None

    # Guard: misrouted trace file
    trace_uow_id = data.get("uow_id")
    if trace_uow_id is not None and trace_uow_id != expected_uow_id:
        log.warning(
            "_read_trace_json: trace file %s has uow_id=%r but expected %r — "
            "treating as absent (misrouted trace file)",
            trace_file, trace_uow_id, expected_uow_id,
        )
        return None

    return data


# ---------------------------------------------------------------------------
# Orphan kill classification — reads trace.json to distinguish kill types
# ---------------------------------------------------------------------------

# Named constants for orphan kill classifications (spec: architectural-proposal-20260426.md)
ORPHAN_KILL_BEFORE_START = "kill_before_start"
ORPHAN_KILL_DURING_EXECUTION = "kill_during_execution"
ORPHAN_COMPLETED_WITHOUT_OUTPUT = "completed_without_output"

# The three ReentryPosture values that trigger orphan trace enrichment in _diagnose_uow.
# Defined at module scope — not inside _diagnose_uow — to avoid re-allocating the
# frozenset on every call. Uses ReentryPosture enum values per the StrEnum golden
# pattern (2026-04-24): enum-backed values must not appear as raw string literals.
_ORPHAN_POSTURES: frozenset[str] = frozenset({
    ReentryPosture.EXECUTOR_ORPHAN,
    ReentryPosture.EXECUTING_ORPHAN,
    ReentryPosture.DIAGNOSING_ORPHAN,
})


def _classify_orphan_from_trace(trace_data: dict | None, output_ref: str | None) -> str:
    """
    Classify the orphan kill type from available trace and result evidence.

    Returns one of three string constants:
    - ORPHAN_COMPLETED_WITHOUT_OUTPUT ("completed_without_output"):
        result.json exists alongside output_ref — the executor subagent completed
        its work and wrote a result, but write_result was never called so the
        Steward never received the completion signal.
    - ORPHAN_KILL_DURING_EXECUTION ("kill_during_execution"):
        trace.json has non-empty surprises or prescription_delta — the agent
        established some working state before being killed (partial work done).
    - ORPHAN_KILL_BEFORE_START ("kill_before_start"):
        Default. trace.json absent, or dispatch-only execution_summary with
        no surprises/prescription_delta — the session ended before any real
        execution was established.

    Priority order: completed_without_output > kill_during_execution > kill_before_start.
    The completed_without_output check runs first because result.json presence is
    the strongest signal — it overrides even non-empty surprises.

    Pure function: only reads files (result.json presence check). No DB writes.
    Note: "pure" here means no DB writes or mutation — not side-effect-free.
    The function reads from the filesystem, so its output depends on file state.

    result.json naming conventions: two forms are checked to handle legacy producers.
    - Canonical form (new executors): `Path(output_ref).with_suffix(".result.json")`
      replaces the existing `.json` suffix, producing `uow_id.result.json`.
    - Legacy form (older executor scripts that appended rather than replaced):
      `str(output_ref) + ".result.json"`, producing `uow_id.json.result.json`.
    The alt-path check ensures that UoWs completed by legacy producers are not
    misclassified as kill_before_start (which would cause unnecessary retries).
    If no legacy producers remain active, the alt-path check never fires but is
    kept to avoid breaking audit continuity for historical UoW replay.

    Args:
        trace_data: Parsed trace.json dict (already validated by _read_trace_json),
            or None when trace.json is absent or invalid.
        output_ref: The UoW's output_ref path (used to derive result.json path).
            May be None when the UoW has no output_ref.
    """
    # Priority 1: result.json exists → agent ran and completed, but write_result skipped.
    if output_ref:
        result_path = Path(output_ref).with_suffix(".result.json")
        if not result_path.exists():
            result_path_alt = Path(str(output_ref) + ".result.json")
            if result_path_alt.exists():
                result_path = result_path_alt
        if result_path.exists():
            return ORPHAN_COMPLETED_WITHOUT_OUTPUT

    # Priority 2: no trace data → cannot distinguish, default to kill_before_start.
    if trace_data is None:
        return ORPHAN_KILL_BEFORE_START

    # Priority 3: trace has execution evidence (surprises or prescription_delta).
    surprises = trace_data.get("surprises") or []
    prescription_delta = trace_data.get("prescription_delta") or ""
    if surprises or prescription_delta:
        return ORPHAN_KILL_DURING_EXECUTION

    # Default: dispatch-only trace with no distinguishing signals.
    return ORPHAN_KILL_BEFORE_START


def _enrich_orphan_completion_rationale(
    base_rationale: str,
    output_ref: str | None,
    uow_id: str,
) -> str:
    """
    Enrich a bare orphan completion_rationale with trace.json evidence.

    Reads trace.json from output_ref (via _read_trace_json), classifies the
    orphan kill type, and returns a rationale string that includes:
    - The orphan_classification label (kill_before_start / kill_during_execution /
      completed_without_output)
    - The execution_summary from the trace (if present)
    - Up to 3 surprises from the trace (if present)

    Returns base_rationale unchanged when:
    - output_ref is None
    - trace.json is absent or invalid
    - trace.json has a mismatched uow_id (handled by _read_trace_json)

    Pure function with respect to state — reads files only.

    Args:
        base_rationale: The original rationale string (from _assess_completion).
        output_ref: The UoW's output_ref path. May be None.
        uow_id: The UoW ID — used to validate the trace's uow_id field.
    """
    trace_data = _read_trace_json(output_ref, expected_uow_id=uow_id)

    kill_class = _classify_orphan_from_trace(trace_data, output_ref)

    if trace_data is None and kill_class == ORPHAN_KILL_BEFORE_START:
        # No trace and default classification — nothing to enrich with
        return base_rationale

    parts: list[str] = [base_rationale, f"orphan_classification={kill_class}"]

    if trace_data is not None:
        exec_summary = trace_data.get("execution_summary", "").strip()
        if exec_summary:
            parts.append(f"execution_summary={exec_summary!r}")

        surprises = [str(s) for s in (trace_data.get("surprises") or []) if s]
        if surprises:
            # Cap at 3 surprises to keep rationale bounded
            surprises_excerpt = surprises[:3]
            parts.append(f"surprises={surprises_excerpt}")

    return " | ".join(parts)


def _bound_prescription_delta(delta: str, history: list[str]) -> str:
    """
    Bound a prescription_delta string to prevent loop gain instability.

    If delta exceeds _PRESCRIPTION_DELTA_MAX_CHARS, truncate it and append
    a trailing note indicating it was bounded.

    history is informational (for potential future smoothing) but does not
    change the bound threshold.

    Pure function — no side effects.
    """
    if not delta or len(delta) <= _PRESCRIPTION_DELTA_MAX_CHARS:
        return delta

    truncated = delta[:_PRESCRIPTION_DELTA_MAX_CHARS]
    return truncated + " ... [prescription_delta bounded — original exceeded limit]"


def _count_non_improving_gate_cycles(steward_log: str | None, n: int = _NON_IMPROVING_GATE_THRESHOLD) -> int:
    """
    Count consecutive non-improving gate_score cycles from the tail of steward_log.

    Reads trace_injection entries in order. A cycle is "non-improving" if its
    gate_score.score is not greater than the previous cycle's score (or if there
    is no gate_score, the entry is skipped entirely).

    Returns the count of consecutive non-improving cycles at the end of the log.
    Returns 0 when the log is absent, empty, has no gate_score entries, or the
    scores are improving.

    Pure function — no side effects.
    """
    if not steward_log:
        return 0

    # Collect gate_score entries from trace_injection events in order
    scores: list[float] = []
    for line in steward_log.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("event") != "trace_injection":
            continue
        gate_score = entry.get("gate_score")
        if gate_score is None:
            continue
        score_val = gate_score.get("score") if isinstance(gate_score, dict) else None
        if score_val is not None:
            try:
                scores.append(float(score_val))
            except (TypeError, ValueError):
                pass

    if len(scores) < 2:
        return 0

    # Find the tail run of non-improving cycles.
    # A "non-improving cycle" is one where the score did not increase vs. the previous.
    # We return the count of consecutive non-improving data points at the tail,
    # starting from 1 (the reference point after the last improvement).
    # With scores [0.5, 0.5, 0.5]: reference=index 0, tail=[1,2] are non-improving → count=3
    # (we include the starting point of the plateau to match the spec's "3 consecutive cycles").
    non_improving = 1  # start counting from the last improving point (or start of log)
    for i in range(len(scores) - 1, 0, -1):
        if scores[i] <= scores[i - 1]:
            non_improving += 1
        else:
            # Found an improvement — the plateau started AFTER this point
            # non_improving already counts from scores[i] forward
            return non_improving if non_improving > 1 else 0  # exclude initialised-but-not-yet-counted tail when first pair improves

    # All scores are non-improving (or only 1 pair) — all data points are the plateau
    return non_improving


def _count_consecutive_llm_fallbacks(current_log_str: str | None) -> int:
    """
    Count how many consecutive prescription events at the tail of steward_log
    used the deterministic fallback path (prescription_path == "fallback").

    Scans prescription and reentry_prescription events in reverse order and
    stops at the first event that used the LLM path or at the beginning of the
    log.  Returns 0 when the log is absent, empty, or the last prescription
    used the LLM path.

    Pure function — reads only current_log_str; no side effects.
    """
    if not current_log_str:
        return 0

    prescription_events: list[dict[str, Any]] = []
    for line in current_log_str.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if entry.get("event") in ("prescription", "reentry_prescription"):
            prescription_events.append(entry)

    # Count consecutive fallbacks from the most recent prescription backwards.
    consecutive = 0
    for event in reversed(prescription_events):
        if event.get("prescription_path") == "fallback":
            consecutive += 1
        else:
            break

    return consecutive


# ---------------------------------------------------------------------------
# Loop-pattern-aware dispatch eligibility (oracle/patterns.md thresholds)
# ---------------------------------------------------------------------------

# Spiral: oracle passes >= this value → escalate dispatch
# Source: oracle/patterns.md §spiral — "oracle_pass_count ≥ 3"
SPIRAL_ORACLE_PASS_THRESHOLD: int = 3

# Dead-end: failed/blocked transitions >= this value → pause dispatch
# Source: oracle/patterns.md §dead-end — "failed or blocked state ≥2 times"
DEAD_END_FAILURE_THRESHOLD: int = 2

# Burst: throttle to this many UoWs per cycle when burst is detected.
# Default is 3 (oracle/patterns.md §burst — "batch into groups of 3").
# When queue depth exceeds thresholds, a larger batch size is used to drain
# faster without losing the throttle protection.
BURST_BATCH_SIZE: int = 3


def _dynamic_burst_batch_size(queue_depth: int) -> int:
    """Return the appropriate BURST_BATCH_SIZE given the current queue depth.

    Thresholds (applied in order, first match wins):
    - queue_depth > 50  → batch size 15
    - queue_depth > 20  → batch size 8
    - default           → BURST_BATCH_SIZE (3)

    Pure function — no side effects.
    """
    if queue_depth > 50:
        return 15
    if queue_depth > 20:
        return 8
    return BURST_BATCH_SIZE

# Burst: hard lower bound for the baseline queue depth used in spike detection.
# Queue depths at or above 2x this value are treated as a burst.
# Source: oracle/patterns.md §burst — hard lower bound comment
BURST_BASELINE_QUEUE_DEPTH: int = 6


def _count_oracle_passes(audit_entries: list[dict]) -> int:
    """Count oracle_approved events in the audit log for a UoW.

    Each oracle_approved entry represents one complete oracle pass that
    returned APPROVED for this UoW. Used by the Spiral pattern detector.

    oracle_approved events are written by oracle_audit.emit_oracle_approved()
    after the oracle agent issues an APPROVED verdict for a WOS-linked PR.
    The oracle agent calls oracle_audit.py (CLI) fire-and-forget after writing
    to oracle/verdicts/pr-{number}.md. See src/orchestration/oracle_audit.py.

    Pure function — reads only audit_entries; no side effects.
    """
    return sum(1 for e in audit_entries if e.get("event") == "oracle_approved")


def _count_failed_or_blocked_transitions(audit_entries: list[dict]) -> int:
    """Count audit entries where the UoW transitioned to failed or blocked.

    Includes both executor-driven failures (to_status='failed') and
    Steward-driven surface/block transitions (to_status='blocked').
    Used by the Dead-end pattern detector.

    Pure function — reads only audit_entries; no side effects.
    """
    terminal = {"failed", "blocked"}
    return sum(1 for e in audit_entries if e.get("to_status") in terminal)


def _check_dispatch_eligibility(
    uow: "UoW",
    audit_entries: list[dict],
    queue_depth: int,
) -> str:
    """Determine whether this UoW should be dispatched, paused, escalated, or throttled.

    Reads UoW history (via audit_entries) and the current queue depth to detect
    the loop anti-patterns defined in oracle/patterns.md.

    Returns one of:
    - "dispatch"  — no pattern detected; proceed normally
    - "pause"     — dead-end detected; suppress re-dispatch, write blocker prescription
    - "escalate"  — spiral detected; pause dispatch, write escalation prescription
    - "throttle"  — burst detected; limit batch size to BURST_BATCH_SIZE per cycle

    Precedence when multiple patterns fire: escalate > pause > throttle > dispatch.

    Pure function — no side effects, no DB writes.
    """
    # Spiral check (highest precedence)
    # oracle_approved events are written by the oracle agent via oracle_audit.py
    # after each APPROVED verdict for a WOS-linked PR. See oracle_audit.py.
    oracle_passes = _count_oracle_passes(audit_entries)
    if oracle_passes >= SPIRAL_ORACLE_PASS_THRESHOLD:
        log.debug(
            "_check_dispatch_eligibility: spiral detected for %s "
            "(oracle_pass_count=%d >= %d)",
            uow.id, oracle_passes, SPIRAL_ORACLE_PASS_THRESHOLD,
        )
        return "escalate"

    # Dead-end check
    failures = _count_failed_or_blocked_transitions(audit_entries)
    if failures >= DEAD_END_FAILURE_THRESHOLD:
        log.debug(
            "_check_dispatch_eligibility: dead-end detected for %s "
            "(failed_or_blocked=%d >= %d)",
            uow.id, failures, DEAD_END_FAILURE_THRESHOLD,
        )
        return "pause"

    # Burst check (lowest precedence above dispatch)
    burst_spike_threshold = BURST_BASELINE_QUEUE_DEPTH * 2
    if queue_depth >= burst_spike_threshold:
        log.debug(
            "_check_dispatch_eligibility: burst detected for %s "
            "(queue_depth=%d >= %d)",
            uow.id, queue_depth, burst_spike_threshold,
        )
        return "throttle"

    return "dispatch"


def _notify_llm_fallback_warning(
    uow: UoW,
    consecutive_fallbacks: int,
) -> None:
    """
    Write an inbox message to Dan when _llm_prescribe has fallen back to the
    deterministic template for _LLM_FALLBACK_WARNING_THRESHOLD consecutive
    cycles on the same UoW.

    Uses the same inbox path as _default_notify_dan_early_warning.  In tests,
    the caller can skip this function entirely by checking the threshold before
    calling — or monkeypatch it to capture the call.
    """
    uow_id = uow.id
    admin_chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", _DAN_CHAT_ID)
    log.warning(
        "WOS LLM FALLBACK: UoW %s has fallen back to deterministic prescription "
        "%d consecutive times — LLM prescription path may be broken",
        uow_id, consecutive_fallbacks,
    )
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "chat_id": admin_chat_id,
        "text": (
            f"WOS: `{uow_id}` LLM prescription has fallen back to deterministic "
            f"for {consecutive_fallbacks} consecutive cycles. "
            "Check `LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS` and `claude -p` availability. "
            "Prescription quality is degraded until LLM path recovers."
        ),
        "timestamp": time.time(),
        "metadata": {
            "type": "wos_llm_fallback_warning",
            "uow_id": uow_id,
            "consecutive_llm_fallbacks": consecutive_fallbacks,
        },
    }
    inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"{msg_id}.json").write_text(
            json.dumps(msg, indent=2), encoding="utf-8"
        )
        log.info("WOS LLM fallback warning written to inbox: %s", msg_id)
    except OSError as exc:
        log.error("Failed to write WOS LLM fallback warning to inbox: %s", exc)


def _build_deterministic_prescription_instructions(
    uow: UoW,
    reentry_posture: str,
    completion_gap: str,
    issue_body: str = "",
    prior_prescriptions: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build natural language prescription instructions for the Executor using
    the deterministic keyword-matching template.

    This is the fallback path used when the LLM call fails or is unavailable.
    It is also the implementation called by _build_prescription_instructions
    when llm_prescriber returns None.

    NOTE: This path does not inject _DISPATCH_CONVENTIONS (YAML frontmatter,
    Minimum viable output, Boundary, agent type, run_in_background, two-step
    output delivery). The deterministic template produces minimal instructions
    only. Executors dispatched via this path may not conform to Lobster's
    subagent dispatch protocol without additional scaffolding at the call site.

    Args:
        uow: The Unit of Work being prescribed.
        reentry_posture: Categorized executor state from diagnosis.
        completion_gap: Human-readable rationale for why work is incomplete.
        issue_body: Raw GitHub issue body text. Used to compose context when
            success_criteria is absent. Pass empty string if unavailable.
        prior_prescriptions: List of prior steward_log prescription entries
            (from _fetch_prior_prescriptions). Injected into re-prescription
            context so the Steward can avoid repeating approaches that did not
            work. Pass None or [] for the first cycle.
    """
    summary = uow.summary
    success_criteria = uow.success_criteria
    cycles = uow.steward_cycles

    # Build the criteria/context block from whatever is available.
    # Priority: explicit success_criteria > issue body > nothing.
    if success_criteria:
        criteria_block = f"Success criteria: {success_criteria}"
    elif issue_body:
        # Truncate very long issue bodies to keep instructions readable.
        body_excerpt = issue_body.strip()
        if len(body_excerpt) > 1500:
            body_excerpt = body_excerpt[:1500] + "\n[...truncated]"
        criteria_block = f"Issue context:\n{body_excerpt}"
    else:
        criteria_block = ""

    if cycles == 0:
        parts = [
            "Execute the following task:",
            "",
            f"Summary: {summary}",
        ]
        if criteria_block:
            parts += ["", criteria_block]
        parts += ["", "Write your output to the output_ref path."]
        return "\n".join(parts)

    posture_context = {
        "execution_complete": "Previous execution completed but output needs improvement.",
        "stall_detected": "Previous execution stalled (timeout). Re-execute with focus on completing within time limits.",
        "crashed_no_output": "Previous execution crashed without producing output. Re-execute, adding error handling.",
        "execution_failed": "Previous execution failed. Diagnose the failure and re-execute.",
        "executor_orphan": "Executor never ran on this UoW. Execute fresh.",
        "diagnosing_orphan": "Steward crashed mid-diagnosis. Re-diagnosing from current state.",
        "executing_orphan": "Subagent was dispatched but never called write_result (crashed or lost context). Re-execute fresh.",
    }

    posture_msg = posture_context.get(reentry_posture, "Continue from previous attempt.")

    parts = [
        f"Re-execution pass (cycle {cycles + 1}):",
        "",
        posture_msg,
        "",
        f"Gap identified: {completion_gap}",
        "",
        f"Original task: {summary}",
    ]
    if criteria_block:
        parts += ["", criteria_block]

    # Inject prior prescription attempts so the Executor avoids repeating
    # approaches that already failed.  Only included when prior data exists.
    if prior_prescriptions:
        prior_lines = ["", "Prior prescription attempts (do not repeat these approaches):"]
        for i, entry in enumerate(prior_prescriptions, start=1):
            assessment = entry.get("completion_assessment", "")
            rationale = entry.get("next_posture_rationale", "")
            cycle_num = entry.get("steward_cycles", "?")
            return_reason = entry.get("return_reason", "")
            prior_lines.append(
                f"  {i}. Cycle {cycle_num}: assessment={assessment!r}; "
                f"rationale={rationale!r}; return_reason={return_reason!r}"
            )
        parts += prior_lines

    return "\n".join(parts)


def _build_prescription_instructions(
    uow: UoW,
    reentry_posture: str,
    completion_gap: str,
    issue_body: str = "",
    llm_prescriber: Callable[..., LLMPrescription | None] | None = _llm_prescribe,
    prior_prescriptions: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build natural language prescription instructions for the Executor.

    Uses the LLM-based prescription path via llm_prescriber. If the LLM prescription
    fails (API unavailable, timeout, parse failure), raises LLMPrescriptionError
    instead of falling back to deterministic.

    Args:
        uow: The Unit of Work being prescribed.
        reentry_posture: Categorized executor state from diagnosis.
        completion_gap: Human-readable rationale for why work is incomplete.
        issue_body: Raw GitHub issue body text. Used when success_criteria is absent.
        llm_prescriber: Callable that takes (uow, reentry_posture, completion_gap,
            issue_body) and returns LLMPrescription or None. Inject None or a stub
            in tests to bypass the LLM call. Defaults to _llm_prescribe.
        prior_prescriptions: List of prior steward_log prescription entries
            (from _fetch_prior_prescriptions). No longer used since deterministic
            fallback is removed. Kept for backward compatibility.

    Raises:
        LLMPrescriptionError: If the LLM prescriber returns None (failure, not bypass).
    """
    if llm_prescriber is None:
        # If llm_prescriber is explicitly None, this is a test/stub scenario
        # Fall back to deterministic only in this case
        return _build_deterministic_prescription_instructions(
            uow, reentry_posture, completion_gap, issue_body,
            prior_prescriptions=prior_prescriptions,
        )

    llm_result = llm_prescriber(uow, reentry_posture, completion_gap, issue_body)
    if llm_result is None:
        # LLM prescription failed — fail hard instead of falling back
        raise LLMPrescriptionError(
            f"LLM prescription failed for UoW {uow.id}. "
            "Check steward logs for details. No deterministic fallback is performed."
        )

    instructions = llm_result.instructions
    success_check = llm_result.success_criteria_check
    # Append the success_criteria_check as a verification note so the
    # Executor has an explicit completion signal alongside the instructions.
    if success_check:
        instructions = (
            instructions.rstrip()
            + f"\n\nCompletion check: {success_check}"
        )
    return instructions


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_steward_schema(conn: sqlite3.Connection) -> None:
    """
    Validate that all fields required for Steward operation are present in uow_registry.

    Raises RuntimeError with a specific message if any required field is absent.
    Call this at Steward startup before processing any UoW.

    Args:
        conn: An open SQLite connection to the registry database.

    Raises:
        RuntimeError: If any required field is missing. Message includes
            "schema migration not applied" and the list of missing fields.
    """
    rows = conn.execute("PRAGMA table_info(uow_registry)").fetchall()
    existing_cols = {row[1] for row in rows}
    missing = _STEWARD_REQUIRED_FIELDS - existing_cols
    if missing:
        missing_sorted = sorted(missing)
        raise RuntimeError(
            f"schema migration not applied — run scripts/migrate_add_steward_fields.py first. "
            f"Missing fields: {missing_sorted}"
        )


# Keep the old name as an alias so any existing callers continue to work.
validate_phase2_schema = validate_steward_schema


# ---------------------------------------------------------------------------
# Registry write helpers (steward-private field updates)
# ---------------------------------------------------------------------------

def _write_steward_fields(
    registry,
    uow_id: str,
    *,
    steward_agenda: str | None = None,
    steward_log: str | None = None,
    workflow_artifact: str | None = None,
    prescribed_skills: str | None = None,
    route_reason: str | None = None,
    steward_cycles: int | None = None,
    completed_at: str | None = None,
    closed_at: str | None = None,
    close_reason: str | None = None,
    retry_count: int | None = None,
    execution_attempts: int | None = None,
) -> None:
    """
    Write Steward-private and Steward-managed fields to the UoW row.

    Uses a direct connection from the Registry (bypasses the public API since
    these fields are Steward-private and not part of the Registry's public
    interface). Executes in a BEGIN IMMEDIATE transaction.
    """
    updates = {}
    if steward_agenda is not None:
        updates["steward_agenda"] = steward_agenda
    if steward_log is not None:
        updates["steward_log"] = steward_log
    if workflow_artifact is not None:
        updates["workflow_artifact"] = workflow_artifact
    if prescribed_skills is not None:
        updates["prescribed_skills"] = prescribed_skills
    if route_reason is not None:
        updates["route_reason"] = route_reason
    if steward_cycles is not None:
        updates["steward_cycles"] = steward_cycles
    if completed_at is not None:
        updates["completed_at"] = completed_at
    if closed_at is not None:
        updates["closed_at"] = closed_at
    if close_reason is not None:
        updates["close_reason"] = close_reason
    if retry_count is not None:
        updates["retry_count"] = retry_count
    if execution_attempts is not None:
        updates["execution_attempts"] = execution_attempts

    if not updates:
        return

    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [uow_id]

    conn = registry._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE uow_registry SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _append_steward_log_entry(
    registry,
    uow_id: str,
    current_log: str | None,
    entry: dict[str, Any],
) -> str:
    """
    Append a JSON entry to steward_log (newline-delimited).

    Returns the updated log string (does NOT write to DB — caller writes).
    The entry is JSON-encoded and appended on a new line.
    """
    entry["timestamp"] = _now_iso()
    entry_str = json.dumps(entry)
    if current_log:
        return current_log.rstrip("\n") + "\n" + entry_str
    return entry_str


# ---------------------------------------------------------------------------
# Hard-cap cleanup arc (S3-A)
# ---------------------------------------------------------------------------

def _archive_uow_artifacts(
    uow_id: str,
    artifact_dir: Path | None,
    archived_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Move the UoW's artifact directory to the archived location.

    Returns a dict with keys:
      - archived_path: str | None — absolute path of the archived directory, or None
      - success: bool
      - error: str | None — error message if archival failed

    Fallback contract: a failure to archive is logged but does not block the
    state transition. Cleanup arc failure is preferable to no cleanup arc.

    Pure filesystem operation — no DB writes, no side effects beyond the move.
    """
    resolved_artifact_dir = Path(artifact_dir) if artifact_dir is not None else _DEFAULT_ARTIFACTS_DIR
    src = resolved_artifact_dir / uow_id

    resolved_archived_dir = Path(archived_dir) if archived_dir is not None else _DEFAULT_ARTIFACTS_ARCHIVED_DIR
    dst = resolved_archived_dir / uow_id

    if not src.exists():
        # No artifact directory — not an error (UoW may have no artifacts yet)
        return {"archived_path": None, "success": True, "error": None}

    try:
        resolved_archived_dir.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
        return {"archived_path": str(dst), "success": True, "error": None}
    except Exception as e:
        log.error("hard_cap cleanup: artifact archival failed for %s: %s", uow_id, e)
        return {"archived_path": None, "success": False, "error": str(e)}


def _write_hard_cap_failure_trace(
    uow: UoW,
    return_reason: str | None,
    failure_traces_dir: Path | None = None,
    archived_artifact_path: str | None = None,
) -> dict[str, Any]:
    """
    Write a structured failure trace JSON to orchestration/failure-traces/<uow_id>.json.

    The trace preserves execution history for post-cleanup audit.  This is written
    AFTER artifact archival so that trace.json (included in the artifact directory by
    S3-B) has already been moved to archived/ — the trace record here captures the
    final state, not a snapshot of live files.

    Returns dict with keys:
      - trace_path: str | None — absolute path of written trace, or None on failure
      - success: bool
      - error: str | None

    Fallback contract: failure to write the trace does not block state transition.
    """
    resolved_dir = Path(failure_traces_dir) if failure_traces_dir is not None else _DEFAULT_FAILURE_TRACES_DIR
    resolved_dir.mkdir(parents=True, exist_ok=True)

    trace = {
        "uow_id": uow.id,
        "reason": CLOSE_REASON_HARD_CAP_CLEANUP,
        "final_return_reason": return_reason,
        "cycle_count_lifetime": uow.lifetime_cycles,
        "summary": uow.summary,
        "success_criteria": uow.success_criteria,
        "archived_artifact_path": archived_artifact_path,
        "timestamp": _now_iso(),
    }

    trace_path = resolved_dir / f"{uow.id}.json"
    try:
        trace_path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
        return {"trace_path": str(trace_path), "success": True, "error": None}
    except Exception as e:
        log.error("hard_cap cleanup: failure trace write failed for %s: %s", uow.id, e)
        return {"trace_path": None, "success": False, "error": str(e)}


def _post_hard_cap_github_comment(
    uow: UoW,
    failure_trace_path: str | None,
) -> bool:
    """
    Post a comment to the source GitHub issue noting the failure trace.

    Returns True on success, False on any error.
    Best-effort: failures are logged but do not block the cleanup arc.
    """
    issue_url = uow.issue_url
    if not issue_url:
        log.info(
            "hard_cap cleanup: no issue_url for %s — skipping GitHub comment", uow.id
        )
        return False

    repo = _repo_from_issue_url(issue_url)
    if not repo:
        log.warning(
            "hard_cap cleanup: could not parse repo from issue_url %r — skipping comment",
            issue_url,
        )
        return False

    # Extract issue number from URL: .../issues/<number>
    try:
        issue_number = int(issue_url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError):
        log.warning(
            "hard_cap cleanup: could not extract issue number from %r — skipping comment",
            issue_url,
        )
        return False

    trace_note = (
        f"\n\nFailure trace written to: `{failure_trace_path}`"
        if failure_trace_path
        else ""
    )
    comment_body = (
        f"**WOS hard cap reached** for UoW `{uow.id}`.\n\n"
        f"This UoW exhausted its lifetime cycle budget ({_HARD_CAP_CYCLES} cycles) "
        f"and has been archived. The cleanup arc has:\n"
        f"- Archived artifacts to `orchestration/artifacts/archived/{uow.id}/`\n"
        f"- Written a failure trace recording the final state{trace_note}\n\n"
        f"A decide-retry requires an explicit operator flag (`force` override) — "
        f"bare retry is rejected at the hard-cap commitment boundary."
    )

    command = [
        "gh", "issue", "comment", str(issue_number),
        "--repo", repo,
        "--body", comment_body,
    ]

    result, error = run_subprocess_with_error_capture(
        component="steward_github",
        uow_id=uow.id,
        command=command,
        timeout_seconds=15,
        check=False,
    )

    if error:
        log.warning(
            "hard_cap cleanup: GitHub comment failed for %s#%s: %s",
            repo, issue_number, error.summary(),
        )
        return False

    if result is None or result.returncode != 0:
        log.warning(
            "hard_cap cleanup: GitHub comment subprocess non-zero for %s#%s",
            repo, issue_number,
        )
        return False

    log.info(
        "hard_cap cleanup: GitHub comment posted for %s#%s", repo, issue_number
    )
    return True


def _run_hard_cap_cleanup(
    uow: UoW,
    registry,
    return_reason: str | None,
    artifact_dir: Path | None,
    failure_traces_dir: Path | None = None,
    archived_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Run the hard-cap cleanup arc atomically (best-effort at each step).

    Steps executed in order:
    1. Archive UoW artifacts to orchestration/artifacts/archived/<uow_id>/
    2. Write failure trace to orchestration/failure-traces/<uow_id>.json
    3. Set close_reason = CLOSE_REASON_HARD_CAP_CLEANUP and closed_at in registry
    4. Post GitHub comment on source issue (optional — best-effort)

    Returns a summary dict for audit log inclusion.

    Fallback contract: each step is individually protected. A failure in archival
    does not block the failure trace write, and a failure in either does not block
    the registry close_reason write. The state transition (blocked) always proceeds.
    """
    uow_id = uow.id
    log.info("hard_cap cleanup arc starting for %s (lifetime_cycles=%s)", uow_id, uow.lifetime_cycles)

    # Step 1: Archive artifacts
    archive_result = _archive_uow_artifacts(uow_id, artifact_dir, archived_dir)
    if not archive_result["success"]:
        log.warning("hard_cap cleanup: archival step failed for %s — continuing", uow_id)

    # Step 2: Write failure trace
    trace_result = _write_hard_cap_failure_trace(
        uow,
        return_reason=return_reason,
        failure_traces_dir=failure_traces_dir,
        archived_artifact_path=archive_result.get("archived_path"),
    )
    if not trace_result["success"]:
        log.warning("hard_cap cleanup: failure trace write failed for %s — continuing", uow_id)

    # Step 3: Set close_reason and closed_at in registry
    try:
        _write_steward_fields(
            registry,
            uow_id,
            close_reason=CLOSE_REASON_HARD_CAP_CLEANUP,
            closed_at=_now_iso(),
        )
    except Exception as e:
        log.error("hard_cap cleanup: registry close_reason write failed for %s: %s", uow_id, e)

    # Step 4: Post GitHub comment (best-effort, does not block)
    github_comment_posted = _post_hard_cap_github_comment(
        uow,
        failure_trace_path=trace_result.get("trace_path"),
    )

    summary = {
        "artifact_archived": archive_result["success"],
        "archived_path": archive_result.get("archived_path"),
        "failure_trace_written": trace_result["success"],
        "failure_trace_path": trace_result.get("trace_path"),
        "close_reason": CLOSE_REASON_HARD_CAP_CLEANUP,
        "github_comment_posted": github_comment_posted,
    }
    log.info("hard_cap cleanup arc complete for %s: %s", uow_id, summary)
    return summary


def _update_agenda_node_status(
    agenda: list[dict[str, Any]],
    target_status: str,
    filter_status: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return a new agenda with nodes matching filter_status updated to target_status.
    If filter_status is None, all nodes are updated.
    Pure function — does not mutate the input.
    """
    return [
        {**node, "status": target_status}
        if (filter_status is None or node.get("status") == filter_status)
        else node
        for node in agenda
    ]


def _mark_current_agenda_node_prescribed(
    agenda: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Mark the first 'pending' agenda node as 'prescribed'.
    Pure function.
    """
    found = False
    result = []
    for node in agenda:
        if not found and node.get("status") == "pending":
            result.append({**node, "status": "prescribed"})
            found = True
        else:
            result.append(node)
    return result


# ---------------------------------------------------------------------------
# Stuck condition detection
# ---------------------------------------------------------------------------

def _detect_stuck_condition(
    uow: UoW,
    reentry_posture: str,
    return_reason: str | None,
) -> str | None:
    """
    Check whether the UoW has hit a stuck condition.

    Returns the condition name string if stuck, or None if not stuck.

    PR C additions (V3):
    - philosophical_register: fires when register=philosophical AND reentry_posture != first_execution.
      On first execution, wait for executor evidence before surfacing.
    - no_gate_improvement: fires for iterative-convergent when gate_score has not improved
      over the last _NON_IMPROVING_GATE_THRESHOLD consecutive cycles (reads from steward_log).
    """
    cycles = uow.lifetime_cycles

    if cycles >= _HARD_CAP_CYCLES:
        return "hard_cap"

    # crashed_no_output + cycles >= 2 (uses steward_cycles for per-attempt crash detection)
    if return_reason == "crashed_no_output" and uow.steward_cycles >= _CRASH_SURFACE_CYCLES:
        return "crash_repeated"

    # PR C, Change 4a: philosophical_register — surface after first execution
    if uow.register == "philosophical" and reentry_posture != "first_execution":
        return "philosophical_register"

    # PR C, Change 4c: no_gate_improvement — iterative-convergent stall detection
    if uow.register == "iterative-convergent":
        non_improving = _count_non_improving_gate_cycles(
            uow.steward_log, n=_NON_IMPROVING_GATE_THRESHOLD
        )
        if non_improving >= _NON_IMPROVING_GATE_THRESHOLD:
            return "no_gate_improvement"

    return None


# ---------------------------------------------------------------------------
# Core per-UoW diagnosis function (pure — returns typed Diagnosis, no DB writes)
# ---------------------------------------------------------------------------

def _diagnose_uow(
    uow: UoW,
    audit_entries: list[dict],
    issue_info: IssueInfo | None,
) -> Diagnosis:
    """
    Produce a diagnosis for a single UoW.

    Pure function: reads inputs, returns a typed Diagnosis dataclass.
    The Diagnosis is frozen and immutable — callers cannot modify it.
    """
    return_reason = _most_recent_return_reason(audit_entries)
    reentry_posture = _determine_reentry_posture(audit_entries, return_reason)
    classification = _classify_return_reason(return_reason)

    output_ref = uow.output_ref
    output_valid = _output_ref_is_valid(output_ref)
    output_content = _read_output_ref(output_ref) if output_valid else ""

    success_criteria_missing = not uow.success_criteria

    is_complete, completion_rationale, executor_outcome = _assess_completion(
        uow, output_content, reentry_posture
    )

    # Orphan trace enrichment: when re-entry is an orphan posture, read trace.json
    # and classify the kill type (kill_before_start / kill_during_execution /
    # completed_without_output). This enriches completion_rationale so the prescriber
    # has evidence rather than diagnosing blind on every orphan re-entry.
    # Only fires for the three orphan postures — normal completion paths are unaffected.
    if reentry_posture in _ORPHAN_POSTURES:
        completion_rationale = _enrich_orphan_completion_rationale(
            base_rationale=completion_rationale,
            output_ref=output_ref,
            uow_id=uow.id,
        )

    stuck_condition = _detect_stuck_condition(uow, reentry_posture, return_reason)

    # Gap 3 (executor-contract.md): `blocked` outcome always routes to Dan.
    # Override stuck_condition here so _process_uow uses the existing surface path.
    if executor_outcome == "blocked" and stuck_condition is None:
        stuck_condition = "executor_blocked"

    # Hard cap overrides completion
    if stuck_condition == "hard_cap":
        is_complete = False

    return Diagnosis(
        reentry_posture=reentry_posture,
        return_reason=return_reason,
        return_reason_classification=classification,
        output_content=output_content,
        output_valid=output_valid,
        is_complete=is_complete,
        completion_rationale=completion_rationale,
        stuck_condition=stuck_condition,
        executor_outcome=executor_outcome,
        success_criteria_missing=success_criteria_missing,
    )


# ---------------------------------------------------------------------------
# GitHub client helper
# ---------------------------------------------------------------------------

def _repo_from_issue_url(issue_url: str | None) -> str | None:
    """Extract 'owner/repo' from a GitHub issue URL.

    Pure function — no side effects.

    Examples:
        "https://github.com/dcetlin/Lobster/issues/42" → "dcetlin/Lobster"
        None → None
        "not-a-url" → None
    """
    if not issue_url:
        return None
    # URL form: https://github.com/{owner}/{repo}/issues/{number}
    prefix = "https://github.com/"
    if not issue_url.startswith(prefix):
        return None
    rest = issue_url[len(prefix):]
    parts = rest.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def _fetch_github_issue(issue_number: int, repo: str) -> IssueInfo:
    """
    Fetch issue info from GitHub using gh CLI for a given repo.

    Returns a typed IssueInfo dataclass.
    On any error, returns IssueInfo with status_code=0 and empty fields.
    """
    command = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "state,labels,body,title",
    ]

    # Use error capture to detect and log subprocess failures with context
    result, error = run_subprocess_with_error_capture(
        component="steward_github",
        uow_id=f"{repo}#{issue_number}",
        command=command,
        timeout_seconds=15,
        check=False,  # Don't auto-log; handle gracefully
    )

    if error:
        log.warning("GitHub fetch error for %s#%s: %s", repo, issue_number, error.summary())
        return IssueInfo(status_code=0, state=None, labels=[], body="", title="")

    if result is None or result.returncode != 0:
        return IssueInfo(status_code=1, state=None, labels=[], body="", title="")

    try:
        data = json.loads(result.stdout)
        labels = [l.get("name", "") for l in data.get("labels", [])]
        return IssueInfo(
            status_code=200,
            state=data.get("state", "open"),
            labels=labels,
            body=data.get("body", ""),
            title=data.get("title", ""),
        )
    except Exception as e:
        log.warning("GitHub parse error for issue %s (repo=%s): %s", issue_number, repo, e)
        return IssueInfo(status_code=0, state=None, labels=[], body="", title="")


def _default_github_client(issue_number: int) -> IssueInfo:
    """
    Fetch issue info from GitHub using gh CLI.

    Falls back to the hardcoded 'dcetlin/Lobster' repo for UoWs that
    pre-date the issue_url field (migration 0005). New UoWs provide
    issue_url and the Steward loop calls _fetch_github_issue directly
    with the derived repo, bypassing this function.

    Returns a typed IssueInfo dataclass.
    """
    return _fetch_github_issue(issue_number, repo="dcetlin/Lobster")


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def _write_workflow_artifact(
    uow_id: str,
    instructions: str,
    prescribed_skills: list[str],
    artifact_dir: Path | None = None,
    executor_type: str = _EXECUTOR_TYPE_GENERAL,
) -> str:
    """
    Write a WorkflowArtifact to disk in front-matter + prose format (.md).

    The disk format (S3P2-B, issue #613) is front-matter + prose rather than
    pure JSON, making prescriptions human-readable and eliminating the class of
    JSONDecodeError failures caused by LLM preamble emission.

    Returns the absolute path to the written file.
    artifact_dir: override for the artifact directory (used in tests).
    executor_type: the executor type to embed in the artifact (defaults to general).
    """
    from src.orchestration.workflow_artifact import WorkflowArtifact, to_frontmatter
    artifact = WorkflowArtifact(
        uow_id=uow_id,
        executor_type=executor_type,
        constraints=[],
        prescribed_skills=prescribed_skills,
        instructions=instructions,
    )
    artifact_text = to_frontmatter(artifact)

    if artifact_dir is not None:
        artifact_dir = Path(artifact_dir)
        artifact_path = artifact_dir / f"{uow_id}.md"
    else:
        artifact_path = Path(os.path.expanduser(
            f"~/lobster-workspace/orchestration/artifacts/{uow_id}.md"
        ))

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(artifact_text, encoding="utf-8")

    return str(artifact_path.resolve())


# ---------------------------------------------------------------------------
# Dan notification
# ---------------------------------------------------------------------------

_DAN_CHAT_ID = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")


def _default_notify_dan(
    uow: UoW,
    condition: str,
    surface_log: str | None = None,
    return_reason: str | None = None,
) -> None:
    """
    Surface a UoW to Dan via the Lobster inbox.

    Writes a structured JSON message to ~/messages/inbox/ so the Lobster
    dispatcher surfaces it to Dan via Telegram. In tests this is replaced
    by a capturing mock via the `notify_dan` parameter.

    For hard_cap notifications, the body includes steward_log (diagnosis
    history across all cycles) and steward_agenda (what the Steward was
    trying to accomplish) so Dan can triage without leaving the inbox thread.
    """
    uow_id = uow.id
    # Use lifetime_cycles for hard_cap reporting (that's what triggered it);
    # steward_cycles for other conditions (per-attempt context).
    cycles = uow.lifetime_cycles if condition == "hard_cap" else uow.steward_cycles
    log.warning(
        "SURFACE TO DAN: UoW %s — condition=%s cycles=%s (lifetime_cycles=%s)",
        uow_id, condition, cycles, uow.lifetime_cycles,
    )
    msg_id = str(uuid.uuid4())
    if condition == "hard_cap":
        # Hard cap: exhaustive context so Dan can triage and act without
        # digging through logs. Include summary, agenda, log, and reason.
        body_lines = [
            f"WOS: UoW `{uow_id}` hit hard cap ({_HARD_CAP_CYCLES} lifetime cycles). "
            f"return_reason: {return_reason}.",
        ]

        # UoW summary — what was this trying to accomplish?
        summary = uow.summary
        if summary:
            body_lines.append(f"\nSummary: {summary}")

        # Success criteria — what would done look like?
        success_criteria = uow.success_criteria
        if success_criteria:
            body_lines.append(f"\nSuccess criteria: {success_criteria}")

        # Steward agenda — the structured forecast of what was planned
        steward_agenda_raw = uow.steward_agenda
        if steward_agenda_raw:
            try:
                agenda = json.loads(steward_agenda_raw)
                # Render agenda nodes as a compact list for readability
                agenda_lines: list[str] = []
                nodes = agenda if isinstance(agenda, list) else [agenda]
                for node in nodes:
                    posture = node.get("posture", "?")
                    status = node.get("status", "?")
                    context = node.get("context", "")
                    agenda_lines.append(f"  [{status}] {posture}: {context[:120]}")
                body_lines.append("\nSteward agenda:\n" + "\n".join(agenda_lines))
            except (json.JSONDecodeError, TypeError, AttributeError):
                # If agenda is not valid JSON or not a list, include raw text
                body_lines.append(f"\nSteward agenda (raw):\n{steward_agenda_raw[:500]}")

        # Steward log — full diagnosis history across all cycles (surface_log == current_log_str)
        if surface_log:
            # Show last N log lines to keep the message readable
            log_lines = [ln for ln in surface_log.strip().splitlines() if ln.strip()]
            _MAX_LOG_LINES = 20
            if len(log_lines) > _MAX_LOG_LINES:
                omitted = len(log_lines) - _MAX_LOG_LINES
                displayed = log_lines[-_MAX_LOG_LINES:]
                body_lines.append(
                    f"\nSteward log (last {_MAX_LOG_LINES} of {len(log_lines)} entries, "
                    f"{omitted} omitted):\n" + "\n".join(displayed)
                )
            else:
                body_lines.append(f"\nSteward log:\n" + "\n".join(log_lines))
    elif condition == "philosophical_register":
        body_lines = [
            f"WOS: UoW {uow_id!r} is in philosophical register — executor returned output "
            f"but completion requires human judgment. "
            f"See output at {uow.output_ref}. "
            f"Summary: {(uow.summary or '')[:200]}",
        ]
    elif condition == "register_mismatch":
        # Extract mismatch details from surface_log for structured message
        _mismatch_register = uow.register
        _mismatch_executor = "unknown"
        _mismatch_direction = f"{_mismatch_register}→unknown"
        if surface_log:
            for _line in reversed(surface_log.strip().splitlines()):
                _stripped = _line.strip()
                if not _stripped:
                    continue
                try:
                    _entry = json.loads(_stripped)
                    if _entry.get("event") == "register_mismatch":
                        _mismatch_executor = _entry.get("executor_type_attempted", "unknown")
                        _mismatch_direction = _entry.get("direction", _mismatch_direction)
                        break
                except (json.JSONDecodeError, TypeError):
                    pass
        body_lines = [
            f"WOS: UoW {uow_id} — register mismatch. "
            f"UoW register: {_mismatch_register}. "
            f"Prescribed executor type: {_mismatch_executor}. "
            f"A {_mismatch_register}-register UoW cannot be dispatched to {_mismatch_executor}. "
            "Manual routing required."
        ]
    elif condition == "no_gate_improvement":
        body_lines = [
            f"WOS: UoW {uow_id!r} — iterative-convergent gate not improving after "
            f"{_NON_IMPROVING_GATE_THRESHOLD} cycles. "
            f"See steward_log for gate_score history and prescription_delta.",
        ]
        if surface_log:
            body_lines.append(f"\nSteward log:\n{surface_log}")
    else:
        body_lines = [
            f"WOS SURFACE: UoW {uow_id} hit condition={condition} "
            f"(steward_cycles={cycles}). Needs human review.",
        ]
        if surface_log:
            body_lines.append(f"\nSteward log:\n{surface_log}")
    # Inline buttons let Dan resolve the stuck UoW without typing commands.
    # The dispatcher routes callback_data="decide_retry:<uow_id>" and
    # callback_data="decide_close:<uow_id>" to handle_decide_retry/close.
    buttons = [
        [
            {"text": "Retry", "callback_data": f"decide_retry:{uow_id}"},
            {"text": "Close", "callback_data": f"decide_close:{uow_id}"},
        ]
    ]
    msg = {
        "id": msg_id,
        "source": "system",
        "chat_id": _DAN_CHAT_ID,
        "text": "\n".join(body_lines),
        "buttons": buttons,
        "timestamp": time.time(),
        "metadata": {
            "type": "wos_surface",
            "uow_id": uow_id,
            "condition": condition,
            "steward_cycles": cycles,
            "return_reason": return_reason,
            "steward_log": surface_log,
            "steward_agenda": uow.steward_agenda,
        },
    }
    inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"{msg_id}.json").write_text(
            json.dumps(msg, indent=2), encoding="utf-8"
        )
        log.info("WOS surface message written to inbox: %s", msg_id)
    except OSError as e:
        log.error("Failed to write WOS surface message to inbox: %s", e)


def _default_notify_dan_early_warning(
    uow: UoW,
    return_reason: str | None,
    new_cycles: int | None = None,
) -> None:
    """
    Send an early-warning notification to Dan when lifetime_cycles reaches
    _EARLY_WARNING_CYCLES (4), five cycles before the hard cap (_HARD_CAP_CYCLES=9).

    new_cycles is the post-prescription cycle count (uow.steward_cycles + 1).
    Pass it explicitly so the message reflects the cycle count after prescription,
    not the stale pre-prescription value on the UoW object.

    Uses the same inbox path as _default_notify_dan. In tests, override via
    the `notify_dan_early_warning` parameter on run_steward_cycle / _process_uow.
    """
    uow_id = uow.id
    cycles = new_cycles if new_cycles is not None else uow.steward_cycles
    admin_chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", _DAN_CHAT_ID)
    log.warning(
        "WOS EARLY WARNING: UoW %s at cycle %s — approaching hard cap (%s)",
        uow_id, cycles, _HARD_CAP_CYCLES,
    )
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "chat_id": admin_chat_id,
        "text": (
            f"⚠️ WOS: UoW `{uow_id}` at cycle {cycles} — "
            f"approaching hard cap ({_HARD_CAP_CYCLES}). "
            f"Last return_reason: {return_reason}"
        ),
        "timestamp": time.time(),
        "metadata": {
            "type": "wos_early_warning",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "return_reason": return_reason,
        },
    }
    inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"{msg_id}.json").write_text(
            json.dumps(msg, indent=2), encoding="utf-8"
        )
        log.info("WOS early-warning message written to inbox: %s", msg_id)
    except OSError as e:
        log.error("Failed to write WOS early-warning message to inbox: %s", e)


# ---------------------------------------------------------------------------
# Escalation notification (retry cap reached)
# ---------------------------------------------------------------------------

def _send_escalation_notification(uow: UoW) -> None:
    """
    Send a Telegram notification to Dan when a UoW has exhausted MAX_RETRIES
    re-dispatch attempts and is being transitioned to needs-human-review.

    Uses the same inbox-write pattern as _default_notify_dan so the dispatcher
    surfaces the message to Dan via Telegram.
    """
    uow_id = uow.id
    chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", _DAN_CHAT_ID)
    log.warning(
        "WOS ESCALATION: UoW %s exhausted %d retries — transitioning to needs-human-review",
        uow_id, MAX_RETRIES,
    )
    msg_id = str(uuid.uuid4())
    text = (
        f"WOS: UoW `{uow_id}` needs human review after {MAX_RETRIES} failed retries.\n\n"
        f"ID: {uow_id}\n"
        f"Summary: {(uow.summary or '')[:200]}\n"
        f"Type: {uow.type}\n"
        f"Last state: {uow.status}\n"
        f"Retry count: {uow.retry_count}\n\n"
        f"To act: reply 'retry {uow_id}' or 'close {uow_id}'.\n"
        f"(Button handlers are a follow-on — text commands are the current interface.)"
    )
    msg = {
        "id": msg_id,
        "source": "system",
        "chat_id": chat_id,
        "text": text,
        "timestamp": time.time(),
        "metadata": {
            "type": "wos_surface",
            "uow_id": uow_id,
            "condition": "retry_cap",
            "retry_count": uow.retry_count,
            "steward_cycles": uow.steward_cycles,
        },
    }
    inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"{msg_id}.json").write_text(
            json.dumps(msg, indent=2), encoding="utf-8"
        )
        log.info("WOS escalation message written to inbox: %s", msg_id)
    except OSError as e:
        log.error("Failed to write WOS escalation message to inbox: %s", e)


# ---------------------------------------------------------------------------
# DB fetch helpers
# ---------------------------------------------------------------------------

def _fetch_audit_entries(registry, uow_id: str) -> list[dict[str, Any]]:
    """Fetch all audit_log entries for a UoW, ordered by id ascending."""
    conn = registry._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE uow_id = ? ORDER BY id ASC",
            (uow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-UoW processing
# ---------------------------------------------------------------------------

def _process_uow(
    uow: UoW,
    registry,
    audit_entries: list[dict[str, Any]],
    issue_info: IssueInfo | None,
    dry_run: bool,
    artifact_dir: Path | None,
    notify_dan: Callable | None,
    notify_dan_early_warning: Callable | None = None,
    llm_prescriber: Callable[..., LLMPrescription | None] | None = _llm_prescribe,
    inline_executor: Callable[[str], Any] | None = None,
) -> StewardOutcome:
    """
    Process a single UoW through the full diagnosis + prescribe/close/surface cycle.

    Returns a StewardOutcome: Prescribed | Done | Surfaced | RaceSkipped | WaitForTrace.

    Args:
        issue_info: Typed IssueInfo from GitHub API (or None if no issue).
        llm_prescriber: Callable returning LLMPrescription or None. Inject None to bypass LLM.
        inline_executor: Optional callable(uow_id) that is invoked immediately after
            the READY_FOR_EXECUTOR transition, collapsing the polling hop described in
            issue #648 Part A.  When provided, the Steward dispatches the Executor
            synchronously rather than waiting for the next heartbeat cycle (0–3 min).
            The Executor's optimistic lock protects against double-execution if a
            concurrent heartbeat fires between the transition and the inline call.
            Defaults to None (no inline dispatch — heartbeat remains the dispatch path).
    """
    uow_id = uow.id
    cycles = uow.steward_cycles

    # Step 1: Claim (optimistic lock) — only if not in dry-run mode
    if not dry_run:
        rows = registry.transition(uow_id, _STATUS_DIAGNOSING, _STATUS_READY_FOR_STEWARD)
        if rows == 0:
            log.debug("UoW %s already claimed by another Steward instance — skipping", uow_id)
            return RaceSkipped(uow_id=uow_id)

    # Step 2: Initialization ritual — write steward_agenda on first contact
    current_agenda_str = uow.steward_agenda
    current_log_str = uow.steward_log

    agenda: list[dict[str, Any]] = []
    if current_agenda_str:
        try:
            agenda = json.loads(current_agenda_str)
        except (json.JSONDecodeError, TypeError):
            agenda = []

    if cycles == 0:
        # Build initial agenda before any other action
        issue_body = issue_info.body if issue_info else ""
        agenda = _build_initial_agenda(uow, issue_body)
        agenda_log_entry = {
            "event": "agenda_update",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "update_type": "initial",
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, agenda_log_entry)

        if not dry_run:
            _write_steward_fields(
                registry, uow_id,
                steward_agenda=json.dumps(agenda),
                steward_log=current_log_str,
            )
            # Write agenda_update to audit_log
            registry.append_audit_log(uow_id, {
                "event": "agenda_update",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "update_type": "initial",
                "timestamp": _now_iso(),
            })

    # Step 3: Diagnose — returns typed Diagnosis dataclass
    diagnosis = _diagnose_uow(uow, audit_entries, issue_info)
    reentry_posture = diagnosis.reentry_posture
    return_reason = diagnosis.return_reason
    is_complete = diagnosis.is_complete
    completion_rationale = diagnosis.completion_rationale
    stuck_condition = diagnosis.stuck_condition
    success_criteria_missing = diagnosis.success_criteria_missing
    executor_outcome = diagnosis.executor_outcome

    # Append diagnosis to steward_log
    diag_log_entry = {
        "event": "diagnosis",
        "uow_id": uow_id,
        "steward_cycles": cycles,
        "re_entry_posture": reentry_posture,
        "return_reason": return_reason,
        "is_complete": is_complete,
        "completion_rationale": completion_rationale,
        "stuck_condition": stuck_condition,
    }
    current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, diag_log_entry)

    # Write diagnosis audit entry BEFORE any prescription or transition
    if not dry_run:
        _write_steward_fields(registry, uow_id, steward_log=current_log_str)

        audit_note: dict[str, Any] = {
            "event": "steward_diagnosis",
            "actor": _ACTOR_STEWARD,
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "re_entry_posture": reentry_posture,
            "return_reason": return_reason,
            "is_complete": is_complete,
            "completion_rationale": completion_rationale,
            "timestamp": _now_iso(),
        }
        if success_criteria_missing:
            audit_note["success_criteria_missing"] = True
            audit_note["note"] = "evaluating against summary field as fallback"
        registry.append_audit_log(uow_id, audit_note)

    # Pre-branch trace write — unconditional, fires before stuck/done/prescribe split.
    # The agenda list at this point contains the initial agenda nodes (from the
    # initialization ritual above). We append one trace entry per cycle so
    # steward_agenda accumulates a full cycle history. Branch-level writes that
    # follow (done: mark nodes complete; prescribe: mark node prescribed) will
    # overwrite steward_agenda with the trace entry already present in the list.
    trace_entry = _build_trace_entry(diagnosis, cycles)
    trace_agenda = _parse_steward_agenda(uow.steward_agenda)
    # On cycle 0, the initial agenda was already written in the initialization
    # ritual above; re-read it from the in-memory `agenda` variable to avoid
    # an extra DB round-trip and to pick up that write's content.
    if cycles == 0:
        trace_agenda = list(agenda)
    trace_agenda.append(trace_entry)
    if not dry_run:
        _write_steward_fields(registry, uow_id, steward_agenda=json.dumps(trace_agenda))

    # Step 4: Convergence or prescription

    # 4a: Stuck condition check (fires before completion/prescription)
    if stuck_condition:
        surface_log = current_log_str

        surface_log_entry = {
            "event": "surface",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "surface_condition": stuck_condition,
            "return_reason": return_reason,
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, surface_log_entry)

        if not dry_run:
            _write_steward_fields(registry, uow_id, steward_log=current_log_str)
            registry.append_audit_log(uow_id, {
                "event": "steward_surface",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "surface_condition": stuck_condition,
                "return_reason": return_reason,
                "timestamp": _now_iso(),
            })

        # Hard cap: run cleanup arc before surfacing to Dan (S3-A).
        # The cleanup arc archives artifacts, writes failure trace, and sets
        # close_reason/closed_at. Each step is individually protected — failure
        # in archival does not block the surface-to-Dan path.
        cleanup_summary: dict[str, Any] | None = None
        if stuck_condition == "hard_cap" and not dry_run:
            # Derive archived_dir from artifact_dir so archived artifacts stay
            # co-located with active ones (under artifacts/archived/).
            _resolved_artifact_dir = (
                Path(artifact_dir) if artifact_dir is not None
                else _DEFAULT_ARTIFACTS_DIR
            )
            cleanup_summary = _run_hard_cap_cleanup(
                uow=uow,
                registry=registry,
                return_reason=return_reason,
                artifact_dir=artifact_dir,
                archived_dir=_resolved_artifact_dir / "archived",
            )
            # Append cleanup summary to audit log
            registry.append_audit_log(uow_id, {
                "event": "hard_cap_cleanup",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "timestamp": _now_iso(),
                **(cleanup_summary or {}),
            })

        # Surface to Dan (injectable for tests)
        _notify = notify_dan or _default_notify_dan
        _notify(uow, stuck_condition, surface_log=current_log_str, return_reason=return_reason)

        if not dry_run:
            registry.transition(uow_id, _STATUS_BLOCKED, _STATUS_DIAGNOSING)

        _append_cycle_trace(
            uow_id=uow_id,
            cycle_num=cycles,
            subagent_excerpt=_read_output_ref(uow.output_ref),
            return_reason=return_reason or "",
            next_action="stuck",
            artifact_dir=artifact_dir,
        )
        return Surfaced(uow_id=uow_id, condition=stuck_condition)

    # 4b: Declare done
    if is_complete:
        # Mark all agenda nodes complete (including the trace entry just appended)
        completed_agenda = _update_agenda_node_status(trace_agenda, "complete")

        closure_entry = {
            "event": "steward_closure",
            "actor": _ACTOR_STEWARD,
            "uow_id": uow_id,
            "assessment": completion_rationale,
            "timestamp": _now_iso(),
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, closure_entry)

        if not dry_run:
            _write_steward_fields(
                registry, uow_id,
                steward_agenda=json.dumps(completed_agenda),
                steward_log=current_log_str,
                completed_at=_now_iso(),
            )
            registry.append_audit_log(uow_id, {
                "event": "steward_closure",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "assessment": completion_rationale,
                "timestamp": _now_iso(),
            })
            # Primary done-transition: expects 'diagnosing' (set by the claim
            # step above). This is the normal path.
            #
            # Fallback done-transition (issue #671): a concurrent startup_sweep
            # may reset status from 'diagnosing' → 'ready-for-steward' between
            # the claim and this transition. When the primary WHERE fails (rows=0),
            # attempt a fallback transition from 'ready-for-steward' so the closure
            # is not silently lost.
            rows = registry.transition(uow_id, _STATUS_DONE, _STATUS_DIAGNOSING)
            if rows == 0:
                rows = registry.transition(uow_id, _STATUS_DONE, _STATUS_READY_FOR_STEWARD)
                if rows == 0:
                    log.warning(
                        "done-transition failed for %s — status was neither 'diagnosing' "
                        "nor 'ready-for-steward' (possible concurrent state change); "
                        "UoW may not have reached 'done'",
                        uow_id,
                    )
                else:
                    log.info(
                        "done-transition fallback succeeded for %s — "
                        "status was 'ready-for-steward' (startup_sweep reset race)",
                        uow_id,
                    )

        _append_cycle_trace(
            uow_id=uow_id,
            cycle_num=cycles,
            subagent_excerpt=_read_output_ref(uow.output_ref),
            return_reason=return_reason or "",
            next_action="done",
            artifact_dir=artifact_dir,
        )
        return Done(uow_id=uow_id)

    # 4b-orphan: executing_orphan short-circuit.
    # A UoW whose subagent exited without calling write_result should not be
    # retried — the retry loop cannot fix a subagent that systematically skips
    # the result contract. Mark failed immediately so the Steward does not
    # re-dispatch into an infinite loop.
    if reentry_posture == "executing_orphan":
        orphan_reason = "executing_orphan: subagent exited without calling write_result"
        orphan_log_entry = {
            "event": "executing_orphan_failed",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "reason": orphan_reason,
            "timestamp": _now_iso(),
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, orphan_log_entry)
        if not dry_run:
            _write_steward_fields(registry, uow_id, steward_log=current_log_str)
            registry.append_audit_log(uow_id, {
                "event": "executing_orphan_failed",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "reason": orphan_reason,
                "timestamp": _now_iso(),
            })
            registry.fail_uow(uow_id, orphan_reason)
        _append_cycle_trace(
            uow_id=uow_id,
            cycle_num=cycles,
            subagent_excerpt=_read_output_ref(uow.output_ref),
            return_reason=return_reason or "",
            next_action="failed",
            artifact_dir=artifact_dir,
        )
        return Surfaced(uow_id=uow_id, condition="executing_orphan")

    # 4c: Prescribe another Executor pass
    # executor-contract.md Steward Interpretation Table: `partial` and `failed`
    # require distinct re-diagnosis inputs. For `partial`, include
    # steps_completed/steps_total from the result file so the prescription
    # reflects how far the previous execution got. For `failed`, re-diagnose
    # with `reason` as the primary input (already in completion_rationale).

    # 4c-retry-cap: gate MAX_RETRIES on execution_attempts (confirmed dispatches).
    #
    # On the first execution (cycles == 0, reentry_posture == "first_execution"),
    # skip the cap entirely — it only applies to re-dispatches after a prior cycle.
    #
    # On re-entry (cycles > 0), always increment retry_count (diagnostic counter).
    # Only increment execution_attempts when return_reason is NOT an infrastructure
    # event (ORPHAN_REASONS). Infrastructure events are session kills — no execution
    # outcome was produced, so they must not consume the execution retry budget.
    #
    # MAX_RETRIES gates on execution_attempts, NOT retry_count. This prevents the
    # 2026-04-26 failure mode where 3 orphan kill events exhausted the retry budget
    # and produced needs-human-review escalations for UoWs with lifetime_cycles=0.
    if cycles > 0 and reentry_posture != "first_execution":
        new_retry_count = uow.retry_count + 1
        # Only count confirmed executions toward the retry budget.
        is_infra_event = _is_infrastructure_event(return_reason)
        new_execution_attempts = uow.execution_attempts + (0 if is_infra_event else 1)

        if new_execution_attempts > MAX_RETRIES:
            # Execution retry cap exceeded — escalate to needs-human-review.
            escalation_entry = {
                "event": "retry_cap_exceeded",
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "retry_count": new_retry_count,
                "execution_attempts": new_execution_attempts,
                "max_retries": MAX_RETRIES,
                "return_reason": return_reason,
            }
            current_log_str = _append_steward_log_entry(
                registry, uow_id, current_log_str, escalation_entry
            )
            if not dry_run:
                _write_steward_fields(
                    registry, uow_id,
                    steward_log=current_log_str,
                    retry_count=new_retry_count,
                    execution_attempts=uow.execution_attempts,  # do not increment at cap
                )
                registry.append_audit_log(uow_id, {
                    "event": "retry_cap_exceeded",
                    "actor": _ACTOR_STEWARD,
                    "uow_id": uow_id,
                    "steward_cycles": cycles,
                    "retry_count": new_retry_count,
                    "execution_attempts": uow.execution_attempts,
                    "max_retries": MAX_RETRIES,
                    "return_reason": return_reason,
                    "timestamp": _now_iso(),
                })
                registry.transition(uow_id, _STATUS_NEEDS_HUMAN_REVIEW, _STATUS_DIAGNOSING)
            _send_escalation_notification(uow)
            _append_cycle_trace(
                uow_id=uow_id,
                cycle_num=cycles,
                subagent_excerpt=_read_output_ref(uow.output_ref),
                return_reason=return_reason or "",
                next_action="escalated",
                artifact_dir=artifact_dir,
            )
            return Surfaced(uow_id=uow_id, condition="retry_cap")
        else:
            # Below cap — increment both counters and proceed with prescription.
            if not dry_run:
                _write_steward_fields(
                    registry, uow_id,
                    retry_count=new_retry_count,
                    execution_attempts=new_execution_attempts,
                )
            if is_infra_event:
                log.info(
                    "_process_uow: UoW %s infrastructure event (return_reason=%r) — "
                    "retry_count=%d, execution_attempts=%d/%d (budget unchanged)",
                    uow_id, return_reason, new_retry_count,
                    uow.execution_attempts, MAX_RETRIES,
                )
            else:
                log.info(
                    "_process_uow: UoW %s execution attempt %d/%d (retry_count=%d)",
                    uow_id, new_execution_attempts, MAX_RETRIES, new_retry_count,
                )

    # 4c-gate: Corrective trace one-cycle temporal gate (cristae-junction delay).
    # Before prescribing again after an executor return, the executor must have
    # written a corrective trace ({output_ref}.trace.json). This forces temporal
    # spacing between action and next prescription — the software equivalent of
    # the cristae geometry delay between proton pump action and ATP synthesis.
    # Gate applies only when result.json exists (executor actually returned).
    output_ref_for_gate = uow.output_ref
    result_file_exists = False
    if output_ref_for_gate:
        _rf = Path(output_ref_for_gate).with_suffix(".result.json")
        if not _rf.exists():
            _rf_alt = Path(str(output_ref_for_gate) + ".result.json")
            if _rf_alt.exists():
                _rf = _rf_alt
        result_file_exists = _rf.exists()

    if result_file_exists and output_ref_for_gate:
        trace_file = Path(output_ref_for_gate).with_suffix(".trace.json")
        if not trace_file.exists():
            trace_file = Path(str(output_ref_for_gate) + ".trace.json")
        trace_exists = trace_file.exists()

        if not trace_exists:
            # trace.json absent — apply one-cycle wait gate (S3-B cristae-junction delay).
            already_waited = _check_trace_gate_waited(current_log_str)
            if not already_waited:
                # First visit: log trace_gate_waited and keep UoW in diagnosing state.
                # The startup_sweep on the next heartbeat will reset diagnosing →
                # ready-for-steward so the gate is re-evaluated with a fresh trace check.
                log.info(
                    "_process_uow: trace.json absent for %s — logging trace_gate_waited, "
                    "keeping in diagnosing state for one heartbeat (cristae-junction delay)",
                    uow_id,
                )
                wait_entry = {
                    "event": "trace_gate_waited",
                    "uow_id": uow_id,
                    "steward_cycles": cycles,
                    "output_ref": output_ref_for_gate,
                    "timestamp": _now_iso(),
                }
                current_log_str = _append_steward_log_entry(
                    registry, uow_id, current_log_str, wait_entry
                )
                if not dry_run:
                    _write_steward_fields(registry, uow_id, steward_log=current_log_str)
                    registry.append_audit_log(uow_id, {
                        "event": "trace_gate_waited",
                        "actor": _ACTOR_STEWARD,
                        "uow_id": uow_id,
                        "steward_cycles": cycles,
                        "note": json.dumps({
                            "trace_gate_waited": _now_iso(),
                            "output_ref": output_ref_for_gate,
                        }),
                        "timestamp": _now_iso(),
                    })
                    # UoW stays in diagnosing — startup_sweep on next heartbeat resets it.
                    # Do NOT transition back to ready-for-steward here.
                # Return WaitForTrace outcome — distinct from Prescribed, counted separately.
                _append_cycle_trace(
                    uow_id=uow_id,
                    cycle_num=cycles,
                    subagent_excerpt=_read_output_ref(uow.output_ref),
                    return_reason=return_reason or "",
                    next_action="wait_for_trace",
                    artifact_dir=artifact_dir,
                )
                return WaitForTrace(uow_id=uow_id)
            else:
                # Already waited one heartbeat — trace still absent.
                # Log trace_gate_timeout and proceed with prescription (non-blocking fallback).
                log.warning(
                    "_process_uow: trace.json absent after one-cycle wait for %s — "
                    "logging trace_gate_timeout and proceeding with prescription",
                    uow_id,
                )
                timeout_entry = {
                    "event": "trace_gate_timeout",
                    "uow_id": uow_id,
                    "steward_cycles": cycles,
                    "output_ref": output_ref_for_gate,
                    "message": "trace.json absent after one-cycle wait — proceeding with prescription (non-blocking fallback)",
                }
                current_log_str = _append_steward_log_entry(
                    registry, uow_id, current_log_str, timeout_entry
                )
                if not dry_run:
                    _write_steward_fields(registry, uow_id, steward_log=current_log_str)
                    registry.append_audit_log(uow_id, {
                        "event": "trace_gate_timeout",
                        "actor": _ACTOR_STEWARD,
                        "uow_id": uow_id,
                        "steward_cycles": cycles,
                        "note": json.dumps({
                            "message": "trace.json absent after one-cycle wait — proceeding with prescription (non-blocking fallback)",
                            "output_ref": output_ref_for_gate,
                        }),
                        "timestamp": _now_iso(),
                    })
        else:
            # trace.json exists — clear any stale trace_gate_waited entries
            cleared_log = _clear_trace_gate_waited(current_log_str)
            if cleared_log != current_log_str:
                current_log_str = cleared_log
                if not dry_run:
                    _write_steward_fields(registry, uow_id, steward_log=current_log_str)

    # PR C, Change 2: Corrective trace injection.
    # When trace.json exists, read it and inject its content into the prescription context.
    # This happens after the trace gate check so we only inject when a valid trace is present.
    _trace_data: dict | None = None
    _trace_surprises: list[str] = []
    _trace_prescription_delta: str = ""
    _trace_gate_score: dict | None = None

    if output_ref_for_gate:
        _trace_data = _read_trace_json(output_ref_for_gate, expected_uow_id=uow_id)
        if _trace_data is not None:
            _trace_surprises = _trace_data.get("surprises") or []
            _raw_prescription_delta = _trace_data.get("prescription_delta") or ""
            # Read historical prescription_deltas from corrective_traces for bounding.
            # Routed through Registry to keep all corrective_traces reads behind the
            # abstraction layer — no raw sqlite3 connections at call sites.
            _prior_deltas: list[str] = registry.get_corrective_trace_history(uow_id)
            _trace_prescription_delta = _bound_prescription_delta(
                _raw_prescription_delta, history=_prior_deltas
            )
            _trace_gate_score = _trace_data.get("gate_score")

            # Write trace_injection entry to steward_log
            _trace_log_entry = {
                "event": "trace_injection",
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "register": _trace_data.get("register", uow.register),
                "gate_score": _trace_gate_score,
                "surprises_count": len(_trace_surprises),
                "prescription_delta_present": bool(_trace_prescription_delta),
                "timestamp": _now_iso(),
            }
            current_log_str = _append_steward_log_entry(
                registry, uow_id, current_log_str, _trace_log_entry
            )
            if not dry_run:
                _write_steward_fields(registry, uow_id, steward_log=current_log_str)
                registry.append_audit_log(uow_id, {
                    "event": "trace_injection",
                    "actor": _ACTOR_STEWARD,
                    "uow_id": uow_id,
                    "steward_cycles": cycles,
                    "note": json.dumps({
                        "execution_summary": _trace_data.get("execution_summary", ""),
                        "surprises_count": len(_trace_surprises),
                        "prescription_delta_present": bool(_trace_prescription_delta),
                        "gate_score": _trace_gate_score,
                    }),
                    "timestamp": _now_iso(),
                })

    new_cycles = cycles + 1
    prescribed_skills = _select_prescribed_skills(uow, reentry_posture)
    selected_executor_type = _select_executor_type(uow)

    # Register-mismatch gate (Change 3): check compatibility before writing artifact.
    # If the selected executor_type is incompatible with the UoW's register, block
    # dispatch and surface to Dan. The gate only fires in the prescribe branch (4c).
    is_compatible, mismatch_reason = _check_register_executor_compatibility(
        uow.register, selected_executor_type
    )
    if not is_compatible:
        log.warning(
            "_process_uow: register_mismatch for %s — register=%r executor_type=%r: %s",
            uow_id, uow.register, selected_executor_type, mismatch_reason,
        )
        _direction = f"{uow.register}\u2192{selected_executor_type}"
        mismatch_obs = {
            "event": "register_mismatch_observation",
            "uow_id": uow_id,
            "register": uow.register,
            "executor_type_attempted": selected_executor_type,
            "direction": _direction,
            "steward_cycles": cycles,
            "timestamp": _now_iso(),
        }
        mismatch_log_entry = {
            "event": "register_mismatch",
            "uow_id": uow_id,
            "steward_cycles": cycles,
            "register": uow.register,
            "executor_type_attempted": selected_executor_type,
            "direction": _direction,
        }
        current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, mismatch_log_entry)
        if not dry_run:
            _write_steward_fields(registry, uow_id, steward_log=current_log_str)
            registry.append_audit_log(uow_id, {
                "event": "register_mismatch_observation",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "register": uow.register,
                "executor_type_attempted": selected_executor_type,
                "direction": _direction,
                "steward_cycles": cycles,
                "timestamp": _now_iso(),
            })
            registry.transition(uow_id, _STATUS_BLOCKED, _STATUS_DIAGNOSING)

        _notify_mismatch = notify_dan or _default_notify_dan
        _notify_mismatch(uow, "register_mismatch", surface_log=current_log_str, return_reason=return_reason)
        return Surfaced(uow_id=uow_id, condition="register_mismatch")

    partial_steps_context: str = ""
    if executor_outcome == "partial" and uow.output_ref:
        # Read steps_completed/steps_total from result file for partial continuation
        output_ref_path = uow.output_ref
        result_file = Path(output_ref_path).with_suffix(".result.json")
        if not result_file.exists():
            result_file_alt = Path(str(output_ref_path) + ".result.json")
            if result_file_alt.exists():
                result_file = result_file_alt
        if result_file.exists():
            try:
                result_data = json.loads(result_file.read_text(encoding="utf-8"))
                steps_completed = result_data.get("steps_completed")
                steps_total = result_data.get("steps_total")
                if steps_completed is not None or steps_total is not None:
                    partial_steps_context = (
                        f"steps_completed={steps_completed}, steps_total={steps_total}"
                    )
            except (json.JSONDecodeError, OSError):
                pass

    if executor_outcome == "partial" and partial_steps_context:
        completion_gap_for_prescription = (
            f"{completion_rationale} [{partial_steps_context}]"
        )
    else:
        completion_gap_for_prescription = completion_rationale

    route_reason = _build_prescription_route_reason(
        uow, reentry_posture, executor_outcome, partial_steps_context, completion_rationale
    )

    # PR C, Change 2: Inject trace content into prescription context.
    # Surprises and prescription_delta from the corrective trace are injected here
    # so the LLM prescriber sees them in the completion_gap context string.
    if _trace_surprises:
        surprises_text = "; ".join(str(s) for s in _trace_surprises)
        completion_gap_for_prescription = (
            f"Executor reported surprises: {surprises_text}. {completion_gap_for_prescription}"
        )
    if _trace_prescription_delta:
        completion_gap_for_prescription = (
            f"{completion_gap_for_prescription} "
            f"Executor recommends prescription change: {_trace_prescription_delta}"
        )
    # For iterative-convergent: include gate_score in completion_gap
    if uow.register == "iterative-convergent" and _trace_gate_score:
        score_val = _trace_gate_score.get("score", "unknown")
        gate_cmd = _trace_gate_score.get("command", "")
        completion_gap_for_prescription = (
            f"{completion_gap_for_prescription} "
            f"[gate_score={score_val}, cmd={gate_cmd!r}]"
        )

    issue_body = issue_info.body if issue_info else ""

    # Fetch prior prescription attempts from steward_log when re-prescribing
    # (cycles > 0).  This lets the Executor see what was already tried so it
    # can avoid repeating approaches that did not work.
    prior_prescriptions = (
        _fetch_prior_prescriptions(current_log_str, limit=3)
        if cycles > 0
        else []
    )

    # Wrap llm_prescriber to capture which path was taken (llm vs fallback).
    # The sentinel records a non-None return, indicating the LLM path succeeded.
    _llm_path_taken: list[bool] = [False]

    def _capturing_prescriber(
        uow_arg: UoW,
        reentry_posture_arg: str,
        completion_gap_arg: str,
        issue_body_arg: str = "",
    ) -> LLMPrescription | None:
        result = llm_prescriber(uow_arg, reentry_posture_arg, completion_gap_arg, issue_body_arg)  # type: ignore[misc]
        if result is not None:
            _llm_path_taken[0] = True
        return result

    effective_prescriber = _capturing_prescriber if llm_prescriber is not None else None

    try:
        instructions = _build_prescription_instructions(
            uow, reentry_posture, completion_gap_for_prescription, issue_body,
            llm_prescriber=effective_prescriber,
            prior_prescriptions=prior_prescriptions,
        )
    except LLMPrescriptionError as e:
        # LLM prescription failed hard — do not fall back, raise error
        log.error(
            "_process_uow: LLM prescription failed for %s — %s",
            uow_id, str(e),
        )
        # Write error audit entry
        if not dry_run:
            registry.append_audit_log(uow_id, {
                "event": "llm_prescription_error",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "error_message": str(e),
                "timestamp": _now_iso(),
            })
            # Transition back to ready-for-steward to allow retry
            # (or remain in diagnosing if transition fails)
            try:
                registry.transition(uow_id, _STATUS_READY_FOR_STEWARD, _STATUS_DIAGNOSING)
            except Exception as transition_err:
                log.error(
                    "_process_uow: failed to transition UoW %s back to ready-for-steward: %s",
                    uow_id, transition_err,
                )
        raise

    # Prescription always comes from LLM now; fallback path has been removed.
    # If LLM prescription fails, an exception is raised (fail-hard behavior).
    prescription_path = "llm"

    # Update agenda: mark current pending node as prescribed
    # Use trace_agenda (which includes the trace entry we just wrote) as the base
    # so the prescription status update is applied on top of the full trace.
    updated_agenda = _mark_current_agenda_node_prescribed(trace_agenda)

    prescription_log_entry = {
        "event": "reentry_prescription" if cycles > 0 else "prescription",
        "uow_id": uow_id,
        "steward_cycles": cycles,
        "return_reason": return_reason,
        "completion_assessment": completion_rationale,
        "prescription_path": prescription_path,
        "dod_revised": False,
        "agenda_revised": False,
        "next_posture_rationale": route_reason,
    }
    current_log_str = _append_steward_log_entry(registry, uow_id, current_log_str, prescription_log_entry)

    if not dry_run:
        # Write workflow artifact to disk first
        artifact_path = _write_workflow_artifact(
            uow_id=uow_id,
            instructions=instructions,
            prescribed_skills=prescribed_skills,
            artifact_dir=artifact_dir,
            executor_type=selected_executor_type,
        )

        # Audit-before-transition: write agenda_update audit entry BEFORE updating
        # steward_agenda in the DB. Only on re-entry (cycles > 0) — the initial
        # agenda_update is written in the cycles == 0 initialization block above.
        if cycles > 0:
            registry.append_audit_log(uow_id, {
                "event": "agenda_update",
                "actor": _ACTOR_STEWARD,
                "uow_id": uow_id,
                "steward_cycles": cycles,
                "agenda_snapshot": updated_agenda,
                "timestamp": _now_iso(),
            })

        # Write all steward fields BEFORE status transition
        _write_steward_fields(
            registry, uow_id,
            steward_agenda=json.dumps(updated_agenda),
            steward_log=current_log_str,
            workflow_artifact=artifact_path,
            prescribed_skills=json.dumps(prescribed_skills),
            route_reason=route_reason,
            steward_cycles=new_cycles,
        )

        # Write prescription audit entry (before status transition)
        registry.append_audit_log(uow_id, {
            "event": "steward_prescription",
            "actor": _ACTOR_STEWARD,
            "uow_id": uow_id,
            "steward_cycles": new_cycles,
            "workflow_primitive": selected_executor_type,
            "prescribed_skills": prescribed_skills,
            "prescription_source": "llm" if _llm_path_taken[0] else "deterministic",
            "instructions_preview": instructions[:80],
            "prescription_path": prescription_path,
            "timestamp": _now_iso(),
        })

        # Transition status to ready-for-executor
        registry.transition(uow_id, _STATUS_READY_FOR_EXECUTOR, _STATUS_DIAGNOSING)

        # Issue #648 Part A — collapse the polling hop.
        # When inline_executor is provided, invoke the Executor immediately after
        # the READY_FOR_EXECUTOR transition rather than waiting for the next
        # heartbeat cycle (which would add 0–3 min of unnecessary latency).
        # The Executor's optimistic lock (step 2 in _claim) guards against
        # double-execution if a concurrent heartbeat fires between transition
        # and inline call — ClaimRejected is logged and re-raised, which the
        # caller's exception handler surfaces.
        if inline_executor is not None:
            try:
                inline_executor(uow_id)
                log.info(
                    "Steward: inline executor dispatch complete for UoW %s",
                    uow_id,
                )
            except Exception as exc:
                log.warning(
                    "Steward: inline executor dispatch failed for UoW %s — %s: %s. "
                    "UoW remains in ready-for-executor; heartbeat will retry.",
                    uow_id, type(exc).__name__, exc,
                )

    # Early warning: fire on the first cycle where cumulative count crosses the threshold.
    # Uses cumulative count (lifetime_cycles from previous attempts + new_cycles this attempt)
    # so the warning fires correctly even after decide-retry resets steward_cycles to 0.
    # Fires regardless of dry_run so tests can capture the notification.
    #
    # First-crossing guard (>= current, < previous):
    #   current  = uow.lifetime_cycles + new_cycles   (post-prescription cumulative)
    #   previous = uow.lifetime_cycles + cycles        (pre-prescription cumulative, cycles == new_cycles - 1)
    #
    # This fires exactly once per UoW lifetime when the cumulative first reaches or jumps past
    # _EARLY_WARNING_CYCLES — preventing multi-fire on all subsequent prescription cycles.
    # The >= on current (not ==) retains the original S3-C fix: a non-sequential jump that
    # skips the exact threshold value is still caught, as long as previous was below threshold.
    #
    # Edge case: if lifetime_cycles itself is externally mutated to >= threshold before the next
    # steward cycle, previous will already be >= threshold and the notification will not fire.
    # This is acceptable: an external mutation of lifetime_cycles is out-of-band and the
    # bounded-notification guarantee takes priority over the external-mutation edge case.
    _cumulative_current = uow.lifetime_cycles + new_cycles
    _cumulative_previous = uow.lifetime_cycles + cycles  # cycles == new_cycles - 1
    if _cumulative_current >= _EARLY_WARNING_CYCLES and _cumulative_previous < _EARLY_WARNING_CYCLES:
        _notify_early = notify_dan_early_warning or _default_notify_dan_early_warning
        _notify_early(uow, return_reason, new_cycles)

    _append_cycle_trace(
        uow_id=uow_id,
        cycle_num=cycles,
        subagent_excerpt=_read_output_ref(uow.output_ref),
        return_reason=return_reason or "",
        next_action="prescribed",
        artifact_dir=artifact_dir,
    )
    return Prescribed(uow_id=uow_id, cycles=new_cycles)


# ---------------------------------------------------------------------------
# Main steward cycle (entry point for tests and heartbeat script)
# ---------------------------------------------------------------------------

def run_steward_cycle(
    registry=None,
    dry_run: bool = False,
    github_client: Callable[[int], IssueInfo] | None = None,
    artifact_dir: Path | None = None,
    notify_dan: Callable | None = None,
    notify_dan_early_warning: Callable | None = None,
    bootup_candidate_gate: bool | None = None,
    db_path: Path | None = None,
    llm_prescriber: Callable[..., LLMPrescription | None] | None = _llm_prescribe,
    inline_executor: Callable[[str], Any] | None = None,
) -> CycleResult:
    """
    Execute one full Steward heartbeat cycle.

    Processes all `ready-for-steward` UoWs through the diagnosis loop.

    Parameters
    ----------
    registry:
        Registry instance. If None, opens production DB.
    dry_run:
        If True, diagnose without writing artifacts or transitioning state.
    github_client:
        Callable(issue_number) → IssueInfo. Returns typed issue info.
        Defaults to the production gh CLI client.
    artifact_dir:
        Override for the artifact directory path. Used in tests.
    notify_dan:
        Callable(uow, condition, surface_log, return_reason) for surface-to-Dan
        notifications. Defaults to the production notification path.
    notify_dan_early_warning:
        Callable(uow, return_reason) for early-warning notifications when
        steward_cycles reaches _EARLY_WARNING_CYCLES (4). Defaults to the
        production notification path.
    bootup_candidate_gate:
        Override for BOOTUP_CANDIDATE_GATE. If None, uses the module constant.
    db_path:
        Path to registry DB. Only used if registry is None.
    llm_prescriber:
        Callable(uow, reentry_posture, completion_gap, issue_body) → LLMPrescription | None.
        Called during prescription to generate LLM-quality instructions.
        Inject None to bypass LLM (tests), or a stub to capture calls.
        Defaults to _llm_prescribe (production path).
    inline_executor:
        Optional callable(uow_id) invoked immediately after the READY_FOR_EXECUTOR
        transition, collapsing the 0–3 min polling hop (issue #648 Part A).
        Defaults to None — heartbeat remains the sole dispatch path.

    Returns
    -------
    CycleResult:
        Typed dataclass with fields: evaluated, prescribed, done, surfaced, skipped,
        race_skipped, considered_ids. Call .as_dict() for dict compatibility.
    """
    from src.orchestration.registry import Registry

    if registry is None:
        registry = Registry(db_path)  # db_path=None → Registry resolves canonical path

    _github_client = github_client or _default_github_client
    _gate = bootup_candidate_gate if bootup_candidate_gate is not None else BOOTUP_CANDIDATE_GATE

    # Step 0: Schema validation
    conn = registry._connect()
    try:
        validate_steward_schema(conn)
    finally:
        conn.close()

    # Ensure artifact directory exists
    if artifact_dir is not None:
        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
    else:
        default_artifact_dir = Path(os.path.expanduser(
            "~/lobster-workspace/orchestration/artifacts"
        ))
        default_artifact_dir.mkdir(parents=True, exist_ok=True)

    # Fetch all ready-for-steward UoWs
    try:
        uows = registry.query(status=_STATUS_READY_FOR_STEWARD)
    except AttributeError:
        # Fallback for pre-#327 registry (no query method yet)
        uows = registry.list(status=_STATUS_READY_FOR_STEWARD)

    log.debug("Steward cycle: %d ready-for-steward UoWs found", len(uows))

    # Queue depth snapshot used by the Burst pattern check in _check_dispatch_eligibility.
    # Captured once before the loop so all per-UoW checks see the same depth value.
    _queue_depth = len(uows)

    # Dynamic batch size: scale up when queue is deep so bursts drain faster.
    _effective_burst_batch_size = _dynamic_burst_batch_size(_queue_depth)
    if _effective_burst_batch_size != BURST_BATCH_SIZE:
        log.info(
            "Steward cycle: dynamic burst batch size=%d (queue_depth=%d, default=%d)",
            _effective_burst_batch_size, _queue_depth, BURST_BATCH_SIZE,
        )

    evaluated = 0
    prescribed = 0
    done = 0
    surfaced = 0
    skipped = 0
    race_skipped = 0
    wait_for_trace = 0
    throttle_count = 0
    shard_blocked = 0
    considered_ids = []

    # Shard-stream parallel dispatch gate — fetch once before the loop.
    # _executing_uows is updated within the loop as UoWs are prescribed
    # so that subsequent candidates in the same cycle see the updated
    # in-flight count (prevents over-dispatch within a single heartbeat).
    from src.orchestration.shard_dispatch import (
        check_shard_dispatch_eligibility,
        read_max_parallel,
        DispatchAllowed,
        DispatchBlocked,
    )
    try:
        _executing_uows = registry.list_executing()
    except Exception:
        log.warning("Steward: could not fetch executing UoWs for shard gate — defaulting to empty list")
        _executing_uows = []
    _max_parallel = read_max_parallel()
    log.debug(
        "Steward cycle: shard-stream gate: executing=%d max_parallel=%d",
        len(_executing_uows), _max_parallel,
    )

    for uow in uows:
        uow_id = uow.id
        source_issue_number = uow.source_issue_number
        considered_ids.append(uow_id)

        # Resolve the GitHub client for this UoW. When issue_url is present
        # (populated at proposal time since migration 0005), derive the repo
        # from the URL — no hardcoded repo slug. For pre-migration UoWs where
        # issue_url is NULL, fall back to _github_client (which uses the legacy
        # hardcoded repo). Pure resolution: no side effects.
        resolved_repo = _repo_from_issue_url(getattr(uow, "issue_url", None))
        def _resolve_issue_info(n: int) -> IssueInfo:
            if resolved_repo:
                return _fetch_github_issue(n, resolved_repo)
            return _github_client(n)

        # BOOTUP_CANDIDATE_GATE: skip if label present and gate is True
        if _gate and source_issue_number:
            issue_info = _resolve_issue_info(source_issue_number)
            labels = issue_info.labels
            if "bootup-candidate" in labels:
                log.debug(
                    "UoW %s (issue #%s) skipped: bootup-candidate gate is active",
                    uow_id, source_issue_number
                )
                skipped += 1
                continue
        else:
            issue_info = (
                _resolve_issue_info(source_issue_number)
                if source_issue_number
                else None
            )

        audit_entries = _fetch_audit_entries(registry, uow_id)

        # Backpressure gate (#617): skip re-prescription when the UoW was
        # returned from ready-for-executor via startup_sweep executor_orphan.
        # This happens when the executor queue is saturated — the startup sweep
        # transitions the UoW back to ready-for-steward, but prescribing again
        # consumes an LLM call without making progress.  Instead, log a
        # backpressure event and leave the UoW in ready-for-steward so it will
        # be re-evaluated on the next heartbeat after the queue drains.
        #
        # Note: _most_recent_classification scans for startup_sweep events only,
        # so executor_orphan return_reasons from execution_complete events (a
        # different scenario) are not intercepted here.
        #
        # Exception (#fix-backpressure-gate): if execution is currently enabled,
        # the executor_orphan classification is stale — it was written when the
        # executor was not running (e.g. execution_enabled=false at startup_sweep
        # time).  When execution is now active, the orphan hold is incorrect and
        # would permanently block the UoW.  Only apply the backpressure hold when
        # execution is disabled (the orphan condition is genuinely valid).
        _sweep_classification = _most_recent_classification(audit_entries)
        if _sweep_classification == "executor_orphan":
            try:
                from src.orchestration.dispatcher_handlers import is_execution_enabled
                _execution_currently_enabled = is_execution_enabled()
            except Exception:
                _execution_currently_enabled = False

            if _execution_currently_enabled:
                log.info(
                    "backpressure: uow_id=%s has stale executor_orphan classification "
                    "but execution is enabled — proceeding with re-prescription (cycle %d)",
                    uow_id,
                    uow.steward_cycles,
                )
            else:
                log.info(
                    "backpressure: uow_id=%s already in ready-for-executor, "
                    "skipping re-prescription (cycle %d)",
                    uow_id,
                    uow.steward_cycles,
                )
                if not dry_run:
                    registry.append_audit_log(uow_id, {
                        "event": "backpressure",
                        "actor": _ACTOR_STEWARD,
                        "uow_id": uow_id,
                        "steward_cycles": uow.steward_cycles,
                        "note": "executor_orphan: skipping re-prescription, execution disabled or queue saturated",
                        "timestamp": _now_iso(),
                    })
                skipped += 1
                continue

        # Dispatch eligibility gate (oracle/patterns.md): check for spiral,
        # dead-end, and burst patterns before committing to a prescription cycle.
        _eligibility = _check_dispatch_eligibility(uow, audit_entries, _queue_depth)
        if _eligibility == "throttle":
            if throttle_count < _effective_burst_batch_size:
                # Within the allowed burst batch — dispatch normally and count it.
                throttle_count += 1
                log.debug(
                    "dispatch_eligibility: uow_id=%s throttle allowed "
                    "(throttle_count=%d/%d)",
                    uow_id, throttle_count, _effective_burst_batch_size,
                )
            else:
                # Beyond _effective_burst_batch_size for this cycle — skip.
                log.info(
                    "dispatch_eligibility: uow_id=%s skipped — pattern=throttle "
                    "(throttle_count=%d >= burst_batch_size=%d)",
                    uow_id, throttle_count, _effective_burst_batch_size,
                )
                if not dry_run:
                    registry.append_audit_log(uow_id, {
                        "event": "dispatch_eligibility_skip",
                        "actor": _ACTOR_STEWARD,
                        "uow_id": uow_id,
                        "steward_cycles": uow.steward_cycles,
                        "eligibility": _eligibility,
                        "timestamp": _now_iso(),
                    })
                skipped += 1
                continue
        elif _eligibility != "dispatch":
            log.info(
                "dispatch_eligibility: uow_id=%s skipped — pattern=%s",
                uow_id, _eligibility,
            )
            if not dry_run:
                registry.append_audit_log(uow_id, {
                    "event": "dispatch_eligibility_skip",
                    "actor": _ACTOR_STEWARD,
                    "uow_id": uow_id,
                    "steward_cycles": uow.steward_cycles,
                    "eligibility": _eligibility,
                    "timestamp": _now_iso(),
                })
            skipped += 1
            continue

        # Shard-stream parallel dispatch gate.
        # Applied before prescription to prevent over-dispatch when multiple
        # UoWs are ready-for-steward in the same heartbeat cycle.
        # Done/Surfaced paths are also blocked when the gate fires — this is
        # acceptable: at most one extra heartbeat delay for completion
        # acknowledgment, which is far cheaper than a scope conflict.
        _shard_decision = check_shard_dispatch_eligibility(
            candidate_file_scope=getattr(uow, "file_scope", None),
            candidate_shard_id=getattr(uow, "shard_id", None),
            executing_uows=_executing_uows,
            max_parallel=_max_parallel,
        )
        if isinstance(_shard_decision, DispatchBlocked):
            log.info(
                "shard-stream: uow_id=%s blocked — %s",
                uow_id, _shard_decision.reason,
            )
            if not dry_run:
                registry.append_audit_log(uow_id, {
                    "event": "shard_dispatch_blocked",
                    "actor": _ACTOR_STEWARD,
                    "uow_id": uow_id,
                    "reason": _shard_decision.reason,
                    "executing_count": len(_executing_uows),
                    "max_parallel": _max_parallel,
                    "timestamp": _now_iso(),
                })
            shard_blocked += 1
            skipped += 1
            continue

        # Step 3 — Juice sensing at dispatch time.
        # Called after all gates pass (shard, eligibility, backpressure) so juice
        # is only computed for UoWs that will actually be dispatched. Juice is
        # computed from the already-fetched audit_entries to avoid a redundant
        # DB round-trip. The result is written back to the registry when the
        # score differs materially from the stored value (>JUICE_UPDATE_DELTA=0.05).
        # An audit log entry with dispatch_signal=juice is written when a
        # juice-priority UoW is dispatched.
        _juice_write_back_needed = False
        try:
            from src.orchestration.juice import JuiceSensor, JUICE_UPDATE_DELTA
            _juice_sensor = JuiceSensor()
            _juice_assessment = _juice_sensor.assess(uow, audit_entries, registry)
            _new_juice_score = _juice_assessment.score
            _current_juice_quality = getattr(uow, "juice_quality", None)

            # Determine if write-back is needed (score differs materially).
            if _juice_assessment.has_juice:
                _new_juice_quality = "juice"
                _new_juice_rationale = _juice_assessment.rationale
                _juice_write_back_needed = _current_juice_quality != "juice"
            else:
                _new_juice_quality = None
                _new_juice_rationale = None
                _juice_write_back_needed = _current_juice_quality == "juice"

            if _juice_write_back_needed and not dry_run:
                try:
                    registry.write_juice(uow_id, _new_juice_quality, _new_juice_rationale)
                    log.debug(
                        "juice: wrote juice_quality=%r for %s (score=%s)",
                        _new_juice_quality, uow_id, _new_juice_score,
                    )
                except Exception as _juice_write_exc:
                    # Non-fatal: juice write failure does not block dispatch.
                    log.warning(
                        "juice: write_juice failed for %s — %s: %s (continuing dispatch)",
                        uow_id, type(_juice_write_exc).__name__, _juice_write_exc,
                    )

            # Log audit entry when dispatching a juice-priority UoW.
            if _juice_assessment.has_juice and not dry_run:
                registry.append_audit_log(uow_id, {
                    "event": "dispatch_signal",
                    "actor": _ACTOR_STEWARD,
                    "uow_id": uow_id,
                    "dispatch_signal": "juice",
                    "juice_score": _new_juice_score,
                    "juice_rationale": _juice_assessment.rationale,
                    "steward_cycles": uow.steward_cycles,
                    "timestamp": _now_iso(),
                })
                log.info(
                    "juice: dispatching juice-priority UoW %s (score=%.3f, rationale=%r)",
                    uow_id, _new_juice_score or 0.0, _juice_assessment.rationale,
                )

        except Exception as _juice_exc:
            # Non-fatal: juice sensing failure does not block dispatch.
            log.warning(
                "juice: sensing failed for %s — %s: %s (continuing dispatch without juice signal)",
                uow_id, type(_juice_exc).__name__, _juice_exc,
            )

        evaluated += 1
        try:
            result = _process_uow(
                uow=uow,
                registry=registry,
                audit_entries=audit_entries,
                issue_info=issue_info,
                dry_run=dry_run,
                artifact_dir=artifact_dir,
                notify_dan=notify_dan,
                notify_dan_early_warning=notify_dan_early_warning,
                llm_prescriber=llm_prescriber,
                inline_executor=inline_executor,
            )
        except Exception:
            log.exception("Steward: unhandled error processing UoW %s — skipping", uow_id)
            skipped += 1
            continue

        match result:
            case Prescribed():
                prescribed += 1
                # Update in-flight list so subsequent UoWs in this cycle see
                # the updated count. Append the just-prescribed UoW to
                # _executing_uows so that the shard gate correctly blocks
                # conflicting candidates dispatched in the same heartbeat.
                # We append the UoW object directly — it carries file_scope
                # and shard_id from the registry row, which is what the gate needs.
                _executing_uows = list(_executing_uows) + [uow]
            case Done():
                done += 1
            case Surfaced():
                surfaced += 1
            case RaceSkipped():
                race_skipped += 1
            case WaitForTrace():
                # One-cycle temporal gate fired — UoW stays in diagnosing;
                # startup_sweep resets it to ready-for-steward next heartbeat.
                wait_for_trace += 1
            case _:
                skipped += 1

    return CycleResult(
        evaluated=evaluated,
        prescribed=prescribed,
        done=done,
        surfaced=surfaced,
        skipped=skipped,
        race_skipped=race_skipped,
        wait_for_trace=wait_for_trace,
        shard_blocked=shard_blocked,
        considered_ids=tuple(considered_ids),
    )
