"""
WOS result contract — utility for subagents executing a Unit of Work.

When the Executor dispatches a subagent to carry out a UoW prescription, that
subagent must write a result.json file at the ``output_ref`` path before it
exits. The Steward reads this file on its next heartbeat cycle to determine
whether the UoW is complete, failed, or needs a follow-on prescription.

Without a result file the Steward cannot distinguish a successful silent exit
from a crash — it will treat the UoW as an orphan and eventually mark it
failed via TTL expiry. Writing the result file is therefore a hard requirement
for every WOS subagent.

Usage (inside a WOS subagent):

    from orchestration.result_writer import write_result
    write_result(output_ref, status="done", summary="PR #42 opened and tests pass")

The ``output_ref`` path is provided to the subagent in its task prompt by the
Executor. Look for it in the prescription block under ``output_ref:``.

Schema written to ``<output_ref>.result.json``:

    {
        "status":     "done" | "failed",
        "outcome":    "complete" | "failed",   # Steward-compatible alias for status
        "success":    true | false,             # Steward-compatible convenience field
        "summary":    "<human-readable one-line summary>",
        "artifacts":  ["<path1>", "<path2>", ...],   # optional
        "written_at": "<ISO-8601 UTC timestamp>"
    }

``status`` is the subagent-facing API; ``outcome`` and ``success`` are written
for backward compatibility with the Steward's result-file parser, which reads
``outcome`` as its primary routing signal (executor-contract.md §Schema).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_result(
    output_ref: str,
    status: Literal["done", "failed"],
    summary: str,
    artifacts: list[str] | None = None,
) -> Path:
    """
    Write a result.json file at the path derived from ``output_ref``.

    The file is written atomically (write to a sibling tmp file, then rename)
    so the Steward never reads a partial write.

    Args:
        output_ref: The output reference path provided in the WOS task prompt.
            Typically an absolute path like
            ``~/lobster-workspace/orchestration/outputs/<uow-id>.json``.
            The result file is written adjacent to this path:
            ``<stem>.result.json`` (replacing the extension) or
            ``<output_ref>.result.json`` (suffix appended) when the path has
            no extension.
        status: ``"done"`` for successful completion, ``"failed"`` for any
            failure that prevents the prescription from being fulfilled.
        summary: A single human-readable sentence describing what happened.
            The Steward surfaces this in its diagnosis log and to the user
            when a surface condition fires. Keep it factual and concise.
        artifacts: Optional list of absolute file paths produced during
            execution (e.g. PR URLs, generated report paths). The Steward
            reads this list when building its diagnosis context.

    Returns:
        The Path where the result file was written.

    Raises:
        OSError: If the parent directory cannot be created or the file cannot
            be written. Let this propagate — the caller's exception handler
            should log the failure and call ``write_result`` with
            ``status="failed"`` if a retry is feasible.
    """
    result_path = _result_json_path(output_ref)
    result_path.parent.mkdir(parents=True, exist_ok=True)

    # Map status to Steward-compatible outcome/success fields so the Steward
    # can read this file without changes (executor-contract.md §Schema).
    outcome = "complete" if status == "done" else "failed"
    success = status == "done"

    payload: dict = {
        "status": status,
        "outcome": outcome,
        "success": success,
        "summary": summary,
        "written_at": _now_iso(),
    }
    if artifacts:
        payload["artifacts"] = artifacts

    _atomic_write(result_path, json.dumps(payload, indent=2))
    return result_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result_json_path(output_ref: str) -> Path:
    """
    Derive the result.json path from output_ref.

    Primary convention: replace extension (foo.json -> foo.result.json).
    Fallback: append .result.json when output_ref has no extension.

    This mirrors the convention used by the Steward in steward.py and the
    Executor in executor.py — all three must agree on the path or the Steward
    will classify the UoW as an orphan.
    """
    p = Path(os.path.expanduser(output_ref))
    if p.suffix:
        return p.with_suffix(".result.json")
    return Path(str(p) + ".result.json")


def _atomic_write(dest: Path, text: str) -> None:
    """
    Write ``text`` to ``dest`` atomically using a sibling tmp file + rename.

    Uses a tmp file in the same directory as ``dest`` so the rename is a
    same-filesystem move (atomic on POSIX).
    """
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=dest.parent,
        prefix=f".{dest.name}.tmp.",
        suffix=".json",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        tmp_path.rename(dest)
    except Exception:
        # Best-effort cleanup; then re-raise so the caller can handle.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
