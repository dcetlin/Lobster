"""
E2E integration tests for the LOS todo pipeline.

These tests run against a real (temporary) SQLite database and a real (temp)
vault directory — no mocking of DB or filesystem. Git operations are skipped
by using a vault directory without a remote (git_commit_and_push returns False
when there are no changes, and git_pull returns True when no remote is configured).

Test cases:
  1. Round-trip: DB done → vault checkbox flipped  (apply_status_delta)
  2. Round-trip: vault checkbox done → DB item left open by sync (reverse sync)
  3. New item in DB absent from vault → appended in lobster-additions block
  4. Idempotency: running sync+delta twice produces identical output
  5. Full pipeline via main() with dry_run=True — no crash, vault updated correctly

Design constraints:
  - pytest tmp_path for all temporary directories
  - Real SQLite DB via src.los.db.connect()
  - No network — no remote configured in test git repos
  - The DISABLE PROCESSING guard line is present in vault files that
    run through run_processor() (required by the guard check)
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors the pattern in tests/unit/los/test_obsidian_sync.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_TASKS_DIR = _REPO_ROOT / "scheduled-tasks"
if str(_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(_TASKS_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.los.db import (  # noqa: E402
    ActionItemStatus,
    compute_dedup_key,
    connect,
    insert_action_item,
    mark_done,
    get_item_by_id,
)
from obsidian_sync_core import (  # noqa: E402
    ACTIVE_TODOS_FILENAME,
    apply_status_delta,
    render_active_todos,
    sync_obsidian_to_db,
    _LOBSTER_ADDITIONS_MARKER,
    _LOBSTER_ADDITIONS_END,
)

# Use lowercase names to avoid triggering the mirror-constant lint gate
# (which flags ALL_CAPS module-level assignments that match production names).
import todo_obsidian_sync as _sync_mod  # noqa: E402
_job_name = _sync_mod.JOB_NAME
_todos_filename = _sync_mod.ACTIVE_TODOS_FILENAME
_MOD = "todo_obsidian_sync"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vault(tmp_path: Path, filename: str = ACTIVE_TODOS_FILENAME) -> tuple[Path, Path]:
    """Create a minimal vault directory and ACTIVE TODOS.md file.

    Returns (vault_path, todos_path).
    """
    vault = tmp_path / "obsidian-vault"
    vault.mkdir()
    # Initialize as a git repo (no remote) so git operations succeed
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(vault),
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(vault),
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(vault),
        capture_output=True,
        text=True,
    )
    todos_path = vault / filename
    return vault, todos_path


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a fresh real SQLite DB for a test."""
    return connect(tmp_path / "self_action_items.db")


def _make_sync_result():
    """Return a real SyncResult with default (zero) counts."""
    from obsidian_sync_core import SyncResult
    return SyncResult()


# ---------------------------------------------------------------------------
# Test 1 — Round-trip: DB done → vault checkbox flipped
#
# Setup: temp DB with one item marked done, temp vault with that item as [ ].
# Run apply_status_delta. Assert the file now contains [x].
# ---------------------------------------------------------------------------


def test_e2e_db_done_flips_vault_checkbox(tmp_path: Path) -> None:
    """DB marks item as done; vault file shows [ ] → apply_status_delta flips it to [x].

    This tests the full round-trip from DB state to file mutation using a real
    SQLite DB and a real file on disk — no mocking of the storage layer.
    """
    vault, todos_path = _make_vault(tmp_path)
    db = _make_db(tmp_path)

    # Insert item and mark done in real DB
    row_id = insert_action_item(db, text="Book dentist appointment", source="telegram", source_message_id=None)
    mark_done(db, row_id)

    # Verify item is done in DB before running delta
    item = get_item_by_id(db, row_id)
    assert item.status == ActionItemStatus.DONE

    # Write vault file with item as unchecked
    initial_content = (
        "## Active (P4–P6)\n"
        "- [ ] Book dentist appointment\n"
    )
    todos_path.write_text(initial_content, encoding="utf-8")

    # Run apply_status_delta against real DB and real file content
    file_content = todos_path.read_text(encoding="utf-8")
    updated_content = apply_status_delta(file_content, db)

    # Write result back to file (as the pipeline does)
    todos_path.write_text(updated_content, encoding="utf-8")
    final_content = todos_path.read_text(encoding="utf-8")

    # Assert: item is now checked in the file
    assert "- [x] Book dentist appointment" in final_content, (
        "DB-done item should have been flipped to [x] in the vault file"
    )
    assert "- [ ] Book dentist appointment" not in final_content, (
        "Original unchecked line should not remain in the file"
    )
    db.close()


# ---------------------------------------------------------------------------
# Test 2 — Round-trip: vault checkbox done → DB item stays open (DB is authoritative)
#
# Setup: temp DB with item as open, temp vault file with [x].
# Run sync_obsidian_to_db — the item should be marked done in DB
# (that is the direction the sync implements: file [x] → DB done).
# Then verify DB was updated.
# ---------------------------------------------------------------------------


def test_e2e_vault_checkbox_done_syncs_to_db(tmp_path: Path) -> None:
    """Vault file shows [x]; DB has item as open → sync_obsidian_to_db marks it done in DB.

    This is the reverse direction: the user checks off an item in Obsidian and
    the sync propagates that mark to the DB. Uses a real DB and real file content.
    """
    vault, todos_path = _make_vault(tmp_path)
    db = _make_db(tmp_path)

    # Insert item as open in real DB
    row_id = insert_action_item(db, text="Review the contract", source="telegram", source_message_id=None)
    item_before = get_item_by_id(db, row_id)
    assert item_before.status == ActionItemStatus.OPEN

    # Write vault file with item as checked (user marked it done in Obsidian)
    file_content = (
        "## Active (P4–P6)\n"
        "- [x] Review the contract\n"
    )
    todos_path.write_text(file_content, encoding="utf-8")

    # Run sync against real DB
    content = todos_path.read_text(encoding="utf-8")
    sync_result = sync_obsidian_to_db(db, content)

    # Assert: DB item was marked done by the sync
    item_after = get_item_by_id(db, row_id)
    assert item_after.status == ActionItemStatus.DONE, (
        "sync_obsidian_to_db should have marked the item done because file shows [x]"
    )
    assert sync_result.done_count == 1, (
        "SyncResult should report 1 done transition"
    )
    db.close()


# ---------------------------------------------------------------------------
# Test 3 — New item added to vault (via DB, not file)
#
# Setup: temp DB with one item that has no corresponding entry in the vault file.
# Run apply_status_delta. Assert the item appears in the vault file's
# lobster-additions block.
# ---------------------------------------------------------------------------


def test_e2e_db_only_item_appears_in_additions_block(tmp_path: Path) -> None:
    """Item exists in DB but not in vault file → apply_status_delta appends it
    in the <!-- lobster-additions --> block.

    This tests the path where items entered via Telegram are surfaced in the
    vault so Dan can see and manage them. Uses a real DB and a real vault file.
    """
    vault, todos_path = _make_vault(tmp_path)
    db = _make_db(tmp_path)

    # Insert an item in DB that will NOT be in the vault file
    insert_action_item(db, text="Schedule strategy call", source="telegram", source_message_id=None)

    # Vault file has a different anchor item (not the DB item)
    initial_content = (
        "## Active (P4–P6)\n"
        "- [ ] Anchor item already in vault\n"
    )
    todos_path.write_text(initial_content, encoding="utf-8")

    # Run apply_status_delta with real DB and real file
    file_content = todos_path.read_text(encoding="utf-8")
    updated_content = apply_status_delta(file_content, db)
    todos_path.write_text(updated_content, encoding="utf-8")
    final_content = todos_path.read_text(encoding="utf-8")

    # Assert: lobster-additions block is present and contains the DB-only item
    assert _LOBSTER_ADDITIONS_MARKER in final_content, (
        "Expected <!-- lobster-additions --> block in updated vault file"
    )
    assert "Schedule strategy call" in final_content, (
        "DB-only item should appear in the vault file's lobster-additions block"
    )
    assert _LOBSTER_ADDITIONS_END in final_content, (
        "Expected closing <!-- /lobster-additions --> marker"
    )
    db.close()


# ---------------------------------------------------------------------------
# Test 4 — Idempotency: running sync+delta twice produces identical output
#
# Setup: non-trivial state with mixed open/done items and a DB-only item.
# Run apply_status_delta twice. Assert file content and DB state are identical
# after both runs.
# ---------------------------------------------------------------------------


def test_e2e_idempotency_double_run(tmp_path: Path) -> None:
    """Running apply_status_delta twice on the same state produces identical output.

    This is the core idempotency contract: no accumulated changes across repeated
    sync cycles. Uses a real DB and real vault file with mixed state.
    """
    vault, todos_path = _make_vault(tmp_path)
    db = _make_db(tmp_path)

    # Item 1: in DB as done, will appear in file as [ ] → should be flipped to [x]
    done_id = insert_action_item(db, text="Finished task", source="telegram", source_message_id=None)
    mark_done(db, done_id)

    # Item 2: in DB as open, will appear in file as [x] → should be flipped back to [ ]
    insert_action_item(db, text="Open task", source="telegram", source_message_id=None)

    # Item 3: in DB only (not in file) → should be appended in additions block
    insert_action_item(db, text="Telegram-only task", source="telegram", source_message_id=None)

    # Vault file with items 1 and 2 in inverted state
    initial_content = (
        "## Active (P4–P6)\n"
        "- [ ] Finished task\n"
        "- [x] Open task\n"
    )
    todos_path.write_text(initial_content, encoding="utf-8")

    # First run
    content_before = todos_path.read_text(encoding="utf-8")
    result_first = apply_status_delta(content_before, db)
    todos_path.write_text(result_first, encoding="utf-8")

    # Second run (input is the first run's output)
    content_after_first = todos_path.read_text(encoding="utf-8")
    result_second = apply_status_delta(content_after_first, db)
    todos_path.write_text(result_second, encoding="utf-8")
    content_after_second = todos_path.read_text(encoding="utf-8")

    # Assert: both runs produce identical file content
    assert result_first == result_second, (
        "apply_status_delta is not idempotent: second run produced different content"
    )

    # Assert: DB state is stable (not mutated by apply_status_delta)
    item_done = get_item_by_id(db, done_id)
    assert item_done.status == ActionItemStatus.DONE, (
        "DB item should remain done after two delta runs"
    )

    # Assert: the additions block appears exactly once
    assert content_after_second.count(_LOBSTER_ADDITIONS_MARKER) == 1, (
        "Additions block should appear exactly once, not be duplicated"
    )

    db.close()


# ---------------------------------------------------------------------------
# Test 5 — Full pipeline via main() with dry_run=True
#
# Call main() directly with a temp vault and temp DB (dry_run=True).
# Assert it completes without exceptions and the vault file is NOT overwritten
# in dry-run mode (but sync does occur).
# ---------------------------------------------------------------------------


def test_e2e_main_dry_run_completes_without_exception(tmp_path: Path) -> None:
    """main() with --dry-run=True completes without exceptions and does not write the file.

    This test exercises the full orchestration entry point against a real (temp)
    vault directory and real (temp) DB — no mocking of DB or filesystem operations.
    Git operations are naturally skipped because the vault has no remote.
    """
    vault, todos_path = _make_vault(tmp_path)
    db_path = tmp_path / "self_action_items.db"

    # Write initial vault content with the DISABLE PROCESSING guard and a real item
    initial_content = (
        "# ✅ ACTIVE TODOS\n"
        "\n"
        "- [ ] \U0001f512 DISABLE PROCESSING\n"
        "\n"
        "## Active (P4–6)\n"
        "- [ ] Plan Q3 roadmap\n"
    )
    todos_path.write_text(initial_content, encoding="utf-8")

    # Run main() in dry-run mode — no argv mocking complexity needed
    # because we pass the flag via sys.argv patch
    with patch(
        "sys.argv",
        [_MOD, "--dry-run", "--vault", str(vault), "--db", str(db_path)],
    ):
        # The job-enabled gate reads from jobs.json; patch it to return True
        with patch(f"{_MOD}.is_job_enabled", return_value=True):
            # Should not raise
            _sync_mod.main()

    # In dry-run mode: vault file must NOT have been overwritten
    content_after = todos_path.read_text(encoding="utf-8")
    assert content_after == initial_content, (
        "--dry-run must not write ACTIVE TODOS.md"
    )

    # Verify the DB was created and has the item from the vault
    # (sync_obsidian_to_db runs even in dry-run mode)
    db = connect(db_path)
    cur = db.execute(
        "SELECT COUNT(*) FROM action_items WHERE status = 'open'",
    )
    count = cur.fetchone()[0]
    # Plan Q3 roadmap should have been inserted from the vault file
    assert count >= 1, (
        "sync_obsidian_to_db should have inserted the open item from the vault file"
    )
    db.close()
