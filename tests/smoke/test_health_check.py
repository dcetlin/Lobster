"""
Smoke tests — Group D: scripts/health-check-v3.sh

These tests verify the most critical correctness properties of the health check
script without requiring systemd, tmux, or Telegram credentials.  They work by
running isolated bash snippets extracted from the script — the same technique
the existing bash test suite uses — so they exercise real production code rather
than mocks.

Why these tests exist:

D1. A syntax error in health-check-v3.sh silently breaks monitoring: the cron
    job exits 0 (bash -n is non-zero but cron discards stderr) and the system
    goes unwatched. Catching syntax errors here means they surface before deploy.

D2. Compaction suppression is the most safety-critical logic in the health check.
    When Claude Code compacts its context, tool calls pause for 1–3 minutes.
    During this window, real user messages can age past STALE_THRESHOLD_SECONDS
    and trigger a false-positive restart. If is_compaction_recent() fails to
    return "true" for a fresh compacted_at, the health check will restart the
    dispatcher mid-compaction — corrupting state and losing messages.

D3. Compaction suppression must have a TTL: if the system crashes during a
    compaction window, the suppression must expire.  If is_compaction_recent()
    incorrectly returns "true" for a stale compacted_at (older than
    COMPACTION_SUPPRESS_SECONDS), monitoring is silently disabled for extended
    periods and genuine stuck-dispatcher events go undetected.

D4. The maintenance flag (written by `lobster stop`) must cause an immediate
    clean exit with no checks run and no restart attempted.  If this flag is
    ignored, manual maintenance windows trigger spurious health-check restarts
    that fight the operator.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Absolute path to the health check script under test.
HEALTH_SCRIPT = Path(__file__).parents[2] / "scripts" / "health-check-v3.sh"

# How long the script suppresses stale-inbox checks after a compaction.
# Must match COMPACTION_SUPPRESS_SECONDS in health-check-v3.sh (420).
COMPACTION_SUPPRESS_SECONDS = 420


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_bash_fragment(
    fragment: str,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """
    Run a bash fragment that sources helper functions from the health check
    script and then executes `fragment`.  Returns the CompletedProcess so
    callers can inspect returncode, stdout, and stderr.
    """
    merged_env = {**os.environ, **(env or {})}
    script = f"""
#!/bin/bash
set -o pipefail

# Source the is_compaction_recent helper and its dependencies.
# We extract only the functions we need so we don't need systemd/tmux/curl.
{fragment}
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=merged_env,
    )


def _extract_function(name: str) -> str:
    """
    Extract a single top-level bash function from the health check script.
    Returns the function source text (including the closing brace).

    Raises AssertionError if the function is not found.
    """
    text = HEALTH_SCRIPT.read_text()
    lines = text.splitlines(keepends=True)

    # Find the line that starts the function definition.
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{name}()"):
            start = i
            break

    assert start is not None, (
        f"Function '{name}()' not found in {HEALTH_SCRIPT}. "
        "Was it renamed or removed?"
    )

    # Collect lines until we hit the closing brace at column 0.
    result = []
    for line in lines[start:]:
        result.append(line)
        if line.rstrip() == "}":
            break

    return "".join(result)


def _is_compaction_recent_script(state_file: Path, tmp_path: Path) -> str:
    """
    Build a self-contained bash script fragment that:
    - Sets the minimal variables is_compaction_recent() reads
    - Injects a stub log_info() so logging doesn't fail
    - Calls is_compaction_recent()
    - Exits with the function's return code

    We source only the functions we need to keep tests isolated.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "health-check.log"

    fn_body = _extract_function("is_compaction_recent")

    return f"""
#!/bin/bash
LOBSTER_STATE_FILE="{state_file}"
COMPACTION_SUPPRESS_SECONDS={COMPACTION_SUPPRESS_SECONDS}
LOG_FILE="{log_file}"
mkdir -p "$(dirname "$LOG_FILE")"
log()      {{ echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }}
log_info() {{ log "INFO" "$1"; }}

{fn_body}

is_compaction_recent
exit $?
"""


# ---------------------------------------------------------------------------
# D1 — syntax check
# ---------------------------------------------------------------------------


def test_health_check_passes_syntax_check() -> None:
    """
    D1: health-check-v3.sh must pass bash -n (no syntax errors).

    Failure mode: a syntax error causes the cron job to silently exit non-zero.
    Bash does not write to cron's email by default, so a syntactically broken
    health check goes completely unnoticed — the system is unwatched.
    """
    assert HEALTH_SCRIPT.exists(), (
        f"health-check-v3.sh not found at {HEALTH_SCRIPT}. "
        "Has the script been moved or renamed?"
    )

    result = subprocess.run(
        ["bash", "-n", str(HEALTH_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bash -n reported syntax errors in {HEALTH_SCRIPT.name}:\n"
        f"{result.stderr}"
    )


# ---------------------------------------------------------------------------
# D2 — compaction suppression fires for a fresh compacted_at
# ---------------------------------------------------------------------------


def test_compaction_suppression_fires_when_compaction_recent(tmp_path: Path) -> None:
    """
    D2: is_compaction_recent() must return true (exit 0) when compacted_at in
    lobster-state.json is within the last COMPACTION_SUPPRESS_SECONDS seconds.

    Failure mode: if this function returns false for a fresh compaction, the
    health check proceeds to check the inbox for stale messages.  During the
    1-3 minute compaction pause, real user messages WILL be stale.  The health
    check then restarts the dispatcher — corrupting its state and discarding any
    in-progress work.  This is the most disruptive false-positive in the system.
    """
    state_file = tmp_path / "lobster-state.json"
    # Write a compacted_at that is 30 seconds old — well within suppress window.
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    state_file.write_text(json.dumps({"mode": "active", "compacted_at": ts}))

    fragment = _is_compaction_recent_script(state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode == 0, (
        "is_compaction_recent() returned false (non-zero) for a 30-second-old "
        "compacted_at, but should return true (0) to suppress stale-inbox checks "
        f"within the {COMPACTION_SUPPRESS_SECONDS}s window.\n"
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# D3 — compaction suppression does NOT fire when compacted_at is stale
# ---------------------------------------------------------------------------


def test_compaction_suppression_off_when_compaction_stale(tmp_path: Path) -> None:
    """
    D3: is_compaction_recent() must return false (non-zero) when compacted_at is
    older than COMPACTION_SUPPRESS_SECONDS.

    Failure mode: if this function returns true for a stale compacted_at, the
    health check suppresses its stale-inbox logic indefinitely after any prior
    compaction event.  A genuinely stuck dispatcher would never trigger a
    restart or alert — monitoring is silently disabled.
    """
    state_file = tmp_path / "lobster-state.json"
    # Use a timestamp far enough in the past to be outside the suppress window.
    stale_age = COMPACTION_SUPPRESS_SECONDS + 120
    stale_epoch = time.time() - stale_age
    stale_ts = datetime.fromtimestamp(stale_epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    state_file.write_text(json.dumps({"mode": "active", "compacted_at": stale_ts}))

    fragment = _is_compaction_recent_script(state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        f"is_compaction_recent() returned true (0) for a compacted_at that is "
        f"{stale_age}s old, but the suppress window is only {COMPACTION_SUPPRESS_SECONDS}s. "
        "Stale compaction timestamps must NOT suppress monitoring.\n"
        f"stderr: {result.stderr!r}"
    )


def test_compaction_suppression_off_when_no_state_file(tmp_path: Path) -> None:
    """
    D3 (edge case): is_compaction_recent() must return false when lobster-state.json
    does not exist.

    Failure mode: if this returns true when the state file is absent (e.g. fresh
    install, deleted file), monitoring is silently disabled from the first run.
    """
    missing_state_file = tmp_path / "nonexistent-state.json"
    # Do NOT create the file.

    fragment = _is_compaction_recent_script(missing_state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        "is_compaction_recent() returned true (0) when lobster-state.json does "
        "not exist. This would suppress stale-inbox monitoring on fresh installs "
        "or after the state file is deleted.\n"
        f"stderr: {result.stderr!r}"
    )


def test_compaction_suppression_off_when_compacted_at_missing(tmp_path: Path) -> None:
    """
    D3 (edge case): is_compaction_recent() must return false when lobster-state.json
    exists but has no compacted_at field.

    Failure mode: if this returns true for a state file that never had a
    compacted_at (e.g. written by an older version of the wrapper), stale-inbox
    monitoring is incorrectly suppressed.
    """
    state_file = tmp_path / "lobster-state.json"
    state_file.write_text(json.dumps({"mode": "active"}))  # no compacted_at

    fragment = _is_compaction_recent_script(state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        "is_compaction_recent() returned true (0) when compacted_at is absent "
        "from lobster-state.json. This would suppress stale-inbox monitoring "
        "on systems that have never had a compaction event.\n"
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# D4 — maintenance flag causes immediate clean exit
# ---------------------------------------------------------------------------


def test_maintenance_flag_causes_clean_exit(tmp_path: Path) -> None:
    """
    D4: When the maintenance flag exists, the health check must exit 0 without
    running any health checks or triggering any restarts.

    Failure mode: if the maintenance flag is ignored, `lobster stop` followed by
    any health check run within the maintenance window causes the health check to
    restart the service the operator just stopped. This creates a fight between
    the operator and the monitor — the system cannot be cleanly taken offline.

    We test this by running the full health-check script with HOME and
    LOBSTER_MESSAGES redirected to a temp directory, placing the maintenance
    flag at the expected path, and asserting a clean exit.
    """
    # Set up a fake messages directory with the maintenance flag in place.
    messages_dir = tmp_path / "messages"
    config_dir = messages_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "lobster-maintenance").touch()

    # Create required directory structure so the script can mkdir log dirs.
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "LOBSTER_MESSAGES": str(messages_dir),
        "LOBSTER_WORKSPACE": str(workspace_dir),
        # Point config.env to a non-existent file so Telegram alerting is
        # safely disabled (no network calls in smoke tests).
        "LOBSTER_CONFIG_DIR": str(tmp_path / "no-config"),
        # Override the lock file to a temp path so the test doesn't conflict
        # with a running health check on the same machine.
        "LOBSTER_HEALTH_LOCK": str(tmp_path / "health-check.lock"),
    }

    result = subprocess.run(
        ["bash", str(HEALTH_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, (
        "health-check-v3.sh did not exit 0 under maintenance flag. "
        f"returncode={result.returncode}\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# D5 — WFM per-message heartbeat (issue #694)
# ---------------------------------------------------------------------------
#
# The dispatcher is considered "fresh" if EITHER the WFM heartbeat file was
# touched recently OR last_processed_at in lobster-state.json was updated
# recently.  These tests verify both signals independently and together.

# How long before a dispatcher is considered stale.
WFM_STALE_SECONDS = 600


def _check_wfm_freshness_script(
    heartbeat_file: Path,
    state_file: Path,
    tmp_path: Path,
) -> str:
    """
    Build a self-contained bash script fragment that:
    - Sets the minimal variables check_wfm_freshness() reads
    - Injects stub log functions so logging doesn't fail
    - Calls check_wfm_freshness()
    - Exits with the function's return code (0=GREEN, 2=RED)
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "health-check.log"

    fn_body = _extract_function("check_wfm_freshness")

    return f"""
#!/bin/bash
HEARTBEAT_FILE="{heartbeat_file}"
LOBSTER_STATE_FILE="{state_file}"
WFM_STALE_SECONDS={WFM_STALE_SECONDS}
LOG_FILE="{log_file}"
mkdir -p "$(dirname "$LOG_FILE")"
log()       {{ echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }}
log_info()  {{ log "INFO"  "$1"; }}
log_warn()  {{ log "WARN"  "$1"; }}
log_error() {{ log "ERROR" "$1"; }}

{fn_body}

check_wfm_freshness
exit $?
"""


def test_wfm_freshness_green_when_heartbeat_recent(tmp_path: Path) -> None:
    """
    D5a: check_wfm_freshness() must return GREEN (0) when the WFM heartbeat
    file was touched recently, even if last_processed_at is absent.

    This is the pre-existing behaviour: WFM heartbeat alone is sufficient.
    """
    heartbeat = tmp_path / "claude-heartbeat"
    heartbeat.touch()  # mtime = now

    state_file = tmp_path / "lobster-state.json"
    state_file.write_text(json.dumps({"mode": "active"}))  # no last_processed_at

    fragment = _check_wfm_freshness_script(heartbeat, state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode == 0, (
        "check_wfm_freshness() returned RED for a fresh WFM heartbeat. "
        f"stderr: {result.stderr!r}"
    )


def test_wfm_freshness_red_when_both_signals_stale(tmp_path: Path) -> None:
    """
    D5b: check_wfm_freshness() must return RED (2) when the WFM heartbeat
    file is stale AND last_processed_at is either absent or also stale.

    Failure mode: if both signals are stale and the function returns GREEN, a
    genuinely stuck dispatcher goes undetected.
    """
    heartbeat = tmp_path / "claude-heartbeat"
    heartbeat.touch()
    # Back-date the heartbeat file to well past the stale threshold
    stale_mtime = time.time() - (WFM_STALE_SECONDS + 120)
    os.utime(heartbeat, (stale_mtime, stale_mtime))

    # last_processed_at is also stale
    stale_epoch = time.time() - (WFM_STALE_SECONDS + 120)
    stale_ts = datetime.fromtimestamp(stale_epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    state_file = tmp_path / "lobster-state.json"
    state_file.write_text(json.dumps({"mode": "active", "last_processed_at": stale_ts}))

    fragment = _check_wfm_freshness_script(heartbeat, state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode == 2, (
        "check_wfm_freshness() returned GREEN when both WFM heartbeat and "
        f"last_processed_at are stale ({WFM_STALE_SECONDS + 120}s old). "
        "A genuinely stuck dispatcher must trigger RED.\n"
        f"stderr: {result.stderr!r}"
    )


def test_wfm_freshness_green_when_last_processed_recent(tmp_path: Path) -> None:
    """
    D5c (the issue #694 fix): check_wfm_freshness() must return GREEN (0) when
    the WFM heartbeat file is stale but last_processed_at is recent.

    This is the core regression test for the fix.  Before this change, a
    dispatcher actively draining a 20-message batch (without returning to
    wait_for_messages) could exhaust the suppression window and trigger a
    spurious health-check restart.  After the fix, any successful mark_processed
    call resets the clock and keeps the health check GREEN.

    Failure mode: if this returns RED, a busy-but-healthy dispatcher gets
    restarted mid-batch, losing in-flight work and corrupting dispatcher state.
    """
    heartbeat = tmp_path / "claude-heartbeat"
    heartbeat.touch()
    # Back-date the WFM heartbeat to well past the stale threshold (simulates
    # a dispatcher that has been processing a long batch without calling WFM)
    stale_mtime = time.time() - (WFM_STALE_SECONDS + 300)
    os.utime(heartbeat, (stale_mtime, stale_mtime))

    # last_processed_at is recent (a few seconds ago) — dispatcher is active
    recent_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    state_file = tmp_path / "lobster-state.json"
    state_file.write_text(
        json.dumps({"mode": "active", "last_processed_at": recent_ts})
    )

    fragment = _check_wfm_freshness_script(heartbeat, state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode == 0, (
        "check_wfm_freshness() returned RED even though last_processed_at is "
        "recent. A dispatcher actively draining messages must not be restarted "
        "just because it hasn't called wait_for_messages recently.\n"
        f"stderr: {result.stderr!r}"
    )


def test_wfm_freshness_green_when_last_processed_absent_heartbeat_recent(
    tmp_path: Path,
) -> None:
    """
    D5d: check_wfm_freshness() must stay GREEN when last_processed_at is
    absent from the state file but the WFM heartbeat is fresh.

    This covers the upgrade path: before this change is deployed, the state
    file has no last_processed_at field.  The health check must not break on
    older state files.
    """
    heartbeat = tmp_path / "claude-heartbeat"
    heartbeat.touch()  # mtime = now

    # State file exists but has no last_processed_at (pre-upgrade format)
    state_file = tmp_path / "lobster-state.json"
    state_file.write_text(json.dumps({"mode": "active", "compacted_at": "2026-01-01T00:00:00Z"}))

    fragment = _check_wfm_freshness_script(heartbeat, state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode == 0, (
        "check_wfm_freshness() returned RED when last_processed_at is absent "
        "but WFM heartbeat is recent. Must remain backward-compatible with "
        "state files that predate issue #694.\n"
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# D6 — quota exhaustion: check_usage_limit and is_limit_wait (issue #724)
# ---------------------------------------------------------------------------
#
# When Claude's API quota is exhausted the session log contains a phrase like
# "out of extra usage" or "you've hit your limit".  The health check must NOT
# restart the dispatcher in this case — restarting is useless because Claude
# will immediately hit the same wall.  Instead:
#   check_usage_limit() detects the phrase, writes LIMIT_WAIT_STATE_FILE with
#     a midnight-UTC target epoch, and returns 0 (= "suppress restart").
#   is_limit_wait() reads the state file and returns 0 (= "still waiting")
#     until midnight UTC, then cleans up the file and returns 1.
#
# These tests verify:
#   D6a  check_usage_limit returns 0 when quota phrase present in a fresh log
#   D6b  check_usage_limit returns 1 when log is absent
#   D6c  check_usage_limit returns 1 when log is stale (> 10 min old)
#   D6d  is_limit_wait returns 0 when state file present and midnight not passed
#   D6e  is_limit_wait returns 1 when state file absent
#   D6f  is_limit_wait returns 1 and removes stale file when midnight has passed

# Recency guard constant from health-check-v3.sh (600 seconds).
LIMIT_LOG_RECENCY_SECONDS = 600


def _check_usage_limit_script(
    session_log: Path,
    limit_state_file: Path,
    tmp_path: Path,
) -> str:
    """
    Build a self-contained bash fragment that stubs all external dependencies
    of check_usage_limit() and calls it.  Returns the function's exit code.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "health-check.log"
    alert_dedup_dir = tmp_path / "alert-dedup"
    alert_dedup_dir.mkdir(parents=True, exist_ok=True)

    fn_body = _extract_function("check_usage_limit")

    return f"""
#!/bin/bash
CLAUDE_SESSION_LOG="{session_log}"
LIMIT_WAIT_STATE_FILE="{limit_state_file}"
ALERT_DEDUP_DIR="{alert_dedup_dir}"
ALERT_DEDUP_COOLDOWN_SECONDS=3600
LOG_FILE="{log_file}"
mkdir -p "$(dirname "$LOG_FILE")"
log()       {{ echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }}
log_info()  {{ log "INFO"  "$1"; }}
log_warn()  {{ log "WARN"  "$1"; }}
log_error() {{ log "ERROR" "$1"; }}
send_telegram_alert()         {{ : ; }}
send_telegram_alert_deduped() {{ : ; }}

{fn_body}

check_usage_limit
exit $?
"""


def _is_limit_wait_script(
    limit_state_file: Path,
    tmp_path: Path,
) -> str:
    """
    Build a self-contained bash fragment that stubs dependencies of
    is_limit_wait() and calls it.  Returns the function's exit code.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "health-check.log"

    fn_body = _extract_function("is_limit_wait")

    return f"""
#!/bin/bash
LIMIT_WAIT_STATE_FILE="{limit_state_file}"
LOG_FILE="{log_file}"
mkdir -p "$(dirname "$LOG_FILE")"
log()      {{ echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }}
log_info() {{ log "INFO" "$1"; }}

{fn_body}

is_limit_wait
exit $?
"""


def test_check_usage_limit_detects_quota_phrase_in_fresh_log(tmp_path: Path) -> None:
    """
    D6a: check_usage_limit() must return 0 when the session log was modified
    recently and contains a quota-exhaustion phrase.

    Failure mode: if this returns 1 the RED handler proceeds to do_restart(),
    which immediately hits the same quota wall — crash-looping until midnight.
    This was the root cause of the 2026-04-08 outage.
    """
    session_log = tmp_path / "claude-session.log"
    limit_state_file = tmp_path / "health-limit-wait-state"

    session_log.write_text("Claude: out of extra usage for this billing period\n")
    # mtime defaults to now — within recency guard

    fragment = _check_usage_limit_script(session_log, limit_state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode == 0, (
        "check_usage_limit() returned 1 (not detected) for a fresh session log "
        "containing 'out of extra usage'. The RED handler will proceed to restart "
        "Claude into the same quota wall.\n"
        f"stderr: {result.stderr!r}"
    )
    assert limit_state_file.exists(), (
        "check_usage_limit() returned 0 but did not write LIMIT_WAIT_STATE_FILE. "
        "is_limit_wait() will not suppress subsequent health-check runs.\n"
        f"stderr: {result.stderr!r}"
    )


def test_check_usage_limit_returns_false_when_log_absent(tmp_path: Path) -> None:
    """
    D6b: check_usage_limit() must return 1 when the session log does not exist.

    Failure mode: if this returns 0, normal crashes (no session log) are
    incorrectly treated as quota exhaustion and restarts are suppressed
    indefinitely.
    """
    session_log = tmp_path / "claude-session.log"  # deliberately not created
    limit_state_file = tmp_path / "health-limit-wait-state"

    fragment = _check_usage_limit_script(session_log, limit_state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        "check_usage_limit() returned 0 (limit detected) when session log does "
        "not exist. This would suppress restarts for ordinary crashes.\n"
        f"stderr: {result.stderr!r}"
    )


def test_check_usage_limit_returns_false_when_log_stale(tmp_path: Path) -> None:
    """
    D6c: check_usage_limit() must return 1 when the session log is older than
    the 10-minute recency guard, even if it contains a quota phrase.

    Failure mode: if this returns 0 for a stale log, a quota event from a
    prior session permanently suppresses restart logic for future crashes.
    """
    session_log = tmp_path / "claude-session.log"
    limit_state_file = tmp_path / "health-limit-wait-state"

    session_log.write_text("Claude: out of extra usage for this billing period\n")
    # Back-date mtime to past the recency guard
    stale_mtime = time.time() - (LIMIT_LOG_RECENCY_SECONDS + 120)
    os.utime(session_log, (stale_mtime, stale_mtime))

    fragment = _check_usage_limit_script(session_log, limit_state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        "check_usage_limit() returned 0 (limit detected) for a session log that "
        f"is more than {LIMIT_LOG_RECENCY_SECONDS}s old. Stale quota phrases from "
        "prior sessions must not suppress restart logic for current crashes.\n"
        f"stderr: {result.stderr!r}"
    )


def test_is_limit_wait_returns_true_before_midnight_utc(tmp_path: Path) -> None:
    """
    D6d: is_limit_wait() must return 0 (still waiting) when LIMIT_WAIT_STATE_FILE
    contains a midnight-UTC target epoch that has not yet passed.

    Failure mode: if this returns 1, the RED handler proceeds to restart during
    the quota window — causing the same crash-loop the fix was designed to prevent.
    """
    limit_state_file = tmp_path / "health-limit-wait-state"

    # Write a state file with a target epoch 2 hours from now
    now = int(time.time())
    future_midnight = now + 7200  # 2 hours ahead
    limit_state_file.write_text(f"{now} 7200 {future_midnight} midnight-utc\n")

    fragment = _is_limit_wait_script(limit_state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode == 0, (
        "is_limit_wait() returned 1 (not waiting) when the target midnight-UTC "
        "epoch is still 2 hours in the future. Restarts should be suppressed "
        "until the quota window expires.\n"
        f"stderr: {result.stderr!r}"
    )


def test_is_limit_wait_returns_false_when_no_state_file(tmp_path: Path) -> None:
    """
    D6e: is_limit_wait() must return 1 (not waiting) when LIMIT_WAIT_STATE_FILE
    does not exist.

    Failure mode: if this returns 0, all RED events are permanently suppressed
    even without a prior quota detection.
    """
    limit_state_file = tmp_path / "health-limit-wait-state"  # not created

    fragment = _is_limit_wait_script(limit_state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        "is_limit_wait() returned 0 (waiting) when LIMIT_WAIT_STATE_FILE does "
        "not exist. Without a state file, no quota event was recorded and "
        "restarts must not be suppressed.\n"
        f"stderr: {result.stderr!r}"
    )


def test_is_limit_wait_returns_false_and_cleans_up_after_midnight_utc(
    tmp_path: Path,
) -> None:
    """
    D6f: is_limit_wait() must return 1 (no longer waiting) and remove the state
    file when the stored target epoch has already passed.

    Failure mode: if this returns 0 after midnight UTC, the quota guard never
    expires and restarts are suppressed permanently — the system stays down even
    after the quota resets.
    """
    limit_state_file = tmp_path / "health-limit-wait-state"

    # Write a state file with a target epoch in the past (quota reset already happened)
    past_midnight = int(time.time()) - 3600  # 1 hour ago
    recorded_at = past_midnight - 43200  # recorded 12 hours before that
    limit_state_file.write_text(f"{recorded_at} 43200 {past_midnight} midnight-utc\n")

    fragment = _is_limit_wait_script(limit_state_file, tmp_path)
    result = _run_bash_fragment(fragment)

    assert result.returncode != 0, (
        "is_limit_wait() returned 0 (still waiting) after midnight UTC has passed. "
        "The quota should have reset and normal restart logic should resume.\n"
        f"stderr: {result.stderr!r}"
    )
    assert not limit_state_file.exists(), (
        "is_limit_wait() did not remove the stale LIMIT_WAIT_STATE_FILE after "
        "midnight UTC passed. The stale file may suppress future restart attempts.\n"
        f"stderr: {result.stderr!r}"
    )
