#!/usr/bin/env python3
"""
Nightly backup of the ledger_db Postgres container (Dan's ledger app).

Context: ledger_db (postgres:17-alpine, part of the ~/ledger stack) had no
backup coverage at all — the last dump on disk was from Sept 22. This script
closes that gap with a nightly pg_dump piped through gzip, written to
~/backups/ledger/ (NOT ~/ledger/backups/, which is root-owned and not
writable by the lobster user), with 14-day retention.

Cron schedule (nightly at 03:15 UTC — 15 min after the dancetlin-infra
postgres backup at 03:00, to avoid any resource contention):
    15 3 * * * cd ~/lobster && uv run scheduled-tasks/ledger-db-backup.py >> ~/lobster-workspace/scheduled-jobs/logs/ledger-db-backup.log 2>&1 # LOBSTER-LEDGER-DB-BACKUP

Type B dispatch: cron calls this script directly (no inbox/ message, no
dispatcher round-trip). The jobs.json enabled gate is checked at the top of
main() so runtime enable/disable works without touching cron.

Security: never logs DB_PASSWORD or any .env secret. The database name and
role are read from the container's own environment (POSTGRES_DB /
POSTGRES_USER) rather than hardcoded, but only those two non-secret keys are
ever surfaced in logs/output.

Run standalone:
    uv run ~/lobster/scheduled-tasks/ledger-db-backup.py [--dry-run]
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.inbox_write import write_inbox_message  # noqa: E402
from src.utils.jobs import is_job_enabled  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("ledger-db-backup")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JOB_NAME = "ledger-db-backup"
CONTAINER = "ledger_db"
BACKUP_DIR = Path.home() / "backups" / "ledger"
RETENTION_DAYS = 14
ADMIN_CHAT_ID = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))


@dataclass(frozen=True)
class DbIdentity:
    db_name: str
    db_user: str


# ---------------------------------------------------------------------------
# Pure / read-only helpers
# ---------------------------------------------------------------------------


def read_db_identity(container: str) -> DbIdentity:
    """Read POSTGRES_DB / POSTGRES_USER from the container's own env.

    Never reads or returns POSTGRES_PASSWORD. Falls back to sane defaults
    (ledger / postgres) if the container env can't be read for any reason.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", container, "env"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        env_map = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                env_map[k] = v
        return DbIdentity(
            db_name=env_map.get("POSTGRES_DB", "ledger"),
            db_user=env_map.get("POSTGRES_USER", "postgres"),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        log.warning("Could not read container env (%s) — using defaults", exc)
        return DbIdentity(db_name="ledger", db_user="postgres")


def dest_path(backup_dir: Path, now: datetime) -> Path:
    ts = now.strftime("%Y%m%d_%H%M%S")
    return backup_dir / f"ledger_{ts}.sql.gz"


def is_valid_gzip(path: Path) -> bool:
    """Return True if `path` is a non-empty, structurally valid gzip file."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(1024 * 1024):
                pass
        return True
    except OSError:
        return False


def old_backups(backup_dir: Path, retention_days: int, now: datetime) -> list[Path]:
    """Return backup files older than retention_days (pure given inputs)."""
    cutoff = now.timestamp() - retention_days * 86400
    if not backup_dir.exists():
        return []
    return [
        p
        for p in backup_dir.glob("ledger_*.sql.gz")
        if p.is_file() and p.stat().st_mtime < cutoff
    ]


# ---------------------------------------------------------------------------
# Side-effecting boundary functions
# ---------------------------------------------------------------------------


def run_pg_dump(container: str, identity: DbIdentity, dest: Path) -> tuple[bool, str]:
    """Run `docker exec <container> pg_dump ...` piped through gzip to dest.

    Returns (success, error_message). Writes atomically via a .tmp sibling.
    """
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dump_cmd = [
        "docker", "exec", container,
        "pg_dump", "-U", identity.db_user, identity.db_name,
    ]
    try:
        with open(tmp_dest, "wb") as out_fh:
            dump_proc = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            gzip_proc = subprocess.Popen(
                ["gzip"], stdin=dump_proc.stdout, stdout=out_fh, stderr=subprocess.PIPE
            )
            dump_proc.stdout.close()
            _, gzip_stderr = gzip_proc.communicate(timeout=300)
            _, dump_stderr = dump_proc.communicate(timeout=300)

        if dump_proc.returncode != 0:
            tmp_dest.unlink(missing_ok=True)
            return False, f"pg_dump exit {dump_proc.returncode}: {dump_stderr.decode(errors='replace').strip()}"
        if gzip_proc.returncode != 0:
            tmp_dest.unlink(missing_ok=True)
            return False, f"gzip exit {gzip_proc.returncode}: {gzip_stderr.decode(errors='replace').strip()}"
    except subprocess.TimeoutExpired:
        tmp_dest.unlink(missing_ok=True)
        return False, "pg_dump/gzip timed out after 300s"
    except OSError as exc:
        tmp_dest.unlink(missing_ok=True)
        return False, f"OS error during dump: {exc}"

    tmp_dest.rename(dest)
    return True, ""


def prune_old_backups(backup_dir: Path, retention_days: int, now: datetime) -> list[str]:
    removed = []
    for p in old_backups(backup_dir, retention_days, now):
        try:
            p.unlink()
            removed.append(p.name)
        except OSError as exc:
            log.warning("Failed to prune %s: %s", p, exc)
    return removed


def alert_failure(reason: str) -> None:
    text = (
        f"[ALERT] ledger_db backup FAILED: {reason}\n\n"
        f"No fresh restore point was created tonight. Investigate ASAP — "
        f"this is the only backup coverage for ledger_db."
    )
    write_inbox_message(JOB_NAME, ADMIN_CHAT_ID, text, datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Resolve paths/identity but don't actually dump")
    args = parser.parse_args()

    if not is_job_enabled(JOB_NAME):
        log.info("Job disabled in jobs.json — skipping")
        return 0

    now = datetime.now(timezone.utc)
    identity = read_db_identity(CONTAINER)
    dest = dest_path(BACKUP_DIR, now)

    log.info("Backing up container=%s db=%s user=%s -> %s", CONTAINER, identity.db_name, identity.db_user, dest)

    if args.dry_run:
        print(f"DRY RUN: would dump {CONTAINER} (db={identity.db_name}, user={identity.db_user}) to {dest}")
        return 0

    ok, err = run_pg_dump(CONTAINER, identity, dest)
    if not ok:
        log.error("Backup failed: %s", err)
        alert_failure(err)
        return 1

    if not is_valid_gzip(dest):
        msg = f"backup written to {dest} but failed gzip validity/non-empty check"
        log.error(msg)
        alert_failure(msg)
        return 1

    size_bytes = dest.stat().st_size
    removed = prune_old_backups(BACKUP_DIR, RETENTION_DAYS, now)

    summary = (
        f"ledger_db backup OK: {dest} ({size_bytes / 1024:.1f} KB, gzip-valid). "
        f"Pruned {len(removed)} backup(s) older than {RETENTION_DAYS}d."
    )
    log.info(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
