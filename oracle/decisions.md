# Oracle: Decisions

## [2026-04-01] PR #537 (dcetlin fork) — fix(inbox_server): replace hardcoded /home/admin/ path in bisque connection URL handler

### Stage 1: Is this solving the right problem?

The bug: `handle_get_bisque_connection_url` returns an error message with hardcoded `/home/admin/lobster/` paths when the dashboard token file is missing. This is wrong for any install where the user is not `admin`. The correct fix is to use the existing `_REPO_DIR` module-level constant (line 784), which already respects `LOBSTER_INSTALL_DIR` env var and falls back to `Path.home() / "lobster"`. STAGE 1: APPROVED.

### Stage 2: Is the implementation well-made?

Changes: 3 lines in the error-message-only branch (token file missing). Adds 2 local variables (`venv_python`, `dashboard_server`) constructed from `_REPO_DIR` and uses an f-string to render the command. The fix is inside the `if not token_file.exists():` branch — only executes when token is missing, which is an error path.

Checks:
- `_REPO_DIR` is defined at module scope (line 784) — no import or lazy-init needed.
- Path construction: `_REPO_DIR / ".venv" / "bin" / "python3"` mirrors the actual venv layout from `install.sh`.
- No behavioral change for the success path.
- String conversion: Python's f-string on a `Path` object calls `__str__()` which renders the absolute path — correct.
- Diff quality: +3/-2 lines. Surgical.

**Verdict: APPROVED — merge.**

---

## [2026-04-01] PR #536 — fix(surface-queue-delivery): correct oracle source key in SOURCE_WEIGHT and _SOURCE_LABELS (issue #263)

### Stage 1: Is this solving the right problem?

Adversarial prior: the wrong fix would be to rename the queue's source_file values from "oracle/decisions.md" to "meta/oracle/learnings.md" — that would require changing all producers that write to the queue and would break any items already in the queue.

Finding: the reflective-surface-queue.json queue stores oracle items with `source_file: "oracle/decisions.md"`. The `SOURCE_WEIGHT` and `_SOURCE_LABELS` dicts both used `"meta/oracle/learnings.md"` as the key — a path that does not exist. The fix aligns the dicts to the actual key value produced by queue writers.

Decision: change the dict keys to match the actual `source_file` value that appears in queue items. This is surgical and correct. STAGE 1: APPROVED.

### Stage 2: Is the implementation well-made?

Changes: two string literals replaced in `SOURCE_WEIGHT` and `_SOURCE_LABELS` dicts. No logic changes. Human-readable label "Oracle Learnings" preserved in `_GROUP_ORDER`.

Checks:
- `priority_score()` calls `SOURCE_WEIGHT.get(source_file, DEFAULT_SOURCE_WEIGHT)` — after fix, oracle items receive weight 20 instead of falling through to 5.
- `_source_label()` calls `_SOURCE_LABELS.get(...)` — after fix, oracle items display "Oracle Learnings" correctly instead of showing the raw path.
- Regression risk: none — the old key was never matched; the fix promotes items from DEFAULT_SOURCE_WEIGHT (5) to their intended weight (20).
- Diff quality: 2 lines changed, net 0. Fully surgical.

**Verdict: APPROVED — merge.**

---

## [2026-04-01] PR #499 — fix(auto-router): correct QUEUE_PATH to live meta/ path (issue #260)

### Stage 1: Is this solving the right problem?

Adversarial prior: the wrong fix would be to create the `hygiene/meta/` directory and migrate — this requires synchronizing two scripts and introducing a migration step. The correct fix depends on whether `hygiene/meta/` organization was intentional (executed) or aspirational (never executed).

Finding: `~/lobster-workspace/hygiene/meta/` does not exist and has never been created. `surface-queue-delivery.py` (the companion script) already uses `~/lobster-workspace/meta/reflective-surface-queue.json`. No migration was ever run. The canonical path was never inhabited.

Decision: Option 2 is correct — make `meta/` the canonical path in auto-router.py, aligning it with surface-queue-delivery.py. This removes the dead path, removes the fallback logic that was always firing, and makes both scripts consistent. Creating `hygiene/meta/` would be new structure with no data migration plan. STAGE 1: APPROVED.

### Stage 2: Is the implementation well-made?

Changes: removes `QUEUE_PATH` pointing to non-existent `hygiene/meta/` path, removes `_OLD_QUEUE_PATH` constant, removes `_resolve_queue_path()` 14-line fallback function, sets `QUEUE_PATH` directly to `meta/reflective-surface-queue.json`, changes `_resolve_queue_path()` call to `QUEUE_PATH`.

Checks:
- `load_queue()` handles missing file: returns `[]` if `not path.exists()` — no crash on first run.
- `surface-queue-delivery.py` consistency: both scripts now reference `meta/reflective-surface-queue.json` — consistent.
- Regression risk: none — the fallback was always firing (hygiene/meta/ never existed), so we are replacing a broken constant with the path that was always being used.
- Diff quality: net -17 lines (19 deleted, 2 added). Minimal. Surgical.

**Verdict: APPROVED — merge.**

---

## [2026-04-01] PR #383 — real executor via `claude -p` + TTL recovery

### Stage 1: Is this solving the right problem?

**Q: Does the synchronous `claude -p` dispatcher solve a real gap, or does it introduce
a worse problem than the ghost-message approach?**

The original `_dispatch_via_inbox` was fire-and-forget: it wrote a message and returned.
The Steward detected stalls only via TTL expiry. This created a gap: UoWs could be stuck
in `active` for hours with no feedback.

The `_dispatch_via_claude_p` dispatcher blocks synchronously, which is the right move
for a 3-minute heartbeat: the executor now has a definitive exit code (0 = dispatched
successfully, non-zero = subprocess failed). This enables the heartbeat to fail fast
and let TTL recovery clean up later, rather than leaving ghost UoWs indefinitely.

Decision: the direction is correct. Synchronous dispatch from a cron-driven heartbeat is
appropriate — the heartbeat process can hold the subprocess open for up to 2 hours
(WOS_EXECUTOR_TIMEOUT=7200), which is within the cron model (3-minute schedule, but
one invocation can run long). The cron model here is Type C (cron-direct), not a
process-supervision model, so blocking is acceptable.

**Q: Is the result.json written by the Executor semantically correct after `claude -p`?**

Finding — potential design gap: When `_dispatch_via_claude_p` runs, the subprocess
executes a functional-engineer agent. That agent calls `mcp__lobster-inbox__write_result`
(inbox delivery), NOT `orchestration.result_writer.write_result` (file-based contract).
This means the functional engineer does NOT write `{output_ref}.result.json`. After the
subprocess exits 0, the Executor writes `result.json` with `outcome=COMPLETE` (Step 5).

The Steward then reads `outcome=COMPLETE` and concludes the work succeeded. This is
semantically weaker than it appears: `outcome=COMPLETE` only means "the subprocess exited 0"
not "the functional engineer actually opened a PR and completed the task." A functional
engineer that encountered an error but still exited 0 would produce a false `outcome=COMPLETE`.

However: this is a known design decision within the WOS system — the functional engineer
is instructed to call `write_result` via MCP (for inbox routing). The exit code IS the
primary signal in the `claude -p` model. The Steward's re-prescription loop exists
precisely to catch cases where `outcome=COMPLETE` did not produce a verifiable artifact
(e.g., PR URL absent). The TTL recovery handles the exit-non-zero path.

The preamble includes "Call write_result with the PR URL and outcome when done" via MCP,
which writes to the inbox (not to output_ref). This is a contract divergence from the
standard executor-contract.md that's acknowledged by the design (PR description says
"subagent reads the GitHub issue, implements the prescription, opens a PR, and calls
write_result") but not formally documented as a contract exception.

Assessment: the design is coherent for the current state of the system. The Steward
re-prescription loop is the fallback for false-positive completes. The risk is
documented and the TTL path covers the crash/non-zero exit case.

**Q: Is 4-hour TTL the right threshold?**

The default `estimated_runtime` ceiling is 30 minutes. The `WOS_EXECUTOR_TIMEOUT` is
7200 seconds (2 hours). TTL_EXCEEDED_HOURS = 4 gives a 2-hour buffer beyond the max
expected agent runtime. This is appropriate — tight enough to surface stalls quickly,
loose enough to avoid false positives on long-running agents.

**Stage 1 verdict: design is sound. One design tension (functional engineer contract vs.
executor-contract.md) is acknowledged and covered by the re-prescription loop.**

---

### Stage 2: Is the implementation well-made?

**Q: Does `recover_ttl_exceeded_uows` handle the concurrent heartbeat case correctly?**

Finding: `recover_ttl_exceeded_uows` opens its own raw connection to query stalled UoWs,
closes it, then iterates and calls `registry.fail_uow()` for each. The `fail_uow` method
uses an optimistic WHERE guard on status='active'. If two heartbeat instances race (rare
but possible in theory), the second `fail_uow` call will silently no-op (rowcount=0).
This is correct behavior. APPROVED.

**Q: Is the dry_run path in `run_ttl_recovery` (heartbeat script) duplicating logic from
the production path in ways that could diverge?**

Finding: `run_ttl_recovery` in `executor-heartbeat.py` has an inline SQL query in the
dry_run branch that reconstructs the TTL cutoff logic instead of calling
`recover_ttl_exceeded_uows`. This is intentional (dry_run skips mutations) but creates
two places where the TTL cutoff calculation lives. If TTL_EXCEEDED_HOURS is changed,
the dry_run query will still calculate correctly (it imports TTL_EXCEEDED_HOURS).
However, the dry_run SQL uses `started_at < ?` (cutoff_iso), which matches the
production query in `recover_ttl_exceeded_uows`. No divergence risk detected.

Minor note: the dry_run branch imports `sqlite3`, `datetime`, `timezone`, `timedelta`
inside the function rather than at module level. This is defensible (keeps local scope),
but the module-level imports in executor-heartbeat.py already include these. This is
a style inconsistency, not a bug.

**Q: Does `_dispatch_via_claude_p` handle the `proc` return value correctly?**

Finding: `subprocess.run(..., check=True)` is called and the result is bound to `proc`
but never used (the `proc` variable is dead). This is harmless — `check=True` raises
on non-zero exit, and the `run_id` return value is derived from `uow_id` and timestamp,
not from `proc`. APPROVED (dead variable is minor style issue, not a bug).

**Q: Does the executor correctly handle the case where `_dispatch_via_claude_p` is used
but the `output_ref.result.json` file does not exist after the subprocess exits?**

Finding: After `_dispatch_via_claude_p` returns, `_run_execution` writes `result.json`
with `outcome=COMPLETE`. The Steward's `_assess_completion` reads this file. Since the
functional engineer writes via MCP (not file-based), the result.json IS written by the
Executor and WILL exist. The Steward will see `outcome=COMPLETE`. If the functional
engineer failed silently (exit 0 but no PR), the Steward's completion verification
(step 6 of `_process_uow`) may still detect incompleteness via audit entries or
by checking whether a PR was actually opened. This is handled at the Steward level,
not the Executor level. No missing file risk. APPROVED.

**Q: Are tests adequate for the `_dispatch_via_claude_p` path?**

Finding: There is NO direct unit test for `_dispatch_via_claude_p` that exercises the
subprocess call (even via mock PATH). The mock_claude_cli.py fixture exists and supports
`install_mock_claude(bin_dir)`, which can override PATH. The integration test
`test_wos_ttl_recovery.py` covers `recover_ttl_exceeded_uows` and `run_startup_sweep`
but does not test the `_dispatch_via_claude_p` function itself.

The `test_executor.py` file covers:
- `test_executor_defaults_to_dispatch_via_inbox` — confirms default is `_dispatch_via_inbox`
- The heartbeat comment (`explicitly passes _dispatch_via_claude_p for production use`)
  is only in a docstring, not a test assertion
- No test exercises `_dispatch_via_claude_p` with a mock binary on PATH

This is the most significant gap in test coverage. `_dispatch_via_claude_p` exercises
`subprocess.run` with `check=True`, `timeout=`, and `capture_output=False`. None of
these behaviors are tested: non-zero exit (CalledProcessError propagation),
TimeoutExpired propagation, or FileNotFoundError when claude binary is absent.

This gap is mitigated by: (1) the function is simple (subprocess.run + return run_id),
(2) the TTL recovery path covers the stall case, (3) tests for the surrounding execution
sequence (TestSuccessfulExecution, TestFailedExecution) exercise the dispatch abstraction
via `_noop_dispatcher` and `fake_dispatcher`. But the subprocess boundary itself is
untested.

**Q: Is the TTL integration test (`test_wos_ttl_recovery.py`) well-written?**

Finding: The test correctly:
- Seeds a UoW to `active` state via direct SQL (not via the 6-step claim sequence,
  which would require a real WorkflowArtifact and subprocess)
- Backdates `started_at` to beyond the TTL threshold
- Asserts: (a) UoW transitions to `failed`, (b) audit entry has `event=execution_failed`,
  `from_status=active`, `to_status=failed`, (c) audit note contains `ttl_exceeded`
- Tests the negative case (fresh UoW not recovered)

The executor_orphan test (startup_sweep) is also present and well-structured.
APPROVED.

**Stage 2 verdict: implementation is correct. Two minor quality notes:**
1. `proc` variable in `_dispatch_via_claude_p` is dead (harmless).
2. No direct unit test for `_dispatch_via_claude_p` subprocess behaviors.

Neither note rises to NEEDS_CHANGES given the TTL safety net and the simplicity of the
function. The test coverage gap is a debt item worth tracking.

---

### Overall verdict: APPROVED

**PR #383** is approved for merge. The synchronous `claude -p` dispatcher is the right
design for the cron-direct executor model. TTL recovery is correctly implemented and
integration-tested. The functional-engineer contract tension (MCP vs. file-based
write_result) is a known design choice within the system's current architecture.

**Recommended follow-up (non-blocking):**
- Add a unit test for `_dispatch_via_claude_p` using mock_claude_cli on PATH
  (exit 0, non-zero exit, timeout) to prevent regressions on the subprocess boundary.

---

## [2026-04-01] PR #388 — registrar success_criteria + steward instruction composition

### Stage 1: Is this solving the right problem?

**Q: Is `success_criteria` always empty on new UoWs, as claimed?**

Confirmed: before this PR, `promote_to_wos` in `cultivator.py` called
`registry.upsert(issue_number=..., title=...)` with no `success_criteria` argument.
The `upsert` signature had no `success_criteria` parameter before this PR. The INSERT
statement did not include `success_criteria`. Since the DB schema has this column with
a default of `''` (empty string), every new UoW created via cultivator had
`success_criteria = ''`. The Steward then logged `success_criteria_missing=True` on
every diagnosis cycle, which is confirmed by the bug description.

The fix is clean and correct: `_extract_success_criteria(issue.body)` is called at
promotion time, not at prescription time. This is the right boundary — extraction
happens once, at data-entry, not repeatedly per prescription cycle.

**Q: Is the fallback (first non-heading paragraph) semantically correct?**

The fallback returns the first non-empty, non-heading paragraph of the issue body,
truncated at 500 chars. This is pragmatic: an issue with no formal criteria section
still gives the executor something concrete. The risk is that the first paragraph may
be a disclaimer, label note, or contextual narrative rather than acceptance criteria.
This is an acceptable tradeoff for issues without formal criteria sections. The
truncation at 500 chars prevents bloat.

**Q: Is Bug 2 (steward prescriptions saying "See issue body for details.") genuinely
fixed?**

Before this PR, `_build_prescription_instructions` built instructions with:
`f"Success criteria: {success_criteria or 'See issue body for details.'}"`

This was a placeholder that was always triggered (because `success_criteria` was always
`''`). The fix addresses both ends: (1) `success_criteria` is now populated at promotion,
and (2) if still absent, the issue body is used directly. The placeholder is eliminated.
CONFIRMED.

**Stage 1 verdict: both bugs are correctly identified and the fix addresses the root
causes at the right boundaries.**

---

### Stage 2: Is the implementation well-made?

**Q: Is `_extract_success_criteria` a pure function with no edge-case bugs?**

Finding 1 — heading search is not anchored to line boundaries:
`body.find("## Acceptance Criteria")` will match the heading anywhere in the string,
including mid-line (e.g., `text ## Acceptance Criteria`). In practice, GitHub issue
bodies follow Markdown conventions where `##` appears at line start. The function uses
`body.find("\n", idx)` to advance past the heading line, so even a mid-line match
would produce a coherent section extraction. Risk is low, but the match is not anchored
with `\n## ` prefix.

Finding 2 — case-sensitive matching for mixed-case variants:
The tuple includes both `"## Acceptance Criteria"` and `"## acceptance criteria"`,
covering the most common cases. However, `"## ACCEPTANCE CRITERIA"` (all caps) or
`"## Acceptance criteria"` (sentence case) would not match. This is acknowledged in
the PR body. GitHub's issue templates typically use title case (`## Acceptance Criteria`),
so coverage is sufficient for this codebase. Minor gap only.

Finding 3 — section boundary detection:
`body.find("\n##", section_start)` finds the next `##` heading. This correctly handles
multi-paragraph criteria sections. It does NOT handle `###` sub-headings within the
criteria section being excluded (e.g., `### Pass/Fail Criteria` under `## Acceptance
Criteria`). The full sub-section content including `###` lines is returned, which is
correct behavior.

Finding 4 — `body.find("\n", idx)` returns -1 if the heading is the last line with no
trailing newline. The code correctly handles this with `if section_start == -1: continue`,
moving to the next heading. APPROVED.

All edge cases are handled correctly or represent acceptable known limitations.

**Q: Is `upsert` → `_upsert_typed` threaded correctly?**

`upsert(success_criteria=...)` calls `_upsert_typed(..., success_criteria)`. The INSERT
statement correctly includes `success_criteria` in both the column list and VALUES tuple.
The positional argument order matches the column order. APPROVED.

**Q: Does the UPDATE path (conflict resolution) also update `success_criteria`?**

Finding — UPDATE path does NOT update `success_criteria`:
The conflict resolution path in `_upsert_typed` (UNIQUE conflict + existing is proposed
→ UPDATE fields) does NOT include `success_criteria` in its UPDATE SET clause. Looking
at the diff: only the INSERT path was changed. If a UoW was previously inserted with
empty `success_criteria` and the cultivator runs again on the same issue, the UPDATE
path would not refresh `success_criteria` from the updated issue body.

This is a known limitation explicitly stated in the PR: "No existing UoW rows are
modified — fix is forward-only." For newly inserted UoWs this is correct. For UoWs
that were inserted empty and then re-swept, the update path is a gap — but this is
acknowledged and the existing skip logic (skip if non-terminal non-proposed record
exists) means re-sweeping a proposed UoW would trigger the UPDATE path, not a re-insert.
The gap is real but low-impact: once a UoW is in-flight (pending, active, etc.), the
cultivator skips it entirely (UpsertSkipped).

**Q: Is `_build_prescription_instructions` in `steward.py` well-structured?**

The refactored function uses a `parts` list with `"\n".join(parts)` rather than
f-string concatenation. This is cleaner and makes the conditional blocks readable.
The `criteria_block` (success_criteria → issue body → empty) is built once and
reused in both the cycle-0 and re-prescription branches. No duplication. APPROVED.

**Q: Does `issue_body` threading from `_process_uow` to `_build_prescription_instructions`
introduce a regression in tests?**

The `_build_prescription_instructions` signature adds `issue_body: str = ""` with a
default. All existing call sites that don't pass `issue_body` continue to work.
The `_process_uow` call site now passes `issue_body = issue_info.get("body", "")
if issue_info else ""`, which is safe when `issue_info` is None (GitHub fetch failed).
APPROVED.

**Q: Is the `_extract_success_criteria` function tested with sufficient coverage?**

Finding: `_extract_success_criteria` is tested indirectly via
`test_upsert_stores_success_criteria` in `test_registry.py`, which exercises one
happy-path case (`## Acceptance Criteria` heading with content).

There is NO dedicated test file for `_extract_success_criteria` covering:
- No matching heading → fallback to first paragraph
- Heading present but section is empty → move to next heading
- `body = ""` → return `""`
- Fallback with first paragraph being a heading (should skip)
- Truncation at 500 chars
- Body ending without trailing newline

The `test_registry_cli.py` test `test_upsert_with_issue_body_populates_success_criteria`
was found but not read in full. This may cover additional cases.

The test gap is meaningful: the extraction logic has enough branches that a dedicated
unit test would be valuable. However, the core happy-path is covered, the pure function
is simple and deterministic, and the fallback is conservative (returns empty string
rather than incorrect data). This is a debt item, not a blocking issue.

**Stage 2 verdict: implementation is correct and structurally sound. Two notes:**
1. UPDATE conflict path does not refresh `success_criteria` — acknowledged and forward-only by design.
2. `_extract_success_criteria` has limited test coverage of edge cases.

Neither note is blocking.

---

### Overall verdict: APPROVED

**PR #388** is approved for merge. Both bugs (#386 and #387) are correctly fixed at
the right boundaries. The extraction function is pure and well-guarded. The steward
instruction builder is cleaner and composable. The success_criteria threading is
backward-compatible.

**Recommended follow-up (non-blocking):**
- Add unit tests for `_extract_success_criteria` edge cases (empty body, no heading,
  heading with empty section, paragraph fallback, 500-char truncation).
- Consider adding `success_criteria` to the UPDATE conflict path so re-sweeping
  refreshes extracted criteria from updated issue bodies.

---

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

---

## [2026-03-31] Steward Feedback Loop (WOS Tier 5 Item 12)

### Stage 1: Is this solving the right problem?

**Question: does the steward_log actually store prescription text in a retrievable way?**

Finding: `steward_log` is a TEXT column in `uow_registry` (newline-delimited JSON entries).
It does NOT store full prescription text. Prescription log entries (`event: "prescription"` /
`event: "reentry_prescription"`) store metadata: `completion_assessment`, `next_posture_rationale`,
`return_reason`, and `steward_cycles`. Full instructions are written to the workflow artifact
file on disk.

Decision: Use the prescription metadata from `steward_log` rather than reading artifact files.
The metadata is sufficient to show the Steward what gap was identified (`completion_assessment`)
and what routing rationale was used (`next_posture_rationale`) — exactly enough to avoid
repeating the same approach. This also keeps the implementation self-contained within the
existing text field with no new DB reads.

**Question: Is N=3 the right limit?**

Finding: Each prescription entry is a short JSON dict (under 200 chars). At N=3, the injected
context adds roughly 300–600 characters to the instructions — well within prompt budget.
N=3 balances recency (avoids padding with old cycles) with coverage (enough to detect a loop).
APPROVED: N=3.

### Stage 2: Is the implementation well-made?

**Check: does it handle the case where steward_log has no entries gracefully?**

Finding: `_fetch_prior_prescriptions` returns `[]` for `None`, empty string, and logs with
no prescription events. The call site uses `prior_prescriptions = [] if cycles == 0` and
conditionally calls `_fetch_prior_prescriptions` only when `cycles > 0`. Even when called
with a log that has no prescription entries, it returns `[]`. The `_build_prescription_instructions`
function only appends the prior context block when `prior_prescriptions` is truthy. No crash
path exists for absent data. APPROVED.

**Check: does it read posture only (not prior prescriptions from a separate store)?**

Confirmed: the implementation reads from `current_log_str` (the steward_log already loaded
in `_process_uow` at the start of the function). No extra DB read. No new fields. The helper
is a pure function over the already-loaded text. APPROVED.

**Verdict: APPROVE — proceed to PR**
