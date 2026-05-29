# Hydra: Research & Integration Assessment

**Repo:** https://github.com/sf8193/hydra  
**Assessed:** 2026-05-29  
**Context:** Lobster WOS evolution design session — five bold structural leaps under consideration

---

## What Hydra Is

Hydra is a multi-platform chat bridge that connects a Claude Code session to Discord and/or Slack via MCP, implementing a daemon+bridge split architecture with session spawning and access control.

That's the complete description. It is not an agent orchestration framework. It is not an alternative to WOS. It is an infrastructure tool for connecting Claude to chat platforms — solving the same class of problem Lobster's Telegram integration already solves, with different implementation choices.

---

## Architecture Overview

```
Discord/Slack Gateway
        |
    daemon.ts           — holds single gateway connection per platform
        |                 manages sessions, access control, message routing
   unix socket (NDJSON)
        |
    bridge.ts           — thin MCP relay, one per Claude session
        |
  Claude Code (--channels plugin:discord)
```

**Key structural choices:**
- One gateway process (daemon) shared across all Claude sessions for a platform
- Claude talks to daemon via MCP notifications, not polling
- Sessions spawned as tmux processes; state persisted in JSON files on disk
- Access control is file-backed (access.json), re-read on every inbound message
- Session spawning: daemon creates a Discord/Slack thread, launches a new Claude session in tmux pointed at that thread

---

## Honest Novelty Assessment

### What is genuinely different from Lobster

**1. The daemon/bridge separation**

Hydra's most structurally interesting decision: the gateway holds a persistent connection (Discord's WebSocket, Slack's Socket Mode) while Claude sessions come and go independently. The bridge is a thin relay with no platform knowledge — it just speaks NDJSON over a unix socket. Sessions reconnect automatically; the daemon queues messages while they're disconnected.

Lobster's Telegram integration uses a polling model (lobster-inbox MCP server + message files). The event arrives, a message file gets written, Lobster reads it on the next `wait_for_messages` poll. Hydra's model is genuinely push-native at the infrastructure level — the gateway fires events and the daemon routes them without polling.

This is real, but it's not a WOS-level concept. It's an infrastructure pattern that could inform how Lobster handles platform connections if/when Discord or Slack are added. For Telegram specifically, the existing MCP + file-backed queue works fine.

**2. Multi-session routing from a single gateway connection**

When Hydra spawns a new Claude session, it creates a thread in Discord/Slack, maps that thread's ID to the session, and routes subsequent messages to the right session. The daemon acts as a stateful message router: `threadToSession` map, bridge connection registry, message queue per session.

Lobster has nothing analogous. Lobster is single-session — one dispatcher, one loop. The WOS executor spawns subagents as subprocesses, but they all write results back to the same inbox; there's no concept of a user talking directly to a subagent in a separate chat thread.

This is worth noting as a pattern, though it's a UI/UX feature rather than an orchestration architecture.

**3. Permission request relay over MCP notifications**

Claude can emit a permission request; the daemon translates it into Discord buttons (Allow / Deny / More); the user clicks a button; the button callback routes the response back to the daemon which forwards it to Claude's MCP bridge. This creates a UI-level approval flow for tool use permissions that doesn't require the user to be watching a terminal.

Lobster has no equivalent. Lobster uses `--dangerously-skip-permissions`. If human-in-the-loop tool approval ever becomes relevant, this pattern is worth studying.

**4. Skills as Claude-plugin-native SKILL.md files**

Hydra packages access management and configuration as skills (`skills/access/SKILL.md`, `skills/configure/SKILL.md`) inside a `.claude-plugin` directory. These are invocable via `/discord:access` and `/discord:configure` directly from Claude's terminal session. The plugin.json declares the MCP server (bridge.ts) and the skills ship alongside it.

Lobster has a skill system but it's Lobster-specific. Hydra's approach demonstrates what it looks like to package skills *as part of* an MCP plugin — the bridge, the daemon management, and the access control UI are all one deployable unit.

This is conceptually clean. Worth noting as a packaging pattern.

### What Hydra does NOT do

- No agent orchestration or task planning
- No memory or context persistence beyond session JSON files on disk
- No work-ordering, stewardship, or self-amendment
- No prescription tracking, verdict accumulation, or health metrics
- No scheduler, heartbeat, or cron-equivalent
- No MCP server for the user to interact with programmatically
- No abstraction beyond "get message, deliver to Claude session, get reply, send it back"

---

## Map Against the Five Evolution Directions

### 1. Adaptive Steward (self-improving prescriptions via verdict accumulation)

**No overlap.** Hydra has no concept of prescriptions, verdicts, or self-improvement. It's stateless with respect to outcomes — it routes messages, it doesn't learn from them.

**Integration potential: None.** The Adaptive Steward requires a feedback loop across sessions. Hydra's architecture doesn't touch this.

### 2. Event-Native Nervous System (signal-driven dispatch, not heartbeat-driven)

**Partial overlap — at the infrastructure layer only.** Hydra's daemon is event-native: Discord/Slack push messages via WebSocket/Socket Mode, and the daemon reacts immediately without polling. This is the correct model for a chat gateway.

Lobster's `wait_for_messages` polling is already low-latency (it returns when messages arrive), but the underlying inbox is still file-backed with a polling heartbeat from the Telegram bot. The gap is small in practice.

**Integration potential: Low for current architecture.** If Lobster ever adds Discord or Slack as a first-class channel, Hydra's daemon model (push gateway → daemon → MCP notification → Claude) is the right pattern to follow. It doesn't inform the WOS executor or dispatcher internals, which are where the Event-Native Nervous System concept is actually targeted.

### 3. Executor Mesh (distributed autonomy, protocol not application)

**Adjacent but not equivalent.** Hydra's multi-session model (daemon routes to multiple Claude instances, each in its own thread) is a form of distributed execution. But it's user-visible distribution — each session is a named entity a human interacts with directly. The Executor Mesh concept is about autonomous work distribution across nodes that collaborate on tasks.

Hydra's session spawning is closer to "give a user their own Claude session in a thread" than "distribute a work unit across autonomous executors." There's no task protocol, no result aggregation, no shared state across sessions.

**Integration potential: None directly.** The Executor Mesh requires a protocol-level abstraction for task handoff and result collection. Hydra doesn't have this.

### 4. Closed-Loop Self-Amendment (automated config changes with human review)

**No overlap.** Hydra's access.json is hand-edited (or edited by skills). There's no self-amendment loop, no proposal/review/apply cycle. The `/discord:access` skill is a convenience wrapper around JSON writes.

One small note: Hydra's access control *does* have a human approval gate (pairing codes, button-based permission approval). This is a specific UI pattern for human-in-the-loop approval, not a self-amendment architecture.

**Integration potential: None.**

### 5. Orientation Layer / Governor (portfolio prescriptions against workstream health)

**No overlap.** Hydra has no concept of workstreams, portfolio health, or prescriptions. It's a message relay, not a strategic orchestrator.

**Integration potential: None.**

---

## What Is Directly Integrable

Nothing from Hydra is directly integrable into Lobster's core. They operate at different layers of abstraction.

What *could* be integrated, scoped correctly:

**If Discord becomes a Lobster channel:** Hydra's daemon+bridge architecture is the right model to follow. The gateway isolation (one connection per platform), the session-to-thread mapping, and the bridge reconnect logic are all worth adopting. This would be a new Lobster capability, not a replacement of anything.

**Permission request UI pattern:** If Lobster ever needs human-in-the-loop tool approval (currently bypassed with `--dangerously-skip-permissions`), the button-based approval flow Hydra implements in Discord is a clean pattern. A Telegram equivalent would use inline keyboard buttons — which Lobster already supports in `send_reply`.

**Plugin packaging:** Hydra's `.claude-plugin` directory structure (plugin.json + SKILL.md files bundled with the MCP server) is a clean way to ship a self-contained integration. Worth studying if Lobster skills ever get packaged for distribution.

---

## What Would Replace Something in Lobster

Nothing. Hydra solves a different problem — multi-platform chat gateway — that Lobster currently solves only for Telegram, via a simpler but functionally adequate polling model.

If sf8193's approach were adopted wholesale, it would change *how* Telegram messages reach Lobster (push via daemon vs. file-backed polling), but it wouldn't change what Lobster does with those messages once it has them.

---

## What Is a Genuine Net-New Concept Lobster Should Consider

**The thread-scoped session model.**

Hydra's pattern of mapping a chat thread to a Claude session creates something Lobster doesn't have: a way for a user to have multiple simultaneous, named, isolated conversations with Claude, each with its own thread and its own context.

Lobster is currently single-session. All conversations flow through one dispatcher. There's no concept of "spawn a session for this topic and let me talk to it directly in its own thread."

Whether Lobster should adopt this depends on whether Dan wants to talk to multiple named Claude instances simultaneously. For a personal assistant system, probably not — the single-session model is simpler and coherent. But if Lobster ever needs to let a user spin up a focused research session while keeping the main dispatcher running, this pattern is the right shape for it.

This is worth holding as a design primitive, not implementing now.

---

## Summary Verdict

Hydra is well-built infrastructure for the problem it solves. The daemon/bridge split is genuinely clever — it isolates platform connection concerns from Claude session concerns in a way that's robust to session restart. The permission button UI is thoughtful. The skills packaging is clean.

It is not an agent orchestration framework. It has no overlap with WOS's core concerns (work ordering, stewardship, prescription accumulation, self-amendment). It doesn't inform the Adaptive Steward, Executor Mesh, Closed-Loop Self-Amendment, or Orientation Layer directions in any substantive way.

The Event-Native Nervous System has the most surface-area overlap, but only at the infrastructure layer — Hydra demonstrates what event-native looks like for chat gateway connections, not for WOS executor dispatch.

If Discord becomes a priority channel for Lobster, come back to this repo. For WOS evolution, it's not a source.
