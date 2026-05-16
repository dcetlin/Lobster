#!/usr/bin/env python3
"""
CC Usage Poller — poll claude.ai for accurate Claude Code quota data.

Hits the claude.ai internal usage API with a stored session cookie to get
authoritative 5-hour and 7-day quota percentages. Writes results to
~/.claude/cc-budget/state.json so the data survives idle-through-reset
cycles that leave the statusLine hook-written data stale.

Problem context: state.json is only updated when Claude Code actively
reports quota via the statusLine hook. During idle periods — including
weekly quota resets — the file retains pre-reset numbers indefinitely.
Observed in production: 138h stale (2026-05-04 → 2026-05-10). This
poller eliminates that drift.

API flow:
    1. GET /api/bootstrap  → find the org with CC plan (seat_tier check)
    2. GET /api/organizations/{org_id}/usage  → quota percentages

Both endpoints are on claude.ai and protected by Cloudflare. The script
uses cloudscraper to handle the CF JS challenge automatically.

Response shape from /api/organizations/{org_id}/usage:
    {
      "five_hour":  { "utilization": 51.0, "resets_at": "<ISO8601>" },
      "seven_day":  { "utilization": 28.0, "resets_at": "<ISO8601>" },
      ...
    }

Cron schedule (every 30 minutes):
    */30 * * * * cd ~/lobster && uv run scheduled-tasks/cc-usage-poller.py >> ~/lobster-workspace/scheduled-jobs/logs/cc-usage-poller.log 2>&1 # LOBSTER-CC-USAGE-POLLER

Type B dispatch: cron calls this script directly (no inbox/ message, no
dispatcher involvement). The jobs.json enabled gate is checked at the top
of main() so that runtime enable/disable is respected without touching cron.

Session cookie:
    Store the sessionKey cookie value (from claude.ai DevTools → Application
    → Cookies → sessionKey) in ~/lobster-user-config/cc-usage-session-cookie
    as a single plain-text line with no quotes.

Dependencies (auto-installed by uv):
    cloudscraper — handles Cloudflare IUAM/bot-detection challenges

Run standalone:
    uv run ~/lobster/scheduled-tasks/cc-usage-poller.py [--dry-run]

Related issue: dcetlin/Lobster#1101
"""

# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cloudscraper",
# ]
# ///

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.jobs import is_job_enabled  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("cc-usage-poller")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bootstrap endpoint — returns account info and org memberships
BOOTSTRAP_URL = "https://claude.ai/api/bootstrap"

# Org usage endpoint template — substitute org UUID
ORG_USAGE_URL = "https://claude.ai/api/organizations/{org_id}/usage"

# Cookie config file — stores the claude.ai sessionKey value
COOKIE_CONFIG_PATH = Path.home() / "lobster-user-config" / "cc-usage-session-cookie"

# State file — written by this script and by the statusLine hook (cc-usage-collect.sh)
STATE_FILE_PATH = Path.home() / ".claude" / "cc-budget" / "state.json"

# Source tag written into state.json to distinguish poller-written data
# from hook-written data
SOURCE_TAG = "cc-usage-poller"

# Telegram chat_id for cookie-expiry alert delivery
ADMIN_CHAT_ID: int = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

# Sentinel file prefix — one file per calendar date prevents repeated alerts
# Format: /tmp/cc-usage-cookie-expired-alert-YYYY-MM-DD
COOKIE_EXPIRY_SENTINEL_PREFIX = "/tmp/cc-usage-cookie-expired-alert-"

# Text of the cookie-expiry Telegram alert
COOKIE_EXPIRY_ALERT_TEXT = (
    "CC usage cookie expired — paste a new session key into "
    "~/lobster-user-config/cc-usage-session-cookie\n\n"
    "Get it from: claude.ai -> DevTools -> Application -> Cookies -> sessionKey"
)

# ---------------------------------------------------------------------------
# Cookie-expiry alert — inbox injection with per-day rate limiting
# ---------------------------------------------------------------------------


def _inbox_dir() -> Path:
    """Return the inbox directory path, respecting LOBSTER_MESSAGES env override."""
    messages_base = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
    return messages_base / "inbox"


def _cookie_expiry_alert_already_sent_today() -> bool:
    """
    Return True if a cookie-expiry alert sentinel exists for today's date.

    Sentinel file format: /tmp/cc-usage-cookie-expired-alert-YYYY-MM-DD
    Creates one sentinel per calendar day to prevent alert floods when the
    cron runs every 30 minutes and the cookie stays expired all day.
    """
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return Path(f"{COOKIE_EXPIRY_SENTINEL_PREFIX}{today}").exists()


def _write_cookie_expiry_alert(http_code: int, dry_run: bool = False) -> None:
    """
    Write a cookie-expiry alert to the Lobster inbox and touch the daily sentinel.

    The dispatcher picks up the inbox message on its next cycle and delivers
    it to the user via Telegram. Fire-and-forget — no delivery confirmation.

    Rate-limited to one alert per calendar day via /tmp sentinel file.
    In dry_run mode: logs the intent but does not write files.
    """
    if _cookie_expiry_alert_already_sent_today():
        log.info("Cookie-expiry alert already sent today — skipping duplicate")
        return

    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "source": "system",
        "type": "message",
        "chat_id": ADMIN_CHAT_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": COOKIE_EXPIRY_ALERT_TEXT,
    }

    if dry_run:
        log.info(
            "[dry-run] Would write cookie-expiry alert (HTTP %d) to inbox: %s",
            http_code,
            msg["text"][:80],
        )
        return

    try:
        inbox = _inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        tmp_path = inbox / f"{msg_id}.json.tmp"
        dest_path = inbox / f"{msg_id}.json"
        tmp_path.write_text(json.dumps(msg, indent=2), encoding="utf-8")
        tmp_path.rename(dest_path)
        log.info("Wrote cookie-expiry alert %s to inbox", msg_id)
    except Exception as exc:
        log.warning("Failed to write cookie-expiry alert to inbox: %s", exc)
        return

    # Touch the sentinel — prevents re-alerting for the rest of today
    try:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        Path(f"{COOKIE_EXPIRY_SENTINEL_PREFIX}{today}").touch()
    except Exception as exc:
        log.warning("Failed to write cookie-expiry sentinel: %s", exc)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cookie reading — pure function, no side effects
# ---------------------------------------------------------------------------


def read_session_cookie(cookie_path: Path) -> str | None:
    """
    Read the session cookie from the config file.

    Returns None (not an error) when:
    - File does not exist — cookie not yet configured
    - File exists but contains only comments or whitespace

    Returns the cookie string when a non-comment, non-empty line is found.
    """
    if not cookie_path.exists():
        return None
    lines = cookie_path.read_text().splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


# ---------------------------------------------------------------------------
# HTTP client — cloudscraper bypasses Cloudflare IUAM/bot-detection
# ---------------------------------------------------------------------------


def _make_scraper(session_cookie: str):
    """
    Create a cloudscraper session with the sessionKey cookie set.

    cloudscraper handles Cloudflare's JavaScript challenge (IUAM) automatically,
    which plain urllib/requests cannot do. The sessionKey cookie is set on the
    claude.ai domain.
    """
    import cloudscraper  # type: ignore[import-untyped]

    scraper = cloudscraper.create_scraper()
    scraper.cookies.set("sessionKey", session_cookie, domain="claude.ai")
    return scraper


# ---------------------------------------------------------------------------
# Org discovery — find the org with the CC plan via /api/bootstrap
# ---------------------------------------------------------------------------


def discover_cc_org_id(scraper: Any) -> str:
    """
    Call /api/bootstrap and return the org UUID for the Claude Code plan.

    Strategy: prefer the org with seat_tier containing 'claude_max' or
    'pro' (CC plan). Fall back to the first non-individual org. If only
    one org exists, return it.

    Raises ValueError if no org can be identified.
    Raises requests.HTTPError / CloudScraper exceptions on auth failure.
    """
    resp = scraper.get(BOOTSTRAP_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    memberships = data.get("account", {}).get("memberships", [])
    if not memberships:
        raise ValueError("No org memberships found in /api/bootstrap response")

    # Score each membership: higher score = more likely the CC plan org
    def _score(m: dict) -> int:
        org = m.get("organization", {})
        name = (org.get("name") or "").lower()
        seat_tier = (m.get("seat_tier") or "").lower()
        score = 0
        # Prefer orgs with claude_max or pro tier
        if "claude_max" in seat_tier or "max" in seat_tier:
            score += 10
        if "pro" in seat_tier:
            score += 5
        # Avoid "individual org" (usually a personal placeholder)
        if "individual" in name:
            score -= 3
        return score

    best = max(memberships, key=_score)
    org_id = best.get("organization", {}).get("uuid")
    if not org_id:
        raise ValueError("Could not extract org UUID from bootstrap membership")

    org_name = best.get("organization", {}).get("name", "unknown")
    seat_tier = best.get("seat_tier", "unknown")
    log.info("Using org '%s' (id=%s, seat_tier=%s)", org_name, org_id, seat_tier)
    return org_id


# ---------------------------------------------------------------------------
# API call — isolated side effect
# ---------------------------------------------------------------------------


def fetch_usage(session_cookie: str) -> dict[str, Any]:
    """
    Discover the CC org and fetch usage from /api/organizations/{org_id}/usage.

    Returns the parsed JSON response body.

    Raises:
        requests.HTTPError — for 4xx/5xx responses (after CF challenge is handled)
        requests.ConnectionError / Timeout — for network-level errors
        json.JSONDecodeError — if the response body is not valid JSON
        ValueError — if org discovery fails
    """
    scraper = _make_scraper(session_cookie)
    org_id = discover_cc_org_id(scraper)
    url = ORG_USAGE_URL.format(org_id=org_id)
    resp = scraper.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Response parsing — pure function
# ---------------------------------------------------------------------------


def parse_usage_response(data: dict[str, Any]) -> dict[str, Any]:
    """
    Extract quota fields from the /api/organizations/{org_id}/usage response.

    The endpoint returns a structure like:
    {
      "five_hour":  { "utilization": 51.0, "resets_at": "<ISO8601>" },
      "seven_day":  { "utilization": 28.0, "resets_at": "<ISO8601>" },
      "seven_day_sonnet": { "utilization": 42.0, "resets_at": "<ISO8601>" },
      ...
    }

    Returns a normalized dict suitable for merging into state.json.
    Raises ValueError if expected keys are absent.
    """
    five_hour = data.get("five_hour")
    seven_day = data.get("seven_day")

    if five_hour is None and seven_day is None:
        raise ValueError(
            f"Expected 'five_hour' or 'seven_day' keys in response. Got keys: {list(data.keys())}"
        )

    def _pct(obj: dict | None) -> float | None:
        if obj is None:
            return None
        v = obj.get("utilization")
        return float(v) if v is not None else None

    def _resets_at(obj: dict | None) -> str | None:
        if obj is None:
            return None
        return obj.get("resets_at")

    return {
        "five_hour_pct": _pct(five_hour),
        "five_hour_resets_at": _resets_at(five_hour),
        "seven_day_pct": _pct(seven_day),
        "seven_day_resets_at": _resets_at(seven_day),
    }


# ---------------------------------------------------------------------------
# State file merge — pure function, produces new state dict
# ---------------------------------------------------------------------------


def merge_into_state(existing: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    """
    Merge parsed usage fields into the existing state.json content.

    Preserves all fields not written by this script (v, session_cost_usd,
    snapshots) so the hook-written data is not lost.
    """
    now_unix = int(datetime.now(tz=timezone.utc).timestamp())
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    updated = dict(existing)
    updated["v"] = existing.get("v", 1)
    updated["ts"] = now_unix
    updated["rate_limits"] = {
        "five_hour": {
            "pct": parsed["five_hour_pct"],
            "resets_at": parsed["five_hour_resets_at"],
        },
        "seven_day": {
            "pct": parsed["seven_day_pct"],
            "resets_at": parsed["seven_day_resets_at"],
        },
    }
    updated["last_updated"] = now_iso
    updated["source"] = SOURCE_TAG
    return updated


# ---------------------------------------------------------------------------
# Atomic state write — isolated side effect
# ---------------------------------------------------------------------------


def write_state_atomically(state: dict[str, Any], state_path: Path) -> None:
    """
    Write state dict to state_path atomically via a temp file + rename.

    Creates parent directories if needed. Atomic rename prevents a partial
    write from leaving state_path in a corrupted state.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=state_path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, state_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Load existing state — handles missing file gracefully
# ---------------------------------------------------------------------------


def load_existing_state(state_path: Path) -> dict[str, Any]:
    """Return the current state.json contents, or {} if absent or unreadable."""
    try:
        return json.loads(state_path.read_text())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(dry_run: bool = False) -> int:
    """
    Poll claude.ai for CC quota data and write to state.json.

    Returns 0 on success, 1 on hard failure (unexpected errors).
    Auth failures and missing cookie return 0 (graceful skip — cron retries).
    """
    if not is_job_enabled("cc-usage-poller"):
        log.info("cc-usage-poller is disabled in jobs.json — skipping")
        return 0

    # Step 1: Read session cookie
    cookie = read_session_cookie(COOKIE_CONFIG_PATH)
    if cookie is None:
        log.warning(
            "Session cookie not configured. Create %s and paste your claude.ai "
            "sessionKey cookie value (from DevTools -> Application -> Cookies -> sessionKey). "
            "Skipping this run.",
            COOKIE_CONFIG_PATH,
        )
        return 0

    if dry_run:
        log.info(
            "[dry-run] Would call %s then %s with session cookie from %s",
            BOOTSTRAP_URL,
            ORG_USAGE_URL,
            COOKIE_CONFIG_PATH,
        )
        log.info("[dry-run] Would write to %s", STATE_FILE_PATH)
        return 0

    # Step 2: Fetch usage from claude.ai (via cloudscraper to handle CF challenge)
    try:
        raw_response = fetch_usage(cookie)
    except Exception as exc:
        # Detect auth errors from requests/cloudscraper HTTPError
        http_code = getattr(getattr(exc, "response", None), "status_code", None)
        if http_code in (401, 403):
            log.error(
                "Auth error %d from claude.ai — session cookie may be expired. "
                "Refresh it: DevTools -> Application -> Cookies -> sessionKey -> copy value -> "
                "paste into %s",
                http_code,
                COOKIE_CONFIG_PATH,
            )
            _write_cookie_expiry_alert(http_code, dry_run=dry_run)
            return 0
        log.error("Error fetching usage from claude.ai: %s — will retry on next cron run", exc)
        return 0

    # Step 3: Parse response
    try:
        parsed = parse_usage_response(raw_response)
    except (ValueError, KeyError) as exc:
        snippet = json.dumps(raw_response)[:300]
        log.error(
            "Failed to parse usage response: %s. Raw response snippet: %s",
            exc,
            snippet,
        )
        return 0

    # Step 4: Merge into existing state
    existing = load_existing_state(STATE_FILE_PATH)
    new_state = merge_into_state(existing, parsed)

    # Step 5: Write atomically
    try:
        write_state_atomically(new_state, STATE_FILE_PATH)
    except OSError as exc:
        log.error("Failed to write state file %s: %s", STATE_FILE_PATH, exc)
        return 1

    log.info(
        "Updated %s — 5h: %.1f%% (resets_at: %s), 7d: %.1f%% (resets_at: %s)",
        STATE_FILE_PATH,
        parsed["five_hour_pct"] or 0.0,
        parsed["five_hour_resets_at"],
        parsed["seven_day_pct"] or 0.0,
        parsed["seven_day_resets_at"],
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll claude.ai for CC quota data")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be done without making any HTTP requests or writing files",
    )
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
