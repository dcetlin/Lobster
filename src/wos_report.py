#!/usr/bin/env python3
"""
wos_report.py — Generate WOS Registry reports in two formats.

Summary PDF (Dan's report): clean card layout, key signals prominent, no verbose logs.
Full markdown report: all fields, full JSON logs, for deep investigation.

Usage:
    uv run ~/lobster/src/wos_report.py [OPTIONS]

Options:
    --chat-id           Telegram chat ID to send to (default: 8075091586)
    --output PATH       Output PDF path (generates summary PDF)
    --full-output PATH  Output markdown path (generates full investigation report)
    --no-send           Generate reports but do not send PDF to Telegram
    --status STATUS     Filter by status (e.g. active, done, proposed; default: all)
    --since YYYY-MM-DD  Include only UoWs created on or after this date
    --ids id1,id2,...   Include only the specified UoW IDs (comma-separated)

Filters combine: --since and --ids are applied after --status.
Both --output and --full-output can be used together.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from src.orchestration.analytics import prescription_quality_summary

# ── fpdf2 ──────────────────────────────────────────────────────────────────────
try:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
except ImportError:
    print("ERROR: fpdf2 not installed. Run: uv add fpdf2", file=sys.stderr)
    sys.exit(1)

# ── constants ─────────────────────────────────────────────────────────────────
REGISTRY_DB = Path(os.environ.get(
    "WOS_REGISTRY_DB",
    Path.home() / "lobster-workspace" / "orchestration" / "registry.db"
))
OUTBOX_DIR = Path(os.environ.get(
    "LOBSTER_OUTBOX",
    Path.home() / "messages" / "outbox"
))
FULL_REPORTS_DIR = Path.home() / "lobster-workspace" / "data" / "ralph-reports"
DEFAULT_CHAT_ID = 8075091586
GITHUB_REPO = "dcetlin/Lobster"

# Status badge colours (R, G, B)
STATUS_COLORS: dict[str, tuple[int, int, int]] = {
    "proposed":           (180, 140,  60),
    "pending":            ( 70, 130, 180),
    "ready-for-steward":  (100, 160, 100),
    "ready-for-executor": ( 80, 180, 120),
    "active":             ( 40, 140, 200),
    "diagnosing":         (180, 100,  50),
    "blocked":            (200,  60,  60),
    "done":               ( 70, 160,  90),
    "failed":             (200,  60,  60),
    "expired":            (140, 120, 100),
}
DEFAULT_STATUS_COLOR = (120, 120, 120)

# Terminal statuses — used for anomaly detection
TERMINAL_STATUSES = {"done", "failed", "expired"}

PAGE_W = 210          # A4 mm
MARGIN = 14           # mm left/right/top
CONTENT_W = PAGE_W - MARGIN * 2

# ── colour palette ─────────────────────────────────────────────────────────────
C_HEADING_BG   = (235, 240, 250)
C_HEADING_TEXT = (50,  80, 140)
C_GRID_LINE    = (210, 215, 225)
C_MUTED        = (120, 120, 130)
C_DARK         = (25,  25,  35)
C_LINK         = (30,  80, 180)
C_DONE         = ( 70, 160,  90)
C_ANOMALY_BG   = (255, 235, 235)
C_ANOMALY_TEXT = (180,  40,  40)
C_WARN_TEXT    = (160,  90,  20)
C_CYCLE_OK     = ( 70, 160,  90)   # 1 steward cycle = healthy
C_CYCLE_WARN   = (180,  90,  20)   # 2+ cycles = investigate


# ── text helpers ──────────────────────────────────────────────────────────────

_UNICODE_MAP = str.maketrans({
    "\u2014": "--",
    "\u2013": "-",
    "\u2019": "'",
    "\u2018": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2022": "*",
    "\u2026": "...",
    "\u00e9": "e",
    "\u00e8": "e",
    "\u00e0": "a",
    "\u00fc": "u",
    "\u00f6": "o",
    "\u00e4": "a",
    "\u00df": "ss",
})


def _safe(text: str) -> str:
    """Replace non-latin-1 characters with ASCII equivalents."""
    text = text.translate(_UNICODE_MAP)
    return "".join(
        c if ord(c) < 256 else "?"
        for c in unicodedata.normalize("NFKD", text)
        if ord(c) < 256 or unicodedata.category(c) not in ("Mn",)
    )


def _fmt_ts(ts: str | None, short: bool = False) -> str:
    """Format a timestamp string. short=True returns MM-DD HH:MM."""
    if not ts:
        return ""
    try:
        ts_clean = ts.replace(" ", "T")
        if "+" in ts_clean:
            dt = datetime.fromisoformat(ts_clean)
        elif ts_clean.endswith("Z"):
            dt = datetime.fromisoformat(ts_clean[:-1]).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(ts_clean).replace(tzinfo=timezone.utc)
        if short:
            return dt.strftime("%m-%d %H:%M")
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ts[:16] if ts else ""


def _duration_str(start_ts: str | None, end_ts: str | None) -> str:
    """Return human-readable duration between two timestamps."""
    if not start_ts or not end_ts:
        return ""
    try:
        def _parse(ts: str) -> datetime:
            ts = ts.replace(" ", "T")
            if "+" in ts:
                return datetime.fromisoformat(ts)
            if ts.endswith("Z"):
                return datetime.fromisoformat(ts[:-1]).replace(tzinfo=timezone.utc)
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)

        delta = _parse(end_ts) - _parse(start_ts)
        secs = int(delta.total_seconds())
        if secs < 0:
            return ""
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return ""


# ── data layer ────────────────────────────────────────────────────────────────

def _source_url(source: str | None, issue_number: int | None) -> str:
    if issue_number:
        return f"https://github.com/{GITHUB_REPO}/issues/{issue_number}"
    if source and source.startswith("github:issue/"):
        num = source.split("/")[-1]
        return f"https://github.com/{GITHUB_REPO}/issues/{num}"
    return source or ""


def _parse_steward_log(raw: str | None) -> list[dict]:
    """Parse newline-delimited JSON steward_log into a list of event dicts."""
    if not raw:
        return []
    events = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


def _extract_first_prsc_reason(log_events: list[dict]) -> str:
    """Extract the first prescription reason (what sent UoW to executor)."""
    for ev in log_events:
        if ev.get("event") in ("prescription", "reentry_prescription"):
            # Try several fields where this surfaces
            reason = (
                ev.get("return_reason")
                or ev.get("re_entry_posture")
                or ev.get("next_posture_rationale")
                or ""
            )
            if reason:
                return reason
            # Fall back to posture from agenda
            posture = ev.get("re_entry_posture") or ""
            if posture:
                return posture
    return ""


def _extract_outcome_summary(log_events: list[dict], uow: dict) -> str:
    """Extract outcome / completion summary from steward closure or audit notes."""
    # Try steward_closure event first
    for ev in reversed(log_events):
        if ev.get("event") == "steward_closure":
            assessment = (
                ev.get("completion_rationale")
                or ev.get("completion_assessment")
                or ev.get("assessment")
                or ""
            )
            if assessment:
                return assessment

    # Fall back to last audit note
    for audit in reversed(uow.get("_audit_events", [])):
        note = audit.get("note", "")
        if note and len(note) > 10:
            return note

    return ""


def _detect_anomalies(uow: dict) -> list[str]:
    """
    Return list of anomaly strings for a UoW.
    Empty list = no anomalies.
    """
    anomalies = []
    status = uow.get("status", "")
    steward_cycles = uow.get("steward_cycles", 0) or 0

    if status == "failed":
        anomalies.append("Status: FAILED")

    if status not in TERMINAL_STATUSES and status not in ("proposed", "pending"):
        anomalies.append(f"Non-terminal status: {status}")

    # Steward never touched it but it's past pending
    if steward_cycles == 0 and status not in ("proposed", "pending", "ready-for-steward"):
        anomalies.append("Steward cycles = 0 (steward may not have processed this)")

    # Done but no workflow artifact (executor produced no output artifact)
    if status == "done" and not uow.get("workflow_artifact"):
        log_events = uow.get("_steward_log_events", [])
        # Only flag if there were steward cycles (not a trivially-completed UoW)
        if steward_cycles > 0:
            anomalies.append("Done but no workflow artifact recorded")

    return anomalies


def fetch_uows(
    status_filter: str | None = None,
    since: str | None = None,
    ids: list[str] | None = None,
) -> list[dict]:
    """Load UoWs from the registry DB, sorted by created_at descending."""
    if not REGISTRY_DB.exists():
        raise FileNotFoundError(f"Registry DB not found: {REGISTRY_DB}")
    conn = sqlite3.connect(str(REGISTRY_DB))
    conn.row_factory = sqlite3.Row
    try:
        conditions: list[str] = []
        params: list[str] = []

        if status_filter:
            conditions.append("status = ?")
            params.append(status_filter)
        if since:
            conditions.append("created_at >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM uow_registry {where} ORDER BY created_at DESC",
            params,
        ).fetchall()

        uows = []
        for row in rows:
            d = dict(row)
            if ids is not None and d["id"] not in ids:
                continue

            audits = conn.execute(
                "SELECT ts, event, from_status, to_status, note "
                "FROM audit_log WHERE uow_id=? ORDER BY ts ASC",
                (d["id"],)
            ).fetchall()
            d["_audit_events"] = [dict(a) for a in audits]
            d["_steward_log_events"] = _parse_steward_log(d.get("steward_log"))

            try:
                d["_prescribed_skills"] = json.loads(d.get("prescribed_skills") or "[]")
            except Exception:
                d["_prescribed_skills"] = []

            uows.append(d)
        return uows
    finally:
        conn.close()


# ── Summary PDF builder ────────────────────────────────────────────────────────

class SummaryReport(FPDF):
    """
    Clean summary PDF for Dan.
    One card per UoW: status prominent, steward cycle health signal,
    timing block, PRSC reason, outcome. Anomalies highlighted in red.
    No verbose log dumps.
    """

    def __init__(self, total: int, generated_at: str, status_counts: dict[str, int]):
        super().__init__()
        self._total = total
        self._generated_at = generated_at
        self._status_counts = status_counts

    def header(self):
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*C_DARK)
        self.cell(0, 10, "WOS Registry -- Summary",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(*C_MUTED)
        self.cell(0, 5, f"Generated {self._generated_at}  |  {self._total} unit(s)",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
        self.ln(2)
        self.set_draw_color(*C_GRID_LINE)
        self.set_line_width(0.3)
        self.line(MARGIN, self.get_y(), PAGE_W - MARGIN, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*C_MUTED)
        self.cell(0, 5, f"WOS Registry Summary  |  {self._generated_at}  |  Page {self.page_no()}",
                  align="C")

    def _render_registry_snapshot(self) -> None:
        """Render the registry snapshot header block on the first page."""
        # Status pills row
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*C_HEADING_TEXT)
        self.cell(0, 6, "Registry Snapshot",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

        x = MARGIN
        y = self.get_y()
        for status_name, count in sorted(self._status_counts.items()):
            sr, sg, sb = STATUS_COLORS.get(status_name, DEFAULT_STATUS_COLOR)
            label = f"{status_name}: {count}"
            self.set_font("Helvetica", "B", 8)
            pill_w = self.get_string_width(label) + 10
            if x + pill_w > PAGE_W - MARGIN:
                x = MARGIN
                y += 7
            self.set_fill_color(sr, sg, sb)
            self.set_text_color(255, 255, 255)
            self.set_xy(x, y)
            self.cell(pill_w, 6, label, fill=True, align="C")
            x += pill_w + 4

        self.set_y(y + 9)
        self.set_draw_color(*C_GRID_LINE)
        self.set_line_width(0.2)
        self.line(MARGIN, self.get_y(), PAGE_W - MARGIN, self.get_y())
        self.ln(5)

    def _field(self, label: str, value: str, label_w: float = 40,
               value_color: tuple = C_DARK, bold_value: bool = False) -> None:
        """Render a label + value row, wrapping value if needed."""
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*C_MUTED)
        self.cell(label_w, 5.5, label + ":", new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font("Helvetica", "B" if bold_value else "", 8)
        self.set_text_color(*value_color)
        val_w = CONTENT_W - label_w
        self.multi_cell(val_w, 5.5, _safe(value),
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*C_DARK)

    def add_uow_card(self, uow: dict) -> None:
        """Render one UoW summary card. Each card gets its own page."""
        self.add_page()

        status = uow.get("status", "unknown")
        sr, sg, sb = STATUS_COLORS.get(status, DEFAULT_STATUS_COLOR)
        anomalies = _detect_anomalies(uow)
        has_anomaly = bool(anomalies)

        # ── Anomaly banner (if any) ────────────────────────────────────────────
        if has_anomaly:
            self.set_fill_color(*C_ANOMALY_BG)
            self.set_draw_color(*C_ANOMALY_TEXT)
            self.set_line_width(0.5)
            self.rect(MARGIN, self.get_y(), CONTENT_W, 7, style="FD")
            self.set_x(MARGIN + 3)
            self.set_font("Helvetica", "B", 8.5)
            self.set_text_color(*C_ANOMALY_TEXT)
            anomaly_text = "ANOMALY: " + "  |  ".join(anomalies)
            self.cell(CONTENT_W - 6, 7, _safe(anomaly_text[:120]),
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_line_width(0.2)
            self.ln(3)

        # ── Title + status badge ────────────────────────────────────────────────
        summary = _safe(uow.get("summary") or uow.get("id", ""))
        badge_text = status.upper()
        badge_w = max(self.get_string_width(badge_text) + 10, 32)
        title_w = CONTENT_W - badge_w - 4

        self.set_font("Helvetica", "B", 12)
        self.set_text_color(*C_DARK)
        self.set_x(MARGIN)

        # Truncate title to fit available width
        while summary and self.get_string_width(summary) > title_w - 2:
            summary = summary[:-4] + "..."

        y_title = self.get_y()
        self.set_xy(MARGIN, y_title)
        self.cell(title_w, 9, summary, new_x=XPos.RIGHT, new_y=YPos.TOP)

        # Status badge — filled pill
        self.set_fill_color(sr, sg, sb)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(badge_w, 9, badge_text, fill=True, align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*C_DARK)
        self.ln(1)

        # Separator
        self.set_draw_color(*C_GRID_LINE)
        self.set_line_width(0.2)
        self.line(MARGIN, self.get_y(), MARGIN + CONTENT_W, self.get_y())
        self.ln(4)

        # ── Identity block ──────────────────────────────────────────────────────
        self._field("ID", uow.get("id", ""))
        self._field("Type", uow.get("type") or "executable")

        source_url = _source_url(uow.get("source"), uow.get("source_issue_number"))
        source_display = uow.get("source") or source_url or "(none)"
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*C_MUTED)
        self.cell(40, 5.5, "Source:", new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font("Helvetica", "", 8)
        val_w = CONTENT_W - 40
        if source_url:
            self.set_text_color(*C_LINK)
            disp = _safe(source_display)
            while disp and self.get_string_width(disp) > val_w - 4:
                disp = disp[:-4] + "..."
            self.cell(val_w, 5.5, disp, link=source_url,
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            self.set_text_color(*C_DARK)
            self.multi_cell(val_w, 5.5, _safe(source_display),
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*C_DARK)

        self.ln(3)

        # ── Timing block ────────────────────────────────────────────────────────
        self.set_fill_color(*C_HEADING_BG)
        self.set_x(MARGIN)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*C_HEADING_TEXT)
        self.cell(CONTENT_W, 5.5, "  EXECUTION TIME",
                  fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1.5)

        created_ts = uow.get("created_at", "")
        started_ts = uow.get("started_at", "")
        completed_ts = uow.get("completed_at", "")
        updated_ts = uow.get("updated_at", "")

        self._field("Created", _fmt_ts(created_ts))
        if started_ts:
            self._field("Started", _fmt_ts(started_ts))
        if completed_ts:
            self._field("Completed", _fmt_ts(completed_ts))
            duration = _duration_str(started_ts or created_ts, completed_ts)
            if duration:
                self._field("Total duration", duration, bold_value=True)
        else:
            self._field("Last updated", _fmt_ts(updated_ts))
            if started_ts:
                duration = _duration_str(started_ts, updated_ts)
                if duration:
                    self._field("Elapsed (so far)", duration)

        self.ln(3)

        # ── Steward cycles health signal ────────────────────────────────────────
        steward_cycles = uow.get("steward_cycles", 0) or 0
        self.set_fill_color(*C_HEADING_BG)
        self.set_x(MARGIN)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*C_HEADING_TEXT)
        self.cell(CONTENT_W, 5.5, "  STEWARD HEALTH",
                  fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1.5)

        if steward_cycles == 0:
            cycle_color = C_MUTED
            cycle_signal = "0 — not yet processed"
        elif steward_cycles == 1:
            cycle_color = C_CYCLE_OK
            cycle_signal = "1 — healthy (single-pass)"
        else:
            cycle_color = C_CYCLE_WARN
            cycle_signal = f"{steward_cycles} — investigate (multiple cycles)"

        self._field("Steward cycles", cycle_signal, value_color=cycle_color, bold_value=True)

        skills = uow.get("_prescribed_skills", [])
        if skills:
            self._field("Skills loaded", ", ".join(skills))

        self.ln(3)

        # ── PRSC reason ─────────────────────────────────────────────────────────
        log_events = uow.get("_steward_log_events", [])
        prsc_reason = _extract_first_prsc_reason(log_events)
        success_criteria = (uow.get("success_criteria") or "").strip()

        self.set_fill_color(*C_HEADING_BG)
        self.set_x(MARGIN)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*C_HEADING_TEXT)
        self.cell(CONTENT_W, 5.5, "  PRESCRIPTION & CRITERIA",
                  fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1.5)

        if prsc_reason:
            self._field("PRSC reason", prsc_reason)
        else:
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(*C_MUTED)
            self.set_x(MARGIN)
            self.cell(CONTENT_W, 5.5, "No prescription recorded yet",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(*C_DARK)

        if success_criteria:
            self._field("Success criteria", success_criteria)

        self.ln(3)

        # ── Outcome ─────────────────────────────────────────────────────────────
        outcome = _extract_outcome_summary(log_events, uow)

        self.set_fill_color(*C_HEADING_BG)
        self.set_x(MARGIN)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*C_HEADING_TEXT)
        self.cell(CONTENT_W, 5.5, "  OUTCOME",
                  fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1.5)

        if status in TERMINAL_STATUSES:
            status_display = status.upper()
            outcome_color = C_DONE if status == "done" else C_ANOMALY_TEXT
            self._field("Final status", status_display,
                        value_color=outcome_color, bold_value=True)

        if outcome:
            self._field("Summary", outcome)
        else:
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(*C_MUTED)
            self.set_x(MARGIN)
            self.cell(CONTENT_W, 5.5, "No outcome recorded",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(*C_DARK)

        # Output ref (if any)
        output_ref = uow.get("output_ref")
        if output_ref:
            self._field("Output ref", output_ref)

        if has_anomaly:
            self.ln(3)
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(*C_ANOMALY_TEXT)
            self.set_x(MARGIN)
            self.cell(CONTENT_W, 5, "See full report for complete logs and audit trail.",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(*C_DARK)


def _render_index_page(pdf: SummaryReport, uows: list[dict]) -> None:
    """Render the registry snapshot + index table on the first page."""
    pdf._render_registry_snapshot()

    # Index table
    col_widths = [14, 70, 22, 28, 14, 14]
    col_headers = ["#", "Summary", "Status", "Created", "Cycles", "Anomaly"]

    pdf.set_fill_color(*C_HEADING_BG)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(*C_HEADING_TEXT)
    hx = MARGIN
    hy = pdf.get_y()
    for w, h in zip(col_widths, col_headers):
        pdf.set_xy(hx, hy)
        pdf.cell(w, 5.5, h, border=0, fill=True, align="L")
        hx += w
    pdf.ln(6)

    for i, uow in enumerate(uows):
        row_y = pdf.get_y()
        row_bg = (250, 250, 252) if i % 2 == 0 else (255, 255, 255)
        pdf.set_fill_color(*row_bg)
        pdf.rect(MARGIN, row_y, sum(col_widths), 5.5, style="F")

        status = uow.get("status", "")
        sr, sg, sb = STATUS_COLORS.get(status, DEFAULT_STATUS_COLOR)
        anomalies = _detect_anomalies(uow)

        values = [
            str(i + 1),
            _safe((uow.get("summary") or "")[:65]),
            status.upper()[:14],
            _fmt_ts(uow.get("created_at"), short=True),
            str(uow.get("steward_cycles", 0) or 0),
            "YES" if anomalies else "-",
        ]
        aligns = ["C", "L", "L", "L", "C", "C"]

        rx = MARGIN
        for j, (w, val, align) in enumerate(zip(col_widths, values, aligns)):
            pdf.set_xy(rx, row_y)
            if j == 2:
                pdf.set_text_color(sr, sg, sb)
                pdf.set_font("Helvetica", "B", 7)
            elif j == 5 and anomalies:
                pdf.set_text_color(*C_ANOMALY_TEXT)
                pdf.set_font("Helvetica", "B", 7)
            else:
                pdf.set_text_color(*C_DARK)
                pdf.set_font("Helvetica", "", 7)
            pdf.cell(w, 5.5, val, border=0, align=align)
            rx += w
        pdf.ln(5.5)

    pdf.set_text_color(*C_DARK)
    pdf.ln(3)


def generate_pdf(uows: list[dict], output_path: Path) -> Path:
    """Render the summary PDF and save to output_path."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    status_counts: dict[str, int] = {}
    for u in uows:
        s = u.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    pdf = SummaryReport(
        total=len(uows),
        generated_at=generated_at,
        status_counts=status_counts,
    )
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    if not uows:
        pdf.set_font("Helvetica", "I", 11)
        pdf.set_text_color(*C_MUTED)
        pdf.cell(0, 10, "No units of work found.",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    else:
        _render_index_page(pdf, uows)
        for uow in uows:
            pdf.add_uow_card(uow)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path


# ── Full markdown report ───────────────────────────────────────────────────────

def generate_full_report(uows: list[dict], output_path: Path) -> Path:
    """
    Write a full investigation-grade markdown report.
    All DB fields, full JSON logs, audit trail, output_ref content.
    No truncation of any field.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []

    lines.append(f"# WOS Full Investigation Report")
    lines.append(f"")
    lines.append(f"Generated: {generated_at}")
    lines.append(f"UoWs included: {len(uows)}")
    lines.append(f"")

    # Registry snapshot
    status_counts: dict[str, int] = {}
    for u in uows:
        s = u.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    lines.append("## Registry Snapshot")
    lines.append("")
    for status, count in sorted(status_counts.items()):
        lines.append(f"- **{status}**: {count}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Per-UoW sections
    for uow in uows:
        uow_id = uow.get("id", "unknown")
        status = uow.get("status", "unknown")
        anomalies = _detect_anomalies(uow)

        lines.append(f"## {uow_id}")
        lines.append("")

        if anomalies:
            lines.append("> **ANOMALY DETECTED**")
            for a in anomalies:
                lines.append(f"> - {a}")
            lines.append("")

        # ── Core fields ────────────────────────────────────────────────────────
        lines.append("### Core Fields")
        lines.append("")

        core_fields = [
            ("id", "ID"),
            ("status", "Status"),
            ("type", "Type"),
            ("posture", "Posture"),
            ("source", "Source"),
            ("summary", "Summary"),
            ("success_criteria", "Success Criteria"),
            ("route_reason", "Route Reason"),
            ("output_ref", "Output Ref"),
            ("workflow_artifact", "Workflow Artifact"),
            ("steward_cycles", "Steward Cycles"),
            ("created_at", "Created"),
            ("started_at", "Started"),
            ("completed_at", "Completed"),
            ("updated_at", "Updated"),
        ]
        for db_key, label in core_fields:
            val = uow.get(db_key)
            if val is not None and str(val).strip():
                lines.append(f"**{label}**: {val}")
        lines.append("")

        # Timing summary
        started_ts = uow.get("started_at", "")
        completed_ts = uow.get("completed_at", "")
        created_ts = uow.get("created_at", "")
        duration = _duration_str(started_ts or created_ts, completed_ts or uow.get("updated_at", ""))
        if duration:
            lines.append(f"**Total duration**: {duration}")
            lines.append("")

        # ── Notes ──────────────────────────────────────────────────────────────
        notes_raw = uow.get("notes")
        if notes_raw:
            lines.append("### Notes")
            lines.append("")
            lines.append("```json")
            try:
                lines.append(json.dumps(json.loads(notes_raw), indent=2))
            except Exception:
                lines.append(str(notes_raw))
            lines.append("```")
            lines.append("")

        # ── Steward log ────────────────────────────────────────────────────────
        steward_log_raw = uow.get("steward_log")
        if steward_log_raw:
            lines.append("### Steward Log (full)")
            lines.append("")
            for ev in uow.get("_steward_log_events", []):
                lines.append("```json")
                lines.append(json.dumps(ev, indent=2))
                lines.append("```")
                lines.append("")
        else:
            lines.append("### Steward Log")
            lines.append("")
            lines.append("_(no steward log recorded)_")
            lines.append("")

        # ── Audit log ─────────────────────────────────────────────────────────
        audit_events = uow.get("_audit_events", [])
        lines.append("### Audit Log (full)")
        lines.append("")
        if audit_events:
            lines.append("| Timestamp | Event | From | To | Note |")
            lines.append("|-----------|-------|------|----|------|")
            for ev in audit_events:
                ts = ev.get("ts", "")
                event = ev.get("event", "")
                from_s = ev.get("from_status", "") or ""
                to_s = ev.get("to_status", "") or ""
                note = (ev.get("note") or "").replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {ts} | {event} | {from_s} | {to_s} | {note} |")
        else:
            lines.append("_(no audit events)_")
        lines.append("")

        # ── Steward agenda ─────────────────────────────────────────────────────
        steward_agenda_raw = uow.get("steward_agenda")
        if steward_agenda_raw:
            lines.append("### Steward Agenda (full)")
            lines.append("")
            lines.append("```json")
            try:
                lines.append(json.dumps(json.loads(steward_agenda_raw), indent=2))
            except Exception:
                lines.append(steward_agenda_raw)
            lines.append("```")
            lines.append("")

        # ── Prescribed skills ──────────────────────────────────────────────────
        skills = uow.get("_prescribed_skills", [])
        if skills:
            lines.append("### Prescribed Skills")
            lines.append("")
            for sk in skills:
                lines.append(f"- {sk}")
            lines.append("")

        # ── Output ref content ─────────────────────────────────────────────────
        output_ref = uow.get("output_ref")
        if output_ref:
            lines.append("### Output Ref Content")
            lines.append("")
            output_path_ref = Path(output_ref)
            if output_path_ref.exists():
                try:
                    content = output_path_ref.read_text(errors="replace")
                    lines.append(f"_Path: `{output_ref}`_")
                    lines.append("")
                    lines.append("```")
                    lines.append(content)
                    lines.append("```")
                except Exception as exc:
                    lines.append(f"_Could not read output_ref: {exc}_")
            else:
                lines.append(f"_Path: `{output_ref}` — file does not exist_")
            lines.append("")

        # ── Workflow artifact content ──────────────────────────────────────────
        workflow_artifact = uow.get("workflow_artifact")
        if workflow_artifact:
            lines.append("### Workflow Artifact Content")
            lines.append("")
            artifact_path = Path(workflow_artifact)
            if artifact_path.exists():
                try:
                    content = artifact_path.read_text(errors="replace")
                    lines.append(f"_Path: `{workflow_artifact}`_")
                    lines.append("")
                    lines.append("```json")
                    try:
                        lines.append(json.dumps(json.loads(content), indent=2))
                    except Exception:
                        lines.append(content)
                    lines.append("```")
                except Exception as exc:
                    lines.append(f"_Could not read workflow artifact: {exc}_")
            else:
                lines.append(f"_Path: `{workflow_artifact}` — file does not exist_")
            lines.append("")

        lines.append("---")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ── Telegram delivery ─────────────────────────────────────────────────────────

def _load_bot_token() -> str:
    """Load TELEGRAM_BOT_TOKEN from the environment or ~/lobster-config/config.env."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return token
    config_file = Path.home() / "lobster-config" / "config.env"
    if config_file.exists():
        for line in config_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN not found in environment or ~/lobster-config/config.env"
    )


def send_document_direct(pdf_path: Path, chat_id: int, caption: str = "") -> None:
    """Send the PDF directly to Telegram via the Bot API (sendDocument)."""
    import mimetypes
    import urllib.request

    token = _load_bot_token()
    url = f"https://api.telegram.org/bot{token}/sendDocument"

    boundary = "lobster-wos-report-boundary"
    file_bytes = pdf_path.read_bytes()
    mime_type = mimetypes.guess_type(str(pdf_path))[0] or "application/pdf"

    def _field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    body = (
        _field("chat_id", str(chat_id))
        + _field("caption", caption)
        + (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{pdf_path.name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode()
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Telegram API returned non-JSON response: {raw!r}") from exc
    if not response.get("ok"):
        raise RuntimeError(f"Telegram API error: {response}")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Generate WOS Registry reports (summary PDF and/or full markdown)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--chat-id", type=int, default=DEFAULT_CHAT_ID,
                        help="Telegram chat ID to send PDF to (default: %(default)s)")
    parser.add_argument("--output", type=Path,
                        help="Output PDF path — generates summary PDF (default: auto-named in ~/messages/documents/)")
    parser.add_argument("--full-output", type=Path,
                        help="Output markdown path — generates full investigation report")
    parser.add_argument("--no-send", action="store_true",
                        help="Generate PDF but do not send to Telegram")
    parser.add_argument("--status", type=str, default=None,
                        help="Filter by status (e.g. active, done, proposed; default: all)")
    parser.add_argument("--since", type=str, default=None,
                        metavar="YYYY-MM-DD",
                        help="Include only UoWs created on or after this date")
    parser.add_argument("--ids", type=str, default=None,
                        metavar="ID1,ID2,...",
                        help="Include only the specified UoW IDs (comma-separated)")
    args = parser.parse_args(argv)

    # Parse --ids into a list (None means no ID filter)
    ids_filter: list[str] | None = None
    if args.ids:
        ids_filter = [i.strip() for i in args.ids.split(",") if i.strip()]

    # Require at least one output flag
    if not args.output and not args.full_output:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        pdf_dir = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")) / "documents"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        args.output = pdf_dir / f"wos-report-{ts}.pdf"

    print(f"Loading registry from {REGISTRY_DB}...")
    uows = fetch_uows(
        status_filter=args.status,
        since=args.since,
        ids=ids_filter,
    )
    print(f"Found {len(uows)} unit(s) of work")

    output_pdf: Path | None = None
    output_md: Path | None = None

    if args.output:
        output_pdf = Path(args.output)
        print(f"Generating summary PDF: {output_pdf}")
        generate_pdf(uows, output_pdf)
        print(f"Summary PDF written: {output_pdf} ({output_pdf.stat().st_size:,} bytes)")

    if args.full_output:
        output_md = Path(args.full_output)
        print(f"Generating full report: {output_md}")
        generate_full_report(uows, output_md)
        print(f"Full report written: {output_md} ({output_md.stat().st_size:,} bytes)")

    if output_pdf and not args.no_send:
        filters: list[str] = []
        if args.status:
            filters.append(f"status={args.status}")
        if args.since:
            filters.append(f"since={args.since}")
        if ids_filter:
            filters.append(f"ids={len(ids_filter)}")
        filter_str = ", ".join(filters)
        caption = f"WOS Registry ({len(uows)} UoWs"
        if filter_str:
            caption += f", {filter_str}"
        caption += f") -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        send_document_direct(output_pdf, args.chat_id, caption)
        print(f"Sent summary PDF to Telegram chat {args.chat_id}")
    elif output_pdf:
        print("--no-send: skipping Telegram delivery")

    results = []
    if output_pdf:
        results.append(str(output_pdf))
    if output_md:
        results.append(str(output_md))
    return results[0] if len(results) == 1 else results


if __name__ == "__main__":
    main()
