# LOS Test Coverage Gaps

The todo pipeline has solid unit coverage for extraction, schema, subtasks, and the vault-watcher detection path. The critical gap is `apply_status_delta` in `obsidian_sync_core.py` — the in-place checkpoint-flipping function has zero test coverage despite being the core write path used by `vault-processor.py`. Secondary gaps are the `main()` orchestration in `todo_obsidian_sync.py`, `git_commit_and_push`, and the absence of any integration test that exercises the full pipeline end-to-end.

The scopes below are ordered by priority and designed to be independently mergeable.

---

## PR 1 — `apply_status_delta` unit tests (critical path)

**Suggested PR title:** `test(los): unit tests for apply_status_delta in obsidian_sync_core`

**Target test file:** `tests/unit/los/test_obsidian_sync.py` (extend existing file)

**What it covers:** Every branch of the in-place checkbox-flip logic in `obsidian_sync_core.apply_status_delta()`. This is the write path executed on every vault-processor run — currently zero test coverage.

**Specific test cases to add:**

1. DB says done, file shows `[ ]` → line flipped to `[x]`
2. DB says open, file shows `[x]` → line flipped back to `[ ]`
3. DB says snoozed, file shows `[x]` → line flipped back to `[ ]`
4. Items in DB not present in file → appended in `<!-- lobster-additions -->` block
5. Existing `<!-- lobster-additions -->` block replaced on second run (not duplicated)
6. `# DISABLE PROCESSING` guard line present → file written unchanged (function exits early)
7. Attribution line appended in additions block alongside the item
8. Subtask items (parent_id IS NOT NULL) excluded from additions block
9. Snoozed item in DB where file shows `[ ]` → left unchecked (no flip to `[x]`)

**Estimated complexity:** L — this is the biggest test gap; requires setting up tmp files with realistic vault content and a mock DB fixture.

---

## PR 2 — `main()` orchestration tests for `todo_obsidian_sync.py`

**Suggested PR title:** `test(los): orchestration tests for todo_obsidian_sync main()`

**Target test file:** `tests/unit/los/test_obsidian_sync.py` (extend) or new `tests/unit/los/test_todo_obsidian_sync.py`

**What it covers:** The top-level control flow in `todo_obsidian_sync.main()`, which is currently entirely untested.

**Specific test cases to add:**

1. `--dry-run` flag passed → sync logic runs, no file writes occur, log output confirms dry-run mode
2. `_is_job_enabled` returns False → function exits before doing any DB or file work
3. Lock already held at `main()` entry → function exits without double-running
4. `git_pull` returns `pull_ok=False` → commit step skipped, function returns without error
5. Vault directory does not exist → handled gracefully (logged, no crash, no partial write)

**Estimated complexity:** M — requires mocking the job-enabled gate, file lock, git pull, and vault path; no new fixtures needed beyond what PR 1 would establish.

---

## PR 3 — `git_commit_and_push` unit tests

**Suggested PR title:** `test(los): unit tests for git_commit_and_push in obsidian_sync_core`

**Target test file:** `tests/unit/los/test_obsidian_sync.py` (extend existing file)

**What it covers:** The git commit-and-push path. `git_pull` has one test; `git_commit_and_push` has none.

**Specific test cases to add:**

1. No staged changes → function returns without calling `git commit`
2. Changes staged → `git commit` and `git push` called with expected arguments
3. `git commit` succeeds, `git push` fails → error surfaced, no silent failure
4. Commit message includes expected attribution/timestamp pattern
5. Called with `dry_run=True` → git commands not executed

**Estimated complexity:** S — straightforward subprocess-mock pattern; `git_pull` test is the reference implementation.

---

## PR 4 — Integration / E2E tests (full pipeline with tmp vault)

**Suggested PR title:** `test(los): integration tests for full sync/render/commit pipeline`

**Target test file:** `tests/integration/los/test_pipeline_e2e.py` (new file, new directory)

**What it covers:** End-to-end execution against a temporary vault directory — verifying that the component chain produces correct output without mocking the internal seams.

**Specific test cases to add:**

1. File read → sync to DB → render → write back: output file matches expected checkbox state for a known input
2. `vault-processor.py run_processor()` executed against a tmp vault directory with staged changes → DB updated, file written, no crash
3. Two consecutive runs against same vault state → idempotent output (no duplicated additions block, no spurious flips)
4. `vault-watcher.py` → `vault-processor.py` subprocess invocation chain: watcher detects a change in tmp vault, invokes processor, processor completes without error
5. Full round-trip: open item in DB → rendered as `[ ]` in file → file manually flipped to `[x]` → sync reads flip → DB updated to done

**Estimated complexity:** L — requires building a tmp vault fixture, wiring the subprocess chain, and carefully isolating from the real vault and DB. Likely needs a `conftest.py` in the new `tests/integration/los/` directory.

---

## PR 5 (optional / low priority) — Direct tests for private helper functions

**Suggested PR title:** `test(los): direct unit tests for _priority_for_section, _parse_item_text, _is_in_same_priority_band`

**Target test file:** `tests/unit/los/test_obsidian_sync.py` (extend existing file)

**What it covers:** Three private helpers that currently have only indirect coverage through higher-level tests. These are low-risk because the indirect coverage catches regressions, but direct tests would make failure messages more diagnostic.

**Specific test cases to add:**

1. `_priority_for_section`: each known section header → expected priority value; unknown header → default
2. `_parse_item_text`: item with subtask marker → parsed correctly; item with attribution comment → attribution stripped or preserved per contract
3. `_is_in_same_priority_band`: items with same priority → True; items spanning a section boundary → False

**Estimated complexity:** S — pure function tests, no fixtures required.

---

## Summary

| PR | Scope | File(s) | Complexity |
|----|-------|---------|------------|
| 1  | `apply_status_delta` unit tests | `test_obsidian_sync.py` | L |
| 2  | `todo_obsidian_sync.main()` orchestration | `test_obsidian_sync.py` or new `test_todo_obsidian_sync.py` | M |
| 3  | `git_commit_and_push` tests | `test_obsidian_sync.py` | S |
| 4  | Integration / E2E pipeline | new `tests/integration/los/test_pipeline_e2e.py` | L |
| 5  | Direct helper function tests | `test_obsidian_sync.py` | S |
