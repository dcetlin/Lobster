"""
Post-completion GitHub sync — closes source issues when WOS UoWs reach `done`.

When the Steward declares a UoW complete (status=done), the corresponding
GitHub issue should be closed automatically with a comment summarizing what
was accomplished. This module implements that sync as a pure sweep function
that is called by the steward-heartbeat script after the Steward cycle.

Design:
- All GitHub I/O is isolated in _close_github_issue() — the only function
  that invokes subprocess.
- build_closure_comment() is a pure function: same UoW always produces the
  same comment template. Testable without any subprocess or DB access.
- run_post_completion_sync() coordinates the sweep: queries done UoWs that
  have not been synced, attempts to close each issue, and records the result.
- Idempotency: github_synced_at is set atomically in the same DB write as
  the audit entry. A failed gh call leaves github_synced_at NULL so the
  next heartbeat cycle retries.
- If issue_url is NULL (pre-migration row or manual UoW), the UoW is skipped
  with a log entry — no error, no retry.

Subprocess protocol:
  gh issue close <number> --repo <repo> --comment "<text>"
  gh issue comment <number> --repo <repo> --body "<text>"

The module does not raise on gh errors — subprocess failures are caught,
logged, and returned in the sync result so the caller can decide how to
surface them.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import Registry, UoW

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure helpers — no side effects
# ---------------------------------------------------------------------------

_ISSUE_URL_RE = re.compile(
    r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)"
)


def _parse_issue_url(issue_url: str) -> tuple[str, str] | None:
    """
    Extract (repo, issue_number) from a canonical GitHub issue URL.

    Returns None if the URL does not match the expected format.
    Pure function — no side effects.

    >>> _parse_issue_url("https://github.com/dcetlin/Lobster/issues/42")
    ('dcetlin/Lobster', '42')
    >>> _parse_issue_url("https://example.com/foo") is None
    True
    """
    m = _ISSUE_URL_RE.match(issue_url)
    if m is None:
        return None
    return m.group(1), m.group(2)


def build_closure_comment(uow: "UoW") -> str:
    """
    Build the GitHub issue closure comment for a completed UoW.

    Pure function — takes a UoW value object and returns a comment string.
    The comment summarizes the UoW outcome: what was done, when it was
    completed, and the UoW ID for cross-referencing in the Lobster registry.

    Args:
        uow: A typed UoW value object in `done` status.

    Returns:
        A Markdown-formatted comment string suitable for posting to GitHub.
    """
    summary = uow.summary or "(no summary)"
    uow_id = uow.id
    completed_at = uow.steward_log  # may be None for pre-Phase-2 UoWs

    lines = [
        "This issue was completed automatically by the Lobster Work Order System (WOS).",
        "",
        f"**UoW ID:** `{uow_id}`",
        f"**Summary:** {summary}",
    ]

    if uow.success_criteria:
        lines.append(f"**Success criteria:** {uow.success_criteria}")

    lines += [
        "",
        "_Closed by Lobster WOS post-completion sync._",
    ]

    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# GitHub I/O — isolated subprocess calls
# ---------------------------------------------------------------------------

class GitHubSyncError(Exception):
    """Raised when a gh CLI call fails during post-completion sync."""


def _close_github_issue(
    repo: str,
    issue_number: str,
    comment: str,
    *,
    _run: type = subprocess,  # injectable for tests
) -> None:
    """
    Close a GitHub issue and post a summary comment.

    This is the only function in this module that invokes subprocess.
    All other functions are pure or delegate here.

    The issue is closed with --comment so the closure and summary arrive
    in a single API call. If the issue is already closed, gh returns a
    non-zero exit code; we treat this as a success (idempotent close).

    Args:
        repo: GitHub repo slug, e.g. "dcetlin/Lobster".
        issue_number: Issue number as a string.
        comment: Markdown comment to post on closure.

    Raises:
        GitHubSyncError: If the gh CLI call fails unexpectedly (not "already closed").
    """
    try:
        _run.run(
            [
                "gh", "issue", "close", issue_number,
                "--repo", repo,
                "--comment", comment,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        # gh exits 1 when the issue is already closed — treat as success.
        if "already closed" in stderr.lower() or "issue was already closed" in stderr.lower():
            log.debug(
                "github_sync: issue #%s in %s is already closed — no-op",
                issue_number, repo,
            )
            return
        raise GitHubSyncError(
            f"gh issue close failed for {repo}#{issue_number}: {stderr.strip()}"
        ) from exc


# ---------------------------------------------------------------------------
# Registry write — mark synced
# ---------------------------------------------------------------------------

def _mark_github_synced(registry: "Registry", uow_id: str, synced_at: str) -> None:
    """
    Write github_synced_at timestamp and audit entry for a UoW.

    Uses the registry's internal _connect() pattern for a single atomic write.
    Does not transition the UoW status — this is a field update only.
    """
    import json
    conn = registry._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE uow_registry SET github_synced_at = ? WHERE id = ?",
            (synced_at, uow_id),
        )
        conn.execute(
            """
            INSERT INTO audit_log (ts, uow_id, event, agent, note)
            VALUES (?, ?, 'github_sync', 'github_sync', ?)
            """,
            (
                synced_at,
                uow_id,
                json.dumps({
                    "event": "github_sync",
                    "actor": "github_sync",
                    "uow_id": uow_id,
                    "synced_at": synced_at,
                    "timestamp": synced_at,
                }),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _query_done_unsynced(registry: "Registry") -> list["UoW"]:
    """
    Return done UoWs that have not yet been GitHub-synced.

    Queries for status=done AND github_synced_at IS NULL AND issue_url IS NOT NULL.
    Skips UoWs without issue_url (pre-migration rows or manual entries).

    Falls back to list(status='done') filtered in Python if the
    github_synced_at column is absent (pre-migration database).
    """
    conn = registry._connect()
    try:
        # Check if github_synced_at column exists
        cols = {row[1] for row in conn.execute("PRAGMA table_info(uow_registry)").fetchall()}
        if "github_synced_at" not in cols:
            log.warning("github_sync: github_synced_at column not present — migration 0006 not applied")
            return []

        rows = conn.execute(
            """
            SELECT * FROM uow_registry
            WHERE status = 'done'
              AND github_synced_at IS NULL
              AND issue_url IS NOT NULL
            ORDER BY updated_at ASC
            """,
        ).fetchall()
        return [registry._row_to_uow(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public sweep API
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PostCompletionSyncResult:
    """Result of a post-completion GitHub sync sweep."""
    synced: int
    skipped_no_url: int
    failed: int
    errors: list[str] = field(default_factory=list)


def run_post_completion_sync(
    registry: "Registry",
    *,
    dry_run: bool = False,
    _close_fn: type = None,  # injectable for tests
) -> PostCompletionSyncResult:
    """
    Sweep done UoWs and close their GitHub source issues with a summary comment.

    Called by steward-heartbeat.py after the Steward cycle. Processes only
    UoWs in `done` status where:
    - `issue_url` is not NULL (has a known GitHub issue URL)
    - `github_synced_at` is NULL (not yet synced)

    For each eligible UoW:
    1. Parse the repo and issue number from `issue_url`.
    2. Build a closure comment via build_closure_comment().
    3. Call gh to close the issue and post the comment.
    4. Write github_synced_at timestamp and audit entry to the registry.

    On gh failure: log the error, increment `failed` counter, and leave
    github_synced_at NULL so the next heartbeat retries.

    In dry_run mode: log what would be done but do not call gh or write
    to the registry.

    Args:
        registry: A Registry instance connected to the WOS database.
        dry_run: If True, log actions without executing them.
        _close_fn: Injectable close function for tests (replaces _close_github_issue).

    Returns:
        PostCompletionSyncResult with counts of synced, skipped, and failed UoWs.
    """
    close_fn = _close_fn if _close_fn is not None else _close_github_issue

    try:
        unsynced = _query_done_unsynced(registry)
    except Exception as exc:
        log.error("github_sync: failed to query done UoWs — %s", exc)
        return PostCompletionSyncResult(synced=0, skipped_no_url=0, failed=1, errors=[str(exc)])

    synced = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    for uow in unsynced:
        if not uow.issue_url:
            # Defensive: _query_done_unsynced already filters, but guard anyway.
            log.debug("github_sync: UoW %s has no issue_url — skipping", uow.id)
            skipped += 1
            continue

        parsed = _parse_issue_url(uow.issue_url)
        if parsed is None:
            log.warning(
                "github_sync: UoW %s has unrecognized issue_url format %r — skipping",
                uow.id, uow.issue_url,
            )
            skipped += 1
            continue

        repo, issue_number = parsed
        comment = build_closure_comment(uow)

        if dry_run:
            log.info(
                "github_sync (DRY RUN): would close %s#%s for UoW %s",
                repo, issue_number, uow.id,
            )
            synced += 1
            continue

        try:
            close_fn(repo=repo, issue_number=issue_number, comment=comment)
        except GitHubSyncError as exc:
            error_msg = str(exc)
            log.error("github_sync: failed to close %s#%s — %s", repo, issue_number, error_msg)
            errors.append(error_msg)
            failed += 1
            continue
        except Exception as exc:
            error_msg = f"unexpected error closing {repo}#{issue_number}: {exc}"
            log.error("github_sync: %s", error_msg)
            errors.append(error_msg)
            failed += 1
            continue

        try:
            _mark_github_synced(registry, uow.id, _now_iso())
            synced += 1
            log.info("github_sync: closed %s#%s for UoW %s", repo, issue_number, uow.id)
        except Exception as exc:
            error_msg = f"gh close succeeded but registry write failed for {uow.id}: {exc}"
            log.error("github_sync: %s", error_msg)
            errors.append(error_msg)
            failed += 1

    return PostCompletionSyncResult(
        synced=synced,
        skipped_no_url=skipped,
        failed=failed,
        errors=errors,
    )
