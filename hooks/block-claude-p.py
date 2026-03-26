#!/usr/bin/env python3
"""PreToolUse hook: detect and warn/block agents from spawning new `claude -p` sessions.

## Purpose

When an agent writes a Bash command containing `claude -p` or `claude --print`,
it creates an independent Claude Code session that competes with the dispatcher's
active MCP stdio connection. The root cause of the 2026-03-25 incident where the
dispatcher's MCP connection dropped was exactly this: scheduled jobs calling
`claude -p` while the dispatcher was running triggered session conflict errors.

This hook prevents agents from generating new `claude -p` invocations by
detecting them in Bash tool calls and either warning or blocking, depending on
the mode configured by the `LOBSTER_BLOCK_CLAUDE_P_MODE` environment variable.

## Scope

Bash tool calls only. Write and Edit tool calls are intentionally ignored — blocking
writes that reference `claude -p` in source code, comments, or docs is unnecessary
and produces false positives that are hard to debug.

## Mode

Controlled by `LOBSTER_BLOCK_CLAUDE_P_MODE`:
  - `warn` (default): log the match, print a warning to stderr, allow the tool call
  - `block`: hard-block the tool call with exit 2

Deploy in `warn` mode first to validate zero false positives, then switch to `block`.

## Allowlist

The hook does not fire for:
  - Lines that start with `#` (shell comments) before the claude invocation
  - Known-safe caller scripts: `run-job.sh`, `claude-persistent.sh`
  - String literal contexts: patterns where `claude -p` appears inside quotes
    as an argument to echo, printf, cat, etc. (heuristic — see _is_allowlisted)

## Logging

Match details are written to ~/lobster-workspace/logs/claude-p-blocks.jsonl
as newline-delimited JSON objects for post-hoc analysis.

## Exit codes
  0 — no match, or match is allowlisted, or mode is `warn`
  1 — warning mode: match detected (soft warning; agent sees it and can reconsider)
  2 — block mode: match detected (hard block; Bash call is aborted)
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Pattern: detect `claude -p` / `claude --print` as an actual shell invocation.
# Matches `claude` followed by optional whitespace and then `-p` or `--print`,
# then a word boundary or whitespace (to avoid matching `-profile`, `--printfoo`).
# ---------------------------------------------------------------------------
_CLAUDE_P_PATTERN = re.compile(r"\bclaude\s+(-p|--print)(\s|$)", re.MULTILINE)

# Known-safe scripts that are permitted to call `claude -p`.
# These are committed infrastructure scripts, not agent-generated calls.
_SAFE_CALLERS = frozenset({"run-job.sh", "claude-persistent.sh"})

# ---------------------------------------------------------------------------
# Allowlist helpers (pure functions)
# ---------------------------------------------------------------------------


def _lines_with_match(command: str) -> list[str]:
    """Return lines of `command` that match the claude -p pattern."""
    return [
        line for line in command.splitlines()
        if _CLAUDE_P_PATTERN.search(line)
    ]


def _is_comment_line(line: str) -> bool:
    """Return True if the match line is a shell comment (starts with #, ignoring whitespace)."""
    return line.lstrip().startswith("#")


def _is_safe_caller(line: str) -> bool:
    """Return True if the line invokes a known-safe caller script."""
    return any(caller in line for caller in _SAFE_CALLERS)


def _is_string_literal_context(line: str) -> bool:
    """Heuristic: return True if `claude -p` appears as an argument to a print command.

    This covers common false-positive patterns like:
      echo "run: claude -p"
      printf "usage: claude -p <task>"

    The heuristic is: if the line starts with echo, printf, cat, or a variable
    assignment containing a quoted string, and the match is inside a quoted section.

    This is intentionally conservative — only clearly safe patterns are excluded.
    Ambiguous cases are left for the warn/block logic to handle.
    """
    stripped = line.lstrip()
    print_prefixes = ("echo ", "echo\t", "printf ", "printf\t")
    if any(stripped.startswith(p) for p in print_prefixes):
        return True
    # Variable assignment: VAR="...claude -p..."
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=\s*[\"']", stripped):
        return True
    return False


def _is_allowlisted(line: str) -> bool:
    """Return True if this line should NOT trigger the hook."""
    return (
        _is_comment_line(line)
        or _is_safe_caller(line)
        or _is_string_literal_context(line)
    )


def _find_triggering_lines(command: str) -> list[str]:
    """Return lines that match the claude -p pattern and are NOT allowlisted."""
    return [
        line for line in _lines_with_match(command)
        if not _is_allowlisted(line)
    ]


# ---------------------------------------------------------------------------
# Logging (side effect, isolated at the boundary)
# ---------------------------------------------------------------------------


def _log_match(command: str, triggering_lines: list[str], mode: str, session_id: str) -> None:
    """Append a JSONL entry to ~/lobster-workspace/logs/claude-p-blocks.jsonl.

    Best-effort: any failure is silently swallowed so the hook never blocks
    due to a logging problem.
    """
    try:
        log_dir = Path(os.path.expanduser("~/lobster-workspace/logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "claude-p-blocks.jsonl"

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "mode": mode,
            "triggering_lines": triggering_lines,
            "command_snippet": command[:500],  # Truncate for log hygiene
        }
        with log_file.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Never block on logging failure


# ---------------------------------------------------------------------------
# Main hook logic
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # Unparseable input — don't block

    tool_name = data.get("tool_name", "")

    # Only check Bash tool calls. Write/Edit are explicitly out of scope.
    if tool_name != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    triggering_lines = _find_triggering_lines(command)

    if not triggering_lines:
        sys.exit(0)

    session_id = data.get("session_id", "unknown")
    mode = os.environ.get("LOBSTER_BLOCK_CLAUDE_P_MODE", "warn").lower().strip()

    _log_match(command, triggering_lines, mode, session_id)

    warning_message = (
        "Warning: Bash command contains `claude -p` / `claude --print`.\n\n"
        "Spawning an independent `claude -p` session competes with the dispatcher's "
        "active MCP stdio connection and can cause session conflicts or dropped connections. "
        "Only committed infrastructure scripts (run-job.sh, claude-persistent.sh) should "
        "call `claude -p` directly.\n\n"
        "If you need to run a scheduled task or background job, route it through the "
        "dispatcher's Agent spawning mechanism instead.\n\n"
        f"Detected in: {triggering_lines[0]!r}"
    )

    if mode == "block":
        print(
            f"BLOCKED: {warning_message}",
            file=sys.stderr,
        )
        sys.exit(2)
    else:
        # warn mode (default): soft warning, allow the call through
        print(warning_message, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
