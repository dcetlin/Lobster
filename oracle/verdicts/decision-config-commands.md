# Design Decision: /config Dispatcher Commands for Mobile Config Access

**Date:** 2026-05-18
**Status:** ACCEPTED
**Vision anchor:** constraint-4 (minimize metabolic cost of cybernetic engagement — structural reduction of friction in system operation)
**Linked PR:** dcetlin/Lobster#1209
**Approved by:** dcetlin (Dan)

## Decision

Authorize adding `/config list`, `/config read <file>`, `/config search <term>`, and `/config append <file> <content>` commands to the dispatcher to enable mobile read/edit access to bootup config files in `~/lobster-user-config/agents/`.

- **`/config list`** — lists all files in `~/lobster-user-config/agents/`
- **`/config read <file>`** — reads and returns the contents of the named config file
- **`/config search <term>`** — searches across config files for a term
- **`/config append <file> <content>`** — appends content to the named config file

These commands are handled inline in the dispatcher (no subagent spawn required) and are documented in `sys.dispatcher.bootup.md` under the command routing section.

## Rationale

Reading and editing user config files (behavioral preferences, personal context, bootup overrides) previously required SSH access to the server or a separate desktop session. This is a friction point that violates constraint-4: every round-trip to a separate tool to inspect or adjust Lobster's operating parameters is metabolic overhead that the system should eliminate structurally.

The `/config` commands collapse that overhead: the user can inspect and adjust config files directly from Telegram, on mobile, without breaking flow. This is an Encoded Orientation change — the dispatcher now autonomously handles file reads and appends on the user's behalf — and is logged here as the authorizing prior decision per constraint-3.

## Constraints

- Scope is limited to `~/lobster-user-config/agents/` — no access to arbitrary filesystem paths
- Append-only writes (no overwrite, no delete) to reduce risk of destructive mobile edits
- Commands are inline dispatcher handlers — no subagent spawn, no LLM round-trip for file I/O
- The command set is additive; no existing dispatcher behavior is changed
