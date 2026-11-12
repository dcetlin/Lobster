"""
Tests for src/utils/staleness_gate.py.

Covers:
- No prior record → file_changed returns True (first scan always runs)
- Identical content on second call → file_changed returns False (skip)
- Modified content → file_changed returns True (re-scan)
- Missing/corrupt store file → file_changed returns True (safe default)
- record_scan persists the hash so the next file_changed call sees it
- Unreadable target file → file_changed returns True (conservative fallback)
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path for all test environments.
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.staleness_gate import file_changed, record_scan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------------------------------------------------------------------------
# Tests — file_changed
# ---------------------------------------------------------------------------

class TestFileChanged:
    def test_no_prior_record_returns_true(self, tmp_path: Path) -> None:
        """First call with no store entry must return True so the first scan runs."""
        target = tmp_path / "file.md"
        _write(target, b"hello world")
        store = tmp_path / "staleness-records.json"

        assert file_changed(target, job_name="test-job", record_path=store) is True

    def test_identical_content_returns_false(self, tmp_path: Path) -> None:
        """After recording a scan, calling file_changed on unchanged content returns False."""
        target = tmp_path / "file.md"
        _write(target, b"content that doesn't change")
        store = tmp_path / "staleness-records.json"

        # Record the first scan
        record_scan(target, job_name="test-job", record_path=store)

        # Same bytes — should report unchanged
        assert file_changed(target, job_name="test-job", record_path=store) is False

    def test_modified_content_returns_true(self, tmp_path: Path) -> None:
        """After recording a scan, modifying the file makes file_changed return True."""
        target = tmp_path / "file.md"
        _write(target, b"original content")
        store = tmp_path / "staleness-records.json"

        record_scan(target, job_name="test-job", record_path=store)

        # Modify the file
        _write(target, b"changed content")

        assert file_changed(target, job_name="test-job", record_path=store) is True

    def test_missing_store_returns_true(self, tmp_path: Path) -> None:
        """When the store file does not exist, treat as 'no record' → changed."""
        target = tmp_path / "file.md"
        _write(target, b"some content")
        store = tmp_path / "nonexistent" / "store.json"  # parent dir doesn't exist

        assert file_changed(target, job_name="test-job", record_path=store) is True

    def test_corrupt_store_returns_true(self, tmp_path: Path) -> None:
        """A store file containing invalid JSON is treated as empty → changed."""
        target = tmp_path / "file.md"
        _write(target, b"some content")
        store = tmp_path / "staleness-records.json"
        store.write_text("NOT VALID JSON }{][")

        assert file_changed(target, job_name="test-job", record_path=store) is True

    def test_missing_target_file_returns_true(self, tmp_path: Path) -> None:
        """When the target file does not exist, return True (let the scorer handle it)."""
        target = tmp_path / "does-not-exist.md"
        store = tmp_path / "staleness-records.json"

        assert file_changed(target, job_name="test-job", record_path=store) is True

    def test_different_jobs_are_independent(self, tmp_path: Path) -> None:
        """Records for one job do not affect another job scanning the same file."""
        target = tmp_path / "file.md"
        _write(target, b"shared content")
        store = tmp_path / "staleness-records.json"

        record_scan(target, job_name="job-a", record_path=store)

        # job-b has no record yet — must see the file as changed
        assert file_changed(target, job_name="job-b", record_path=store) is True
        # job-a has a record — must see the file as unchanged
        assert file_changed(target, job_name="job-a", record_path=store) is False


# ---------------------------------------------------------------------------
# Tests — record_scan
# ---------------------------------------------------------------------------

class TestRecordScan:
    def test_record_scan_creates_store(self, tmp_path: Path) -> None:
        """record_scan creates the store file when it does not exist."""
        target = tmp_path / "file.md"
        _write(target, b"data")
        store = tmp_path / "staleness-records.json"

        assert not store.exists()
        record_scan(target, job_name="test-job", record_path=store)
        assert store.exists()

    def test_record_scan_persists_correct_hash(self, tmp_path: Path) -> None:
        """The stored hash matches SHA-256 of the file bytes."""
        target = tmp_path / "file.md"
        content = b"precise content"
        _write(target, content)
        store = tmp_path / "staleness-records.json"

        record_scan(target, job_name="test-job", record_path=store)

        data = json.loads(store.read_text())
        key = f"test-job::{target.resolve()}"
        expected = hashlib.sha256(content).hexdigest()
        assert data[key] == expected

    def test_record_scan_updates_existing_record(self, tmp_path: Path) -> None:
        """record_scan overwrites an existing hash with the new one."""
        target = tmp_path / "file.md"
        _write(target, b"v1")
        store = tmp_path / "staleness-records.json"

        record_scan(target, job_name="test-job", record_path=store)
        _write(target, b"v2")
        record_scan(target, job_name="test-job", record_path=store)

        data = json.loads(store.read_text())
        key = f"test-job::{target.resolve()}"
        expected = hashlib.sha256(b"v2").hexdigest()
        assert data[key] == expected

    def test_record_scan_on_missing_file_is_silent(self, tmp_path: Path) -> None:
        """record_scan on a non-existent file does not raise — swallows silently."""
        store = tmp_path / "staleness-records.json"
        # Should not raise
        record_scan(tmp_path / "missing.md", job_name="test-job", record_path=store)
