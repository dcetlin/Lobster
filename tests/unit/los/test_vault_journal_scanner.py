"""
Tests for vault-journal-scanner.py

Tests are written against behaviors derived from the spec (issue #1285):
- Unchecked checkbox items in journal files are extracted
- Checked items, ACTIVE TODOS.md, and excluded directories are skipped
- Files not changed since last scan are skipped (state tracking)
- State is loaded and saved correctly
- Dedup via src.los.db ensures the same item from multiple sources is not doubled
- dry-run mode logs but does not write to DB or update state

No external services, no API calls, no Telegram output.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.los.db import connect, get_open_items


# Import the scanner module — path manipulation in the script itself ensures
# this works when the test is run from the repo root via pytest.
import importlib.util
import sys

_SCANNER_PATH = Path(__file__).parent.parent.parent.parent / "scheduled-tasks" / "vault-journal-scanner.py"


def _load_scanner():
    """Dynamically load vault-journal-scanner as a module."""
    spec = importlib.util.spec_from_file_location("vault_journal_scanner", _SCANNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scanner = _load_scanner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "self_action_items.db"


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "scanner-state.json"


# ---------------------------------------------------------------------------
# extract_checkboxes — pure function, no I/O
# ---------------------------------------------------------------------------


class TestExtractCheckboxes:
    def test_finds_unchecked_items(self):
        content = "- [ ] Call Sarah\n- [ ] Buy milk\n"
        result = scanner.extract_checkboxes(content)
        assert result == ["Call Sarah", "Buy milk"]

    def test_skips_checked_items(self):
        content = "- [x] Done thing\n- [X] Also done\n"
        result = scanner.extract_checkboxes(content)
        assert result == []

    def test_skips_partially_checked_variants(self):
        # Only standard - [ ] pattern is extracted; everything else is skipped
        content = "- [-] In progress\n- [/] Half done\n"
        result = scanner.extract_checkboxes(content)
        assert result == []

    def test_handles_nested_items_with_leading_whitespace(self):
        content = "\t- [ ] Nested item\n  - [ ] Another nested\n"
        result = scanner.extract_checkboxes(content)
        assert result == ["Nested item", "Another nested"]

    def test_returns_empty_for_no_todos(self):
        content = "# Just a journal entry\n\nSome prose text.\n"
        assert scanner.extract_checkboxes(content) == []

    def test_strips_trailing_whitespace_from_item_text(self):
        content = "- [ ]   Lots of leading spaces   \n"
        result = scanner.extract_checkboxes(content)
        assert result == ["Lots of leading spaces"]

    def test_mixed_content(self):
        content = (
            "# Journal\n\n"
            "Some thoughts.\n\n"
            "- [ ] Action item 1\n"
            "- [x] Already done\n"
            "- [ ] Action item 2\n"
            "\nMore prose.\n"
        )
        result = scanner.extract_checkboxes(content)
        assert result == ["Action item 1", "Action item 2"]


# ---------------------------------------------------------------------------
# collect_journal_files — pure scanner over directory tree
# ---------------------------------------------------------------------------


class TestCollectJournalFiles:
    def test_finds_md_files_in_vault(self, vault_path):
        (vault_path / "journal-1.md").write_text("entry")
        (vault_path / "journal-2.md").write_text("entry")
        files = scanner.collect_journal_files(vault_path)
        names = {f.name for f in files}
        assert "journal-1.md" in names
        assert "journal-2.md" in names

    def test_excludes_active_todos(self, vault_path):
        (vault_path / "✅ ACTIVE TODOS.md").write_text("- [ ] Something")
        (vault_path / "journal.md").write_text("- [ ] Real item")
        files = scanner.collect_journal_files(vault_path)
        names = {f.name for f in files}
        assert "✅ ACTIVE TODOS.md" not in names
        assert "journal.md" in names

    def test_excludes_obsidian_directory(self, vault_path):
        obsidian_dir = vault_path / ".obsidian"
        obsidian_dir.mkdir()
        (obsidian_dir / "config.md").write_text("# config")
        (vault_path / "journal.md").write_text("entry")
        files = scanner.collect_journal_files(vault_path)
        paths = {str(f) for f in files}
        assert not any(".obsidian" in p for p in paths)

    def test_excludes_git_directory(self, vault_path):
        git_dir = vault_path / ".git"
        git_dir.mkdir()
        (git_dir / "COMMIT_EDITMSG").write_text("msg")
        (vault_path / "journal.md").write_text("entry")
        files = scanner.collect_journal_files(vault_path)
        paths = {str(f) for f in files}
        assert not any(".git" in p for p in paths)

    def test_finds_nested_journal_files(self, vault_path):
        week_dir = vault_path / "Journal Entries" / "2026" / "Week 1"
        week_dir.mkdir(parents=True)
        (week_dir / "entry.md").write_text("entry")
        files = scanner.collect_journal_files(vault_path)
        assert any("entry.md" == f.name for f in files)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


class TestLoadScannerState:
    def test_returns_empty_dict_when_file_missing(self, state_path):
        result = scanner.load_scanner_state(state_path)
        assert result == {}

    def test_loads_valid_state(self, state_path):
        state = {"journal/entry.md": "2026-05-01T00:00:00+00:00"}
        state_path.write_text(json.dumps(state))
        result = scanner.load_scanner_state(state_path)
        assert result == state

    def test_returns_empty_on_malformed_json(self, state_path):
        state_path.write_text("not json{{{")
        result = scanner.load_scanner_state(state_path)
        assert result == {}

    def test_filters_non_string_values(self, state_path):
        state = {"journal.md": "2026-01-01T00:00Z", "bad": 12345}
        state_path.write_text(json.dumps(state))
        result = scanner.load_scanner_state(state_path)
        assert "journal.md" in result
        assert "bad" not in result


class TestSaveScannerState:
    def test_saves_and_loads_roundtrip(self, state_path):
        state = {"a.md": "2026-01-01T00:00:00Z", "b.md": "2026-02-01T00:00:00Z"}
        scanner.save_scanner_state(state_path, state)
        assert state_path.exists()
        loaded = scanner.load_scanner_state(state_path)
        assert loaded == state

    def test_creates_parent_directories(self, tmp_path):
        nested_path = tmp_path / "deep" / "nested" / "state.json"
        scanner.save_scanner_state(nested_path, {"x.md": "2026-01-01T00:00:00Z"})
        assert nested_path.exists()


# ---------------------------------------------------------------------------
# _is_file_changed
# ---------------------------------------------------------------------------


class TestIsFileChanged:
    def test_file_with_no_last_scan_is_always_changed(self, tmp_path):
        f = tmp_path / "new.md"
        f.write_text("content")
        assert scanner._is_file_changed(f, None) is True

    def test_file_modified_after_last_scan_is_changed(self, tmp_path):
        f = tmp_path / "modified.md"
        f.write_text("content")
        # Set last_scanned to 10 minutes in the past relative to file mtime
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        past_iso = (mtime - timedelta(minutes=10)).isoformat()
        assert scanner._is_file_changed(f, past_iso) is True

    def test_file_not_modified_since_scan_is_unchanged(self, tmp_path):
        f = tmp_path / "unchanged.md"
        f.write_text("content")
        # Set last_scanned to 10 minutes AFTER file mtime
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        future_iso = (mtime + timedelta(minutes=10)).isoformat()
        assert scanner._is_file_changed(f, future_iso) is False


# ---------------------------------------------------------------------------
# scan_vault — integration-style tests using a real in-memory DB
# ---------------------------------------------------------------------------


class TestScanVault:
    def test_extracts_items_from_new_journal_file(self, vault_path, db_path):
        journal = vault_path / "122 (5.15.26).md"
        journal.write_text("- [ ] Call Sarah\n- [ ] Buy milk\n")

        files_scanned, items_extracted, new_state = scanner.scan_vault(
            vault_path=vault_path,
            db_path=db_path,
            state={},
            dry_run=False,
        )

        assert files_scanned == 1
        assert items_extracted == 2

        conn = connect(db_path)
        items = get_open_items(conn)
        conn.close()
        texts = {i.text for i in items}
        assert "Call Sarah" in texts
        assert "Buy milk" in texts

    def test_skips_file_not_changed_since_last_scan(self, vault_path, db_path):
        journal = vault_path / "old.md"
        journal.write_text("- [ ] Old item\n")
        # Set last_scanned AFTER file mtime so file appears unchanged
        mtime = datetime.fromtimestamp(journal.stat().st_mtime, tz=timezone.utc)
        future_iso = (mtime + timedelta(minutes=10)).isoformat()
        state = {scanner._file_key(vault_path, journal): future_iso}

        files_scanned, items_extracted, new_state = scanner.scan_vault(
            vault_path=vault_path,
            db_path=db_path,
            state=state,
            dry_run=False,
        )

        assert files_scanned == 0
        assert items_extracted == 0

    def test_does_not_write_on_dry_run(self, vault_path, db_path):
        journal = vault_path / "dry.md"
        journal.write_text("- [ ] Should not be saved\n")

        files_scanned, items_extracted, new_state = scanner.scan_vault(
            vault_path=vault_path,
            db_path=db_path,
            state={},
            dry_run=True,
        )

        # items_extracted is still populated so caller can see what would be extracted
        assert items_extracted == 1
        # But the DB was not written (db_path may not even exist)
        if db_path.exists():
            conn = connect(db_path)
            items = get_open_items(conn)
            conn.close()
            assert len(items) == 0

    def test_dedup_prevents_double_insertion(self, vault_path, db_path):
        """Same text in two different files should only produce one DB row."""
        j1 = vault_path / "entry1.md"
        j2 = vault_path / "entry2.md"
        j1.write_text("- [ ] Pack for Boston trip\n")
        j2.write_text("- [ ] Pack for Boston trip\n")

        files_scanned, items_extracted, _ = scanner.scan_vault(
            vault_path=vault_path,
            db_path=db_path,
            state={},
            dry_run=False,
        )

        conn = connect(db_path)
        items = get_open_items(conn)
        conn.close()
        # Only one open row despite two files having the same text
        matching = [i for i in items if i.text == "Pack for Boston trip"]
        assert len(matching) == 1

    def test_excludes_active_todos_file(self, vault_path, db_path):
        todos_file = vault_path / "✅ ACTIVE TODOS.md"
        todos_file.write_text("- [ ] Should be excluded\n")
        journal = vault_path / "journal.md"
        journal.write_text("- [ ] Real journal item\n")

        _, items_extracted, _ = scanner.scan_vault(
            vault_path=vault_path,
            db_path=db_path,
            state={},
            dry_run=False,
        )

        conn = connect(db_path)
        items = get_open_items(conn)
        conn.close()
        texts = {i.text for i in items}
        assert "Should be excluded" not in texts
        assert "Real journal item" in texts

    def test_updates_state_for_scanned_files(self, vault_path, db_path):
        journal = vault_path / "new-entry.md"
        journal.write_text("- [ ] Track me\n")

        _, _, new_state = scanner.scan_vault(
            vault_path=vault_path,
            db_path=db_path,
            state={},
            dry_run=False,
        )

        key = scanner._file_key(vault_path, journal)
        assert key in new_state

    def test_handles_file_with_no_checkboxes(self, vault_path, db_path):
        journal = vault_path / "prose.md"
        journal.write_text("Just some prose. No todos here.\n")

        files_scanned, items_extracted, new_state = scanner.scan_vault(
            vault_path=vault_path,
            db_path=db_path,
            state={},
            dry_run=False,
        )

        # File with no todos still gets marked as scanned (mtime tracking)
        assert items_extracted == 0
        key = scanner._file_key(vault_path, journal)
        assert key in new_state


# ---------------------------------------------------------------------------
# _source_name — pure function
# ---------------------------------------------------------------------------


class TestSourceName:
    def test_produces_journal_prefixed_source(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        f = vault / "122 (5.15.26).md"
        f.touch()
        source = scanner._source_name(vault, f)
        assert source.startswith("journal:")

    def test_stem_is_slugified(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        f = vault / "Journal Entries" / "2026" / "Week 19.md"
        f.parent.mkdir(parents=True)
        f.touch()
        source = scanner._source_name(vault, f)
        # Should be lowercase with hyphens, no spaces or dots
        slug = source.removeprefix("journal:")
        assert " " not in slug
        assert slug == slug.lower()
