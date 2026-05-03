# WOS V3 Sprint 002 — Register-Aware Dispatch and Completion Policy

*Status: Complete as of 2026-04-07*

---

## Sprint Scope

Sprint 002 delivered the register-aware Steward and Executor changes specified in the [WOS V3 Steward/Executor Spec](wos-v3-steward-executor-spec.md). These changes make the Steward's diagnosis, completion assessment, and Dan-interrupt conditions register-aware, and make the Executor's dispatch table route by `executor_type` rather than unconditionally dispatching `functional-engineer`.

Sprint 001 (PR A) established the trace substrate — the Executor writing `trace.json` at all exit paths. Sprint 002 builds on that substrate: the Steward now reads traces (PR C), applies register-specific completion policies (PR C), blocks category-wrong dispatch via the register-mismatch gate (PR D), and the Executor routes to register-appropriate dispatch functions (PR B).

**Spec changes delivered:** 1 (Register-Aware Diagnosis), 2 (Corrective Trace Injection), 3 (Register-Mismatch Gate), 4 (Expanded Dan Interrupt Conditions), 5 (Register-Appropriate Executor Routing).

**Not in sprint 002:** Change 6 (trace.json Write Requirement) was delivered in sprint 001 as PR A (#611). S3 (Observation Loop Pattern Synthesis) and S4 (Scaling Governor) remain deferred pending operational evidence.

---

## PR Table

| PR Letter | GitHub Ref | Title | Merged | Spec Changes |
|-----------|-----------|-------|--------|--------------|
| A | #611 | executor writes trace.json and corrective_traces DB row at all exit paths | 2026-04-04 | Change 6 (sprint 001) |
| B | #649 | executor register-appropriate routing — dispatch table for executor_type | 2026-04-07 | Change 5 |
| C | #653 | register-aware diagnosis + corrective trace injection | 2026-04-07 | Changes 1, 2 |
| D | #651 | register-mismatch gate — Steward blocks category-wrong dispatch | 2026-04-07 | Changes 3, 4 |

---

## Post-Merge Audit (PR E — this UoW)

UoW `uow_20260502_eec712` is a post-merge audit and sprint documentation task. It audits Changes 1 and 4 against the spec, runs the associated test suites, and creates this sprint record.

### Change 1 — `_register_completion_policy` (steward.py)

**`_register_completion_policy()` function (line 2232):**

- Returns `"machine-gate"` for `"operational"` — confirmed
- Returns `"machine-gate"` for `"iterative-convergent"` — confirmed
- Returns `"always-surface"` for `"philosophical"` — confirmed
- Returns `"require-confirmation"` for `"human-judgment"` — confirmed
- Returns `"machine-gate"` (default) for unknown registers — confirmed

**`_assess_completion()` register policy application (line 1120):**

- `always-surface` (philosophical): Returns `(False, "register=philosophical: completion requires human judgment — surfacing to Dan", "philosophical_surface")` regardless of `result.json` outcome. Uses `"philosophical_surface"` as executor_outcome, not `"hard_cap"`. — confirmed
- `require-confirmation` (human-judgment): Returns `(False, ...)` unless `uow.close_reason` is populated. When `close_reason` is present, returns `(True, ..., "complete")`. — confirmed
- `machine-gate` (operational, iterative-convergent): Existing logic unchanged — falls through to `return True`. — confirmed

**Verdict: No gaps found in Change 1.**

### Change 4 — Expanded Dan Interrupt Conditions (steward.py)

**4a `philosophical_register` in `_detect_stuck_condition()` (line 3324):**

- Fires when `uow.register == "philosophical"` AND `reentry_posture != "first_execution"` — confirmed
- Does NOT fire on `first_execution` (one-cycle wait for evidence) — confirmed

**4c `no_gate_improvement` in `_detect_stuck_condition()` (line 3329):**

- Fires for `iterative-convergent` register when `_count_non_improving_gate_cycles(steward_log, n=3) >= 3` — confirmed
- `_count_non_improving_gate_cycles()` (line 2455) parses `trace_injection` events from `steward_log` and counts consecutive cycles where `gate_score.score` did not improve — confirmed
- Uses named constant `_NON_IMPROVING_GATE_THRESHOLD = 3` — confirmed

**Surface messages in `_default_notify_dan()` (line 3543):**

- `philosophical_register` (line 3619): Message matches spec format: `"WOS: UoW {id} is in philosophical register — executor returned output but completion requires human judgment. See output at {output_ref}. Summary: {summary[:200]}"` — confirmed
- `no_gate_improvement` (line 3651): Message includes the UoW ID, cycle threshold, and a reference to the steward_log. The spec calls for inlined `{history}`, `{cmd}`, and `{delta}` fields; the implementation instead appends the full `steward_log` (which contains `trace_injection` events with `gate_score` and `prescription_delta` data). The information is equivalent — Dan sees all the data — but the format differs from the spec's inline pattern. **Minor format deviation, not a functional gap.** The steward_log already contains the structured JSON entries that the spec's variables would have been extracted from.

**Verdict: No functional gaps found in Change 4. One minor format deviation in the `no_gate_improvement` surface message (data included via steward_log append rather than inlined extraction).**

---

## Test Coverage

| Test File | Tests | Result |
|-----------|-------|--------|
| `tests/unit/test_orchestration/test_steward_register_aware_diagnosis.py` | 35 | All passed |
| `tests/unit/test_orchestration/test_steward_register_mismatch.py` | 21 | All passed |

**Total: 56 tests, 0 failures.**

Key test areas:
- `_register_completion_policy()` — all 4 registers + unknown default
- `_read_trace_json()` — valid file, missing file, uow_id mismatch, invalid JSON
- `_bound_prescription_delta()` — short/exact/oversized/empty delta
- `_count_non_improving_gate_cycles()` — zero/improving/non-improving/reset/plateau/no-entries
- `_assess_completion()` with register policies — philosophical surfaces, human-judgment pending/confirmed, operational/iterative-convergent close normally
- `_detect_stuck_condition()` new conditions — philosophical_register on reentry, philosophical_register skips first_execution, no_gate_improvement fires/does-not-fire
- Integration tests — trace injection event written, philosophical surface fires on reentry, philosophical does not surface on first execution
- `_check_register_executor_compatibility()` — all valid/incompatible register-executor pairings, mismatch direction
- Register-mismatch gate integration — mismatch fires, blocks artifact write, observation logged to steward_log and audit_log, compatible pairings prescribe normally

---

## Outstanding Items

None — sprint complete. All spec changes 1-5 are implemented and tested. Change 6 was delivered in sprint 001. Future work (S3 Observation Loop, S4 Scaling Governor, S5 Dan-Interrupt Cartridge) remains deferred pending operational evidence from the register-aware dispatch running in production.
