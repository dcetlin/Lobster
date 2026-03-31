## Footer Reset

When the user sends `/footer-reset`, `/reset-footer`, or when the message mentions footer drift, side-effects label confusion, or signal footer problems, execute the footer reset protocol below.

### Canonical Format Reminder (always inject first)

**The only accepted label is `side-effects:` — no other label is valid.**

Two valid forms:

**With side effects** — end the reply with a fenced code block:

````
```side-effects:
✅ 🐙 📝
```
````

**No side effects** — write the explicit null on its own line (not a code block):

```
side-effects: none
```

**Signal vocabulary (10-signal set):**

| Signal | Meaning |
|--------|---------|
| `🤖` | spawned — subagent or background task launched |
| `✅` | done — task completed |
| `🐙` | PR — pull request opened or updated |
| `🔀` | merged — PR or branch merged |
| `🗑️` | closed — issue or PR closed |
| `⚠️` | blocked — work is blocked |
| `📝` | wrote — file or doc written |
| `🔍` | read — file or data read |
| `🔧` | config — configuration changed |
| `💬` | decide — decision made or surfaced |

**Common wrong patterns (all invalid):**

| Wrong | Why it fails |
|-------|-------------|
| `` ```signals: ✅ ``` `` | Label must be `side-effects:`, not `signals:` |
| `` ```effects: ✅ ``` `` | Label must be `side-effects:`, not `effects:` |
| `` ```side-effects ✅ ``` `` | Missing colon — label is `side-effects:` with colon |
| `side-effects:` (inline, no code block) | Use a fenced code block for non-null case |
| Omitting footer entirely | Silent omission fails the hook; use `side-effects: none` explicitly |

---

### How to respond to `/footer-reset`

1. **Immediately send** the canonical reminder above (formatted for Telegram — use bold, code blocks).

2. **Spawn a background subagent** to audit recent outbox messages (follows the 7-second rule). Use this exact prompt:

```
Run the footer audit and send results to chat_id={chat_id}.

1. Execute:
   uv run ~/lobster/lobster-shop/footer-reset/src/footer_audit.py

2. Send the EXACT script output as a reply via send_reply to chat_id={chat_id}.

3. If the script fails, send: "Footer audit failed: <error>"
```

3. Once the subagent result arrives, the dispatcher relays it. No further action needed.

---

### Contextual trigger behavior

When the skill activates contextually (footer drift mention, label confusion, etc.) rather than via explicit command:

- **Do not** proactively inject the full canonical reminder unless the user is clearly confused or asking for guidance.
- **Do** note the correct format briefly (one sentence) if correcting a drift observation.
- Spawn the audit subagent only if the user asks for an audit or if anomalies were just surfaced.
