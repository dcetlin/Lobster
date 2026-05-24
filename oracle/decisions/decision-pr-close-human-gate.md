# Decision: PR-Close Human-Gate Enforcement Layer

**Date:** 2026-05-18
**Status:** Decided — prompt guard in place, PreToolUse hook deferred
**Related PR:** #1190 (feat/wos-pr-close-human-gate-uow_20260514_dbb5ef)
**Oracle gap addressed:** Gap 4 (R1 verdict, blocking)

## Decision

The PR-close human-gate check is currently enforced via a mandatory guard section
injected into every executor prompt. A PreToolUse hook intercepting `gh pr close`
shell commands is **explicitly deferred** to a follow-on issue.

## Rationale for deferral

1. **Acute incident covered.** PR #1190 was triggered by a specific hygiene-run
   incident in which executors auto-closed gated PRs with no guard at all. The
   prompt guard directly addresses that incident path. Shipping it now stops the
   bleeding; hook implementation is an improvement, not a prerequisite for safety.

2. **Prompt guard is defense-in-depth, not the only layer.** The `is_pr_human_gated()`
   utility function is wired into the prompt template and is also callable from any
   structural enforcement path added later. The guard text instructs executors to
   call `gh pr view --json labels` and check before closing — the same check the
   hook would perform. A hook would make the check mandatory rather than advisory,
   but the advisory guard reduces expected failure rate substantially.

3. **Hook implementation scope is non-trivial.** A correct PreToolUse hook must:
   parse the `gh pr close` command from `tool_input["command"]`, extract the PR
   number and repo, call `gh pr view`, compare labels against `HUMAN_GATE_LABELS`,
   and abort the tool call with a structured error. This requires a hook file,
   registration in `.claude/settings.json`, and end-to-end testing. Bundling this
   into PR #1190 would expand scope beyond the acute fix.

4. **Pattern acknowledgment.** This deferral explicitly acknowledges the
   `advisory-to-hook promotion` golden pattern (2026-04-22) and the principle-3
   finding in the R1 oracle verdict. The deferral is not a disagreement with the
   oracle's structural analysis — it is a scope decision for this PR only.

## What this decision does NOT do

- It does not establish a precedent that advisory enforcement is acceptable for
  deterministic conditionals in general. Principle-3 (vision.yaml) stands.
- It does not close the hook implementation work item. A follow-on issue must be
  opened to implement the PreToolUse hook.

## Follow-on action required

Open a GitHub issue titled:
  "Implement PreToolUse hook for gh pr close human-gate check (structural enforcement)"

The issue should reference PR #1190 and this decision doc. The hook should call
`is_pr_human_gated()` from `src.orchestration.wos_issue_lifecycle` as its check
implementation, making the existing utility function the operative enforcement
mechanism rather than a callable-but-not-called library.
