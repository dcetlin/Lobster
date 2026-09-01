#!/usr/bin/env python3
"""One-time ET→PT timezone transition. Scheduled to run 2026-05-29T12:00Z.
"""

import os
import sys
import re

sys.path.insert(0, os.path.expanduser("~/lobster"))

from src.utils.jobs import is_job_enabled

JOB_NAME = "tz-transition-et-to-pt"


def main():
    if not is_job_enabled(JOB_NAME):
        print(f"[{JOB_NAME}] disabled, skipping")
        return

    # Update owner.toml
    owner_toml = os.path.expanduser("~/lobster-config/owner.toml")
    text = open(owner_toml).read()
    # Remove the "relocating May 29" comment line
    text = re.sub(r"# Current: ET \(relocating.*?\)\n", "", text)
    # Change the timezone value
    text = text.replace('timezone = "America/New_York"', 'timezone = "America/Los_Angeles"')
    open(owner_toml, "w").write(text)
    print(f"[{JOB_NAME}] Updated owner.toml: America/New_York → America/Los_Angeles")

    # Update user.base.bootup.md
    bootup = os.path.expanduser(
        "~/lobster-user-config/agents/user.base.bootup.md"
    )
    text = open(bootup).read()

    # Remove the transition note paragraph (the **Note:** line)
    text = re.sub(
        r"\n\*\*Note:\*\* Dan is on the East Coast until ~2026-05-29\..*?Pacific\.\n",
        "\n",
        text,
        flags=re.DOTALL,
    )
    # Update the morning window reference
    text = text.replace(
        "morning window (6-10 AM Eastern)",
        "morning window (6-10 AM Pacific)",
    )
    open(bootup, "w").write(text)
    print(f"[{JOB_NAME}] Updated user.base.bootup.md: ET → PT")

    # Send Telegram confirmation
    try:
        import requests
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586")
        if token:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": int(chat_id),
                    "text": "Timezone updated: East Coast → Pacific (America/Los_Angeles). Morning delivery window is now 6–10 AM PT.",
                },
                timeout=10,
            )
    except Exception as e:
        print(f"[{JOB_NAME}] Telegram notify failed (non-fatal): {e}")

    print(f"[{JOB_NAME}] Transition complete.")


if __name__ == "__main__":
    main()
