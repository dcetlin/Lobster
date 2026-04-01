#!/usr/bin/env python3
"""
wos_report.py — Generate a PDF of the WOS Registry and send it to Telegram.

Usage:
    uv run ~/lobster/src/wos_report.py [OPTIONS]

Options:
    --chat-id           Telegram chat ID to send to (default: 8075091586)
    --output PATH       Output PDF path (default: ~/messages/documents/wos-report-<timestamp>.pdf)
    --no-send           Generate PDF but do not queue for Telegram delivery
    --status STATUS     Filter by status (e.g. active, done, proposed; default: all)
    --since YYYY-MM-DD  Include only UoWs created on or after this date
    --ids id1,id2,...   Include only the specified UoW IDs (comma-separated)

Filters combine: --since and --ids are applied after --status.
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

# Canonical lifecycle state ordering for timeline display
LIFECYCLE_STATES = [
    "proposed", "pending", "ready-for-steward", "diagnosing",
    "ready-for-executor", "active", "done", "failed", "expired", "blocked",
]

PAGE_W = 210          # A4 mm
MARGIN = 14           # mm left/right/top
CONTENT_W = PAGE_W - MARGIN * 2
CARD_PAD = 5          # mm inner padding
LABEL_W = 34          # mm for field labels

# ── colour palette ─────────────────────────────────────────────────────────────
C_HEADING_BG   = (235, 240, 250)   # light blue for section headings
C_HEADING_TEXT = (50,  80, 140)    # dark blue for section heading text
C_GRID_LINE    = (210, 215, 225)   # subtle grid line
C_MUTED        = (120, 120, 130)   # muted labels
C_DARK         = (25,  25,  35)    # near-black body text
C_LINK         = (30,  80, 180)    # hyperlink blue
C_STEWARD_BG   = (235, 245, 255)   # steward section tint
C_EXECUTOR_BG  = (235, 250, 240)   # executor section tint
C_DONE_NODE    = ( 70, 160,  90)   # completed lifecycle node
C_PENDING_NODE = (190, 195, 210)   # future/unreached lifecycle node
C_ACTIVE_NODE  = ( 40, 140, 200)   # current-status lifecycle node


# ── text helpers ──────────────────────────────────────────────────────────────

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


def _parse_steward_agenda(raw: str | None) -> list[dict]:
    """Parse steward_agenda JSON array."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        return []


def _extract_lifecycle_from_audit(audit_events: list[dict]) -> list[dict]:
    """
    Build an ordered list of {status, ts} pairs from audit log events,
    deduplicated so each status appears only once (first occurrence).
    """
    transitions = []
    seen: set[str] = set()

    for ev in audit_events:
        event_type = ev.get("event", "")
        to_s = ev.get("to_status") or ""
        ts = ev.get("ts", "")

        if event_type == "created" and to_s and to_s not in seen:
            transitions.append({"status": to_s, "ts": ts})
            seen.add(to_s)
        elif event_type == "status_change" and to_s and to_s not in seen:
            transitions.append({"status": to_s, "ts": ts})
            seen.add(to_s)

    return transitions


def _build_steward_exchange(log_events: list[dict]) -> list[dict]:
    """
    Extract the steward-executor back-and-forth from steward_log events.
    Returns list of {event, cycle, ts, posture, assessment, return_reason, ...}.
    """
    exchange = []
    for ev in log_events:
        event_type = ev.get("event", "")
        if event_type in ("diagnosis", "prescription", "reentry_prescription",
                          "steward_closure", "agenda_update"):
            exchange.append({
                "event": event_type,
                "cycle": ev.get("steward_cycles", 0),
                "ts": ev.get("timestamp", ""),
                "posture": ev.get("re_entry_posture") or "",
                "assessment": (ev.get("completion_rationale")
                               or ev.get("completion_assessment")
                               or ev.get("assessment")
                               or ""),
                "rationale": ev.get("next_posture_rationale") or "",
                "is_complete": ev.get("is_complete", False),
                "return_reason": ev.get("return_reason") or "",
            })
    return exchange


def fetch_uows(
    status_filter: str | None = None,
    since: str | None = None,
    ids: list[str] | None = None,
) -> list[dict]:
    """Load UoWs from the registry DB, sorted by created_at descending.

    Args:
        status_filter: If given, only UoWs with this exact status are returned.
        since:         If given (ISO date string YYYY-MM-DD), only UoWs whose
                       created_at is >= this date are returned.
        ids:           If given, only UoWs whose id is in this list are returned.
                       Applied after status_filter and since.
    """
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
            # Normalize to ISO datetime so string comparison works regardless of
            # whether the DB stores timestamps with or without the time component.
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
            # Apply --ids filter in Python (simpler than a parameterised IN clause)
            if ids is not None and d["id"] not in ids:
                continue

            # All audit events (chronological) for lifecycle reconstruction
            audits = conn.execute(
                "SELECT ts, event, from_status, to_status, note "
                "FROM audit_log WHERE uow_id=? ORDER BY ts ASC",
                (d["id"],)
            ).fetchall()
            d["_audit_events"] = [dict(a) for a in audits]

            # Parse steward data
            d["_steward_log_events"] = _parse_steward_log(d.get("steward_log"))
            d["_steward_agenda_list"] = _parse_steward_agenda(d.get("steward_agenda"))

            # Parse prescribed skills
            try:
                d["_prescribed_skills"] = json.loads(d.get("prescribed_skills") or "[]")
            except Exception:
                d["_prescribed_skills"] = []

            uows.append(d)
        return uows
    finally:
        conn.close()


# ── PDF builder ───────────────────────────────────────────────────────────────

class WoSReport(FPDF):
    """A4 PDF report of WOS Registry UoWs — rich visual layout."""

    def __init__(self, total: int, generated_at: str):
        super().__init__()
        self._total = total
        self._generated_at = generated_at

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*C_DARK)
        self.cell(0, 10, "WOS Registry",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*C_MUTED)
        self.cell(0, 5, f"Generated {self._generated_at}  |  {self._total} unit(s)",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
        self.ln(3)
        self.set_draw_color(*C_GRID_LINE)
        self.set_line_width(0.3)
        self.line(MARGIN, self.get_y(), PAGE_W - MARGIN, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*C_MUTED)
        self.cell(0, 5, f"WOS Registry  |  {self._generated_at}  |  Page {self.page_no()}",
                  align="C")

    # ── section heading ────────────────────────────────────────────────────────

    def _section_heading(self, card_x: float, label: str,
                         bg_color: tuple = C_HEADING_BG):
        """Render a tinted full-width section heading bar."""
        x = card_x + CARD_PAD
        w = CONTENT_W - CARD_PAD * 2
        y = self.get_y() + 2
        self.set_fill_color(*bg_color)
        self.rect(x, y, w, 5.5, style="F")
        self.set_xy(x + 2, y + 0.5)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*C_HEADING_TEXT)
        self.cell(w - 4, 4.5, label.upper(),
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*C_DARK)
        self.ln(1)

    # ── field row ──────────────────────────────────────────────────────────────

    def _label_value(self, card_x: float, label: str, value: str, url: str = ""):
        """Render a label + value row."""
        self.set_x(card_x + CARD_PAD)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*C_MUTED)
        self.cell(LABEL_W, 5, label + ":",
                  new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font("Helvetica", "", 8)
        val_w = CONTENT_W - CARD_PAD * 2 - LABEL_W
        if url:
            self.set_text_color(*C_LINK)
            disp = _safe(value)
            while disp and self.get_string_width(disp) > val_w - 4:
                disp = disp[:-4] + "..."
            self.cell(val_w, 5, disp, link=url,
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(*C_DARK)
        else:
            self.set_text_color(*C_DARK)
            self.multi_cell(val_w, 5, _safe(value),
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ── lifecycle timeline ─────────────────────────────────────────────────────

    def _render_lifecycle_timeline(self, card_x: float, uow: dict):
        """
        Render a visual timeline of status transitions as a row of labelled nodes.
        Reached states are coloured; unreached states are greyed out.
        Current status node uses the status badge colour.
        """
        audit_events = uow.get("_audit_events", [])
        transitions = _extract_lifecycle_from_audit(audit_events)
        current_status = uow.get("status", "")

        # Build reached set (status -> ts)
        reached: dict[str, str] = {t["status"]: t["ts"] for t in transitions}
        if current_status and current_status not in reached:
            reached[current_status] = uow.get("updated_at", "")

        # Determine node sequence: canonical order, then any extra states
        node_states = [s for s in LIFECYCLE_STATES
                       if s in reached or s == current_status]
        for s in reached:
            if s not in node_states:
                node_states.append(s)

        if not node_states:
            return

        self._section_heading(card_x, "Lifecycle Timeline")

        x_start = card_x + CARD_PAD + 4
        avail_w = CONTENT_W - CARD_PAD * 2 - 8
        y_base = self.get_y()

        n = len(node_states)
        spacing = min(avail_w / max(n, 1), 22)
        node_r = 2.0

        # Connecting line
        if n > 1:
            line_y = y_base + node_r + 0.5
            self.set_draw_color(*C_PENDING_NODE)
            self.set_line_width(0.5)
            self.line(x_start + node_r,
                      line_y,
                      x_start + (n - 1) * spacing + node_r,
                      line_y)

        for i, state in enumerate(node_states):
            nx = x_start + i * spacing
            ny = y_base

            is_current = (state == current_status)
            is_terminal_current = is_current and state in ("done", "failed", "expired")
            is_reached = state in reached

            if is_terminal_current:
                r, g, b = STATUS_COLORS.get(state, C_DONE_NODE)
            elif is_current:
                r, g, b = C_ACTIVE_NODE
            elif is_reached:
                r, g, b = C_DONE_NODE
            else:
                r, g, b = C_PENDING_NODE

            self.set_fill_color(r, g, b)
            self.set_draw_color(r, g, b)
            self.ellipse(nx, ny, node_r * 2, node_r * 2, style="F")

            # Abbreviated label (max 5 chars)
            abbrev = {
                "proposed": "prop",
                "pending": "pend",
                "ready-for-steward": "rfs",
                "ready-for-executor": "rfe",
                "active": "actv",
                "diagnosing": "diag",
                "blocked": "blkd",
                "done": "done",
                "failed": "fail",
                "expired": "expd",
            }.get(state, state[:4])

            self.set_font("Helvetica", "", 5.5)
            if is_reached or is_current:
                self.set_text_color(*C_DARK)
            else:
                self.set_text_color(*C_PENDING_NODE)

            lw = self.get_string_width(abbrev)
            self.set_xy(nx + node_r - lw / 2, ny + node_r * 2 + 0.5)
            self.cell(lw + 1, 3.5, abbrev)

            # Timestamp under label for reached states
            if is_reached and reached.get(state):
                ts_short = _fmt_ts(reached[state], short=True)
                self.set_font("Helvetica", "", 4.5)
                self.set_text_color(*C_MUTED)
                tsw = self.get_string_width(ts_short)
                self.set_xy(nx + node_r - tsw / 2, ny + node_r * 2 + 4.2)
                self.cell(tsw + 1, 3, ts_short)

        self.set_y(y_base + node_r * 2 + 10)
        self.set_text_color(*C_DARK)

    # ── prescription ──────────────────────────────────────────────────────────

    def _render_prescription(self, card_x: float, uow: dict):
        """
        Render what the steward prescribed to the executor:
        agenda posture entries and the instructions written to the workflow artifact.
        """
        agenda_list = uow.get("_steward_agenda_list", [])
        workflow_artifact_path = uow.get("workflow_artifact")
        instructions = ""

        if workflow_artifact_path:
            try:
                artifact = json.loads(Path(workflow_artifact_path).read_text())
                instructions = artifact.get("instructions", "")
            except Exception:
                pass

        agenda_lines = []
        for i, entry in enumerate(agenda_list):
            posture = entry.get("posture", "")
            context = entry.get("context", "")
            entry_status = entry.get("status", "")
            if posture and context:
                agenda_lines.append(
                    f"Step {i + 1} [{posture}]: {context[:120]}"
                    + (f" ({entry_status})" if entry_status else "")
                )

        if not instructions and not agenda_lines:
            return

        self._section_heading(card_x, "Steward Prescription", bg_color=C_STEWARD_BG)

        inner_x = card_x + CARD_PAD + 2
        inner_w = CONTENT_W - CARD_PAD * 2 - 4

        if agenda_lines:
            self.set_x(inner_x)
            self.set_font("Helvetica", "B", 7)
            self.set_text_color(*C_MUTED)
            self.cell(inner_w, 4, "Agenda:",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            for line in agenda_lines:
                self.set_x(inner_x + 2)
                self.set_font("Helvetica", "", 7.5)
                self.set_text_color(*C_DARK)
                self.multi_cell(inner_w - 2, 4.5, _safe(line),
                                new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        if instructions:
            self.ln(1)
            self.set_x(inner_x)
            self.set_font("Helvetica", "B", 7)
            self.set_text_color(*C_MUTED)
            self.cell(inner_w, 4, "Instructions to executor:",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            instr_display = (instructions[:400]
                             + ("..." if len(instructions) > 400 else ""))
            self.set_x(inner_x + 2)
            self.set_font("Courier", "", 7)
            self.set_text_color(55, 65, 90)
            self.multi_cell(inner_w - 2, 4.2, _safe(instr_display),
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        self.set_text_color(*C_DARK)
        self.ln(1)

    # ── steward-executor exchange ──────────────────────────────────────────────

    def _render_exchange(self, card_x: float, uow: dict):
        """
        Render the steward-executor back-and-forth per steward_cycles:
        diagnosis -> prescription -> executor -> re-diagnosis.
        """
        log_events = uow.get("_steward_log_events", [])
        exchange = _build_steward_exchange(log_events)

        if not exchange:
            return

        self._section_heading(card_x, "Steward <-> Executor Exchange")

        inner_x = card_x + CARD_PAD + 2
        inner_w = CONTENT_W - CARD_PAD * 2 - 4

        # Group by cycle number
        cycles: dict[int, list[dict]] = {}
        for ev in exchange:
            c = ev.get("cycle", 0)
            cycles.setdefault(c, []).append(ev)

        for cycle_num in sorted(cycles.keys()):
            events = cycles[cycle_num]

            self.set_x(inner_x)
            self.set_font("Helvetica", "B", 7.5)
            self.set_text_color(*C_HEADING_TEXT)
            self.cell(inner_w, 4.5, f"Cycle {cycle_num + 1}",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(*C_DARK)

            for ev in events:
                etype = ev.get("event", "")
                ts_short = _fmt_ts(ev.get("ts"), short=True)
                posture = ev.get("posture", "")
                assessment = ev.get("assessment", "")
                return_reason = ev.get("return_reason", "")

                if etype == "diagnosis":
                    prefix = "[DIAG]"
                    color = (75, 100, 165)
                elif etype in ("prescription", "reentry_prescription"):
                    prefix = "[PRSC]"
                    color = (75, 140, 75)
                elif etype == "steward_closure":
                    prefix = "[CLOS]"
                    color = (55, 145, 75)
                elif etype == "agenda_update":
                    prefix = "[AGND]"
                    color = (130, 85, 160)
                else:
                    prefix = f"[{etype[:4].upper()}]"
                    color = C_MUTED

                parts = []
                if posture:
                    parts.append(f"posture={posture[:35]}")
                if return_reason and return_reason != posture:
                    parts.append(f"reason={return_reason[:30]}")
                if assessment:
                    parts.append(assessment[:65])

                detail = "  ".join(parts)[:110]
                line = f"{prefix} {ts_short}  {detail}"

                self.set_x(inner_x + 3)
                self.set_font("Courier", "", 6.5)
                self.set_text_color(*color)
                self.multi_cell(inner_w - 3, 4, _safe(line),
                                new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            self.ln(0.5)

        self.set_text_color(*C_DARK)
        self.ln(1)

    # ── agenda stability & executor posture ────────────────────────────────────

    def _render_agenda_stability(self, card_x: float, uow: dict):
        """
        Show steward cycle count, agenda posture evolution, prescribed skills,
        and inferred executor posture.
        """
        steward_cycles = uow.get("steward_cycles", 0)
        prescribed_skills = uow.get("_prescribed_skills", [])
        agenda_list = uow.get("_steward_agenda_list", [])

        postures_seen = [a.get("posture", "") for a in agenda_list if a.get("posture")]
        unique_postures = list(dict.fromkeys(postures_seen))  # ordered dedup

        if len(unique_postures) <= 1:
            stability = "stable"
        elif len(unique_postures) <= 2:
            stability = "evolved"
        else:
            stability = "highly evolved"

        # Executor posture from workflow artifact first, then agenda inference
        executor_posture = ""
        workflow_artifact_path = uow.get("workflow_artifact")
        if workflow_artifact_path:
            try:
                artifact = json.loads(Path(workflow_artifact_path).read_text())
                executor_posture = artifact.get("executor_type", "")
            except Exception:
                pass
        if not executor_posture:
            for p in unique_postures:
                if p not in ("pending_evaluation",):
                    executor_posture = p
                    break

        # Skip section if no meaningful data beyond the default
        has_data = (steward_cycles > 0 or prescribed_skills or
                    executor_posture or len(unique_postures) > 1)
        if not has_data:
            # Still show skills if present even on cycle-0 UoWs
            if not prescribed_skills:
                return

        self._section_heading(card_x, "Executor Posture & Agenda", bg_color=C_EXECUTOR_BG)

        inner_x = card_x + CARD_PAD + 2
        inner_w = CONTENT_W - CARD_PAD * 2 - 4

        def _row(label: str, value: str, value_color: tuple = C_DARK):
            self.set_x(inner_x)
            self.set_font("Helvetica", "B", 7.5)
            self.set_text_color(*C_MUTED)
            self.cell(32, 4.5, label,
                      new_x=XPos.RIGHT, new_y=YPos.TOP)
            self.set_font("Helvetica", "", 7.5)
            self.set_text_color(*value_color)
            self.cell(inner_w - 32, 4.5, _safe(value),
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(*C_DARK)

        _row("Steward cycles:", str(steward_cycles))

        if unique_postures:
            stab_color = C_DONE_NODE if stability == "stable" else (180, 100, 40)
            posture_display = f"{stability}  ({' -> '.join(unique_postures[:5])})"
            _row("Agenda stability:", posture_display, value_color=stab_color)

        if executor_posture:
            _row("Executor posture:", executor_posture)

        if prescribed_skills:
            _row("Skills loaded:", ", ".join(prescribed_skills))

        self.ln(1)

    # ── card ───────────────────────────────────────────────────────────────────

    def add_uow_card(self, uow: dict):
        """Render one UoW on its own page with all rich sections."""
        self.add_page()

        card_x = MARGIN

        # ── Title + badge ─────────────────────────────────────────────────────
        summary = _safe(uow.get("summary") or uow.get("id", ""))
        title_w = CONTENT_W - 46  # leave room for badge

        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*C_DARK)
        self.set_x(card_x)
        while summary and self.get_string_width(summary) > title_w:
            summary = summary[:-4] + "..."
        self.cell(title_w, 8, summary,
                  new_x=XPos.RIGHT, new_y=YPos.TOP)

        status = uow.get("status", "unknown")
        r, g, b = STATUS_COLORS.get(status, DEFAULT_STATUS_COLOR)
        self.set_fill_color(r, g, b)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 7.5)
        badge_text = status.upper()
        badge_w = max(self.get_string_width(badge_text) + 8, 28)
        self.cell(badge_w, 8, badge_text, fill=True, align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*C_DARK)
        self.ln(2)

        # Separator under title
        self.set_draw_color(*C_GRID_LINE)
        self.set_line_width(0.2)
        self.line(card_x, self.get_y(), card_x + CONTENT_W, self.get_y())
        self.ln(3)

        # ── Identity fields ────────────────────────────────────────────────────
        self._label_value(card_x, "ID", uow.get("id", ""))

        source_url = _source_url(uow.get("source"), uow.get("source_issue_number"))
        source_display = uow.get("source") or source_url
        self._label_value(card_x, "Source", source_display or "(none)", url=source_url)

        self._label_value(card_x, "Created", _fmt_ts(uow.get("created_at")))
        if uow.get("started_at"):
            self._label_value(card_x, "Started", _fmt_ts(uow.get("started_at")))
        if uow.get("completed_at"):
            self._label_value(card_x, "Completed", _fmt_ts(uow.get("completed_at")))
        else:
            self._label_value(card_x, "Updated", _fmt_ts(uow.get("updated_at")))

        success_criteria = (uow.get("success_criteria") or "").strip()
        if success_criteria:
            self._label_value(card_x, "Success criteria", success_criteria)

        self.ln(2)

        # ── Rich sections ──────────────────────────────────────────────────────
        self._render_lifecycle_timeline(card_x, uow)
        self._render_agenda_stability(card_x, uow)
        self._render_prescription(card_x, uow)
        self._render_exchange(card_x, uow)


# ── summary index page ─────────────────────────────────────────────────────────

def _render_index_page(pdf: WoSReport, uows: list[dict]) -> None:
    """Render a status-summary bar and index table on the first page."""
    # Status pill summary
    status_counts: dict[str, int] = {}
    for u in uows:
        s = u.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*C_HEADING_TEXT)
    pdf.cell(0, 6, "Status Summary",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)

    x = MARGIN
    y = pdf.get_y()
    for status_name, count in sorted(status_counts.items()):
        sr, sg, sb = STATUS_COLORS.get(status_name, DEFAULT_STATUS_COLOR)
        label = f"{status_name}: {count}"
        pdf.set_font("Helvetica", "B", 7.5)
        pill_w = pdf.get_string_width(label) + 8
        if x + pill_w > PAGE_W - MARGIN:
            x = MARGIN
            y += 7
        pdf.set_fill_color(sr, sg, sb)
        pdf.set_text_color(255, 255, 255)
        pdf.set_xy(x, y)
        pdf.cell(pill_w, 5.5, label, fill=True, align="C")
        x += pill_w + 3

    pdf.ln(10)

    # Column layout
    col_widths = [12, 66, 22, 22, 14, 20]
    col_headers = ["ID", "Summary", "Status", "Created", "Cyles", "Skills"]

    # Header row
    pdf.set_fill_color(*C_HEADING_BG)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(*C_HEADING_TEXT)
    hx = MARGIN
    hy = pdf.get_y() + 2
    for w, h in zip(col_widths, col_headers):
        pdf.set_xy(hx, hy)
        pdf.cell(w, 5.5, h, border=0, fill=True, align="L")
        hx += w
    pdf.ln(7)

    # Data rows
    for i, uow in enumerate(uows):
        row_y = pdf.get_y()
        row_bg = (250, 250, 252) if i % 2 == 0 else (255, 255, 255)
        pdf.set_fill_color(*row_bg)
        pdf.rect(MARGIN, row_y, sum(col_widths), 5, style="F")

        status = uow.get("status", "")
        sr, sg, sb = STATUS_COLORS.get(status, DEFAULT_STATUS_COLOR)
        skills = uow.get("_prescribed_skills", [])

        values = [
            _safe(uow.get("id", "")[-6:]),
            _safe((uow.get("summary") or "")[:62]),
            status.upper()[:12],
            _fmt_ts(uow.get("created_at"), short=True),
            str(uow.get("steward_cycles", 0)),
            ",".join(skills)[:18] or "-",
        ]
        aligns = ["L", "L", "L", "L", "C", "L"]

        rx = MARGIN
        for j, (w, val, align) in enumerate(zip(col_widths, values, aligns)):
            pdf.set_xy(rx, row_y)
            if j == 2:
                pdf.set_text_color(sr, sg, sb)
                pdf.set_font("Helvetica", "B", 7)
            else:
                pdf.set_text_color(*C_DARK)
                pdf.set_font("Helvetica", "", 7)
            pdf.cell(w, 5, val, border=0, align=align)
            rx += w
        pdf.ln(5)

    pdf.ln(3)
    pdf.set_draw_color(*C_GRID_LINE)
    pdf.set_line_width(0.3)
    pdf.line(MARGIN, pdf.get_y(), PAGE_W - MARGIN, pdf.get_y())


# ── prescription quality page ─────────────────────────────────────────────────

def _render_prescription_quality_page(pdf: WoSReport) -> None:
    """
    Render a Prescription Quality summary page using analytics.prescription_quality_summary().

    Reads directly from the registry DB (same path REGISTRY_DB points to).
    If data is sparse, renders the data_gap note instead of an empty table.
    """
    try:
        summary = prescription_quality_summary(registry_path=REGISTRY_DB)
    except Exception as exc:  # noqa: BLE001
        # Don't let an analytics failure break the whole report
        summary = {
            "per_uow": [],
            "aggregate": {
                "total_uows": 0, "uows_with_data": 0,
                "avg_cycles_to_done": None,
                "pct_llm": None, "pct_fallback": None,
                "total_prescriptions": 0,
                "llm_prescriptions": 0, "fallback_prescriptions": 0,
            },
            "data_gap": f"analytics error: {exc}",
        }

    pdf.add_page()

    # Section title
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*C_HEADING_TEXT)
    pdf.cell(0, 8, "Prescription Quality",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")
    pdf.set_draw_color(*C_GRID_LINE)
    pdf.set_line_width(0.3)
    pdf.line(MARGIN, pdf.get_y(), PAGE_W - MARGIN, pdf.get_y())
    pdf.ln(4)
    pdf.set_text_color(*C_DARK)

    agg = summary["aggregate"]
    data_gap = summary.get("data_gap")

    # ── Aggregate metrics ──────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*C_HEADING_TEXT)
    pdf.set_fill_color(*C_HEADING_BG)
    pdf.cell(CONTENT_W, 5.5, "  AGGREGATE METRICS",
             fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_text_color(*C_DARK)

    def _metric_row(label: str, value: str) -> None:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*C_MUTED)
        pdf.cell(60, 5, label + ":", new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*C_DARK)
        pdf.cell(CONTENT_W - 60, 5, value, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    _metric_row("Total UoWs", str(agg["total_uows"]))
    _metric_row("UoWs with prescription data", str(agg["uows_with_data"]))
    _metric_row("Total prescriptions", str(agg["total_prescriptions"]))

    if agg["pct_llm"] is not None:
        _metric_row(
            "LLM prescriptions",
            f"{agg['llm_prescriptions']}  ({agg['pct_llm']}%)",
        )
        _metric_row(
            "Fallback prescriptions",
            f"{agg['fallback_prescriptions']}  ({agg['pct_fallback']}%)",
        )
    else:
        _metric_row("LLM / Fallback split", "no prescription data yet")

    if agg["avg_cycles_to_done"] is not None:
        _metric_row(
            "Avg cycles to done",
            f"{agg['avg_cycles_to_done']:.1f}",
        )
    else:
        _metric_row("Avg cycles to done", "no completed UoWs yet")

    pdf.ln(4)

    # ── Data gap note ──────────────────────────────────────────────────────────
    if data_gap:
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(140, 100, 40)
        pdf.multi_cell(CONTENT_W, 5, _safe(f"Note: {data_gap}"),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)
        pdf.set_text_color(*C_DARK)

    # ── Per-UoW table ──────────────────────────────────────────────────────────
    per_uow = summary["per_uow"]
    uows_with_data = [r for r in per_uow if r["prescription_paths"]]

    if not uows_with_data:
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*C_MUTED)
        pdf.cell(0, 5, "No per-UoW prescription data to display.",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        return

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*C_HEADING_TEXT)
    pdf.set_fill_color(*C_HEADING_BG)
    pdf.cell(CONTENT_W, 5.5, "  PER-UOW BREAKDOWN",
             fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # Table headers
    col_widths = [12, 58, 22, 14, 16, 16, 44]
    col_headers = ["ID", "Summary", "Status", "Cycles", "LLM", "Fallback", "Path sequence"]

    pdf.set_fill_color(*C_HEADING_BG)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(*C_HEADING_TEXT)
    hx = MARGIN
    hy = pdf.get_y() + 1
    for w, h in zip(col_widths, col_headers):
        pdf.set_xy(hx, hy)
        pdf.cell(w, 5, h, border=0, fill=True, align="L")
        hx += w
    pdf.ln(6)

    # Table rows
    for i, rec in enumerate(uows_with_data):
        row_y = pdf.get_y()
        row_bg = (250, 250, 252) if i % 2 == 0 else (255, 255, 255)
        pdf.set_fill_color(*row_bg)
        pdf.rect(MARGIN, row_y, sum(col_widths), 5, style="F")

        status = rec.get("status", "")
        sr, sg, sb = STATUS_COLORS.get(status, DEFAULT_STATUS_COLOR)

        # Compact path display: e.g. "llm llm fallback" → "L L F"
        path_abbrev = " ".join(
            "L" if p == "llm" else "F" for p in rec["prescription_paths"][:10]
        )
        if len(rec["prescription_paths"]) > 10:
            path_abbrev += "..."

        values = [
            _safe(rec["id"][-6:]),
            _safe((rec["summary"] or "")[:55]),
            status.upper()[:12],
            str(rec["steward_cycles"]),
            str(rec["llm_count"]),
            str(rec["fallback_count"]),
            _safe(path_abbrev),
        ]
        aligns = ["L", "L", "L", "C", "C", "C", "L"]

        rx = MARGIN
        for j, (w, val, align) in enumerate(zip(col_widths, values, aligns)):
            pdf.set_xy(rx, row_y)
            if j == 2:
                pdf.set_text_color(sr, sg, sb)
                pdf.set_font("Helvetica", "B", 7)
            else:
                pdf.set_text_color(*C_DARK)
                pdf.set_font("Helvetica", "", 7)
            pdf.cell(w, 5, val, border=0, align=align)
            rx += w
        pdf.ln(5)

    pdf.set_text_color(*C_DARK)


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
        pdf.set_text_color(*C_MUTED)
        pdf.cell(0, 10, "No units of work found.",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    else:
        _render_index_page(pdf, uows)
        _render_prescription_quality_page(pdf)
        for uow in uows:
            pdf.add_uow_card(uow)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path


# ── Telegram delivery ─────────────────────────────────────────────────────────

def queue_for_telegram(pdf_path: Path, chat_id: int, caption: str = "") -> Path:
    """Write an outbox JSON file that instructs the bot to send this PDF."""
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
    parser = argparse.ArgumentParser(
        description="Generate WOS Registry PDF report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--chat-id", type=int, default=DEFAULT_CHAT_ID,
                        help="Telegram chat ID to send to (default: %(default)s)")
    parser.add_argument("--output", type=Path,
                        help="Output PDF path (default: auto-named in ~/messages/documents/)")
    parser.add_argument("--no-send", action="store_true",
                        help="Generate PDF but do not queue for Telegram delivery")
    parser.add_argument("--status", type=str, default=None,
                        help="Filter by status (e.g. active, done, proposed; default: all)")
    parser.add_argument("--since", type=str, default=None,
                        metavar="YYYY-MM-DD",
                        help="Include only UoWs created on or after this date")
    parser.add_argument("--ids", type=str, default=None,
                        metavar="ID1,ID2,...",
                        help="Include only the specified UoW IDs (comma-separated)")
    args = parser.parse_args(argv)

    # Resolve output path
    if args.output:
        output_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Use ~/messages/documents/ visible to lobster-router.
        # /tmp is private to the systemd service (PrivateTmp=true), so PDFs
        # written there are invisible to the bot process.
        pdf_dir = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages")) / "documents"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        output_path = pdf_dir / f"wos-report-{ts}.pdf"

    # Parse --ids into a list (None means no ID filter)
    ids_filter: list[str] | None = None
    if args.ids:
        ids_filter = [i.strip() for i in args.ids.split(",") if i.strip()]

    print(f"Loading registry from {REGISTRY_DB}...")
    uows = fetch_uows(
        status_filter=args.status,
        since=args.since,
        ids=ids_filter,
    )
    print(f"Found {len(uows)} unit(s) of work")

    print(f"Generating PDF: {output_path}")
    generate_pdf(uows, output_path)
    print(f"PDF written: {output_path} ({output_path.stat().st_size:,} bytes)")

    if not args.no_send:
        # Build a descriptive caption
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
        outbox_file = queue_for_telegram(output_path, args.chat_id, caption)
        print(f"Queued for Telegram delivery: {outbox_file}")
    else:
        print("--no-send: skipping Telegram delivery")

    return str(output_path)


if __name__ == "__main__":
    main()
