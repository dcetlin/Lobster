"""
wos_dashboard_gen.py — Generate a fresh WOS HTML dashboard from live registry data.

Produces an HTML file with embedded JSON data (same structure as the manually-built
v4 dashboard, but generated from the current registry state), then writes it to the
bisque-uploads directory so the bisque relay can serve it.

Entry points:
    generate_and_upload() -> str
        Build the dashboard, write to ~/messages/bisque-uploads/, return public URL.

    _build_data(db_path: Path, ledger_path: Path) -> tuple[dict, dict]
        Pure function: returns (D, CC) dicts for embedding in the HTML.

The public URL is constructed from LOBSTER_PUBLIC_IP (or ifconfig.me fallback) and
the bisque relay port (9101 by default or BISQUE_RELAY_PORT env var).

All DB access uses the canonical REGISTRY_DB path from src.orchestration.paths — the
same resolution chain used by the executor, steward, and analytics modules.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path resolution — all through canonical sources, no inline derivation
# ---------------------------------------------------------------------------

def _registry_path() -> Path:
    from src.orchestration.paths import REGISTRY_DB
    return REGISTRY_DB


def _ledger_path() -> Path:
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", str(Path.home() / "lobster-workspace")))
    return workspace / "data" / "token-ledger.jsonl"


def _uploads_dir() -> Path:
    messages_dir = Path(os.environ.get("LOBSTER_INBOX_DIR", str(Path.home() / "messages" / "inbox"))).parent
    return messages_dir / "bisque-uploads"


def _bisque_base_url() -> str:
    """Return the HTTP base URL of the bisque relay server.

    Priority:
    1. BISQUE_RELAY_HTTP_URL env var
    2. LOBSTER_PUBLIC_IP env var with default port 9101
    3. ifconfig.me curl call (fallback)
    4. localhost fallback
    """
    env_url = os.environ.get("BISQUE_RELAY_HTTP_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")

    public_ip = os.environ.get("LOBSTER_PUBLIC_IP", "").strip()
    if not public_ip:
        # Try config file
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


# ---------------------------------------------------------------------------
# Data queries — pure functions over SQLite
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _build_registry_data(db_path: Path) -> dict[str, Any]:
    """Build the D dict: UoW registry data for the dashboard."""
    conn = _connect(db_path)
    try:
        now_iso = datetime.now(timezone.utc).isoformat()

        # Status counts
        rows = conn.execute(
            "SELECT status, count(*) as cnt FROM uow_registry GROUP BY status ORDER BY cnt DESC"
        ).fetchall()
        status_counts = [{"status": r["status"], "cnt": r["cnt"]} for r in rows]
        total_uows = sum(r["cnt"] for r in rows)

        # Outcome category counts
        rows = conn.execute(
            "SELECT outcome_category, count(*) as cnt FROM uow_registry GROUP BY outcome_category ORDER BY cnt DESC"
        ).fetchall()
        outcome_counts = [{"outcome_category": r["outcome_category"], "cnt": r["cnt"]} for r in rows]

        done_cnt_row = conn.execute(
            "SELECT count(*) FROM uow_registry WHERE status IN ('done','closed','expired','cancelled','failed')"
        ).fetchone()
        done_cnt = done_cnt_row[0] if done_cnt_row else 0

        active_cnt_row = conn.execute(
            "SELECT count(*) FROM uow_registry WHERE status IN ('ready-for-steward','ready-for-executor','active','executing','proposed','pending','needs-human-review','blocked','diagnosing')"
        ).fetchone()
        active_cnt = active_cnt_row[0] if active_cnt_row else 0

        # Total audit events
        try:
            total_audit_row = conn.execute("SELECT count(*) FROM audit_log").fetchone()
            total_audit = total_audit_row[0] if total_audit_row else 0
        except sqlite3.OperationalError:
            total_audit = 0

        # Total corrective traces
        try:
            total_traces_row = conn.execute("SELECT count(*) FROM corrective_traces").fetchone()
            total_traces = total_traces_row[0] if total_traces_row else 0
        except sqlite3.OperationalError:
            total_traces = 0

        # Dispatch and execution stats from audit_log
        dispatch_count = 0
        exec_fail_count = 0
        try:
            dc_row = conn.execute(
                "SELECT count(*) FROM audit_log WHERE event = 'dispatched'"
            ).fetchone()
            dispatch_count = dc_row[0] if dc_row else 0
            ef_row = conn.execute(
                "SELECT count(*) FROM audit_log WHERE event IN ('execution_failed','failed')"
            ).fetchone()
            exec_fail_count = ef_row[0] if ef_row else 0
        except sqlite3.OperationalError:
            pass

        # Date range
        dr_row = conn.execute(
            "SELECT min(created_at) as min_dt, max(created_at) as max_dt FROM uow_registry"
        ).fetchone()
        date_range = {
            "min_dt": dr_row["min_dt"] if dr_row else None,
            "max_dt": dr_row["max_dt"] if dr_row else None,
        }

        # Weekly creation counts (last 8 weeks)
        rows = conn.execute("""
            SELECT strftime('%Y-W%W', created_at) as week, count(*) as cnt
            FROM uow_registry
            WHERE created_at >= datetime('now', '-56 days')
            GROUP BY week
            ORDER BY week
        """).fetchall()
        weekly = [{"week": r["week"], "cnt": r["cnt"]} for r in rows]

        # All UoWs (for the queue tab — pull all fields)
        uow_rows = conn.execute("""
            SELECT
                id, summary, status, created_at, updated_at, completed_at,
                execution_attempts, outcome_category, source_issue_number,
                lifetime_cycles, retry_count, posture, parent, register, type,
                source, artifacts, steward_cycles, close_reason
            FROM uow_registry
            ORDER BY created_at DESC
        """).fetchall()

        # Build issue URL for each UoW
        def _issue_url(row: sqlite3.Row) -> str | None:
            num = row["source_issue_number"]
            if num:
                return f"https://github.com/dcetlin/Lobster/issues/{num}"
            return None

        all_uows = []
        for row in uow_rows:
            all_uows.append({
                "id": row["id"],
                "summary": row["summary"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "completed_at": row["completed_at"],
                "execution_attempts": row["execution_attempts"] or 0,
                "outcome_category": row["outcome_category"],
                "source_issue_number": row["source_issue_number"],
                "issue_url": _issue_url(row),
                "lifetime_cycles": row["lifetime_cycles"] or 0,
                "retry_count": row["retry_count"] or 0,
                "posture": row["posture"],
                "parent": row["parent"],
                "register": row["register"],
                "type": row["type"],
                "source": row["source"],
                "steward_cycles": row["steward_cycles"] or 0,
                "close_reason": row["close_reason"],
                # Token fields (None if no ledger join done)
                "lo": None, "li": None, "lcr": None, "lcw": None, "lc": None, "ec": None,
            })

        # Audit trail for recent 50 UoWs (for drilldown)
        audit_by_uow: dict[str, list] = {}
        recent_ids = [u["id"] for u in all_uows[:50]]
        if recent_ids:
            try:
                placeholders = ",".join("?" * len(recent_ids))
                audit_rows = conn.execute(
                    f"SELECT uow_id, event, from_status, to_status, note, ts FROM audit_log "
                    f"WHERE uow_id IN ({placeholders}) ORDER BY ts ASC",
                    recent_ids,
                ).fetchall()
                for ar in audit_rows:
                    uid = ar["uow_id"]
                    if uid not in audit_by_uow:
                        audit_by_uow[uid] = []
                    audit_by_uow[uid].append({
                        "event": ar["event"],
                        "from": ar["from_status"],
                        "to": ar["to_status"],
                        "note": ar["note"],
                        "ts": ar["ts"],
                    })
            except sqlite3.OperationalError:
                pass

        # Corrective traces for recent 50 UoWs
        traces_by_uow: dict[str, list] = {}
        if recent_ids:
            try:
                placeholders = ",".join("?" * len(recent_ids))
                trace_rows = conn.execute(
                    f"SELECT uow_id, gate_score, summary, created_at FROM corrective_traces "
                    f"WHERE uow_id IN ({placeholders}) ORDER BY created_at DESC",
                    recent_ids,
                ).fetchall()
                for tr in trace_rows:
                    uid = tr["uow_id"]
                    if uid not in traces_by_uow:
                        traces_by_uow[uid] = []
                    traces_by_uow[uid].append({
                        "gate_score": tr["gate_score"],
                        "summary": tr["summary"],
                        "created_at": tr["created_at"],
                    })
            except sqlite3.OperationalError:
                pass

        return {
            "generated_at": now_iso,
            "status_counts": status_counts,
            "outcome_counts": outcome_counts,
            "total_uows": total_uows,
            "done_cnt": done_cnt,
            "active_cnt": active_cnt,
            "total_audit": total_audit,
            "total_traces": total_traces,
            "dispatch_count": dispatch_count,
            "exec_fail_count": exec_fail_count,
            "date_range": date_range,
            "weekly": weekly,
            "all_uows": all_uows,
            "audit_by_uow": audit_by_uow,
            "traces_by_uow": traces_by_uow,
        }
    finally:
        conn.close()


def _build_cc_data(ledger_path: Path) -> dict[str, Any]:
    """Build the CC dict: Claude Code usage data from token-ledger.jsonl.

    Reads ledger entries from the last 7 days for daily chart and per-UoW data.
    All-time totals cover the full ledger.
    """
    if not ledger_path.exists():
        return {
            "all_time": {"calls": 0, "output": 0, "cache_read": 0, "input": 0, "est_cost_usd": 0},
            "daily_chart": [],
            "model_breakdown": [],
            "uow_top20_by_output": [],
            "stale": True,
            "stale_note": f"Token ledger not found at {ledger_path}",
        }

    # Parse ledger — it can be large so read it in a streaming fashion
    all_entries: list[dict] = []
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff_ts = cutoff_7d.timestamp()

    try:
        with ledger_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    all_entries.append(entry)
                except json.JSONDecodeError:
                    pass
    except OSError:
        return {
            "all_time": {"calls": 0, "output": 0, "cache_read": 0, "input": 0, "est_cost_usd": 0},
            "daily_chart": [],
            "model_breakdown": [],
            "uow_top20_by_output": [],
            "stale": True,
            "stale_note": f"Could not read token ledger: {ledger_path}",
        }

    if not all_entries:
        return {
            "all_time": {"calls": 0, "output": 0, "cache_read": 0, "input": 0, "est_cost_usd": 0},
            "daily_chart": [],
            "model_breakdown": [],
            "uow_top20_by_output": [],
            "stale": False,
        }

    # All-time totals
    total_calls = len(all_entries)
    total_output = sum(e.get("output", 0) for e in all_entries)
    total_cache_read = sum(e.get("cache_read", 0) for e in all_entries)
    total_input = sum(e.get("input", 0) for e in all_entries)
    # Sonnet 4.6 pricing: $3/1M input, $15/1M output, $0.30/1M cache_read
    est_cost = (total_input * 3 + total_output * 15) / 1_000_000 + total_cache_read * 0.30 / 1_000_000

    # Daily chart — last 7 days
    recent_entries = [e for e in all_entries if e.get("ts", 0) >= cutoff_ts]
    daily: dict[str, dict] = {}
    for e in recent_entries:
        ts = e.get("ts", 0)
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if day not in daily:
            daily[day] = {"day": day, "output": 0, "calls": 0}
        daily[day]["output"] += e.get("output", 0)
        daily[day]["calls"] += 1
    daily_chart = sorted(daily.values(), key=lambda x: x["day"])

    # Model breakdown (all-time)
    model_counts: dict[str, int] = {}
    for e in all_entries:
        model = e.get("model", "unknown") or "unknown"
        model_counts[model] = model_counts.get(model, 0) + 1
    model_breakdown = [
        {"model": m, "calls": c}
        for m, c in sorted(model_counts.items(), key=lambda x: -x[1])
    ]

    # Per-UoW token data — join by task_id matching uow_YYYYMMDD_xxxxxx pattern
    import re
    uow_pattern = re.compile(r"uow_\d{8}_[0-9a-f]+")
    uow_tokens: dict[str, dict] = {}
    for e in all_entries:
        task_id = e.get("task_id", "") or ""
        # Extract uow_id from task_id (format: "wos-uow_YYYYMMDD_xxxxxx" or "uow_YYYYMMDD_xxxxxx")
        match = uow_pattern.search(task_id)
        if match:
            uid = match.group(0)
            if uid not in uow_tokens:
                uow_tokens[uid] = {"output": 0, "input": 0, "cache_read": 0, "cache_write": 0, "calls": 0, "est_cost": 0}
            uow_tokens[uid]["output"] += e.get("output", 0)
            uow_tokens[uid]["input"] += e.get("input", 0)
            uow_tokens[uid]["cache_read"] += e.get("cache_read", 0)
            uow_tokens[uid]["cache_write"] += e.get("cache_write", 0)
            uow_tokens[uid]["calls"] += 1
            uow_tokens[uid]["est_cost"] += (
                e.get("input", 0) * 3 + e.get("output", 0) * 15
            ) / 1_000_000 + e.get("cache_read", 0) * 0.30 / 1_000_000

    # Top 20 UoWs by output tokens
    top20 = sorted(uow_tokens.items(), key=lambda x: -x[1]["output"])[:20]
    uow_top20 = [
        {
            "id": uid,
            "summary": "",  # filled in by caller if needed
            "output": data["output"],
            "input": data["input"],
            "cache_read": data["cache_read"],
            "est_cost": round(data["est_cost"], 4),
        }
        for uid, data in top20
    ]

    return {
        "all_time": {
            "calls": total_calls,
            "output": total_output,
            "cache_read": total_cache_read,
            "input": total_input,
            "est_cost_usd": round(est_cost, 2),
        },
        "daily_chart": daily_chart,
        "model_breakdown": model_breakdown,
        "uow_top20_by_output": uow_top20,
        "stale": False,
        "uow_tokens": {uid: data for uid, data in uow_tokens.items()},
    }


def _enrich_uow_tokens(d_data: dict, cc_data: dict) -> None:
    """Mutate d_data['all_uows'] in-place to attach token data from cc_data."""
    uow_tokens = cc_data.get("uow_tokens", {})
    if not uow_tokens:
        return
    for uow in d_data["all_uows"]:
        tok = uow_tokens.get(uow["id"])
        if tok:
            uow["lo"] = tok.get("output")
            uow["li"] = tok.get("input")
            uow["lcr"] = tok.get("cache_read")
            uow["lcw"] = tok.get("cache_write")
            uow["lc"] = tok.get("calls")
            # Estimate cost
            out = tok.get("output", 0) or 0
            inp = tok.get("input", 0) or 0
            cr = tok.get("cache_read", 0) or 0
            uow["ec"] = f"${round((inp * 3 + out * 15) / 1_000_000 + cr * 0.30 / 1_000_000, 4)}"


def _build_data(
    db_path: Path | None = None,
    ledger_path: Path | None = None,
) -> tuple[dict, dict]:
    """Build (D, CC) dicts from live data. Pure except for IO."""
    if db_path is None:
        db_path = _registry_path()
    if ledger_path is None:
        ledger_path = _ledger_path()

    d_data = _build_registry_data(db_path)
    cc_data = _build_cc_data(ledger_path)
    _enrich_uow_tokens(d_data, cc_data)
    return d_data, cc_data


# ---------------------------------------------------------------------------
# HTML template — same structure as v4 but with live data injection points
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WOS Dashboard (live)</title>
<style>
:root{{--bg:#f5f5f5;--surface:#fff;--surface2:#f0f0f0;--border:#ddd;--text:#1a1a1a;--text2:#555;--text3:#888;--accent:#4a7fc0;--accent-light:#e8f0fa;--done-col:#1a7a3c;--done-bg:#d4f7e0;--pend-col:#a06000;--pend-bg:#fef3cd;--fail-col:#c0392b;--fail-bg:#fde8e6;--act-col:#1565c0;--act-bg:#e3f0ff;--cl-col:#666;--cl-bg:#eee;--seed-col:#166534;--seed-bg:#dcfce7;--pearl-col:#5b21b6;--pearl-bg:#ede9fe;--heat-col:#b45309;--heat-bg:#fef3c7;--shadow:0 1px 4px rgba(0,0,0,.08)}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1117;--surface:#1c1f2a;--surface2:#252837;--border:#333;--text:#e8e8e8;--text2:#aaa;--text3:#666;--accent:#6fa3e0;--accent-light:#1a2540;--done-col:#4ade80;--done-bg:#0a2e1a;--pend-col:#fbbf24;--pend-bg:#2a1e00;--fail-col:#f87171;--fail-bg:#2a0a0a;--act-col:#60a5fa;--act-bg:#0a1e3a;--cl-col:#888;--cl-bg:#252525;--seed-col:#86efac;--seed-bg:#0a2a14;--pearl-col:#c4b5fd;--pearl-bg:#1e1040;--heat-col:#fcd34d;--heat-bg:#2a1a00;--shadow:0 1px 4px rgba(0,0,0,.4)}}}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);line-height:1.5}}
.wrap{{max-width:1200px;margin:0 auto;padding:16px}}
h1{{font-size:1.5rem;font-weight:700;margin-bottom:4px}}
h2{{font-size:1rem;font-weight:600;margin-bottom:12px;color:var(--text2);text-transform:uppercase;letter-spacing:.05em}}
.meta{{color:var(--text3);font-size:.78rem;margin-bottom:16px}}
.sec{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:14px;box-shadow:var(--shadow)}}
.pgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:12px}}
.scard{{background:var(--surface2);border-radius:8px;padding:10px 12px;text-align:center}}
.scard .n{{font-size:1.7rem;font-weight:700;color:var(--accent)}}
.scard .l{{font-size:.68rem;color:var(--text3);text-transform:uppercase;letter-spacing:.04em}}
.bar{{height:8px;border-radius:4px;overflow:hidden;display:flex;margin-bottom:12px}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.7rem;font-weight:600;white-space:nowrap}}
.bd{{color:var(--done-col);background:var(--done-bg)}}
.bp{{color:var(--pend-col);background:var(--pend-bg)}}
.bf{{color:var(--fail-col);background:var(--fail-bg)}}
.ba{{color:var(--act-col);background:var(--act-bg)}}
.bc{{color:var(--cl-col);background:var(--cl-bg)}}
.bs{{color:var(--seed-col);background:var(--seed-bg)}}
.bpe{{color:var(--pearl-col);background:var(--pearl-bg)}}
.bh{{color:var(--heat-col);background:var(--heat-bg)}}
.tbl{{width:100%;border-collapse:collapse;font-size:.8rem}}
.tbl th{{text-align:left;padding:5px 8px;color:var(--text3);font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border)}}
.tbl td{{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top}}
.tbl tr:last-child td{{border-bottom:none}}
.urow{{cursor:pointer;transition:background .1s}}
.urow:hover{{background:var(--accent-light)}}
.urow.exp{{background:var(--accent-light)}}
.uid{{font-family:monospace;font-size:.68rem;color:var(--text3);white-space:nowrap}}
.gh{{color:var(--accent);text-decoration:none;font-size:.72rem}}
.gh:hover{{text-decoration:underline}}
.drow td{{padding:0}}
.dpanel{{max-height:0;overflow:hidden;transition:max-height .3s ease}}
.dpanel.open{{max-height:900px;overflow-y:auto}}
.dinner{{padding:12px 16px;background:var(--surface2);border-top:1px solid var(--border)}}
.dsec{{margin-bottom:10px}}
.dsec label{{font-size:.68rem;font-weight:600;text-transform:uppercase;color:var(--text3);letter-spacing:.04em;display:block;margin-bottom:3px}}
.tl{{display:flex;flex-direction:column;gap:3px}}
.tli{{display:flex;gap:8px;font-size:.75rem}}
.tlts{{color:var(--text3);white-space:nowrap;min-width:115px}}
.tlfr{{color:var(--fail-col)}}
.tlto{{color:var(--done-col)}}
.tlsym{{color:var(--text3)}}
.sumtxt{{font-size:.83rem;background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:7px 10px}}
.dgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:7px}}
.dkv .k{{color:var(--text3);font-size:.67rem;text-transform:uppercase}}
.dkv .v{{color:var(--text);font-weight:500;font-size:.78rem}}
.empty{{text-align:center;padding:36px 20px;color:var(--text3);font-size:.88rem}}
.tabs{{display:flex;gap:4px;margin-bottom:14px;flex-wrap:wrap}}
.tab{{padding:5px 13px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--text2);font-size:.8rem;cursor:pointer;font-weight:500;transition:all .12s}}
.tab.act{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.tab:hover:not(.act){{background:var(--accent-light)}}
.tc{{display:none}}
.tc.act{{display:block}}
.fbar{{display:flex;gap:8px;margin-bottom:11px;flex-wrap:wrap;align-items:center}}
.fi{{padding:5px 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface2);color:var(--text);font-size:.8rem;min-width:190px}}
.fs{{padding:5px 8px;border:1px solid var(--border);border-radius:6px;background:var(--surface2);color:var(--text);font-size:.8rem}}
.fc{{font-size:.75rem;color:var(--text3);margin-left:auto}}
.chev{{display:inline-block;transition:transform .18s;margin-left:3px;font-size:.6rem;color:var(--text3)}}
.exp .chev{{transform:rotate(180deg)}}
.tok-col{{font-size:.7rem;color:var(--text2);white-space:nowrap;font-family:monospace}}
.tok-none{{color:var(--text3);font-size:.68rem}}
@media(max-width:600px){{.pgrid{{grid-template-columns:repeat(2,1fr)}}.tbl th:nth-child(4),.tbl td:nth-child(4),.tbl th:nth-child(5),.tbl td:nth-child(5){{display:none}}}}
</style>
</head>
<body>
<div class="wrap">
<h1>WOS Dashboard <span style="font-size:.9rem;opacity:.6">live</span></h1>
<p class="meta" id="gen-meta"></p>

<div class="tabs">
  <button class="tab act" onclick="switchTab('queue',this)">Queue</button>
  <button class="tab" onclick="switchTab('seeds',this)">Seeds</button>
  <button class="tab" onclick="switchTab('pearls',this)">Pearls</button>
  <button class="tab" onclick="switchTab('usage',this)">Usage Stats</button>
</div>

<div class="tc act" id="tc-queue">
<div class="sec">
<div class="pgrid" id="stat-cards"></div>
<div class="bar" id="status-bar"></div>
<div class="fbar">
  <input class="fi" id="q-search" placeholder="Search summary, ID, status..." oninput="filterQ()">
  <select class="fs" id="q-status" onchange="filterQ()">
    <option value="">All statuses</option>
    <option value="ready-for-steward">ready-for-steward</option>
    <option value="proposed">proposed</option>
    <option value="needs-human-review">needs-human-review</option>
    <option value="done">done</option>
    <option value="closed">closed</option>
    <option value="expired">expired</option>
    <option value="cancelled">cancelled</option>
    <option value="failed">failed</option>
  </select>
  <select class="fs" id="q-tok" onchange="filterQ()">
    <option value="">Any token data</option>
    <option value="has">Has token data</option>
    <option value="none">No token data</option>
  </select>
  <span class="fc" id="q-count"></span>
</div>
<table class="tbl">
<thead><tr><th>ID</th><th>Summary</th><th>Status</th><th>Created</th><th>Tokens (out)</th><th>GH</th></tr></thead>
<tbody id="q-body"></tbody>
</table>
</div>
</div>

<div class="tc" id="tc-seeds">
<div class="sec">
<h2>Seeds</h2>
<div class="empty" id="seeds-grid">Loading...</div>
</div>
</div>

<div class="tc" id="tc-pearls">
<div class="sec">
<h2>Pearls</h2>
<div class="empty" id="pearls-grid">Loading...</div>
</div>
</div>

<div class="tc" id="tc-usage">
<div class="sec">
<h2>Usage Overview</h2>
<div class="pgrid" id="usage-cards"></div>
<div style="margin-top:10px">
  <div style="font-size:.7rem;color:var(--text3);margin-bottom:4px;font-weight:600">OUTPUT TOKENS BY DAY (LAST 7 DAYS)</div>
  <div style="display:flex;align-items:flex-end;gap:3px;height:50px;" id="cc-trend"></div>
</div>
</div>
<div class="sec">
<h2>All-time Token Totals</h2>
<div class="pgrid" id="alltime-cards"></div>
</div>
<div class="sec">
<h2>Top UoWs by Output Tokens</h2>
<div id="tok-chart-container"><div class="empty">Loading...</div></div>
</div>
</div>

</div>
<script>
const D={D_DATA};
const CC={CC_DATA};

// Format helpers
function fmt(n){{return n==null?'—':n.toLocaleString();}}
function fmtDate(s){{if(!s)return'—';try{{return new Date(s).toISOString().slice(0,10);}}catch{{return s.slice(0,10);}}}}
function statusBadge(s){{
  const m={{
    'done':'bd','closed':'bc','expired':'bc','cancelled':'bc',
    'failed':'bf','ready-for-steward':'ba','ready-for-executor':'ba',
    'active':'ba','executing':'ba','proposed':'bp',
    'needs-human-review':'bf','blocked':'bf',
  }};
  return `<span class="badge ${{m[s]||'bc'}}">${{s}}</span>`;
}}
function outcomeBadge(o){{
  if(!o)return'';
  const m={{'seed':'bs','pearl':'bpe','heat':'bh'}};
  return `<span class="badge ${{m[o]||'bc'}}">${{o}}</span>`;
}}

// Meta line
(function(){{
  const el=document.getElementById('gen-meta');
  const d=new Date(D.generated_at);
  el.textContent=`Generated ${{d.toUTCString()}} — ${{D.total_uows.toLocaleString()}} UoWs`;
}})();

// Stat cards and status bar
(function(){{
  const sc=document.getElementById('stat-cards');
  const bar=document.getElementById('status-bar');
  const active=D.status_counts.filter(s=>['ready-for-steward','ready-for-executor','active','executing','proposed','pending','needs-human-review','blocked','diagnosing'].includes(s.status));
  const done=D.status_counts.filter(s=>s.status==='done').reduce((a,b)=>a+b.cnt,0);
  const closed=D.status_counts.filter(s=>s.status==='closed').reduce((a,b)=>a+b.cnt,0);
  const failed=D.status_counts.filter(s=>s.status==='failed').reduce((a,b)=>a+b.cnt,0);
  const total=D.total_uows;
  sc.innerHTML=`
    <div class="scard"><div class="n">${{total.toLocaleString()}}</div><div class="l">Total UoWs</div></div>
    <div class="scard"><div class="n">${{D.active_cnt.toLocaleString()}}</div><div class="l">Active/Queued</div></div>
    <div class="scard"><div class="n">${{done.toLocaleString()}}</div><div class="l">Done</div></div>
    <div class="scard"><div class="n">${{closed.toLocaleString()}}</div><div class="l">Closed</div></div>
    <div class="scard"><div class="n">${{failed.toLocaleString()}}</div><div class="l">Failed</div></div>
    <div class="scard"><div class="n">${{D.total_audit.toLocaleString()}}</div><div class="l">Audit Events</div></div>`;
  // Status bar
  const colors={{'done':'#1a7a3c','closed':'#aaa','expired':'#ccc','cancelled':'#ddd','ready-for-steward':'#1565c0','proposed':'#a06000','failed':'#c0392b','needs-human-review':'#8b0000'}};
  const barsHtml=D.status_counts.map(s=>{{
    const pct=(s.cnt/total*100).toFixed(1);
    const col=colors[s.status]||'#999';
    return `<div class="bseg" style="width:${{pct}}%;background:${{col}}" title="${{s.status}}: ${{s.cnt}} (${{pct}}%)"></div>`;
  }}).join('');
  bar.innerHTML=barsHtml;
}})();

// Queue tab
let qFiltered=D.all_uows;
function buildQueue(){{
  const tbody=document.getElementById('q-body');
  const stf=document.getElementById('q-status').value;
  const srch=document.getElementById('q-search').value.toLowerCase();
  const tokf=document.getElementById('q-tok').value;
  qFiltered=D.all_uows.filter(u=>{{
    if(stf && u.status!==stf)return false;
    if(tokf==='has' && u.lo==null)return false;
    if(tokf==='none' && u.lo!=null)return false;
    if(srch){{
      const hay=(u.summary||'')+(u.id||'')+(u.status||'');
      if(!hay.toLowerCase().includes(srch))return false;
    }}
    return true;
  }});
  document.getElementById('q-count').textContent=`${{qFiltered.length}} / ${{D.all_uows.length}} UoWs`;
  tbody.innerHTML=qFiltered.map((u,i)=>{{
    const audit=D.audit_by_uow[u.id]||[];
    const tokCell=u.lo!=null?`<span class="tok-col">${{fmt(u.lo)}} out</span>`:`<span class="tok-none">—</span>`;
    const ghCell=u.issue_url?`<a class="gh" href="${{u.issue_url}}" target="_blank">#${{u.source_issue_number||'?'}}</a>`:'';
    return `<tr class="urow" id="urow-${{i}}" onclick="toggleRow(${{i}})">
      <td><span class="uid">${{u.id}}</span></td>
      <td>${{(u.summary||'').slice(0,90)}}${{(u.summary||'').length>90?'…':''}} <span class="chev" id="chev-${{i}}">▼</span></td>
      <td>${{statusBadge(u.status)}} ${{outcomeBadge(u.outcome_category)}}</td>
      <td style="font-size:.72rem;color:var(--text3);white-space:nowrap">${{fmtDate(u.created_at)}}</td>
      <td>${{tokCell}}</td>
      <td>${{ghCell}}</td>
    </tr>
    <tr class="drow" id="drow-${{i}}"><td colspan="6"><div class="dpanel" id="dp-${{i}}"><div class="dinner">
      <div class="dsec"><div class="sumtxt">${{u.summary||'(no summary)'}}</div></div>
      ${{u.lo!=null?`<div class="dsec"><label>Token Usage</label><div class="dgrid">
        <div class="dkv"><div class="k">Output</div><div class="v">${{fmt(u.lo)}}</div></div>
        <div class="dkv"><div class="k">Input</div><div class="v">${{fmt(u.li)}}</div></div>
        <div class="dkv"><div class="k">Cache Read</div><div class="v">${{fmt(u.lcr)}}</div></div>
        <div class="dkv"><div class="k">API Calls</div><div class="v">${{u.lc||0}}</div></div>
        <div class="dkv"><div class="k">Est. Cost</div><div class="v">${{u.ec||'n/a'}}</div></div>
      </div></div>`:'<div class="dsec"><label>Token Usage</label><span style="font-size:.75rem;color:var(--text3)">No token data</span></div>'}}
      <div class="dgrid" style="margin-bottom:10px">
        <div class="dkv"><div class="k">Status</div><div class="v">${{u.status}}</div></div>
        <div class="dkv"><div class="k">Register</div><div class="v">${{u.register||'—'}}</div></div>
        <div class="dkv"><div class="k">Posture</div><div class="v">${{u.posture||'—'}}</div></div>
        <div class="dkv"><div class="k">Exec Attempts</div><div class="v">${{u.execution_attempts||0}}</div></div>
        <div class="dkv"><div class="k">Steward Cycles</div><div class="v">${{u.steward_cycles||0}}</div></div>
        <div class="dkv"><div class="k">Outcome</div><div class="v">${{u.outcome_category||'—'}}</div></div>
        <div class="dkv"><div class="k">Created</div><div class="v">${{fmtDate(u.created_at)}}</div></div>
        <div class="dkv"><div class="k">Updated</div><div class="v">${{fmtDate(u.updated_at)}}</div></div>
      </div>
      ${{audit.length?`<div class="dsec"><label>Audit Trail</label><div class="tl">${{audit.map(a=>{{
        const sym=a.event==='dispatched'?'→':a.event==='completed'?'✓':a.event==='failed'?'✗':'·';
        const cls=a.event==='failed'?'tlfr':a.event==='completed'?'tlto':'tlsym';
        return `<div class="tli"><span class="tlts">${{a.ts?a.ts.slice(0,16):''}}</span><span class="${{cls}}">${{sym}}</span><span>${{a.event}}${{a.from?` ${{a.from}}→${{a.to}}`:''}}</span></div>`;
      }}).join('')}}</div></div>`:'<div class="dsec"><label>Audit Trail</label><div class="empty" style="padding:8px">No audit events (only loaded for most recent 50 UoWs)</div></div>'}}
      ${{u.issue_url?`<div><a class="gh" href="${{u.issue_url}}" target="_blank">GitHub Issue #${{u.source_issue_number}}</a></div>`:''}}
    </div></div></td></tr>`;
  }}).join('');
}}
function filterQ(){{buildQueue();}}
function toggleRow(i){{
  const panel=document.getElementById('dp-'+i);
  const row=document.getElementById('urow-'+i);
  const open=panel.classList.toggle('open');
  row.classList.toggle('exp',open);
}}
buildQueue();

// Seeds & Pearls
(function(){{
  function buildOutcome(id, outcome){{
    const el=document.getElementById(id);
    const items=D.all_uows.filter(u=>u.outcome_category===outcome);
    if(!items.length){{el.innerHTML='<div class="empty">None found</div>';return;}}
    el.innerHTML=items.map(u=>`<div class="sec" style="margin-bottom:8px">
      <div style="font-size:.83rem;font-weight:600">${{(u.summary||'').slice(0,100)}}</div>
      <div style="font-size:.72rem;color:var(--text3)">${{fmtDate(u.created_at)}} · ${{u.status}}</div>
      ${{u.issue_url?`<a class="gh" href="${{u.issue_url}}" target="_blank">#${{u.source_issue_number}}</a>`:''}}
      ${{outcomeBadge(u.outcome_category)}}
    </div>`).join('');
  }}
  buildOutcome('seeds-grid','seed');
  buildOutcome('pearls-grid','pearl');
}})();

// Usage stats
(function(){{
  const uc=document.getElementById('usage-cards');
  const total=D.total_uows;
  const comp_rate=total>0?Math.round(D.done_cnt/total*100):0;
  const exec_rate=D.dispatch_count>0?Math.round(D.exec_fail_count/D.dispatch_count*100):0;
  const seeds=D.all_uows.filter(u=>u.outcome_category==='seed').length;
  const pearls=D.all_uows.filter(u=>u.outcome_category==='pearl').length;
  uc.innerHTML=`
    <div class="scard"><div class="n">${{comp_rate}}%</div><div class="l">Terminal Rate</div></div>
    <div class="scard"><div class="n">${{exec_rate}}%</div><div class="l">Exec Fail Rate</div></div>
    <div class="scard"><div class="n">${{D.total_audit.toLocaleString()}}</div><div class="l">Audit Events</div></div>
    <div class="scard"><div class="n">${{seeds}}</div><div class="l">Seeds</div></div>
    <div class="scard"><div class="n">${{pearls}}</div><div class="l">Pearls</div></div>`;

  // Daily trend
  const trend=document.getElementById('cc-trend');
  const chart=CC.daily_chart||[];
  const maxOut=Math.max(...chart.map(d=>d.output),1);
  trend.innerHTML=chart.map(d=>{{
    const h=Math.max(Math.round(d.output/maxOut*46),1);
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;min-width:8px">
      <div style="width:100%;height:${{h}}px;background:var(--accent);border-radius:2px 2px 0 0" title="${{d.day}}: ${{d.output.toLocaleString()}} output tokens, ${{d.calls}} calls"></div>
      <div style="font-size:.5rem;color:var(--text3);transform:rotate(-45deg);white-space:nowrap">${{d.day.slice(5)}}</div>
    </div>`;
  }}).join('');

  // All-time totals
  const at=CC.all_time||{{}};
  const atc=document.getElementById('alltime-cards');
  atc.innerHTML=`
    <div class="scard"><div class="n">${{fmt(at.calls)}}</div><div class="l">API Calls</div></div>
    <div class="scard"><div class="n">${{fmt(at.output)}}</div><div class="l">Output Tokens</div></div>
    <div class="scard"><div class="n">${{fmt(at.cache_read)}}</div><div class="l">Cache Read Tokens</div></div>
    <div class="scard"><div class="n">$${{at.est_cost_usd}}</div><div class="l">Est. Cost (USD)</div></div>`;

  // Top UoWs by tokens
  const container=document.getElementById('tok-chart-container');
  const top20=CC.uow_top20_by_output||[];
  if(!top20.length){{container.innerHTML='<div class="empty">No UoW token data found in ledger</div>';return;}}
  const maxTok=Math.max(...top20.map(u=>u.output),1);
  container.innerHTML=`<div style="display:flex;flex-direction:column;gap:6px">`+top20.map(u=>{{
    const pct=Math.round(u.output/maxTok*100);
    const isExp=u.output>5000;
    const cost=u.est_cost>0?` · $${{u.est_cost.toFixed(4)}}`:'';
    return `<div style="display:grid;grid-template-columns:180px 1fr 80px;gap:8px;align-items:center;font-size:.72rem">
      <div style="font-family:monospace;font-size:.65rem;color:var(--text3);overflow:hidden;text-overflow:ellipsis">${{u.id}}</div>
      <div style="background:var(--surface2);border-radius:3px;height:14px;overflow:hidden">
        <div style="height:100%;width:${{pct}}%;background:${{isExp?'#ef4444':'var(--accent)'}};border-radius:3px"></div>
      </div>
      <div style="text-align:right;color:var(--text2);font-family:monospace">${{fmt(u.output)}}${{cost}}</div>
    </div>`;
  }}).join('')+`</div>`;
}})();

function switchTab(id,btn){{
  document.querySelectorAll('.tc').forEach(t=>t.classList.remove('act'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('act'));
  document.getElementById('tc-'+id).classList.add('act');
  btn.classList.add('act');
}}
</script>
</body>
</html>
"""


def generate_html(d_data: dict, cc_data: dict) -> str:
    """Render the HTML dashboard with embedded JSON data."""
    # Strip non-serializable keys from cc_data before embedding
    cc_embed = {k: v for k, v in cc_data.items() if k != "uow_tokens"}
    d_json = json.dumps(d_data, ensure_ascii=False, separators=(",", ":"))
    cc_json = json.dumps(cc_embed, ensure_ascii=False, separators=(",", ":"))
    return _HTML_TEMPLATE.replace("{D_DATA}", d_json).replace("{CC_DATA}", cc_json)


def generate_and_upload(
    db_path: Path | None = None,
    ledger_path: Path | None = None,
) -> str:
    """Generate a fresh dashboard HTML file and return its public URL.

    Writes to ~/messages/bisque-uploads/ (the bisque relay's upload directory).
    Returns the full public URL, e.g. http://5.78.201.64:9101/files/<uuid>.html.
    """
    d_data, cc_data = _build_data(db_path, ledger_path)
    html = generate_html(d_data, cc_data)

    uploads = _uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.html"
    dest = uploads / filename
    dest.write_text(html, encoding="utf-8")

    base_url = _bisque_base_url()
    return f"{base_url}/files/{filename}"


if __name__ == "__main__":
    url = generate_and_upload()
    print(url)
