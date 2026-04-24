# Design Decision: Automated GitHub Issue Filing and Telegram Escalation from system-retrospective Job

**Date:** 2026-04-23
**Status:** ACCEPTED
**Vision anchor:** principle-1 (proactive resilience), vision.yaml core.inviolable_constraints.constraint-3 (Encoded Orientation requires prior logged decision of same class)
**Observation-to-behavioral-change loop:** vision.yaml current_focus.this_week.primary (WOS execution health) — the retrospective job is the structural mechanism that closes the observe→orient loop by surfacing recurring smells without manual inspection.

## Decision

Authorize automated GitHub issue filing and Telegram escalation from the `system-retrospective` scheduled job:

- **Automated issue filing:** When a smell pattern with `severity: high` is detected and no open issue for that pattern already exists (checked via `gh issue list --label smell:{id}`), the job may file a GitHub issue on `dcetlin/Lobster` — up to `MAX_ISSUES_PER_RUN = 3` issues per execution to guard against floods.
- **Telegram escalation:** When a smell is detected with `recurrence_count >= ESCALATION_THRESHOLD` (2) consecutive runs, the job writes an inbox message to the admin chat (`ADMIN_CHAT_ID`) via `write_inbox_message`. The message is informational only — no destructive action is taken.

## Rationale

The system retrospective job closes a proprioceptive gap: without it, recurring smells (e.g., bare python3 in migrations, oracle dual-write) are only surfaced when an oracle review happens to catch them. This is reactive detection, not structural prevention (principle-1). Automated issue filing and Telegram escalation convert detection into a forcing function — a smell that fires 2+ times in a row reaches human attention without requiring a code review to catch it first.

Both actions are bounded and reversible:
- Issue filing is rate-limited (`MAX_ISSUES_PER_RUN`) and deduplicated by label check
- Escalation is informational; no autonomous close, retry, or destructive action occurs
- The job is gated by `jobs.json` `enabled` field — it can be disabled without a code change

This decision has the same structural class as `decision-needs-human-review-escalation.md`: an Encoded Orientation action (system acts without Dan's explicit input per invocation) backed by a prior logged decision (this file) and a traceable vision.yaml anchor (principle-1, constraint-3).

## Constraints

- Issue filing is gated on `severity: high` only — medium and low smells are logged to the assessment document but do not trigger automated issue creation
- Telegram escalation is gated on `recurrence_count >= ESCALATION_THRESHOLD` (a named constant, currently 2) — single-run detections do not escalate
- Both actions are read-only from the user's perspective: no PR merges, no code changes, no data deletion occur automatically
- The job writes at most `MAX_ISSUES_PER_RUN = 3` issues per execution
- Existing open issues for a pattern (tracked via `issue_ref` in smell-patterns.yaml or via label search) suppress duplicate filing
