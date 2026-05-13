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

With --format html, the dashboard is written to a canonical stable filename
(wos-dashboard-active.html) in the bisque-uploads directory and the public URL is
printed to stdout. This makes the URL stable across regenerations:
  http://<PUBLIC_IP>:9101/files/wos-dashboard-active.html

Exits 0 on success.

Design:
- Pure functions over data; all side effects isolated at the boundary (main).
- Uses Registry.list() and audit_queries for all data access — no raw DB connections
  opened directly in this module; all DB access goes through audit_queries._connect().
- Composable: build_dashboard_data() returns a plain dict usable by all renderers.
- generate_drilldown_urls() is the single composition point for drilldown side effects;
  it maps over UoW IDs and isolates per-UoW errors without aborting the whole batch.
- upload_html() is the single bisque upload point for the dashboard; it writes to a
  canonical filename so the URL is stable across regenerations.
- _fetch_issue_metadata() and _derive_category_from_labels() are pure/near-pure enrichment
  helpers that surface GitHub metadata without mutating the registry. A single gh CLI call
  fetches both title and labels in one round-trip, eliminating the semantic inconsistency
  window that two sequential calls would create.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
# Bisque upload helpers — side-effectful, isolated
# ---------------------------------------------------------------------------

# Canonical filename for the dashboard HTML file in bisque-uploads.
# Using a stable name (not a UUID) makes the URL cross-linkable and bookmarkable.
DASHBOARD_CANONICAL_FILENAME = "wos-dashboard-active.html"


def _uploads_dir() -> Path:
    """Return the bisque-uploads directory path."""
    messages_dir = Path(
        os.environ.get("LOBSTER_INBOX_DIR", str(Path.home() / "messages" / "inbox"))
    ).parent
    return messages_dir / "bisque-uploads"


def _bisque_base_url() -> str:
    """Return the HTTP base URL of the bisque relay server.

    Priority:
    1. BISQUE_RELAY_HTTP_URL env var
    2. LOBSTER_PUBLIC_IP env var with default port 9101
    3. Parse ~/lobster-config/config.env
    4. curl ifconfig.me fallback
    5. localhost last resort
    """
    env_url = os.environ.get("BISQUE_RELAY_HTTP_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")

    public_ip = os.environ.get("LOBSTER_PUBLIC_IP", "").strip()
    if not public_ip:
        config_file = Path.home() / "lobster-config" / "config.env"
        if config_file.exists():
            for line in config_file.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("LOBSTER_PUBLIC_IP="):
                    public_ip = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    break
                if stripped.startswith("BISQUE_RELAY_HTTP_URL="):
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'").rstrip("/")

    if not public_ip:
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "5", "-4", "ifconfig.me"],
                capture_output=True, text=True, timeout=6,
            )
            public_ip = result.stdout.strip()
        except Exception:
            pass

    port = os.environ.get("BISQUE_RELAY_PORT", "9101")
    if public_ip:
        return f"http://{public_ip}:{port}"
    return f"http://localhost:{port}"


def upload_html(html: str) -> str:
    """Write the dashboard HTML to the canonical filename and return its public URL.

    Writes to wos-dashboard-active.html (stable across regenerations).
    Returns the full public URL: {base_url}/files/wos-dashboard-active.html

    Side effects: writes to ~/messages/bisque-uploads/wos-dashboard-active.html
    """
    uploads = _uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    dest = uploads / DASHBOARD_CANONICAL_FILENAME
    dest.write_text(html, encoding="utf-8")
    base_url = _bisque_base_url()
    return f"{base_url}/files/{DASHBOARD_CANONICAL_FILENAME}"


# ---------------------------------------------------------------------------
# GitHub metadata enrichment — near-pure helpers
# ---------------------------------------------------------------------------

def _fetch_issue_metadata(issue_url: str | None) -> dict | None:
    """Fetch title and labels for a GitHub issue in a single gh CLI call.

    Returns {"title": str | None, "labels": list[dict]} on success, or None
    if the URL is absent, the CLI call fails, or the JSON response is malformed.
    Never raises.

    A single combined call (--json title,labels) eliminates the semantic
    inconsistency window that two sequential calls would create, and halves
    the subprocess overhead per UoW.
    """
    if not issue_url:
        return None

    try:
        result = subprocess.run(
            ["gh", "issue", "view", issue_url, "--json", "title,labels"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return {
            "title": data.get("title") or None,
            "labels": data.get("labels") or [],
        }
    except Exception:
        return None


def _derive_category_from_labels(labels: list[dict] | None) -> str:
    """Derive a display category from GitHub labels.

    Taxonomy:
    - type:* → use the value after the colon (e.g. type:bug → "bug",
      type:feat → "feature")
    - workstream:* → use the value after the colon as category (fallback
      when no type: label is present)
    - no matching label → "general"

    type: labels take priority over workstream: labels when both are present.
    """
    if not labels:
        return "general"

    type_label: str | None = None
    workstream_label: str | None = None

    for label in labels:
        name = label.get("name", "")
        if name.startswith("type:") and type_label is None:
            value = name[len("type:"):]
            # Normalize common aliases
            type_label = "feature" if value == "feat" else value
        elif name.startswith("workstream:") and workstream_label is None:
            workstream_label = name[len("workstream:"):]

    if type_label:
        return type_label
    if workstream_label:
        return workstream_label
    return "general"


def _enrich_uow_with_github_metadata(uow_row: dict) -> dict:
    """Fetch issue title and category from GitHub and return an enriched UoW dict.

    Adds 'issue_title' and 'category' keys. Either may be None/default if the
    GitHub call fails (non-fatal — the row renders without enrichment).

    Called per-UoW at render time. Side effect: one subprocess call to gh CLI,
    which fetches title and labels in a single round-trip via _fetch_issue_metadata.
    """
    issue_url = uow_row.get("issue_url")
    if not issue_url:
        return {**uow_row, "issue_title": None, "category": "general"}

    metadata = _fetch_issue_metadata(issue_url)
    if metadata is None:
        return {**uow_row, "issue_title": None, "category": "general"}

    title = metadata["title"]
    category = _derive_category_from_labels(metadata["labels"])

    return {**uow_row, "issue_title": title, "category": category}


# ---------------------------------------------------------------------------
# CC quota widget — pure functions reading ~/.claude/cc-budget/state.json
# ---------------------------------------------------------------------------

# Stale threshold: state older than this is treated as unavailable.
CC_QUOTA_STALE_THRESHOLD_MINUTES = 60

# Color thresholds: <70% green, 70–89% yellow/orange, ≥90% red.
CC_QUOTA_COLOR_GREEN_MAX = 70
CC_QUOTA_COLOR_RED_MIN = 90


def _read_cc_budget_state(state_path: str | None = None) -> dict | None:
    """Read CC budget state from state.json and return the parsed dict, or None.

    Pure read: no side effects beyond file I/O. Returns None when:
    - File is absent or unreadable
    - JSON is malformed
    - Required keys (five_hour_pct, seven_day_pct, fetched_at) are missing

    Path resolution order:
    1. state_path argument (if provided)
    2. LOBSTER_CC_BUDGET_STATE env var
    3. ~/.claude/cc-budget/state.json (default)
    """
    if state_path is None:
        state_path = os.environ.get(
            "LOBSTER_CC_BUDGET_STATE",
            str(Path.home() / ".claude" / "cc-budget" / "state.json"),
        )
    try:
        text = Path(state_path).read_text(encoding="utf-8")
        data = json.loads(text)
        # Validate required keys are present
        if not all(k in data for k in ("five_hour_pct", "seven_day_pct", "fetched_at")):
            return None
        return data
    except Exception:
        return None


def _cc_quota_color(pct: float) -> str:
    """Return a CSS color string for the given usage percentage.

    <70%  → green (#1a7a3c)
    70–89% → orange (#a06000)
    ≥90%  → red (#c0392b)
    """
    if pct >= CC_QUOTA_COLOR_RED_MIN:
        return "#c0392b"
    if pct >= CC_QUOTA_COLOR_GREEN_MAX:
        return "#a06000"
    return "#1a7a3c"


def _format_cc_quota_widget(state: dict | None, now: datetime) -> str:
    """Return an HTML string for the CC quota widget.

    Handles two cases:
    - state is None or stale (>60 min): shows "CC quota: unavailable"
    - state is fresh: shows 5h%, 7d%, and relative data age

    Pure function: no side effects. All inputs are arguments.
    """
    # Determine if state is usable (present and fresh).
    if state is not None:
        try:
            fetched_at_str = state["fetched_at"].replace("Z", "+00:00")
            fetched_at = datetime.fromisoformat(fetched_at_str)
            age_minutes = (now - fetched_at).total_seconds() / 60
            if age_minutes > CC_QUOTA_STALE_THRESHOLD_MINUTES:
                state = None  # treat as unavailable
        except Exception:
            state = None

    if state is None:
        return (
            "<div style='display:inline-flex;align-items:center;gap:8px;"
            "font-size:.8rem;color:var(--text3)'>"
            "<strong>CC quota:</strong> <span>unavailable</span></div>"
        )

    five_pct = state["five_hour_pct"]
    seven_pct = state["seven_day_pct"]

    # Relative age string (e.g. "8m ago")
    age_seconds = int((now - fetched_at).total_seconds())
    if age_seconds < 60:
        age_str = f"{age_seconds}s ago"
    else:
        age_str = f"{age_seconds // 60}m ago"

    five_color = _cc_quota_color(five_pct)
    seven_color = _cc_quota_color(seven_pct)

    return (
        "<div style='display:inline-flex;align-items:center;gap:10px;"
        "font-size:.8rem;flex-wrap:wrap'>"
        f"<strong>CC quota:</strong>"
        f"<span style='color:{five_color};font-weight:600'>5h: {five_pct:.0f}%</span>"
        f"<span style='color:{seven_color};font-weight:600'>7d: {seven_pct:.0f}%</span>"
        f"<span style='color:var(--text3);font-size:.72rem'>as of {age_str}</span>"
        "</div>"
    )


# ---------------------------------------------------------------------------
# Pure data-gathering functions
# ---------------------------------------------------------------------------

def _active_uows(registry: Any) -> list[dict]:
    """Return UoWs in active, ready-for-executor, or executing state.

    Each dict has: id, status, steward_cycles, time_in_state_seconds,
    issue_url (raw, for downstream enrichment).
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
            "issue_url": getattr(uow, "issue_url", None),
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

    cc_quota is included as a pre-rendered HTML string so render_html stays
    a pure function — all state reads and formatting happen here.
    """
    from src.orchestration.analytics import outcome_cost_correlation

    now = datetime.now(timezone.utc)
    cc_state = _read_cc_budget_state()
    return {
        "generated_at": now.isoformat(),
        "active_uows": _active_uows(registry),
        "throughput_24h": _throughput_24h(registry_path),
        "cycle_histogram_7d": _cycle_histogram_last_7d(registry, registry_path),
        "stalled_uows": _stalled_uows(registry),
        "bootup_candidate_gate": _bootup_gate_status(registry),
        "cc_quota": _format_cc_quota_widget(cc_state, now),
        "outcome_cost": outcome_cost_correlation(registry_path),
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
        return f'<td class="uid"><a href="{url}" target="_blank">{uow_id} &#x2197;</a></td>'
    return f'<td class="uid">{uow_id}</td>'


def _active_uow_row(u: dict, drilldown_urls: dict[str, str]) -> str:
    """Render a single active-UoW table row, including title and category badge.

    Columns: UoW ID (linked if drilldown available), issue title (or em-dash),
    status badge + category badge, steward cycle count, time in state.
    """
    title_display = u.get("issue_title") or "—"
    category = u.get("category") or "general"
    id_cell = _uow_id_cell(u["id"], drilldown_urls)
    category_badge = f"<span class='badge bc' style='margin-left:4px'>{category}</span>"

    return (
        f"<tr>"
        f"{id_cell}"
        f"<td style='font-size:.78rem;color:var(--text2)'>{title_display}</td>"
        f"<td><span class='badge {_status_badge_class(u['status'])}'>{u['status']}</span>"
        f"{category_badge}</td>"
        f"<td>{u['steward_cycles']}</td>"
        f"<td>{_fmt_duration(u['time_in_state_seconds'])}</td>"
        f"</tr>"
    )


def render_html(data: dict[str, Any], drilldown_urls: dict[str, str]) -> str:
    """Render the dashboard data as a self-contained HTML page.

    Pure function: no IO. drilldown_urls maps uow_id → public URL for any UoWs
    that have a pre-generated drilldown page. Rows with no entry in drilldown_urls
    show the UoW ID as plain text.

    Each active UoW row now shows:
    - UoW ID (linked if drilldown URL available)
    - Issue title (fetched from GitHub; "—" if absent)
    - Status badge + category badge (derived from GitHub labels)
    - Steward cycle count
    - Time in current state

    The CC quota widget (data["cc_quota"]) is injected near the top of the page,
    alongside the status summary, as a pre-rendered HTML string.
    """
    generated_at = data.get("generated_at", "")
    active = data.get("active_uows", [])
    tp = data.get("throughput_24h", {"completed": 0, "failed": 0})
    hist = data.get("cycle_histogram_7d", {})
    stalls = data.get("stalled_uows", [])
    gate = data.get("bootup_candidate_gate", {})
    cc_quota_html = data.get("cc_quota") or ""

    # --- Active UoWs table ---
    if active:
        active_rows = "\n".join(
            _active_uow_row(u, drilldown_urls)
            for u in active
        )
        active_section = f"""
        <table class="tbl">
          <thead><tr>
            <th>UoW ID</th><th>Title</th><th>Status / Category</th><th>Cycles</th><th>In State</th>
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
{f'<div class="sec" style="padding:10px 14px;margin-bottom:8px">{cc_quota_html}</div>' if cc_quota_html else ''}

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
        # Enrich each active UoW with title and category from GitHub.
        # Errors per-UoW are non-fatal: _enrich_uow_with_github_metadata never raises.
        data["active_uows"] = [
            _enrich_uow_with_github_metadata(u)
            for u in data["active_uows"]
        ]
        html = render_html(data, drilldown_urls=drilldown_urls)
        # Write to canonical filename in bisque-uploads and print the stable URL.
        url = upload_html(html)
        print(url)
    else:
        print(render_text(data), end="")

    return 0


if __name__ == "__main__":
    sys.exit(main())
