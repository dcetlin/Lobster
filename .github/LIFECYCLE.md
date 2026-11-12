# Lobster Issue Lifecycle

Issues in this repo follow a sweep-based lifecycle. The system prevents single-perspective
lock-in: the posture that advances an issue cannot also audit its own advancement.

## States

| Label | Meaning |
|-------|---------|
| `design-seed` | Surfaced from philosophy explorations; raw seed not yet shaped as a design question |
| `needs-design` | Design question formulated; not ready for implementation |
| `needs-agent-posture` | Requires a specific posture (named in issue body) before advancing |
| `ready-to-execute` | Design fully specified; can enter implementation |
| `auditing` | Implementation complete or in progress; under adversarial or meta audit |
| (closed) | Resolved, deferred, or out of scope |

## Action Types

Each issue carries exactly one `action:*` label describing what kind of work advances it now.

| Label | What it means | Typical posture |
|-------|--------------|-----------------|
| `action:iterate-design` | Produce the next design iteration | oracle |
| `action:challenge-design` | Adversarial challenge of an existing design | lobster-oracle adversarial |
| `action:implement` | Build it | functional-engineer |
| `action:experiment` | Time-bounded experiment with named before/after conditions | functional-engineer + lobster-oracle |
| `action:design-conversation` | Requires dialogue with Dan before advancing | dispatcher (not a subagent) |
| `blocked-on-dan` | Waiting for Dan's input or action | — |

Action type is mutable — it changes as an issue advances through states.

## State Transitions

```
design-seed
    │
    ▼ (oracle reads seed, formulates design question)
needs-design  ──────────────────────────────────────┐
    │                                                │
    │ (design reaches limit of current posture)      │ (design fully specified,
    ▼                                                │  resolution condition named)
needs-agent-posture                                  │
    │                                                │
    │ (required posture applied)                     │
    └──────────────────────► ready-to-execute ◄──────┘
                                    │
                                    ▼ (implementation begins/completes)
                                auditing
                                    │
                     ┌──────────────┴──────────────┐
                     │                             │
                     ▼ (audit passes)              ▼ (new design req surfaced)
                   closed                      needs-design
```

## Ergonomic Preconditions

An issue **cannot** move to `ready-to-execute` unless its body contains:

1. **Source traceability** — link to the philosophy-explore file/voice note where the seed was surfaced
2. **Posture specification** — if `needs-agent-posture`, the required posture is explicitly named
3. **Resolution condition** — what would have to be true for this issue to be closed

An issue **cannot** move from `auditing` to `closed` if the same posture that produced the
implementation is the only posture that has audited it.

## Dependency Metadata

Each issue body may contain these structured fields:

```
depends-on: [#X, #Y]      # hard block — must be complete before this advances
benefits-from: [#X, #Y]   # soft signal — completion raises quality ceiling here
enables: [#X, #Y]          # issues that become more executable when this completes
```

## Batch Readout

Run `scripts/issue-batch.sh` to see the current queue at a glance:

```
./scripts/issue-batch.sh
./scripts/issue-batch.sh --repo dcetlin/Lobster
./scripts/issue-batch.sh --json   # machine-readable output
```

Output format:
```
Executable now (ready-to-execute, unblocked)     N issues
In design iteration (can run now, no blocker)    N issues
Needs design conversation with Dan               N issues
Needs agent posture (specified in issue)         N issues
Blocked by dependency                            N issues
Blocked on Dan                                   N issues
Under audit                                      N issues
Design seed (not yet shaped)                     N issues
High upstream leverage (enables >= 3):  [list]
```

The **high upstream leverage** list is the key planning signal. Issues that `enables` three or more
downstream issues should be scheduled before leaf-node issues, independent of surface-level priority.

## Creating a New Design Seed

Use the "Design seed" issue template. Required fields before the issue can advance:
- Source traceability (philosophy-explore file, voice note, or session date)
- Resolution condition (what would make this closeable)

Optional but recommended at creation time: `depends-on`, `benefits-from`, `enables` metadata.

## Execution Ordering Heuristic

When multiple issues are executable, prefer highest `enables` count first.
Tie-break: Dan's explicit priority signal, then estimated impact on the absorption-ceiling /
transparency arc, then FIFO.

This is advisory, not algorithmic. The batch readout surfaces upstream-leverage candidates
so the ordering decision is cheap.
