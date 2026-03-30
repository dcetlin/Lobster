#!/usr/bin/env python3
"""
PostToolUse hook: Write a phase_alignment signal when a PR merge is detected.
Signal-write-and-exit only — never spawns oracle directly (too slow for a hook).
Oracle spawning is handled by the dispatcher reading oracle-review-requested signals.

Always exits 0 (non-blocking). Completes in < 100ms.
"""
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

PR_MERGE_PATTERNS = [
    r'gh\s+pr\s+merge',
    r'gh\s+pr\s+close',
    r'git\s+merge\s+--squash',
    r'git\s+merge\s+.*pull',
]

# Rate limit: don't write a signal if one was written in the last 5 minutes
# (prevents multiple signals from a compound merge sequence)
RATE_LIMIT_SECONDS = 300


def is_pr_merge(command: str) -> bool:
    return any(re.search(p, command) for p in PR_MERGE_PATTERNS)


def extract_pr_ref(command: str) -> str:
    """Extract PR number or branch reference from command."""
    # gh pr merge 42 or gh pr merge --squash 42
    match = re.search(r'gh\s+pr\s+(?:merge|close)\s+(\d+)', command)
    if match:
        return f"PR #{match.group(1)}"

    # branch name
    match = re.search(r'git\s+merge\s+(\S+)', command)
    if match:
        return f"branch {match.group(1)}"

    return "unknown ref"


def check_rate_limit(signals_dir: Path) -> bool:
    """Return True if we're within rate limit (should NOT write new signal)."""
    try:
        recent_signals = sorted(signals_dir.glob("*.json"), reverse=True)
        for signal_file in recent_signals[:5]:  # Check only 5 most recent
            try:
                data = json.loads(signal_file.read_text())
                if data.get("signal_type") == "oracle-review-requested":
                    ts = datetime.fromisoformat(data["timestamp"])
                    age_seconds = (datetime.utcnow() - ts).total_seconds()
                    if age_seconds < RATE_LIMIT_SECONDS:
                        return True  # Within rate limit, skip
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    except Exception:
        pass
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")

    if not is_pr_merge(command):
        sys.exit(0)

    signals_dir = Path.home() / "lobster-workspace" / "signals" / "phase-alignment"

    try:
        signals_dir.mkdir(parents=True, exist_ok=True)

        if check_rate_limit(signals_dir):
            sys.exit(0)  # Rate limited, skip silently

        pr_ref = extract_pr_ref(command)
        signal = {
            "timestamp": datetime.utcnow().isoformat(),
            "signal_type": "oracle-review-requested",
            "trajectory_area": "project",
            "notable": True,
            "notable_reason": "PR merge detected — oracle review requested",
            "text": f"PR merge detected ({pr_ref}). Oracle review queued.",
            "task_id": data.get("tool_use_id", ""),
            "chat_id": "",
        }

        fname = (
            f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            f"_{uuid.uuid4().hex[:8]}.json"
        )
        (signals_dir / fname).write_text(json.dumps(signal, indent=2))

    except Exception:
        pass  # Never block on hook failure

    sys.exit(0)


if __name__ == "__main__":
    main()
