# WOS Completion Report Spec

*Status: Approved — 2026-05-09*

---

## Purpose

The WOS execution layer currently closes UoWs into silence: the audit trail is complete, but nothing reaches the operator. This spec defines the notification layer that bridges the Done()/Failed() transition to Dan's attention stream. Two surfaces: a per-cycle Telegram ping emitted at each UoW close, and a daily digest that aggregates the day's metabolic signal. Together they give Dan real-time visibility into what completed and end-of-day calibration data for system health. The schema feeding both surfaces is `UoWCompletionSurface` (defined in `docs/wos/uow-completion-surface-schema.md`).

---

## Per-Cycle Ping

**Trigger:** End of Done() or Failed() branch in `_process_uow()` in `steward.py`. Non-fatal — inbox write failure must not block the Done/Failed transition.

**Message type:** `wos_done` (new inbox type; dispatcher handler required).

**Short-form Telegram format (default):**
```
UoW done: <uow_title> [<primary_outcome>]
<steward_cycles> cycle(s) · <token_usage> tokens · <seeds_surfaced_count> seeds surfaced
```

**Rich-form Telegram format (non-pearl outcomes, or >1 execution attempt):**
```
UoW done: <uow_title>
Outcome : <primary_outcome>
Topology: <gate_fired description> (<steward_cycles> cycles, <execution_attempts> attempt(s))
Tokens  : <token_usage>
Seeds   : <seeds_surfaced_count> new item(s) surfaced
Rationale: <completion_rationale>
```

**Failed UoW format:**
```
UoW failed: <uow_title>
Topology: <gate_fired> gate (<steward_cycles> cycles, <execution_attempts> attempts)
Tokens  : <token_usage or "unknown">
Failure : <failure_summary>
```

**Fields sourced from `UoWCompletionSurface`:**

| Field | Registry source | Status |
|---|---|---|
| `uow_title` | `uow_registry.summary` | Present |
| `primary_outcome` | `uow_registry.outcome_category` | Present (migration 0018) |
| `steward_cycles` | `uow_registry.steward_cycles` | Present |
| `lifetime_cycles` | `uow_registry.lifetime_cycles` | Present |
| `token_usage` | `uow_registry.token_usage` | Present (migration 0015) |
| `execution_attempts` | `uow_registry.execution_attempts` | Present (migration 0014) |
| `gate_fired` | `uow_registry.gate_fired` | **Missing** — see Schema Additions §1 |
| `seeds_surfaced_count` | structured `write_result` seeds field | **Missing** — see Schema Additions §2; interim: count from `artifacts` where `category='seed'` |
| `completion_rationale` | `steward_log` event `steward_closure`, field `assessment` | Present (unextracted) |
| `failure_summary` | `uow_registry.close_reason` + audit_log | Present (unextracted) |

---

## Daily Digest

**Trigger:** EOD scheduled job (existing `wos_metrics_report` job or equivalent), OR when the day's completed UoW count crosses a configurable threshold (default: 10).

**Format:**
```
WOS Daily — <date>
Completed : <N> UoW(s)
  pearl <N>  seed <N>  heat <N>  shit <N>
  Seeds surfaced: <total seeds count>
  Avg tokens: <avg token_usage>
  Avg cycles: <avg steward_cycles>
Failed    : <N> UoW(s) (<dominant gate>)
Churn     : <spiral gate count> spiral / <dead_end count> dead-end / <burst count> burst today
CC usage  : <cc_usage_pct>% of quota · <remaining_tokens> remaining
```

**Fields:**
- Outcome distribution: count of each `primary_outcome` value for the day's closed UoWs.
- `seeds_surfaced`: sum of `seeds_surfaced_count` across all completed UoWs (interim: from `artifacts` approximation).
- `cc_usage_pct` / `remaining_tokens`: drawn from existing quota tracking (already available to dispatcher); represents remaining Claude Code session quota at digest time.
- Gate churn: count of UoWs where each `gate_fired` value appeared. This is the day's topology health signal.
- `avg_tokens` / `avg_cycles`: mean over completed UoWs for the day. Excluded from failed-only days.

**Consolidation threshold:** If no UoWs completed during the day, suppress the digest (no noise for idle days). If fewer than 3 UoWs completed, collapse to a one-liner: `WOS: <N> completed (pearl <N>, heat <N>) — <total tokens> tokens`.

---

## Schema Additions

### Addition 1: `gate_fired` registry column (small — ~3 lines SQL)

Add `gate_fired TEXT NULL DEFAULT 'none'` to `uow_registry` as migration 0019.

In `_process_uow()`, when `_check_dispatch_eligibility()` returns a non-`"dispatch"` verdict, translate via the mapping and write the highest-severity gate seen to the registry:

```python
_GATE_TRANSLATION = {"escalate": "spiral", "pause": "dead_end", "throttle": "burst", "dispatch": "none"}
_GATE_SEVERITY    = {"spiral": 3, "dead_end": 2, "burst": 1, "none": 0}
```

Precedence: `spiral > dead_end > burst > none`. Once written, only upgrade — never downgrade.

### Addition 2: `seeds_surfaced` structured write_result extension (medium)

Extend `write_result` to accept an optional `seeds` list (backward compatible — field is optional):

```json
{"seeds": [{"title": "...", "description": "...", "suggested_issue": "..."}]}
```

In `maybe_complete_wos_uow()`, parse and store in a new `seeds_surfaced TEXT NULL` JSON column (migration 0020). The `UoWCompletionSurface` reads from this field instead of the `artifacts` approximation.

Interim (before Addition 2 lands): populate `seeds_surfaced_count` from `artifacts` where `category='seed'`. This over-counts auto-extracted issue refs but is acceptable until the structured field lands.

---

## Implementation Order

1. **Notification layer** — `wos_done` inbox message type + dispatcher handler + per-cycle ping. No schema additions required; degrades gracefully when `gate_fired` / `seeds_surfaced` are absent. Insertion point: end of Done() branch in `_process_uow()` (~line 4440 in `steward.py`).

2. **`gate_fired` registry column** — migration 0019 (3 lines SQL) + one write in `_process_uow()`. Notification layer gains topology signal immediately.

3. **Daily digest job** — wire `wos_metrics_report` (or new job) to emit the digest format above, reading from the registry. CC quota draw from existing quota tracking.

4. **`seeds_surfaced` structured reporting** — migration 0020 + `write_result` wire extension + `maybe_complete_wos_uow()` update. Most invasive step; defer until the notification layer is stable and the artifact-approximation gap is confirmed as noisy.
