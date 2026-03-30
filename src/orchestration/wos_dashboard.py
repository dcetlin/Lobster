"""
wos_dashboard.py — WOS observability dashboard.

Produces a text or JSON status report covering:
  1. Active UoWs (active, ready-for-executor, executing)
  2. UoW throughput: completed/failed in the last 24h
  3. Cycle histogram: steward_cycle distribution at completion (last 7 days)
  4. Active stalls: UoWs stuck in ready-for-steward/ready-for-executor >30m
  5. BOOTUP_CANDIDATE_GATE status

Run as:
    uv run src/orchestration/wos_dashboard.py [--format text|json]

Exits 0 on success.

Design:
- Pure functions over data; all side effects isolated at the boundary (main).
- Uses Registry.list() and audit_queries for all data access — no raw DB connections.
- Composable: build_dashboard_data() returns a plain dict usable by both renderers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path / DB resolution
# ---------------------------------------------------------------------------

def _default_registry_path() -> Path:
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    workspace = os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace"))
    return Path(workspace) / "orchestration" / "registry.db"


# ---------------------------------------------------------------------------
# Pure data-gathering functions
# ---------------------------------------------------------------------------

def _active_uows(registry: Any) -> list[dict]:
    """Return UoWs in active, ready-for-executor, or executing state.

    Each dict has: id, status, steward_cycles, time_in_state_seconds.
    """
    from src.orchestration.registry import UoWStatus
    active_statuses = {
        UoWStatus.ACTIVE,
        UoWStatus.READY_FOR_EXECUTOR,
        # 'executing' is not a canonical UoWStatus in the StrEnum but guard
        # against future additions by using string comparison below.
    }

    now = datetime.now(timezone.utc)
    result = []

    for uow in registry.list():
        if uow.status not in active_statuses and str(uow.status) != "executing":
            continue

        # Compute time-in-state as seconds since updated_at
        try:
            updated = datetime.fromisoformat(uow.updated_at.replace("Z", "+00:00"))
            time_in_state = int((now - updated).total_seconds())
        except (AttributeError, ValueError):
            time_in_state = -1

        result.append({
            "id": uow.id,
            "status": str(uow.status),
            "steward_cycles": uow.steward_cycles,
            "time_in_state_seconds": time_in_state,
        })

    return result


def _throughput_24h(registry_path: Path) -> dict[str, int]:
    """Return completed/failed counts in the last 24h from audit_log."""
    from src.orchestration import audit_queries
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    outcomes = audit_queries.execution_outcomes(since=since, registry_path=registry_path)
    return {
        "completed": outcomes.get("execution_complete", 0),
        "failed": outcomes.get("execution_failed", 0),
    }


def _cycle_histogram_last_7d(registry: Any, registry_path: Path) -> dict[str, int]:
    """Distribution of steward_cycles at completion, for UoWs completed in the last 7 days.

    Looks at UoWs that transitioned to 'done' in the last 7 days and groups
    by their current steward_cycles count. Returns {"cycles=N": count} dict.
    """
    from src.orchestration import audit_queries

    since = datetime.now(timezone.utc) - timedelta(days=7)
    since_iso = since.isoformat()

    # Collect UoW IDs that completed (done) in the last 7 days via audit_log.
    # We use a direct connection through audit_queries' helper for a single
    # targeted query rather than loading all UoWs.
    completed_uow_ids = _fetch_completed_uow_ids_since(registry_path, since_iso)

    if not completed_uow_ids:
        return {}

    # Map each completed UoW to its steward_cycles value.
    histogram: dict[str, int] = {}
    for uow_id in completed_uow_ids:
        uow = registry.get(uow_id)
        if uow is None:
            continue
        cycles = uow.steward_cycles or 0
        key = f"cycles={cycles}"
        histogram[key] = histogram.get(key, 0) + 1

    return dict(sorted(histogram.items(), key=lambda kv: int(kv[0].split("=")[1])))


def _fetch_completed_uow_ids_since(registry_path: Path, since_iso: str) -> list[str]:
    """Return UoW IDs that have an execution_complete audit entry since since_iso."""
    import sqlite3

    # Fall back to read-write connection if DB doesn't exist yet (no results).
    try:
        conn = sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.OperationalError:
        return []

    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT uow_id
            FROM audit_log
            WHERE event = 'execution_complete'
              AND ts >= ?
            """,
            (since_iso,),
        ).fetchall()
        return [row["uow_id"] for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _stalled_uows(registry: Any, stall_threshold_minutes: int = 30) -> list[dict]:
    """Return UoWs in ready-for-steward or ready-for-executor for longer than threshold.

    Each dict has: id, status, time_in_state_seconds.
    """
    from src.orchestration.registry import UoWStatus
    stall_statuses = {UoWStatus.READY_FOR_STEWARD, UoWStatus.READY_FOR_EXECUTOR}
    threshold_seconds = stall_threshold_minutes * 60
    now = datetime.now(timezone.utc)
    result = []

    for uow in registry.list():
        if uow.status not in stall_statuses:
            continue
        try:
            updated = datetime.fromisoformat(uow.updated_at.replace("Z", "+00:00"))
            elapsed = int((now - updated).total_seconds())
        except (AttributeError, ValueError):
            elapsed = -1

        if elapsed >= threshold_seconds:
            result.append({
                "id": uow.id,
                "status": str(uow.status),
                "time_in_state_seconds": elapsed,
            })

    return result


def _bootup_gate_status(registry: Any) -> dict[str, Any]:
    """Return BOOTUP_CANDIDATE_GATE status and count of blocked UoWs.

    'gate_open' = True means the gate is active and blocking bootup candidates.
    blocked_count is the number of ready-for-steward UoWs that would be skipped.
    """
    from src.orchestration.steward import BOOTUP_CANDIDATE_GATE
    from src.orchestration.registry import UoWStatus

    # Count UoWs in ready-for-steward (candidates that the gate might block).
    ready_for_steward = registry.list(status=str(UoWStatus.READY_FOR_STEWARD))
    blocked_count = len(ready_for_steward)

    return {
        "gate_open": BOOTUP_CANDIDATE_GATE,
        "blocked_count": blocked_count if BOOTUP_CANDIDATE_GATE else 0,
        "description": (
            "gate is OPEN — bootup-candidate UoWs are skipped by the Steward"
            if BOOTUP_CANDIDATE_GATE
            else "gate is CLOSED — all UoWs are processed normally"
        ),
    }


# ---------------------------------------------------------------------------
# Top-level data assembly — pure function
# ---------------------------------------------------------------------------

def build_dashboard_data(
    registry: Any,
    registry_path: Path,
) -> dict[str, Any]:
    """Assemble the full dashboard payload as a plain dict.

    This function is the composition point: each sub-query is pure and
    independently testable; build_dashboard_data just calls them in sequence.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_uows": _active_uows(registry),
        "throughput_24h": _throughput_24h(registry_path),
        "cycle_histogram_7d": _cycle_histogram_last_7d(registry, registry_path),
        "stalled_uows": _stalled_uows(registry),
        "bootup_candidate_gate": _bootup_gate_status(registry),
    }


# ---------------------------------------------------------------------------
# Text renderer — pure function mapping dict → str
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: int) -> str:
    """Format seconds as a human-readable duration string."""
    if seconds < 0:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"


def render_text(data: dict[str, Any]) -> str:
    """Render the dashboard data as a plain-text report string."""
    lines: list[str] = []
    lines.append(f"WOS Dashboard — {data['generated_at']}")
    lines.append("=" * 60)

    # 1. Active UoWs
    active = data["active_uows"]
    lines.append(f"\n[1] Active UoWs ({len(active)})")
    if active:
        for u in active:
            lines.append(
                f"  {u['id']}  status={u['status']}  "
                f"cycles={u['steward_cycles']}  "
                f"in-state={_fmt_duration(u['time_in_state_seconds'])}"
            )
    else:
        lines.append("  (none)")

    # 2. Throughput
    tp = data["throughput_24h"]
    lines.append(f"\n[2] Throughput (last 24h)")
    lines.append(f"  completed: {tp['completed']}  failed: {tp['failed']}")

    # 3. Cycle histogram
    hist = data["cycle_histogram_7d"]
    lines.append(f"\n[3] Steward-cycle distribution at completion (last 7d)")
    if hist:
        parts = [f"{k}: {v}" for k, v in hist.items()]
        lines.append("  " + ",  ".join(parts))
    else:
        lines.append("  (no completions in last 7d)")

    # 4. Stalls
    stalls = data["stalled_uows"]
    lines.append(f"\n[4] Active stalls >30m ({len(stalls)})")
    if stalls:
        for s in stalls:
            lines.append(
                f"  STALLED  {s['id']}  status={s['status']}  "
                f"in-state={_fmt_duration(s['time_in_state_seconds'])}"
            )
    else:
        lines.append("  (none)")

    # 5. BOOTUP_CANDIDATE_GATE
    gate = data["bootup_candidate_gate"]
    lines.append(f"\n[5] BOOTUP_CANDIDATE_GATE")
    lines.append(f"  {gate['description']}")
    if gate["gate_open"]:
        lines.append(f"  UoWs currently in ready-for-steward: {gate['blocked_count']}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="WOS observability dashboard — text or JSON status report",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override registry DB path (default: auto-detected from env)",
    )
    args = parser.parse_args(argv)

    registry_path = Path(args.db) if args.db else _default_registry_path()

    # Import here to keep module-level imports minimal (testable without full env).
    from src.orchestration.registry import Registry

    registry = Registry(registry_path)
    data = build_dashboard_data(registry, registry_path)

    if args.format == "json":
        print(json.dumps(data, indent=2))
    else:
        print(render_text(data), end="")

    return 0


if __name__ == "__main__":
    sys.exit(main())
