#!/usr/bin/env python3
"""
wos_active_summary.py — CLI entry point for get_active_summary.

Usage:
    uv run scripts/wos_active_summary.py [DB_PATH]

Prints a JSON array of non-terminal UoWs to stdout. Reads from local SQLite
DB only — no gh CLI or GitHub API calls. If the registry is empty, prints [].

Called by scripts/wos-status.sh (the dispatcher-facing entry point).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow importing from src/ regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from orchestration.classify_intake import get_active_summary


def main() -> None:
    db_path_str: str
    if len(sys.argv) > 1:
        db_path_str = sys.argv[1]
    else:
        db_path_str = os.environ.get(
            "REGISTRY_DB_PATH",
            str(Path.home() / "lobster-workspace" / "data" / "registry.db"),
        )

    db_path = Path(db_path_str).expanduser()

    if not db_path.exists():
        # Registry absent — return empty summary rather than failing.
        print("[]")
        return

    summary = get_active_summary(db_path)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
