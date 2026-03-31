# Oracle: Decisions

## [2026-03-31] Tier 6 Item 14 — Re-prescription cycle integration test

### Stage 1 review: design correctness

**Q: Does the hard cap check in _detect_stuck_condition match the design doc's stated cap?**

The design doc specifies _HARD_CAP_CYCLES = 5. The check is `cycles >= _HARD_CAP_CYCLES`.
This means the cap fires on the Steward cycle that reads `steward_cycles=5`, which is the
*sixth* Steward invocation (after five prescriptions and five executor failures). This is
correct behavior: the UoW gets exactly 5 chances before the Steward surfaces.

The test was initially written with `_HARD_CAP_CYCLES - 1` fail cycles, which would only
reach steward_cycles=4 (below the cap). Fixed to run `_HARD_CAP_CYCLES` fail cycles so
steward_cycles reaches 5, which is the threshold that triggers surfacing.

**Q: Does `_simulate_executor_fail` faithfully reproduce the production failure path?**

The helper uses the real Executor (6-step claim sequence), then overwrites result.json with
outcome=failed. This correctly exercises: (a) the atomic claim transaction, (b) the
output_ref being non-NULL and non-empty (Executor writes it), and (c) the Steward's
`_assess_completion` reading the result file and returning is_complete=False for
outcome=failed. The `execution_failed` audit entry is injected directly to simulate what
the subagent would write via write_result in production.

**Q: Is the transition from active → ready-for-steward handled correctly after executor fails?**

The real Executor's `execute_uow` → `_run_execution` → `complete_uow` call transitions to
ready-for-steward even when dispatching succeeds (because dispatch is a noop in tests). The
result.json overwrite then makes the *Steward's* view of the outcome be "failed". This is
the correct simulation: the Executor always returns the UoW to ready-for-steward; the Steward
reads the result file to determine whether the work succeeded.

### Stage 2 review: test coverage completeness

**Covered:**
- Single failure → re-prescription (steward_cycles 0→1→2)
- Multiple failures → steward_cycles increments correctly (1, 2, 3)
- Hard cap fires at exactly steward_cycles=5 (not earlier, not later)
- status=blocked at cap, not ready-for-executor
- notify_dan called with condition='hard_cap' at cap
- Early-warning notification fires at steward_cycles=4 (EARLY_WARNING_CYCLES)
- Audit log records steward_prescription events for each re-prescription pass
- Full end-to-end sequence from seed through cap

**Out of scope (not covered by this test):**
- outcome=partial re-prescription path (partial steps context)
- outcome=blocked surfaces to Dan via executor_blocked condition
- TTL-exceeded UoWs (separate recovery path via recover_ttl_exceeded_uows)
- Concurrent Steward instances (optimistic lock race)
- BOOTUP_CANDIDATE_GATE interaction (tested in test_wos_pipeline.py)

**Decision: all in-scope requirements from Tier 6 Item 14 are covered.**
