"""
wos_dashboard.py — WOS observability dashboard.

Produces a text, JSON, or HTML status report covering:
  1. Active UoWs (active, ready-for-executor, executing)
  2. UoW throughput: completed/failed in the last 24h
  3. Cycle histogram: steward_cycle distribution at completion (last 7 days)
  4. Active stalls: UoWs stuck in ready-for-steward/ready-for-executor >30m
  5. BOOTUP_CANDIDATE_GATE status

Run as:
    uv run src/orchestration/wos_dashboard.py [--format text|json|html] [--with-drilldowns]

The --with-drilldowns flag (only meaningful with --format html) generates a per-UoW
drilldown HTML page for each active and stalled UoW via wos_uow_detail_gen.generate_and_upload(),
then links each UoW row to its detail page. Generating drilldowns for all UoWs can be
slow if the queue is large; it is off by default.

Exits 0 on success.

Design:
- Pure functions over data; all side effects isolated at the boundary (main).
- Uses Registry.list() and audit_queries for all data access — no raw DB connections
  opened directly in this module; all DB access goes through audit_queries._connect().
- Composable: build_dashboard_data() returns a plain dict usable by all renderers.
- generate_drilldown_urls() is the single composition point for drilldown side effects;
  it maps over UoW IDs and isolates per-UoW errors without aborting the whole batch.
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
    """Return UoW IDs that have an execution_complete audit entry since since_iso.

    Delegates to audit_queries.completed_uow_ids_since() so that all DB access
    goes through audit_queries._connect() (WAL mode, busy_timeout=5000).
    """
    from src.orchestration import audit_queries
    return audit_queries.completed_uow_ids_since(since=since_iso, registry_path=registry_path)


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

    Reads the file flag via is_bootup_candidate_gate_active() so the dashboard
    always reflects the current on-disk state, not a stale module-load value.
    """
    from src.orchestration.steward import is_bootup_candidate_gate_active
    from src.orchestration.registry import UoWStatus

    gate_active = is_bootup_candidate_gate_active()

    # Count UoWs in ready-for-steward (candidates that the gate might block).
    ready_for_steward = registry.list(status=str(UoWStatus.READY_FOR_STEWARD))
    blocked_count = len(ready_for_steward)

    return {
        "gate_open": gate_active,
        "blocked_count": blocked_count if gate_active else 0,
        "description": (
            "gate is OPEN — bootup-candidate UoWs are skipped by the Steward"
            if gate_active
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
# Drilldown URL generation — side-effectful, isolated at boundary
# ---------------------------------------------------------------------------

def generate_drilldown_urls(
    uow_ids: list[str],
    db_path: Path | None,
    ledger_path: Path | None,
) -> dict[str, str]:
    """Generate and upload a per-UoW drilldown page for each UoW ID.

    Returns a dict mapping uow_id → public URL for successfully generated pages.
    UoWs that fail (not found, upload error) are silently omitted — the caller
    receives whatever succeeded.

    Side effects: writes HTML files to ~/messages/bisque-uploads/ and returns
    public bisque URLs.
    """
    from src.orchestration import wos_uow_detail_gen

    result: dict[str, str] = {}
    for uow_id in uow_ids:
        try:
            url = wos_uow_detail_gen.generate_and_upload(
                uow_id=uow_id,
                db_path=db_path,
                ledger_path=ledger_path,
            )
            result[uow_id] = url
        except Exception:
            # Log failure silently — one bad UoW should not abort the whole batch.
            pass
    return result


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
# HTML renderer — pure function mapping dict + drilldown_urls → str
# ---------------------------------------------------------------------------

def _uow_id_cell(uow_id: str, drilldown_urls: dict[str, str]) -> str:
    """Return an HTML table cell for a UoW ID, optionally linked to its drilldown page."""
    url = drilldown_urls.get(uow_id)
    if url:
        return f'<td class="uid"><a href="{url}" target="_blank">{uow_id} ↗</a></td>'
    return f'<td class="uid">{uow_id}</td>'


def render_html(data: dict[str, Any], drilldown_urls: dict[str, str]) -> str:
    """Render the dashboard data as a self-contained HTML page.

    Pure function: no IO. drilldown_urls maps uow_id → public URL for any UoWs
    that have a pre-generated drilldown page. Rows with no entry in drilldown_urls
    show the UoW ID as plain text.
    """
    generated_at = data.get("generated_at", "")
    active = data.get("active_uows", [])
    tp = data.get("throughput_24h", {"completed": 0, "failed": 0})
    hist = data.get("cycle_histogram_7d", {})
    stalls = data.get("stalled_uows", [])
    gate = data.get("bootup_candidate_gate", {})

    # --- Active UoWs table ---
    if active:
        active_rows = "\n".join(
            f"<tr>"
            f"{_uow_id_cell(u['id'], drilldown_urls)}"
            f"<td><span class='badge {_status_badge_class(u['status'])}'>{u['status']}</span></td>"
            f"<td>{u['steward_cycles']}</td>"
            f"<td>{_fmt_duration(u['time_in_state_seconds'])}</td>"
            f"</tr>"
            for u in active
        )
        active_section = f"""
        <table class="tbl">
          <thead><tr>
            <th>UoW ID</th><th>Status</th><th>Cycles</th><th>In State</th>
          </tr></thead>
          <tbody>{active_rows}</tbody>
        </table>"""
    else:
        active_section = "<p class='empty'>No active UoWs</p>"

    # --- Stalled UoWs table ---
    if stalls:
        stall_rows = "\n".join(
            f"<tr>"
            f"{_uow_id_cell(s['id'], drilldown_urls)}"
            f"<td><span class='badge bf'>{s['status']}</span></td>"
            f"<td>{_fmt_duration(s['time_in_state_seconds'])}</td>"
            f"</tr>"
            for s in stalls
        )
        stalls_section = f"""
        <table class="tbl">
          <thead><tr>
            <th>UoW ID</th><th>Status</th><th>Stalled For</th>
          </tr></thead>
          <tbody>{stall_rows}</tbody>
        </table>"""
    else:
        stalls_section = "<p class='empty'>No stalls &gt;30m</p>"

    # --- Histogram ---
    if hist:
        hist_items = "  ".join(
            f"<span class='badge bc'>{k}: {v}</span>" for k, v in hist.items()
        )
        hist_section = f"<div style='display:flex;flex-wrap:wrap;gap:6px;'>{hist_items}</div>"
    else:
        hist_section = "<p class='empty'>No completions in last 7d</p>"

    # --- Gate badge ---
    gate_open = gate.get("gate_open", False)
    gate_badge_class = "bf" if gate_open else "bd"
    gate_label = "OPEN" if gate_open else "CLOSED"
    gate_desc = gate.get("description", "")
    blocked_note = ""
    if gate_open:
        blocked_note = f"<span style='font-size:.75rem;color:var(--text2);margin-left:8px'>({gate.get('blocked_count', 0)} ready-for-steward)</span>"

    drilldown_note = (
        "<p class='meta' style='margin-bottom:10px'>Drilldown links generated for each row.</p>"
        if drilldown_urls else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WOS Dashboard</title>
<style>
:root{{--bg:#f5f5f5;--surface:#fff;--surface2:#f0f0f0;--border:#ddd;--text:#1a1a1a;--text2:#555;--text3:#888;--accent:#4a7fc0;--done-col:#1a7a3c;--done-bg:#d4f7e0;--pend-col:#a06000;--pend-bg:#fef3cd;--fail-col:#c0392b;--fail-bg:#fde8e6;--act-col:#1565c0;--act-bg:#e3f0ff;--cl-col:#666;--cl-bg:#eee;--shadow:0 1px 4px rgba(0,0,0,.08)}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1117;--surface:#1c1f2a;--surface2:#252837;--border:#333;--text:#e8e8e8;--text2:#aaa;--text3:#666;--accent:#6fa3e0;--done-col:#4ade80;--done-bg:#0a2e1a;--pend-col:#fbbf24;--pend-bg:#2a1e00;--fail-col:#f87171;--fail-bg:#2a0a0a;--act-col:#60a5fa;--act-bg:#0a1e3a;--cl-col:#888;--cl-bg:#252525;--shadow:0 1px 4px rgba(0,0,0,.4)}}}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);line-height:1.5}}
.wrap{{max-width:960px;margin:0 auto;padding:16px}}
h1{{font-size:1.3rem;font-weight:700;margin-bottom:4px}}
h2{{font-size:.8rem;font-weight:600;margin-bottom:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.05em}}
.meta{{color:var(--text3);font-size:.75rem;margin-bottom:12px}}
.sec{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px;box-shadow:var(--shadow)}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.72rem;font-weight:600;white-space:nowrap}}
.bd{{color:var(--done-col);background:var(--done-bg)}}
.bp{{color:var(--pend-col);background:var(--pend-bg)}}
.bf{{color:var(--fail-col);background:var(--fail-bg)}}
.ba{{color:var(--act-col);background:var(--act-bg)}}
.bc{{color:var(--cl-col);background:var(--cl-bg)}}
.tbl{{width:100%;border-collapse:collapse;font-size:.8rem}}
.tbl th{{text-align:left;padding:5px 8px;color:var(--text3);font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border)}}
.tbl td{{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:middle}}
.tbl tr:last-child td{{border-bottom:none}}
.uid{{font-family:monospace;font-size:.78rem}}
.uid a{{color:var(--accent);text-decoration:none}}
.uid a:hover{{text-decoration:underline}}
.pgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;margin-bottom:4px}}
.scard{{background:var(--surface2);border-radius:8px;padding:10px 12px;text-align:center}}
.scard .n{{font-size:1.4rem;font-weight:700;color:var(--accent)}}
.scard .l{{font-size:.65rem;color:var(--text3);text-transform:uppercase;letter-spacing:.04em}}
.empty{{color:var(--text3);font-size:.82rem;padding:8px 0}}
</style>
</head>
<body>
<div class="wrap">
<h1>WOS Dashboard</h1>
<p class="meta">Generated {generated_at}</p>
{drilldown_note}

<div class="sec">
  <h2>Active UoWs ({len(active)})</h2>
  {active_section}
</div>

<div class="sec">
  <h2>Throughput (last 24h)</h2>
  <div class="pgrid">
    <div class="scard"><div class="n">{tp['completed']}</div><div class="l">Completed</div></div>
    <div class="scard"><div class="n">{tp['failed']}</div><div class="l">Failed</div></div>
  </div>
</div>

<div class="sec">
  <h2>Steward-Cycle Distribution (last 7d)</h2>
  {hist_section}
</div>

<div class="sec">
  <h2>Active Stalls &gt;30m ({len(stalls)})</h2>
  {stalls_section}
</div>

<div class="sec">
  <h2>BOOTUP_CANDIDATE_GATE</h2>
  <span class="badge {gate_badge_class}">{gate_label}</span>{blocked_note}
  <p style="font-size:.78rem;color:var(--text2);margin-top:6px">{gate_desc}</p>
</div>

</div>
</body>
</html>"""


def _status_badge_class(status: str) -> str:
    """Map a UoW status string to a CSS badge class."""
    mapping = {
        "done": "bd",
        "closed": "bc",
        "expired": "bc",
        "cancelled": "bc",
        "failed": "bf",
        "needs-human-review": "bf",
        "blocked": "bf",
        "active": "ba",
        "ready-for-executor": "ba",
        "executing": "ba",
        "ready-for-steward": "ba",
        "proposed": "bp",
        "pending": "bp",
    }
    return mapping.get(status, "bc")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="WOS observability dashboard — text, JSON, or HTML status report",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "html"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override registry DB path (default: auto-detected from env)",
    )
    parser.add_argument(
        "--with-drilldowns",
        action="store_true",
        default=False,
        help=(
            "Generate per-UoW drilldown pages and embed links in the HTML output. "
            "Only meaningful with --format html. Can be slow for large queues."
        ),
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help="Override token ledger path (default: auto-detected from env). Used with --with-drilldowns.",
    )
    args = parser.parse_args(argv)

    registry_path = Path(args.db) if args.db else _default_registry_path()
    ledger_path = Path(args.ledger) if args.ledger else None

    # Import here to keep module-level imports minimal (testable without full env).
    from src.orchestration.registry import Registry

    registry = Registry(registry_path)
    data = build_dashboard_data(registry, registry_path)

    if args.format == "json":
        print(json.dumps(data, indent=2))
    elif args.format == "html":
        drilldown_urls: dict[str, str] = {}
        if args.with_drilldowns:
            # Collect all UoW IDs from active + stalled sections.
            uow_ids = [u["id"] for u in data["active_uows"]] + [
                s["id"] for s in data["stalled_uows"]
            ]
            drilldown_urls = generate_drilldown_urls(
                uow_ids=uow_ids,
                db_path=registry_path,
                ledger_path=ledger_path,
            )
        print(render_html(data, drilldown_urls=drilldown_urls), end="")
    else:
        print(render_text(data), end="")

    return 0


if __name__ == "__main__":
    sys.exit(main())
