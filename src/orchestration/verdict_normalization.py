"""
verdict_normalization.py — Haiku normalization and verdict accumulator upsert.

§3-I Adaptive Steward (wos-evolution-spec.md), Fork 1 — Option C (LOCKED).

At UoW closure, this module:
1. Normalizes the raw hypothesis string via a Haiku call (deduplicates phrasing,
   enforces consistent vocabulary).
2. Upserts the scored, normalized hypothesis into `verdict_accumulator`.
3. Logs both the raw and normalized form to `normalization_log` for observability.

Design principles:
- Non-fatal: all methods catch and log exceptions so UoW closure is never blocked.
- Pure normalization function: `normalize_hypothesis_text` is a pure transformation
  (modulo network I/O) — it does not write to any DB.
- Idempotency: verdict_accumulator upsert uses INSERT OR REPLACE with
  n_successes/n_failures/n_partial increments so duplicate calls are safe.
- LLM dispatch: uses `claude -p` subprocess via _build_claude_env() from steward,
  matching the WOS codebase standard (CLAUDE_CODE_OAUTH_TOKEN, no ANTHROPIC_API_KEY).

Model: claude-haiku-4-5 (overridable via LOBSTER_HAIKU_MODEL env var).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

log = logging.getLogger("verdict_normalization")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Haiku model ID for normalization calls (spec §3-I, Fork 1 Option C).
#: Overridable via LOBSTER_HAIKU_MODEL env var.
HAIKU_MODEL: str = os.environ.get("LOBSTER_HAIKU_MODEL", "claude-haiku-4-5")

#: claude binary name — must be on PATH (ensured by _build_claude_env).
_CLAUDE_BIN: str = "claude"

#: Timeout for the claude -p normalization subprocess in seconds.
_NORMALIZATION_TIMEOUT_SECS: int = 30

#: Outcome labels accepted by the accumulator upsert.
VerdictOutcome = Literal["pass", "fail", "partial"]

#: Normalization system prompt sent to claude -p.
_NORMALIZATION_SYSTEM_PROMPT: str = (
    "You are a hypothesis normalization assistant for a software engineering workflow system. "
    "Given a raw hypothesis string describing what a system believes is the root cause of a problem "
    "and how it plans to fix it, produce a canonical normalized form that:\n"
    "1. Removes implementation-specific details that would prevent matching across similar hypotheses.\n"
    "2. Uses consistent vocabulary (e.g. 'configuration issue' not 'config problem' or 'config bug').\n"
    "3. Preserves the core causal claim and proposed fix.\n"
    "4. Is at most 140 characters.\n"
    "5. Is in present tense, third person (e.g. 'Missing X causes Y; fix by adding Z').\n\n"
    "Respond with ONLY the normalized hypothesis string — no explanation, no quotes, no punctuation "
    "unless part of the hypothesis itself."
)

#: Maximum characters for the normalized hypothesis (mirrors PrescriptionObject spec).
_HYPOTHESIS_MAX_CHARS: int = 140

#: Default path to the WOS metrics DB (relative to LOBSTER_WORKSPACE).
_METRICS_DB_SUBPATH: str = "orchestration/wos-metrics.db"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _metrics_db_path() -> Path:
    """Return the absolute path to wos-metrics.db, honouring LOBSTER_WORKSPACE."""
    workspace = os.environ.get(
        "LOBSTER_WORKSPACE",
        str(Path.home() / "lobster-workspace"),
    )
    return Path(workspace) / _METRICS_DB_SUBPATH


def _connect_metrics_db() -> sqlite3.Connection:
    db_path = _metrics_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create normalization_log and verdict_accumulator tables if absent."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS normalization_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            uow_id     TEXT    NOT NULL,
            raw        TEXT    NOT NULL,
            normalized TEXT    NOT NULL,
            model      TEXT    NOT NULL,
            ts         TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS verdict_accumulator (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            register             TEXT    NOT NULL,
            diagnosis_hypothesis TEXT    NOT NULL,
            n_successes          INTEGER NOT NULL DEFAULT 0,
            n_failures           INTEGER NOT NULL DEFAULT 0,
            n_partial            INTEGER NOT NULL DEFAULT 0,
            last_updated         TEXT    NOT NULL,
            UNIQUE(register, diagnosis_hypothesis)
        );

        CREATE TABLE IF NOT EXISTS prescription_hypothesis_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            uow_id       TEXT    NOT NULL,
            hypothesis   TEXT    NOT NULL,
            register     TEXT    NOT NULL,
            generated_at TEXT    NOT NULL,
            outcome      TEXT    NULL,
            scored_at    TEXT    NULL
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Normalization (Haiku call)
# ---------------------------------------------------------------------------

def normalize_hypothesis_text(
    raw: str,
    *,
    model: str = HAIKU_MODEL,
    anthropic_client=None,  # kept for test injection; preferred path is claude -p subprocess
) -> str:
    """
    Normalize *raw* hypothesis string via a claude -p subprocess call.

    Uses _build_claude_env() from steward to ensure CLAUDE_CODE_OAUTH_TOKEN is
    present, matching the standard WOS codebase auth pattern. No ANTHROPIC_API_KEY
    is required.

    Args:
        raw: The raw hypothesis string (will be truncated to 140 chars if the
             model returns something longer).
        model: The model ID to use. Defaults to HAIKU_MODEL (overridable via
               LOBSTER_HAIKU_MODEL env var).
        anthropic_client: Deprecated. When provided, falls back to the legacy
            Anthropic SDK path for test injection. In production this should be
            None (uses claude -p subprocess).

    Returns:
        Normalized hypothesis string (≤140 chars).

    Raises:
        RuntimeError: If the claude -p call fails or returns an empty response.
    """
    # Legacy test-injection path: if an anthropic_client mock is explicitly
    # provided (e.g. in unit tests), use the SDK call directly.
    if anthropic_client is not None:
        response = anthropic_client.messages.create(
            model=model,
            max_tokens=200,
            system=[
                {
                    "type": "text",
                    "text": _NORMALIZATION_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"Normalize this hypothesis:\n\n{raw}",
                }
            ],
        )
        if not response.content:
            raise RuntimeError(
                f"Haiku normalization returned empty content for hypothesis: {raw!r}"
            )
        text_blocks = [
            block.text for block in response.content
            if hasattr(block, "text")
        ]
        if not text_blocks:
            raise RuntimeError(
                f"Haiku normalization returned no text content for hypothesis: {raw!r}"
            )
        normalized = text_blocks[0].strip()[:_HYPOTHESIS_MAX_CHARS]
        if not normalized:
            raise RuntimeError(
                f"Haiku normalization returned empty string for hypothesis: {raw!r}"
            )
        return normalized

    # Production path: claude -p subprocess using CLAUDE_CODE_OAUTH_TOKEN.
    # Importing here to avoid a circular import (steward imports nothing from
    # verdict_normalization, but verdict_normalization only needs _build_claude_env).
    from orchestration.steward import _build_claude_env

    prompt = f"{_NORMALIZATION_SYSTEM_PROMPT}\n\nNormalize this hypothesis:\n\n{raw}"
    command = [_CLAUDE_BIN, "-p", prompt, "--output-format", "text", "--model", model]
    claude_env = _build_claude_env()

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_NORMALIZATION_TIMEOUT_SECS,
            check=False,
            env=claude_env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"claude -p normalization timed out after {_NORMALIZATION_TIMEOUT_SECS}s "
            f"for hypothesis: {raw!r}"
        )

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p normalization exited {proc.returncode} for hypothesis: {raw!r} "
            f"— stderr: {proc.stderr.strip()[:200]!r}"
        )

    normalized = (proc.stdout or "").strip()[:_HYPOTHESIS_MAX_CHARS]
    if not normalized:
        raise RuntimeError(
            f"claude -p normalization returned empty output for hypothesis: {raw!r}"
        )

    return normalized


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def _log_normalization(
    conn: sqlite3.Connection,
    uow_id: str,
    raw: str,
    normalized: str,
    model: str,
) -> None:
    """
    Write one row to normalization_log.

    Pure side effect — no return value.
    """
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO normalization_log (uow_id, raw, normalized, model, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (uow_id, raw, normalized, model, ts),
    )


def _upsert_verdict(
    conn: sqlite3.Connection,
    register: str,
    hypothesis_normalized: str,
    outcome: VerdictOutcome,
) -> None:
    """
    Insert or update a verdict_accumulator row.

    On INSERT: initialize the matching outcome counter to 1.
    On conflict (same register + hypothesis): increment the matching counter.

    Uses SQLite's INSERT OR IGNORE + UPDATE pattern to avoid the
    INSERT OR REPLACE anti-pattern (which resets non-matched counters to 0).

    Pure side effect — no return value.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Ensure the row exists first.
    conn.execute(
        """
        INSERT OR IGNORE INTO verdict_accumulator
            (register, diagnosis_hypothesis, n_successes, n_failures, n_partial, last_updated)
        VALUES (?, ?, 0, 0, 0, ?)
        """,
        (register, hypothesis_normalized, now),
    )

    # Increment the appropriate counter.
    if outcome == "pass":
        conn.execute(
            "UPDATE verdict_accumulator SET n_successes = n_successes + 1, last_updated = ? "
            "WHERE register = ? AND diagnosis_hypothesis = ?",
            (now, register, hypothesis_normalized),
        )
    elif outcome == "fail":
        conn.execute(
            "UPDATE verdict_accumulator SET n_failures = n_failures + 1, last_updated = ? "
            "WHERE register = ? AND diagnosis_hypothesis = ?",
            (now, register, hypothesis_normalized),
        )
    else:  # "partial"
        conn.execute(
            "UPDATE verdict_accumulator SET n_partial = n_partial + 1, last_updated = ? "
            "WHERE register = ? AND diagnosis_hypothesis = ?",
            (now, register, hypothesis_normalized),
        )


def _mark_hypothesis_scored(
    conn: sqlite3.Connection,
    uow_id: str,
    outcome: VerdictOutcome,
) -> None:
    """
    Write outcome + scored_at to prescription_hypothesis_log for the given uow_id.

    Idempotency guard: only updates rows where scored_at IS NULL.
    Pure side effect — no return value.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE prescription_hypothesis_log "
        "SET outcome = ?, scored_at = ? "
        "WHERE uow_id = ? AND scored_at IS NULL",
        (outcome, now, uow_id),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def score_and_normalize_verdict(
    uow_id: str,
    register: str,
    hypothesis_raw: str,
    outcome: VerdictOutcome,
    *,
    model: str = HAIKU_MODEL,
    anthropic_client=None,  # kept for test injection only; production uses claude -p subprocess
    db_path: Path | None = None,
) -> None:
    """
    Normalize *hypothesis_raw* via claude -p subprocess and upsert the scored verdict.

    Called at UoW closure by `maybe_complete_wos_uow` and the failure path.
    Non-fatal: all exceptions are caught and logged at WARNING level so that
    UoW closure is never blocked.

    Steps:
    1. Normalize the raw hypothesis via `normalize_hypothesis_text` (claude -p subprocess).
    2. Write one row to `normalization_log` (raw + normalized + model + ts).
    3. Upsert into `verdict_accumulator` (increment appropriate counter).
    4. Mark the `prescription_hypothesis_log` row as scored (idempotent).

    Args:
        uow_id: The UoW identifier.
        register: The UoW's attentional register (written to verdict_accumulator).
        hypothesis_raw: The raw hypothesis string (from UoW summary or
            prescription_hypothesis_log).
        outcome: One of "pass", "fail", "partial".
        model: Haiku model ID (injectable for tests).
        anthropic_client: Pre-constructed Anthropic client (injectable for tests).
        db_path: Override the metrics DB path (injectable for tests).
    """
    if not hypothesis_raw or not hypothesis_raw.strip():
        log.warning(
            "score_and_normalize_verdict: empty hypothesis for UoW %r — skipping",
            uow_id,
        )
        return

    try:
        normalized = normalize_hypothesis_text(
            hypothesis_raw.strip(),
            model=model,
            anthropic_client=anthropic_client,
        )
    except Exception as exc:
        log.warning(
            "score_and_normalize_verdict: Haiku normalization failed for UoW %r — %s: %s",
            uow_id,
            type(exc).__name__,
            exc,
        )
        return

    effective_db_path = db_path or _metrics_db_path()
    try:
        conn = sqlite3.connect(str(effective_db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            _ensure_schema(conn)
            _log_normalization(conn, uow_id, hypothesis_raw.strip(), normalized, model)
            _upsert_verdict(conn, register, normalized, outcome)
            _mark_hypothesis_scored(conn, uow_id, outcome)
            conn.commit()
            log.info(
                "score_and_normalize_verdict: scored UoW %r (outcome=%r, "
                "register=%r, normalized=%r)",
                uow_id,
                outcome,
                register,
                normalized,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        log.warning(
            "score_and_normalize_verdict: DB write failed for UoW %r — %s: %s",
            uow_id,
            type(exc).__name__,
            exc,
        )


def log_prescription_hypothesis(
    uow_id: str,
    hypothesis: str,
    register: str,
    generated_at: str,
    *,
    db_path: Path | None = None,
) -> None:
    """
    Write a row to prescription_hypothesis_log at prescription time.

    Called by the Steward when it generates a new prescription so the
    verdict scoring hook can look up the hypothesis at UoW closure.

    Non-fatal: all exceptions are caught and logged at WARNING level.

    Args:
        uow_id: The UoW identifier.
        hypothesis: The raw hypothesis string (diagnosis_hypothesis from
            PrescriptionObject, or derived from uow.summary).
        register: The UoW's attentional register.
        generated_at: ISO timestamp of prescription generation.
        db_path: Override the metrics DB path (injectable for tests).
    """
    effective_db_path = db_path or _metrics_db_path()
    try:
        conn = sqlite3.connect(str(effective_db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO prescription_hypothesis_log "
                "(uow_id, hypothesis, register, generated_at) "
                "VALUES (?, ?, ?, ?)",
                (uow_id, hypothesis, register, generated_at),
            )
            conn.commit()
            log.debug(
                "log_prescription_hypothesis: logged hypothesis for UoW %r (register=%r)",
                uow_id,
                register,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        log.warning(
            "log_prescription_hypothesis: DB write failed for UoW %r — %s: %s",
            uow_id,
            type(exc).__name__,
            exc,
        )


def get_hypothesis_for_uow(
    uow_id: str,
    *,
    db_path: Path | None = None,
) -> str | None:
    """
    Look up the raw hypothesis for a UoW from prescription_hypothesis_log.

    Returns the hypothesis from the most recent unscored row for *uow_id*,
    or None if no such row exists.

    Used by the verdict scoring hook to find the hypothesis at UoW closure
    when it was not passed explicitly.

    Non-fatal: returns None on any DB error.
    """
    effective_db_path = db_path or _metrics_db_path()
    if not Path(effective_db_path).exists():
        return None
    try:
        conn = sqlite3.connect(str(effective_db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT hypothesis FROM prescription_hypothesis_log "
                "WHERE uow_id = ? AND scored_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (uow_id,),
            ).fetchone()
            return row["hypothesis"] if row else None
        finally:
            conn.close()
    except Exception as exc:
        log.warning(
            "get_hypothesis_for_uow: DB read failed for UoW %r — %s: %s",
            uow_id,
            type(exc).__name__,
            exc,
        )
        return None
