#!/usr/bin/env python3
"""router-liveness-check.py — Deterministic Telegram router liveness check.

Checks whether the lobster-router (Telegram bot) is genuinely healthy or hung
by querying the Telegram getWebhookInfo API for pending_update_count.

Verdict logic (deterministic — no LLM inference):
  - pending_update_count == 0  → HEALTHY (no undelivered backlog)
  - pending_update_count > 0   → UNHEALTHY (updates are accumulating)
  - API call fails              → UNKNOWN (cannot determine — not a hang verdict)

This replaces the previous ad-hoc LLM-based investigation that incorrectly
inferred HUNG from log silence + empty inbox — both of which are consistent
with healthy-idle operation when there is no user traffic.

Exit codes:
  0 — HEALTHY or UNKNOWN (no action needed)
  1 — UNHEALTHY (pending updates accumulating)

Output: JSON to stdout with verdict, pending_update_count, and details.
When called from health-check-v3.sh, the exit code drives escalation.

Usage:
  uv run scripts/router-liveness-check.py
  uv run scripts/router-liveness-check.py --config-env ~/lobster-config/config.env
  uv run scripts/router-liveness-check.py --bot-token "TOKEN"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 10


class Verdict(StrEnum):
    """Router liveness verdict — deterministic, no ambiguity."""
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


# Exit codes
EXIT_HEALTHY = 0
EXIT_UNHEALTHY = 1


@dataclass(frozen=True)
class LivenessResult:
    """Immutable result of a router liveness check."""
    verdict: Verdict
    pending_update_count: int | None
    detail: str


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def evaluate_verdict(pending_update_count: int | None, error: str | None = None) -> LivenessResult:
    """Produce a deterministic liveness verdict from getWebhookInfo data.

    Pure function — no I/O, no side effects.
    """
    if error is not None or pending_update_count is None:
        return LivenessResult(
            verdict=Verdict.UNKNOWN,
            pending_update_count=pending_update_count,
            detail=error or "No pending_update_count available",
        )

    if pending_update_count == 0:
        return LivenessResult(
            verdict=Verdict.HEALTHY,
            pending_update_count=0,
            detail="pending_update_count=0 — no undelivered backlog, router is consuming updates",
        )

    return LivenessResult(
        verdict=Verdict.UNHEALTHY,
        pending_update_count=pending_update_count,
        detail=f"pending_update_count={pending_update_count} — updates are accumulating, router may be hung",
    )


def result_to_dict(result: LivenessResult) -> dict[str, Any]:
    """Serialize a LivenessResult to a JSON-safe dict. Pure function."""
    return {
        "verdict": str(result.verdict),
        "pending_update_count": result.pending_update_count,
        "detail": result.detail,
    }


# ---------------------------------------------------------------------------
# I/O functions (side effects isolated here)
# ---------------------------------------------------------------------------


def load_bot_token(config_env_path: str | None = None) -> str | None:
    """Load the Telegram bot token from environment or config.env file.

    Lookup order:
      1. TELEGRAM_BOT_TOKEN environment variable
      2. config.env file (default: ~/lobster-config/config.env)

    Returns None if no token found.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token

    if config_env_path is None:
        config_dir = os.environ.get("LOBSTER_CONFIG_DIR", os.path.expanduser("~/lobster-config"))
        config_env_path = os.path.join(config_dir, "config.env")

    path = Path(config_env_path)
    if not path.exists():
        return None

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("TELEGRAM_BOT_TOKEN="):
            val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                return val

    return None


def fetch_webhook_info(bot_token: str) -> tuple[int | None, str | None]:
    """Call Telegram getWebhookInfo and return (pending_update_count, error).

    Returns (count, None) on success, (None, error_message) on failure.
    Side effect: makes an HTTP request to the Telegram API.
    """
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/getWebhookInfo"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if not data.get("ok"):
            return None, f"Telegram API returned ok=false: {data.get('description', 'unknown error')}"

        result = data.get("result", {})
        pending = result.get("pending_update_count")
        if pending is None:
            return None, "getWebhookInfo response missing pending_update_count field"

        return int(pending), None

    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        return None, f"Response parse error: {e}"
    except Exception as e:
        return None, f"Unexpected error: {e}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def check_liveness(bot_token: str) -> LivenessResult:
    """Run the full liveness check. Composes I/O + pure verdict logic."""
    pending_count, error = fetch_webhook_info(bot_token)
    return evaluate_verdict(pending_count, error)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic Telegram router liveness check via getWebhookInfo.",
    )
    parser.add_argument(
        "--config-env",
        type=str,
        default=None,
        help="Path to config.env file (default: ~/lobster-config/config.env)",
    )
    parser.add_argument(
        "--bot-token",
        type=str,
        default=None,
        help="Telegram bot token (overrides config.env lookup)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output result as JSON (default)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    token = args.bot_token or load_bot_token(args.config_env)
    if not token:
        error_result = LivenessResult(
            verdict=Verdict.UNKNOWN,
            pending_update_count=None,
            detail="No TELEGRAM_BOT_TOKEN found in environment or config.env",
        )
        print(json.dumps(result_to_dict(error_result), indent=2))
        return EXIT_HEALTHY  # UNKNOWN is not actionable — don't trigger escalation

    result = check_liveness(token)
    print(json.dumps(result_to_dict(result), indent=2))

    if result.verdict == Verdict.UNHEALTHY:
        return EXIT_UNHEALTHY

    return EXIT_HEALTHY


if __name__ == "__main__":
    sys.exit(main())
