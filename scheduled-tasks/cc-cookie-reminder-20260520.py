#!/usr/bin/env python3
"""
One-shot CC session cookie reminder for 2026-05-20 at 14:00 UTC.

Sends Dan a Telegram message reminding him to grab the CC session cookie
from claude.ai/settings/usage and paste it to Lobster.

After firing, disable this job via:
    update_scheduled_job("cc-cookie-reminder-20260520", enabled=False)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.inbox_write import write_inbox_message  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("cc-cookie-reminder-20260520")

JOB_NAME = "cc-cookie-reminder-20260520"
CHAT_ID = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))

MESSAGE = (
    "Reminder: your CC session cookie expired. To refresh it:\n\n"
    "1. Go to claude.ai/settings/usage\n"
    "2. Open DevTools → Network tab\n"
    "3. Reload the page\n"
    "4. Find any request with a Cookie: header\n"
    "5. Copy the sessionKey=sk-ant-sid02-... value\n"
    "6. Paste it here and I'll write it to ~/lobster-user-config/cc-usage-session-cookie"
)


def main() -> int:
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    msg_id = write_inbox_message(JOB_NAME, CHAT_ID, MESSAGE, timestamp)
    log.info("CC cookie reminder queued (msg_id=%s)", msg_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
