# PR Review Layer Model — Design

*Status: DRAFT — Phase 2 WOS Sprint, Item 21*
*Written: 2026-03-30*

---

## 1. Purpose

When a PR is opened on dcetlin/Lobster, the system currently has no routing logic. Every PR goes to Dan by default — which means Dan spends attention on trivial subagent cleanup PRs and may miss PRs that genuinely need human judgment. This design installs a classification layer that fires when a PR is opened and routes it to one of three paths: oracle-only, oracle+deep, or Dan-required.

The goal is not to replace Dan's judgment — it is to protect it. The system should route to Dan only when there is a real reason.

---

## 2. Classification Signals

### 2.1 PR Size

Measured in total lines changed (additions + deletions).

| Bucket | Lines changed | Weight |
|--------|--------------|--------|
| Trivial | 0–50 | Low |
| Small | 51–200 | Medium |
| Medium | 201–500 | Medium-High |
| Large | 501+ | High — escalation candidate |

Size alone does not determine routing. It is one input to the decision tree.

### 2.2 File Categories

Files touched are classified into categories. A PR may touch multiple categories.

| Category | Pattern examples | Notes |
|----------|-----------------|-------|
| `orchestration` | `agents/`, `scheduled-tasks/`, `connectors/`, `hooks/` | Behavioral code; higher scrutiny |
| `docs` | `docs/`, `*.md` (not CLAUDE.md) | Lower risk by default |
| `config` | `config/`, `*.json`, `*.yml` (not docker) | Medium risk |
| `install` | `install.sh`, `scripts/`, `deploy/` | High risk — affects live systems |
| `schema` | `memory/`, files containing `ALTER TABLE`, `CREATE TABLE` | High risk — migration required |
| `claude-md` | `CLAUDE.md`, `.claude/` | Always escalates to Dan |
| `tests` | `tests/`, `*_test.py`, `test_*.py` | Positive signal (test coverage present) |
| `docker` | `docker*`, `docker-compose*` | Medium risk |

### 2.3 Author Type

| Author type | Signal |
|-------------|--------|
| Subagent (bot account, or PR body contains "Generated with Claude Code") | Lower trust, but oracle handles |
| Human contributor (not the repo owner) | Dan-required by default |
| Repo owner (dcetlin) | Applies normal routing |

Bot/subagent PRs are expected to have well-scoped changes. If they don't (e.g., large + touches orchestration), the size/category signals override the author discount.

### 2.4 Test Coverage Delta

Measured as presence/absence of test file changes, not line-level coverage metrics (which require CI).

| Signal | Interpretation |
|--------|---------------|
| PR touches `tests/` or adds `*_test.py` | Positive — test coverage present |
| PR modifies `orchestration` or `schema` files but has no test changes | Negative — coverage gap |
| PR is docs-only | Tests not applicable |

A negative test coverage signal on a behavioral change is an escalation trigger (see Section 4).

---

## 3. Decision Tree

```
PR opened
  │
  ├─► [HARD ESCALATION] — any of these present → DAN-REQUIRED immediately
  │     - touches CLAUDE.md or .claude/
  │     - tagged "needs-dan-review"
  │     - author is external contributor (not dcetlin or known bot)
  │     - >500 lines changed
  │     - touches install.sh or deploy/ scripts
  │
  ├─► [SOFT ESCALATION CHECK] — any of these present → accumulate escalation score
  │     - touches schema files (+2)
  │     - touches orchestration files (+1)
  │     - no test changes on behavioral PR (+2)
  │     - medium or large size (+1)
  │     - touches config (+1)
  │
  │   If escalation score >= 3 → ORACLE + DEEP path
  │   If escalation score 1–2 → ORACLE-ONLY path (with note in report)
  │   If escalation score 0   → ORACLE-ONLY path
  │
  └─► Output: routing decision + classification report attached to PR as comment
```

### Path definitions

**ORACLE-ONLY**
- Oracle agent runs adversarial review: logic errors, missing edge cases, spec compliance.
- Oracle posts a comment with findings.
- No further routing unless oracle flags a critical finding (see Section 4).
- Dan is not pinged.

**ORACLE + DEEP**
- Oracle agent runs adversarial review.
- Deep review agent runs: checks for architectural coherence, WOS principle compliance, cross-doc consistency.
- Both agents post findings.
- If either agent flags a critical finding, escalates to DAN-REQUIRED.
- Dan is not pinged unless escalation triggers.

**DAN-REQUIRED**
- Oracle and deep reviews run (their output is available when Dan looks at the PR).
- Dan is notified via Telegram/Slack with a summary: PR title, routing reason, and oracle headline findings.
- PR is labeled `needs-dan-review` if not already.

---

## 4. Escalation Triggers

These conditions force escalation to DAN-REQUIRED regardless of initial classification:

| Trigger | Reason |
|---------|--------|
| `CLAUDE.md` or `.claude/` touched | Dispatcher behavior change; high blast radius |
| `>500 lines changed` | Scope too large for automated review confidence |
| `needs-dan-review` label present | Explicit human signal |
| External contributor (not repo owner or known bots) | Human-to-human review expected |
| `install.sh` or `deploy/` touched | Affects live system state |
| Oracle flags `CRITICAL` severity finding | Oracle identified a likely breaking change |
| Schema change with no corresponding migration file | Data integrity risk |
| PR modifies >3 file categories simultaneously | High surface area; coordination risk |

When any escalation trigger fires, the routing reason is appended to the Dan notification: "Escalated because: touches CLAUDE.md."

---

## 5. Integration Point

### Where classification fires

The classifier fires in the WOS event loop when a `pull_request.opened` or `pull_request.reopened` GitHub webhook event is received. If webhooks are not yet wired, the classifier fires from the periodic sweep cycle when it detects a PR in `open` state that has no `lobster-reviewed` label.

### Concrete integration sequence

```
GitHub PR opened
  │
  ├─► Webhook or sweep cycle detects new PR
  │
  ├─► PRClassifier.classify(pr_metadata) → ClassificationResult
  │     - pr_metadata: title, body, files_changed, lines_added, lines_deleted,
  │                    labels, author, base_branch
  │     - ClassificationResult: { path: "oracle-only"|"oracle+deep"|"dan-required",
  │                               escalation_triggers: list[str],
  │                               escalation_score: int,
  │                               file_categories: list[str] }
  │
  ├─► Route based on ClassificationResult.path:
  │     oracle-only   → spawn OracleAgent(pr_url)
  │     oracle+deep   → spawn OracleAgent(pr_url) + DeepReviewAgent(pr_url)
  │     dan-required  → spawn OracleAgent + DeepReviewAgent + notify Dan
  │
  ├─► Label PR: add "lobster-reviewed" + path label ("oracle-reviewed", "deep-reviewed", "needs-dan-review")
  │
  └─► Write classification report as PR comment (within 60 seconds of PR open)
```

### Module path

```
~/lobster/agents/pr_classifier.py
```

Public API:
- `classify(pr_metadata: dict) -> ClassificationResult`
- `ClassificationResult` is a TypedDict with fields: `path`, `escalation_triggers`, `escalation_score`, `file_categories`, `author_type`, `size_bucket`

### Failure behavior

If the classifier itself fails (GitHub API unavailable, malformed PR metadata), default to `dan-required` and include the failure reason in the notification. The classifier must not silently drop a PR.

---

## 6. Classification Report Format

Every PR receives a classification comment within 60 seconds:

```
**Lobster PR Review Classification**

Path: oracle-only
Size: small (47 lines)
Categories: docs
Author: subagent

No escalation triggers present.

Oracle review running — results will appear in a follow-up comment.
```

For escalated PRs:

```
**Lobster PR Review Classification**

Path: dan-required
Size: large (612 lines)
Categories: orchestration, schema, config
Author: dcetlin

Escalation triggers:
- >500 lines changed
- schema change detected (no migration file found)

Oracle + deep reviews running. @dcetlin review requested.
```

---

## 7. Open Questions (not blocking Phase 2)

- **Webhook vs. poll:** Webhook integration is cleaner but requires a registered GitHub webhook. The sweep-cycle polling fallback is the Phase 2 MVP. Webhook wiring is Phase 3.
- **Known bot list:** The classifier needs a list of known bot accounts (subagents) to distinguish from external contributors. For Phase 2, the heuristic is: PR body contains "Generated with Claude Code" or "Co-Authored-By: Claude".
- **Oracle severity levels:** The `CRITICAL` escalation trigger requires Oracle to output a structured severity field. Oracle's output schema should include `severity: "low"|"medium"|"high"|"critical"`. If Oracle does not yet produce this field, skip the oracle-critical escalation trigger in Phase 2 MVP and document the gap.
- **Merge blocking:** Should `needs-dan-review` PRs be blocked from merging until Dan approves? Deferred — branch protection rules are a GitHub configuration decision, not a classifier decision.
