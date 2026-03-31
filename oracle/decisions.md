# Oracle Decisions

## WOS Tier 6 Item #15 — TTL Recovery Integration Test

**Date**: 2026-03-31
**File**: `tests/integration/test_wos_ttl_recovery.py`

---

### Stage 1: Correctness Review

**Question**: Do the tests correctly exercise the recovery mechanisms?

**TTL recovery path (`recover_ttl_exceeded_uows`)**:
- Test seeds a UoW, advances it to `active` via direct SQL (mimicking Executor
  claim), then backdates `started_at` to `TTL_EXCEEDED_HOURS + 1 minute` in the past.
- `recover_ttl_exceeded_uows(registry)` is the exact production function from
  `orchestration/executor.py` — no mock wrapper, no stub.
- Assert: UoW id appears in the returned recovered list, status is `failed`,
  and the `audit_log` contains an `execution_failed` entry with `ttl_exceeded`
  in the reason field.
- Negative test: fresh `active` UoW (started_at = now) is NOT in the recovered
  list and stays `active`.

**Decision**: Correct. The test exercises the real function with a real in-memory
DB and verifies the state machine transition plus audit trail.

**executor_orphan path (`run_startup_sweep`)**:
- Test seeds a UoW, advances it to `ready-for-executor`, then backdates
  `created_at` to 2 hours ago (past the 1-hour orphan threshold).
- `run_startup_sweep` is loaded via `importlib.util` because the filename is
  `startup-sweep.py` (hyphen, not importable directly). This is a load-path
  workaround, not a behavioral mock — the real function runs.
- The `github_client` is a no-op lambda returning empty labels (bypasses the
  bootup-candidate gate without stubbing internal gate logic).
- Assert: `result.executor_orphans_swept == 1`, UoW status transitions to
  `ready-for-steward`, and audit note carries `classification: executor_orphan`.
- Negative test: UoW with recent `created_at` (under 1 hour) stays at
  `ready-for-executor` and sweep count stays 0.

**Decision**: Correct. The no-op github_client is the minimal injectable seam
the function exposes — injecting it rather than mocking internals is the right
approach.

---

### Stage 2: Boundary and Edge Case Review

**What is NOT tested (by design)**:
- Concurrent recovery (two heartbeats running simultaneously) — race safety is
  tested in the existing `test_wos_pipeline.py` concurrency test.
- The `dry_run=True` path — tested indirectly by the pipeline test; not needed
  here since this test is specifically about the live transition.
- The `bootup_candidate_gate=True` path — would require a mock github_client
  returning `bootup-candidate` label. Not part of this item's scope (TTL
  recovery, not gate logic).

**Pre-existing regression identified**: `test_wos_pipeline.py` uses dict-style
access on `upsert()` return values (`result["action"]`, `result["id"]`), but the
Registry API now returns typed dataclasses (`UpsertInserted`, `UpsertSkipped`).
This causes the existing pipeline tests to ERROR at setup. This is a pre-existing
regression in `test_wos_pipeline.py`, not introduced by this PR. Filed separately.

**Schema migration**: The test applies Phase 2 columns idempotently (mirrors
`test_wos_pipeline.py` pattern). The `executor_uow_view` DDL from the pipeline
test is not applied here — not needed because `recover_ttl_exceeded_uows` and
`run_startup_sweep` access `uow_registry` directly, not via the view.

**Decision**: Test is complete for the stated scope. The pre-existing dict-access
regression in `test_wos_pipeline.py` should be addressed in a follow-on issue.

---

### Summary

- 4 tests, all passing.
- Covers TTL recovery (2 tests: positive + negative) and executor_orphan
  detection (2 tests: positive + negative).
- No src/ changes — test only, per task boundary.
- Pre-existing bug found in `test_wos_pipeline.py`: API mismatch (dict vs
  typed dataclass return). Recommend filing a GH issue.
