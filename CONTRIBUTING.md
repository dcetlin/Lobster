# Contributing to Lobster

## Engineering Principles

Before opening a PR, read [`docs/engineering-principles.md`](docs/engineering-principles.md).
It is a four-question checklist that every PR is evaluated against:

1. Does each module have one contract?
2. Are isolation boundaries enforced at the DB/view layer?
3. Is audit-before-transition observed for every state change?
4. Does the audit_log tell the full story on its own?

These are structural constraints, not style preferences. PRs that violate them
will be blocked at review.

## Development Setup

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for environment setup and
local testing instructions.

## PR Sequence (WOS Phase 2)

PRs in the WOS Phase 2 sequence (#302–#307) must be opened and merged in order.
The integration test harness (issue #318) must pass for each PR before merge.
