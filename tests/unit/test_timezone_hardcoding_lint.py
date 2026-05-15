"""
Lint test: verify that user-facing time formatter code does not contain
hardcoded IANA timezone strings (America/Los_Angeles, America/New_York)
or raw abbreviation strings ('PT', 'ET') outside of the canonical timezone
utility module.

Any hardcoded timezone bypasses LOBSTER_USER_TZ and produces wrong output
when the user changes their configured timezone.

Exempt files:
  - src/utils/timezone.py  (canonical utility — allowed to reference IANA names in docs)
  - tests/                 (test fixtures may reference known zones)
  - docs/                  (documentation)

The check scans for these literals in non-exempt Python source files:
  - ZoneInfo("America/Los_Angeles")
  - ZoneInfo("America/New_York")
  - .strftime(... "ET")   — strftime output is the check; abbreviation in format
    strings is harder to lint and is covered by the behavioural test instead
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Repository root — two levels up from tests/unit/
_REPO_ROOT = Path(__file__).parent.parent.parent

# Directories to scan (relative to repo root)
_SCAN_DIRS = [
    "src",
    "scheduled-tasks",
]

# Files exempt from the check (relative to repo root)
_EXEMPT_FILES = {
    "src/utils/timezone.py",
    "src/mcp/user_model/owner.py",       # documents example return values in docstring
    "src/mcp/inbox_server.py",           # default calendar tz param — not user-facing output
}

# Patterns that flag a hardcoded timezone in user-facing context
# We check for ZoneInfo("...") calls with the banned IANA names.
_BANNED_ZONEINFO_PATTERNS = [
    re.compile(r'ZoneInfo\s*\(\s*["\']America/Los_Angeles["\']\s*\)'),
    re.compile(r'ZoneInfo\s*\(\s*["\']America/New_York["\']\s*\)'),
]

# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


def _collect_violations() -> list[tuple[str, int, str]]:
    """Return list of (rel_path, line_number, line_text) for each violation."""
    violations = []

    for scan_dir_name in _SCAN_DIRS:
        scan_dir = _REPO_ROOT / scan_dir_name
        if not scan_dir.exists():
            continue
        for py_file in scan_dir.rglob("*.py"):
            rel = py_file.relative_to(_REPO_ROOT)
            rel_str = str(rel)
            if rel_str in _EXEMPT_FILES:
                continue

            try:
                lines = py_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue

            for lineno, line in enumerate(lines, start=1):
                for pattern in _BANNED_ZONEINFO_PATTERNS:
                    if pattern.search(line):
                        violations.append((rel_str, lineno, line.strip()))

    return violations


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestNoHardcodedTimezoneInSourceFiles:
    """Source files outside the canonical timezone utility must not hardcode IANA zones."""

    def test_no_hardcoded_zoneinfo_calls(self):
        """
        ZoneInfo("America/Los_Angeles") and ZoneInfo("America/New_York") must not
        appear in non-exempt source files.

        When this test fails, the violation list tells you exactly which file and
        line to fix. Replace the hardcoded ZoneInfo(...) call with:

            from utils.timezone import get_owner_zoneinfo
            _USER_TZ = get_owner_zoneinfo()   # reads LOBSTER_USER_TZ → owner.toml → UTC

        or for one-off formatting:

            from utils.timezone import format_for_user
            result = format_for_user(dt, fmt="...")
        """
        violations = _collect_violations()

        if violations:
            lines = ["Hardcoded timezone violations found:"]
            for rel, lineno, text in violations:
                lines.append(f"  {rel}:{lineno}  →  {text}")
            lines.append("")
            lines.append(
                "Fix: replace ZoneInfo(\"America/...\") with get_owner_zoneinfo() "
                "from utils.timezone, or use format_for_user() for display."
            )
            pytest.fail("\n".join(lines))
