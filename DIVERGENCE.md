# DIVERGENCE.md

Intentional divergences between dcetlin/Lobster (ours) and SiderealPress/lobster (upstream).

Each entry records the file, what we kept vs. what upstream has, the rationale, and when it was last audited.

---

## Divergence 1: `.claude/sys.dispatcher.bootup.md`

**Our version:** ~127KB — includes Tier-1 gate table, WOS handlers, posture temperature system, IFTTT rule loading, task system integration, epistemic hooks, and dcetlin-specific operational context.

**Upstream version:** ~41KB — trimmed to core dispatcher loop; all dcetlin-specific features removed.

**Decision:** Keep ours. The Tier-1 gate table, WOS handlers, posture temperature, and IFTTT loading are all operationally active in this install. Removing them would break running behavior.

**Future work:** A future pass should trim prose sections that are now redundant with the gate table, to reduce context load without losing behavioral coverage.

**Date audited:** 2026-03-31

---

## Divergence 2: `scheduled-tasks/dispatch-job.sh`

**Our version:** Uses the `jobs.json` `enabled` field to gate whether a job is dispatched (cron + jobs.json architecture).

**Upstream version:** Uses `systemctl is-enabled` to gate dispatch (systemd-native architecture).

**Decision:** Keep ours. This install uses cron + jobs.json, not systemd timers. Applying upstream's version would break job dispatch entirely.

**Future work:** Backport two specific improvements from upstream independently:
1. Auto-disable on missing task file (upstream added this guard)
2. Inbox dedup guard (upstream added deduplication before dispatching)

**Date audited:** 2026-03-31

---

## Divergence 3: `scripts/health-check-v3.sh` — Check 12

**Our version:** Includes Check 12 — a memory capability probe that fires a real `memory_store` call to verify the memory subsystem is live (not silently broken).

**Upstream version:** Does not include Check 12.

**Decision:** Keep ours. Check 12 was added after the 2026-03-23 memory outage where a silent `ImportError` left the memory object as `None` for an entire session lifetime. Passive health checks did not catch this. Check 12 catches it actively.

Upstream never experienced this failure mode, so they have no reason to add this check. It is a dcetlin-specific operational hardening.

**Date audited:** 2026-03-31
