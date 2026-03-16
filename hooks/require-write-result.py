#!/usr/bin/env python3
"""
Stop hook: ensure subagents call write_result before exiting.
Injects a reminder if write_result was not called during the session.

Dispatcher sessions (detected via session_role.is_dispatcher()) are exempt —
the dispatcher never calls write_result, so the check only applies to subagents.

When write_result was called, this hook also marks the session completed in
agent_sessions.db synchronously — without relying on the unreliable server-side
auto-unregister path in inbox_server.py.
"""
import json
import sys
from pathlib import Path

# Import shared session role utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher, get_session_id


def _mark_session_completed(session_id: str) -> None:
    """Mark the agent session completed in agent_sessions.db.

    Best-effort: any failure is silently swallowed so the hook always exits 0.
    Uses session_store.session_end() which matches on id OR task_id and is
    idempotent — safe to call even if inbox_server already updated the row.
    """
    try:
        hooks_dir = Path(__file__).parent
        repo_src = hooks_dir.parent / "src"
        sys.path.insert(0, str(repo_src))
        from agents.session_store import session_end  # noqa: PLC0415
        session_end(
            id_or_task_id=session_id,
            status="completed",
            result_summary="Completed via SubagentStop hook",
        )
    except Exception:
        pass  # DB update is best-effort; never block exit


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # If we can't read transcript, don't block

    # Dispatcher sessions are exempt — skip the write_result check.
    # session_role.is_dispatcher() uses the marker file as primary signal and
    # the transcript (which is present in Stop hooks) as fallback.
    if is_dispatcher(data):
        sys.exit(0)

    transcript = data.get("transcript", [])

    # Collect all tool call items so we can inspect both name and input.
    all_tool_use_items = []
    for msg in transcript:
        if isinstance(msg, dict):
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        all_tool_use_items.append(item)

    tool_call_names = [item.get("name", "") for item in all_tool_use_items]

    if "mcp__lobster-inbox__write_result" in tool_call_names:
        # write_result was called — verify it was called with a non-null chat_id.
        # chat_id=0 is explicitly allowed: it is the dispatcher system route for
        # background agents that were spawned without a user chat_id.
        write_result_items = [
            item for item in all_tool_use_items
            if item.get("name") == "mcp__lobster-inbox__write_result"
        ]
        valid_calls = [
            item for item in write_result_items
            if item.get("input", {}).get("chat_id") is not None
        ]
        if valid_calls:
            # At least one valid call found — mark session and allow exit.
            session_id = get_session_id(data)
            if session_id:
                _mark_session_completed(session_id)
            sys.exit(0)
        else:
            # write_result was called but chat_id was None in every call —
            # the MCP server rejected the call and the result was not stored.
            print(
                "STOP: write_result was called but chat_id was None in every call. "
                "The MCP server rejects write_result without a chat_id, so your result "
                "was not delivered. "
                "Call write_result again with a valid chat_id. "
                "If you were not given a user chat_id, use chat_id=0 — that is the "
                "dispatcher system route for background agents with no user context."
            )
            sys.exit(2)

    # Subagent finished without calling write_result — block exit
    print(
        "STOP: You must call mcp__lobster-inbox__write_result before finishing. "
        "The dispatcher is waiting for your result. "
        "If the task failed, report the failure — but you must call write_result. "
        "Call it now with your findings, then you may exit."
    )
    sys.exit(2)  # Exit 2 to hard-block the session from terminating


if __name__ == "__main__":
    main()
