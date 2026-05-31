# Steward–Dan Consultation Reply Routing Design

**UoW:** uow_20260525_588984  
**Date:** 2026-05-25  
**Status:** DRAFT — pending Dan's review of Open Questions

## Background

The WOS v2 design (approved via the oracle verdict for PR #564, 2026-04-01) requires
the Steward to send Dan a direct Telegram message when it encounters a human dependency,
with Dan's reply re-triggering a Steward cycle. Today no such path exists:

- `wos_escalate` / `wos_surface` surface UoWs to Dan but do not pause the steward cycle
  counter or wait for a reply before re-queuing.
- `waiting_for_signal` exists only as a **trace posture vocabulary** entry
  (`_determine_trace_posture()` maps `executor_outcome == "blocked"` to it) — it is
  not a real UoW state in `uow_registry`.
- There is no DB field for the Telegram message ID of a consultation message, no
  timeout logic for unanswered consultations, and no dispatcher path that inspects
  `reply_to_message_id` against a stored consultation ID.

This document specifies the complete design for those three missing pieces.

---

## Section 1 — Steward Outgoing Message Schema

### 1.1 When the Steward enters `waiting_for_signal`

A UoW enters the consultation flow when the Steward determines it cannot prescribe
without human input. The current trigger is `executor_outcome == "blocked"` —
the Executor's result file carries `outcome: "owner_decision_required"` (or the
equivalent string). The Steward must:

1. Transition the UoW to the new `waiting_for_signal` registry state (Section 3).
2. Write a `wos_consultation` inbox message (new type, defined below).
3. Record `consultation_message_id` and `waiting_since` on the UoW record.
4. **Not** increment `steward_cycles` — the cap-timer freeze is also in Section 3.

### 1.2 Telegram message text format

The consultation message sent to Dan must embed the UoW ID in a machine-readable
footer that survives a plain-text inline reply (i.e., if Dan quotes the message and
types a reply, the footer is present in the parent message).

```
WOS consultation: `{uow_id}`

**{uow_title}**
Cycle: {steward_cycles}

The Executor returned with a decision needed:

{executor_blocked_reason}

Please reply with your guidance. When you reply to this message, the Steward
will resume from where it left off.

[ref:{uow_id}]
```

Rules for the footer line:
- Format: `[ref:{uow_id}]` — square-bracket syntax, colon separator, no spaces.
- Must appear on its own line at the end of the message.
- The dispatcher uses this as a fallback when `reply_to_message_id` lookup fails
  (e.g., Dan types a new message and pastes the UoW ID by hand).

### 1.3 Inline button

One inline button is provided as a convenience shortcut:

| Label | `callback_data` |
|-------|-----------------|
| "Acknowledged — I'll reply inline" | `wos_consult_ack:{uow_id}` |

The button press is **not required** for re-trigger — it is an acknowledgment that
Dan has seen the message. Pressing it transitions the UoW to a sub-state
`consultation_acknowledged` (stored in `consultation_state` field, Section 3.4),
but does not re-trigger the Steward cycle.

The actual re-trigger happens on Dan's text reply (Section 2).

### 1.4 Inbox message type

The Steward writes a new inbox message type `wos_consultation` to the inbox queue:

```python
# Pseudocode — Steward writes this to trigger the dispatcher send
{
    "type": "wos_consultation",
    "uow_id": uow_id,
    "uow_title": uow.summary,
    "steward_cycles": uow.steward_cycles,
    "executor_blocked_reason": result.reason,  # from Executor's write_result call
    "chat_id": LOBSTER_ADMIN_CHAT_ID,
}
```

The dispatcher handles `wos_consultation` via a new `handle_wos_consultation`
function in `dispatcher_handlers.py` (see Section 2.1). That function:

1. Calls `send_reply(chat_id, text, buttons=[...])` and captures the returned
   Telegram message ID.
2. Writes `consultation_message_id` and `waiting_since` to the UoW record.
3. Transitions the UoW status to `waiting_for_signal` (Section 3.1).

```python
# Pseudocode — dispatcher handle_wos_consultation
def handle_wos_consultation(msg: dict) -> dict:
    uow_id = msg["uow_id"]
    text = _build_consultation_text(msg)
    buttons = [["Acknowledged — I'll reply inline",
                f"wos_consult_ack:{uow_id}"]]

    telegram_msg_id = send_reply(
        chat_id=msg["chat_id"],
        text=text,
        buttons=buttons,
    )

    # Store Telegram message ID and freeze the UoW
    registry.set_consultation_state(
        uow_id=uow_id,
        consultation_message_id=telegram_msg_id,
        waiting_since=utc_now_iso(),
    )
    registry.transition(uow_id, to="waiting_for_signal",
                        audit_note="consultation_message_sent")

    return {"action": "done", "telegram_msg_id": telegram_msg_id}
```

### 1.5 Correlation fields stored on the UoW record

After the consultation message is sent, the following fields are written to the
UoW record (new schema fields — Section 3.4):

| Field | Type | Value |
|-------|------|-------|
| `consultation_message_id` | `INTEGER NULL` | Telegram message ID of the sent consultation message |
| `waiting_since` | `TEXT NULL` (ISO-8601) | Timestamp when UoW entered `waiting_for_signal` |
| `signal_timeout_hours` | `INTEGER NOT NULL DEFAULT 24` | Hours before the UoW is escalated if no reply arrives |
| `consultation_state` | `TEXT NULL` | `"sent"`, `"acknowledged"`, or `"replied"` |

---

## Section 2 — Dispatcher Reply Detection and Re-trigger Logic

### 2.1 Detection path A — button callback

When Dan presses the "Acknowledged" button, the dispatcher receives a message with
`type="callback"` and `callback_data="wos_consult_ack:{uow_id}"`. The existing
`route_callback_message` function in `dispatcher_handlers.py` is extended to handle
this prefix:

```python
# Pseudocode — extension of route_callback_message
if data.startswith("wos_consult_ack:"):
    uow_id = data[len("wos_consult_ack:"):]
    registry.set_consultation_state(uow_id, consultation_state="acknowledged")
    return {
        "action": "send_reply",
        "text": f"Got it — I'll wait for your reply on UoW `{uow_id}`.",
        "chat_id": chat_id,
        "handled": True,
    }
```

This is an acknowledgment, not a re-trigger. The Steward cycle resumes only when
Dan sends a text reply (Path B below).

### 2.2 Detection path B — text reply (primary path)

When Dan replies to the consultation message inline, the dispatcher receives a
message with `type="message"` (or `"text"`) where `reply_to_message_id` is the
Telegram message ID of the original consultation message.

**Lookup mechanism:**

The dispatcher performs this lookup on every incoming text message:

```python
# Pseudocode — dispatcher main loop, applied before normal routing
def check_for_consultation_reply(msg: dict, registry) -> str | None:
    """
    Return the UoW ID if this message is a reply to an open consultation,
    else return None.
    """
    reply_to_id = msg.get("reply_to_message_id")
    if not reply_to_id:
        return None

    # Query the uow_registry for any UoW in waiting_for_signal with
    # this consultation_message_id
    uow = registry.find_uow_by_consultation_message_id(reply_to_id)
    if uow and uow.status == "waiting_for_signal":
        return uow.id
    return None
```

The DB query backing `find_uow_by_consultation_message_id`:

```sql
SELECT id FROM uow_registry
WHERE consultation_message_id = ?
  AND status = 'waiting_for_signal'
LIMIT 1
```

This requires an index on `consultation_message_id` (Section 3.4).

### 2.3 Fallback path B' — UoW ID embedded in reply text

If Dan sends a message not as a Telegram reply-thread but containing the footer
pattern `[ref:{uow_id}]` (e.g., he types a new message and pastes the footer), the
dispatcher detects this via regex and routes it the same way as Path B.

```python
import re
_REF_PATTERN = re.compile(r'\[ref:(uow_[A-Za-z0-9_]+)\]')

def extract_uow_ref_from_text(text: str) -> str | None:
    m = _REF_PATTERN.search(text)
    return m.group(1) if m else None
```

This is applied only when the `reply_to_message_id` lookup (Path B) returns no match.

### 2.4 Re-trigger mechanism

Once a consultation reply is identified (Path B or B'), the dispatcher:

1. Extracts Dan's reply text.
2. Calls `registry.record_consultation_reply(uow_id, reply_text, telegram_msg_id)`.
3. Transitions the UoW status from `waiting_for_signal` → `ready-for-steward`.
4. Writes a `wos_execute` inbox message to trigger the Steward's next cycle.

```python
# Pseudocode — dispatcher consultation reply handler
def handle_consultation_reply(uow_id: str, dan_reply_text: str,
                               dan_msg_id: int, registry) -> None:
    registry.record_consultation_reply(
        uow_id=uow_id,
        reply_text=dan_reply_text,
        telegram_msg_id=dan_msg_id,
        replied_at=utc_now_iso(),
    )
    registry.transition(
        uow_id=uow_id,
        to="ready-for-steward",
        audit_note=f"consultation_reply_received: {dan_reply_text[:120]!r}",
    )
    write_wos_execute_message(uow_id)
    send_reply(
        chat_id=ADMIN_CHAT_ID,
        text=f"Got it — resuming UoW `{uow_id}` with your guidance.",
    )
```

### 2.5 Data passed to the Steward on re-trigger

The `wos_execute` message is the standard re-trigger path. No new fields are needed
in the message itself: the Steward reads the UoW record directly and finds the
consultation reply in the new `consultation_reply_text` field (Section 3.4).

The Steward's prescription prompt should include the consultation reply text, quoted
verbatim, so the Executor receives it as part of the task context:

```python
# Pseudocode — Steward prescription augmentation
def _augment_prescription_with_consultation_reply(uow: UoW, prescription: str) -> str:
    if uow.consultation_reply_text:
        return (
            f"**Human guidance received:** {uow.consultation_reply_text}\n\n"
            + prescription
        )
    return prescription
```

### 2.6 Edge cases

| Case | Handling |
|------|----------|
| Reply arrives after UoW cancelled or `done` | `find_uow_by_consultation_message_id` returns no match (status check). Dispatcher sends: "UoW `{id}` is no longer active — no action taken." |
| UoW moved to `ready-for-steward` before reply (e.g., timeout) | Status check fails; same "no longer active" response. |
| Duplicate replies (Dan replies twice) | First reply transitions status to `ready-for-steward`. Second reply: status is no longer `waiting_for_signal`, so the lookup returns no match. Dispatcher sends: "UoW `{id}` already has your guidance — Steward will process it on the next heartbeat." |
| Reply from a non-Dan user | The consultation message is sent only to `LOBSTER_ADMIN_CHAT_ID`. Telegram DMs to the bot from other users won't have `reply_to_message_id` matching a consultation message ID (different chat). Edge case to consider: if a bot is in a group and another user replies to the forwarded message — guard by checking `chat_id == LOBSTER_ADMIN_CHAT_ID`. |
| `send_reply` fails to return a message ID | If the Telegram API does not return a message ID (API error), log the failure and leave the UoW in `diagnosing` state. The Steward will be re-queued via startup sweep on the next heartbeat and will retry the consultation send. `consultation_message_id` is written only after confirmed delivery. |

---

## Section 3 — `waiting_for_signal` State and Cap-Timer Freeze

### 3.1 State definition

`waiting_for_signal` is a new real UoW registry state (not just a trace posture label).

| Property | Value |
|----------|-------|
| **Meaning** | The Steward has sent Dan a consultation message and is waiting for a human reply before it can prescribe. No executor work can proceed while in this state. |
| **Valid predecessor states** | `diagnosing` (Steward writes `wos_consultation` during a diagnosis pass) |
| **Valid successor states** | `ready-for-steward` (on Dan's reply), `blocked` (on timeout with no reply after `signal_timeout_hours`) |
| **Terminal?** | No |
| **Visible to Executor?** | No — the Executor never claims a UoW in this state |

State machine addition (insert into the existing table in `wos-v2-design.md`):

| From | To | Actor | Trigger |
|------|----|-------|---------|
| `diagnosing` | `waiting_for_signal` | Steward | Executor returned `outcome=owner_decision_required`; consultation message sent |
| `waiting_for_signal` | `ready-for-steward` | Dispatcher | Dan's reply received |
| `waiting_for_signal` | `blocked` | `steward-heartbeat.py` | `waiting_since + signal_timeout_hours` exceeded with no reply |

### 3.2 Cap-timer freeze

`steward_cycles` must **not** increment while a UoW is in `waiting_for_signal`.

The freeze is enforced at **two gates**:

**Gate 1 — Steward heartbeat skip:**  
`steward-heartbeat.py` must skip UoWs in `waiting_for_signal` status entirely during
Phase 3 (the main steward loop). The observation loop (Phase 2) must also skip
`waiting_for_signal` UoWs — they are not stalled; they are intentionally paused.

```python
# Pseudocode — steward-heartbeat.py Phase 3 eligibility gate
def is_eligible_for_steward_cycle(uow: UoW) -> bool:
    return uow.status == "ready-for-steward"
    # waiting_for_signal UoWs are NOT eligible — return False implicitly
```

**Gate 2 — Dispatcher re-trigger path:**  
When the dispatcher calls `registry.transition(uow_id, to="ready-for-steward")`
on receipt of Dan's reply, it must NOT increment `steward_cycles`. The
`steward_cycles` increment happens only inside the Steward's own
`_process_uow()` function when it successfully completes a diagnosis-to-prescription
pass. The re-trigger transition is a state-only change; `steward_cycles` stays at
its pre-consultation value until the Steward's next full cycle.

### 3.3 Signal timeout

If Dan does not reply within `signal_timeout_hours` (default: 24 hours), the Steward
heartbeat's timeout check transitions the UoW to `blocked`:

```python
# Pseudocode — steward-heartbeat.py consultation timeout check
# Runs in the observation loop phase, after the standard timeout check

def check_consultation_timeouts(registry, dry_run: bool = False) -> int:
    waiting_uows = registry.list(status="waiting_for_signal")
    now = utc_now()
    timed_out = 0
    for uow in waiting_uows:
        if not uow.waiting_since:
            continue
        elapsed_hours = (now - parse_iso(uow.waiting_since)).total_seconds() / 3600
        timeout_hours = uow.signal_timeout_hours or 24
        if elapsed_hours >= timeout_hours:
            if not dry_run:
                registry.transition(
                    uow_id=uow.id,
                    to="blocked",
                    audit_note=f"consultation_timeout: no reply after {elapsed_hours:.1f}h",
                )
                send_reply(
                    chat_id=LOBSTER_ADMIN_CHAT_ID,
                    text=(
                        f"WOS: UoW `{uow.id}` consultation timed out after "
                        f"{elapsed_hours:.0f}h with no reply. "
                        f"UoW is now `blocked`. Use `/decide {uow.id} retry` to resume."
                    ),
                )
                timed_out += 1
    return timed_out
```

The `signal_timeout_hours` field is configurable per UoW (written at consultation
time). The global default is 24 hours. No separate "Dan gets a reminder" mechanism
is specified here, but it is a natural extension — see Open Questions §4.

### 3.4 Schema changes

New fields on `uow_registry`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `consultation_message_id` | `INTEGER NULL` | `NULL` | Telegram message ID of the sent consultation message |
| `consultation_reply_text` | `TEXT NULL` | `NULL` | Dan's reply text, stored verbatim when received |
| `consultation_reply_telegram_id` | `INTEGER NULL` | `NULL` | Telegram message ID of Dan's reply |
| `waiting_since` | `TEXT NULL` (ISO-8601) | `NULL` | When the UoW entered `waiting_for_signal` |
| `signal_timeout_hours` | `INTEGER NOT NULL` | `24` | Hours before auto-escalation to `blocked` |
| `consultation_state` | `TEXT NULL` | `NULL` | `"sent"` / `"acknowledged"` / `"replied"` |

Required index:

```sql
CREATE INDEX IF NOT EXISTS idx_uow_consultation_message_id
    ON uow_registry (consultation_message_id)
    WHERE consultation_message_id IS NOT NULL;
```

Required new `waiting_for_signal` status string — add to `UoWStatus` enum in
`src/orchestration/registry.py`:

```python
class UoWStatus(StrEnum):
    # ... existing values ...
    WAITING_FOR_SIGNAL = "waiting_for_signal"
```

### 3.5 Heartbeat behavior for `waiting_for_signal` UoWs

`steward-heartbeat.py` behavior when it encounters a UoW in `waiting_for_signal`:

| Phase | Action |
|-------|--------|
| Phase 1 (startup sweep) | **Skip entirely.** The startup sweep must not reclassify `waiting_for_signal` UoWs as orphans — they are intentionally paused, not stalled. |
| Phase 2 (observation loop / timeout check) | **Run consultation timeout check** (Section 3.3). If `waiting_since + signal_timeout_hours` exceeded, transition to `blocked`. Otherwise skip. |
| Phase 2b (heartbeat stall recovery) | **Skip.** No heartbeat TTL applies — these UoWs have no running executor. |
| Phase 3 (main steward loop) | **Skip.** Only `ready-for-steward` UoWs are eligible. |

```python
# Pseudocode — updated steward-heartbeat.py main loop structure
def run_steward_cycle(registry, ...):
    eligible = registry.list(status="ready-for-steward")
    # waiting_for_signal UoWs are not in this list — they are implicitly skipped
    for uow in eligible:
        process_uow(uow)
```

---

## Open Questions

These design decisions require Dan's input before implementation can begin.

1. **Timeout reminder cadence.** Should the Steward send Dan a reminder at some
   interval before the full `signal_timeout_hours` timeout fires? For example, a
   12-hour reminder before a 24-hour timeout. If yes: what text, and should the
   reminder include a button to extend the timeout?

2. **`signal_timeout_hours` configurability.** The default is 24 hours. Should
   there be a way to set a longer timeout per-UoW at consultation time (e.g., for
   UoWs that require a design session), or is 24 hours always the right ceiling?

3. **Multi-round consultations.** This design assumes a single consultation-and-reply
   round. Is there a use case where the Steward needs to send a second consultation
   message on the same UoW (e.g., Dan's reply raises a new question)? If yes, the
   schema and state machine need a `consultation_round` counter and a way to
   correlate multiple message IDs.

4. **`wos_consultation` new inbox type vs. extending `wos_escalate`.** The design
   uses a new type `wos_consultation` to keep the semantics clean (`wos_escalate`
   means "exhausted retries"; `wos_consultation` means "needs input before
   proceeding"). Dan should confirm that this separation is desired, or whether
   `wos_escalate` should grow a `reason="consultation"` branch instead.

5. **`consultation_message_id` fallback when Telegram API returns no ID.** The
   current spec leaves the UoW in `diagnosing` for the startup sweep to recover.
   Is there a better fallback — e.g., write a `wos_consultation_failed` audit entry
   and transition directly to `blocked`?

6. **Interaction with the `/decide` command.** The existing `/decide {uow_id} retry`
   command can unblock a `blocked` UoW. Should `/decide {uow_id} retry` also work
   for a `waiting_for_signal` UoW (e.g., if Dan wants to skip the consultation and
   just retry), or should `waiting_for_signal` only be clearable by an actual reply?
