# Oracle Decisions

## 2026-03-31 — WOS Simple Arc Integration Test (Tier 6 item #13)

### Stage 1: Right problem?

**Question:** Does the test prove the happy-path arc the task specification asks for?

**Arc specified:**
1. GH issue → Cultivator seeds UoW (status: pending)
2. Steward evaluates UoW, prescribes work (status: ready-for-executor)
3. Executor claims UoW, dispatches subagent (status: active)
4. Subagent writes result.json at output_ref (status still active)
5. Steward evaluates result, marks done (status: done)

**What the test actually tests:**

`test_full_arc_reaches_done` — seeds a UoW at pending (steps 1–2 via upsert + approve), advances to ready-for-steward (simulates trigger evaluator), runs Steward cycle 1 (step 2 → ready-for-executor), runs Executor with mocked dispatcher (steps 3–4: claims → active → writes result.json → ready-for-steward), runs Steward cycle 2 (step 5 → done). Asserts final status = done.

`test_arc_status_sequence_is_correct` — re-runs the same arc and verifies the audit_log contains the required events in the correct sequence: created, status_change (×2), steward_prescription, claimed, execution_complete, steward_closure.

`test_done_is_terminal` — proves that a third Steward cycle after done finds 0 UoWs to evaluate (done has no re-entry path).

**Verdict: yes.** The test proves the pipeline wiring for the happy path. The single deviation from the specification is that the trigger evaluator (pending → ready-for-steward) is simulated via `set_status_direct` rather than a real trigger evaluator. This is appropriate: the trigger evaluator is not built yet (it is labeled ASPIRATIONAL in the design doc), and the test's goal is pipeline wiring, not trigger logic.

---

### Stage 2: Well made?

**Functional principles applied:**
- All mocks are pure functions (no shared mutable state between tests).
- `_make_mock_dispatcher` returns a (function, calls_log) pair — the function is stateless; the log list is per-call-site.
- `_noop_github_client`, `_noop_notify_dan`, `_noop_notify_dan_early_warning` are pure constants injected via the `run_steward_cycle` injectable parameters.
- No global state mutation; each test fixture (`arc_env`) creates a fresh Registry in a fresh tmp_path.

**Isolation:**
- DB: per-test `tmp_path` SQLite file — not in-memory (correct: multiple connections need to share the DB, matching production).
- Artifact dir: `tmp_path/artifacts` — no writes to production `~/lobster-workspace/`.
- Output dir: `tmp_path/outputs` — `executor_module._OUTPUT_DIR_TEMPLATE` is patched and restored in a try/finally block.

**Gaps / known issues:**

1. `_OUTPUT_DIR_TEMPLATE` is patched at the module level, not via a proper injectable. This is the only mutation of shared state in the test. It is safe because tests run sequentially (pytest default), but is fragile under parallel execution. The Executor constructor already accepts `dispatcher` as injectable; an `output_dir` injectable on the Executor would be the clean fix. Filed as a known gap — not fixing in this PR (constraint: no src/ changes).

2. The test does not cover the `pending → ready-for-steward` trigger evaluator step. The trigger evaluator is not yet built; `set_status_direct` is the correct stand-in.

3. The existing `tests/integration/test_wos_pipeline.py` has several ERRORing fixtures (stale API calls like `result["action"]` and `registry.confirm()`). These pre-exist this PR and are not regressions introduced here.

**Verdict: well made.** The three test cases are focused, self-contained, and each asserts a distinct property of the arc. The mock injection pattern is clean. The one shared-state mutation is documented and safe for sequential execution.
