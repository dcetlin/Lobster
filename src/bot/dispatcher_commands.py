"""
Core dispatcher chat-command helpers: /help, /quota, /status, /config, and the
inline prose command index (usage, agents, inbox, debug, restart mcp/dispatcher).

Extracted from the (now-removed) src/orchestration/dispatcher_handlers.py during
the process-layer slimdown (2026-08-03). That file mixed WOS-orchestration
handlers with a large set of generic, non-WOS command helpers; this module keeps
only the generic ones so these commands keep working without any WOS/orchestration
dependency. WOS-specific fields (e.g. the WOS line in the prose `status` command)
were dropped rather than carried forward.

Consumed by src/bot/pre_handler.py and documented in .claude/sys.dispatcher.bootup.md
(Inline Command Index / System Status Commands sections).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.utils.timezone import format_iso_for_user as _format_iso_for_user


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

COMMAND_HELP: str = """Lobster command index

System status:
  /status             — running agents, CC usage snapshot
  /quota              — CC quota windows and reset times (5h and 7d)
  status / health     — usage %, active agents (prose command)
  usage               — Claude quota windows and reset times (prose command)
  usage full          — full usage report (spawns subagent)
  agents              — list active subagent sessions
  inbox               — queue depth and processing state

LOS (action items):
  /todos              — show open action items with Done/Snooze buttons
  /todo add <text>    — add a new action item
  /todo done <text>   — mark an item done by partial text or ID
  /todo snooze <text> [days] — snooze an item (default: 3 days)

Config (user bootup files):
  /config list                  — list all user config files with line counts
  /config read <filename>       — show file contents (chunked if long)
  /config search <query>        — search for text across all user config files
  /config append <filename> <text> — append text to a user config file

Skills:
  /shop               — list available skills
  /shop install <name> — install and activate a skill
  /skill activate/deactivate <name> — toggle a skill

Restart:
  restart mcp         — restart MCP server (auto-reconnects)
  restart dispatcher  — instructions to restart dispatcher process

Debug:
  debug on / debug off — toggle debug flag file

Help:
  /help / help        — this index
"""


def handle_help() -> str:
    """Handle 'help' / '/help' command — return command index."""
    return COMMAND_HELP


# ---------------------------------------------------------------------------
# CC quota state — path and stale threshold
# ---------------------------------------------------------------------------

# Default path for the cc-budget state file written by cc-usage-poller.py.
# Overridable via LOBSTER_CC_BUDGET_STATE env var or the state_path argument.
_CC_BUDGET_STATE_PATH: Path = Path.home() / ".claude" / "cc-budget" / "state.json"

# Data older than this many hours is treated as unavailable (poller may be down).
QUOTA_STALE_THRESHOLD_HOURS: int = 2


def read_quota_state(state_path: Path | None = None) -> dict | None:
    """Read the CC budget state written by cc-usage-poller.

    Pure read: no side effects beyond file I/O. Returns the parsed dict, or None
    when the file is absent, unreadable, malformed, or missing ``rate_limits``.

    Path resolution order:
    1. ``state_path`` argument (if provided)
    2. ``LOBSTER_CC_BUDGET_STATE`` env var
    3. ``~/.claude/cc-budget/state.json`` (default)
    """
    resolved: Path
    if state_path is not None:
        resolved = state_path
    else:
        env_override = os.environ.get("LOBSTER_CC_BUDGET_STATE")
        resolved = Path(env_override) if env_override else _CC_BUDGET_STATE_PATH

    try:
        text = resolved.read_text(encoding="utf-8")
        data = json.loads(text)
        # Accept state that has either:
        # - ``rate_limits`` (written by cc-usage-poller / cc-usage-collect.sh — v2 schema)
        # - ``token_usage`` (written by local-session-parser — cookie-free fallback)
        # Require at least one to ensure we have meaningful data, not a bare empty dict.
        if "rate_limits" not in data and "token_usage" not in data:
            return None
        return data
    except Exception:
        return None


def _is_quota_state_stale(state: dict) -> bool:
    """Return True if the state's last_updated timestamp exceeds QUOTA_STALE_THRESHOLD_HOURS.

    Falls back to False (fresh) when last_updated is absent or unparseable so that
    partial state data is still surfaced rather than silently suppressed.
    """
    from datetime import datetime as _datetime, timezone as _timezone, timedelta as _timedelta

    last_updated = state.get("last_updated")
    if not last_updated:
        return False  # no timestamp — assume fresh rather than suppress
    try:
        ts_str = last_updated.replace("Z", "+00:00")
        ts = _datetime.fromisoformat(ts_str)
        age = _datetime.now(_timezone.utc) - ts
        return age > _timedelta(hours=QUOTA_STALE_THRESHOLD_HOURS)
    except Exception:
        return False


def _format_token_usage_fallback(token_usage: dict) -> str:
    """Format a CC usage string from local-session-parser token counts.

    Called when rate_limits percentage data is unavailable (poller cookie
    expired) but local token counts from the session-file parser are present.

    Format:
        CC usage (local): today 269M tokens | week 1.2B tokens | 5h 42M tokens
        [cookie expired — no % available]

    Pure function: no side effects. All inputs are arguments.
    """
    def _fmt_tokens(n: int) -> str:
        """Format a raw token count as a human-readable abbreviated string."""
        if n >= 1_000_000_000:
            return f"{n / 1_000_000_000:.1f}B"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.0f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}K"
        return str(n)

    today = _fmt_tokens(token_usage.get("tokens_today", 0))
    week = _fmt_tokens(token_usage.get("tokens_this_week", 0))
    five_h = _fmt_tokens(token_usage.get("five_hour_tokens", 0))
    return (
        f"CC usage (local): today {today} | week {week} | 5h {five_h} tokens\n"
        f"[cookie expired — quota % unavailable]"
    )


def format_quota_message(state: dict | None) -> str:
    """Format a CC usage string from the cc-budget state dict.

    Data source priority:
    1. ``rate_limits`` with non-None pct values (poller or statusLine hook) — full % format.
    2. ``token_usage`` from local-session-parser — token counts format when % unavailable.
    3. "unavailable" string — when neither source has fresh data.

    Returns the unavailable message when:
    - ``state`` is None (file missing/unreadable)
    - ``state`` is stale (older than QUOTA_STALE_THRESHOLD_HOURS) AND has no token_usage
    - ``rate_limits`` pct values are None and ``token_usage`` is absent or stale

    Format when poller data is available:
        CC usage: 5h 42% | 7d 15%. Resets 5h: May 15 4:10 PM ET / 7d: May 22 11:00 AM ET.

    Format when only local token counts are available (cookie expired):
        CC usage (local): today 269M | week 1.2B | 5h 42M tokens
        [cookie expired — quota % unavailable]

    Pure function: no side effects. All inputs are arguments.
    """
    _UNAVAILABLE = "CC usage data unavailable — poller may not have run yet."

    if state is None:
        return _UNAVAILABLE

    # Try rate_limits (poller / hook data) first — requires non-None pct values.
    try:
        rl = state.get("rate_limits", {})
        five_pct = rl.get("five_hour", {}).get("pct")
        seven_pct = rl.get("seven_day", {}).get("pct")
        five_resets_at = rl.get("five_hour", {}).get("resets_at")
        seven_resets_at = rl.get("seven_day", {}).get("resets_at")
        has_pct = five_pct is not None and seven_pct is not None
    except (KeyError, TypeError, AttributeError):
        has_pct = False

    if has_pct:
        # Staleness check only applies to the percentage-based path.
        if _is_quota_state_stale(state):
            has_pct = False  # fall through to token_usage check below
        else:
            def _fmt_reset(iso: str | None) -> str:
                """Format an ISO reset timestamp in the owner's configured timezone."""
                if not iso:
                    return "unknown"
                try:
                    return _format_iso_for_user(iso, fmt="%b %-d %-I:%M %p %Z")
                except Exception:
                    return iso[:16]  # fallback: truncated ISO

            five_reset_str = _fmt_reset(five_resets_at)
            seven_reset_str = _fmt_reset(seven_resets_at)
            return (
                f"CC usage: 5h {five_pct:.0f}% | 7d {seven_pct:.0f}%. "
                f"Resets — 5h: {five_reset_str} / 7d: {seven_reset_str}."
            )

    # Fallback: local token counts from session-file parser.
    # These use their own last_updated timestamp inside token_usage, independent
    # of the top-level state last_updated (which may be stale from the poller).
    token_usage = state.get("token_usage")
    if token_usage:
        token_last_updated = token_usage.get("last_updated")
        token_fresh = False
        if token_last_updated:
            try:
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                ts = _dt.fromisoformat(token_last_updated.replace("Z", "+00:00"))
                age = _dt.now(_tz.utc) - ts
                token_fresh = age <= _td(hours=QUOTA_STALE_THRESHOLD_HOURS)
            except Exception:
                token_fresh = True  # unparseable → assume fresh

        if token_fresh:
            return _format_token_usage_fallback(token_usage)

    return _UNAVAILABLE


def format_status_message(
    active_sessions: list[dict],
    quota_state: dict | None,
) -> str:
    """Format a system status snapshot for Telegram display.

    Assembles two lines from independently-sourced data:
    - Active agent count and IDs from active_sessions
    - CC usage percentage from quota_state (or unavailable)

    Pure function: all inputs are arguments; no file reads or MCP calls.

    Note: this previously also included a WOS queue-depth line; that was
    removed when the WOS/orchestration process layer was purged (2026-08-03).

    Example output:
        ◉ Agents: 2 running (task-a, task-b)
        ◉ CC usage: 5h 42% | 7d 15%. Resets — 5h: May 15 4:10 PM ET / 7d: May 22 11:00 AM ET.
    """
    # Active agents line
    agent_count = len(active_sessions)
    if agent_count == 0:
        agents_line = "◉ Agents: 0 running"
    else:
        agent_ids = [
            s.get("task_id") or s.get("id") or "?"
            for s in active_sessions
        ]
        agents_line = f"◉ Agents: {agent_count} running ({', '.join(agent_ids)})"

    # CC usage line
    quota_line = "◉ " + format_quota_message(quota_state)

    return "\n".join([agents_line, quota_line])


# ---------------------------------------------------------------------------
# Inline dispatcher command handlers (prose commands: status, usage, agents,
# inbox, debug, restart mcp/dispatcher)
#
# These handlers implement snag-reachable commands that execute directly on
# the dispatcher main thread without spawning a subagent. Each function is
# pure with respect to MCP calls — any MCP data (active_sessions, inbox msgs)
# must be gathered by the dispatcher before calling these functions.
# ---------------------------------------------------------------------------

# Path to the debug-enabled flag file. Touch to enable; unlink to disable.
_DEBUG_FLAG_PATH: Path = Path.home() / "lobster-workspace" / "data" / "debug-enabled"


def handle_usage() -> str:
    """Handle prose 'usage' command — inline CC quota read from state.json.

    Pure file read: reads cc-budget/state.json via read_quota_state() and
    formats the result using format_quota_message(). Adds session cost when
    available. Returns the unavailable message when the file is absent or stale.
    """
    state = read_quota_state()
    quota_msg = format_quota_message(state)
    if state:
        cost = state.get("session_cost_usd")
        if cost is not None:
            quota_msg += f"\nSession cost: ${cost:.2f}"
    return quota_msg


def handle_status(active_sessions: list[dict]) -> str:
    """Handle prose 'status' / 'health' command — inline system snapshot.

    Reads cc-budget/state.json directly (fast file read). active_sessions must
    be gathered by the dispatcher via get_active_sessions() before calling this
    function.

    Returns a 2-line status string covering agent count and CC usage. Prior to
    the 2026-08-03 process-layer slimdown this also included a WOS state line;
    that was dropped along with the WOS/orchestration subsystem.
    """
    quota_state = read_quota_state()

    agent_count = len(active_sessions)
    quota_msg = format_quota_message(quota_state)

    return "\n".join([
        f"Active agents: {agent_count}",
        quota_msg,
    ])


def handle_agents(active_sessions: list[dict]) -> str:
    """Handle prose 'agents' command — format active session list.

    active_sessions must be gathered by the dispatcher via get_active_sessions()
    before calling this function.
    """
    if not active_sessions:
        return "No active agents."
    lines = [f"Active agents ({len(active_sessions)}):"]
    for s in active_sessions:
        agent_id = s.get("task_id") or s.get("agent_id") or s.get("id") or "?"
        desc = s.get("description", "")
        lines.append(f"  • {agent_id}: {desc}")
    return "\n".join(lines)


def handle_inbox(msgs: list[dict], total_count: int) -> str:
    """Handle prose 'inbox' command — format queue depth and recent messages.

    msgs and total_count must be gathered by the dispatcher via check_inbox()
    and get_stats() before calling this function.
    """
    lines = [f"Inbox: {total_count} pending"]
    for m in (msgs or [])[:5]:
        preview = (m.get("text") or "")[:60].replace("\n", " ")
        if preview:
            lines.append(f"  • {preview}")
    return "\n".join(lines)


def handle_debug(on: bool) -> str:
    """Handle 'debug on' / 'debug off' — toggle the debug-enabled flag file.

    Touches ~/lobster-workspace/data/debug-enabled to enable debug mode;
    unlinks it to disable. Returns a confirmation string.
    """
    try:
        if on:
            _DEBUG_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _DEBUG_FLAG_PATH.touch()
            return f"Debug mode enabled. Flag: `{_DEBUG_FLAG_PATH}`"
        else:
            if _DEBUG_FLAG_PATH.exists():
                _DEBUG_FLAG_PATH.unlink()
            return "Debug mode disabled. Flag file removed."
    except OSError as exc:
        return f"Debug toggle failed: {exc}"


def handle_restart_mcp() -> str:
    """Handle 'restart mcp' — return the inline ACK message.

    The dispatcher sends this text as an immediate reply, then spawns a subagent
    to run ~/lobster/scripts/restart-mcp.sh --no-wait. The subagent performs
    the actual restart; the dispatcher reconnects automatically.

    Returns the ACK text to send before the subagent is spawned.
    """
    return (
        "MCP restart initiated. The service will restart in ~5 seconds. "
        "Reconnection is automatic — you may see a brief gap in responsiveness."
    )


def handle_restart_dispatcher() -> str:
    """Handle 'restart dispatcher' — return manual restart instructions.

    The Claude Code process cannot restart itself. This function returns
    the instructions Dan must follow to restart the dispatcher manually.
    """
    return (
        "The dispatcher (Claude Code process) cannot restart itself.\n\n"
        "To restart:\n"
        "1. Open a new terminal on the Lobster host\n"
        "2. Run: ~/lobster/scripts/claude-persistent.sh\n"
        "3. The new session will pick up from the inbox queue automatically."
    )


def handle_usage_full() -> str:
    """Handle 'usage full' — return the spawning acknowledgement.

    This command is NOT snag-reachable by design: it requires a subagent.
    Returns the ack text to send before spawning the usage-report subagent.
    The dispatcher is responsible for the actual Task spawn with the appropriate
    prompt (run usage-report.sh --format full, or fall back to state.json).
    """
    return "Spawning usage report agent..."


# ---------------------------------------------------------------------------
# /config — user bootup file access from Telegram (issue #1018)
# ---------------------------------------------------------------------------

# Allowlist of user config files accessible via /config commands.
# System files in .claude/ are not included — those are protected.
_USER_CONFIG_DIR: Path = Path.home() / "lobster-user-config" / "agents"
_USER_CONFIG_FILENAMES: tuple[str, ...] = (
    "user.base.bootup.md",
    "user.base.context.md",
    "user.dispatcher.bootup.md",
    "user.subagent.bootup.md",
    "system-audit.context.md",
    "user.development.md",
    "user.epistemic.md",
)

# Telegram message size limit (chars). Content beyond this is chunked.
_TELEGRAM_CHAR_LIMIT: int = 4000


def _config_file_path(filename: str) -> Path | None:
    """Return the resolved path for a user config file, or None if not allowed."""
    # Strip leading path components — accept bare filename or agents/filename
    name = Path(filename).name
    if name not in _USER_CONFIG_FILENAMES:
        return None
    p = _USER_CONFIG_DIR / name
    return p if p.exists() else None


def handle_config_list() -> str:
    """Return a formatted list of user config files with line counts."""
    lines: list[str] = ["User config files in ~/lobster-user-config/agents/:", ""]
    found = False
    for name in _USER_CONFIG_FILENAMES:
        p = _USER_CONFIG_DIR / name
        if p.exists():
            try:
                line_count = len(p.read_text(encoding="utf-8").splitlines())
            except OSError:
                line_count = 0
            lines.append(f"  {name} ({line_count} lines)")
            found = True
    if not found:
        lines.append("  (no user config files found)")
    return "\n".join(lines)


def handle_config_read(filename: str) -> tuple[str, bool]:
    """Read a user config file and return (text, needs_chunking).

    Returns (error_message, False) if the file is not found or not allowed.
    Returns (content, True) if content exceeds _TELEGRAM_CHAR_LIMIT.
    Returns (content, False) otherwise.
    """
    p = _config_file_path(filename)
    if p is None:
        name = Path(filename).name
        if name not in _USER_CONFIG_FILENAMES:
            return (
                f"Not allowed: '{name}' is not in the user config allowlist.\n"
                "Use /config list to see available files.",
                False,
            )
        return (f"File not found: '{name}' (may not exist yet).", False)

    try:
        content = p.read_text(encoding="utf-8")
    except OSError as exc:
        return (f"Could not read '{p.name}': {exc}", False)

    needs_chunking = len(content) > _TELEGRAM_CHAR_LIMIT
    return (content, needs_chunking)


def handle_config_search(query: str) -> str:
    """Search for a term across all user config files.

    Returns matching lines with filename and line number, formatted for Telegram.
    """
    if not query or not query.strip():
        return "Usage: /config search <query>"

    query = query.strip()
    results: list[str] = []
    matched_files = 0

    for name in _USER_CONFIG_FILENAMES:
        p = _USER_CONFIG_DIR / name
        if not p.exists():
            continue
        try:
            file_results: list[str] = []
            for lineno, line in enumerate(
                p.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if query.lower() in line.lower():
                    # Truncate long lines for Telegram readability
                    display = line.rstrip()
                    if len(display) > 120:
                        display = display[:117] + "..."
                    file_results.append(f"  L{lineno}: {display}")
            if file_results:
                results.append(f"{name}:")
                results.extend(file_results)
                matched_files += 1
        except OSError:
            continue

    if not results:
        return f"No matches for '{query}' in user config files."

    header = f"Search results for '{query}' ({matched_files} file(s)):"
    body = "\n".join(results)
    full = f"{header}\n\n{body}"

    # Truncate if over limit, with a note
    if len(full) > _TELEGRAM_CHAR_LIMIT:
        truncated = full[: _TELEGRAM_CHAR_LIMIT - 60]
        full = truncated + f"\n\n... (truncated, {len(full)} chars total)"

    return full


def handle_config_append(filename: str, text: str) -> str:
    """Append text to a user config file.

    Returns a confirmation string or an error message.
    """
    if not text or not text.strip():
        return "Usage: /config append <filename> <text>"

    p = _config_file_path(filename)
    if p is None:
        name = Path(filename).name
        if name not in _USER_CONFIG_FILENAMES:
            return (
                f"Not allowed: '{name}' is not in the user config allowlist.\n"
                "Use /config list to see available files."
            )
        # File doesn't exist yet — create it
        p = _USER_CONFIG_DIR / name

    try:
        _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            # Ensure we start on a new line
            f.write(f"\n{text.strip()}\n")
        # Return a confirmation with the last 200 chars of the file for verification
        content = p.read_text(encoding="utf-8")
        tail = content[-200:].strip()
        return f"Appended to {p.name}.\n\nTail:\n{tail}"
    except OSError as exc:
        return f"Could not write to '{p.name}': {exc}"
