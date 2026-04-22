#!/usr/bin/env python3
"""PreToolUse hook: blocks Agent calls from the dispatcher where the prompt lacks
dispatch template fields.

Every subagent spawned by the dispatcher must include both:
  - 'Minimum viable output: <deliverable>'
  - 'Boundary: do not <X>'

These fields enforce the Dispatch template gate from the Tier-1 Gate Register in
CLAUDE.md, ensuring the dispatcher always scopes its subagents explicitly. This
survives context compaction because the enforcement is mechanical, not advisory.

Only fires for Agent tool calls from the dispatcher. Subagents are exempt.

Exit codes:
  0 — tool is not Agent, session is a subagent, or both template fields are present
  2 — hard block: dispatcher called Agent without required dispatch template fields
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher_session

REQUIRED_FIELDS = [
    "Minimum viable output:",
    "Boundary:",
]

try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

if tool_name != "Agent":
    sys.exit(0)

if not is_dispatcher_session(data):
    sys.exit(0)

prompt = tool_input.get("prompt", "")

missing = [field for field in REQUIRED_FIELDS if field not in prompt]

if not missing:
    sys.exit(0)

missing_list = ", ".join(f"'{f}'" for f in missing)
print(
    f"BLOCKED: Agent prompt missing dispatch template field(s): {missing_list}.\n"
    "Every subagent call must include 'Minimum viable output: <deliverable>' and\n"
    "'Boundary: do not <X>' in the prompt body.\n"
    "See Tier-1 Gate Register in CLAUDE.md (Dispatch template gate).",
    file=sys.stderr,
)
sys.exit(2)
