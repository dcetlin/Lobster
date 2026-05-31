# WOS Prescription Throttle Gate

## Problem

The WOS prescription engine (cultivator.py) scans GitHub issues and promotes them into the registry as `proposed` UoWs on every sweep cycle. Because each sweep can add dozens of new UoWs while the executor pipeline closes only a handful per session, the queue grows unboundedly. After 5+ consecutive async-deep-work sessions, the opened-to-closed ratio dropped to ~0.38 — meaning the system was producing UoWs more than twice as fast as it was consuming them. Without a throttle, the proposed-state backlog becomes a noise source that obscures genuine signal and wastes steward cycles on re-evaluation.

## Mechanism

Two classes in `src/orchestration/wos_throttle.py` work together:

**`ConsumptionRateMonitor`**
- Reads the WOS registry SQLite DB (read-only, no mutations)
- Computes `consumption_rate = closed_count / (closed_count + open_count)` over a configurable rolling window (default: 7 days)
- "Closed" = done, cancelled, expired, failed; "Open" = proposed, ready-for-steward, executing, blocked, needs-human-review
- Exposes: `get_rate() -> float`, `is_backlog_critical(threshold=0.6) -> bool`, `backlog_depth() -> int`
- Fails open on DB errors (returns `rate=1.0`, `depth=0`) — throttle does not fire if the monitor cannot read state

**`PrescriptionThrottleGate`**
- Takes a `ConsumptionRateMonitor` instance
- `should_suppress_prescription()` returns `True` only when BOTH conditions hold:
  - `monitor.is_backlog_critical(threshold)` — rate is below the configured threshold
  - `monitor.backlog_depth() >= min_depth` — the absolute open count is large enough to warrant suppression
- `gate_status() -> dict` returns `{suppressed, rate, depth, threshold, min_depth, reason}` for logging

The gate is wired into `promote_to_wos()` in `cultivator.py`, immediately before the per-issue `registry.upsert()` loop. When suppressed, the function logs a WARNING and returns `([], 0)` — no UoWs are written, no issues are promoted.

## Threshold Rationale

**Rate threshold: 0.6** — Observed 7-day rate at time of implementation was 0.38 (109 done vs 177 proposed). A threshold of 0.6 leaves headroom: suppression fires only when fewer than 60% of recently opened UoWs are closed, indicating genuine production-over-consumption imbalance. A threshold closer to 1.0 would suppress too aggressively; closer to 0.0 would never fire.

**Minimum depth: 5** — Suppressing when 2-3 UoWs are open and the rate is low would be a false positive on a nearly-idle queue. The depth guard ensures suppression only fires when there is a real queue to drain. At observed backlog depths of 177-180 items, the depth=5 floor is easily met.

Both parameters are configurable via `PrescriptionThrottleGate(monitor, threshold=0.6, min_depth=5)` and `ConsumptionRateMonitor(window_days=7)`.

## Limitations

- **Does not drain the existing backlog.** The throttle prevents new UoWs from being added; it does not close or cancel the 177 already-proposed UoWs. A separate backlog triage step is needed to reduce depth.
- **Does not throttle reactivation.** `garden_caretaker.py` can reactivate archived UoWs back to `proposed`; this path is not gated by the throttle.
- **Rolling window is creation-date based.** A UoW created 8 days ago is invisible to the monitor even if it is still open. This is intentional — it prevents ancient stale proposals from permanently suppressing new work.
- **Does not distinguish issue priority.** When throttled, all new promotions are blocked equally. High-priority issues will not be promoted until the backlog drains below the threshold.
- **Single point of wiring.** Only `cultivator.py`'s `promote_to_wos()` is gated. If other code paths write UoWs directly (e.g. via `registry_cli.py`), they are not throttled.
