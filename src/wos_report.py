#!/usr/bin/env python3
"""
wos_report.py — Generate a PDF of the WOS Registry and send it to Telegram.

Usage:
    uv run ~/lobster/src/wos_report.py [--chat-id CHAT_ID] [--output PATH] [--no-send]

Options:
    --chat-id   Telegram chat ID to send to (default: 8075091586)
    --output    Output file path (default: /tmp/wos-report-<timestamp>.pdf)
    --no-send   Generate PDF but do not queue for sending
    --status    Filter by status (default: all)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

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
    "done":               (100, 170,  90),
    "failed":             (200,  60,  60),
    "expired":            (140, 120, 100),
}
DEFAULT_STATUS_COLOR = (120, 120, 120)

PAGE_W = 210          # A4 mm
MARGIN = 14           # mm left/right/top
CONTENT_W = PAGE_W - MARGIN * 2
CARD_PAD = 5          # mm inner padding
LABEL_W = 32          # mm for field labels


# ── text helpers ──────────────────────────────────────────────────────────────

# Unicode replacements that keep text readable when forced through latin-1
_UNICODE_MAP = str.maketrans({
    "\u2014": "--",   # em dash
    "\u2013": "-",    # en dash
    "\u2019": "'",    # right single quote
    "\u2018": "'",    # left single quote
    "\u201c": '"',    # left double quote
    "\u201d": '"',    # right double quote
    "\u2022": "*",    # bullet
    "\u2026": "...",  # ellipsis
    "\u00e9": "e",    # e acute
    "\u00e8": "e",    # e grave
    "\u00e0": "a",    # a grave
    "\u00fc": "u",    # u umlaut
    "\u00f6": "o",    # o umlaut
    "\u00e4": "a",    # a umlaut
    "\u00df": "ss",   # sharp s
})


def _safe(text: str) -> str:
    """Replace non-latin-1 characters with ASCII equivalents."""
    text = text.translate(_UNICODE_MAP)
    # For anything still outside latin-1, replace with ?
    return "".join(
        c if ord(c) < 256 else "?"
        for c in unicodedata.normalize("NFKD", text)
        if ord(c) < 256 or unicodedata.category(c) not in ("Mn",)
    )


# ── data layer ────────────────────────────────────────────────────────────────

def _source_url(source: str | None, issue_number: int | None) -> str:
    """Derive a GitHub URL from the source field."""
    if issue_number:
        return f"https://github.com/{GITHUB_REPO}/issues/{issue_number}"
    if source and source.startswith("github:issue/"):
        num = source.split("/")[-1]
        return f"https://github.com/{GITHUB_REPO}/issues/{num}"
    return source or ""


def _fmt_ts(ts: str | None) -> str:
    """Format a timestamp string to YYYY-MM-DD HH:MM UTC, or return empty string."""
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
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ts[:16] if ts else ""


def _fmt_audit_event(row: dict) -> str:
    """Format a single audit row into a concise one-liner."""
    ts = _fmt_ts(row.get("ts"))
    event = row.get("event", "")
    from_s = row.get("from_status") or ""
    to_s = row.get("to_status") or ""
    note = row.get("note") or ""

    if note and note.startswith("{"):
        try:
            n = json.loads(note)
            note = n.get("assessment") or n.get("return_reason") or n.get("reason") or ""
        except Exception:
            pass
    note = note[:60] + "..." if len(note) > 60 else note

    if from_s and to_s:
        line = f"{ts}  {event}: {from_s} -> {to_s}"
    else:
        line = f"{ts}  {event}"
    if note:
        line += f"  ({note})"
    return _safe(line)


def fetch_uows(status_filter: str | None = None) -> list[dict]:
    """Load UoWs from the registry DB, sorted by created_at descending."""
    if not REGISTRY_DB.exists():
        raise FileNotFoundError(f"Registry DB not found: {REGISTRY_DB}")
    conn = sqlite3.connect(str(REGISTRY_DB))
    conn.row_factory = sqlite3.Row
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM uow_registry WHERE status=? ORDER BY created_at DESC",
                (status_filter,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM uow_registry ORDER BY created_at DESC"
            ).fetchall()

        uows = []
        for row in rows:
            d = dict(row)
            audits = conn.execute(
                "SELECT ts, event, from_status, to_status, note "
                "FROM audit_log WHERE uow_id=? ORDER BY ts DESC LIMIT 4",
                (d["id"],)
            ).fetchall()
            d["_audit_events"] = [dict(a) for a in reversed(audits)]
            uows.append(d)
        return uows
    finally:
        conn.close()


# ── PDF builder ───────────────────────────────────────────────────────────────

class WoSReport(FPDF):
    """A4 PDF report of WOS Registry UoWs."""

    def __init__(self, total: int, generated_at: str):
        super().__init__()
        self._total = total
        self._generated_at = generated_at

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(30, 30, 30)
        self.cell(0, 10, "WOS Registry",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 5, f"Generated {self._generated_at}  |  {self._total} unit(s)",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
        self.ln(3)
        self.set_draw_color(200, 200, 200)
        self.set_line_width(0.3)
        self.line(MARGIN, self.get_y(), PAGE_W - MARGIN, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(160, 160, 160)
        self.cell(0, 5, f"WOS Registry  |  {self._generated_at}  |  Page {self.page_no()}",
                  align="C")

    # ── field row ──────────────────────────────────────────────────────────────

    def _label_value(self, card_x: float, label: str, value: str, url: str = ""):
        """Render a label + value row with proper indentation."""
        self.set_x(card_x + CARD_PAD)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(100, 100, 100)
        self.cell(LABEL_W, 5, label + ":",
                  new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font("Helvetica", "", 8)
        val_w = CONTENT_W - CARD_PAD * 2 - LABEL_W
        if url:
            self.set_text_color(30, 80, 180)
            disp = _safe(value)
            while disp and self.get_string_width(disp) > val_w - 4:
                disp = disp[:-4] + "..."
            self.cell(val_w, 5, disp, link=url,
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(30, 30, 30)
        else:
            self.set_text_color(30, 30, 30)
            self.multi_cell(val_w, 5, _safe(value),
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def _render_audit_line(self, card_x: float, line: str):
        self.set_x(card_x + CARD_PAD + 3)
        self.set_font("Courier", "", 7)
        self.set_text_color(80, 80, 80)
        self.multi_cell(CONTENT_W - CARD_PAD * 2 - 3, 4.5, line,
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ── card ───────────────────────────────────────────────────────────────────

    def add_uow_card(self, uow: dict):
        """Add one UoW card. Starts a new page if needed."""
        ESTIMATED_CARD_H = 72
        if self.get_y() + ESTIMATED_CARD_H > self.h - 20:
            self.add_page()

        card_x = MARGIN
        card_y = self.get_y()
        card_w = CONTENT_W

        # Light background rectangle (height estimated; redrawn as outline after)
        self.set_fill_color(248, 249, 250)
        self.set_draw_color(220, 220, 220)
        self.set_line_width(0.3)
        self.rect(card_x, card_y, card_w, ESTIMATED_CARD_H, style="F")

        # ── Title row ──────────────────────────────────────────────────────────
        self.set_xy(card_x + CARD_PAD, card_y + CARD_PAD)
        summary = _safe(uow.get("summary") or uow.get("id", ""))
        title_w = card_w - CARD_PAD * 2 - 42   # leave room for badge

        self.set_font("Helvetica", "B", 10)
        self.set_text_color(20, 20, 20)
        while summary and self.get_string_width(summary) > title_w:
            summary = summary[:-4] + "..."
        self.cell(title_w, 7, summary,
                  new_x=XPos.RIGHT, new_y=YPos.TOP)

        # Status badge (right side of title row)
        status = uow.get("status", "unknown")
        r, g, b = STATUS_COLORS.get(status, DEFAULT_STATUS_COLOR)
        self.set_fill_color(r, g, b)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 7.5)
        badge_text = status.upper()
        badge_w = max(self.get_string_width(badge_text) + 6, 26)
        self.cell(badge_w, 7, badge_text, fill=True, align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(30, 30, 30)

        self.ln(1)

        # ── Fields ─────────────────────────────────────────────────────────────
        self._label_value(card_x, "ID", uow.get("id", ""))

        source_url = _source_url(uow.get("source"), uow.get("source_issue_number"))
        source_display = uow.get("source") or source_url
        self._label_value(card_x, "Source", source_display or "(none)", url=source_url)

        self._label_value(card_x, "Steward cycles", str(uow.get("steward_cycles", 0)))
        self._label_value(card_x, "Created", _fmt_ts(uow.get("created_at")))
        self._label_value(card_x, "Updated", _fmt_ts(uow.get("updated_at")))

        # ── Lifecycle ──────────────────────────────────────────────────────────
        audit_events = uow.get("_audit_events", [])
        if audit_events:
            self.ln(1.5)
            self.set_x(card_x + CARD_PAD)
            self.set_font("Helvetica", "B", 7.5)
            self.set_text_color(100, 100, 100)
            self.cell(0, 4.5, "Lifecycle (last events):",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            for ev in audit_events[-4:]:
                self._render_audit_line(card_x, _fmt_audit_event(ev))

        # ── Outline over background rect ───────────────────────────────────────
        actual_h = self.get_y() + CARD_PAD - card_y
        self.set_draw_color(210, 210, 220)
        self.set_line_width(0.4)
        self.rect(card_x, card_y, card_w, actual_h, style="D")
        self.set_y(card_y + actual_h + 4)


# ── PDF generation ────────────────────────────────────────────────────────────

def generate_pdf(uows: list[dict], output_path: Path) -> Path:
    """Render the WOS report PDF and save it to output_path."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pdf = WoSReport(total=len(uows), generated_at=generated_at)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    if not uows:
        pdf.set_font("Helvetica", "I", 11)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 10, "No units of work found.",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    else:
        for uow in uows:
            pdf.add_uow_card(uow)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path


# ── Telegram delivery ─────────────────────────────────────────────────────────

def queue_for_telegram(pdf_path: Path, chat_id: int, caption: str = "") -> Path:
    """Write an outbox JSON file that instructs lobster_bot to send this PDF."""
    import uuid
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    msg_id = uuid.uuid4().hex[:12]
    outbox_file = OUTBOX_DIR / f"wos-report-{msg_id}.json"
    payload = {
        "chat_id": chat_id,
        "type": "document",
        "document_path": str(pdf_path),
        "filename": pdf_path.name,
        "caption": caption,
        "mime_type": "application/pdf",
    }
    tmp = outbox_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.rename(outbox_file)
    return outbox_file


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Generate WOS Registry PDF report")
    parser.add_argument("--chat-id", type=int, default=DEFAULT_CHAT_ID,
                        help="Telegram chat ID to send to")
    parser.add_argument("--output", type=Path,
                        help="Output PDF path (default: auto-named in /tmp)")
    parser.add_argument("--no-send", action="store_true",
                        help="Generate PDF but do not queue for Telegram delivery")
    parser.add_argument("--status", type=str, default=None,
                        help="Filter by status (e.g. active, done, proposed)")
    args = parser.parse_args(argv)

    if args.output:
        output_path = args.output
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = Path(tempfile.gettempdir()) / f"wos-report-{ts}.pdf"

    print(f"Loading registry from {REGISTRY_DB}...")
    uows = fetch_uows(status_filter=args.status)
    print(f"Found {len(uows)} unit(s) of work")

    print(f"Generating PDF: {output_path}")
    generate_pdf(uows, output_path)
    print(f"PDF written: {output_path} ({output_path.stat().st_size:,} bytes)")

    if not args.no_send:
        caption = f"WOS Registry ({len(uows)} UoWs"
        if args.status:
            caption += f", status={args.status}"
        caption += f") -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        outbox_file = queue_for_telegram(output_path, args.chat_id, caption)
        print(f"Queued for Telegram delivery: {outbox_file}")
    else:
        print("--no-send: skipping Telegram delivery")

    return str(output_path)


if __name__ == "__main__":
    main()
