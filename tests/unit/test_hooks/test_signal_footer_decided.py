"""
Unit tests for ⚖️ decided signal routing in hooks/signal-footer-check.py.

Tests cover:
- Messages with no side-effects block pass without routing
- Side-effects blocks without 'decided' pass without writing anything
- 'decided' with emoji routes to decisions table in DB
- 'decided' without emoji also triggers routing
- Routing appends a dated entry to decisions-ledger.md
- Bare 'decided' with no description falls back to 'decision reached'
- DB write failure does not block the send_reply (exit 0)
- Non-send_reply tool calls are skipped
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "signal-footer-check.py"


def _run(
    tool_input: dict,
    tool_name: str = "mcp__lobster-inbox__send_reply",
    env_overrides: dict | None = None,
) -> tuple[int, str, str]:
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def _make_text(side_effects_content: str) -> str:
    return f"Here is my reply.\n\n```side-effects:\n{side_effects_content}\n```"


class TestDecidedSignalDetection:

    def test_no_side_effects_block_passes(self):
        """Messages with no side-effects block pass without routing."""
        rc, _, _ = _run({"chat_id": 6036, "text": "Just a message, no footer."})
        assert rc == 0

    def test_side_effects_without_decided_passes(self):
        """A side-effects block that has no 'decided' line is allowed without writing anything."""
        text = _make_text("🤖 spawned  some-agent\n✅ done     task complete\n")
        rc, _, _ = _run({"chat_id": 6036, "text": text})
        assert rc == 0

    def test_decided_line_routes_to_db(self, tmp_path):
        """A side-effects block with '⚖️ decided <desc>' writes to decisions table."""
        db_path = tmp_path / "memory.db"
        ledger_path = tmp_path / "decisions-ledger.md"
        ledger_path.write_text("")

        text = _make_text("⚖️ decided  use OAuth for authentication\n")
        rc, _, _ = _run(
            {"chat_id": 6036, "text": text},
            env_overrides={
                "DECIDED_DB_PATH": str(db_path),
                "DECIDED_LEDGER_PATH": str(ledger_path),
            },
        )
        assert rc == 0

        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT summary, source, category FROM decisions").fetchall()
        assert len(rows) == 1
        assert "use OAuth for authentication" in rows[0][0]
        assert rows[0][1] == "decided-signal"
        assert rows[0][2] == "decision"

    def test_decided_line_without_emoji_routes_to_db(self, tmp_path):
        """'decided <desc>' without the emoji also triggers routing."""
        db_path = tmp_path / "memory.db"
        ledger_path = tmp_path / "decisions-ledger.md"
        ledger_path.write_text("")

        text = _make_text("decided  use Redis for caching\n")
        rc, _, _ = _run(
            {"chat_id": 6036, "text": text},
            env_overrides={
                "DECIDED_DB_PATH": str(db_path),
                "DECIDED_LEDGER_PATH": str(ledger_path),
            },
        )
        assert rc == 0

        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT summary FROM decisions").fetchall()
        assert len(rows) == 1
        assert "use Redis for caching" in rows[0][0]

    def test_decided_line_appends_to_ledger(self, tmp_path):
        """Routing appends a dated markdown entry to decisions-ledger.md."""
        db_path = tmp_path / "memory.db"
        ledger_path = tmp_path / "decisions-ledger.md"
        ledger_path.write_text("# Decisions\n")

        text = _make_text("⚖️ decided  ship the feature as-is\n")
        rc, _, _ = _run(
            {"chat_id": 6036, "text": text},
            env_overrides={
                "DECIDED_DB_PATH": str(db_path),
                "DECIDED_LEDGER_PATH": str(ledger_path),
            },
        )
        assert rc == 0

        content = ledger_path.read_text()
        assert "ship the feature as-is" in content
        assert "---" in content

    def test_decided_with_no_description_uses_fallback(self, tmp_path):
        """A bare 'decided' line with no description writes 'decision reached' to DB."""
        db_path = tmp_path / "memory.db"
        ledger_path = tmp_path / "decisions-ledger.md"
        ledger_path.write_text("")

        text = _make_text("⚖️ decided\n")
        rc, _, _ = _run(
            {"chat_id": 6036, "text": text},
            env_overrides={
                "DECIDED_DB_PATH": str(db_path),
                "DECIDED_LEDGER_PATH": str(ledger_path),
            },
        )
        assert rc == 0

        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT summary FROM decisions").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "decision reached"

    def test_routing_failure_does_not_block_send(self, tmp_path):
        """If the DB path is unwritable, the hook still exits 0 (does not block send_reply)."""
        ledger_path = tmp_path / "decisions-ledger.md"
        ledger_path.write_text("")

        text = _make_text("⚖️ decided  proceed with plan B\n")
        rc, _, _ = _run(
            {"chat_id": 6036, "text": text},
            env_overrides={
                "DECIDED_DB_PATH": "/nonexistent/path/memory.db",
                "DECIDED_LEDGER_PATH": str(ledger_path),
            },
        )
        assert rc == 0

    def test_non_send_reply_tool_is_skipped(self):
        """The hook ignores non-send_reply tool calls."""
        text = _make_text("⚖️ decided  something important\n")
        rc, _, _ = _run(
            {"chat_id": 6036, "text": text},
            tool_name="mcp__lobster-inbox__mark_processed",
        )
        assert rc == 0
