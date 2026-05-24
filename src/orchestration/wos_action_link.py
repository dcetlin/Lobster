"""
wos_action_link.py — Canonical site for WOS Telegram deep-link encoding.

This module is the single source of truth for encoding WOS action payloads
into Telegram deep-link URLs.  Both wos_dashboard.py and wos_uow_detail_gen.py
import from here so that payload format changes (e.g. adding a chat_id field for
multi-tenant routing) require a single edit rather than two.

Payload format: base64url(JSON) where JSON is {"a": action, "u": uow_id}.
Short keys keep the encoded payload within Telegram's 64-char start-parameter limit.
The trailing padding characters ("=") are stripped because Telegram drops them.
"""

from __future__ import annotations

import base64
import json
import os


def tg_deep_link(action: str, uow_id: str) -> str:
    """Return a Telegram https://t.me deep link that encodes a WOS action callback.

    Payload format: base64url(JSON) where JSON is {"a": action, "u": uow_id}.
    Uses short keys to stay within Telegram's 64-char start-parameter limit.
    Bot username is read from TELEGRAM_BOT_USERNAME env var; falls back to 'LobsterBot'.

    This is the canonical implementation — all callers must import from here.
    """
    payload_json = json.dumps({"a": action, "u": uow_id}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).rstrip(b"=").decode()
    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "LobsterBot")
    return f"https://t.me/{bot_username}?start={payload_b64}"
