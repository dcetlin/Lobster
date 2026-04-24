"""
WOS execution completion helper (issue #669).

Provides maybe_complete_wos_uow — the deferred execution_complete transition
for the async inbox dispatch path.

Background: when the Executor writes a wos_execute message to the inbox (fire-
and-forget), it transitions the UoW to 'executing' rather than 'ready-for-
steward'. The execution_complete audit entry and the final executing →
ready-for-steward transition happen here, only after the subagent confirms
completion by calling write_result.

This module is imported by both inbox_server.py (production) and test code.
It has no dependency on inbox_server's heavy MCP server stack — only on the
orchestration.registry module.

Naming convention: task_id for WOS dispatches is "wos-{uow_id}", set by
route_wos_message in dispatcher_handlers.py.

Close-out protocol: when a UoW with a GitHub issue source transitions to
ready-for-steward, a comment is posted to the source issue summarising the
completion. Source format: "github:issue/N" (set at germination time). Non-GitHub
sources (telegram, system, etc.) are silently skipped — no side effect.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("wos_completion")

# Lazy import — only imported when a GitHub issue source is detected.
# This avoids pulling in the lifecycle module in test environments that
# don't have a real gh CLI available.
_lifecycle_imported = False
_stamp_issue_complete_fn = None
_stamp_issue_unverifiable_fn = None


def _import_lifecycle() -> None:
    """Import wos_issue_lifecycle functions on first use (lazy, non-blocking)."""
    global _lifecycle_imported, _stamp_issue_complete_fn, _stamp_issue_unverifiable_fn
    if _lifecycle_imported:
        return
    try:
        from orchestration.wos_issue_lifecycle import (
            stamp_issue_complete as _sic,
            stamp_issue_unverifiable as _siu,
        )
        _stamp_issue_complete_fn = _sic
        _stamp_issue_unverifiable_fn = _siu
    except ImportError as exc:
        log.debug("wos_completion: could not import wos_issue_lifecycle — %s", exc)
    finally:
        _lifecycle_imported = True


#: Prefix used by route_wos_message to form the task_id for wos_execute dispatches.
WOS_TASK_ID_PREFIX = "wos-"

#: write_result status value that signals successful subagent completion.
WRITE_RESULT_SUCCESS_STATUS = "success"

#: Pattern that identifies a GitHub issue source reference.
#: Format: "github:issue/<number>" (integer issue number, no leading zeros required).
_GITHUB_ISSUE_SOURCE_RE = re.compile(r"^github:issue/(\d+)$")

# ---------------------------------------------------------------------------
# Result file enrichment — summary + artifact extraction
# ---------------------------------------------------------------------------

#: Maximum characters to capture from result_text as the summary field.
_RESULT_SUMMARY_MAX_CHARS: int = 300

#: Regex patterns for extracting artifact references from agent output.
_PR_REF_RE = re.compile(r"PR\s*#(\d+)", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"issue\s*#(\d+)", re.IGNORECASE)
_FILE_PATH_RE = re.compile(r"(~/[^\s,;\"']+\.(?:md|json|py|sh|txt|yaml|yml))")

#: Patterns for typed outcome_refs extraction (issue #880).
#: PR: explicit "PR #N", "pull request #N", or GitHub pull URL.
_OUTCOME_PR_RE = re.compile(
    r"(?:pull\s+request|PR)\s*#(\d+)|/pull/(\d+)",
    re.IGNORECASE,
)
#: Issue: "issue #N", "closes #N", "fixes #N".
_OUTCOME_ISSUE_RE = re.compile(
    r"(?:issue|closes|fixes)\s*#(\d+)",
    re.IGNORECASE,
)
#: Commit SHA: 7–40 hex chars, word-bounded, not inside a URL path segment.
#: Conservative: must be preceded/followed by whitespace or punctuation to
#: avoid false positives in file content and hex color codes.
_OUTCOME_COMMIT_RE = re.compile(r"(?<![/\w])([0-9a-f]{7,40})(?![/\w])")
#: File path: absolute-looking or home-relative paths with recognized extensions.
_OUTCOME_FILE_RE = re.compile(
    r"(?:^|[\s(,])([~/][^\s,;\"']+\.(?:py|md|yaml|yml|json|sh|txt))",
    re.MULTILINE,
)

#: Words that are very unlikely to be commit SHAs (common English hex-looking words).
_SHA_DENYLIST = frozenset({"dead", "beef", "cafe", "babe", "face", "fade"})


def _is_plausible_sha(candidate: str) -> bool:
    """
    Return True when candidate looks like a git commit SHA.

    A plausible SHA:
    - Is 7–40 lowercase hex characters.
    - Is not in the SHA denylist (common false-positive hex words).
    - Is not all-digit (would be mistaken for a number).

    Pure function — no I/O.
    """
    if candidate.lower() in _SHA_DENYLIST:
        return False
    if candidate.isdigit():
        return False
    return True


def _extract_outcome_refs(text: str, repo: str = "dcetlin/Lobster") -> list[dict]:
    """
    Extract typed outcome refs from agent result text for storage in the registry.

    Pure function — no side effects.

    Returns a list of typed ref dicts matching the issue #880 schema:
      [{type: "pr"|"issue"|"file"|"commit", ref: str, category: str}]

    Classification heuristics:
    - PR refs   → category "pearl" (a merged PR is a concrete deliverable)
    - Issue refs → category "seed"  (a filed issue is a future-value pointer)
    - File refs  → category "pearl" (a committed file is a concrete artifact)
    - Commit SHAs → category "pearl" (a merged commit is a concrete deliverable)

    Deduplication: each (type, ref) pair appears at most once.

    Args:
        text: The result_text string from the write_result call.
        repo: The GitHub repo slug used to qualify PR and issue refs.
    """
    seen: set[tuple[str, str]] = set()
    refs: list[dict] = []

    def _add(ref_type: str, ref: str, category: str) -> None:
        key = (ref_type, ref)
        if key not in seen:
            seen.add(key)
            refs.append({"type": ref_type, "ref": ref, "category": category})

    # PR refs
    for m in _OUTCOME_PR_RE.finditer(text):
        number = m.group(1) or m.group(2)
        _add("pr", f"{repo}#{number}", "pearl")

    # Issue refs
    for m in _OUTCOME_ISSUE_RE.finditer(text):
        number = m.group(1)
        _add("issue", f"{repo}#{number}", "seed")

    # File paths
    for m in _OUTCOME_FILE_RE.finditer(text):
        path = m.group(1).strip()
        _add("file", path, "pearl")

    # Commit SHAs — only when text explicitly contains "commit" nearby to reduce noise
    if re.search(r"\bcommit\b", text, re.IGNORECASE):
        for m in _OUTCOME_COMMIT_RE.finditer(text):
            sha = m.group(1).lower()
            if _is_plausible_sha(sha):
                _add("commit", sha, "pearl")

    return refs


def _extract_artifact_refs(text: str) -> dict:
    """
    Extract PR numbers, issue numbers, and file paths from agent output text.

    Pure function — no side effects.

    Returns a dict with keys:
    - "pr_numbers": list of int PR numbers found (e.g. [42, 99])
    - "issue_numbers": list of int issue numbers found (e.g. [123])
    - "file_paths": list of str file paths found (e.g. ["~/lobster-workspace/foo.md"])

    Note: _extract_outcome_refs provides the typed list form used for the registry
    artifacts field (issue #880). This function is retained for result.json enrichment.
    """
    pr_numbers = [int(m) for m in _PR_REF_RE.findall(text)]
    issue_numbers = [int(m) for m in _ISSUE_REF_RE.findall(text)]
    file_paths = _FILE_PATH_RE.findall(text)
    return {
        "pr_numbers": pr_numbers,
        "issue_numbers": issue_numbers,
        "file_paths": file_paths,
    }


def _enrich_result_file(output_ref: str, result_text: str | None) -> None:
    """
    Read the existing result.json at output_ref, add 'summary' and 'refs' fields,
    and rewrite it atomically.

    Called after a successful UoW completion (outcome=complete) to capture:
    - summary: first _RESULT_SUMMARY_MAX_CHARS chars of result_text
    - refs: extracted PR numbers, issue numbers, and file paths from result_text

    No-op when:
    - result_text is None or empty
    - result file does not exist (subagent skipped result_writer)
    - result file is unreadable or not valid JSON

    Side effects are isolated to this function. Failures are logged and swallowed
    — result file enrichment must never block the UoW transition.
    """
    if not result_text:
        return

    # Derive result file path (mirrors result_writer._result_json_path)
    p = Path(os.path.expanduser(output_ref))
    if p.suffix:
        result_path = p.with_suffix(".result.json")
    else:
        result_path = Path(str(p) + ".result.json")

    if not result_path.exists():
        log.debug(
            "_enrich_result_file: result file not found at %s — skipping enrichment",
            result_path,
        )
        return

    try:
        import json
        import tempfile
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(
            "_enrich_result_file: could not read result file %s — %s: %s",
            result_path, type(exc).__name__, exc,
        )
        return

    try:
        import json
        import tempfile

        summary = result_text[:_RESULT_SUMMARY_MAX_CHARS]
        refs = _extract_artifact_refs(result_text)

        # Only add fields not already present (subagent may have set summary itself)
        if "summary" not in payload:
            payload["summary"] = summary
        if "refs" not in payload and any(refs.values()):
            payload["refs"] = refs

        # Atomic rewrite
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=result_path.parent,
            prefix=f".{result_path.name}.enrich.",
            suffix=".json",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2))
            tmp_path.rename(result_path)
            log.debug(
                "_enrich_result_file: enriched %s (summary_len=%d, refs=%s)",
                result_path, len(summary), refs,
            )
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    except Exception as exc:
        log.warning(
            "_enrich_result_file: failed to enrich result file %s — %s: %s",
            result_path, type(exc).__name__, exc,
        )

# ---------------------------------------------------------------------------
# Back-propagation — write result text to executor output file (issue #867)
# ---------------------------------------------------------------------------

def _backpropagate_result_to_output_file(
    uow_id: str,
    output_ref: str,
    result_text: str | None,
) -> None:
    """
    Write the write_result payload to the executor output file when none exists.

    Problem (issue #867): when a WOS subagent calls write_result, the result
    text is stored in the MCP/session layer but the executor output file
    ({output_ref}.result.json) is never updated. The Steward reads that file to
    determine completion, so UoWs whose subagents did not explicitly write a
    result file are classified as 'outcome_unverifiable'.

    Fix: when maybe_complete_wos_uow fires (write_result confirmed), check
    whether {output_ref}.result.json already exists. If not, write a minimal
    conforming result file so the Steward can verify the outcome.

    The result file schema follows executor-contract.md:
    - uow_id: the UoW ID (validated by the Steward before reading other fields)
    - outcome: "complete" (write_result status="success" implies success)
    - success: true
    - reason: the write_result text (truncated to 500 chars)
    - executor_id: "write_result_backprop" (identifies this synthetic record)

    Also writes result_text to the primary output_ref path when that file is
    missing or empty (sentinel-only), so agent-status.sh and the steward have
    human-readable content to display.

    Non-fatal: failures are logged and swallowed — back-propagation must never
    block the UoW transition or cause write_result to fail.

    Args:
        uow_id: The WOS unit-of-work ID.
        output_ref: The output_ref path for this UoW (absolute path).
        result_text: The text field from the write_result call. May be None or empty.
    """
    if not output_ref:
        return

    try:
        import json as _json
        import tempfile as _tempfile

        p = Path(output_ref)
        # Derive result.json path — mirrors executor._result_json_path
        if p.suffix:
            result_path = p.with_suffix(".result.json")
        else:
            result_path = Path(str(p) + ".result.json")

        if result_path.exists():
            # Already present — _enrich_result_file will add summary/refs
            log.debug(
                "_backpropagate_result_to_output_file: result file already exists at %s — skipping",
                result_path,
            )
            return

        # Write a minimal conforming result.json
        reason = (result_text or "").strip()[:500] or "completed via write_result"
        payload = {
            "uow_id": uow_id,
            "outcome": "complete",
            "success": True,
            "reason": reason,
            "executor_id": "write_result_backprop",
        }

        result_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp → rename so the Steward never reads a partial file
        tmp_fd, tmp_name = _tempfile.mkstemp(
            dir=result_path.parent,
            prefix=f".{result_path.name}.backprop.",
            suffix=".json",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(_json.dumps(payload, indent=2))
            tmp_path.rename(result_path)
            log.info(
                "_backpropagate_result_to_output_file: wrote result.json for UoW %r at %s",
                uow_id,
                result_path,
            )
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        # Also write result_text to the primary output_ref when it is missing
        # or still a sentinel (zero-byte file written by the executor to guard
        # against crashed_output_ref_missing detection).
        if result_text:
            primary = p
            if not primary.exists() or primary.stat().st_size == 0:
                primary.parent.mkdir(parents=True, exist_ok=True)
                primary.write_text(result_text, encoding="utf-8")
                log.info(
                    "_backpropagate_result_to_output_file: wrote result_text to primary output %s",
                    primary,
                )

    except Exception as exc:
        log.warning(
            "_backpropagate_result_to_output_file: failed for UoW %r — %s: %s",
            uow_id, type(exc).__name__, exc,
        )


# ---------------------------------------------------------------------------
# Output classification — pure function
# ---------------------------------------------------------------------------

#: Valid output classification labels (seed/pearl/heat/shit).
_OUTPUT_CLASSIFICATIONS = frozenset({"seed", "pearl", "heat", "shit"})

#: Default classification when no heuristic fires — seed is the conservative
#: choice: assume the output has future value until the Steward evaluates it.
_DEFAULT_CLASSIFICATION = "seed"


def classify_uow_output(output_ref: str | None, result_text: str | None) -> str:
    """
    Heuristic classification of a UoW's output type.

    Pure function — no side effects, no I/O. Callers supply the output_ref path
    and (optionally) the write_result text so the classifier can inspect both
    without touching the filesystem or the registry.

    Classification table (first match wins):
    - "pearl"   : output_ref ends with .md or .txt AND result text mentions "PR #"
                  (UoW produced a PR — a concrete deliverable)
    - "pearl"   : result text contains "pull request" or "PR #" or "merged"
    - "heat"    : result text contains "nothing to do" or "no changes" or "skipped"
    - "seed"    : default — output has future value but no stronger signal present

    "shit" is not generated by the classifier — it requires human judgment and is
    set by the Steward during evaluation.

    Args:
        output_ref: The output_ref path written to the UoW at claim time. May be None.
        result_text: The text field from the write_result call. May be None or empty.

    Returns:
        One of "pearl", "heat", or "seed" (default).
    """
    normalized = (result_text or "").lower()

    # Pearl: produced a pull request
    if re.search(r"\bpr\s*#\d+\b|pull request|merged", normalized):
        return "pearl"

    # Heat: found nothing to do
    if re.search(r"\bnothing to do\b|\bno changes\b|\bskipped\b|\bno-op\b", normalized):
        return "heat"

    # Default: conservative — assume future value
    return _DEFAULT_CLASSIFICATION


# ---------------------------------------------------------------------------
# Close-out comment — pure builder + side-effecting poster
# ---------------------------------------------------------------------------

def _build_closeout_comment(
    uow_id: str,
    output_classification: str,
    result_text: str | None,
    agent_type: str,
    date_str: str,
) -> str:
    """
    Pure builder for the GitHub close-out comment body.

    Builds the structured comment that is posted to the source issue when a UoW
    completes. All inputs are plain strings — no I/O, no registry access.

    Args:
        uow_id: The WOS unit-of-work ID.
        output_classification: One of seed/pearl/heat/shit.
        result_text: One-sentence summary from the subagent's write_result call.
                     Truncated to 200 chars to keep the comment readable.
        agent_type: The executor type that ran the UoW (e.g. "functional-engineer").
        date_str: ISO date string (YYYY-MM-DD) for the completion date.

    Returns:
        Formatted markdown comment body string.
    """
    summary_raw = (result_text or "").strip()
    # Truncate to first sentence or 200 chars, whichever is shorter
    first_sentence_end = summary_raw.find(". ")
    if 0 < first_sentence_end < 200:
        summary = summary_raw[: first_sentence_end + 1]
    else:
        summary = summary_raw[:200] + ("…" if len(summary_raw) > 200 else "")

    return (
        f"## WOS UoW Complete — {uow_id}\n"
        f"**Output type:** {output_classification}\n"
        f"**Summary:** {summary}\n"
        f"**Executed by:** {agent_type} on {date_str}\n"
        f"**Result:** {uow_id}.result.json\n"
    )


def _post_github_comment(
    repo: str,
    issue_number: int,
    comment_body: str,
    gh_bin: str = "gh",
) -> None:
    """
    Post a comment to a GitHub issue via the gh CLI.

    Side-effecting: spawns a subprocess. Non-blocking on failure — logs a warning
    and returns so that the UoW transition is never gated on comment delivery.

    Args:
        repo: GitHub repo slug in "owner/repo" format.
        issue_number: Integer issue number.
        comment_body: Markdown comment body to post.
        gh_bin: Path to the gh binary (injectable for tests).
    """
    try:
        subprocess.run(
            [gh_bin, "issue", "comment", str(issue_number), "--repo", repo, "--body", comment_body],
            capture_output=True,
            timeout=30,
            check=True,
        )
        log.info(
            "wos_completion: posted close-out comment to %s#%d",
            repo,
            issue_number,
        )
    except subprocess.CalledProcessError as exc:
        log.warning(
            "wos_completion: gh issue comment failed for %s#%d — exit %d: %s",
            repo,
            issue_number,
            exc.returncode,
            (exc.stderr or b"").decode(errors="replace")[:300],
        )
    except Exception as exc:
        log.warning(
            "wos_completion: could not post close-out comment to %s#%d — %s: %s",
            repo,
            issue_number,
            type(exc).__name__,
            exc,
        )


def _extract_github_issue(source: str) -> tuple[str, int] | None:
    """
    Parse a UoW source string and return (repo, issue_number) for GitHub sources.

    Pure function — no side effects.

    Args:
        source: The UoW source field. Expected format: "github:issue/N".
                The repo slug is read from the LOBSTER_WOS_REPO env var (default:
                "dcetlin/Lobster") so that the source field can remain source-agnostic.

    Returns:
        (repo, issue_number) tuple when source is a GitHub issue reference, else None.
    """
    m = _GITHUB_ISSUE_SOURCE_RE.match(source or "")
    if not m:
        return None
    issue_number = int(m.group(1))
    repo = os.environ.get("LOBSTER_WOS_REPO", "dcetlin/Lobster")
    return (repo, issue_number)


def _post_closeout_comment_if_github(
    uow_id: str,
    source: str,
    output_ref: str | None,
    result_text: str | None,
    agent_type: str,
    gh_bin: str = "gh",
) -> None:
    """
    Classify the UoW output and post a close-out comment to the source GitHub issue.

    No-op when the source is not a GitHub issue reference. Side effects
    (subprocess call) are isolated here — callers remain pure with respect to
    the registry transition.

    Args:
        uow_id: The WOS unit-of-work ID.
        source: The UoW source field (e.g. "github:issue/123" or "telegram").
        output_ref: The output_ref path (used as fallback for classification).
        result_text: Text from the subagent's write_result call.
        agent_type: The executor_type string from the UoW workflow_artifact.
        gh_bin: Path to the gh binary (injectable for tests).
    """
    parsed = _extract_github_issue(source)
    if parsed is None:
        log.debug(
            "wos_completion: source %r is not a GitHub issue — skipping close-out comment",
            source,
        )
        return

    repo, issue_number = parsed
    classification = classify_uow_output(output_ref, result_text)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    comment_body = _build_closeout_comment(
        uow_id=uow_id,
        output_classification=classification,
        result_text=result_text,
        agent_type=agent_type,
        date_str=date_str,
    )
    _post_github_comment(repo, issue_number, comment_body, gh_bin=gh_bin)


# ---------------------------------------------------------------------------
# Issue lifecycle stamp — bidirectional GitHub state sync on completion
# ---------------------------------------------------------------------------

def _stamp_issue_on_completion(
    uow_id: str,
    source: str,
    issue_number: int | None,
    output_ref: str | None,
    result_text: str | None,
    gh_bin: str = "gh",
) -> None:
    """
    Call stamp_issue_complete for any successful UoW completion.

    The source issue is closed regardless of metabolic classification
    (pearl, heat, or seed). A seed UoW — one that filed a follow-up — still
    addressed the source issue and should close it. The metabolic category
    is recorded in the separate close-out comment posted by
    _post_closeout_comment_if_github; it does not drive lifecycle decisions.

    stamp_issue_unverifiable is NOT called here. That function is reserved
    for future cases where a UoW completes but cannot be verified (no artifact
    trail AND no subagent confirmation). All write_result=success paths call
    stamp_issue_complete unconditionally.

    Non-GitHub sources or missing issue_number → no-op.
    Import or gh failures are non-fatal.
    """
    parsed = _extract_github_issue(source)
    if parsed is None or issue_number is None:
        return

    repo, _ = parsed
    _import_lifecycle()

    summary = (result_text or "").strip()[:200]

    if _stamp_issue_complete_fn is not None:
        _stamp_issue_complete_fn(issue_number, uow_id, summary, repo=repo, gh_bin=gh_bin)
    else:
        log.debug(
            "wos_completion: _stamp_issue_on_completion skipped for %s#%d "
            "(lifecycle module not available)",
            repo, issue_number,
        )


# ---------------------------------------------------------------------------
# Main entry point — called by inbox_server.py on every write_result
# ---------------------------------------------------------------------------

def maybe_complete_wos_uow(
    task_id: str,
    status: str,
    result_text: str | None = None,
    gh_bin: str = "gh",
) -> None:
    """
    Transition a WOS UoW from 'executing' to 'ready-for-steward' when its
    subagent calls write_result with status='success'.

    This is the deferred execution_complete transition for the async inbox
    dispatch path (issue #669). The Executor transitions active → executing at
    dispatch time; this function fires the execution_complete audit entry and
    the executing → ready-for-steward transition only after the subagent
    confirms completion via write_result.

    After a successful transition, if the UoW source is a GitHub issue, a
    structured close-out comment is posted to that issue. Non-GitHub sources
    are skipped silently. Comment failure never blocks the registry transition.

    Conditions required to fire:
    - task_id starts with WOS_TASK_ID_PREFIX ("wos-")
    - status == "success" (only successful completions advance the UoW)
    - UoW exists in the registry with status == "executing"

    A UoW not in 'executing' status is skipped silently — this handles the
    case where TTL recovery already failed the UoW, or where a duplicate
    write_result arrives after the first has already completed it.

    Errors are logged but never raised — write_result delivery must not be
    blocked by registry update failures.

    Args:
        task_id: The task_id string from the write_result call.
                 Expected format: "wos-{uow_id}".
        status:  The status from the write_result call ("success" or "error").
        result_text: Optional text from the write_result call; used in the
                     close-out comment summary and output classification.
        gh_bin:  Path to the gh CLI binary (injectable for tests; default "gh").
    """
    if not task_id.startswith(WOS_TASK_ID_PREFIX):
        return
    if status != WRITE_RESULT_SUCCESS_STATUS:
        # Only advance to ready-for-steward on success. Failed write_results
        # leave the UoW in 'executing' for TTL recovery to handle.
        return

    uow_id = task_id[len(WOS_TASK_ID_PREFIX):]
    try:
        from orchestration.registry import Registry, UoWStatus
        from orchestration.paths import REGISTRY_DB

        # Use canonical REGISTRY_DB path (honours REGISTRY_DB_PATH env override).
        # Existence check before Registry() so we skip gracefully in test envs
        # that have no WOS install.
        db_path = REGISTRY_DB
        if not db_path.exists():
            log.debug(
                "maybe_complete_wos_uow: registry DB not found at %s — "
                "skipping (no WOS install or test env)",
                db_path,
            )
            return

        registry = Registry()
        uow = registry.get(uow_id)
        if uow is None:
            log.debug(
                "maybe_complete_wos_uow: UoW %r not found in registry — skipping",
                uow_id,
            )
            return

        if uow.status != UoWStatus.EXECUTING:
            log.debug(
                "maybe_complete_wos_uow: UoW %r is in status %r (expected 'executing') — "
                "skipping (already recovered or duplicate write_result)",
                uow_id,
                uow.status,
            )
            return

        output_ref = uow.output_ref or ""
        registry.complete_uow(uow_id, output_ref)
        log.info(
            "maybe_complete_wos_uow: UoW %r transitioned executing → ready-for-steward "
            "(execution_complete written on write_result confirmation)",
            uow_id,
        )

        # Back-propagation (issue #867): write result_text to the executor output
        # file when the subagent did not write one itself. Must run BEFORE
        # _enrich_result_file so the file exists for enrichment to update.
        if output_ref:
            _backpropagate_result_to_output_file(uow_id, output_ref, result_text)

        # Enrich result file with summary + extracted artifact refs from agent output.
        # Non-fatal — enrichment failure never blocks the UoW transition.
        if output_ref:
            try:
                _enrich_result_file(output_ref, result_text)
            except Exception as enrich_exc:
                log.warning(
                    "maybe_complete_wos_uow: result file enrichment failed for UoW %r — %s: %s",
                    uow_id, type(enrich_exc).__name__, enrich_exc,
                )

        # Registry artifact population (issue #880): extract typed outcome refs from
        # result_text and store them in the registry artifacts field so the steward
        # and retrospective job can traverse the causal chain without reading result files.
        # Non-fatal — artifact extraction must never block the UoW transition.
        if result_text:
            try:
                repo = os.environ.get("LOBSTER_WOS_REPO", "dcetlin/Lobster")
                outcome_refs = _extract_outcome_refs(result_text, repo=repo)
                if outcome_refs:
                    registry.update_artifacts(uow_id, outcome_refs)
                    log.info(
                        "maybe_complete_wos_uow: artifacts extracted for UoW %r: %s",
                        uow_id,
                        outcome_refs,
                    )
                else:
                    log.debug(
                        "maybe_complete_wos_uow: no artifact refs found in result_text for UoW %r",
                        uow_id,
                    )
            except Exception as artifacts_exc:
                log.warning(
                    "maybe_complete_wos_uow: artifact extraction failed for UoW %r — %s: %s",
                    uow_id, type(artifacts_exc).__name__, artifacts_exc,
                )

        # Close-out protocol: post a structured comment to the source GitHub issue.
        # Non-GitHub sources are silently skipped. Comment failure is non-fatal.
        uow_source = uow.source or ""
        agent_type = "functional-engineer"  # conservative default; UoW register is available below
        # Use uow.register to pick a more descriptive agent_type label when available.
        if hasattr(uow, "register") and uow.register:
            agent_type = uow.register

        _post_closeout_comment_if_github(
            uow_id=uow_id,
            source=uow_source,
            output_ref=output_ref,
            result_text=result_text,
            agent_type=agent_type,
            gh_bin=gh_bin,
        )

        # Lifecycle stamp: update GitHub issue state based on output classification.
        # Non-GitHub sources are silently skipped. Stamp failure is non-fatal.
        _stamp_issue_on_completion(
            uow_id=uow_id,
            source=uow_source,
            issue_number=uow.source_issue_number,
            output_ref=output_ref,
            result_text=result_text,
            gh_bin=gh_bin,
        )

    except Exception as exc:
        log.warning(
            "maybe_complete_wos_uow: failed to complete UoW %r — %s: %s",
            uow_id,
            type(exc).__name__,
            exc,
        )
