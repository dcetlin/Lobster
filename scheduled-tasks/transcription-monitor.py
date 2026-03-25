#!/usr/bin/env python3
"""
Transcription Keep-Alive Monitor

Checks if whisper-cli is currently running. If so, reads the pending
transcription JSON to compute elapsed/estimated time and writes a
keep-alive ping to ~/messages/outbox/ so the user gets progress
feedback during long transcriptions.

Self-silencing: exits immediately (no outbox write) when whisper-cli is
not running. Safe to call from cron every 5 minutes.

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
# This means a 10-minute audio file takes ~3.3 minutes of CPU time.
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


def find_pending_voice_json() -> dict | None:
    """Return the first voice message JSON from pending-transcription/, or None.

    Reads all .json files and returns the first with type == "voice".
    Pure modulo filesystem I/O.
    """
    try:
        for json_file in sorted(PENDING_DIR.glob("*.json")):
            if json_file.name.endswith(".tmp"):
                continue
            try:
                data = json.loads(json_file.read_text())
                if data.get("type") == "voice":
                    return data
            except Exception:
                continue
    except Exception:
        pass
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


def compute_progress_stats(
    audio_duration_s: float,
    elapsed_s: float,
    realtime_multiplier: float,
) -> dict:
    """Compute transcription progress statistics from timing inputs.

    Returns a dict with:
        elapsed_min: float
        estimated_total_min: float
        remaining_min: float
        realtime_factor: float  (elapsed / audio_duration)
        audio_min: float

    All pure math — no I/O.
    """
    estimated_total_s = audio_duration_s * realtime_multiplier
    remaining_s = max(0.0, estimated_total_s - elapsed_s)
    realtime_factor = elapsed_s / audio_duration_s if audio_duration_s > 0 else 0.0

    return {
        "audio_min": audio_duration_s / 60.0,
        "elapsed_min": elapsed_s / 60.0,
        "estimated_total_min": estimated_total_s / 60.0,
        "remaining_min": remaining_s / 60.0,
        "realtime_factor": realtime_factor,
    }


def format_ping_message(stats: dict) -> str:
    """Format a human-readable progress ping from computed stats.

    Pure string formatting — no I/O.
    """
    audio_min = stats["audio_min"]
    elapsed_min = stats["elapsed_min"]
    remaining_min = stats["remaining_min"]
    factor = stats["realtime_factor"]

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
    msg_data = find_pending_voice_json()
    if msg_data is None:
        # whisper is running but no pending JSON found — could be finishing up.
        # Exit silently; the completion ping from worker.py will fire shortly.
        return 0

    # Step 3: Compute elapsed time.
    # Prefer process start time from /proc; fall back to file mtime of the JSON.
    start_time: float | None = get_process_start_time(pid)
    if start_time is None:
        # Fall back: use mtime of the pending JSON file as a proxy for when
        # transcription started (close enough for a progress heuristic).
        try:
            json_files = sorted(PENDING_DIR.glob("*.json"))
            if json_files:
                start_time = json_files[0].stat().st_mtime
        except Exception:
            pass

    if start_time is None:
        # Can't determine elapsed time — skip ping to avoid misleading output.
        return 0

    elapsed_s = time.time() - start_time

    # Step 4: Compute stats.
    audio_duration_s = float(msg_data.get("audio_duration", 0) or 0)
    if audio_duration_s <= 0:
        # No duration metadata — can't estimate, skip ping.
        return 0

    stats = compute_progress_stats(audio_duration_s, elapsed_s, REALTIME_MULTIPLIER)

    # Step 5: Determine chat_id.
    chat_id = msg_data.get("chat_id")
    if chat_id is None:
        chat_id = read_admin_chat_id()
    if chat_id is None:
        return 1

    # Step 6: Build and write the ping.
    text = format_ping_message(stats)
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
