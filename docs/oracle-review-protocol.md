# Oracle Review Protocol

This document defines the oracle review process and the YAML frontmatter schema used to make document review status machine-readable.

## What is Oracle Review?

Oracle review is a two-stage adversarial review process applied to significant documents before they are delivered or committed. The oracle agent (`lobster-oracle`) evaluates:

- **Stage 1 (Vision alignment):** Does this document serve the right problem, given the vision?
- **Stage 2 (Quality):** Does it do what it claims? What failure modes exist?

Verdicts are recorded in `~/lobster/oracle/decisions.md`. The oracle gate in `CLAUDE.md` requires every code PR to pass oracle review before merge.

## YAML Frontmatter Schema

All oracle-reviewed documents must include YAML frontmatter indicating their review status. This makes status machine-readable and enforceable at delivery time — rather than relying on convention alone.

```yaml
---
oracle_status: approved | pending | not_required
oracle_pr: <PR URL or null>
oracle_date: <ISO date or null>
---
```

### Field definitions

**`oracle_status`** — required. One of three values:

| Value | Meaning |
|---|---|
| `approved` | Oracle reviewed this document and issued an APPROVED verdict. The `oracle_date` field must be set. |
| `pending` | Oracle review has been requested or is in progress. Document must not be delivered until status changes to `approved`. |
| `not_required` | Document is not subject to oracle review (e.g., internal reference docs, stub files, meeting notes). The author is responsible for this judgment. |

**`oracle_pr`** — the PR URL associated with the oracle review, or `null` if the review was not tied to a specific PR (e.g., a standalone design doc reviewed directly).

**`oracle_date`** — the ISO 8601 date (`YYYY-MM-DD`) when the oracle issued its verdict, or `null` if oracle_status is `pending` or `not_required`.

### Examples

A document that has been oracle-approved as part of a PR review:

```yaml
---
oracle_status: approved
oracle_pr: https://github.com/dcetlin/Lobster/pull/42
oracle_date: 2026-04-01
---
```

A document pending review:

```yaml
---
oracle_status: pending
oracle_pr: null
oracle_date: null
---
```

A document that does not require review (e.g., a raw reference dump):

```yaml
---
oracle_status: not_required
oracle_pr: null
oracle_date: null
---
```

## When to Apply Frontmatter

Apply oracle frontmatter to:
- Design documents (sprint design docs, architecture proposals, retros)
- Docs delivered to the user as substantial outputs (>500 words or multi-source synthesis)
- Any document the oracle has explicitly reviewed

Do NOT apply to:
- Bootup files and configuration files (these are governed by git review, not oracle review)
- Short reference stubs or index files
- Internal scaffolding files

## Integration with Delivery

See `sys.subagent.bootup.md` for the pre-delivery check: before calling `send_reply` to deliver a substantial document, verify that `oracle_status: approved` is present in its frontmatter. If not, use `write_task_output` with `status=pending` and stop.

## Integration with Oracle Agent

See `.claude/agents/lobster-oracle.md` for the oracle output protocol. After issuing an APPROVED verdict and writing to `~/lobster/oracle/decisions.md`, the oracle agent must also add `oracle_status: approved` frontmatter to the reviewed document (adding or updating the field).
