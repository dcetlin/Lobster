## Footer Reset — Audit Mode

When the user sends `/footer-reset` or `/reset-footer`, or explicitly asks to audit outbox messages for footer drift, run the footer audit.

### How to respond

Spawn a background subagent (follows the 7-second rule) with this exact prompt:

```
Run the footer audit and send results to chat_id={chat_id}.

1. Execute:
   uv run ~/lobster/lobster-shop/footer-reset/src/footer_audit.py

2. Send the EXACT script output as a reply via send_reply to chat_id={chat_id}.

3. If the script fails, send: "Footer audit failed: <error>"

side-effects: none
```

Do not inject any canonical format reminder. The audit speaks for itself.

### What this skill does NOT do

- Does not re-inject the canonical format reminder
- Does not attempt to correct drift in-session
- Does not modify any messages

The audit is purely observational: it reads outbox messages and reports findings.
