"""
UoW Registry — SQLite-backed store for Units of Work.

Design constraints enforced here:
- All writes use BEGIN IMMEDIATE transactions.
- Audit log entry is written in the same transaction as the registry change.
  If either fails, both roll back (Principle 1: no silent transitions).
- WAL mode is enabled on every connection for concurrent read safety.
- The UNIQUE(source_issue_number, sweep_date) constraint is the DB-level
  dedup gate; the pre-write decision table adds cross-sweep-date logic on top.
- INSERT OR REPLACE is never used — it would silently discard execution state.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL_UOW_REGISTRY = """
CREATE TABLE IF NOT EXISTS uow_registry (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'executable',
    source TEXT NOT NULL,
    source_issue_number INTEGER,
    sweep_date TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    posture TEXT NOT NULL DEFAULT 'solo',
    agent TEXT,
    children TEXT DEFAULT '[]',
    parent TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    summary TEXT NOT NULL,
    output_ref TEXT,
    hooks_applied TEXT DEFAULT '[]',
    route_reason TEXT,
    route_evidence TEXT DEFAULT '{}',
    trigger TEXT DEFAULT '{"type": "immediate"}',
    UNIQUE(source_issue_number, sweep_date)
);
"""

_DDL_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    uow_id      TEXT NOT NULL,
    event       TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    agent       TEXT,
    note        TEXT
);
"""

# Statuses that indicate in-flight work (block re-proposal)
_NON_TERMINAL_STATUSES = frozenset({"proposed", "pending", "active", "blocked"})
# Statuses that are terminal (allow re-proposal for the same issue)
_TERMINAL_STATUSES = frozenset({"done", "failed", "expired"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_uow_id() -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    random_part = uuid.uuid4().hex[:6]
    return f"uow_{date_part}_{random_part}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class Registry:
    """
    All public methods are pure write-then-read operations that keep the
    connection open only for the duration of the operation.

    The `db_path` is the only mutable state. Every method opens a fresh
    connection, executes within a BEGIN IMMEDIATE transaction, and closes.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(_DDL_UOW_REGISTRY)
            conn.execute(_DDL_AUDIT_LOG)
            conn.commit()
        finally:
            conn.close()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        # Deserialize JSON-stored fields
        for field in ("children", "hooks_applied", "route_evidence", "trigger"):
            if d.get(field) and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def _write_audit(
        self,
        conn: sqlite3.Connection,
        uow_id: str,
        event: str,
        from_status: str | None = None,
        to_status: str | None = None,
        agent: str | None = None,
        note: str | None = None,
    ) -> None:
        """Write a single audit log entry. Must be called inside an active transaction."""
        conn.execute(
            """
            INSERT INTO audit_log (ts, uow_id, event, from_status, to_status, agent, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_now_iso(), uow_id, event, from_status, to_status, agent, note),
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def upsert(
        self,
        issue_number: int,
        title: str,
        sweep_date: str | None = None,
        uow_type: str = "executable",
    ) -> dict[str, Any]:
        """
        Propose a UoW for a GitHub issue.

        Decision table (evaluated before any write):
        - No existing non-terminal record → INSERT new proposed record
        - Existing proposed (any sweep_date) → SKIP
        - Existing pending/active/blocked (any sweep_date) → SKIP
        - Existing done/failed/expired (any sweep_date) → INSERT new proposed record
        - UNIQUE(issue, sweep_date) conflict + existing is proposed → UPDATE fields
        - UNIQUE(issue, sweep_date) conflict + existing is non-proposed → no-op update (fields unchanged)
        """
        if sweep_date is None:
            sweep_date = datetime.now(timezone.utc).date().isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Cross-sweep-date pre-check: any non-terminal record for this issue?
            existing = conn.execute(
                """
                SELECT id, status FROM uow_registry
                WHERE source_issue_number = ?
                  AND status NOT IN ('done', 'failed', 'expired')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (issue_number,),
            ).fetchone()

            if existing:
                skip_reason = f"existing record {existing['id']} is in '{existing['status']}' status"
                self._write_audit(
                    conn,
                    uow_id=existing["id"],
                    event="skipped",
                    note=f"upsert skipped: {skip_reason}",
                )
                conn.commit()
                return {
                    "id": existing["id"],
                    "action": "skipped",
                    "reason": skip_reason,
                }

            # Check for same-date UNIQUE conflict (terminal record from a prior run today).
            # This is rare but possible: same issue went terminal and is being re-swept on
            # the same calendar date. In this case we skip to avoid a phantom audit entry.
            same_date_row = conn.execute(
                """
                SELECT id, status FROM uow_registry
                WHERE source_issue_number = ? AND sweep_date = ?
                """,
                (issue_number, sweep_date),
            ).fetchone()

            if same_date_row:
                # Terminal row exists for this exact (issue, sweep_date) — skip for today.
                # The next sweep on a new date will create a fresh record.
                skip_reason = (
                    f"terminal record {same_date_row['id']} (status={same_date_row['status']}) "
                    f"already exists for sweep_date={sweep_date}; will re-propose on next sweep date"
                )
                self._write_audit(
                    conn,
                    uow_id=same_date_row["id"],
                    event="skipped",
                    note=f"upsert skipped: {skip_reason}",
                )
                conn.commit()
                return {
                    "id": same_date_row["id"],
                    "action": "skipped",
                    "reason": skip_reason,
                }

            uow_id = _generate_uow_id()
            now = _now_iso()
            source = f"github:issue/{issue_number}"

            # Audit entry is written BEFORE the INSERT (Principle 1: audit first).
            # If the INSERT fails, both roll back together.
            self._write_audit(conn, uow_id=uow_id, event="created", to_status="proposed")

            conn.execute(
                """
                INSERT INTO uow_registry (
                    id, type, source, source_issue_number, sweep_date,
                    status, posture, created_at, updated_at, summary,
                    route_reason, route_evidence, trigger
                ) VALUES (?, ?, ?, ?, ?, 'proposed', 'solo', ?, ?, ?, ?, '{}', '{"type": "immediate"}')
                """,
                (
                    uow_id,
                    uow_type,
                    source,
                    issue_number,
                    sweep_date,
                    now,
                    now,
                    title,
                    "phase1-default: no classifier",
                ),
            )
            conn.commit()
            return {"id": uow_id, "action": "inserted"}

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def confirm(self, uow_id: str) -> dict[str, Any]:
        """
        Transition a UoW from proposed → pending.

        Returns:
        - success: {"id", "status": "pending", "previous_status": "proposed"}
        - already non-proposed: {"id", "status": <current>, "action": "noop", "reason": "already <status>"}
        - not found: {"error": "not found", "id": <id>}
        - expired: {"error": "expired", "id": <id>}
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                "SELECT id, status FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()

            if row is None:
                conn.commit()
                return {
                    "error": "not found",
                    "id": uow_id,
                    "message": f"UoW `{uow_id}` not found. Run `/wos status proposed` to see current proposals.",
                }

            current_status = row["status"]

            if current_status == "expired":
                conn.commit()
                return {
                    "error": "expired",
                    "id": uow_id,
                    "message": f"UoW `{uow_id}` has expired. Wait for the next sweep to re-propose, or run a manual sweep.",
                }

            if current_status != "proposed":
                conn.commit()
                return {
                    "id": uow_id,
                    "status": current_status,
                    "action": "noop",
                    "reason": f"already {current_status} — no action taken.",
                }

            now = _now_iso()
            self._write_audit(
                conn,
                uow_id=uow_id,
                event="status_change",
                from_status="proposed",
                to_status="pending",
            )
            conn.execute(
                "UPDATE uow_registry SET status = 'pending', updated_at = ? WHERE id = ?",
                (now, uow_id),
            )
            conn.commit()
            return {"id": uow_id, "status": "pending", "previous_status": "proposed"}

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, uow_id: str) -> dict[str, Any]:
        """Return a UoW record by id, or {"error": "not found", "id": <id>}."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                return {"error": "not found", "id": uow_id}
            return self._row_to_dict(row)
        finally:
            conn.close()

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        """Return all UoW records, optionally filtered by status."""
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM uow_registry WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM uow_registry ORDER BY created_at DESC"
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def expire_proposals(self) -> dict[str, Any]:
        """
        Transition proposed records older than 14 days to 'expired'.
        Writes an audit entry for each expiry in the same transaction.
        Returns {"expired_count": N, "ids": [...]}.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            to_expire = conn.execute(
                """
                SELECT id FROM uow_registry
                WHERE status = 'proposed' AND created_at <= ?
                """,
                (cutoff,),
            ).fetchall()

            expired_ids = [r["id"] for r in to_expire]
            now = _now_iso()

            for uow_id in expired_ids:
                self._write_audit(
                    conn,
                    uow_id=uow_id,
                    event="expired",
                    from_status="proposed",
                    to_status="expired",
                    note="auto-expired: proposed for >14 days",
                )

            if expired_ids:
                placeholders = ",".join("?" * len(expired_ids))
                conn.execute(
                    f"UPDATE uow_registry SET status='expired', updated_at=? WHERE id IN ({placeholders})",
                    [now] + expired_ids,
                )

            conn.commit()
            return {"expired_count": len(expired_ids), "ids": expired_ids}

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def check_stale(
        self,
        issue_checker: Callable[[int], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return active UoWs whose source GitHub issue is closed.

        The `issue_checker` is a callable that takes an issue number and returns
        True if the issue is closed. In production, this calls `gh issue view`.
        Injecting it as a parameter makes the function testable without subprocess.
        """
        if issue_checker is None:
            issue_checker = _gh_issue_is_closed

        conn = self._connect()
        try:
            active_rows = conn.execute(
                """
                SELECT * FROM uow_registry
                WHERE status = 'active' AND source_issue_number IS NOT NULL
                """
            ).fetchall()
        finally:
            conn.close()

        stale = []
        for row in active_rows:
            issue_num = row["source_issue_number"]
            if issue_checker(issue_num):
                d = self._row_to_dict(row)
                stale.append({
                    "id": d["id"],
                    "source_issue_number": issue_num,
                    "summary": d["summary"],
                    "status": d["status"],
                    "created_at": d["created_at"],
                })
        return stale

    def set_status_direct(self, uow_id: str, new_status: str) -> None:
        """
        Direct status set — bypasses the confirm flow.
        Used in tests and for terminal status transitions (done, failed, expired).
        Writes an audit entry in the same transaction.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM uow_registry WHERE id = ?", (uow_id,)
            ).fetchone()
            if row is None:
                conn.commit()
                return
            old_status = row["status"]
            now = _now_iso()
            self._write_audit(
                conn,
                uow_id=uow_id,
                event="status_change",
                from_status=old_status,
                to_status=new_status,
                note="direct status set",
            )
            conn.execute(
                "UPDATE uow_registry SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, uow_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def gate_readiness(self) -> dict[str, Any]:
        """
        Compute Phase 1 → Phase 2 autonomy gate metric.

        Gate is met when:
        1. Phase 1 has run for >= 14 days (oldest record is >= 14 days old)
        2. proposed-to-confirmed ratio >= 80% over the last 7 days

        Returns a dict with: gate_met, days_running, proposed_to_confirmed_ratio_7d, reason
        """
        conn = self._connect()
        try:
            oldest = conn.execute(
                "SELECT MIN(created_at) as oldest FROM uow_registry"
            ).fetchone()["oldest"]

            if oldest is None:
                return {
                    "gate_met": False,
                    "days_running": 0,
                    "proposed_to_confirmed_ratio_7d": 0.0,
                    "reason": "no records in registry",
                }

            oldest_dt = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_running = (now - oldest_dt).days

            # Ratio over last 7 days
            seven_days_ago = (now - timedelta(days=7)).isoformat()
            proposed_last_7d = conn.execute(
                """
                SELECT COUNT(*) as c FROM uow_registry
                WHERE created_at >= ?
                """,
                (seven_days_ago,),
            ).fetchone()["c"]

            confirmed_last_7d = conn.execute(
                """
                SELECT COUNT(*) as c FROM audit_log
                WHERE event = 'status_change'
                  AND to_status = 'pending'
                  AND ts >= ?
                """,
                (seven_days_ago,),
            ).fetchone()["c"]

            ratio = (confirmed_last_7d / proposed_last_7d) if proposed_last_7d > 0 else 0.0

            days_ok = days_running >= 14
            ratio_ok = ratio >= 0.80

            if days_ok and ratio_ok:
                reason = "all conditions met"
            elif not days_ok:
                reason = f"phase 1 has only been running {days_running} days (need >=14)"
            else:
                reason = f"proposed-to-confirmed ratio {ratio:.0%} over last 7 days (need >=80%)"

            return {
                "gate_met": days_ok and ratio_ok,
                "days_running": days_running,
                "proposed_to_confirmed_ratio_7d": round(ratio, 4),
                "reason": reason,
            }
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Production issue checker (subprocess to gh CLI)
# ---------------------------------------------------------------------------

def _gh_issue_is_closed(issue_number: int) -> bool:
    """Return True if the GitHub issue is closed. Uses gh CLI."""
    import subprocess
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", "dcetlin/Lobster",
             "--json", "state", "--jq", ".state"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip().upper() == "CLOSED"
    except Exception:
        # If we can't check, assume open (conservative: don't flag as stale)
        return False
