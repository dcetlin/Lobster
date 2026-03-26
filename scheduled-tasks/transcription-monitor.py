#!/usr/bin/env python3
"""
Transcription Keep-Alive Monitor

Checks if whisper-cli is currently running. If so, reads the pending
transcription JSON to compute elapsed/estimated time and writes a
keep-alive ping to ~/messages/outbox/ so the user gets progress
feedback during long transcriptions.

Self-silencing: exits immediately (no outbox write) when whisper-cli is
not running. Safe to call from cron every 5 minutes.

Progress tracking:
  If worker.py has already written a <id>.progress file (a plain float
  representing audio-seconds transcribed so far), that is used for a real
  completion percentage.  Otherwise falls back to the 3x-realtime heuristic.

Usage:
    uv run scheduled-tasks/transcription-monitor.py

Outbox format matches notify_transcription_complete() in worker.py:
    {
        "id": "<timestamp>_transcription_monitor_<hex>",
        "source": "telegram",
        "chat_id": <int>,
        "text": "...",
        "timestamp": "<iso>",
        "reply_to_message_id": <int>  # optional
    }
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_CONFIG_DIR = Path(os.environ.get("LOBSTER_CONFIG_DIR", Path.home() / "lobster-config"))

PENDING_DIR = _MESSAGES / "pending-transcription"
OUTBOX_DIR = _MESSAGES / "outbox"

# Heuristic: whisper-cli processes audio at ~3x realtime on this hardware.
# Used as a fallback when no .progress file exists yet.
REALTIME_MULTIPLIER = 3.0


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def find_whisper_pid() -> int | None:
    """Return the PID of a running whisper-cli process, or None if not found.

    Uses pgrep to match the binary name. Returns the first PID found.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "whisper-cli"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        pids = [int(p) for p in result.stdout.strip().split() if p.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def get_process_start_time(pid: int) -> float | None:
    """Return the process start time as a Unix epoch float, or None on failure.

    Reads /proc/<pid>/stat which contains the process start time in clock
    ticks since system boot. Converts to wall-clock epoch using /proc/uptime.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 (0-indexed: 21) in /proc/<pid>/stat is start time in ticks
        fields = stat.split()
        start_ticks = int(fields[21])
        uptime_secs = float(Path("/proc/uptime").read_text().split()[0])
        clock_ticks = os.sysconf("SC_CLK_TCK")
        boot_time = time.time() - uptime_secs
        start_epoch = boot_time + (start_ticks / clock_ticks)
        return start_epoch
    except Exception:
        return None


def find_pending_voice_file() -> tuple[dict, Path] | None:
    """Return (msg_data, json_path) for the first pending voice message, or None."""
    try:
        for json_file in sorted(PENDING_DIR.glob("*.json")):
            if json_file.name.endswith(".tmp"):
                continue
            try:
                data = json.loads(json_file.read_text())
                if data.get("type") == "voice":
                    return data, json_file
            except Exception:
                continue
    except Exception:
        pass
    return None


def read_progress_seconds(pending_stem: str) -> float | None:
    """Read audio-seconds-processed from the .progress file, or None if absent."""
    progress_file = PENDING_DIR / f"{pending_stem}.progress"
    try:
        return float(progress_file.read_text().strip())
    except Exception:
        return None


def read_admin_chat_id() -> int | None:
    """Return the first TELEGRAM_ALLOWED_USERS entry from config.env, or None."""
    try:
        config_file = _CONFIG_DIR / "config.env"
        if not config_file.exists():
            return None
        for line in config_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("TELEGRAM_ALLOWED_USERS="):
                val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                first = val.split(",")[0].strip()
                if first.lstrip("-").isdigit():
                    return int(first)
    except Exception:
        pass
    return None


def format_ping_message(
    audio_duration_s: float,
    elapsed_s: float,
    audio_processed_s: float | None,
) -> str:
    """Format a human-readable progress ping.

    When audio_processed_s is provided (real progress from whisper-cli), uses
    the actual transcription rate to compute percentage and remaining time.
    Otherwise falls back to the 3x-realtime heuristic.
    """
    audio_min = audio_duration_s / 60.0
    elapsed_min = elapsed_s / 60.0

    if audio_processed_s is not None and audio_processed_s > 0 and audio_duration_s > 0:
        # Real progress path
        pct = min(audio_processed_s / audio_duration_s, 1.0)
        pct_int = int(pct * 100)
        # Estimate remaining using the actual transcription rate so far
        rate = elapsed_s / audio_processed_s  # wall-clock seconds per audio-second
        remaining_audio_s = audio_duration_s - audio_processed_s
        remaining_min = (remaining_audio_s * rate) / 60.0
        return (
            f"\U0001f3a4 Transcribing: {audio_min:.1f} min audio \u2014 "
            f"{pct_int}% done, ~{remaining_min:.1f} min remaining"
        )
    else:
        # Heuristic fallback (no .progress file yet)
        estimated_total_s = audio_duration_s * REALTIME_MULTIPLIER
        remaining_s = max(0.0, estimated_total_s - elapsed_s)
        remaining_min = remaining_s / 60.0
        factor = elapsed_s / audio_duration_s if audio_duration_s > 0 else 0.0
        return (
            f"\U0001f3a4 Still transcribing: "
            f"{audio_min:.1f} min audio, "
            f"{elapsed_min:.1f} min elapsed, "
            f"~{remaining_min:.1f} min est. remaining "
            f"({factor:.1f}x realtime)"
        )


def build_outbox_reply(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None,
) -> dict:
    """Build an outbox reply dict in the standard Lobster format.

    Pure construction — no I/O.
    """
    now = datetime.now(timezone.utc)
    ts_ms = int(now.timestamp() * 1000)
    reply_id = f"{ts_ms}_transcription_monitor_{uuid.uuid4().hex[:8]}"

    reply: dict = {
        "id": reply_id,
        "source": "telegram",
        "chat_id": chat_id,
        "text": text,
        "timestamp": now.isoformat(),
    }
    if reply_to_message_id is not None:
        reply["reply_to_message_id"] = reply_to_message_id

    return reply


def atomic_write_json(dest: Path, data: dict) -> None:
    """Write data as JSON to dest atomically via a temp-then-rename."""
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.rename(dest)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the transcription monitor. Returns 0 on success, 1 on error."""

    # Step 1: Check if whisper-cli is running.
    pid = find_whisper_pid()
    if pid is None:
        # Not running — silent exit, no ping needed.
        return 0

    # Step 2: Find the pending voice message JSON.
    result = find_pending_voice_file()
    if result is None:
        # whisper is running but no pending JSON found — could be finishing up.
        # Exit silently; the completion ping from worker.py will fire shortly.
        return 0
    msg_data, pending_file = result

    # Step 3: Compute elapsed time.
    # Prefer process start time from /proc; fall back to file mtime of the JSON.
    start_time: float | None = get_process_start_time(pid)
    if start_time is None:
        try:
            start_time = pending_file.stat().st_mtime
        except Exception:
            pass

    if start_time is None:
        # Can't determine elapsed time — skip ping to avoid misleading output.
        return 0

    elapsed_s = time.time() - start_time

    # Step 4: Get audio duration.
    audio_duration_s = float(msg_data.get("audio_duration", 0) or 0)
    if audio_duration_s <= 0:
        # No duration metadata — can't estimate, skip ping.
        return 0

    # Step 5: Try to read real progress from the .progress file.
    audio_processed_s = read_progress_seconds(pending_file.stem)

    # Step 6: Determine chat_id.
    chat_id = msg_data.get("chat_id")
    if chat_id is None:
        chat_id = read_admin_chat_id()
    if chat_id is None:
        return 1

    # Step 7: Build and write the ping.
    text = format_ping_message(audio_duration_s, elapsed_s, audio_processed_s)
    reply_to_message_id = msg_data.get("telegram_message_id")
    reply = build_outbox_reply(int(chat_id), text, reply_to_message_id)

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUTBOX_DIR / f"{reply['id']}.json"
    atomic_write_json(dest, reply)

    print(f"[transcription-monitor] Ping written: {dest.name}")
    print(f"[transcription-monitor] Message: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
