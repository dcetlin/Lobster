# Posture 2 Idempotency Refinements

**Date:** 2026-05-24
**Status:** Design — pre-implementation
**Workstream:** wos-posture2-design
**References:** multiposture-spec.html §3, PR #1258 (fix-p1-sidecar-liveness)

---

## 1. The Idempotency Problem in Posture 2

Posture 1 (batch dispatch) has a natural idempotency story: executor-heartbeat is a cron process that reads the registry on every tick. The registry is the source of truth. If a UoW is already in `executing` status with a valid `claimed_until`, the executor skips it. The claimed_until window is set atomically at dispatch, and TTL recovery is handled by `reset_expired_claims()`.

Posture 2 (dispatcher-direct) is different. The dispatcher is an interactive session, not a polling loop. It receives a `/dispatch <uow_id>` command, builds a prompt, and calls the Agent tool — all within a single session turn. This creates three idempotency risks that Posture 1 does not have:

1. **Double-dispatch from the same turn** — the dispatcher could spawn two Agent calls for the same UoW if the handler is called twice before the status transition commits.
2. **Dispatcher restart mid-spawn** — the dispatcher session could crash after spawning the Agent but before registering the session, leaving a live agent with no registry entry and a UoW that looks unclaimed.
3. **Posture 1 racing with Posture 2** — executor-heartbeat could pick up the same UoW between when `handle_wos_dispatch` validates eligibility and when the dispatcher sets `claimed_until`.

The mechanisms described below address all three.

---

## 2. The Claim Window: What Posture 2 Needs That Posture 1 Doesn't

### 2.1 Atomic claim before Agent spawn

The dispatcher must set `claimed_until` and transition the UoW to `executing` status **before** calling the Agent tool — not after. This is the critical sequencing difference from Posture 1.

In Posture 1, the wos_execute inbox message is the dispatch signal, and executor-heartbeat updates the UoW status as part of processing that message. The claim and the spawn are effectively coupled through the inbox message routing.

In Posture 2, the Agent tool call is direct. There is no inbox message to anchor the transaction. The only safe ordering is:

```
1. Validate UoW eligibility (status, execution_enabled, quota gates)
2. Write claimed_until = now + TTL to registry (atomic UPDATE with WHERE status IN eligible_statuses)
3. Confirm rowcount = 1 (if 0, another actor claimed it — abort)
4. Spawn Agent via Agent tool
5. Register session via session_start
```

If step 3 returns rowcount 0, a concurrent actor (another `/dispatch` call or executor-heartbeat) claimed the UoW first. The dispatcher responds to Dan with a conflict message and does not spawn an agent.

If the dispatcher crashes between steps 4 and 5, the agent is live but unregistered. The agent's own startup heartbeat write (see section 4) is the signal that it is running. The TTL recovery path handles eventual cleanup if the agent also dies.

### 2.2 Posture 2 TTL is shorter than Posture 1

Posture 1 sets `claimed_until = dispatch_ts + max(2 × estimated_runtime, 3600)`. This long window exists because the observation loop (as of this writing) cannot reliably detect dead agents via heartbeat gap alone — sidecar masking was the structural problem fixed in PR #1258.

Posture 2 can use a shorter TTL because:

- The dispatcher holds the spawn context directly. When `claimed_until` expires on a Posture 2 UoW, the dispatcher can re-dispatch immediately without waiting for the next steward cycle.
- Agent-originated heartbeats (PR #1238) are the primary liveness signal for Posture 2 UoWs. An agent that fails to write a startup heartbeat within 5 minutes is dead.
- The dispatcher session is live when a Posture 2 agent is running (it just spawned it). The dispatcher receives the `write_result` notification. If the notification never arrives and `claimed_until` expires, the dispatcher knows the agent died.

**Recommended Posture 2 TTL:** 30 minutes, or `estimated_cycles × 15 minutes`, whichever is larger. This is substantially shorter than Posture 1's multi-hour window and reflects the fact that the dispatcher is the TTL observer, not a polling daemon checking once every 3 minutes.

### 2.3 What Posture 2 adds to the claimed_until model: dispatcher-held spawn context

In Posture 1, TTL expiry routes the UoW back to `ready-for-steward`, and the steward re-queues it for executor-heartbeat on the next batch cycle. The executor that re-dispatches it may be a different executor-heartbeat invocation, and it reconstructs the dispatch from the registry alone.

In Posture 2, the dispatcher that set `claimed_until` holds the spawn context — the UoW ID, the chat_id, the admin session. When TTL expires, the dispatcher can re-dispatch directly (or notify Dan that the agent died and ask whether to retry) without going through the steward's re-queue cycle.

This is the structural advantage of Posture 2's idempotency model: the TTL observer is also the re-dispatch actor. The round-trip through steward-heartbeat is not required.

**Implementation implication:** The dispatcher should log the Posture 2 spawn with enough context to re-dispatch on TTL expiry. At minimum: `{uow_id, claimed_until, task_id, chat_id, spawned_at}`. This log entry is what enables the dispatcher to offer "re-dispatch?" to Dan when the agent does not complete within the TTL window.

---

## 3. Sidecar Liveness Lesson Applied to Posture 2

### 3.1 What the sidecar bug was

PR #1258 fixed two interacting bugs in Posture 1:

1. `heartbeat_sidecar.py:_collect_in_flight_uows()` wrote fresh `heartbeat_at` values for **all** UoWs in `executing` status — including UoWs whose `claimed_until` had already expired. A UoW with an expired claim has a dead agent by definition. The unconditional heartbeat refresh masked this: `now - heartbeat_at > heartbeat_ttl + buffer` could never fire, so the steward's Phase 2b stall detection was permanently disabled for these UoWs.

2. `registry.py:reset_expired_claims()` transitioned expired UoWs to `ready-for-executor` instead of `ready-for-steward`. The executor re-dispatched them immediately, the steward never applied orphan retry budgets, and the cycle repeated indefinitely.

The fix: sidecar skips UoWs with expired `claimed_until`; expired claims route to `ready-for-steward` so the steward can apply orphan budgets before re-dispatch.

### 3.2 The lesson for Posture 2: no sidecar involvement

Posture 2 dispatches via the Agent tool directly from the dispatcher session. The sidecar runs in executor-heartbeat (a separate cron process). The sidecar should not write heartbeats for Posture 2 UoWs — for the same reason it should not write heartbeats for Posture 1 UoWs with expired claims.

**Rule:** The sidecar must not write `heartbeat_at` for any UoW that:
- Has a non-null `claimed_until` that is in the future (the agent may be live — heartbeat is the agent's job), **and**
- Has a `posture: direct` marker in its audit log or dispatch note (identifying it as a Posture 2 UoW)

The first condition is already implemented by PR #1258 (sidecar filters UoWs with expired `claimed_until`). The second condition — excluding active Posture 2 UoWs from sidecar writes entirely — is a new requirement.

The structural reason: Posture 2 agents are spawned with the expectation that they will write their own startup heartbeat within 5 minutes. The dispatcher has a direct liveness channel (the Agent tool registration and the session registry). If the dispatcher sees a Posture 2 UoW whose agent has not written a heartbeat 5 minutes after spawn, the dispatcher can detect this without waiting for the sidecar's next tick. The sidecar adding a write would only mask this detection.

**Implementation:** When the dispatcher spawns a Posture 2 agent, it writes a dispatch audit entry with `posture: direct`. The sidecar's `_collect_in_flight_uows` filters UoWs with this marker in their dispatch note, in addition to the existing `claimed_until` expiry filter.

### 3.3 Agent-originated liveness is the only valid signal for Posture 2

Posture 2 agents must write a startup heartbeat as their first action, before any other work:

```python
mcp__lobster-inbox__write_wos_heartbeat(
    uow_id='<uow_id>',
    token_usage=0  # startup write — no tokens consumed yet
)
```

The dispatcher can check `uow_heartbeat_log` for a post-dispatch agent-originated row after 5 minutes. If none exists, the agent never started. The dispatcher notifies Dan and offers to retry or kill the UoW.

This mirrors the observation loop improvement planned in multiposture-spec.html §2.3 (Change 1b), applied to the Posture 2 lane specifically.

---

## 4. write_result Failure Risk and Accepted Double-Execution Risk

### 4.1 The risk

`write_result` is called by the spawned agent at the end of execution. It notifies the dispatcher (via `subagent_notification`) and triggers the dispatcher to send Dan a completion message. If `write_result` fails (network error, MCP session expiry, agent crash after writing the result file but before calling `write_result`), the dispatcher never receives the notification.

The UoW result file at `output_ref` may be complete. The steward will read it on the next cycle and transition the UoW to done. But the dispatcher still has `claimed_until` set and may re-dispatch when the TTL expires — before the steward has processed the result.

This is the **accepted double-execution risk**: a window exists between when the agent writes the result file and when either (a) `write_result` delivers the notification, or (b) the steward reads the result file. A TTL expiry in this window would cause a re-dispatch of a UoW that is actually complete.

### 4.2 Mitigations

**Mitigation 1: commit-then-report pattern**

The agent writes the result file to `output_ref` before calling `write_result`. If `write_result` fails, the steward reads the result file on the next cycle and transitions the UoW to done. The steward's done transition is the authoritative completion signal.

The dispatcher should check UoW status before re-dispatching on TTL expiry:

```python
uow = registry.get(uow_id)
if uow.status in ('done', 'failed'):
    # Result was committed — skip re-dispatch, clear TTL
    return
```

This check is sufficient to prevent double-execution in the common case.

**Mitigation 2: idempotency at the task level**

Posture 2 UoWs should be designed to be re-runnable when possible. For UoWs that produce a GitHub PR, re-running produces a conflict on the branch rather than a duplicate PR. The agent should check for an existing PR on its branch before creating a new one.

For UoWs that write files or make database changes, the commit-then-report pattern (mitigation 1) is sufficient: the second dispatch sees the result file already written and exits early.

**Mitigation 3: short TTL reduces the double-execution window**

The 30-minute TTL recommended in section 2.2 minimizes the window during which a re-dispatch could occur. A UoW that writes its result file in minute 28 and fails to call `write_result` has a 2-minute exposure window before TTL expiry triggers a re-dispatch check. The steward's next cycle (within 3 minutes) will see the result file and close the UoW before the re-dispatch check fires.

In practice, the double-execution window is narrow: `TTL - agent_execution_time`. A UoW that takes 25 minutes to run has a 5-minute window. A UoW that takes 29 minutes has a 1-minute window. For UoWs that routinely use their full TTL budget, the dispatcher should extend `claimed_until` via a heartbeat extension mechanism rather than accepting the narrow window.

### 4.3 What is explicitly not mitigated

Distributed transactions between `write_result`, the dispatcher notification, and the registry status update are not attempted. The system accepts at-most-once delivery for the dispatcher notification and relies on the steward's result-file read as the idempotency backstop.

If `write_result` delivers the notification but the dispatcher crashes before sending Dan's completion message, Dan does not see the completion message. The dispatcher will see the UoW as done on the next startup (it reads active sessions and cross-checks with the registry). Dan will see the outcome on the next `/wos status` check.

---

## 5. Summary: Posture 2 Idempotency Contract

| Mechanism | How it works | Where it's different from Posture 1 |
|---|---|---|
| `claimed_until` set atomically before Agent spawn | Status UPDATE with WHERE guard; rowcount=0 means conflict | Set by dispatcher directly, not via inbox message routing |
| Sidecar exclusion | Sidecar skips Posture 2 UoWs (posture:direct marker in audit) | Posture 1 UoWs are written by sidecar as backup; Posture 2 has no sidecar involvement |
| Agent-originated startup heartbeat | Agent writes to uow_heartbeat_log within 5 min of spawn | Same contract as Posture 1 (PR #1238), but dispatcher checks directly rather than waiting for steward observation loop |
| TTL expiry → dispatcher re-dispatch | Dispatcher holds spawn context; can offer Dan a retry without going through steward re-queue | Posture 1 routes expired claims through steward; Posture 2 dispatcher holds the re-dispatch decision |
| commit-then-report | Result file written before write_result; steward reads it as backstop | Same as Posture 1; mitigates write_result failure |
| Status check before TTL re-dispatch | Dispatcher checks `uow.status` before re-dispatching on TTL expiry | Posture 1 executor checks status as part of the dispatch gate; Posture 2 dispatcher must add this check explicitly |

---

## 6. Open Questions

These are unresolved before Posture 2 implementation begins:

1. **Posture marker in audit log.** What field in the dispatch audit entry records `posture: direct`? The multiposture-spec proposes a `posture` YAML field in the agent prompt frontmatter; this needs to be written to the registry (dispatch_note or a new column) so the sidecar can filter on it. Decision: use `dispatch_note` JSON field `"posture": "direct"`.

2. **Dispatcher TTL observer implementation.** Where in the dispatcher loop does the Posture 2 TTL check run? Options: (a) after every `wait_for_messages` timeout; (b) as part of the subagent_notification handler; (c) as a separate scheduled check. The dispatcher cannot block on a timer, so option (c) requires a scheduled wos_execute-style message. Option (a) is simpler: after each `wait_for_messages` return, check for any Posture 2 UoWs whose `claimed_until` is past.

3. **Concurrent Posture 2 sessions.** Can the dispatcher spawn multiple Posture 2 agents simultaneously? The context-pressure threshold (20 active sessions) governs this in aggregate, but Posture 2 doesn't currently have its own concurrent-session cap. A cap of 3–5 simultaneous Posture 2 sessions seems reasonable; beyond that, Dan should be using Posture 1.

4. **Posture 2 re-dispatch authorization.** When the dispatcher detects a TTL expiry on a Posture 2 UoW, should it re-dispatch automatically or ask Dan? The multiposture-spec treats gates as warnings for Posture 2 (not hard gates). The conservative choice is to notify Dan and wait for confirmation before re-dispatching a potentially-expensive UoW.
