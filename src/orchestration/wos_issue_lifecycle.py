"""
WOS Issue Lifecycle — bidirectional GitHub issue tracking for Units of Work.

Maintains GitHub issue state in sync with WOS UoW state across all 4 lifecycle
transitions:

  created     → stamp_issue_executing   (adds wos:executing label + comment)
  pearl       → stamp_issue_complete    (removes label, closes issue, comments)
  shit/failed → stamp_issue_failed      (removes label, leaves open, comments)
  unverifiable→ stamp_issue_unverifiable (removes label, leaves open, comments)

All public functions:
- Accept (issue_number, uow_id, ..., repo) — no registry dependency (pure side effect)
- Catch all exceptions, log, return bool success — never raise
- Do NOT block UoW processing on failure

Idempotency:
- stamp_issue_executing checks whether the issue already has wos:executing
  before adding it. Returns False if already present — callers use this as
  a creation guard to skip duplicate UoW creation.

Label management:
- wos:executing label is auto-created if absent (color: WOS_EXECUTING_LABEL_COLOR)
- Label operations are independent — creation failure is non-fatal

Source format:
- All public functions accept an integer issue_number and a repo slug (owner/repo).
  Callers supply these from UoW.source_issue_number and the LOBSTER_WOS_REPO env var.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Optional

log = logging.getLogger("wos_issue_lifecycle")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: GitHub label applied when a UoW is executing against this issue.
WOS_EXECUTING_LABEL: str = "wos:executing"

#: Label color (hex, no leading #) — blue to indicate active machine work.
WOS_EXECUTING_LABEL_COLOR: str = "0052cc"

#: Subprocess timeout for gh CLI calls (seconds).
_GH_TIMEOUT: int = 30


# ---------------------------------------------------------------------------
# Internal helpers — pure composition
# ---------------------------------------------------------------------------

def _run_gh(args: list[str], gh_bin: str = "gh") -> subprocess.CompletedProcess:
    """
    Run a gh CLI command and return the CompletedProcess.

    Raises subprocess.CalledProcessError on non-zero exit (check=True).
    Callers catch this; the public functions convert all exceptions to False.
    """
    return subprocess.run(
        [gh_bin] + args,
        capture_output=True,
        timeout=_GH_TIMEOUT,
        check=True,
    )


def _ensure_wos_executing_label_exists(repo: str, gh_bin: str = "gh") -> bool:
    """
    Ensure the wos:executing label exists on the repo.

    If the label is absent, creates it with WOS_EXECUTING_LABEL_COLOR.
    Returns True on success or if label already exists. Returns False if
    any gh CLI call fails (non-blocking — callers proceed without the label
    in that case, with a warning logged).

    Pure side effect: no registry access, no UoW state.
    """
    try:
        result = _run_gh(
            ["label", "list", "--repo", repo, "--json", "name"],
            gh_bin=gh_bin,
        )
        labels_json = json.loads(result.stdout.decode(errors="replace"))
        existing_names = {lbl["name"] for lbl in labels_json}

        if WOS_EXECUTING_LABEL in existing_names:
            log.debug(
                "wos_issue_lifecycle: label %r already exists on %s — no-op",
                WOS_EXECUTING_LABEL,
                repo,
            )
            return True

        # Label is absent — create it.
        log.info(
            "wos_issue_lifecycle: creating label %r on %s (color #%s)",
            WOS_EXECUTING_LABEL,
            repo,
            WOS_EXECUTING_LABEL_COLOR,
        )
        _run_gh(
            [
                "label", "create", WOS_EXECUTING_LABEL,
                "--repo", repo,
                "--color", WOS_EXECUTING_LABEL_COLOR,
                "--description", "WOS UoW is actively executing against this issue",
            ],
            gh_bin=gh_bin,
        )
        return True

    except subprocess.CalledProcessError as exc:
        log.warning(
            "wos_issue_lifecycle: _ensure_wos_executing_label_exists failed for %s "
            "— exit %d: %s",
            repo,
            exc.returncode,
            (exc.stderr or b"").decode(errors="replace")[:300],
        )
        return False
    except Exception as exc:
        log.warning(
            "wos_issue_lifecycle: _ensure_wos_executing_label_exists unexpected error "
            "for %s — %s: %s",
            repo, type(exc).__name__, exc,
        )
        return False


def _issue_has_wos_executing_label(issue_number: int, repo: str, gh_bin: str = "gh") -> bool:
    """
    Return True if the GitHub issue already has the wos:executing label.

    Used as the idempotency guard in stamp_issue_executing — if the label
    is already present, a UoW is already executing and a new one must not
    be created.

    Returns False on any gh CLI failure (conservative: allows execution to
    proceed when the check itself fails).
    """
    try:
        result = _run_gh(
            [
                "issue", "view", str(issue_number),
                "--repo", repo,
                "--json", "labels",
            ],
            gh_bin=gh_bin,
        )
        data = json.loads(result.stdout.decode(errors="replace"))
        label_names = {lbl["name"] for lbl in data.get("labels", [])}
        return WOS_EXECUTING_LABEL in label_names
    except Exception as exc:
        log.debug(
            "wos_issue_lifecycle: could not check labels on %s#%d — %s: %s "
            "(assuming label absent)",
            repo, issue_number, type(exc).__name__, exc,
        )
        return False


def _remove_wos_executing_label(issue_number: int, repo: str, gh_bin: str = "gh") -> None:
    """
    Remove the wos:executing label from the issue.

    Raises subprocess.CalledProcessError on failure — callers catch this.
    """
    _run_gh(
        [
            "issue", "edit", str(issue_number),
            "--repo", repo,
            "--remove-label", WOS_EXECUTING_LABEL,
        ],
        gh_bin=gh_bin,
    )


def _post_comment(issue_number: int, repo: str, body: str, gh_bin: str = "gh") -> None:
    """
    Post a comment to the GitHub issue.

    Raises subprocess.CalledProcessError on failure — callers catch this.
    """
    _run_gh(
        [
            "issue", "comment", str(issue_number),
            "--repo", repo,
            "--body", body,
        ],
        gh_bin=gh_bin,
    )


def _close_issue(issue_number: int, repo: str, gh_bin: str = "gh") -> None:
    """
    Close the GitHub issue.

    Raises subprocess.CalledProcessError on failure — callers catch this.
    """
    _run_gh(
        ["issue", "close", str(issue_number), "--repo", repo],
        gh_bin=gh_bin,
    )


# ---------------------------------------------------------------------------
# Public lifecycle stamp functions
# ---------------------------------------------------------------------------

def stamp_issue_executing(
    issue_number: int,
    uow_id: str,
    repo: str = "dcetlin/Lobster",
    gh_bin: str = "gh",
) -> bool:
    """
    Stamp a GitHub issue as having an active WOS UoW executing against it.

    Steps:
    1. Ensure wos:executing label exists on the repo (create if absent).
    2. Check if issue already has wos:executing label → idempotency guard.
       If already present, return False — caller must skip UoW creation.
    3. Add wos:executing label to the issue.
    4. Post a comment noting the UoW ID and executing status.

    Returns:
        True  — label added and comment posted successfully.
        False — issue already has the label (idempotency guard fired), or
                any gh CLI call failed (non-blocking).

    Never raises. All exceptions are logged and False is returned.
    """
    try:
        # Step 1: Ensure label exists in the repo (non-blocking if this fails)
        _ensure_wos_executing_label_exists(repo, gh_bin=gh_bin)

        # Step 2: Idempotency guard — has this issue already been stamped?
        if _issue_has_wos_executing_label(issue_number, repo, gh_bin=gh_bin):
            log.warning(
                "wos_issue_lifecycle: stamp_issue_executing skipped for %s#%d "
                "— wos:executing label already present (duplicate UoW creation guard fired). "
                "UoW %r not created.",
                repo, issue_number, uow_id,
            )
            return False

        # Step 3: Add label to the issue
        _run_gh(
            [
                "issue", "edit", str(issue_number),
                "--repo", repo,
                "--add-label", WOS_EXECUTING_LABEL,
            ],
            gh_bin=gh_bin,
        )

        # Step 4: Post comment
        comment_body = (
            f"## WOS UoW Created — {uow_id}\n"
            f"**Status:** executing\n"
            f"A Work Orchestration System unit of work has been created for this issue. "
            f"The executor will pick it up shortly.\n"
            f"**UoW ID:** `{uow_id}`"
        )
        _post_comment(issue_number, repo, comment_body, gh_bin=gh_bin)

        log.info(
            "wos_issue_lifecycle: stamped %s#%d as executing (UoW %s)",
            repo, issue_number, uow_id,
        )
        return True

    except subprocess.CalledProcessError as exc:
        log.warning(
            "wos_issue_lifecycle: stamp_issue_executing failed for %s#%d (UoW %s) "
            "— exit %d: %s",
            repo, issue_number, uow_id,
            exc.returncode,
            (exc.stderr or b"").decode(errors="replace")[:300],
        )
        return False
    except Exception as exc:
        log.warning(
            "wos_issue_lifecycle: stamp_issue_executing unexpected error for %s#%d "
            "(UoW %s) — %s: %s",
            repo, issue_number, uow_id, type(exc).__name__, exc,
        )
        return False


def stamp_issue_complete(
    issue_number: int,
    uow_id: str,
    summary: str,
    repo: str = "dcetlin/Lobster",
    gh_bin: str = "gh",
) -> bool:
    """
    Stamp a GitHub issue as completed after a successful (pearl) UoW.

    Steps:
    1. Remove wos:executing label from the issue.
    2. Close the issue.
    3. Post a completion comment with the UoW ID and summary.

    The close step is non-fatal when the issue is already closed — this
    handles the case where a human closed the issue during execution.

    Returns:
        True  — all steps completed (or close was a no-op on already-closed).
        False — label removal failed or any unexpected error.

    Never raises. All exceptions are logged and False is returned.
    """
    try:
        # Step 1: Remove label
        _remove_wos_executing_label(issue_number, repo, gh_bin=gh_bin)

        # Step 2: Close issue (non-fatal if already closed)
        try:
            _close_issue(issue_number, repo, gh_bin=gh_bin)
        except subprocess.CalledProcessError as close_exc:
            stderr_msg = (close_exc.stderr or b"").decode(errors="replace")
            if "already closed" in stderr_msg.lower() or "already" in stderr_msg.lower():
                log.info(
                    "wos_issue_lifecycle: %s#%d already closed — skipping close (UoW %s)",
                    repo, issue_number, uow_id,
                )
            else:
                log.warning(
                    "wos_issue_lifecycle: gh issue close failed for %s#%d (UoW %s) "
                    "— exit %d: %s (continuing to post comment)",
                    repo, issue_number, uow_id,
                    close_exc.returncode,
                    stderr_msg[:300],
                )

        # Step 3: Post completion comment
        truncated_summary = summary[:200] + ("…" if len(summary) > 200 else "")
        comment_body = (
            f"## WOS UoW Complete — {uow_id}\n"
            f"**Outcome:** pearl (verified artifact)\n"
            f"**Summary:** {truncated_summary}\n"
            f"This issue was closed automatically by the Work Orchestration System."
        )
        _post_comment(issue_number, repo, comment_body, gh_bin=gh_bin)

        log.info(
            "wos_issue_lifecycle: stamped %s#%d as complete (UoW %s)",
            repo, issue_number, uow_id,
        )
        return True

    except subprocess.CalledProcessError as exc:
        log.warning(
            "wos_issue_lifecycle: stamp_issue_complete failed for %s#%d (UoW %s) "
            "— exit %d: %s",
            repo, issue_number, uow_id,
            exc.returncode,
            (exc.stderr or b"").decode(errors="replace")[:300],
        )
        return False
    except Exception as exc:
        log.warning(
            "wos_issue_lifecycle: stamp_issue_complete unexpected error for %s#%d "
            "(UoW %s) — %s: %s",
            repo, issue_number, uow_id, type(exc).__name__, exc,
        )
        return False


def stamp_issue_failed(
    issue_number: int,
    uow_id: str,
    repo: str = "dcetlin/Lobster",
    gh_bin: str = "gh",
) -> bool:
    """
    Stamp a GitHub issue after a failed UoW execution.

    Steps:
    1. Remove wos:executing label from the issue.
    2. Post a failure comment (issue left OPEN — retry eligible).

    The issue is NOT closed — it remains in the queue for re-dispatch.

    Returns:
        True  — label removed and comment posted.
        False — any gh CLI failure (non-blocking).

    Never raises. All exceptions are logged and False is returned.
    """
    try:
        # Step 1: Remove label
        _remove_wos_executing_label(issue_number, repo, gh_bin=gh_bin)

        # Step 2: Post failure comment (issue stays open)
        comment_body = (
            f"## WOS UoW Failed — {uow_id}\n"
            f"**Outcome:** execution_failed\n"
            f"The executing unit of work failed. This issue has been returned to the queue "
            f"and is eligible for re-dispatch after Steward re-diagnosis.\n"
            f"**UoW ID:** `{uow_id}`"
        )
        _post_comment(issue_number, repo, comment_body, gh_bin=gh_bin)

        log.info(
            "wos_issue_lifecycle: stamped %s#%d as failed (UoW %s) — issue left open",
            repo, issue_number, uow_id,
        )
        return True

    except subprocess.CalledProcessError as exc:
        log.warning(
            "wos_issue_lifecycle: stamp_issue_failed failed for %s#%d (UoW %s) "
            "— exit %d: %s",
            repo, issue_number, uow_id,
            exc.returncode,
            (exc.stderr or b"").decode(errors="replace")[:300],
        )
        return False
    except Exception as exc:
        log.warning(
            "wos_issue_lifecycle: stamp_issue_failed unexpected error for %s#%d "
            "(UoW %s) — %s: %s",
            repo, issue_number, uow_id, type(exc).__name__, exc,
        )
        return False


def stamp_issue_unverifiable(
    issue_number: int,
    uow_id: str,
    repo: str = "dcetlin/Lobster",
    gh_bin: str = "gh",
) -> bool:
    """
    Stamp a GitHub issue after a UoW completed but could not be verified.

    Steps:
    1. Remove wos:executing label from the issue.
    2. Post an unverifiable comment (issue left OPEN — sweeper can re-pick up).

    The issue is NOT closed — it remains visible for re-triage.

    Returns:
        True  — label removed and comment posted.
        False — any gh CLI failure (non-blocking).

    Never raises. All exceptions are logged and False is returned.
    """
    try:
        # Step 1: Remove label
        _remove_wos_executing_label(issue_number, repo, gh_bin=gh_bin)

        # Step 2: Post unverifiable comment (issue stays open)
        comment_body = (
            f"## WOS UoW Unverifiable — {uow_id}\n"
            f"**Outcome:** completed but unverifiable (no artifact trail)\n"
            f"The executing unit of work completed but left no verifiable artifact. "
            f"This issue has been left open for re-triage by the Sweeper or Dan.\n"
            f"**UoW ID:** `{uow_id}`"
        )
        _post_comment(issue_number, repo, comment_body, gh_bin=gh_bin)

        log.info(
            "wos_issue_lifecycle: stamped %s#%d as unverifiable (UoW %s) — issue left open",
            repo, issue_number, uow_id,
        )
        return True

    except subprocess.CalledProcessError as exc:
        log.warning(
            "wos_issue_lifecycle: stamp_issue_unverifiable failed for %s#%d (UoW %s) "
            "— exit %d: %s",
            repo, issue_number, uow_id,
            exc.returncode,
            (exc.stderr or b"").decode(errors="replace")[:300],
        )
        return False
    except Exception as exc:
        log.warning(
            "wos_issue_lifecycle: stamp_issue_unverifiable unexpected error for %s#%d "
            "(UoW %s) — %s: %s",
            repo, issue_number, uow_id, type(exc).__name__, exc,
        )
        return False
