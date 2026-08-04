#!/usr/bin/env python3
"""PreToolUse hook: blocks the dispatcher from calling Agent without background intent.

The Lobster dispatcher runs in an infinite message-processing loop. A foreground
Agent call blocks the dispatcher for the full duration of the agent — potentially
minutes — while incoming messages queue up unprocessed and health-check pings go
unanswered.

This hook enforces the 7-second rule as a hard constraint for the dispatcher.
Subagents are exempt: they may legitimately spawn nested agents synchronously
when the result is needed to decide the next step.

Note: Claude Code has used both "Agent" and "Task" as the tool name for spawning
subagents across versions. Both are treated identically.

## Background intent signals (either is sufficient)

1. **tool_input["run_in_background"] is True** — the classic signal. Available
   when the Agent tool schema includes the field. In Claude Code 2.1.123+ with
   some model variants, the schema has additionalProperties: false and omits
   run_in_background, causing the client to strip the field before the hook
   sees it. This signal is checked first for backward compatibility.

2. **YAML frontmatter `background: true` in prompt** — the schema-safe workaround
   for issue #1872. The dispatcher includes `background: true` in the YAML
   frontmatter block at the top of the prompt. This survives schema validation
   because it's part of the `prompt` field (which is always present in the schema).

   Example prompt structure:
       ---
       task_id: fix-pr-42
       chat_id: 12345
       source: telegram
       background: true
       ---

       <actual task instructions>

   The hook checks for a line matching `background: true` or `background: True`
   within the frontmatter block (case-insensitive on the value).

## Agent-channel invariant (fail-closed, applies to dispatcher AND subagents)

Independent of the background-intent checks above, this hook also enforces a
structural guarantee for the agent channel (`source="local-claude"` — a local
Claude Code session talking to the dispatcher over SSH, see
docs/agent-channel.md and the agent-channel protocol spec).

A subagent delegated to answer a `source: local-claude` request is the only
thing carrying that request's identity (`request_id`) into the background —
the dispatcher's normal `write_result` relay path does not carry `request_id`,
so a subagent spawned without one has no way to address its reply to
`agent-replies/<request_id>.json`. If the frontmatter declares
`source: local-claude` without a filesystem-safe `request_id`, this hook
blocks the dispatch outright (agent-channel protocol spec principles 3 and 6)
rather than let a subagent start work it cannot deliver. This check fires
regardless of dispatcher/subagent role or background intent — a malformed
agent-channel delegation is never allowed to run.

Exit codes:
  0 — tool is not Agent/Task, background intent is signalled, or session is a subagent
  2 — hard block: dispatcher called Agent/Task without background intent, OR
      frontmatter declares source: local-claude without a valid request_id
"""
import json
import re
import sys
from pathlib import Path

# Import the shared dispatcher/subagent detection utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher

# Tool names used to spawn subagents across CC versions.
AGENT_TOOL_NAMES = {"Agent", "Task"}

# Values accepted as truthy for `background:` in the YAML frontmatter.
# Covers both YAML true/false and Python-style True/False that Claude often writes.
_BACKGROUND_TRUE_VALUES = frozenset({"true", "yes", "1"})


def _has_background_true_in_frontmatter(prompt: str) -> bool:
    """Return True if the prompt has YAML frontmatter with `background: true`.

    Accepts both YAML-style `true` and Python-style `True` (case-insensitive).
    Returns False if:
    - No frontmatter block is present
    - The frontmatter has no `background` key
    - The `background` value is anything other than a truthy string

    A frontmatter block is the `---` ... `---` section at the start of the prompt
    (after stripping leading whitespace).
    """
    prompt = prompt.lstrip()
    if not prompt.startswith("---"):
        return False

    # Find the closing --- of the frontmatter block.
    rest = prompt[3:]
    # Match closing delimiter: must be followed by newline or end-of-string
    m = re.search(r"\n---(?:\n|$)", rest)
    if m is None:
        return False
    end = m.start()

    block = rest[:end]
    for line in block.splitlines():
        line = line.strip()
        if re.match(r"^background\s*:", line, re.IGNORECASE):
            _, _, value = line.partition(":")
            return value.strip().lower() in _BACKGROUND_TRUE_VALUES

    return False


# ---------------------------------------------------------------------------
# Agent-channel invariant: source: local-claude requires a valid request_id
# ---------------------------------------------------------------------------
# request_id doubles as a filesystem path component (agent-replies/<request_id>.json)
# downstream in src/mcp/reliability.py's sanitize_request_id(). The pattern/length
# below are duplicated (not imported) so this hook stays a dependency-free,
# single-file script — consistent with the rest of this file. Keep in sync with
# src/mcp/reliability.py::_REQUEST_ID_PATTERN / _REQUEST_ID_MAX_LEN.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_REQUEST_ID_MAX_LEN = 128


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _frontmatter_fields(prompt: str) -> dict:
    """Extract simple scalar key:value pairs from the YAML frontmatter block.

    Minimal parser mirroring hooks/auto-register-agent.py's
    _parse_yaml_frontmatter. Duplicated (not imported) for the same
    dependency-free-script reason as _REQUEST_ID_PATTERN above. Keys are
    lower-cased; values are returned raw (quote-stripped by callers as needed).
    """
    prompt = prompt.lstrip()
    if not prompt.startswith("---"):
        return {}
    rest = prompt[3:]
    m = re.search(r"\n---(?:\n|$)", rest)
    if m is None:
        return {}
    block = rest[: m.start()]
    fields: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()
    return fields


def _local_claude_request_id_error(prompt: str) -> str | None:
    """Return a block-reason string for an invalid local-claude dispatch, else None.

    Returns None when the prompt's frontmatter does not declare
    `source: local-claude`, or when it does and also carries a valid
    `request_id` — i.e. there is nothing to block.
    """
    fields = _frontmatter_fields(prompt)
    source = _strip_quotes(fields.get("source", "")).lower()
    if source != "local-claude":
        return None

    request_id = _strip_quotes(fields.get("request_id", ""))
    if not request_id:
        return (
            "frontmatter declares source: local-claude but has no request_id. "
            "A local-claude-sourced subagent cannot address its reply without "
            "one (agent-channel protocol spec, principle 3 — per-request identity)."
        )
    if len(request_id) > _REQUEST_ID_MAX_LEN or not _REQUEST_ID_PATTERN.match(request_id):
        return (
            f"frontmatter request_id {request_id!r} is not filesystem-safe — only "
            "letters, digits, '-', and '_' are allowed, max "
            f"{_REQUEST_ID_MAX_LEN} characters (agent-channel protocol spec, "
            "principle 6 — filesystem-safe request identity)."
        )
    return None


data = json.load(sys.stdin)
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool not in AGENT_TOOL_NAMES:
    sys.exit(0)

# Agent-channel invariant (fail-closed, unconditional — applies to dispatcher AND
# subagents alike, independent of background intent). See module docstring.
_local_claude_prompt = inp.get("prompt", "")
_local_claude_error = _local_claude_request_id_error(_local_claude_prompt)
if _local_claude_error:
    print(
        f"BLOCKED: {_local_claude_error}\n\n"
        "Include request_id in the frontmatter block, e.g.:\n\n"
        "  ---\n"
        "  task_id: <slug>\n"
        "  chat_id: local-claude\n"
        "  source: local-claude\n"
        "  request_id: <request_id from the inbound local-claude message>\n"
        "  background: true\n"
        "  ---\n\n"
        "See docs/agent-channel.md.",
        file=sys.stderr,
    )
    sys.exit(2)

# Signal 1: run_in_background in tool_input (available when schema includes the field).
if inp.get("run_in_background") is True:
    sys.exit(0)

# Signal 2: background: true in prompt frontmatter (schema-safe workaround for #1872).
# When the Agent schema strips run_in_background (additionalProperties: false),
# the dispatcher signals background intent via the prompt frontmatter instead.
prompt = inp.get("prompt", "")
if _has_background_true_in_frontmatter(prompt):
    sys.exit(0)

# Only enforce for the dispatcher. Subagents may call Agent synchronously.
if not is_dispatcher(data):
    sys.exit(0)

print(
    "BLOCKED: Dispatcher called Agent without background intent. "
    "The Agent tool schema may strip run_in_background before the hook sees it "
    "(issue #1872). Include `background: true` in the YAML frontmatter of the prompt:\n\n"
    "  ---\n"
    "  task_id: <slug>\n"
    "  chat_id: <id>\n"
    "  source: telegram\n"
    "  background: true\n"
    "  ---\n\n"
    "This ensures background intent is visible to the hook regardless of schema "
    "validation. The result will be delivered via write_result.",
    file=sys.stderr,
)
sys.exit(2)
