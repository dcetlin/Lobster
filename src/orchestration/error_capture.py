"""
Error capture and classification for WOS subprocess invocations.

This module provides error visibility when subprocesses fail, particularly
for LLM prescription dispatch and agent execution. It captures stderr,
classifies errors, and detects repeated failures that indicate manual
intervention is needed.

Design patterns:
- Pure functions for error classification and context building
- Immutable error records with full subprocess state
- Functional composition for error handling chains
- Side effects (logging, notifications) isolated at boundaries
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, asdict
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("wos.error_capture")


class ErrorType(StrEnum):
    """Classification of subprocess errors."""
    TIMEOUT = "timeout"
    NONZERO_EXIT = "nonzero_exit"
    STDERR_OUTPUT = "stderr_output"
    MISSING_BINARY = "missing_binary"
    BUILD_FAILURE = "build_failure"
    UV_ERROR = "uv_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SubprocessError:
    """Immutable record of a subprocess failure with context."""
    component: str          # e.g. "executor", "steward", "prescription"
    uow_id: str            # Unit of Work ID that triggered this
    error_type: ErrorType
    exit_code: int | None
    stderr: str            # Captured stderr (may be empty)
    stdout: str            # Captured stdout (may be empty)
    command: list[str]     # Command that was run
    timestamp: float       # Unix timestamp when error occurred

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        d = asdict(self)
        d["error_type"] = str(self.error_type)
        return d

    def summary(self) -> str:
        """Return a brief error summary for logging."""
        return f"{self.component}({self.uow_id}): {self.error_type} — exit={self.exit_code}"

    def detail(self) -> str:
        """Return detailed error info including stderr excerpt."""
        stderr_preview = self.stderr[:500].strip() if self.stderr else "<no stderr>"
        return f"{self.summary()}\nstderr: {stderr_preview}"


@dataclass(frozen=True)
class ErrorClassification:
    """Result of classifying an error."""
    error: SubprocessError
    is_fatal: bool          # True if error suggests manual intervention needed
    classification: str     # Human-readable classification
    recovery_hint: str      # Suggested action


def classify_error(err: SubprocessError) -> ErrorClassification:
    """
    Classify a subprocess error to determine if it's fatal.

    Fatal errors are those that typically need manual intervention:
    - Build failures (uv errors, compilation failures)
    - Missing binaries or dependencies
    - Timeouts after retries

    Returns an ErrorClassification with guidance.

    Design: Uses actual exception frames and exit codes as primary signals,
    not generic "error" keywords that appear in benign output.
    """
    stderr = err.stderr.lower()

    # Primary signal: exit code (non-zero indicates failure)
    is_nonzero_exit = err.exit_code is not None and err.exit_code != 0

    # Detect actual Python exceptions by frame markers
    has_traceback = "traceback" in stderr or "error:" in stderr
    is_exception = has_traceback or "exception" in stderr

    # Detect build/dependency errors — only if actual exception frames or specific errors
    is_build_error = (
        is_exception and (
            "error" in stderr or
            "failed" in stderr or
            "permission denied" in stderr
        )
    )

    is_uv_error = (
        is_exception and
        ("uv" in err.command[0] or "uv run" in " ".join(err.command))
    )

    is_missing_binary = (
        err.error_type == ErrorType.MISSING_BINARY or
        "not found" in stderr or
        "no such file" in stderr
    )

    # Determine if fatal
    is_fatal = is_build_error or is_uv_error or is_missing_binary or is_nonzero_exit
    if err.error_type == ErrorType.TIMEOUT:
        # Timeouts are usually transient, but repeated ones are fatal
        is_fatal = False

    classification = ""
    recovery_hint = ""

    if is_uv_error:
        classification = "uv/dependency error"
        recovery_hint = "Check dependency installation; may require manual package resolution"
    elif is_build_error:
        classification = "build failure"
        recovery_hint = "Review build logs; may require code changes or environment fixes"
    elif is_missing_binary:
        classification = "missing binary/dependency"
        recovery_hint = "Verify required tools are installed and on PATH"
    elif err.error_type == ErrorType.TIMEOUT:
        classification = "timeout"
        recovery_hint = "Subprocess exceeded time limit; may indicate hung process or resource contention"
    elif is_nonzero_exit:
        classification = "subprocess exit failure"
        recovery_hint = "Subprocess exited with non-zero code; review logs and command arguments"
    else:
        classification = "subprocess error"
        recovery_hint = "Review subprocess output and logs"

    return ErrorClassification(
        error=err,
        is_fatal=is_fatal,
        classification=classification,
        recovery_hint=recovery_hint,
    )


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    """A point-in-time error record for tracking recurring failures."""
    timestamp: float
    component: str
    uow_id: str
    error_type: str
    is_fatal: bool


# Simple in-memory error tracking for detecting repeated failures
# Key: (component, uow_id, error_type)
# Value: list of timestamps (oldest first)
_ERROR_HISTORY: dict[tuple[str, str, str], list[float]] = {}


def _prune_old_errors(window_seconds: int = 300) -> None:
    """Remove error records older than window_seconds (internal, pure state management)."""
    now = time.time()
    cutoff = now - window_seconds
    for key in list(_ERROR_HISTORY.keys()):
        timestamps = _ERROR_HISTORY[key]
        # Keep only recent errors
        recent = [ts for ts in timestamps if ts >= cutoff]
        if recent:
            _ERROR_HISTORY[key] = recent
        else:
            del _ERROR_HISTORY[key]


def has_repeated_error(
    component: str,
    uow_id: str,
    error_type: str,
    threshold: int = 3,
    window_seconds: int = 300,
) -> bool:
    """
    Check if the same error has occurred threshold+ times in the last window_seconds.

    Pure function on observation: idempotent, records current error timestamp.
    """
    _prune_old_errors(window_seconds)

    key = (component, uow_id, error_type)
    timestamps = _ERROR_HISTORY.get(key, [])
    now = time.time()

    # Add current error
    timestamps.append(now)
    _ERROR_HISTORY[key] = timestamps

    # Check threshold
    return len(timestamps) >= threshold


def log_subprocess_error(
    err: SubprocessError,
    include_stderr: bool = True,
) -> None:
    """
    Log a subprocess error with context.

    Logs at WARNING level for classified (non-fatal) errors and ERROR level
    for fatal ones.
    """
    classification = classify_error(err)

    msg = f"{err.component}({err.uow_id}): {classification.classification}"
    if include_stderr and err.stderr:
        stderr_preview = err.stderr[:300].strip()
        msg += f"\nstderr: {stderr_preview}"
    msg += f"\n→ {classification.recovery_hint}"

    if classification.is_fatal:
        log.error(msg)
    else:
        log.warning(msg)


def capture_subprocess_error(
    component: str,
    uow_id: str,
    command: list[str],
    returncode: int | None,
    stderr: str,
    stdout: str = "",
) -> SubprocessError:
    """
    Capture a subprocess error with full context.

    Classifies the error type and returns an immutable error record.
    """
    error_type = _classify_error_type(returncode, stderr, command)

    return SubprocessError(
        component=component,
        uow_id=uow_id,
        error_type=error_type,
        exit_code=returncode,
        stderr=stderr,
        stdout=stdout,
        command=command,
        timestamp=time.time(),
    )


def _classify_error_type(returncode: int | None, stderr: str, command: list[str] | None = None) -> ErrorType:
    """Classify error based on exit code, stderr content, and command."""
    if returncode is None:
        return ErrorType.TIMEOUT

    stderr_lower = stderr.lower()

    # Check if uv command in stderr or if uv was the command
    is_uv_related = False
    if command:
        is_uv_related = any("uv" in arg for arg in command)
    is_uv_related = is_uv_related or ("uv" in stderr_lower and ("error" in stderr_lower or "failed" in stderr_lower))

    if is_uv_related:
        return ErrorType.UV_ERROR

    if any(word in stderr_lower for word in ["build", "compile", "failed to build"]):
        return ErrorType.BUILD_FAILURE

    if returncode == 127 or "not found" in stderr_lower:
        return ErrorType.MISSING_BINARY

    if returncode != 0:
        return ErrorType.NONZERO_EXIT

    if stderr:
        return ErrorType.STDERR_OUTPUT

    return ErrorType.UNKNOWN


def run_subprocess_with_error_capture(
    component: str,
    uow_id: str,
    command: list[str],
    timeout_seconds: int,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, SubprocessError | None]:
    """
    Run a subprocess and capture errors if they occur.

    Returns (proc, error) where error is None on success.

    Pure function with respect to the caller:
    - Always captures stderr even on success
    - Never raises subprocess.CalledProcessError
    - Returns error object for caller to handle

    Args:
        env: Optional environment dict for the subprocess. When provided,
             replaces the inherited environment entirely. Callers that need
             to augment (not replace) the current env should pass a merged
             dict: {**os.environ, "KEY": "value"}. When None (default),
             the subprocess inherits the parent environment unchanged.
    """
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,  # Never auto-raise; we handle errors explicitly
            env=env,
        )

        # Check for error and capture it
        if proc.returncode != 0:
            error = capture_subprocess_error(
                component=component,
                uow_id=uow_id,
                command=command,
                returncode=proc.returncode,
                stderr=proc.stderr,
                stdout=proc.stdout,
            )
            if check:
                log_subprocess_error(error)
            return proc, error

        return proc, None

    except subprocess.TimeoutExpired as e:
        error = SubprocessError(
            component=component,
            uow_id=uow_id,
            error_type=ErrorType.TIMEOUT,
            exit_code=None,
            stderr=f"subprocess timed out after {timeout_seconds}s",
            stdout="",
            command=command,
            timestamp=time.time(),
        )
        if check:
            log_subprocess_error(error)
        return None, error  # type: ignore

    except FileNotFoundError:
        error = SubprocessError(
            component=component,
            uow_id=uow_id,
            error_type=ErrorType.MISSING_BINARY,
            exit_code=None,
            stderr=f"binary not found: {command[0]}",
            stdout="",
            command=command,
            timestamp=time.time(),
        )
        if check:
            log_subprocess_error(error)
        return None, error  # type: ignore

    except Exception as e:
        error = SubprocessError(
            component=component,
            uow_id=uow_id,
            error_type=ErrorType.UNKNOWN,
            exit_code=None,
            stderr=str(e),
            stdout="",
            command=command,
            timestamp=time.time(),
        )
        if check:
            log_subprocess_error(error)
        return None, error  # type: ignore
