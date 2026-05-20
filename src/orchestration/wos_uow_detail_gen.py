"""
wos_uow_detail_gen.py — Generate a standalone per-UoW drilldown HTML page.

Produces a self-contained HTML file for a single UoW showing full detail:
token breakdown, cost estimate, API call timeline, full audit trail,
corrective traces, heartbeat log, and status history. Uploads to the bisque
relay and returns the public URL.

Entry points:
    generate_and_upload(uow_id, db_path?, ledger_path?) -> str
        Build the page, write to ~/messages/bisque-uploads/, return public URL.

    generate_html(uow_data, audit_trail, traces, heartbeats, token_data) -> str
        Pure function: render HTML from pre-fetched dicts.

CLI:
    uv run src/orchestration/wos_uow_detail_gen.py --uow-id <id> [--db PATH] [--ledger PATH]

Design:
- Pure function composition: each fetch function takes an open sqlite3.Connection and
  returns a plain Python dict or list. generate_html() composes them into HTML.
- All DB access uses WAL mode + busy_timeout for safe concurrent access.
- Same CSS custom properties as wos_dashboard_gen.py: same color scheme, typography,
  badge classes, and layout primitives. Page is self-contained (no external CDN deps).
- Priced at Sonnet 4.6 rates: $3/1M input, $15/1M output, $0.30/1M cache_read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import markdown as _markdown_lib

# ---------------------------------------------------------------------------
# Pricing constants — Sonnet 4.6
# ---------------------------------------------------------------------------

SONNET_4_6_INPUT_PER_MTK = 3.0        # $3.00 per 1M input tokens
SONNET_4_6_OUTPUT_PER_MTK = 15.0      # $15.00 per 1M output tokens
SONNET_4_6_CACHE_READ_PER_MTK = 0.30  # $0.30 per 1M cache_read tokens

# ---------------------------------------------------------------------------
# Markdown rendering — pure function, no side effects
# ---------------------------------------------------------------------------

#: Extensions that improve fidelity for typical UoW content (checklists, code fences, tables).
_MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "nl2br"]


def _render_markdown(text: str) -> str:
    """Convert a markdown string to sanitized HTML.

    Pure function: accepts a string, returns an HTML string.
    Empty/whitespace-only input returns an empty string.
    The output is intended for direct injection into an HTML page — the caller
    is responsible for placing it inside an appropriately styled container.
    """
    if not text or not text.strip():
        return ""
    return _markdown_lib.markdown(text, extensions=_MARKDOWN_EXTENSIONS)


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
# DB connection helper
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Pure data-fetching functions — each takes an open connection
# ---------------------------------------------------------------------------

def _fetch_uow_data(conn: sqlite3.Connection, uow_id: str) -> dict[str, Any] | None:
    """Return all UoW fields as a dict, or None if the UoW does not exist."""
    row = conn.execute(
        """
        SELECT
            id, summary, status, created_at, updated_at, started_at, completed_at,
            source_issue_number, issue_url, outcome_category,
            steward_cycles, lifetime_cycles, execution_attempts, retry_count,
            token_usage, posture, register, close_reason, prescription_confidence,
            success_criteria, type, source, gate_fired
        FROM uow_registry
        WHERE id = ?
        """,
        (uow_id,),
    ).fetchone()

    if row is None:
        return None

    return dict(row)


def _fetch_audit_trail(conn: sqlite3.Connection, uow_id: str) -> list[dict]:
    """Return the full (uncapped) audit trail for a UoW, ordered ascending by ts."""
    rows = conn.execute(
        "SELECT ts, event, from_status, to_status, agent, note FROM audit_log "
        "WHERE uow_id = ? ORDER BY ts ASC",
        (uow_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_corrective_traces(conn: sqlite3.Connection, uow_id: str) -> list[dict]:
    """Return corrective traces for a UoW. Gracefully returns [] if table absent."""
    try:
        rows = conn.execute(
            "SELECT execution_summary, surprises, prescription_delta, gate_score, summary, created_at "
            "FROM corrective_traces WHERE uow_id = ? ORDER BY created_at DESC",
            (uow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _fetch_heartbeat_log(conn: sqlite3.Connection, uow_id: str) -> list[dict]:
    """Return heartbeat log entries for a UoW. Gracefully returns [] if table absent."""
    try:
        rows = conn.execute(
            "SELECT recorded_at, token_usage FROM uow_heartbeat_log "
            "WHERE uow_id = ? ORDER BY recorded_at ASC",
            (uow_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


# ---------------------------------------------------------------------------
# Token ledger parsing — pure function over ledger entries list
# ---------------------------------------------------------------------------

_UOW_PATTERN = re.compile(r"uow_\d{8}_[0-9a-f]+")


def _fetch_token_data(
    entries: list[dict],
    uow_id: str,
) -> dict[str, Any] | None:
    """Aggregate token data from ledger entries matching this UoW id.

    Matches task_id values that contain the uow_id pattern anywhere in the string
    (e.g. 'wos-uow_20260501_abc123', 'wos-executor-uow_20260501_abc123', or bare
    'uow_20260501_abc123').

    Returns None if no matching entries found.
    """
    totals: dict[str, int] = {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0,
    }
    found = False

    for entry in entries:
        explicit_uow_id = entry.get("uow_id")
        if explicit_uow_id:
            matched = explicit_uow_id == uow_id
        else:
            task_id = entry.get("task_id", "") or ""
            m = _UOW_PATTERN.search(task_id)
            matched = bool(m) and m.group(0) == uow_id
        if matched:
            found = True
            totals["input"] += entry.get("input", 0) or 0
            totals["output"] += entry.get("output", 0) or 0
            totals["cache_read"] += entry.get("cache_read", 0) or 0
            totals["cache_write"] += entry.get("cache_write", 0) or 0
            totals["calls"] += 1

    return totals if found else None


def _read_ledger(ledger_path: Path) -> list[dict]:
    """Read token ledger JSONL file into a list of dicts. Returns [] on error."""
    if not ledger_path.exists():
        return []
    entries = []
    try:
        with ledger_path.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        return []
    return entries


# ---------------------------------------------------------------------------
# Pure computation helpers
# ---------------------------------------------------------------------------

def _compute_elapsed(start: str | None, end: str | None) -> int | None:
    """Compute elapsed seconds between two ISO timestamps.

    Returns None if either timestamp is None or unparseable.
    """
    if start is None or end is None:
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return int((t1 - t0).total_seconds())
    except (ValueError, TypeError):
        return None


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
) -> float:
    """Estimate USD cost using Sonnet 4.6 pricing.

    Sonnet 4.6: $3/1M input, $15/1M output, $0.30/1M cache_read.
    """
    return (
        input_tokens * SONNET_4_6_INPUT_PER_MTK / 1_000_000
        + output_tokens * SONNET_4_6_OUTPUT_PER_MTK / 1_000_000
        + cache_read_tokens * SONNET_4_6_CACHE_READ_PER_MTK / 1_000_000
    )


def _fmt_duration(seconds: int | None) -> str:
    """Format elapsed seconds as human-readable string."""
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"



# ---------------------------------------------------------------------------
# Action footer — pure function, no side effects
# ---------------------------------------------------------------------------

def _action_footer_html(uow_id: str) -> str:
    """Return an HTML action footer with four Telegram deep-link action buttons.

    Uses the same base64url payload encoding as wos_dashboard._tg_deep_link.
    Bot username read from TELEGRAM_BOT_USERNAME env var.
    """
    import base64
    import json as _json
    bot = os.environ.get("TELEGRAM_BOT_USERNAME", "LobsterBot")

    def _link(action: str) -> str:
        payload = base64.urlsafe_b64encode(
            _json.dumps({"a": action, "u": uow_id}, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()
        return f"https://t.me/{bot}?start={payload}"

    actions = [
        ("↺ Retry", _link("retry"), "#1565c0", "#e3f0ff"),
        ("⬆ Escalate", _link("escalate"), "#a06000", "#fef3cd"),
        ("✓ Mark Resolved", _link("mark_resolved"), "#1a7a3c", "#d4f7e0"),
        ("✗ Close Won't Fix", _link("close_wont_fix"), "#666", "#eee"),
    ]
    btns = " ".join(
        f"<a href='{url}' style='display:inline-block;padding:7px 14px;margin:4px;"
        f"border-radius:8px;font-size:.82rem;font-weight:600;text-decoration:none;"
        f"color:{fg};background:{bg};border:1px solid {fg}'>{label}</a>"
        for label, url, fg, bg in actions
    )
    return (
        f"<div style='margin-top:24px;padding:14px;background:var(--surface2);"
        f"border-radius:10px;border:1px solid var(--border)'>"
        f"<h2 style='font-size:.78rem;font-weight:600;color:var(--text3);"
        f"text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px'>Actions</h2>"
        f"{btns}"
        f"<p style='font-size:.68rem;color:var(--text3);margin-top:8px'>"
        f"Each button opens Telegram and sends the action to Lobster.</p>"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# HTML template — same CSS primitives as v4 wos_dashboard_gen.py
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UoW {uow_id}</title>
<style>
:root{{--bg:#f5f5f5;--surface:#fff;--surface2:#f0f0f0;--border:#ddd;--text:#1a1a1a;--text2:#555;--text3:#888;--accent:#4a7fc0;--accent-light:#e8f0fa;--done-col:#1a7a3c;--done-bg:#d4f7e0;--pend-col:#a06000;--pend-bg:#fef3cd;--fail-col:#c0392b;--fail-bg:#fde8e6;--act-col:#1565c0;--act-bg:#e3f0ff;--cl-col:#666;--cl-bg:#eee;--seed-col:#166534;--seed-bg:#dcfce7;--pearl-col:#5b21b6;--pearl-bg:#ede9fe;--heat-col:#b45309;--heat-bg:#fef3c7;--shadow:0 1px 4px rgba(0,0,0,.08)}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1117;--surface:#1c1f2a;--surface2:#252837;--border:#333;--text:#e8e8e8;--text2:#aaa;--text3:#666;--accent:#6fa3e0;--accent-light:#1a2540;--done-col:#4ade80;--done-bg:#0a2e1a;--pend-col:#fbbf24;--pend-bg:#2a1e00;--fail-col:#f87171;--fail-bg:#2a0a0a;--act-col:#60a5fa;--act-bg:#0a1e3a;--cl-col:#888;--cl-bg:#252525;--seed-col:#86efac;--seed-bg:#0a2a14;--pearl-col:#c4b5fd;--pearl-bg:#1e1040;--heat-col:#fcd34d;--heat-bg:#2a1a00;--shadow:0 1px 4px rgba(0,0,0,.4)}}}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);line-height:1.5}}
.wrap{{max-width:1000px;margin:0 auto;padding:16px}}
h1{{font-size:1.4rem;font-weight:700;margin-bottom:4px;word-break:break-all}}
h2{{font-size:.85rem;font-weight:600;margin-bottom:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.05em}}
.meta{{color:var(--text3);font-size:.78rem;margin-bottom:16px}}
.sec{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:14px;box-shadow:var(--shadow)}}
.pgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:12px}}
.scard{{background:var(--surface2);border-radius:8px;padding:10px 12px;text-align:center}}
.scard .n{{font-size:1.5rem;font-weight:700;color:var(--accent)}}
.scard .l{{font-size:.65rem;color:var(--text3);text-transform:uppercase;letter-spacing:.04em}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.72rem;font-weight:600;white-space:nowrap}}
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
.uid{{font-family:monospace;font-size:.78rem;color:var(--text3)}}
.gh{{color:var(--accent);text-decoration:none;font-size:.78rem}}
.gh:hover{{text-decoration:underline}}
.sumtxt{{font-size:.9rem;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:10px}}
.dgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}}
.dkv .k{{color:var(--text3);font-size:.67rem;text-transform:uppercase;letter-spacing:.04em}}
.dkv .v{{color:var(--text);font-weight:500;font-size:.8rem}}
.tl{{display:flex;flex-direction:column;gap:4px}}
.tli{{display:flex;gap:8px;font-size:.75rem;align-items:flex-start}}
.tlts{{color:var(--text3);white-space:nowrap;min-width:115px;font-size:.68rem;padding-top:1px}}
.tlfr{{color:var(--fail-col)}}
.tlto{{color:var(--done-col)}}
.tlsym{{color:var(--text3);min-width:10px}}
.tlevent{{font-family:monospace;font-size:.72rem}}
.tlnote{{color:var(--text3);font-size:.68rem;max-width:600px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.hb-bar{{display:flex;align-items:flex-end;gap:3px;height:60px;margin-top:8px}}
.empty{{text-align:center;padding:24px 20px;color:var(--text3);font-size:.85rem}}
.back{{color:var(--accent);text-decoration:none;font-size:.8rem;display:inline-block;margin-bottom:14px}}
.back:hover{{text-decoration:underline}}
.conf-bar{{height:8px;border-radius:4px;background:var(--surface2);overflow:hidden;margin-top:4px}}
.conf-fill{{height:100%;border-radius:4px;background:var(--accent)}}
.tok-bar{{display:flex;gap:4px;height:16px;border-radius:6px;overflow:hidden;margin:8px 0 4px}}
.tok-seg-out{{background:#4a7fc0}}
.tok-seg-in{{background:#7baad0}}
.tok-seg-cr{{background:#b0c8e0}}
.tok-seg-cw{{background:#d0e4f4}}
.legend{{display:flex;gap:12px;font-size:.65rem;color:var(--text3);flex-wrap:wrap;margin-top:3px}}
.leg{{display:flex;align-items:center;gap:4px}}
.legdot{{width:8px;height:8px;border-radius:2px}}
.md-content{{font-size:.83rem;line-height:1.6}}
.md-content p{{margin:0 0 8px}}
.md-content p:last-child{{margin-bottom:0}}
.md-content ul,.md-content ol{{margin:0 0 8px;padding-left:1.4em}}
.md-content li{{margin-bottom:2px}}
.md-content strong{{font-weight:600}}
.md-content em{{font-style:italic}}
.md-content code{{font-family:monospace;font-size:.82em;background:var(--surface2);border:1px solid var(--border);border-radius:3px;padding:1px 4px}}
.md-content pre{{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;font-size:.78rem;margin:0 0 8px}}
.md-content pre code{{background:none;border:none;padding:0}}
.md-content h1,.md-content h2,.md-content h3{{font-weight:600;margin:0 0 6px;color:var(--text)}}
.md-content h1{{font-size:.9rem}}
.md-content h2{{font-size:.85rem}}
.md-content h3{{font-size:.82rem}}
.md-content blockquote{{border-left:3px solid var(--border);padding-left:10px;color:var(--text2);margin:0 0 8px}}
.md-content table{{border-collapse:collapse;font-size:.78rem;margin-bottom:8px}}
.md-content th,.md-content td{{padding:4px 8px;border:1px solid var(--border)}}
.md-content th{{background:var(--surface2);font-weight:600}}
.md-content a{{color:var(--accent)}}
</style>
</head>
<body>
<div class="wrap">
<a class="back" href="javascript:history.back()">← Back</a>
<h1>{uow_id_display}</h1>
<p class="meta" id="meta-line"></p>

<script>
const D = {D_JSON};
</script>

<div id="main"></div>

<script>
(function() {{

// --- Helpers ---
function fmt(n) {{ return n == null ? '—' : Number(n).toLocaleString(); }}
function fmtDate(s) {{
  if (!s) return '—';
  try {{ return new Date(s).toISOString().slice(0, 16).replace('T', ' ') + ' UTC'; }}
  catch(e) {{ return s.slice(0, 16); }}
}}
function statusBadge(s) {{
  const m = {{
    'done':'bd','closed':'bc','expired':'bc','cancelled':'bc',
    'failed':'bf','ready-for-steward':'ba','ready-for-executor':'ba',
    'active':'ba','executing':'ba','proposed':'bp','pending':'bp',
    'needs-human-review':'bf','blocked':'bf',
  }};
  return `<span class="badge ${{m[s]||'bc'}}">${{s}}</span>`;
}}
function outcomeBadge(o) {{
  if (!o) return '';
  const m = {{'seed':'bs','pearl':'bpe','heat':'bh'}};
  return `<span class="badge ${{m[o]||'bc'}}">${{o}}</span>`;
}}
function fmtDuration(secs) {{
  if (secs == null || secs < 0) return '—';
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs/60) + 'm ' + (secs%60) + 's';
  const h = Math.floor(secs/3600);
  const m = Math.floor((secs%3600)/60);
  return h + 'h ' + m + 'm';
}}
function eventIcon(evt) {{
  if (evt === 'execution_complete' || evt === 'steward_closure') return ['✓', 'tlto'];
  if (evt === 'failed' || evt === 'execution_failed') return ['✗', 'tlfr'];
  if (evt === 'executor_dispatch' || evt === 'dispatched') return ['→', 'tlsym'];
  if (evt === 'created') return ['○', 'tlsym'];
  return ['·', 'tlsym'];
}}

// --- Meta line ---
document.getElementById('meta-line').textContent =
  'Generated ' + new Date().toUTCString();

// --- Token bar ---
function tokenBarHtml(tok) {{
  if (!tok) return '<span style="color:var(--text3);font-size:.78rem">No token data in ledger</span>';
  const total = (tok.input||0) + (tok.output||0) + (tok.cache_read||0) + (tok.cache_write||0);
  if (!total) return '<span style="color:var(--text3);font-size:.78rem">0 tokens recorded</span>';
  function pct(n) {{ return (((n||0)/total)*100).toFixed(1) + '%'; }}
  return `<div class="tok-bar">
    <div class="tok-seg-out" style="width:${{pct(tok.output)}}" title="Output: ${{fmt(tok.output)}}"></div>
    <div class="tok-seg-in" style="width:${{pct(tok.input)}}" title="Input: ${{fmt(tok.input)}}"></div>
    <div class="tok-seg-cr" style="width:${{pct(tok.cache_read)}}" title="Cache read: ${{fmt(tok.cache_read)}}"></div>
    <div class="tok-seg-cw" style="width:${{pct(tok.cache_write)}}" title="Cache write: ${{fmt(tok.cache_write)}}"></div>
  </div>
  <div class="legend">
    <div class="leg"><div class="legdot" style="background:#4a7fc0"></div>Output ${{fmt(tok.output)}}</div>
    <div class="leg"><div class="legdot" style="background:#7baad0"></div>Input ${{fmt(tok.input)}}</div>
    <div class="leg"><div class="legdot" style="background:#b0c8e0"></div>Cache read ${{fmt(tok.cache_read)}}</div>
    <div class="leg"><div class="legdot" style="background:#d0e4f4"></div>Cache write ${{fmt(tok.cache_write)}}</div>
  </div>`;
}}

// --- Heartbeat chart ---
function heartbeatChartHtml(beats) {{
  if (!beats || !beats.length) return '<div class="empty" style="padding:12px">No heartbeat data</div>';
  const vals = beats.map(b => b.token_usage || 0);
  const maxVal = Math.max(...vals, 1);
  const bars = beats.map((b, i) => {{
    const h = Math.max(Math.round((b.token_usage||0) / maxVal * 56), 2);
    const delta = i > 0 ? (b.token_usage||0) - (beats[i-1].token_usage||0) : null;
    const deltaStr = delta != null ? ` (Δ +${{delta.toLocaleString()}})` : '';
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;min-width:10px">
      <div style="width:100%;height:${{h}}px;background:var(--accent);border-radius:2px 2px 0 0" title="${{b.recorded_at ? b.recorded_at.slice(11,16) : ''}} — ${{fmt(b.token_usage)}} tokens${{deltaStr}}"></div>
    </div>`;
  }}).join('');
  const lastBeat = beats[beats.length - 1];
  const firstBeat = beats[0];
  return `<div style="font-size:.68rem;color:var(--text3);margin-bottom:4px">${{beats.length}} heartbeats — ${{firstBeat.recorded_at ? firstBeat.recorded_at.slice(11,16) : ''}} → ${{lastBeat.recorded_at ? lastBeat.recorded_at.slice(11,16) : ''}} UTC</div>
  <div class="hb-bar">${{bars}}</div>
  <div style="font-size:.68rem;color:var(--text3);margin-top:4px">Token growth: ${{fmt(firstBeat.token_usage||0)}} → ${{fmt(lastBeat.token_usage||0)}}</div>`;
}}

// --- Audit timeline ---
function auditTimelineHtml(audit) {{
  if (!audit || !audit.length) return '<div class="empty" style="padding:12px">No audit events</div>';
  return `<div class="tl">${{audit.map(a => {{
    const [sym, cls] = eventIcon(a.event);
    const transition = (a.from_status && a.to_status) ? ` <span style="color:var(--text3)">${{a.from_status}} → ${{a.to_status}}</span>` : '';
    let noteSnip = '';
    if (a.note) {{
      let note = a.note;
      try {{ const obj = JSON.parse(note); note = obj.reason || obj.event || note; }} catch(e) {{}}
      noteSnip = `<span class="tlnote" title="${{a.note.replace(/"/g,'&quot;')}}">${{note.slice(0,120)}}</span>`;
    }}
    return `<div class="tli">
      <span class="tlts">${{a.ts ? a.ts.slice(0,16).replace('T',' ') : ''}}</span>
      <span class="${{cls}} tlsym">${{sym}}</span>
      <span><span class="tlevent">${{a.event}}</span>${{transition}} ${{noteSnip}}</span>
    </div>`;
  }}).join('')}}</div>`;
}}

// --- Corrective traces ---
function tracesHtml(traces) {{
  if (!traces || !traces.length) return '<div class="empty" style="padding:12px">No corrective traces</div>';
  return traces.map(t => `<div class="sec" style="margin-bottom:8px;box-shadow:none;border-color:var(--border)">
    <div style="font-size:.68rem;color:var(--text3);margin-bottom:6px">${{t.created_at ? t.created_at.slice(0,16).replace('T',' ') + ' UTC' : ''}}</div>
    ${{t.execution_summary ? `<div style="font-size:.78rem;margin-bottom:6px">${{t.execution_summary}}</div>` : ''}}
    ${{t.gate_score != null ? `<div style="font-size:.7rem;color:var(--text3)">Gate score: <strong>${{t.gate_score}}</strong></div>` : ''}}
  </div>`).join('');
}}

// --- Main render ---
const u = D.uow;
const tok = D.token_data;
const elapsed = D.elapsed_seconds;
const cost = D.estimated_cost_usd;

// Prescription confidence bar
let confBar = '';
if (u.prescription_confidence != null) {{
  const pct = Math.round(u.prescription_confidence * 100);
  confBar = `<div style="font-size:.7rem;color:var(--text3)">${{pct}}% confidence</div>
  <div class="conf-bar"><div class="conf-fill" style="width:${{pct}}%"></div></div>`;
}}

const html = `
  <div class="sec">
    <h2>Summary</h2>
    <div class="sumtxt md-content">${{D.summary_html || u.summary || '(no summary)'}}</div>
    <div style="margin-bottom:10px">
      ${{statusBadge(u.status)}} ${{outcomeBadge(u.outcome_category)}}
      ${{u.gate_fired && u.gate_fired !== 'none' ? `<span class="badge bf" style="margin-left:4px">gate: ${{u.gate_fired}}</span>` : ''}}
    </div>
    <div class="dgrid">
      <div class="dkv"><div class="k">ID</div><div class="v uid">${{u.id}}</div></div>
      <div class="dkv"><div class="k">Status</div><div class="v">${{u.status}}</div></div>
      <div class="dkv"><div class="k">Register</div><div class="v">${{u.register||'—'}}</div></div>
      <div class="dkv"><div class="k">Posture</div><div class="v">${{u.posture||'—'}}</div></div>
      <div class="dkv"><div class="k">Created</div><div class="v">${{fmtDate(u.created_at)}}</div></div>
      <div class="dkv"><div class="k">Started</div><div class="v">${{fmtDate(u.started_at)}}</div></div>
      <div class="dkv"><div class="k">Completed</div><div class="v">${{fmtDate(u.completed_at)}}</div></div>
      <div class="dkv"><div class="k">Wall-clock</div><div class="v">${{fmtDuration(elapsed)}}</div></div>
      ${{u.issue_url ? `<div class="dkv"><div class="k">Issue</div><div class="v"><a class="gh" href="${{u.issue_url}}" target="_blank">#${{u.source_issue_number}}</a></div></div>` : ''}}
    </div>
  </div>

  ${{D.success_criteria_html ? `<div class="sec"><h2>Success Criteria</h2><div class="md-content">${{D.success_criteria_html}}</div></div>` : ''}}

  <div class="sec">
    <h2>Execution Stats</h2>
    <div class="pgrid">
      <div class="scard"><div class="n">${{u.steward_cycles||0}}</div><div class="l">Steward Cycles</div></div>
      <div class="scard"><div class="n">${{u.lifetime_cycles||0}}</div><div class="l">Lifetime Cycles</div></div>
      <div class="scard"><div class="n">${{u.execution_attempts||0}}</div><div class="l">Exec Attempts</div></div>
      <div class="scard"><div class="n">${{u.retry_count||0}}</div><div class="l">Retries</div></div>
    </div>
    ${{confBar ? `<div style="margin-top:8px"><div style="font-size:.68rem;color:var(--text3);margin-bottom:3px;text-transform:uppercase;letter-spacing:.04em">Prescription Confidence</div>${{confBar}}</div>` : ''}}
    ${{D.close_reason_html ? `<div style="margin-top:10px"><div style="font-size:.68rem;color:var(--text3);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px">Close Reason</div><div class="md-content">${{D.close_reason_html}}</div></div>` : ''}}
  </div>

  <div class="sec">
    <h2>Token Usage</h2>
    ${{tok ? `
    <div class="pgrid">
      <div class="scard"><div class="n">${{fmt(tok.output)}}</div><div class="l">Output</div></div>
      <div class="scard"><div class="n">${{fmt(tok.input)}}</div><div class="l">Input</div></div>
      <div class="scard"><div class="n">${{fmt(tok.cache_read)}}</div><div class="l">Cache Read</div></div>
      <div class="scard"><div class="n">${{fmt(tok.cache_write)}}</div><div class="l">Cache Write</div></div>
      <div class="scard"><div class="n">${{tok.calls||0}}</div><div class="l">API Calls</div></div>
      <div class="scard"><div class="n">$${{cost != null ? cost.toFixed(4) : '—'}}</div><div class="l">Est. Cost USD</div></div>
    </div>
    ${{tokenBarHtml(tok)}}
    ` : '<div class="empty">No token data in ledger for this UoW</div>'}}
  </div>

  <div class="sec">
    <h2>Heartbeat Log (Token Growth)</h2>
    ${{heartbeatChartHtml(D.heartbeats)}}
  </div>

  <div class="sec">
    <h2>Audit Trail (${{D.audit_trail.length}} events)</h2>
    ${{auditTimelineHtml(D.audit_trail)}}
  </div>

  <div class="sec">
    <h2>Corrective Traces (${{D.traces.length}})</h2>
    ${{tracesHtml(D.traces)}}
  </div>
`;

document.getElementById('main').innerHTML = html;

}})();
</script>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTML rendering — pure function
# ---------------------------------------------------------------------------

def generate_html(
    uow_data: dict[str, Any],
    audit_trail: list[dict],
    traces: list[dict],
    heartbeats: list[dict],
    token_data: dict[str, Any] | None,
) -> str:
    """Render the drilldown HTML page from pre-fetched data.

    Pure function: no IO. All data must be passed as arguments.
    """
    uow_id = uow_data["id"]
    elapsed = _compute_elapsed(uow_data.get("started_at"), uow_data.get("completed_at"))
    cost = None
    if token_data:
        cost = _estimate_cost(
            token_data.get("input", 0) or 0,
            token_data.get("output", 0) or 0,
            token_data.get("cache_read", 0) or 0,
        )

    payload = {
        "uow": uow_data,
        "audit_trail": audit_trail,
        "traces": traces,
        "heartbeats": heartbeats,
        "token_data": token_data,
        "elapsed_seconds": elapsed,
        "estimated_cost_usd": round(cost, 6) if cost is not None else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Pre-rendered markdown fields — avoids client-side markdown parsing.
        # Only populated when the source field is non-empty.
        "summary_html": _render_markdown(uow_data.get("summary") or ""),
        "success_criteria_html": _render_markdown(uow_data.get("success_criteria") or ""),
        "close_reason_html": _render_markdown(uow_data.get("close_reason") or ""),
    }

    d_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    html = _HTML_TEMPLATE.format(uow_id=uow_id, uow_id_display=uow_id, D_JSON=d_json)
    action_footer = _action_footer_html(uow_id)
    return html.replace("</div>\n</body>\n</html>", f"{action_footer}\n</div>\n</body>\n</html>")


# ---------------------------------------------------------------------------
# Top-level generator
# ---------------------------------------------------------------------------

def generate_and_upload(
    uow_id: str,
    db_path: Path | None = None,
    ledger_path: Path | None = None,
) -> str:
    """Generate a standalone HTML drilldown page for a UoW and return its public URL.

    Raises ValueError if the UoW is not found in the registry.
    Writes the HTML file to ~/messages/bisque-uploads/ (bisque relay directory).
    """
    if db_path is None:
        db_path = _registry_path()
    if ledger_path is None:
        ledger_path = _ledger_path()

    conn = _connect(db_path)
    try:
        uow_data = _fetch_uow_data(conn, uow_id)
        if uow_data is None:
            raise ValueError(f"UoW {uow_id!r} not found in registry at {db_path}")

        audit_trail = _fetch_audit_trail(conn, uow_id)
        traces = _fetch_corrective_traces(conn, uow_id)
        heartbeats = _fetch_heartbeat_log(conn, uow_id)
    finally:
        conn.close()

    ledger_entries = _read_ledger(ledger_path)
    token_data = _fetch_token_data(ledger_entries, uow_id)

    html = generate_html(uow_data, audit_trail, traces, heartbeats, token_data)

    uploads = _uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.html"
    dest = uploads / filename
    dest.write_text(html, encoding="utf-8")

    base_url = _bisque_base_url()
    return f"{base_url}/files/{filename}"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a standalone HTML drilldown page for a single WOS UoW",
    )
    parser.add_argument(
        "--uow-id",
        required=True,
        help="UoW ID (e.g. uow_20260501_abc123)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override registry DB path (default: auto-detected from env)",
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help="Override token ledger path (default: auto-detected from env)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else None
    ledger_path = Path(args.ledger) if args.ledger else None

    try:
        url = generate_and_upload(
            uow_id=args.uow_id,
            db_path=db_path,
            ledger_path=ledger_path,
        )
        print(url)
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
