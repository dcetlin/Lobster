# Oracle: Decisions

---

### [2026-04-22] PR #830 re-oracle — feat(orient): proprioceptive gate-miss logging and nightly consolidation summary

**Prior NEEDS_CHANGES verdict:** [2026-04-22] PR #830. One gap named.

**Prior gap tracking:**

- **Gap 1 (bare `python3` in nightly-consolidation.md step 1c):** ADDRESSED. The diff shows `uv run python -c "..."` at the invocation site in step 1c. Confirmed by `gh pr diff 830 | grep python`: the only Python invocation added is `uv run python -c`. No bare `python3` appears anywhere in the diff. Resolution criterion (a) from the revision contract is satisfied: the revision shows `uv run` at the invocation site.

**Vision alignment:** The adversarial prior for this re-review is unchanged from the original: this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths. The re-review is a single-line fix to a named implementation defect; it introduces no new direction, no new assumption, and no new foreclosure. The vision alignment finding from the original entry stands without revision: direction is Confirmed against vision.yaml constraint-3 (OODA Orient as schwerpunkt), principle-5 (discriminator improvement over rule addition), and current_focus.after_that (proprioceptive pulse named explicitly). The fix closes the only gap that was blocking approval; no new gaps are introduced by the change.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Gap 1 is closed.** `uv run python -c` replaces the former `python3 -c` at exactly the invocation site named in the revision contract. The fix is a one-token change at the correct location. The surrounding logic (cutoff computation, JSON parsing, gate-miss filtering, Counter aggregation) is unchanged from the original submission and was previously assessed as correct.
- **No new content was added beyond the `uv run` fix.** The diff is identical in scope to the original PR — three files (`nightly-consolidation.md`, `sys.dispatcher.bootup.md`, `CLAUDE.md`) — confirming no feature bundling was introduced during the revision.
- **All other Stage 2 findings from the original entry remain valid.** The `sys.dispatcher.bootup.md` pseudocode expansion is correct; the CLAUDE.md gate-miss logging subsection is correctly structured; no canonical-templates counterpart gap applies.
- **The bare-python3-in-instruction-layer pattern (learnings.md PR #804, #821, #830) is now correctly applied.** This re-review checked for recurrence: no other Python invocation in the three changed files uses a bare `python3`. The pattern that constrained this review: reading the learnings.md entry for PR #830 before examining the diff confirmed that the only thing to verify was `uv run` at the invocation site — which prevented a pass-through approval based on surface-level diff reading.

**Patterns introduced:** No new patterns beyond those named in the original entry.

**What this forecloses:** Nothing beyond what was noted in the original entry.

**Opportunity cost note:** Single-line fix closing a named revision contract gap. Negligible cost.

**VERDICT: APPROVED**

---

### [2026-04-22] PR #830 — feat(orient): proprioceptive gate-miss logging and nightly consolidation summary

**Vision alignment:** The adversarial prior — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — finds the most significant tension not in the direction but in the mechanism. The theory of change is: if gate misses are logged via `write_observation`, nightly consolidation surfaces them, and Orient improves over time. The prior asks what would have to be true for this to generate useful signal. The critical condition: the dispatcher must be oriented enough to recognize a gate miss as a gate miss before the logging instruction fires. A dispatcher that reliably identifies gate misses has already half-corrected the failure; the logging is then diagnostic. If the dispatcher misses the gate because it lacks sufficient Orient state, it will also miss the logging instruction — the same failure mode the logging is meant to surface. The issue (#193) notes "zero behavioral-miss observations in 2984 events," which is consistent with either gates being honored or gates being missed without self-recognition. This PR addresses the former case and partially addresses the latter on the margin (it expands the orientation surface the dispatcher reads). The direction is correct: vision.yaml constraint-3 names OODA Orient as the schwerpunkt; current_focus.after_that names the proprioceptive pulse explicitly; principle-5 names discriminator improvement over rule addition. The mechanism is not the cheapest available test of the underlying assumption, but it is strictly additive: even partial signal is better than no signal, and the instruction is placed in the correct location (immediately after the gate table). The larger concern is a concrete implementation defect in nightly-consolidation.md (see Stage 2) that would prevent the consolidation leg of the feedback loop from working.

**Alignment verdict:** Confirmed

**Quality finding:**
- **`bare python3` in nightly-consolidation.md step 1c violates the `uv` convention and matches a named failure pattern.** The embedded `python3 -c "..."` block in the new step 1c is the exact pattern named in learnings.md PR #804 and PR #821: "bare python3 in instruction documents" and "bare python3 in bash scripts." If `python3` is absent from PATH (because `uv` manages the Python installation), the script silently produces empty output, `gate_miss_summary` is empty, and the proprioceptive bullet is never written. The PR would appear to work (no errors), but the nightly consolidation leg of the feedback loop would produce no signal — which is worse than the pre-PR state because it would suppress even a "no gate misses" confirmation. The fix is to replace `python3 -c` with `uv run python -c` or equivalent. This is a NEEDS_CHANGES finding: the Python invocation convention is a documented system rule (CLAUDE.md), and this PR violates it in the highest-consequence location (the log-reader that closes the feedback loop).
- **CLAUDE.md gate-miss logging section is correctly structured.** The vocabulary of 8 named gates maps 1:1 to the Tier-1 Gate Register rows. The examples are concrete and unambiguous. The instruction correctly places logging alongside recovery rather than instead of it. No behavioral compression is lost — the table-as-compaction-resistant-encoding golden pattern (2026-03-27) is honored: the new subsection uses prose rather than a table row, but this is appropriate because the subsection is a procedure (when/how to call write_observation), not a gate trigger.
- **`sys.dispatcher.bootup.md` pseudocode expansion is unambiguous and internally consistent.** The full branch tree for `subagent_observation` is now explicit. The `memory_store` conditional correctly requires both `gate=` and `outcome=miss` in the text, preventing non-gate system_error observations from being routed into the gate-miss memory pipeline. The Bash log-write idiom (`Bash(f'echo ...')`) is consistent with existing dispatcher pseudocode patterns.
- **No canonical-templates counterpart gap.** `nightly-consolidation.md` has no counterpart in `memory/canonical-templates/` (confirmed by directory listing). The PR #811 learning (runtime edit without canonical-templates update creates installation-class divergence) does not apply here.

**Patterns introduced:** Proprioceptive feedback loop as a three-layer instruction-layer structure: (1) point-of-miss logging in CLAUDE.md; (2) memory routing in the `subagent_observation` handler; (3) nightly consolidation step reading the accumulated log. This layering is a correct application of the existing observation pipeline — it does not introduce new infrastructure, only new use of existing infrastructure.

**What this forecloses:** Nothing structural. The gate table rows are unchanged. The logging instruction is additive. If the proprioceptive signal turns out to be low-volume (because the dispatcher rarely self-diagnoses), the nightly step still runs harmlessly.

**Opportunity cost note:** The proprioceptive pulse was explicitly named in current_focus.after_that — not current_focus.primary (which is WOS executor starvation). The PR is correctly scoped to the instruction layer with zero Python changes, minimizing cost. The `uv` fix is one line and does not change the approach.

**VERDICT: NEEDS_CHANGES**

**Revision contract:**
- **Gap 1: bare `python3` in nightly-consolidation.md step 1c.** The `python3 -c "..."` invocation must be replaced with `uv run python -c "..."` or `uv run python3 -c "..."`. Resolution criteria: (a) addressed — the revision shows `uv run` at the invocation site; (b) disputed — the author states why `python3` is guaranteed to be in PATH on this instance independent of `uv`, with a specific reason; (c) deferred — the author acknowledges the gap and states why it is not addressed now. The `|| echo 0` pattern is not a sufficient fallback because the failure mode is the entire `python3` invocation producing empty output, not a nonzero exit code.


---

### [2026-04-22] PR #799 — fix: use absolute paths for all oracle/decisions.md references in agent instructions

**Note:** PR #799 was MERGED before oracle review was requested. The following is a post-merge record.

**Vision alignment:** The adversarial prior: this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths. PR #799 is a pure text substitution: two relative `oracle/decisions.md` references in agent instruction prose replaced with `~/lobster/oracle/decisions.md`. The prior asks whether the underlying assumption is wrong. Could canonicalizing these paths create a different failure mode — for instance, if `~/lobster/` is not the canonical install path on all instances? The PR description is scoped to the dcetlin/Lobster instance, and the install path `~/lobster/` is the documented canonical path per CLAUDE.md. There is no evidence of a multi-instance deployment where `~/lobster/` varies. The problem being fixed is real: an agent operating after context compaction would resolve a bare `oracle/decisions.md` relative to its cwd, silently missing the file. The fix eliminates that failure mode without introducing new assumptions. The adversarial prior finds no purchase.

**Alignment verdict:** Confirmed

**Quality finding:**
- **The fix addresses the correct failure mode.** Bare relative paths in LLM instruction prose are resolved relative to cwd at execution time, not relative to the file containing them. After context compaction, cwd is unspecified. Absolute paths eliminate this ambiguity entirely.
- **Scope is appropriate.** Only two locations were modified. The PR description states that other references in `sys.dispatcher.bootup.md` and `sys.subagent.bootup.md` were already fixed in a prior commit on the same branch — consistent with a multi-commit sweep completing an incremental fix.
- **No logic changes, no state transitions, no external dependencies.** Pure text substitution in instruction prose. Risk of regression is effectively zero.
- **Post-merge oracle review has no blocking function.** The record is informational only.

**Patterns introduced:** Absolute path canonicalization for oracle file references in agent instruction prose — a pattern that should be applied to any future references to `~/lobster/oracle/` paths in instruction files.

**What this forecloses:** Nothing. Absolute paths are strictly more reliable than relative paths in this context.

**Opportunity cost note:** Not applicable for a two-line text substitution fix.

**VERDICT: APPROVED** (post-merge record; no merge action required)

---

### [2026-04-22] PR #828 — fix(tests): allow ValueError/KeyError in bot import test

**Vision alignment:** The adversarial prior entering this review: this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths. PR #828 is a test-only fix: it narrows exception handling in `test_bot_module_importable` to remove a bare `except Exception: pass` that was silently swallowing unexpected failures, and explicitly names `ValueError`/`KeyError` as the expected exception types when `TELEGRAM_BOT_TOKEN` is absent. The prior asks: is the underlying assumption wrong? Could fixing the test mask a deeper problem — for instance, that the module should not raise `ValueError` or `KeyError` at import time regardless of env vars? That would be the version of this problem that forecloses better paths. The PR description does not address why the module raises at import time rather than at startup time. However, this is a test-only PR fixing a known CI failure mode in production environments; the module's import-time env var dependency is a pre-existing design fact, not something this PR introduces or validates. The adversarial prior finds no strong purchase. This is the right problem (test was both too silent and too narrow for production env) addressed with a targeted fix.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Removing the bare `except Exception: pass` is the correct fix.** The old handler silently swallowed any exception that was not `ImportError`, hiding unexpected failures. The new form explicitly names `ValueError` and `KeyError` as expected outcomes and lets everything else propagate. This is a direct application of the principle that catch-all handlers obscure failure modes — removing it is strictly better.
- **The `handle_message` attribute assertion was removed alongside the exception narrowing.** The old test asserted that after a clean import, `lobster_bot` exposes `handle_message`. That assertion is gone in the new version. The loss is real: the test no longer verifies structural integrity of the imported module in the happy path (env vars present, clean import). This is a weakening of the test that is not required by the stated fix. The correct minimal change would have been to keep the attribute assertion in the clean-import branch and narrow only the exception handling. As-written, the test now only verifies "the import attempt does not raise an unexpected exception" — it does not verify that the module exposes its public interface after a successful import.
- **The order of `except` clauses is correct.** `except (ValueError, KeyError)` appears before `except ImportError` — Python evaluates `except` clauses in order and `ImportError` is not a subclass of `ValueError` or `KeyError`, so the ordering does not produce any masking. Both branches are reachable without interference.
- **No production code was modified.** All changes are confined to `tests/integration/test_installation.py`. No feature bundling, no path changes, no migrations needed.

**Patterns introduced:** None that propagate. The narrowed exception handling is a local correction to a single test method.

**What this forecloses:** The removal of the `handle_message` assertion makes it harder to detect a future regression where the bot module imports cleanly but no longer exposes its public interface. A future PR could silently remove or rename `handle_message` without this test catching it. This is a minor regression in test coverage that does not foreclose anything significant given the module's current stability, but it is worth noting.

**Opportunity cost note:** Not applicable for a test-only fix of this scope.

**VERDICT: APPROVED**

Advisory note: The `handle_message` attribute assertion was removed as part of this fix but is not required to be absent. A follow-on addition restoring the attribute check in the clean-import branch would improve the test without reopening the CI failure this PR fixes. This is not a merge condition — the CI failure is real and the fix is correct — but it is a named regression in test depth.

---

### [2026-04-22] PR #827 — fix: GardenCaretaker oracle gaps — rate limit, constraint-3, age-only exclusion

**Prior NEEDS_CHANGES verdict:** [2026-04-22] PR #826. Three gaps named.

**Prior gap tracking:**

- **Gap 1 (Constraint-3 compliance):** ADDRESSED. od-3 exists in `~/lobster-user-config/vision.yaml` at `active_project.open_decisions`. The entry explicitly authorizes GardenCaretaker to auto-advance proposed UoWs when the source issue is open AND carries a qualifying label. It explicitly excludes age-only qualification. It names `requalify_proposed()` as the Encoded Orientation decision and cites constraint-3 as the structural anchor. The prior gap required "a prior logged decision of the same class with a traceable vision.yaml anchor" — od-3 is that decision, and it is exactly the form gap 1 named.

- **Gap 2 (Rate limit mitigation):** ADDRESSED. `REQUALIFY_BATCH_SIZE = 20` constant is named (not a magic literal), documented at module scope with a descriptive comment block. `requalify_proposed()` now sorts all proposed UoWs by `updated_at` ascending (oldest-unchecked first) and slices to the constant before the API loop. Debug logging fires when the batch is truncated (len(all_proposed) > REQUALIFY_BATCH_SIZE). At 100 proposed UoWs, full queue rotation occurs in ~5 cycles (~75 minutes). The prior gap required "a configurable batch size, a minimum-interval guard, or a last_checked_at mechanism" — the batch size cap is that mechanism.

- **Gap 3 (Age-only silent promotion):** ADDRESSED. `require_label: bool = False` parameter added to `_qualifies()` with a default that preserves all existing callers unchanged. `scan()` path is completely unmodified — verified by reading the diff (only `requalify_proposed()` calls `_qualifies` with `require_label=True`). The implementation is correct: qualifying label check fires first; if no label, `if require_label: return False` blocks the age path; `_is_old_enough()` is now unreachable from `requalify_proposed()`. The prior gap required "the method is changed to exclude age-only qualification" — this change satisfies that exactly.

**Vision alignment:** The adversarial prior entering this review: these three fixes address named oracle gaps, but does the od-3 decision itself authorize the right thing? The question is whether label-gated auto-advancement changes the "proposed as staging/review area" semantic in a way that forecloses Steward review. The learnings.md entry from PR #826 (2026-04-22) named this exactly: "Proposed state as a staging/review area is a semantic contract with the human reviewer." od-3 resolves this tension by design: the qualifying label (ready-to-execute, high-priority, bug) IS the Steward's review signal. Auto-advancing on a qualifying label is not bypassing Steward review — it is acting on Steward review that has already been expressed. Age-only advancement (now prohibited) was the version that bypassed review. The theory of change is sound. The decision exists. The fixes are targeted. The adversarial prior finds no purchase: this is the right work, correctly scoped, with the right authorization structure.

**Alignment verdict:** Confirmed

**Quality finding:**
- **All three gaps are cleanly addressed.** The diff is 59 additions, 7 deletions, entirely in `src/orchestration/garden_caretaker.py`. No feature bundling — no files touched beyond the named scope. od-3 confirmed present in vision.yaml at `active_project.open_decisions`. The parameter extension pattern (`require_label: bool = False`) is additive and backward-compatible. Batch sort uses `or ""` fallback for None `updated_at` values, which sorts None-timestamp records first — consistent with "longest-unchecked first" intent.
- **Stale test `test_proposed_uow_that_aged_past_threshold_is_auto_advanced` is a stale assertion of prohibited behavior.** The test name describes exactly what od-3 prohibits: age-only auto-advancement through `requalify_proposed()`. The test's failure is not a CI anomaly — it is confirmation that Gap 3 was correctly implemented. The test must be updated or deleted before merge. Options: (a) delete the test if age-only advancement via `requalify_proposed()` is fully out of scope; (b) invert the assertion to verify that age-only UoWs are NOT auto-advanced through the `requalify_proposed()` path. Either resolution closes the stale test; merging with the test intact leaves a known failing test in CI that future PRs must suppress or inherit.
- **No new patterns introduced that propagate problematically.** The `require_label` boolean parameter, the `REQUALIFY_BATCH_SIZE` constant, and the docstring citation of od-3 all follow established conventions in this codebase. The batch-size constant pattern mirrors `_HARD_CAP_CYCLES` and similar named constants.
- **The `requalify_proposed()` docstring was updated** to match the new behavior — both the one-line summary and the behavior description now accurately describe label-only advancement and batch-size limiting. This prevents the "comment/code mismatch at state-machine transition" failure pattern from learnings.md (2026-04-04, PR #607).

**Patterns introduced:** Batch-capped registry query with oldest-first prioritization — a named constant caps external API calls per cycle regardless of queue depth; sorting by `updated_at` ascending ensures fairness across the queue. Reusable for any GardenCaretaker method that loops over a registry query and calls an external source per item.

**What this forecloses:** Age-only proposed→ready-for-steward promotion via `requalify_proposed()` is now structurally blocked. If od-3 is revisited and age-only advancement is later authorized, the `require_label` parameter makes the change minimal: `requalify_proposed()` switches back to calling `_qualifies(snapshot, self.config)` without the parameter.

**Opportunity cost note:** Three targeted fixes resolving a named NEEDS_CHANGES verdict. No opportunity cost relative to current_focus — draining the proposed queue is explicitly named as the primary constraint in vision.yaml `current_focus.this_week.primary`.

**VERDICT: APPROVED — with one merge condition:** The stale test `test_proposed_uow_that_aged_past_threshold_is_auto_advanced` must be removed or inverted before merge. This is a merge condition, not a follow-on task. A failing test that asserts prohibited behavior must not be inherited by main — it creates CI noise and ambiguity for future reviewers. The fix is mechanical (delete the test or invert the assertion) and can be completed in the same merge wave. All three gaps from PR #826 are closed; the merge condition is scoped to the test file only.

---

### [2026-04-22] PR #825 — feat: promote Dispatch template and PR Merge gates to hook enforcement

**Vision alignment:** The adversarial prior entering this review: this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths. The active constraint in vision.yaml is WOS executor starvation and pipeline health — not harness enforcement quality. PR #825 is infrastructure hardening on two advisory gates, not a constraint-relieving fix. However, the adversarial prior finds only partial purchase here. Vision.yaml `principle-3` ("Determinism over judgment for conditionals — if-then logic and field checks are code, not LLM instructions") directly names the design move: both gate conditions are field checks that require no LLM judgment, making hook enforcement the structurally correct encoding. `principle-5` ("Discriminator improvement over rule addition — when a behavioral rule isn't followed, improve the discriminator") supports the mechanism: the existing advisory prose rules are not being followed reliably after compaction, and hooks improve the discriminator rather than adding more prose. The work does not foreclose anything and introduces no irreversible structures. The opportunity cost is real — this scope could have gone to executor starvation recovery — but the PR is narrow (254 additions, 0 deletions, two scripts plus two migrations) and the enforcement benefit is systemic: once installed, it prevents a class of compaction-induced failures indefinitely. The theory of change is defensible even under the current constraint.

**Alignment verdict:** Confirmed

**Quality finding:**
- **`find_oracle_verdict` correctly reads the most-recent entry first.** `decisions.md` is reverse-chronological (newest at top). The function splits on `### [` and iterates forward, returning on the first PR #N match — which is the topmost (most recent) entry. For a PR that has gone through NEEDS_CHANGES → re-oracle → APPROVED, the re-oracle entry appears above the original entry in the file, so the function finds APPROVED and allows the merge. The boundary regex `PR #N(?!\d)` prevents PR #99 matching against PR #999 entries. The comment "most recent entry is the topmost section in the file" is accurate.
- **Dispatcher session detection delegates to `is_dispatcher_session(hook_input)`** from the well-tested `session_role.py`. The fast path (agent_id present → subagent → exempt) is correct for PreToolUse context. The layered fallback (Claude UUID state file → hook marker file → process-tree walk) is the same logic that has been running in production for other hooks. No new session-detection logic is introduced.
- **The "no oracle entry → warn but allow" path in `pr-merge-gate.py` is correctly calibrated.** Infrastructure PRs and pre-oracle installs would be falsely blocked by a strict "no entry → block" policy. The warning-only path gives the enforcement benefit for tracked PRs without creating a bootstrap problem. The PR description's own oracle approval needs to be present in decisions.md before merging PR #825 — which this entry satisfies.
- **Migration idempotency guards use `contains("dispatch-template-check")` and `contains("pr-merge-gate")` in jq.** These match on the command string. The guard condition checks the result against `"0"` (string comparison against an integer-producing jq expression, but the `// echo "0"` fallback makes both cases strings). This is a minor type-coercion quirk consistent with the pattern used in earlier migrations in upgrade.sh and does not produce incorrect behavior.

**Patterns introduced:** Hook-enforcement promotion pattern — an advisory gate that is compaction-vulnerable, stated in a one-sentence trigger, pure string check on tool input → PreToolUse hook. This is the first instance of promoting a Tier-1 gate to mechanical enforcement. The learnings.md addition correctly names the non-promotable boundary: Relay filter, Design Gate, and Bias to Action require semantic judgment and cannot be promoted. This distinction is load-bearing for future gate-promotion decisions.

**What this forecloses:** Nothing of significance. Both hooks can be disabled by removing the upgrade.sh-registered settings.json entries. Neither gate is irreversible.

**Opportunity cost note:** The 254 additions are infrastructure hardening. An equivalent scope applied to executor starvation recovery would have been more aligned with the active constraint. However, the PR is small enough that the opportunity cost argument is weak — this scope prevents an indefinitely recurring class of compaction-induced failures with minimal investment.

**VERDICT: APPROVED**

---

### [2026-04-22] PR #824 — fix: idempotency guard for duplicate proposed UoWs + dedup migration

**Vision alignment:** The adversarial prior entering this review — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — was applied to the framing that this fix serves `current_focus.this_week.primary`: "drain 106 proposed UoWs through confirmed pipeline." The adversarial question is whether 34 duplicates out of 106 proposed UoWs (32%) is the starvation root cause, or whether this is hygiene that doesn't move the pipeline needle. The PR description is explicit: "The idempotency check was already present and correct for new runs — the duplicates were a historical artifact." This means the fix does not prevent future duplicates from a current bug — it removes historical noise. The 'cancelled' addition addresses a legacy status (not in the current UoWStatus enum) that caused re-proposal to be blocked when a cancelled UoW was followed by a new sweep. The PR is correctly scoped: the primary executor starvation was addressed in PR #822 (OAuth token env stripping). PR #824 is a necessary hygiene step — cleaning a population of proposals that would otherwise be visible as false signal in the registry — but it does not unblock the executor by itself. This is consistent with `principle-1: proactive resilience over reactive recovery` applied retrospectively: the dedup prevents the 34 historical duplicates from consuming executor attention. Alignment is confirmed with the note that this is a parallel hygiene task, not the primary starvation fix.

**Alignment verdict:** Confirmed

**Quality finding:**
- **upgrade.sh Migration 80 has a double-increment bug in the `migrated` counter.** The migration script is run inside the `if` condition's pipe (`uv run "$dedup_script" | grep -q ...`), then a `migrated=$((migrated + 1))` is placed unconditionally after the if/else block. In the `else` branch, if the `&&` chain succeeds, `migrated` is also incremented — resulting in a double increment (once from the else chain, once from the unconditional line). The `migrated` counter is reporting-only (gates the "No migrations needed" message), not behavioral, so this is cosmetic rather than a blocking correctness defect.
- **The `if`-branch grep pattern includes `"Nothing to do"` (capitalized) which does not match the script's actual output `"nothing to do."` (lowercase).** The real success cases `"No duplicate"` and `"expired"` do match correctly, so this dead branch is harmless. The script's no-duplicate message matches `No duplicate` correctly.
- **The one-line registry.py fix is correct and minimal.** Adding `'cancelled'` to the non-terminal exclusion list in `_upsert_typed()` is semantically sound: `cancelled` is a legacy terminal disposition equivalent to `expired`. The WHERE clause guard in the migration's UPDATE (`AND status='proposed'`) makes the migration idempotent — re-running does not re-expire already-expired rows. This satisfies `principle-1`.
- **Test coverage is appropriate for the scope.** `test_reinsert_after_cancelled` uses direct SQL injection to set the legacy status, which is the only way to test a status not in the enum. The test comment documents this explicitly, satisfying the learnings.md pattern from PR #738. The 7 migration tests cover the expected boundary cases: no-op, keeps newest, three-way dedup, audit entries, multi-issue isolation, and dry-run correctness. The `uv run "$dedup_script"` invocation in upgrade.sh is compliant with the CLAUDE.md `uv` convention.

**Patterns introduced:** Standalone one-time DB cleanup script with `BEGIN IMMEDIATE` + rollback-on-exception + audit log write, with idempotency enforced by a status guard in the WHERE clause. Reusable pattern for future historical data cleanup scripts that must leave a traceable audit trail.

**What this forecloses:** Nothing. The fix is low-coupling and scoped entirely to the proposed-status deduplication path. No state machine transitions, no executor dispatch paths, and no GardenCaretaker sweep logic are touched.

**Opportunity cost note:** The primary executor starvation was addressed in PR #822. This PR is the follow-on hygiene step correctly sequenced after the starvation root cause fix. The opportunity cost is low — 34 historical duplicates confirmed by dry-run represent a bounded cleanup with no alternative path.

**VERDICT: APPROVED**

---

### [2026-04-22] PR #826 — feat: GardenCaretaker auto-advances proposed UoWs every cycle

**Vision alignment:** The adversarial prior entering this review: this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths. The PR's theory of change is that 106 proposed UoWs are stuck because re-qualification is manual and slow. But vision.yaml `current_focus.current_constraint` names the actual stall: "WOS executor is dispatching UoWs but pipeline has starvation symptoms — RALPH Cycle 7 all 4 UoWs failed, issue sweeper stalled." The proposed→ready-for-steward gate is not named as the bottleneck. The real bottleneck is downstream of it. The PR description corroborates this: the PR explains that UoWs get reset back to `proposed` by `tend()` after source issues reopen — this is a real narrow problem, but the proposed solution is maximal: check all 77+ proposed UoWs against GitHub every 15 minutes, permanently. This is an Encoded Orientation decision (system acts without Dan's explicit input to advance work into the execution pipeline). Inviolable constraint-3 requires such decisions to have "a prior logged decision of the same class and a traceable vision.yaml anchor." No prior logged decision to auto-advance proposed UoWs exists. The vision.yaml `current_focus.this_week.primary` says "drain 106 proposed UoWs through confirmed pipeline" — the word "confirmed" is load-bearing here, and auto-qualification bypasses the confirmation step. The cheaper test that wasn't run: how many of the 106 stuck proposed UoWs are actually re-activated UoWs (tended back to proposed after source reopened) vs. never-qualified seeds? If the re-activation path is rare (which the PR's own description suggests — "a source issue is reopened"), then the 77 API calls/cycle are largely querying UoWs that will never re-qualify, for a problem that arises infrequently. The correct scope fix is narrower: re-qualify only UoWs that were previously qualified and then reset, not all proposed UoWs on every cycle.

**Alignment verdict:** Questioned

**Quality finding:**
- **Implementation is technically clean.** `requalify_proposed()` correctly delegates to the existing `_qualifies()` predicate — no new qualification rules, no new side effects. The `where_status` guard on the transition prevents double-advances. Audit log writes `auto_qualified` to distinguish from seed-time qualification. Error handling is graceful: source errors skip the UoW without blocking the cycle. The implementation does exactly what it says.
- **Rate limit exposure is unmitigated and structurally growing.** 77 proposed UoWs at cycle start = 77 GitHub API calls per 15-minute cycle = ~5,500 calls/day from this method alone, before any other GardenCaretaker source calls. The PR description acknowledges this as "potential rate limit concern" but provides no mitigation. GitHub's unauthenticated rate limit is 60 req/hour; authenticated is 5,000 req/hour. At 77 UoWs, the method alone consumes ~15% of the hourly authenticated budget. As proposed UoWs accumulate, this scales linearly. No backoff, no batching, no rate-limit detection is present.
- **The proposed population is not homogeneous.** The method re-evaluates ALL proposed UoWs, but the PR description names the target problem as UoWs "reset back to proposed after source reopens." Newly seeded UoWs that haven't qualified at seed time (because they lack labels and are too new) will be re-checked every cycle until they age past the 3-day threshold — at which point they auto-advance regardless of whether Dan has reviewed them. The age-based qualification path in `_qualifies()` means any issue open for 3+ days will be auto-advanced into ready-for-steward without label-based signal from Dan. This may be intended behavior but it is a significant expansion of what the PR description implies.
- **Test coverage is thorough for the happy path.** 10 new tests cover: qualifying label, age threshold, blocking label, no source_ref, deleted source, source error, audit event, non-proposed UoWs untouched, run() integration. The test for `test_proposed_uow_without_source_ref_is_skipped` correctly uses direct SQL to simulate a legacy row — this is an explicit seam documented in the test (see learnings.md 2026-04-09 PR #738 pattern). The seam is intentional and documented inline. No test exercises the behavior when a UoW was previously qualified, then reset to proposed by `tend()` — the stated primary use case.

**Patterns introduced:** Auto-advancement of UoWs without an explicit human approval step is now a production pattern in the GardenCaretaker cycle. This is the first instance of an Encoded Orientation decision being added to the GardenCaretaker without a documented prior logged decision and vision.yaml anchor (constraint-3). If this merges, the precedent is set for similar auto-advancement decisions downstream.

**What this forecloses:** The manual approval gate was the enforcement point for Dan's review of work before it enters the execution pipeline. Bypassing it for all proposed UoWs that pass `_qualifies()` makes it structurally harder to reinstate meaningful review — the system will drain proposed state before Dan can audit the pool. The "proposed" state loses its meaning as a staging/review area; it becomes a transient state that clears itself every 15 minutes.

**Opportunity cost note:** The correct narrow fix for the re-activation path is adding a `requalification_eligible` flag or processing only UoWs in `proposed` whose `audit_log` contains a `reactivated` event — this would target the stated problem (tend() re-activations) without promoting all 77+ proposed UoWs automatically. That narrower fix was not pursued.

**VERDICT: NEEDS_CHANGES**

**Revision contract:**

- **Gap 1: Constraint-3 compliance** — An Encoded Orientation decision (system auto-advances UoWs into the execution pipeline without Dan's explicit input) requires a prior logged decision and a traceable vision.yaml anchor. Neither exists. Resolution options: (a) Dan logs an explicit decision in vision.yaml `open_decisions` authorizing this auto-advancement behavior; (b) the scope is narrowed so the system only re-qualifies UoWs that were previously in a higher state and got reset (where Dan's prior approval constitutes the prior logged decision), not all proposed UoWs; or (c) this is explicitly documented as a design decision with a citation to the vision anchor that authorizes it. Addressed = one of these three resolutions is present. Not addressed = the method runs on all proposed UoWs with no documented authorization.
- **Gap 2: Rate limit mitigation** — 77+ GitHub API calls per 15-minute cycle is unmitigated. Addressed = a configurable batch size, a minimum-interval guard between API calls, or a mechanism to skip UoWs that were recently checked (e.g., last_checked_at timestamp). Not addressed = the method calls `source.get_issue()` for every proposed UoW on every cycle with no throttle.
- **Gap 3: Age-qualification silent promotion** — The 3-day age path in `_qualifies()` will auto-advance any issue open 3+ days into ready-for-steward, including issues Dan has not labeled. This is either intended (and should be documented as a policy decision) or unintended (and should be excluded from `requalify_proposed()` — only label-qualified UoWs should be auto-advanced; age-qualified UoWs still require Dan's label signal). Addressed = the PR description explicitly states the age-qualification promotion is intended policy, OR the method is changed to exclude age-only qualification. Not addressed = the PR description still implies this only fires for re-activated UoWs while in practice it fires for all age-eligible proposed UoWs.

---

### [2026-04-22] PR #822 — fix: pass CLAUDE_CODE_OAUTH_TOKEN env to claude -p in executor subprocess dispatch

**Vision alignment:** This fix directly addresses the named `current_focus.current_constraint` in vision.yaml: "WOS executor is dispatching UoWs but pipeline has starvation symptoms." Issue #820 traced 47% of UoW failures to `CLAUDE_CODE_OAUTH_TOKEN` being stripped by cron — exactly the starvation mechanism the current focus names. The adversarial prior (is this solving the wrong problem, or foreclosing a better path?) was actively tested. The alternative — adding the token to the cron environment block directly — would couple authentication to cron config, a less portable and less auditable pattern than explicit env injection at the subprocess boundary. The seam-first golden pattern (place abstraction at the boundary where two systems with different evolutionary rates meet) supports the chosen approach: the subprocess spawn is the correct injection point, and `_build_claude_env()` already existed for this exact purpose. The fix is minimal and precise — it solves the active starvation cause without broadening scope.

**Alignment verdict:** Confirmed

**Quality finding:**
- **`_dispatch_via_inbox` correctly excluded.** Confirmed by reading the implementation: `_dispatch_via_inbox` writes a JSON file to the filesystem inbox and returns immediately — no subprocess spawned, no OAuth token needed. The cron env-stripping problem does not apply to it. The PR's exclusion of this path is structurally sound, not a coverage gap.
- **No circular import.** `steward.py` contains zero imports of `orchestration.executor` or any executor module. The dependency direction is unidirectional: executor imports from steward. `_build_claude_env` is a pure utility function (reads env + credentials.json, returns a dict copy) that belongs to the credential management concern already housed in steward.py. The import is clean.
- **`_build_claude_env` handles the cron case correctly.** The credentials.json fallback reads `creds["claudeAiOauth"]["accessToken"]` — exactly matching the structure written by Claude Code. The test for this path (`test_dispatch_via_claude_p_falls_back_to_credentials_json`) patches `steward_mod._CREDENTIALS_PATH` to a temp file, deletes the env var, and asserts the captured env contains the token from the file. The test JSON structure matches the production code's extraction path. All 3 new `TestAuthTokenPassthrough` tests pass on the PR branch.
- **Pre-existing `TestOptimisticLock` failures confirmed.** Ran against `main` directly: 3 failures, identical error messages, unrelated to auth token injection. The PR did not introduce them.

**Patterns introduced:** The `_build_claude_env()` reuse across executor.py and steward.py confirms the seam-first pattern from golden-patterns.md — a named function at the auth-injection boundary that handles both the interactive and cron cases. No new structural patterns introduced; this PR correctly extends an existing one.

**What this forecloses:** Nothing of significance. The `_dispatch_via_claude_p` / `_dispatch_via_stub` paths are explicitly non-default in production (default is `_dispatch_via_inbox`). The env injection adds no behavioral change for interactive sessions (fast path returns immediately if token is already in env).

**Opportunity cost note:** Fix is two call sites + one import. Minimal surface. The only alternative (cron environment block) would require cron config changes per-deploy and would not benefit from the token refresh logic already present in `_build_claude_env`. The chosen approach is strictly better.

**VERDICT: APPROVED**

---

### [2026-04-22] Re-oracle PR #821 — feat: unified usage-report.sh API with dispatcher wiring

**Prior NEEDS_CHANGES verdict:** [2026-04-22] PR #821. One gap identified.

**Prior gap status:**

- **Gap 1: `python3` bare invocation in `emit_summary`** — ADDRESSED. Commit 190cc7ce replaces `python3 -` with `uv run python -` in the `emit_summary` function in `scripts/usage-report.sh`. The diff shows `uv run python - "${STATE_FILE}" "${LEDGER_FILE}" "${OUTCOME_LEDGER_FILE}" "${WINDOW}" <<'PYEOF'` — the exact substitution named in the revision contract. No remaining bare `python3` references appear anywhere in the PR diff. The learnings.md pattern "Bare `python3` in bash scripts recurred" (2026-04-22, PR #821) names this failure mode and the detection rule; the fix satisfies the detection criterion: `scripts/usage-report.sh` now contains `uv run python -` at the invocation site.

**Vision alignment:** The Stage 1 finding from the original review is carried forward unchanged: the work serves `current_focus.secondary` (usage observability, "cc-budget attribution so WOS parallelism and quota burn are measurable, not guessed"). The adversarial prior — this implementation is solving the wrong problem — was not confirmed by the original review and is not reasserted now. The single-line fix at commit 190cc7ce does not change the scope, architecture, or alignment of the work. Stage 1 verdict stands.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Gap 1 is cleanly closed.** `uv run python -` now appears at the `emit_summary` invocation site. The fix matches the revision contract exactly — "the revision shows `uv run` in the heredoc invocation line."
- **No new issues introduced.** The fix is a single-token substitution (`python3` → `uv run python`) on one line. No other lines in the diff were modified. Regression surface is zero.
- **No remaining `python3` references in the diff.** The grep against the full PR diff for `python3` returned no output. The fix is complete, not partial.
- **The learnings.md pattern (CLI Query Semantics, 2026-04-22) was the active prior.** It constrained my Stage 2 check by directing me to grep the full diff for `python3` — not just check the changed line — because the named failure pattern applies to any `scripts/*.sh` file, not only the specific line fixed. No other `python3` instances exist. The pattern confirmed the fix is sufficient.

**Patterns introduced:** None new. The fix removes the antipattern named in learnings.md (bare `python3` in a bash script). No new structural patterns are introduced.

**What this forecloses:** Nothing. The fix does not change script behavior, output schema, or calling conventions.

**Opportunity cost note:** Single-line fix. Negligible.

**VERDICT: APPROVED**

---

### [2026-04-22] PR #821 — feat: unified usage-report.sh API with dispatcher wiring

**Vision alignment:** The theory of change in vision.yaml is that usage observability should make "quota tradeoffs measurable, not guessed" (`current_focus.secondary`). The horizon field names "cc-budget JSONL fix" as the specific blocker — which implies the underlying data may be unreliable before the fix is applied. The Stage 1 question is whether a reporting wrapper built on top of potentially unfixed data is the right path, or whether the cc-budget JSONL fix should land first. On examination, this threat scenario does not hold: the PR description says the tests ran against live runtime data with valid quota percentages (15.5%/22.3%), and the state.json field path `.rate_limits.five_hour.pct` is confirmed correct against the live file. The "cc-budget JSONL fix" in the horizon appears to refer to attribution metadata, not to the quota state data this script reads. The wrapper does not foreclose the JSONL fix — it reads independently from the state.json and ledger files. The work is correctly aligned with `current_focus.secondary`. The morning briefing omission (no proactive quota surfacing, only reactive query handling) is a scope decision, not a misalignment — the PR description explicitly scopes to dispatcher-query response, and the horizon names it as follow-on. One structural defect applies: `emit_summary` invokes `python3` directly, violating CLAUDE.md's "always use uv" convention. The learnings.md entry (2026-04-20, PR #804) names this failure pattern exactly: bare `python3` silently produces empty output if uv manages the Python installation and `python3` is not in PATH. This is a NEEDS_CHANGES defect, not a misalignment.

**Alignment verdict:** Confirmed

**Quality finding:**
- **`emit_summary` invokes `python3 -` directly — CLAUDE.md violation and learnings.md named failure.** The learnings.md entry from 2026-04-20 (PR #804) states: "Bash inline `python3 -c "..."` in a scheduled task instruction document violates the `uv` convention. The failure mode is silent: if python3 is not in PATH (because uv manages the Python installation), the inline script produces empty output." This PR reproduces the exact pattern. The fix is to replace `python3 -` with `uv run python -` (or equivalent). This pattern constrained what I wrote: rather than flagging this as a style note, the learnings.md entry elevated it to a structural correctness defect with a named failure mode (silent empty output). Without the prior, this would have been a style observation; with it, it is the NEEDS_CHANGES trigger.
- **Field path `.rate_limits.five_hour.pct` confirmed correct.** Live inspection of `~/.claude/cc-budget/state.json` confirms the field path: `rate_limits.five_hour.pct = 15.5`, `rate_limits.seven_day.pct = 22.3`. The `emit_summary` Python block reads `state.get("rate_limits", {}).get("five_hour") or {}` then `.get("pct")` — this matches the live schema.
- **`--window` flag threading is correct.** `emit_flamegraph` delegates as `token-flamegraph.sh --window "${WINDOW}"`. Confirmed `token-flamegraph.sh` accepts `--window 1h|24h|7d`. Argument parsing in the wrapper uses the canonical shift-plus-bottom-shift pattern; no off-by-one. The `--window=` (equals-syntax) form is also handled. Both forms tested against the arg table in the diff.
- **Feature-bundling check passes.** 240 additions, 0 deletions, 2 files changed: `scripts/usage-report.sh` (new) and `.claude/sys.dispatcher.bootup.md` (new section). Both files are explicitly accounted for in the PR description. The learnings.md mechanical check (count files, cross-reference PR description) is negative — no undocumented scope in the diff.

**Patterns introduced:** Wrapper-script entry point for multi-script tooling: a single callable (`usage-report.sh`) that delegates to `cc-usage-collect.sh` state and `token-flamegraph.sh` without modifying either. The Python inline is embedded via heredoc (`<<'PYEOF'`) rather than a separate script file, which keeps the implementation self-contained but couples the Python runtime dependency to the bash script's execution environment.

**What this forecloses:** Nothing structurally. The wrapper is read-only with no state writes. The dispatcher bootup section documents a subagent invocation pattern, which can be updated without a schema migration. Morning briefing integration (proactive quota surfacing) is not foreclosed — it would be an additive wiring step.

**Opportunity cost note:** Usage observability is explicitly named in `current_focus.secondary` and the `current_constraint` field names it as a source of guesswork. This is correctly prioritized per vision.yaml. The primary constraint (WOS executor starvation) is separate and not blocked by this work.

**VERDICT: NEEDS_CHANGES**

**Revision contract:**
- **Gap 1: `python3` bare invocation in `emit_summary`.** Replace `python3 -` with `uv run python -` (or `uv run -`) in the `emit_summary` function in `scripts/usage-report.sh`. Resolution is decidable: the revision shows `uv run` in the heredoc invocation line. Generic "fixed python invocation" without the specific substitution visible in the diff does not close this gap.

---

### [2026-04-21] Re-oracle v3 PR #804 — enhancement: negentropic sweep process improvements (artifact types, vision drift, stall signal, resolution rate)

**Prior NEEDS_CHANGES verdict (v2):** [2026-04-21] Re-oracle PR #804. One remaining gap.

**Prior gap status:**

- **Gap 3 (remaining from v2 — Migration 78 path mismatch):** ADDRESSED. Commit e3dae685 corrects the `ROTATION_STATE` variable in Migration 78 from `${LOBSTER_WORKSPACE}/data/rotation-state.json` to `${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/hygiene/rotation-state.json`. Verification: `hygiene/rotation-state.json` exists at the canonical path; `data/rotation-state.json` does not exist. The `[ -f "$ROTATION_STATE" ]` guard will now evaluate true; `jq -e '.cycle_start_timestamp'` will find the field absent (confirmed: file contains only `current_night` and `last_run`); the migration will write the ISO timestamp and increment `migrated`. The learnings.md pattern "Migration path mismatch produces silent no-op" (2026-04-21) named this defect precisely — tracing the reader rather than trusting the writer's path assumption is what confirmed the fix is now correct.

- **Gaps 1, 2, 4:** Still intact. Confirmed by diff inspection: OR-semantics label queries (`RESOLVED_HYGIENE` + `RESOLVED_BUG` as separate `--label` flags), canonical oracle paths (`~/lobster/oracle/learnings.md` and `~/lobster/oracle/golden-patterns.md`), and Migration 79 archival of `hygiene/sweep-context.md` are all unchanged. Commit e3dae685 touched only `scripts/upgrade.sh` — no regressions possible in other files.

**Vision alignment:** The adversarial prior for this third pass — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — was applied to a single-path-variable correction. The threat scenario that could make this wasted effort: if the real problem is that `rotation-state.json` is written in a location that future code might move, then a migration pointing to the current location defers a future breakage rather than structurally preventing it. This scenario does not survive scrutiny: the path is referenced by the document itself (`~/lobster-workspace/hygiene/rotation-state.json`) and the reader code, and the migration now matches both. The fix is correctly pointed at the defect the prior oracle named. Stage 1 verdict: alignment confirmed.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Gap 3 is cleanly closed.** Migration 78 now targets `hygiene/rotation-state.json`. The file exists there. The field is absent. The migration will fire, write the timestamp, and increment `migrated`. The `[ -f ]` guard that previously silently no-oped will now evaluate true.
- **The fix is surgical — one variable, one file, one commit.** Commit e3dae685 modified only `scripts/upgrade.sh`. No other files were touched. The risk surface for regression is zero.
- **Gaps 1, 2, and 4 are confirmed still intact.** No regressions from the path fix.
- **The learnings.md "Migration path mismatch produces silent no-op" pattern (2026-04-21) was the active prior that constrained this verification.** It directed me to trace the reader (what code reads `rotation-state.json` and from where) rather than trusting the writer's assumption. That trace confirmed the fix targets the correct file. Without this prior, verification might have stopped at "the path string changed" without confirming the file exists at the new path.

**Patterns introduced:** None new beyond those named in the v1 and v2 verdicts.

**What this forecloses:** Nothing. The path fix does not introduce new constraints or close off directions.

**Opportunity cost note:** Single-variable path fix. Opportunity cost is negligible. No alternative path was blocked.

**VERDICT: APPROVED**

---

### [2026-04-21] Re-oracle PR #804 — enhancement: negentropic sweep process improvements (artifact types, vision drift, stall signal, resolution rate)

**Prior NEEDS_CHANGES verdict:** [2026-04-20] PR #804. Four gaps named.

**Prior gap status:**

- **Gap 1 (`--label bug,hygiene` AND semantics):** ADDRESSED. The resolution rate metric now uses two separate queries — `RESOLVED_HYGIENE` and `RESOLVED_BUG` — summed as `closed_prior_count`. OR semantics via separate invocations. The diff confirms this is explicit in the bash block.

- **Gap 2 (wrong oracle learnings path `~/lobster-workspace/oracle/learnings.md`):** ADDRESSED. The new versioned `memory/canonical-templates/sweep-context.md` references `~/lobster/oracle/learnings.md` and `~/lobster/oracle/golden-patterns.md` in Step 1. The string `~/lobster-workspace/oracle/learnings.md` does not appear in the new file.

- **Gap 3 (no migration for `cycle_start_timestamp`):** PARTIALLY ADDRESSED — path mismatch makes the migration a silent no-op on existing instances. Migration 78 exists in `upgrade.sh` and correctly writes an ISO 8601 string (not a Unix integer — the follow-up fix is confirmed). However, Migration 78 targets `${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/data/rotation-state.json`. The actual runtime file is at `~/lobster-workspace/hygiene/rotation-state.json` — confirmed by live inspection. The `data/rotation-state.json` path does not exist. The migration's `[ -f "$ROTATION_STATE" ]` guard will evaluate false, the migration will silently no-op, and the first Night 7 post-upgrade will still fire a false drift warning. The gap named in the prior verdict (false drift warning on first Night 7) is not closed.

- **Gap 4 (no migration for sweep-context.md path change):** ADDRESSED. Migration 79 archives the old runtime copy at `~/lobster-workspace/hygiene/sweep-context.md` to `$OLD_SWEEP.archived-$(date +%Y%m%d)`. The `uv run python` convention fix is present in the vision drift bash snippet in the new sweep-context.md.

**Vision alignment:** The adversarial prior — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — was re-evaluated with attention to whether the re-oracle fixes themselves introduce new defects. The prior Stage 1 finding (alignment verdict: Confirmed) stands unchanged. The revision contract was mostly met; one defect was introduced in correcting Gap 3 — the migration was written targeting the wrong runtime path.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Gap 3 correction introduced a path mismatch.** Migration 78 targets `${LOBSTER_WORKSPACE}/data/rotation-state.json` but the live state file is at `~/lobster-workspace/hygiene/rotation-state.json`. The mismatch is confirmed by live inspection: `data/rotation-state.json` does not exist; `hygiene/rotation-state.json` exists and lacks `cycle_start_timestamp`. The migration will silently no-op — `substep` will never fire and `migrated` will not increment.
- **Gaps 1, 2, and 4 are fully resolved.** The two-query OR semantics for the resolution rate, the canonical oracle path in sweep-context.md, the `uv run python` convention fix, and the Migration 79 archival of the old runtime copy are all correct and present in the diff.
- **The new sweep-context.md is otherwise clean.** The file consistently uses the correct oracle paths, the artifact-type sub-classification table is present and structurally sound, the stall signal logic is complete, and the rotation-state.json path is consistent within the document (the document correctly references `~/lobster-workspace/hygiene/rotation-state.json` — which is the right path; the migration is the one that has the wrong path).
- **double-counting note in the resolution rate metric is correctly self-documented.** The comment "issues with both labels are double-counted, but that is acceptable for this rate metric" is present and accurate — this is not a defect.

**Patterns introduced:** No new patterns introduced by this revision beyond those named in the original 2026-04-20 verdict.

**What this forecloses:** Same as prior verdict — nothing significant.

**Opportunity cost note:** Same as prior verdict.

**VERDICT: NEEDS_CHANGES**

**Revision contract (single remaining gap):**

- **Gap 3 (path mismatch in Migration 78):** Correct the `ROTATION_STATE` variable in Migration 78 to target `${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/hygiene/rotation-state.json` (not `data/rotation-state.json`). Addressed: the path in Migration 78 matches the actual location of rotation-state.json, confirmed by `ls ~/lobster-workspace/hygiene/rotation-state.json` returning the file. Gaps 1, 2, and 4 do not require re-review — they are closed and this verdict does not reopen them.

---

### [2026-04-21] PR #805 — refactor: consolidate write_inbox_message() into shared utility (closes #781)

**Vision alignment:** The adversarial prior — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — was applied against the threat scenario that a deduplication refactor consumes agent cycles while the executor pipeline has starvation symptoms named as this week's primary constraint in current_focus. vision.yaml current_focus.primary is "WOS execution health: fix executor dispatch starvation, resolve RALPH Cycle 7 failures, drain 106 proposed UoWs." This PR addresses none of those. The horizon line explicitly names only executor starvation and cc-budget attribution as next priorities — maintenance refactoring is not listed. That said, the adversarial prior did not survive into a full "Misaligned" verdict because: (1) the work does not foreclose any execution health paths — deduplication is non-blocking and fully reversible; (2) the problem being solved is real (six-way copy-paste of a critical inbox write function creates drift risk on any schema change); (3) `what_not_to_touch` does not name scheduled-task utility consolidation; (4) the scope is cleanly bounded and the implementation correctly defers the structurally different cases (write_task_output, ralph-loop, lobstertalk_unified). The work is questioned on timing, not direction.

**Alignment verdict:** Questioned

**Quality finding:**
- **Feature-bundling check passes (mechanical, per learnings.md PR #712/#717 pattern).** 8 files changed: 6 script updates removing local duplicates, 1 new canonical module, 1 test file. All 8 are accounted for in the PR description. No files outside the stated scope appear in the diff. The check cost 30 seconds and is negative — recorded here because the learnings.md pattern states this check has caught bundling in 4 prior PRs and should be applied every review.
- **Interface migration caller enumeration passes.** The learnings.md pattern from PR #720 requires a repo-wide grep for the function name under migration before the first round. Applied: `grep -r "write_inbox_message"` across the codebase surfaces `lobstertalk_unified.py` (`_write_inbox_message`, different signature — full dict), `ralph-loop.py` (`_write_inbox_message`, different signature — dry_run param), `oom-monitor.py` (different signature — inbox_dir param), and test helpers (inline implementations for test isolation). None of these are parallel implementations of the 6-script pattern; all have structurally different signatures that justify separate treatment. The PR correctly left them out of scope.
- **Return type standardization to `str` is backward-compatible.** Five callers previously ignored the `None` return value; they continue to compile and run without modification. The sixth (auto-router.py) already returned `str` and called the function identically. No TypeError, no silent breakage. The `job_name` parameter addition changes the call signature — all six call sites were updated in the same diff. No missed callers in production scheduled-tasks.
- **Source field fix (`LOBSTER_DEFAULT_SOURCE` vs. hardcoded `"telegram"`) has one behavioral consequence on non-Telegram instances: messages from five scripts that previously hardcoded `"telegram"` will now correctly use the configured source.** This is the only non-zero behavioral change in the PR. It is correct by design and matches the behavior `auto-router.py` already had. No test covers the case where `LOBSTER_DEFAULT_SOURCE` is absent and verifies the default is `"telegram"` — this is covered by `test_source_defaults_to_telegram_when_env_absent`, which passes. No gap.

**Patterns introduced:** Canonical shared utility at `src/utils/inbox_write.py` for the inbox write seam across scheduled-task scripts. This is an application of the seam-first abstraction golden pattern: the function is placed precisely at the boundary where 6 producers with identical output contracts meet the inbox filesystem. The `job_name` parameter in the message ID prefix (e.g., `daily-metrics_<uuid>`) adds traceability that the per-script implementations approximated (some used `{JOB_NAME}_`, some used hardcoded prefixes like `proposals_digest_`) but the shared module standardizes.

**What this forecloses:** Nothing. The shared module is not a one-way door. Scripts can revert to local implementations if the shared module's interface becomes unsuitable. The `write_task_output` functions were correctly left local — the PR description documents the per-script filename convention reason, which is the right basis for the scope boundary.

**Opportunity cost note:** Agent cycle on utility consolidation while current_focus.primary is WOS executor starvation (106 proposed UoWs, RALPH Cycle 7 all-failed, executor dispatch starvation). The PR is correctly scoped and cleanly executed, but the timing is not aligned with this week's stated priority line. This is not a veto on the work; it is a question the builder should hold for future prioritization decisions: hygiene work before the primary constraint is cleared has implicit opportunity cost.

**VERDICT: APPROVED**

---

### [2026-04-20] PR #804 — enhancement: negentropic sweep process improvements (artifact types, vision drift, stall signal, resolution rate)

**Vision alignment:** The adversarial prior — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — was applied with the specific threat scenario: if the sweep's core failure is that escalations aren't being actioned (not that thresholds are undifferentiated), then artifact-type tiers and vision drift detection address symptoms while the actual feedback loop remains broken. This threat scenario partially collapses on examination: improvement #4 (resolution rate + ENTROPY ACCUMULATION signal) directly addresses whether escalations are being actioned — the PR is not blind to the feedback loop failure, it measures it. This is on-vision with principle-3 ("Determinism over judgment for conditionals"). The constraint-3 Encoded Orientation concern was the surviving Stage 1 tension: the artifact-type sub-classification adds three new autonomous action tiers (14/30/60-day thresholds) without a prior logged decision in vision.yaml or decisions.md explicitly authorizing them. However, the prior sweep context already authorized autonomous file removal; this PR adds type differentiation within an existing autonomous category — refinement of encoded behavior, not a new class. The opportunity cost question is clean: this is current_focus.secondary ("Negentropic sweep results available for review"). The adversarial prior did not survive scrutiny on vision alignment. It survives on implementation quality — three defects found that require changes before this PR achieves its stated goal.

**Alignment verdict:** Confirmed

**Quality finding:**
- **`--label bug,hygiene` is AND semantics, not OR (confirmed by live gh CLI test).** `gh issue list --label bug,hygiene` returns only issues tagged with BOTH labels. Sweep-filed issues are tagged `hygiene` or `bug` but rarely both. The resolution rate metric will systematically undercount closed escalations, producing artificially low or zero rates that would incorrectly trigger ENTROPY ACCUMULATION. This is a false positive generator in the metric's core signal. Correct form is two separate `--label` invocations or a search query with OR logic.
- **Wrong oracle learnings path in the versioned sweep-context.md.** Step 1 of the new `memory/canonical-templates/sweep-context.md` instructs the sweep to read `~/lobster-workspace/oracle/learnings.md` — the legacy workspace path that PR #796 established as non-canonical. Canonical path is `~/lobster/oracle/learnings.md`. Since the PR's stated rationale is making sweep improvements versionable and authoritative, shipping the wrong oracle path in the versioned copy undermines the goal. Both paths exist on this instance, so this is not a crash — but it reads from the potentially stale workspace copy rather than the git-tracked repo copy.
- **No migration for `cycle_start_timestamp` causes a false drift warning on first Night 7 post-merge.** `rotation-state.json` currently has no `cycle_start_timestamp` field. When Night 7 runs, the python3 inline script returns `0` for CYCLE_START (the `if ts else 0` branch); `VISION_MTIME > 0` is always true, so the drift warning fires for every Night 7 until a Night 1 writes the field. A migration step populating `cycle_start_timestamp` in the existing rotation-state.json would prevent one guaranteed false positive per instance on the first Night 7.
- **No migration for sweep-context.md path change in upgrade.sh (confirmed absent).** The old runtime copy at `~/lobster-workspace/hygiene/sweep-context.md` remains on disk and authoritative for any instance running a pre-upgrade task file. A migration step that either removes the old copy or writes a redirect notice would seal this gap. `python3` vs `uv run python3` in the vision drift bash snippet is a convention gap (CLAUDE.md: "Always use uv"); failure mode is silent if python3 is not in PATH.

**Patterns introduced:** Artifact-type sub-classification as a named autonomy calibration structure — this is a direct application of the "determinism over judgment for conditionals" principle (principle-3) to the sweep's file-removal decisions. The table format (type/patterns/threshold) follows the table-as-compaction-resistant encoding golden pattern. The resolution rate metric introduces a sweep self-monitoring loop — the sweep now watches its own effectiveness rather than only describing entropy. The ENTROPY ACCUMULATION signal names a failure mode that previously had no detection path. The learnings stall signal names another previously undetectable condition. These are structurally sound additions.

**What this forecloses:** Nothing significant. The versioned sweep-context.md enables future sweep improvements to be PR-reviewed — this is an additive structural change. The artifact-type thresholds can be tuned. The resolution rate metric can be corrected to OR semantics.

**Opportunity cost note:** Sweep infrastructure improvements while WOS executor starvation is current_focus.primary. This is explicitly current_focus.secondary per vision.yaml — within stated scope.

**VERDICT: NEEDS_CHANGES**

**Revision contract:**

- **Gap 1: `--label bug,hygiene` AND-vs-OR defect** — the resolution rate gh query must use OR semantics. Addressed: revise the `gh issue list` command to use two separate `--label` flags (`--label bug --label hygiene`) or an equivalent OR query. "Addressed" is decidable if the revised query returns hygiene-only issues and bug-only issues when tested against dcetlin/Lobster.

- **Gap 2: wrong oracle learnings path in versioned sweep-context.md** — Step 1 must reference `~/lobster/oracle/learnings.md` not `~/lobster-workspace/oracle/learnings.md`. Addressed: the string `~/lobster-workspace/oracle/learnings.md` must not appear in `memory/canonical-templates/sweep-context.md`.

- **Gap 3: no migration for `cycle_start_timestamp`** — upgrade.sh (or user-update.sh for instance-specific) must populate `cycle_start_timestamp` with the current timestamp in rotation-state.json if the field is absent. Addressed: migration step exists and the field is present in rotation-state.json post-upgrade. Alternatively, the bash snippet can be made robust to the absent-field case by treating `CYCLE_START=0` as "drift check not applicable this cycle" and skipping the warning rather than always firing it. Either resolution closes this gap; the PR author may choose which path.

- **Gap 4: no migration for sweep-context.md path change** — upgrade.sh must include a step that removes or tombstones `~/lobster-workspace/hygiene/sweep-context.md` so the old copy is not treated as authoritative. Addressed: a migration step exists that removes or clearly marks the old path as superseded. `python3` → `uv run python3` is a convention fix that may be bundled in the same revision.

---

### [2026-04-20] PR #798 — fix: warn when Claude API token near expiry in _build_claude_env

**Vision alignment:** The PR's theory of change is that a silent 401 failure when a Claude OAuth token expires is indistinguishable from other subprocess errors until logs are examined post-failure, and that earlier warning via structured logging closes this observability gap. The adversarial prior: is this solving the wrong problem? The competing hypothesis is that detection-only is an inferior intermediate state — the root cause (token expiry in a long-running unattended system) should be solved by auto-refresh, not warning logging. Under this reading, the PR adds code complexity while leaving the failure structurally unresolved. However, auto-refresh requires credential management surface that is outside current WOS Phase 1 scope (current_focus.primary is executor starvation and pipeline drain). The PR explicitly defers refresh logic. Detection does not foreclose refresh — any future refresh implementation would need the same expiry-parsing path internally. Vision principle-1 ("Structural prevention is preferred over reactive recovery") supports early detection over post-401 diagnosis; principle-3 ("Determinism over judgment for conditionals — if-then logic and field checks are code") supports encoding the check in the data path rather than expecting it to surface through log archaeology. The feature-bundling learnings.md pattern (recurring across PRs #498, #712, #714, #717) was checked: the diff touches exactly two files (steward.py and a new test file), both enumerated in the PR description — pattern does not apply. The constraint-3 Encoded Orientation check was considered: this PR modifies a production helper function, not an agent definition, bootup file, gate table, or vision.yaml. The check does not apply at this register. Stage 1 verdict: the adversarial prior did not survive scrutiny.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Fast-path exemption is architecturally correct.** When `CLAUDE_CODE_OAUTH_TOKEN` is already set in the environment, it was explicitly injected by the operator or a prior rotation step — the assumption is that the caller knows the token's validity. Reading credentials.json to second-guess an externally-supplied token would be surprising behavior with no action path (the env var is already in flight). The test `test_build_claude_env_fast_path_skips_expiry_check` confirms this path produces no expiry warnings, which is the correct contract.
- **Silent swallow of unreadable credentials is acceptable here.** `_check_token_expiry` is called inside the existing `try/except (OSError, json.JSONDecodeError, KeyError)` block that guards the entire credentials.json read. If the file is unreadable, the outer handler already logs a WARNING — adding a second warning for the expiry check that can't fire is correct omission. The risk is that a corrupt `expiresAt` field (readable JSON, parseable file, but malformed expiry value) silently skips the check; this is handled separately by the inner `except (ValueError, OSError, OverflowError)` in `_check_token_expiry` itself, which falls back to DEBUG. The degradation chain is complete.
- **Unix timestamp handling via `datetime.fromtimestamp(ts, tz=timezone.utc)` is correct for the Claude credential store.** The Claude Code credential store writes `expiresAt` as a Unix millisecond integer in some versions. The PR uses `datetime.fromtimestamp(expires_at, tz=timezone.utc)` which interprets the value as Unix *seconds*. If Claude Code's credential store uses milliseconds (a common Node.js convention), a value like `1745000000000` would be parsed as year 57,000 — producing no warning for an actually-expired token. The PR description states it handles "Unix timestamps (int/float)" but does not specify the epoch scale. This is the one unverified assumption in the implementation. The test uses `datetime.now().timestamp()` which produces seconds — it would not catch a milliseconds mismatch. This is flagged as a quality risk, not a blocker, because: (a) the failure mode is silent omission of a warning (no false positive, no crash), and (b) the ISO 8601 string path is the more common expiresAt format in modern Claude Code credential files.
- **Test coverage is thorough for the stated scope.** 6 pure unit tests for `_check_token_expiry` and 4 integration-style tests for `_build_claude_env`. The fast path, ISO string, Unix timestamp, None, and malformed cases are all covered. The only gap is the milliseconds-vs-seconds ambiguity noted above, which requires inspection of the actual Claude credential store format to resolve definitively.

**Patterns introduced:** Pure helper function pattern (`_check_token_expiry`) with explicit input/output contract isolated from I/O, side effects contained in the helper, and graceful degradation for all malformed inputs. This is a correct application of the functional decomposition principle already present in steward.py. The named constant `_TOKEN_EXPIRY_WARN_SECONDS = 2 * 3600` follows the companion-constant convention (cf. PR #696 learning: "document the invariant the pair is meant to maintain"). No new behavioral patterns at the system level.

**What this forecloses:** Nothing. Detection-only is a one-way step toward full refresh handling, not away from it. The `_check_token_expiry` function is the natural foundation for a future refresh hook at the same call site.

**Opportunity cost note:** The PR is self-bounded to an unaddressed observability gap. The current_focus is WOS executor starvation (106 proposed UoWs awaiting execution, RALPH Cycle 7 failures). This PR does not address the starvation symptoms directly, but the silent 401 failure mode it detects is a candidate cause of executor failures when the OAuth token lapses during a long-running execution window — making it marginally on-path.

**VERDICT: APPROVED**

---

### [2026-04-20] PR #796 — fix: replace relative oracle/decisions.md references with absolute paths

**Vision alignment:** The PR's theory of change is that relative `oracle/decisions.md` references in three agent instruction files resolved to a stale git worktree path instead of `~/lobster/oracle/decisions.md` (the live, git-tracked file). The symptom — merge gate agents reporting "no APPROVED entry" after oracle approval — is precisely what would occur if the reading agent resolved the relative path against a different working directory than the writing agent. The adversarial prior entering Stage 1: is this solving the wrong problem? The alternative failure hypothesis is that context compaction after a long session caused the merge gate instruction to be lost, and the blocking was an orientation failure rather than a path failure. However, the PR cites three specific PRs blocked on 2026-04-09 (#717, #720, #724), which is the same date PR #727 was reviewed for a closely related path inconsistency. The learnings.md entry for PR #727 (2026-04-09) documents the exact failure class: "any time an oracle file path appears in both an instruction document and a code constant, verify they point to the same resolved location." PR #727 closed the `~/lobster-workspace/oracle/` to `~/lobster/oracle/` split; PR #796 closes the residual relative-path references in the same files. The two PRs address different but related expressions of the same underlying problem. Reading learnings.md first caused me to check whether PR #727's noted residual inconsistency (negentropic-sweep.md line 57) was still open — it is not; `negentropic-sweep.md` line 57 now reads `~/lobster/oracle/learnings.md`. One remaining inconsistency exists outside PR #796's scope: `lobster-meta.md` line 14 still reads from `~/lobster-workspace/oracle/learnings.md`. This is not in the PR's stated scope, does not affect the merge gate, and is not a defect introduced by this PR. The adversarial prior did not survive scrutiny. The feature-bundling pattern from learnings.md (PRs #498, #712, #714, #717) was checked first: all three changed files are explicitly enumerated in the PR description — bundling pattern does not apply.

**Alignment verdict:** Confirmed

**Quality finding:**
- **All three relative references are correctly targeted and the replacements are complete.** CLAUDE.md PR Merge Gate row now uses `~/lobster/oracle/decisions.md` in all three places (Trigger, Enforcement, and the "must confirm" clause). `sys.subagent.bootup.md` adds a new line explicitly directing merge agents to the absolute path. `lobster-oracle.md` description prose is updated for consistency. The diff is minimal and precisely scoped — 4 additions, 2 deletions across three files.
- **The sys.subagent.bootup.md change is additive, not a replacement.** The PR adds a new explicit PR-merge instruction line rather than changing an existing reference. This means the merge gate is now reinforced in two places in the subagent bootup: the original frontmatter review context and the new explicit absolute-path reference. This is structurally correct — the additional line serves as a discriminator specifically for the merge-agent case.
- **The PR's own verification grep is complete and appropriate.** The PR states a grep across target files confirmed zero remaining relative `oracle/decisions.md` references without the `~/lobster/` prefix. Current repo state confirms this: only `lobster-oracle.md` line 6 description prose retains `oracle/decisions.md` (non-operative context), and that is the exact line this PR changes.
- **One residual path inconsistency remains outside this PR's scope.** `lobster-meta.md` line 14 reads from `~/lobster-workspace/oracle/learnings.md` — the legacy workspace path. This affects the negentropic sweep's cross-reference signal (lobster-meta reads workspace learnings, oracle agent now writes to repo learnings) but does not affect the PR merge gate. Not a blocker for this PR; should be a follow-on fix.

**Patterns introduced:** Pure document correction — no new behavioral patterns introduced. The fix extends the "absolute path over relative path for cross-role agent instruction files" convention that PR #727 established. When an agent instruction file must reference a file at a known absolute location, use the absolute path. Relative paths in instruction text are unreliable when the agent's working directory is not the repo root.

**What this forecloses:** Nothing. The fix does not prevent future path changes — if `~/lobster/oracle/decisions.md` moves, all three references would need updating, which is the correct behavior. No alternative path resolution strategy is foreclosed.

**Opportunity cost note:** Three-file document correction with four changed lines. The cost is negligible relative to the operational impact of merge gate blockage on multiple PRs.

**VERDICT: APPROVED**

---

### [2026-04-15] PR #753 — fix(observability): compact JSONL ledger format + usage-retro.py (Tier 1)

**Vision alignment:** The active_project phase_intent is "Build the substrate that lets every agent make intent-anchored decisions" — WOS Phase 1 + Vision Object substrate. The current_focus.what_not_to_touch does not name observability, but principle-4 ("Integration rate before new feature rate — wire what exists before building more") is the relevant test. The PR bundles two distinct things: a one-character bug fix (`jq -n` → `jq -cn`) that restores data integrity on 468 existing records, and a new analytical tool (usage-retro.py) that adds surface area outside the current critical path. The adversarial prior: the usage-retro.py component is building a new analytical capability on top of a data layer that has been broken since the ledger was introduced — raising the question of whether the analysis is premature given that the data integrity baseline was never confirmed. However, the bug fix is self-evidently correct: a JSONL format that silently drops all records is a data integrity defect regardless of phase. For usage-retro.py, the vision alignment question is whether the analytical tool serves a decision Dan cannot currently make — the PR's own retroactive findings (oracle subagents and merge subagents are the dominant quota consumers) are a direct input to the kind of intent-anchored routing decisions the Vision Object substrate is meant to support. The world in which the retro tool is wasted effort: the findings it produces are already visible by reading the raw JSONL, or they don't change any routing or budget decision. The PR's own retroactive output (April 7-8 crash day correlation with 191 calls) suggests the data is actionable. Stage 1 verdict: the bug fix is clearly aligned; the retro tool is adjacent to the critical path but serves a real observability gap that the PR demonstrates with concrete findings. One genuine concern: the PR modifies two bootup docs (sys.debug.dispatcher.bootup.md and sys.subagent.bootup.md) without mentioning these changes in the PR description — this is the feature-bundling pattern documented in learnings.md, which has recurred in PRs #498, #712, #714, and #717. The unbundled changes are a subagent type rename (`general-purpose` → `lobster-generalist`), which is a valid maintenance correction but not part of the stated PR scope.

**Alignment verdict:** Confirmed

**VERDICT: APPROVED**

**Quality finding:**
- **The `jq -cn` fix is correct and complete.** The `-c` flag produces compact (single-line) output; without it, jq's multi-line pretty-printed output breaks any line-oriented JSONL parser. The fix is a single character, the root cause is correctly diagnosed, and the consequence (flamegraph silently skipping all 468 records) is verified by the PR's own retroactive output. No issue with this fix.
- **usage-retro.py is well-structured for a one-time analytical script.** The hardcoded EDT offset (`ET = timezone(timedelta(hours=-4))`) is a latent defect: the script will report times one hour off during EST (November through mid-March). Since this is a CLI tool run interactively, the impact is low but the correct fix is `zoneinfo.ZoneInfo("America/New_York")`, which is in the stdlib from Python 3.9+. The LOBSTER_WORKSPACE env var fallback is correct. The JSON decode error tolerance (silently skipping malformed lines) is appropriate for a recovery tool.
- **Feature-bundling: two bootup doc changes are not mentioned in the PR description.** The diff includes `sys.debug.dispatcher.bootup.md` and `sys.subagent.bootup.md` — both renaming `general-purpose` to `lobster-generalist` as the subagent_type. This is the named failure pattern from learnings.md (recurring in #498, #712, #714, #717): "count the changed files, compare against the PR title and description, flag any file not mentioned." These changes are low-risk corrections and are not blocked here, but the bundling pattern should not propagate. The subagent_type rename is a legitimate fix — `general-purpose` appears to no longer be a valid type — but it belongs in its own PR or at minimum in the PR description.
- **usage-retro.py has no wiring to any scheduled job or dispatcher command.** It is a manually-invoked CLI tool. This is appropriate for Tier 1 of the workstream — the goal is retroactive visibility, not automated alerting. No concern here; Tier 2 (role-tagged attribution) is the right place to add wiring.

**Patterns introduced:** Hardcoded UTC offset in timezone handling — `timezone(timedelta(hours=-4))` is a recurring pattern in the codebase that will diverge from ET during EST season. The correct pattern is `zoneinfo.ZoneInfo("America/New_York")`.

**What this forecloses:** Nothing of significance. The bug fix is a pure correction. The retro script is additive. The subagent type rename is a correct maintenance change.

**Opportunity cost note:** The retro script is ~209 lines of clean, focused Python that produces directly actionable findings (oracle + merge + WOS subagents dominate quota). Given that the system hit quota on April 15 (today), the visibility this provides is timely relative to the Vision Object and WOS substrate work. The EDT hardcoding is a minor defect, not a blocker.

---

### [2026-04-09] PR #738 — test(wos): Sprint 4 pipeline test harness (HARNESS-001 through HARNESS-004)

**Vision alignment:** The active_project phase_intent is "Build the substrate that lets every agent make intent-anchored decisions." The current phase's success criteria require the Registry live, vision_ref carried per UoW, and the morning briefing surfacing staleness warnings. This PR closes test coverage gaps (G1-G3) on production paths that are already load-bearing in that substrate. vision.yaml principle-4 ("Wire what exists before building more") directly supports closing test gaps on existing wired infrastructure before layering more features. The adversarial prior: test work can be premature formalization of code that hasn't stabilized, or it can create false confidence through synthetic arc simulations that bypass the real contract boundaries. The Stage 1 question is whether the tested paths are genuinely exercised in production and whether the harness design reaches the actual seams. The world in which this work is wasted: wos_completion.py is dead or transitional code, or the arc harness validates synthetic paths that diverge from the real executor-steward contract because HARNESS-004 bypasses the actual Executor. Having seen the PR: that concern is partially real but is handled correctly — the PR documents why it bypasses the Executor and traces precisely which code path `_simulate_crashed_execution` exercises instead. The G2 gap (zero coverage on `maybe_complete_wos_uow`) is genuinely load-bearing: it is the deferred `execution_complete` transition in the async inbox dispatch path, and untested filtering logic there would be invisible to any regression. Stage 1 verdict stands as Confirmed.

**Alignment verdict:** Confirmed

**VERDICT: APPROVED**

**Quality finding:**
- **wos_completion.py unit tests are thorough and correctly isolated.** The 12-test suite covers the full behavioral surface: prefix gate, error-status no-op, executing+success transition, audit entry, not-found skip, duplicate idempotency, missing DB, done skip, registry exception absorption, and constant value anchoring. The exception absorption test patches at the correct module level (`orchestration.registry.Registry`) and the idempotency test verifies the second call lands on a non-executing UoW rather than retrying the transition — both are non-obvious behaviors that would otherwise be invisible to a regression.
- **HARNESS-004 crash path design is sound and well-documented.** The decision to bypass `Executor.execute_uow()` and inject directly via `registry.record_startup_sweep_active(classification="crashed_no_output")` is correct: running the real Executor writes an `execution_complete` audit entry that `_most_recent_return_reason()` treats as authoritative, masking the crash simulation. The PR docstring traces exactly which code path in `_detect_stuck_condition` this exercises and why `classification` is the fallback read. This is a principled bypass, not a lazy shortcut — it mirrors the exact path `startup-sweep.py` takes in production.
- **One assertion softening in AC-6 is a minor divergence from spec.** HARNESS-001 asserts `steward_cycles >= 1` rather than `== 2` as the design doc specifies, with a comment explaining the 0-indexed cycle counting. The comment is honest but the spec drift introduces test tolerance that could mask a regression where steward_cycles is 1 when it should be 2. The `>= 1` form passes even on a single-pass arc where the closure happened in the same heartbeat as the prescription. This is low-severity but is a named discrepancy.
- **vision_ref is seeded via direct SQL in both test files because the public upsert API does not yet accept it.** This is correct practice for test isolation and is explicitly acknowledged in both test comments. However, it also signals that the public API seam for vision_ref is not closed: a future test or sweeper that uses `registry.upsert()` to set vision_ref will need this path to be opened. The test pattern correctly surfaces this gap without creating a workaround that hides it.

**Patterns introduced:** Direct SQL injection as explicit API seam surfacer — using direct SQL writes in tests when the public API doesn't yet accept a field makes the gap visible and documents it at the test level rather than papering over it with a private method. Pure mock helper factories (`_make_capturing_notify_dan`, `_make_mock_dispatcher`) returning `(fn, log_list)` tuples are the correct functional pattern for capturing side-effects in integration tests without reaching into internal state.

**What this forecloses:** Nothing — no production code is modified. The only mild foreclosure is the `>= 1` AC-6 form: if this becomes a canonical harness people copy, the soft assertion tolerance propagates.

**Opportunity cost note:** No production feature was deferred by this PR. Coverage work on existing load-bearing paths is aligned with principle-4 and does not compete with the current phase's critical path (sweeper, vision_ref wiring, morning briefing staleness check).

---

### [2026-04-09] PR #732 — docs: mode-discriminator rewrite for Design Gate vs. Bias to Action

**Vision alignment:** The active operating principle from vision.yaml is principle-5: "When a behavioral rule isn't followed, improve the discriminator — do not add more rules." Issue #225 is a premise-review escalation naming exactly this failure: the existing description resolves the gate tension via priority ordering rather than discriminator improvement. This PR is directly responsive to that named principle. The adversarial prior entering Stage 1: the fix may describe the correct taxonomy without providing a decision-procedural discriminator — explanation where the failure requires classification. What the dispatcher needs is a test it can apply under working memory pressure that returns a binary answer before consulting the gate table. The Stage 1 question is whether the new section provides that test or merely explains why the confusion exists. The world in which this work is wasted: the new section is explanatory prose that requires the dispatcher to synthesize a classification from multiple signals, which is the same cognitive load as the original ambiguity expressed differently.

**Alignment verdict:** Confirmed

**Quality finding:**
- **The discriminator is decision-procedural, not explanatory.** Step 1 is a binary question ("Can you state the concrete output artifact in one sentence from the message alone?") with a binary Yes/No branch to ACTION or DESIGN_OPEN mode. This is structurally the same form as the DESIGN_OPEN trigger in the existing gate table (also a one-sentence artifact test), but it is presented as the classification entry point rather than buried in a gate row. A dispatcher reading under context pressure encounters the question before the table, not after.
- **The signal lists are positive discriminators, not ambiguity zone descriptions.** Each mode lists signals where any single signal is sufficient — not a weighted combination requiring synthesis. This is the correct form for a gate selector applied under compaction pressure. The DESIGN_OPEN signals list is complete: it covers problem-without-deliverable, exploratory language, and clarification-required — the three configurations where the original table was most ambiguous.
- **The parenthetical priority disclaimers are cleanly removed.** The Design Gate and Bias to Action rows in the table previously carried inline text asserting "table position does not imply priority." That inline assertion acknowledged the problem while embedding the ambiguous table position as the document structure. The new section solves the problem structurally: mode recognition is the pre-table step, so the table rows do not need disclaimers. The removal is correct.
- **One residual ambiguity: the Bias to Action gate row still says "fire only after DESIGN_OPEN has been ruled out."** With the new mode-recognition section preceding the table, this is now redundant — the dispatcher arrives at the Bias to Action row only after having classified to ACTION mode. The redundancy is benign but creates a minor contradiction: if the dispatcher has already run Step 1 and landed in ACTION mode, the phrase "after DESIGN_OPEN has been ruled out" implies a second DESIGN_OPEN check inside the table. This is not a blocking defect — the behavior is correct — but it is a residual of the old priority-implication phrasing that the PR didn't fully clean up. A follow-on cleanup could remove that phrase.

**Patterns introduced:** Mode-recognition-as-pre-table-gate. The discriminator section establishes that gate selectors run before the gate table, not inside it. This is the correct structural form for a classifier that controls which of several mutually exclusive gates applies. If additional pairs of mutually exclusive gates are added in the future, this section is the correct precedent for how to express the exclusivity.

**What this forecloses:** Nothing. The enforcement logic for Design Gate and Bias to Action is unchanged. No architectural direction is closed.

**Opportunity cost note:** Issue #225 was open since 2026-03-23 and was labeled premise-review. The fix is a 24-line documentation change. The cost of not shipping was dispatcher misidentification on every ambiguous message during that period. No opportunity cost to this work.

**VERDICT: APPROVED**

---

## [2026-04-09] PR #731 — fix(wos): move evaluated counter after backpressure gate

**Vision alignment:** This is a counter-semantics correctness fix in `run_steward_cycle`. The adversarial prior — that "evaluated" could legitimately mean "UoWs considered for processing (including skipped ones)" — does not survive inspection of the CycleResult naming convention and the `evaluated`/`skipped` pairing: the pair only has coherent meaning if they partition the candidate set, not if they overlap. The work was surfaced by the negentropic sweep acting on a learnings.md pattern filed 3 days earlier (the "evaluated counter incremented before guard gate fires" pattern from 2026-04-06). This is the oracle loop functioning as designed. The fix is a one-line relocation with no architectural surface, no interface changes, no new patterns introduced, and nothing foreclosed. It directly serves `principle-3` (determinism over judgment for conditionals — counter arithmetic is code, not inference). No deeper premise is implicated.

**Alignment verdict:** Confirmed

**Quality finding:**
- `evaluated += 1` at line 3814 is placed after the complete `if _sweep_classification == "executor_orphan":` block, meaning it fires for UoWs that (a) have no executor_orphan classification, and (b) have the classification but fall through because execution is currently enabled. Both paths correctly reach processing. The placement answers the key review question affirmatively.
- The BOOTUP_CANDIDATE_GATE (lines 3743-3752) also has `skipped += 1; continue` that fires before line 3814. Both early-exit gates are excluded from `evaluated`. The fix is complete.
- The 5 pre-existing test failures are confirmed unrelated: they trace to `issue_info.body` AttributeError (the IssueInfo dataclass migration defect already in learnings.md from PR #720). They do not touch the backpressure counter path.
- 4 backpressure-specific unit tests pass and directly cover the counter behavior under test.

**Patterns introduced:** None. One-line relocation of existing arithmetic.

**What this forecloses:** Nothing. Counter placement cannot foreclose architectural directions.

**Opportunity cost note:** Not applicable — this is a 3-day-old learnings.md entry being closed by its prescribed fix. The sweep-to-fix latency is appropriate.

**VERDICT: APPROVED**

---

## PR #726 — WOS front-matter+prose artifact format

**Date:** 2026-04-09
**Task ID:** oracle-pr726-wos-frontmatter
**Verdict:** APPROVED

Stage 1 adversarial prior did not survive: the pure-JSON format was causing concrete JSONDecodeError failures when LLMs emitted preamble before the artifact block — this is damage repair, not scope expansion.

Key findings:
- from_frontmatter() uses json.loads() on the entire envelope line — no colon-truncation risk.
- The ---json sentinel is distinct from bare --- used by _parse_workflow_artifact in steward.py.
- The four steward tests updated by inspection are the complete set that read prescription artifacts from disk.
- One latent issue noted in learnings.md: instructions prose containing a standalone --- line would silently truncate the artifact. Low-probability; not blocking.

**Advisory (non-blocking):** Misleading comment in from_frontmatter about "stripping a leading newline" — inaccurate but behavior is correct. Also: standalone --- in instructions prose would silently truncate; low-probability edge case.

## [2026-04-01] PR #551 — feat(monitoring): file-size-monitor for bootup/config files

### Stage 1: Vision alignment (formed before reading implementation)

**Vision alignment:** The PR addresses a documented production bug — `sys.dispatcher.bootup.md` silently exceeded the Read tool's 2,000-line limit by 403 lines, making Voice Note Brain Dumps, Google Calendar, and Context Recovery sections invisible on every agent startup. The theory of change is observability-first: detect size drift via a weekly cron-direct script, file a GitHub issue, let the operator prune. The vision tension is real: `principle-1` ("Proactive resilience over reactive recovery — structural prevention is preferred over better correction mechanisms") points toward compression of the bootup docs themselves rather than a monitor. The golden pattern "compression as architectural response to accumulation critique" (golden-patterns.md, 2026-03-23) names the structurally correct intervention: compress encoding, do not add monitoring infrastructure. The learnings.md pattern "absorption-ceiling response via context-expansion" (2026-03-23) is a near-relative: adding a safety net below a growing document does not address the growth. The monitor normalizes operating near the threshold rather than enforcing structural limits at write time. That said, the adversarial prior is not confirmed: the underlying bug is real, the monitor does not foreclose compression (the harder fix remains open), and it introduces no LLM cost, inbox writes, or screen dependency. It is a lightweight symptom-layer response to a cause-layer problem that has not yet been addressed. The cause-layer fix (compression) is not foreclosed but is also not prompted by this PR.

**Alignment verdict:** Questioned

### Stage 2: Quality review

- **Does it do what it claims?** Yes. `check_files()` walks `FILE_THRESHOLDS`, counts lines via binary read (correct — no encoding ambiguity), logs each result, and builds violation dicts. `fetch_open_issue_titles()` fetches the first 200 open issues by title and uses set membership for deduplication — correct for the expected volume. `file_github_issue()` calls `gh issue create` with proper timeout and error degradation. Dry-run mode is a clean code path.
- **Issue title deduplication has a fragility:** The deduplication key is the exact issue title string `"warn: {rel_path} exceeds {threshold}-line threshold ({actual} lines)"`. If the file oscillates around the threshold between runs, the actual line count will differ across weeks and the title will not match — a new issue is filed even if a prior one is open for the same file. The correct deduplication key should be file-stable (e.g., `warn: {rel_path} exceeds {threshold}-line threshold`) without the actual count. This is a concrete defect that will produce issue spam under normal fluctuation conditions.
- **REPO constant points to SiderealPress/lobster, not dcetlin/Lobster.** This is the upstream repo. For a dcetlin fork install, the `gh` CLI's default remote may or may not be SiderealPress; if it is dcetlin/Lobster, issues will be filed to the wrong repo. This should be derived from the git remote or made configurable via env var, consistent with the `LOBSTER_WORKSPACE` env var pattern already used in the script.
- **Cron entry in upgrade.sh uses `$HOME/.local/bin/uv` directly** rather than the `uv` path resolved via PATH. If uv is installed elsewhere, the cron entry silently fails. The pattern used elsewhere in the system is `command -v uv` or the `uv` wrapper — this should follow the same convention.
- **Threshold for `oracle/learnings.md` is 300 lines.** The file is already over 300 lines (it was over 100 lines in just the first 100 lines read). This will fire immediately on the first live run, which may or may not be intentional. If intentional (backlog of existing issues to clear), the issue body should say so; if not, the threshold needs recalibration.

**Patterns introduced:** cron-direct observability scripts that file GitHub issues on threshold breach; deduplication by exact-title set membership; `--dry-run` mode as first-class script behavior.

**What this forecloses:** Nothing structural. The compression path (golden pattern) remains open. Future operators may develop tolerance for "approaching-but-not-exceeding" thresholds because the monitor exists — this is a soft foreclosure of compression urgency, not a hard architectural one.

**Opportunity cost note:** The structurally correct intervention — applying table-as-compaction-resistant encoding to compress the bootup docs — was not built instead. That work remains in the backlog. This PR creates monitoring without addressing growth discipline.

**Verdict: NEEDS_CHANGES**

Issues requiring resolution before merge:
1. Deduplication key includes the actual line count, causing issue spam when a file oscillates around the threshold. Remove the count from the title used for dedup (keep it in the issue body).
2. `REPO = "SiderealPress/lobster"` is hardcoded. Should be derived from git remote or overridable via env var (e.g., `LOBSTER_GITHUB_REPO`) consistent with other env-var patterns in the script.
3. Cron entry hardcodes `$HOME/.local/bin/uv` — should use `$(command -v uv)` or the system's canonical uv path convention used elsewhere in upgrade.sh.
4. `oracle/learnings.md` threshold of 300 lines is already exceeded. Recalibrate or document that the first run is expected to fire.

---

### [2026-04-09] PR #730 — fix: off-by-one in _count_non_improving_gate_cycles early-return branch

**Vision alignment (Stage 1 — written before evaluating implementation):**
The adversarial prior: is this fix solving the wrong problem, or does the prescription (`return non_improving`) correct the bug without the engineer's conditional? The learnings.md entry for this exact bug (2026-04-08, "off-by-one in reverse-scan non-improving counter") names `return non_improving` as the correction. But the prescription was written before the fully-improving edge case was traced through. The Stage 1 question is whether `return non_improving` actually holds for all cases, or whether the prescription itself has a defect the engineer correctly identified. The function's governing structure role (counting non-improving cycles to gate `no_gate_improvement` stuck-condition detection) is central to Sprint 3 commitment gate work — getting this wrong delays gate intervention. The fix either repairs or preserves the off-by-one. The right posture is to treat the prescription as a hypothesis, not a ground truth. What would have to be true for the prescription to be correct? The prescription's example in the issue (`[0.5, 0.8, 0.5, 0.5]` → result=2) would have to be correct. If that example itself is wrong, the prescription is wrong. This is exactly what must be verified in Stage 2.

**Alignment verdict:** Confirmed

**Quality finding:**
- The prescription `return non_improving` in issue #728 is incorrect for the fully-improving case. Trace: `[0.5, 0.7, 0.9]` — i=2 finds improvement immediately, `non_improving=1`, `return 1` — but the correct answer is 0 (tail is improving, not stalled). The engineer's observation that the prescription breaks the fully-improving case is verified.
- The engineer's fix `return non_improving if non_improving > 1 else 0` is correct for all verified edge cases: (a) fully-improving `[0.5, 0.7, 0.9]` → `non_improving=1` → returns 0; (b) improvement-then-plateau `[0.5, 0.5, 0.8, 0.8]` → `non_improving=2` → returns 2; (c) all non-improving `[0.5, 0.5, 0.5]` → falls through, unaffected by this change; (d) single-element or two-element fully improving `[0.5, 0.9]` → `non_improving=1` → returns 0.
- The issue's stated example `[0.5, 0.8, 0.5, 0.5]` → result=2 is itself incorrect (the correct result is 3: the non-improving tail from index 1 onward is [0.8, 0.5, 0.5]). The engineer correctly identified this discrepancy and substituted the better regression case `[0.5, 0.5, 0.8, 0.8]` → result=2. The regression test's exact-equality assertion (`assert result == 2`) covers the case the old relational assertion (`result < NON_IMPROVING_GATE_THRESHOLD`) could not.
- The learnings.md "test coverage gap hides early-return path bug" pattern directly constrained what I looked for: the regression test must use exact equality (`result == N`), not a relational bound. The new test uses `assert result == 2`. This satisfies the pattern's detection rule. The pattern's effect on my analysis: I did not accept the passing test count (35) as evidence of correctness without verifying the assertion form.
- 35 tests pass in the target file. The pre-existing `ModuleNotFoundError` in `test_attunement.py` is unrelated (declared pre-existing by engineer; visible as a collection error, not a test failure in this file).

**Patterns introduced:**
- Exact-equality regression test as the canonical form for counting function bugs: `assert result == N`, not `result < THRESHOLD`. The existing test `test_resets_after_improvement` retains the weaker relational form and remains weak (though it now passes for the right reason). No cleanup of the existing test was required — its weakness is documented in learnings.md.
- Conditional zero-return on `non_improving > 1` as the correct form for the "improvement found at first check" edge case in reverse-scan counters: when `non_improving` was initialized to 1 but the loop immediately finds improvement (before any confirmed non-improving pair is counted), the correct return is 0, not 1.

**What this forecloses:**
Nothing. Pure function change, no structural impact on callers. The gate's behavior is now correct; it fires 1 cycle sooner when the UoW showed improvement before stalling.

**Opportunity cost note:**
This is a learning-not-remediated bug from the 2026-04-08 oracle cycle, surfaced by the negentropic sweep. Correct gate timing is a prerequisite for the Sprint 3 commitment gate to function as specified — an off-by-one here delays stuck-condition detection by one full steward cycle per affected UoW.

**VERDICT: APPROVED**

---

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

---

## [2026-04-01] PR #550 — fix(wos-report): send PDF as Telegram document directly (first review)

### Stage 1: Is this solving the right problem?

**Q: Is replacing the outbox queue with a direct Bot API call the correct direction?**

The outbox queue approach (`queue_for_telegram`) writes a JSON file to `~/messages/outbox/` and relies on the Telegram bot process to pick it up later. This creates a hidden dependency: if the bot is not running when `wos_report.py` is invoked, the PDF is silently queued and never delivered. The fix eliminates this intermediary by calling the Telegram Bot API directly from `wos_report.py`.

Decision: the direction is correct. The script already knows the bot token and chat ID; calling the API directly removes the delivery dependency without adding new external coupling (the Telegram API is already a boundary this system crosses). STAGE 1: APPROVED.

---

### Stage 2: Is the implementation well-made?

**Q: Is using `curl` via subprocess the right transport mechanism?**

Finding — wrong abstraction at the HTTP transport boundary:
`send_document_direct` shells out to `curl` to perform the multipart/form-data POST. This introduces a hard runtime dependency on `curl` being installed and available on PATH. Python's stdlib provides `urllib.request` and `http.client`, which can perform the same multipart upload without shelling out. The rest of the Lobster codebase does not use subprocess for HTTP calls — this is an inconsistency.

Additionally: the Telegram Bot API token appears in the URL string (`https://api.telegram.org/bot{token}/sendDocument`), which is passed as an argument to the `curl` subprocess. The token is visible in `/proc/*/cmdline` and `ps aux` output for the duration of the subprocess call. Using `urllib.request` keeps the token entirely in-process.

This is a NEEDS_CHANGES item: replace the `curl` subprocess with a pure-Python `urllib.request` multipart upload.

**Q: Is `import subprocess` inside the function body correct style?**

Finding — deferred import that should be at module level (moot if subprocess is removed):
`import subprocess` is placed inside `send_document_direct()` rather than at module scope. Standard convention for this codebase is top-level imports. This is a minor style issue that becomes irrelevant if `subprocess` is removed entirely (as required by the finding above).

**Q: Is the JSON parse guarded against malformed output?**

Finding — bare `json.loads(result.stdout)` has no guard:
If `curl` returns empty stdout or non-JSON content (e.g., a network error page), `json.loads` raises `JSONDecodeError` with no contextual information. The error handler only catches `result.returncode != 0`. This gap means certain failure modes (curl exits 0 but returns non-JSON) produce uninformative errors. This is a secondary issue that also becomes moot when subprocess+curl is replaced with `urllib.request`, whose response handling can be structured correctly.

**Q: Are the token-loading and document-sending functions well-decomposed?**

Finding: `_load_bot_token()` is a clean pure function with clear fallback logic. The decomposition between token loading and sending is correct. The logic inside `send_document_direct()` (build URL, post file, check response) is the right scope for one function. The structural decomposition is sound. APPROVED.

---

### Overall verdict: NEEDS_CHANGES

**Required before merge:**
1. Replace `curl` subprocess with `urllib.request` multipart upload — eliminates the external binary dependency and keeps the token in-process (not visible in `/proc`/`ps`).
2. Remove `import subprocess` (made unnecessary by fix 1).
3. Add a JSON parse guard in the response handler: catch `json.JSONDecodeError` and re-raise as `RuntimeError` with the raw response text included.

No other files need to change. The `_load_bot_token()` function and the overall structure are correct.

---

## [2026-04-01] PR #550 — fix(wos-report): send PDF as Telegram document directly (re-review after fixes)

### Changes reviewed

The follow-up commit replaces the `curl` subprocess with a `urllib.request` multipart/form-data upload:
- `subprocess` import removed entirely
- `mimetypes` and `urllib.request` imported (deferred inside function — consistent with precedent in this file)
- Multipart body assembled as bytes using a fixed boundary string
- `urllib.request.urlopen` performs the POST with a 60-second timeout
- `json.JSONDecodeError` caught and re-raised as `RuntimeError` with the raw response included

### Stage 2 re-check

**Q: Is the multipart encoding correct?**

Finding: The body assembles three parts — `chat_id`, `caption`, and `document` (binary). Each field part uses `\r\n` line endings per RFC 2046. The file part correctly sets `Content-Type` to the guessed MIME type (fallback `application/pdf`). The closing delimiter `--{boundary}--\r\n` is correct. The `Content-Type` header on the request includes the boundary parameter. APPROVED.

**Q: Does `urlopen` raise on HTTP errors?**

Finding: `urllib.request.urlopen` raises `urllib.error.HTTPError` (a subclass of `IOError`) for HTTP 4xx/5xx responses. This propagates naturally to the caller. The success path checks `response.get("ok")` — Telegram always returns 200 OK even for logical errors (e.g., wrong chat_id), so the `ok` check is the correct semantic gate. Both transport errors (HTTPError) and API logical errors (ok=false) are handled. APPROVED.

**Q: Is the deferred import style consistent with the file?**

Finding: The file already has function-level deferred imports in another section. The style is established precedent here. APPROVED.

**Q: Are all three NEEDS_CHANGES items resolved?**

1. curl replaced with urllib — YES.
2. subprocess import removed — YES.
3. JSONDecodeError guard added — YES.

### Overall verdict: APPROVED

**PR #550** is approved for merge. All three NEEDS_CHANGES items are addressed. The urllib multipart implementation is structurally correct, keeps the token in-process, and handles both transport and API-level errors.

---

## [2026-04-04] PR #601 — feat(wos-v3): schema migration — register field + corrective_traces

### Stage 1: Is this solving the right problem?
The schema change operationalizes V3's three new structural primitives: register (attentional configuration), corrective_traces (learning artifacts), and delivery≠closure (closed_at/close_reason). These are the exact gaps V3 identified in V2. STAGE 1: APPROVED.

### Stage 2: Is the implementation well-made?
Migration is clean and additive — all four ALTER TABLE statements backfill safely with correct NULL defaults. executor_uow_view rebuild is correct for SQLite (drop+recreate pattern). delivery≠closure fields correctly declared with NULL defaults (enforcement appropriately deferred to Steward). Two advisory gaps noted but not blocking: (1) no CHECK constraint enforcing valid register values at the DB layer — immutability is advisory, enforced only through the Registry class; (2) corrective_traces has no FK constraint on uow_id. Neither blocks merge — the Registry class enforces both constraints adequately at the application layer for the current scale.

### Overall verdict: APPROVED

---

## [2026-04-04] PR #602 — feat(wos-v3): register classification at germination (Germinator/Registrar)

### Stage 1: Is this solving the right problem?
PR #602 operationalizes register classification at germination — the gate that makes register immutable from the moment of creation. This is the structural mechanism that prevents category-wrong dispatch. The 4-gate ordered algorithm matches the V3 design spec precisely. STAGE 1: APPROVED.

### Stage 2: Is the implementation well-made?
The `RegisterClassification` frozen dataclass with observability fields (gate_fired, evidence, confidence) is the right design — see golden-patterns.md for this pattern. The Cultivator→Germinator handoff is correctly wired. WOS-INDEX.md naming resolution is correct. 22 tests cover all gates and ordering.

One mandatory fix before merge: remove `"register"` from `_PHILOSOPHICAL_TERMS` in `germinator.py` (approx line 153). `_is_philosophical()` uses boolean frozenset intersection — single token match fires Gate 3 at full confidence. The word "register" appears throughout V3 technical writing (register field, register mismatch gate, classify_register), causing systematic false positives on V3 meta-issues. Direction of failure is conservative (held for Dan review rather than category-wrong dispatch), but it degrades the philosophical register's signal value.

Fix applied before merge: one-line removal.

### Overall verdict: APPROVED (with pre-merge fix applied)

---

## [2026-04-04] PR #607 — feat(wos-v3): corrective trace mandatory one-cycle temporal gate (re-review after NEEDS_CHANGES)

### Stage 1: Vision alignment (formed before reading implementation)

**Vision alignment:** The adversarial prior entering this re-review: were the three NEEDS_CHANGES items fixed at root, or were they surface patches that leave the underlying structural problem in place? The learnings.md pattern "inter-component contract introduced without upstream documentation" (2026-03-30) constrained Stage 1 materially — the gate depends on the executor writing trace.json, but executor.py does not write trace.json. This means the "contract violation" path is not the exceptional path; it is the permanent production path for every UoW until a separate executor PR ships. The gate adds a mandatory one-cycle delay to every UoW that completes with result.json (which is all of them), plus a contract violation log entry on every second steward cycle visit. This is not a defect in the gate's logic — it is a consequence of the gate shipping ahead of its producer. Vision principle-1 ("proactive resilience over reactive recovery") supports the gate's intent. Vision principle-4 ("integration rate before new feature rate") raises the question of whether the gate should be gated on executor trace.json support shipping in the same PR or wave. The adversarial prior is not confirmed as misaligned — the gate is correctly designed to handle the "no trace.json" case gracefully — but the production behavior until executor trace.json support ships is: every UoW waits one extra cycle, then proceeds as a contract violation. This should be documented.

**Alignment verdict:** Confirmed

### Stage 2: Verification of three NEEDS_CHANGES items

**Item 1 — State transition: skip path leaves UoW at ready-for-steward**

VERIFIED. The transition call `registry.transition(uow_id, _STATUS_READY_FOR_STEWARD, _STATUS_DIAGNOSING)` uses the API's `(uow_id, new_status, from_status)` signature — it transitions FROM `_STATUS_DIAGNOSING` TO `_STATUS_READY_FOR_STEWARD`. The fix agent confirmed this was already correct in the original commit; commit 7fa2c63 documents it as confirmed rather than changed.

**Item 2 — Timestamp in wait_entry**

VERIFIED. Commit 7fa2c63 adds `"timestamp": _now_iso()` to the `wait_entry` dict. The field is present in the diff. Fix is complete.

**Item 3 — Dan notification on contract violation path**

VERIFIED. Commit 7fa2c63 adds:
```python
_notify_cv = notify_dan or _default_notify_dan
_notify_cv(
    uow,
    f"Executor contract violation: trace.json absent after one-cycle wait for UoW {uow_id}. "
    f"Prescribing anyway — check executor output at {output_ref_for_gate}.",
)
```
The notification call is present. Fix is complete.

### Stage 2: Quality review (full)

All three NEEDS_CHANGES items are resolved. Residual quality observations:

- **`violation_entry` dict lacks a `timestamp` field.** The `wait_entry` dict now has `"timestamp": _now_iso()` (the NEEDS_CHANGES fix), but the `violation_entry` dict at the contract violation branch has no timestamp. The two sibling log entries are inconsistent. Not a blocker — the audit_log entry for the violation does include `"timestamp": _now_iso()` — but the steward_log entry for the violation is missing a timestamp while its sibling wait_entry is not. Future forensic queries that expect consistent log entry schemas will find the inconsistency.

- **Tests do not assert that notify_dan was called on the contract violation path.** The `test_trace_absent_second_reentry_proceeds_with_contract_violation` test passes `notify_dan=lambda *a, **kw: None` and asserts on UoW status and steward_log content. It does not assert that the lambda was called. If the `_notify_cv(...)` call were deleted, the test would still pass. The behavioral fix (item 3) is unverified by the test suite. Not a blocker for this re-review — the code is present and correct — but the test is incomplete as a safety net.

- **Executor does not write trace.json today.** A check of executor.py confirms no trace-writing code. In production, every UoW completing with result.json will hit the gate, wait one cycle, then proceed as a contract violation. The contract violation path is the operational steady state until executor trace.json support ships. The gate is designed to handle this gracefully — the one-cycle delay is bounded, and the contract violation log makes the deviation observable. But this is a meaningful pipeline throughput impact (every UoW takes one extra steward cycle before re-prescription) that is not documented in the PR description.

- **Pure helper functions `_check_trace_gate_waited` and `_clear_trace_gate_waited` are correctly implemented.** Both scan steward_log JSON entries, handle None/empty input safely, and are pure (no side effects). The gate logic in `_process_uow` correctly sequences: result_file_exists check → trace_exists check → already_waited check → wait or proceed.

- **Three tests are structurally sound.** Test 1 (first re-entry, no trace.json): verifies skip behavior, `ready-for-steward` status, `trace_gate_waited` in log, no workflow_artifact. Test 2 (second re-entry, already waited): verifies prescription proceeds, `ready-for-executor` status, `trace_gate_contract_violation` in log. Test 3 (trace.json present): verifies prescription proceeds, `ready-for-executor` status, no `trace_gate_waited` in log.

### Overall verdict: APPROVED

All three NEEDS_CHANGES items from the prior review are resolved. The two residual quality gaps (violation_entry timestamp inconsistency, notify_dan assertion gap in tests) are real but non-blocking. The executor-doesn't-write-trace.json observation is a system-level consequence that should be tracked as a follow-up issue, not a defect in this PR. The gate logic is correct, bounded, and observable.

---

## [2026-04-04] PR #611 — feat(wos-v3): executor writes trace.json and corrective_traces DB row (PR A)

### Stage 1: Vision alignment (formed before reading implementation)

**Vision alignment:** The adversarial prior entering this review: is PR A adding trace infrastructure to a pipeline that has more foundational convergence issues, deferring those harder problems — or is it specifically closing a named operational gap? The PR #607 oracle verdict (decisions.md, 2026-04-04) named the gap directly: "Executor does not write trace.json today. In production, every UoW completing with result.json will hit the gate, wait one cycle, then proceed as a contract violation. The contract violation path is the operational steady state until executor trace.json support ships." PR #611 is the direct closure of that finding. This is not a speculative substrate addition; it is the producer that the gate in PR #607 requires. Two learnings.md failure patterns were active as priors. The "stub-as-live-code in consequential decision paths" pattern (present in learnings.md) caused me to examine whether the four helper functions are concrete implementations or documented stubs — they are concrete, and the known limitation (`register="operational"` hardcoding in partial/blocked paths) is explicitly acknowledged with PR B-D as tracked successors, matching the pattern's guidance that a documented stub with a tracked successor at a non-safety-critical position is acceptable. The "prior oracle finding forwarded as current root cause without re-verification" pattern (2026-04-01) caused me to verify that the gap named in PR #607's oracle review is still open — confirmed: no trace-writing code exists in executor.py before this PR. Vision principle-4 ("integration rate before new feature rate") is the direct alignment anchor: this PR wires what exists (the trace gate from #607) rather than adding new capability.

**Alignment verdict:** Confirmed

### Stage 2: Quality review

**Four pure helper functions are correctly structured.** `_trace_json_path` is stateless with a documented fallback for extension-less paths (appends `.trace.json`). `_build_trace` is a pure constructor returning a dict with all required schema fields, correct defaults (`surprises=[]` not `None`; `gate_score=None` documented as PR B work), and ISO timestamp via `_now_iso()`. `_write_trace_json` implements atomic write (tmp→rename), matching the existing `_write_result_json` pattern — partial writes are not visible to the Steward. `_insert_corrective_trace` is best-effort with correct never-raises semantics, matching the V3 non-blocking contract. The `conn.close()` call is present on the happy path; the exception path relies on garbage collection, which is functionally equivalent for SQLite but marginally inconsistent with the happy path's explicit close — not a blocker.

**All 5 executor exit paths are covered.** Claim failures (null artifact, missing file, deserialization failure): trace written before `registry.fail_uow()` and `ClaimRejected` return. Complete path: trace written between `_write_result_json` and `complete_uow` — correct ordering. Crash path: trace written in `_run_step_sequence` exception handler alongside result.json. Partial and blocked paths: trace written before `complete_uow`. The `register` field flows correctly from `ClaimSucceeded.register` through `_run_step_sequence` → `_run_execution` for the complete and crash paths; partial/blocked use hardcoded `"operational"` with documented rationale and a tracked enrichment path in PR B-D.

**Two latent NameError bugs fixed (`logger` → `log`)** in the claim sequence exception handler and TTL recovery. These were pre-existing silent bugs — any exception hitting those paths would have produced a secondary `NameError` masking the original exception. The fix is correct and low-risk.

**32 tests across 7 test classes.** Complete path (file created, schema valid, register field, surprises list, parseable timestamp, nonempty summary: 7 tests). Partial path (file created, schema valid). Blocked path (file created, schema valid, reason in surprises via DB). Crash path (file created, schema valid, exception in surprises). DB corrective_traces (row inserted on complete, uow_id correct, register correct, summary nonempty, surprises valid JSON, created_at close to trace timestamp, row inserted on crash). Coverage is comprehensive. One test complexity note: `test_trace_json_file_created` in `TestWriteTraceJsonOnPartial` uses nested dispatchers to reach the report_partial path — result is correct but the test is hard to read. The `_PartialStop`/`_BlockedStop` sentinel propagation in the test environment causes the crash handler to overwrite trace.json; in production the dispatcher returns normally and no overwrite occurs. This is a test-environment artifact, correctly handled.

**Patterns introduced:** Thin trace at executor level — executor writes mechanical `execution_summary`, empty `surprises` on success, null `prescription_delta`, and null `gate_score`. Steward enriches later. This separation (executor has facts, Steward has interpretation) extends the "seam-first abstraction" golden pattern: the trace dict is a minimal but named struct at the executor/steward seam, leaving enrichment as a backend swap in PR B-D without touching the executor.

**What this forecloses:** Nothing significant. All schema fields (`surprises`, `prescription_delta`, `gate_score`) are nullable/empty by default; PR B-D can populate them without breaking existing consumers. The `register="operational"` hardcoding in partial/blocked paths will require a public API change when enriched — explicitly flagged in comments.

**Opportunity cost note:** Not applicable — this closes a live operational issue (every production UoW was taking one extra steward cycle and logging a contract violation) that was explicitly named in the prior oracle review.

### Overall verdict: APPROVED

**PR #611** is approved for merge. All 5 exit paths write trace.json. All 4 pure helper functions are correctly implemented. The thin trace design is clearly documented with enrichment deferred to PR B-D. Best-effort DB write semantics are correct and explicitly documented. The `register="operational"` hardcoding in partial/blocked paths is a documented known limitation with tracked successors. Two latent NameError bugs are fixed. 32 tests pass. The PR closes the gap named by the PR #607 oracle review and satisfies the trace gate contract. Ready to merge.

---

## [2026-04-06] PR #600 — docs: WOS vision, doc index, and orchestration landscape

### Stage 1: Vision alignment

**Q: Is this PR aligned with WOS intent?**

Documentation-only PR. No code changes. Adds three missing documents:
- `wos-vision.md` — first-principles vision and premises
- `WOS-INDEX.md` — documentation ecosystem map (now superseded on main by the component glossary, but wos-vision.md and wos-orchestration-landscape.md remain)
- `wos-orchestration-landscape.md` — external systems survey

All three address a genuine gap: no canonical vision/premises document existed, only fragments in versioned design docs. STAGE 1: APPROVED.

### Stage 2: Is the implementation well-made?

**Q: Does wos-vision.md risk becoming a decision substrate for agents?**

Finding: This is the named risk in vision.yaml itself (risk-2). Without explicit authority headers, an agent reading wos-vision.md could treat it as canonical intent rather than querying vision.yaml structurally.

Resolution: Authority headers added before merge. The header explicitly states agents must query vision.yaml directly, that this document has no machine update authority, and that the canonical source is vision.yaml (Dan-owned, agent-read-only). The risk is addressed.

**Q: Are WOS-INDEX.md and wos-orchestration-landscape.md correctly registered?**

Finding: Both carry lower risk (reference/survey material) but still required explicit registration to prevent ambiguity about their authority level. Note: WOS-INDEX.md has been superseded on main by the component glossary (V3); the authority header has been adapted to match the actual document content.

Resolution: Both have appropriate headers added before merge. APPROVED.

**Q: Does any content in these documents contradict vision.yaml or introduce new behavioral constraints?**

Finding: All three documents are descriptive/survey material. None introduce new behavioral constraints. None contradict the premise architecture in vision.yaml. APPROVED.

### Human authority

Dan approved this PR explicitly with the condition that authority headers be added. Condition fulfilled before merge.

### Overall verdict: APPROVED

**PR #600** is approved for merge. Authority headers added to all three files before merge, addressing the named vision.yaml risk-2 concern. No code changes — documentation only.

---

## [2026-04-09] PR #717 (S3P2-H) — oracle-s3p2-h-r3

### Overall verdict: APPROVED

**PR #717** is approved for merge. All three NEEDS_CHANGES items from prior oracle rounds have been resolved: source_ref forwarding fixed with full INSERT chain, prescribed_ps NameError fixed (parsed_ps), and PR description updated to document all 3 changed files. Bind parameter count 14/14 verified. New test added and passes.

---

## [2026-04-09] PR #720 (S3P2-A Steward typed migration) — oracle-s3p2-a5

### Overall verdict: APPROVED

**PR #720** is approved for merge. Comprehensive fix — all CycleResult/IssueInfo/LLMPrescription plain dict usages converted to dataclass access across 6 test files and production startup-sweep.py. 73 tests passed. No remaining plain dict accesses anywhere in tests or production.

---

## [2026-04-09] PR #724 (fix: quota sleep-until-midnight) — oracle-pr-724

### Overall verdict: APPROVED

**PR #724** is approved for merge. Field ordering verified, quota_wait case confirmed correct, auth-failure ordering safe, no dual-alert risk.

---

### [2026-04-09] PR #727 — fix: oracle agent path references corrected to ~/lobster/oracle/

**Vision alignment:** The adversarial prior entering Stage 1: is this solving the wrong problem, or foreclosing a better path? The PR's theory of change is that oracle agents were instructed to write verdicts and learnings to `~/lobster-workspace/oracle/`, but the canonical location per `paths.py` (lines 47-48: `ORACLE_DECISIONS = LOBSTER_REPO / "oracle" / "decisions.md"`) is `~/lobster/oracle/`, which is tracked in git. This mismatch caused two separate live oracle files to accumulate independently: `~/lobster-workspace/oracle/decisions.md` (989KB, 4547 lines — the legacy-path file) and `~/lobster/oracle/decisions.md` (58KB, 789 lines — the repo-tracked file). Both files exist and have real content. Recent oracle writes (PR #717, #720, #724) went to the repo path, confirming the repo path is the current operative location for the PR merge gate. The fix is correctly targeted: it closes the seam on future oracle agent writes by correcting the instruction text to match where the merge gate reads from. The stranded history in the workspace file is a data migration question that is out of scope for a text-correction PR. No learnings.md failure pattern applies structurally — the "feature-bundling" pattern does not apply (diff scope precisely matches PR description), and the "verification artifact scope mismatch" pattern is not triggered (the verification grep is appropriate for a text-correction claim). One uncorrected reference exists: `negentropic-sweep.md` line 57 still reads `~/lobster-workspace/oracle/learnings.md`. This was not changed by the PR. Since the workspace learnings.md (199KB) has more accumulated content than the repo learnings.md (3.9KB), the negentropic sweep reading the workspace path may actually be reading the more complete source — this is not a defect introduced by the PR, but it is a remaining path inconsistency in the system. The adversarial prior did not survive scrutiny: the PR is solving the right problem, the fix is correctly scoped, and nothing is foreclosed.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Three corrections are exact and correct.** `lobster-oracle.md` line 82 (decisions.md append instruction), line 94 (learnings.md append instruction), and `docs/oracle-review-protocol.md` line 12 (verdicts location description) are all changed from `~/lobster-workspace/oracle/` to `~/lobster/oracle/`. The diff is exactly what the PR description claims.
- **Engineer's verification is complete and appropriate for this scope.** The grep confirmed zero remaining `~/lobster-workspace/oracle/decisions.md` or `~/lobster-workspace/oracle/learnings.md` references in the target files. This is the correct verification for a text-correction PR.
- **One uncorrected path remains in the system: `negentropic-sweep.md` line 57.** This is not in the PR's stated scope and is not a defect introduced by this PR. But it is a remaining inconsistency: the negentropic sweep task reads from the workspace oracle learnings path, while the oracle agent now writes learnings to the repo path. Future negentropic sweeps will not see oracle learnings written after this PR ships, unless that reference is also corrected. Advisory only — the PR is correct as stated.
- **No runtime impact.** These are instruction text changes, not code. The only failure mode (stranded oracle history at workspace path) predates this PR and requires a separate data migration to resolve.

**Patterns introduced:** None new to system character. This is a targeted instruction-text correction with no architectural implications.

**What this forecloses:** Nothing. The fix does not prevent a future migration of workspace oracle history to the repo path. The negentropic sweep path inconsistency remains open and should be addressed in a follow-on issue.

**Opportunity cost note:** Not applicable. This is a targeted correctness fix for a mismatch that caused oracle verdicts to be written to a location the merge gate does not read from.

**VERDICT: APPROVED**

PR #727 is approved for merge. Three path references correctly updated. One remaining path inconsistency (`negentropic-sweep.md` line 57) noted but out of scope for this PR — recommend filing a follow-on issue.


---

### [2026-04-09] PR #739 — fix(tests): resolve 18 integration test failures

**Vision alignment:** The PR's claim is that 18 pre-existing integration test failures were caused by test-infrastructure drift against production code changes, and that no production behavior is incorrect. Stage 1 adversarial prior: test fixes can paper over production bugs by lowering assertion bars to accept incorrect production output. The specific risk here is that the `pending → ready-for-steward` assertion change reflects tests being updated to accept incorrect production behavior rather than tests correcting a stale expectation. What would have to be true for this to be the right path: (1) `approve()` genuinely and correctly auto-advances atomically past `pending`, (2) `first_execution` genuinely maps to `orienting` in ADR-004 vocabulary, (3) `updated_at` is genuinely the age anchor in the executor orphan sweep, and (4) `lifetime_cycles` genuinely drives the hard_cap check. All four are verifiable against production code and were confirmed in Stage 2. The PR is aligned with vision.yaml principle-4 ("Wire what exists before building more"): a test suite with 18 failures against current production behavior is not wired. Restoring test reliability without touching production code is the minimum viable correctness step before any further substrate work can be trusted.

**Alignment verdict:** Confirmed

**VERDICT: APPROVED**

**Quality finding:**
- **All four root causes trace cleanly to production code changes that were not mirrored in test stubs.** `IssueInfo` as a typed dataclass (dict stubs failing attribute access), `approve()` atomic two-step transition (pending no longer a resting state), `updated_at` as the executor-orphan age anchor (not `created_at`), and `_determine_trace_posture()` mapping `first_execution → "orienting"` per ADR-004 — each is directly verifiable in the production code and correctly identified as the root cause.
- **The `TestBootupCandidateGate` assertion change is semantically correct but introduces a stale docstring problem.** The class docstring still reads "stays in 'pending' until the gate is cleared" and `_seed_uow`'s docstring still reads "approve-to-pending steps" — both are now wrong. The assertions are correct; the documentation is not. This is a low-severity correctness gap but matches the learnings.md pattern "comment/code mismatch at state-machine transition causes silent state divergence" (PR #607). No test reader can understand the gate contract from the stale docstrings.
- **The hard_cap/lifetime_cycles fix is sound.** Production code at line 651-652 of steward.py is unambiguous: `cycles = uow.lifetime_cycles; if cycles >= _HARD_CAP_CYCLES`. Setting `steward_cycles` in the test was inert — it could never trigger the hard_cap branch. Setting `lifetime_cycles` is correct. The fallback trace entry lookup (searching by anomalies content rather than cycle number) correctly accounts for the fact that `cycle` in a trace entry records `steward_cycles` (per-attempt), which is 0 when the test fires from a fresh UoW even though `lifetime_cycles` was set to the cap.
- **The executor orphan fix (backdating both `created_at` and `updated_at`) is the minimal correct fix.** startup-sweep.py line 356 explicitly uses `updated_at` as the age anchor with `created_at` as a fallback — the prior test backdated only `created_at`, leaving `updated_at` at now, so the UoW appeared fresh. The precondition assertion is also updated to verify both fields are backdated, which correctly anchors the test contract against future changes to the age anchor logic.

**Patterns introduced:** `IssueInfo` stub pattern (returning typed dataclass from github_client stubs) is now consistent across all 5 test files — this is the correct pattern and propagates a uniform stub contract. The anomalies-content-based trace entry lookup (rather than cycle-number-based) is a sound adaptation to a two-counter world (`steward_cycles` per-attempt vs `lifetime_cycles` cumulative), but search-by-content is weaker than search-by-identity — if a future cycle also has anomalies, the lookup returns the first one, which may not be the stuck-condition entry.

**What this forecloses:** Stale docstrings in `TestBootupCandidateGate` (class docstring and `_seed_uow` docstring) are not corrected. A future test author reading the class docstring will have an incorrect mental model of the gate contract. The PR does not foreclose any production paths.

**Opportunity cost note:** No production features deferred. The only marginal cost is the stale docstring not being corrected alongside the assertion fix — a one-line change that was not made.



---

### [2026-04-14] Design decision: oracle document review protocol

**Vision alignment:** The system's stated phase intent is to build a substrate that lets every agent make intent-anchored decisions. The oracle's existing code-review protocol operationalizes this for code PRs: it names the failure mode before seeing the implementation, then adjudicates whether the implementation forecloses better paths. Document artifacts -- bootup docs, agent definitions, protocol specs, context files -- are at least as load-bearing as code in this substrate. They encode the behavioral contracts that all agents operate under. A code PR that ships a wrong algorithm is wrong in one place; a document that encodes a wrong premise propagates that premise into every agent session that reads it. Yet documents have no external adjudicator: unlike code, there are no tests to run. The adversarial prior does not transfer cleanly -- "this document is solving the wrong problem" must be cashed out as "this document makes something invisible that matters." The vision alignment for this protocol extension: principle-5 ("when a behavioral rule isn't followed, improve the discriminator") is a document-governance principle as much as a gate-table principle. This protocol gives the oracle a decision-procedural discriminator for document review rather than an open-ended "is this good?" question.

**Alignment verdict:** Confirmed

**Quality finding:**
- The key structural transfer from code review is not the gate (APPROVED/NEEDS_CHANGES) but the requirement that NEEDS_CHANGES names a specific thing that must change. For code, this is a failing behavior. For documents, this is a named gap -- something the document makes invisible that a reader would need to know. The three-path resolution contract (addressed / disputed / deferred) replaces the binary "fixed it" that works for code bugs but fails for documents, where a gap may be intentionally out of scope.
- The revision contract's three paths are necessary because documents have legitimate reasons not to address every gap: scope constraints, deliberate deferral, or genuine disagreement with the oracle's interpretation. The "disputed" path is the accountability mechanism -- it requires the author to state a specific reason for disagreement rather than silently omitting the gap. Without it, "I didn't fix gap 2" is indistinguishable from "I disagree that gap 2 is a gap."
- Prior gap tracking (enumerating previous named gaps and their status before issuing a new verdict) closes the cycle that code re-review relies on test-rerun to close. Without this, a document can be re-reviewed on the second pass without the oracle knowing whether the first pass's gaps were addressed, creating the appearance of resolution without the substance.
- The "interpretation finding" field forces the oracle to take a position in disputable terms -- not "this document is incomplete" but "this document treats X as settled when a reader would need to know Y to evaluate that claim." This is the minimum requirement for accountable review of interpretation.

**Patterns introduced:** Named-gap citation structure as the document analog of failing-test citation. Three-path gap resolution (addressed/disputed/deferred) as the accountability mechanism for document revision cycles. Prior gap tracking as the document analog of test-rerun for code re-review.

**What this forecloses:** Open-ended "improve it" review cycles where a document can be revised without tracing revisions to named gaps. This is the primary failure mode the protocol exists to prevent -- generic improvement that satisfies the form of revision without closing the specific interpretive gap the oracle identified.

**Opportunity cost note:** No tooling, automation, or new files were built. The protocol is entirely expressed in the oracle agent definition and this decisions.md entry. The cost of not having this: document artifacts in the system accumulate without accountable review, and the oracle has no decision-procedural basis for document verdicts. This has been true since the oracle was introduced. The cost was diffuse (every document reviewed without the named-gap structure) rather than acute.

**VERDICT: APPROVED**

---

### [2026-04-20] PR #795 — fix(bootup): align spawned-agent signal with user.base.bootup.md

**Vision alignment:** The active_project phase_intent is "Build the substrate that lets every agent make intent-anchored decisions." PR #795 does not touch that substrate directly — it corrects a behavioral inconsistency in a bootup document. The adversarial prior: is this solving the wrong problem? The signal legend in sys.subagent.bootup.md specifies the footer format that subagents emit when reporting side effects. A mismatch between this file (`🤖 spawned`) and user.base.bootup.md (`🚀 spawned  <task-name>`) means subagents reading the system file produce footers that (a) use the wrong emoji and (b) omit the task slug — the information the dispatcher would use to identify which task was spawned. This is not cosmetic: the footer is a structured signal in a protocol, and user.base.bootup.md line 201 explicitly says "Never `🤖 spawned`." The world in which this work is wasted: no subagent actually reads and follows the signal legend. That concern is not trivially dismissible — LLMs do not execute format specs mechanically — but the fix is a single line and costs essentially nothing. principle-1 (proactive resilience over reactive recovery) supports catching protocol-level discrepancies before they cause downstream parsing confusion. Stage 1 verdict: alignment confirmed. The fix is not strategic work, but it is the right problem and it is cheap.

**Alignment verdict:** Confirmed

**Quality finding:**
- **The diff is exactly what the PR claims.** Single line changed in the signal legend: `🤖` spawned replaced by `🚀 spawned  <task-name>` including the double-space separator and task-slug instruction. The PR's self-test (`grep -n "🤖" .claude/sys.subagent.bootup.md`) confirms only the GitHub attribution lines remain — those are a structurally distinct use of `🤖` (PR body prefix, not spawned-agent signal) and are correctly left unchanged.
- **Cross-file consistency verified.** The new text matches user.base.bootup.md line 190 exactly and line 201 explicitly reinforces it ("Never `🤖 spawned`"). No ambiguity remains between the two files.
- **No side effects.** Documentation-only change. No code paths, no test surface, no MCP tools, no cron entries, no migration required.
- **One residual observation: the signal legend still says "10-signal set."** After this change, the legend entry count is still 10 (the spawned entry was replaced, not added or removed), so the count remains accurate. Not a defect — confirming this is correct.

**Patterns introduced:** None. This is a correction to an existing pattern (the signal-legend protocol), not the introduction of a new one.

**What this forecloses:** Nothing. The fix is a one-line document correction with no architectural consequence.

**Opportunity cost note:** The opportunity cost is negligible — a single-line documentation fix that closes a behavioral specification gap. Not relevant to WOS execution health or the vision substrate, but also not competing with them.

**VERDICT: APPROVED**

---

### [2026-04-20] PR #797 — oracle: wire OODA constraint-3 Encoded Orientation check into Stage 2

**Vision alignment:** The theory of change in vision.yaml is that all agents should make decisions anchored to specific fields — constraint-2 (every priority decision must be traceable to a specific field, not inferred from conversational texture) and constraint-3 (Encoded Orientation decisions require a prior logged decision and a traceable vision.yaml anchor) jointly define a structural anchoring requirement. Until this PR, constraint-3 was orphaned: vision.yaml defined it, but no agent enforced it. The oracle is the correct enforcement point because it gates every PR before merge. The adversarial prior here was: does adding a named check to an LLM agent's instruction document actually change oracle behavior on future PRs, or does it only label a criterion the oracle was already informally applying? This prior was active because of the "library with no wiring" failure pattern from learnings.md (any new check is inert until it changes a verdict). The world in which this work is wasted effort: the oracle was already denying Encoded Orientation PRs without the named check, making this instruction redundant. Having read the existing Stage 2 criteria (five questions with no explicit constraint-3 reference), that scenario is disconfirmed. The prior oracle criteria did not direct the oracle to look for decisions.md anchors. The check closes a real structural gap. The golden-patterns.md "adversarial prior seeding" pattern caused me to hold the question "does the oracle actually apply this mechanically?" as the primary evaluation axis — not whether the wording is elegant.

**Alignment verdict:** Confirmed

**Quality finding:**
- **The check is self-applying and passes its own criteria.** This PR encodes a new behavioral orientation into lobster-oracle.md (an agent definition), which triggers the Encoded Orientation check. The vision.yaml anchor is unambiguous: `inviolable_constraints[constraint-3]` directly and verbatim motivates the change. For the "prior logged decision" anchor, the check provides "or equivalent" relief — the vision.yaml constraint itself is the equivalent logged intent. The check passes reflexively, which is the correct behavior.
- **The artifact-type enumeration is concrete and mechanical.** "Agent definitions, bootup files, gate tables, vision.yaml, CLAUDE.md" gives the oracle a specific list to check against. This prevents the oracle from treating the classification as a pure judgment call. Most PRs touch code files and will pass the check automatically; the check fires only when the diff includes one of the named artifact types.
- **The "mechanical" exception has one ambiguous edge case.** "No new orientation encoded" is the operative phrase for passing refactors through without the check. A refactor that moves an existing behavioral instruction from one artifact to another — without changing it — could be classified either way. The check does not provide a test for this edge case. The failure mode is inconsistency across oracle runs, not false positives or false negatives on clear cases.
- **"Or equivalent" in anchor (a) is undefined.** The check requires "a prior logged decision in oracle/decisions.md or equivalent" — but "equivalent" is not specified. Future oracle runs might accept conversational context, GitHub issue descriptions, or vision.yaml constraints themselves as equivalents inconsistently. Tightening this to enumerate valid equivalents (vision.yaml constraint entries, resolved open decisions) would improve consistency, but this is a minor improvement, not a blocker.

**Patterns introduced:** Named check as a bold paragraph within Stage 2 — establishes the format for future Stage 2 criteria additions. The self-applying property (the check applies to the PR that introduces it) is a structural feature worth preserving: any future Stage 2 criterion added via PR should be checked against itself.

**What this forecloses:** Behavioral orientation changes that were previously light-touch (touching an agent definition without justifying the change against vision.yaml and a prior decision) now require the justification to be present before the oracle approves. This is the correct friction. It does not foreclose legitimate orientation changes; it requires them to be deliberate.

**Opportunity cost note:** Two lines against a 169-line file; low cost. The current_focus priority is WOS execution health, not oracle improvements. However, the oracle gates all PRs — improving it protects the WOS work from misaligned behavioral drift. The opportunity cost of not doing this was accumulating Encoded Orientation changes without structural accountability.

**VERDICT: APPROVED**

---

### [2026-04-20] PR #799 — fix: use absolute paths for all oracle/decisions.md references in agent instructions

**Vision alignment:** The theory of change in vision.yaml is structural anchoring — agents making decisions from explicit, locatable references, not inferred context. A path reference that resolves to the wrong location (or no location) after context compaction is exactly the failure mode constraint-2 ("every agent decision must be traceable to a specific field — not inferred from conversational texture") exists to prevent. The path-fix sweep is the right problem: fixing the reference precision that makes oracle review enforceably structural rather than advisory. The adversarial prior: is there a scenario in which all of this work is wasted? Yes — if PR #796 already fixed the same locations, PR #799's diff applies against a stale base and cannot merge cleanly. That scenario requires examination before Stage 1 can be called confirmed.

The "path fix completeness" learning from PR #796 oracle entry (2026-04-20, learnings.md) was the active prior here. That learning states: "when fixing a path inconsistency, enumerate all path-reference forms — not just the form observed in the failing case." Applying this, I enumerated all path forms across all agent instruction files before evaluating the PR's scope. This produced a concrete finding: PR #799 is CONFLICTING with main (mergeStateStatus: DIRTY), meaning its diff was computed against a pre-PR-#796 base. PR #796 already fixed several of the same locations that PR #799 targets.

**Alignment verdict:** Questioned

**Quality finding:**
- **PR #799 cannot merge: merge conflict with main.** `gh pr view 799 --json mergeable,mergeStateStatus` returns `CONFLICTING / DIRTY`. PR #796 merged first and fixed the same CLAUDE.md PR Merge Gate row and the same lobster-oracle.md frontmatter line. The diffs are not identical (PR #796 used backtick-wrapped paths in frontmatter; PR #799 removes the backticks), but they touch the same lines, creating a conflict the PR cannot resolve automatically.
- **Post-PR #796 main is already substantially fixed.** After PR #796 merges, the locations PR #799 targets in CLAUDE.md (3 occurrences in the PR Merge Gate row) are already at `~/lobster/oracle/decisions.md`. The lobster-oracle.md frontmatter is also already fixed. PR #799's additions are redundant for those locations and cannot be applied as-is.
- **One remaining unfixed reference exists that neither PR addresses.** `lobster-oracle.md` line 77 (Encoded Orientation check) reads `oracle/decisions.md` — a bare relative reference inside the behavioral prose of the Encoded Orientation check. This is a read-reference: the oracle is instructed to "check `oracle/decisions.md`" when verifying a prior logged decision. Post-merge of both PRs, this reference remains unfixed. An oracle agent checking for a prior decision via this reference would search relative to cwd rather than `~/lobster/oracle/decisions.md`.
- **docs/oracle-review-protocol.md line 90 also has a remaining bare reference.** After PR #796 and PR #799 both merge, `docs/oracle-review-protocol.md:90` still reads `oracle/decisions.md` (without the `~/lobster/` prefix). This is a documentation file, not an agent instruction — lower severity — but the sweep's stated definition of done ("No relative oracle/decisions.md references remain in agent instructions") does not cover it.

**Patterns introduced:** The conflict scenario confirms a new failure mode: two sequential fix-sweep PRs branched off the same base independently fixing the same locations, where the second PR becomes unapplicable after the first merges. This is a variant of the "feature bundling" pattern in learnings.md but operating in reverse: two PRs too narrowly scoped, fixing overlapping subsets from different branch points rather than coordinating as a single sweep.

**What this forecloses:** Nothing structural — the path-fix work is purely corrective and introduces no architectural commitment.

**Opportunity cost note:** Not relevant — path fixes are zero-opportunity-cost corrections. The cost here is the conflict resolution needed before PR #799 can be applied.

**VERDICT: NEEDS_CHANGES**
**Revision contract:**
- **Gap 1: Merge conflict with main** — PR #799 must be rebased onto current main (post-PR #796) before it can be evaluated for merge. The conflict is on the CLAUDE.md PR Merge Gate row and the lobster-oracle.md frontmatter — both lines already changed by PR #796. The rebased PR should retain only the changes not already present on main.
- **Gap 2: lobster-oracle.md Encoded Orientation check (line ~77) still references bare `oracle/decisions.md`** — after rebase, this remaining reference should be fixed in the same PR since it is in scope of the sweep's stated definition of done. Addressed = the line reads `~/lobster/oracle/decisions.md` after the fix. Not addressed = the sweep remains incomplete.
- **Gap 3 (optional, lower priority): docs/oracle-review-protocol.md line 90** — bare `oracle/decisions.md` in the "After issuing an APPROVED verdict" sentence. This is documentation, not an agent instruction, so it is not a blocker, but the sweep's definition of done should state whether documentation files are in or out of scope.


---

### [2026-04-20] PR #799 (re-review) — fix: use absolute paths for all oracle/decisions.md references in agent instructions

**Prior gap tracking:**
- **Gap 1: Merge conflict with main** — ADDRESSED. PR rebased onto main post-PR #796 merge. Single commit remains, touching only the two locations not already fixed by PR #796.
- **Gap 2: lobster-oracle.md Encoded Orientation check (line ~77) bare `oracle/decisions.md`** — ADDRESSED. The diff changes `oracle/decisions.md` → `~/lobster/oracle/decisions.md` in the Encoded Orientation check prose of `.claude/agents/lobster-oracle.md`. The specific instruction ("a prior logged decision exists in `oracle/decisions.md` or equivalent") now reads the absolute path.
- **Gap 3: docs/oracle-review-protocol.md line 90** — ADDRESSED. The diff changes `oracle/decisions.md` → `~/lobster/oracle/decisions.md` in the "After issuing an APPROVED verdict" sentence. The author chose to include it in scope, satisfying the gap's framing question ("state whether documentation files are in or out of scope") by including them.

**Vision alignment:** The theory of change is identical to the prior review: path references that do not resolve to the live oracle file undermine the structural enforceability of oracle review, which is the mechanism by which constraint-2 ("every agent decision must be traceable — not inferred") and constraint-3 (Encoded Orientation requires a prior logged decision) are structurally enforced rather than advisory. This re-review's adversarial prior: is there a scenario in which the rebased PR still fails to complete the sweep? The residual-reference question from the prior review remains relevant — are there other bare references not covered by this PR and not covered by PR #796? The sweep's definition of done is "no relative `oracle/decisions.md` references remain in agent instructions." The PR covers the two locations its description claims. Whether other bare references exist in agent instruction files (as opposed to documentation or non-operative prose) determines whether the sweep's stated definition of done is met. Examining the committed repo state (lobster-oracle.md line 6 frontmatter description still reads "Writes to oracle/decisions.md") and sys.dispatcher.bootup.md context note — these are non-operative context strings (frontmatter description, a quoted note), not instructions the agent acts on. The lobster-oracle.md line 6 reference is in the agent card's own `description` field, which describes the agent's function to a reader, not a path the agent traverses. This passes the operative-reference test. The adversarial prior does not survive: this PR completes the sweep's operative scope.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Both changes are precisely targeted and correct.** The Encoded Orientation check in lobster-oracle.md is an agent instruction the oracle acts on (step (a): "a prior logged decision exists in [path]") — making it absolute is operationally necessary, not cosmetic. The docs/oracle-review-protocol.md change is in the "Integration with Oracle Agent" section that instructs the oracle agent on its post-APPROVED action; it is semi-operative and the fix is appropriate.
- **Encoded Orientation check passes as mechanical.** The PR modifies an agent definition file (.claude/agents/lobster-oracle.md), which triggers the Encoded Orientation check. However, the change is purely a path string fix: the behavioral orientation encoded (verify prior logged decision and vision.yaml anchor before allowing Encoded Orientation PRs) is unchanged. No new orientation is encoded; no old orientation is removed. The check passes automatically as a mechanical path correction. Citing golden-patterns.md "self-applying Stage 2 check as structural correctness test" (2026-04-20): the Encoded Orientation check does not fire on itself here because the PR encodes no new behavioral orientation — it only corrects the path string the check uses.
- **No residual operative bare references after this PR applies.** The non-absolute references remaining in the repo (lobster-oracle.md line 6 description field, sys.dispatcher.bootup.md OODA note, various design/retro docs) are non-operative: they are prose descriptions, architectural references in documentation, or quoted context — not path instructions the agent follows when doing its work. The sweep's definition of done ("no relative references in agent instructions") is satisfied.
- **The learnings.md entry for PR #799 ("sequential fix-sweep PR conflict") is confirmed as correctly scoped.** The rebase strategy (drop conflicting commits, keep only the residual) is the correct resolution: the rebased PR is exactly 1 commit ahead of main touching exactly the 2 unfixed locations. No over-application, no under-application.

**Patterns introduced:** None new. This PR extends the absolute-path convention established by PR #796 and PR #727. No new behavioral or structural patterns.

**What this forecloses:** Nothing. The correction is additive precision on path strings; it does not constrain future changes to the oracle infrastructure.

**Opportunity cost note:** Two-line documentation correction. Zero opportunity cost.

**VERDICT: APPROVED**

---

### [2026-04-20] PR #800 — fix: implement OAuth token refresh in _build_claude_env

**Vision alignment:** PR #800 is the natural continuation of PR #798, which added expiry detection but explicitly deferred refresh. Issue #775 named the full fix: when a WOS run exceeds ~8 hours, the OAuth token expires and steward cycles fail with a 401 that is indistinguishable from other subprocess errors. The adversarial prior entering Stage 1: is refresh the right fix, or is this addressing a symptom — with the root cause being WOS runs that exceed the token lifetime? Under the symptom-fix hypothesis, the correct fix would be reducing steward cycle duration or frequency, not adding a refresh mechanism that depends on an undocumented Anthropic endpoint discovered by binary inspection. However, this hypothesis collapses for two reasons: (1) WOS execution health is the explicit `current_focus.this_week.primary` in vision.yaml — "fix executor dispatch starvation" — and unforced 401 failures are a direct starvation contributor; (2) the graceful degradation path (on failure, log ERROR and continue with old token) means endpoint instability does not make the system worse than the pre-PR baseline. The endpoint instability concern is real — `_ANTHROPIC_OAUTH_TOKEN_ENDPOINT` was discovered from the Claude CLI binary, not documentation, so Anthropic could change it without notice. But the response to this risk is correct: treat refresh as best-effort. The constraint-3 Encoded Orientation check applies here because `_refresh_oauth_token` writes credentials.json without Dan's per-cycle input. The prior logged decision is Issue #775 (closed by this PR). The vision.yaml anchor is `current_focus.this_week.primary` ("fix executor dispatch starvation" — 401 failures are a starvation contributor). Both are present. The adversarial prior did not survive scrutiny.

**Alignment verdict:** Confirmed

**Quality finding:**
- **The millisecond timestamp fix (primary bug) is correctly implemented and well-tested.** `_normalise_timestamp()` applies the `> 1e11` threshold to detect ms-format values — the exact threshold documented in the learnings.md entry for PR #798. The two ms-specific tests (`test_ms_timestamp_near_expiry_triggers_refresh`, `test_ms_timestamp_fresh_does_not_trigger_refresh`) exercise the exact scenario the PR #798 oracle review flagged as uncovered. This directly closes the quality gap named in the PR #798 decision entry.
- **Credential write-back field preservation is tested and correct.** `test_preserves_other_credential_fields_on_write` verifies that `refreshToken`, `scopes`, `subscriptionType`, and `rateLimitTier` survive the write. The merge logic (`creds.setdefault("claudeAiOauth", {})` then field assignment) is sound. The `setdefault` defense for a missing `claudeAiOauth` key is logically unreachable given that the caller extracted `refreshToken` from `oauth` before calling `_refresh_oauth_token` — but it does not harm correctness.
- **Locally-declared mirror of production constant in test file.** `test_token_refresh.py` line 296 declares `_MILLIS_THRESHOLD = 1e11` locally, mirroring `_MILLISECOND_TIMESTAMP_THRESHOLD = 1e11` from steward.py. This is the divergence-risk pattern named in learnings.md (PR #696: "A locally-declared mirror of a production constant creates a divergence risk"). Because learnings.md was read before the diff, I specifically searched for locally-mirrored constants — this is the behavioral change the pattern citation constrains. The correct form is to import `steward._MILLISECOND_TIMESTAMP_THRESHOLD` directly. This is a quality gap, not a blocker: the test does not verify the threshold value itself, only uses it to branch ms vs. seconds parsing in an assertion. If the threshold changes, this test silently tests against the stale value.
- **`expires_in: 0` creates a refresh loop.** `_refresh_oauth_token` uses `data.get("expires_in", 0)` with a default of 0. If the Anthropic endpoint returns a response without `expires_in` or with `expires_in: 0`, the written `expiresAt` equals the current timestamp — immediately expired. On the next steward cycle, `_check_token_expiry` returns `EXPIRED`, triggering another refresh attempt. This loop resolves only when the endpoint returns a valid `expires_in` or the refresh itself fails. A non-zero default (e.g., 28800 — 8 hours) or a `> 0` validation before writing would prevent this. This is a minor gap that only fires on malformed endpoint responses; no test covers it.

**Patterns introduced:** `_normalise_timestamp` as an isolated unit responsible for epoch-scale detection introduces a clean seam: any future caller parsing externally-sourced timestamps can use this function rather than re-implementing the threshold check. The `_TokenStatus` class-level string constants pattern is functional but does not provide exhaustiveness checking — future callers should use `if status in (...)` (as `_build_claude_env` correctly does) rather than chained equality checks that silently pass through on an unknown status.

**What this forecloses:** The endpoint dependency `_ANTHROPIC_OAUTH_TOKEN_ENDPOINT` is now embedded in production code with no change-notification path. If Anthropic changes this endpoint, the system degrades silently to the pre-PR #798 baseline (continue with old token, log ERROR). This forecloses a proactive credential-rotation signal to Dan as an alternative fix — that alternative would require no undocumented endpoint. The graceful degradation makes this acceptable, but the dependency on a binary-discovered endpoint is now a permanent fixture in the production path unless explicitly removed.

**Opportunity cost note:** This PR is directly on-path for `current_focus.this_week.primary` ("WOS execution health"). The 401-failure mode for long-running WOS cycles is a documented starvation contributor. No opportunity cost relative to stated priorities.

**VERDICT: APPROVED**


---

### [2026-04-20] PR #786 — hygiene: update dispatch-job.sh tombstone with Phase 2 tracking issue and eligibility date

**Vision alignment:** PR #786 is a tombstone comment update for `dispatch-job.sh`, adding a concrete Phase 2 eligibility date (2026-04-26), a live tracking issue reference (#785), and a dependency note about `bot-talk-check-dispatch.sh`'s 6 `exec` calls that must be replaced before archival. The adversarial prior entering Stage 1: is this solving the wrong problem? Specifically — is an underspecified tombstone comment the actual bottleneck, or is this PR spending oracle attention on commentary when `current_focus.this_week.primary` is WOS execution health? The prior does not survive scrutiny. The vision.yaml `horizon` field explicitly names "confirm PRs #786/#787 oracle-approved and merged" as an immediate next step. The tombstone's original defect was real: it referenced a closed issue (#1083) with no eligibility date, leaving a future engineer without an actionable pointer to Phase 2 conditions. Fixing that is infrastructure hygiene, not distraction — it encodes context that would otherwise require reconstruction, which is directly aligned with `constraint-4` ("minimize metabolic cost of cybernetic engagement"). The "adversarial prior seeding before implementation review" golden pattern (golden-patterns.md, 2026-03-27) constrained this analysis by requiring the Stage 1 finding to be formed before reading the diff — enforcing the check that alignment verdict is about the problem being solved, not the quality of the solution.

**Alignment verdict:** Confirmed

**Quality finding:**
- **The change is exactly scoped.** Six lines added, two lines modified — all comment text. No logic, no test surface, no behavior change. The syntax checks (`bash -n`) are the appropriate verification. The PR description correctly characterizes this as "pure comment-only change; no logic affected."
- **The three additions are individually load-bearing.** The Phase 1 merge date (`2026-04-12`) makes the 2-week clock unambiguous. The eligibility date (`2026-04-26`) is computable from the merge date, but encoding it in the file removes the computation from a future engineer. The `bot-talk-check-dispatch.sh` dependency note with the specific count (6 `exec` calls) encodes a discovery that would otherwise require a grep — this is the highest-value addition.
- **Constraint-3 Encoded Orientation check: not triggered.** This PR modifies only a comment in a shell script — it does not change any agent instruction, behavioral gate, bootup file, or vision.yaml field. No Encoded Orientation audit is required.
- **No failure patterns from learnings.md apply.** The feature-bundling pattern was the active prior (learnings.md documents it as recurring in the same sprint). The diff contains exactly what the PR description says: one file, three comment additions. No bundling detected.

**Patterns introduced:** None new. The practice of tombstoning with explicit eligibility dates and live tracking issue references is an extension of the infrastructure hygiene discipline already present in the codebase. No new behavioral or structural patterns are introduced.

**What this forecloses:** Nothing. Comment-only change. All paths remain open.

**Opportunity cost note:** This PR was explicitly placed on the vision.yaml horizon as a gate before pipeline restoration. Oracle review is therefore on-path, not bureaucratic overhead.

**VERDICT: APPROVED**

---

### [2026-04-21] PR #806 — Remove RALPH acronym from active job descriptions and code

**Vision alignment:** PR #806 retires the RALPH label (Recursive Autonomous Loop for Pipeline Health) from human-readable prose — comments, docstrings, task descriptions, log messages — while explicitly preserving functional identifiers (`ralph-loop`, `ralph-test`, `ralph-state.json`, `ralph-reports/`) for a separate migration. The adversarial prior: is cosmetic naming cleanup the right expenditure of scope while `current_focus.this_week.primary` names "fix executor dispatch starvation, resolve RALPH Cycle 7 failures"? The prior weakens for two reasons. First, Dan explicitly directed the retirement — this is not an agent-initiated cleanup. Second, `constraint-4` ("minimize metabolic cost of cybernetic engagement") is served by removing a cute acronym with no semantic load; the RALPH name adds cognitive overhead without adding orientation. The incremental scope is correct: attempting a full rename including DB/cron identifiers mid-starvation investigation would add operational risk. The vision.yaml `principle-4` ("wire what exists before building more") supports the bounded scope. The adversarial prior does not survive scrutiny on the naming retirement itself. However, the diff contains a material out-of-scope deletion (Migrations 78 and 79 from upgrade.sh) that was not in the PR description — this is a quality issue, not an alignment issue.

**Alignment verdict:** Confirmed

**Quality finding:**
- **All RALPH prose substitutions are correct and complete within stated scope.** `ralph-loop.md` (task definition) and `ralph-loop.py` (dispatch script) consistently replace "RALPH" with neutral descriptive language ("WOS test run cycle", "WOS pipeline health loop", "pipeline health state"). No behavioral change — functional identifiers (`source = 'ralph-test'`, `ralph-state.json`, path variables) are unchanged throughout both files, consistent with the PR's explicit out-of-scope declaration.
- **Migrations 78 and 79 are removed from upgrade.sh with no mention in the PR description.** PR #804 (merged 2026-04-21 at 02:34 UTC, before this PR was opened) added Migrations 78 and 79: (78) populate `cycle_start_timestamp` in `rotation-state.json` to prevent false vision-drift warnings on first Night 7 run; (79) archive the old `sweep-context.md` from the runtime path to prevent it being treated as authoritative. Both are functional migrations with downstream effects. PR #806 removes them silently as part of the RALPH naming diff. The `mergeStateStatus: CLEAN` confirms no merge conflict — this is an active deletion, not a merge artifact. Any install that runs upgrade.sh for the first time after this PR merges will never execute these migrations. This is the feature-bundling pattern from learnings.md (PR #714, PR #712, PR #717: "always cross-check diff file list against PR description to detect undocumented scope expansion") — applied here as *undescribed deletion* rather than undescribed addition. Detection was triggered by the oracle vocabulary loaded from learnings.md before the diff was read; without that vocabulary the upgrade.sh deletions would likely have read as an unrelated cleanup and passed.
- **The `success_criteria` string for type-A UoW changes from `'RALPH test output'` to `'WOS test output'`.** This is a behavioral change: any in-flight type-A UoW at the time of upgrade will have the old `success_criteria` string in the DB; the executor will evaluate against it correctly because success_criteria is read from the DB record, not from the script. But future injected type-A UoWs will write `'WOS test output'` as the target string and write to `/tmp/ralph-test-{run_id}.md` with the old filename pattern. The file path (`/tmp/ralph-test-{run_id}.md`) is unchanged — so the success criteria references a file name that still contains "ralph". This is a minor internal inconsistency that does not affect operational behavior but would not survive a strict naming-consistency audit in a follow-on PR.
- **Constraint-3 Encoded Orientation check: not triggered.** This PR modifies a task definition document (ralph-loop.md) and a dispatcher script (ralph-loop.py), but all behavioral logic is unchanged. No agent behavioral instruction, gate table, bootup file, or vision.yaml field is modified. The substitutions are prose-only.

**Patterns introduced:** The partial-rename pattern — retire human-readable labels first, migrate functional identifiers in a follow-on PR requiring DB/cron coordination — is now established for future label retirements in this codebase. This is a deliberate and sensible approach for cases where full rename is operationally risky.

**What this forecloses:** If the partial rename ships and the follow-on identifier migration (ralph-loop → wos-health-loop, ralph-state.json → wos-health-state.json, ralph-reports/ → wos-health-reports/, ralph-test source marker) is not tracked in an issue, the identifier migration is likely to be forgotten. The codebase will have WOS-labeled prose pointing at ralph-* identifiers for an extended period. This is an acceptable risk Dan chose by directing incremental retirement; it is not foreclosure by the PR itself.

**Opportunity cost note:** Low-cost housekeeping directed by Dan. The migration deletion is the substantive concern — it is not about opportunity cost but about correctness.

**VERDICT: NEEDS_CHANGES**

**Revision contract:**
- **Gap 1: Silent deletion of Migrations 78 and 79** — `upgrade.sh` must restore Migrations 78 and 79 exactly as they appeared after PR #804 merged to main. The RALPH naming PR has no standing to remove functional migration steps. Addressed: the restored migrations appear in the diff. Disputed: author provides a specific reason why removing them is correct (e.g., they were superseded by a later migration or are now no-ops). Deferred: author acknowledges the gap and states why restoration is not being done now. Generic removal without tracing to a reason does not count as resolution.

---

### [2026-04-21] Re-oracle: PR #806 — Remove RALPH acronym from active job descriptions and code (pass 2)

**Prior gap tracking:**
- **Gap 1: Silent deletion of Migrations 78 and 79** — STATUS: ADDRESSED. Commit 82b4f63 restored both migrations verbatim from main. Verified: the PR branch (`retire-ralph-naming`) contains Migrations 78 and 79 at lines 2688–2712 of `scripts/upgrade.sh`, byte-for-byte identical to main. The diff for this PR shows zero deletions in the Migration 78/79 region. The prior gap is fully closed.

**Vision alignment:** No change from the first oracle entry (2026-04-21). The Stage 1 finding locks before seeing the implementation and does not change after. PR #806 retires RALPH human-readable labels as directed by Dan — alignment verdict was Confirmed in the first pass and remains Confirmed. The restoration of Migrations 78 and 79 does not touch vision alignment; it was a quality issue.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Gap 1 is closed.** Migrations 78 and 79 are present on the PR branch, verified against the main branch blob. The upgrade.sh diff for this PR touches only the Migration 67 comment block (three lines of cosmetic label text). No functional migration content is added or removed.
- **RALPH prose substitutions carry forward unchanged from pass 1.** All findings from the first oracle pass remain accurate: the naming changes are complete within stated scope, functional identifiers (`source = 'ralph-test'`, path variables, cron marker `LOBSTER-RALPH-LOOP`) are correctly preserved, and no behavioral logic is modified. The `success_criteria` partial-inconsistency noted in pass 1 (file path still contains `ralph-test` while content string now says `WOS test output`) remains — it is not a blocker and does not require a new gap.
- **Feature-bundling-as-deletion pattern from learnings.md (2026-04-21, PR #806):** the pattern was written into learnings.md from the first oracle pass. Reading it now before the second diff confirms the detection vocabulary was correctly applied in pass 1: the PR diff no longer exhibits the deletion pattern. The oracle vocabulary loaded from learnings.md constrained this pass by focusing scrutiny on the upgrade.sh diff first — the fastest confirmation or refutation of the named gap.
- **No new regressions.** Three files changed: `scheduled-jobs/tasks/ralph-loop.md`, `scheduled-tasks/ralph-loop.py`, `scripts/upgrade.sh`. This matches the PR description exactly. The upgrade.sh hunk is a single contiguous block in the Migration 67 region — no other migration regions are touched. No new patterns, deletions, or bundled changes detected.

**Patterns introduced:** No new patterns. The first oracle pass established the partial-rename pattern (retire prose labels first, migrate functional identifiers in a separate PR). This pass adds no structural novelty.

**What this forecloses:** No change from pass 1.

**Opportunity cost note:** No change from pass 1.

**VERDICT: APPROVED**

---

### [2026-04-21] PR #811 — feat: WOS pattern register (oracle/patterns.md) wired into oracle and sweep

**Vision alignment:** The WOS Pattern Register extends the oracle's detection vocabulary with named WOS execution loop signatures (spiral, cascade, burst, dead-end, steady-state). The vision's theory of change requires agents to make intent-anchored decisions without asking Dan — and recognition of systemic execution patterns is a precondition for the steward and oracle to route or escalate correctly. The oracle-vocabulary-as-detection-precondition golden pattern applies: this PR adds a named vocabulary layer the oracle reads before scanning a diff, which is precisely what that pattern specifies as the correct approach. The adversarial prior — all of this is solving the wrong problem — does not survive for the patterns.md addition itself: the patterns are currently unnamed across oracle, steward, and sweep, and naming them in a single document reduces the "different names for the same thing" coordination cost. The adversarial concern that does survive Stage 1: the PR description claims it updates `hygiene/sweep-context.md` to add a Pattern Match step, but this is a runtime file change, not a repo change. PR #804 moved sweep-context.md into `memory/canonical-templates/sweep-context.md` as the versioned canonical. The Pattern Match step was applied to the runtime copy but not to the canonical template — meaning a new install seeded from the canonical will not have Pattern Match. This is the exact failure mode named in the 2026-04-20 learnings.md Document Review entry: "Moving an instruction document from a runtime path to a versioned repo path requires the new versioned copy to be updated when new steps are added." The Stage 1 question — is this solving the right problem in a direction that forecloses better paths? — is confirmed for the repo changes (oracle/patterns.md, lobster-oracle.md), but the runtime-only sweep-context.md update is incomplete: the canonical template is the authoritative file post-PR-#804, and it was not updated.

**Alignment verdict:** Confirmed (for repo changes) / NEEDS_CHANGES (for sweep-context.md wiring)

**Quality finding:**
- **oracle/patterns.md is well-formed.** Five patterns, each with signal threshold and explicit responses for steward, oracle, and sweep. The "Notes on evolution" section correctly identifies that thresholds are initial estimates adjusted via oracle decisions. The citation bar (behavioral change, not labeling) matches the existing learnings.md and golden-patterns.md standard. The table-as-compaction-resistant-encoding golden pattern is not violated here — the markdown section format is appropriate for a reference document read pre-review, not a bootup doc.
- **lobster-oracle.md wiring is correct in scope.** The two-line addition is additive, correctly positioned after the existing vocabulary read instructions, and sets the same behavioral bar ("flag loop signatures when visible"). No conflicts with existing oracle instructions.
- **Gap: Pattern Match step is in the wrong file.** PR #804 (merged 2026-04-21) moved sweep-context.md from `hygiene/` to `memory/canonical-templates/sweep-context.md`. The Pattern Match step was applied to the runtime copy at `~/lobster-workspace/hygiene/sweep-context.md` but NOT to `memory/canonical-templates/sweep-context.md`. Confirmed by grep: the canonical-templates version has no "Pattern Match" section, no reference to `~/lobster/oracle/patterns.md`. The runtime copy has it at line 118. Future installations seeded from the canonical template will not have Pattern Match — the patterns.md vocabulary will be unconnected to the sweep agent on new instances.
- **Encoded Orientation check (constraint-3):** The lobster-oracle.md change is an Encoded Orientation decision — it changes a behavioral default for an agent definition durably. Prior logged decision: PR #804 established `memory/canonical-templates/sweep-context.md` as the versioned canonical. The oracle-vocabulary-as-detection-precondition golden pattern (2026-03-29) is the traceable vision.yaml anchor (constraint-3, orient is the schwerpunkt). Both conditions are satisfied for the oracle read instruction. The sweep-context.md runtime edit lacks the same anchor because it went to the deprecated file.

**Patterns introduced:** WOS loop taxonomy as shared pre-review vocabulary (three consumers: oracle, steward, sweep) — a "single source of truth for pattern names" architectural decision. This is the correct structural form for cross-agent vocabulary.

**What this forecloses:** The patterns.md file is declared read-only, human-editable only. This forecloses programmatic generation of patterns from learnings data — which may be desirable eventually but is correctly deferred per the PR's explicit statement.

**Opportunity cost note:** The sweep-context.md canonical template update was the higher-value half of this PR's stated goals. The oracle read instruction is live; the sweep Pattern Match is live on this instance. But the canonical template gap means the two are disconnected for future installs.

**VERDICT: NEEDS_CHANGES**
**Revision contract:**
- **Gap 1: Pattern Match step absent from canonical template** — `memory/canonical-templates/sweep-context.md` must include the Pattern Match section (read `~/lobster/oracle/patterns.md`, apply each pattern's threshold, generate prescriptions) between the detection pass and the refactor pass, matching what was applied to the runtime copy at `~/lobster-workspace/hygiene/sweep-context.md` lines 118+. The gap is closed when `grep "Pattern Match" memory/canonical-templates/sweep-context.md` returns a match. This is the only required gap; the oracle/patterns.md and lobster-oracle.md changes are approved as-is.


---

### [2026-04-21] Re-oracle v2 PR #811 — feat: WOS pattern register (oracle/patterns.md) wired into oracle and sweep

**Prior NEEDS_CHANGES verdict (v1):** [2026-04-21] PR #811. One gap: Pattern Match step was applied to the runtime copy of sweep-context.md but omitted from `memory/canonical-templates/sweep-context.md`. New installs seeded from the canonical would not have sweep-pattern integration.

**Prior gap status:**

- **Gap 1 (canonical template missing Pattern Match step):** ADDRESSED. Commit 2a2490c8 adds the Pattern Match section to `memory/canonical-templates/sweep-context.md` at line 128. Verification: `grep -n "Pattern Match"` on the PR branch returns `128:### Pattern Match`. The section is substantively identical to the runtime sweep-context.md Pattern Match step. Installation-class divergence is closed.

**Vision alignment:** The adversarial prior — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — was held through both passes of this PR. The threat scenario: creating oracle/patterns.md establishes a shared vocabulary for loop signatures, but if the vocabulary is never consumed by the oracle's detection pass in practice (the oracle reads it but does not change its behavior because the pattern names are too coarse), the file becomes a decorative document rather than a functional discriminator. This scenario cannot be fully resolved without a live oracle run, but the design addresses it structurally: the citation bar ("behavioral change, not labeling") is the same bar applied to learnings.md and golden-patterns.md — if the oracle names a pattern without stating how it constrained analysis, that is the failure mode, and it is catchable at meta-review. The canonical-template gap that triggered v1 NEEDS_CHANGES was the only structural defect. With it closed, the PR is correctly pointed at a real operational need (shared WOS pipeline vocabulary). Stage 1 verdict: alignment confirmed.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Gap 1 closed cleanly.** The Pattern Match section in the canonical template matches the runtime version. The fix commit is minimal (9 lines added to canonical template) and introduces no side effects. The canonical template is now a complete version of sweep-context.md including the Pattern Match step.
- **oracle/patterns.md content is correctly scoped.** Five patterns (spiral, cascade, burst, dead-end, steady-state) with signal thresholds, and three-role response tables (steward, oracle, sweep). The thresholds are labeled as initial estimates with an explicit evolution note. This is appropriate epistemic humility for a new vocabulary.
- **lobster-oracle.md diff is additions-only with no regressions.** The document-review mode, Encoded Orientation Stage 2 check, and patterns.md load instruction are all well-anchored. The Encoded Orientation check passes its own self-applying test (constraint-3 in vision.yaml is the direct anchor). The document-review format adds named-gap tracking that makes future re-reviews decidable without re-reading the full document.
- **Feature-bundling check (learnings.md 2026-04-08 pattern):** The branch diff shows 90+ files changed vs. main — this is a long-lived branch with significant divergence. However, the claimed PR scope is three files (lobster-oracle.md, sweep-context.md, oracle/patterns.md), and the diff against main for those three files is clean with no unexpected deletions. The branch divergence is pre-existing, not introduced by this PR. No regression introduced in the three claimed files.

**Patterns introduced:** Named-vocabulary pattern register as a standalone file (`oracle/patterns.md`) composable with the existing learnings.md + golden-patterns.md reads. Three-role response tables (steward/oracle/sweep) as the encoding format for each pattern — this format makes it clear which system is responsible for detecting, flagging, and prescribing for each loop signature.

**What this forecloses:** Using informal pattern naming in oracle reviews — the oracle is now expected to use the vocabulary in patterns.md when flagging loop signatures, which increases consistency but also means the pattern register must stay current or drift from actual usage.

**Opportunity cost note:** Issue #810 (steward pattern-aware dispatch) is explicitly out of scope and tracked separately. This PR is correctly bounded to the vocabulary and documentation layer.

**VERDICT: APPROVED**

---

### [2026-04-21] PR #813 — fix: correct stale oracle learnings.md path in lobster-meta.md

**Vision alignment:** This PR is a one-line path correction in an agent instruction file. The adversarial prior entering Stage 1: is the path fix the actual problem, or is there a deeper structural issue being papered over with a string correction? The scenario where this work is wasted: the wrong-path problem is a symptom of a broader oracle directory split that remains unaddressed, and fixing one reference leaves the agent reading from an older version of a file that exists at both the workspace and repo paths. The prior survives partial scrutiny — both `~/lobster-workspace/oracle/learnings.md` and `~/lobster/oracle/learnings.md` exist on the live filesystem (contra the PR description's claim that the workspace path "does not exist"). However, the repo copy is the canonical, version-controlled, up-to-date location (1017 lines vs. 824 in the workspace copy). Pointing to the repo copy is unambiguously correct. This PR is also the explicit follow-on action named in the PR #796 oracle verdict (decisions.md line ~159): "lobster-meta.md line 14 reads from ~/lobster-workspace/oracle/learnings.md — should be a follow-on fix." The work is tightly scoped, correctly targeted, and directly serves the vision by restoring one of the lobster-meta agent's two cross-reference signals. Alignment verdict is Confirmed.
**Alignment verdict:** Confirmed
**Quality finding:**
- The diff is exactly one line, touching only the stale path reference in `.claude/agents/lobster-meta.md`. The scope constraint holds.
- The PR description incorrectly states the workspace oracle path "does not exist" — `~/lobster-workspace/oracle/learnings.md` exists and contains 824 lines of real content (older learnings from PRs #696–#720). The actual defect was that the agent was reading a stale subset copy instead of the canonical repo version. This does not affect the fix's correctness but means the PR description mischaracterizes the failure mode.
- The corrected path `~/lobster/oracle/learnings.md` is verified to exist and is the canonical location used by the oracle agent's own read instructions (`lobster-oracle.md` line 18).
- The same line in lobster-meta.md already had the correct prefix for `golden-patterns.md` (`~/lobster/oracle/golden-patterns.md`), confirming this was a half-corrected line — a localized inconsistency, not a systemic one remaining after this fix.
**Patterns introduced:** None. This is a path correctness fix with no behavioral pattern introduced.
**What this forecloses:** Nothing of substance. The workspace oracle directory continues to exist as a runtime artifact; this fix simply routes the lobster-meta agent to the canonical version.
**Opportunity cost note:** None. This is a correctness fix directly named in a prior oracle verdict. The cost of not shipping it is a persistently degraded lobster-meta cross-reference signal.

**VERDICT: APPROVED**

---

### [2026-04-21] PR #788 — Remove duplicated _default_db_path() from heartbeat scripts

**Vision alignment:** The adversarial prior entering Stage 1: this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths. The PR removes three identical copies of `_default_db_path()` from `executor-heartbeat.py`, `steward-heartbeat.py`, and `garden-caretaker.py`, consolidating to `REGISTRY_DB` in `src/orchestration/paths.py`. The threat scenario that could make this wasted effort: the shared DB path should be part of a richer config layer, not a module-level constant, and consolidating to a constant forecloses that move. This scenario does not survive scrutiny — `paths.py` already exists and is explicitly designed to centralize exactly this class of path derivation (its own module docstring names inline re-derivation as the prior bug pattern). The learnings.md "Migration path mismatch produces silent no-op" pattern (2026-04-21) was the load-bearing prior: it specifically names divergent path references across files as a defect class. This PR eliminates three independent divergence points, directly preventing that failure mode. The work is maintenance-class hygiene that reduces future surface area — consistent with operating principle-1 (proactive resilience over reactive recovery). The timing concern (current_focus is WOS execution health, not cleanup) is real but does not constitute misalignment: the PR description notes no new behavior, no new wiring, and no scope expansion.

**Alignment verdict:** Confirmed

**Quality finding:**
- The critical behavioral correctness issue was correctly identified and fixed in the PR itself: `paths.py` previously lacked `REGISTRY_DB_PATH` env override support, while all three inline functions honored it. The PR fixes `paths.py` before doing the consolidation, making the consolidation behavior-preserving. This is the right sequence.
- The `REGISTRY_DB` constant is now module-level — computed once at import time. For cron-dispatched scripts (each invocation is a fresh process), this is equivalent to the prior call-per-invocation pattern. No regression exists in production. However, any test that imports the heartbeat module and then attempts to set `REGISTRY_DB_PATH` via `monkeypatch.setenv` would not affect the already-frozen constant — this is a latent test-authoring trap, not a current defect. No existing test exercises this path.
- Tests pass (22 + 122 = 144 reported) but do not exercise `main()` — they inject `tmp_path`-based DB paths directly into `Registry`. The change from `_default_db_path()` call to `REGISTRY_DB` in `main()` has no test coverage. This is a pre-existing gap in test design (tests never called `main()`), not introduced by this PR. The PR's feature-bundling-as-check ("grep -n REGISTRY_DB — exactly one import + one usage per file") is an appropriate mechanical verification for this change class.
- No `test_paths.py` exists; `REGISTRY_DB`'s `REGISTRY_DB_PATH` branch has no dedicated test. Pre-existing gap surfaced by this PR but not created by it.

**Patterns introduced:** Module-level constant (not callable) as the canonical form for env-derived paths in `paths.py`. This differs from the callable pattern the inline functions used. The module docstring already establishes this as the intended pattern for paths.py, so this is pattern extension, not a new pattern introduction.

**What this forecloses:** The heartbeat scripts can no longer independently override DB path resolution logic without modifying `paths.py`. This is the intended outcome, not a foreclosure risk — the point is that path logic lives in one place.

**Opportunity cost note:** Three review slots consumed for hygiene. The PR is correctly scoped and clean, so the per-slot cost is low. The vision.yaml current_focus is WOS execution health; this PR does not advance that focus but also does not compete with it. No missed higher-priority alternative is visible.

**VERDICT: APPROVED**

---

### [2026-04-21] PR #814 — feat: pattern-aware dispatch eligibility check in steward

**Vision alignment:** The adversarial prior entering Stage 1: this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths. The theory of change is that the steward, by detecting spiral/dead-end/burst loop patterns before dispatching, becomes self-governing rather than requiring manual rescue. This maps to learnings.md's named failure pattern "execution capability built ahead of governing structures produces recurring need for manual rescue" (2026-04-08) — this PR is a governing structure. That alignment is real. However, two signals cut against it. First, vision.yaml `current_focus.what_not_to_touch` explicitly states "New detection or classification rules — improve Orient routing before adding more detection." The spiral/dead-end/burst checks are new detection rules, placed in a field that functions as the system's declared scope boundary for the current week. The oracle cannot waive a what_not_to_touch field because the implementation is well-executed — the field exists precisely to hold this boundary against good-looking work that is out of scope. Second, Gap 1 (spiral gate reads `oracle_approved` audit events not yet written anywhere) means the most architecturally significant of the three gates silently reads zero at every invocation and never fires. Shipping a gate whose primary signal source does not exist is not staged delivery — it is a library without wiring, which learnings.md names explicitly as a recurring failure mode (2026-04-08: "any production routing path that requires a new caller to be added is not effective until the caller is added"). The orbit of this PR is the right problem class, but the timing and the silent-zero defect are Stage 1 concerns that the implementation quality does not resolve.

**Alignment verdict:** Questioned

**Quality finding:**
- **Throttle verdict is a skip, not a batch-size reduction.** `BURST_BATCH_SIZE = 3` is defined as a named constant and cited in the function docstring ("limit batch size to BURST_BATCH_SIZE per cycle"), but the integration in `run_steward_cycle` unconditionally routes all non-`dispatch` verdicts to `skipped += 1; continue`. There is no batch-size tracking, no per-cycle count of throttled vs. allowed UoWs, no partial pass-through. The constant is exported, tested for its face value (`assert BURST_BATCH_SIZE == 3`), but never consumed in logic. This is the patterns.md documentation mismatch failure at the contract layer — the docstring and the patterns.md spec both say "batch into groups of 3" but the implementation says "skip unconditionally." A UoW in a queue of 12 will be skipped on every steward heartbeat until the queue drops below 12, which may never happen organically. The "throttle" verdict label overpromises the behavior delivered.
- **Spiral gate is silently non-functional.** `_count_oracle_passes` counts `event == "oracle_approved"` entries in the audit log. No code in the codebase writes that event today (the PR description acknowledges this as Gap 1). The gate will return 0 oracle passes for every UoW in every steward cycle until the `oracle_approved` audit write is wired. This means the gate providing highest-precedence protection against the spiral pattern — the one most relevant to the current RALPH Cycle 7 failures — cannot fire. Tests are correct (they test the gate's logic with injected audit entries) but they do not and cannot test whether the event is ever written in practice.
- **Dead-end gate is functional as specified.** `_count_failed_or_blocked_transitions` reads `to_status` in `{"failed", "blocked"}` from real audit entries that are written today. This gate will fire on real data. The threshold (2) matches patterns.md and is imported by tests rather than re-declared, consistent with the learnings.md constant-mirroring failure pattern (2026-04-20 PR #800 and PR #696).
- **Test design is sound where it can be.** Pure function decomposition (`_count_oracle_passes`, `_count_failed_or_blocked_transitions`) allows tests to exercise counting logic independently. Threshold constants are imported from production, not re-declared locally. Precedence tests cover all three pairwise combinations. The 29 tests cover what the implementation does. The gap is that what the implementation does does not fully match what the PR description and patterns.md spec say it does — the tests validate the implementation accurately, not the spec faithfully.

**Patterns introduced:** Dispatch gate as a pre-`_process_uow` eligibility check in `run_steward_cycle` — a new structural slot in the loop that future pattern detectors can occupy. The audit entry `dispatch_eligibility_skip` with `eligibility` field establishes a queryable signal for observability of gate firings. Named constants for pattern thresholds with `# Source: oracle/patterns.md §...` comments establish traceability from code to pattern vocabulary.

**What this forecloses:** Until the throttle verdict is implemented as actual batch-size gating (not a skip), the burst response cannot be tuned without a code change. The current behavior — skip all UoWs in a burst queue — is more aggressive than the spec intends, which means the throttle label masks a potential starvation outcome.

**Opportunity cost note:** This PR was built while vision.yaml `current_focus.what_not_to_touch` names "New detection or classification rules" as out of scope. The dead-end gate is the most immediately useful component and is directly relevant to the RALPH Cycle 7 starvation constraint. Splitting: shipping only the dead-end gate (functionally correct, in-scope by the narrowest reading) vs. all three gates (two non-functional or misspecified) would have been the lower-risk staging decision. The PR as submitted installs a gate slot that is partially wired — better than nothing, but the spiral gate cannot protect against the pattern it names until the audit write is added.

**VERDICT: NEEDS_CHANGES**
**Revision contract:**
- **Gap A (blocking): Throttle behavior mismatch.** The throttle verdict must either (a) implement actual batch-size limiting — allow up to `BURST_BATCH_SIZE` UoWs through per cycle even during burst, skip the rest — or (b) rename the verdict to `skip_burst` and update `BURST_BATCH_SIZE`'s docstring and patterns.md reference to remove the "batch into groups of 3" claim. The current implementation silently delivers harder behavior than specified. This is "addressed" when the behavior matches the docstring and the patterns.md §burst Steward response.
- **Gap B (advisory): Spiral gate silent zero.** Either (a) add a `# TODO(#NNN): oracle_approved audit write not yet wired — this gate will always return 0 until that integration ships` comment in `_count_oracle_passes` and in the `_check_dispatch_eligibility` spiral branch so future readers are not confused, or (b) defer the entire spiral detection block (with its threshold constant) to the PR that wires the audit write. "Addressed" means the code's behavior and the documentation of its current behavior are consistent — a reader should not have to read the PR description to know the gate is not yet functional.

---

### [2026-04-21] Re-oracle PR #814 — feat: loop-pattern-aware dispatch eligibility check in Steward

**Prior NEEDS_CHANGES verdict:** [2026-04-21] PR #814. Two gaps named.

**Prior gap status:**

- **Gap A (throttle batch-size — blocking):** ADDRESSED. `throttle_count = 0` is introduced before the loop. The `throttle` branch in `run_steward_cycle` now checks `if throttle_count < BURST_BATCH_SIZE: throttle_count += 1` (allow through, fall through to `_process_uow`) vs. `else: skipped += 1; continue` (skip beyond batch limit). The first `BURST_BATCH_SIZE` (3) UoWs that receive a `throttle` verdict are dispatched normally; subsequent ones are skipped. The audit entry is written only on the skip path. The behavior now matches the `BURST_BATCH_SIZE` constant's docstring and the patterns.md §burst Steward response specification. The prior defect (unconditional skip on any throttle verdict) is closed.

- **Gap B (spiral gate silent zero — advisory):** ADDRESSED. Two locations now document the non-functional state: (1) the `_count_oracle_passes` docstring carries a NOTE that oracle_pass_count will always be 0 until the oracle agent integration writes `event="oracle_approved"`, with the issue reference `#810`; (2) the spiral check branch in `_check_dispatch_eligibility` repeats the same NOTE inline. A reader of either the helper or the top-level gate function encounters the documentation without consulting the PR description. The revision contract criterion ("a reader should not have to read the PR description to know the gate is not yet functional") is met.

**Vision alignment:** The adversarial prior — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — was re-examined against the updated context. The `what_not_to_touch` exception for "WOS dispatch loop pattern gating (spiral/dead-end/burst)" in vision.yaml removes the Stage 1 scope-boundary concern from the original verdict. The theory of change (steward becomes self-governing against loop patterns) maps directly to `current_focus.primary` ("WOS execution health: fix executor dispatch starvation"). The two fixes are targeted at real behavioral mismatches from the prior verdict and do not introduce new problems. The opportunity cost note from the original verdict is no longer applicable: the in-scope exception resolves the timing concern. Stage 1 verdict: Confirmed.

**Alignment verdict:** Confirmed

**Quality finding:**
- **Gap A fix is behaviorally correct.** The `throttle_count` variable accumulates across the cycle (not reset per-UoW), which is the correct semantic for a per-cycle batch limit. The allowed-through path falls through to `evaluated += 1` and `_process_uow` without a `continue`, so throttle-allowed UoWs are dispatched normally. The skip path writes `dispatch_eligibility_skip` with `eligibility: "throttle"`. The pure-function tests for `_check_dispatch_eligibility` correctly test the return value; the `throttle_count` integration is in `run_steward_cycle` and not unit-tested in isolation — this is acceptable for an integration-level accumulator.
- **Gap B documentation is present at both call sites.** `_count_oracle_passes` docstring and the spiral branch in `_check_dispatch_eligibility` both carry the NOTE with `#810` reference. The documentation is discoverable by any reader of either function without PR history.
- **Feature-bundling check passes.** Three files changed: `paths.py` (+1 constant), `steward.py` (new code), test file (new). All three are within stated scope. Mechanical check cost 30 seconds; negative result recorded per learnings.md PR #712/#717/#806 pattern.
- **`_queue_depth` snapshot before the loop is correct.** The burst check is consistent across the cycle — all UoWs see the same depth. Inline comment documents the reason. No per-UoW re-query.

**Patterns introduced:** No patterns introduced beyond those named in the original [2026-04-21] PR #814 verdict. The batch-size gating pattern (allow first N, skip rest, count with a per-cycle accumulator) is an application of the existing steward loop pattern already established in `run_steward_cycle`.

**What this forecloses:** The spiral gate remains non-functional until `event="oracle_approved"` is written by the oracle integration. This is now explicitly documented in code, so the foreclosure is legible. The dead-end gate and burst batch-size gate are both functional.

**Opportunity cost note:** With the `what_not_to_touch` exception in place, the opportunity cost question is resolved. These gates are in scope as part of WOS execution health.

**VERDICT: APPROVED**


---

### [2026-04-22] PR #823 — fix: redirect test output artifacts away from production outputs dir (#819)

**Vision alignment:** The adversarial prior — this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths — finds no purchase here. The contamination is empirically grounded (154 of 295 failed UoWs in the production record are test artifacts), the impact on observability is real (apparent failure rate inflated from ~21% to ~44%), and this kind of corrupted signal directly impairs the WOS execution health work that is the current_focus.primary. The theory of change is: move test writes to isolated tmpdir, production record becomes accurate. The question is whether the env-var seam is the right lever or an over-extension. An env-var seam adds a production-facing runtime knob for path override — a capability not stated in the issue and not required for the fix. A fixture-only approach (monkeypatching the constant in conftest.py) would achieve the same isolation without adding a new env-var contract. However, the env-var approach has a defensible advantage: it makes the redirect mechanism explicit and inspectable at process level (visible in `ps -e` / environment introspection), whereas a monkeypatch is invisible outside the Python runtime. This is not a direction-foreclosing choice. The alignment verdict is Confirmed.

**Alignment verdict:** Confirmed

**Quality finding:**
- **`_OUTPUT_DIR_TEMPLATE` env-var read at import time is effectively vestigial in the fixture path.** The fixture sets `WOS_OUTPUTS_DIR` via `monkeypatch.setenv` at test setup time — after module import. At import time, `_OUTPUT_DIR_TEMPLATE` will have already captured the production path (env var not yet set). The call-time read in `_output_ref_path()` is what actually achieves the redirect. The module-level env-var read matters only if `WOS_OUTPUTS_DIR` is set in the process environment before Python starts (e.g., CI env var injection). The PR's stated purpose for the dual read — "existing tests that monkeypatch it directly continue to work unchanged" — is correct but the reasoning is reversed: those tests continue to work because they monkeypatch the attribute after import (overriding whatever value the import captured), not because of the env-var read at import time. This is a documentation accuracy issue, not a correctness defect.
- **Three existing tests that monkeypatch `_OUTPUT_DIR_TEMPLATE` directly now have their explicit monkeypatching silently superseded by the autouse fixture.** `_output_ref_path()` checks `WOS_OUTPUTS_DIR` from the environment first, so when the autouse fixture is active (every test), `_OUTPUT_DIR_TEMPLATE`'s value is irrelevant to `_output_ref_path()`'s behavior. Those tests' explicit monkeypatching is now dead code. They continue to pass (both the autouse redirect and the monkeypatch point to non-production tmpdirs), but any assertion in those tests that infers `_output_ref_path()` behavior from `_OUTPUT_DIR_TEMPLATE`'s value is testing a now-unreachable code path. Severity: advisory. Tests pass; the risk is future confusion when someone changes `_OUTPUT_DIR_TEMPLATE` in a test and wonders why `_output_ref_path()` ignores it.
- **Env var cleanup between tests is correctly handled.** `monkeypatch.setenv` in `isolate_executor_outputs` uses pytest's `monkeypatch` fixture, which restores all patched values on test teardown. No env pollution between tests.
- **Production count verification (1788 before/after) is meaningful for the stated claim.** It confirms the test run wrote zero new artifacts to the production directory. It does not address the already-present ~154 contaminating artifacts — issue #819's impact statement described those as existing contamination, not something the fix would remove retroactively. The verification scope matches the fix scope.

**Patterns introduced:** Autouse conftest fixture using `monkeypatch.setenv` for filesystem isolation. This extends the existing conftest isolation pattern (e.g., `isolate_inbox_server_paths`) to the executor output path. The autouse scope means no test can accidentally write to production without explicitly opting out — a correct default.

**What this forecloses:** The env-var contract (`WOS_OUTPUTS_DIR`) is now a public interface point. Future changes to the production output path must update both `_OUTPUT_DIR_TEMPLATE`'s default string and document the env-var override. The "belt-and-suspenders" double-read pattern (constant at import, env at call time) introduces a subtle precedence order that future contributors may misread. If a future engineer monkeypatches `_OUTPUT_DIR_TEMPLATE` expecting it to override `_output_ref_path()` behavior, they will be silently wrong.

**Opportunity cost note:** The existing ~154 contaminating artifacts remain in the production directory after this fix. The fix prevents future contamination; it does not clean up past contamination. If production failure-rate metrics are being used for decision-making now, a one-time cleanup of the production directory (or a timestamp-based filter from the fix's merge date) would be needed to get a clean signal. Not a PR scope issue — this is an issue-level gap.

**VERDICT: APPROVED**

---

### [2026-04-22] PR #829 — fix: improve mode-recognition discriminator, migrate rule to discriminator

**Vision alignment:** The adversarial prior entering Stage 1: this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths. The current_focus.primary is WOS executor starvation and pipeline health — a behavioral config change to CLAUDE.md is a different axis entirely. However, the adversarial prior finds no structural purchase here. Vision.yaml `principle-5` ("Discriminator improvement over rule addition — when a behavioral rule isn't followed, improve the discriminator") directly names this move: the existing mode-recognition section was not producing reliable classification under compaction pressure, and this PR improves the discriminator rather than adding another advisory note on top of it. This PR closes a named residual from PR #732's oracle verdict ("the Bias to Action gate row still says 'fire only after DESIGN_OPEN has been ruled out' — this is redundant but benign, a follow-on cleanup"). The cheaper test was already run: PR #732 confirmed the pre-table classifier form is the correct architecture; this PR completes the encoding. The opportunity cost is negligible — 15 insertions, 16 deletions, pure behavioral config, no code change. The table-as-compaction-resistant-encoding golden pattern (2026-03-27) applies here: the checklist form is scannable under compaction pressure in a way informal prose is not, for the same reason table rows resist corruption. That pattern constrained this review: I looked for whether the checklist form actually achieves compaction resistance or merely looks like it — the criterion being whether each step's trigger can be applied without synthesis. The stage 1 verdict is: Confirmed.

**Alignment verdict:** Confirmed

**Quality finding:**
- **The checklist form solves the synthesis problem correctly.** The old signal lists required the dispatcher to synthesize a binary classification from a list of characterizations — "Discriminator signals that indicate ACTION mode" gave the right vocabulary but required judgment at application time. The new form makes each item a self-contained boolean test that resolves directly to a classification branch. The Step 1 / Step 2 ordering with an explicit "stop at first match" instruction eliminates the need to evaluate all signals before deciding. This is the discriminator-improvement over rule-addition pattern applied correctly.
- **The fourth ACTION signal carries the author's named open question.** "Message asks Lobster to execute a specific, named command or task with a stated target" is broader than the other three signals, which all require an artifact reference. A message like "run the nightly sweep" contains a named command and could reasonably be considered ACTION — but it could also be a high-level operational request whose scope and parameters are genuinely open. The open question is whether "stated target" is load-bearing. Examining the four signals: signals 1-3 anchor on artifact references (file, PR, issue, component, or a modification to an existing artifact), which are concrete and verifiable from the message text. Signal 4 anchors on "named command or task" — a somewhat softer boundary. However, the default-to-DESIGN_OPEN fallback provides the correct safety: when signal 4 is borderline, the dispatcher can decline to classify ACTION and fall through to DESIGN_OPEN. This is the right failure mode. The open question is real but not blocking; the fallback handles the ambiguous case.
- **Bias to Action gate row update is correctly scoped.** The new trigger ("Classifier returned ACTION. Proceed with implementation without asking for confirmation.") removes the classification work that had been embedded inside a gate row — where the old text was re-describing what an ACTION message looks like. The gate row now correctly states the precondition (classifier output) and the action (proceed). The redundant "fire only after DESIGN_OPEN has been ruled out" enforcement clause is cleanly gone. This is exactly the residual PR #732's oracle verdict named.
- **Design Gate trigger in the table is now slightly inconsistent with the new classifier.** The Design Gate row still reads "A message is DESIGN_OPEN when no concrete output artifact can be stated in one sentence from the message alone" — this is correct but uses the older single-question form, while the classifier above now enumerates four DESIGN_OPEN signals. This is not a defect (the trigger is accurate), but a reader who navigates from the gate table row back to the classifier to verify will find the two representations slightly asymmetric. The gate table row is the compaction-resistant memory anchor; the classifier section is the application procedure. The asymmetry is acceptable — the table row does not need to repeat all four signals.

**Patterns introduced:** Mechanical checklist as the classifier form for a behavioral gate selector. The discriminator section now defines a two-step procedure with explicit branching at each step and a named default, rather than listing signals for the dispatcher to weigh. This is the correct evolution: from "here are signals that indicate X" (vocabulary list) to "run these checks in order, stop at first match" (procedure). Future gate selectors in CLAUDE.md should use this form.

**What this forecloses:** Nothing structural. The enforcement logic for Design Gate and Bias to Action is unchanged. No gate is removed. The default-to-DESIGN_OPEN fallback preserves the cautious failure mode for ambiguous messages. The old "do not use table row order to infer priority" workaround line is correctly removed — the classifier's structural position before the table makes it unnecessary, and removing it removes the implicit acknowledgment that table row order was ambiguous.

**Opportunity cost note:** The PR is 15 insertions, 16 deletions, a net reduction of one line, and closes a named oracle residual from PR #732. This is the minimum viable fix for the named residual. No opportunity cost is generated by this scope.

**VERDICT: APPROVED**

---

### [2026-04-22] Doc review: docs/vision-object.md — PR #835 "Field Specificity and the Unfakeability Criterion"

**Vision alignment:** The theory of change in vision.yaml is that the Vision Object produces structurally different routing decisions — not just better-justified ones. Risk-2 in vision.yaml names "vision_ref populated with boilerplate rather than genuine anchoring" as a live concern, and constraint-2 names the failure mode directly: "fluency is not understanding." This PR documents a previously unnamed failure mode — post-hoc citation — where an agent satisfies the structural citation criterion (citation present) while failing the functional criterion (routing was modified by the field). The work is correctly positioned: before you can enforce specificity, you need to name the distinction. The adversarial prior asks whether the document creates an illusion of a solved problem by naming a criterion that has no enforcement hook. The scope clarification section directly addresses this: it defers the specificity audit to follow-on work and explicitly frames the document as a field-authorship criterion, not a verdict on existing content. The prior is disconfirmed — the document does not claim to close the structural gap; it names and frames it. The direction is correct.

**Alignment verdict:** Confirmed

**Interpretation finding:** The document names the post-hoc citation failure mode correctly and gives it a test (the bootup candidate test). What the document makes somewhat invisible is that the failure mode it names is not hypothetical — vision.yaml currently contains fields (e.g., `phase_intent`) that would likely fail the bootup candidate test as sole load-bearing elements. The scope clarification defers this observation intentionally, but a reader who does not hold vision.yaml in context alongside this document may read the unfakeability criterion as a prospective authorship guide rather than a retrospective diagnostic that applies immediately to the current artifact. The document does not lie about this; it names "specificity audit" as needed work. The gap is one of operational urgency, not specification accuracy: the criterion and the current artifact that needs to pass it are in the same repository, and no mechanism connects them.

**Named gaps:**

- **Gap 1: No connection between the unfakeability criterion and the specificity audit of current vision.yaml fields** — The document defines the criterion and names the needed follow-on ("existing fields...should be noted as needing a specificity audit"), but does not identify which fields require the audit, does not assign ownership, and does not create a traceable artifact (e.g., a checklist, an open decision, a GitHub issue reference). A reader of vision-object.md in 30 days will find a criterion document and a vision.yaml whose fields may still fail that criterion, with no visible link between them. This gap is not a correctness defect in this PR — it is a completeness boundary the document intentionally draws. The gap is addressable by: (a) adding a named open decision in vision.yaml flagging the specificity audit as pending; or (b) creating a GitHub issue for the specificity audit with a reference from this doc. Neither is required for this PR to be approved; both would close the gap cleanly.

**Patterns introduced:** The specificity/fluency distinction as a named criterion for Vision Object field authorship. This extends the existing substrate test (applied to the Vision Object as a whole) to the field level. The bootup candidate test ("with/without field present, would routing differ?") is the field-level analog of sc-4 ("removing vision.yaml would cause structurally different routing outcomes"). The pattern nests correctly and is reusable for any structured intent artifact.

**What this forecloses:** Nothing structural. The section is additive specification with no code surface. The enforcement mechanism (how agents verify the criterion at runtime) remains unbuilt; this document does not foreclose any implementation path for enforcement.

**VERDICT: APPROVED**
